"""Schema, detection, migration and projection behavior."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from backup_fixtures import (
    ANSWER_V2,
    ASSISTANT,
    CONV1,
    CONV2,
    KASSISTANT,
    KMSG_A2,
    KPROVIDER,
    MODEL,
    NODE1,
    PROVIDER,
    make_backup,
    read_observables,
)

from rikkelivo import archive, convert, rikkahub, schema


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _convert(source: Path, app: str, *, legacy: bool = False,
             tmp_path: Path):
    target = convert.target_profile(app, legacy=legacy)  # type: ignore[arg-type]
    out = tmp_path / f"out-{app}-{int(legacy)}.zip"
    with convert.prepare_conversion(source, target) as plan:
        convert.write_conversion(plan, out)
        return out, plan


def _staged_db(zip_path: Path, tmp_path: Path) -> sqlite3.Connection:
    staged = tmp_path / f"staged-{zip_path.stem}"
    with zipfile.ZipFile(zip_path) as zf:
        info = archive.validate_format(zf, archive.detect_archive(zf), staged)
        assert info.kind
    db = staged / "payload" / "rikka_hub.db"
    if not db.exists():
        db = staged / "payload" / "database" / "kelivo.db"
    return sqlite3.connect(db)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", schema.RH_VERSIONS)
def test_room_schema_creation_roundtrip(tmp_path, version):
    path = tmp_path / f"room-{version}.db"
    schema.create_room_database(path, version)
    con = sqlite3.connect(path)
    try:
        schema.validate_room_database(con, version)
        assert con.execute("PRAGMA user_version").fetchone()[0] == version
        identity = con.execute(
            "SELECT identity_hash FROM room_master_table WHERE id = 42"
        ).fetchone()[0]
        assert identity == schema.rh_identity(version)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


@pytest.mark.parametrize("version", schema.DRIFT_VERSIONS)
def test_drift_schema_creation_roundtrip(tmp_path, version):
    path = tmp_path / f"drift-{version}.db"
    schema.create_drift_database(path, version)
    con = sqlite3.connect(path)
    try:
        schema.validate_drift_database(con, version)
        assert con.execute("PRAGMA user_version").fetchone()[0] == version
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_room_validation_rejects_unknown_schema(tmp_path):
    path = tmp_path / "unknown.db"
    schema.create_room_database(path, 31)
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(archive.UnsupportedArchiveError):
        make_rh_with_db(tmp_path, path)


def make_rh_with_db(tmp_path: Path, db: Path) -> None:
    source = tmp_path / "wrong.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.write(db, "rikka_hub.db")
        zf.writestr("settings.json", json.dumps({"displaySetting": {}}))
    with zipfile.ZipFile(source) as zf:
        archive.validate_format(zf, archive.detect_archive(zf),
                                tmp_path / "staging")


# ---------------------------------------------------------------------------
# Detection and validation
# ---------------------------------------------------------------------------

def test_detect_mutually_exclusive_markers(tmp_path):
    source = _write_zip(tmp_path / "both.zip", {
        "rikka_hub.db": b"x", "chats.json": b"{}",
        "settings.json": b"{}",
    })
    with zipfile.ZipFile(source) as zf:
        with pytest.raises(archive.ArchiveError, match="多种聊天数据格式"):
            archive.detect_archive(zf)


def test_unknown_settings_only_is_not_corruption(tmp_path):
    source = _write_zip(tmp_path / "bare.zip", {
        "settings.json": b"{}",
    })
    with pytest.raises(archive.UnsupportedArchiveError,
                       match="信息不足"):
        archive.inspect_backup(source)


def test_encrypted_magic_rejected(tmp_path):
    source = tmp_path / "locked.kbackup"
    source.write_bytes(b"KELIVENC" + b"\x00" * 32)
    with pytest.raises(archive.UnsupportedArchiveError, match="加密"):
        archive.inspect_backup(source)


def test_duplicate_and_absolute_entries_rejected(tmp_path):
    for name, member in (("dup.zip", "same.txt"),):
        with zipfile.ZipFile(tmp_path / name, "w") as zf:
            zf.writestr("settings.json", "{}")
            zf.writestr(member, "a")
            zf.writestr(member, "b")
    with zipfile.ZipFile(tmp_path / "dup.zip") as zf:
        with pytest.raises(archive.ArchiveError, match="重复"):
            archive.detect_archive(zf)
    absolute = _write_zip(tmp_path / "abs.zip", {
        "settings.json": b"{}", "/etc/passwd": b"x"})
    with zipfile.ZipFile(absolute) as zf:
        with pytest.raises(archive.ArchiveError, match="绝对路径"):
            archive.detect_archive(zf)


def test_wal_without_database_is_rejected(tmp_path):
    source = _write_zip(tmp_path / "wal.zip", {
        "rikka_hub-wal": b"", "settings.json": b"{}"})
    with zipfile.ZipFile(source) as zf:
        with pytest.raises(archive.ArchiveError, match="WAL"):
            archive.detect_archive(zf)


def test_rh_wal_transaction_is_imported(tmp_path):
    db = tmp_path / "rikka_hub.db"
    schema.create_room_database(db, 31)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute(
        "INSERT INTO ConversationEntity (id,assistant_id,title,nodes,"
        "create_at,update_at) VALUES (?,?,?,?,?,?)",
        (CONV1, rikkahub.DEFAULT_ASSISTANT_ID, "wal", "[]", 1, 2))
    con.execute(
        "INSERT INTO message_node (id,conversation_id,node_index,messages,"
        "select_index) VALUES (?,?,?,?,?)",
        (NODE1, CONV1, 0, json.dumps([{
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "role": "user",
            "parts": [{"type": "text", "text": "wal-visible"}],
            "annotations": [], "createdAt": "2026-01-01T00:00:00.000",
            "finishedAt": None, "modelId": None, "usage": None,
            "translation": None}]), 0))
    con.commit()
    main = db.read_bytes()
    wal = Path(str(db) + "-wal").read_bytes()
    con.close()
    assert b"wal-visible" not in main
    source = tmp_path / "wal-backed.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr("rikka_hub.db", main)
        zf.writestr("rikka_hub-wal", wal)
        zf.writestr("settings.json", "{}")
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    view = read_observables(out)
    assert (view["conversations"], view["messages"]) == (1, 1)


def test_simple_tokenizer_source_reads_ordinary_tables(tmp_path):
    db = tmp_path / "rikka_hub.db"
    schema.create_room_database(db, 31)
    con = sqlite3.connect(db)
    con.execute("PRAGMA writable_schema=ON")
    con.execute("UPDATE sqlite_master SET sql=replace(sql,'unicode61','simple')"
                " WHERE name='message_fts'")
    con.execute("PRAGMA writable_schema=OFF")
    con.execute(
        "INSERT INTO ConversationEntity (id,assistant_id,title,nodes,"
        "create_at,update_at) VALUES (?,?,?,?,?,?)",
        (CONV1, rikkahub.DEFAULT_ASSISTANT_ID, "fts", "[]", 1, 2))
    con.commit()
    con.close()
    source = tmp_path / "simple.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.write(db, "rikka_hub.db")
        zf.writestr("settings.json", "{}")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    codes = {item.code for item in plan.diagnostics}
    if not schema.probe_fts().simple:
        assert "search_tokenizer_changed" in codes
    with zipfile.ZipFile(out) as zf:
        inner = zf.read("rikka_hub.db")
    local = tmp_path / "output.db"
    local.write_bytes(inner)
    con = sqlite3.connect(local)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM ConversationEntity").fetchone()[0] == 1
        fts_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='message_fts'"
        ).fetchone()[0]
        assert ("unicode61" in fts_sql) or ("simple" in fts_sql)
    finally:
        con.close()


def test_manifest_hash_mismatch_rejected(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    # Replace the database with different bytes without updating the manifest.
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    members["database/kelivo.db"] = members["database/kelivo.db"] + b"\x00"
    broken = _write_zip(tmp_path / "broken.zip", members)
    with zipfile.ZipFile(broken) as zf:
        with pytest.raises(archive.ArchiveError, match="SHA256|大小"):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "staging")


def test_manifest_counts_must_match_database(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    manifest["database"]["messageCount"] = 99
    members["manifest.json"] = json.dumps(manifest).encode()
    broken = _write_zip(tmp_path / "bad-count.zip", members)
    with zipfile.ZipFile(broken) as zf:
        with pytest.raises(archive.ArchiveError, match="计数"):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "staging")


# ---------------------------------------------------------------------------
# RikkaHub native migrations
# ---------------------------------------------------------------------------

def test_rh_24_to_31_migrates_memory_usage_and_consolidation(tmp_path):
    source = make_backup(tmp_path / "rh24.zip", app="rikkahub", kind="rh",
                         version=24)
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["schema_version"] == 31
    assert "/profile.md" in view["memory_paths"]
    assert "CHAT" in view["usage_sources"]
    with zipfile.ZipFile(out) as zf:
        db = tmp_path / "out.db"
        db.write_bytes(zf.read("rikka_hub.db"))
    con = sqlite3.connect(db)
    try:
        consolidation = con.execute(
            "SELECT conversation_id, consolidated_at FROM memory_consolidation"
        ).fetchall()
        assert (CONV1, 5000) in consolidation
        assert (CONV2, 9000) in consolidation
        profile = con.execute(
            "SELECT content FROM memory_file WHERE path='/profile.md'"
        ).fetchone()[0]
        assert profile.startswith("---\nname: profile")
        assert "- [stated] legacy memory one" in profile
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "usage_history_reconstructed" in codes


def test_rh_31_to_24_downgrades_and_reports(tmp_path):
    source = make_backup(tmp_path / "rh31.zip", app="rikkahub", kind="rh",
                         version=31)
    out, plan = _convert(source, "rikkahub", legacy=True, tmp_path=tmp_path)
    view = read_observables(out)
    assert view["schema_version"] == 24
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        db = tmp_path / "out24.db"
        db.write_bytes(zf.read("rikka_hub.db"))
    assert not any(name.startswith("media_creation/") for name in names)
    con = sqlite3.connect(db)
    try:
        memory = con.execute("SELECT content FROM MemoryEntity").fetchall()
        assert any("/profile.md" not in row[0] for row in memory)
        assert memory
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "memory_file" not in tables
        assert "usage_record" not in tables
    finally:
        con.close()
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "媒体创作" in details
    codes = {item.code for item in plan.diagnostics}
    assert "unsupported_data" in codes
    assert "historical_type_normalized" in codes


def test_rh_same_version_preserves_all_entities(tmp_path):
    source = make_backup(tmp_path / "rh31.zip", app="rikkahub", kind="rh",
                         version=31)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        db = tmp_path / "same.db"
        db.write_bytes(zf.read("rikka_hub.db"))
        names = set(zf.namelist())
    assert "media_creation/fixture.png" in names
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_folder").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM usage_record").fetchone()[0] == 2
        assert con.execute(
            "SELECT COUNT(*) FROM media_creation_record").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM translation_history").fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
        paths = {row[0] for row in con.execute("SELECT path FROM memory_file")}
        assert "/topics/project.md" in paths and "/global.md" in paths
        node = con.execute(
            "SELECT messages, select_index FROM message_node WHERE id=?",
            (NODE1,)).fetchone()
        messages = json.loads(node[0])
        assert messages[node[1]]["parts"][1]["text"] == ANSWER_V2
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Historical selection dialects
# ---------------------------------------------------------------------------

def test_json1_ordinal_selection(tmp_path):
    source = make_backup(tmp_path / "json1.zip", app="kelivo", kind="json",
                         version=1)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    assert read_observables(out)["selected_text"] == ANSWER_V2


def test_json2_actual_selection(tmp_path):
    source = make_backup(tmp_path / "json2.zip", app="cuplivo", kind="json",
                         version=2)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    assert read_observables(out)["selected_text"] == ANSWER_V2


def test_ambiguous_single_token_selection_is_rejected(tmp_path):
    messages = []
    for version, text in ((0, "v0"), (2, "v2"), (5, "v5")):
        messages.append({
            "id": f"m{version}", "role": "assistant", "content": text,
            "timestamp": "2026-01-01T00:00:00.000", "modelId": None,
            "providerId": None, "totalTokens": None, "conversationId": "c1",
            "isStreaming": False, "reasoningText": None,
            "reasoningStartAt": None, "reasoningFinishedAt": None,
            "translation": None, "reasoningSegmentsJson": None,
            "groupId": "g1", "subgroupId": None, "version": version,
            "promptTokens": None, "completionTokens": None,
            "cachedTokens": None, "durationMs": None, "isPreset": False,
            "speakerAssistantId": None,
        })
    document = {
        "version": 1,
        "conversations": [{
            "id": "c1", "title": "T",
            "createdAt": "2026-01-01T00:00:00.000",
            "updatedAt": "2026-01-01T00:00:01.000",
            "messageIds": ["m0", "m2", "m5"], "isPinned": False,
            "mcpServerIds": [], "assistantId": None, "truncateIndex": -1,
            "versionSelections": {"g1": 2}, "summary": None,
            "lastSummarizedMessageCount": 0, "chatSuggestions": [],
            "conversationKind": "normal",
        }],
        "messages": messages, "toolEvents": {}, "geminiThoughtSigs": {},
        "groupChats": [], "groupMembers": [],
    }
    source = _write_zip(tmp_path / "ambiguous.zip", {
        "settings.json": json.dumps({"theme_v1": "dark"}).encode(),
        "chats.json": json.dumps(document).encode(),
    })
    with pytest.raises(archive.ArchiveError, match="选择意义不明确"):
        _convert(source, "kelivo", tmp_path=tmp_path)


def test_jsonl_tool_and_signature_survive(tmp_path):
    source = make_backup(tmp_path / "jsonl.zip", app="cuplivo", kind="jsonl",
                         version=2)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        tool = con.execute(
            "SELECT payload FROM message_part_rows WHERE revision_id=? "
            "AND kind='tool_call'", (KMSG_A2,)).fetchone()
        assert tool is not None and "tool-result" in tool[0]
        artifact = con.execute(
            "SELECT payload FROM provider_artifact_rows WHERE revision_id=? "
            "AND kind='gemini_thought_signature'", (KMSG_A2,)).fetchone()
        assert artifact is not None and artifact[0] == "fixture-signature"
    finally:
        con.close()


def test_forward_compatible_archive_is_projected(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    db_path = tmp_path / "future.db"
    db_path.write_bytes(members["database/kelivo.db"])
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE future_extension (id TEXT)")
    con.execute("INSERT INTO future_extension VALUES ('x')")
    con.execute("PRAGMA user_version = 4")
    con.commit()
    con.close()
    members["database/kelivo.db"] = db_path.read_bytes()
    manifest["database"]["schemaVersion"] = 4
    manifest["database"]["minimumReadableSchemaVersion"] = 2
    manifest["entries"]["database/kelivo.db"] = {
        "bytes": len(members["database/kelivo.db"]),
        "sha256": __import__("hashlib").sha256(
            members["database/kelivo.db"]).hexdigest(),
    }
    members["manifest.json"] = json.dumps(manifest).encode()
    future = _write_zip(tmp_path / "future.zip", members)
    out, plan = _convert(future, "kelivo", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["schema_version"] == 3
    assert view["conversations"] == 2
    codes = {item.code for item in plan.diagnostics}
    assert "forward_extensions_dropped" in codes


def test_future_archive_without_declaration_is_rejected(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    manifest["formatVersion"] = 9
    manifest.pop("minimumReadableFormatVersion", None)
    members["manifest.json"] = json.dumps(manifest).encode()
    future = _write_zip(tmp_path / "future.zip", members)
    with zipfile.ZipFile(future) as zf:
        with pytest.raises(archive.UnsupportedArchiveError, match="无法"):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "staging")


# ---------------------------------------------------------------------------
# Cross-family projection
# ---------------------------------------------------------------------------

def test_rh_to_kelivo_preserves_parts_selection_and_memory(tmp_path):
    source = make_backup(tmp_path / "rh31.zip", app="rikkahub", kind="rh",
                         version=31)
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["selected_text"] == ANSWER_V2
    con = _staged_db(out, tmp_path)
    try:
        selections = json.loads(con.execute(
            "SELECT version_selections_json FROM conversation_rows LIMIT 1"
        ).fetchone()[0])
        selected_version = selections[NODE1]
        parts = con.execute(
            "SELECT kind, payload FROM message_part_rows WHERE revision_id IN "
            "(SELECT id FROM message_rows WHERE conversation_id=? AND "
            "group_id=? AND version=?) ORDER BY ordinal",
            (CONV1, NODE1, selected_version)).fetchall()
        assert [kind for kind, _payload in parts] == [
            "reasoning", "text", "tool_call", "image"]
        assert parts[1][1] == ANSWER_V2
        tool = json.loads(parts[2][1])
        assert tool["content"] == "tool-result"
        image = json.loads(parts[3][1])
        assert image["uri"].startswith("kelivo-file:///upload/")
        memory = con.execute(
            "SELECT type, status, content FROM memory_entry_rows "
            "ORDER BY sort_order").fetchall()
        # The fixture assistant uses global memory, so its private files are
        # dormant and must stay archived; the shared file becomes an active
        # assistant-scoped copy instead of a target-global entry.
        assert ("identity", "archived", "profile text") in memory
        assert ("instruction", "archived", "project body") in memory
        assert ("instruction", "active", "shared") in memory
        workspaces = con.execute(
            "SELECT payload FROM extension_entity_rows WHERE kind='workspace'"
        ).fetchall()
        assert len(workspaces) == 1
        payload = json.loads(workspaces[0][0])
        assert payload["hostPath"] is None
        assert payload["kind"] == "managed"
    finally:
        con.close()
    codes = {item.code for item in plan.diagnostics}
    assert "workspace_metadata_only" in codes


def test_kelivo_to_rh_memory_permissions_and_selection(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["selected_text"] == ANSWER_V2
    con = _staged_db(out, tmp_path)
    try:
        profile = con.execute(
            "SELECT content FROM memory_file WHERE path='/profile.md'"
        ).fetchall()
        preferences = con.execute(
            "SELECT content FROM memory_file WHERE path='/preferences.md'"
        ).fetchall()
        assert profile and "## Identity" in profile[0][0]
        assert "global identity" in profile[0][0]
        assert preferences and "assistant instruction" in preferences[0][0]
        conversation_id = con.execute(
            "SELECT id FROM ConversationEntity WHERE title=?",
            ("Conversation one",)).fetchone()[0]
        node = con.execute(
            "SELECT messages, select_index FROM message_node WHERE "
            "conversation_id=? ORDER BY node_index",
            (conversation_id,)).fetchall()
        revision_node = next(
            item for item in node if len(json.loads(item[0])) > 1)
        selected = json.loads(revision_node[0])[revision_node[1]]
        texts = [part["text"] for part in selected["parts"]
                 if part["type"] == "text"]
        assert ANSWER_V2 in texts
        assert selected["id"] == selected["id"].lower()
        tool_parts = [part for part in selected["parts"]
                      if part["type"] == "tool"]
        assert tool_parts and tool_parts[0]["output"][0]["text"] == "tool-result"
    finally:
        con.close()


def test_kelivo_to_rh_message_ids_are_uuids(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        for row in con.execute("SELECT messages FROM message_node"):
            for message in json.loads(row[0]):
                assert rikkahub.is_uuid(message["id"])
                assert message["id"] == message["id"].lower()
        conversation = con.execute(
            "SELECT id, assistant_id FROM ConversationEntity").fetchall()
        assert all(rikkahub.is_uuid(row[0]) for row in conversation)
        assert all(rikkahub.is_uuid(row[1]) for row in conversation)
    finally:
        con.close()


def test_group_chat_is_linearized_with_speaker_reference(tmp_path):
    source = make_backup(tmp_path / "group.zip", app="cuplivo",
                         kind="json", version=2, variant="group")
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        selected_texts = []
        for row in con.execute("SELECT messages, select_index FROM message_node"):
            messages = json.loads(row[0])
            selected_texts.append("".join(
                part.get("text", "") for part in messages[row[1]]["parts"]
                if part.get("type") == "text"))
    finally:
        con.close()
    assert any(text.startswith("> **") and "answer-v2" in text
               for text in selected_texts)
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "发送者引用" in details


def test_workspace_files_are_not_installed_into_rh(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert not any(name.startswith("workspaces/") for name in names)
    assert any(name.startswith("skills/") for name in names)
    codes = {item.code for item in plan.diagnostics}
    assert "workspace_metadata_only" in codes


def test_media_and_translation_drop_are_reported(tmp_path):
    source = make_backup(tmp_path / "rh31.zip", app="rikkahub", kind="rh",
                         version=31)
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert not any(name.startswith("media_creation/") for name in names)
    categories = {item.category for item in plan.diagnostics}
    assert "media creation" in categories
    assert "translation history" in categories
    assert "usage ledger" in categories


# ---------------------------------------------------------------------------
# Settings and authentication
# ---------------------------------------------------------------------------

def _rh_settings_source(tmp_path: Path, providers: list[dict],
                        assistants: list[dict]) -> Path:
    db = tmp_path / "rikka_hub.db"
    schema.create_room_database(db, 31)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO ConversationEntity (id,assistant_id,title,nodes,"
        "create_at,update_at) VALUES (?,?,?,?,?,?)",
        (CONV1, assistants[0]["id"] if assistants else
         rikkahub.DEFAULT_ASSISTANT_ID, "T", "[]", 1, 2))
    con.commit()
    con.close()
    source = tmp_path / "settings-source.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.write(db, "rikka_hub.db")
        zf.writestr("settings.json", json.dumps({
            "displaySetting": {}, "providers": providers,
            "assistants": assistants, "mcpServers": [], "quickMessages": [],
            "modeInjections": [], "lorebooks": [], "searchServices": [],
            "ttsProviders": [], "asrProviders": [], "assistantTags": [],
        }))
    return source


def test_chatgpt_account_maps_between_native_types(tmp_path):
    provider = {
        "id": PROVIDER, "type": "chatgpt", "enabled": True,
        "name": "ChatGPT", "customHeaders": [],
        "models": [{"id": MODEL, "modelId": "gpt-5", "displayName": "GPT",
                    "type": "CHAT", "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"], "abilities": []}],
        "credential": {
            "accessToken": "access-fixture", "refreshToken": "refresh-fixture",
            "expiresAt": 1_800_000_000_000, "accountId": "acct",
            "email": "user@example.com", "plan": "plus",
        },
    }
    source = _rh_settings_source(tmp_path, [provider], [{
        "id": ASSISTANT, "name": "A", "chatModelId": MODEL,
        "enableMemory": False, "localTools": [], "customHeaders": [],
        "customBodies": [], "presetMessages": [],
    }])
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        row = con.execute(
            "SELECT payload FROM provider_rows WHERE provider_key=?",
            (PROVIDER,)).fetchone()
        payload = json.loads(row[0])
        assert payload["oauthProvider"] == "chatgpt"
        assert payload["useResponseApi"] is True
        assert payload["baseUrl"] == "https://chatgpt.com/backend-api/codex"
        credentials = payload["oauthCredentials"]
        assert credentials["refreshToken"] == "refresh-fixture"
        assert credentials["requiresLogin"] is False
        assert credentials["sessionId"]
        assert "T" in credentials["expiresAt"] or "Z" in credentials["expiresAt"]
    finally:
        con.close()
    # Round-trip back to RikkaHub keeps the native account shape.
    back, _ = _convert(out, "rikkahub", tmp_path=tmp_path)
    with zipfile.ZipFile(back) as zf:
        settings_payload = json.loads(zf.read("settings.json"))
    provider_out = settings_payload["providers"][0]
    assert provider_out["type"] == "chatgpt"
    credential = provider_out["credential"]
    assert credential["refreshToken"] == "refresh-fixture"
    assert credential["expiresAt"] == 1_800_000_000_000
    assert credential["accountId"] == "acct"


def test_grok_signed_out_provider_keeps_models(tmp_path):
    provider = {
        "id": PROVIDER, "type": "grok", "enabled": True, "name": "Grok",
        "customHeaders": [], "credential": None,
        "models": [{"id": MODEL, "modelId": "grok-4", "displayName": "Grok",
                    "type": "CHAT", "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"], "abilities": []}],
    }
    source = _rh_settings_source(tmp_path, [provider], [{
        "id": ASSISTANT, "name": "A", "chatModelId": MODEL,
        "enableMemory": False, "localTools": [], "customHeaders": [],
        "customBodies": [], "presetMessages": [],
    }])
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        payload = json.loads(con.execute(
            "SELECT payload FROM provider_rows").fetchone()[0])
        assert payload["oauthProvider"] == "grok"
        assert "oauthCredentials" not in payload
        assert payload["models"] == ["grok-4"]
        assistant = json.loads(con.execute(
            "SELECT payload FROM assistant_rows").fetchone()[0])
        assert assistant["chatModelProvider"] == PROVIDER
    finally:
        con.close()


def test_requires_login_is_not_activated_in_rh(tmp_path):
    settings_payload = {
        "theme_v1": "dark",
        "provider_configs_v1": json.dumps({KPROVIDER: {
            "id": KPROVIDER, "enabled": True, "name": "ChatGPT",
            "apiKey": "", "oauthProvider": "chatgpt",
            "oauthCredentials": {
                "accessToken": "a", "refreshToken": "r",
                "expiresAt": "2030-01-01T00:00:00Z",
                "sessionId": "s", "requiresLogin": True,
            },
            "baseUrl": "https://chatgpt.com/backend-api/codex",
            "providerType": "openai", "useResponseApi": True,
            "models": ["gpt-5"], "modelOverrides": {},
            "customHeaders": [], "customBody": [],
        }}),
        "providers_order_v1": [KPROVIDER],
        "assistants_v1": json.dumps([{
            "id": KASSISTANT, "name": "A",
            "chatModelProvider": KPROVIDER, "chatModelId": "gpt-5",
            "enableMemory": False,
        }]),
    }
    source = _write_zip(tmp_path / "login.zip", {
        "settings.json": json.dumps(settings_payload).encode(),
    })
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        settings_out = json.loads(zf.read("settings.json"))
    provider = settings_out["providers"][0]
    assert provider["type"] == "chatgpt"
    assert provider["credential"] is None
    assert settings_out["assistants"][0]["chatModelId"] is not None


def test_legacy_rh_has_no_account_providers(tmp_path):
    provider = {
        "id": PROVIDER, "type": "chatgpt", "enabled": True,
        "name": "ChatGPT", "customHeaders": [],
        "models": [{"id": MODEL, "modelId": "gpt-5", "displayName": "GPT",
                    "type": "CHAT", "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"], "abilities": []}],
        "credential": {
            "accessToken": "a", "refreshToken": "r",
            "expiresAt": 1_800_000_000_000, "accountId": None,
            "email": None, "plan": None,
        },
    }
    source = _rh_settings_source(tmp_path, [provider], [{
        "id": ASSISTANT, "name": "A", "chatModelId": MODEL,
        "enableMemory": False, "localTools": [], "customHeaders": [],
        "customBodies": [], "presetMessages": [],
    }])
    out, plan = _convert(source, "rikkahub", legacy=True, tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        settings_payload = json.loads(zf.read("settings.json"))
    assert settings_payload["providers"] == []
    assistants = settings_payload["assistants"]
    assert assistants[0]["chatModelId"] is None
    details = " ".join(item.detail for item in plan.diagnostics)
    assert "账号型提供商" in details


def test_kelivo_feature_keys_and_presets(tmp_path):
    source = make_backup(tmp_path / "rh31.zip", app="rikkahub", kind="rh",
                         version=31)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        settings_json = json.loads(zf.read("settings.json"))
    assistants = json.loads(settings_json["assistants_v1"])
    assert isinstance(assistants[0]["presetMessages"], list)
    assert assistants[0]["presetMessages"][0]["content"] == "preset hello"
    assert assistants[0]["chatModelProvider"] == PROVIDER
    assert assistants[0]["chatModelId"] == "gpt-x"
    assert settings_json["title_model_v1"] == f"{PROVIDER}::gpt-x"
    assert settings_json["translate_prompt_v1"].startswith("Translate for")
    assert "{{" not in settings_json["translate_prompt_v1"]
    assert settings_json["per_chat_model_enabled_v1"] is True


# ---------------------------------------------------------------------------
# Settings-only and manifest consistency
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app,kind,version,schema", [
    ("rikkahub", "rh", 31, None),
    ("kelivo", "native", 3, 3),
])
def test_settings_only_never_creates_database(tmp_path, app, kind, version,
                                               schema):
    source = make_backup(tmp_path / "settings.zip", app=app, kind=kind,
                         version=version, schema_version=schema,
                         settings_only=True)
    for target_app in ("rikkahub", "kelivo", "cuplivo"):
        for legacy in (False, True):
            out, _plan = _convert(source, target_app, legacy=legacy,
                                  tmp_path=tmp_path)
            view = read_observables(out)
            assert view["database_present"] is False
            assert (view["conversations"], view["messages"]) == (0, 0)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                assert "rikka_hub.db" not in names
                assert "database/kelivo.db" not in names
                if "manifest.json" in names:
                    manifest = json.loads(zf.read("manifest.json"))
                    assert manifest["payloadKind"] == "settings-only"
                    assert "database" not in manifest


def test_native_manifest_business_ids_match_settings(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        settings_payload = json.loads(zf.read("settings.json"))
    assistants = json.loads(settings_payload["assistants_v1"])
    assert manifest["businessEntityRowIds"]["assistants_v1"] == [
        assistant["id"] for assistant in assistants]
    memory = json.loads(settings_payload["memory_entries_v1"])
    assert manifest["businessEntityRowIds"]["memory_entries_v1"] == [
        entry["id"] for entry in memory]
    entries = manifest["entries"]
    assert "manifest.json" not in entries
    assert "settings.json" in entries
    assert manifest["includeFiles"] is True
    assert all(root in {"settings.json", "upload", "images", "avatars",
                        "fonts", "sessions", "skills", "workspaces",
                        "database"}
               for name in entries for root in [name.split("/", 1)[0]])


@pytest.mark.parametrize("version", schema.RH_VERSIONS)
def test_each_published_room_version_upgrades(tmp_path, version):
    source = make_backup(tmp_path / f"rh{version}.zip", app="rikkahub",
                         kind="rh", version=version)
    out, _plan = _convert(source, "rikkahub", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["schema_version"] == 31
    assert (view["conversations"], view["messages"]) == (2, 6)
    assert view["selected_text"] == "answer-v2"


@pytest.mark.parametrize("schema_version", schema.DRIFT_VERSIONS)
def test_archive_schema_versions_are_readable(tmp_path, schema_version):
    archive_version = 2 if schema_version < 3 else 3
    source = make_backup(tmp_path / f"native{schema_version}.zip",
                         app="kelivo", kind="native", version=archive_version,
                         schema_version=schema_version)
    out, _plan = _convert(source, "kelivo", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["schema_version"] == 3
    assert (view["conversations"], view["messages"]) == (2, 6)
    assert view["selected_text"] == "answer-v2"


def test_kelivo_to_cuplivo_native_preserves_tables_and_files(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, _plan = _convert(source, "cuplivo", tmp_path=tmp_path)
    view = read_observables(out)
    assert view["manifest_ok"] is True
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        db = tmp_path / "native-out.db"
        db.write_bytes(zf.read("database/kelivo.db"))
        settings_payload = json.loads(zf.read("settings.json"))
    assert "skills/fixture-skill/SKILL.md" in names
    assert "workspaces/workspace-1/files/notes.txt" in names
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM memory_entry_rows").fetchone()[0] >= 1
        assert con.execute(
            "SELECT COUNT(*) FROM extension_entity_rows").fetchone()[0] >= 2
        conversation = con.execute(
            "SELECT extras_json FROM conversation_rows WHERE id='conv-1'"
        ).fetchone()[0]
        assert "workspace.id" in conversation
        assert "per_chat_model_enabled_v1" in settings_payload
    finally:
        con.close()


def test_cuplivo_proactive_and_manual_compress_are_pruned(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="cuplivo",
                         kind="native", version=3, schema_version=3)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    settings_payload = json.loads(members["settings.json"])
    settings_payload["manual_compress_quality_v1"] = 80
    settings_payload["proactive_care_decision_model_v1"] = "model"
    assistants = json.loads(settings_payload["assistants_v1"])
    assistants[0]["enableProactiveCare"] = True
    assistants[0]["proactiveCarePrompt"] = "check in"
    settings_payload["assistants_v1"] = json.dumps(assistants)
    members["settings.json"] = json.dumps(settings_payload).encode()
    manifest = json.loads(members["manifest.json"])
    manifest["entries"]["settings.json"] = {
        "bytes": len(members["settings.json"]),
        "sha256": __import__("hashlib").sha256(
            members["settings.json"]).hexdigest(),
    }
    members["manifest.json"] = json.dumps(manifest).encode()
    source = _write_zip(tmp_path / "cuplivo-prune.zip", members)
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        output_settings = json.loads(zf.read("settings.json"))
        db = tmp_path / "pruned.db"
        db.write_bytes(zf.read("database/kelivo.db"))
    assert "manual_compress_quality_v1" not in output_settings
    assert "proactive_care_decision_model_v1" not in output_settings
    output_assistants = json.loads(output_settings["assistants_v1"])
    assert "enableProactiveCare" not in output_assistants[0]
    con = sqlite3.connect(db)
    try:
        keys = {row[0] for row in con.execute(
            "SELECT key FROM preference_rows")}
        assert "manual_compress_quality_v1" not in keys
        payload = json.loads(con.execute(
            "SELECT payload FROM assistant_rows").fetchone()[0])
        assert "enableProactiveCare" not in payload
    finally:
        con.close()
    assert any(item.category == "cuplivo semantics"
               for item in plan.diagnostics)


def test_malformed_rh_message_json_is_rejected(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    db_path = tmp_path / "bad.db"
    db_path.write_bytes(members["rikka_hub.db"])
    con = sqlite3.connect(db_path)
    con.execute("UPDATE message_node SET messages='not-json'")
    con.commit()
    con.close()
    members["rikka_hub.db"] = db_path.read_bytes()
    broken = _write_zip(tmp_path / "bad-message.zip", members)
    with zipfile.ZipFile(broken) as zf:
        with pytest.raises(archive.ArchiveError):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "stage")


def test_room_identity_mismatch_is_rejected(tmp_path):
    source = make_backup(tmp_path / "rh.zip", app="rikkahub", kind="rh",
                         version=31)
    with zipfile.ZipFile(source) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    db_path = tmp_path / "bad-identity.db"
    db_path.write_bytes(members["rikka_hub.db"])
    con = sqlite3.connect(db_path)
    con.execute("UPDATE room_master_table SET identity_hash='deadbeef'")
    con.commit()
    con.close()
    members["rikka_hub.db"] = db_path.read_bytes()
    broken = _write_zip(tmp_path / "bad-identity.zip", members)
    with zipfile.ZipFile(broken) as zf:
        with pytest.raises(archive.ArchiveError, match="identity"):
            archive.validate_format(zf, archive.detect_archive(zf),
                                    tmp_path / "stage")


def test_group_lineages_are_split_for_kelivo_native_target(tmp_path):
    source = make_backup(tmp_path / "group.zip", app="cuplivo",
                         kind="json", version=2, variant="group")
    out, plan = _convert(source, "kelivo", tmp_path=tmp_path)
    con = _staged_db(out, tmp_path)
    try:
        rows = con.execute(
            "SELECT id, group_id, version, sender_id FROM message_rows "
            "WHERE conversation_id='conv-1' ORDER BY message_order"
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        assert by_id["k-a2"][1] != by_id["k-a0"][1]
        assert by_id["k-a2"][1] != by_id["k-a5"][1]
        assert by_id["k-a2"][3] == KASSISTANT
        selections = json.loads(con.execute(
            "SELECT version_selections_json FROM conversation_rows "
            "WHERE id='conv-1'").fetchone()[0])
        assert by_id["k-a2"][1] in selections
    finally:
        con.close()
    assert any(item.category == "group lineage"
               for item in plan.diagnostics)


def test_iso_fractions_with_other_precision_parse(tmp_path):
    from rikkelivo.times import iso_to_ms, iso_to_utc_z

    nine = "2026-01-01T00:00:00.123456789Z"
    one = "2026-01-01T00:00:00.5Z"
    assert iso_to_ms(nine) is not None
    assert iso_to_ms(one) is not None
    assert iso_to_utc_z(nine) is not None
    assert iso_to_utc_z(one) is not None


def test_native_receipts_and_no_streaming_sidecars(tmp_path):
    source = make_backup(tmp_path / "native.zip", app="kelivo",
                         kind="native", version=3, schema_version=3)
    out, _plan = _convert(source, "cuplivo", tmp_path=tmp_path)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        db = tmp_path / "receipts.db"
        db.write_bytes(zf.read("database/kelivo.db"))
    assert not any(name.endswith(("-wal", "-shm", "-journal"))
                   for name in names)
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT key, value FROM chat_storage_meta_rows "
            "WHERE key IN ('hive_migration_complete_v1',"
            " 'business_migration_complete_v1', 'active_streaming_ids')"
        ).fetchall()
        assert ("hive_migration_complete_v1", "true") in rows
        assert ("business_migration_complete_v1", "true") in rows
        assert all(key != "active_streaming_ids" for key, _ in rows)
        assert con.execute(
            "SELECT COUNT(*) FROM message_rows WHERE is_streaming != 0"
        ).fetchone()[0] == 0
    finally:
        con.close()
