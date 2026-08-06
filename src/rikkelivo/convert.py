"""Bidirectional backup conversion between RikkaHub and Kelivo formats."""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import sqlite3
import tempfile
import uuid as _uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from rikkelivo import kelivo, rikkahub
from rikkelivo import settings as settings_bridge
from rikkelivo.style import comment, ok, spinner, warn
from rikkelivo.times import iso_to_ms, ms_to_iso

_UPLOAD_PREFIX = "upload/"


def _summary_line(items: tuple[tuple[str, int], ...]) -> str:
    parts = [f"{count} {name}" for name, count in items if count]
    return "、".join(parts) if parts else "（无数据）"


def _list_skills(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in zf.infolist()
            if i.filename.startswith("skills/") and not i.is_dir()]


def _list_uploads(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in zf.infolist()
            if i.filename.startswith(_UPLOAD_PREFIX) and not i.is_dir()]


def _list_managed(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Kelivo managed asset entries outside upload/ (images/, avatars/).

    Message attachment markers can point into these directories (MCP tool
    images land in images/), so their bytes must be carried into the
    RikkaHub backup as well; they are rebased onto upload/ on the way.
    """
    return [i for i in zf.infolist()
            if (i.filename.startswith("images/")
                or i.filename.startswith("avatars/"))
            and not i.is_dir()]


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _uploads_size(uploads: list[zipfile.ZipInfo]) -> str:
    from rikkelivo.style import human_size
    return human_size(sum(i.file_size for i in uploads))


def _zip_mtime_ms(info: zipfile.ZipInfo) -> int:
    return int(_dt.datetime(*info.date_time).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Kelivo -> RikkaHub
# ---------------------------------------------------------------------------

def _build_nodes(
    conv: dict[str, Any],
    msgs_by_id: dict[str, dict[str, Any]],
    conv_title: str,
    conv_update_ms: int | None,
    fts_rows: list[tuple[str, str, str, str, str, str]],
    model_by_key: dict[tuple[str, str], str],
    model_by_name: dict[str, str],
    speaker_names: dict[str, str],
    tool_events_by_msg: dict[str, list[dict[str, Any]]],
) -> tuple[list[tuple[str, int, list[dict[str, Any]], int]], bool]:
    """Group Kelivo messages into RikkaHub nodes.

    One node per Kelivo group (groupId, falling back to message id); messages
    inside a node are versions ordered by version.  select_index comes from
    the conversation's versionSelections (the selected version), defaulting to
    the last message, matching RikkaHub's own behavior.

    Returns (nodes, is_group) where each node is
    (node_id, node_index, rikkahub_messages, select_index).
    """
    is_group = conv.get("conversationKind") == "group"
    selections = conv.get("versionSelections") or {}
    if not isinstance(selections, dict):
        selections = {}

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mid in conv.get("messageIds") or []:
        m = msgs_by_id.get(mid)
        if m is not None and mid not in seen:
            ordered.append(m)
            seen.add(mid)
    for mid, m in msgs_by_id.items():  # orphans after referenced ones
        if mid not in seen:
            ordered.append(m)
            seen.add(mid)

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    by_gid: dict[str, list[dict[str, Any]]] = {}
    for m in ordered:
        gid = m.get("groupId") or m["id"]
        if gid not in by_gid:
            by_gid[gid] = []
            groups.append((gid, by_gid[gid]))
        by_gid[gid].append(m)

    nodes: list[tuple[str, int, list[dict[str, Any]], int]] = []
    for node_index, (gid, members) in enumerate(groups):
        members.sort(key=lambda m: m.get("version", 0) or 0)
        group_ctx = None
        if is_group:
            speaker = members[0].get("speakerAssistantId")
            if speaker:
                group_ctx = {"speaker_name": speaker_names.get(speaker, speaker)}
        rh_msgs = [
            kelivo.to_rikkahub_message(
                m,
                model_uuid=model_by_key.get(
                    (m.get("providerId") or "", m.get("modelId") or "")) or
                    model_by_name.get(m.get("modelId") or ""),
                group_ctx=group_ctx if m.get("role") == "assistant" else None,
                tool_events=tool_events_by_msg.get(m["id"]),
            )
            for m in members
        ]
        # select_index: the version selected by Kelivo, else the last one.
        selected_version = selections.get(gid)
        select_index = len(rh_msgs) - 1
        if selected_version is not None and isinstance(selected_version, int):
            for i, m in enumerate(members):
                if (m.get("version", 0) or 0) == selected_version:
                    select_index = i
                    break
        node_id = _node_id(conv, gid)
        nodes.append((node_id, node_index, rh_msgs, select_index))
        for m in rh_msgs:
            text = ""
            for p in m.get("parts", []):
                if p.get("type") == "text" and p.get("text"):
                    text = p["text"]
                    break
            fts_rows.append((text, node_id, m["id"], conv["id"], conv_title,
                             str(conv_update_ms) if conv_update_ms else ""))
    return nodes, is_group


def _node_id(conv: dict[str, Any], gid: str) -> str:
    """Deterministic node id from conversation + group so round trips are
    stable and group metadata survives RikkaHub rewrites of siblings."""
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                           f"rikkelivo-node:{conv['id']}:{gid}"))


def to_rikkahub(src: Path, out: Path) -> tuple[dict[str, Any], list[str]]:
    """Convert a Kelivo backup zip into a RikkaHub backup zip."""
    notes: list[str] = []
    with zipfile.ZipFile(src) as zf:
        uploads = _list_uploads(zf)
        managed = _list_managed(zf)
        chats_raw = _read_member(zf, "chats.json")
        if chats_raw is None:
            raise ValueError("not a Kelivo backup: missing chats.json")
        chats = kelivo.load_chats(chats_raw)
        kelivo_settings_raw = _read_member(zf, "settings.json")
        kelivo_settings = (json.loads(kelivo_settings_raw)
                           if kelivo_settings_raw else {})

        convs = chats.get("conversations") or []
        msgs = chats.get("messages") or []
        msgs_by_id = {m["id"]: m for m in msgs if isinstance(m, dict)
                      and m.get("id")}
        by_conv: dict[str, list[dict[str, Any]]] = {}
        for m in msgs_by_id.values():
            by_conv.setdefault(m.get("conversationId") or "", []).append(m)

        model_by_key = settings_bridge.build_model_maps_k2r(kelivo_settings)
        model_by_name: dict[str, str] = {}
        for (_key, name), mid in model_by_key.items():
            model_by_name.setdefault(name, mid)

        tool_events_raw = chats.get("toolEvents") or {}
        tool_events_by_msg: dict[str, list[dict[str, Any]]] = {}
        if isinstance(tool_events_raw, dict):
            for mid, events in tool_events_raw.items():
                if isinstance(events, list):
                    tool_events_by_msg[str(mid)] = [
                        e for e in events if isinstance(e, dict)]

        # Group chat speaker names from assistants_v1.
        speaker_names: dict[str, str] = {}
        for a in settings_bridge._parse_json_str(
                kelivo_settings.get("assistants_v1")) or []:
            if isinstance(a, dict) and a.get("id"):
                speaker_names[str(a["id"])] = str(a.get("name") or a["id"])

        group_chats = chats.get("groupChats") or []
        group_members = chats.get("groupMembers") or []
        group_meta_by_conv: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for g in group_chats:
            if isinstance(g, dict) and g.get("conversationId"):
                members = [m for m in group_members
                           if isinstance(m, dict)
                           and m.get("groupChatId") == g.get("id")]
                group_meta_by_conv[str(g["conversationId"])] = (g, members)

        total_msgs = 0
        total_nodes = 0
        fts_rows: list[tuple[str, str, str, str, str, str]] = []
        group_conv_titles: list[str] = []
        n_conv_mcp = 0
        n_conv_trunc = 0
        n_conv_summary = 0

        skills = _list_skills(zf)
        skills_count = len(skills)
        with tempfile.TemporaryDirectory(prefix="rikkelivo-") as tmp:
            db_path = Path(tmp) / "rikka_hub.db"
            con, _fts_warning = rikkahub.create_database(str(db_path))
            try:
                for c in convs:
                    conv_msgs = [m for m in by_conv.get(c["id"], [])]
                    msgs_by_conv_id = {m["id"]: m for m in conv_msgs}
                    create_ms = iso_to_ms(c.get("createdAt"))
                    update_ms = iso_to_ms(c.get("updatedAt"))
                    rikkahub.write_conversation(con, {
                        "id": c["id"],
                        "assistant_id": c.get("assistantId") or
                            kelivo_settings.get("assistantId") or "",
                        "title": c.get("title") or "",
                        "nodes": "[]",
                        "create_at": create_ms or 0,
                        "update_at": update_ms or 0,
                        "suggestions": json.dumps(
                            c.get("chatSuggestions") or [], ensure_ascii=False),
                        "is_pinned": 1 if c.get("isPinned") else 0,
                    })
                    nodes, is_group = _build_nodes(
                        c, msgs_by_conv_id, c.get("title") or "", update_ms,
                        fts_rows, model_by_key, model_by_name, speaker_names,
                        tool_events_by_msg)
                    if is_group:
                        group_conv_titles.append(c.get("title") or c["id"])
                        meta = group_meta_by_conv.get(c["id"])
                        if meta and nodes:
                            # Attach group metadata to the first message.
                            gchat, members = meta
                            first = nodes[0][2]
                            if first:
                                extra = first[0].setdefault(
                                    "x_rikkelivo", {})
                                extra["kind"] = "group"
                                extra.setdefault("groupChat", gchat)
                                extra.setdefault("groupMembers", members)
                    for node_id, node_index, node_msgs, select_index in nodes:
                        rikkahub.write_node(con, node_id, c["id"],
                                            node_index, node_msgs,
                                            select_index)
                    total_nodes += len(nodes)
                    total_msgs += len(conv_msgs)
                    if isinstance(c.get("mcpServerIds"), list) \
                            and c["mcpServerIds"]:
                        n_conv_mcp += 1
                    if isinstance(c.get("truncateIndex"), int) \
                            and c["truncateIndex"] >= 0:
                        n_conv_trunc += 1
                    if (c.get("summary") or "").strip():
                        n_conv_summary += 1

                conv_loss = []
                if n_conv_mcp:
                    conv_loss.append(f"{n_conv_mcp} 个会话的 MCP 服务关联")
                if n_conv_trunc:
                    conv_loss.append(f"{n_conv_trunc} 个会话的上下文截断标记")
                if n_conv_summary:
                    conv_loss.append(f"{n_conv_summary} 个会话的摘要")
                if conv_loss:
                    notes.append("⚠ 以下会话字段在 RikkaHub 中无对应列，"
                                 "未转换：" + "、".join(conv_loss))

                for info in sorted(uploads, key=lambda i: i.filename):
                    data = zf.read(info.filename)
                    name = PurePosixPath(info.filename).name
                    ts = _zip_mtime_ms(info)
                    rikkahub.write_managed_file(
                        con, "upload", info.filename, name,
                        mimetypes.guess_type(name)[0]
                        or "application/octet-stream",
                        len(data), ts, ts)
                for info in sorted(managed, key=lambda i: i.filename):
                    data = zf.read(info.filename)
                    rebased = kelivo._rebased_upload_name(info.filename)
                    name = PurePosixPath(info.filename).name
                    ts = _zip_mtime_ms(info)
                    rikkahub.write_managed_file(
                        con, "upload", rebased, name,
                        mimetypes.guess_type(name)[0]
                        or "application/octet-stream",
                        len(data), ts, ts)
                rikkahub.write_fts(con, fts_rows)
                # Kelivo assistant memories -> RikkaHub MemoryEntity.
                memories_raw = kelivo_settings.get("assistant_memories_v1")
                imported_memories = 0
                if isinstance(memories_raw, str):
                    try:
                        parsed = json.loads(memories_raw)
                    except (ValueError, TypeError):
                        parsed = None
                    if isinstance(parsed, list):
                        for m in parsed:
                            if not isinstance(m, dict):
                                continue
                            con.execute(
                                "INSERT INTO MemoryEntity (assistant_id,"
                                " content) VALUES (?, ?)",
                                (settings_bridge._entity_uuid(
                                    m.get("assistantId"), "assistant"),
                                 str(m.get("content") or "")))
                            imported_memories += 1

                con.commit()
            finally:
                con.close()
            rh_settings = _zip_rikkahub(out, db_path, uploads, managed, zf,
                                         kelivo_settings, len(convs), notes)

    def _rh_len(value) -> int:
        return len(value) if isinstance(value, list) else 0

    summary = _summary_line((
        ("会话", len(convs)),
        ("消息", total_msgs),
        ("节点", total_nodes),
        ("上传文件", len(uploads)),
        ("提供商", _rh_len(rh_settings.get("providers"))),
        ("助手", _rh_len(rh_settings.get("assistants"))),
        ("助手记忆", imported_memories),
        ("MCP 服务", _rh_len(rh_settings.get("mcpServers"))),
        ("搜索服务", _rh_len(rh_settings.get("searchServices"))),
        ("TTS 服务", _rh_len(rh_settings.get("ttsProviders"))),
        ("快捷短语", _rh_len(rh_settings.get("quickMessages"))),
        ("提示注入", _rh_len(rh_settings.get("modeInjections"))),
        ("世界书", _rh_len(rh_settings.get("lorebooks"))),
        ("技能", skills_count),
    ))
    return summary, notes


def _zip_rikkahub(out: Path, db_path: Path,
                  uploads: list[zipfile.ZipInfo],
                  managed: list[zipfile.ZipInfo],
                  src_zf: zipfile.ZipFile,
                  kelivo_settings: dict[str, Any],
                  n_convs: int, notes: list[str]) -> dict[str, Any]:
    rh_settings, set_notes = settings_bridge.build_rikkahub_settings(
        kelivo_settings)
    notes.extend(set_notes)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
        zo.writestr("settings.json",
                    json.dumps(rh_settings, ensure_ascii=False, indent=1))
        with spinner("Writing rikka_hub.db"):
            zo.write(db_path, "rikka_hub.db")
        for info in uploads:
            zo.writestr(info.filename, src_zf.read(info.filename))
        # images//avatars/ assets are rebased onto upload/ (RikkaHub's
        # restore writes upload/ entries flat and skips other directories).
        for info in managed:
            zo.writestr(kelivo._rebased_upload_name(info.filename),
                        src_zf.read(info.filename))
        for info in _list_skills(src_zf):
            zo.writestr(info.filename, src_zf.read(info.filename))
    return rh_settings



# ---------------------------------------------------------------------------
# RikkaHub -> Kelivo
# ---------------------------------------------------------------------------

def to_kelivo(src: Path, out: Path) -> tuple[dict[str, Any], list[str]]:
    """Convert a RikkaHub backup zip into a Kelivo backup zip."""
    notes: list[str] = []
    tool_events_by_msg: dict[str, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(src) as zf:
        uploads = _list_uploads(zf)
        skills = _list_skills(zf)
        skills_count = len(skills)
        rh_settings_raw = _read_member(zf, "settings.json")
        rh_settings = json.loads(rh_settings_raw) if rh_settings_raw else {}
        model_names = settings_bridge.build_model_maps_r2k(rh_settings)

        with tempfile.TemporaryDirectory(prefix="rikkelivo-") as tmp:
            tmpd = Path(tmp)
            (tmpd / "rikka_hub.db").write_bytes(zf.read("rikka_hub.db"))
            for suffix in ("-wal", "-shm"):
                data = _read_member(zf, f"rikka_hub.db{suffix}")
                if data is not None:
                    (tmpd / f"rikka_hub.db{suffix}").write_bytes(data)

            con = sqlite3.connect(str(tmpd / "rikka_hub.db"))
            try:
                convs = rikkahub.load_conversations(con)
                nodes = rikkahub.load_nodes(con)
                con.row_factory = sqlite3.Row
                memories = [dict(r) for r in con.execute(
                    "SELECT id, assistant_id, content FROM MemoryEntity")]
            finally:
                con.close()

        kconvs: list[dict[str, Any]] = []
        kmsgs: list[dict[str, Any]] = []
        group_chats: list[dict[str, Any]] = []
        group_members: list[dict[str, Any]] = []
        seen_groups: set[str] = set()
        n_meta_prompt = 0
        n_meta_injection = 0
        n_meta_lorebook = 0
        n_meta_workspace = 0
        n_meta_folder = 0

        for c in convs:
            conv_nodes = sorted(
                (n for n in nodes if n["conversation_id"] == c["id"]),
                key=lambda n: n["node_index"])
            message_ids: list[str] = []
            version_selections: dict[str, int] = {}
            conv_kind = "normal"

            for node in conv_nodes:
                gid = node["id"]
                group_msgs: list[dict[str, Any]] = []
                for idx, m in enumerate(node["messages"]):
                    extra = m.get("x_rikkelivo")
                    if isinstance(extra, dict):
                        if extra.get("kind") == "group":
                            conv_kind = "group"
                        gchat = extra.get("groupChat")
                        members = extra.get("groupMembers")
                        if isinstance(gchat, dict) and isinstance(members, list):
                            if gchat.get("id") not in seen_groups:
                                group_chats.append(gchat)
                                group_members.extend(
                                    x for x in members if isinstance(x, dict))
                                seen_groups.add(gchat.get("id"))
                    km, events, _dropped = rikkahub.to_kelivo_message(
                        m, c["id"],
                        group_id=gid,
                        version=idx,
                        model_name=model_names.get(m.get("modelId") or ""),
                    )
                    if events:
                        tool_events_by_msg[km["id"]] = events
                    group_msgs.append(km)
                    kmsgs.append(km)
                # versionSelections is keyed by the group id that messages
                # actually carry (the original Kelivo group id survives in
                # x_rikkelivo), so selections round-trip.
                sel_key = gid
                if group_msgs and group_msgs[0].get("groupId") != gid:
                    sel_key = group_msgs[0]["groupId"]
                # Kelivo shows one message per group; version 0 is the
                # representative, the selected version via versionSelections.
                if group_msgs:
                    message_ids.append(group_msgs[0]["id"])
                if node.get("select_index", 0) and len(group_msgs) > 1:
                    sel = int(node["select_index"])
                    if 0 <= sel < len(group_msgs):
                        version_selections[sel_key] = sel

            kconvs.append(kelivo.build_conversation(
                c["id"], c["title"] or "",
                ms_to_iso(c.get("create_at")) or "",
                ms_to_iso(c.get("update_at")) or "",
                message_ids,
                c.get("assistant_id") or "",
                is_pinned=bool(c.get("is_pinned")),
                chat_suggestions=_parse_json_list(c.get("suggestions")),
                version_selections=version_selections,
            ))
            if conv_kind == "group":
                kconvs[-1]["conversationKind"] = "group"
            if (c.get("custom_system_prompt") or "").strip():
                n_meta_prompt += 1
            if _parse_json_list(c.get("mode_injection_ids")):
                n_meta_injection += 1
            if _parse_json_list(c.get("lorebook_ids")):
                n_meta_lorebook += 1
            if (c.get("workspace_cwd") or "").strip():
                n_meta_workspace += 1
            if (c.get("folder_id") or "").strip():
                n_meta_folder += 1

        meta_loss = []
        if n_meta_prompt:
            meta_loss.append(f"{n_meta_prompt} 个会话的自定义系统提示词")
        if n_meta_injection:
            meta_loss.append(f"{n_meta_injection} 个会话的模式注入")
        if n_meta_lorebook:
            meta_loss.append(f"{n_meta_lorebook} 个会话的世界书")
        if n_meta_workspace:
            meta_loss.append(f"{n_meta_workspace} 个会话的工作区路径")
        if n_meta_folder:
            meta_loss.append(f"{n_meta_folder} 个会话的文件夹归属")
        if meta_loss:
            notes.append("⚠ 以下会话字段在 Kelivo 中无对应列，未转换："
                         + "、".join(meta_loss))

        doc = kelivo.build_document(kconvs, kmsgs)
        if tool_events_by_msg:
            doc["toolEvents"] = tool_events_by_msg
        if group_chats:
            doc["groupChats"] = group_chats
            doc["groupMembers"] = group_members
        kel_settings, set_notes = settings_bridge.build_kelivo_settings(
            rh_settings)
        notes.extend(set_notes)
        try:
            mcp_parsed = json.loads(kel_settings.get("mcp_servers_v1", "[]"))
        except (ValueError, TypeError):
            mcp_parsed = []
        if any(m.get("id") not in settings_bridge.KL_BUILTIN_MCP_IDS
               for m in mcp_parsed if isinstance(m, dict)):
            notes.append("⚠ 由于 Kelivo 的 BUG，导入备份前请先手动禁用所有的 "
                         "MCP 服务器，否则将造成 MCP 服务器设置无法正常导入")
        if memories:
            kel_settings["assistant_memories_v1"] = json.dumps([
                {"id": m["id"],
                 "assistantId": settings_bridge._entity_uuid(
                     m["assistant_id"], "assistant"),
                 "content": m["content"]}
                for m in memories
            ], ensure_ascii=False)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            with spinner("Writing chats.json"):
                zo.writestr("chats.json", kelivo.dump_chats(doc))
            zo.writestr("settings.json",
                        json.dumps(kel_settings, ensure_ascii=False, indent=1))
            zo.writestr("deleted.json", kelivo.dump_deleted())
            for info in uploads:
                zo.writestr(info.filename, zf.read(info.filename))
            for info in skills:
                zo.writestr(info.filename, zf.read(info.filename))

        if skills:
            # Kelivo's restore never copies the skills/ directory back (its
            # backup writes it, but restore only handles upload/images/
            # avatars/fonts/workspaces), so ship a standalone skills zip for
            # the in-app manual import (skills page -> pick zip).
            skills_zip = out.with_name(f"{out.stem}.skills.zip")
            with zipfile.ZipFile(skills_zip, "w", zipfile.ZIP_DEFLATED) as sz:
                for info in skills:
                    sz.writestr(info.filename, zf.read(info.filename))
            notes.append(f"⚠ 由于 Kelivo 暂不支持通过备份恢复 Skills，"
                         f"rikkelivo 已将 Skills 单独导出为一个压缩包"
                         f"（{skills_zip}），可在 Kelivo 的 Skills 页面"
                         f"从压缩包导入")

    total_msgs = len(kmsgs)

    def _kl_len(key: str) -> int:
        value = kel_settings.get(key, "[]")
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            return len(parsed) if isinstance(parsed, (list, dict)) else 0
        except (ValueError, TypeError):
            return 0

    summary = _summary_line((
        ("会话", len(kconvs)),
        ("消息", total_msgs),
        ("工具调用记录", sum(len(v) for v in tool_events_by_msg.values())),
        ("上传文件", len(uploads)),
        ("提供商", _kl_len("provider_configs_v1")),
        ("助手", _kl_len("assistants_v1")),
        ("助手记忆", _kl_len("assistant_memories_v1")),
        ("MCP 服务", max(0, _kl_len("mcp_servers_v1") - len(
            settings_bridge.KL_BUILTIN_MCPS))),
        ("搜索服务", _kl_len("search_services_v1")),
        ("TTS 服务", _kl_len("tts_services_v1")),
        ("快捷短语", _kl_len("quick_phrases_v1")),
        ("提示注入", _kl_len("instruction_injections_v1")),
        ("世界书", _kl_len("world_books_v1")),
        ("技能", skills_count),
    ))
    return summary, notes


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []
