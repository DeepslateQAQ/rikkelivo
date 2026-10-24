"""Kelivo and Cuplivo backup formats.

One family, three payload shapes for chats plus two settings containers:

* ``kelivo-archive`` -- ``manifest.json`` + ``settings.json`` + SQLite
  ``database/kelivo.db`` (archive 2/3, schema 1–3),
* ``kelivo-json`` -- historical ``chats.json`` version 1 (official Kelivo) or
  version 2 (Cuplivo),
* ``cuplivo-jsonl`` -- historical ``conversations.jsonl`` / ``messages.jsonl``
  / ``chats_meta.json`` (format_version 2).

Native SQL follows the frozen Drift schema in ``schemas/kelivo``.  Historical
payloads are read through the same neutral projection; their selection dialect
(actual version vs ordinal) is decided from evidence, never from the root
version alone.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import mimetypes
import re
import sqlite3
import tempfile
import urllib.parse
import uuid as _uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
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
    MemoryEntry,
    Msg,
    Part,
    RevisionGroup,
    KelivoPayload,
    LegacyChatData,
    Snapshot,
    TableRows,
    TargetProfile,
    Usage,
    Workspace,
)
from rikkelivo.times import iso_to_ms, ms_to_iso

KELIVO_URI_PREFIX = "kelivo-file:///"
KELIVO_ASSET_ROOTS: tuple[str, ...] = (
    "upload", "images", "avatars", "fonts", "sessions", "skills", "workspaces",
)
_ENTITY_NS = _uuid.uuid5(_uuid.NAMESPACE_URL, "rikkelivo-entity")

#: Business entity kinds, mirroring the application's BusinessEntityKind.
_ENTITY_KINDS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("assistant", "assistants_v1", "assistant_rows", "id", None),
    ("provider", "provider_configs_v1", "provider_rows", "provider_key", None),
    ("providerGroup", "provider_groups_v1", "provider_group_rows", "id", None),
    ("mcpServer", "mcp_servers_v1", "mcp_server_rows", "id", None),
    ("worldBook", "world_books_v1", "world_book_rows", "id", None),
    ("assistantMemory", "assistant_memories_v1", "assistant_memory_rows", "id", None),
    ("quickPhrase", "quick_phrases_v1", "quick_phrase_rows", "id", None),
    ("searchService", "search_services_v1", "search_service_rows", "id", None),
    ("ttsService", "tts_services_v1", "tts_service_rows", "id", None),
    ("instructionInjection", "instruction_injections_v1",
     "instruction_injection_rows", "id", None),
    ("assistantTag", "assistant_tags_v1", "assistant_tag_rows", "id", None),
    ("memoryEntry", "memory_entries_v1", "memory_entry_rows", "id", None),
    ("workspace", "workspaces_v1", "extension_entity_rows", "id", "workspace"),
    ("skill", "skills_v1", "extension_entity_rows", "id", "skill"),
    ("userProfileField", "user_profile_fields_v1",
     "user_profile_field_rows", "id", None),
)
_ENTITY_KEYS = {kind[1] for kind in _ENTITY_KINDS}
_PROVIDER_ORDER_KEY = "providers_order_v1"
_MEMORY_ENTRY_KEY = "memory_entries_v1"


def _us_to_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value // 1000


def _ms_to_us(value: int | None) -> int | None:
    return None if value is None else int(value) * 1000


def _now_us() -> int:
    return int(_dt.datetime.now().timestamp() * 1_000_000)


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _json_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def kelivo_uri(entry: str) -> str:
    return KELIVO_URI_PREFIX + "/".join(
        urllib.parse.quote(part, safe="") for part in entry.split("/"))


def parse_kelivo_uri(uri: str) -> str | None:
    """Return the asset-root-relative entry of a managed ``kelivo-file`` URI."""
    if not uri.startswith(KELIVO_URI_PREFIX):
        return None
    raw = uri[len(KELIVO_URI_PREFIX):]
    parts = [urllib.parse.unquote(part) for part in raw.split("/") if part]
    if not parts or "/" in parts[-1] and parts[-1].endswith("/"):
        return None
    if ".." in parts or "." in parts:
        return None
    return "/".join(parts)


_LEGACY_ROOT_RE = re.compile(
    r"(?:^|/)(upload|images|avatars|fonts|sessions|skills|workspaces)/")


def normalize_managed_path(path: str) -> str | None:
    """Map an absolute or legacy managed path onto its asset-root entry."""
    if not path:
        return None
    text = path[7:] if path.startswith("file://") else path
    text = text.replace("\\", "/")
    match = _LEGACY_ROOT_RE.search(text)
    if not match:
        return None
    tail = text[match.start(1):]
    parts = [part for part in tail.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Native SQLite reading
# ---------------------------------------------------------------------------

def _staged_db_path(staging: Path) -> Path:
    return staging / "payload" / "database" / "kelivo.db"


def read_backup(zf: zipfile.ZipFile, info: FormatInfo,
                staging: Path) -> Backup:
    from rikkelivo import archive

    staged = archive.load_staged(staging)
    settings_raw = staged.read_json("settings.json") if staged.member(
        "settings.json") else {}
    if settings_raw is None:
        settings_raw = {}
    if not isinstance(settings_raw, dict):
        raise ArchiveError("settings.json 不是 JSON 对象")
    assets: list[Asset] = []
    for entry, member in sorted(staged.members.items()):
        if not any(entry.startswith(root + "/") for root in KELIVO_ASSET_ROOTS):
            continue
        assets.append(Asset(entry=entry, staged_path=member.path,
                            size=member.size, sha256=member.sha256,
                            mtime=member.mtime))
    payload: KelivoPayload
    diagnostics: list[Diagnostic] = []
    if info.kind == model.KIND_KELIVO_ARCHIVE:
        payload = KelivoPayload(settings=settings_raw, scope=info.scope,
                                app_version=info.app_version)
        if info.database_present:
            db_path = _staged_db_path(staging)
            if not db_path.exists():
                raise ArchiveError("暂存目录缺少 database/kelivo.db")
            declared = info.schema_version or schema.DRIFT_LATEST
            version = declared if declared in schema.DRIFT_VERSIONS \
                else schema.DRIFT_LATEST
            tables, extra = _read_drift_tables(db_path, version,
                                               declared=declared)
            payload.tables = tables
            diagnostics.extend(extra)
        backup = Backup(info=info, payload=payload, assets=assets,
                        diagnostics=model.merge_diagnostics(diagnostics),
                        roots=tuple(sorted({asset.entry.split("/")[0]
                                            for asset in assets})))
        return backup
    if info.kind == model.KIND_KELIVO_JSON:
        payload = KelivoPayload(settings=settings_raw, scope=info.scope)
        payload.legacy_chat = _read_legacy_json(staged, info)
        return Backup(info=info, payload=payload, assets=assets,
                      diagnostics=(), roots=tuple(sorted(
                          {asset.entry.split("/")[0] for asset in assets})))
    if info.kind == model.KIND_CUPLIVO_JSONL:
        payload = KelivoPayload(settings=settings_raw, scope=info.scope)
        payload.legacy_chat = _read_jsonl(staged, info)
        return Backup(info=info, payload=payload, assets=assets,
                      diagnostics=(), roots=tuple(sorted(
                          {asset.entry.split("/")[0] for asset in assets})))
    # Settings-only archive.
    payload = KelivoPayload(settings=settings_raw, scope=info.scope)
    return Backup(info=info, payload=payload, assets=assets, diagnostics=(),
                  roots=tuple(sorted({asset.entry.split("/")[0]
                                      for asset in assets})))


def _read_drift_tables(db_path: Path, version: int, *,
                       declared: int | None = None
                       ) -> tuple[dict[str, TableRows], list[Diagnostic]]:
    declared = declared or version
    drift = schema.load_drift(version)
    con = sqlite3.connect(str(db_path))
    try:
        tables = {
            table: schema.read_rows(con, table)
            for table in drift.table_columns
        }
        diagnostics: list[Diagnostic] = []
        if declared > schema.DRIFT_LATEST:
            extra_tables = {
                str(row[0]) for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")
            } - set(drift.table_columns)
            extra_columns = 0
            for table, columns in drift.table_columns.items():
                actual = tuple(str(row[1]) for row in con.execute(
                    f'PRAGMA table_info("{table}")'))
                extra_columns += max(0, len(actual) - len(columns))
            if extra_tables or extra_columns:
                diagnostics.append(Diagnostic(
                    "forward_extensions_dropped", "schema",
                    len(extra_tables) + extra_columns,
                    "新版本备份的未知表／列已按当前已知结构投影"))
        return tables, diagnostics
    finally:
        con.close()


def _read_legacy_json(staged: Any, info: FormatInfo) -> LegacyChatData:
    chats = staged.read_json("chats.json")
    if not isinstance(chats, dict):
        raise ArchiveError("chats.json 不是 JSON 对象")
    version = chats.get("version")
    deleted = None
    if staged.member("deleted.json") is not None:
        deleted = staged.read_json("deleted.json")
        if not isinstance(deleted, dict):
            deleted = None
    return LegacyChatData(
        root_version=int(version) if isinstance(version, int) else 1,
        format_version=None,
        conversations=[c for c in chats.get("conversations") or []],
        messages=[m for m in chats.get("messages") or []],
        tool_events={str(k): [e for e in v if isinstance(e, dict)]
                     for k, v in (chats.get("toolEvents") or {}).items()
                     if isinstance(v, list)},
        gemini_sigs={str(k): str(v) for k, v in
                     (chats.get("geminiThoughtSigs") or {}).items()},
        group_chats=[g for g in chats.get("groupChats") or []
                     if isinstance(g, dict)],
        group_members=[m for m in chats.get("groupMembers") or []
                       if isinstance(m, dict)],
        jsonl=False,
        meta={},
        cuplivo_evidence=_legacy_cuplivo_evidence(chats),
        deleted_document=deleted,
    )


def _read_jsonl(staged: Any, info: FormatInfo) -> LegacyChatData:
    meta = staged.read_json("chats_meta.json")
    conversations = [entry["conversation"] for entry in
                     _jsonl_records(staged, "conversations.jsonl", "conversation")]
    messages = _jsonl_records(staged, "messages.jsonl", "message")
    tool_events: dict[str, list[dict[str, Any]]] = {}
    gemini: dict[str, str] = {}
    plain_messages: list[dict[str, Any]] = []
    for record in messages:
        message = record["message"]
        plain_messages.append(message)
        events = record.get("toolEvents")
        if isinstance(events, list):
            tool_events[str(message.get("id"))] = [
                event for event in events if isinstance(event, dict)]
        signature = record.get("geminiSignature")
        if isinstance(signature, str) and signature:
            gemini[str(message.get("id"))] = signature
    deleted = None
    if staged.member("deleted.json") is not None:
        deleted = staged.read_json("deleted.json")
        if not isinstance(deleted, dict):
            deleted = None
    return LegacyChatData(
        root_version=2,
        format_version=2,
        conversations=conversations,
        messages=plain_messages,
        tool_events=tool_events,
        gemini_sigs=gemini,
        group_chats=[g for g in meta.get("groupChats") or []
                     if isinstance(g, dict)],
        group_members=[m for m in meta.get("groupMembers") or []
                       if isinstance(m, dict)],
        jsonl=True,
        meta=meta,
        cuplivo_evidence=("JSONL2 格式",),
        deleted_document=deleted,
    )


def _jsonl_records(staged: Any, entry: str, wrapper: str) -> list[dict[str, Any]]:
    member = staged.member(entry)
    assert member is not None
    text = member.path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def _legacy_cuplivo_evidence(chats: dict[str, Any]) -> tuple[str, ...]:
    evidence: list[str] = []
    if chats.get("groupChats") or chats.get("groupMembers"):
        evidence.append("Cuplivo 群聊记录")
    for conversation in chats.get("conversations") or []:
        if isinstance(conversation, dict) and conversation.get(
                "conversationKind") == "group":
            evidence.append("Cuplivo conversationKind")
            break
    for message in chats.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("subgroupId") is not None:
            evidence.append("Cuplivo subgroupId")
            break
        if message.get("speakerAssistantId") is not None:
            evidence.append("Cuplivo speakerAssistantId")
            break
        if message.get("isPreset"):
            evidence.append("Cuplivo isPreset")
            break
    return tuple(dict.fromkeys(evidence))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def decode(backup: Backup) -> Snapshot:
    payload = backup.payload
    assert isinstance(payload, KelivoPayload)
    if payload.legacy_chat is not None:
        return _decode_legacy(backup, payload)
    snapshot = Snapshot(settings=dict(payload.settings))
    tables = payload.tables
    if not tables:
        snapshot.has_chat_domain = False
        if "skills" in (backup.info.scope or ()):
            snapshot.skills = _skills_from_assets(backup.assets)
        return snapshot
    snapshot.has_chat_domain = True
    conv_rows = _rows(tables, "conversation_rows")
    messages = _sql_messages(tables)
    for row in conv_rows:
        conv = _conversation_from_sql(row, messages.get(str(row["id"]), []))
        snapshot.conversations.append(conv)
        mcp = _mcp_ids(tables, conv.id)
        conv.mcp_ids = mcp
        if mcp:
            snapshot.mcp_by_conversation[conv.id] = mcp
    snapshot.memory_entries = _memory_entries(tables)
    if not snapshot.memory_entries:
        snapshot.memory_entries = _entries_from_settings(payload.settings)
    snapshot.assistant_flags = _assistant_flags(tables)
    snapshot.workspaces = _workspaces_from_tables(tables)
    if not snapshot.workspaces:
        snapshot.workspaces = _legacy_workspaces(payload.settings)
    snapshot.skills = _skills_from_tables(tables, backup.assets)
    if not snapshot.skills:
        snapshot.skills = _skills_from_settings(payload.settings, backup.assets)
    snapshot.generation_runs = tables.get("generation_run_rows")
    snapshot.tombstones = tables.get("tombstone_rows")
    snapshot.extensions = tables.get("extension_entity_rows")
    snapshot.deleted_document = None
    snapshot.asset_entries = [asset.entry for asset in backup.assets]
    fallbacks = sum(
        1 for conv in snapshot.conversations for message in conv.messages
        if message.native.get("_fallback_timestamp"))
    if fallbacks:
        snapshot.diagnostics = (Diagnostic(
            "value_defaulted", "timestamp", fallbacks,
            "消息缺少时间，已使用会话创建时间"),)
    return snapshot


def _rows(tables: dict[str, TableRows], name: str) -> list[dict[str, Any]]:
    table = tables.get(name)
    return table.dicts() if table else []


def _sql_messages(tables: dict[str, TableRows]) -> dict[str, list[dict[str, Any]]]:
    parts_by_revision: dict[str, list[tuple[int, str, str]]] = {}
    parts_table = tables.get("message_part_rows")
    for part in parts_table.dicts() if parts_table else []:
        parts_by_revision.setdefault(str(part["revision_id"]), []).append(
            (int(part["ordinal"]), str(part["kind"]), str(part["payload"])))
    artifacts_by_revision: dict[str, list[dict[str, Any]]] = {}
    artifacts_table = tables.get("provider_artifact_rows")
    for artifact in artifacts_table.dicts() if artifacts_table else []:
        artifacts_by_revision.setdefault(str(artifact["revision_id"]), []).append(
            artifact)
    by_conversation: dict[str, list[dict[str, Any]]] = {}
    message_table = tables.get("message_rows")
    if message_table is None:
        return by_conversation
    for row in message_table.dicts():
        row = dict(row)
        row["parts"] = sorted(parts_by_revision.get(str(row["id"]), []),
                              key=lambda item: item[0])
        row["artifacts"] = sorted(
            artifacts_by_revision.get(str(row["id"]), []),
            key=lambda item: str(item["kind"]))
        by_conversation.setdefault(str(row["conversation_id"]), []).append(row)
    for rows in by_conversation.values():
        rows.sort(key=lambda item: (int(item["message_order"]), str(item["id"])))
    return by_conversation


_EXTRA_ALIASES = {
    "workspace.id": "workspaceId",
    "workspace.cwd": "workspaceCwd",
    "prompt.system": "customSystemPrompt",
    "prompt.instructionIds": "modeInjectionIds",
    "prompt.worldBookIds": "lorebookIds",
    "skills.ids": "enabledSkills",
}


def _normalize_extras(conv: Conv) -> None:
    for source, target in _EXTRA_ALIASES.items():
        if source in conv.extras and target not in conv.extras:
            conv.extras[target] = conv.extras[source]


def _conversation_from_sql(row: dict[str, Any],
                           messages: list[dict[str, Any]]) -> Conv:
    conv_id = str(row["id"])
    created_ms = _us_to_ms(row.get("created_at")) or 0
    updated_ms = _us_to_ms(row.get("updated_at")) or 0
    extras = _parse_json(row.get("extras_json")) or {}
    conv = Conv(
        id=conv_id,
        title=str(row.get("title") or ""),
        created_ms=created_ms,
        updated_ms=updated_ms,
        assistant_id=str(row["assistant_id"]) if row.get("assistant_id") else None,
        is_pinned=bool(row.get("is_pinned")),
        mcp_ids=[],
        summary=str(row["summary"]) if row.get("summary") is not None else None,
        last_summarized_count=_int(row.get("last_summarized_message_count")),
        suggestions=[str(item) for item in
                     (_parse_json(row.get("chat_suggestions_json")) or [])
                     if isinstance(item, str)],
        chat_model_provider=str(row["chat_model_provider"])
        if row.get("chat_model_provider") else None,
        chat_model_id=str(row["chat_model_id"])
        if row.get("chat_model_id") else None,
        injected_memory_hash=str(row["injected_memory_hash"])
        if row.get("injected_memory_hash") is not None else None,
        last_memory_extracted_order=_int(row.get("last_memory_extracted_order"), -1),
        extras={str(k): v for k, v in extras.items()}
        if isinstance(extras, dict) else {},
        native=dict(row),
    )
    _normalize_extras(conv)
    truncate = _int(row.get("truncate_index"), -1)
    conv.truncate_slot = truncate if truncate >= 0 else None
    selections = _parse_json(row.get("version_selections_json")) or {}
    if not isinstance(selections, dict):
        selections = {}
    groups: dict[tuple[str, str | None, str | None], list[Msg]] = {}
    group_order: list[tuple[str, str | None, str | None]] = []
    for row_message in messages:
        msg = _message_from_sql(row_message, conv_id, created_ms)
        key = (msg.group_id or msg.id, msg.subgroup_id, msg.speaker_id)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(msg)
        conv.messages.append(msg)
    resolved: dict[str, Msg | None] = {}
    for group_id, value in selections.items():
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        candidates = [msg for members in groups.values() for msg in members
                      if (msg.group_id or msg.id) == str(group_id)
                      and msg.version == value]
        if len(candidates) > 1:
            resolved[str(group_id)] = candidates[-1]
        elif candidates:
            resolved[str(group_id)] = candidates[0]
        else:
            resolved[str(group_id)] = None
    for key in group_order:
        members = groups[key]
        group_id = key[0]
        owners = {msg.id for msg in members}
        explicit_selected = resolved.get(group_id)
        explicit = explicit_selected is not None \
            and explicit_selected.id in owners
        if explicit and explicit_selected is not None:
            selected = explicit_selected
        else:
            selected = sorted(
                members,
                key=lambda m: (m.version if m.version is not None else 0))[-1]
        conv.groups.append(RevisionGroup(
            conversation_id=conv_id,
            group_id=group_id,
            subgroup_id=key[1],
            message_ids=[m.id for m in members],
            version_numbers=[m.version or 0 for m in members],
            selected_message_id=selected.id,
            speaker_id=key[2],
            selection_explicit=explicit,
        ))
    if any(message.speaker_id for message in conv.messages):
        conv.conversation_kind = "group"
    return conv


def _message_from_sql(row: dict[str, Any], conversation_id: str,
                      fallback_ms: int) -> Msg:
    parts: list[Part] = []
    for _ordinal, kind, payload in row.get("parts") or []:
        part = _part_from_row(kind, payload)
        if part is not None:
            part.native.setdefault("origin_family", "kelivo")
            parts.append(part)
    group_id = str(row["group_id"]) if row.get("group_id") else str(row["id"])
    timestamp_ms = _us_to_ms(row.get("timestamp"))
    timestamp_fallback = timestamp_ms is None
    if timestamp_fallback:
        timestamp_ms = fallback_ms
    extras_raw = _parse_json(row.get("extras_json"))
    extras = {str(key): value for key, value in extras_raw.items()} \
        if isinstance(extras_raw, dict) else {}
    return Msg(
        id=str(row["id"]),
        role=str(row["role"]),
        parts=parts,
        created_ms=timestamp_ms,
        model_id=str(row["model_id"]) if row.get("model_id") else None,
        provider_id=str(row["provider_id"]) if row.get("provider_id") else None,
        usage=_usage_from_row(row),
        last_usage=_usage_from_extras(row),
        duration_ms=_int(row["duration_ms"]) if row.get("duration_ms") is not None
        else None,
        translation=str(row["translation"]) if row.get("translation") is not None
        else None,
        reasoning_segments_json=str(row["reasoning_segments_json"])
        if row.get("reasoning_segments_json") is not None else None,
        group_id=group_id,
        subgroup_id=None,
        version=_int(row.get("version")),
        speaker_id=str(row["sender_id"]) if row.get("sender_id") else None,
        extras=extras,
        artifacts=[Artifact(
            kind=str(item["kind"]), payload=str(item["payload"]),
            created_ms=(_us_to_ms(item.get("created_at"))
                        if _us_to_ms(item.get("created_at")) is not None
                        else timestamp_ms),
            updated_ms=(_us_to_ms(item.get("updated_at"))
                        if _us_to_ms(item.get("updated_at")) is not None
                        else timestamp_ms),
        ) for item in row.get("artifacts") or []],
        native=dict(row, _fallback_timestamp=timestamp_fallback),
    )


def _usage_from_row(row: dict[str, Any]) -> Usage | None:
    values = [row.get("prompt_tokens"), row.get("completion_tokens"),
              row.get("cached_tokens"), row.get("total_tokens")]
    if all(value is None for value in values):
        return None
    return Usage(
        prompt=_int(row.get("prompt_tokens")),
        completion=_int(row.get("completion_tokens")),
        cached=_int(row.get("cached_tokens")),
        total=_int(row.get("total_tokens")),
    )


def _usage_from_extras(row: dict[str, Any]) -> Usage | None:
    extras = _parse_json(row.get("extras_json"))
    if not isinstance(extras, dict):
        return None
    finish = extras.get("tokens.finish")
    return Usage.from_native(finish) if isinstance(finish, dict) else None


def _part_from_row(kind: str, payload: str) -> Part | None:
    if kind == "text":
        return Part("text", text=payload, native={"kind": kind, "payload": payload})
    if kind == "reasoning":
        return Part("reasoning", text=payload,
                    native={"kind": kind, "payload": payload})
    if kind == "tool_call":
        try:
            event = json.loads(payload)
        except ValueError as exc:
            raise ArchiveError("Kelivo tool_call 部件不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ArchiveError("Kelivo tool_call 部件不是对象")
        content = event.get("content")
        output = [Part("text", text=str(content))] if isinstance(content, str) \
            and content else []
        metadata = event.get("metadata") if isinstance(
            event.get("metadata"), dict) else None
        return Part(
            "tool", call_id=str(event.get("id") or ""),
            name=str(event.get("name") or ""),
            input_json=json.dumps(event.get("arguments") or {}, ensure_ascii=False),
            output=output, metadata=metadata,
            native={"kind": kind, "payload": payload})
    if kind == "assistant_round_end":
        return Part("unknown", raw_kind=kind, raw_payload=payload,
                    native={"kind": kind, "payload": payload})
    if kind == "image":
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("uri"), str):
            raise ArchiveError("Kelivo image 部件无效")
        return Part("image", uri=data["uri"],
                    mime=data.get("mime") if isinstance(data.get("mime"), str)
                    else None,
                    asset_id=data.get("assetId") if isinstance(
                        data.get("assetId"), str) else None,
                    unavailable=bool(data.get("unavailable")),
                    native={"kind": kind, "payload": payload})
    if kind == "file":
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("uri"), str) \
                or not isinstance(data.get("name"), str):
            raise ArchiveError("Kelivo file 部件无效")
        return Part("file", uri=data["uri"], name=data["name"],
                    mime=data.get("mime") if isinstance(data.get("mime"), str)
                    else None,
                    asset_id=data.get("assetId") if isinstance(
                        data.get("assetId"), str) else None,
                    unavailable=bool(data.get("unavailable")),
                    native={"kind": kind, "payload": payload})
    if kind == "provider_auth_error":
        return Part("unknown", raw_kind=kind, raw_payload=payload,
                    native={"kind": kind, "payload": payload})
    return Part("unknown", raw_kind=kind, raw_payload=payload,
                native={"kind": kind, "payload": payload})


def _mcp_ids(tables: dict[str, TableRows], conversation_id: str) -> list[str]:
    table = tables.get("conversation_mcp_server_rows")
    if not table:
        return []
    rows = [row for row in table.dicts()
            if str(row["conversation_id"]) == conversation_id]
    rows.sort(key=lambda row: (int(row["ordinal"]), str(row["server_id"])))
    return [str(row["server_id"]) for row in rows]


def _memory_entries(tables: dict[str, TableRows]) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for row in _rows(tables, "memory_entry_rows"):
        payload = _parse_json(row.get("payload"))
        payload = payload if isinstance(payload, dict) else {}
        entries.append(MemoryEntry(
            id=str(row["id"]),
            scope=str(row["scope"]),
            assistant_id=str(row["assistant_id"])
            if row.get("assistant_id") else None,
            type=str(row["type"]),
            status=str(row["status"]),
            content=str(row["content"]),
            source=str(payload.get("source") or "manual"),
            created_us=_int(row["entry_created_at"]),
            updated_us=_int(row["entry_updated_at"]),
            related_ids=[str(item) for item in payload.get("relatedIds") or []
                         if isinstance(item, str)],
            native=dict(row),
        ))
    for row in _rows(tables, "assistant_memory_rows"):
        payload = _parse_json(row.get("payload"))
        payload = payload if isinstance(payload, dict) else {}
        content = payload.get("content")
        if not isinstance(content, str):
            continue
        entries.append(MemoryEntry(
            id=str(row["id"]),
            scope="assistant",
            assistant_id=str(row["assistant_id"]),
            type="instruction",
            status="active",
            content=content,
            source="manual",
            created_us=0,
            updated_us=_int(row.get("updated_at")),
            native={"legacy_assistant_memory": True, "row": dict(row),
                    "payload": payload},
        ))
    return entries


def _assistant_flags(tables: dict[str, TableRows]) -> dict[str, model.AssistantMemoryFlags]:
    flags: dict[str, model.AssistantMemoryFlags] = {}
    for row in _rows(tables, "assistant_rows"):
        payload = _parse_json(row.get("payload"))
        if not isinstance(payload, dict):
            continue
        flags[str(row["id"])] = model.AssistantMemoryFlags(
            use_global=bool(payload.get("useGlobalMemory")),
            enable=bool(payload.get("enableMemory", True)),
        )
    return flags


def _workspaces_from_tables(tables: dict[str, TableRows]) -> list[Workspace]:
    out: list[Workspace] = []
    for row in _rows(tables, "extension_entity_rows"):
        if str(row["kind"]) != "workspace":
            continue
        payload = _parse_json(row.get("payload"))
        if not isinstance(payload, dict):
            continue
        out.append(_workspace_from_payload(payload, fallback_id=str(row["id"])))
    return out


def _workspace_from_payload(payload: dict[str, Any], *,
                            fallback_id: str) -> Workspace:
    created = iso_to_ms(payload.get("createdAt")) or 0
    updated = iso_to_ms(payload.get("updatedAt")) or created
    last_used = iso_to_ms(payload.get("lastUsedAt"))
    return Workspace(
        id=str(payload.get("id") or fallback_id),
        name=str(payload.get("name") or ""),
        created_ms=created,
        updated_ms=updated,
        last_used_ms=last_used,
        kind=str(payload.get("kind") or "managed"),
        host_path=payload.get("hostPath") if isinstance(
            payload.get("hostPath"), str) else None,
        shell_needs_approval=bool(payload.get("shellNeedsApproval")),
        default_cwd=str(payload.get("defaultCwd") or ""),
        disabled_tools=[str(item) for item in payload.get("disabledTools") or []
                        if isinstance(item, str)],
        native={"payload": dict(payload)},
    )


def _skills_from_tables(tables: dict[str, TableRows],
                        assets: list[Asset]) -> list[model.Skill]:
    skills: dict[str, model.Skill] = {}
    for row in _rows(tables, "extension_entity_rows"):
        if str(row["kind"]) != "skill":
            continue
        payload = _parse_json(row.get("payload"))
        payload = payload if isinstance(payload, dict) else {}
        skill_id = str(payload.get("id") or row["id"])
        skills[skill_id] = model.Skill(
            id=skill_id, name=skill_id, entries=[], native={"payload": payload})
    for asset in assets:
        if not asset.entry.startswith("skills/"):
            continue
        parts = asset.entry.split("/")
        if len(parts) < 3:
            continue
        skill = skills.setdefault(parts[1], model.Skill(
            id=parts[1], name=parts[1], entries=[]))
        skill.entries.append(asset.entry)
    return list(skills.values())


def _skills_from_assets(assets: list[Asset]) -> list[model.Skill]:
    return _skills_from_tables({}, assets)


def _skills_from_settings(settings: dict[str, Any],
                          assets: list[Asset]) -> list[model.Skill]:
    skills: dict[str, model.Skill] = {}
    for item in _as_list(_parse_json(settings.get("skills_v1"))):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        skill_id = item["id"]
        skills[skill_id] = model.Skill(
            id=skill_id, name=skill_id, entries=[], native={"payload": dict(item)})
    for asset in assets:
        if not asset.entry.startswith("skills/"):
            continue
        parts = asset.entry.split("/")
        if len(parts) < 3:
            continue
        skill = skills.setdefault(parts[1], model.Skill(
            id=parts[1], name=parts[1], entries=[]))
        skill.entries.append(asset.entry)
    return list(skills.values())


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _entries_from_settings(settings: dict[str, Any]) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for index, item in enumerate(_as_list(_parse_json(settings.get("memory_entries_v1")))):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            continue
        entries.append(MemoryEntry(
            id=str(item.get("id") or f"memory-{index}"),
            scope=str(item.get("scope") or "assistant"),
            assistant_id=item.get("assistantId")
            if isinstance(item.get("assistantId"), str) else None,
            type=str(item.get("type") or "instruction"),
            status=str(item.get("status") or "active"),
            content=item["content"],
            source=str(item.get("source") or "manual"),
            created_us=_int(item.get("createdAt")),
            updated_us=_int(item.get("updatedAt")),
            native={"payload": dict(item)},
        ))
    return entries


# ---------------------------------------------------------------------------
# Legacy chat decoding
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(r"\[(image|file):([^\]\n]+)\]")


def split_legacy_markers(content: str) -> tuple[str, list[Part]]:
    """Promote attachment markers outside code fences and inline code."""
    parts: list[Part] = []
    out: list[str] = []
    index = 0
    in_fence = False
    fence_marker = ""
    length = len(content)
    while index < length:
        if content.startswith("```", index):
            marker = content[index:index + 3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif content.startswith(fence_marker, index):
                in_fence = False
            out.append("```")
            index += 3
            continue
        if not in_fence and content[index] == "`":
            end = content.find("`", index + 1)
            if end != -1:
                out.append(content[index:end + 1])
                index = end + 1
                continue
        if not in_fence and content.startswith("[image:", index):
            close = content.find("]", index)
            if close != -1 and "\n" not in content[index:close]:
                path = content[index + 7:close].strip()
                if path:
                    parts.append(Part("image", uri=path))
                    index = close + 1
                    continue
        if not in_fence and content.startswith("[file:", index):
            close = content.find("]", index)
            if close != -1 and "\n" not in content[index:close]:
                body = content[index + 6:close]
                pieces = body.split("|")
                if pieces and pieces[0].strip():
                    name = pieces[1] if len(pieces) > 1 and pieces[1] else \
                        pieces[0].rsplit("/", 1)[-1]
                    mime = pieces[2] if len(pieces) > 2 and pieces[2] \
                        else "application/octet-stream"
                    parts.append(Part("file", uri=pieces[0].strip(),
                                      name=name, mime=mime))
                    index = close + 1
                    continue
        out.append(content[index])
        index += 1
    return "".join(out), parts


def _legacy_message(raw: dict[str, Any], legacy: LegacyChatData,
                    created_fallback: int) -> Msg:
    message_id = str(raw["id"])
    conversation_id = str(raw["conversationId"])
    created_ms = iso_to_ms(raw.get("timestamp"))
    timestamp_fallback = created_ms is None
    if timestamp_fallback:
        created_ms = created_fallback
    parts: list[Part] = []
    raw_parts = raw.get("parts")
    if isinstance(raw_parts, list):
        for entry in raw_parts:
            kind = str(entry.get("kind") or "")
            payload = entry.get("payload")
            payload = payload if isinstance(payload, str) else _json_str(payload)
            part = _part_from_row(kind, payload)
            if part is not None:
                part.native.setdefault("origin_family", "kelivo")
                parts.append(part)
    else:
        text, attachments = split_legacy_markers(str(raw.get("content") or ""))
        parts.append(Part("text", text=text))
        parts.extend(attachments)
    reasoning_text = raw.get("reasoningText")
    if isinstance(reasoning_text, str) and reasoning_text \
            and not any(part.kind == "reasoning" for part in parts):
        parts.insert(0, Part(
            "reasoning", text=reasoning_text,
            created_ms=iso_to_ms(raw.get("reasoningStartAt")),
            finished_ms=iso_to_ms(raw.get("reasoningFinishedAt"))))
    events = legacy.tool_events.get(message_id, [])
    _apply_legacy_tool_events(parts, events)
    artifacts: list[Artifact] = []
    signature = legacy.gemini_sigs.get(message_id)
    if signature:
        artifacts.append(Artifact("gemini_thought_signature", signature,
                                  created_ms, created_ms))
    extras: dict[str, Any] = {}
    for key in ("firstTokenMs", "reasoningTokens", "cacheWriteTokens"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            extras[{"firstTokenMs": "timing.firstTokenMs",
                    "reasoningTokens": "tokens.reasoning",
                    "cacheWriteTokens": "tokens.cacheWrite"}[key]] = value
    group_id = raw.get("groupId") if isinstance(raw.get("groupId"), str) else None
    subgroup = raw.get("subgroupId")
    speaker = raw.get("speakerAssistantId")
    return Msg(
        id=message_id,
        role=str(raw["role"]),
        parts=parts,
        created_ms=created_ms,
        model_id=raw.get("modelId") if isinstance(raw.get("modelId"), str) else None,
        provider_id=raw.get("providerId") if isinstance(
            raw.get("providerId"), str) else None,
        usage=_usage_from_native_message(raw),
        last_usage=Usage.from_native(raw.get("finishUsage")),
        duration_ms=_int(raw["durationMs"]) if isinstance(
            raw.get("durationMs"), int) else None,
        translation=raw.get("translation") if isinstance(
            raw.get("translation"), str) else None,
        reasoning_segments_json=raw.get("reasoningSegmentsJson")
        if isinstance(raw.get("reasoningSegmentsJson"), str) else None,
        group_id=group_id or message_id,
        subgroup_id=str(subgroup) if isinstance(subgroup, str) and subgroup else None,
        version=_int(raw.get("version")),
        speaker_id=str(speaker) if isinstance(speaker, str) and speaker else None,
        is_preset=bool(raw.get("isPreset")),
        extras=extras,
        artifacts=artifacts,
        native=dict(raw, _fallback_timestamp=timestamp_fallback),
    )


def _usage_from_native_message(raw: dict[str, Any]) -> Usage | None:
    values = [raw.get("promptTokens"), raw.get("completionTokens"),
              raw.get("cachedTokens"), raw.get("totalTokens")]
    if all(value is None for value in values):
        return None
    return Usage(
        prompt=_int(raw.get("promptTokens")),
        completion=_int(raw.get("completionTokens")),
        cached=_int(raw.get("cachedTokens")),
        total=_int(raw.get("totalTokens")),
    )


def _apply_legacy_tool_events(parts: list[Part],
                              events: list[dict[str, Any]]) -> None:
    existing: dict[str, Part] = {
        part.call_id or "": part for part in parts if part.kind == "tool"}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("name"), str):
            continue
        call_id = str(event.get("id") or "")
        content = event.get("content")
        output = [Part("text", text=str(content))] if isinstance(
            content, str) and content else []
        if call_id and call_id in existing:
            existing[call_id].output = output
            existing[call_id].metadata = event.get("metadata") if isinstance(
                event.get("metadata"), dict) else existing[call_id].metadata
            continue
        parts.append(Part(
            "tool", call_id=call_id, name=str(event["name"]),
            input_json=json.dumps(event.get("arguments") or {}, ensure_ascii=False),
            output=output,
            metadata=event.get("metadata") if isinstance(
                event.get("metadata"), dict) else None))


def _decode_legacy(backup: Backup, payload: KelivoPayload) -> Snapshot:
    legacy = payload.legacy_chat
    assert legacy is not None
    snapshot = Snapshot(settings=dict(payload.settings))
    snapshot.has_chat_domain = True
    messages_by_id = {str(message["id"]): message for message in legacy.messages}
    messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for message in legacy.messages:
        messages_by_conversation.setdefault(
            str(message.get("conversationId")), []).append(message)
    group_members_by_chat: dict[str, list[dict[str, Any]]] = {}
    for member in legacy.group_members:
        group_members_by_chat.setdefault(
            str(member.get("groupChatId")), []).append(member)
    groups_by_conversation: dict[str, list[GroupChat]] = {}
    for group in legacy.group_chats:
        groups_by_conversation.setdefault(
            str(group.get("conversationId")), []).append(GroupChat(
                id=str(group.get("id")),
                conversation_id=str(group.get("conversationId")),
                members=list(group_members_by_chat.get(str(group.get("id")), [])),
                native=dict(group),
            ))
    diagnostics: list[Diagnostic] = []
    dialect = backup.info.selection_encoding
    missing_total = 0
    appended_total = 0
    for raw_conversation in legacy.conversations:
        conv = _legacy_conversation(
            raw_conversation, messages_by_id, messages_by_conversation,
            legacy, groups_by_conversation.get(
                str(raw_conversation.get("id")), []),
            dialect, diagnostics)
        if conv is not None:
            snapshot.conversations.append(conv)
        missing_total += conv.native.pop("_missing_refs", 0) if conv else 0
    snapshot.group_chats = [group for groups in groups_by_conversation.values()
                            for group in groups]
    snapshot.deleted_document = legacy.deleted_document
    snapshot.memory_entries = _legacy_memory_entries(payload.settings, diagnostics)
    snapshot.assistant_flags = _assistant_flags_from_settings(payload.settings)
    snapshot.workspaces = _legacy_workspaces(payload.settings)
    snapshot.skills = _skills_from_assets(backup.assets)
    snapshot.asset_entries = [asset.entry for asset in backup.assets]
    snapshot.counts = {
        "conversations": len(snapshot.conversations),
        "messages": sum(len(conv.messages) for conv in snapshot.conversations),
    }
    fallbacks = sum(
        1 for conv in snapshot.conversations for message in conv.messages
        if message.native.get("_fallback_timestamp"))
    if fallbacks:
        diagnostics.append(Diagnostic(
            "value_defaulted", "timestamp", fallbacks,
            "消息缺少时间，已使用会话创建时间"))
    snapshot.diagnostics = model.merge_diagnostics(diagnostics)
    return snapshot


def _assistant_flags_from_settings(
    settings: dict[str, Any]) -> dict[str, model.AssistantMemoryFlags]:
    flags: dict[str, model.AssistantMemoryFlags] = {}
    assistants = _parse_json(settings.get("assistants_v1"))
    if isinstance(assistants, list):
        for assistant in assistants:
            if not isinstance(assistant, dict) or not isinstance(
                    assistant.get("id"), str):
                continue
            flags[assistant["id"]] = model.AssistantMemoryFlags(
                use_global=bool(assistant.get("useGlobalMemory")),
                enable=bool(assistant.get("enableMemory", True)),
            )
    return flags


def _legacy_memory_entries(settings: dict[str, Any],
                           diagnostics: list[Diagnostic]) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    raw = _parse_json(settings.get("assistant_memories_v1"))
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str):
                continue
            entries.append(MemoryEntry(
                id=str(item.get("id") or f"legacy-memory-{index}"),
                scope="assistant",
                assistant_id=str(item.get("assistantId") or "") or None,
                type="instruction",
                status="active",
                content=content,
                source="manual",
                created_us=0,
                updated_us=0,
                native={"legacy": True, "payload": dict(item)},
            ))
    if entries:
        diagnostics.append(Diagnostic(
            "historical_type_normalized", "memory", len(entries),
            "旧简单记忆没有类型与时钟，按 instruction 条目迁移"))
    return entries


def _legacy_workspaces(settings: dict[str, Any]) -> list[Workspace]:
    raw = _parse_json(settings.get("workspaces_v1"))
    if not isinstance(raw, list):
        return []
    workspaces: list[Workspace] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            workspaces.append(_workspace_from_payload(
                item, fallback_id=str(item.get("id") or f"workspace-{index}")))
    return workspaces


def _legacy_conversation(raw: dict[str, Any],
                         messages_by_id: dict[str, dict[str, Any]],
                         messages_by_conversation: dict[str, list[dict[str, Any]]],
                         legacy: LegacyChatData,
                         groups: list[GroupChat],
                         dialect: str,
                         diagnostics: list[Diagnostic]) -> Conv | None:
    conv_id = str(raw["id"])
    created_ms = iso_to_ms(raw.get("createdAt"))
    if created_ms is None:
        created_ms = 0
    updated_ms = iso_to_ms(raw.get("updatedAt"))
    if updated_ms is None:
        updated_ms = created_ms
    message_ids = [str(item) for item in raw.get("messageIds") or []]
    present = list(messages_by_conversation.get(conv_id, []))
    missing = 0
    seen_ids: set[str] = set()
    for message_id in message_ids:
        message = messages_by_id.get(message_id)
        if message is None:
            missing += 1
            continue
        if str(message.get("conversationId")) != conv_id:
            raise ArchiveError(
                f"消息 {message_id} 被会话 {conv_id} 引用，但归属另一个会话")
        seen_ids.add(message_id)
    # Historical writers store the message streams in display order.  When
    # every present message is referenced by messageIds, that list is the
    # authoritative order; a representative-only list (this converter's old
    # JSON2 shape, one id per revision group) carries no order and the stream
    # order is used instead.
    if len(seen_ids) >= len(present):
        ordered = []
        used: set[str] = set()
        for message_id in message_ids:
            message = messages_by_id.get(message_id)
            if message is None or message_id in used:
                continue
            used.add(message_id)
            ordered.append(message)
        for message in present:
            if str(message.get("id")) not in used:
                ordered.append(message)
    else:
        ordered = present
    appended = max(0, len(ordered) - len(seen_ids))
    if missing:
        diagnostics.append(Diagnostic(
            "selection_unavailable", "message reference", missing,
            "chats.json 引用了本包中不存在的旧消息，已按现存消息迁移"))
        diagnostics.append(Diagnostic(
            "source_completeness_unknown", "message", missing,
            "历史 JSON 可能为增量备份，缺失引用未补造"))
    if appended:
        diagnostics.append(Diagnostic(
            "source_completeness_unknown", "message", appended,
            "未被 messageIds 引用的实际消息已按源顺序追加"))
    conv = Conv(
        id=conv_id,
        title=str(raw.get("title") or ""),
        created_ms=created_ms,
        updated_ms=updated_ms,
        assistant_id=str(raw["assistantId"]) if isinstance(
            raw.get("assistantId"), str) else None,
        is_pinned=bool(raw.get("isPinned")),
        mcp_ids=[str(item) for item in raw.get("mcpServerIds") or []
                 if isinstance(item, str)],
        summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        last_summarized_count=_int(raw.get("lastSummarizedMessageCount")),
        suggestions=[str(item) for item in raw.get("chatSuggestions") or []
                     if isinstance(item, str)],
        chat_model_provider=raw.get("chatModelProvider") if isinstance(
            raw.get("chatModelProvider"), str) else None,
        chat_model_id=raw.get("chatModelId") if isinstance(
            raw.get("chatModelId"), str) else None,
        injected_memory_hash=raw.get("injectedMemoryHash") if isinstance(
            raw.get("injectedMemoryHash"), str) else None,
        last_memory_extracted_order=_int(raw.get("lastMemoryExtractedOrder"), -1),
        conversation_kind=str(raw.get("conversationKind") or "normal"),
        extras=raw.get("extras") if isinstance(raw.get("extras"), dict) else {},
        native=dict(raw),
    )
    conv.native["_missing_refs"] = missing
    for message_raw in ordered:
        conv.messages.append(_legacy_message(message_raw, legacy, created_ms))
    _build_legacy_groups(conv, raw, dialect, diagnostics,
                         has_missing_evidence=bool(missing))
    if groups:
        conv.conversation_kind = "group"
    if legacy.jsonl:
        truncate = _int(raw.get("truncateIndex"), -1)
        conv.truncate_slot = truncate if truncate >= 0 else None
    else:
        conv.truncate_slot = _legacy_truncate_slot(raw, ordered)
    return conv


def _build_legacy_groups(conv: Conv, raw: dict[str, Any], dialect: str,
                         diagnostics: list[Diagnostic],
                         *, has_missing_evidence: bool) -> None:
    grouped: dict[tuple[str, str | None, str | None], list[Msg]] = {}
    order: list[tuple[str, str | None, str | None]] = []
    for message in conv.messages:
        key = (message.group_id or message.id, message.subgroup_id,
               message.speaker_id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(message)
    selections = raw.get("versionSelections")
    selections = selections if isinstance(selections, dict) else {}
    resolved: dict[str, Msg] = {}
    by_group_id: dict[str, list[Msg]] = {}
    for key in order:
        by_group_id.setdefault(key[0], []).extend(grouped[key])
    for group_id, value in selections.items():
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        members = by_group_id.get(str(group_id))
        if not members:
            continue
        resolved[str(group_id)] = _resolve_selection(
            conv.id, str(group_id), value, members, dialect,
            diagnostics, has_missing_evidence)
    for key in order:
        members = grouped[key]
        group_id = key[0]
        owners = {item.id for item in members}
        explicit_selected = resolved.get(group_id)
        explicit = explicit_selected is not None \
            and explicit_selected.id in owners
        if not explicit and group_id in resolved \
                and has_missing_evidence:
            # The reported fallback choice is written explicitly so the
            # target does not silently make a different default decision.
            explicit = True
        selected = explicit_selected if explicit_selected is not None \
            and explicit_selected.id in owners else sorted(
            members,
            key=lambda item: (item.version
                              if item.version is not None else 0))[-1]
        conv.groups.append(RevisionGroup(
            conversation_id=conv.id,
            group_id=group_id,
            subgroup_id=key[1],
            message_ids=[item.id for item in members],
            version_numbers=[item.version or 0 for item in members],
            selected_message_id=selected.id,
            speaker_id=key[2],
            selection_explicit=explicit,
        ))


def _resolve_selection(conversation_id: str, group_id: str, value: int,
                       members: list[Msg], dialect: str,
                       diagnostics: list[Diagnostic],
                       has_missing_evidence: bool) -> Msg | None:
    by_version: dict[int, list[Msg]] = {}
    for message in members:
        by_version.setdefault(message.version or 0, []).append(message)
    actual_candidates = by_version.get(value, [])
    ordered = sorted(members, key=lambda item: (
        item.version if item.version is not None else 0))
    ordinal_candidate = ordered[value] if 0 <= value < len(ordered) else None
    if dialect == model.SELECTION_ACTUAL:
        if len(actual_candidates) == 1:
            return actual_candidates[0]
        if len(actual_candidates) > 1:
            diagnostics.append(Diagnostic(
                "selection_unavailable", "selection", 1,
                f"会话 {conversation_id} 分组 {group_id} 的版本 {value} "
                "对应多条并行消息，已选择最后一条"))
            return actual_candidates[-1]
        return _missing_selection(conversation_id, group_id, diagnostics,
                                  has_missing_evidence)
    if dialect == model.SELECTION_ORDINAL:
        if ordinal_candidate is not None:
            return ordinal_candidate
        return _missing_selection(conversation_id, group_id, diagnostics,
                                  has_missing_evidence)
    # Undetermined: accept only agreement.
    actual = actual_candidates[0] if len(actual_candidates) == 1 else None
    if len(actual_candidates) > 1:
        actual = actual_candidates[-1]
    if actual is not None and ordinal_candidate is not None:
        if actual.id != ordinal_candidate.id:
            raise ArchiveError(
                f"会话 {conversation_id} 分组 {group_id} 的选择意义不明确："
                f"按实际版本得到 {actual.id}，按版本序号得到 {ordinal_candidate.id}")
        return actual
    if actual is not None:
        return actual
    if ordinal_candidate is not None:
        return ordinal_candidate
    return _missing_selection(conversation_id, group_id, diagnostics,
                              has_missing_evidence)


def _missing_selection(conversation_id: str, group_id: str,
                       diagnostics: list[Diagnostic],
                       has_missing_evidence: bool) -> Msg | None:
    if has_missing_evidence:
        diagnostics.append(Diagnostic(
            "selection_unavailable", "selection", 1,
            f"会话 {conversation_id} 分组 {group_id} 的选中版本不在包内，"
            "已回退到现存末修订"))
        return None
    raise ArchiveError(
        f"会话 {conversation_id} 分组 {group_id} 的选中版本不存在")


def _legacy_truncate_slot(raw: dict[str, Any],
                          ordered: list[dict[str, Any]]) -> int | None:
    value = _int(raw.get("truncateIndex"), -1)
    if value < 0:
        return None
    if value == 0:
        return 0
    if value >= len(ordered):
        return len({str(m.get("groupId") or m.get("id")) for m in ordered})
    prefix = ordered[:value]
    return len({str(m.get("groupId") or m.get("id")) for m in prefix})


# ---------------------------------------------------------------------------
# Snapshot -> native SQL payload
# ---------------------------------------------------------------------------

_INTERNAL_EXTRA_KEYS = {
    "config", "customSystemPrompt", "modeInjectionIds", "lorebookIds",
    "workspaceCwd", "folderId", "workspaceId", "enabledSkills",
}


def _empty_tables(version: int) -> dict[str, TableRows]:
    drift = schema.load_drift(version)
    return {table: TableRows(columns=columns, rows=[])
            for table, columns in drift.table_columns.items()}


def _row_for(table: str, values: dict[str, Any]) -> tuple[Any, ...]:
    columns = schema.load_drift(schema.DRIFT_LATEST).table_columns[table]
    return tuple(values.get(column) for column in columns)


def encode_snapshot(snapshot: Snapshot, target: TargetProfile,
                    settings: model.SettingsResult,
                    asset_entries: list[Asset]) -> tuple[KelivoPayload, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    tables = _empty_tables(schema.DRIFT_LATEST)
    include_chats = snapshot.has_chat_domain
    if include_chats:
        _encode_conversations(snapshot, settings, tables, diagnostics)
    _fill_business_tables(tables, settings, diagnostics)
    _fill_preferences(tables, settings, diagnostics)
    if include_chats:
        tables["chat_storage_meta_rows"].rows.extend([
            ("hive_migration_complete_v1", "true"),
            ("business_migration_complete_v1", "true"),
        ])
    scope = _output_scope(snapshot)
    output_settings = dict(settings.settings)
    if any(conv.chat_model_id or conv.chat_model_provider
           for conv in snapshot.conversations):
        output_settings["per_chat_model_enabled_v1"] = True
    payload = KelivoPayload(
        settings=output_settings,
        tables=tables if include_chats else {},
        legacy_chat=None,
        scope=scope,
    )
    return payload, diagnostics


def _output_scope(snapshot: Snapshot) -> frozenset[str]:
    scope = set(snapshot.scope or model.SCOPE_CATEGORIES)
    if not snapshot.has_chat_domain:
        scope.discard("chats")
    else:
        scope.add("chats")
    if snapshot.skills:
        scope.add("skills")
    if snapshot.workspaces:
        scope.add("workspaces")
    if snapshot.asset_entries and any(
            entry.startswith(("upload/", "images/", "avatars/", "fonts/",
                              "sessions/"))
            for entry in snapshot.asset_entries):
        scope.add("files")
    return frozenset(scope)


def _encode_conversations(snapshot: Snapshot, settings: model.SettingsResult,
                          tables: dict[str, TableRows],
                          diagnostics: list[Diagnostic]) -> None:
    conv_table = tables["conversation_rows"]
    message_table = tables["message_rows"]
    part_table = tables["message_part_rows"]
    artifact_table = tables["provider_artifact_rows"]
    mcp_table = tables["conversation_mcp_server_rows"]
    dropped_server_tools = 0
    downgraded_reasoning_axes = 0
    for conv in snapshot.conversations:
        extras, extra_diagnostics = _encode_extras(conv, settings, diagnostics)
        diagnostics.extend(extra_diagnostics)
        conv_provider = conv.chat_model_provider
        conv_model = conv.chat_model_id
        if conv_model:
            mapped_model = settings.models_by_rh_uuid.get(conv_model)
            if mapped_model:
                conv_provider, conv_model = mapped_model
        selections: dict[str, int] = {}
        for group in conv.groups:
            if not group.selected_message_id \
                    or not group.selection_explicit \
                    or (len(group.message_ids) <= 1
                        and not group.lineage_split):
                continue
            selected = next((m for m in conv.messages
                             if m.id == group.selected_message_id), None)
            if selected is not None and selected.version is not None:
                selections[group.group_id] = selected.version
        conv_table.rows.append(_row_for("conversation_rows", {
            "id": conv.id,
            "title": conv.title,
            "created_at": (_ms_to_us(conv.created_ms)
                           if conv.created_ms is not None else 0),
            "updated_at": (_ms_to_us(conv.updated_ms)
                           if conv.updated_ms is not None else 0),
            "is_pinned": 1 if conv.is_pinned else 0,
            "assistant_id": settings.assistant_ids.get(
                conv.assistant_id or "", conv.assistant_id) or None,
            "truncate_index": conv.truncate_slot
            if conv.truncate_slot is not None else -1,
            "version_selections_json": json.dumps(selections, ensure_ascii=False),
            "summary": conv.summary,
            "last_summarized_message_count": conv.last_summarized_count,
            "chat_suggestions_json": json.dumps(conv.suggestions,
                                               ensure_ascii=False),
            "injected_memory_hash": conv.injected_memory_hash,
            "last_memory_extracted_order": conv.last_memory_extracted_order
            if conv.last_memory_extracted_order is not None else -1,
            "chat_model_provider": conv_provider,
            "chat_model_id": conv_model,
            "extras_json": json.dumps(extras, ensure_ascii=False),
        }))
        mapped_mcp = [settings.entity_ids.get("mcp", {}).get(server_id)
                      for server_id in conv.mcp_ids]
        for ordinal, server_id in enumerate(mapped_mcp):
            if not server_id:
                diagnostics.append(Diagnostic(
                    "identity_reference_unresolved", "mcp server", 1,
                    "会话引用的 MCP 服务未能映射到目标"))
                continue
            mcp_table.rows.append((conv.id, server_id, ordinal))
        for order, message in enumerate(conv.messages):
            _encode_message(conv, message, order, settings, message_table,
                            part_table, artifact_table, diagnostics)
            if any(part.kind == "server_tool" for part in message.parts):
                dropped_server_tools += 1
            reasoning_parts = [part for part in message.parts
                               if part.kind == "reasoning"]
            if len(reasoning_parts) > 1 and any(
                    part.created_ms for part in reasoning_parts[1:]):
                downgraded_reasoning_axes += 1
    if dropped_server_tools:
        diagnostics.append(Diagnostic(
            "unsupported_data", "server tool", dropped_server_tools,
            "服务端工具的调用语义在 Kelivo 中无等价表达，仅保留可读输出"))
    if downgraded_reasoning_axes:
        diagnostics.append(Diagnostic(
            "value_defaulted", "reasoning timeline", downgraded_reasoning_axes,
            "多段推理的逐段计时在 Kelivo 中退化为消息级时间轴"))


def _encode_message(conv: Conv, message: Msg, order: int,
                    settings: model.SettingsResult,
                    message_table: TableRows, part_table: TableRows,
                    artifact_table: TableRows,
                    diagnostics: list[Diagnostic]) -> None:
    timestamp_source = message.created_ms if message.created_ms is not None \
        else conv.created_ms
    if message.created_ms is None:
        diagnostics.append(Diagnostic(
            "value_defaulted", "timestamp", 1,
            "消息缺少时间，已使用会话时间"))
    timestamp_us = _ms_to_us(timestamp_source)
    if timestamp_us is None:
        timestamp_us = 0
    role = message.role if message.role in ("user", "assistant") else "assistant"
    if role != message.role:
        diagnostics.append(Diagnostic(
            "historical_type_normalized", "message role", 1,
            f"角色 {message.role} 在 Kelivo 中不支持，按 assistant 存储"))
    usage = message.usage
    provider = message.provider_id
    model_id = message.model_id
    if model_id and not provider:
        mapped = settings.models_by_rh_uuid.get(model_id)
        if mapped:
            provider, model_id = mapped
        else:
            model_id = None
            diagnostics.append(Diagnostic(
                "identity_reference_unresolved", "message model", 1,
                "消息的模型引用无法映射到目标模型"))
    reasoning_parts = [part for part in message.parts
                       if part.kind == "reasoning"]
    reasoning_start = next((part.created_ms for part in reasoning_parts
                            if part.created_ms), None)
    reasoning_finished = next((part.finished_ms for part in reversed(
        reasoning_parts) if part.finished_ms), None)
    extras: dict[str, Any] = {}
    for key in ("tokens.reasoning", "tokens.cacheWrite", "timing.firstTokenMs"):
        if isinstance(message.extras.get(key), int):
            extras[key] = message.extras[key]
    if message.last_usage is not None:
        extras["tokens.finish"] = {
            "promptTokens": message.last_usage.prompt,
            "completionTokens": message.last_usage.completion,
            "cachedTokens": message.last_usage.cached,
            "totalTokens": message.last_usage.total,
        }
    message_table.rows.append(_row_for("message_rows", {
        "id": message.id,
        "conversation_id": conv.id,
        "role": role,
        "timestamp": timestamp_us,
        "model_id": model_id,
        "provider_id": provider,
        "total_tokens": usage.total if usage else None,
        "is_streaming": 0,
        "reasoning_start_at": _ms_to_us(reasoning_start),
        "reasoning_finished_at": _ms_to_us(reasoning_finished),
        "translation": message.translation,
        "reasoning_segments_json": message.reasoning_segments_json,
        "group_id": message.group_id or message.id,
        "version": message.version if message.version is not None else 0,
        "prompt_tokens": usage.prompt if usage else None,
        "completion_tokens": usage.completion if usage else None,
        "cached_tokens": usage.cached if usage else None,
        "duration_ms": message.duration_ms,
        "message_order": order,
        "updated_at": None,
        "sender_id": message.speaker_id,
        "extras_json": json.dumps(extras, ensure_ascii=False),
    }))
    ordinal = 0
    for part in message.parts:
        encoded = _kelivo_part_payload(part, diagnostics)
        if encoded is None:
            continue
        kind, payload = encoded
        part_table.rows.append((None, conv.id, message.id, ordinal, kind,
                                payload, timestamp_us, timestamp_us))
        ordinal += 1
    for artifact in message.artifacts:
        artifact_table.rows.append((
            conv.id, message.id, artifact.kind, artifact.payload,
            (_ms_to_us(artifact.created_ms)
             if artifact.created_ms is not None else timestamp_us),
            (_ms_to_us(artifact.updated_ms)
             if artifact.updated_ms is not None else timestamp_us),
        ))
    if not message.parts and ordinal == 0:
        part_table.rows.append((None, conv.id, message.id, 0, "text", "",
                                timestamp_us, timestamp_us))


def _kelivo_part_payload(part: Part,
                         diagnostics: list[Diagnostic]) -> tuple[str, str] | None:
    if part.kind == "text":
        return "text", part.text or ""
    if part.kind == "reasoning":
        return "reasoning", part.text or ""
    if part.kind == "tool":
        event: dict[str, Any] = {
            "id": part.call_id or "",
            "name": part.name or "",
            "arguments": _arguments(part.input_json),
        }
        output_parts = list(part.output or [])
        output_text = "\n".join(
            child.text or "" for child in output_parts
            if child.kind == "text")
        if output_text:
            event["content"] = output_text
        if part.metadata:
            event["metadata"] = part.metadata
        non_text = [child for child in output_parts
                    if child.kind != "text"]
        if non_text:
            diagnostics.append(Diagnostic(
                "unsupported_data", "tool output attachment",
                len(non_text),
                "Kelivo 工具事件没有附件结构，工具输出附件未迁移"))
        return "tool_call", json.dumps(event, ensure_ascii=False)
    if part.kind == "image":
        payload = {"uri": part.uri or ""}
        if part.mime:
            payload["mime"] = part.mime
        if part.asset_id:
            payload["assetId"] = part.asset_id
        if part.unavailable:
            payload["unavailable"] = True
        return "image", json.dumps(payload, ensure_ascii=False)
    if part.kind in ("file", "video", "audio"):
        payload = {
            "uri": part.uri or "",
            "name": part.name or _basename(part.uri or "") or "file",
        }
        mime = part.mime or ("video/mp4" if part.kind == "video" else
                             "audio/mpeg" if part.kind == "audio" else None)
        if mime:
            payload["mime"] = mime
        if part.asset_id:
            payload["assetId"] = part.asset_id
        if part.unavailable:
            payload["unavailable"] = True
        if part.kind in ("video", "audio"):
            diagnostics.append(Diagnostic(
                "unsupported_data", "attachment part", 1,
                "RikkaHub 的音视频部件在 Kelivo 中按文件表达"))
        return "file", json.dumps(payload, ensure_ascii=False)
    if part.kind == "server_tool":
        readable = part.text or ""
        if not readable and part.raw_payload:
            try:
                payload = json.loads(part.raw_payload)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("output") is not None:
                readable = json.dumps(payload["output"], ensure_ascii=False)
        return ("text", readable) if readable else None
    if part.kind == "unknown" and part.raw_kind \
            and part.raw_payload is not None \
            and part.native.get("origin_family") == "kelivo":
        return part.raw_kind, part.raw_payload
    diagnostics.append(Diagnostic(
        "unsupported_data", "message part", 1,
        f"Kelivo 无法表达的消息部件 {part.raw_kind or part.kind}"))
    return None


def _arguments(raw: str | None) -> Any:
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": raw}
    return parsed


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _encode_extras(conv: Conv, settings: model.SettingsResult,
                   diagnostics: list[Diagnostic]) -> tuple[dict[str, Any], list[Diagnostic]]:
    diagnostics_local: list[Diagnostic] = []
    extras = {key: value for key, value in conv.extras.items()
              if key not in _INTERNAL_EXTRA_KEYS}
    config = conv.extras.get("config")
    config = config if isinstance(config, dict) else {}
    workspace_id = conv.extras.get("workspaceId") or config.get("workspaceId")
    if isinstance(workspace_id, str) and workspace_id:
        mapped = settings.workspace_ids.get(workspace_id, workspace_id)
        extras["workspace.id"] = mapped
    workspace_cwd = conv.extras.get("workspaceCwd")
    if isinstance(workspace_cwd, str) and workspace_cwd:
        extras["workspace.cwd"] = workspace_cwd
    skills = conv.extras.get("enabledSkills")
    if isinstance(skills, list):
        mapped_skills = []
        for name in skills:
            if not isinstance(name, str):
                continue
            target_id = settings.skill_ids.get(name, name)
            mapped_skills.append(target_id)
        extras["skills.ids"] = mapped_skills
    prompt_system = conv.extras.get("customSystemPrompt")
    if isinstance(prompt_system, str) and prompt_system:
        permissions = settings.assistant_permissions.get(
            conv.assistant_id or "", {})
        if permissions.get("allow_system_prompt", True):
            extras["prompt.system"] = prompt_system
        else:
            diagnostics_local.append(Diagnostic(
                "unsupported_data", "conversation prompt", 1,
                "助手未允许会话自定义系统提示词，已丢弃该会话提示词"))
    instructions = conv.extras.get("modeInjectionIds")
    if isinstance(instructions, list) and instructions:
        mapped = [settings.entity_ids.get("instruction", {}).get(str(item))
                  for item in instructions]
        kept = [item for item in mapped if item]
        if len(kept) != len(instructions):
            diagnostics_local.append(Diagnostic(
                "identity_reference_unresolved", "prompt injection",
                len(instructions) - len(kept),
                "会话的提示注入引用未能映射到目标"))
        if kept:
            extras["prompt.instructionIds"] = kept
    lorebooks = conv.extras.get("lorebookIds")
    if isinstance(lorebooks, list) and lorebooks:
        mapped = [settings.entity_ids.get("worldBook", {}).get(str(item))
                  for item in lorebooks]
        kept = [item for item in mapped if item]
        if len(kept) != len(lorebooks):
            diagnostics_local.append(Diagnostic(
                "identity_reference_unresolved", "world book",
                len(lorebooks) - len(kept),
                "会话的世界书引用未能映射到目标"))
        if kept:
            extras["prompt.worldBookIds"] = kept
    return extras, diagnostics_local


# ---------------------------------------------------------------------------
# Settings entities -> business tables
# ---------------------------------------------------------------------------

def _fill_business_tables(tables: dict[str, TableRows],
                          settings: model.SettingsResult,
                          diagnostics: list[Diagnostic],
                          *, only_empty: bool = False) -> None:
    now = _now_us()
    source = settings.settings
    for kind, source_key, table, id_column, extension_kind in _ENTITY_KINDS:
        if only_empty and tables[table].rows:
            continue
        raw = source.get(source_key)
        if raw is None:
            continue
        decoded = _parse_json(raw)
        if kind == "provider":
            if not isinstance(decoded, dict):
                diagnostics.append(Diagnostic(
                    "unsupported_data", "provider", 1,
                    "provider_configs_v1 不是对象，已跳过"))
                continue
            order = _parse_json(source.get(_PROVIDER_ORDER_KEY))
            order = [str(item) for item in order] if isinstance(order, list) else []
            ordered = [key for key in order if key in decoded]
            ordered += [key for key in decoded if key not in ordered]
            updated = False
            for index, key in enumerate(ordered):
                payload = decoded[key]
                if not isinstance(payload, dict):
                    continue
                if not isinstance(payload.get("id"), str) or not payload["id"]:
                    payload = dict(payload, id=key)
                    decoded[key] = payload
                    updated = True
                tables[table].rows.append(_row_for(table, {
                    "provider_key": key,
                    "sort_order": index,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "updated_at": now,
                }))
            if updated:
                source[source_key] = json.dumps(decoded, ensure_ascii=False)
            continue
        if not isinstance(decoded, list):
            diagnostics.append(Diagnostic(
                "unsupported_data", kind, 1,
                f"{source_key} 不是列表，已跳过"))
            continue
        updated_list = False
        for index, payload in enumerate(decoded):
            if not isinstance(payload, dict):
                continue
            entity_id = payload.get("id")
            if not isinstance(entity_id, str) or not entity_id.strip():
                entity_id = str(_uuid.uuid5(
                    _ENTITY_NS, f"{source_key}:{index}:"
                               f"{json.dumps(payload, sort_keys=True)}"))
                payload = dict(payload)
                payload["id"] = entity_id
                decoded[index] = payload
                updated_list = True
            values: dict[str, Any] = {
                id_column: entity_id,
                "sort_order": index,
                "payload": json.dumps(payload, ensure_ascii=False),
                "updated_at": now,
            }
            if extension_kind is not None:
                values["kind"] = extension_kind
                values["owner_id"] = payload.get("ownerId") if isinstance(
                    payload.get("ownerId"), str) else None
            if table == "assistant_memory_rows":
                assistant_id = payload.get("assistantId")
                if not isinstance(assistant_id, str) or not assistant_id:
                    diagnostics.append(Diagnostic(
                        "unsupported_data", "assistant memory", 1,
                        "旧助手记忆缺少 assistantId，已跳过"))
                    continue
                values["assistant_id"] = assistant_id
            if table == "memory_entry_rows":
                _fill_memory_entry(values, payload, now)
            tables[table].rows.append(_row_for(table, values))
        if updated_list:
            source[source_key] = json.dumps(decoded, ensure_ascii=False)


def _fill_memory_entry(values: dict[str, Any], payload: dict[str, Any],
                       now: int) -> None:
    content = str(payload.get("content") or "")
    scope = str(payload.get("scope") or "assistant")
    type_ = str(payload.get("type") or "instruction")
    status = str(payload.get("status") or "active")
    created = payload.get("createdAt")
    updated = payload.get("updatedAt")
    values.update({
        "scope": scope,
        "assistant_id": payload.get("assistantId")
        if isinstance(payload.get("assistantId"), str) else None,
        "type": type_,
        "status": status,
        "content": content,
        "content_normalized": _normalize_memory(content),
        "entry_created_at": int(created) if isinstance(created, int)
        else int(updated) if isinstance(updated, int) else 0,
        "entry_updated_at": int(updated) if isinstance(updated, int)
        else int(created) if isinstance(created, int) else 0,
    })


def _normalize_memory(content: str) -> str:
    return " ".join(content.split()).lower()


def _fill_preferences(tables: dict[str, TableRows],
                      settings: model.SettingsResult,
                      diagnostics: list[Diagnostic],
                      *, only_empty: bool = False) -> None:
    now = _now_us()
    table = tables["preference_rows"]
    if only_empty and table.rows:
        return
    invalid = 0
    for key, value in settings.settings.items():
        if key in _ENTITY_KEYS or key == _PROVIDER_ORDER_KEY:
            continue
        if not isinstance(key, str) or not key:
            continue
        if not _valid_preference(value):
            invalid += 1
            continue
        table.rows.append((key, json.dumps(value, ensure_ascii=False), now))
    if invalid:
        diagnostics.append(Diagnostic(
            "unsupported_data", "preference", invalid,
            "Kelivo 设置只能保存标量或字符串列表，已跳过其它值"))


def _valid_preference(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, int) \
            or isinstance(value, float) or isinstance(value, str):
        return True
    return isinstance(value, list) and all(
        isinstance(item, str) for item in value)


# ---------------------------------------------------------------------------
# Snapshot -> historical chats.json payload
# ---------------------------------------------------------------------------

_LEGACY_DELETED_KEYS = (
    "conversation", "message", "assistant", "worldBook", "quickPhrase",
    "mcpServer", "memory", "groupChat", "workspaceFile",
)


def encode_legacy(snapshot: Snapshot, target: TargetProfile,
                  settings: model.SettingsResult,
                  asset_entries: list[Asset]) -> tuple[KelivoPayload, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    cuplivo = target.app == "cuplivo"
    dialect = model.SELECTION_ACTUAL if cuplivo else model.SELECTION_ORDINAL
    conversations: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    tool_events: dict[str, list[dict[str, Any]]] = {}
    gemini_sigs: dict[str, str] = {}
    assistant_names = _assistant_names(settings.settings)
    speaker_names = _speaker_names(snapshot, settings)
    for conv in snapshot.conversations:
        group_names = {group.id: group for group in snapshot.group_chats
                       if group.conversation_id == conv.id}
        if not cuplivo and group_names:
            _decorate_group_messages(conv, speaker_names, diagnostics)
        message_ids: list[str] = []
        selections: dict[str, int] = {}
        first_index_by_group: dict[str, int] = {}
        for index, message in enumerate(conv.messages):
            first_index_by_group.setdefault(message.group_id or message.id, index)
            encoded = _legacy_message_dict(
                message, conv, cuplivo, tool_events, gemini_sigs, diagnostics)
            messages.append(encoded)
        if cuplivo:
            group_sizes: dict[str, int] = {}
            for group in conv.groups:
                group_sizes[group.group_id] = group_sizes.get(
                    group.group_id, 0) + len(group.message_ids)
            for group in conv.groups:
                selected = next((m for m in conv.messages
                                 if m.id == group.selected_message_id), None)
                chosen = selected or next(
                    (m for m in conv.messages
                     if (m.group_id or m.id) == group.group_id), None)
                if chosen is not None:
                    message_ids.append(chosen.id)
                if selected is not None and selected.version is not None \
                        and group.selection_explicit \
                        and (len(group.message_ids) > 1
                             or group.lineage_split
                             or group_sizes.get(group.group_id, 0) > 1):
                    selections[group.group_id] = selected.version
        else:
            message_ids = [message.id for message in conv.messages]
            for group in conv.groups:
                members = [m for m in conv.messages
                           if (m.group_id or m.id) == group.group_id]
                ordered = sorted(
                    members,
                    key=lambda m: (m.version if m.version is not None else 0))
                selected_index = next(
                    (index for index, m in enumerate(ordered)
                     if m.id == group.selected_message_id), None)
                if selected_index is not None and group.selection_explicit \
                        and (len(group.message_ids) > 1
                             or group.lineage_split):
                    selections[group.group_id] = selected_index
        truncate = -1
        if conv.truncate_slot is not None:
            if cuplivo:
                truncate = min(conv.truncate_slot, len(conv.groups))
            else:
                if conv.truncate_slot >= len(conv.groups):
                    truncate = len(message_ids)
                else:
                    group = conv.groups[conv.truncate_slot]
                    truncate = first_index_by_group.get(group.group_id,
                                                        len(message_ids))
        conversations.append({
            "id": conv.id,
            "title": conv.title,
            "createdAt": ms_to_iso(conv.created_ms) or "",
            "updatedAt": ms_to_iso(conv.updated_ms) or "",
            "messageIds": message_ids,
            "isPinned": conv.is_pinned,
            "mcpServerIds": [settings.entity_ids.get("mcp", {}).get(server_id)
                             or server_id for server_id in conv.mcp_ids],
            "assistantId": settings.assistant_ids.get(
                conv.assistant_id or "", conv.assistant_id),
            "truncateIndex": truncate,
            "versionSelections": selections,
            "summary": conv.summary,
            "lastSummarizedMessageCount": conv.last_summarized_count,
            "chatSuggestions": conv.suggestions,
            "injectedMemoryHash": conv.injected_memory_hash,
            "lastMemoryExtractedOrder": conv.last_memory_extracted_order
            if conv.last_memory_extracted_order is not None else -1,
            "chatModelProvider": conv.chat_model_provider,
            "chatModelId": conv.chat_model_id,
            "extras": _legacy_extras(conv, settings),
        })
        if cuplivo and group_names:
            conversations[-1]["conversationKind"] = "group"
    group_chats: list[dict[str, Any]] = []
    group_members: list[dict[str, Any]] = []
    if cuplivo:
        for group in snapshot.group_chats:
            group_chats.append(dict(group.native) if group.native else {
                "id": group.id,
                "conversationId": group.conversation_id,
            })
            group_members.extend(dict(member) for member in group.members)
    legacy = LegacyChatData(
        root_version=2 if cuplivo else 1,
        format_version=2 if cuplivo else 1,
        conversations=conversations,
        messages=messages,
        tool_events=tool_events,
        gemini_sigs=gemini_sigs,
        group_chats=group_chats,
        group_members=group_members,
        jsonl=False,
        meta={"deleted": snapshot.deleted_document
              or {key: [] for key in _LEGACY_DELETED_KEYS}}
        if cuplivo else {},
        cuplivo_evidence=("Cuplivo legacy JSON2",) if cuplivo else (),
    )
    output_settings = dict(settings.settings)
    if any(conv.chat_model_id or conv.chat_model_provider
           for conv in snapshot.conversations):
        output_settings["per_chat_model_enabled_v1"] = True
    payload = KelivoPayload(settings=output_settings, tables={},
                            legacy_chat=legacy, scope=_output_scope(snapshot))
    return payload, diagnostics


def _assistant_names(settings: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    assistants = _parse_json(settings.get("assistants_v1"))
    if isinstance(assistants, list):
        for assistant in assistants:
            if isinstance(assistant, dict) and isinstance(assistant.get("id"), str):
                names[assistant["id"]] = str(assistant.get("name") or "")
    return names


def _speaker_names(snapshot: Snapshot,
                   settings: model.SettingsResult) -> dict[str, str]:
    names = _assistant_names(settings.settings)
    for conv in snapshot.conversations:
        for message in conv.messages:
            if message.speaker_id and message.speaker_id in names:
                names.setdefault(message.speaker_id, names[message.speaker_id])
    return names


def _decorate_group_messages(conv: Conv, names: dict[str, str],
                             diagnostics: list[Diagnostic]) -> None:
    decorated = 0
    for message in conv.messages:
        if message.role != "assistant" or not message.speaker_id:
            continue
        speaker = names.get(message.speaker_id) or message.speaker_id
        text_part = next((part for part in message.parts
                          if part.kind == "text"), None)
        if text_part is None:
            text_part = Part("text", text="")
            message.parts.insert(0, text_part)
        body = text_part.text or ""
        quoted = "\n".join(f"> {line}" for line in body.split("\n")) if body else ">"
        text_part.text = f"> **{speaker}**:\n{quoted}"
        decorated += 1
    if decorated:
        diagnostics.append(Diagnostic(
            "unsupported_data", "group chat", decorated,
            "目标不支持群聊控制，消息正文已加入发送者引用"))


def _legacy_extras(conv: Conv,
                   settings: model.SettingsResult) -> dict[str, Any]:
    extras = {key: value for key, value in conv.extras.items()
              if key not in _INTERNAL_EXTRA_KEYS}
    config = conv.extras.get("config")
    config = config if isinstance(config, dict) else {}
    workspace_id = conv.extras.get("workspaceId") or config.get("workspaceId")
    if isinstance(workspace_id, str) and workspace_id:
        extras["workspace.id"] = settings.workspace_ids.get(
            workspace_id, workspace_id)
    if isinstance(conv.extras.get("workspaceCwd"), str) and conv.extras[
            "workspaceCwd"]:
        extras["workspace.cwd"] = conv.extras["workspaceCwd"]
    if isinstance(conv.extras.get("enabledSkills"), list):
        extras["skills.ids"] = [
            settings.skill_ids.get(item, item)
            for item in conv.extras["enabledSkills"]
            if isinstance(item, str)]
    if isinstance(conv.extras.get("customSystemPrompt"), str) and conv.extras[
            "customSystemPrompt"]:
        permissions = settings.assistant_permissions.get(
            conv.assistant_id or "", {})
        if permissions.get("allow_system_prompt", True):
            extras["prompt.system"] = conv.extras["customSystemPrompt"]
    instructions = conv.extras.get("modeInjectionIds")
    if isinstance(instructions, list) and instructions:
        extras["prompt.instructionIds"] = [
            settings.entity_ids.get("instruction", {}).get(item) or item
            for item in instructions if isinstance(item, str)]
    lorebooks = conv.extras.get("lorebookIds")
    if isinstance(lorebooks, list) and lorebooks:
        extras["prompt.worldBookIds"] = [
            settings.entity_ids.get("worldBook", {}).get(item) or item
            for item in lorebooks if isinstance(item, str)]
    return extras


def _legacy_message_dict(message: Msg, conv: Conv, cuplivo: bool,
                         tool_events: dict[str, list[dict[str, Any]]],
                         gemini_sigs: dict[str, str],
                         diagnostics: list[Diagnostic]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    texts: list[str] = []
    markers: list[str] = []
    reasoning_texts: list[str] = []
    events: list[dict[str, Any]] = []
    for part in message.parts:
        if part.kind == "text":
            texts.append(part.text or "")
            parts.append({"kind": "text", "payload": part.text or ""})
        elif part.kind == "reasoning":
            reasoning_texts.append(part.text or "")
            parts.append({"kind": "reasoning", "payload": part.text or ""})
        elif part.kind == "tool":
            event = {
                "id": part.call_id or "",
                "name": part.name or "",
                "arguments": _arguments(part.input_json),
            }
            output_text = "\n".join(
                child.text or "" for child in part.output or []
                if child.kind == "text")
            if output_text:
                event["content"] = output_text
            if part.metadata:
                event["metadata"] = part.metadata
            events.append(event)
            parts.append({"kind": "tool_call",
                          "payload": json.dumps(event, ensure_ascii=False)})
        elif part.kind == "image":
            payload = {"uri": part.uri or ""}
            if part.mime:
                payload["mime"] = part.mime
            if part.asset_id:
                payload["assetId"] = part.asset_id
            if part.unavailable:
                payload["unavailable"] = True
            parts.append({"kind": "image",
                          "payload": json.dumps(payload, ensure_ascii=False)})
            marker_path = _marker_path(part.uri or "")
            if marker_path:
                markers.append(f"[image:{marker_path}]")
        elif part.kind in ("file", "video", "audio"):
            payload = {"uri": part.uri or "",
                       "name": part.name or _basename(part.uri or "") or "file"}
            mime = part.mime or ("video/mp4" if part.kind == "video"
                                 else "audio/mpeg" if part.kind == "audio"
                                 else "application/octet-stream")
            payload["mime"] = mime
            if part.asset_id:
                payload["assetId"] = part.asset_id
            if part.unavailable:
                payload["unavailable"] = True
            parts.append({"kind": "file",
                          "payload": json.dumps(payload, ensure_ascii=False)})
            marker_path = _marker_path(part.uri or "")
            if marker_path:
                markers.append(
                    f"[file:{marker_path}|{payload['name']}|{mime}]")
        elif part.kind == "server_tool":
            readable = part.text or ""
            if readable:
                texts.append(readable)
                parts.append({"kind": "text", "payload": readable})
            diagnostics.append(Diagnostic(
                "unsupported_data", "server tool", 1,
                "服务端工具调用语义在历史 JSON 中无等价表达"))
        else:
            if part.kind == "unknown" and part.raw_kind \
                    and part.raw_payload is not None \
                    and part.native.get("origin_family") == "kelivo":
                parts.append({"kind": part.raw_kind,
                              "payload": part.raw_payload})
                continue
            diagnostics.append(Diagnostic(
                "unsupported_data", "message part", 1,
                f"历史 JSON 无法表达的消息部件 {part.raw_kind or part.kind}"))
    content = "\n\n".join(texts)
    if markers:
        marker_text = "\n".join(markers)
        content = f"{content}\n{marker_text}" if content else marker_text
    if events:
        tool_events[message.id] = events
    signature = next((artifact.payload for artifact in message.artifacts
                      if artifact.kind == "gemini_thought_signature"), None)
    if signature:
        gemini_sigs[message.id] = signature
    lost_artifacts = [artifact for artifact in message.artifacts
                      if artifact.kind != "gemini_thought_signature"]
    if lost_artifacts:
        diagnostics.append(Diagnostic(
            "unsupported_data", "provider artifact",
            len(lost_artifacts),
            "历史 JSON 只能表达 Gemini 思考签名"))
    reasoning_text = "\n\n".join(reasoning_texts) if reasoning_texts else None
    reasoning_start = next((part.created_ms for part in message.parts
                            if part.kind == "reasoning" and part.created_ms), None)
    reasoning_finished = next((part.finished_ms for part in reversed(
        message.parts) if part.kind == "reasoning" and part.finished_ms), None)
    dictionary: dict[str, Any] = {
        "id": message.id,
        "role": message.role,
        "content": content,
        "parts": parts,
        "timestamp": ms_to_iso(
            message.created_ms if message.created_ms is not None
            else conv.created_ms) or "",
        "modelId": message.model_id,
        "providerId": message.provider_id,
        "totalTokens": message.usage.total if message.usage else None,
        "conversationId": conv.id,
        "isStreaming": False,
        "reasoningText": reasoning_text,
        "reasoningStartAt": ms_to_iso(reasoning_start),
        "reasoningFinishedAt": ms_to_iso(reasoning_finished),
        "translation": message.translation,
        "reasoningSegmentsJson": message.reasoning_segments_json,
        "groupId": message.group_id or message.id,
        "version": message.version if message.version is not None else 0,
        "promptTokens": message.usage.prompt if message.usage else None,
        "completionTokens": message.usage.completion if message.usage else None,
        "cachedTokens": message.usage.cached if message.usage else None,
        "durationMs": message.duration_ms,
        "isPreset": message.is_preset,
        "speakerAssistantId": message.speaker_id,
    }
    if message.subgroup_id is not None:
        dictionary["subgroupId"] = message.subgroup_id
    for key, target_key in (("timing.firstTokenMs", "firstTokenMs"),
                            ("tokens.reasoning", "reasoningTokens"),
                            ("tokens.cacheWrite", "cacheWriteTokens")):
        value = message.extras.get(key)
        if isinstance(value, int):
            dictionary[target_key] = value
    if message.last_usage is not None:
        dictionary["finishUsage"] = {
            "promptTokens": message.last_usage.prompt,
            "completionTokens": message.last_usage.completion,
            "cachedTokens": message.last_usage.cached,
            "totalTokens": message.last_usage.total,
        }
    return dictionary


def _marker_path(uri: str) -> str | None:
    entry = parse_kelivo_uri(uri)
    if entry is not None:
        return "/" + entry
    managed = normalize_managed_path(uri)
    if managed is not None:
        return "/" + managed
    if uri.startswith(("http://", "https://", "data:")):
        return uri
    return None


# ---------------------------------------------------------------------------
# Native archive <-> native archive (same family)
# ---------------------------------------------------------------------------

_OFFICIAL_DROPPED_PREFERENCE_PREFIXES = (
    "manual_compress_", "proactive_care_",
)
_ASSISTANT_PROACTIVE_KEYS = (
    "enableProactiveCare", "proactiveCareNextMessageAt",
    "proactiveCarePrompt", "proactiveCareDecisionPrompt",
)
_ACTIVE_RUN_STATES = ("preparing", "requesting", "streaming", "waiting_tool")


def migrate_native(backup: Backup, target: TargetProfile
                   ) -> tuple[KelivoPayload, list[Diagnostic]]:
    payload = backup.payload
    assert isinstance(payload, KelivoPayload)
    if payload.legacy_chat is not None:
        raise ArchiveError("历史 JSON 负载需要通过投影迁移")
    diagnostics: list[Diagnostic] = []
    settings = json.loads(json.dumps(payload.settings))
    if target.app == "kelivo":
        settings = _prune_cuplivo_semantics(settings, diagnostics)
    source_tables = payload.tables
    version = backup.info.schema_version or schema.DRIFT_LATEST
    if not source_tables:
        return KelivoPayload(settings=settings, tables={}, legacy_chat=None,
                             scope=payload.scope,
                             app_version=payload.app_version), diagnostics
    _drop_active_streaming_ids(source_tables)
    _normalize_streaming(source_tables)
    diagnostics.extend(_normalize_generation_runs(source_tables))
    tables = _empty_tables(schema.DRIFT_LATEST)
    dropped_tables = 0
    for table, rows in source_tables.items():
        if table not in tables:
            dropped_tables += len(rows.rows)
            continue
        target_columns = tables[table].columns
        source_columns = rows.columns
        for row in rows.rows:
            values = dict(zip(source_columns, row))
            if table == "conversation_rows":
                values.setdefault("extras_json", "{}")
            if table == "message_rows":
                values.setdefault("extras_json", "{}")
            if table == "memory_entry_rows":
                values.setdefault("payload", "{}")
            tables[table].rows.append(
                tuple(values.get(column) for column in target_columns))
    if target.app == "kelivo":
        _prune_cuplivo_tables(tables, diagnostics)
    migrated_settings = model.SettingsResult(settings=settings)
    _fill_business_tables(tables, migrated_settings, diagnostics,
                          only_empty=True)
    _fill_preferences(tables, migrated_settings, diagnostics, only_empty=True)
    if dropped_tables:
        diagnostics.append(Diagnostic(
            "unsupported_data", "table", dropped_tables,
            "当前 schema 没有这些表"))
    if version > schema.DRIFT_LATEST:
        diagnostics.append(Diagnostic(
            "forward_extensions_dropped", "schema", 1,
            "新版本备份的未知表／列已按当前已知结构投影"))
    meta = tables["chat_storage_meta_rows"]
    forced = {"hive_migration_complete_v1", "business_migration_complete_v1"}
    meta.rows = [row for row in meta.rows
                 if str(row[0]) not in forced]
    meta.rows.extend([("hive_migration_complete_v1", "true"),
                      ("business_migration_complete_v1", "true")])
    return KelivoPayload(
        settings=settings,
        tables=tables,
        legacy_chat=None,
        scope=payload.scope,
        app_version=payload.app_version,
    ), diagnostics


def _drop_active_streaming_ids(tables: dict[str, TableRows]) -> None:
    meta = tables.get("chat_storage_meta_rows")
    if meta is None:
        return
    index = meta.index("key")
    meta.rows = [row for row in meta.rows
                 if str(row[index]) != "active_streaming_ids"]


def _normalize_streaming(tables: dict[str, TableRows]) -> None:
    message = tables.get("message_rows")
    if message is None:
        return
    index = message.index("is_streaming")
    for row_number, row in enumerate(message.rows):
        if row[index]:
            mutable = list(row)
            mutable[index] = 0
            message.rows[row_number] = tuple(mutable)


def _normalize_generation_runs(tables: dict[str, TableRows]) -> list[Diagnostic]:
    runs = tables.get("generation_run_rows")
    if runs is None or not runs.rows:
        return []
    now = _now_us()
    state_index = runs.index("state")
    revision_index = runs.index("state_revision")
    error_index = runs.index("error_code")
    created_index = runs.index("created_at")
    updated_index = runs.index("updated_at")
    terminal_index = runs.index("terminal_at")
    normalized = 0
    for row_number, row in enumerate(runs.rows):
        if str(row[state_index]) not in _ACTIVE_RUN_STATES:
            continue
        mutable = list(row)
        mutable[state_index] = "interrupted"
        mutable[revision_index] = _int(row[revision_index]) + 1
        mutable[error_index] = "app_restart"
        created = _int(row[created_index])
        stamp = max(now, created)
        mutable[updated_index] = stamp
        mutable[terminal_index] = stamp
        runs.rows[row_number] = tuple(mutable)
        normalized += 1
    if normalized:
        return [Diagnostic(
            "historical_type_normalized", "generation run", normalized,
            "恢复时中断的生成任务已归一化为 interrupted，时钟为本次准备时间")]
    return []


def _prune_cuplivo_semantics(settings: dict[str, Any],
                             diagnostics: list[Diagnostic]) -> dict[str, Any]:
    dropped_keys = 0
    for key in list(settings):
        if any(str(key).startswith(prefix)
               for prefix in _OFFICIAL_DROPPED_PREFERENCE_PREFIXES):
            value = settings.pop(key)
            if value not in (None, "", False, 0, [], {}):
                dropped_keys += 1
    assistants = _parse_json(settings.get("assistants_v1"))
    dropped_assistant_fields = 0
    if isinstance(assistants, list):
        for assistant in assistants:
            if not isinstance(assistant, dict):
                continue
            for key in _ASSISTANT_PROACTIVE_KEYS:
                if key not in assistant:
                    continue
                value = assistant.pop(key)
                if value not in (None, "", False, 0):
                    dropped_assistant_fields += 1
        settings["assistants_v1"] = json.dumps(assistants, ensure_ascii=False)
    if dropped_keys or dropped_assistant_fields:
        diagnostics.append(Diagnostic(
            "unsupported_data", "cuplivo semantics",
            dropped_keys + dropped_assistant_fields,
            "官方 Kelivo 不等价的 Cuplivo 主动关怀／手动压缩设置已移除"))
    return settings


def _prune_cuplivo_tables(tables: dict[str, TableRows],
                          diagnostics: list[Diagnostic]) -> None:
    removed = 0
    preferences = tables.get("preference_rows")
    if preferences is not None:
        key_index = preferences.index("key")
        kept = []
        for row in preferences.rows:
            key = str(row[key_index])
            if any(key.startswith(prefix)
                   for prefix in _OFFICIAL_DROPPED_PREFERENCE_PREFIXES):
                removed += 1
                continue
            kept.append(row)
        preferences.rows = kept
    assistants = tables.get("assistant_rows")
    if assistants is not None:
        payload_index = assistants.index("payload")
        for row_number, row in enumerate(assistants.rows):
            payload = _parse_json(row[payload_index])
            if not isinstance(payload, dict):
                continue
            changed = False
            for key in _ASSISTANT_PROACTIVE_KEYS:
                if key in payload and payload[key] not in (None, "", False, 0):
                    removed += 1
                    changed = True
                payload.pop(key, None)
            if changed:
                mutable = list(row)
                mutable[payload_index] = json.dumps(
                    payload, ensure_ascii=False)
                assistants.rows[row_number] = tuple(mutable)
    conversations = tables.get("conversation_rows")
    if conversations is not None:
        extras_index = (conversations.columns.index("extras_json")
                        if "extras_json" in conversations.columns else None)
        if extras_index is not None:
            for row_number, row in enumerate(conversations.rows):
                extras = _parse_json(row[extras_index])
                if not isinstance(extras, dict):
                    continue
                kept_extras = {
                    key: value for key, value in extras.items()
                    if not str(key).lower().startswith("proactive")}
                if len(kept_extras) != len(extras):
                    removed += len(extras) - len(kept_extras)
                    mutable = list(row)
                    mutable[extras_index] = json.dumps(
                        kept_extras, ensure_ascii=False)
                    conversations.rows[row_number] = tuple(mutable)
    if removed:
        diagnostics.append(Diagnostic(
            "unsupported_data", "cuplivo semantics", removed,
            "官方 Kelivo 不等价的 Cuplivo 主动关怀／手动压缩数据已移除"))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_backup(backup: Backup, target: TargetProfile, destination: Path) -> None:
    payload = backup.payload
    assert isinstance(payload, KelivoPayload)
    if target.kind == model.KIND_KELIVO_ARCHIVE:
        _write_native_archive(backup, payload, destination)
    elif target.kind == model.KIND_KELIVO_JSON:
        _write_legacy_archive(backup, payload, target, destination)
    else:
        raise ArchiveError(f"不支持写出 Kelivo 目标类型 {target.kind}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_native_archive(backup: Backup, payload: KelivoPayload,
                          destination: Path) -> None:
    from rikkelivo import __version__

    include_chats = bool(payload.tables)
    settings_bytes = json.dumps(
        payload.settings, ensure_ascii=False, separators=(",", ":")).encode()
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    scope = payload.scope or frozenset(model.SCOPE_CATEGORIES)
    include_files = bool({"files", "skills", "workspaces"} & set(scope))
    scope_json = {category: category in scope
                  for category in model.SCOPE_CATEGORIES}
    business_ids = _business_entity_row_ids(payload)
    entries: dict[str, tuple[int, str]] = {
        "settings.json": (len(settings_bytes),
                          hashlib.sha256(settings_bytes).hexdigest()),
    }
    assets = backup.assets
    for asset in assets:
        root = asset.entry.split("/", 1)[0]
        if root not in KELIVO_ASSET_ROOTS:
            raise ArchiveError(f"附件根目录不受支持: {asset.entry}")
        if include_files:
            entries[asset.entry] = (asset.size, asset.sha256)
    database_block: dict[str, Any] | None = None
    db_path: Path | None = None
    try:
        if include_chats:
            db_path = _write_drift_database(payload.tables, destination.parent)
            database_block = {
                "entry": "database/kelivo.db",
                "schemaVersion": schema.DRIFT_LATEST,
                "minimumReadableSchemaVersion": 2,
                "conversationCount": _row_count(payload.tables,
                                                "conversation_rows"),
                "messageCount": _row_count(payload.tables, "message_rows"),
            }
            entries["database/kelivo.db"] = (
                db_path.stat().st_size, _sha256_file(db_path))
        manifest = {
            "format": "kelivo-backup",
            "formatVersion": schema.DRIFT_LATEST,
            "minimumReadableFormatVersion": schema.DRIFT_LATEST,
            "payloadKind": "sqlite" if include_chats else "settings-only",
            "createdAtUtc": created_at,
            "appVersion": f"rikkelivo/{__version__}",
            "includeChats": include_chats,
            "includeFiles": include_files,
            "secretsIncluded": True,
            "businessEntityRowIds": business_ids,
            "scope": scope_json,
            "entries": {
                name: {"bytes": size, "sha256": digest}
                for name, (size, digest) in sorted(entries.items())
            },
        }
        if database_block is not None:
            manifest["database"] = database_block
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(entries):
                if name == "database/kelivo.db":
                    assert db_path is not None
                    zf.write(db_path, name)
                elif name == "settings.json":
                    zf.writestr(name, settings_bytes)
                else:
                    zf.write(_asset_path(backup, name), name)
            zf.writestr("manifest.json",
                        json.dumps(manifest, ensure_ascii=False))
    finally:
        if db_path is not None:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(db_path) + suffix)
                if candidate.exists():
                    candidate.unlink()


def _asset_path(backup: Backup, entry: str) -> Path:
    for asset in backup.assets:
        if asset.entry == entry:
            return asset.staged_path
    raise ArchiveError(f"缺少附件 {entry}")


def _row_count(tables: dict[str, TableRows], table: str) -> int:
    rows = tables.get(table)
    return len(rows.rows) if rows else 0


def _write_legacy_archive(backup: Backup, payload: KelivoPayload,
                          target: TargetProfile, destination: Path) -> None:
    legacy = payload.legacy_chat
    assert legacy is not None
    document = {
        "version": 2 if target.app == "cuplivo" else 1,
        "conversations": legacy.conversations,
        "messages": legacy.messages,
        "toolEvents": legacy.tool_events,
        "geminiThoughtSigs": legacy.gemini_sigs,
        "groupChats": legacy.group_chats,
        "groupMembers": legacy.group_members,
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", json.dumps(
            payload.settings, ensure_ascii=False, indent=1))
        zf.writestr("chats.json", json.dumps(
            document, ensure_ascii=False, indent=1))
        if target.app == "cuplivo":
            zf.writestr("deleted.json", json.dumps(
                (legacy.meta or {}).get("deleted")
                or {key: [] for key in _LEGACY_DELETED_KEYS},
                ensure_ascii=False))
        for asset in backup.assets:
            zf.write(asset.staged_path, asset.entry)


def _business_entity_row_ids(payload: KelivoPayload) -> dict[str, list[str]]:
    """Row ids exactly in the order the settings lists declare them."""
    tables = payload.tables
    mapping = {
        "assistants_v1": "assistant_rows",
        "provider_groups_v1": "provider_group_rows",
        "mcp_servers_v1": "mcp_server_rows",
        "world_books_v1": "world_book_rows",
        "assistant_memories_v1": "assistant_memory_rows",
        "quick_phrases_v1": "quick_phrase_rows",
        "search_services_v1": "search_service_rows",
        "tts_services_v1": "tts_service_rows",
        "instruction_injections_v1": "instruction_injection_rows",
        "assistant_tags_v1": "assistant_tag_rows",
        "memory_entries_v1": "memory_entry_rows",
        "user_profile_fields_v1": "user_profile_field_rows",
    }
    result: dict[str, list[str]] = {}
    for source_key, table in mapping.items():
        raw = _parse_json(payload.settings.get(source_key))
        ids = [str(item["id"]) for item in raw
               if isinstance(item, dict) and isinstance(item.get("id"), str)
               ] if isinstance(raw, list) else []
        if len(ids) != len(raw) if isinstance(raw, list) else False:
            ids = _table_ids(tables, table)
        result[source_key] = ids
    result["workspaces_v1"] = _settings_or_extension_ids(
        payload, "workspaces_v1", "workspace")
    result["skills_v1"] = _settings_or_extension_ids(
        payload, "skills_v1", "skill")
    return result


def _table_ids(tables: dict[str, TableRows], table: str) -> list[str]:
    rows = tables.get(table)
    if rows is None:
        return []
    id_index = rows.columns.index("id")
    sort_index = rows.columns.index("sort_order") if "sort_order" in rows.columns else None
    ordered = sorted(rows.rows,
                     key=lambda row: (int(row[sort_index])
                                      if sort_index is not None else 0,
                                      str(row[id_index])))
    return [str(row[id_index]) for row in ordered]


def _settings_or_extension_ids(payload: KelivoPayload, key: str,
                               kind: str) -> list[str]:
    raw = _parse_json(payload.settings.get(key))
    if isinstance(raw, list):
        ids = [str(item["id"]) for item in raw
               if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if len(ids) == len(raw):
            return ids
    return _extension_ids(payload.tables, kind)


def _extension_ids(tables: dict[str, TableRows], kind: str) -> list[str]:
    rows = tables.get("extension_entity_rows")
    if rows is None:
        return []
    kind_index = rows.columns.index("kind")
    id_index = rows.columns.index("id")
    sort_index = rows.columns.index("sort_order")
    return [str(row[id_index]) for row in sorted(
        (row for row in rows.rows if str(row[kind_index]) == kind),
        key=lambda row: int(row[sort_index]))]


_INSERT_ORDER = (
    "conversation_rows",
    "message_rows",
    "message_part_rows",
    "provider_artifact_rows",
    "conversation_mcp_server_rows",
    "chat_storage_meta_rows",
    "assistant_rows",
    "provider_rows",
    "provider_group_rows",
    "mcp_server_rows",
    "world_book_rows",
    "assistant_memory_rows",
    "quick_phrase_rows",
    "search_service_rows",
    "tts_service_rows",
    "instruction_injection_rows",
    "assistant_tag_rows",
    "preference_rows",
    "memory_entry_rows",
    "user_profile_field_rows",
    "message_prompt_rows",
    "tombstone_rows",
    "extension_entity_rows",
    "asset_rows",
    "message_asset_rows",
    "asset_gc_rows",
    "gc_audit_rows",
    "asset_reference_dirty_rows",
    "generation_run_rows",
)


def _write_drift_database(tables: dict[str, TableRows], directory: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="rikkelivo-kelivo-", suffix=".db", dir=directory, delete=False)
    db_path = Path(handle.name)
    handle.close()
    db_path.unlink()
    schema.create_drift_database(db_path, schema.DRIFT_LATEST)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode = DELETE")
        ordered = [table for table in _INSERT_ORDER if table in tables]
        ordered += [table for table in tables if table not in ordered]
        for table in ordered:
            rows = tables.get(table)
            if rows is None or not rows.rows:
                continue
            quoted = ", ".join(f'"{column}"' for column in rows.columns)
            placeholders = ", ".join("?" for _ in rows.columns)
            con.executemany(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                [tuple(row) for row in rows.rows])
        con.commit()
        schema.validate_drift_database(con, schema.DRIFT_LATEST)
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise ArchiveError("输出 Kelivo 数据库完整性检查失败")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise ArchiveError("输出 Kelivo 数据库外键检查失败")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return db_path
