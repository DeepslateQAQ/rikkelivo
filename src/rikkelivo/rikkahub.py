"""RikkaHub backup format: ``rikka_hub.db`` (Room) plus ``settings.json``.

Schema statements come from the vendored Room exports (``schemas/rikkahub``),
so a database written here is structurally identical to one the app produced:
every table, column default, index, foreign key and the Room identity hash for
the target version.  The search index is rebuilt from the message rows; the
application's private FTS tokenizer is reused when the interpreter has it and
reported otherwise.

The module owns two directions:

* native -> :class:`rikkelivo.model.Snapshot` (used when the target is the
  Kelivo family),
* :class:`~rikkelivo.model.Snapshot` -> native payload (used when the source
  is the Kelivo family), plus the native-to-native schema migration.
"""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import re
import sqlite3
import uuid as _uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from rikkelivo import model, schema
from rikkelivo.archive import ArchiveError
from rikkelivo.model import (
    Artifact,
    Asset,
    Backup,
    Conv,
    Diagnostic,
    FormatInfo,
    GroupChat,
    MemoryDoc,
    Msg,
    Part,
    RevisionGroup,
    RikkaPayload,
    Snapshot,
    TableRows,
    TargetProfile,
    Usage,
    Workspace,
)
from rikkelivo.times import iso_to_ms, iso_to_utc_z, ms_to_iso

DEFAULT_ASSISTANT_ID = "0950e2dc-9bd5-4801-afa3-aa887aa36b4e"
RH_FILES_PREFIX = "file:///data/user/0/me.rerere.rikkahub/files/"

_ENTITY_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "rikkelivo-entity")
_NODE_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "rikkelivo-node")
_MEMORY_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "rikkelivo-memory")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def entity_uuid(value: Any, namespace: str = "") -> str:
    """Normalise an arbitrary id to a legal UUID, deterministically."""
    if is_uuid(value):
        return str(value).lower()
    seed = f"{namespace}:{value}" if namespace else str(value)
    return str(_uuid.uuid5(_ENTITY_NS, seed))


def node_uuid(conversation_id: str, group_id: str) -> str:
    return str(_uuid.uuid5(_NODE_NS, f"{conversation_id}:{group_id}"))


def _json_load(raw: str | None) -> Any:
    if raw in (None, ""):
        return None
    return json.loads(raw)


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = _json_load(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _json_obj(raw: str | None) -> dict[str, Any]:
    try:
        value = _json_load(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _now_ms() -> int:
    return int(_dt.datetime.now().timestamp() * 1000)


# ---------------------------------------------------------------------------
# Message parts: native RikkaHub JSON <-> neutral parts
# ---------------------------------------------------------------------------

_UNSUPPORTED_RH_PART_TYPES = {
    "search": "RikkaHub 历史 search 部件",
}


def _iso_to_ms(value: Any) -> int | None:
    return iso_to_ms(value) if isinstance(value, str) else None


def parts_from_native(parts: Any) -> list[Part]:
    if not isinstance(parts, list):
        raise ArchiveError("RikkaHub 消息的 parts 不是数组")
    out: list[Part] = []
    for raw in parts:
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            raise ArchiveError("RikkaHub 消息包含无效部件")
        kind = raw["type"]
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None
        if kind == "text":
            out.append(Part("text", text=str(raw.get("text") or ""),
                            metadata=metadata, native=raw))
        elif kind == "reasoning":
            out.append(Part(
                "reasoning", text=str(raw.get("reasoning") or ""),
                created_ms=_iso_to_ms(raw.get("createdAt")),
                finished_ms=_iso_to_ms(raw.get("finishedAt")),
                reasoning_type=raw.get("reasoningType")
                if isinstance(raw.get("reasoningType"), str) else None,
                metadata=metadata, native=raw))
        elif kind in ("image", "video", "audio"):
            out.append(Part(kind, uri=str(raw.get("url") or ""),
                            metadata=metadata, native=raw))
        elif kind == "document":
            out.append(Part(
                "file", uri=str(raw.get("url") or ""),
                name=str(raw.get("fileName") or ""),
                mime=str(raw.get("mime") or ""), metadata=metadata, native=raw))
        elif kind == "tool":
            output = raw.get("output")
            output_parts: list[Part] = []
            if isinstance(output, list):
                output_parts = parts_from_native(output)
            elif output is not None:
                raise ArchiveError("RikkaHub 工具部件的 output 无效")
            out.append(Part(
                "tool",
                call_id=str(raw.get("toolCallId") or ""),
                name=str(raw.get("toolName") or ""),
                input_json=raw.get("input") if isinstance(raw.get("input"), str)
                else json.dumps(raw.get("input"), ensure_ascii=False),
                output=output_parts,
                approval=raw.get("approvalState")
                if isinstance(raw.get("approvalState"), dict) else None,
                metadata=metadata, native=raw))
        elif kind == "server_tool":
            output = raw.get("output")
            readable = output if isinstance(output, str) else (
                json.dumps(output, ensure_ascii=False)
                if output is not None else "")
            out.append(Part(
                "server_tool", text=readable,
                call_id=str(raw.get("toolCallId") or ""),
                name=str(raw.get("toolName") or ""),
                input_json=json.dumps(raw.get("input"), ensure_ascii=False)
                if raw.get("input") is not None else None,
                status=raw.get("status") if isinstance(raw.get("status"), str)
                else None,
                metadata=metadata, native=raw))
        elif kind == "tool_call":
            # Deprecated client-tool call without a result.
            out.append(Part(
                "tool", call_id=str(raw.get("toolCallId") or ""),
                name=str(raw.get("toolName") or ""),
                input_json=raw.get("arguments")
                if isinstance(raw.get("arguments"), str)
                else json.dumps(raw.get("arguments"), ensure_ascii=False),
                output=[], approval=raw.get("approvalState")
                if isinstance(raw.get("approvalState"), dict) else None,
                metadata=metadata, native=raw))
        elif kind == "tool_result":
            content = raw.get("content")
            text = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False)
            out.append(Part(
                "tool", call_id=str(raw.get("toolCallId") or ""),
                name=str(raw.get("toolName") or ""),
                input_json=raw.get("arguments")
                if isinstance(raw.get("arguments"), str)
                else json.dumps(raw.get("arguments"), ensure_ascii=False),
                output=[Part("text", text=text)], metadata=metadata, native=raw))
        else:
            out.append(Part(
                "unknown", raw_kind=kind,
                raw_payload=json.dumps(raw, ensure_ascii=False),
                metadata=metadata, native=raw))
    return out


def part_to_native(part: Part) -> dict[str, Any] | None:
    """Encode a neutral part as native RikkaHub JSON.

    ``None`` means the kind cannot be expressed by RikkaHub and the caller
    counts it as dropped data.
    """
    metadata = part.metadata
    if part.kind == "text":
        return {"type": "text", "text": part.text or "", "metadata": metadata}
    if part.kind == "reasoning":
        return {
            "type": "reasoning",
            "reasoning": part.text or "",
            "createdAt": _ms_to_instant(part.created_ms),
            "finishedAt": _ms_to_instant(part.finished_ms),
            "metadata": metadata,
            "reasoningType": part.reasoning_type or "reasoning_text",
        }
    if part.kind == "image":
        return {"type": "image", "url": part.uri or "", "metadata": metadata}
    if part.kind == "video":
        return {"type": "video", "url": part.uri or "", "metadata": metadata}
    if part.kind == "audio":
        return {"type": "audio", "url": part.uri or "", "metadata": metadata}
    if part.kind == "file":
        return {
            "type": "document",
            "url": part.uri or "",
            "fileName": part.name or _basename(part.uri or ""),
            "mime": part.mime or "application/octet-stream",
            "metadata": metadata,
        }
    if part.kind == "tool":
        output = [encoded for encoded in
                  (part_to_native(child) for child in part.output or [])
                  if encoded is not None]
        return {
            "type": "tool",
            "toolCallId": part.call_id or "",
            "toolName": part.name or "",
            "input": part.input_json if part.input_json is not None else "{}",
            "output": output,
            "approvalState": part.approval or {"type": "auto"},
            "metadata": metadata,
        }
    if part.kind == "server_tool":
        return {
            "type": "server_tool",
            "toolCallId": part.call_id or "",
            "toolName": part.name or "",
            "input": _maybe_json(part.input_json),
            "output": None,
            "status": part.status or "completed",
            "metadata": metadata,
        }
    return None


def _maybe_json(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _ms_to_instant(ms: int | None) -> str | None:
    return iso_to_utc_z(ms_to_iso(ms))


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or "file"


# ---------------------------------------------------------------------------
# Native message <-> neutral message
# ---------------------------------------------------------------------------

def message_from_native(raw: Any, conversation_id: str,
                        node_id: str, fallback_ms: int,
                        version: int) -> Msg:
    if not isinstance(raw, dict):
        raise ArchiveError("RikkaHub message_node.messages 包含非对象条目")
    message_id = raw.get("id")
    role = raw.get("role")
    if not isinstance(message_id, str) or not message_id:
        raise ArchiveError("RikkaHub 消息缺少 id")
    if not is_uuid(message_id):
        raise ArchiveError(f"RikkaHub 消息 id 不是 UUID: {message_id}")
    if not isinstance(role, str) or role not in ("system", "user", "assistant", "tool"):
        raise ArchiveError(f"RikkaHub 消息 {message_id} 的 role 无效")
    for key in ("createdAt", "finishedAt"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str)
                                  or iso_to_ms(value) is None):
            raise ArchiveError(f"RikkaHub 消息 {message_id} 的 {key} 无效")
    if raw.get("modelId") is not None and not isinstance(raw.get("modelId"), str):
        raise ArchiveError(f"RikkaHub 消息 {message_id} 的 modelId 无效")
    if raw.get("usage") is not None:
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            raise ArchiveError(f"RikkaHub 消息 {message_id} 的 usage 无效")
        for key in ("promptTokens", "completionTokens", "cachedTokens",
                    "totalTokens"):
            value = usage.get(key)
            if value is not None and (not isinstance(value, int)
                                      or isinstance(value, bool)):
                raise ArchiveError(
                    f"RikkaHub 消息 {message_id} 的 usage.{key} 无效")
    parts = parts_from_native(raw.get("parts"))
    created_ms = _iso_to_ms(raw.get("createdAt"))
    fallback_used = created_ms is None
    if fallback_used:
        created_ms = fallback_ms
    annotations = raw.get("annotations")
    return Msg(
        id=message_id,
        role=role,
        parts=parts,
        created_ms=created_ms,
        finished_ms=_iso_to_ms(raw.get("finishedAt")),
        model_id=str(raw["modelId"]) if isinstance(raw.get("modelId"), str) else None,
        model_snapshot=raw.get("modelSnapshot")
        if isinstance(raw.get("modelSnapshot"), dict) else None,
        usage=Usage.from_native(raw.get("usage")),
        last_usage=Usage.from_native(raw.get("lastUsage")),
        generation_time_ms=_opt_int(raw.get("generationTimeMs")),
        duration_ms=_opt_int(raw.get("generationTimeMs")),
        step_count=_opt_int(raw.get("stepCount")),
        translation=raw.get("translation")
        if isinstance(raw.get("translation"), str) else None,
        is_context_checkpoint=bool(raw.get("isContextCheckpoint")),
        group_id=node_id,
        version=version,
        annotations=[item for item in annotations if isinstance(item, dict)]
        if isinstance(annotations, list) else [],
        native=dict(raw, _fallback_timestamp=fallback_used)
        if fallback_used else raw,
    )


def message_to_native(msg: Msg, *, model_uuid: str | None,
                      dropped_annotations: list[int],
                      fallback_ms: int = 0) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    dropped_parts = 0
    for part in msg.parts:
        encoded = part_to_native(part)
        if encoded is None:
            dropped_parts += 1
            continue
        parts.append(encoded)
    dropped_annotations[0] += len(msg.annotations)
    dropped_annotations[1] += dropped_parts
    if not parts:
        parts.append({"type": "text", "text": "", "metadata": None})
    snapshot = msg.model_snapshot
    if snapshot is None and msg.model_id and not model_uuid:
        snapshot = {"modelId": msg.model_id, "displayName": msg.model_id}
    created_ms = msg.created_ms if msg.created_ms is not None else fallback_ms
    if msg.created_ms is None:
        dropped_annotations[2] += 1
    message: dict[str, Any] = {
        "id": msg.id,
        "role": msg.role,
        "parts": parts,
        "annotations": [],
        "createdAt": ms_to_iso(created_ms) or "",
        "finishedAt": ms_to_iso(msg.finished_ms),
        "modelId": model_uuid,
        "usage": _usage_to_native(msg.usage),
        "translation": msg.translation,
    }
    if snapshot is not None:
        message["modelSnapshot"] = snapshot
    generation_time = msg.generation_time_ms
    if generation_time is None:
        generation_time = msg.duration_ms
    if generation_time is not None:
        message["generationTimeMs"] = generation_time
    if msg.last_usage is not None:
        message["lastUsage"] = _usage_to_native(msg.last_usage)
    if msg.step_count is not None:
        message["stepCount"] = msg.step_count
    if msg.is_context_checkpoint:
        message["isContextCheckpoint"] = True
    return message


def _usage_to_native(usage: Usage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "promptTokens": usage.prompt,
        "completionTokens": usage.completion,
        "cachedTokens": usage.cached,
        "totalTokens": usage.total,
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _staged_db_path(staging: Path) -> Path:
    return staging / "payload" / "rikka_hub.db"


@dataclass
class _RhMessageNode:
    node_id: str
    conversation_id: str
    node_index: int
    messages: list[dict[str, Any]]
    select_index: int


def read_backup(zf: zipfile.ZipFile, info: FormatInfo,
                staging: Path) -> Backup:
    from rikkelivo import archive

    staged = archive.load_staged(staging)
    settings = staged.read_json("settings.json") if staged.member(
        "settings.json") else {}
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ArchiveError("settings.json 不是 JSON 对象")
    assets: list[Asset] = []
    for entry, member in sorted(staged.members.items()):
        if entry in ("rikka_hub.db", "rikka_hub-wal", "rikka_hub.db-wal",
                     "rikka_hub.db-shm", "rikka_hub-shm", "settings.json",
                     "rikka_hub.db-shm"):
            continue
        assets.append(Asset(entry=entry, staged_path=member.path,
                            size=member.size, sha256=member.sha256,
                            mtime=member.mtime))
    diagnostics: list[Diagnostic] = []
    if info.database_present:
        try:
            identity_con = sqlite3.connect(
                str(_staged_db_path(staging)))
            identity_row = identity_con.execute(
                "SELECT identity_hash FROM room_master_table WHERE id = 42"
            ).fetchone()
            identity_con.close()
        except sqlite3.Error:
            identity_row = None
        if identity_row is None or not identity_row[0]:
            diagnostics.append(Diagnostic(
                "value_defaulted", "room identity", 1,
                "源数据库缺少 Room identity，已按已知结构校验并在输出中重建"))
        db_path = _staged_db_path(staging)
        if not db_path.exists():
            raise ArchiveError("暂存目录缺少 rikka_hub.db")
        tables, extra = _read_room_tables(db_path, info.schema_version or 0)
        payload = RikkaPayload(schema_version=info.schema_version or 0,
                               settings=settings, tables=tables)
        return Backup(info=info, payload=payload, assets=assets,
                      diagnostics=model.merge_diagnostics(diagnostics + extra),
                      roots=tuple(sorted({asset.entry.split("/")[0]
                                          for asset in assets})),
                      incremental=False)
    payload = RikkaPayload(schema_version=schema.RH_LATEST, settings=settings,
                           tables={})
    return Backup(info=info, payload=payload, assets=assets,
                  diagnostics=model.merge_diagnostics(diagnostics),
                  roots=tuple(sorted({asset.entry.split("/")[0]
                                      for asset in assets})),
                  incremental=False)


def _read_room_tables(db_path: Path,
                      version: int) -> tuple[dict[str, TableRows], list[Diagnostic]]:
    if version not in schema.RH_VERSIONS:
        raise ArchiveError(f"RikkaHub schema {version} 未查证")
    room = schema.load_room(version)
    diagnostics: list[Diagnostic] = []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        tables: dict[str, TableRows] = {}
        for entity in room.entities:
            tables[entity.table] = schema.read_rows(con, entity.table)
        _validate_nodes(con, room, diagnostics)
        locale = None
        try:
            row = con.execute("SELECT locale FROM android_metadata LIMIT 1").fetchone()
            if row is not None:
                locale = str(row[0])
        except sqlite3.Error:
            locale = None
        tables["__android_metadata"] = TableRows(
            columns=("locale",), rows=[(locale,)] if locale else [])
        nodes_without_messages = _count_nodes_embedded(con, room)
        if nodes_without_messages:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "conversation",
                nodes_without_messages,
                "会话节点原先嵌在 ConversationEntity.nodes，已按当前结构迁移"))
        return tables, diagnostics
    finally:
        con.close()


def _count_nodes_embedded(con: sqlite3.Connection, room: schema.RoomSchema) -> int:
    """Conversations whose nodes live in the legacy JSON column only."""
    if "message_node" not in room.tables:
        return 0
    count = 0
    for row in con.execute(
            "SELECT id, nodes FROM ConversationEntity"):
        nodes = _json_list(row["nodes"])
        if not nodes:
            continue
        has_rows = con.execute(
            "SELECT 1 FROM message_node WHERE conversation_id = ? LIMIT 1",
            (row["id"],)).fetchone()
        count += len(nodes) if has_rows is None else 0
    return count


NODE_EMBEDDED_ID = "00000000-0000-4000-8000-000000000000"


def validate_database_content(con: sqlite3.Connection, version: int) -> None:
    """Validate application JSON and references before the content is read."""
    room = schema.load_room(version)
    conversation_columns = {
        field.column for field in room.tables["ConversationEntity"].fields}
    config_column = "config" if "config" in conversation_columns else "'{}'"
    for row in con.execute(
            "SELECT id, nodes, suggestions, mode_injection_ids, lorebook_ids,"
            f" {config_column} FROM ConversationEntity"):
        try:
            suggestions = json.loads(row[2] or "[]")
            mode_ids = json.loads(row[3] or "[]")
            lorebook_ids = json.loads(row[4] or "[]")
            config = json.loads(row[5] or "{}")
        except (ValueError, TypeError) as exc:
            raise ArchiveError(
                f"RikkaHub 会话 {row[0]} 的 JSON 字段无效") from exc
        if not isinstance(suggestions, list) or not isinstance(mode_ids, list) \
                or not isinstance(lorebook_ids, list) \
                or not isinstance(config, dict):
            raise ArchiveError(f"RikkaHub 会话 {row[0]} 的 JSON 字段类型无效")
        raw_nodes = row[1]
        if raw_nodes not in (None, "", "[]"):
            try:
                parsed = json.loads(raw_nodes)
            except (ValueError, TypeError) as exc:
                raise ArchiveError(
                    f"RikkaHub 会话 {row[0]} 的 nodes 不是有效 JSON") from exc
            if not isinstance(parsed, list):
                raise ArchiveError(f"RikkaHub 会话 {row[0]} 的 nodes 无效")
    _validate_nodes(con, room, [])


def _validate_nodes(con: sqlite3.Connection, room: schema.RoomSchema,
                    diagnostics: list[Diagnostic]) -> None:
    conversations = {str(row[0]) for row in con.execute(
        "SELECT id FROM ConversationEntity")}
    seen_index: set[tuple[str, int]] = set()
    seen_ids: set[str] = set()
    seen_message_ids: set[str] = set()
    node_rows = list(con.execute(
        "SELECT id, conversation_id, node_index, messages, select_index"
        " FROM message_node ORDER BY conversation_id, node_index, id"))
    embedded_nodes = {str(row[0]): _json_list(row[1]) for row in con.execute(
        "SELECT id, nodes FROM ConversationEntity")}
    nodes_by_conversation: dict[str, int] = {}
    for row in con.execute(
            "SELECT id, assistant_id, create_at, update_at"
            " FROM ConversationEntity"):
        if not is_uuid(row[0]):
            raise ArchiveError(f"RikkaHub 会话 id 不是 UUID: {row[0]}")
        if row[1] and not is_uuid(row[1]):
            raise ArchiveError(f"RikkaHub 会话 {row[0]} 的助手 id 不是 UUID")
        if not isinstance(row[2], int) or not isinstance(row[3], int):
            raise ArchiveError(f"RikkaHub 会话 {row[0]} 的时间戳无效")
    for row in node_rows:
        node_id = str(row[0])
        conversation_id = str(row[1])
        index = _int_or_zero(row[2])
        if conversation_id not in conversations:
            raise ArchiveError(
                f"RikkaHub 消息节点 {node_id} 引用了不存在的会话")
        if not is_uuid(node_id):
            raise ArchiveError(f"RikkaHub 消息节点 id 不是 UUID: {node_id}")
        if node_id in seen_ids:
            raise ArchiveError(f"RikkaHub 消息节点 id 重复: {node_id}")
        seen_ids.add(node_id)
        key = (conversation_id, index)
        if key in seen_index:
            raise ArchiveError(
                f"RikkaHub 会话 {conversation_id} 的 node_index {index} 重复")
        seen_index.add(key)
        nodes_by_conversation[conversation_id] = nodes_by_conversation.get(
            conversation_id, 0) + 1
        raw_messages = _json_list(row[3])
        if not raw_messages:
            raise ArchiveError(f"RikkaHub 消息节点 {node_id} 没有消息")
        select_index = row[4]
        if not isinstance(select_index, int) or isinstance(select_index, bool) \
                or not 0 <= select_index < len(raw_messages):
            raise ArchiveError(f"RikkaHub 消息节点 {node_id} 的 select_index 无效")
        for raw in raw_messages:
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                if raw["id"] in seen_message_ids:
                    raise ArchiveError(f"RikkaHub 消息 id 重复: {raw['id']}")
                seen_message_ids.add(raw["id"])
            message_from_native(raw, conversation_id, node_id, 0, 0)
    for conversation_id, nodes in embedded_nodes.items():
        if not nodes:
            continue
        if nodes_by_conversation.get(conversation_id):
            raise ArchiveError(
                f"RikkaHub 会话 {conversation_id} 同时包含两种节点存储")
        for index, node in enumerate(nodes):
            if not isinstance(node, dict) or not isinstance(
                    node.get("messages"), list) or not node["messages"]:
                raise ArchiveError(
                    f"RikkaHub 会话 {conversation_id} 的嵌入式节点无效")
            select_index = node.get("selectIndex", 0)
            if not isinstance(select_index, int) \
                    or not 0 <= select_index < len(node["messages"]):
                raise ArchiveError(
                    f"RikkaHub 会话 {conversation_id} 的节点选择无效")


# ---------------------------------------------------------------------------
# Native -> neutral snapshot
# ---------------------------------------------------------------------------

def decode(backup: Backup) -> Snapshot:
    payload = backup.payload
    assert isinstance(payload, RikkaPayload)
    tables = payload.tables
    conversations = _table_dicts(tables, "ConversationEntity")
    nodes_by_conversation = _load_nodes(tables)
    snapshot = Snapshot(settings=dict(payload.settings))
    snapshot.has_chat_domain = bool(backup.info.database_present)
    snapshot.memory_docs = _memory_docs(tables)
    snapshot.memory_entries = _legacy_memory_entries(tables)
    snapshot.assistant_flags = _assistant_flags(payload.settings)
    snapshot.workspaces = _workspaces(tables)
    snapshot.group_chats = []
    snapshot.mcp_by_conversation = {}
    for row in conversations:
        conv = _conversation_from_row(row, nodes_by_conversation.get(
            str(row.get("id")), []))
        snapshot.conversations.append(conv)
    snapshot.media = {name: tables[name] for name in (
        "media_creation_session", "media_creation_node", "media_creation_record")
        if name in tables}
    snapshot.translations = tables.get("translation_history")
    snapshot.usage = tables.get("usage_record")
    snapshot.favorites = tables.get("favorites")
    snapshot.folders = tables.get("conversation_folder")
    snapshot.gen_media = tables.get("GenMediaEntity")
    snapshot.consolidation = tables.get("memory_consolidation")
    snapshot.extensions = None
    snapshot.asset_entries = [asset.entry for asset in backup.assets]
    snapshot.skills = _skills(backup.assets)
    fallbacks = sum(
        1 for conv in snapshot.conversations for message in conv.messages
        if message.native.get("_fallback_timestamp"))
    if fallbacks:
        snapshot.diagnostics = (Diagnostic(
            "value_defaulted", "timestamp", fallbacks,
            "消息缺少时间，已使用会话创建时间"),)
    return snapshot


def _table_dicts(tables: dict[str, TableRows], name: str) -> list[dict[str, Any]]:
    table = tables.get(name)
    return table.dicts() if table else []


def _load_nodes(tables: dict[str, TableRows]) -> dict[str, list[_RhMessageNode]]:
    table = tables.get("message_node")
    out: dict[str, list[_RhMessageNode]] = {}
    if not table:
        return out
    for row in table.dicts():
        node = _RhMessageNode(
            node_id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            node_index=int(row["node_index"]),
            messages=[m for m in _json_list(row["messages"])],
            select_index=int(row["select_index"]),
        )
        out.setdefault(node.conversation_id, []).append(node)
    for nodes in out.values():
        nodes.sort(key=lambda item: item.node_index)
    return out


def _conversation_from_row(row: dict[str, Any],
                           nodes: list[_RhMessageNode]) -> Conv:
    conv_id = str(row["id"])
    created_ms = _int_or_zero(row.get("create_at"))
    updated_ms = _int_or_zero(row.get("update_at"))
    config = _json_obj(row.get("config"))
    conv = Conv(
        id=conv_id,
        title=str(row.get("title") or ""),
        created_ms=created_ms,
        updated_ms=updated_ms,
        assistant_id=str(row.get("assistant_id") or ""),
        is_pinned=bool(row.get("is_pinned")),
        suggestions=[str(item) for item in _json_list(row.get("suggestions"))
                     if isinstance(item, (str, int, float))],
        native=dict(row),
    )
    if config:
        conv.chat_model_provider = config.get("chatModelProvider") if isinstance(
            config.get("chatModelProvider"), str) else None
        conv.chat_model_id = config.get("chatModelId") if isinstance(
            config.get("chatModelId"), str) else None
        conv.extras["config"] = config
    if row.get("custom_system_prompt"):
        conv.extras["customSystemPrompt"] = str(row["custom_system_prompt"])
    mode_ids = [str(item) for item in _json_list(row.get("mode_injection_ids"))]
    if mode_ids:
        conv.extras["modeInjectionIds"] = mode_ids
    lorebook_ids = [str(item) for item in _json_list(row.get("lorebook_ids"))]
    if lorebook_ids:
        conv.extras["lorebookIds"] = lorebook_ids
    if row.get("workspace_cwd"):
        conv.extras["workspaceCwd"] = str(row["workspace_cwd"])
    if row.get("folder_id"):
        conv.extras["folderId"] = str(row["folder_id"])
    if not nodes:
        nodes = _embedded_nodes(row)
    checkpoint_slot: int | None = None
    for node in nodes:
        group = RevisionGroup(
            conversation_id=conv_id,
            group_id=node.node_id,
            subgroup_id=None,
            message_ids=[str(m.get("id")) for m in node.messages],
            version_numbers=list(range(len(node.messages))),
            selected_message_id=str(node.messages[node.select_index].get("id")),
        )
        conv.groups.append(group)
        for index, raw in enumerate(node.messages):
            message = message_from_native(
                raw, conv_id, node.node_id, created_ms, index)
            if message.is_context_checkpoint:
                checkpoint_slot = len(conv.groups) - 1
            conv.messages.append(message)
    if checkpoint_slot is not None:
        conv.truncate_slot = checkpoint_slot
    if not conv.assistant_id:
        conv.assistant_id = DEFAULT_ASSISTANT_ID
    return conv


def _embedded_nodes(row: dict[str, Any]) -> list[_RhMessageNode]:
    conv_id = str(row["id"])
    out: list[_RhMessageNode] = []
    for index, node in enumerate(_json_list(row.get("nodes"))):
        if not isinstance(node, dict):
            continue
        raw_messages = node.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            continue
        selected = node.get("selectIndex", 0)
        out.append(_RhMessageNode(
            node_id=str(node.get("id") or node_uuid(conv_id, str(index))),
            conversation_id=conv_id,
            node_index=index,
            messages=raw_messages,
            select_index=int(selected) if isinstance(selected, int) else 0,
        ))
    return out


def _memory_docs(tables: dict[str, TableRows]) -> list[MemoryDoc]:
    docs: list[MemoryDoc] = []
    for row in _table_dicts(tables, "memory_file"):
        docs.append(MemoryDoc(
            assistant_id=str(row["assistant_id"]),
            path=str(row["path"]),
            content=str(row["content"] or ""),
            updated_ms=int(row["updated_at"] or 0),
        ))
    return docs


def _legacy_memory_entries(tables: dict[str, TableRows]) -> list[model.MemoryEntry]:
    rows = _table_dicts(tables, "MemoryEntity")
    entries: list[model.MemoryEntry] = []
    for row in rows:
        entries.append(model.MemoryEntry(
            id="rh-legacy-%d" % (len(entries) + 1),
            scope="assistant",
            assistant_id=str(row["assistant_id"]),
            type="instruction",
            status="active",
            content=str(row["content"] or ""),
            source="manual",
        ))
    return entries


def _assistant_flags(settings: dict[str, Any]) -> dict[str, model.AssistantMemoryFlags]:
    flags: dict[str, model.AssistantMemoryFlags] = {}
    assistants = settings.get("assistants")
    if isinstance(assistants, list):
        for assistant in assistants:
            if not isinstance(assistant, dict) or not is_uuid(assistant.get("id")):
                continue
            flags[str(assistant["id"])] = model.AssistantMemoryFlags(
                use_global=bool(assistant.get("useGlobalMemory")),
                enable=bool(assistant.get("enableMemory", True)),
            )
    return flags


def _workspaces(tables: dict[str, TableRows]) -> list[Workspace]:
    out: list[Workspace] = []
    for row in _table_dicts(tables, "workspaces"):
        out.append(Workspace(
            id=str(row["id"]),
            name=str(row["name"] or ""),
            created_ms=int(row["created_at"] or 0),
            updated_ms=int(row["updated_at"] or 0),
            last_used_ms=_opt_int(row.get("last_access_at")),
            root=str(row["root"] or ""),
            shell_status=str(row["shell_status"] or ""),
            tool_approvals=str(row["tool_approvals"] or "{}"),
            shell_compatibility_mode=_opt_int(row.get("shell_compatibility_mode")),
            native=dict(row),
        ))
    return out


def _skills(assets: Iterable[Asset]) -> list[model.Skill]:
    grouped: dict[str, list[str]] = {}
    for asset in assets:
        if not asset.entry.startswith("skills/"):
            continue
        parts = asset.entry.split("/")
        if len(parts) < 3:
            continue
        grouped.setdefault(parts[1], []).append(asset.entry)
    skills: list[model.Skill] = []
    for name, entries in grouped.items():
        if not any(entry == f"skills/{name}/SKILL.md" for entry in entries):
            continue
        skills.append(model.Skill(id=name, name=name, entries=entries))
    return skills


# ---------------------------------------------------------------------------
# Snapshot -> native payload
# ---------------------------------------------------------------------------

def encode_from_snapshot(snapshot: Snapshot, target: TargetProfile,
                         settings: model.SettingsResult,
                         asset_entries: list[Asset]) -> tuple[RikkaPayload, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    version = target.schema_version or schema.RH_LATEST
    if version not in schema.RH_VERSIONS:
        raise ArchiveError(f"目标 RikkaHub schema {version} 未查证")
    room = schema.load_room(version)
    if not snapshot.has_chat_domain:
        payload_settings, settings_diagnostics = migrate_settings(
            dict(settings.settings), version)
        diagnostics.extend(settings_diagnostics)
        return RikkaPayload(schema_version=version,
                            settings=payload_settings, tables={}), diagnostics
    tables: dict[str, TableRows] = {
        entity.table: TableRows(columns=tuple(f.column for f in entity.fields),
                                rows=[])
        for entity in room.entities
    }
    dropped_annotations = [0, 0, 0]
    conversation_rows, node_rows = _encode_conversations(
        snapshot, version, settings, dropped_annotations, diagnostics)
    _project_rows(tables["ConversationEntity"], conversation_rows)
    if "message_node" in tables:
        _project_rows(tables["message_node"], node_rows)
    if "favorites" in tables:
        for fav in snapshot.favorites.rows if snapshot.favorites else []:
            tables["favorites"].rows.append(fav)
    if "conversation_folder" in tables:
        for folder in snapshot.folders.rows if snapshot.folders else []:
            tables["conversation_folder"].rows.append(folder)
    if "GenMediaEntity" in tables and snapshot.gen_media:
        for row in snapshot.gen_media.rows:
            tables["GenMediaEntity"].rows.append(row)
    if "translation_history" in tables and snapshot.translations:
        for row in snapshot.translations.rows:
            tables["translation_history"].rows.append(row)
    if "memory_consolidation" in tables and snapshot.consolidation:
        for row in snapshot.consolidation.rows:
            tables["memory_consolidation"].rows.append(row)
    if "workspaces" in tables:
        tables["workspaces"].rows.extend(
            _encode_workspaces(snapshot, version, settings, diagnostics))
    _encode_memory(snapshot, version, tables, diagnostics)
    if "usage_record" in tables:
        tables["usage_record"].rows.extend(
            _encode_usage(snapshot, version, diagnostics))
    managed = _managed_file_rows(asset_entries)
    if managed is not None and "managed_files" in tables:
        tables["managed_files"].rows.extend(managed)
    if "media_creation_session" in tables and snapshot.media:
        for table_name in ("media_creation_session", "media_creation_node",
                           "media_creation_record"):
            source = snapshot.media.get(table_name)
            if source is None:
                continue
            target_table = tables[table_name]
            positions = [target_table.columns.index(column)
                         for column in source.columns
                         if column in target_table.columns]
            for row in source.rows:
                target_row: list[Any] = [None] * len(target_table.columns)
                for source_index, column in enumerate(source.columns):
                    if column in target_table.columns:
                        target_row[target_table.columns.index(column)] = \
                            row[source_index]
                target_table.rows.append(tuple(target_row))
    if dropped_annotations[0]:
        diagnostics.append(Diagnostic(
            "unsupported_data", "message annotations",
            dropped_annotations[0], "RikkaHub 无法表达注解"))
    if dropped_annotations[1]:
        diagnostics.append(Diagnostic(
            "unsupported_data", "message part", dropped_annotations[1],
            "RikkaHub 无法表达的消息部件"))
    if dropped_annotations[2]:
        diagnostics.append(Diagnostic(
            "value_defaulted", "timestamp", dropped_annotations[2],
            "消息缺少时间，已使用会话创建时间"))
    payload_settings, settings_diagnostics = migrate_settings(
        dict(settings.settings), version)
    diagnostics.extend(settings_diagnostics)
    tables["__android_metadata"] = TableRows(
        columns=("locale",), rows=[("en_US",)])
    return RikkaPayload(schema_version=version, settings=payload_settings,
                        tables=tables), diagnostics


def _project_rows(table: TableRows, values: list[dict[str, Any]]) -> None:
    for row in values:
        table.rows.append(tuple(row.get(column) for column in table.columns))


def _encode_conversations(
    snapshot: Snapshot, version: int, settings: model.SettingsResult,
    dropped: list[int], diagnostics: list[Diagnostic],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conv_rows: list[dict[str, Any]] = []
    node_rows: list[dict[str, Any]] = []
    for conv in snapshot.conversations:
        conv_uuid = entity_uuid(conv.id, "conversation")
        assistant_uuid = settings.assistant_ids.get(
            conv.assistant_id or "") or DEFAULT_ASSISTANT_ID
        config = dict(conv.extras.get("config") or {})
        config.pop("chatModelProvider", None)
        provider = conv.chat_model_provider
        model = conv.chat_model_id
        if model:
            mapped = settings.model_ids.get((provider or "", model))
            if mapped:
                config["chatModelId"] = mapped
            else:
                config.pop("chatModelId", None)
                diagnostics.append(Diagnostic(
                    "identity_reference_unresolved", "conversation model", 1,
                    "会话的模型引用无法映射到目标模型"))
        workspace_id = conv.extras.get("workspaceId") or config.get(
            "workspaceId")
        if isinstance(workspace_id, str) and workspace_id:
            config["workspaceId"] = settings.workspace_ids.get(
                workspace_id) or entity_uuid(workspace_id, "workspace")
        skills = conv.extras.get("enabledSkills")
        if isinstance(skills, list):
            config["enabledSkills"] = [str(name) for name in skills
                                       if isinstance(name, str)]
        mcp_servers = [settings.entity_ids.get("mcp", {}).get(server_id)
                       for server_id in conv.mcp_ids]
        mcp_servers = [item for item in mcp_servers if item]
        if mcp_servers:
            config["mcpServers"] = mcp_servers
        config.setdefault("reasoningLevel", "AUTO")
        config.setdefault("enableWebSearch", False)
        config.setdefault("builtInSearch", False)
        workspace_cwd = str(conv.extras.get("workspaceCwd") or "")
        folder_id = str(conv.extras.get("folderId") or "")
        node_index = 0
        for group in conv.groups:
            group_id = entity_uuid(group.group_id, "node")
            node_id = node_uuid(conv_uuid, group_id)
            native_messages: list[dict[str, Any]] = []
            selected_index = 0
            explicit_selection = False
            for index, message in enumerate(conv.messages):
                if message.group_id != group.group_id:
                    continue
                model_uuid = None
                if message.model_id:
                    model_uuid = settings.model_ids.get(
                        (message.provider_id or "", message.model_id))
                native = message_to_native(
                    message, model_uuid=model_uuid,
                    dropped_annotations=dropped, fallback_ms=conv.created_ms)
                native_messages.append(native)
                if group.selected_message_id and \
                        message.id == group.selected_message_id:
                    selected_index = len(native_messages) - 1
                    explicit_selection = group.selection_explicit
            if not native_messages:
                continue
            if not explicit_selection:
                selected_index = len(native_messages) - 1
            node_rows.append({
                "id": node_id,
                "conversation_id": conv_uuid,
                "node_index": node_index,
                "messages": json.dumps(native_messages, ensure_ascii=False),
                "select_index": selected_index,
            })
            node_index += 1
        conv_rows.append({
            "id": conv_uuid,
            "assistant_id": assistant_uuid,
            "title": conv.title,
            "nodes": "[]",
            "create_at": conv.created_ms,
            "update_at": conv.updated_ms,
            "suggestions": json.dumps(conv.suggestions, ensure_ascii=False),
            "is_pinned": 1 if conv.is_pinned else 0,
            "custom_system_prompt": str(
                conv.extras.get("customSystemPrompt") or ""),
            "mode_injection_ids": json.dumps(
                conv.extras.get("modeInjectionIds") or [], ensure_ascii=False),
            "lorebook_ids": json.dumps(
                conv.extras.get("lorebookIds") or [], ensure_ascii=False),
            "workspace_cwd": workspace_cwd,
            "folder_id": folder_id,
            "config": json.dumps(config, ensure_ascii=False),
        })
    return conv_rows, node_rows


def _encode_workspaces(snapshot: Snapshot, version: int,
                       settings: model.SettingsResult,
                       diagnostics: list[Diagnostic]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for workspace in snapshot.workspaces:
        workspace_uuid = settings.workspace_ids.get(workspace.id) or entity_uuid(
            workspace.id, "workspace")
        rows.append((
            workspace_uuid,
            workspace.name,
            workspace_uuid,
            "BROKEN",
            workspace.created_ms,
            workspace.updated_ms,
            workspace.last_used_ms,
            "{}",
        ) + ((0,) if version >= 25 else ()))
        diagnostics.append(Diagnostic(
            "workspace_metadata_only", "workspace", 1,
            "工作区只迁移了元数据；目录内容、rootfs 与执行策略未恢复"))
    return rows


def _managed_file_rows(assets: list[Asset]) -> list[tuple[Any, ...]] | None:
    rows: list[tuple[Any, ...]] = []
    for asset in assets:
        if not asset.entry.startswith(("upload/", "fonts/", "media_creation/")):
            continue
        folder, _, _ = asset.entry.partition("/")
        rows.append((
            None,
            folder,
            asset.entry,
            PurePosixPath(asset.entry).name,
            mimetypes.guess_type(asset.entry)[0] or "application/octet-stream",
            asset.size,
            asset.mtime * 1000,
            asset.mtime * 1000,
        ))
    return rows


def _encode_memory(snapshot: Snapshot, version: int,
                   tables: dict[str, TableRows],
                   diagnostics: list[Diagnostic]) -> None:
    if version >= 28:
        table = tables["memory_file"]
        for doc in snapshot.memory_docs:
            table.rows.append((
                entity_uuid(doc.assistant_id, "assistant"), doc.path,
                doc.content, doc.updated_ms))
        if snapshot.memory_entries:
            diagnostics.append(Diagnostic(
                "unsupported_data", "memory entry",
                len(snapshot.memory_entries),
                "RikkaHub 的 Markdown 记忆无法表达 typed memory 检索与关系"))
        return
    table = tables["MemoryEntity"]
    for doc in snapshot.memory_docs:
        table.rows.append((None, entity_uuid(doc.assistant_id, "assistant"),
                           doc.content))
    for entry in snapshot.memory_entries:
        if entry.status != "active":
            diagnostics.append(Diagnostic(
                "unsupported_data", "memory entry", 1,
                "legacy RikkaHub 记忆没有 archived 状态"))
            continue
        owner = entry.assistant_id or "__global__"
        table.rows.append((None, entity_uuid(owner, "assistant"), entry.content))


def _encode_usage(snapshot: Snapshot, version: int,
                  diagnostics: list[Diagnostic]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    if snapshot.usage is not None:
        # A real ledger is authoritative, including an empty one.
        target_columns = tuple(
            field.column for field in
            schema.load_room(version).tables["usage_record"].fields)
        positions = {name: index for index, name in enumerate(
            snapshot.usage.columns)}
        for row in snapshot.usage.rows:
            target_row: list[Any] = [None] * len(target_columns)
            for index, column in enumerate(target_columns):
                if column in positions:
                    target_row[index] = row[positions[column]]
            rows.append(tuple(target_row))
        return rows
    for conv in snapshot.conversations:
        for message in conv.messages:
            if message.role != "assistant" or message.usage is None:
                continue
            usage = message.usage
            if usage.prompt <= 0 and usage.completion <= 0:
                continue
            created_ms = message.created_ms if message.created_ms is not None \
                else conv.created_ms
            rows.append((
                None,
                created_ms,
                "CHAT",
                None,
                None,
                (message.model_snapshot or {}).get("modelId")
                if isinstance(message.model_snapshot, dict) else None,
                None,
                conv.id,
                usage.prompt,
                usage.completion,
                usage.cached,
            ))
    if rows:
        diagnostics.append(Diagnostic(
            "usage_history_reconstructed", "usage", len(rows),
            "源没有独立用量流水，已按消息自身用量重建 CHAT 记录"))
    return rows


# ---------------------------------------------------------------------------
# Settings: native V1–V5 migrations and legacy downgrade
# ---------------------------------------------------------------------------

_LEGACY_PART_TYPES = {
    "me.rerere.ai.ui.UIMessagePart.Text": "text",
    "me.rerere.ai.ui.UIMessagePart.Image": "image",
    "me.rerere.ai.ui.UIMessagePart.Video": "video",
    "me.rerere.ai.ui.UIMessagePart.Audio": "audio",
    "me.rerere.ai.ui.UIMessagePart.Document": "document",
    "me.rerere.ai.ui.UIMessagePart.Reasoning": "reasoning",
    "me.rerere.ai.ui.UIMessagePart.Search": "search",
    "me.rerere.ai.ui.UIMessagePart.ToolCall": "tool_call",
    "me.rerere.ai.ui.UIMessagePart.ToolResult": "tool_result",
    "me.rerere.ai.ui.UIMessagePart.Tool": "tool",
    "UIMessagePart.Text": "text",
    "UIMessagePart.Image": "image",
    "UIMessagePart.Video": "video",
    "UIMessagePart.Audio": "audio",
    "UIMessagePart.Document": "document",
    "UIMessagePart.Reasoning": "reasoning",
    "UIMessagePart.Search": "search",
    "UIMessagePart.ToolCall": "tool_call",
    "UIMessagePart.ToolResult": "tool_result",
    "UIMessagePart.Tool": "tool",
    "Text": "text", "Image": "image", "Video": "video", "Audio": "audio",
    "Document": "document", "Reasoning": "reasoning", "Search": "search",
    "ToolCall": "tool_call", "ToolResult": "tool_result", "Tool": "tool",
}


def migrate_settings(settings: dict[str, Any],
                     target_version: int) -> tuple[dict[str, Any], list[Diagnostic]]:
    """Apply the app's published settings JSON migrations (V1–V5)."""
    root = json.loads(json.dumps(settings))
    diagnostics: list[Diagnostic] = []
    mcp = root.get("mcpServers")
    if isinstance(mcp, list):
        changed = 0
        for entry in mcp:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "me.rerere.rikkahub.data.mcp.McpServerConfig.SseTransportServer":
                entry["type"] = "sse"
                changed += 1
            elif kind == \
                    "me.rerere.rikkahub.data.mcp.McpServerConfig.StreamableHTTPServer":
                entry["type"] = "streamable_http"
                changed += 1
        if changed:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "mcp server", changed,
                "MCP 传输类型已改为短名"))
    assistants = root.get("assistants")
    if isinstance(assistants, list):
        part_changes = 0
        quick_extracted: list[dict[str, Any]] = []
        injection_changed = 0
        for assistant in assistants:
            if not isinstance(assistant, dict):
                continue
            presets = assistant.get("presetMessages")
            if isinstance(presets, list):
                for preset in presets:
                    if not isinstance(preset, dict):
                        continue
                    parts = preset.get("parts")
                    if isinstance(parts, list):
                        part_changes += _migrate_parts(parts)
            quick = assistant.get("quickMessages")
            if isinstance(quick, list):
                ids: list[str] = []
                for index, message in enumerate(quick):
                    if not isinstance(message, dict):
                        continue
                    message_id = message.get("id") or str(_uuid.uuid5(
                        _ENTITY_NS,
                        f"quick-message:{assistant.get('id')}:{index}"))
                    message["id"] = message_id
                    ids.append(message_id)
                    quick_extracted.append(message)
                assistant.pop("quickMessages", None)
                assistant["quickMessageIds"] = ids
            if "allowConversationPromptInjection" in assistant:
                if assistant.get("allowConversationPromptInjection") is True:
                    assistant["modeInjectionIds"] = []
                    assistant["lorebookIds"] = []
                assistant.pop("allowConversationPromptInjection", None)
                injection_changed += 1
        if part_changes:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "assistant preset part",
                part_changes, "预设消息的部件类型已改为短名"))
        if injection_changed:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "assistant injection flag",
                injection_changed,
                "已移除旧的会话注入开关并按其规则清空关联"))
        if quick_extracted:
            existing = root.get("quickMessages")
            existing_ids = {
                item.get("id") for item in existing
                if isinstance(item, dict)} if isinstance(existing, list) else set()
            merged = list(existing) if isinstance(existing, list) else []
            added = [item for item in quick_extracted
                     if item.get("id") not in existing_ids]
            root["quickMessages"] = merged + added
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "quick message", len(added),
                "助手内嵌的快捷短语已提取为全局快捷短语"))
    providers = root.get("providers")
    if isinstance(providers, list):
        converted = 0
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            if _migrate_quota_query(provider):
                converted += 1
        if converted:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "provider quota",
                converted, "余额查询 balanceOption 已改为 quotaQuery"))
    if target_version < 31:
        root = _drop_account_providers(root, diagnostics)
        root = _downgrade_quota_query(root, diagnostics)
    return root, diagnostics


def _migrate_parts(parts: list[Any]) -> int:
    changed = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        mapped = _LEGACY_PART_TYPES.get(kind)
        if mapped and mapped != kind:
            part["type"] = mapped
            changed += 1
        output = part.get("output")
        if isinstance(output, list):
            changed += _migrate_parts(output)
    return changed


def _migrate_quota_query(provider: dict[str, Any]) -> bool:
    changed = False
    balance = provider.pop("balanceOption", None)
    if isinstance(balance, dict) and provider.get("type") == "openai" \
            and "quotaQuery" not in provider:
        api_path = balance.get("apiPath") or "/credits"
        provider["quotaQuery"] = {
            "enabled": bool(balance.get("enabled")),
            "url": api_path if str(api_path).startswith("http")
            else "{{baseUrl}}" + str(api_path),
            "headers": [{"name": "Authorization",
                         "value": "Bearer {{apiKey}}"}],
            "resultPath": balance.get("resultPath") or "data.total_usage",
        }
        changed = True
    models = provider.get("models")
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            override = model.get("providerOverwrite")
            if isinstance(override, dict):
                changed = _migrate_quota_query(override) or changed
    return changed


def _drop_account_providers(settings: dict[str, Any],
                            diagnostics: list[Diagnostic]) -> dict[str, Any]:
    """Remove OAuth account providers a legacy RikkaHub cannot deserialize."""
    providers = settings.get("providers")
    if not isinstance(providers, list):
        return settings
    kept: list[dict[str, Any]] = []
    dropped_ids: set[str] = set()
    dropped_models: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if provider.get("type") in ("chatgpt", "grok"):
            dropped_ids.add(str(provider.get("id")))
            for model in _as_list_dicts(provider.get("models")):
                dropped_models.add(str(model.get("id")))
            continue
        kept.append(provider)
    if not dropped_ids:
        return settings
    settings["providers"] = kept
    available_models = [str(model.get("id"))
                        for provider in kept
                        for model in _as_list_dicts(provider.get("models"))]
    for assistant in _as_list_dicts(settings.get("assistants")):
        if str(assistant.get("chatModelId")) in dropped_models:
            assistant["chatModelId"] = None
    favorites = settings.get("favoriteModels")
    if isinstance(favorites, list):
        settings["favoriteModels"] = [
            item for item in favorites if str(item) not in dropped_models]
    for key in ("chatModelId", "fastModelId", "titleModelId",
                "suggestionModelId", "imageGenerationModelId", "ocrModelId",
                "compressModelId", "translateModeId"):
        if str(settings.get(key)) in dropped_models:
            if available_models:
                settings[key] = available_models[0]
            else:
                settings.pop(key, None)
    diagnostics.append(Diagnostic(
        "unsupported_data", "provider", len(dropped_ids),
        "legacy RikkaHub 没有账号型提供商，已丢弃并清理引用"))
    return settings


def _as_list_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] \
        if isinstance(value, list) else []


def _downgrade_quota_query(settings: dict[str, Any],
                           diagnostics: list[Diagnostic]) -> dict[str, Any]:
    """Express quotaQuery as the legacy balanceOption when it is equivalent."""
    providers = settings.get("providers")
    dropped = 0
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            quota = provider.pop("quotaQuery", None)
            if quota is None:
                continue
            api_path = None
            headers = quota.get("headers")
            equivalent = (
                isinstance(quota, dict)
                and isinstance(quota.get("url"), str)
                and isinstance(headers, list)
                and len(headers) == 1
                and isinstance(headers[0], dict)
                and headers[0].get("name") == "Authorization"
                and headers[0].get("value") == "Bearer {{apiKey}}"
            )
            if equivalent:
                url = str(quota["url"])
                prefix = "{{baseUrl}}"
                api_path = url[len(prefix):] if url.startswith(prefix) else url
                provider["balanceOption"] = {
                    "enabled": bool(quota.get("enabled")),
                    "apiPath": api_path,
                    "resultPath": quota.get("resultPath")
                    or "data.total_usage",
                }
            else:
                dropped += 1
            models = provider.get("models")
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and isinstance(
                            model.get("providerOverwrite"), dict):
                        _downgrade_quota_query(
                            {"providers": [model["providerOverwrite"]]},
                            diagnostics)
    if dropped:
        diagnostics.append(Diagnostic(
            "unsupported_data", "provider quota", dropped,
            "legacy RikkaHub 无法表达该额度查询"))
    return settings


# ---------------------------------------------------------------------------
# Native tables -> target native version
# ---------------------------------------------------------------------------

def _empty_payload(version: int, settings: dict[str, Any]) -> RikkaPayload:
    room = schema.load_room(version)
    tables = {
        entity.table: TableRows(
            columns=tuple(field.column for field in entity.fields), rows=[])
        for entity in room.entities
    }
    return RikkaPayload(schema_version=version, settings=settings, tables=tables)


def migrate_native(backup: Backup, target: TargetProfile
                   ) -> tuple[RikkaPayload, list[Diagnostic]]:
    assert isinstance(backup.payload, RikkaPayload)
    source = backup.payload
    target_version = target.schema_version or schema.RH_LATEST
    if target_version not in schema.RH_VERSIONS:
        raise ArchiveError(f"目标 RikkaHub schema {target_version} 未查证")
    settings, diagnostics = migrate_settings(source.settings, target_version)
    if not source.tables:
        target_payload = RikkaPayload(schema_version=target_version,
                                      settings=settings, tables={})
        return target_payload, diagnostics
    target_payload = _empty_payload(target_version, settings)
    source_version = source.schema_version
    source_room = schema.load_room(source_version)
    target_room = schema.load_room(target_version)
    target_tables = target_payload.tables
    for table in target_room.tables:
        source_table = source.tables.get(table)
        if source_table is None:
            continue
        target_table = target_tables[table]
        for row in source_table.rows:
            target_row: list[Any] = [None] * len(target_table.columns)
            for source_index, column in enumerate(source_table.columns):
                if column in target_table.columns:
                    target_row[target_table.columns.index(column)] = \
                        row[source_index]
            target_table.rows.append(tuple(target_row))
    # New columns carry the schema's documented default.
    for table, entity in target_room.tables.items():
        target_table = target_tables[table]
        for field in entity.fields:
            if field.default is None:
                continue
            index = target_table.columns.index(field.column)
            default = _sql_literal(field.default)
            for row_number, row in enumerate(target_table.rows):
                if row[index] is None:
                    mutable = list(row)
                    mutable[index] = default
                    target_table.rows[row_number] = tuple(mutable)
    if source_version < 28 <= target_version:
        docs, migrated = _memory_entity_to_files(source.tables)
        target_tables["memory_file"].rows.extend(docs)
        if migrated:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "memory", migrated,
                "旧逐条记忆已按上游迁移合并为 profile 文件；源没有记忆时钟，"
                "updated_at 为本次迁移时间"))
    elif source_version >= 28 > target_version:
        rows, count = _memory_files_to_entities(
            source.tables)
        target_tables["MemoryEntity"].rows.extend(rows)
        if count:
            diagnostics.append(Diagnostic(
                "historical_type_normalized", "memory", count,
                "Markdown 记忆已降级为 legacy 逐条记忆"))
    if source_version < 29 <= target_version:
        conv_table = source.tables.get("ConversationEntity")
        if conv_table is not None:
            id_index = conv_table.index("id")
            update_index = conv_table.index("update_at")
            target_tables["memory_consolidation"].rows.extend(
                (str(row[id_index]), int(row[update_index]))
                for row in conv_table.rows)
    if source_version < 31 <= target_version:
        rows, timestamp_fallbacks = _reconstruct_usage(source.tables)
        target_tables["usage_record"].rows.extend(rows)
        if rows:
            diagnostics.append(Diagnostic(
                "usage_history_reconstructed", "usage", len(rows),
                "按上游迁移从消息用量回填 CHAT 记录"))
        if timestamp_fallbacks:
            diagnostics.append(Diagnostic(
                "value_defaulted", "timestamp", timestamp_fallbacks,
                "用量记录的消息缺少时间，已使用会话创建时间"))
    if source_version > target_version:
        dropped_new = 0
        for table in source_room.tables:
            if table not in target_room.tables:
                dropped_new += len(source.tables.get(table, TableRows((), [])).rows)
        if dropped_new:
            diagnostics.append(Diagnostic(
                "unsupported_data", "table", dropped_new,
                f"legacy RikkaHub schema {target_version} 没有这些表"))
        removed_columns = []
        for table in source_room.tables:
            if table not in target_room.tables:
                continue
            target_columns = set(target_tables[table].columns)
            removed = [field.column for field in source_room.tables[table].fields
                       if field.column not in target_columns]
            if removed:
                removed_columns.append(f"{table}.{','.join(removed)}")
        if removed_columns:
            diagnostics.append(Diagnostic(
                "unsupported_data", "column", len(removed_columns),
                "legacy RikkaHub schema 没有这些列: "
                + "; ".join(removed_columns[:6])))
        json_losses = _legacy_json_field_losses(
            source.tables, target_version)
        for category, count in json_losses.items():
            if count:
                diagnostics.append(Diagnostic(
                    "unsupported_data", category, count,
                    f"legacy RikkaHub schema 没有对应的消息字段 {category}"))
    metadata = source.tables.get("__android_metadata")
    if metadata is not None:
        target_payload.tables["__android_metadata"] = metadata
    target_payload.settings = settings
    return target_payload, diagnostics


def _legacy_json_field_losses(tables: dict[str, TableRows],
                              target_version: int) -> dict[str, int]:
    losses: dict[str, int] = {}
    node = tables.get("message_node")
    if node is not None:
        index = node.index("messages")
        for row in node.rows:
            for message in _json_list(row[index]):
                if not isinstance(message, dict):
                    continue
                if target_version < 31:
                    for field, label in (
                            ("lastUsage", "lastUsage"),
                            ("stepCount", "stepCount"),
                            ("generationTimeMs", "generationTimeMs"),
                            ("isContextCheckpoint", "上下文检查点")):
                        if message.get(field):
                            losses[label] = losses.get(label, 0) + 1
    conv = tables.get("ConversationEntity")
    if conv is not None and target_version < 27:
        config_index = (conv.columns.index("config")
                        if "config" in conv.columns else None)
        if config_index is not None:
            count = sum(1 for row in conv.rows
                        if str(row[config_index] or "").strip() not in ("", "{}", "null"))
            if count:
                losses["会话固定配置"] = count
    return losses


def _sql_literal(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        return text


def _memory_entity_to_files(tables: dict[str, TableRows]) -> tuple[list[tuple[Any, ...]], int]:
    table = tables.get("MemoryEntity")
    if table is None:
        return [], 0
    by_assistant: dict[str, list[str]] = {}
    id_index = table.index("id")
    assistant_index = table.index("assistant_id")
    content_index = table.index("content")
    for row in sorted(table.rows, key=lambda item: item[id_index]):
        by_assistant.setdefault(str(row[assistant_index]), []).append(
            str(row[content_index]))
    now = _now_ms()
    rows: list[tuple[Any, ...]] = []
    for assistant_id, contents in by_assistant.items():
        lines = []
        for content in contents:
            collapsed = " ".join(line.strip() for line in content.splitlines()).strip()
            if collapsed:
                lines.append(collapsed)
        if not lines:
            continue
        profile = ("---\nname: profile\ndescription: Who the user is\n---\n\n"
                   + "".join(f"- [stated] {line}\n" for line in lines))
        rows.append((assistant_id, "/profile.md", profile, now))
    return rows, len(rows)


def _memory_files_to_entities(tables: dict[str, TableRows]) -> tuple[list[tuple[Any, ...]], int]:
    table = tables.get("memory_file")
    if table is None:
        return [], 0
    assistant_index = table.index("assistant_id")
    content_index = table.index("content")
    rows = [(None, str(row[assistant_index]), str(row[content_index]))
            for row in table.rows]
    return rows, len(rows)


def _reconstruct_usage(
    tables: dict[str, TableRows]
) -> tuple[list[tuple[Any, ...]], int]:
    """The upstream 30->31 backfill, replayed over the source message rows."""
    conv_table = tables.get("ConversationEntity")
    node_table = tables.get("message_node")
    if conv_table is None or node_table is None:
        return [], 0
    conv_ids = conv_table.index("id")
    conv_created = conv_table.index("create_at")
    conv_assistant = conv_table.index("assistant_id")
    creates = {str(row[conv_ids]): int(row[conv_created]) for row in conv_table.rows}
    assistants = {str(row[conv_ids]): str(row[conv_assistant])
                 for row in conv_table.rows}
    node_conv = node_table.index("conversation_id")
    node_messages = node_table.index("messages")
    rows: list[tuple[Any, ...]] = []
    timestamp_fallbacks = 0
    for node in node_table.rows:
        conversation_id = str(node[node_conv])
        for message in _json_list(node[node_messages]):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            prompt = _int_or_zero(usage.get("promptTokens"))
            completion = _int_or_zero(usage.get("completionTokens"))
            if prompt <= 0 and completion <= 0:
                continue
            created = _iso_to_ms(message.get("createdAt"))
            if created is None:
                created = creates.get(conversation_id, 0)
                timestamp_fallbacks += 1
            snapshot = message.get("modelSnapshot")
            model_name = snapshot.get("modelId") if isinstance(snapshot, dict) else None
            rows.append((
                None, created, "CHAT", None,
                message.get("modelId") if isinstance(message.get("modelId"), str) else None,
                model_name if isinstance(model_name, str) else None,
                assistants.get(conversation_id), conversation_id,
                prompt, completion, _int_or_zero(usage.get("cachedTokens")),
            ))
    return rows, timestamp_fallbacks


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_backup(backup: Backup, target: TargetProfile, destination: Path) -> None:
    payload = backup.payload
    assert isinstance(payload, RikkaPayload)
    assets = backup.assets
    database_path: Path | None = None
    if payload.tables:
        database_path = _write_database(payload, destination.parent)
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "settings.json",
                json.dumps(payload.settings, ensure_ascii=False, indent=1))
            if database_path is not None:
                zf.write(database_path, "rikka_hub.db")
            for asset in assets:
                zf.write(asset.staged_path, asset.entry)
    finally:
        if database_path is not None:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(database_path) + suffix)
                if candidate.exists():
                    candidate.unlink()


def _write_database(payload: RikkaPayload, directory: Path) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        prefix="rikkelivo-rh-", suffix=".db", dir=directory, delete=False)
    db_path = Path(handle.name)
    handle.close()
    db_path.unlink()
    warning = schema.create_room_database(db_path, payload.schema_version)
    if warning is None and schema.probe_fts().fts5 is False:  # pragma: no cover
        raise ArchiveError("当前 SQLite 缺少 FTS5，无法创建搜索索引")
    con = sqlite3.connect(str(db_path))
    try:
        tables = payload.tables
        _insert_tables(con, tables, schema.load_room(payload.schema_version))
        _write_fts(con, tables)
        locale_rows = tables.get("__android_metadata")
        if locale_rows and locale_rows.rows:
            con.execute("DELETE FROM android_metadata")
            con.execute("INSERT INTO android_metadata (locale) VALUES (?)",
                        (locale_rows.rows[0][0] or "en_US",))
        con.commit()
        schema.validate_room_database(con, payload.schema_version)
        for table in schema.load_room(payload.schema_version).tables:
            result = con.execute(f'PRAGMA integrity_check("{table}")').fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ArchiveError(f"输出数据库表 {table} 完整性检查失败")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise ArchiveError("输出数据库外键检查失败")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return db_path


_INSERT_ORDER = (
    "ConversationEntity",
    "message_node",
    "MemoryEntity",
    "GenMediaEntity",
    "memory_file",
    "memory_consolidation",
    "favorites",
    "workspaces",
    "conversation_folder",
    "media_creation_session",
    "media_creation_node",
    "media_creation_record",
    "translation_history",
    "usage_record",
    "managed_files",
)


def _insert_tables(con: sqlite3.Connection, tables: dict[str, TableRows],
                   room: schema.RoomSchema) -> None:
    for table in _INSERT_ORDER:
        if table not in room.tables:
            continue
        rows = tables.get(table)
        if rows is None or not rows.rows:
            continue
        quoted = ", ".join(f'"{column}"' for column in rows.columns)
        placeholders = ", ".join("?" for _ in rows.columns)
        con.executemany(
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
            rows.rows)


def _write_fts(con: sqlite3.Connection, tables: dict[str, TableRows]) -> None:
    nodes = tables.get("message_node")
    conversations = tables.get("ConversationEntity")
    if nodes is None or conversations is None:
        return
    titles = {}
    updates = {}
    if conversations is not None:
        id_index = conversations.index("id")
        title_index = conversations.index("title")
        update_index = conversations.index("update_at")
        for row in conversations.rows:
            titles[str(row[id_index])] = str(row[title_index] or "")
            updates[str(row[id_index])] = str(row[update_index])
    node_id_index = nodes.index("id")
    conv_index = nodes.index("conversation_id")
    messages_index = nodes.index("messages")
    rows: list[tuple[str, str, str, str, str, str]] = []
    for node in nodes.rows:
        node_id = str(node[node_id_index])
        conversation_id = str(node[conv_index])
        for message in _json_list(node[messages_index]):
            if not isinstance(message, dict):
                continue
            text = "\n".join(
                str(part.get("text") or "")
                for part in message.get("parts") or []
                if isinstance(part, dict) and part.get("type") == "text")
            rows.append((text, node_id, str(message.get("id") or ""),
                         conversation_id, titles.get(conversation_id, ""),
                         updates.get(conversation_id, "")))
    if rows:
        con.executemany(
            "INSERT INTO message_fts (text, node_id, message_id,"
            " conversation_id, title, update_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows)
