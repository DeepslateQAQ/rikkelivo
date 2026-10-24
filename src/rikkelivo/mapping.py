"""Target projection: source native payload -> target native payload.

The projection is where a backup stops being "what the source wrote" and
becomes "what the target can import".  It decides native-to-native migration
versus family translation, converts settings exactly once, allocates the final
attachment names, and accounts for every category the target cannot express.
No hidden fields and no sidecar packages are produced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

from rikkelivo import kelivo, model, rikkahub, settings as settings_bridge
from rikkelivo.model import (
    Asset,
    Backup,
    Conv,
    Diagnostic,
    KelivoPayload,
    MemoryDoc,
    Msg,
    RikkaPayload,
    Snapshot,
    TargetProfile,
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_HELPER_ROOTS = ("upload", "images", "avatars", "fonts", "sessions")


def project_backup(backup: Backup, target: TargetProfile
                   ) -> tuple[Backup, tuple[Diagnostic, ...]]:
    diagnostics: list[Diagnostic] = list(backup.diagnostics)
    source_family = backup.family
    target_family = model.APP_FAMILY[target.app]
    if source_family == "rikkahub" and target_family == "rikkahub":
        payload, extra = rikkahub.migrate_native(backup, target)
        diagnostics.extend(extra)
        assets = list(backup.assets)
        if target.schema_version is not None and target.schema_version < 26:
            kept = [asset for asset in assets
                    if not asset.entry.startswith("media_creation/")]
            if len(kept) != len(assets):
                diagnostics.append(Diagnostic(
                    "unsupported_data", "media creation",
                    len(assets) - len(kept),
                    "legacy RikkaHub 没有媒体创作库，相关文件未迁移"))
            assets = kept
        projected = Backup(
            info=backup.info,
            payload=payload,
            assets=assets,
            diagnostics=model.merge_diagnostics(diagnostics),
            roots=backup.roots,
            incremental=backup.incremental,
        )
        return projected, model.merge_diagnostics(diagnostics)
    if (source_family == "kelivo" and target_family == "kelivo"
            and _is_archive(backup) and target.kind == model.KIND_KELIVO_ARCHIVE):
        payload, extra = kelivo.migrate_native(backup, target)
        diagnostics.extend(extra)
        assets, dropped_assets = _filter_scope_assets(
            backup.assets, backup.info.scope)
        if dropped_assets:
            diagnostics.append(Diagnostic(
                "unsupported_data", "file", dropped_assets,
                "不在源备份范围内的文件未迁移"))
        projected = Backup(
            info=backup.info,
            payload=payload,
            assets=assets,
            diagnostics=model.merge_diagnostics(diagnostics),
            roots=backup.roots,
            incremental=backup.incremental,
        )
        return projected, model.merge_diagnostics(diagnostics)
    snapshot = _decode(backup)
    diagnostics.extend(snapshot.diagnostics)
    snapshot.scope = backup.info.scope
    if source_family == "kelivo" and target_family == "kelivo":
        snapshot.memory_entries = _entries_from_settings(backup)
    _filter_scope(snapshot, backup)
    split_needed = (
        target_family == "rikkahub"
        or target.kind == model.KIND_KELIVO_ARCHIVE
        or (target.kind == model.KIND_KELIVO_JSON and target.app == "kelivo"))
    if split_needed:
        splits = _split_lineages(snapshot)
        if splits:
            diagnostics.append(Diagnostic(
                "unsupported_data", "group lineage", splits,
                "并行群聊分支已拆成独立版本线"))
    if target_family == "rikkahub" or (
            target.kind == model.KIND_KELIVO_JSON
            and target.app == "kelivo"):
        _decorate_group_messages(snapshot, backup, diagnostics)
    _apply_context_boundary(snapshot, target, diagnostics)
    asset_plan = _plan_assets(backup, snapshot, target, diagnostics)
    _rewrite_part_uris(snapshot, backup, target, asset_plan, diagnostics)
    settings_result = settings_bridge.convert_settings(backup, target)
    diagnostics.extend(settings_result.diagnostics)
    if target_family == "rikkahub":
        snapshot.memory_docs = list(settings_result.memory_docs)
        snapshot.memory_entries = []
        _remap_rikkahub_ids(snapshot, settings_result)
    else:
        snapshot.memory_entries = list(settings_result.memory_entries)
        if target.app == "kelivo":
            settings_result.settings = kelivo._prune_cuplivo_semantics(
                settings_result.settings, diagnostics)
    _extra_independent(snapshot, backup, target, diagnostics)
    if target_family == "rikkahub":
        payload, extra = rikkahub.encode_from_snapshot(
            snapshot, target, settings_result, asset_plan.assets)
    else:
        if target.kind == model.KIND_KELIVO_JSON:
            payload, extra = kelivo.encode_legacy(
                snapshot, target, settings_result, asset_plan.assets)
        else:
            payload, extra = kelivo.encode_snapshot(
                snapshot, target, settings_result, asset_plan.assets)
    diagnostics.extend(extra)
    snapshot.counts = _counts(payload)
    projected = Backup(
        info=backup.info,
        payload=payload,
        assets=asset_plan.assets,
        diagnostics=model.merge_diagnostics(diagnostics),
        roots=tuple(sorted({asset.entry.split("/")[0]
                            for asset in asset_plan.assets})),
        incremental=backup.incremental,
    )
    return projected, model.merge_diagnostics(diagnostics)


def _is_archive(backup: Backup) -> bool:
    return backup.info.kind == model.KIND_KELIVO_ARCHIVE


def _decode(backup: Backup) -> Snapshot:
    if backup.family == "rikkahub":
        return rikkahub.decode(backup)
    return kelivo.decode(backup)


def _entries_from_settings(backup: Backup) -> list[model.MemoryEntry]:
    payload = backup.payload
    if not isinstance(payload, KelivoPayload):
        return []
    from rikkelivo.settings import _memory_entries_from_kelivo

    return _memory_entries_from_kelivo(payload.settings, backup)


def _filter_scope_assets(assets: list[Asset], scope
                         ) -> tuple[list[Asset], int]:
    if not scope:
        return list(assets), 0
    kept: list[Asset] = []
    dropped = 0
    for asset in assets:
        root = asset.entry.split("/", 1)[0]
        if root == "skills":
            category = "skills"
        elif root == "workspaces":
            category = "workspaces"
        elif root in _HELPER_ROOTS or root == "media_creation":
            category = "files"
        else:
            category = "files"
        if category in scope:
            kept.append(asset)
        else:
            dropped += 1
    return kept, dropped


def _filter_scope(snapshot: Snapshot, backup: Backup) -> None:
    scope = backup.info.scope
    if not scope:
        return
    if "chats" not in scope:
        snapshot.conversations = []
        snapshot.has_chat_domain = False
    if "memories" not in scope:
        snapshot.memory_docs = []
        snapshot.memory_entries = []
    if "workspaces" not in scope:
        snapshot.workspaces = []
    if "skills" not in scope:
        snapshot.skills = []
    if "files" not in scope:
        snapshot.asset_entries = [
            entry for entry in snapshot.asset_entries
            if not entry.startswith(_HELPER_ROOTS)]


def _assistant_names(backup: Backup) -> dict[str, str]:
    payload = backup.payload
    settings = payload.settings if isinstance(
        payload, (RikkaPayload, KelivoPayload)) else {}
    if backup.family == "rikkahub":
        return {str(a.get("id")): str(a.get("name") or "")
                for a in settings.get("assistants") or []
                if isinstance(a, dict) and a.get("id")}
    assistants = settings.get("assistants_v1")
    if isinstance(assistants, str):
        try:
            assistants = json.loads(assistants)
        except ValueError:
            assistants = []
    return {str(a.get("id")): str(a.get("name") or "")
            for a in assistants or []
            if isinstance(a, dict) and a.get("id")}


def _split_lineages(snapshot: Snapshot) -> int:
    """Give every (group, subgroup, speaker) lineage its own target group id."""
    splits = 0
    for conv in snapshot.conversations:
        lineage_keys = [(group, (group.group_id, group.subgroup_id,
                                 group.speaker_id))
                        for group in conv.groups]
        if not any(key[1] is not None or key[2] is not None
                   for _group, key in lineage_keys):
            continue
        splits += 1
        lineages = {
            key: rikkahub.entity_uuid(
                f"{conv.id}:{key[0]}:{key[1]}:{key[2]}", "lineage")
            for _group, key in lineage_keys
        }
        for group, key in lineage_keys:
            if key[1] is not None or key[2] is not None:
                group.group_id = lineages[key]
                group.lineage_split = True
        for message in conv.messages:
            key = (message.group_id or message.id, message.subgroup_id,
                   message.speaker_id)
            if key in lineages and (key[1] is not None or key[2] is not None):
                message.group_id = lineages[key]
    return splits


def _decorate_group_messages(snapshot: Snapshot, backup: Backup,
                             diagnostics: list[Diagnostic]) -> None:
    names = _assistant_names(backup)
    decorated = 0
    for conv in snapshot.conversations:
        if conv.conversation_kind != "group" and not any(
                message.speaker_id for message in conv.messages):
            continue
        for message in conv.messages:
            if message.role != "assistant" or not message.speaker_id:
                continue
            speaker = names.get(message.speaker_id) or message.speaker_id
            text_part = next((part for part in message.parts
                              if part.kind == "text"), None)
            if text_part is None:
                text_part = model.Part("text", text="")
                message.parts.insert(0, text_part)
            body = text_part.text or ""
            quoted = "\n".join(f"> {line}" for line in body.split("\n")) \
                if body else ">"
            text_part.text = f"> **{speaker}**:\n{quoted}"
            decorated += 1
        for message in conv.messages:
            message.speaker_id = None
            message.subgroup_id = None
        for group in conv.groups:
            group.subgroup_id = None
            group.speaker_id = None
        conv.conversation_kind = "normal"
    if decorated:
        diagnostics.append(Diagnostic(
            "unsupported_data", "group chat", decorated,
            "目标不支持群聊控制，消息正文已加入发送者引用"))


def _apply_context_boundary(snapshot: Snapshot, target: TargetProfile,
                            diagnostics: list[Diagnostic]) -> None:
    target_family = model.APP_FAMILY[target.app]
    approximated = 0
    unrepresentable = 0
    for conv in snapshot.conversations:
        if conv.truncate_slot is None:
            continue
        if target_family == "kelivo":
            approximated += 1
            continue
        if target.schema_version is not None and target.schema_version < 31:
            conv.truncate_slot = None
            unrepresentable += 1
            continue
        if conv.truncate_slot >= len(conv.groups):
            conv.truncate_slot = None
            unrepresentable += 1
            continue
        group = conv.groups[conv.truncate_slot]
        selected = next((message for message in conv.messages
                         if message.id == group.selected_message_id), None)
        if selected is None:
            conv.truncate_slot = None
            unrepresentable += 1
            continue
        selected.is_context_checkpoint = True
        approximated += 1
    if approximated:
        diagnostics.append(Diagnostic(
            "context_boundary_approximated", "context boundary",
            approximated,
            "上下文边界已按目标的消息检查点表达，不再随分支动态重算"))
    if unrepresentable:
        diagnostics.append(Diagnostic(
            "context_boundary_approximated", "context boundary",
            unrepresentable,
            "目标没有等价的消息检查点，边界状态无法表达，已保留全部可见历史"))


def _remap_rikkahub_ids(snapshot: Snapshot,
                        settings_result: model.SettingsResult) -> None:
    for conv in snapshot.conversations:
        conv.id = rikkahub.entity_uuid(conv.id, "conversation")
        target_assistant = settings_result.assistant_ids.get(
            conv.assistant_id or "")
        conv.assistant_id = target_assistant or rikkahub.DEFAULT_ASSISTANT_ID
        for group in conv.groups:
            group.conversation_id = conv.id
            old_group = group.group_id
            group.group_id = rikkahub.entity_uuid(old_group, "group")
            group.message_ids = [rikkahub.entity_uuid(item, "message")
                                 for item in group.message_ids]
            if group.selected_message_id:
                group.selected_message_id = rikkahub.entity_uuid(
                    group.selected_message_id, "message")
        for message in conv.messages:
            message.id = rikkahub.entity_uuid(message.id, "message")
            message.group_id = rikkahub.entity_uuid(
                message.group_id or message.id, "group")
            message.subgroup_id = None
            message.speaker_id = None
    for doc in snapshot.memory_docs:
        doc.assistant_id = settings_result.assistant_ids.get(
            doc.assistant_id, doc.assistant_id)
        if doc.assistant_id != "__global__":
            doc.assistant_id = rikkahub.entity_uuid(
                doc.assistant_id, "assistant")


def counts_for_backup(backup: Backup) -> dict[str, int]:
    counts = _counts(backup.payload)
    counts["files"] = len(backup.assets)
    return counts


def _counts(payload: RikkaPayload | KelivoPayload) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(payload, RikkaPayload):
        tables = payload.tables
        counts["conversations"] = _row_count(tables, "ConversationEntity")
        node_table = tables.get("message_node")
        counts["messages"] = sum(
            len(_messages_from_json(row))
            for row in (node_table.rows if node_table else []))
        counts["nodes"] = _row_count(tables, "message_node")
        counts["memory"] = _row_count(tables, "memory_file") + _row_count(
            tables, "MemoryEntity")
        counts["workspaces"] = _row_count(tables, "workspaces")
        counts["usage"] = _row_count(tables, "usage_record")
        counts["providers"] = len(payload.settings.get("providers") or [])
        counts["assistants"] = len(payload.settings.get("assistants") or [])
        return counts
    if payload.legacy_chat is not None:
        counts["conversations"] = len(payload.legacy_chat.conversations)
        counts["messages"] = len(payload.legacy_chat.messages)
        settings = payload.settings
        counts["assistants"] = len(_json_list(settings.get("assistants_v1")))
        counts["providers"] = len(_json_map(settings.get("provider_configs_v1")))
        counts["memory"] = len(_json_list(settings.get("memory_entries_v1")))
        return counts
    tables = payload.tables
    counts["conversations"] = _row_count(tables, "conversation_rows")
    counts["messages"] = _row_count(tables, "message_rows")
    counts["memory"] = max(
        _row_count(tables, "memory_entry_rows"),
        len(_json_list(payload.settings.get("memory_entries_v1"))))
    extension_table = tables.get("extension_entity_rows")
    counts["workspaces"] = sum(
        1 for row in (extension_table.rows if extension_table else [])
        if str(row[0]) == "workspace")
    settings = payload.settings
    counts["assistants"] = len(_json_list(settings.get("assistants_v1")))
    counts["providers"] = len(_json_map(settings.get("provider_configs_v1")))
    return counts


def _row_count(tables: dict[str, model.TableRows], table: str) -> int:
    rows = tables.get(table)
    return len(rows.rows) if rows else 0


def _messages_from_json(row: Iterable[Any]) -> list[Any]:
    values = list(row)
    if len(values) < 4:
        return []
    try:
        payload = json.loads(str(values[3]))
    except (ValueError, TypeError):
        return []
    return payload if isinstance(payload, list) else []


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _json_map(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@dataclass
class _AssetPlan:
    assets: list[Asset] = field(default_factory=list)
    #: source asset entry -> target asset entry
    entry_map: dict[str, str] = field(default_factory=dict)
    #: entries that were dropped, for diagnostics
    dropped: list[tuple[str, str]] = field(default_factory=list)


def _plan_assets(backup: Backup, snapshot: Snapshot, target: TargetProfile,
                 diagnostics: list[Diagnostic]) -> _AssetPlan:
    plan = _AssetPlan()
    target_family = model.APP_FAMILY[target.app]
    used_names: set[str] = set()
    skills_with_manifest: set[str] = set()
    for asset in backup.assets:
        if asset.entry.startswith("skills/"):
            parts = asset.entry.split("/")
            if len(parts) >= 3 and parts[2] == "SKILL.md":
                skills_with_manifest.add(parts[1])
    # Native same-family copy keeps every entry verbatim.
    if (backup.family == "kelivo" and target_family == "kelivo"
            and target.kind == model.KIND_KELIVO_ARCHIVE
            and backup.info.kind == model.KIND_KELIVO_ARCHIVE):
        plan.assets = list(backup.assets)
        plan.entry_map = {asset.entry: asset.entry for asset in backup.assets}
        return plan
    if (backup.family == "rikkahub" and target_family == "rikkahub"):
        plan.assets = list(backup.assets)
        plan.entry_map = {asset.entry: asset.entry for asset in backup.assets}
        return plan
    for asset in backup.assets:
        entry = asset.entry
        root = entry.split("/", 1)[0]
        if target_family == "rikkahub":
            if root == "sessions":
                plan.dropped.append((entry, "unsupported_root"))
                continue
            if root in _HELPER_ROOTS:
                if not _files_in_scope(backup, root):
                    plan.dropped.append((entry, "scope"))
                    continue
                mapped = _flat_upload_name(entry, used_names,
                                           allow_fonts=(root == "fonts"))
                if mapped is None:
                    plan.dropped.append((entry, "collision"))
                    continue
                plan.entry_map[entry] = mapped
                plan.assets.append(_with_entry(asset, mapped))
            elif root == "skills":
                if not _scope_allows(backup, "skills"):
                    plan.dropped.append((entry, "scope"))
                    continue
                if entry.split("/")[1] not in skills_with_manifest:
                    plan.dropped.append((entry, "no_manifest"))
                    continue
                plan.entry_map[entry] = entry
                plan.assets.append(asset)
            elif root == "workspaces":
                plan.dropped.append((entry, "workspace_files"))
            elif root == "media_creation":
                plan.entry_map[entry] = entry
                plan.assets.append(asset)
            else:
                plan.dropped.append((entry, "unsupported_root"))
            continue
        # Kelivo-family target.
        if root == "media_creation":
            plan.dropped.append((entry, "media_library"))
            continue
        if root not in kelivo.KELIVO_ASSET_ROOTS:
            plan.dropped.append((entry, "unsupported_root"))
            continue
        if root in _HELPER_ROOTS and not _files_in_scope(backup, root):
            plan.dropped.append((entry, "scope"))
            continue
        if root == "skills":
            if not _scope_allows(backup, "skills"):
                plan.dropped.append((entry, "scope"))
                continue
            parts = entry.split("/")
            if len(parts) < 3 or parts[1] not in skills_with_manifest:
                plan.dropped.append((entry, "no_manifest"))
                continue
        if root == "workspaces" and not _scope_allows(backup, "workspaces"):
            plan.dropped.append((entry, "scope"))
            continue
        mapped = entry
        if backup.family == "rikkahub" and root in ("upload", "images",
                                                    "avatars", "fonts"):
            mapped = _flat_kelivo_name(entry, used_names)
            if mapped is None:
                plan.dropped.append((entry, "collision"))
                continue
        elif mapped in used_names:
            mapped = _hashed_name(mapped, used_names)
            if mapped is None:
                plan.dropped.append((entry, "collision"))
                continue
        used_names.add(mapped)
        plan.entry_map[entry] = mapped
        plan.assets.append(_with_entry(asset, mapped))
    _report_dropped(plan, diagnostics)
    return plan


def _with_entry(asset: Asset, entry: str) -> Asset:
    return Asset(entry=entry, staged_path=asset.staged_path, size=asset.size,
                 sha256=asset.sha256, mtime=asset.mtime)


def _report_dropped(plan: _AssetPlan, diagnostics: list[Diagnostic]) -> None:
    counts: dict[str, int] = {}
    for _entry, reason in plan.dropped:
        counts[reason] = counts.get(reason, 0) + 1
    details = {
        "workspace_files": ("workspace_metadata_only", "workspace",
                            "工作区文件未迁移到 RikkaHub"),
        "media_library": ("unsupported_data", "media creation",
                          "媒体创作库在 Kelivo 中没有独立功能，文件未迁移"),
        "no_manifest": ("unsupported_data", "skill",
                        "缺少 SKILL.md 的技能目录未迁移"),
        "collision": ("unsupported_data", "file", "附件命名冲突，未迁移"),
        "scope": ("unsupported_data", "file", "不在源备份范围内的文件未迁移"),
        "unsupported_root": ("unsupported_data", "file",
                             "目标不支持的附件根目录"),
    }
    for reason, count in counts.items():
        code, category, detail = details.get(
            reason, ("unsupported_data", "file", "附件未迁移"))
        diagnostics.append(Diagnostic(code, category, count, detail))


def _scope_allows(backup: Backup, category: str) -> bool:
    scope = backup.info.scope
    return not scope or category in scope


def _files_in_scope(backup: Backup, root: str) -> bool:
    if root in ("skills",):
        return _scope_allows(backup, "skills")
    if root == "workspaces":
        return _scope_allows(backup, "workspaces")
    return _scope_allows(backup, "files")


def _safe_basename(entry: str) -> str:
    name = PurePosixPath(entry).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return name


def _hashed_name(entry: str, used: set[str]) -> str | None:
    digest = hashlib.sha256(entry.encode()).hexdigest()
    for length in (16, 64):
        candidate = f"upload/{digest[:length]}_{_safe_basename(entry)}"
        if candidate not in used:
            return candidate
    return None


def _flat_upload_name(entry: str, used: set[str],
                      *, allow_fonts: bool) -> str | None:
    digest = hashlib.sha256(entry.encode()).hexdigest()[:16]
    basename = _safe_basename(entry)
    folder = "fonts" if allow_fonts and entry.startswith("fonts/") else "upload"
    candidate = f"{folder}/{digest}_{basename}"
    if candidate in used:
        digest_full = hashlib.sha256(entry.encode()).hexdigest()
        candidate = f"{folder}/{digest_full}_{basename}"
        if candidate in used:
            return None
    used.add(candidate)
    return candidate


def _flat_kelivo_name(entry: str, used: set[str]) -> str | None:
    root = entry.split("/", 1)[0]
    basename = _safe_basename(entry)
    folder = "fonts" if root == "fonts" else "upload"
    candidate = f"{folder}/{basename}"
    if candidate in used:
        digest = hashlib.sha256(entry.encode()).hexdigest()[:16]
        candidate = f"{folder}/{digest}_{basename}"
        if candidate in used:
            return None
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# URI rewriting
# ---------------------------------------------------------------------------

def _source_entry_for_uri(uri: str, family: str) -> str | None:
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith(RH_FILES_PREFIX):
        return uri[len(RH_FILES_PREFIX):]
    entry = kelivo.parse_kelivo_uri(uri)
    if entry is not None:
        return entry
    return kelivo.normalize_managed_path(uri)


RH_FILES_PREFIX = "file:///data/user/0/me.rerere.rikkahub/files/"


def _target_uri(entry: str, target_family: str) -> str:
    if target_family == "rikkahub":
        return RH_FILES_PREFIX + entry
    return kelivo.kelivo_uri(entry)


def _rewrite_part_uris(snapshot: Snapshot, backup: Backup,
                       target: TargetProfile, plan: _AssetPlan,
                       diagnostics: list[Diagnostic]) -> None:
    target_family = model.APP_FAMILY[target.app]
    missing = 0
    unsupported = 0
    changed = 0

    def _rewrite(parts: list[model.Part]) -> None:
        nonlocal missing, unsupported, changed
        for part in parts:
            if part.output:
                _rewrite(part.output)
            if not part.uri:
                continue
            if part.uri.startswith(("http://", "https://", "data:")):
                continue
            entry = _source_entry_for_uri(part.uri, backup.family)
            if entry is None:
                unsupported += 1
                continue
            mapped = plan.entry_map.get(entry)
            if mapped is None:
                if any(source == entry for source, _reason in plan.dropped):
                    unsupported += 1
                    continue
                missing += 1
                if target_family == "kelivo":
                    part.unavailable = True
                continue
            part.uri = _target_uri(mapped, target_family)
            changed += 1
            if target_family == "kelivo":
                part.unavailable = False

    for conv in snapshot.conversations:
        for message in conv.messages:
            _rewrite(message.parts)
    if missing:
        diagnostics.append(Diagnostic(
            "source_file_missing", "attachment", missing,
            "源包中缺少这些附件的文件，已保留可读引用"))
    if unsupported:
        diagnostics.append(Diagnostic(
            "unsupported_data", "attachment", unsupported,
            "目标无法使用这些附件路径，引用保持原样"))


# ---------------------------------------------------------------------------
# Independent data categories
# ---------------------------------------------------------------------------

def _extra_independent(snapshot: Snapshot, backup: Backup,
                       target: TargetProfile,
                       diagnostics: list[Diagnostic]) -> None:
    target_family = model.APP_FAMILY[target.app]
    if target_family == "kelivo":
        media_sessions = _row_count(snapshot.media, "media_creation_session")
        media_records = _row_count(snapshot.media, "media_creation_record")
        if media_sessions or media_records:
            diagnostics.append(Diagnostic(
                "unsupported_data", "media creation",
                media_sessions + media_records,
                "目标没有独立的媒体创作库，仅报告丢弃"))
        if snapshot.translations is not None and snapshot.translations.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "translation history",
                len(snapshot.translations.rows),
                "独立翻译历史在目标中没有对应功能"))
        if snapshot.usage is not None and snapshot.usage.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "usage ledger",
                len(snapshot.usage.rows),
                "独立用量流水在目标中没有对应功能"))
        if snapshot.favorites is not None and snapshot.favorites.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "favorite",
                len(snapshot.favorites.rows),
                "收藏在目标中没有原生功能"))
        if snapshot.folders is not None and snapshot.folders.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "folder",
                len(snapshot.folders.rows),
                "会话文件夹在目标中没有原生功能"))
        if snapshot.gen_media is not None and snapshot.gen_media.rows:
            diagnostics.append(Diagnostic(
                "source_file_missing", "generated media",
                len(snapshot.gen_media.rows),
                "生成媒体记录没有对应文件，仅报告"))
        if snapshot.consolidation is not None and snapshot.consolidation.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "memory consolidation",
                len(snapshot.consolidation.rows),
                "记忆整理时间在目标中没有对应结构"))
        if snapshot.generation_runs is not None and snapshot.generation_runs.rows:
            diagnostics.append(Diagnostic(
                "unsupported_data", "generation run",
                len(snapshot.generation_runs.rows),
                "生成任务记录在目标中没有对应结构"))
        if snapshot.tombstones is not None and snapshot.tombstones.rows \
                and target.kind == model.KIND_KELIVO_JSON:
            diagnostics.append(Diagnostic(
                "unsupported_data", "tombstone",
                len(snapshot.tombstones.rows),
                "删除墓碑在历史 JSON 中没有对应结构"))
        annotations = sum(len(message.annotations)
                          for conv in snapshot.conversations
                          for message in conv.messages)
        if annotations:
            diagnostics.append(Diagnostic(
                "unsupported_data", "annotation", annotations,
                "Kelivo 没有消息注解"))
        step_counts = sum(1 for conv in snapshot.conversations
                          for message in conv.messages
                          if message.step_count is not None)
        if step_counts:
            diagnostics.append(Diagnostic(
                "unsupported_data", "step count", step_counts,
                "Kelivo 没有模型步数"))
        if snapshot.deleted_document:
            count = sum(len(value) for value in snapshot.deleted_document.values()
                        if isinstance(value, list))
            preserves_deleted = (target.kind == model.KIND_KELIVO_JSON
                                 and target.app == "cuplivo")
            if count and not preserves_deleted:
                diagnostics.append(Diagnostic(
                    "unsupported_data", "tombstone", count,
                    "历史 deleted.json 墓碑在目标中没有对应结构"))
        return
    artifact_kinds: dict[str, int] = {}
    annotations = 0
    step_counts = 0
    extra_fields = 0
    for conv in snapshot.conversations:
        for message in conv.messages:
            annotations += len(message.annotations)
            if message.step_count is not None:
                step_counts += 1
            for key in ("tokens.reasoning", "tokens.cacheWrite",
                        "timing.firstTokenMs"):
                if key in message.extras:
                    extra_fields += 1
            for artifact in message.artifacts:
                artifact_kinds[artifact.kind] = artifact_kinds.get(
                    artifact.kind, 0) + 1
    if artifact_kinds:
        diagnostics.append(Diagnostic(
            "unsupported_data", "provider artifact",
            sum(artifact_kinds.values()),
            "RikkaHub 没有 artifact 存储: "
            + ", ".join(sorted(artifact_kinds))))
    if annotations:
        diagnostics.append(Diagnostic(
            "unsupported_data", "annotation", annotations,
            "RikkaHub 没有消息注解"))
    if step_counts:
        diagnostics.append(Diagnostic(
            "unsupported_data", "step count", step_counts,
            "RikkaHub 没有模型步数"))
    if extra_fields:
        diagnostics.append(Diagnostic(
            "unsupported_data", "token detail", extra_fields,
            "RikkaHub 没有推理/缓存写入/首 token 细分"))
    if snapshot.generation_runs is not None and snapshot.generation_runs.rows:
        diagnostics.append(Diagnostic(
            "unsupported_data", "generation run",
            len(snapshot.generation_runs.rows),
            "Kelivo 生成任务记录在 RikkaHub 中没有对应结构"))
    if snapshot.tombstones is not None and snapshot.tombstones.rows:
        diagnostics.append(Diagnostic(
            "unsupported_data", "tombstone",
            len(snapshot.tombstones.rows),
            "Kelivo 删除墓碑在 RikkaHub 中没有对应结构"))
    if snapshot.deleted_document:
        count = sum(len(value) for value in snapshot.deleted_document.values()
                    if isinstance(value, list))
        if count:
            diagnostics.append(Diagnostic(
                "unsupported_data", "tombstone", count,
                "历史 deleted.json 墓碑在 RikkaHub 中没有对应结构"))


def _reconstruct_usage_count(snapshot: Snapshot) -> int:
    count = 0
    for conv in snapshot.conversations:
        for message in conv.messages:
            usage = message.usage
            if message.role == "assistant" and usage is not None and (
                    usage.prompt > 0 or usage.completion > 0):
                count += 1
    return count
