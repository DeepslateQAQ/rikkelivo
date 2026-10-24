"""Deterministic backup fixtures and independent output observables.

Fixtures are written directly against the vendored schemas and the historical
wire shapes; they never call the converter's writers.  ``read_observables``
opens a produced backup with plain ZIP/JSON/SQLite code and interprets the
selection encoding according to the *consumer* format, so tests cannot pass by
reusing the code under test.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from rikkelivo import model, schema

CONV1 = "11111111-1111-4111-8111-111111111111"
CONV2 = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"
PROVIDER = "44444444-4444-4444-8444-444444444444"
MODEL = "55555555-5555-4555-8555-555555555555"
NODE_USER1 = "99999999-9999-4999-8999-999999999991"
NODE1 = "66666666-6666-4666-8666-666666666666"
NODE_USER2 = "99999999-9999-4999-8999-999999999992"
NODE2 = "77777777-7777-4777-8777-777777777777"
MSG_USER1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
MSG_A0 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb0"
MSG_A2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
MSG_A5 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb5"
MSG_USER2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
MSG_A2B = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"

KCONV1 = "conv-1"
KCONV2 = "conv-2"
KASSISTANT = "assistant-1"
KPROVIDER = "provider-1"
KMSG_USER1 = "k-user-1"
KMSG_A0 = "k-a0"
KMSG_A2 = "k-a2"
KMSG_A5 = "k-a5"
KMSG_USER2 = "k-user-2"
KMSG_A2B = "k-a2b"
KGROUP1 = "k-group-1"

ANSWER_V2 = "answer-v2"
RH_ASSISTANT = "0950e2dc-9bd5-4801-afa3-aa887aa36b4e"


# ---------------------------------------------------------------------------
# Public fixture entry point
# ---------------------------------------------------------------------------

def make_backup(path: Path, *, app: str, kind: str, version: int,
                schema_version: int | None = None, settings_only: bool = False,
                rich: bool = True, variant: str | None = None) -> Path:
    """Create one deterministic source backup.

    ``kind`` is ``rh`` (Room), ``json`` (chats.json), ``jsonl`` (JSONL2) or
    ``native`` (manifest + SQLite).  ``version`` is the Room version, the
    chats.json root version, the JSONL format version, or the archive version.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "rh":
        return _make_rh(path, app=app, version=version,
                        settings_only=settings_only, rich=rich,
                        variant=variant)
    if kind == "json":
        return _make_json(path, app=app, version=version,
                          settings_only=settings_only, rich=rich,
                          variant=variant)
    if kind == "jsonl":
        return _make_jsonl(path, app=app, version=version,
                           settings_only=settings_only, rich=rich,
                           variant=variant)
    if kind == "native":
        return _make_native(path, app=app, version=version,
                            schema_version=schema_version or 3,
                            settings_only=settings_only, rich=rich,
                            variant=variant)
    raise ValueError(f"unknown fixture kind: {kind}")


# ---------------------------------------------------------------------------
# RikkaHub fixture
# ---------------------------------------------------------------------------

def _rh_messages(*, group: bool = False, checkpoint: bool = False,
                 missing_file: bool = False, missing_time: bool = False,
                 epoch_zero: bool = False
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    user1 = _rh_message(MSG_USER1, "user", [{"type": "text", "text": "question"}],
                        "2026-01-01T10:00:00.000")
    a0 = _rh_message(MSG_A0, "assistant", [
        {"type": "text", "text": "answer-v0"}], "2026-01-01T10:00:01.000")
    a2_parts = [
        {"type": "reasoning", "reasoning": "thinking",
         "createdAt": "2026-01-01T10:00:02Z",
         "finishedAt": "2026-01-01T10:00:03Z", "metadata": None,
         "reasoningType": "reasoning_text"},
        {"type": "text", "text": ANSWER_V2, "metadata": None},
        {"type": "tool", "toolCallId": "call-1", "toolName": "search",
         "input": json.dumps({"q": "x"}), "output": [
             {"type": "text", "text": "tool-result", "metadata": None}],
         "approvalState": {"type": "auto"}, "metadata": None},
        {"type": "image",
         "url": "file:///data/user/0/me.rerere.rikkahub/files/upload/fixture.png",
         "metadata": None},
    ]
    a2 = _rh_message(MSG_A2, "assistant", a2_parts, "2026-01-01T10:00:04.000",
                     usage={"promptTokens": 3, "completionTokens": 4,
                            "cachedTokens": 1, "totalTokens": 7},
                     last_usage={"promptTokens": 1, "completionTokens": 2,
                                 "cachedTokens": 0, "totalTokens": 3})
    if checkpoint:
        a2["isContextCheckpoint"] = True
    if missing_time:
        a2.pop("createdAt", None)
    if epoch_zero:
        import datetime as _dt
        a2["createdAt"] = _dt.datetime.fromtimestamp(0).strftime(
            "%Y-%m-%dT%H:%M:%S.000")
    if missing_file:
        a2["parts"].append({
            "type": "image",
            "url": "file:///data/user/0/me.rerere.rikkahub/files/upload/"
                   "missing.png",
            "metadata": None,
        })
    a5 = _rh_message(MSG_A5, "assistant", [
        {"type": "text", "text": "answer-v5"}], "2026-01-01T10:00:05.000")
    if group:
        a2["parts"].append({"type": "text", "text": "group context",
                            "metadata": None})
    user2 = _rh_message(MSG_USER2, "user", [
        {"type": "text", "text": "second question"}],
        "2026-01-01T11:00:00.000")
    a2b = _rh_message(MSG_A2B, "assistant", [
        {"type": "text", "text": "second answer", "metadata": None},
        {"type": "document",
         "url": "file:///data/user/0/me.rerere.rikkahub/files/upload/note.txt",
         "fileName": "note.txt", "mime": "text/plain", "metadata": None},
    ], "2026-01-01T11:00:01.000",
        model_snapshot={"modelId": "gpt-x", "displayName": "Fixture Model"})
    return [user1, a0, a2, a5, user2, a2b], [a0, a2, a5]


def _rh_message(message_id: str, role: str, parts: list[dict[str, Any]],
                created: str, *, usage: dict[str, int] | None = None,
                last_usage: dict[str, int] | None = None,
                model_snapshot: dict[str, str] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id,
        "role": role,
        "parts": parts,
        "annotations": [],
        "createdAt": created,
        "finishedAt": None,
        "modelId": None,
        "usage": usage,
        "translation": None,
    }
    if last_usage is not None:
        message["lastUsage"] = last_usage
    if model_snapshot is not None:
        message["modelSnapshot"] = model_snapshot
    return message


def _rh_settings(app: str) -> dict[str, Any]:
    return {
        "displaySetting": {"userNickname": "Fixtures"},
        "providers": [{
            "id": PROVIDER,
            "type": "openai",
            "enabled": True,
            "name": "Fixture Provider",
            "apiKey": "fixture-key",
            "baseUrl": "https://api.example.com/v1",
            "chatCompletionsPath": "/chat/completions",
            "useResponseApi": False,
            "models": [{
                "id": MODEL,
                "modelId": "gpt-x",
                "displayName": "Fixture Model",
                "type": "CHAT",
                "inputModalities": ["TEXT"],
                "outputModalities": ["TEXT"],
                "abilities": ["TOOL"],
            }],
        }],
        "assistants": [{
            "id": ASSISTANT,
            "name": "Fixture Assistant",
            "chatModelId": MODEL,
            "systemPrompt": "be kind to {{user}}",
            "enableMemory": True,
            "useGlobalMemory": True,
            "useAssistantAvatar": False,
            "avatar": {"type": "me.rerere.rikkahub.data.model.Avatar.Dummy"},
            "customHeaders": [],
            "customBodies": [],
            "presetMessages": [{
                "id": "88888888-8888-4888-8888-888888888888",
                "role": "user",
                "parts": [{"type": "text", "text": "preset hello",
                           "metadata": None}],
                "annotations": [],
            }],
            "enabledSkills": ["fixture-skill"],
            "localTools": ["time_info"],
        }],
        "mcpServers": [],
        "quickMessages": [{"id": "99999999-9999-4999-8999-999999999999",
                           "title": "Hi", "content": "hello"}],
        "modeInjections": [],
        "lorebooks": [],
        "searchServices": [],
        "ttsProviders": [],
        "asrProviders": [],
        "assistantTags": [],
        "chatModelId": MODEL,
        "titleModelId": MODEL,
        "suggestionModelId": MODEL,
        "translateModeId": MODEL,
        "ocrModelId": MODEL,
        "compressModelId": MODEL,
        "titlePrompt": "Summarize for {{char}}",
        "enableAutoTitle": True,
        "enableSuggestion": True,
        "translatePrompt": "Translate for {{user}}",
    }


def _make_rh(path: Path, *, app: str, version: int, settings_only: bool,
             rich: bool, variant: str | None) -> Path:
    root = path.parent / f".{path.stem}-rh"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    db = root / "rikka_hub.db"
    settings = _rh_settings(app)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", json.dumps(
            settings, ensure_ascii=False, indent=1))
        if not settings_only:
            schema.create_room_database(db, version)
            con = sqlite3.connect(db)
            try:
                _fill_rh(con, version, rich=rich, variant=variant)
                con.commit()
            finally:
                con.close()
            zf.write(db, "rikka_hub.db")
            zf.writestr("upload/fixture.png", b"fixture-image")
            zf.writestr("upload/note.txt", b"fixture-note")
            if rich and version >= 26:
                zf.writestr("media_creation/fixture.png", b"media")
        zf.writestr("skills/fixture-skill/SKILL.md", b"# Fixture skill\n")
    shutil.rmtree(root)
    return path


def _fill_rh(con: sqlite3.Connection, version: int, *, rich: bool,
             variant: str | None) -> None:
    group = variant == "group"
    messages, revisions = _rh_messages(
        group=group, checkpoint=variant == "checkpoint",
        missing_file=variant == "missing-file",
        missing_time=variant == "missing-time",
        epoch_zero=variant == "epoch-zero")
    room = schema.load_room(version)
    conv_columns = [field.column for field in room.tables[
        "ConversationEntity"].fields]
    conversations = [
        {CONV1: (CONV1, ASSISTANT, "Conversation one", "[]", 1000, 5000,
                 "[]", 0, "", "[]", "[]", "", "",
                 json.dumps({"chatModelId": MODEL})),
         CONV2: (CONV2, ASSISTANT, "Conversation two", "[]", 6000, 9000,
                 "[]", 1, "", "[]", "[]", "", "", "{}"),
         }[key]
        for key in (CONV1, CONV2)
    ]
    for values in conversations:
        mapping = dict(zip(("id", "assistant_id", "title", "nodes", "create_at",
                            "update_at", "suggestions", "is_pinned",
                            "custom_system_prompt", "mode_injection_ids",
                            "lorebook_ids", "workspace_cwd", "folder_id",
                            "config"), values))
        con.execute(
            "INSERT INTO ConversationEntity ({}) VALUES ({})".format(
                ", ".join(f'"{column}"' for column in conv_columns),
                ", ".join("?" for _ in conv_columns)),
            [mapping.get(column) for column in conv_columns])
    node_user1 = [messages[0]]
    node1_messages = [messages[1], messages[2], messages[3]]
    node_user2 = [messages[4]]
    node2_messages = [messages[5]]
    nodes = [
        (NODE_USER1, CONV1, 0, node_user1, 0),
        (NODE1, CONV1, 1, node1_messages, 1),
        (NODE_USER2, CONV2, 0, node_user2, 0),
        (NODE2, CONV2, 1, node2_messages, 0),
    ]
    for node_id, conversation_id, index, node_messages, select_index in nodes:
        con.execute(
            "INSERT INTO message_node (id,conversation_id,node_index,messages,"
            "select_index) VALUES (?,?,?,?,?)",
            (node_id, conversation_id, index,
             json.dumps(node_messages, ensure_ascii=False), select_index))
    if version >= 28:
        con.execute(
            "INSERT INTO memory_file (assistant_id,path,content,updated_at)"
            " VALUES (?,?,?,?)", (ASSISTANT, "/profile.md", "profile text", 100))
        con.execute(
            "INSERT INTO memory_file (assistant_id,path,content,updated_at)"
            " VALUES (?,?,?,?)",
            (ASSISTANT, "/topics/project.md", "project body", 200))
        con.execute(
            "INSERT INTO memory_file (assistant_id,path,content,updated_at)"
            " VALUES (?,?,?,?)", ("__global__", "/global.md", "shared", 300))
        if variant == "memory-blank":
            con.execute(
                "INSERT INTO memory_file (assistant_id,path,content,updated_at)"
                " VALUES (?,?,?,?)", (ASSISTANT, "/blank.md", "   \n", 400))
    else:
        con.execute("INSERT INTO MemoryEntity (assistant_id,content) VALUES (?,?)",
                    (ASSISTANT, "legacy memory one"))
        con.execute("INSERT INTO MemoryEntity (assistant_id,content) VALUES (?,?)",
                    (ASSISTANT, "legacy memory two"))
        if variant == "memory-blank":
            con.execute(
                "INSERT INTO MemoryEntity (assistant_id,content) VALUES (?,?)",
                (ASSISTANT, "  "))
    workspace_values = {
        "id": "wi000000-0000-4000-8000-000000000001",
        "name": "Fixture Workspace",
        "root": "fixture-workspace",
        "shell_status": "READY",
        "created_at": 10,
        "updated_at": 20,
        "last_access_at": None,
        "tool_approvals": "{}",
        "shell_compatibility_mode": 0,
    }
    workspace_columns = [field.column for field in room.tables[
        "workspaces"].fields]
    con.execute(
        "INSERT INTO workspaces ({}) VALUES ({})".format(
            ", ".join(f'"{c}"' for c in workspace_columns),
            ", ".join("?" for _ in workspace_columns)),
        [workspace_values.get(c) for c in workspace_columns])
    if version >= 26:
        con.execute(
            "INSERT INTO media_creation_session (id,title,draft,create_at,update_at)"
            " VALUES (?,?,?,?,?)", ("ms-1", "Media", "{}", 1, 2))
        con.execute(
            "INSERT INTO media_creation_node (id,session_id,selected_record_id,create_at)"
            " VALUES (?,?,?,?)", ("mn-1", "ms-1", "mr-1", 1))
        con.execute(
            "INSERT INTO media_creation_record (id,session_id,node_id,provider_id,"
            "provider_name,model_id,kind,prompt,params,inputs,status,task_id,error,"
            "outputs,create_at,update_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mr-1", "ms-1", "mn-1", "p1", "P", "m1", "image", "prompt", "{}",
             "[]", "completed", None, None, "[]", 1, 2))
    if version >= 30:
        con.execute(
            "INSERT INTO translation_history (id,source_text,translated_text,"
            "target_language,model_id,create_at) VALUES (?,?,?,?,?,?)",
            ("th-1", "hello", "你好", "zh-CN", "gpt-x", 5))
    if version >= 31:
        con.execute(
            "INSERT INTO usage_record (created_at,source,provider_id,model_id,"
            "model_name,assistant_id,conversation_id,prompt_tokens,"
            "completion_tokens,cached_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (100, "CHAT", PROVIDER, MODEL, "gpt-x", ASSISTANT, CONV1, 3, 4, 1))
        con.execute(
            "INSERT INTO usage_record (created_at,source,provider_id,model_id,"
            "model_name,assistant_id,conversation_id,prompt_tokens,"
            "completion_tokens,cached_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (200, "API", PROVIDER, MODEL, "gpt-x", ASSISTANT, None, 9, 0, 0))
    con.execute(
        "INSERT INTO favorites (id,type,ref_key,ref_json,snapshot_json,"
        "meta_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("fav-1", "message", "fixture", "{}", "{}", None, 1, 2))
    con.execute(
        "INSERT INTO conversation_folder (id,assistant_id,name,sort_index,"
        "create_at) VALUES (?,?,?,?,?)",
        ("folder-1", ASSISTANT, "Fixture", 0, 1))


# ---------------------------------------------------------------------------
# Kelivo / Cuplivo native fixture
# ---------------------------------------------------------------------------

def _kelivo_settings(app: str, *, rich: bool = True,
                     variant: str | None = None) -> dict[str, Any]:
    provider = {
        "id": KPROVIDER,
        "enabled": True,
        "name": "Fixture Provider",
        "apiKey": "fixture-key",
        "baseUrl": "https://api.example.com/v1",
        "providerType": "openai",
        "models": ["gpt-x"],
        "modelOverrides": {
            "gpt-x": {"name": "Fixture Model", "type": "chat",
                      "input": ["text"], "output": ["text"],
                      "abilities": ["tool"]},
        },
        "customHeaders": [],
        "customBody": [],
    }
    assistant = {
        "id": KASSISTANT,
        "name": "Fixture Assistant",
        "chatModelProvider": KPROVIDER,
        "chatModelId": "gpt-x",
        "enableMemory": True,
        "systemPrompt": "be kind to {user}",
        "messageTemplate": "{{ message }}",
        "presetMessages": [{"id": "preset-1", "role": "user",
                            "content": "preset hello"}],
        "skillIds": ["fixture-skill"],
        "defaultWorkspaceId": "workspace-1",
        "searchEnabled": False,
        "mcpServerIds": [],
        "customHeaders": [],
        "customBody": [],
        "regexRules": [],
    }
    settings: dict[str, Any] = {
        "provider_configs_v1": json.dumps({KPROVIDER: provider},
                                          ensure_ascii=False),
        "providers_order_v1": [KPROVIDER],
        "assistants_v1": json.dumps([assistant], ensure_ascii=False),
        "mcp_servers_v1": json.dumps([{
            "id": "mcp-1", "name": "Fixture MCP", "enabled": True,
            "transport": "streamable_http", "url": "https://mcp.example.com",
            "headers": {}, "tools": []}], ensure_ascii=False),
        "instruction_injections_v1": json.dumps([{
            "id": "instr-1", "title": "Instruction", "prompt": "inject",
            "group": "Fixture"}], ensure_ascii=False),
        "quick_phrases_v1": json.dumps([{
            "id": "phrase-1", "title": "Hi", "content": "hello",
            "isGlobal": True, "assistantId": None}], ensure_ascii=False),
        "assistant_tags_v1": json.dumps([{
            "id": "tag-1", "name": "Tag"}], ensure_ascii=False),
        "world_books_v1": json.dumps([], ensure_ascii=False),
        "search_services_v1": json.dumps([], ensure_ascii=False),
        "tts_services_v1": json.dumps([], ensure_ascii=False),
        "user_profile_fields_v1": json.dumps([], ensure_ascii=False),
        "theme_v1": "dark",
        "selected_model_v1": f"{KPROVIDER}::gpt-x",
        "title_model_v1": f"{KPROVIDER}::gpt-x",
        "title_generation_enabled_v1": True,
        "suggestion_model_v1": f"{KPROVIDER}::gpt-x",
        "suggestion_generation_enabled_v1": True,
        "translate_model_v1": f"{KPROVIDER}::gpt-x",
        "ocr_model_v1": f"{KPROVIDER}::gpt-x",
        "compress_model_v1": f"{KPROVIDER}::gpt-x",
        "title_prompt_v1": "Summarize for {assistant_name}",
        "translate_prompt_v1": "Translate for {nickname}",
        "translate_target_lang_v1": "zh-CN",
        "per_chat_model_enabled_v1": False,
    }
    if app == "kelivo":
        settings["memory_entries_v1"] = json.dumps([{
            "id": "memory-global",
            "scope": "global",
            "assistantId": None,
            "type": "identity",
            "status": "active",
            "content": "global identity",
            "source": "manual",
            "relatedIds": [],
            "createdAt": 1_700_000_000_000_000,
            "updatedAt": 1_700_000_001_000_000,
        }, {
            "id": "memory-assistant",
            "scope": "assistant",
            "assistantId": KASSISTANT,
            "type": "instruction",
            "status": "active",
            "content": "assistant instruction",
            "source": "manual",
            "relatedIds": [],
            "createdAt": 1_700_000_002_000_000,
            "updatedAt": 1_700_000_003_000_000,
        }], ensure_ascii=False)
    elif variant == "memory-edge":
        settings["memory_entries_v1"] = json.dumps([{
            "id": "memory-blank", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "instruction",
            "status": "active", "content": "   ", "source": "manual",
            "relatedIds": [], "createdAt": 1, "updatedAt": 2,
        }, {
            "id": "memory-archived", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "workflow",
            "status": "archived", "content": "archived entry",
            "source": "manual", "relatedIds": [],
            "createdAt": 1, "updatedAt": 2,
        }, {
            "id": "memory-large", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "instruction",
            "status": "active", "content": "x" * 50_000,
            "source": "manual", "relatedIds": [],
            "createdAt": 1, "updatedAt": 2,
        }], ensure_ascii=False)
    else:
        settings["memory_entries_v1"] = json.dumps([{
            "id": "memory-instruction",
            "scope": "assistant",
            "assistantId": KASSISTANT,
            "type": "instruction",
            "status": "active",
            "content": "cuplivo instruction",
            "source": "manual",
            "relatedIds": [],
            "createdAt": 1_700_000_000_000_000,
            "updatedAt": 1_700_000_001_000_000,
        }], ensure_ascii=False)
    if variant == "memory-edge":
        settings["memory_entries_v1"] = json.dumps([{
            "id": "memory-blank", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "instruction",
            "status": "active", "content": "   ", "source": "manual",
            "relatedIds": [], "createdAt": 1, "updatedAt": 2,
        }, {
            "id": "memory-archived", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "workflow",
            "status": "archived", "content": "archived entry",
            "source": "manual", "relatedIds": [],
            "createdAt": 1, "updatedAt": 2,
        }, {
            "id": "memory-large", "scope": "assistant",
            "assistantId": KASSISTANT, "type": "instruction",
            "status": "active", "content": "x" * 50_000,
            "source": "manual", "relatedIds": [],
            "createdAt": 1, "updatedAt": 2,
        }], ensure_ascii=False)
    settings["workspaces_v1"] = json.dumps([{
        "id": "workspace-1",
        "name": "Fixture Workspace",
        "kind": "managed",
        "hostPath": None,
        "shellNeedsApproval": True,
        "defaultCwd": "",
        "disabledTools": [],
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:01.000Z",
        "lastUsedAt": None,
    }], ensure_ascii=False)
    settings["skills_v1"] = json.dumps([{
        "id": "fixture-skill",
        "enabled": True,
        "useCount": 0,
        "source": "file",
        "installedAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:01.000Z",
    }], ensure_ascii=False)
    bool_keys = {
        "title_generation_enabled_v1": True,
        "suggestion_generation_enabled_v1": True,
        "ocr_enabled_v1": True,
    }
    for key, value in bool_keys.items():
        settings.setdefault(key, value)
    return settings


def _kelivo_messages(*, group: bool = False) -> list[dict[str, Any]]:
    def message(message_id: str, role: str, text: str, order: int,
                group_id: str, version: int, **extra: Any) -> dict[str, Any]:
        base = {
            "id": message_id,
            "conversation_id": None,
            "role": role,
            "timestamp": 1_700_000_000_000_000 + order,
            "model_id": None,
            "provider_id": None,
            "total_tokens": None,
            "is_streaming": 0,
            "reasoning_start_at": None,
            "reasoning_finished_at": None,
            "translation": None,
            "reasoning_segments_json": None,
            "group_id": group_id,
            "version": version,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cached_tokens": None,
            "duration_ms": None,
            "message_order": order,
            "updated_at": None,
            "sender_id": None,
            "extras_json": "{}",
            "_parts": [("text", text)],
        }
        base.update(extra)
        return base
    messages = [
        message(KMSG_USER1, "user", "question", 0, "g-user-1", 0),
        message(KMSG_A0, "assistant", "answer-v0", 1, KGROUP1, 0),
        message(KMSG_A2, "assistant", ANSWER_V2, 2, KGROUP1, 2,
                model_id="gpt-x", provider_id=KPROVIDER, total_tokens=7,
                prompt_tokens=3, completion_tokens=4, cached_tokens=1,
                extras_json=json.dumps({"tokens.finish": {
                    "promptTokens": 1, "completionTokens": 2,
                    "cachedTokens": 0, "totalTokens": 3}}),
                _parts=[("reasoning", "thinking"), ("text", ANSWER_V2),
                        ("tool_call", json.dumps({
                            "id": "call-1", "name": "search",
                            "arguments": {"q": "x"},
                            "content": "tool-result"}))]),
        message(KMSG_A5, "assistant", "answer-v5", 3, KGROUP1, 5),
        message(KMSG_USER2, "user", "second question", 4, "g-user-2", 0),
        message(KMSG_A2B, "assistant", "second answer", 5, "g-a2b", 0,
                _parts=[("text", "second answer"), ("file", json.dumps({
                    "uri": "kelivo-file:///upload/note.txt",
                    "name": "note.txt", "mime": "text/plain"}))]),
    ]
    if group:
        messages[2]["subgroup_id"] = "sub-1"
        messages[2]["sender_id"] = KASSISTANT
    return messages


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _insert_row(con: sqlite3.Connection, table: str,
                values: dict[str, Any]) -> None:
    columns = [str(row[1]) for row in
               con.execute(f'PRAGMA table_info("{table}")')]
    used = [column for column in columns if column in values]
    con.execute(
        "INSERT INTO {}({}) VALUES ({})".format(
            f'"{table}"',
            ", ".join(f'"{column}"' for column in used),
            ", ".join("?" for _ in used)),
        [values[column] for column in used])


def _fill_kelivo(con: sqlite3.Connection, app: str, *, rich: bool,
                 variant: str | None) -> None:
    group = variant == "group"
    now = 1_700_000_000_000_000
    conversations = [
        (KCONV1, "Conversation one", now, now + 10, 0, KASSISTANT,
         {"truncate": 1, "truncate-empty": 2}.get(variant, -1),
         json.dumps({KGROUP1: 2}), None, 0, "[]", None, -1,
         KPROVIDER, "gpt-x",
         json.dumps({"workspace.id": "workspace-1",
                     "skills.ids": ["fixture-skill"],
                     "prompt.system": "conversation prompt"})
         if rich else "{}"),
        (KCONV2, "Conversation two", now + 20, now + 30, 1, KASSISTANT, -1,
         "{}", None, 0, "[]", None, -1, None, None, "{}"),
    ]
    conversation_columns = (
        "id", "title", "created_at", "updated_at", "is_pinned",
        "assistant_id", "truncate_index", "version_selections_json", "summary",
        "last_summarized_message_count", "chat_suggestions_json",
        "injected_memory_hash", "last_memory_extracted_order",
        "chat_model_provider", "chat_model_id", "extras_json",
    )
    for conversation in conversations:
        _insert_row(con, "conversation_rows",
                    dict(zip(conversation_columns, conversation)))
    for message in _kelivo_messages(group=group):
        parts = message.pop("_parts")
        conversation_id = KCONV1 if message["id"] in (
            KMSG_USER1, KMSG_A0, KMSG_A2, KMSG_A5) else KCONV2
        values = dict(message, conversation_id=conversation_id)
        _insert_row(con, "message_rows", values)
        for ordinal, (kind, payload) in enumerate(parts):
            con.execute(
                "INSERT INTO message_part_rows (conversation_id,revision_id,"
                "ordinal,kind,payload,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (conversation_id, message["id"], ordinal, kind, payload,
                 message["timestamp"], message["timestamp"]))
    con.execute(
        "INSERT INTO provider_artifact_rows (conversation_id,revision_id,kind,"
        "payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (KCONV1, KMSG_A2, "gemini_thought_signature", "fixture-signature",
         now, now))
    con.execute(
        "INSERT INTO provider_artifact_rows (conversation_id,revision_id,kind,"
        "payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (KCONV1, KMSG_A2, "ocr_result", "fixture-ocr", now, now))
    if variant == "unknown-part":
        con.execute(
            "INSERT INTO message_part_rows (conversation_id,revision_id,"
            "ordinal,kind,payload,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (KCONV1, KMSG_A2, 10, "fixture_unknown", "opaque", now, now))
    if rich:
        con.execute(
            "INSERT INTO assistant_rows (id,sort_order,payload,updated_at)"
            " VALUES (?,?,?,?)",
            (KASSISTANT, 0, json.dumps(
                {"id": KASSISTANT, "name": "Fixture Assistant",
                 "chatModelProvider": KPROVIDER, "chatModelId": "gpt-x",
                 "enableMemory": True}, ensure_ascii=False), now))
        provider = {
            "id": KPROVIDER, "enabled": True, "name": "Fixture Provider",
            "apiKey": "fixture-key",
            "baseUrl": "https://api.example.com/v1",
            "providerType": "openai", "models": ["gpt-x"],
            "modelOverrides": {"gpt-x": {"name": "Fixture Model",
                                        "type": "chat", "input": ["text"],
                                        "output": ["text"],
                                        "abilities": ["tool"]}},
            "customHeaders": [], "customBody": [],
        }
        con.execute(
            "INSERT INTO provider_rows (provider_key,sort_order,payload,updated_at)"
            " VALUES (?,?,?,?)",
            (KPROVIDER, 0, json.dumps(provider, ensure_ascii=False), now))
        if _has_table(con, "memory_entry_rows"):
            con.execute(
            "INSERT INTO memory_entry_rows (id,sort_order,scope,assistant_id,"
            "type,status,content,content_normalized,entry_created_at,"
            "entry_updated_at,payload,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("memory-1", 0, "assistant", KASSISTANT, "identity", "active",
             "assistant identity", "assistant identity", now, now + 1000,
             json.dumps({"id": "memory-1", "scope": "assistant",
                         "assistantId": KASSISTANT, "type": "identity",
                         "status": "active", "content": "assistant identity",
                         "source": "manual", "relatedIds": [],
                         "createdAt": now, "updatedAt": now + 1000}),
             now))
        if _has_table(con, "extension_entity_rows"):
            con.execute(
            "INSERT INTO extension_entity_rows (kind,id,sort_order,owner_id,"
            "payload,updated_at) VALUES (?,?,?,?,?,?)",
            ("workspace", "workspace-1", 0, None, json.dumps({
                "id": "workspace-1", "name": "Fixture Workspace",
                "kind": "managed", "hostPath": None,
                "shellNeedsApproval": True, "defaultCwd": "",
                "disabledTools": [],
                "createdAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-01-01T00:00:01.000Z",
                "lastUsedAt": None}), now))
            con.execute(
            "INSERT INTO extension_entity_rows (kind,id,sort_order,owner_id,"
            "payload,updated_at) VALUES (?,?,?,?,?,?)",
            ("skill", "fixture-skill", 0, None, json.dumps({
                "id": "fixture-skill", "enabled": True, "useCount": 0,
                "source": "file",
                "installedAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-01-01T00:00:01.000Z"}), now))
        con.execute(
            "INSERT INTO conversation_mcp_server_rows (conversation_id,"
            "server_id,ordinal) VALUES (?,?,?)", (KCONV1, "mcp-1", 0))
    con.execute(
        "INSERT INTO chat_storage_meta_rows (key,value) VALUES (?,?)",
        ("hive_migration_complete_v1", "true"))
    con.execute(
        "INSERT INTO chat_storage_meta_rows (key,value) VALUES (?,?)",
        ("business_migration_complete_v1", "true"))


def _make_native(path: Path, *, app: str, version: int, schema_version: int,
                 settings_only: bool, rich: bool, variant: str | None) -> Path:
    root = path.parent / f".{path.stem}-native"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    settings = _kelivo_settings(app, rich=rich, variant=variant)
    assets = {
        "upload/note.txt": b"fixture-note",
        "upload/pic.png": b"fixture-image",
        "skills/fixture-skill/SKILL.md": b"# Fixture skill\n",
        "workspaces/workspace-1/files/notes.txt": b"workspace note",
    }
    entries: dict[str, tuple[bytes, str]] = {}
    db = root / "kelivo.db"
    include_chats = not settings_only
    if include_chats:
        schema.create_drift_database(db, schema_version)
        con = sqlite3.connect(db)
        try:
            _fill_kelivo(con, app, rich=rich, variant=variant)
            con.commit()
        finally:
            con.close()
    settings_bytes = json.dumps(settings, ensure_ascii=False,
                                separators=(",", ":")).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", settings_bytes)
        entries["settings.json"] = (settings_bytes,
                                    hashlib.sha256(settings_bytes).hexdigest())
        if include_chats:
            data = db.read_bytes()
            zf.writestr("database/kelivo.db", data)
            entries["database/kelivo.db"] = (
                data, hashlib.sha256(data).hexdigest())
        for entry, data in assets.items():
            zf.writestr(entry, data)
            entries[entry] = (data, hashlib.sha256(data).hexdigest())
    if root.exists():
        shutil.rmtree(root)
    scope = {category: True for category in model.SCOPE_CATEGORIES}
    manifest: dict[str, Any] = {
        "format": "kelivo-backup",
        "formatVersion": version,
        "payloadKind": "sqlite" if include_chats else "settings-only",
        "createdAtUtc": "2026-01-01T00:00:00.000000Z",
        "appVersion": "1.3.1+80" if app == "kelivo" else "4.3.0+6",
        "includeChats": include_chats,
        "includeFiles": True,
        "secretsIncluded": True,
        "scope": scope,
    }
    if version >= 3:
        manifest["minimumReadableFormatVersion"] = 3
    if include_chats:
        manifest["database"] = {
            "entry": "database/kelivo.db",
            "schemaVersion": schema_version,
            "minimumReadableSchemaVersion": 2,
            "conversationCount": 2,
            "messageCount": 6,
        }
    manifest["entries"] = {
        name: {"bytes": len(data), "sha256": digest}
        for name, (data, digest) in entries.items()
    }
    manifest["businessEntityRowIds"] = {
        "assistants_v1": [KASSISTANT],
        "mcp_servers_v1": ["mcp-1"],
        "quick_phrases_v1": ["phrase-1"],
        "assistant_tags_v1": ["tag-1"],
        "memory_entries_v1": ["memory-1"] if rich else [],
        "workspaces_v1": ["workspace-1"],
        "skills_v1": ["fixture-skill"],
    }
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return path


# ---------------------------------------------------------------------------
# Historical chats.json fixture
# ---------------------------------------------------------------------------

def _legacy_conversations(app: str, version: int, *, rich: bool,
                          variant: str | None) -> list[dict[str, Any]]:
    group = variant == "group"
    selection = 2 if version == 2 else 1
    first = {
        "id": KCONV1,
        "title": "Conversation one",
        "createdAt": "2026-01-01T10:00:00.000",
        "updatedAt": "2026-01-01T10:00:10.000",
        "messageIds": [KMSG_USER1, KMSG_A0, KMSG_A2, KMSG_A5],
        "isPinned": False,
        "mcpServerIds": ["mcp-1"] if rich else [],
        "assistantId": KASSISTANT,
        "truncateIndex": 1 if variant == "truncate" else -1,
        "versionSelections": {KGROUP1: selection},
        "summary": None,
        "lastSummarizedMessageCount": 0,
        "chatSuggestions": [],
        "conversationKind": "group" if group else "normal",
        "extras": {"workspace.id": "workspace-1"} if rich else {},
    }
    second = {
        "id": KCONV2,
        "title": "Conversation two",
        "createdAt": "2026-01-01T11:00:00.000",
        "updatedAt": "2026-01-01T11:00:10.000",
        "messageIds": [KMSG_USER2, KMSG_A2B],
        "isPinned": True,
        "mcpServerIds": [],
        "assistantId": KASSISTANT,
        "truncateIndex": -1,
        "versionSelections": {},
        "summary": None,
        "lastSummarizedMessageCount": 0,
        "chatSuggestions": [],
        "conversationKind": "normal",
        "extras": {},
    }
    return [first, second]


def _legacy_messages(*, group: bool) -> list[dict[str, Any]]:
    def message(message_id: str, role: str, content: str, group_id: str,
                version: int, **extra: Any) -> dict[str, Any]:
        base = {
            "id": message_id,
            "role": role,
            "content": content,
            "timestamp": "2026-01-01T10:00:00.000",
            "modelId": "gpt-x",
            "providerId": KPROVIDER,
            "totalTokens": None,
            "conversationId": KCONV1,
            "isStreaming": False,
            "reasoningText": None,
            "reasoningStartAt": None,
            "reasoningFinishedAt": None,
            "translation": None,
            "reasoningSegmentsJson": None,
            "groupId": group_id,
            "subgroupId": None,
            "version": version,
            "promptTokens": None,
            "completionTokens": None,
            "cachedTokens": None,
            "durationMs": None,
            "isPreset": False,
            "speakerAssistantId": None,
        }
        base.update(extra)
        return base
    messages = [
        message(KMSG_USER1, "user", "question", "g-user-1", 0),
        message(KMSG_A0, "assistant", "answer-v0", KGROUP1, 0),
        message(KMSG_A2, "assistant", ANSWER_V2, KGROUP1, 2,
                reasoningText="thinking",
                reasoningStartAt="2026-01-01T10:00:02.000",
                reasoningFinishedAt="2026-01-01T10:00:03.000"),
        message(KMSG_A5, "assistant", "answer-v5", KGROUP1, 5),
        message(KMSG_USER2, "user", "second question", "g-user-2", 0,
                conversationId=KCONV2),
        message(KMSG_A2B, "assistant",
                "second answer\n[image:/upload/pic.png]", "g-a2b", 0,
                conversationId=KCONV2),
    ]
    if group:
        messages[2]["subgroupId"] = "sub-1"
        messages[2]["speakerAssistantId"] = KASSISTANT
        messages[2]["isPreset"] = False
    return messages


def _make_json(path: Path, *, app: str, version: int, settings_only: bool,
               rich: bool, variant: str | None) -> Path:
    settings = _kelivo_settings(app, rich=rich)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", json.dumps(
            settings, ensure_ascii=False, indent=1))
        if not settings_only:
            group = variant == "group"
            conversations = _legacy_conversations(
                app, version, rich=rich, variant=variant)
            messages = _legacy_messages(group=group)
            document = {
                "version": version,
                "conversations": conversations,
                "messages": messages,
                "toolEvents": {
                    KMSG_A2: [{"id": "call-1", "name": "search",
                               "arguments": {"q": "x"},
                               "content": "tool-result"}],
                } if rich else {},
                "geminiThoughtSigs": {
                    KMSG_A2: "fixture-signature"} if rich else {},
                "groupChats": [{
                    "id": "group-1",
                    "conversationId": KCONV1,
                    "title": "Fixture Group",
                    "createdAt": "2026-01-01T10:00:00.000",
                    "updatedAt": "2026-01-01T10:00:10.000",
                }] if group else [],
                "groupMembers": [{
                    "groupChatId": "group-1",
                    "memberKey": "user",
                    "assistantId": None,
                    "sortOrder": 0,
                }, {
                    "groupChatId": "group-1",
                    "memberKey": KASSISTANT,
                    "assistantId": KASSISTANT,
                    "sortOrder": 1,
                }] if group else [],
            }
            zf.writestr("chats.json", json.dumps(
                document, ensure_ascii=False, indent=1))
            zf.writestr("upload/pic.png", b"fixture-image")
            zf.writestr("upload/note.txt", b"fixture-note")
    return path


def _make_jsonl(path: Path, *, app: str, version: int, settings_only: bool,
                rich: bool, variant: str | None) -> Path:
    settings = _kelivo_settings(app, rich=rich)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", json.dumps(
            settings, ensure_ascii=False, indent=1))
        if not settings_only:
            conversations = _legacy_conversations(
                app, 2, rich=rich, variant=variant)
            messages = _legacy_messages(group=variant == "group")
            conversation_lines = [
                json.dumps({"conversation": conversation}, ensure_ascii=False)
                for conversation in conversations
            ]
            message_lines = []
            for message in messages:
                record: dict[str, Any] = {"message": message}
                if message["id"] == KMSG_A2 and rich:
                    record["toolEvents"] = [{
                        "id": "call-1", "name": "search",
                        "arguments": {"q": "x"}, "content": "tool-result"}]
                    record["geminiSignature"] = "fixture-signature"
                message_lines.append(json.dumps(record, ensure_ascii=False))
            meta = {
                "format_version": version,
                "tables": ["conversations", "messages"],
                "conversation_count": len(conversation_lines),
                "message_count": len(message_lines),
                "zip_tables": {
                    "conversations": "conversations.jsonl",
                    "messages": "messages.jsonl",
                },
                "groupChats": [{"id": "group-1", "conversationId": KCONV1}]
                if variant == "group" else [],
                "groupMembers": [] if variant != "group" else [{
                    "groupChatId": "group-1", "memberKey": KASSISTANT,
                    "assistantId": KASSISTANT, "sortOrder": 0}],
            }
            zf.writestr("chats_meta.json", json.dumps(meta, ensure_ascii=False))
            zf.writestr("conversations.jsonl", "\n".join(conversation_lines))
            zf.writestr("messages.jsonl", "\n".join(message_lines))
            zf.writestr("upload/pic.png", b"fixture-image")
    return path


# ---------------------------------------------------------------------------
# Independent observables
# ---------------------------------------------------------------------------

def read_observables(path: Path) -> dict[str, Any]:
    """Open a produced backup without the converter's readers."""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "rikka_hub.db" in names:
            return _observe_rh(path, zf)
        if "manifest.json" in names:
            return _observe_native(path, zf)
        if "chats_meta.json" in names:
            return _observe_jsonl(path, zf)
        if "chats.json" in names:
            return _observe_json(path, zf)
        return {
            "kind": "settings-only",
            "database_present": False,
            "conversations": 0,
            "messages": 0,
            "selected_text": None,
            "manifest_ok": None,
            "settings_present": "settings.json" in names,
        }


def _text_of_parts(parts: Any) -> str | None:
    if not isinstance(parts, list):
        return None
    texts = [part.get("text") for part in parts
             if isinstance(part, dict) and part.get("type") == "text"]
    return "\n".join(str(text) for text in texts if text is not None)


def _observe_rh(path: Path, zf: zipfile.ZipFile) -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        db_path.write_bytes(zf.read("rikka_hub.db"))
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
            conversations = con.execute(
                "SELECT * FROM ConversationEntity ORDER BY create_at, id"
            ).fetchall()
            conversation_count = len(conversations)
            message_count = 0
            selected_text = None
            node_ok = True
            for conversation in conversations:
                nodes = con.execute(
                    "SELECT * FROM message_node WHERE conversation_id = ? "
                    "ORDER BY node_index", (conversation["id"],)).fetchall()
                for node in nodes:
                    messages = json.loads(node["messages"])
                    message_count += len(messages)
                    if not 0 <= node["select_index"] < len(messages):
                        node_ok = False
                    elif selected_text is None and len(messages) > 1:
                        selected_text = _text_of_parts(
                            messages[node["select_index"]].get("parts"))
            fk_ok = not con.execute("PRAGMA foreign_key_check").fetchall()
            fts = con.execute(
                "SELECT name FROM sqlite_master WHERE name = 'message_fts'"
            ).fetchone()
            managed = con.execute(
                "SELECT relative_path FROM managed_files ORDER BY relative_path"
            ).fetchall()
            memory_paths = [
                row[0] for row in con.execute(
                    "SELECT path FROM memory_file ORDER BY assistant_id, path")
            ] if user_version >= 28 else []
            usage_sources = [
                row[0] for row in con.execute(
                    "SELECT source FROM usage_record ORDER BY created_at")
            ] if user_version >= 31 else []
        finally:
            con.close()
    return {
        "kind": "rh-sqlite",
        "database_present": True,
        "schema_version": user_version,
        "conversations": conversation_count,
        "messages": message_count,
        "selected_text": selected_text,
        "node_selection_ok": node_ok,
        "foreign_keys_ok": fk_ok,
        "fts_present": fts is not None,
        "managed_files": [row[0] for row in managed],
        "memory_paths": memory_paths,
        "usage_sources": usage_sources,
        "settings_present": "settings.json" in zf.namelist(),
        "manifest_ok": None,
    }


def _observe_native(path: Path, zf: zipfile.ZipFile) -> dict[str, Any]:
    import tempfile

    manifest = json.loads(zf.read("manifest.json"))
    entries = manifest.get("entries", {})
    hashes_ok = True
    declared = set(entries)
    for name, meta in entries.items():
        if name not in zf.namelist():
            hashes_ok = False
            continue
        data = zf.read(name)
        if len(data) != meta.get("bytes"):
            hashes_ok = False
        if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
            hashes_ok = False
    extras = set(zf.namelist()) - declared - {"manifest.json"}
    result: dict[str, Any] = {
        "kind": "kelivo-archive",
        "format_version": manifest.get("formatVersion"),
        "payload_kind": manifest.get("payloadKind"),
        "database_present": "database/kelivo.db" in zf.namelist(),
        "manifest_ok": hashes_ok and not extras,
        "entries": sorted(declared),
        "scope": manifest.get("scope"),
        "hashes_ok": hashes_ok,
        "settings_present": "settings.json" in zf.namelist(),
    }
    if result["database_present"]:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "k.db"
            db_path.write_bytes(zf.read("database/kelivo.db"))
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            try:
                user_version = con.execute("PRAGMA user_version").fetchone()[0]
                conversation_count = con.execute(
                    "SELECT COUNT(*) FROM conversation_rows").fetchone()[0]
                message_count = con.execute(
                    "SELECT COUNT(*) FROM message_rows").fetchone()[0]
                selected_text = None
                conversations = con.execute(
                    "SELECT * FROM conversation_rows ORDER BY created_at, id"
                ).fetchall()
                for conversation in conversations:
                    selections = json.loads(
                        conversation["version_selections_json"] or "{}")
                    revision_groups = [
                        group_id for group_id, _value in selections.items()
                        if con.execute(
                            "SELECT COUNT(*) FROM message_rows WHERE "
                            "conversation_id = ? AND COALESCE(group_id, id) = ?",
                            (conversation["id"], group_id)).fetchone()[0] > 1]
                    ordered_groups = revision_groups + [
                        group_id for group_id in selections
                        if group_id not in revision_groups]
                    for group_id in ordered_groups:
                        version = selections[group_id]
                        row = con.execute(
                            "SELECT payload FROM message_part_rows WHERE "
                            "revision_id IN (SELECT id FROM message_rows WHERE "
                            "conversation_id = ? AND COALESCE(group_id, id) = ? "
                            "AND version = ?) AND kind = 'text' "
                            "ORDER BY ordinal LIMIT 1",
                            (conversation["id"], group_id, version)).fetchone()
                        if row is not None:
                            selected_text = row[0]
                            break
                    if selected_text is None:
                        row = con.execute(
                            "SELECT payload FROM message_part_rows WHERE "
                            "revision_id IN (SELECT id FROM message_rows WHERE "
                            "conversation_id = ? ORDER BY message_order LIMIT 1) "
                            "AND kind = 'text' ORDER BY ordinal LIMIT 1",
                            (conversation["id"],)).fetchone()
                        if row is not None:
                            selected_text = row[0]
                    if selected_text is not None:
                        break
                declared_counts = manifest.get("database", {})
                counts_match = (
                    declared_counts.get("conversationCount") == conversation_count
                    and declared_counts.get("messageCount") == message_count)
                fk_ok = not con.execute("PRAGMA foreign_key_check").fetchall()
                memory = con.execute(
                    "SELECT COUNT(*) FROM memory_entry_rows").fetchone()[0]
                workspaces = con.execute(
                    "SELECT COUNT(*) FROM extension_entity_rows "
                    "WHERE kind = 'workspace'").fetchone()[0]
            finally:
                con.close()
            result.update({
                "schema_version": user_version,
                "conversations": conversation_count,
                "messages": message_count,
                "selected_text": selected_text,
                "counts_match": counts_match,
                "foreign_keys_ok": fk_ok,
                "memory_entries": memory,
                "workspaces": workspaces,
            })
    else:
        result.update({"conversations": 0, "messages": 0,
                       "selected_text": None})
    return result


def _observe_json(path: Path, zf: zipfile.ZipFile) -> dict[str, Any]:
    document = json.loads(zf.read("chats.json"))
    version = document.get("version")
    conversations = document.get("conversations") or []
    messages = {message["id"]: message for message in
                document.get("messages") or []}
    selected_text = None
    for conversation in conversations:
        selections = conversation.get("versionSelections") or {}
        for group_id, value in selections.items():
            members = sorted(
                (message for message in messages.values()
                 if message["conversationId"] == conversation["id"]
                 and (message.get("groupId") or message["id"]) == group_id),
                key=lambda item: (item.get("version", 0), item["timestamp"]))
            if not members:
                continue
            if version == 2:
                chosen = next((item for item in members
                               if item.get("version", 0) == value), None)
            else:
                chosen = members[value] if 0 <= value < len(members) else None
            if chosen is not None:
                selected_text = _json_message_text(chosen)
                break
        if selected_text is not None:
            break
    return {
        "kind": "kelivo-json",
        "format_version": version,
        "database_present": False,
        "conversations": len(conversations),
        "messages": len(messages),
        "selected_text": selected_text,
        "manifest_ok": None,
        "settings_present": "settings.json" in zf.namelist(),
    }


def _json_message_text(message: dict[str, Any]) -> str | None:
    parts = message.get("parts")
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                texts.append(str(part.get("payload") or ""))
        if texts:
            return "\n".join(texts)
    content = message.get("content")
    return str(content) if isinstance(content, str) else None


def _observe_jsonl(path: Path, zf: zipfile.ZipFile) -> dict[str, Any]:
    meta = json.loads(zf.read("chats_meta.json"))
    conversations = [json.loads(line)["conversation"] for line in
                     zf.read("conversations.jsonl").decode().splitlines()
                     if line.strip()]
    message_records = [json.loads(line) for line in
                       zf.read("messages.jsonl").decode().splitlines()
                       if line.strip()]
    messages = {record["message"]["id"]: record["message"]
                for record in message_records}
    selected_text = None
    for conversation in conversations:
        selections = conversation.get("versionSelections") or {}
        for group_id, value in selections.items():
            chosen = next((
                message for message in messages.values()
                if message["conversationId"] == conversation["id"]
                and (message.get("groupId") or message["id"]) == group_id
                and message.get("version", 0) == value), None)
            if chosen is not None:
                selected_text = _json_message_text(chosen)
                break
        if selected_text is not None:
            break
    return {
        "kind": "cuplivo-jsonl",
        "format_version": meta.get("format_version"),
        "database_present": False,
        "conversations": len(conversations),
        "messages": len(messages),
        "selected_text": selected_text,
        "manifest_ok": (meta.get("conversation_count") == len(conversations)
                        and meta.get("message_count") == len(messages)),
        "settings_present": "settings.json" in zf.namelist(),
    }
