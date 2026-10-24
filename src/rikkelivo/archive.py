"""Reading, validating and staging a source backup archive.

Every rule here exists to make an untrusted ZIP safe to look at: member names
are canonicalised and checked, links and duplicate/casing collisions are
refused, extraction is streamed into a private staging directory with hard
size limits, and format detection never falls back from a broken structured
payload to a laxer one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from rikkelivo import model, schema
from rikkelivo.times import iso_to_ms
from rikkelivo.model import (
    KIND_CUPLIVO_JSONL,
    KIND_KELIVO_ARCHIVE,
    KIND_KELIVO_JSON,
    KIND_RH_SQLITE,
    KIND_SETTINGS_ONLY,
)

#: Restore limits published by the Kelivo application itself.
MAX_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SETTINGS_BYTES = 1024 * 1024 * 1024
MAX_ENTRY_BYTES = 8 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024

ENCRYPTED_MAGIC = b"KELIVENC"

KELIVO_ASSET_ROOTS: tuple[str, ...] = (
    "upload", "images", "avatars", "fonts", "sessions", "skills", "workspaces",
)

RH_ASSET_ROOTS: tuple[str, ...] = (
    "upload", "fonts", "skills", "media_creation",
)

RH_MEMBERS: frozenset[str] = frozenset({
    "rikka_hub.db", "rikka_hub-wal", "rikka_hub.db-wal", "rikka_hub-shm",
    "rikka_hub.db-shm", "settings.json",
})

LEGACY_MEMBERS: frozenset[str] = frozenset({
    "settings.json", "chats.json", "deleted.json",
})
JSONL_MEMBERS: frozenset[str] = frozenset({
    "settings.json", "conversations.jsonl", "messages.jsonl",
    "chats_meta.json", "deleted.json",
})
ARCHIVE_MEMBERS: frozenset[str] = frozenset({
    "manifest.json", "settings.json", "database/kelivo.db",
})

MANIFEST_NAME = "manifest.json"
DATABASE_ENTRY = "database/kelivo.db"


class ArchiveError(ValueError):
    """The source is not a usable backup archive."""


class UnsupportedArchiveError(ArchiveError):
    """The archive is a documented shape this build deliberately refuses."""


# ---------------------------------------------------------------------------
# ZIP member safety
# ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^[A-Za-z]:($|/)")


def canonical_member_name(raw: str) -> str:
    if not raw or "\x00" in raw:
        raise ArchiveError("备份包含非法文件名")
    if "\\" in raw:
        raise ArchiveError(f"备份条目名包含反斜杠: {raw}")
    normalised = raw.replace("\\", "/")
    if normalised.startswith("/") or normalised.startswith("//") \
            or _DRIVE_RE.match(normalised):
        raise ArchiveError(f"备份包含绝对路径条目: {raw}")
    parts = normalised.split("/")
    if parts and parts[-1] == "":
        parts.pop()
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ArchiveError(f"备份包含非法路径条目: {raw}")
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _is_dir(info: zipfile.ZipInfo) -> bool:
    return info.is_dir() or info.filename.endswith("/")


def _check_member_set(infos: Iterable[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    """Validate names, collisions, links and file/dir prefix conflicts."""
    members: dict[str, zipfile.ZipInfo] = {}
    directories: set[str] = set()
    seen_case: set[str] = set()
    infos = list(infos)
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(f"备份条目数超过上限 {MAX_ENTRIES}")
    for info in infos:
        if info.flag_bits & 0x1:
            raise UnsupportedArchiveError("备份条目已加密，无法读取")
        if _is_symlink(info):
            raise ArchiveError(f"备份包含符号链接条目: {info.filename}")
        canonical = canonical_member_name(info.filename)
        folded = canonical.lower()
        if folded in seen_case:
            raise ArchiveError(f"备份包含重复或大小写冲突的条目: {canonical}")
        seen_case.add(folded)
        if _is_dir(info):
            directories.add(canonical)
        else:
            members[canonical] = info
    folded_files = {name.lower() for name in members}
    for name in list(members) + list(directories):
        parts = name.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index]).lower()
            if prefix in folded_files:
                raise ArchiveError(f"备份条目同时作为文件与目录: {name}")
    return members


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StagedMember:
    entry: str
    path: Path
    size: int
    sha256: str
    mtime: int


@dataclass(frozen=True)
class Staged:
    root: Path
    members: dict[str, StagedMember]

    def member(self, entry: str) -> StagedMember | None:
        return self.members.get(entry)

    def read_json(self, entry: str) -> Any:
        staged = self.members.get(entry)
        if staged is None:
            return None
        try:
            return json.loads(staged.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"{entry} 不是有效的 JSON: {exc}") from exc

    def read_bytes(self, entry: str, *, limit: int | None = None) -> bytes | None:
        staged = self.members.get(entry)
        if staged is None:
            return None
        if limit is not None and staged.size > limit:
            raise ArchiveError(f"{entry} 超过大小上限")
        return staged.path.read_bytes()

    def entries_under(self, root: str) -> list[StagedMember]:
        prefix = root.rstrip("/") + "/"
        return sorted((m for name, m in self.members.items()
                       if name.startswith(prefix)),
                      key=lambda m: m.entry)


_STAGE_INDEX = "_rikkelivo_staged.json"


#: RikkaHub's own WAL file name differs from the one SQLite discovers next to
#: a database called ``rikka_hub.db``; staging normalises it so opening the
#: staged copy replays committed transactions.
_STAGE_ALIASES = {"rikka_hub-wal": "rikka_hub.db-wal"}


def _stage_path(root: Path, entry: str) -> Path:
    parts = entry.split("/")
    return root.joinpath("payload", *parts)


def stage_archive(zf: zipfile.ZipFile, staging: Path) -> Staged:
    """Copy every non-directory member into ``staging`` under hard limits."""
    infos = _check_member_set(zf.infolist())
    payload_root = staging / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)
    total = 0
    staged: dict[str, StagedMember] = {}
    index_rows: dict[str, dict[str, Any]] = {}
    for entry, info in sorted(infos.items()):
        if info.file_size > MAX_ENTRY_BYTES:
            raise ArchiveError(f"{entry} 超过单文件大小上限")
        if entry in ("manifest.json",) and info.file_size > MAX_MANIFEST_BYTES:
            raise ArchiveError("manifest.json 超过大小上限")
        if entry == "settings.json" and info.file_size > MAX_SETTINGS_BYTES:
            raise ArchiveError("settings.json 超过大小上限")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ArchiveError("备份解压后总大小超过上限")
        destination = _stage_path(staging, _STAGE_ALIASES.get(entry, entry))
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with zf.open(info) as source, destination.open("wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ENTRY_BYTES:
                    raise ArchiveError(f"{entry} 实际大小超过上限")
                digest.update(chunk)
                target.write(chunk)
        if written != info.file_size:
            raise ArchiveError(f"{entry} 的实际大小与 ZIP 目录记录不符")
        mtime = int(_zipinfo_datetime(info).timestamp())
        staged[entry] = StagedMember(
            entry=entry, path=destination, size=written,
            sha256=digest.hexdigest(), mtime=mtime)
        index_rows[entry] = {
            "path": str(destination.relative_to(staging)),
            "size": written, "sha256": digest.hexdigest(), "mtime": mtime,
        }
    (staging / _STAGE_INDEX).write_text(
        json.dumps(index_rows, ensure_ascii=False), encoding="utf-8")
    return Staged(root=staging, members=staged)


def load_staged(staging: Path) -> Staged:
    index_path = staging / _STAGE_INDEX
    try:
        rows = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError("暂存目录缺少索引") from exc
    members = {
        entry: StagedMember(
            entry=entry,
            path=staging / row["path"],
            size=int(row["size"]),
            sha256=str(row["sha256"]),
            mtime=int(row["mtime"]),
        )
        for entry, row in rows.items()
    }
    return Staged(root=staging, members=members)


def _zipinfo_datetime(info: zipfile.ZipInfo):
    import datetime as _dt
    try:
        return _dt.datetime(*info.date_time)
    except ValueError:  # pragma: no cover - malformed DOS timestamp
        return _dt.datetime(1980, 1, 1)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectedFormat:
    kind: str
    family: str
    producer: str | None
    markers: tuple[str, ...]
    settings_only: bool = False


_CHAT_MARKERS = ("rikka_hub.db", "manifest.json", "chats_meta.json", "chats.json")


def detect_archive(zf: zipfile.ZipFile) -> DetectedFormat:
    """Identify the payload family from mutually exclusive markers."""
    infos = _check_member_set(zf.infolist())
    names = set(infos)
    markers = [marker for marker in _CHAT_MARKERS if marker in names]
    has_wal = "rikka_hub-wal" in names or "rikka_hub.db-wal" in names
    if has_wal and "rikka_hub.db" not in names:
        raise ArchiveError("RikkaHub 备份包含 WAL 文件但没有 rikka_hub.db")
    if "database/kelivo.db" in names and MANIFEST_NAME not in names:
        raise ArchiveError("备份包含 database/kelivo.db 但缺少 manifest.json")
    if len(markers) > 1:
        raise ArchiveError(
            "备份中同时存在多种聊天数据格式: " + " / ".join(markers))
    if "rikka_hub.db" in names:
        return DetectedFormat(KIND_RH_SQLITE, "rikkahub", "rikkahub",
                              ("rikka_hub.db",))
    if MANIFEST_NAME in names:
        manifest = _peek_manifest(infos[MANIFEST_NAME], zf)
        if manifest.get("format") != "kelivo-backup":
            raise ArchiveError("manifest.json 不是 kelivo-backup 格式")
        return DetectedFormat(KIND_KELIVO_ARCHIVE, "kelivo", None,
                              ("manifest.json",))
    if "chats_meta.json" in names:
        return DetectedFormat(KIND_CUPLIVO_JSONL, "kelivo", None,
                              ("chats_meta.json",))
    if "chats.json" in names:
        return DetectedFormat(KIND_KELIVO_JSON, "kelivo", None, ("chats.json",))
    # No chat payload at all: a settings-only archive whose family must be
    # established from the settings this file actually carries.
    settings = _peek_json(infos.get("settings.json"), zf)
    family, producer = _classify_settings(settings)
    return DetectedFormat(KIND_SETTINGS_ONLY, family, producer, ("settings.json",),
                          settings_only=True)


def _peek_json(info: zipfile.ZipInfo | None, zf: zipfile.ZipFile) -> dict[str, Any] | None:
    if info is None:
        return None
    if info.file_size > MAX_SETTINGS_BYTES:
        raise ArchiveError("settings.json 超过大小上限")
    try:
        with zf.open(info) as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"settings.json 损坏: {exc}") from exc
    if not isinstance(data, dict):
        raise ArchiveError("settings.json 不是 JSON 对象")
    return data


def _peek_manifest(info: zipfile.ZipInfo, zf: zipfile.ZipFile) -> dict[str, Any]:
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ArchiveError("manifest.json 超过大小上限")
    try:
        with zf.open(info) as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"manifest.json 损坏: {exc}") from exc
    if not isinstance(data, dict):
        raise ArchiveError("manifest.json 不是 JSON 对象")
    return data


def _classify_settings(settings: dict[str, Any] | None) -> tuple[str, str | None]:
    """Decide which family a settings-only archive belongs to."""
    if settings is None:
        raise UnsupportedArchiveError(
            "备份不含聊天数据也不含 settings.json，无法识别来源应用")
    rh_markers = [key for key in ("displaySetting", "providers", "assistants",
                                  "quickMessages", "modeInjections", "ttsProviders")
                  if key in settings]
    kl_markers = [key for key in settings if isinstance(key, str)
                  and key.endswith("_v1")]
    strong_rh = []
    for key in rh_markers:
        value = settings[key]
        if key == "displaySetting":
            strong_rh.append(key)
        elif value not in (None, [], {}, ""):
            strong_rh.append(key)
    if strong_rh and kl_markers:
        raise UnsupportedArchiveError(
            "settings.json 同时包含两种应用的设置字段，无法确定来源应用")
    if strong_rh:
        return "rikkahub", "rikkahub"
    if kl_markers:
        return "kelivo", None
    if rh_markers:
        # Empty mirror keys written by the old bridge are not family evidence.
        return "rikkahub", "rikkahub"
    raise UnsupportedArchiveError(
        "settings.json 缺少可识别的应用设置字段，信息不足以确定来源应用")


# ---------------------------------------------------------------------------
# Format validation
# ---------------------------------------------------------------------------

def validate_format(zf: zipfile.ZipFile, detected: DetectedFormat,
                    staging: Path) -> model.FormatInfo:
    staged = stage_archive(zf, staging)
    return _validate_staged(staged, detected)


def _validate_staged(staged: Staged, detected: DetectedFormat) -> model.FormatInfo:
    if detected.kind == KIND_RH_SQLITE:
        return _validate_rikkahub(staged)
    if detected.kind == KIND_KELIVO_ARCHIVE:
        return _validate_kelivo_archive(staged)
    if detected.kind == KIND_KELIVO_JSON:
        return _validate_kelivo_json(staged)
    if detected.kind == KIND_CUPLIVO_JSONL:
        return _validate_cuplivo_jsonl(staged)
    if detected.kind == KIND_SETTINGS_ONLY:
        return _validate_settings_only(staged, detected)
    raise ArchiveError(f"无法识别的备份类型: {detected.kind}")


def _check_roots(staged: "Staged | _PreviewStaged",
                 allowed_members: Iterable[str],
                 allowed_roots: Iterable[str], *, base: str = "JSON",
                 kind_label: str) -> None:
    roots = tuple(allowed_roots)
    for entry in staged.members:
        if entry in allowed_members:
            continue
        if any(entry.startswith(root + "/") for root in roots):
            continue
        raise ArchiveError(f"{kind_label} 备份包含不认识的条目: {entry}")


def _settings_from_staged(staged: Staged) -> tuple[bool, dict[str, Any]]:
    staged_member = staged.member("settings.json")
    if staged_member is None:
        return False, {}
    data = staged.read_json("settings.json")
    if not isinstance(data, dict):
        raise ArchiveError("settings.json 不是 JSON 对象")
    return True, data


# --- RikkaHub -------------------------------------------------------------

def _read_sqlite_user_version(path: Path) -> int:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ArchiveError(f"无法打开 SQLite 数据库: {exc}") from exc
    try:
        row = con.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()


def _validate_rikkahub(staged: Staged) -> model.FormatInfo:
    db = staged.member("rikka_hub.db")
    if db is None:
        raise ArchiveError("RikkaHub 备份缺少 rikka_hub.db")
    wal_names = [name for name in ("rikka_hub-wal", "rikka_hub.db-wal")
                 if staged.member(name) is not None]
    if len(wal_names) > 1:
        raise ArchiveError("RikkaHub 备份同时包含两种 WAL 文件名")
    _check_roots(staged, RH_MEMBERS, RH_ASSET_ROOTS, kind_label="RikkaHub")
    # SHM is a transient index rebuilt on first open; it is never restored.
    settings_present, settings = _settings_from_staged(staged)
    schema_version = _read_sqlite_user_version(db.path)
    if schema_version not in schema.RH_VERSIONS:
        raise UnsupportedArchiveError(
            f"RikkaHub 数据库 schema 版本 {schema_version} 未查证，拒绝导入")
    for entry in staged.members:
        parts = entry.split("/")
        root = parts[0]
        if root in ("upload", "fonts") and len(parts) != 2:
            raise ArchiveError(f"RikkaHub 的 {root}/ 只允许扁平文件: {entry}")
        if root == "media_creation" and schema_version < 26:
            raise ArchiveError(
                f"Room {schema_version} 不支持 media_creation/ 文件: {entry}")
    con = sqlite3.connect(str(db.path))
    try:
        try:
            schema.validate_room_database(con, schema_version)
        except schema.SchemaError as exc:
            raise ArchiveError(str(exc)) from exc
        from rikkelivo import rikkahub as _rikkahub
        _rikkahub.validate_database_content(con, schema_version)
        _ensure_sqlite_healthy(
            con, label="RikkaHub",
            tables=[entity.table for entity in
                    schema.load_room(schema_version).entities])
        conversation_count = int(con.execute(
            "SELECT COUNT(*) FROM ConversationEntity").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise ArchiveError(f"RikkaHub 数据库损坏: {exc}") from exc
    finally:
        con.close()
    scope = _rikkahub_scope(settings, staged, schema_version)
    producer = "rikkahub"
    return model.FormatInfo(
        family="rikkahub",
        producer=producer,
        kind=KIND_RH_SQLITE,
        format_version=None,
        schema_version=schema_version,
        settings_present=settings_present,
        database_present=True,
        scope=scope,
        selection_encoding=model.SELECTION_NODE_INDEX,
        selection_evidence=f"Room {schema_version}: message_node.select_index",
        app_version=None,
    )


def _ensure_sqlite_healthy(con: sqlite3.Connection, *, label: str,
                           tables: Iterable[str] | None = None) -> None:
    """Validate the ordinary tables and referential integrity.

    A full-database ``integrity_check`` is deliberately *not* a precondition
    for RikkaHub: its search index uses the application's private ``simple``
    FTS tokenizer, which this interpreter cannot load, and a whole-database
    check would fail on the virtual table while every business table is
    perfectly readable.  Business tables are therefore checked one by one.
    """
    if tables is not None:
        for table in tables:
            result = con.execute(f'PRAGMA integrity_check("{table}")').fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ArchiveError(f"{label} 表 {table} 完整性检查失败")
    problems = con.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise ArchiveError(f"{label} 数据库外键检查失败")


def _rikkahub_scope(settings: dict[str, Any], staged: "Staged | _PreviewStaged",
                    schema_version: int) -> frozenset[str]:
    scope = {"settings"}
    if staged.member("rikka_hub.db") is not None:
        scope.add("chats")
    for root in RH_ASSET_ROOTS:
        if staged.entries_under(root):
            if root == "skills":
                scope.add("skills")
            else:
                scope.add("files")
    for key, category in (("assistants", "assistants"),
                          ("providers", "providers"),
                          ("mcpServers", "mcp"),
                          ("lorebooks", "worldBooks"),
                          ("quickMessages", "quickPhrases"),
                          ("modeInjections", "instructions"),
                          ("searchServices", "searchServices"),
                          ("ttsProviders", "speechServices"),
                          ("asrProviders", "speechServices")):
        if settings.get(key):
            scope.add(category)
    if schema_version >= 28 and staged.member("rikka_hub.db") is not None:
        scope.add("memories")
    if staged.entries_under("skills"):
        scope.add("skills")
    return frozenset(scope)


# --- Kelivo native archive ------------------------------------------------

def _validate_kelivo_archive(staged: Staged) -> model.FormatInfo:
    manifest = staged.read_json(MANIFEST_NAME)
    if not isinstance(manifest, dict):
        raise ArchiveError("manifest.json 不是 JSON 对象")
    if manifest.get("format") != "kelivo-backup":
        raise ArchiveError("manifest.json 不是 kelivo-backup 格式")
    format_version = manifest.get("formatVersion")
    if not isinstance(format_version, int) or isinstance(format_version, bool):
        raise ArchiveError("manifest.json 缺少 formatVersion")
    if format_version in (2, schema.DRIFT_LATEST):
        pass
    elif format_version > schema.DRIFT_LATEST:
        declared = manifest.get("minimumReadableFormatVersion")
        if not isinstance(declared, int) or declared > schema.DRIFT_LATEST:
            raise UnsupportedArchiveError(
                f"archive 版本 {format_version} 无法被当前版本读取")
    else:
        raise UnsupportedArchiveError(
            f"archive 版本 {format_version} 未查证，拒绝导入")

    payload_kind = manifest.get("payloadKind")
    include_chats = manifest.get("includeChats")
    include_files = manifest.get("includeFiles")
    if not isinstance(payload_kind, str) or not isinstance(include_chats, bool) \
            or not isinstance(include_files, bool) \
            or not isinstance(manifest.get("appVersion"), str) \
            or not isinstance(manifest.get("createdAtUtc"), str) \
            or manifest.get("secretsIncluded") is not True:
        raise ArchiveError("manifest.json 字段不完整")
    if format_version >= schema.DRIFT_LATEST and "scope" not in manifest:
        raise ArchiveError("archive 3 备份缺少 scope")
    scope = _native_scope(manifest, format_version)
    business_ids = manifest.get("businessEntityRowIds")
    if business_ids is not None:
        if not isinstance(business_ids, dict):
            raise ArchiveError("manifest.json 的 businessEntityRowIds 无效")
        for key, value in business_ids.items():
            if not isinstance(key, str) or not isinstance(value, list) \
                    or any(not isinstance(item, str) for item in value):
                raise ArchiveError("manifest.json 的 businessEntityRowIds 无效")

    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, dict) or not raw_entries:
        raise ArchiveError("manifest.json 缺少 entries")
    declared: dict[str, tuple[int, str]] = {}
    for name, meta in raw_entries.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            raise ArchiveError("manifest.json 的条目元数据无效")
        canonical = canonical_member_name(name)
        if canonical != name or name in (MANIFEST_NAME,):
            raise ArchiveError(f"manifest.json 中的条目名非法: {name}")
        size = meta.get("bytes")
        digest = meta.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 \
                or not isinstance(digest, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArchiveError(f"manifest.json 条目元数据无效: {name}")
        declared[name] = (size, digest)

    unknown_entries: list[str] = []
    for name in declared:
        if name in ARCHIVE_MEMBERS:
            continue
        if any(name.startswith(root + "/") for root in KELIVO_ASSET_ROOTS):
            if not include_files:
                raise ArchiveError(f"manifest 声明包含文件但 includeFiles 为 false: {name}")
            continue
        if format_version > schema.DRIFT_LATEST:
            unknown_entries.append(name)
            continue
        raise ArchiveError(f"备份包含不认识的根条目: {name}")
    if "settings.json" not in declared:
        raise ArchiveError("manifest 缺少 settings.json")
    for name in declared:
        member = staged.member(name)
        size, digest = declared[name]
        if member is None:
            raise ArchiveError(f"manifest 声明的文件缺失: {name}")
        if member.size != size:
            raise ArchiveError(f"{name} 的实际大小与 manifest 不符")
        if member.sha256 != digest:
            raise ArchiveError(f"{name} 的 SHA256 与 manifest 不符")
    for name in staged.members:
        if name == MANIFEST_NAME:
            continue
        if name not in declared:
            raise ArchiveError(f"备份包含 manifest 未声明的文件: {name}")

    settings_present, _settings = _settings_from_staged(staged)
    database_present = staged.member(DATABASE_ENTRY) is not None
    database_meta = manifest.get("database")
    if payload_kind == "sqlite":
        if not include_chats or not database_present \
                or not isinstance(database_meta, dict):
            raise ArchiveError("manifest.json 的数据库声明不一致")
        schema_version = database_meta.get("schemaVersion")
        minimum = database_meta.get("minimumReadableSchemaVersion")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ArchiveError("manifest.json 的 schemaVersion 无效")
        if schema_version not in schema.DRIFT_VERSIONS:
            if schema_version > schema.DRIFT_LATEST:
                if not isinstance(minimum, int) or minimum > schema.DRIFT_LATEST:
                    raise UnsupportedArchiveError(
                        f"SQLite schema {schema_version} 无法被当前版本读取")
            else:
                raise UnsupportedArchiveError(
                    f"SQLite schema {schema_version} 未查证，拒绝导入")
        if database_meta.get("entry") != DATABASE_ENTRY:
            raise ArchiveError("manifest.json 的数据库条目名无效")
        conversation_count = database_meta.get("conversationCount")
        message_count = database_meta.get("messageCount")
        if not isinstance(conversation_count, int) \
                or not isinstance(message_count, int):
            raise ArchiveError("manifest.json 的聊天计数无效")
        database_member = staged.member(DATABASE_ENTRY)
        assert database_member is not None
        con = sqlite3.connect(str(database_member.path))
        try:
            _validate_kelivo_database(con, schema_version)
            _ensure_sqlite_healthy(con, label="Kelivo")
            actual_conversations = int(con.execute(
                "SELECT COUNT(*) FROM conversation_rows").fetchone()[0])
            actual_messages = int(con.execute(
                "SELECT COUNT(*) FROM message_rows").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise ArchiveError(f"Kelivo 数据库损坏: {exc}") from exc
        finally:
            con.close()
        if (actual_conversations, actual_messages) != (
                conversation_count, message_count):
            raise ArchiveError("manifest.json 的聊天计数与数据库实际行数不符")
        database_present = True
    elif payload_kind == "settings-only":
        if include_chats or database_present or database_meta is not None:
            raise ArchiveError("settings-only 备份不应包含聊天数据库")
        schema_version = None
        if not settings_present:
            raise ArchiveError("settings-only 备份缺少 settings.json")
    else:
        raise ArchiveError(f"未知的 payloadKind: {payload_kind}")

    if unknown_entries:
        # Future archive with a readable declaration: the unknown entries are
        # dropped and reported during projection.
        pass
    return model.FormatInfo(
        family="kelivo",
        producer=None,
        kind=KIND_KELIVO_ARCHIVE,
        format_version=format_version,
        schema_version=schema_version,
        settings_present=settings_present,
        database_present=database_present,
        scope=scope,
        selection_encoding=model.SELECTION_ACTUAL,
        selection_evidence="SQLite conversation_rows.version_selections_json",
        app_version=str(manifest.get("appVersion")),
    )


def _validate_kelivo_database(con: sqlite3.Connection, schema_version: int) -> None:
    if schema_version in schema.DRIFT_VERSIONS:
        schema.validate_drift_database(con, schema_version)
        return
    # A newer schema that declared compatibility: the core tables and columns
    # must still exist exactly as known, and only additions are tolerated.
    base = schema.load_drift(schema.DRIFT_LATEST)
    tables = {
        str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")
    }
    missing = set(base.table_columns) - tables
    if missing:
        raise UnsupportedArchiveError(
            "新版本数据库缺少当前已知的核心表: " + ", ".join(sorted(missing)))
    for table, columns in base.table_columns.items():
        actual = tuple(str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")'))
        if actual[:len(columns)] != columns:
            raise UnsupportedArchiveError(
                f"新版本数据库的核心表 {table} 列结构不兼容")


def _native_scope(manifest: dict[str, Any], format_version: int) -> frozenset[str]:
    declared = manifest.get("scope")
    if declared is None:
        return frozenset(c for c in model.SCOPE_CATEGORIES
                         if c != "environmentVariables")
    if not isinstance(declared, dict):
        raise ArchiveError("manifest.json 的 scope 无效")
    missing = [c for c in model.SCOPE_CATEGORIES if not isinstance(
        declared.get(c), bool)]
    if missing:
        raise ArchiveError("manifest.json 的 scope 缺少类别: "
                           + ", ".join(missing))
    return frozenset(c for c in model.SCOPE_CATEGORIES if declared[c])


# --- legacy JSON ----------------------------------------------------------

def _validate_kelivo_json(staged: Staged) -> model.FormatInfo:
    _check_roots(staged, LEGACY_MEMBERS, KELIVO_ASSET_ROOTS,
                 kind_label="Kelivo JSON")
    settings_present, settings = _settings_from_staged(staged)
    chats = staged.read_json("chats.json")
    if not isinstance(chats, dict):
        raise ArchiveError("chats.json 不是 JSON 对象")
    conversations, messages = _legacy_lists(chats)
    version = chats.get("version")
    if version is None:
        # No stamp: only a complete known v1 shape is accepted.
        root_version = 1
    elif isinstance(version, int) and not isinstance(version, bool) \
            and version in (1, 2):
        root_version = version
    else:
        raise UnsupportedArchiveError(f"chats.json 版本 {version!r} 未查证")
    _validate_legacy_conversations(conversations)
    _validate_legacy_messages(messages)
    _validate_tool_events(chats.get("toolEvents"))
    _validate_gemini_sigs(chats.get("geminiThoughtSigs"))
    group_chats, group_members = _legacy_groups(chats)
    selection, evidence = _legacy_selection_dialect(
        root_version, conversations, messages, group_chats, group_members)
    _validate_deleted(staged)
    scope = _legacy_scope(settings, staged, chats)
    return model.FormatInfo(
        family="kelivo",
        producer=None,
        kind=KIND_KELIVO_JSON,
        format_version=root_version,
        schema_version=None,
        settings_present=settings_present,
        database_present=False,
        scope=scope,
        selection_encoding=selection,
        selection_evidence=evidence,
        app_version=None,
    )


def _legacy_lists(chats: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    conversations = chats.get("conversations")
    messages = chats.get("messages")
    if not isinstance(conversations, list) or not isinstance(messages, list):
        raise ArchiveError("chats.json 缺少 conversations/messages 列表")
    return conversations, messages


def _validate_legacy_conversations(conversations: list[Any]) -> None:
    seen: set[str] = set()
    for entry in conversations:
        if not isinstance(entry, dict):
            raise ArchiveError("chats.json 的会话不是对象")
        conv_id = entry.get("id")
        if not isinstance(conv_id, str) or not conv_id:
            raise ArchiveError("chats.json 的会话缺少 id")
        if conv_id in seen:
            raise ArchiveError(f"chats.json 包含重复会话 id: {conv_id}")
        seen.add(conv_id)
        title = entry.get("title")
        if not isinstance(title, str):
            raise ArchiveError(f"会话 {conv_id} 的 title 无效")
        for key in ("createdAt", "updatedAt"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                raise ArchiveError(f"会话 {conv_id} 的 {key} 无效")
            if iso_to_ms(value) is None:
                raise ArchiveError(f"会话 {conv_id} 的 {key} 不是有效日期")
        message_ids = entry.get("messageIds")
        if not isinstance(message_ids, list) \
                or any(not isinstance(item, str) for item in message_ids):
            raise ArchiveError(f"会话 {conv_id} 的 messageIds 无效")
        selections = entry.get("versionSelections", {})
        if not isinstance(selections, dict) or any(
                not isinstance(key, str) for key in selections):
            raise ArchiveError(f"会话 {conv_id} 的 versionSelections 无效")
        if any(not isinstance(value, int) or isinstance(value, bool)
               for value in selections.values()):
            raise ArchiveError(f"会话 {conv_id} 的 versionSelections 值无效")
        for key in ("mcpServerIds", "chatSuggestions"):
            value = entry.get(key, [])
            if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value):
                raise ArchiveError(f"会话 {conv_id} 的 {key} 无效")
        truncate = entry.get("truncateIndex", -1)
        if not isinstance(truncate, int) or isinstance(truncate, bool):
            raise ArchiveError(f"会话 {conv_id} 的 truncateIndex 无效")


def _validate_legacy_messages(messages: list[Any]) -> None:
    seen: set[str] = set()
    for entry in messages:
        if not isinstance(entry, dict):
            raise ArchiveError("chats.json 的消息不是对象")
        message_id = entry.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ArchiveError("chats.json 的消息缺少 id")
        if message_id in seen:
            raise ArchiveError(f"chats.json 包含重复消息 id: {message_id}")
        seen.add(message_id)
        if not isinstance(entry.get("role"), str) or not entry.get("role"):
            raise ArchiveError(f"消息 {message_id} 缺少 role")
        if not isinstance(entry.get("conversationId"), str):
            raise ArchiveError(f"消息 {message_id} 缺少 conversationId")
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            raise ArchiveError(f"消息 {message_id} 的 timestamp 无效")
        if iso_to_ms(timestamp) is None:
            raise ArchiveError(f"消息 {message_id} 的 timestamp 不是有效日期")
        for key in ("groupId", "subgroupId", "speakerAssistantId", "modelId",
                    "providerId"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise ArchiveError(f"消息 {message_id} 的 {key} 类型无效")
        if "isPreset" in entry and not isinstance(entry["isPreset"], bool):
            raise ArchiveError(f"消息 {message_id} 的 isPreset 无效")
        version = entry.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ArchiveError(f"消息 {message_id} 的 version 无效")
        parts = entry.get("parts")
        if parts is not None:
            if not isinstance(parts, list):
                raise ArchiveError(f"消息 {message_id} 的 parts 无效")
            for part in parts:
                if not isinstance(part, dict) \
                        or not isinstance(part.get("kind"), str) \
                        or not isinstance(part.get("payload"), str):
                    raise ArchiveError(f"消息 {message_id} 的 parts 无效")
        elif not isinstance(entry.get("content"), str):
            raise ArchiveError(f"消息 {message_id} 缺少 content")
        for key in ("totalTokens", "promptTokens", "completionTokens",
                    "cachedTokens", "durationMs"):
            value = entry.get(key)
            if value is not None and (not isinstance(value, int)
                                      or isinstance(value, bool) or value < 0):
                raise ArchiveError(f"消息 {message_id} 的 {key} 无效")


def _validate_tool_events(raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ArchiveError("chats.json 的 toolEvents 无效")
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            raise ArchiveError("chats.json 的 toolEvents 无效")
        for event in value:
            if not isinstance(event, dict) or not isinstance(event.get("name"), str):
                raise ArchiveError("chats.json 的 toolEvents 无效")


def _validate_gemini_sigs(raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw.items()):
        raise ArchiveError("chats.json 的 geminiThoughtSigs 无效")


def _legacy_groups(chats: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_chats = chats.get("groupChats") or []
    raw_members = chats.get("groupMembers") or []
    if not isinstance(raw_chats, list) or not isinstance(raw_members, list):
        raise ArchiveError("chats.json 的群聊数据无效")
    group_chats: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_chats:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ArchiveError("chats.json 的群聊记录无效")
        if entry["id"] in seen:
            raise ArchiveError(f"chats.json 包含重复群聊 id: {entry['id']}")
        seen.add(entry["id"])
        if not isinstance(entry.get("conversationId"), str):
            raise ArchiveError("chats.json 的群聊记录缺少 conversationId")
        group_chats.append(entry)
    group_members: list[dict[str, Any]] = []
    for entry in raw_members:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("groupChatId"), str):
            raise ArchiveError("chats.json 的群聊成员记录无效")
        group_members.append(entry)
    return group_chats, group_members


def _validate_deleted(staged: Staged) -> None:
    if staged.member("deleted.json") is None:
        return
    data = staged.read_json("deleted.json")
    if not isinstance(data, dict):
        raise ArchiveError("deleted.json 不是 JSON 对象")
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, list):
            raise ArchiveError("deleted.json 的形制无效")


def _legacy_selection_dialect(
    root_version: int,
    conversations: list[Any],
    messages: list[Any],
    group_chats: list[Any],
    group_members: list[Any],
) -> tuple[str, str]:
    """Decide whether versionSelections hold ordinals or actual versions."""
    if group_chats or group_members:
        return model.SELECTION_ACTUAL, "存在 Cuplivo 原生群聊记录"
    cuplivo_fields = []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        if entry.get("subgroupId") is not None:
            cuplivo_fields.append("subgroupId")
        if entry.get("speakerAssistantId") is not None:
            cuplivo_fields.append("speakerAssistantId")
        if entry.get("isPreset"):
            cuplivo_fields.append("isPreset")
    for entry in conversations:
        if isinstance(entry, dict) and entry.get("conversationKind") == "group":
            cuplivo_fields.append("conversationKind")
    if cuplivo_fields:
        return (model.SELECTION_ACTUAL,
                f"存在 Cuplivo 原生字段: {sorted(set(cuplivo_fields))[0]}")
    if root_version == 2:
        return model.SELECTION_ACTUAL, "chats.json version 2 (Cuplivo 导出)"
    evidence = _resolution_evidence(conversations, messages)
    if evidence == "actual":
        return model.SELECTION_ACTUAL, "按消息候选判定为实际版本号"
    if evidence == "ordinal":
        return model.SELECTION_ORDINAL, "按消息候选判定为版本序号"
    if evidence == "ambiguous":
        return (model.SELECTION_UNDETERMINED,
                "版本号与序号均有效但指向不同消息，转换时将拒绝猜测")
    return (model.SELECTION_UNDETERMINED,
            "无群聊字段且消息候选无法判定 selection 方言")


def _resolution_evidence(conversations: list[Any],
                         messages: list[Any]) -> str:
    """Return 'actual', 'ordinal', 'both-invalid' or 'ambiguous'."""
    by_id = {entry["id"]: entry for entry in messages if isinstance(entry, dict)}
    any_actual = False
    any_ordinal = False
    any_selection = False
    disagreement = False
    for conversation in conversations:
        selections = conversation.get("versionSelections") or {}
        if not isinstance(selections, dict):
            continue
        members_by_group: dict[str, list[dict[str, Any]]] = {}
        for message_id in conversation.get("messageIds", []):
            message = by_id.get(message_id)
            if message is None:
                continue
            group_id = message.get("groupId") or message["id"]
            members_by_group.setdefault(str(group_id), []).append(message)
        for group_id, value in selections.items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            any_selection = True
            members = sorted(
                members_by_group.get(str(group_id), []),
                key=lambda item: (item.get("version") or 0,
                                  str(item.get("id"))))
            actual = [m for m in members if (m.get("version") or 0) == value]
            ordinal = members[value] if 0 <= value < len(members) else None
            actual_valid = len(actual) == 1
            ordinal_valid = ordinal is not None
            any_actual = any_actual or actual_valid
            any_ordinal = any_ordinal or ordinal_valid
            if actual_valid and ordinal_valid \
                    and actual[0].get("id") != ordinal.get("id"):
                disagreement = True
    if disagreement:
        return "ambiguous"
    if not any_selection:
        return "both-invalid"
    if any_actual and any_ordinal:
        return "actual"
    if any_actual:
        return "actual"
    if any_ordinal:
        return "ordinal"
    return "both-invalid"


# --- JSONL v2 -------------------------------------------------------------

def _validate_cuplivo_jsonl(staged: Staged) -> model.FormatInfo:
    _check_roots(staged, JSONL_MEMBERS, KELIVO_ASSET_ROOTS,
                 kind_label="Cuplivo JSONL")
    settings_present, settings = _settings_from_staged(staged)
    for entry in ("conversations.jsonl", "messages.jsonl"):
        if staged.member(entry) is None:
            raise ArchiveError(f"JSONL 备份缺少 {entry}")
    meta = staged.read_json("chats_meta.json")
    if not isinstance(meta, dict):
        raise ArchiveError("chats_meta.json 不是 JSON 对象")
    format_version = meta.get("format_version")
    if format_version != 2:
        raise UnsupportedArchiveError(
            f"chats_meta.json format_version={format_version!r} 未查证")
    tables = meta.get("tables")
    if not isinstance(tables, list) or "conversations" not in tables \
            or "messages" not in tables:
        raise ArchiveError("chats_meta.json 的 tables 无效")
    zip_tables = meta.get("zip_tables")
    if not isinstance(zip_tables, dict) \
            or zip_tables.get("conversations") != "conversations.jsonl" \
            or zip_tables.get("messages") != "messages.jsonl":
        raise ArchiveError("chats_meta.json 的 zip_tables 无效")
    conversation_count = meta.get("conversation_count")
    message_count = meta.get("message_count")
    if not isinstance(conversation_count, int) \
            or not isinstance(message_count, int):
        raise ArchiveError("chats_meta.json 的计数无效")
    _legacy_groups(meta)
    conversation_records = _read_jsonl(
        staged, "conversations.jsonl", "conversation")
    message_records = _read_jsonl(staged, "messages.jsonl", "message")
    if len(conversation_records) != conversation_count:
        raise ArchiveError("conversations.jsonl 行数与声明不符")
    if len(message_records) != message_count:
        raise ArchiveError("messages.jsonl 行数与声明不符")
    _validate_legacy_conversations(
        [record["conversation"] for record in conversation_records])
    _validate_legacy_messages(
        [record["message"] for record in message_records])
    scope = _legacy_scope(settings, staged, meta)
    return model.FormatInfo(
        family="kelivo",
        producer=None,
        kind=KIND_CUPLIVO_JSONL,
        format_version=2,
        schema_version=None,
        settings_present=settings_present,
        database_present=False,
        scope=scope,
        selection_encoding=model.SELECTION_ACTUAL,
        selection_evidence="JSONL2 writer persisted Conversation.toJson versions",
        app_version=None,
    )


def _read_jsonl(staged: Staged, entry: str, wrapper: str) -> list[dict[str, Any]]:
    member = staged.member(entry)
    assert member is not None
    out: list[dict[str, Any]] = []
    try:
        text = member.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"{entry} 不是 UTF-8 文本: {exc}") from exc
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise ArchiveError(f"{entry} 第 {lineno} 行不是有效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(
                payload.get(wrapper), dict):
            raise ArchiveError(f"{entry} 第 {lineno} 行缺少 {wrapper} 包装")
        if "toolEvents" in payload and not isinstance(
                payload["toolEvents"], list):
            raise ArchiveError(f"{entry} 第 {lineno} 行 toolEvents 无效")
        if "geminiSignature" in payload and not isinstance(
                payload["geminiSignature"], str):
            raise ArchiveError(f"{entry} 第 {lineno} 行 geminiSignature 无效")
        out.append(payload)
    return out


# --- settings-only --------------------------------------------------------

def _validate_settings_only(staged: Staged,
                            detected: DetectedFormat) -> model.FormatInfo:
    settings_present, settings = _settings_from_staged(staged)
    if not settings_present:
        raise ArchiveError("仅设置备份缺少 settings.json")
    family, producer = _classify_settings(settings)
    if family != detected.family:
        raise ArchiveError("settings.json 的应用指纹与备份类型不一致")
    if detected.family == "rikkahub":
        _check_roots(staged, {"settings.json"}, RH_ASSET_ROOTS,
                     kind_label="RikkaHub")
        selection = model.SELECTION_NODE_INDEX
        scope = _rikkahub_scope(settings, staged, schema.RH_LATEST)
    else:
        _check_roots(staged, {"settings.json"},
                     [root for root in KELIVO_ASSET_ROOTS if root != "upload"]
                     + ["upload"], kind_label="Kelivo")
        selection = model.SELECTION_ACTUAL
        scope = _kelivo_settings_scope(settings)
        for root in KELIVO_ASSET_ROOTS:
            if staged.entries_under(root):
                scope = scope | {"files"}
        if staged.entries_under("skills"):
            scope = scope | {"skills"}
    scope = frozenset(set(scope) - {"chats"})
    return model.FormatInfo(
        family=family,
        producer=producer,
        kind=KIND_SETTINGS_ONLY,
        format_version=None,
        schema_version=None,
        settings_present=True,
        database_present=False,
        scope=scope,
        selection_encoding=selection,
        selection_evidence="无聊天数据",
        app_version=None,
    )


def _legacy_scope(settings: dict[str, Any], staged: "Staged | _PreviewStaged",
                  chats: dict[str, Any]) -> frozenset[str]:
    scope = {"settings", "chats"}
    if any(staged.entries_under(root) for root in KELIVO_ASSET_ROOTS):
        scope.add("files")
    if staged.entries_under("skills"):
        scope.add("skills")
    if staged.entries_under("workspaces"):
        scope.add("workspaces")
    scope |= _kelivo_settings_scope(settings)
    return frozenset(scope)


def _kelivo_settings_scope(settings: dict[str, Any]) -> set[str]:
    scope = {"settings"}
    for key in settings:
        category = kelivo_setting_category(key)
        if category:
            scope.add(category)
    return scope


#: Copied from the application's BackupContentFilter.categoryForKey.
def kelivo_setting_category(key: str) -> str | None:
    if not isinstance(key, str):
        return None
    if key == "environment_variables_v1":
        return "environmentVariables"
    if key == "assistants_v1" or key == "current_assistant_id_v1" \
            or key.startswith("assistant_tag"):
        return "assistants"
    if key.startswith("provider") or key == "selected_model_v1" \
            or key == "pinned_models_v1" or key == "reasoning_choice_by_model_v1":
        return "providers"
    if key.startswith("mcp_"):
        return "mcp"
    if key.startswith("world_books_"):
        return "worldBooks"
    if key == "assistant_memories_v1" or key.startswith("memory_") \
            or key == "user_profile_fields_v1":
        return "memories"
    if key.startswith("skills_"):
        return "skills"
    if key.startswith("workspaces_"):
        return "workspaces"
    if key.startswith("quick_phrases_"):
        return "quickPhrases"
    if key.startswith("instruction_injection"):
        return "instructions"
    if key.startswith("search_"):
        return "searchServices"
    if key.startswith("tts_") or key.startswith("asr_"):
        return "speechServices"
    return "settings"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def open_backup(source: Path) -> zipfile.ZipFile:
    """Open a source archive, rejecting encrypted containers with a clear error."""
    try:
        with source.open("rb") as handle:
            magic = handle.read(8)
    except OSError as exc:
        raise ArchiveError(f"无法读取 {source}: {exc}") from exc
    if magic.startswith(ENCRYPTED_MAGIC):
        raise UnsupportedArchiveError(
            "该文件是未发布的 Kelivo 加密备份 (.kbackup)，当前版本不支持")
    if not zipfile.is_zipfile(source):
        raise ArchiveError(f"{source} 不是 ZIP 备份文件")
    try:
        return zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:  # pragma: no cover - race
        raise ArchiveError(f"无法打开 ZIP 备份: {exc}") from exc


def inspect_backup(source: Path) -> model.FormatInfo:
    """Cheap detection for the interactive wizard (no full hash validation)."""
    with closing(open_backup(source)) as zf:
        return _inspect(zf)


def _inspect(zf: zipfile.ZipFile) -> model.FormatInfo:
    detected = detect_archive(zf)
    infos = _check_member_set(zf.infolist())
    names = set(infos)
    settings = _peek_json(infos.get("settings.json"), zf)
    settings_present = settings is not None
    if detected.kind == KIND_RH_SQLITE:
        version = _peek_sqlite_version(zf, infos["rikka_hub.db"])
        scope = _rikkahub_scope(settings or {}, _PreviewStaged(names),
                                version or schema.RH_LATEST)
        return model.FormatInfo(
            family="rikkahub", producer="rikkahub", kind=KIND_RH_SQLITE,
            format_version=None, schema_version=version,
            settings_present=settings_present, database_present=True,
            scope=scope, selection_encoding=model.SELECTION_NODE_INDEX,
            selection_evidence=f"Room {version}: message_node.select_index")
    if detected.kind == KIND_KELIVO_ARCHIVE:
        manifest = _peek_manifest(infos[MANIFEST_NAME], zf)
        scope = _native_scope(manifest, int(manifest.get("formatVersion") or 0))
        database = manifest.get("database")
        return model.FormatInfo(
            family="kelivo", producer=None, kind=KIND_KELIVO_ARCHIVE,
            format_version=manifest.get("formatVersion"),
            schema_version=(database or {}).get("schemaVersion")
            if isinstance(database, dict) else None,
            settings_present="settings.json" in names,
            database_present="database/kelivo.db" in names,
            scope=scope, selection_encoding=model.SELECTION_ACTUAL,
            selection_evidence="SQLite conversation_rows.version_selections_json",
            app_version=manifest.get("appVersion") if isinstance(
                manifest.get("appVersion"), str) else None)
    if detected.kind == KIND_CUPLIVO_JSONL:
        meta = _peek_json(infos.get("chats_meta.json"), zf) or {}
        return model.FormatInfo(
            family="kelivo", producer=None, kind=KIND_CUPLIVO_JSONL,
            format_version=meta.get("format_version"), schema_version=None,
            settings_present=settings_present, database_present=False,
            scope=_legacy_scope(settings or {}, _PreviewStaged(names), meta),
            selection_encoding=model.SELECTION_ACTUAL,
            selection_evidence="JSONL2 Conversation.toJson versions")
    if detected.kind == KIND_KELIVO_JSON:
        chats = _peek_json(infos.get("chats.json"), zf) or {}
        version = chats.get("version") if isinstance(chats.get("version"), int) else 1
        group_chats = chats.get("groupChats") or []
        group_members = chats.get("groupMembers") or []
        conversations = chats.get("conversations") or []
        messages = chats.get("messages") or []
        try:
            selection, evidence = _legacy_selection_dialect(
                version if isinstance(version, int) else 1,
                conversations if isinstance(conversations, list) else [],
                messages if isinstance(messages, list) else [],
                group_chats if isinstance(group_chats, list) else [],
                group_members if isinstance(group_members, list) else [])
        except (KeyError, TypeError):
            selection, evidence = (
                model.SELECTION_UNDETERMINED, "需要完整读取后判定")
        return model.FormatInfo(
            family="kelivo", producer=None, kind=KIND_KELIVO_JSON,
            format_version=version, schema_version=None,
            settings_present=settings_present, database_present=False,
            scope=_legacy_scope(settings or {}, _PreviewStaged(names), chats),
            selection_encoding=selection, selection_evidence=evidence)
    family, producer = _classify_settings(settings)
    scope = (_rikkahub_scope(settings or {}, _PreviewStaged(names),
                             schema.RH_LATEST)
             if family == "rikkahub"
             else _kelivo_settings_scope(settings or {})) - {"chats"}
    return model.FormatInfo(
        family=family, producer=producer, kind=KIND_SETTINGS_ONLY,
        format_version=None, schema_version=None, settings_present=True,
        database_present=False, scope=frozenset(scope),
        selection_encoding=(model.SELECTION_NODE_INDEX
                            if family == "rikkahub"
                            else model.SELECTION_ACTUAL),
        selection_evidence="无聊天数据")


class _PreviewStaged:
    """Minimal name-only view used by the cheap wizard inspection."""

    def __init__(self, names: set[str]) -> None:
        self._names = names

    @property
    def members(self) -> dict[str, None]:
        return {name: None for name in self._names}

    def member(self, entry: str) -> "_PreviewMember | None":
        return _PreviewMember(entry) if entry in self._names else None

    def entries_under(self, root: str) -> list[str]:
        prefix = root.rstrip("/") + "/"
        return [name for name in self._names if name.startswith(prefix)]


@dataclass(frozen=True)
class _PreviewMember:
    entry: str


def _peek_sqlite_version(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> int | None:
    """Read ``user_version`` from the SQLite header without extracting the DB."""
    try:
        with zf.open(info) as handle:
            header = handle.read(64)
    except (OSError, zipfile.BadZipFile):
        return None
    if len(header) < 64 or not header.startswith(b"SQLite format 3\x00"):
        return 0
    return int.from_bytes(header[60:64], "big")
