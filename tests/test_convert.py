"""Core conversion behavior: parts, tools, attachments, boundaries, settings."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backup_fixtures import (
    ANSWER_V2,
    CONV1,
    KASSISTANT,
    KGROUP1,
    KMSG_A2,
    KPROVIDER,
    MODEL,
    NODE1,
    PROVIDER,
    make_backup,
    read_observables,
)

from rikkelivo import archive, convert, kelivo, rikkahub, settings


def _convert(source: Path, app: str, *, legacy: bool = False,
             tmp_path: Path):
    target = convert.target_profile(app, legacy=legacy)  # type: ignore[arg-type]
    out = tmp_path / f"o-{app}-{int(legacy)}.zip"
    with convert.prepare_conversion(source, target) as plan:
        convert.write_conversion(plan, out)
        return out, plan


def _db_of(zip_path: Path, tmp_path: Path) -> Path:
    staged = tmp_path / f"stage-{zip_path.stem}"
    with zipfile.ZipFile(zip_path) as zf:
        archive.validate_format(zf, archive.detect_archive(zf), staged)
    for candidate in (staged / "payload" / "rikka_hub.db",
                      staged / "payload" / "database" / "kelivo.db"):
        if candidate.exists():
            return candidate
    raise AssertionError("no database in output")


def _settings_of(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("settings.json"))


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def test_old_python_entry_points_removed():
    assert not hasattr(convert, "to_rikkahub")
    assert not hasattr(convert, "to_kelivo")
    assert not hasattr(kelivo, "build_message")


# ---------------------------------------------------------------------------
# Legacy markers and prompt variables
# ---------------------------------------------------------------------------

def test_legacy_marker_parser_skips_code():
    text, parts = kelivo.split_legacy_markers(
        "before\n```\n[image:code.png]\n```\n"
        "inline `[file:a|b|c]`\n[image:real.png]\n[file:x.txt|n.txt|text/plain]")
    assert "[image:code.png]" in text
    assert "`[file:a|b|c]`" in text
    assert [part.kind for part in parts] == ["image", "file"]
    assert parts[0].uri == "real.png"
    assert parts[1].name == "n.txt"


def test_legacy_plain_content_promotes_markers(tmp_path):
    source = make_backup(tmp_path / "json1.zip", app="kelivo", kind="json",
                         version=1)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        row = con.execute(
            "SELECT payload FROM message_part_rows WHERE revision_id IN "
            "(SELECT id FROM message_rows WHERE conversation_id='conv-2' "
            "AND role='assistant') AND kind='image'").fetchone()
        assert row is not None
        assert json.loads(row[0])["uri"].startswith("kelivo-file:///upload/")
        text = con.execute(
            "SELECT payload FROM message_part_rows WHERE revision_id IN "
            "(SELECT id FROM message_rows WHERE conversation_id='conv-2' "
            "AND role='assistant') AND kind='text'").fetchone()[0]
        assert "[image:" not in text
    finally:
        con.close()


def test_prompt_variables_round_trip():
    assert settings.convert_prompt_vars("hi {user}", to_rikkahub=True) == \
        "hi {{user}}"
    assert settings.convert_prompt_vars("hi {{char}}", to_rikkahub=False) == \
        "hi {assistant_name}"
    assert settings.convert_prompt_vars("no vars", True) == "no vars"


# ---------------------------------------------------------------------------
# Parts, tools, usage
# ---------------------------------------------------------------------------

def test_parts_order_and_tool_round_trip(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    rh, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    db = _db_of(rh, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        conversation_id = con.execute(
            "SELECT id FROM ConversationEntity WHERE title='Conversation one'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT messages, select_index FROM message_node WHERE "
            "conversation_id=? ORDER BY node_index",
            (conversation_id,)).fetchall()
        selected = json.loads(rows[1][0])[rows[1][1]]
        kinds = [part["type"] for part in selected["parts"]]
        assert kinds == ["reasoning", "text", "tool"]
        assert selected["usage"]["promptTokens"] == 3
        assert selected["lastUsage"]["completionTokens"] == 2
    finally:
        con.close()
    back, _plan = _convert(rh, "kelivo", tmp_path=tmp_path)
    db = _db_of(back, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        conversation_id = con.execute(
            "SELECT id FROM conversation_rows WHERE title='Conversation one'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT id, version, prompt_tokens, completion_tokens, "
            "cached_tokens, total_tokens, extras_json FROM message_rows "
            "WHERE conversation_id=? ORDER BY message_order",
            (conversation_id,)).fetchall()
        assistant = next(row for row in rows if row[2] == 3)
        assert assistant[2:6] == (3, 4, 1, 7)
        extras = json.loads(assistant[6])
        assert extras["tokens.finish"]["promptTokens"] == 1
        event = con.execute(
            "SELECT payload FROM message_part_rows WHERE revision_id=? "
            "AND kind='tool_call'", (assistant[0],)).fetchone()
        assert json.loads(event[0])["content"] == "tool-result"
    finally:
        con.close()


def test_attachments_map_and_missing_file_is_marked(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31, variant="missing-file")
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    codes = {item.code for item in plan.diagnostics}
    assert "source_file_missing" in codes
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        payloads = [json.loads(row[0]) for row in con.execute(
            "SELECT payload FROM message_part_rows WHERE kind='image'")]
        assert any(payload["uri"] == "kelivo-file:///upload/fixture.png"
                   for payload in payloads)
        assert any(payload.get("unavailable") for payload in payloads)
    finally:
        con.close()


def test_attachment_references_use_rh_upload_mapping(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        message_rows = con.execute(
            "SELECT messages FROM message_node WHERE conversation_id="
            "(SELECT id FROM ConversationEntity WHERE title='Conversation two')"
        ).fetchall()
        parts = [part for row in message_rows
                 for message in json.loads(row[0])
                 for part in message["parts"]]
        document = next(part for part in parts
                        if part["type"] == "document")
        assert document["url"].startswith(rikkahub.RH_FILES_PREFIX + "upload/")
        managed = con.execute(
            "SELECT relative_path FROM managed_files").fetchall()
        assert any(rikkahub.RH_FILES_PREFIX + row[0] == document["url"]
                   for row in managed)
    finally:
        con.close()


def test_unknown_part_kind_is_reported(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="unknown-part")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    assert out.exists()
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "无法表达" in details


# ---------------------------------------------------------------------------
# Context boundary
# ---------------------------------------------------------------------------

def test_rh_checkpoint_becomes_kelivo_truncate_index(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31, variant="checkpoint")
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        assert con.execute(
            "SELECT truncate_index FROM conversation_rows LIMIT 1"
        ).fetchone()[0] == 1
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "context_boundary_approximated" in codes


def test_kelivo_truncate_index_becomes_rh_checkpoint(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="truncate")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        conversation_id = con.execute(
            "SELECT id FROM ConversationEntity WHERE title='Conversation one'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT messages, select_index FROM message_node WHERE "
            "conversation_id=? ORDER BY node_index",
            (conversation_id,)).fetchall()
        selected = json.loads(rows[1][0])[rows[1][1]]
        assert selected.get("isContextCheckpoint") is True
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "context_boundary_approximated" in codes


def test_empty_context_is_not_faked_as_empty_message(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="truncate-empty")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["selected_text"] == ANSWER_V2
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "全部可见历史" in details


def test_legacy_rh_reports_context_boundary_loss(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="truncate")
    out, plan = _convert(source, "rikkahub", legacy=True, tmp_path=tmp_path)
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "边界" in details


# ---------------------------------------------------------------------------
# Memory edge cases
# ---------------------------------------------------------------------------

def test_blank_and_archived_memory_are_dropped(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="memory-edge")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        contents = " ".join(row[0] for row in con.execute(
            "SELECT content FROM memory_file"))
        assert "archived entry" not in contents
        assert "xxxx" in contents  # large content is kept, only warned about
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "memory_blank_content_unsupported" in codes
    assert "unsupported_data" in codes
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "48KiB" in details


def test_blank_rh_memory_file_is_dropped_for_kelivo(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31, variant="memory-blank")
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM memory_entry_rows WHERE content='   \n'"
        ).fetchone()[0] == 0
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "memory_blank_content_unsupported" in codes


# ---------------------------------------------------------------------------
# Selection edge cases
# ---------------------------------------------------------------------------

def test_missing_reference_falls_back_and_reports(tmp_path):
    messages = []
    for message_id, version, text in (("u1", 0, "q"), ("a0", 0, "v0"),
                                      ("a5", 5, "v5")):
        messages.append({
            "id": message_id, "role": "user" if message_id == "u1"
            else "assistant", "content": text,
            "timestamp": "2026-01-01T00:00:00.000", "modelId": None,
            "providerId": None, "totalTokens": None, "conversationId": "c1",
            "isStreaming": False, "reasoningText": None,
            "reasoningStartAt": None, "reasoningFinishedAt": None,
            "translation": None, "reasoningSegmentsJson": None,
            "groupId": "u1" if message_id == "u1" else "g1",
            "subgroupId": None, "version": version,
            "promptTokens": None, "completionTokens": None,
            "cachedTokens": None, "durationMs": None, "isPreset": False,
            "speakerAssistantId": None,
        })
    document = {
        "version": 2,
        "conversations": [{
            "id": "c1", "title": "T",
            "createdAt": "2026-01-01T00:00:00.000",
            "updatedAt": "2026-01-01T00:00:01.000",
            "messageIds": ["u1", "a0", "a2", "a5"], "isPinned": False,
            "mcpServerIds": [], "assistantId": None, "truncateIndex": -1,
            "versionSelections": {"g1": 2}, "summary": None,
            "lastSummarizedMessageCount": 0, "chatSuggestions": [],
            "conversationKind": "normal",
        }],
        "messages": messages, "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    source = tmp_path / "incremental.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("settings.json", json.dumps({"theme_v1": "dark"}))
        zf.writestr("chats.json", json.dumps(document))
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        values = json.loads(con.execute(
            "SELECT version_selections_json FROM conversation_rows"
        ).fetchone()[0])
        assert values["g1"] == 5
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "selection_unavailable" in codes
    assert "source_completeness_unknown" in codes


def test_group_subgroup_survives_cuplivo_legacy_target(tmp_path):
    source = make_backup(tmp_path / "group.zip", app="cuplivo",
                         kind="json", version=2, variant="group")
    out, _plan = _convert(source, "cuplivo", legacy=True, tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        document = json.loads(zf.read("chats.json"))
    assert document["version"] == 2
    assert document["groupChats"]
    assert document["groupMembers"]
    assistant = [message for message in document["messages"]
                 if message["id"] == KMSG_A2][0]
    assert assistant["subgroupId"] == "sub-1"
    assert assistant["speakerAssistantId"] == KASSISTANT
    assert document["conversations"][0]["versionSelections"][KGROUP1] == 2


# ---------------------------------------------------------------------------
# Settings pairing
# ---------------------------------------------------------------------------

def _settings_only_json(tmp_path: Path, settings_payload: dict) -> Path:
    path = tmp_path / "settings.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("settings.json", json.dumps(settings_payload))
    return path


def _kelivo_provider(provider_id: str) -> dict:
    return {
        "id": provider_id, "enabled": True, "name": provider_id,
        "apiKey": "k", "baseUrl": f"https://{provider_id}.example/v1",
        "providerType": "openai", "models": ["shared"],
        "modelOverrides": {"shared": {"name": "Shared"}},
        "customHeaders": [], "customBody": [],
    }


def test_provider_model_pairing_uses_provider_and_logical_id(tmp_path):
    settings_payload = {
        "provider_configs_v1": json.dumps({
            "p1": _kelivo_provider("p1"), "p2": _kelivo_provider("p2")}),
        "providers_order_v1": ["p1", "p2"],
        "assistants_v1": json.dumps([{
            "id": "a", "name": "A", "chatModelProvider": "p2",
            "chatModelId": "shared", "enableMemory": False,
        }]),
    }
    source = _settings_only_json(tmp_path, settings_payload)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    settings_out = _settings_of(out)
    expected_provider = rikkahub.entity_uuid("p2", "provider")
    expected_model = settings._model_uuid(expected_provider, "shared")
    assert settings_out["assistants"][0]["chatModelId"] == expected_model
    assert settings_out["providers"][1]["id"] == expected_provider


def test_unqualified_model_name_only_when_unique(tmp_path):
    settings_payload = {
        "provider_configs_v1": json.dumps({
            "p1": _kelivo_provider("p1"), "p2": _kelivo_provider("p2")}),
        "providers_order_v1": ["p1", "p2"],
        "assistants_v1": json.dumps([{
            "id": "a", "name": "A", "chatModelId": "shared",
            "enableMemory": False,
        }]),
    }
    source = _settings_only_json(tmp_path, settings_payload)
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    settings_out = _settings_of(out)
    assert settings_out["assistants"][0]["chatModelId"] is None
    codes = {item.code for item in plan.diagnostics}
    assert "identity_reference_unresolved" in codes


def test_unique_unqualified_model_resolves(tmp_path):
    settings_payload = {
        "provider_configs_v1": json.dumps({"p1": _kelivo_provider("p1")}),
        "providers_order_v1": ["p1"],
        "assistants_v1": json.dumps([{
            "id": "a", "name": "A", "chatModelId": "shared",
            "enableMemory": False,
        }]),
    }
    source = _settings_only_json(tmp_path, settings_payload)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    settings_out = _settings_of(out)
    provider_id = rikkahub.entity_uuid("p1", "provider")
    assert settings_out["assistants"][0]["chatModelId"] == \
        settings._model_uuid(provider_id, "shared")


# ---------------------------------------------------------------------------
# No hidden carry channels
# ---------------------------------------------------------------------------

def test_output_has_no_sidecar_or_private_fields(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    assert not (tmp_path / "o-kelivo-0.skills.zip").exists()
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        payloads = [row[0] for row in con.execute(
            "SELECT payload FROM message_part_rows")]
        assert payloads
        assert all("x_rikkelivo" not in payload for payload in payloads)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Authentication and provider kinds
# ---------------------------------------------------------------------------

def _kelivo_settings_only(tmp_path: Path, provider: dict,
                          assistant: dict | None = None) -> Path:
    payload = {
        "provider_configs_v1": json.dumps({"p1": provider}),
        "providers_order_v1": ["p1"],
        "assistants_v1": json.dumps([assistant or {
            "id": "a1", "name": "A", "chatModelProvider": "p1",
            "chatModelId": "m1", "enableMemory": False,
        }]),
    }
    return _settings_only_json(tmp_path, payload)


def test_kimi_and_claude_oauth_are_unsupported(tmp_path):
    for oauth in ("kimi", "claude"):
        provider = {
            "id": "p1", "enabled": True, "name": oauth, "apiKey": "",
            "oauthProvider": oauth, "oauthCredentials": {
                "accessToken": "a", "refreshToken": "r",
                "expiresAt": "2030-01-01T00:00:00Z", "sessionId": "s"},
            "baseUrl": "https://example.com", "providerType": "openai",
            "models": ["m1"], "modelOverrides": {},
            "customHeaders": [], "customBody": [],
        }
        case_dir = tmp_path / oauth
        case_dir.mkdir()
        source = _kelivo_settings_only(case_dir, provider)
        out, plan = _convert(source, "rikkahub", tmp_path=case_dir)
        settings_out = _settings_of(out)
        assert settings_out["providers"] == []
        assert settings_out["assistants"][0]["chatModelId"] is None
        assert any(item.code == "unsupported_data"
                   for item in plan.diagnostics)


def test_active_multi_key_provider_is_dropped(tmp_path):
    provider = {
        "id": "p1", "enabled": True, "name": "Multi", "apiKey": "",
        "multiKeyEnabled": True,
        "apiKeys": [{"id": "k1", "key": "secret"}],
        "baseUrl": "https://example.com", "providerType": "openai",
        "models": ["m1"], "modelOverrides": {},
        "customHeaders": [], "customBody": [],
    }
    source = _kelivo_settings_only(tmp_path, provider)
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    assert _settings_of(out)["providers"] == []
    assert any(item.code == "unsupported_data" for item in plan.diagnostics)


def test_inactive_multi_key_is_kept_with_warning(tmp_path):
    provider = {
        "id": "p1", "enabled": True, "name": "Multi", "apiKey": "primary",
        "multiKeyEnabled": False,
        "apiKeys": [{"id": "k1", "key": "secret"}],
        "baseUrl": "https://example.com", "providerType": "openai",
        "models": ["m1"], "modelOverrides": {},
        "customHeaders": [], "customBody": [],
    }
    source = _kelivo_settings_only(tmp_path, provider)
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    providers = _settings_of(out)["providers"]
    assert len(providers) == 1
    assert providers[0]["apiKey"] == "primary"
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "备用 API Key" in details


def test_provider_overwrite_model_is_dropped(tmp_path):
    settings_payload = {
        "displaySetting": {},
        "providers": [{
            "id": PROVIDER, "type": "openai", "enabled": True,
            "name": "P", "apiKey": "k", "baseUrl": "https://example.com",
            "chatCompletionsPath": "/chat/completions",
            "useResponseApi": False, "customHeaders": [],
            "models": [{
                "id": MODEL, "modelId": "m1", "displayName": "M",
                "type": "CHAT", "inputModalities": ["TEXT"],
                "outputModalities": ["TEXT"], "abilities": [],
                "providerOverwrite": {
                    "id": "sub", "type": "openai", "enabled": True,
                    "name": "sub", "apiKey": "k", "baseUrl": "https://sub",
                    "models": [],
                },
            }],
        }],
        "assistants": [], "mcpServers": [], "quickMessages": [],
        "modeInjections": [], "lorebooks": [], "searchServices": [],
        "ttsProviders": [], "asrProviders": [], "assistantTags": [],
    }
    source = _settings_only_json(tmp_path, settings_payload)
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    settings_out = _settings_of(out)
    providers = json.loads(settings_out["provider_configs_v1"])
    assert providers == {}
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "providerOverwrite" in details


# ---------------------------------------------------------------------------
# Truncation and legacy memory
# ---------------------------------------------------------------------------

def test_jsonl_truncate_index_is_logical(tmp_path):
    source = make_backup(tmp_path / "jsonl.zip", app="cuplivo",
                         kind="jsonl", version=2, variant="truncate")
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        conversation_id = con.execute(
            "SELECT id FROM ConversationEntity WHERE title='Conversation one'"
        ).fetchone()[0]
        rows = con.execute(
            "SELECT messages, select_index FROM message_node WHERE "
            "conversation_id=? ORDER BY node_index",
            (conversation_id,)).fetchall()
        selected = json.loads(rows[1][0])[rows[1][1]]
        assert selected.get("isContextCheckpoint") is True
    finally:
        con.close()


def test_kelivo_entries_become_one_legacy_memory_row_each(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3,
                         variant="memory-edge")
    out, _plan = _convert(source, "rikkahub", legacy=True, tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        rows = con.execute("SELECT content FROM MemoryEntity").fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith("xxxx")
    finally:
        con.close()


def test_rh_upload_subdirectories_are_rejected(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    members["upload/sub/nested.png"] = b"x"
    broken = tmp_path / "nested.zip"
    with zipfile.ZipFile(broken, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    with zipfile.ZipFile(broken) as zf:
        with pytest.raises(archive.ArchiveError, match="扁平文件"):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "stage")


def test_epoch_zero_timestamp_is_not_replaced(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31, variant="epoch-zero")
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        timestamps = [row[0] for row in con.execute(
            "SELECT timestamp FROM message_rows")]
        assert 0 in timestamps
    finally:
        con.close()


def test_missing_message_time_falls_back_with_diagnostic(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31, variant="missing-time")
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    assert any(item.code == "value_defaulted"
               and item.category == "timestamp"
               for item in plan.diagnostics)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        # Conversation one was created at 1000 ms; the missing message time
        # falls back to it (1000 ms -> 1_000_000 us), never to "now".
        assert con.execute(
            "SELECT timestamp FROM message_rows WHERE role='assistant' "
            "AND version != 0 LIMIT 1").fetchone()[0] == 1_000_000
    finally:
        con.close()


def test_usage_backfill_missing_time_falls_back_and_counts(tmp_path):
    source = make_backup(tmp_path / "rh24.zip", app="rikkahub", kind="rh",
                         version=24, variant="missing-time")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    assert any(item.code == "value_defaulted"
               and item.category == "timestamp"
               for item in plan.diagnostics)
    db = _db_of(out, tmp_path)
    con = __import__("sqlite3").connect(db)
    try:
        assert con.execute(
            "SELECT created_at FROM usage_record").fetchone()[0] == 1000
    finally:
        con.close()
