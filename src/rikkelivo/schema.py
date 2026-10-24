"""Schema resources: read the vendored Room/Drift dumps and build databases.

The resources under ``rikkelivo/schemas`` are the exact schema exports of the
pinned upstream releases (see ``schemas/README.md``).  Nothing here writes an
approximate DDL: a database we create is byte-for-byte the set of statements
the application itself validates against, and a database we read must match
one of those known shapes exactly unless it declares forward compatibility.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

RH_VERSIONS: tuple[int, ...] = (24, 25, 26, 27, 28, 29, 30, 31)
DRIFT_VERSIONS: tuple[int, ...] = (1, 2, 3)

RH_LATEST = 31
RH_LEGACY = 24
DRIFT_LATEST = 3

#: The app's search index and its FTS5 shadow tables.  They are not business
#: data: an output database rebuilds them, and validation ignores them.
RH_FTS_TABLE = "message_fts"
RH_FTS_SHADOW_TABLES: frozenset[str] = frozenset({
    "message_fts_data",
    "message_fts_idx",
    "message_fts_content",
    "message_fts_docsize",
    "message_fts_config",
})
RH_BOOKKEEPING_TABLES: frozenset[str] = frozenset({
    "room_master_table",
    "android_metadata",
    "sqlite_sequence",
})
RH_FTS_COLUMNS: tuple[str, ...] = (
    "text",
    "node_id",
    "message_id",
    "conversation_id",
    "title",
    "update_at",
)


class SchemaError(ValueError):
    """The file does not match any documented schema shape."""


def _schema_dir() -> Any:
    try:
        base = resources.files("rikkelivo").joinpath("schemas")
    except (ModuleNotFoundError, AttributeError):  # pragma: no cover - frozen fallback
        base = Path(__file__).resolve().parent / "schemas"
    if isinstance(base, Path):
        return base
    if base.is_dir():
        return base
    # Nuitka / unusual loaders: fall back to the directory next to this file.
    return Path(__file__).resolve().parent / "schemas"


def _load_json(relative: str) -> dict[str, Any]:
    path = _schema_dir().joinpath(relative)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:  # pragma: no cover - packaging bug
        raise SchemaError(f"缺失 schema 资源 {relative}: {exc}") from exc
    return json.loads(text)


@dataclass(frozen=True)
class RoomField:
    column: str
    affinity: str
    not_null: bool
    default: str | None


@dataclass(frozen=True)
class RoomIndex:
    name: str
    sql: str
    unique: bool
    columns: tuple[str, ...]


@dataclass(frozen=True)
class RoomEntity:
    table: str
    create_sql: str
    fields: tuple[RoomField, ...]
    primary_key: tuple[str, ...]
    auto_generate: bool
    indices: tuple[RoomIndex, ...]
    foreign_keys: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RoomSchema:
    version: int
    identity: str
    entities: tuple[RoomEntity, ...]
    setup_queries: tuple[str, ...]

    @property
    def tables(self) -> dict[str, RoomEntity]:
        return {entity.table: entity for entity in self.entities}

    def table_columns(self) -> dict[str, tuple[str, ...]]:
        return {entity.table: tuple(f.column for f in entity.fields)
                for entity in self.entities}


@functools.lru_cache(maxsize=None)
def load_room(version: int) -> RoomSchema:
    if version not in RH_VERSIONS:
        raise SchemaError(f"未知的 RikkaHub schema 版本: {version}")
    data = _load_json(f"rikkahub/room_{version}.json")
    database = data["database"]
    if int(database["version"]) != version:
        raise SchemaError("RikkaHub schema 资源版本不一致")
    entities = []
    for raw in database["entities"]:
        entities.append(RoomEntity(
            table=raw["tableName"],
            create_sql=raw["createSql"],
            fields=tuple(RoomField(
                column=field["columnName"],
                affinity=field["affinity"],
                not_null=bool(field.get("notNull", False)),
                default=field.get("defaultValue"),
            ) for field in raw["fields"]),
            primary_key=tuple(raw.get("primaryKey", {}).get("columnNames", ())),
            auto_generate=bool(raw.get("primaryKey", {}).get("autoGenerate", False)),
            indices=tuple(RoomIndex(
                name=index["name"],
                sql=index["createSql"],
                unique=bool(index.get("unique", False)),
                columns=tuple(index.get("columnNames", ())),
            ) for index in raw.get("indices", ())),
            foreign_keys=tuple(raw.get("foreignKeys", ())),
        ))
    return RoomSchema(
        version=version,
        identity=str(database["identityHash"]),
        entities=tuple(entities),
        setup_queries=tuple(database.get("setupQueries", ())),
    )


def rh_schema_for_version(version: int) -> RoomSchema:
    return load_room(version)


def rh_identity(version: int) -> str:
    return load_room(version).identity


@dataclass(frozen=True)
class DriftSchema:
    version: int
    ddl: tuple[str, ...]
    table_columns: dict[str, tuple[str, ...]]
    index_names: frozenset[str]


def _split_sql_columns(create_sql: str, table: str) -> tuple[str, ...]:
    """Extract declared column names from one ``CREATE TABLE`` statement."""
    start = create_sql.find("(")
    end = create_sql.rfind(")")
    if start < 0 or end < 0:
        raise SchemaError(f"{table}: 无法解析 DDL")
    body = create_sql[start + 1:end]
    columns: list[str] = []
    depth = 0
    current = ""
    pieces: list[str] = []
    in_quote = False
    for char in body:
        if char == '"':
            in_quote = not in_quote
        if not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                pieces.append(current)
                current = ""
                continue
        current += char
    if current.strip():
        pieces.append(current)
    for piece in pieces:
        piece = piece.strip()
        upper = piece.upper()
        if upper.startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK",
                             "CONSTRAINT")):
            continue
        if piece.startswith('"'):
            closing = piece.find('"', 1)
            columns.append(piece[1:closing])
        else:
            columns.append(piece.split()[0])
    return tuple(columns)


@functools.lru_cache(maxsize=None)
def load_drift(version: int) -> DriftSchema:
    if version not in DRIFT_VERSIONS:
        raise SchemaError(f"未知的 Kelivo schema 版本: {version}")
    data = _load_json(f"kelivo/drift_{version}.json")
    ddl: list[str] = []
    table_columns: dict[str, tuple[str, ...]] = {}
    index_names: set[str] = set()
    for fixed in data.get("fixed_sql", ()):
        for statement in fixed.get("sql", ()):
            if statement.get("dialect") != "sqlite":
                continue
            sql = statement["sql"].strip().rstrip(";")
            if not sql:
                continue
            ddl.append(sql)
            upper = sql.upper()
            if upper.startswith("CREATE TABLE"):
                name = sql.split('"')[1] if '"' in sql else sql.split()[5]
                table_columns[name] = _split_sql_columns(sql, name)
            elif upper.startswith("CREATE INDEX") or upper.startswith(
                    "CREATE UNIQUE INDEX"):
                parts = sql.split()
                index_names.add(parts[parts.index("ON") - 1])
    if not ddl:
        raise SchemaError("Kelivo schema 资源为空")
    return DriftSchema(
        version=version,
        ddl=tuple(ddl),
        table_columns=table_columns,
        index_names=frozenset(index_names),
    )


def drift_schema_for_version(version: int) -> DriftSchema:
    return load_drift(version)


# ---------------------------------------------------------------------------
# FTS tokenizer capability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FtsProbe:
    fts5: bool
    simple: bool


@functools.lru_cache(maxsize=1)
def probe_fts() -> FtsProbe:
    """Probe this interpreter's SQLite for FTS5 and the app's ``simple`` tokenizer."""
    with tempfile.TemporaryDirectory(prefix="rikkelivo-fts-") as tmp:
        con = sqlite3.connect(str(Path(tmp) / "probe.db"))
        try:
            try:
                con.execute("CREATE VIRTUAL TABLE p USING fts5(x)")
            except sqlite3.OperationalError:
                return FtsProbe(fts5=False, simple=False)
            try:
                con.execute("CREATE VIRTUAL TABLE q USING fts5(x, tokenize='simple')")
            except sqlite3.OperationalError as exc:
                if "simple" not in str(exc) or "tokenizer" not in str(exc):
                    raise
                return FtsProbe(fts5=True, simple=False)
            return FtsProbe(fts5=True, simple=True)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Database creation
# ---------------------------------------------------------------------------

def rrh_fts_ddl(tokenizer: str) -> str:
    columns = ", ".join(
        column if column == "text" else f"{column} UNINDEXED"
        for column in RH_FTS_COLUMNS
    )
    return (f"CREATE VIRTUAL TABLE {RH_FTS_TABLE} USING fts5("
            f"{columns}, tokenize = '{tokenizer}')")


def create_room_database(path: Path, version: int) -> str | None:
    """Create an empty database with the exact Room schema of ``version``.

    Returns a tokenizer warning when the app's ``simple`` tokenizer is not
    available and the search index had to be rebuilt with ``unicode61``.
    """
    schema = load_room(version)
    probe = probe_fts()
    if not probe.fts5:
        raise SchemaError(
            "当前 SQLite 缺少 FTS5，无法创建 RikkaHub 的消息搜索索引")
    warning: str | None = None
    tokenizer = "simple"
    if not probe.simple:
        tokenizer = "unicode61"
        warning = ("消息搜索索引使用 unicode61 分词器（当前 SQLite 缺少 FTS5 "
                   "'simple'），检索分词行为与原应用不同")
    con = sqlite3.connect(str(path))
    try:
        for entity in schema.entities:
            con.execute(entity.create_sql.replace("${TABLE_NAME}", entity.table))
            for index in entity.indices:
                con.execute(index.sql.replace("${TABLE_NAME}", entity.table))
        for query in schema.setup_queries:
            con.execute(query.replace("${TABLE_NAME}", "room_master_table"))
        con.execute(rrh_fts_ddl(tokenizer))
        con.execute(
            "CREATE TABLE IF NOT EXISTS android_metadata (locale TEXT)")
        con.execute("INSERT INTO android_metadata (locale) VALUES (?)", ("en_US",))
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
    finally:
        con.close()
    return warning


def create_drift_database(path: Path, version: int) -> None:
    """Create an empty database with the exact Drift schema of ``version``."""
    schema = load_drift(version)
    con = sqlite3.connect(str(path))
    try:
        for statement in schema.ddl:
            con.execute(statement)
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _table_info(con: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    return tuple(con.execute(f'PRAGMA table_info("{table}")'))


def _sqlite_tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")
    }


def _sqlite_indices(con: sqlite3.Connection) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for table in _sqlite_tables(con):
        for row in con.execute(f'PRAGMA index_list("{table}")'):
            result[str(row[1])] = bool(row[2])
    return result


def validate_room_database(con: sqlite3.Connection, version: int) -> tuple[str, ...]:
    """Validate a staged RikkaHub database; returns warnings, raises on mismatch."""
    schema = load_room(version)
    user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if user_version != version:
        raise SchemaError(
            f"RikkaHub 数据库 user_version={user_version}，与声明的 {version} 不符")
    tables = _sqlite_tables(con)
    expected = set(schema.tables)
    allowed_extra = set(RH_FTS_SHADOW_TABLES) | set(RH_BOOKKEEPING_TABLES) | {
        RH_FTS_TABLE}
    missing = expected - tables
    if missing:
        raise SchemaError(
            "RikkaHub 数据库缺少表: " + ", ".join(sorted(missing)))
    unknown = tables - expected - allowed_extra
    if unknown:
        raise SchemaError(
            "RikkaHub 数据库包含未知表: " + ", ".join(sorted(unknown)))

    for table, entity in schema.tables.items():
        info = _table_info(con, table)
        actual = [(str(row[1]), str(row[2]).upper(), bool(row[3]), int(row[5]))
                  for row in info]
        wanted = [(field.column, field.affinity.upper(), field.not_null,
                   index + 1 if field.column in entity.primary_key else 0)
                  for index, field in enumerate(entity.fields)]
        if actual != wanted:
            raise SchemaError(f"RikkaHub 表 {table} 的列与 {version} 版本 schema 不符")
        defaults = {str(row[1]): row[4] for row in info}
        for field in entity.fields:
            expected_default = field.default
            if expected_default is None:
                continue
            if defaults.get(field.column) != expected_default:
                raise SchemaError(f"RikkaHub 表 {table}.{field.column} 默认值不符")

    indices = _sqlite_indices(con)
    for entity in schema.entities:
        for index in entity.indices:
            if index.name not in indices:
                raise SchemaError(
                    f"RikkaHub 数据库缺少索引 {index.name}")
            if indices[index.name] != index.unique:
                raise SchemaError(f"RikkaHub 索引 {index.name} 唯一性不符")
            columns = tuple(str(row[2]) for row in
                            con.execute(f'PRAGMA index_info("{index.name}")'))
            if columns != index.columns:
                raise SchemaError(f"RikkaHub 索引 {index.name} 列不符")

    expected_fk = {
        (entity.table, fk["table"], tuple(fk["columns"]),
         tuple(fk["referencedColumns"]), str(fk.get("onDelete", "NO ACTION")).upper())
        for entity in schema.entities for fk in entity.foreign_keys
    }
    actual_fk = set()
    for table in expected:
        for row in con.execute(f'PRAGMA foreign_key_list("{table}")'):
            actual_fk.add((table, str(row[2]), (str(row[3]),), (str(row[4]),),
                           str(row[6]).upper()))
    missing_fk = expected_fk - actual_fk
    if missing_fk:
        raise SchemaError("RikkaHub 数据库外键定义与 schema 不符")

    warnings: list[str] = []
    identity: str | None = None
    if "room_master_table" in tables:
        row = con.execute(
            "SELECT identity_hash FROM room_master_table WHERE id = 42").fetchone()
        if row is not None and row[0]:
            identity = str(row[0])
    if identity is not None:
        if identity != schema.identity:
            raise SchemaError(
                f"RikkaHub Room identity {identity} 与 schema {version} 的 "
                f"{schema.identity} 不符")
    else:
        warnings.append(
            "RikkaHub 数据库缺少 room_master_table 的 identity_hash，"
            "已按完整已知结构校验并在输出中重建")
    return tuple(warnings)


def validate_drift_database(con: sqlite3.Connection, version: int) -> None:
    """Validate a staged Kelivo/Cuplivo SQLite database against a known schema."""
    schema = load_drift(version)
    user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if user_version != version:
        raise SchemaError(
            f"Kelivo 数据库 user_version={user_version}，与声明的 {version} 不符")
    tables = _sqlite_tables(con)
    expected = set(schema.table_columns)
    missing = expected - tables
    if missing:
        raise SchemaError("Kelivo 数据库缺少表: " + ", ".join(sorted(missing)))
    unknown = tables - expected
    if unknown:
        raise SchemaError("Kelivo 数据库包含未知表: " + ", ".join(sorted(unknown)))
    for table, columns in schema.table_columns.items():
        actual = tuple(str(row[1]) for row in _table_info(con, table))
        if actual != columns:
            raise SchemaError(
                f"Kelivo 表 {table} 的列与 schema {version} 不符")
    indices = _sqlite_indices(con)
    missing_indices = schema.index_names - set(indices)
    if missing_indices:
        raise SchemaError(
            "Kelivo 数据库缺少索引: " + ", ".join(sorted(missing_indices)))


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def read_rows(con: sqlite3.Connection, table: str,
              order_by: Iterable[str] = ()) -> Any:
    from rikkelivo.model import TableRows

    info = _table_info(con, table)
    columns = tuple(str(row[1]) for row in info)
    quoted = ", ".join(f'"{column}"' for column in columns)
    clauses = f' FROM "{table}"'
    order = [column for column in order_by if column in columns]
    if order:
        clauses += " ORDER BY " + ", ".join(f'"{column}"' for column in order)
    rows = [tuple(row) for row in con.execute(f"SELECT {quoted}{clauses}")]
    return TableRows(columns=columns, rows=rows)
