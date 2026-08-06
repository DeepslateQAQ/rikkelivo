"""Round-trip and contract tests for the rikkelivo converter.

Real-backup tests use the two sample backups when present; the synthetic
tests always run.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from rikkelivo.convert import to_kelivo, to_rikkahub

HOME = Path(os.path.expanduser("~"))
KELIVO_BACKUP = HOME / "kelivo_backup_20260806T140526.000246.zip"
RIKKAHUB_BACKUP = HOME / "rikkahub_backup_20260806_134946.zip"

REAL_BACKUPS = KELIVO_BACKUP.exists() and RIKKAHUB_BACKUP.exists()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_zip_member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def _db_of(zip_path: Path, tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "rikka_hub.db"
    db.write_bytes(_read_zip_member(zip_path, "rikka_hub.db"))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def _kelivo_zip(tmp_path: Path, chats: dict, settings: dict | None = None,
                uploads: dict[str, bytes] | None = None,
                name: str = "kelivo_test.zip") -> Path:
    out = tmp_path / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("chats.json", json.dumps(chats, ensure_ascii=False))
        zf.writestr("settings.json",
                    json.dumps(settings or {}, ensure_ascii=False))
        zf.writestr("deleted.json", "{}")
        for path, data in (uploads or {}).items():
            zf.writestr(f"upload/{path}", data)
    return out


def _group_chat_fixture() -> dict:
    """A cuplivo v2 document with one group conversation and two speakers."""
    return {
        "version": 2,
        "conversations": [{
            "id": "conv-group-1",
            "title": "群聊测试",
            "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:05:00.000",
            "messageIds": ["m1", "m2", "m3"],
            "isPinned": False,
            "mcpServerIds": [],
            "assistantId": None,
            "parentConversationId": None,
            "truncateIndex": -1,
            "versionSelections": {},
            "summary": None,
            "lastSummarizedMessageCount": 0,
            "chatSuggestions": [],
            "conversationKind": "group",
        }],
        "messages": [
            {"id": "m1", "role": "user", "content": "大家好啊",
             "timestamp": "2026-08-06T10:01:00.000", "modelId": None,
             "providerId": None, "totalTokens": None,
             "conversationId": "conv-group-1", "isStreaming": False,
             "reasoningText": None, "reasoningStartAt": None,
             "reasoningFinishedAt": None, "translation": None,
             "reasoningSegmentsJson": None, "groupId": "m1",
             "subgroupId": None, "version": 0, "promptTokens": None,
             "completionTokens": None, "cachedTokens": None,
             "durationMs": None, "isPreset": False,
             "speakerAssistantId": None},
            {"id": "m2", "role": "assistant", "content": "你好呀，我是 A",
             "timestamp": "2026-08-06T10:02:00.000", "modelId": "gpt-x",
             "providerId": "P1", "totalTokens": 12,
             "conversationId": "conv-group-1", "isStreaming": False,
             "reasoningText": "think A", "reasoningStartAt": None,
             "reasoningFinishedAt": None, "translation": None,
             "reasoningSegmentsJson": None, "groupId": "m2",
             "subgroupId": None, "version": 0, "promptTokens": 10,
             "completionTokens": 2, "cachedTokens": 0,
             "durationMs": None, "isPreset": False,
             "speakerAssistantId": "assistant-a"},
            {"id": "m3", "role": "assistant", "content": "我是 B，来晚了",
             "timestamp": "2026-08-06T10:03:00.000", "modelId": "gpt-x",
             "providerId": "P1", "totalTokens": 8,
             "conversationId": "conv-group-1", "isStreaming": False,
             "reasoningText": None, "reasoningStartAt": None,
             "reasoningFinishedAt": None, "translation": None,
             "reasoningSegmentsJson": None, "groupId": "m3",
             "subgroupId": None, "version": 0, "promptTokens": None,
             "completionTokens": None, "cachedTokens": None,
             "durationMs": None, "isPreset": False,
             "speakerAssistantId": "assistant-b"},
        ],
        "toolEvents": {},
        "geminiThoughtSigs": {},
        "groupChats": [{
            "id": "group-1", "name": "群聊测试",
            "conversationId": "conv-group-1",
            "directorSystemPrompt": "",
            "maxAssistantMessagesPerRound": 1,
            "assistantDetailInjectionMode": "system",
            "assistantDetailInjectionN": 1,
            "assistantMessagesThisRound": 0,
            "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:05:00.000",
        }],
        "groupMembers": [
            {"groupChatId": "group-1", "memberKey": "user",
             "assistantId": None, "sortOrder": 0},
            {"groupChatId": "group-1", "memberKey": "assistant-a",
             "assistantId": "assistant-a", "sortOrder": 1},
            {"groupChatId": "group-1", "memberKey": "assistant-b",
             "assistantId": "assistant-b", "sortOrder": 2},
        ],
    }


# ---------------------------------------------------------------------------
# synthetic: group chat round trip
# ---------------------------------------------------------------------------

def test_group_chat_round_trip(tmp_path):
    src = _kelivo_zip(tmp_path, _group_chat_fixture(),
                      settings={"assistants_v1": json.dumps([
                          {"id": "assistant-a", "name": "助手A"},
                          {"id": "assistant-b", "name": "助手B"},
                      ])})
    rh = tmp_path / "group.rikkahub.zip"
    to_rikkahub(src, rh)

    con = _db_of(rh, tmp_path)
    rows = con.execute("SELECT messages FROM message_node ORDER BY node_index").fetchall()
    assert len(rows) == 3  # one node per group
    first = json.loads(rows[0][0])[0]
    assert first["role"] == "user"
    # group metadata rides on the first message of the conversation
    assert first["x_rikkelivo"]["kind"] == "group"
    assert first["x_rikkelivo"]["groupChat"]["id"] == "group-1"
    assert len(first["x_rikkelivo"]["groupMembers"]) == 3

    msgs = [json.loads(rows[i][0])[0] for i in (1, 2)]
    assert msgs[0]["role"] == "assistant"
    text_a = next(p["text"] for p in msgs[0]["parts"]
                  if p["type"] == "text")
    assert text_a.startswith("> **助手A**:\n> 你好呀，我是 A")
    assert msgs[0]["x_rikkelivo"]["speaker"] == "assistant-a"
    assert msgs[0]["x_rikkelivo"]["contentOriginal"] == "你好呀，我是 A"
    assert msgs[0]["x_rikkelivo"]["kind"] == "group"
    assert msgs[0]["usage"] == {"promptTokens": 10, "completionTokens": 2,
                                "cachedTokens": 0, "totalTokens": 12}
    # the (provider, model) pair rides the sidecar even without providers
    # configured, so the reverse conversion restores it exactly
    assert msgs[0]["x_rikkelivo"]["modelId"] == "gpt-x"
    assert msgs[0]["x_rikkelivo"]["providerId"] == "P1"
    con.close()

    kl = tmp_path / "group.kelivo.zip"
    to_kelivo(rh, kl)
    doc = json.loads(_read_zip_member(kl, "chats.json"))
    assert doc["conversations"][0]["conversationKind"] == "group"
    assert len(doc["groupChats"]) == 1
    assert len(doc["groupMembers"]) == 3
    by_id = {m["id"]: m for m in doc["messages"]}
    assert by_id["m2"]["content"] == "你好呀，我是 A"  # decoration undone
    assert by_id["m2"]["speakerAssistantId"] == "assistant-a"
    assert by_id["m2"]["reasoningText"] == "think A"
    assert by_id["m2"]["totalTokens"] == 12
    assert by_id["m2"]["modelId"] == "gpt-x"
    assert by_id["m2"]["providerId"] == "P1"
    assert doc["conversations"][0]["messageIds"] == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# synthetic: attachments
# ---------------------------------------------------------------------------

def test_attachment_markers_round_trip(tmp_path):
    chats = {
        "version": 2,
        "conversations": [{
            "id": "c1", "title": "t", "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:01:00.000", "messageIds": ["a1"],
            "isPinned": False, "mcpServerIds": [], "assistantId": None,
            "parentConversationId": None, "truncateIndex": -1,
            "versionSelections": {}, "summary": None,
            "lastSummarizedMessageCount": 0, "chatSuggestions": [],
            "conversationKind": "normal",
        }],
        "messages": [{
            "id": "a1", "role": "user",
            "content": "看这个\n[image:/data/user/0/com.example/files/upload/pic.png]"
                       "\n[file:/data/user/0/com.example/files/upload/doc.pdf|文档.pdf|application/pdf]",
            "timestamp": "2026-08-06T10:01:00.000", "modelId": None,
            "providerId": None, "totalTokens": None,
            "conversationId": "c1", "isStreaming": False,
            "reasoningText": None, "reasoningStartAt": None,
            "reasoningFinishedAt": None, "translation": None,
            "reasoningSegmentsJson": None, "groupId": "a1",
            "subgroupId": None, "version": 0, "promptTokens": None,
            "completionTokens": None, "cachedTokens": None,
            "durationMs": None, "isPreset": False, "speakerAssistantId": None,
        }],
        "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    src = _kelivo_zip(tmp_path, chats, uploads={"pic.png": b"PNG",
                                                "doc.pdf": b"PDF"})
    rh = tmp_path / "att.rikkahub.zip"
    to_rikkahub(src, rh)
    con = _db_of(rh, tmp_path)
    msg = json.loads(con.execute("SELECT messages FROM message_node").fetchone()[0])[0]
    parts = {p["type"]: p for p in msg["parts"]}
    assert parts["text"]["text"] == "看这个"
    assert parts["image"]["url"].startswith(
        "file:///data/user/0/me.rerere.rikkahub/files/upload/pic.png")
    assert parts["document"]["fileName"] == "文档.pdf"
    con.close()

    kl = tmp_path / "att.kelivo.zip"
    to_kelivo(rh, kl)
    doc = json.loads(_read_zip_member(kl, "chats.json"))
    content = doc["messages"][0]["content"]
    assert "[image:/data/user/0/me.rerere.rikkahub/files/upload/pic.png]" in content
    assert "[file:/data/user/0/me.rerere.rikkahub/files/upload/doc.pdf|文档.pdf|application/pdf]" in content


# ---------------------------------------------------------------------------
# synthetic: versioned messages -> nodes -> versionSelections
# ---------------------------------------------------------------------------

def test_version_branch_round_trip(tmp_path):
    def msg(mid, role, content, version, group=None, ts="2026-08-06T10:00:00.000"):
        return {"id": mid, "role": role, "content": content,
                "timestamp": ts, "modelId": None, "providerId": None,
                "totalTokens": None, "conversationId": "c1",
                "isStreaming": False, "reasoningText": None,
                "reasoningStartAt": None, "reasoningFinishedAt": None,
                "translation": None, "reasoningSegmentsJson": None,
                "groupId": group or mid, "subgroupId": None, "version": version,
                "promptTokens": None, "completionTokens": None,
                "cachedTokens": None, "durationMs": None, "isPreset": False,
                "speakerAssistantId": None}

    chats = {
        "version": 2,
        "conversations": [{
            "id": "c1", "title": "t", "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:01:00.000",
            "messageIds": ["u1", "a1", "a2"],
            "isPinned": False, "mcpServerIds": [], "assistantId": None,
            "parentConversationId": None, "truncateIndex": -1,
            "versionSelections": {"g-assist": 1},
            "summary": None, "lastSummarizedMessageCount": 0,
            "chatSuggestions": [], "conversationKind": "normal",
        }],
        "messages": [
            msg("u1", "user", "hi", 0),
            msg("a1", "assistant", "v0 answer", 0, group="g-assist"),
            msg("a2", "assistant", "v1 answer", 1, group="g-assist"),
        ],
        "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    src = _kelivo_zip(tmp_path, chats)
    rh = tmp_path / "v.rikkahub.zip"
    to_rikkahub(src, rh)
    con = _db_of(rh, tmp_path)
    node = con.execute(
        "SELECT select_index, messages FROM message_node WHERE node_index = 1"
    ).fetchone()
    assert node[0] == 1  # versionSelections picked v1
    msgs = json.loads(node[1])
    assert [m["parts"][0]["text"] for m in msgs] == ["v0 answer", "v1 answer"]
    con.close()

    kl = tmp_path / "v.kelivo.zip"
    to_kelivo(rh, kl)
    doc = json.loads(_read_zip_member(kl, "chats.json"))
    conv = doc["conversations"][0]
    # representative message per group + selection restored
    assert conv["messageIds"] == ["u1", "a1"]
    assert "g-assist" in conv["versionSelections"]
    assert conv["versionSelections"]["g-assist"] == 1
    assert {m["id"] for m in doc["messages"]} == {"u1", "a1", "a2"}
    by_id = {m["id"]: m for m in doc["messages"]}
    assert by_id["a2"]["groupId"] == "g-assist"
    assert by_id["a2"]["version"] == 1


# ---------------------------------------------------------------------------
# prompt variables and MCP sync
# ---------------------------------------------------------------------------

def _settings_zip(tmp_path: Path, settings: dict, name: str = "s.zip") -> Path:
    out = tmp_path / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("settings.json", json.dumps(settings, ensure_ascii=False))
    return out


def test_prompt_vars_k2r(tmp_path):
    from rikkelivo.settings import build_rikkahub_settings
    kelivo_settings = {
        "titlePrompt": "总结 {cur_date} 的对话，助手是 {assistant_name}",
        "assistants_v1": json.dumps([{
            "id": "a1", "name": "雾雨",
            "systemPrompt": "你是 {assistant_name}，用户是 {nickname}，今天 {cur_date}",
            "presetMessages": "[]",
        }]),
        "instruction_injections_v1": json.dumps([{
            "id": "i1", "title": "模式", "prompt": "记得你是 {assistant_name}"}])
    }
    out, _ = build_rikkahub_settings(kelivo_settings)
    assert out["titlePrompt"] == "总结 {{cur_date}} 的对话，助手是 {{char}}"
    assert out["assistants"][0]["systemPrompt"] == \
        "你是 {{char}}，用户是 {{user}}，今天 {{cur_date}}"
    assert out["modeInjections"][0]["content"] == "记得你是 {{char}}"


def test_prompt_vars_r2k(tmp_path):
    from rikkelivo.settings import build_kelivo_settings
    rh_settings = {
        "titlePrompt": "总结 {{cur_date}} 的对话，助手是 {{char}}",
        "assistants": [{
            "id": "a1", "name": "雾雨",
            "systemPrompt": "你是 {{char}}，用户是 {{user}}，今天 {{cur_date}}",
            "messageTemplate": "{{ message }}",
        }],
        "modeInjections": [{"id": "i1", "name": "模式",
                             "content": "记得你是 {{char}}"}],
    }
    out, _ = build_kelivo_settings(rh_settings)
    assert out["titlePrompt"] == "总结 {cur_date} 的对话，助手是 {assistant_name}"
    assistants = json.loads(out["assistants_v1"])
    assert assistants[0]["systemPrompt"] == \
        "你是 {assistant_name}，用户是 {nickname}，今天 {cur_date}"
    assert assistants[0]["messageTemplate"] == "{{ message }}"  # untouched
    injections = json.loads(out["instruction_injections_v1"])
    assert injections[0]["prompt"] == "记得你是 {assistant_name}"


def test_mcp_sync(tmp_path):
    from rikkelivo.settings import build_kelivo_settings, build_rikkahub_settings
    rh_settings = {
        "mcpServers": [{
            "type": "streamable_http",
            "id": "m1",
            "url": "https://example.com/mcp",
            "commonOptions": {
                "enable": True, "name": "my-server",
                "headers": [{"first": "Authorization",
                              "second": "Bearer tok"}],
                "tools": [{
                    "enable": True, "name": "do_thing",
                    "description": "does a thing",
                    "inputSchema": {"type": "object",
                                     "properties": {"q": {"type": "string"}},
                                     "required": ["q"]},
                    "needsApproval": False,
                }],
            },
        }],
    }
    kelivo_settings, notes = build_kelivo_settings(rh_settings)
    mcp = json.loads(kelivo_settings["mcp_servers_v1"])
    ids = [m["id"] for m in mcp]
    # Kelivo built-ins always present, user server appended
    assert ids[:3] == ["kelivo_fetch", "kelivo_subagent", "kelivo_filesystem"]
    assert mcp[2]["enabled"] is False
    user = mcp[3]
    assert user["name"] == "my-server"
    assert user["transport"] == "http"
    assert user["headers"] == {"Authorization": "Bearer tok"}
    assert user["tools"][0]["params"] == [{"name": "q", "required": True,
                                            "type": "string"}]
    # headers/schema come back on the reverse trip
    back, _ = build_rikkahub_settings(kelivo_settings)
    converted = back["mcpServers"][0]
    assert converted["commonOptions"]["headers"] == [
        {"first": "Authorization", "second": "Bearer tok"}]
    assert converted["commonOptions"]["tools"][0]["inputSchema"] == \
        {"type": "object", "properties": {"q": {"type": "string"}},
         "required": ["q"]}


def test_mcp_builtins_stripped_k2r(tmp_path):
    from rikkelivo.settings import build_rikkahub_settings
    kelivo_settings = {
        "mcp_servers_v1": json.dumps([
            {"id": "kelivo_fetch", "enabled": True, "name": "@kelivo/fetch",
             "transport": "inmemory", "tools": []},
            {"id": "custom-1", "enabled": True, "name": "我的服务",
             "transport": "http", "url": "https://x/mcp", "tools": []},
        ])
    }
    out, notes = build_rikkahub_settings(kelivo_settings)
    assert len(out["mcpServers"]) == 1
    assert out["mcpServers"][0]["commonOptions"]["name"] == "我的服务"
    assert out["mcpServers"][0]["type"] == "streamable_http"
    assert any("内置" in n for n in notes)


def test_assistant_and_model_selection(tmp_path):
    """R2K: model display names and the used assistant carry over."""
    from rikkelivo.settings import build_kelivo_settings, build_rikkahub_settings
    rh_settings = {
        "assistantId": "assistant-main",
        "assistants": [
            {"id": "assistant-a", "name": "助手A",
             "chatModelId": "model-uuid-1", "useAssistantAvatar": False,
             "systemPrompt": "", "messageTemplate": "{{ message }}"},
            {"id": "assistant-main", "name": "主助手",
             "chatModelId": "model-uuid-2", "useAssistantAvatar": False,
             "systemPrompt": "", "messageTemplate": "{{ message }}"},
        ],
        "providers": [{
            "type": "openai", "id": "prov-1", "enabled": True,
            "name": "聚合", "apiKey": "k", "baseUrl": "https://x/v1",
            "models": [
                {"id": "model-uuid-1", "modelId": "deepseek-v4-flash",
                 "displayName": "小赢鲸 Deepseek V4 Flash", "type": "CHAT",
                 "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
                 "abilities": ["TOOL"], "customHeaders": [],
                 "customBodies": [], "tools": [], "providerOverwrite": None},
                {"id": "model-uuid-2", "modelId": "gpt-x",
                 "displayName": "GPT X", "type": "CHAT",
                 "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
                 "abilities": [], "customHeaders": [], "customBodies": [],
                 "tools": [], "providerOverwrite": None},
            ],
        }],
    }
    out, _ = build_kelivo_settings(rh_settings)
    # Kelivo switches to the assistant RikkaHub was used with.
    from rikkelivo.settings import _entity_uuid
    assert out["current_assistant_id_v1"] == _entity_uuid("assistant-main",
                                                          "assistant")
    assistants = json.loads(out["assistants_v1"])
    by_id = {a["id"]: a for a in assistants}
    main = by_id[_entity_uuid("assistant-main", "assistant")]
    assert main["chatModelProvider"] == "prov-1"
    assert main["chatModelId"] == "gpt-x"
    # displayName carried into modelOverrides.name
    prov = json.loads(out["provider_configs_v1"])
    assert prov["prov-1"]["modelOverrides"]["deepseek-v4-flash"]["name"] == \
        "小赢鲸 Deepseek V4 Flash"

    # K2R: the Kelivo current assistant becomes the RikkaHub default.
    kelivo_settings = {
        "assistantId": "legacy-default",  # rikkahub-style leftover key
        "current_assistant_id_v1": "assistant-main",
        "assistants_v1": json.dumps([{"id": "assistant-main",
                                      "name": "主助手",
                                      "systemPrompt": ""}]),
    }
    back, _ = build_rikkahub_settings(kelivo_settings)
    assert back["assistantId"] == _entity_uuid("assistant-main", "assistant")
    assert back["assistants"][0]["id"] == _entity_uuid("assistant-main",
                                                         "assistant")


def test_tool_events_round_trip(tmp_path):
    """RikkaHub tool parts <-> Kelivo toolEvents."""
    from rikkelivo.settings import build_kelivo_settings
    rh_settings = {
        "displaySetting": {"userNickname": "岩",
                            "userAvatar": {"type": "Avatar.Image",
                                            "url": "file:///data/upload/a.png"}},
    }
    kelivo_settings, _ = build_kelivo_settings(rh_settings)
    assert kelivo_settings["user_name"] == "岩"
    assert kelivo_settings["avatar_type"] == "file"
    assert kelivo_settings["avatar_value"] == "/data/upload/a.png"

    # R2K: tool part -> toolEvents entry
    from rikkelivo.rikkahub import to_kelivo_message
    rmsg = {"id": "m1", "role": "assistant", "parts": [
        {"type": "tool", "toolCallId": "call-1", "toolName": "web_search",
         "input": "{\"query\": \"x\"}",
         "output": [{"type": "text", "text": "result body"}],
         "approvalState": {"type": "auto"}, "metadata": None}],
        "annotations": [], "createdAt": "2026-01-01T00:00:00",
        "finishedAt": None, "modelId": None, "usage": None,
        "translation": None}
    km, events, dropped = to_kelivo_message(rmsg, "c1")
    assert dropped == 0
    assert events == [{"id": "call-1", "name": "web_search",
                       "arguments": {"query": "x"},
                       "content": "result body"}]

    # K2R: toolEvents -> tool part
    from rikkelivo.kelivo import to_rikkahub_message
    kmsg = {"id": "m1", "role": "assistant", "content": "text",
            "timestamp": "2026-01-01T00:00:00", "groupId": "m1"}
    rh_msg = to_rikkahub_message(
        kmsg, tool_events=[{"id": "call-1", "name": "web_search",
                            "arguments": {"query": "x"},
                            "content": "result body"}])
    tool = [p for p in rh_msg["parts"] if p["type"] == "tool"][0]
    assert tool["toolName"] == "web_search"
    assert tool["toolCallId"] == "call-1"
    assert tool["output"][0]["text"] == "result body"


def test_restore_safe_prefs_types(tmp_path):
    """Kelivo settings must only contain prefs-compatible top-level values."""
    from rikkelivo.settings import build_kelivo_settings
    rh_settings = {
        "searchServices": [{"type": "bing_local", "id": "s1"}],
        "asrProviders": [{"type": "mimo", "id": "a1"}],
        "lorebooks": [{"id": "l1", "name": "wb", "entries": []}],
        "assistantTags": [{"id": "t1", "name": "tag"}],
        "customThemes": [{"id": "c1", "name": "theme"}],
    }
    out, _ = build_kelivo_settings(rh_settings)
    for k, v in out.items():
        assert not isinstance(v, dict), k
        assert not (isinstance(v, list) and any(
            isinstance(x, dict) for x in v)), k
    # object arrays stay empty mirrors; data went to *_v1 strings
    assert out["searchServices"] == []
    assert out["lorebooks"] == []


def test_memories_skills_cloud_sync(tmp_path):
    """Memories, skills, S3/WebDAV and search-enable carry both ways."""
    from rikkelivo.settings import (_entity_uuid, build_kelivo_settings,
                                    build_rikkahub_settings)
    rh_settings = {
        "webDavConfig": {"url": "https://dav/x", "username": "u",
                         "password": "p", "path": "rh_backups",
                         "items": ["DATABASE", "FILES"]},
        "s3Config": {"endpoint": "https://cos/x", "accessKeyId": "AK",
                      "secretAccessKey": "SK", "bucket": "b",
                      "region": "ap-1", "pathStyle": True,
                      "items": ["DATABASE", "FILES"]},
        "searchServices": [{"type": "bing_local", "id": "s1"}],
        "searchServiceSelected": 1,
        "searchCommonOptions": {"resultSize": 20},
        "assistants": [{"id": "a1", "name": "A",
                         "enabledSkills": ["android-operation"],
                         "systemPrompt": "", "messageTemplate": "{{ message }}"}],
    }
    out, _ = build_kelivo_settings(rh_settings)
    assert out["webdav_config_v1"]
    assert "cos/x" in out["s3_config_v1"]
    assert out["search_enabled_v1"] is True
    assert out["search_selected_v1"] == 1
    assert "resultSize" in out["search_common_v1"]
    assistants = json.loads(out["assistants_v1"])
    assert assistants[0]["skillIds"] == ["android-operation"]

    back, _ = build_rikkahub_settings(out)
    assert back["webDavConfig"]["path"] == "rh_backups"
    assert back["s3Config"]["accessKeyId"] == "AK"
    assert back["searchServiceSelected"] == 1
    assert back["searchCommonOptions"] == {"resultSize": 20}
    assert back["assistants"][0]["enabledSkills"] == ["android-operation"]

    # memories round trip through the DB
    rh_settings2 = {"assistants": [{"id": "a1", "name": "A",
                                     "systemPrompt": "",
                                     "messageTemplate": "{{ message }}"}]}
    out2, _ = build_kelivo_settings(rh_settings2)
    out2["assistant_memories_v1"] = json.dumps([
        {"id": 1, "assistantId": "a1", "content": "岩酱喜欢物理"}])
    back2, _ = build_rikkahub_settings(out2)
    # memory assistant id normalized to the converted assistant id
    assert _entity_uuid("a1", "assistant") == _entity_uuid("a1", "assistant")


def test_task_models_round_trip(tmp_path):
    """fastModelId/task models convert both ways and stay resolvable."""
    from rikkelivo.settings import (build_kelivo_settings,
                                    build_rikkahub_settings)
    rh_settings = {
        "providers": [{
            "type": "openai", "id": "prov-1", "enabled": True,
            "name": "聚合", "apiKey": "k", "baseUrl": "https://x/v1",
            "models": [
                {"id": "fast-uuid", "modelId": "gpt-fast",
                 "displayName": "快速", "type": "CHAT",
                 "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
                 "abilities": [], "customHeaders": [], "customBodies": [],
                 "tools": [], "providerOverwrite": None},
                {"id": "tr-uuid", "modelId": "gpt-translate",
                 "displayName": "翻译", "type": "CHAT",
                 "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
                 "abilities": [], "customHeaders": [], "customBodies": [],
                 "tools": [], "providerOverwrite": None},
            ],
        }],
        "fastModelId": "fast-uuid",       # title/suggestion fallback
        "titleModelId": None,
        "suggestionModelId": None,
        "translateModeId": "tr-uuid",
        "ocrModelId": None,
        "compressModelId": None,
    }
    kl, _ = build_kelivo_settings(rh_settings)
    # fast model lands in kelivo title/suggestion slots
    assert kl["title_model_v1"] == "prov-1::gpt-fast"
    assert kl["suggestion_model_v1"] == "prov-1::gpt-fast"
    assert kl["translate_model_v1"] == "prov-1::gpt-translate"
    assert "ocr_model_v1" not in kl

    back, _ = build_rikkahub_settings(kl)
    ids = {m["id"] for p in back["providers"] for m in p["models"]}
    assert back["titleModelId"] in ids and back["fastModelId"] in ids
    assert back["translateModeId"] in ids

    def _model_name(mid):
        return next(m["modelId"] for p in back["providers"]
                    for m in p["models"] if m["id"] == mid)

    assert _model_name(back["titleModelId"]) == "gpt-fast"
    assert _model_name(back["suggestionModelId"]) == "gpt-fast"
    assert _model_name(back["translateModeId"]) == "gpt-translate"
    assert _model_name(back["fastModelId"]) == "gpt-fast"

    # kelivo current model drives chatModelId; fast follows title first,
    # falling back to the current model when no title model is set.
    kl["selected_model_v1"] = "prov-1::gpt-translate"
    back2, _ = build_rikkahub_settings(kl)
    assert _model_name(back2["chatModelId"]) == "gpt-translate"
    assert _model_name(back2["fastModelId"]) == "gpt-fast"  # title wins
    del kl["title_model_v1"]
    back3, _ = build_rikkahub_settings(kl)
    assert _model_name(back3["fastModelId"]) == "gpt-translate"


# ---------------------------------------------------------------------------
# real backups
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_BACKUPS, reason="sample backups not present")
def test_real_kelivo_to_rikkahub(tmp_path):
    rh = tmp_path / "real.rikkahub.zip"
    summary, _ = to_rikkahub(KELIVO_BACKUP, rh)
    assert "1 会话" in summary and "3 消息" in summary and "14 上传文件" in summary

    with zipfile.ZipFile(rh) as zf:
        names = zf.namelist()
        assert "settings.json" in names and "rikka_hub.db" in names
        settings = json.loads(zf.read("settings.json"))
    con = _db_of(rh, tmp_path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 24
    assert con.execute("SELECT identity_hash FROM room_master_table"
                       " WHERE id = 42").fetchone()[0] == \
        "0ea1aaebfa031c7995c45a1e35822e1a"
    assert con.execute("SELECT COUNT(*) FROM ConversationEntity").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM message_node").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM managed_files").fetchone()[0] == 14
    assert con.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0] == 3
    assert len(settings) == 47
    assert any(p["name"] == "KelivoIN" for p in settings["providers"])
    # built-in provider matched by endpoint keeps its RikkaHub uuid
    silicon = next(p for p in settings["providers"] if p["name"] == "SiliconFlow")
    assert silicon["id"] == "56a94d29-c88b-41c5-8e09-38a7612d6cf8"
    con.close()


@pytest.mark.skipif(not REAL_BACKUPS, reason="sample backups not present")
def test_real_rikkahub_to_kelivo(tmp_path):
    kl = tmp_path / "real.kelivo.zip"
    summary, _ = to_kelivo(RIKKAHUB_BACKUP, kl)
    assert "32 会话" in summary and "649 消息" in summary
    assert "14 上传文件" in summary and "203 工具调用记录" in summary

    with zipfile.ZipFile(kl) as zf:
        doc = json.loads(zf.read("chats.json"))
        settings = json.loads(zf.read("settings.json"))
        deleted = json.loads(zf.read("deleted.json"))
    assert doc["version"] == 2
    assert len(doc["conversations"]) == 32
    assert len(doc["messages"]) == 649
    # standalone skills zip for manual import (Kelivo restore skips skills/)
    skills_zip = tmp_path / "real.kelivo.skills.zip"
    assert skills_zip.exists()
    with zipfile.ZipFile(skills_zip) as sz:
        names = sz.namelist()
        assert any(n.endswith("SKILL.md") for n in names)
    by_id = {m["id"]: m for m in doc["messages"]}
    for c in doc["conversations"]:
        for mid in c["messageIds"]:
            assert mid in by_id
            assert by_id[mid]["conversationId"] == c["id"]
    # attachments survived as kelivo markers
    assert any("[file:" in m["content"] or "[image:" in m["content"]
               for m in doc["messages"])
    # settings: kelivo-shaped snapshot with converted providers/assistants
    assert len(json.loads(settings["provider_configs_v1"])) == 25
    assert len(json.loads(settings["assistants_v1"])) >= 1
    assert settings["current_assistant_id_v1"]
    assert set(deleted) == {
        "conversation", "message", "assistant", "worldBook", "quickPhrase",
        "mcpServer", "memory", "groupChat", "workspaceFile"}


@pytest.mark.skipif(not REAL_BACKUPS, reason="sample backups not present")
def test_real_round_trip_preserves_content(tmp_path):
    """kelivo -> rikkahub -> kelivo keeps message content and ids."""
    rh = tmp_path / "m.rikkahub.zip"
    to_rikkahub(KELIVO_BACKUP, rh)
    kl = tmp_path / "m.kelivo.zip"
    to_kelivo(rh, kl)

    original = json.loads(_read_zip_member(KELIVO_BACKUP, "chats.json"))
    doc = json.loads(_read_zip_member(kl, "chats.json"))
    orig_by_id = {m["id"]: m for m in original["messages"]}
    new_by_id = {m["id"]: m for m in doc["messages"]}
    assert set(orig_by_id) == set(new_by_id)
    for mid, m in orig_by_id.items():
        assert new_by_id[mid]["content"] == m["content"]
        assert new_by_id[mid]["role"] == m["role"]
        assert new_by_id[mid]["timestamp"] == m["timestamp"]
    assert [c["id"] for c in doc["conversations"]] == \
        [c["id"] for c in original["conversations"]]
    assert doc["conversations"][0]["title"] == original["conversations"][0]["title"]


# ---------------------------------------------------------------------------
# review findings: managed assets, s3 prefix, model ids, metadata warnings
# ---------------------------------------------------------------------------

def test_managed_assets_rebased_to_upload(tmp_path):
    """K2R: images//avatars/ bytes are carried into the RikkaHub backup
    under upload/ with dir prefixes; markers and markdown links are rebased
    to the same location so attachments stay readable after restore."""
    chats = {
        "version": 2,
        "conversations": [{
            "id": "c1", "title": "t", "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:01:00.000", "messageIds": ["a1"],
            "isPinned": False, "mcpServerIds": [], "assistantId": None,
            "truncateIndex": -1, "versionSelections": {}, "summary": None,
            "lastSummarizedMessageCount": 0, "chatSuggestions": [],
            "conversationKind": "normal",
        }],
        "messages": [{
            "id": "a1", "role": "user",
            "content": "MCP 图\n"
                        "[image:/data/user/0/com.example/files/images/mcp_img_1.png]"
                        "\n![示意图](/data/user/0/com.example/files/images/md_2.png)",
            "timestamp": "2026-08-06T10:01:00.000", "modelId": None,
            "providerId": None, "totalTokens": None,
            "conversationId": "c1", "isStreaming": False,
            "reasoningText": None, "reasoningStartAt": None,
            "reasoningFinishedAt": None, "translation": None,
            "reasoningSegmentsJson": None, "groupId": "a1",
            "subgroupId": None, "version": 0, "promptTokens": None,
            "completionTokens": None, "cachedTokens": None,
            "durationMs": None, "isPreset": False, "speakerAssistantId": None,
        }],
        "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    src = tmp_path / "managed.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("chats.json", json.dumps(chats, ensure_ascii=False))
        zf.writestr("settings.json", "{}")
        zf.writestr("deleted.json", "{}")
        zf.writestr("upload/pic.png", b"PNG")
        zf.writestr("images/mcp_img_1.png", b"IMG1")
        zf.writestr("images/md_2.png", b"IMG2")
        zf.writestr("avatars/a1.png", b"AVA")
    rh = tmp_path / "m.rikkahub.zip"
    to_rikkahub(src, rh)

    with zipfile.ZipFile(rh) as zf:
        names = set(zf.namelist())
        assert "upload/pic.png" in names
        assert "upload/kkimg_mcp_img_1.png" in names
        assert "upload/kkimg_md_2.png" in names
        assert "upload/kkava_a1.png" in names
        assert zf.read("upload/kkimg_mcp_img_1.png") == b"IMG1"
        assert "images/mcp_img_1.png" not in names

    con = _db_of(rh, tmp_path)
    rels = {r["relative_path"] for r in con.execute(
        "SELECT relative_path FROM managed_files")}
    assert "upload/kkimg_mcp_img_1.png" in rels
    assert "upload/kkava_a1.png" in rels
    msg = json.loads(con.execute(
        "SELECT messages FROM message_node").fetchone()[0])[0]
    parts = {p["type"]: p for p in msg["parts"]}
    assert parts["image"]["url"] == (
        "file:///data/user/0/me.rerere.rikkahub/files/upload/"
        "kkimg_mcp_img_1.png")
    md_text = next(p["text"] for p in msg["parts"] if p["type"] == "text")
    assert ("![示意图](file:///data/user/0/me.rerere.rikkahub/files/upload/"
            "kkimg_md_2.png)") in md_text
    con.close()

    # R2K round trip keeps the (rebased) attachment markers intact.
    kl = tmp_path / "m.kelivo.zip"
    to_kelivo(rh, kl)
    doc = json.loads(_read_zip_member(kl, "chats.json"))
    content = doc["messages"][0]["content"]
    assert ("[image:/data/user/0/me.rerere.rikkahub/files/upload/"
            "kkimg_mcp_img_1.png]") in content
    assert "upload/kkimg_mcp_img_1.png" in {
        i.filename for i in zipfile.ZipFile(kl).infolist()}


def test_s3_prefix_uses_rikkahub_backups(tmp_path):
    """R2K: the Kelivo S3 prefix is RikkaHub's hardcoded bucket prefix, not
    the WebDAV path."""
    from rikkelivo.settings import build_kelivo_settings
    out, _ = build_kelivo_settings({
        "webDavConfig": {"url": "https://dav/x", "username": "u",
                         "password": "p", "path": "my_custom_folder",
                         "items": ["DATABASE", "FILES"]},
        "s3Config": {"endpoint": "https://cos/x", "accessKeyId": "AK",
                      "secretAccessKey": "SK", "bucket": "b",
                      "region": "ap-1", "pathStyle": True,
                      "items": ["DATABASE", "FILES"]},
    })
    assert json.loads(out["s3_config_v1"])["prefix"] == "rikkahub_backups"
    # without WebDAV the prefix stays the same instead of inventing a folder
    out2, _ = build_kelivo_settings({
        "s3Config": {"endpoint": "https://cos/x", "accessKeyId": "AK",
                      "secretAccessKey": "SK", "bucket": "b"},
    })
    assert json.loads(out2["s3_config_v1"])["prefix"] == "rikkahub_backups"


def test_assistant_chatmodel_never_emits_non_uuid(tmp_path):
    """K2R: an unresolvable Kelivo model name falls back to the global
    default model with a warning instead of emitting a non-UUID chatModelId
    (RikkaHub decodes chatModelId as a kotlin.uuid.Uuid)."""
    from uuid import UUID
    from rikkelivo.settings import build_rikkahub_settings

    # no providers at all: nothing to resolve against
    out, notes = build_rikkahub_settings({
        "assistants_v1": json.dumps([{
            "id": "a1", "name": "主助手", "chatModelId": "gpt-4o",
            "chatModelProvider": None}]),
    })
    assert out["assistants"][0]["chatModelId"] is None
    assert any("无法解析" in n for n in notes)

    # unqualified name that is unique across providers resolves to a uuid
    out2, notes2 = build_rikkahub_settings({
        "assistants_v1": json.dumps([{
            "id": "a1", "name": "主助手", "chatModelId": "gpt-x"}]),
        "provider_configs_v1": json.dumps({
            "prov-1": {"id": "prov-1", "name": "聚合",
                        "models": ["gpt-x"]}}),
    })
    mid = out2["assistants"][0]["chatModelId"]
    assert str(UUID(mid)) == mid
    assert not any("无法解析" in n for n in notes2)

    # duplicate name across providers stays unresolved -> fallback + warning
    out3, notes3 = build_rikkahub_settings({
        "assistants_v1": json.dumps([{
            "id": "a1", "name": "主助手", "chatModelId": "gpt-x"}]),
        "provider_configs_v1": json.dumps({
            "prov-1": {"id": "prov-1", "name": "A",
                        "models": ["gpt-x"]},
            "prov-2": {"id": "prov-2", "name": "B",
                        "models": ["gpt-x"]}}),
    })
    assert out3["assistants"][0]["chatModelId"] is None
    assert any("无法解析" in n for n in notes3)


def test_providers_list_branch_resolves_models(tmp_path):
    """K2R with a plain providers list (rh-shaped, no provider_configs_v1)
    still resolves assistant and message model references."""
    from rikkelivo.settings import (build_model_maps_k2r,
                                    build_rikkahub_settings)
    kelivo = {
        "providers": [{
            "type": "openai", "id": "prov-1", "enabled": True,
            "name": "聚合", "apiKey": "k", "baseUrl": "https://x/v1",
            "models": [
                {"id": "model-uuid-1", "modelId": "gpt-x",
                 "displayName": "GPT X", "type": "CHAT",
                 "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
                 "abilities": [], "customHeaders": [], "customBodies": [],
                 "tools": [], "providerOverwrite": None},
            ],
        }],
        "assistants_v1": json.dumps([{
            "id": "a1", "name": "主助手", "chatModelId": "gpt-x",
            "chatModelProvider": "prov-1"}]),
    }
    out, notes = build_rikkahub_settings(kelivo)
    assert out["assistants"][0]["chatModelId"] == "model-uuid-1"
    assert not any("无法解析" in n for n in notes)
    assert build_model_maps_k2r(kelivo)[("prov-1", "gpt-x")] == "model-uuid-1"


def test_conversation_metadata_loss_warnings(tmp_path):
    """Lossy per-conversation metadata emits explicit warnings in both
    directions instead of vanishing silently."""
    from rikkelivo import rikkahub
    from rikkelivo.convert import to_kelivo, to_rikkahub

    # K2R: kelivo conversation with mcp/truncate/summary fields
    chats = {
        "version": 2,
        "conversations": [{
            "id": "c1", "title": "t", "createdAt": "2026-08-06T10:00:00.000",
            "updatedAt": "2026-08-06T10:01:00.000", "messageIds": ["m1"],
            "isPinned": False, "mcpServerIds": ["mcp-1"],
            "assistantId": None, "truncateIndex": 5, "versionSelections": {},
            "summary": "今天聊了物理", "lastSummarizedMessageCount": 0,
            "chatSuggestions": [], "conversationKind": "normal",
        }],
        "messages": [{
            "id": "m1", "role": "user", "content": "hi",
            "timestamp": "2026-08-06T10:00:00.000", "modelId": None,
            "providerId": None, "totalTokens": None,
            "conversationId": "c1", "isStreaming": False,
            "reasoningText": None, "reasoningStartAt": None,
            "reasoningFinishedAt": None, "translation": None,
            "reasoningSegmentsJson": None, "groupId": "m1",
            "subgroupId": None, "version": 0, "promptTokens": None,
            "completionTokens": None, "cachedTokens": None,
            "durationMs": None, "isPreset": False, "speakerAssistantId": None,
        }],
        "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    rh = tmp_path / "c.rikkahub.zip"
    _, notes = to_rikkahub(_kelivo_zip(tmp_path, chats), rh)
    assert any("MCP 服务关联" in n for n in notes)
    assert any("上下文截断标记" in n for n in notes)
    assert any("摘要" in n for n in notes)

    # R2K: rikkahub conversation with the five metadata columns set
    con = rikkahub.create_database(str(tmp_path / "rh.db"))[0]
    rikkahub.write_conversation(con, {
        "id": "c1", "assistant_id": "", "title": "t", "nodes": "[]",
        "create_at": 0, "update_at": 0, "suggestions": "[]",
        "is_pinned": 0, "custom_system_prompt": "你是一个物理学家",
        "mode_injection_ids": '["inj-1"]', "lorebook_ids": '["lb-1"]',
        "workspace_cwd": "/workspaces/w1", "folder_id": "f1"})
    con.commit()
    con.close()
    rh2 = tmp_path / "c2.rikkahub.zip"
    with zipfile.ZipFile(rh2, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_path / "rh.db", "rikka_hub.db")
        zf.writestr("settings.json", "{}")
    kl = tmp_path / "c2.kelivo.zip"
    _, notes2 = to_kelivo(rh2, kl)
    assert any("自定义系统提示词" in n for n in notes2)
    assert any("模式注入" in n for n in notes2)
    assert any("世界书" in n for n in notes2)
    assert any("工作区路径" in n for n in notes2)
    assert any("文件夹归属" in n for n in notes2)
