"""Settings bridge between RikkaHub and Kelivo (cuplivo) settings files.

The two apps are independent projects with different settings schemas, so this
bridge is best-effort by design:

- Keys that exist in both apps with the same name and value type are copied
  verbatim (verified byte-identical in the reference backups).
- RikkaHub stores providers/assistants/mcpServers/TTS/quick phrases/mode
  injections as native JSON lists; Kelivo (cuplivo) stores the same concepts
  as JSON-stringified preference values (`provider_configs_v1`,
  `assistants_v1`, `mcp_servers_v1`, `tts_services_v1`, `quick_phrases_v1`,
  `instruction_injections_v1`).  The bridge converts between the two with
  per-field mappings harvested from both apps' sources (kotlinx-serialization
  data classes for RikkaHub, ProviderConfig/Assistant toJson for Kelivo).
- Target-only keys are filled with the target app's own defaults so the
  resulting settings.json is structurally complete.

RikkaHub restores settings leniently (Json { ignoreUnknownKeys = true;
encodeDefaults = true }), so converted entries must only avoid wrong *types*
and invalid enum/discriminator values.

Every conversion returns (settings_dict, notes) where notes is a list of
human-readable warnings about lossy mappings.
"""

from __future__ import annotations

import json
import re
import uuid as uuidlib
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Shared keys: copied verbatim (same name, same JSON type in both apps)
# ---------------------------------------------------------------------------
# Prompt-bearing shared keys: their placeholder variables need rewriting.
_PROMPT_KEYS = ("titlePrompt", "translatePrompt", "suggestionPrompt",
                "ocrPrompt", "compressPrompt")
SHARED_KEYS: tuple[str, ...] = (
    "themeId", "customThemes", "developerMode", "dynamicColor",
    "favoriteModels", "chatModelId", "fastModelId", "imageGenerationModelId",
    "titlePrompt", "translateModeId", "translatePrompt",
    "translateThinkingBudget", "enableSuggestion", "suggestionPrompt",
    "ocrModelId", "ocrPrompt", "compressModelId", "compressPrompt",
    "assistantId", "assistantTags", "defaultTTSPlaybackSpeed",
    "launchCount", "sponsorAlertDismissedAt", "searchServiceSelected",
    "selectedTTSProviderId", "selectedASRProviderId",
    "webServerEnabled", "webServerPort", "webServerJwtEnabled",
    "webServerAccessPassword", "webServerLocalhostOnly",
    "searchServices", "asrProviders", "lorebooks",
)

# ---------------------------------------------------------------------------
# RikkaHub-side constants (harvested from rikkahub source / reference backup)
# ---------------------------------------------------------------------------
RH_FILES_PREFIX = "file:///data/user/0/me.rerere.rikkahub/files/"
RH_AVATAR_DUMMY = {"type": "me.rerere.rikkahub.data.model.Avatar.Dummy"}

RH_DISPLAY_DEFAULTS: dict[str, Any] = {
    "userAvatar": RH_AVATAR_DUMMY,
    "userNickname": "",
    "useAppIconStyleLoadingIndicator": True,
    "showUserAvatar": True,
    "showAssistantBubble": False,
    "bubbleOpacity": 1.0,
    "showModelIcon": True,
    "showModelName": True,
    "showDateTimeInMessage": False,
    "showTokenUsage": True,
    "showThinkingContent": True,
    "autoCloseThinking": True,
    "showUpdates": True,
    "showMessageJumper": True,
    "messageJumperOnLeft": False,
    "fontSizeRatio": 1.0,
    "enableMessageGenerationHapticEffect": False,
    "skipCropImage": True,
    "enableNotificationOnMessageGeneration": False,
    "enableLiveUpdateNotification": False,
    "codeBlockAutoWrap": False,
    "codeBlockAutoCollapse": False,
    "showLineNumbers": False,
    "ttsOnlyReadQuoted": False,
    "ttsOnlyReadOutsideBrackets": False,
    "autoPlayTTSAfterGeneration": False,
    "pasteLongTextAsFile": False,
    "pasteLongTextThreshold": 1000,
    "sendOnEnter": False,
    "enableAutoScroll": True,
    "enableLatexRendering": True,
    "enableBlurEffect": False,
    "chatFontFamily": "default",
    "chatCustomFontPath": "",
    "chatCustomFontName": "",
    "enableVolumeKeyScroll": False,
    "volumeKeyScrollRatio": 1.0,
}

RH_ONLY_DEFAULTS: dict[str, Any] = {
    "titleModelId": None,
    "suggestionModelId": None,
    "displaySetting": RH_DISPLAY_DEFAULTS,
    "searchCommonOptions": {"resultSize": 10},
    "webDavConfig": {"url": "", "username": "", "password": "",
                     "path": "rikkahub_backups", "items": ["DATABASE", "FILES"]},
    "s3Config": {"endpoint": "", "accessKeyId": "", "secretAccessKey": "",
                 "bucket": "", "region": "auto", "pathStyle": True,
                 "items": ["DATABASE", "FILES"]},
    "backupReminderConfig": {"enabled": False, "intervalDays": 7,
                             "lastBackupTime": 0},
    "modeInjections": [],
    "quickMessages": [],
    "providers": [],
    "assistants": [],
    "mcpServers": [],
    "ttsProviders": [],
}

RH_ASSISTANT_DEFAULTS: dict[str, Any] = {
    "tags": [],
    "contextMessageLimit": 0,
    "useGlobalMemory": False,
    "presetMessages": [],
    "quickMessageIds": [],
    "regexes": [],
    "reasoningLevel": "auto",
    "customHeaders": [],
    "customBodies": [],
    "mcpServers": [],
    "localTools": ["time_info"],
    "enableWebSearch": False,
    "workspaceId": None,
    "backgroundOpacity": 1.0,
    "useGradientBackground": False,
    "modeInjectionIds": [],
    "lorebookIds": [],
    "enabledSkills": [],
    "enableTimeReminder": False,
    "allowConversationSystemPrompt": False,
    "allowConversationPromptInjection": False,
}

RH_PROVIDER_DEFAULTS: dict[str, Any] = {
    "type": "openai",
    "enabled": True,
    "apiKey": "",
    "baseUrl": "https://api.openai.com/v1",
    "chatCompletionsPath": "/chat/completions",
    "useResponseApi": False,
    "includeHistoryReasoning": True,
    "balanceOption": {"enabled": False, "apiPath": "/credits",
                      "resultPath": "data.total_usage"},
}

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

# ---------------------------------------------------------------------------
# Kelivo-side constants (harvested from cuplivo source / reference backup)
# ---------------------------------------------------------------------------
KL_ONLY_DEFAULTS: dict[str, Any] = {
    "android_background_chat_mode_v1": "on_notify",
    "app_launch_count_v1": 3,
    "app_locale_v1": "system",
    "display_app_font_family_v1": "Noto Serif SC",
    "display_app_font_is_google_v1": True,
    "display_code_font_family_v1": "Ubuntu Mono",
    "display_code_font_is_google_v1": True,
    "display_collapse_thinking_steps_v1": True,
    "display_show_tool_result_summary_v1": True,
    "suggestion_insert_on_tap_only_v1": True,
    "display_new_chat_on_assistant_switch_v1": True,
    "display_new_chat_after_delete_v1": True,
    "display_auto_collapse_code_block_v1": True,
    "display_use_new_assistant_avatar_ux_v1": True,
    "display_show_provider_in_chat_message_v1": True,
    "display_auto_collapse_code_block_lines_v1": 10,
    "display_show_chat_list_date_v1": True,
    "image_cropper_enabled_v1": True,
    "display_chat_message_background_style_v1": "frosted",
    "display_haptics_on_generate_v1": True,
    "display_use_pure_background_v1": False,
    "display_enter_to_send_on_mobile_v1": False,
    "display_desktop_show_tray_v1": False,
    "display_desktop_minimize_to_tray_on_close_v1": False,
    "global_proxy_bypass_v1":
        "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1",
    "mcp_request_timeout_ms_v1": 30000,
    "provider_group_collapsed_v1": "{}",
    "provider_group_map_v1": "{}",
    "providers_order_v1": ["OpenAI", "SiliconFlow", "Gemini", "OpenRouter",
                           "KelivoIN", "Tensdaq", "DeepSeek", "AIhubmix",
                           "Aliyun", "Zhipu AI", "Claude", "Grok",
                           "ByteDance"],
    # RikkaHub-shaped mirror keys stay empty, like a fresh Kelivo install
    # (Kelivo keeps its data in the *_v1 preference strings).
    "providers": [],
    "assistants": [],
    "mcpServers": [],
    "quickMessages": [],
    "modeInjections": [],
    "ttsProviders": [],
    "searchServices": [],
    "asrProviders": [],
    "lorebooks": [],
    "assistantTags": [],
    "favoriteModels": [],
    "customThemes": [],
}

KL_ASSISTANT_DEFAULTS: dict[str, Any] = {
    "useAssistantName": True,
    "chatModelProvider": None,
    "contextMessageSize": 64,
    "limitContextMessages": True,
    "streamOutput": True,
    "thinkingBudget": 0,
    "searchEnabled": False,
    "mcpServerIds": [],
    "localToolIds": [],
    "skillIds": [],
    "background": None,
    "customHeaders": [],
    "customBody": [],
    "enableMemory": True,
    "memoryMode": "tool",
    "enableRecentChatsReference": False,
    "recentChatsSummaryMessageCount": 5,
    "memoryRecordPrompt": "",
    "presetMessages": "[]",
    "regexRules": [],
    "enableProactiveCare": False,
    "proactiveCareNextMessageAt": None,
    "proactiveCarePrompt": "",
    "proactiveCareDecisionPrompt": "",
    "docxMode": "extract",
    "pdfMode": "extract",
    "otherOfficeMode": "direct",
    "ocrMode": "auto",
    "enableTimeInjection": False,
    "discoverable": False,
    "handoffId": None,
    "handoffDescription": None,
}

KL_PROVIDER_DEFAULTS: dict[str, Any] = {
    "enableToolResultImages": None,
    "vertexAI": None,
    "location": None,
    "projectId": None,
    "serviceAccountJson": None,
    "proxyEnabled": None,
    "proxyType": None,
    "proxyHost": None,
    "proxyPort": None,
    "proxyUsername": None,
    "proxyPassword": None,
    "avatarType": None,
    "avatarValue": None,
    "multiKeyEnabled": None,
    "apiKeys": None,
    "keyManagement": None,
    "aihubmixAppCodeEnabled": None,
    "balanceEnabled": None,
    "balanceApiPath": None,
    "balanceResultPath": None,
    "claudePromptCachingEnabled": False,
    "claudePromptCachingTtl": "5m",
    "customHeaders": None,
    "customBody": None,
}

# Kelivo ships three built-in in-memory MCP servers (see cuplivo
# mcp_provider.dart `_ensureBuiltin*ServerPresent`).  They are injected into
# Kelivo backups converted from RikkaHub (which has no equivalents) and
# stripped when converting away from Kelivo.
KL_BUILTIN_MCPS: list[dict[str, Any]] = [
    {"id": "kelivo_fetch", "enabled": True,
     "name": "@kelivo/fetch", "transport": "inmemory", "tools": []},
    {"id": "kelivo_subagent", "enabled": True,
     "name": "@kelivo/subagent", "transport": "inmemory", "tools": []},
    {"id": "kelivo_filesystem", "enabled": False,
     "name": "@kelivo/filesystem", "transport": "inmemory", "tools": []},
]
KL_BUILTIN_MCP_IDS = {m["id"] for m in KL_BUILTIN_MCPS}
KL_BUILTIN_MCP_NAMES = {m["name"] for m in KL_BUILTIN_MCPS}

# Fields the converter keeps when mapping a Kelivo assistant onto the
# RikkaHub shape (everything else is dropped and reported).
_RH_ASSISTANT_KEPT = {
    "id", "name", "avatar", "useAssistantAvatar", "chatModelId",
    "chatModelProvider", "temperature", "topP", "maxTokens", "background",
    "streamOutput", "systemPrompt", "messageTemplate", "enableMemory",
    "enableRecentChatsReference", "customHeaders", "customBody",
    "presetMessages", "skillIds",
}
# Fields kept when mapping a RikkaHub assistant onto the Kelivo shape.
_KL_ASSISTANT_KEPT = {
    "id", "name", "avatar", "useAssistantAvatar", "chatModelId",
    "temperature", "topP", "maxTokens", "background", "streamOutput",
    "systemPrompt", "messageTemplate", "enableMemory",
    "enableRecentChatsReference", "customHeaders", "customBodies",
    "presetMessages", "enabledSkills",
}

_MODEL_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-model")
_PROVIDER_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-provider")
_ENTITY_NS = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "rikkelivo-entity")

# Prompt placeholder variables: RikkaHub renders both `{{key}}` and `{key}`
# (PlaceholderTransformer.kt replaces both forms) with its own key set;
# Kelivo (cuplivo) renders only single-brace `{key}` placeholders in
# system prompts (prompt_transformer.dart buildPlaceholders) and its key set
# differs: {assistant_name}/{nickname} vs RikkaHub's {char}/{user}, and
# Kelivo has {cur_time}/{cur_datetime} which RikkaHub lacks.  Message
# templates use the same `{{ message }}` mustache style in both apps and are
# left untouched.
#
# RikkaHub key -> Kelivo key (keys absent here keep their name; RikkaHub-only
# keys without a Kelivo counterpart map onto the closest equivalent).
_PROMPT_VAR_K2K: dict[str, str] = {
    "char": "assistant_name",
    "user": "nickname",
    "cur_date": "cur_date",
    "cur_time": "cur_time",  # no RikkaHub counterpart; keep name
    "cur_datetime": "cur_datetime",  # no RikkaHub counterpart; keep name
    "model_id": "model_id",
    "model_name": "model_name",
    "locale": "locale",
    "timezone": "timezone",
    "system_version": "system_version",
    "device_info": "device_info",
    "battery_level": "battery_level",
    "nickname": "nickname",
}
# Kelivo key -> RikkaHub key
_PROMPT_VAR_K2R: dict[str, str] = {
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
    """Rewrite prompt placeholder variables between the two apps' formats.

    `to_rikkahub=True`: Kelivo `{key}` -> RikkaHub `{{key}}` (single braces
    also work in RikkaHub, but double is canonical and unambiguous).
    `to_rikkahub=False`: RikkaHub `{{key}}`/`{key}` -> Kelivo `{key}`.
    Unknown keys are left verbatim (RikkaHub would render them literally
    either way; Kelivo tolerates them too).
    """
    table = _PROMPT_VAR_K2R if to_rikkahub else _PROMPT_VAR_K2K

    def _sub(match: re.Match) -> str:
        open_, key, close = match.group(1), match.group(2), match.group(3)
        if to_rikkahub:
            if close == "}}":
                return match.group(0)  # already double-braced, keep form
            target = table.get(key)
            return f"{{{{{target}}}}}" if target else match.group(0)
        target = table.get(key)
        if not target:
            return match.group(0)
        return f"{{{target}}}"

    # Single pass; double braces are consumed atomically by the regex.
    return _PROMPT_VAR_RE.sub(_sub, text)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _entity_uuid(value: Any, namespace: str = "") -> str:
    """Normalize an arbitrary id to a legal UUID.

    RikkaHub deserializes id fields as kotlin.uuid.Uuid; any non-UUID string
    (Kelivo uses human-readable ids like "KelivoIN" or "kelivo_fetch") would
    crash the restore, so ids are mapped deterministically.
    """
    if _is_uuid(value):
        return str(value)
    seed = f"{namespace}:{value}" if namespace else str(value)
    return str(uuidlib.uuid5(_ENTITY_NS, seed))


# Built-in providers shipped by both apps, matched by (baseUrl, providerType)
# K->R and by RikkaHub provider UUID R->K.  Reusing the RikkaHub built-in UUID
# keeps provider ids stable and avoids duplicate provider entries; the
# RikkaHub ids below match the reference backup's own provider list.
BUILTIN_PROVIDERS: list[dict[str, str]] = [
    {"kelivo_key": "OpenAI", "type": "openai",
     "url": "https://api.openai.com/v1",
     "rh_id": "1eeea727-9ee5-4cae-93e6-6fb01a4d051e", "rh_name": "OpenAI"},
    {"kelivo_key": "SiliconFlow", "type": "openai",
     "url": "https://api.siliconflow.cn/v1",
     "rh_id": "56a94d29-c88b-41c5-8e09-38a7612d6cf8", "rh_name": "硅基流动"},
    {"kelivo_key": "Gemini", "type": "google",
     "url": "https://generativelanguage.googleapis.com/v1beta",
     "rh_id": "6ab18148-c138-4394-a46f-1cd8c8ceaa6d", "rh_name": "Gemini"},
    {"kelivo_key": "OpenRouter", "type": "openai",
     "url": "https://openrouter.ai/api/v1",
     "rh_id": "d5734028-d39b-4d41-9841-fd648d65440e", "rh_name": "OpenRouter"},
    {"kelivo_key": "DeepSeek", "type": "openai",
     "url": "https://api.deepseek.com/v1",
     "rh_id": "f099ad5b-ef03-446d-8e78-7e36787f780b", "rh_name": "DeepSeek"},
    {"kelivo_key": "AIhubmix", "type": "openai",
     "url": "https://aihubmix.com/v1",
     "rh_id": "1b1395ed-b702-4aeb-8bc1-b681c4456953", "rh_name": "AiHubMix"},
    {"kelivo_key": "Aliyun", "type": "openai",
     "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "rh_id": "f76cae46-069a-4334-ab8e-224e4979e58c", "rh_name": "阿里云百炼"},
    {"kelivo_key": "Zhipu AI", "type": "openai",
     "url": "https://open.bigmodel.cn/api/paas/v4",
     "rh_id": "3bc40dc1-b11a-46fa-863b-6306971223be", "rh_name": "智谱AI开放平台"},
    {"kelivo_key": "Grok", "type": "openai",
     "url": "https://api.x.ai/v1",
     "rh_id": "ff3cde7e-0f65-43d7-8fb2-6475c99f5990", "rh_name": "xAI"},
    {"kelivo_key": "ByteDance", "type": "openai",
     "url": "https://ark.cn-beijing.volces.com/api/v3",
     "rh_id": "3dfd6f9b-f9d9-417f-80c1-ff8d77184191", "rh_name": "火山引擎"},
]


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


def _find_builtin_by_kelivo(entry: dict[str, Any], key: str) -> dict[str, str] | None:
    ptype = str(entry.get("providerType") or "").lower()
    base = _normalize_url(str(entry.get("baseUrl") or ""))
    for b in BUILTIN_PROVIDERS:
        if _normalize_url(b["url"]) != base:
            continue
        btype = b["type"]
        if ptype in ("openai", "google", "claude") and ptype != btype:
            continue
        if not ptype and key == b["kelivo_key"]:
            return b
        if ptype == btype or (ptype == "" and key == b["kelivo_key"]):
            return b
    return None


def _find_builtin_by_rh_id(rh_id: str) -> dict[str, str] | None:
    for b in BUILTIN_PROVIDERS:
        if b["rh_id"] == rh_id:
            return b
    return None


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _model_uuid(provider_id: str, model_name: str) -> str:
    return str(uuidlib.uuid5(_MODEL_NS, f"{provider_id}:{model_name}"))


def _upper_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return ["TEXT"]
    out = [str(v).upper() for v in values]
    return out or ["TEXT"]


def _lower_list(values: Any, default: list[str]) -> list[str]:
    if not isinstance(values, list):
        return default
    out = [str(v).lower() for v in values]
    return out or default


def _override_map(overrides: Any) -> dict[str, Any]:
    return overrides if isinstance(overrides, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_json_str(value: Any) -> Any:
    """Parse a Kelivo *_v1 JSON-stringified preference value."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _json_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Kelivo -> RikkaHub entry converters
# ---------------------------------------------------------------------------

def _convert_provider_to_rh(entry: dict[str, Any], key: str) -> dict[str, Any]:
    provider_type = str(entry.get("providerType") or "openai").lower()
    if provider_type in ("google", "gemini"):
        provider_type = "google"
    elif provider_type in ("claude", "anthropic"):
        provider_type = "claude"
    else:
        provider_type = "openai"

    builtin = _find_builtin_by_kelivo(entry, key)
    provider_id = (builtin["rh_id"] if builtin
                   else _entity_uuid(entry.get("id") or key, "provider"))

    out: dict[str, Any] = dict(RH_PROVIDER_DEFAULTS)
    out["type"] = provider_type
    out["id"] = provider_id
    out["enabled"] = bool(entry.get("enabled", True))
    out["name"] = str(entry.get("name") or (builtin["rh_name"] if builtin else ""))
    if entry.get("apiKey") is not None:
        out["apiKey"] = str(entry["apiKey"])
    if entry.get("baseUrl") is not None:
        out["baseUrl"] = str(entry["baseUrl"])
    if entry.get("chatPath") is not None:
        out["chatCompletionsPath"] = str(entry["chatPath"])
    if entry.get("useResponseApi") is not None:
        out["useResponseApi"] = bool(entry["useResponseApi"])

    overrides = _override_map(entry.get("modelOverrides"))
    models: list[dict[str, Any]] = []
    for name in _as_list(entry.get("models")):
        name = str(name)
        ov = overrides.get(name) if isinstance(overrides, dict) else None
        ov = ov if isinstance(ov, dict) else {}
        model = dict(RH_MODEL_DEFAULTS)
        model["modelId"] = name
        model["displayName"] = str(ov.get("name") or name)
        model["id"] = _model_uuid(provider_id, name)
        mtype = str(ov.get("type") or "chat").upper()
        if mtype not in ("CHAT", "IMAGE", "EMBEDDING"):
            mtype = "CHAT"
        model["type"] = mtype
        model["inputModalities"] = _upper_list(ov.get("input"))
        model["outputModalities"] = _upper_list(ov.get("output"))
        abilities = _as_list(ov.get("abilities"))
        model["abilities"] = [str(a).upper() for a in abilities]
        models.append(model)
    out["models"] = models
    return out


def _convert_assistant_to_rh(
    entry: dict[str, Any],
    *,
    model_by_key: dict[tuple[str, str], str] | None = None,
    model_by_name: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Map a Kelivo assistant onto the RikkaHub shape.

    Returns (assistant, note).  ``note`` is a loss warning when the
    assistant's chatModelId could not be resolved to a RikkaHub model uuid:
    RikkaHub deserializes chatModelId as a kotlin.uuid.Uuid, so a raw Kelivo
    model-name string would fail the whole settings restore.
    """
    out: dict[str, Any] = dict(RH_ASSISTANT_DEFAULTS)
    note: str | None = None
    out["id"] = _entity_uuid(entry.get("id"), "assistant")
    out["name"] = str(entry.get("name") or "")
    avatar = entry.get("avatar")
    if isinstance(avatar, dict) and avatar.get("type"):
        out["avatar"] = avatar
    elif isinstance(avatar, str) and avatar:
        # Kelivo stores avatars as plain path/url/base64 strings.
        out["avatar"] = {"type": "me.rerere.rikkahub.data.model.Avatar.Image",
                          "url": avatar}
    else:
        out["avatar"] = RH_AVATAR_DUMMY
    if entry.get("useAssistantAvatar") is not None:
        out["useAssistantAvatar"] = bool(entry["useAssistantAvatar"])
    if entry.get("chatModelId") is not None:
        model_uuid = None
        name = str(entry["chatModelId"])
        provider_key = entry.get("chatModelProvider")
        if model_by_key is not None and provider_key:
            model_uuid = model_by_key.get((str(provider_key), name))
        if model_uuid is None and not provider_key \
                and model_by_name is not None:
            # Unqualified model name: safe only when it is unique across
            # all providers.
            model_uuid = model_by_name.get(name)
        if model_uuid:
            out["chatModelId"] = model_uuid
        else:
            # Never emit a non-UUID identifier: RikkaHub's restore would
            # fail decoding settings.json.  Fall back to the global default
            # model and warn about the loss.
            out["chatModelId"] = None
            note = (f"助手「{out['name'] or entry.get('id') or '?'}」的模型"
                    f"「{name}」无法解析为 RikkaHub 模型 UUID，"
                    f"已回退为全局默认模型")
    for field in ("temperature", "topP", "maxTokens", "background"):
        if entry.get(field) is not None:
            out[field] = entry[field]
    if entry.get("streamOutput") is not None:
        out["streamOutput"] = bool(entry["streamOutput"])
    for field in ("systemPrompt", "messageTemplate"):
        if entry.get(field) is not None:
            value = str(entry[field])
            if field == "systemPrompt":
                value = convert_prompt_vars(value, to_rikkahub=True)
            out[field] = value
    if entry.get("enableMemory") is not None:
        out["enableMemory"] = bool(entry["enableMemory"])
    if entry.get("enableRecentChatsReference") is not None:
        out["enableRecentChatsReference"] = bool(entry["enableRecentChatsReference"])
    if entry.get("customHeaders") is not None:
        out["customHeaders"] = _as_list(entry["customHeaders"])
    if entry.get("customBody") is not None:
        out["customBodies"] = _as_list(entry["customBody"])
    # Kelivo skillIds (skill names) -> RikkaHub enabledSkills.
    if entry.get("skillIds"):
        out["enabledSkills"] = [str(s) for s in _as_list(entry["skillIds"])]
    presets = _parse_json_str(entry.get("presetMessages"))
    if isinstance(presets, list):
        out["presetMessages"] = [
            {
                "id": str(p.get("id") or uuidlib.uuid4()),
                "role": str(p.get("role") or "user"),
                "parts": [{"type": "text",
                           "text": str(p.get("content") or ""),
                           "metadata": None}],
                "annotations": [],
                "createdAt": _now_iso(),
                "finishedAt": None,
                "modelId": None,
                "usage": None,
                "translation": None,
            }
            for p in presets if isinstance(p, dict)
        ]
    return out, note


def _convert_mcp_to_rh(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Kelivo MCP server -> RikkaHub.  Returns None for Kelivo built-ins."""
    server_id = str(entry.get("id") or "")
    server_name = str(entry.get("name") or "")
    if server_id in KL_BUILTIN_MCP_IDS or server_name in KL_BUILTIN_MCP_NAMES:
        return None  # RikkaHub has no equivalent built-in
    # RikkaHub supports only sse | streamable_http; map the Kelivo enum onto
    # the closest equivalent.
    transport = str(entry.get("transport") or "streamable_http")
    if transport in ("inmemory", "http", "stdio"):
        transport = "streamable_http"
    tools = []
    for t in _as_list(entry.get("tools")):
        if not isinstance(t, dict):
            continue
        tools.append({
            "enable": bool(t.get("enabled", True)),
            "name": str(t.get("name") or ""),
            "description": t.get("description"),
            "inputSchema": _params_to_schema(
                t.get("schema"), _as_list(t.get("params"))),
            "needsApproval": bool(t.get("needsApproval", False)),
        })
    headers = entry.get("headers")
    rh_headers = []
    if isinstance(headers, dict):
        # RikkaHub serializes Pair<String,String> as {"first","second"}.
        rh_headers = [{"first": str(k), "second": str(v)}
                      for k, v in headers.items()]
    return {
        "type": transport,
        "id": _entity_uuid(entry.get("id"), "mcp"),
        "url": str(entry.get("url") or ""),
        "commonOptions": {
            "enable": bool(entry.get("enabled", True)),
            "name": str(entry.get("name") or ""),
            "headers": rh_headers,
            "tools": tools,
            "oauth": None,
        },
    }


def _params_to_schema(schema: Any, params: list[Any]) -> dict[str, Any] | None:
    """Kelivo params/schema -> RikkaHub inputSchema (JSON Schema object)."""
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        name = str(p["name"])
        prop: dict[str, Any] = {}
        if p.get("type"):
            prop["type"] = str(p["type"])
        if p.get("description"):
            prop["description"] = str(p["description"])
        if p.get("default") is not None:
            prop["default"] = p["default"]
        properties[name] = prop
        if p.get("required"):
            required.append(name)
    if not properties:
        return None
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _schema_to_params(schema: Any) -> list[dict[str, Any]]:
    """RikkaHub inputSchema -> Kelivo McpParamSpec list."""
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = schema.get("required")
    required_set = set(required) if isinstance(required, list) else set()
    params: list[dict[str, Any]] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        param: dict[str, Any] = {"name": str(name),
                                 "required": str(name) in required_set}
        if prop.get("type"):
            param["type"] = str(prop["type"])
        if prop.get("description"):
            param["description"] = str(prop["description"])
        if prop.get("default") is not None:
            param["default"] = prop["default"]
        params.append(param)
    return params


def _convert_tts_to_rh(entry: dict[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("kind") or entry.get("type") or "openai")
    out: dict[str, Any] = {"type": kind,
                           "id": _entity_uuid(entry.get("id"), "tts"),
                           "name": str(entry.get("name") or "")}
    for field in ("apiKey", "baseUrl"):
        if entry.get(field) is not None:
            out[field] = str(entry[field])
    if entry.get("voiceId") is not None:
        out["voiceId"] = str(entry["voiceId"])
    if entry.get("voice") is not None:
        out["voice"] = str(entry["voice"])
    if entry.get("language") is not None:
        out["language"] = str(entry["language"])
    return out


def _convert_quick_phrase_to_rh(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _entity_uuid(entry.get("id"), "quick_phrase"),
        "title": str(entry.get("title") or ""),
        "content": str(entry.get("content") or ""),
    }


def _convert_injection_to_rh(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "mode",
        "id": _entity_uuid(entry.get("id"), "injection"),
        "name": str(entry.get("title") or entry.get("name") or ""),
        "enabled": True,
        "priority": 0,
        "position": "after_system_prompt",
        "content": convert_prompt_vars(
            str(entry.get("prompt") or entry.get("content") or ""),
            to_rikkahub=True),
        "injectDepth": 4,
        "role": "user",
    }


def _convert_world_book_to_rh(entry: dict[str, Any]) -> dict[str, Any]:
    """Kelivo world book -> RikkaHub lorebook (RegexInjection entries)."""
    entries: list[dict[str, Any]] = []
    for e in _as_list(entry.get("entries")):
        if not isinstance(e, dict):
            continue
        position = str(e.get("position") or "AFTER_SYSTEM_PROMPT").lower()
        entries.append({
            "type": "regex",
            "id": _entity_uuid(e.get("id"), "lorebook_entry"),
            "name": str(e.get("name") or ""),
            "enabled": bool(e.get("enabled", True)),
            "priority": int(e.get("priority") or 0),
            "position": position,
            "content": convert_prompt_vars(
                str(e.get("content") or ""), to_rikkahub=True),
            "injectDepth": int(e.get("injectDepth") or 4),
            "role": str(e.get("role") or "USER").lower(),
            "keywords": _as_list(e.get("keywords")),
            "useRegex": bool(e.get("useRegex", False)),
            "caseSensitive": bool(e.get("caseSensitive", False)),
            "scanDepth": int(e.get("scanDepth") or 4),
            "constantActive": bool(e.get("constantActive", False)),
        })
    return {
        "id": _entity_uuid(entry.get("id"), "lorebook"),
        "name": str(entry.get("name") or ""),
        "description": str(entry.get("description") or ""),
        "enabled": bool(entry.get("enabled", True)),
        "entries": entries,
    }


# Search service types present in both apps (same JSON shape); the rest are
# app-specific and dropped with a warning.
_SHARED_SEARCH_TYPES = {
    "bing_local", "zhipu", "tavily", "exa", "searxng", "linkup",
    "brave", "metaso", "ollama", "perplexity", "jina", "bocha",
    "grok", "serper",
}


def _convert_search_to_rh(entry: dict[str, Any]) -> dict[str, Any] | None:
    stype = str(entry.get("type") or "")
    if stype not in _SHARED_SEARCH_TYPES:
        return None
    out: dict[str, Any] = {"type": stype,
                           "id": _entity_uuid(entry.get("id"), "search")}
    for field in ("apiKey", "url", "engines", "language", "username",
                  "password", "depth", "maxTokens", "maxTokensPerPage",
                  "summary", "model", "customUrl", "systemPrompt",
                  "searchUrl", "scrapeUrl"):
        if entry.get(field) is not None:
            out[field] = entry[field]
    return out


def _convert_search_to_kl(entry: dict[str, Any]) -> dict[str, Any] | None:
    stype = str(entry.get("type") or "")
    if stype not in _SHARED_SEARCH_TYPES:
        return None
    out: dict[str, Any] = {"type": stype,
                           "id": _entity_uuid(entry.get("id"), "search"),
                           "apiKeys": [],
                           "keyManagement": None}
    api_key = entry.get("apiKey")
    if isinstance(api_key, str) and api_key:
        now = int(datetime.now().timestamp() * 1000)
        out["apiKeys"] = [{
            "id": _entity_uuid(entry.get("id"), "search_key"),
            "key": api_key,
            "isEnabled": True,
            "priority": 5,
            "usage": {"totalRequests": 0, "successfulRequests": 0,
                       "failedRequests": 0, "consecutiveFailures": 0,
                       "lastUsed": None},
            "status": "active",
            "createdAt": now,
            "updatedAt": now,
        }]
    for field in ("url", "engines", "language", "username",
                  "password", "depth", "maxTokens", "maxTokensPerPage",
                  "summary", "model", "customUrl", "systemPrompt",
                  "searchUrl", "scrapeUrl"):
        if entry.get(field) is not None:
            out[field] = entry[field]
    return out


def _model_by_key_from_providers(
    providers: list[Any],
) -> dict[tuple[str, str], str]:
    """(provider id, model name) -> model uuid from a plain providers list.

    Handles both RikkaHub-shaped entries (models as dicts with modelId/id)
    and bare Kelivo-style entries (models as name strings).
    """
    out: dict[tuple[str, str], str] = {}
    for entry in providers:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        pid = str(entry["id"])
        for m in _as_list(entry.get("models")):
            if isinstance(m, dict):
                name = str(m.get("modelId") or "")
                mid = str(m.get("id") or _model_uuid(pid, name))
            elif isinstance(m, str) and m:
                name = m
                mid = _model_uuid(pid, name)
            else:
                continue
            if name:
                out[(pid, name)] = mid
    return out


def _unique_model_names(providers: list[Any]) -> dict[str, str]:
    """model name -> uuid, only for names exposed by exactly one provider.

    An unqualified Kelivo chatModelId (no chatModelProvider) can only be
    resolved safely when it is unambiguous across every provider.
    """
    owners: dict[str, set[str]] = {}
    uuids: dict[str, str] = {}
    for p in providers:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        pid = str(p["id"])
        for m in _as_list(p.get("models")):
            if not (isinstance(m, dict) and m.get("modelId")):
                continue
            name = str(m["modelId"])
            owners.setdefault(name, set()).add(pid)
            uuids.setdefault(name, str(m.get("id") or ""))
    return {name: mid for name, mid in uuids.items()
            if len(owners[name]) == 1 and mid}


def build_model_maps_k2r(kelivo: dict[str, Any]) -> dict[tuple[str, str], str]:
    """(kelivo provider key, model name) -> RikkaHub model uuid.

    Mirrors the provider conversion inside build_rikkahub_settings so message
    modelIds can be resolved to the same deterministic uuids.  Falls back to
    the plain ``providers`` list when provider_configs_v1 is absent.
    """
    providers_raw = _parse_json_str(kelivo.get("provider_configs_v1"))
    if not isinstance(providers_raw, dict):
        return _model_by_key_from_providers(_as_list(kelivo.get("providers")))
    out: dict[tuple[str, str], str] = {}
    for key, v in providers_raw.items():
        if not isinstance(v, dict):
            continue
        builtin = _find_builtin_by_kelivo(v, key)
        pid = (builtin["rh_id"] if builtin
               else _entity_uuid(v.get("id") or key, "provider"))
        for name in _as_list(v.get("models")):
            name = str(name)
            out[(str(key), name)] = _model_uuid(pid, name)
            if v.get("id"):
                out[(str(v["id"]), name)] = _model_uuid(pid, name)
    return out


def build_model_maps_r2k(rikkahub: dict[str, Any]) -> dict[str, str]:
    """RikkaHub model uuid -> Kelivo model name (the provider's modelId)."""
    out: dict[str, str] = {}
    for entry in _as_list(rikkahub.get("providers")):
        if not isinstance(entry, dict):
            continue
        for model in _as_list(entry.get("models")):
            if isinstance(model, dict) and model.get("id"):
                out[str(model["id"])] = str(model.get("modelId") or "")
    return out


def build_rikkahub_settings(kelivo: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert a Kelivo settings object into a RikkaHub settings object."""
    notes: list[str] = []
    out: dict[str, Any] = {}

    # 1. shared scalar keys
    for key in SHARED_KEYS:
        if key in kelivo:
            value = kelivo[key]
            if key in _PROMPT_KEYS and isinstance(value, str):
                value = convert_prompt_vars(value, to_rikkahub=True)
            out[key] = value

    # 2. RikkaHub-only keys with app defaults
    for key, value in RH_ONLY_DEFAULTS.items():
        out.setdefault(key, value)
    out.setdefault("themeId", "sakura")

    # User profile (nickname/avatar) from Kelivo's prefs keys.
    display = dict(out.get("displaySetting") or RH_DISPLAY_DEFAULTS)
    if kelivo.get("user_name"):
        display["userNickname"] = str(kelivo["user_name"])
    avatar_type = kelivo.get("avatar_type")
    avatar_value = kelivo.get("avatar_value")
    if avatar_type == "emoji" and avatar_value:
        display["userAvatar"] = {
            "type": "me.rerere.rikkahub.data.model.Avatar.Emoji",
            "content": str(avatar_value)}
    elif avatar_type in ("url", "file") and avatar_value:
        url = str(avatar_value)
        if not (url.startswith("file://") or url.startswith("http://")
                or url.startswith("https://")):
            url = "file://" + url
        display["userAvatar"] = {
            "type": "me.rerere.rikkahub.data.model.Avatar.Image",
            "url": url}
    out["displaySetting"] = display

    # S3 / WebDAV backup destinations.
    webdav = _parse_json_str(kelivo.get("webdav_config_v1"))
    if isinstance(webdav, dict):
        out["webDavConfig"] = {
            "url": str(webdav.get("url") or ""),
            "username": str(webdav.get("username") or ""),
            "password": str(webdav.get("password") or ""),
            "path": str(webdav.get("path") or "rikkahub_backups"),
            "items": ["DATABASE", "FILES"],
        }
    s3 = _parse_json_str(kelivo.get("s3_config_v1"))
    if isinstance(s3, dict):
        out["s3Config"] = {
            "endpoint": str(s3.get("endpoint") or ""),
            "accessKeyId": str(s3.get("accessKeyId") or ""),
            "secretAccessKey": str(s3.get("secretAccessKey") or ""),
            "bucket": str(s3.get("bucket") or ""),
            "region": str(s3.get("region") or "auto"),
            "pathStyle": bool(s3.get("pathStyle", True)),
            "items": ["DATABASE", "FILES"],
        }

    # Search: selected index and common options back into RikkaHub.
    selected = kelivo.get("search_selected_v1")
    if isinstance(selected, int):
        out["searchServiceSelected"] = selected
    common = _parse_json_str(kelivo.get("search_common_v1"))
    if isinstance(common, dict) and isinstance(common.get("resultSize"), int):
        out["searchCommonOptions"] = {"resultSize": common["resultSize"]}

    # 3. containers from Kelivo *_v1 JSON strings
    providers_raw = _parse_json_str(kelivo.get("provider_configs_v1"))
    model_by_key: dict[tuple[str, str], str] = {}
    rh_id_by_kl_key: dict[str, str] = {}
    if isinstance(providers_raw, dict):
        providers = []
        for k, v in providers_raw.items():
            if not isinstance(v, dict):
                continue
            p = _convert_provider_to_rh(v, k)
            providers.append(p)
            pid = p["id"]
            rh_id_by_kl_key[str(k)] = pid
            if v.get("id"):
                rh_id_by_kl_key[str(v["id"])] = pid
            for name in _as_list(v.get("models")):
                name = str(name)
                model_by_key[(str(k), name)] = _model_uuid(pid, name)
                if v.get("id"):
                    model_by_key[(str(v["id"]), name)] = _model_uuid(pid, name)
        out["providers"] = providers

    elif _as_list(kelivo.get("providers")):
        providers = _as_list(kelivo["providers"])
        out["providers"] = providers
        model_by_key = _model_by_key_from_providers(providers)

    # Task models: Kelivo "providerKey::modelId" -> RikkaHub model uuids.
    # Kelivo's current model (selected_model_v1) maps to RikkaHub's default
    # chat model; the fast model follows the title model (round-trip of the
    # R2K mapping) or the current model as Kelivo's generic fallback.
    def _rh_model_ref(ref: Any) -> str | None:
        if not isinstance(ref, str) or "::" not in ref:
            return None
        kl_key, model = ref.split("::", 1)
        pid = rh_id_by_kl_key.get(kl_key)
        if not pid or not model:
            return None
        return _model_uuid(pid, model)

    for kl_key, rh_key in (("title_model_v1", "titleModelId"),
                           ("suggestion_model_v1", "suggestionModelId"),
                           ("translate_model_v1", "translateModeId"),
                           ("ocr_model_v1", "ocrModelId"),
                           ("compress_model_v1", "compressModelId")):
        ref = _rh_model_ref(kelivo.get(kl_key))
        if ref:
            out[rh_key] = ref
    title_ref = _rh_model_ref(kelivo.get("title_model_v1"))
    current_ref = _rh_model_ref(kelivo.get("selected_model_v1"))
    if current_ref:
        out["chatModelId"] = current_ref
    if title_ref:
        out["fastModelId"] = title_ref
    elif current_ref:
        out["fastModelId"] = current_ref

    assistants_raw = _parse_json_str(kelivo.get("assistants_v1"))
    if isinstance(assistants_raw, list):
        model_by_name = _unique_model_names(out.get("providers", []))
        assistants = []
        for e in assistants_raw:
            if not isinstance(e, dict):
                continue
            a, note = _convert_assistant_to_rh(
                e, model_by_key=model_by_key, model_by_name=model_by_name)
            assistants.append(a)
            if note:
                notes.append(note)
        out["assistants"] = assistants
        # Switch the RikkaHub default assistant to the one Kelivo was
        # actually used with (its current_assistant_id_v1), when present in
        # the converted list; otherwise keep the carried-over assistantId.
        current = kelivo.get("current_assistant_id_v1")
        if current and any(a.get("id") == _entity_uuid(current, "assistant")
                           for a in assistants):
            out["assistantId"] = _entity_uuid(current, "assistant")
        dropped_fields = sum(
            len(set(e.keys()) - _RH_ASSISTANT_KEPT)
            for e in assistants_raw if isinstance(e, dict))
        if dropped_fields:
            notes.append(f"{len(assistants)} 个助手的 {dropped_fields} 个 "
                         "Kelivo 独有字段")
    elif _as_list(kelivo.get("assistants")):
        out["assistants"] = _as_list(kelivo["assistants"])

    mcp_raw = _parse_json_str(kelivo.get("mcp_servers_v1"))
    if isinstance(mcp_raw, list):
        mcp = [c for e in mcp_raw if isinstance(e, dict)
               for c in [_convert_mcp_to_rh(e)] if c is not None]
        out["mcpServers"] = mcp

        dropped_builtin = sum(
            1 for e in mcp_raw if isinstance(e, dict)
            and (str(e.get("id") or "") in KL_BUILTIN_MCP_IDS
                 or str(e.get("name") or "") in KL_BUILTIN_MCP_NAMES))
        if dropped_builtin:
            notes.append(f"{dropped_builtin} 个 Kelivo 内置 MCP 服务")
    elif _as_list(kelivo.get("mcpServers")):
        out["mcpServers"] = _as_list(kelivo["mcpServers"])

    tts_raw = _parse_json_str(kelivo.get("tts_services_v1"))
    if isinstance(tts_raw, list):
        out["ttsProviders"] = [_convert_tts_to_rh(e)
                               for e in tts_raw if isinstance(e, dict)]
    elif _as_list(kelivo.get("ttsProviders")):
        out["ttsProviders"] = _as_list(kelivo["ttsProviders"])

    qp_raw = _parse_json_str(kelivo.get("quick_phrases_v1"))
    if isinstance(qp_raw, list):
        out["quickMessages"] = [_convert_quick_phrase_to_rh(e)
                                for e in qp_raw if isinstance(e, dict)]
    elif _as_list(kelivo.get("quickMessages")):
        out["quickMessages"] = _as_list(kelivo["quickMessages"])

    inj_raw = _parse_json_str(kelivo.get("instruction_injections_v1"))
    if isinstance(inj_raw, list):
        out["modeInjections"] = [_convert_injection_to_rh(e)
                                 for e in inj_raw if isinstance(e, dict)]
    elif _as_list(kelivo.get("modeInjections")):
        out["modeInjections"] = _as_list(kelivo["modeInjections"])

    wb_raw = _parse_json_str(kelivo.get("world_books_v1"))
    if isinstance(wb_raw, list):
        lorebooks = [_convert_world_book_to_rh(e)
                     for e in wb_raw if isinstance(e, dict)]
        if lorebooks:
            out["lorebooks"] = lorebooks


    sr_raw = _parse_json_str(kelivo.get("search_services_v1"))
    if isinstance(sr_raw, list):
        converted = [_convert_search_to_rh(e)
                     for e in sr_raw if isinstance(e, dict)]
        dropped = sum(1 for e in sr_raw if isinstance(e, dict)
                      and str(e.get("type") or "") not in _SHARED_SEARCH_TYPES)
        if converted:
            out["searchServices"] = [c for c in converted if c is not None]
        if dropped:
            notes.append(f"{dropped} 个搜索服务（类型不受 RikkaHub 支持）")

    return out, notes


# ---------------------------------------------------------------------------
# RikkaHub -> Kelivo entry converters
# ---------------------------------------------------------------------------

def _convert_provider_to_kl(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    pid = str(entry.get("id") or uuidlib.uuid4())
    builtin = _find_builtin_by_rh_id(pid)
    kelivo_key = builtin["kelivo_key"] if builtin else pid

    overrides: dict[str, Any] = {}
    for model in _as_list(entry.get("models")):
        if not isinstance(model, dict) or not model.get("modelId"):
            continue
        name = str(model["modelId"])
        overrides[name] = {
            "name": str(model.get("displayName") or name),
            "type": str(model.get("type") or "CHAT").lower(),
            "input": _lower_list(model.get("inputModalities"), ["text"]),
            "output": _lower_list(model.get("outputModalities"), ["text"]),
            "abilities": _lower_list(model.get("abilities"), []),
        }
    out: dict[str, Any] = dict(KL_PROVIDER_DEFAULTS)
    out["id"] = pid
    out["enabled"] = bool(entry.get("enabled", True))
    out["name"] = str(entry.get("name") or (builtin["kelivo_key"] if builtin else ""))
    out["apiKey"] = str(entry.get("apiKey") or "")
    out["baseUrl"] = str(entry.get("baseUrl") or "")
    out["providerType"] = str(entry.get("type") or "openai")
    if entry.get("chatCompletionsPath") is not None:
        out["chatPath"] = str(entry["chatCompletionsPath"])
    if entry.get("useResponseApi") is not None:
        out["useResponseApi"] = bool(entry["useResponseApi"])
    if entry.get("vertexAI") is not None:
        out["vertexAI"] = bool(entry["vertexAI"])
    if entry.get("projectId") is not None:
        out["projectId"] = str(entry["projectId"])
    if entry.get("location") is not None:
        out["location"] = str(entry["location"])
    if entry.get("promptCaching") is not None:
        out["claudePromptCachingEnabled"] = bool(entry["promptCaching"])
    if entry.get("promptCacheTtl") is not None:
        out["claudePromptCachingTtl"] = str(entry["promptCacheTtl"])
    if entry.get("customHeaders") is not None:
        out["customHeaders"] = _as_list(entry["customHeaders"])
    if entry.get("customBodies") is not None:
        out["customBody"] = _as_list(entry["customBodies"])
    out["models"] = [str(m["modelId"]) for m in _as_list(entry.get("models"))
                     if isinstance(m, dict) and m.get("modelId")]
    out["modelOverrides"] = overrides
    return kelivo_key, out


def _convert_assistant_to_kl(
    entry: dict[str, Any],
    *,
    model_by_uuid: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(KL_ASSISTANT_DEFAULTS)
    out["id"] = _entity_uuid(entry.get("id"), "assistant")
    out["name"] = str(entry.get("name") or "")
    # Kelivo stores avatar as a plain String? (path/url/base64, null for
    # initial-letter avatar) — NOT an object.  RikkaHub's Avatar.Image url
    # maps onto it directly.
    avatar = entry.get("avatar")
    if isinstance(avatar, dict):
        out["avatar"] = avatar.get("url")
    else:
        out["avatar"] = avatar
    if entry.get("useAssistantAvatar") is not None:
        out["useAssistantAvatar"] = bool(entry["useAssistantAvatar"])
    if entry.get("chatModelId") is not None:
        resolved = None
        if model_by_uuid is not None:
            resolved = model_by_uuid.get(str(entry["chatModelId"]))
        if resolved:
            out["chatModelProvider"], out["chatModelId"] = resolved
        else:
            out["chatModelId"] = str(entry["chatModelId"])
    for field in ("temperature", "topP", "maxTokens", "background"):
        if entry.get(field) is not None:
            out[field] = entry[field]
    if entry.get("streamOutput") is not None:
        out["streamOutput"] = bool(entry["streamOutput"])
    for field in ("systemPrompt", "messageTemplate"):
        if entry.get(field) is not None:
            value = str(entry[field])
            if field == "systemPrompt":
                value = convert_prompt_vars(value, to_rikkahub=False)
            out[field] = value
    if entry.get("enableMemory") is not None:
        out["enableMemory"] = bool(entry["enableMemory"])
    if entry.get("enableRecentChatsReference") is not None:
        out["enableRecentChatsReference"] = bool(entry["enableRecentChatsReference"])
    if entry.get("customHeaders") is not None:
        out["customHeaders"] = _as_list(entry["customHeaders"])
    if entry.get("customBodies") is not None:
        out["customBody"] = _as_list(entry["customBodies"])
    # RikkaHub enabledSkills (skill names) -> Kelivo skillIds.
    if entry.get("enabledSkills"):
        out["skillIds"] = [str(s) for s in _as_list(entry["enabledSkills"])]
    presets = _as_list(entry.get("presetMessages"))
    if presets:
        out["presetMessages"] = json.dumps([
            {
                "id": str(p.get("id") or uuidlib.uuid4()),
                "role": str(p.get("role") or "user"),
                "content": "\n\n".join(
                    str(part.get("text") or "")
                    for part in _as_list(p.get("parts"))
                    if isinstance(part, dict) and part.get("type") == "text"
                ),
            }
            for p in presets if isinstance(p, dict)
        ], ensure_ascii=False)
    now = _now_iso()
    out["createdAt"] = now
    out["updatedAt"] = now
    return out


def _convert_mcp_to_kl(entry: dict[str, Any]) -> dict[str, Any] | None:
    """RikkaHub MCP server -> Kelivo.  Returns None for Kelivo built-ins."""
    common = entry.get("commonOptions") if isinstance(entry.get("commonOptions"), dict) else {}
    server_id = str(entry.get("id") or "")
    server_name = str(common.get("name") or "")
    if server_id in KL_BUILTIN_MCP_IDS or server_name in KL_BUILTIN_MCP_NAMES:
        return None  # built-ins are re-added by the converter
    tools = []
    for t in _as_list(common.get("tools")):
        if not isinstance(t, dict):
            continue
        tool: dict[str, Any] = {
            "enabled": bool(t.get("enable", True)),
            "name": str(t.get("name") or ""),
            "description": t.get("description"),
            "params": _schema_to_params(t.get("inputSchema")),
        }
        if t.get("inputSchema") is not None:
            tool["schema"] = t["inputSchema"]
        if t.get("needsApproval"):
            tool["needsApproval"] = True
        tools.append(tool)
    # Kelivo transports are an enum: sse | http | stdio | inmemory.
    transport = str(entry.get("type") or "http")
    if transport == "streamable_http":
        transport = "http"
    headers = common.get("headers")
    kl_headers = None
    if isinstance(headers, list):
        pairs = {}
        for h in headers:
            if isinstance(h, dict) and h.get("first") is not None:
                pairs[str(h["first"])] = str(h.get("second") or "")
            elif isinstance(h, list) and len(h) >= 2 and h[0] is not None:
                pairs[str(h[0])] = str(h[1])
        if pairs:
            kl_headers = pairs
    out: dict[str, Any] = {
        "id": _entity_uuid(entry.get("id"), "mcp"),
        "enabled": bool(common.get("enable", True)),
        "name": str(common.get("name") or ""),
        "transport": transport,
        "tools": tools,
    }
    if kl_headers:
        out["headers"] = kl_headers
    if transport not in ("stdio", "inmemory"):
        out["url"] = str(entry.get("url") or "")
    return out


def _convert_tts_to_kl(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _entity_uuid(entry.get("id"), "tts"),
        "enabled": True,
        "name": str(entry.get("name") or ""),
        "kind": str(entry.get("type") or "openai"),
    }
    for field in ("apiKey", "baseUrl"):
        if entry.get(field) is not None:
            out[field] = str(entry[field])
    voice = entry.get("voice") or entry.get("voiceId") or entry.get("voiceName")
    if voice is not None:
        out["voiceId"] = str(voice)
    if entry.get("language") is not None:
        out["language"] = str(entry["language"])
    return out


def _convert_quick_phrase_to_kl(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _entity_uuid(entry.get("id"), "quick_phrase"),
        "title": str(entry.get("title") or ""),
        "content": str(entry.get("content") or ""),
        "isGlobal": True,
        "assistantId": None,
    }


def _convert_injection_to_kl(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _entity_uuid(entry.get("id"), "injection"),
        "title": str(entry.get("name") or ""),
        "prompt": convert_prompt_vars(
            str(entry.get("content") or ""), to_rikkahub=False),
        "group": None,
    }


def _convert_lorebook_to_kl(entry: dict[str, Any]) -> dict[str, Any]:
    """RikkaHub lorebook -> Kelivo world book."""
    entries: list[dict[str, Any]] = []
    for e in _as_list(entry.get("entries")):
        if not isinstance(e, dict):
            continue
        position = str(e.get("position") or "after_system_prompt").upper()
        entries.append({
            "id": _entity_uuid(e.get("id"), "lorebook_entry"),
            "name": str(e.get("name") or ""),
            "enabled": bool(e.get("enabled", True)),
            "priority": int(e.get("priority") or 0),
            "position": position,
            "content": convert_prompt_vars(
                str(e.get("content") or ""), to_rikkahub=False),
            "injectDepth": int(e.get("injectDepth") or 4),
            "role": str(e.get("role") or "user").upper(),
            "keywords": [str(k) for k in _as_list(e.get("keywords"))],
            "useRegex": bool(e.get("useRegex", False)),
            "caseSensitive": bool(e.get("caseSensitive", False)),
            "scanDepth": int(e.get("scanDepth") or 4),
            "constantActive": bool(e.get("constantActive", False)),
        })
    return {
        "id": _entity_uuid(entry.get("id"), "lorebook"),
        "name": str(entry.get("name") or ""),
        "description": str(entry.get("description") or ""),
        "enabled": bool(entry.get("enabled", True)),
        "entries": entries,
    }


def build_kelivo_settings(rikkahub: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert a RikkaHub settings object into a Kelivo (cuplivo) settings
    object shaped like a SharedPreferences snapshot."""
    notes: list[str] = []
    out: dict[str, Any] = dict(KL_ONLY_DEFAULTS)

    # 1. shared scalar keys
    for key in SHARED_KEYS:
        if key in rikkahub:
            value = rikkahub[key]
            # Kelivo restores settings via prefs.restore(), which only
            # accepts String/bool/int/double/List<String>.  Object-array
            # values (RikkaHub searchServices/asrProviders/lorebooks/
            # assistantTags/customThemes) would abort the whole restore and
            # drop every later key, so they stay empty mirror keys here —
            # their data travels in the *_v1 JSON strings instead.
            if isinstance(value, list) and any(
                    isinstance(v, dict) for v in value):
                continue
            if key in _PROMPT_KEYS and isinstance(value, str):
                value = convert_prompt_vars(value, to_rikkahub=False)
            out[key] = value

    # 2. providers -> provider_configs_v1
    providers: dict[str, Any] = {}
    model_by_uuid: dict[str, tuple[str, str]] = {}
    for entry in _as_list(rikkahub.get("providers")):
        if not isinstance(entry, dict):
            continue
        key, converted = _convert_provider_to_kl(entry)
        providers[key] = converted
        for name in converted.get("models") or []:
            for model in _as_list(entry.get("models")):
                if isinstance(model, dict) and model.get("modelId") == name:
                    model_by_uuid[str(model.get("id"))] = (key, str(name))
                    break
    if providers:
        out["provider_configs_v1"] = _json_str(providers)

    else:
        out["provider_configs_v1"] = "{}"

    # Task models (title/suggestion/translate/ocr/compress) -> Kelivo's
    # "providerKey::modelId" preference format.  RikkaHub's fastModelId is the
    # fallback for title/suggestion generation when unset, mirroring Kelivo's
    # own fallback chain, so it is written into those two keys explicitly.
    def _kl_model_ref(model_uuid: Any) -> str | None:
        if not model_uuid or not model_by_uuid:
            return None
        hit = model_by_uuid.get(str(model_uuid))
        if not hit:
            return None
        return f"{hit[0]}::{hit[1]}"

    title_ref = _kl_model_ref(rikkahub.get("titleModelId"))
    if title_ref is None:
        title_ref = _kl_model_ref(rikkahub.get("fastModelId"))
    if title_ref:
        out["title_model_v1"] = title_ref

    suggestion_ref = _kl_model_ref(rikkahub.get("suggestionModelId"))
    if suggestion_ref is None:
        suggestion_ref = _kl_model_ref(rikkahub.get("fastModelId"))
    if suggestion_ref:
        out["suggestion_model_v1"] = suggestion_ref

    for rh_key, kl_key in (("translateModeId", "translate_model_v1"),
                           ("ocrModelId", "ocr_model_v1"),
                           ("compressModelId", "compress_model_v1")):
        ref = _kl_model_ref(rikkahub.get(rh_key))
        if ref:
            out[kl_key] = ref

    # Default chat model -> Kelivo's current model (selected_model_v1), the
    # fallback for everything else in Kelivo.
    chat_ref = _kl_model_ref(rikkahub.get("chatModelId"))
    if chat_ref:
        out["selected_model_v1"] = chat_ref

    # 2. user profile (nickname/avatar) from RikkaHub displaySetting
    display = rikkahub.get("displaySetting")
    if isinstance(display, dict):
        nickname = display.get("userNickname")
        if isinstance(nickname, str) and nickname:
            out["user_name"] = nickname
        avatar = display.get("userAvatar")
        if isinstance(avatar, dict):
            avatar_type = str(avatar.get("type") or "")
            if "Emoji" in avatar_type and avatar.get("content"):
                out["avatar_type"] = "emoji"
                out["avatar_value"] = str(avatar["content"])
            elif "Image" in avatar_type and avatar.get("url"):
                url = str(avatar["url"])
                if url.startswith("http://") or url.startswith("https://"):
                    out["avatar_type"] = "url"
                    out["avatar_value"] = url
                else:
                    out["avatar_type"] = "file"
                    out["avatar_value"] = url[7:] if url.startswith("file://") else url

    # S3 / WebDAV backup destinations.
    webdav = rikkahub.get("webDavConfig")
    if isinstance(webdav, dict):
        out["webdav_config_v1"] = _json_str({
            "url": str(webdav.get("url") or ""),
            "username": str(webdav.get("username") or ""),
            "password": str(webdav.get("password") or ""),
            "path": str(webdav.get("path") or "kelivo_backups"),
            "userAgent": "",
            "includeChats": True,
            "includeFiles": True,
        })
    s3 = rikkahub.get("s3Config")
    if isinstance(s3, dict):
        out["s3_config_v1"] = _json_str({
            "endpoint": str(s3.get("endpoint") or ""),
            "region": str(s3.get("region") or "us-east-1"),
            "bucket": str(s3.get("bucket") or ""),
            "accessKeyId": str(s3.get("accessKeyId") or ""),
            "secretAccessKey": str(s3.get("secretAccessKey") or ""),
            "sessionToken": "",
            # RikkaHub hardcodes the S3 backup prefix to "rikkahub_backups/"
            # in S3Sync.kt (its S3Config has no prefix field), so keep the
            # converted Kelivo config pointing at the same bucket location;
            # the WebDAV path is unrelated and must not leak in here.
            "prefix": "rikkahub_backups",
            "pathStyle": bool(s3.get("pathStyle", True)),
            "userAgent": "",
            "includeChats": True,
            "includeFiles": True,
        })

    # Search services: enable search and carry the selected index/common
    # options so the converted services actually take effect.
    if rikkahub.get("searchServices"):
        out["search_enabled_v1"] = True
        selected = rikkahub.get("searchServiceSelected")
        if isinstance(selected, int):
            out["search_selected_v1"] = selected
        common = rikkahub.get("searchCommonOptions")
        if isinstance(common, dict):
            result_size = common.get("resultSize")
            out["search_common_v1"] = _json_str({
                "resultSize": result_size if isinstance(result_size, int) else 10,
                "timeout": 5000,
            })

    # 3. assistants -> assistants_v1
    assistants = [_convert_assistant_to_kl(e, model_by_uuid=model_by_uuid)
                  for e in _as_list(rikkahub.get("assistants")) if isinstance(e, dict)]
    if assistants:
        out["assistants_v1"] = _json_str(assistants)
        assistant_ids = {a.get("id") for a in assistants}
        # The assistant RikkaHub was actually used with: its default
        # assistantId wins when present in the converted list (RikkaHub
        # merges the built-in default into the user list at read time);
        # otherwise fall back to the first converted assistant.
        current = rikkahub.get("assistantId")
        if not current or _entity_uuid(current, "assistant") not in assistant_ids:
            current = assistants[0].get("id")
        if current:
            out["current_assistant_id_v1"] = _entity_uuid(current, "assistant")
        dropped_fields = sum(
            len(set(e.keys()) - _KL_ASSISTANT_KEPT)
            for e in _as_list(rikkahub.get("assistants")) if isinstance(e, dict))
        if dropped_fields:
            notes.append(f"{len(assistants)} 个助手的 {dropped_fields} 个 "
                         "RikkaHub 独有字段")
    else:
        out["assistants_v1"] = "[]"

    # 4. mcpServers -> mcp_servers_v1 (Kelivo's three built-in in-memory
    # servers are always present; user servers are appended)
    mcp = [_convert_mcp_to_kl(e)
           for e in _as_list(rikkahub.get("mcpServers")) if isinstance(e, dict)]
    mcp = [m for m in mcp if m is not None]
    out["mcp_servers_v1"] = _json_str([dict(b) for b in KL_BUILTIN_MCPS] + mcp)

    # 5. ttsProviders -> tts_services_v1 + tts_selected_v1
    tts = [_convert_tts_to_kl(e)
           for e in _as_list(rikkahub.get("ttsProviders")) if isinstance(e, dict)]
    out["tts_services_v1"] = _json_str(tts) if tts else "[]"
    selected = rikkahub.get("selectedTTSProviderId")
    if selected is not None:
        for i, t in enumerate(tts):
            if t.get("id") == str(selected):
                out["tts_selected_v1"] = i
                break

    # 6. quickMessages -> quick_phrases_v1
    qp = [_convert_quick_phrase_to_kl(e)
          for e in _as_list(rikkahub.get("quickMessages")) if isinstance(e, dict)]
    out["quick_phrases_v1"] = _json_str(qp) if qp else "[]"

    # 7. modeInjections -> instruction_injections_v1
    inj = [_convert_injection_to_kl(e)
           for e in _as_list(rikkahub.get("modeInjections")) if isinstance(e, dict)]
    out["instruction_injections_v1"] = _json_str(inj) if inj else "[]"

    # 8. lorebooks -> world_books_v1
    wb = [_convert_lorebook_to_kl(e)
          for e in _as_list(rikkahub.get("lorebooks")) if isinstance(e, dict)]
    if wb:
        out["world_books_v1"] = _json_str(wb)

    # 9. searchServices -> search_services_v1
    sr = [_convert_search_to_kl(e)
          for e in _as_list(rikkahub.get("searchServices")) if isinstance(e, dict)]
    dropped = sum(1 for e in _as_list(rikkahub.get("searchServices"))
                  if isinstance(e, dict)
                  and str(e.get("type") or "") not in _SHARED_SEARCH_TYPES)
    if sr:
        out["search_services_v1"] = _json_str([s for s in sr if s is not None])
    if dropped:
        notes.append(f"{dropped} 个搜索服务（类型不受 Kelivo 支持）")

    return out, notes
