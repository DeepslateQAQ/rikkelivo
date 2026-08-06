"""RikkaHub backup format: rikka_hub.db (SQLite) plus settings.json and upload/.

The database schema below is copied verbatim from a real RikkaHub backup
(sqlite_master).  The FTS5 tables are the app's message search index; the
converter populates them so search works immediately after restore.  The
`room_master_table` is intentionally left empty: Room adopts databases whose
master table is missing by writing its own identity hash.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

DDL: list[str] = [
    """CREATE TABLE `ConversationEntity` (`id` TEXT NOT NULL,
        `assistant_id` TEXT NOT NULL DEFAULT '0950e2dc-9bd5-4801-afa3-aa887aa36b4e',
        `title` TEXT NOT NULL, `nodes` TEXT NOT NULL,
        `create_at` INTEGER NOT NULL, `update_at` INTEGER NOT NULL,
        `suggestions` TEXT NOT NULL DEFAULT '[]',
        `is_pinned` INTEGER NOT NULL DEFAULT 0,
        `custom_system_prompt` TEXT NOT NULL DEFAULT '',
        `mode_injection_ids` TEXT NOT NULL DEFAULT '[]',
        `lorebook_ids` TEXT NOT NULL DEFAULT '[]',
        `workspace_cwd` TEXT NOT NULL DEFAULT '',
        `folder_id` TEXT NOT NULL DEFAULT '', PRIMARY KEY(`id`));""",
    """CREATE TABLE `GenMediaEntity` (`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        `path` TEXT NOT NULL, `model_id` TEXT NOT NULL, `prompt` TEXT NOT NULL,
        `create_at` INTEGER NOT NULL, `type` TEXT NOT NULL DEFAULT 'image_generation',
        `source_paths` TEXT);""",
    """CREATE TABLE `MemoryEntity` (`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        `assistant_id` TEXT NOT NULL, `content` TEXT NOT NULL);""",
    "CREATE TABLE android_metadata (locale TEXT);",
    """CREATE TABLE `conversation_folder` (`id` TEXT NOT NULL,
        `assistant_id` TEXT NOT NULL, `name` TEXT NOT NULL,
        `sort_index` INTEGER NOT NULL DEFAULT 0, `create_at` INTEGER NOT NULL,
        PRIMARY KEY(`id`));""",
    """CREATE TABLE `favorites` (`id` TEXT NOT NULL, `type` TEXT NOT NULL,
        `ref_key` TEXT NOT NULL, `ref_json` TEXT NOT NULL,
        `snapshot_json` TEXT NOT NULL, `meta_json` TEXT,
        `created_at` INTEGER NOT NULL, `updated_at` INTEGER NOT NULL,
        PRIMARY KEY(`id`));""",
    """CREATE TABLE `managed_files` (`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        `folder` TEXT NOT NULL, `relative_path` TEXT NOT NULL,
        `display_name` TEXT NOT NULL, `mime_type` TEXT NOT NULL,
        `size_bytes` INTEGER NOT NULL, `created_at` INTEGER NOT NULL,
        `updated_at` INTEGER NOT NULL);""",
    """CREATE VIRTUAL TABLE message_fts USING fts5(
        text, node_id UNINDEXED, message_id UNINDEXED,
        conversation_id UNINDEXED, title UNINDEXED, update_at UNINDEXED,
        tokenize = 'simple');""",
    """CREATE TABLE `message_node` (`id` TEXT NOT NULL,
        `conversation_id` TEXT NOT NULL, `node_index` INTEGER NOT NULL,
        `messages` TEXT NOT NULL, `select_index` INTEGER NOT NULL,
        PRIMARY KEY(`id`),
        FOREIGN KEY(`conversation_id`) REFERENCES `ConversationEntity`(`id`)
        ON UPDATE NO ACTION ON DELETE CASCADE );""",
    "CREATE TABLE room_master_table (id INTEGER PRIMARY KEY,identity_hash TEXT);",
    """CREATE TABLE "workspaces" (`id` TEXT NOT NULL, `name` TEXT NOT NULL,
        `root` TEXT NOT NULL, `shell_status` TEXT NOT NULL,
        `created_at` INTEGER NOT NULL, `updated_at` INTEGER NOT NULL,
        `last_access_at` INTEGER,
        `tool_approvals` TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(`id`));""",
    "CREATE INDEX `index_message_node_conversation_id` ON `message_node` (`conversation_id`);",
    "CREATE UNIQUE INDEX `index_managed_files_relative_path` ON `managed_files` (`relative_path`);",
    "CREATE INDEX `index_managed_files_folder` ON `managed_files` (`folder`);",
    "CREATE UNIQUE INDEX `index_favorites_ref_key` ON `favorites` (`ref_key`);",
    "CREATE INDEX `index_favorites_type` ON `favorites` (`type`);",
    "CREATE INDEX `index_favorites_created_at` ON `favorites` (`created_at`);",
    "CREATE UNIQUE INDEX `index_workspaces_root` ON `workspaces` (`root`);",
    "CREATE INDEX `index_workspaces_updated_at` ON `workspaces` (`updated_at`);",
    "CREATE INDEX `index_conversation_folder_assistant_id` ON `conversation_folder` (`assistant_id`);",
]

# Room identity hash for schema version 24 (from app/schemas/.../24.json).
ROOM_IDENTITY_HASH = "0ea1aaebfa031c7995c45a1e35822e1a"
ROOM_SCHEMA_VERSION = 24

# RikkaHub's own default assistant object, used as the template when writing
# assistants derived from Kelivo data (fields not present in the source keep
# these values).  Harvested from a real backup.
RH_ASSISTANT_DEFAULTS: dict[str, Any] = {
    "tags": [],
    "contextMessageLimit": 0,
    "streamOutput": True,
    "enableMemory": True,
    "useGlobalMemory": False,
    "enableRecentChatsReference": False,
    "messageTemplate": "{{ message }}",
    "presetMessages": [],
    "quickMessageIds": [],
    "regexes": [],
    "reasoningLevel": "high",
    "maxTokens": None,
    "customHeaders": [],
    "customBodies": [],
    "mcpServers": [],
    "localTools": [
        {"type": "time_info"},
        {"type": "javascript_engine"},
        {"type": "clipboard"},
        {"type": "ask_user"},
    ],
    "enableWebSearch": False,
    "workspaceId": None,
    "background": None,
    "backgroundOpacity": 1.0,
    "useGradientBackground": False,
    "modeInjectionIds": [],
    "lorebookIds": [],
    "enabledSkills": [],
    "enableTimeReminder": True,
    "allowConversationSystemPrompt": False,
    "allowConversationPromptInjection": False,
}

# RikkaHub provider template (harvested); per-entry values are overlaid.
RH_PROVIDER_DEFAULTS: dict[str, Any] = {
    "type": "openai",
    "enabled": True,
    "apiKey": "",
    "baseUrl": "",
    "chatCompletionsPath": "/chat/completions",
    "useResponseApi": False,
    "balanceOption": {"enabled": False, "apiPath": "/credits",
                      "resultPath": "data.total_usage"},
    "includeHistoryReasoning": True,
}

# RikkaHub model template (harvested).
RH_MODEL_DEFAULTS: dict[str, Any] = {
    "type": "CHAT",
    "customHeaders": [],
    "customBodies": [],
    "inputModalities": ["TEXT"],
    "outputModalities": ["TEXT"],
    "abilities": ["TOOL"],
    "tools": [],
    "providerOverwrite": None,
}


def create_database(path: str) -> tuple[sqlite3.Connection, str | None]:
    """Create an empty RikkaHub database with the exact app schema.

    Room validates the schema version and, when room_master_table is missing
    or empty, re-validates every entity table against its compiled schema
    before adopting the database.  We write the exact v24 identity hash so the
    database is indistinguishable from an app-made backup.

    Returns (connection, fts_warning).  The FTS5 search index uses the app's
    `simple` tokenizer; builds without it fall back to `unicode61` (search
    still works, tokenization differs slightly).
    """
    con = sqlite3.connect(path)
    warning: str | None = None
    for stmt in DDL:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "tokenizer" in str(exc):
                warning = ("! 消息搜索索引使用 unicode61 分词器 "
                           "(当前 Python 的 SQLite 缺少 FTS5 'simple')")
                continue
            raise
    if warning:
        con.execute(
            "CREATE VIRTUAL TABLE message_fts USING fts5("
            " text, node_id UNINDEXED, message_id UNINDEXED,"
            " conversation_id UNINDEXED, title UNINDEXED, update_at UNINDEXED,"
            " tokenize = 'unicode61');")
    con.execute(f"PRAGMA user_version = {ROOM_SCHEMA_VERSION}")
    con.execute("INSERT OR REPLACE INTO room_master_table (id, identity_hash)"
                " VALUES (42, ?)", (ROOM_IDENTITY_HASH,))
    con.execute("INSERT INTO android_metadata (locale) VALUES (?)",
                ("zh_CN_#Hans",))
    con.commit()
    return con, warning


def write_conversation(
    con: sqlite3.Connection,
    conv: dict[str, Any],
) -> None:
    """Insert one ConversationEntity row."""
    con.execute(
        "INSERT INTO ConversationEntity (id, assistant_id, title, nodes,"
        " create_at, update_at, suggestions, is_pinned, custom_system_prompt,"
        " mode_injection_ids, lorebook_ids, workspace_cwd, folder_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conv["id"],
            conv.get("assistant_id", ""),
            conv["title"],
            conv.get("nodes", "[]"),
            conv["create_at"],
            conv["update_at"],
            conv.get("suggestions", "[]"),
            conv.get("is_pinned", 0),
            conv.get("custom_system_prompt", ""),
            conv.get("mode_injection_ids", "[]"),
            conv.get("lorebook_ids", "[]"),
            conv.get("workspace_cwd", ""),
            conv.get("folder_id", ""),
        ),
    )


def write_node(
    con: sqlite3.Connection,
    node_id: str,
    conversation_id: str,
    node_index: int,
    messages: list[dict[str, Any]],
    select_index: int | None = None,
) -> None:
    """Insert one message_node row."""
    if select_index is None:
        select_index = max(0, len(messages) - 1)
    con.execute(
        "INSERT INTO message_node (id, conversation_id, node_index,"
        " messages, select_index) VALUES (?, ?, ?, ?, ?)",
        (node_id, conversation_id, node_index,
         json.dumps(messages, ensure_ascii=False), select_index),
    )


def write_fts(
    con: sqlite3.Connection,
    rows: Iterable[tuple[str, str, str, str, str, str]],
) -> None:
    """Populate the FTS5 search index (text, node_id, message_id,
    conversation_id, title, update_at)."""
    con.executemany("INSERT INTO message_fts (text, node_id, message_id,"
                    " conversation_id, title, update_at) VALUES (?, ?, ?, ?, ?, ?)",
                    rows)


def write_managed_file(
    con: sqlite3.Connection,
    folder: str,
    relative_path: str,
    display_name: str,
    mime_type: str,
    size_bytes: int,
    created_at: int,
    updated_at: int,
) -> None:
    con.execute(
        "INSERT INTO managed_files (folder, relative_path, display_name,"
        " mime_type, size_bytes, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (folder, relative_path, display_name, mime_type, size_bytes,
         created_at, updated_at),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_conversations(con: sqlite3.Connection) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    out = []
    for row in con.execute("SELECT * FROM ConversationEntity ORDER BY update_at"):
        out.append(dict(row))
    return out


def load_nodes(con: sqlite3.Connection) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    out = []
    for row in con.execute("SELECT * FROM message_node"
                           " ORDER BY conversation_id, node_index"):
        d = dict(row)
        d["messages"] = json.loads(d["messages"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# RikkaHub message -> Kelivo message conversion
# ---------------------------------------------------------------------------

_IMAGE_MARKER = "[image:{path}]"
_FILE_MARKER = "[file:{path}|{name}|{mime}]"


def _parts_to_markers(parts: list[dict[str, Any]]) -> str:
    """Serialize image/document parts into Kelivo attachment markers.

    Kelivo stores attachments as `[image:<path>]` / `[file:<path>|<name>|<mime>]`
    markers appended to the message content; absolute local paths are resolved
    through SandboxPathResolver.fix(), which maps any path containing an
    /upload/ (or /images/, /avatars/) segment onto the current app data dir.
    """
    markers: list[str] = []
    for p in parts:
        kind = p.get("type")
        url = p.get("url") or ""
        path = url[7:] if url.startswith("file://") else url
        if kind == "image" and path:
            markers.append(_IMAGE_MARKER.format(path=path))
        elif kind == "document" and path:
            markers.append(_FILE_MARKER.format(
                path=path,
                name=(p.get("fileName") or path.rsplit("/", 1)[-1] or "file"),
                mime=(p.get("mime") or "text/plain"),
            ))
    return "\n".join(markers)


def _usage_field(usage: dict[str, Any] | None, key: str) -> int | None:
    if not usage:
        return None
    value = usage.get(key)
    return value if isinstance(value, int) else None


def to_kelivo_message(
    rmsg: dict[str, Any],
    conversation_id: str,
    *,
    group_id: str | None = None,
    version: int = 0,
    model_name: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Map a RikkaHub message onto the Kelivo shape.

    Returns (kelivo_message, tool_events, dropped_tool_parts).  Kelivo records
    tool calls per assistant message in its toolEvents map (exported to
    chats.json) with entries {id, name, arguments, content}; RikkaHub tool
    parts are converted into those events instead of being dropped.

    `group_id`/`version`/`model_name` are the Kelivo-side values derived from
    the node structure (a RikkaHub node is one Kelivo group; each message in
    the node is a version).  A `x_rikkelivo` field attached by the reverse
    conversion restores the original Kelivo values instead, so group chats
    survive a round trip through RikkaHub.
    """
    parts = rmsg.get("parts") or []
    texts: list[str] = []
    reasoning: str | None = None
    reasoning_start: str | None = None
    reasoning_finish: str | None = None
    tool_events: list[dict[str, Any]] = []
    marker_parts: list[dict[str, Any]] = []
    for p in parts:
        kind = p.get("type")
        if kind == "text":
            texts.append(p.get("text") or "")
        elif kind == "reasoning":
            if reasoning is None:
                reasoning = p.get("reasoning")
                reasoning_start = p.get("createdAt")
                reasoning_finish = p.get("finishedAt")
        elif kind == "tool":
            event = _tool_part_to_event(p)
            if event is not None:
                tool_events.append(event)
        elif kind in ("image", "document"):
            marker_parts.append(p)

    content = "\n\n".join(texts)
    markers = _parts_to_markers(marker_parts)
    if markers:
        content = f"{content}\n{markers}" if content else markers

    extra = rmsg.get("x_rikkelivo")
    if not isinstance(extra, dict):
        extra = {}
    content_original = extra.get("contentOriginal")
    if isinstance(content_original, str) and content_original:
        # Undo the speaker blockquote decoration from the forward conversion;
        # attachment markers were appended after the original content.
        content = content_original
        if markers:
            content = f"{content}\n{markers}" if content else markers

    usage = rmsg.get("usage") or {}
    return (
        {
            "id": rmsg["id"],
            "role": rmsg.get("role", "user"),
            "content": content,
            "timestamp": rmsg.get("createdAt"),
            "modelId": extra.get("modelId") or model_name,
            "providerId": extra.get("providerId"),
            "totalTokens": _usage_field(usage, "totalTokens"),
            "conversationId": conversation_id,
            "isStreaming": False,
            "reasoningText": reasoning,
            "reasoningStartAt": reasoning_start,
            "reasoningFinishedAt": reasoning_finish,
            "translation": rmsg.get("translation"),
            "reasoningSegmentsJson": None,
            "groupId": extra.get("groupId") or group_id or rmsg["id"],
            "subgroupId": extra.get("subgroupId"),
            "version": extra.get("version", version),
            "promptTokens": _usage_field(usage, "promptTokens"),
            "completionTokens": _usage_field(usage, "completionTokens"),
            "cachedTokens": _usage_field(usage, "cachedTokens"),
            "durationMs": None,
            "isPreset": bool(extra.get("isPreset", False)),
            "speakerAssistantId": extra.get("speaker"),
        },
        tool_events,
        0,  # tool parts are now converted to events, never dropped
    )


def _tool_part_to_event(p: dict[str, Any]) -> dict[str, Any] | None:
    """RikkaHub tool part -> Kelivo toolEvent entry.

    Kelivo event shape (chats.json toolEvents):
    {id, name, arguments: Map, content: String?, metadata?}.
    """
    name = p.get("toolName")
    if not name:
        return None
    arguments: dict[str, Any] = {}
    raw_input = p.get("input")
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                arguments = parsed
        except (ValueError, TypeError):
            arguments = {"raw": raw_input}
    elif isinstance(raw_input, dict):
        arguments = raw_input
    # Output is an array of parts; join text parts into the event content.
    content: str | None = None
    output = p.get("output")
    if isinstance(output, list):
        chunks = []
        for part in output:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text") or ""))
            else:
                chunks.append(json.dumps(part, ensure_ascii=False))
        if chunks:
            content = "\n".join(chunks)
    event: dict[str, Any] = {
        "id": str(p.get("toolCallId") or ""),
        "name": str(name),
        "arguments": arguments,
        "content": content,
    }
    if isinstance(p.get("metadata"), dict) and p["metadata"]:
        event["metadata"] = p["metadata"]
    return event
