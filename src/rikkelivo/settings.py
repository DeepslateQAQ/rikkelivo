"""Single settings conversion for every supported direction.

The bridge produces the target's native settings payload *and* the identifier
maps the chat projection needs, in one pass: providers and models are paired by
(provider key, logical model id), assistants and services by their native ids,
and memory is projected under the target's own permission model.  A target
reference is only kept when it resolves; anything that cannot be expressed is
dropped and reported, never silently rebound to a same-named sibling.
"""

from __future__ import annotations

import json
import re
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any

from rikkelivo import model
from rikkelivo.model import (
    AssistantMemoryFlags,
    Backup,
    Diagnostic,
    KelivoPayload,
    MemoryDoc,
    MemoryEntry,
    RikkaPayload,
    SettingsResult,
    TargetProfile,
)
from rikkelivo.times import iso_to_ms, ms_to_iso

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RH_DEFAULT_ASSISTANT_ID = "0950e2dc-9bd5-4801-afa3-aa887aa36b4e"
RH_FILES_PREFIX = "file:///data/user/0/me.rerere.rikkahub/files/"
RH_AVATAR_DUMMY = {"type": "me.rerere.rikkahub.data.model.Avatar.Dummy"}

_ENTITY_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-entity")
_MODEL_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-model")
_MEMORY_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-memory")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_MEMORY_SECTION_ORDER = ("workflow", "voice", "instruction")


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _entity_uuid(value: Any, namespace: str = "") -> str:
    if _is_uuid(value):
        return str(value).lower()
    seed = f"{namespace}:{value}" if namespace else str(value)
    return str(uuidlib.uuid5(_ENTITY_NS, seed))


def _model_uuid(provider_id: str, logical_id: str) -> str:
    return str(uuidlib.uuid5(_MODEL_NS, f"{provider_id}:{logical_id}"))


def _parse(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _diag(code: str, category: str, count: int, detail: str) -> Diagnostic:
    return Diagnostic(code, category, count, detail)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ---------------------------------------------------------------------------
# Prompt placeholders
# ---------------------------------------------------------------------------

_PROMPT_VAR_K2K = {
    "char": "assistant_name",
    "user": "nickname",
    "cur_date": "cur_date",
    "cur_time": "cur_time",
    "cur_datetime": "cur_datetime",
    "model_id": "model_id",
    "model_name": "model_name",
    "locale": "locale",
    "timezone": "timezone",
    "system_version": "system_version",
    "device_info": "device_info",
    "battery_level": "battery_level",
    "nickname": "nickname",
}
_PROMPT_VAR_K2R = {
    "assistant_name": "char",
    "nickname": "user",
    "cur_date": "cur_date",
    "cur_time": "cur_time",
    "cur_datetime": "cur_datetime",
    "model_id": "model_id",
    "model_name": "model_name",
    "locale": "locale",
    "timezone": "timezone",
    "system_version": "system_version",
    "device_info": "device_info",
    "battery_level": "battery_level",
    "user": "user",
    "char": "char",
}
_PROMPT_VAR_RE = re.compile(r"(\{\{|\{)([A-Za-z_]+)(\}\}|\})")


def convert_prompt_vars(text: str, to_rikkahub: bool) -> str:
    """Rewrite prompt placeholders between the two applications' syntax."""
    table = _PROMPT_VAR_K2R if to_rikkahub else _PROMPT_VAR_K2K

    def _sub(match: re.Match) -> str:
        opening, key, closing = match.groups()
        if to_rikkahub:
            if closing == "}}":
                return match.group(0)
            target = table.get(key)
            return f"{{{{{target}}}}}" if target else match.group(0)
        target = table.get(key)
        if not target:
            return match.group(0)
        return f"{{{target}}}"

    return _PROMPT_VAR_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_settings(source: Backup, target: TargetProfile) -> SettingsResult:
    source_family = source.family
    target_family = model.APP_FAMILY[target.app]
    if target_family == "kelivo" and source_family == "kelivo":
        return _kelivo_to_kelivo(source, target)
    if target_family == "rikkahub" and source_family == "rikkahub":
        return _rikkahub_to_rikkahub(source)
    if source_family == "rikkahub":
        return _rikkahub_to_kelivo(source, target)
    return _kelivo_to_rikkahub(source, target)


def _rikkahub_to_rikkahub(source: Backup) -> SettingsResult:
    payload = source.payload
    assert isinstance(payload, RikkaPayload)
    settings = dict(payload.settings)
    providers = _as_list(settings.get("providers"))
    assistants = _as_list(settings.get("assistants"))
    return SettingsResult(
        settings=settings,
        provider_ids={str(p.get("id")): str(p.get("id"))
                      for p in providers if isinstance(p, dict) and p.get("id")},
        assistant_ids={str(a.get("id")): str(a.get("id"))
                       for a in assistants if isinstance(a, dict) and a.get("id")},
        workspace_ids={},
        entity_ids=_rikkahub_entity_ids(settings),
    )


def _rikkahub_entity_ids(settings: dict[str, Any]) -> dict[str, dict[str, str]]:
    maps = {"mcp": {}, "instruction": {}, "worldBook": {}, "quickPhrase": {},
            "search": {}, "tts": {}, "asr": {}}
    for key, kind in (("mcpServers", "mcp"), ("modeInjections", "instruction"),
                      ("lorebooks", "worldBook"), ("quickMessages", "quickPhrase"),
                      ("searchServices", "search"), ("ttsProviders", "tts"),
                      ("asrProviders", "asr")):
        for entry in _as_list(settings.get(key)):
            if isinstance(entry, dict) and entry.get("id"):
                maps[kind][str(entry["id"])] = str(entry["id"])
    return maps


# ---------------------------------------------------------------------------
# Kelivo source -> Kelivo target
# ---------------------------------------------------------------------------

def _kelivo_to_kelivo(source: Backup, target: TargetProfile) -> SettingsResult:
    payload = source.payload
    assert isinstance(payload, KelivoPayload)
    settings = json.loads(json.dumps(payload.settings))
    providers = _as_dict(_parse(settings.get("provider_configs_v1")))
    assistants = _as_list(_parse(settings.get("assistants_v1")))
    diagnostics: list[Diagnostic] = []
    provider_ids = {str(key): str(key) for key in providers}
    models_by_rh_uuid: dict[str, tuple[str, str]] = {}
    model_ids: dict[tuple[str, str], str] = {}
    for key, value in providers.items():
        if not isinstance(value, dict):
            continue
        for logical in _as_list(value.get("models")):
            if isinstance(logical, str) and logical:
                model_ids[(str(key), logical)] = _model_uuid(str(key), logical)
    assistant_ids = {
        str(a["id"]): str(a["id"]) for a in assistants
        if isinstance(a, dict) and a.get("id")}
    entity_ids = _kelivo_entity_ids(settings)
    flags = _flags_from_kelivo_assistants(assistants)
    entries = _memory_entries_from_kelivo(settings, source)
    workspaces = _as_list(_parse(settings.get("workspaces_v1")))
    workspace_ids = {
        str(w.get("id")): str(w.get("id")) for w in workspaces
        if isinstance(w, dict) and w.get("id")}
    skills = _as_list(_parse(settings.get("skills_v1")))
    skill_ids = {str(s.get("id")): str(s.get("id")) for s in skills
                 if isinstance(s, dict) and s.get("id")}
    return SettingsResult(
        settings=settings,
        provider_ids=provider_ids,
        model_ids=model_ids,
        assistant_ids=assistant_ids,
        workspace_ids=workspace_ids,
        skill_ids=skill_ids,
        models_by_rh_uuid=models_by_rh_uuid,
        entity_ids=entity_ids,
        memory_entries=entries,
        assistant_permissions={
            str(a["id"]): {"allow_system_prompt": True}
            for a in assistants if isinstance(a, dict) and a.get("id")},
        diagnostics=tuple(diagnostics),
    )


def _kelivo_entity_ids(settings: dict[str, Any]) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {
        "mcp": {}, "instruction": {}, "worldBook": {}, "quickPhrase": {},
        "search": {}, "tts": {}, "asr": {},
    }
    for source_key, kind in (("mcp_servers_v1", "mcp"),
                             ("instruction_injections_v1", "instruction"),
                             ("world_books_v1", "worldBook"),
                             ("quick_phrases_v1", "quickPhrase"),
                             ("search_services_v1", "search"),
                             ("tts_services_v1", "tts"),
                             ("asr_services_v1", "asr")):
        for entry in _as_list(_parse(settings.get(source_key))):
            if isinstance(entry, dict) and entry.get("id"):
                maps[kind][str(entry["id"])] = str(entry["id"])
    return maps


def _flags_from_kelivo_assistants(
    assistants: list[Any]) -> dict[str, AssistantMemoryFlags]:
    flags: dict[str, AssistantMemoryFlags] = {}
    for assistant in assistants:
        if not isinstance(assistant, dict) or not assistant.get("id"):
            continue
        flags[str(assistant["id"])] = AssistantMemoryFlags(
            use_global=False,
            enable=bool(assistant.get("enableMemory", True)),
        )
    return flags


def _memory_entries_from_kelivo(settings: dict[str, Any],
                                source: Backup) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    raw = _as_list(_parse(settings.get("memory_entries_v1")))
    for index, payload in enumerate(raw):
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, str):
            continue
        entries.append(MemoryEntry(
            id=str(payload.get("id") or f"memory-{index}"),
            scope=str(payload.get("scope") or "assistant"),
            assistant_id=payload.get("assistantId")
            if isinstance(payload.get("assistantId"), str) else None,
            type=str(payload.get("type") or "instruction"),
            status=str(payload.get("status") or "active"),
            content=content,
            source=str(payload.get("source") or "manual"),
            created_us=int(payload.get("createdAt") or 0)
            if isinstance(payload.get("createdAt"), int) else 0,
            updated_us=int(payload.get("updatedAt") or 0)
            if isinstance(payload.get("updatedAt"), int) else 0,
            related_ids=[str(item) for item in payload.get("relatedIds") or []
                         if isinstance(item, str)],
            native={"payload": dict(payload)},
        ))
    return entries


# ---------------------------------------------------------------------------
# RikkaHub source -> Kelivo target
# ---------------------------------------------------------------------------

def _rikkahub_to_kelivo(source: Backup,
                        target: TargetProfile) -> SettingsResult:
    payload = source.payload
    assert isinstance(payload, RikkaPayload)
    settings = payload.settings
    diagnostics: list[Diagnostic] = []
    provider_payloads: dict[str, Any] = {}
    provider_order: list[str] = []
    model_ids: dict[tuple[str, str], str] = {}
    models_by_rh_uuid: dict[str, tuple[str, str]] = {}
    provider_ids: dict[str, str] = {}
    for provider in _as_list(settings.get("providers")):
        if not isinstance(provider, dict) or not provider.get("id"):
            continue
        converter = _provider_r2k(provider, diagnostics)
        if converter is None:
            continue
        key, payload_entry, model_map = converter
        provider_ids[str(provider["id"])] = key
        provider_payloads[key] = payload_entry
        provider_order.append(key)
        for rh_uuid, logical in model_map:
            model_ids[(str(provider["id"]), logical)] = rh_uuid
            models_by_rh_uuid[rh_uuid] = (key, logical)
    workspaces_out, workspace_ids = _workspaces_r2k(source, diagnostics)
    assistants_out: list[dict[str, Any]] = []
    assistant_ids: dict[str, str] = {}
    assistant_permissions: dict[str, dict[str, bool]] = {}
    for assistant in _as_list(settings.get("assistants")):
        if not isinstance(assistant, dict) or not assistant.get("id"):
            continue
        mapped = _assistant_r2k(assistant, models_by_rh_uuid, diagnostics,
                                workspace_ids=workspace_ids)
        assistants_out.append(mapped)
        assistant_ids[str(assistant["id"])] = str(mapped["id"])
        assistant_permissions[str(assistant["id"])] = {
            "allow_system_prompt": bool(
                assistant.get("allowConversationSystemPrompt")),
        }
    mcp_out, mcp_ids = _mcp_r2k(settings.get("mcpServers"), diagnostics)
    instructions_out, instruction_ids = _instructions_r2k(
        settings.get("modeInjections"), diagnostics)
    quick_out, quick_ids = _quick_phrases_r2k(settings.get("quickMessages"))
    tags_out, tag_ids = _tags_r2k(settings.get("assistantTags"))
    entities = {
        "mcp": mcp_ids,
        "instruction": instruction_ids,
        "worldBook": {},
        "quickPhrase": quick_ids,
        "search": {},
        "tts": {},
        "asr": {},
    }
    skills = [skill.id for skill in _source_skills(source)]
    skill_ids = {name: name for name in skills}
    output: dict[str, Any] = {
        "provider_configs_v1": _json(provider_payloads),
        "providers_order_v1": provider_order,
        "assistants_v1": _json(assistants_out),
        "mcp_servers_v1": _json(mcp_out),
        "instruction_injections_v1": _json(instructions_out),
        "quick_phrases_v1": _json(quick_out),
        "assistant_tags_v1": _json(tags_out),
        "world_books_v1": _json([]),
        "search_services_v1": _json([]),
        "tts_services_v1": _json([]),
        "memory_entries_v1": _json([]),
        "workspaces_v1": _json(workspaces_out),
        "skills_v1": _json([]),
        "user_profile_fields_v1": _json([]),
    }
    if settings.get("worldBooks") or settings.get("lorebooks"):
        diagnostics.append(_diag(
            "unsupported_data", "world book", 1,
            "RikkaHub 世界书与 Kelivo 条目结构不等价，未迁移条目"))
    search_services = _as_list(settings.get("searchServices"))
    if search_services:
        diagnostics.append(_diag(
            "unsupported_data", "search service", len(search_services),
            "搜索服务协议不同，未迁移"))
    tts_services = _as_list(settings.get("ttsProviders")) + _as_list(
        settings.get("asrProviders"))
    if tts_services:
        diagnostics.append(_diag(
            "unsupported_data", "speech service", len(tts_services),
            "语音服务协议不同，未迁移"))
    _copy_feature_settings_r2k(settings, output, models_by_rh_uuid,
                               entity_ids=None, diagnostics=diagnostics)
    memory_entries, memory_diagnostics = _memory_r2k(
        source, assistants_out, model.SettingsResult(
            settings=output, assistant_ids=assistant_ids),
        source_flags=_assistant_flags_from_rh_settings(settings))
    output["memory_entries_v1"] = _json([
        _memory_entry_payload(entry) for entry in memory_entries])
    diagnostics.extend(memory_diagnostics)
    return SettingsResult(
        settings=output,
        provider_ids=provider_ids,
        model_ids=model_ids,
        assistant_ids=assistant_ids,
        workspace_ids=workspace_ids,
        skill_ids=skill_ids,
        models_by_rh_uuid=models_by_rh_uuid,
        entity_ids=entities,
        memory_entries=memory_entries,
        assistant_permissions=assistant_permissions,
        diagnostics=tuple(diagnostics),
    )


def _provider_r2k(provider: dict[str, Any],
                  diagnostics: list[Diagnostic]
                  ) -> tuple[str, dict[str, Any], list[tuple[str, str]]] | None:
    rh_id = str(provider["id"])
    key = _entity_uuid(rh_id, "provider")
    kind = str(provider.get("type") or "openai")
    base: dict[str, Any] = {
        "id": key,
        "enabled": bool(provider.get("enabled", True)),
        "name": str(provider.get("name") or ""),
        "apiKey": str(provider.get("apiKey") or ""),
        "baseUrl": str(provider.get("baseUrl") or ""),
        "models": [],
        "modelOverrides": {},
        "customHeaders": _headers_r2k(provider.get("customHeaders")),
        "customBody": _bodies_r2k(provider.get("customBodies")),
    }
    models = _as_list(provider.get("models"))
    model_map: list[tuple[str, str]] = []
    kept_models = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("providerOverwrite"), dict):
            diagnostics.append(_diag(
                "unsupported_data", "model override", 1,
                "模型的 providerOverwrite 在 Kelivo 中无等价结构，已丢弃该模型"))
            continue
        logical = str(entry.get("modelId") or "")
        rh_uuid = str(entry.get("id") or _model_uuid(key, logical))
        if not logical:
            continue
        kept_models.append(logical)
        base["models"].append(logical)
        base["modelOverrides"][logical] = _model_override_r2k(entry)
        model_map.append((rh_uuid, logical))
    if not model_map:
        diagnostics.append(_diag(
            "unsupported_data", "provider", 1,
            "提供商没有可表达的模型，已丢弃"))
        return None
    if kind in ("chatgpt", "grok"):
        base["apiKey"] = ""
        base["providerType"] = "openai"
        base["oauthProvider"] = kind
        base["baseUrl"] = ("https://chatgpt.com/backend-api/codex"
                           if kind == "chatgpt" else "https://api.x.ai/v1")
        base["useResponseApi"] = True
        credential = provider.get("credential")
        if isinstance(credential, dict):
            oauth = _credential_r2k(credential, diagnostics)
            if oauth is not None:
                base["oauthCredentials"] = oauth
        return key, base, model_map
    if kind == "google":
        base["providerType"] = "google"
        base["vertexAI"] = bool(provider.get("vertexAI"))
        if provider.get("useServiceAccount"):
            base["serviceAccountJson"] = _json({
                "type": "service_account",
                "private_key": provider.get("privateKey") or "",
                "client_email": provider.get("serviceAccountEmail") or "",
                "project_id": provider.get("projectId") or "",
                "location": provider.get("location") or "",
                "private_key_id": provider.get("privateKeyId") or "",
            })
        base["location"] = provider.get("location") or None
        base["projectId"] = provider.get("projectId") or None
    elif kind == "claude":
        base["providerType"] = "claude"
        base["chatPath"] = None
        base["useResponseApi"] = False
        base["claudePromptCachingEnabled"] = bool(provider.get("promptCaching"))
        ttl = str(provider.get("promptCacheTtl") or "5m")
        base["claudePromptCachingTtl"] = "1h" if ttl in ("1h", "ONE_HOUR") else "5m"
    else:
        base["providerType"] = "openai"
        base["chatPath"] = str(provider.get("chatCompletionsPath") or
                               "/chat/completions")
        base["useResponseApi"] = bool(provider.get("useResponseApi"))
    quota = provider.get("quotaQuery")
    if isinstance(quota, dict) and quota.get("enabled"):
        url = str(quota.get("url") or "")
        api_path = url[len("{{baseUrl}}"):] if url.startswith("{{baseUrl}}") \
            else url
        base["balanceEnabled"] = True
        base["balanceApiPath"] = api_path
        base["balanceResultPath"] = str(
            quota.get("resultPath") or "data.total_usage")
    return key, base, model_map


def _model_override_r2k(entry: dict[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("type") or "CHAT").lower()
    return {
        "apiModelId": entry.get("modelId"),
        "name": entry.get("displayName") or entry.get("modelId"),
        "type": "embedding" if kind == "embedding" else (
            "image" if kind == "image" else "chat"),
        "input": [str(item).lower() for item in
                  _as_list(entry.get("inputModalities"))] or ["text"],
        "output": [str(item).lower() for item in
                   _as_list(entry.get("outputModalities"))] or ["text"],
        "abilities": [str(item).lower() for item in
                      _as_list(entry.get("abilities"))],
    }


def _credential_r2k(credential: dict[str, Any],
                    diagnostics: list[Diagnostic]) -> dict[str, Any] | None:
    access = credential.get("accessToken")
    refresh = credential.get("refreshToken")
    expires = credential.get("expiresAt")
    if not isinstance(access, str) or not access \
            or not isinstance(refresh, str) or not refresh \
            or not isinstance(expires, int) or expires <= 0:
        diagnostics.append(_diag(
            "unsupported_data", "oauth credential", 1,
            "凭据缺少可用的 refresh token 或过期时间，目标保持未登录"))
        return None
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": datetime.fromtimestamp(
            expires / 1000, tz=timezone.utc).isoformat(),
        "sessionId": str(uuidlib.uuid4()),
        "accountId": credential.get("accountId")
        if isinstance(credential.get("accountId"), str) else None,
        "email": credential.get("email")
        if isinstance(credential.get("email"), str) else None,
        "plan": credential.get("plan")
        if isinstance(credential.get("plan"), str) else None,
        "requiresLogin": False,
    }


def _headers_r2k(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("key")
            header_value = entry.get("value")
            if isinstance(name, str) and name and isinstance(header_value, str):
                out.append({"name": name, "value": header_value})
    return out


def _bodies_r2k(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            key = entry.get("key") or entry.get("name")
            body_value = entry.get("value")
            if isinstance(key, str) and key and isinstance(body_value, str):
                out.append({"key": key, "value": body_value})
    return out


def _assistant_r2k(assistant: dict[str, Any],
                   models_by_rh_uuid: dict[str, tuple[str, str]],
                   diagnostics: list[Diagnostic],
                   *, workspace_ids: dict[str, str] | None = None) -> dict[str, Any]:
    model_id = assistant.get("chatModelId")
    provider_key = None
    logical_id = None
    if isinstance(model_id, str):
        mapped = models_by_rh_uuid.get(model_id)
        if mapped:
            provider_key, logical_id = mapped
        else:
            diagnostics.append(_diag(
                "identity_reference_unresolved", "assistant model", 1,
                "助手的模型引用无法映射，已清空该助手的模型"))
    avatar = assistant.get("avatar")
    avatar_value = None
    if isinstance(avatar, dict) and isinstance(avatar.get("url"), str):
        avatar_value = avatar["url"]
    presets = []
    dropped_parts = 0
    for preset in _as_list(assistant.get("presetMessages")):
        if not isinstance(preset, dict):
            continue
        texts = []
        for part in _as_list(preset.get("parts")):
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
            else:
                dropped_parts += 1
        role = preset.get("role") if preset.get("role") in (
            "user", "assistant") else "user"
        presets.append({
            "id": _entity_uuid(preset.get("id") or _entity_uuid(
                f"{assistant.get('id')}:preset", "preset"), "preset"),
            "role": role,
            "content": "\n\n".join(texts),
        })
    if dropped_parts:
        diagnostics.append(_diag(
            "unsupported_data", "preset part", dropped_parts,
            "预设消息中的非文本部件未迁移"))
    if _as_list(assistant.get("regexes")):
        diagnostics.append(_diag(
            "unsupported_data", "assistant regex",
            len(_as_list(assistant.get("regexes"))),
            "RikkaHub 助手正则规则在 Kelivo 中没有等价结构"))
    local_tools = [str(item) for item in _as_list(assistant.get("localTools"))
                   if isinstance(item, str)]
    mcp_servers = [str(item) for item in _as_list(assistant.get("mcpServers"))
                   if isinstance(item, str)]
    context_limit = int(assistant.get("contextMessageLimit") or 0)
    return {
        "id": str(assistant["id"]),
        "name": str(assistant.get("name") or ""),
        "avatar": avatar_value,
        "useAssistantAvatar": bool(assistant.get("useAssistantAvatar")),
        "useAssistantName": True,
        "chatModelProvider": provider_key,
        "chatModelId": logical_id,
        "temperature": assistant.get("temperature"),
        "topP": assistant.get("topP"),
        "contextMessageSize": context_limit if context_limit > 0 else 64,
        "limitContextMessages": context_limit > 0,
        "streamOutput": bool(assistant.get("streamOutput", True)),
        "maxTokens": assistant.get("maxTokens"),
        "systemPrompt": convert_prompt_vars(
            str(assistant.get("systemPrompt") or ""), to_rikkahub=False),
        "allowConversationSystemPrompt": bool(
            assistant.get("allowConversationSystemPrompt")),
        "allowConversationPromptInjection": False,
        "messageTemplate": str(assistant.get("messageTemplate") or
                               "{{ message }}"),
        "searchEnabled": bool(assistant.get("enableWebSearch")),
        "mcpServerIds": mcp_servers,
        "defaultWorkspaceId": (workspace_ids or {}).get(
            str(assistant.get("workspaceId") or "")) or None,
        "localToolIds": local_tools,
        "skillIds": [str(item) for item in
                     _as_list(assistant.get("enabledSkills"))],
        "background": assistant.get("background")
        if isinstance(assistant.get("background"), str) else None,
        "useGradientBackground": bool(assistant.get("useGradientBackground")),
        "customHeaders": _headers_r2k(assistant.get("customHeaders")),
        "customBody": _bodies_r2k(assistant.get("customBodies")),
        "enableMemory": bool(assistant.get("enableMemory")),
        "enableRecentChatsReference": bool(
            assistant.get("enableRecentChatsReference")),
        "presetMessages": presets,
        "regexRules": [],
        "enableTimeInjection": bool(assistant.get("enableTimeReminder")),
    }


def _mcp_r2k(value: Any, diagnostics: list[Diagnostic]
             ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        server_id = _entity_uuid(entry["id"], "mcp")
        transport = str(entry.get("type") or "streamable_http")
        if transport not in ("sse", "streamable_http", "stdio"):
            diagnostics.append(_diag(
                "unsupported_data", "mcp server", 1,
                f"未知的 MCP 传输类型 {transport}"))
            continue
        payload: dict[str, Any] = {
            "id": server_id,
            "name": str(entry.get("name") or ""),
            "enabled": bool(entry.get("enabled", True)),
            "transport": transport,
            "tools": [],
        }
        if transport == "stdio":
            payload["command"] = str(entry.get("command") or "")
            payload["args"] = [str(item) for item in _as_list(entry.get("args"))]
            payload["env"] = _as_dict(entry.get("env"))
            if entry.get("workingDirectory"):
                payload["workingDirectory"] = str(entry["workingDirectory"])
        else:
            payload["url"] = str(entry.get("url") or "")
            payload["headers"] = _as_dict(entry.get("headers"))
        out.append(payload)
        mapping[str(entry["id"])] = server_id
    return out, mapping


def _instructions_r2k(value: Any, diagnostics: list[Diagnostic]
                      ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    dropped = 0
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if entry.get("type") not in (None, "mode"):
            dropped += 1
            continue
        instruction_id = _entity_uuid(entry["id"], "instruction")
        out.append({
            "id": instruction_id,
            "title": str(entry.get("name") or ""),
            "prompt": str(entry.get("content") or ""),
            "group": "",
        })
        mapping[str(entry["id"])] = instruction_id
    if dropped:
        diagnostics.append(_diag(
            "unsupported_data", "prompt injection", dropped,
            "非模式注入的提示注入未迁移"))
    return out, mapping


def _quick_phrases_r2k(value: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        phrase_id = _entity_uuid(entry["id"], "quick")
        out.append({
            "id": phrase_id,
            "title": str(entry.get("title") or ""),
            "content": str(entry.get("content") or ""),
            "isGlobal": True,
            "assistantId": None,
        })
        mapping[str(entry["id"])] = phrase_id
    return out, mapping


def _tags_r2k(value: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        tag_id = _entity_uuid(entry["id"], "tag")
        out.append({"id": tag_id, "name": str(entry.get("name") or "")})
        mapping[str(entry["id"])] = tag_id
    return out, mapping


def _workspaces_r2k(source: Backup,
                    diagnostics: list[Diagnostic]
                    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    payload = source.payload
    assert isinstance(payload, RikkaPayload)
    table = payload.tables.get("workspaces")
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    if table is None:
        return out, mapping
    columns = {name: index for index, name in enumerate(table.columns)}
    for row in table.rows:
        source_id = str(row[columns["id"]])
        workspace_id = _entity_uuid(source_id, "workspace")
        mapping[source_id] = workspace_id
        out.append({
            "id": workspace_id,
            "name": str(row[columns["name"]] or ""),
            "kind": "managed",
            "hostPath": None,
            "shellNeedsApproval": True,
            "defaultCwd": "",
            "disabledTools": [],
            "createdAt": ms_to_iso(int(row[columns["created_at"]] or 0)) or "",
            "updatedAt": ms_to_iso(int(row[columns["updated_at"]] or 0)) or "",
            "lastUsedAt": None,
        })
        diagnostics.append(_diag(
            "workspace_metadata_only", "workspace", 1,
            "工作区仅迁移元数据；源 rootfs 内容与执行策略未恢复"))
    return out, mapping


def _source_skills(source: Backup) -> list[model.Skill]:
    grouped: dict[str, list[str]] = {}
    for asset in source.assets:
        if not asset.entry.startswith("skills/"):
            continue
        parts = asset.entry.split("/")
        if len(parts) < 3:
            continue
        grouped.setdefault(parts[1], []).append(asset.entry)
    return [model.Skill(id=name, name=name, entries=entries)
            for name, entries in grouped.items()]


def _flags_from_rikkahub_assistants(
    assistants: list[dict[str, Any]]) -> dict[str, AssistantMemoryFlags]:
    flags: dict[str, AssistantMemoryFlags] = {}
    for assistant in assistants:
        if not isinstance(assistant, dict) or not assistant.get("id"):
            continue
        try:
            flags[str(assistant["id"])] = AssistantMemoryFlags(
                use_global=False,
                enable=bool(assistant.get("enableMemory")),
            )
        except KeyError:  # pragma: no cover - defensive
            continue
    return flags


def _copy_feature_settings_r2k(settings: dict[str, Any],
                               output: dict[str, Any],
                               models_by_rh_uuid: dict[str, tuple[str, str]],
                               *, entity_ids: dict[str, dict[str, str]] | None,
                               diagnostics: list[Diagnostic]) -> None:
    for source_key, target_key, kind in (
            ("chatModelId", "selected_model_v1", "model"),
            ("titleModelId", "title_model_v1", "model"),
            ("suggestionModelId", "suggestion_model_v1", "model"),
            ("translateModeId", "translate_model_v1", "model"),
            ("ocrModelId", "ocr_model_v1", "model"),
            ("compressModelId", "compress_model_v1", "model")):
        value = settings.get(source_key)
        if isinstance(value, str) and value in models_by_rh_uuid:
            provider_key, logical = models_by_rh_uuid[value]
            output[target_key] = f"{provider_key}::{logical}"
    for source_key, target_key in (
            ("titlePrompt", "title_prompt_v1"),
            ("suggestionPrompt", "suggestion_prompt_v1"),
            ("translatePrompt", "translate_prompt_v1"),
            ("ocrPrompt", "ocr_prompt_v1"),
            ("compressPrompt", "compress_prompt_v1")):
        value = settings.get(source_key)
        if isinstance(value, str) and value:
            output[target_key] = convert_prompt_vars(value, to_rikkahub=False)
    output["title_generation_enabled_v1"] = bool(
        settings.get("enableAutoTitle", True))
    output["suggestion_generation_enabled_v1"] = bool(
        settings.get("enableSuggestion", True))
    if output.get("ocr_model_v1"):
        output["ocr_enabled_v1"] = True
    if isinstance(settings.get("translateTargetLanguage"), str):
        output["translate_target_lang_v1"] = settings["translateTargetLanguage"]
    selected_tts = settings.get("selectedTTSProviderId")
    if isinstance(selected_tts, str):
        mapped = (
            (entity_ids or {}).get("tts", {}).get(selected_tts)
            or (_entity_uuid(selected_tts, "tts") if _is_uuid(selected_tts)
                else None))
        if mapped:
            output["tts_selected_service_id_v1"] = mapped
    selected_asr = settings.get("selectedASRProviderId")
    if isinstance(selected_asr, str):
        mapped = (entity_ids or {}).get("asr", {}).get(selected_asr)
        if mapped:
            output["asr_selected_service_id_v1"] = mapped


# ---------------------------------------------------------------------------
# Kelivo source -> RikkaHub target
# ---------------------------------------------------------------------------

def _kelivo_to_rikkahub(source: Backup,
                        target: TargetProfile) -> SettingsResult:
    payload = source.payload
    assert isinstance(payload, KelivoPayload)
    source_settings = payload.settings
    diagnostics: list[Diagnostic] = []
    providers = _as_dict(_parse(source_settings.get("provider_configs_v1")))
    order = _as_list(_parse(source_settings.get("providers_order_v1")))
    ordered_keys = [str(key) for key in order if str(key) in providers]
    ordered_keys += [str(key) for key in providers if str(key) not in ordered_keys]
    rh_providers: list[dict[str, Any]] = []
    provider_ids: dict[str, str] = {}
    model_ids: dict[tuple[str, str], str] = {}
    models_by_rh_uuid: dict[str, tuple[str, str]] = {}
    for key in ordered_keys:
        entry = providers.get(key)
        if not isinstance(entry, dict):
            continue
        converted = _provider_k2r(str(key), entry, diagnostics)
        if converted is None:
            continue
        rh_provider, aliases = converted
        rh_providers.append(rh_provider)
        provider_ids[str(key)] = str(rh_provider["id"])
        for logical, rh_uuid in aliases:
            model_ids[(str(key), logical)] = rh_uuid
            models_by_rh_uuid[rh_uuid] = (str(key), logical)
    assistants_in = _as_list(_parse(source_settings.get("assistants_v1")))
    rh_assistants: list[dict[str, Any]] = []
    assistant_ids: dict[str, str] = {}
    for assistant in assistants_in:
        if not isinstance(assistant, dict) or not assistant.get("id"):
            continue
        mapped = _assistant_k2r(assistant, models_by_rh_uuid, diagnostics)
        rh_assistants.append(mapped)
        assistant_ids[str(assistant["id"])] = str(mapped["id"])
    mcp_out, mcp_ids = _mcp_k2r(_parse(source_settings.get("mcp_servers_v1")),
                                diagnostics)
    instructions_out, instruction_ids = _instructions_k2r(
        _parse(source_settings.get("instruction_injections_v1")))
    quick_out, quick_ids = _quick_phrases_k2r(
        _parse(source_settings.get("quick_phrases_v1")))
    tags_out, tag_ids = _tags_k2r(_parse(source_settings.get("assistant_tags_v1")))
    entities = {
        "mcp": mcp_ids,
        "instruction": instruction_ids,
        "worldBook": {},
        "quickPhrase": quick_ids,
        "search": {},
        "tts": {},
        "asr": {},
    }
    settings: dict[str, Any] = {
        "providers": rh_providers,
        "assistants": rh_assistants,
        "mcpServers": mcp_out,
        "modeInjections": instructions_out,
        "quickMessages": quick_out,
        "assistantTags": tags_out,
        "lorebooks": [],
        "searchServices": [],
        "ttsProviders": [],
        "asrProviders": [],
        "favoriteModels": [],
    }
    _copy_feature_settings_k2r(source_settings, settings, model_ids,
                               diagnostics)
    assistant_id = _entity_uuid(
        assistants_in[0]["id"], "assistant") if assistants_in and isinstance(
            assistants_in[0], dict) and assistants_in[0].get("id") \
        else RH_DEFAULT_ASSISTANT_ID
    if not any(str(a.get("id")) == assistant_id for a in rh_assistants):
        assistant_id = str(rh_assistants[0]["id"]) if rh_assistants \
            else RH_DEFAULT_ASSISTANT_ID
    settings["assistantId"] = assistant_id
    _resolve_active_models(settings, rh_providers, diagnostics)
    workspaces_in = _as_list(_parse(source_settings.get("workspaces_v1")))
    workspace_ids = {
        str(w.get("id")): _entity_uuid(w.get("id"), "workspace")
        for w in workspaces_in if isinstance(w, dict) and w.get("id")}
    skills_in = _as_list(_parse(source_settings.get("skills_v1")))
    skill_ids = {
        str(s.get("id")): str(s.get("id")) for s in skills_in
        if isinstance(s, dict) and s.get("id")}
    entries, docs = _memory_k2r(source, rh_assistants, assistant_ids,
                                diagnostics,
                                legacy_target=target.schema_version is not None
                                and target.schema_version < 28)
    for assistant in rh_assistants:
        if assistant.get("useGlobalMemory") is True:
            assistant["useGlobalMemory"] = False
    if _as_list(_parse(source_settings.get("world_books_v1"))):
        diagnostics.append(_diag(
            "unsupported_data", "world book",
            len(_as_list(_parse(source_settings.get("world_books_v1")))),
            "Kelivo 世界书条目与 RikkaHub 结构不等价，未迁移条目"))
    return SettingsResult(
        settings=settings,
        provider_ids=provider_ids,
        model_ids=model_ids,
        assistant_ids=assistant_ids,
        workspace_ids=workspace_ids,
        skill_ids=skill_ids,
        models_by_rh_uuid=models_by_rh_uuid,
        entity_ids=entities,
        memory_entries=[],
        memory_docs=docs,
        diagnostics=tuple(diagnostics),
    )


def _provider_k2r(key: str, entry: dict[str, Any],
                  diagnostics: list[Diagnostic]
                  ) -> tuple[dict[str, Any], list[tuple[str, str]]] | None:
    provider_id = _entity_uuid(key, "provider")
    oauth = entry.get("oauthProvider")
    multi_key = bool(entry.get("multiKeyEnabled"))
    api_keys = _as_list(entry.get("apiKeys"))
    base: dict[str, Any] = {
        "id": provider_id,
        "enabled": bool(entry.get("enabled", True)),
        "name": str(entry.get("name") or ""),
        "customHeaders": _headers_k2r(entry.get("customHeaders")),
        "customBodies": _headers_k2r(entry.get("customBody")),
        "quotaQuery": _quota_k2r(entry),
        "includeHistoryReasoning": True,
    }
    if isinstance(oauth, str):
        if oauth not in ("chatgpt", "grok"):
            diagnostics.append(_diag(
                "unsupported_data", "provider auth", 1,
                f"OAuth 提供商 {oauth} 在 RikkaHub 中没有原生账户类型，已丢弃"))
            return None
        base["type"] = oauth
        credential = _credential_k2r(entry.get("oauthCredentials"), diagnostics)
        base["credential"] = credential
        base["models"] = []
    else:
        if multi_key and api_keys:
            diagnostics.append(_diag(
                "unsupported_data", "provider auth", 1,
                "活动的多 API Key 模式在 RikkaHub 中没有等价结构，已丢弃该提供商"))
            return None
        if api_keys and not multi_key:
            diagnostics.append(_diag(
                "unsupported_data", "provider auth", len(api_keys),
                "未启用的备用 API Key 未迁移"))
        provider_type = str(entry.get("providerType") or "").lower()
        vertex = bool(entry.get("vertexAI"))
        service_account = entry.get("serviceAccountJson")
        if provider_type in ("google", "gemini") or vertex or service_account:
            base["type"] = "google"
            base["apiKey"] = str(entry.get("apiKey") or "")
            base["baseUrl"] = str(entry.get("baseUrl")
                                  or "https://generativelanguage.googleapis.com/v1beta")
            parsed = _service_account(service_account)
            if vertex or service_account:
                if parsed is None:
                    diagnostics.append(_diag(
                        "unsupported_data", "provider auth", 1,
                        "无效的 Vertex AI service account，已丢弃该提供商"))
                    return None
                base["vertexAI"] = True
                base["useServiceAccount"] = True
                base["privateKey"] = parsed.get("private_key", "")
                base["serviceAccountEmail"] = parsed.get("client_email", "")
                base["projectId"] = parsed.get("project_id", "")
                base["location"] = parsed.get("location", "us-central1")
            else:
                base["vertexAI"] = False
                base["useServiceAccount"] = False
        elif provider_type in ("claude", "anthropic"):
            base["type"] = "claude"
            base["apiKey"] = str(entry.get("apiKey") or "")
            base["baseUrl"] = str(entry.get("baseUrl")
                                  or "https://api.anthropic.com/v1")
            base["promptCaching"] = bool(
                entry.get("claudePromptCachingEnabled"))
            base["promptCacheTtl"] = (
                "1h" if entry.get("claudePromptCachingTtl") == "1h"
                else "5m")
        else:
            base["type"] = "openai"
            base["apiKey"] = str(entry.get("apiKey") or "")
            base["baseUrl"] = str(entry.get("baseUrl")
                                  or "https://api.openai.com/v1")
            base["chatCompletionsPath"] = str(
                entry.get("chatPath") or "/chat/completions")
            base["useResponseApi"] = bool(entry.get("useResponseApi"))
        base["models"] = []
    aliases: list[tuple[str, str]] = []
    base["models"] = []
    for logical in _as_list(entry.get("models")):
        if not isinstance(logical, str) or not logical:
            continue
        rh_uuid = _model_uuid(provider_id, logical)
        override = _as_dict(_as_dict(entry.get("modelOverrides")).get(logical))
        model: dict[str, Any] = {
            "modelId": str(override.get("apiModelId") or logical),
            "displayName": str(override.get("name") or logical),
            "id": rh_uuid,
            "type": {"image": "IMAGE", "embedding": "EMBEDDING"}.get(
                str(override.get("type") or "chat").lower(), "CHAT"),
            "customHeaders": [],
            "customBodies": [],
            "inputModalities": _uppercase(
                override.get("input"), ["TEXT"]),
            "outputModalities": _uppercase(
                override.get("output"), ["TEXT"]),
            "abilities": _uppercase(override.get("abilities"), []),
            "tools": [],
            "providerOverwrite": None,
        }
        base["models"].append(model)
        aliases.append((logical, rh_uuid))
        api_model = override.get("apiModelId")
        if isinstance(api_model, str) and api_model and api_model != logical:
            # Provider-embedded request name: usable as an alias only when it
            # names exactly one model inside this provider.
            aliases.append((api_model, rh_uuid))
    if not base["models"]:
        diagnostics.append(_diag(
            "unsupported_data", "provider", 1,
            "提供商没有可表达的模型，已丢弃"))
        return None
    return base, aliases


def _headers_k2r(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in _as_list(value):
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("key")
            header_value = entry.get("value")
            if isinstance(name, str) and name and isinstance(header_value, str):
                out.append({"name": name, "value": header_value})
    return out


def _quota_k2r(entry: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(entry.get("balanceEnabled"))
    api_path = str(entry.get("balanceApiPath") or "/credits")
    headers = [{"name": "Authorization", "value": "Bearer {{apiKey}}"}]
    return {
        "enabled": enabled,
        "url": api_path if api_path.startswith("http") else "{{baseUrl}}" + api_path,
        "headers": headers,
        "resultPath": str(entry.get("balanceResultPath")
                          or "data.total_usage"),
    }


def _service_account(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not parsed.get("private_key") or not parsed.get("client_email"):
        return None
    return parsed


def _credential_k2r(credentials: Any,
                    diagnostics: list[Diagnostic]) -> dict[str, Any] | None:
    if not isinstance(credentials, dict):
        return None
    if credentials.get("requiresLogin") is True:
        diagnostics.append(_diag(
            "unsupported_data", "oauth credential", 1,
            "源会话要求重新登录，目标保持未登录"))
        return None
    access = credentials.get("accessToken")
    refresh = credentials.get("refreshToken")
    expires = credentials.get("expiresAt")
    expires_ms = iso_to_ms(expires) if isinstance(expires, str) else None
    if not isinstance(access, str) or not access \
            or not isinstance(refresh, str) or not refresh \
            or not expires_ms:
        diagnostics.append(_diag(
            "unsupported_data", "oauth credential", 1,
            "凭据缺少可用的 refresh token 或过期时间，目标保持未登录"))
        return None
    for key in ("sessionId", "deviceId", "organizationId", "organizationName"):
        if credentials.get(key):
            diagnostics.append(_diag(
                "unsupported_data", "oauth metadata", 1,
                f"{key} 在 RikkaHub 中没有等价字段"))
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_ms,
        "accountId": credentials.get("accountId")
        if isinstance(credentials.get("accountId"), str) else None,
        "email": credentials.get("email")
        if isinstance(credentials.get("email"), str) else None,
        "plan": credentials.get("plan")
        if isinstance(credentials.get("plan"), str) else None,
    }


def _uppercase(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    out = [str(item).upper() for item in value]
    return out or list(default)


def _assistant_k2r(assistant: dict[str, Any],
                   models_by_rh_uuid: dict[str, tuple[str, str]],
                   diagnostics: list[Diagnostic]) -> dict[str, Any]:
    provider = assistant.get("chatModelProvider")
    model_id = assistant.get("chatModelId")
    rh_model = None
    if isinstance(model_id, str) and model_id:
        if isinstance(provider, str) and provider:
            rh_model = next((rh_uuid for rh_uuid, (key, logical) in
                             models_by_rh_uuid.items()
                             if key == provider and logical == model_id), None)
        else:
            matches = [rh_uuid for rh_uuid, (key, logical) in
                       models_by_rh_uuid.items() if logical == model_id]
            if len(matches) == 1:
                rh_model = matches[0]
        if rh_model is None:
            diagnostics.append(_diag(
                "identity_reference_unresolved", "assistant model", 1,
                "助手的模型引用无法映射，已清空该助手的模型"))
    presets = []
    for preset in _as_list(_parse(assistant.get("presetMessages"))):
        if not isinstance(preset, dict):
            continue
        role = preset.get("role") if preset.get("role") in (
            "user", "assistant") else "user"
        presets.append({
            "id": _entity_uuid(preset.get("id") or _entity_uuid(
                f"{assistant.get('id')}:preset", "preset"), "preset"),
            "role": role,
            "parts": [{"type": "text",
                       "text": str(preset.get("content") or ""),
                       "metadata": None}],
            "annotations": [],
        })
    if _as_list(_parse(assistant.get("regexRules"))):
        diagnostics.append(_diag(
            "unsupported_data", "assistant regex",
            len(_as_list(_parse(assistant.get("regexRules")))),
            "Kelivo 助手正则规则在 RikkaHub 中没有等价结构"))
    local_tools = [item for item in _as_list(assistant.get("localToolIds"))
                   if isinstance(item, str)]
    mcp_ids = [_entity_uuid(item, "mcp")
               for item in _as_list(assistant.get("mcpServerIds"))
               if isinstance(item, str)]
    skill_ids = [str(item) for item in _as_list(assistant.get("skillIds"))
                 if isinstance(item, str)]
    context_size = assistant.get("contextMessageSize")
    limit = bool(assistant.get("limitContextMessages"))
    avatar = assistant.get("avatar")
    if isinstance(avatar, str) and avatar:
        avatar_value = {"type": "me.rerere.rikkahub.data.model.Avatar.Image",
                        "url": avatar}
    else:
        avatar_value = RH_AVATAR_DUMMY
    return {
        "id": _entity_uuid(assistant["id"], "assistant"),
        "name": str(assistant.get("name") or ""),
        "avatar": avatar_value,
        "useAssistantAvatar": bool(assistant.get("useAssistantAvatar")),
        "chatModelId": rh_model,
        "systemPrompt": convert_prompt_vars(
            str(assistant.get("systemPrompt") or ""), to_rikkahub=True),
        "temperature": assistant.get("temperature"),
        "topP": assistant.get("topP"),
        "contextMessageLimit": int(context_size)
        if limit and isinstance(context_size, int) else 0,
        "streamOutput": bool(assistant.get("streamOutput", True)),
        "enableMemory": bool(assistant.get("enableMemory")),
        "useGlobalMemory": False,
        "autoConsolidateMemory": bool(assistant.get("autoOrganizeMemory")),
        "enableRecentChatsReference": bool(
            assistant.get("allowPastConversationRecall")
            or assistant.get("enableRecentChatsReference")),
        "messageTemplate": str(assistant.get("messageTemplate")
                               or "{{ message }}"),
        "presetMessages": presets,
        "quickMessageIds": [],
        "regexes": [],
        "reasoningLevel": "AUTO",
        "maxTokens": assistant.get("maxTokens"),
        "customHeaders": _headers_k2r(assistant.get("customHeaders")),
        "customBodies": _headers_k2r(assistant.get("customBody")),
        "mcpServers": mcp_ids,
        "localTools": local_tools or ["time_info"],
        "enableWebSearch": bool(assistant.get("searchEnabled")),
        "workspaceId": _entity_uuid(assistant["defaultWorkspaceId"], "workspace")
        if isinstance(assistant.get("defaultWorkspaceId"), str)
        and assistant["defaultWorkspaceId"] else None,
        "background": assistant.get("background")
        if isinstance(assistant.get("background"), str) else None,
        "backgroundOpacity": 1.0,
        "useGradientBackground": bool(assistant.get("useGradientBackground")),
        "modeInjectionIds": [],
        "lorebookIds": [],
        "enabledSkills": skill_ids,
        "enableTimeReminder": bool(assistant.get("enableTimeInjection")),
        "allowConversationSystemPrompt": bool(
            assistant.get("allowConversationSystemPrompt")),
    }


def _mcp_k2r(value: Any, diagnostics: list[Diagnostic]
             ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        server_id = _entity_uuid(entry["id"], "mcp")
        transport = str(entry.get("transport") or "")
        if transport == "inmemory":
            diagnostics.append(_diag(
                "unsupported_data", "mcp server", 1,
                "Kelivo 内置 MCP 服务在 RikkaHub 中没有等价实现"))
            continue
        if transport not in ("sse", "streamable_http", "stdio"):
            diagnostics.append(_diag(
                "unsupported_data", "mcp server", 1,
                f"未知的 MCP 传输类型 {transport}"))
            continue
        payload: dict[str, Any] = {
            "id": server_id,
            "enabled": bool(entry.get("enabled", True)),
            "name": str(entry.get("name") or ""),
            "type": transport,
        }
        if transport == "stdio":
            payload["command"] = str(entry.get("command") or "")
            payload["args"] = [str(item) for item in _as_list(entry.get("args"))]
            payload["env"] = _as_dict(entry.get("env"))
            if entry.get("workingDirectory"):
                payload["workingDirectory"] = str(entry["workingDirectory"])
        else:
            payload["url"] = str(entry.get("url") or "")
            payload["headers"] = _as_dict(entry.get("headers"))
        out.append(payload)
        mapping[str(entry["id"])] = server_id
    return out, mapping


def _instructions_k2r(value: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        instruction_id = _entity_uuid(entry["id"], "instruction")
        out.append({
            "id": instruction_id,
            "type": "mode",
            "name": str(entry.get("title") or ""),
            "enabled": True,
            "priority": 0,
            "position": "after_system_prompt",
            "content": str(entry.get("prompt") or ""),
            "injectDepth": 4,
            "role": "user",
        })
        mapping[str(entry["id"])] = instruction_id
    return out, mapping


def _quick_phrases_k2r(value: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        phrase_id = _entity_uuid(entry["id"], "quick")
        out.append({
            "id": phrase_id,
            "title": str(entry.get("title") or ""),
            "content": str(entry.get("content") or ""),
        })
        mapping[str(entry["id"])] = phrase_id
    return out, mapping


def _tags_k2r(value: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for entry in _as_list(value):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        tag_id = _entity_uuid(entry["id"], "tag")
        out.append({"id": tag_id, "name": str(entry.get("name") or "")})
        mapping[str(entry["id"])] = tag_id
    return out, mapping


def _copy_feature_settings_k2r(source: dict[str, Any], output: dict[str, Any],
                               model_ids: dict[tuple[str, str], str],
                               diagnostics: list[Diagnostic]) -> None:
    def _resolve(raw: Any) -> str | None:
        if not isinstance(raw, str) or "::" not in raw:
            return None
        provider, _, logical = raw.partition("::")
        return model_ids.get((provider, logical))

    selected = _resolve(source.get("selected_model_v1"))
    chat_models = [uuid for (_key, _logical), uuid in model_ids.items()]
    if selected:
        output["chatModelId"] = selected
    elif chat_models:
        output["chatModelId"] = chat_models[0]
    for source_key, target_key in (
            ("title_model_v1", "titleModelId"),
            ("suggestion_model_v1", "suggestionModelId"),
            ("translate_model_v1", "translateModeId"),
            ("ocr_model_v1", "ocrModelId"),
            ("compress_model_v1", "compressModelId"),
            ("fast_model_v1", "fastModelId")):
        mapped = _resolve(source.get(source_key))
        if mapped:
            output[target_key] = mapped
    for source_key, target_key in (
            ("title_prompt_v1", "titlePrompt"),
            ("suggestion_prompt_v1", "suggestionPrompt"),
            ("translate_prompt_v1", "translatePrompt"),
            ("ocr_prompt_v1", "ocrPrompt"),
            ("compress_prompt_v1", "compressPrompt")):
        value = source.get(source_key)
        if isinstance(value, str) and value:
            output[target_key] = convert_prompt_vars(value, to_rikkahub=True)
    output["enableAutoTitle"] = bool(
        source.get("title_generation_enabled_v1", True))
    output["enableSuggestion"] = bool(
        source.get("suggestion_generation_enabled_v1", True))
    if isinstance(source.get("translate_target_lang_v1"), str):
        output["translateTargetLanguage"] = source["translate_target_lang_v1"]
    if isinstance(source.get("translate_thinking_budget_v1"), int):
        output["translateThinkingBudget"] = source["translate_thinking_budget_v1"]


def _resolve_active_models(settings: dict[str, Any],
                           providers: list[dict[str, Any]],
                           diagnostics: list[Diagnostic]) -> None:
    models = [entry for provider in providers
              for entry in provider.get("models", [])
              if isinstance(entry, dict)]
    by_id = {str(entry.get("id")) for entry in models}
    if settings.get("chatModelId") not in by_id:
        if models:
            settings["chatModelId"] = models[0]["id"]
        else:
            settings.pop("chatModelId", None)
    for key in ("titleModelId", "suggestionModelId", "translateModeId",
                "ocrModelId", "compressModelId", "fastModelId",
                "imageGenerationModelId"):
        if key in settings and settings[key] not in by_id:
            settings.pop(key, None)
            diagnostics.append(_diag(
                "identity_reference_unresolved", "settings model", 1,
                f"{key} 指向的模型不在可迁移范围内，已清空"))


# ---------------------------------------------------------------------------
# Memory projection
# ---------------------------------------------------------------------------

def _assistant_flags_from_rh_settings(
    settings: dict[str, Any]) -> dict[str, AssistantMemoryFlags]:
    flags: dict[str, AssistantMemoryFlags] = {}
    for assistant in _as_list(settings.get("assistants")):
        if not isinstance(assistant, dict) or not assistant.get("id"):
            continue
        flags[str(assistant["id"])] = AssistantMemoryFlags(
            use_global=bool(assistant.get("useGlobalMemory")),
            enable=bool(assistant.get("enableMemory", True)),
        )
    return flags


def _memory_r2k(source: Backup, assistants: list[dict[str, Any]],
                settings: SettingsResult,
                *, source_flags: dict[str, AssistantMemoryFlags]
                ) -> tuple[list[MemoryEntry], list[Diagnostic]]:
    """RikkaHub markdown files -> Kelivo typed entries."""
    diagnostics: list[Diagnostic] = []
    payload = source.payload
    assert isinstance(payload, RikkaPayload)
    docs: list[MemoryDoc] = []
    legacy_entries: list[MemoryEntry] = []
    memory_table = payload.tables.get("memory_file")
    if memory_table is not None:
        assistant_index = memory_table.columns.index("assistant_id")
        path_index = memory_table.columns.index("path")
        content_index = memory_table.columns.index("content")
        updated_index = memory_table.columns.index("updated_at")
        for row in memory_table.rows:
            docs.append(MemoryDoc(
                assistant_id=str(row[assistant_index]),
                path=str(row[path_index]),
                content=str(row[content_index] or ""),
                updated_ms=int(row[updated_index] or 0),
            ))
    legacy_table = payload.tables.get("MemoryEntity")
    if legacy_table is not None:
        assistant_index = legacy_table.columns.index("assistant_id")
        content_index = legacy_table.columns.index("content")
        for row in legacy_table.rows:
            legacy_entries.append(MemoryEntry(
                id=str(uuidlib.uuid5(
                    _MEMORY_NS,
                    f"legacy:{row[assistant_index]}:{row[content_index]}")),
                scope="assistant",
                assistant_id=str(row[assistant_index]),
                type="instruction",
                status="active",
                content=str(row[content_index] or ""),
                source="manual",
            ))
    entries: list[MemoryEntry] = []
    flag_map = {str(a["id"]): a for a in assistants}
    dropped_blank = 0
    global_opt_in = {
        assistant_id for assistant_id, flags in source_flags.items()
        if flags.use_global and assistant_id in flag_map}
    for doc in docs:
        if not doc.content.strip():
            dropped_blank += 1
            continue
        if doc.assistant_id == "__global__":
            targets = global_opt_in
        else:
            targets = {_entity_uuid(doc.assistant_id, "assistant")}
        for owner in targets:
            if owner not in flag_map:
                diagnostics.append(_diag(
                    "memory_visibility_changed", "memory", 1,
                    "共享记忆的目标助手不在迁移范围内，已跳过"))
                continue
            status = "active"
            if doc.assistant_id == "__global__":
                status = "active"
            elif source_flags.get(owner) and source_flags[owner].use_global:
                status = "archived"
            entry = MemoryEntry(
                id=str(uuidlib.uuid5(
                    _MEMORY_NS, f"{doc.assistant_id}:{doc.path}:{owner}")),
                scope="assistant",
                assistant_id=owner,
                type="identity" if doc.path == "/profile.md" else "instruction",
                status=status,
                content=doc.content,
                source="manual",
                created_us=doc.updated_ms * 1000,
                updated_us=doc.updated_ms * 1000,
            )
            entries.append(entry)
    for entry in legacy_entries:
        if not entry.content.strip():
            dropped_blank += 1
            continue
        entry.assistant_id = _entity_uuid(entry.assistant_id, "assistant")
        entries.append(entry)
    if dropped_blank:
        diagnostics.append(_diag(
            "memory_blank_content_unsupported", "memory", dropped_blank,
            "空白记忆文件无法通过目标校验，已丢弃"))
    if docs:
        diagnostics.append(_diag(
            "value_defaulted", "memory", len(docs),
            "源记忆没有创建时钟，createdAt 使用 updatedAt"))
    if any(doc.assistant_id == "__global__" for doc in docs):
        if global_opt_in:
            diagnostics.append(_diag(
                "memory_visibility_changed", "memory", len(global_opt_in),
                "共享记忆变成每个助手的独立副本"))
        else:
            diagnostics.append(_diag(
                "unsupported_data", "memory", 1,
                "没有启用共享记忆的助手，共享文件已丢弃"))
    return entries, diagnostics


def _memory_entry_payload(entry: MemoryEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": entry.id,
        "scope": entry.scope,
        "assistantId": entry.assistant_id,
        "type": entry.type,
        "status": entry.status,
        "content": entry.content,
        "source": entry.source,
        "relatedIds": entry.related_ids,
        "createdAt": entry.created_us,
        "updatedAt": entry.updated_us,
    }
    return payload


def _memory_k2r(source: Backup, assistants: list[dict[str, Any]],
                assistant_ids: dict[str, str],
                diagnostics: list[Diagnostic], *,
                legacy_target: bool = False
                ) -> tuple[list[MemoryEntry], list[MemoryDoc]]:
    """Kelivo typed entries -> RikkaHub markdown files."""
    payload = source.payload
    assert isinstance(payload, KelivoPayload)
    settings = payload.settings
    entries: list[MemoryEntry] = []
    raw = _as_list(_parse(settings.get("memory_entries_v1")))
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        entries.append(MemoryEntry(
            id=str(item.get("id") or f"memory-{index}"),
            scope=str(item.get("scope") or "assistant"),
            assistant_id=item.get("assistantId")
            if isinstance(item.get("assistantId"), str) else None,
            type=str(item.get("type") or "instruction"),
            status=str(item.get("status") or "active"),
            content=content,
            source=str(item.get("source") or "manual"),
            created_us=int(item.get("createdAt") or 0)
            if isinstance(item.get("createdAt"), int) else 0,
            updated_us=int(item.get("updatedAt") or 0)
            if isinstance(item.get("updatedAt"), int) else 0,
            native={"payload": dict(item)},
        ))
    legacy = payload.tables.get("assistant_memory_rows")
    if legacy is not None:
        assistant_index = legacy.columns.index("assistant_id")
        payload_index = legacy.columns.index("payload")
        for row in legacy.rows:
            data = _as_dict(_parse(row[payload_index]))
            content = data.get("content")
            if not isinstance(content, str):
                continue
            entries.append(MemoryEntry(
                id=str(data.get("id") or uuidlib.uuid5(
                    _MEMORY_NS, str(row[payload_index]))),
                scope="assistant",
                assistant_id=str(row[assistant_index]),
                type="instruction",
                status="active",
                content=content,
                source="manual",
            ))
    blank = [entry for entry in entries if not entry.content.strip()]
    if blank:
        diagnostics.append(_diag(
            "memory_blank_content_unsupported", "memory", len(blank),
            "空白记忆条目无法通过目标校验，已丢弃"))
    entries = [entry for entry in entries if entry.content.strip()]
    active = [entry for entry in entries if entry.status == "active"]
    archived = len(entries) - len(active)
    if archived:
        diagnostics.append(_diag(
            "unsupported_data", "memory", archived,
            "RikkaHub 记忆没有 archived 状态，已丢弃归档条目"))
    target_assistants = [str(a["id"]) for a in assistants if a.get("id")]
    global_entries = [entry for entry in active if entry.scope == "global"]
    private: dict[str, list[MemoryEntry]] = {}
    for entry in active:
        if entry.scope == "global":
            continue
        owner = entry.assistant_id or ""
        private.setdefault(owner, []).append(entry)
    target_has_memory = bool(target_assistants)
    docs: list[MemoryDoc] = []
    if global_entries and not target_has_memory:
        diagnostics.append(_diag(
            "unsupported_data", "memory", len(global_entries),
            "目标没有助手，全局记忆无处放置"))
    skipped_orphans = 0
    for owner in target_assistants:
        selected = list(global_entries) + list(private.get(owner, []))
        if not selected:
            continue
        if legacy_target:
            docs.extend(_legacy_memory_docs(owner, selected, diagnostics))
        else:
            docs.extend(_aggregate_memory(owner, selected, diagnostics))
    used = set(target_assistants)
    for owner, owner_entries in private.items():
        if owner in used:
            continue
        if not owner_entries:
            continue
        mapped_owner = _entity_uuid(owner, "assistant") if owner else str(
            uuidlib.uuid5(_MEMORY_NS, "orphan"))
        if legacy_target:
            docs.extend(_legacy_memory_docs(
                mapped_owner, owner_entries, diagnostics))
        else:
            docs.extend(_aggregate_memory(
                mapped_owner, owner_entries, diagnostics))
        skipped_orphans += 1
    if skipped_orphans:
        diagnostics.append(_diag(
            "memory_visibility_changed", "memory", skipped_orphans,
            "没有对应助手的私有记忆按确定身份保留"))
    if global_entries and target_has_memory:
        diagnostics.append(_diag(
            "memory_visibility_changed", "memory", len(global_entries),
            "全局记忆已写入每个助手的独立记忆库"))
    return [], docs


def _legacy_memory_docs(owner: str, entries: list[MemoryEntry],
                        diagnostics: list[Diagnostic]) -> list[MemoryDoc]:
    docs: list[MemoryDoc] = []
    for entry in entries:
        if not entry.content.strip():
            continue
        docs.append(MemoryDoc(
            assistant_id=owner,
            path=f"/memory/{entry.id}.md",
            content=entry.content,
            updated_ms=entry.updated_us // 1000,
        ))
    return docs


def _aggregate_memory(owner: str, entries: list[MemoryEntry],
                      diagnostics: list[Diagnostic]) -> list[MemoryDoc]:
    identity = sorted(
        (entry for entry in entries if entry.type == "identity"),
        key=lambda item: (item.created_us, item.id))
    instructions = sorted(
        (entry for entry in entries if entry.type != "identity"),
        key=lambda item: (_MEMORY_SECTION_ORDER.index(item.type)
                          if item.type in _MEMORY_SECTION_ORDER else 3,
                          item.created_us, item.id))
    docs: list[MemoryDoc] = []
    updated = max((entry.updated_us for entry in entries), default=0) // 1000
    if identity:
        content = _memory_document("Identity", identity)
        docs.append(_memory_doc(owner, "/profile.md", content, updated,
                                diagnostics))
    if instructions:
        sections: dict[str, list[MemoryEntry]] = {}
        for entry in instructions:
            name = entry.type if entry.type in _MEMORY_SECTION_ORDER \
                else "instruction"
            sections.setdefault(name, []).append(entry)
        parts = []
        for name in _MEMORY_SECTION_ORDER:
            if name in sections:
                parts.append(_memory_document(name.title(), sections[name]))
        docs.append(_memory_doc(owner, "/preferences.md", "\n\n".join(parts),
                                updated, diagnostics))
    if updated == 0:
        diagnostics.append(_diag(
            "value_defaulted", "memory", 1,
            "源记忆没有时钟，文件 updated_at 为 0"))
    return docs


def _memory_document(heading: str, entries: list[MemoryEntry]) -> str:
    lines = [f"## {heading}"]
    for index, entry in enumerate(entries, start=1):
        lines.append("")
        lines.append(f"### Memory {index}")
        lines.append(entry.content)
    return "\n".join(lines)


def _memory_doc(owner: str, path: str, content: str, updated_ms: int,
                diagnostics: list[Diagnostic]) -> MemoryDoc:
    if len(content.encode("utf-8")) > 48 * 1024:
        diagnostics.append(_diag(
            "value_defaulted", "memory", 1,
            "记忆文件超过 48KiB，目标应用的原生编辑增长限制可能影响后续扩展"))
    return MemoryDoc(assistant_id=owner, path=path, content=content,
                     updated_ms=updated_ms, pinned=path in (
                         "/profile.md", "/preferences.md"))
