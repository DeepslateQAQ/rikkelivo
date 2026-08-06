"""Kelivo backup format: chats.json document.

Observed layout (version 2):

    {
      "version": 2,
      "conversations": [ {...} ],
      "messages": [ {...} ],
      "toolEvents": {},
      "geminiThoughtSigs": {},
      "groupChats": [],
      "groupMembers": []
    }

Conversation keys (order matters for readability only):
  id, title, createdAt, updatedAt, messageIds, isPinned, mcpServerIds,
  assistantId, truncateIndex, versionSelections,
  summary, lastSummarizedMessageCount, chatSuggestions, conversationKind

Message keys:
  id, role, content, timestamp, modelId, providerId, totalTokens,
  conversationId, isStreaming, reasoningText, reasoningStartAt,
  reasoningFinishedAt, translation, reasoningSegmentsJson, groupId,
  subgroupId, version, promptTokens, completionTokens, cachedTokens,
  durationMs, isPreset, speakerAssistantId
"""

from __future__ import annotations

import json
import re
from typing import Any

from rikkelivo import settings
from rikkelivo.times import iso_to_utc_z

VERSION = 2


def load_chats(text: str | bytes) -> dict[str, Any]:
    """Parse a chats.json document."""
    return json.loads(text)


def _empty_deleted_document() -> dict[str, list]:
    return {
        "conversation": [],
        "message": [],
        "assistant": [],
        "worldBook": [],
        "quickPhrase": [],
        "mcpServer": [],
        "memory": [],
        "groupChat": [],
        "workspaceFile": [],
    }


def build_conversation(
    conv_id: str,
    title: str,
    created_at: str,
    updated_at: str,
    message_ids: list[str],
    assistant_id: str,
    *,
    is_pinned: bool = False,
    chat_suggestions: list[str] | None = None,
    version_selections: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a Kelivo conversation object with defaults for optional fields."""
    return {
        "id": conv_id,
        "title": title,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "messageIds": message_ids,
        "isPinned": is_pinned,
        "mcpServerIds": [],
        "assistantId": assistant_id,
        "truncateIndex": -1,
        "versionSelections": version_selections or {},
        "summary": None,
        "lastSummarizedMessageCount": 0,
        "chatSuggestions": chat_suggestions or [],
        "conversationKind": "normal",
    }


def build_message(
    msg_id: str,
    role: str,
    content: str,
    timestamp: str,
    conversation_id: str,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    total_tokens: int | None = None,
    reasoning_text: str | None = None,
    reasoning_start_at: str | None = None,
    reasoning_finished_at: str | None = None,
    translation: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cached_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a Kelivo message object with defaults for optional fields."""
    return {
        "id": msg_id,
        "role": role,
        "content": content,
        "timestamp": timestamp,
        "modelId": model_id,
        "providerId": provider_id,
        "totalTokens": total_tokens,
        "conversationId": conversation_id,
        "isStreaming": False,
        "reasoningText": reasoning_text,
        "reasoningStartAt": reasoning_start_at,
        "reasoningFinishedAt": reasoning_finished_at,
        "translation": translation,
        "reasoningSegmentsJson": None,
        "groupId": msg_id,
        "subgroupId": None,
        "version": 0,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "cachedTokens": cached_tokens,
        "durationMs": None,
        "isPreset": False,
        "speakerAssistantId": None,
    }


def build_document(
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the full chats.json document."""
    return {
        "version": VERSION,
        "conversations": conversations,
        "messages": messages,
        "toolEvents": {},
        "geminiThoughtSigs": {},
        "groupChats": [],
        "groupMembers": [],
    }


def dump_chats(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=1)


def dump_deleted() -> str:
    return json.dumps(_empty_deleted_document(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Kelivo message -> RikkaHub parts conversion
# ---------------------------------------------------------------------------

_IMAGE_TAG = re.compile(r"\[image:([^\]]+)\]")
_FILE_TAG = re.compile(r"\[file:([^\]|]+)\|([^\]|]*)\|([^\]]+)\]")
_MD_IMG_TAG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Managed-subdirectory prefixes understood by both apps (Kelivo's
# SandboxPathResolver maps any path containing /upload/ to its own data dir).
_MANAGED_DIRS = ("/upload/", "/images/", "/avatars/")

# Prefixes used when rebasing Kelivo images//avatars/ files onto RikkaHub's
# flat upload/ layout (RikkaHub's restore does not create subdirectories).
_REBASED_IMAGE_PREFIX = "upload/kkimg_"
_REBASED_AVATAR_PREFIX = "upload/kkava_"


def _rebase_tail(tail: str, marker: str) -> str:
    """Map the managed-dir tail of an attachment path onto the destination.

    ``/upload/`` files keep their name (RikkaHub writes them flat under
    upload/).  ``/images/`` and ``/avatars/`` files get a dir-specific prefix
    and move under upload/ too, matching where convert.py copies their bytes
    in the output zip.
    """
    if marker == "/upload/":
        return tail
    base = tail.rsplit("/", 1)[-1]
    if marker == "/images/":
        return _REBASED_IMAGE_PREFIX + base
    return _REBASED_AVATAR_PREFIX + base


def _rebased_upload_name(entry: str) -> str:
    """Zip entry name for a Kelivo managed asset copied into a RikkaHub zip.

    images/x.png -> upload/kkimg_x.png; avatars/x.png -> upload/kkava_x.png;
    anything else passes through unchanged.
    """
    if entry.startswith("images/"):
        return _REBASED_IMAGE_PREFIX + entry.rsplit("/", 1)[-1]
    if entry.startswith("avatars/"):
        return _REBASED_AVATAR_PREFIX + entry.rsplit("/", 1)[-1]
    return entry


def _rikkahub_url(path: str) -> str:
    """Rewrite an attachment path to RikkaHub's file:// URL scheme.

    `file://` is stripped, then the managed-subdirectory tail is rebased onto
    RikkaHub's own files dir so the restored backup resolves the file.
    """
    p = path[7:] if path.startswith("file://") else path
    p = p.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    for marker in _MANAGED_DIRS:
        idx = p.find(marker)
        if idx != -1:
            return settings.RH_FILES_PREFIX + _rebase_tail(p[idx + 1:], marker)
    return "file://" + p


def _split_attachments(content: str):
    """Split Kelivo content into (text, [attachment parts]).

    Kelivo embeds attachments as `[image:<path>]` / `[file:<path>|<name>|<mime>]`
    markers appended to the message content.  Markdown `![alt](path)` images
    stored under the managed directories are rebased like the markers so
    their bytes survive the round trip.
    """
    parts: list[dict[str, Any]] = []

    def _replace_file(match: re.Match) -> str:
        path, name, mime = match.group(1), match.group(2), match.group(3)
        parts.append({"type": "document", "url": _rikkahub_url(path.strip()),
                      "fileName": name or path.rsplit("/", 1)[-1],
                      "mime": mime or "text/plain", "metadata": None})
        return ""

    def _replace_image(match: re.Match) -> str:
        path = match.group(1)
        parts.append({"type": "image", "url": _rikkahub_url(path.strip()),
                      "metadata": None})
        return ""

    def _rewrite_md_image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        if any(marker in src for marker in _MANAGED_DIRS):
            return f"![{alt}]({_rikkahub_url(src)})"
        return match.group(0)

    text = _FILE_TAG.sub(_replace_file, content)
    text = _IMAGE_TAG.sub(_replace_image, text)
    text = _MD_IMG_TAG.sub(_rewrite_md_image, text)
    return text.rstrip(), parts


def to_rikkahub_message(
    kmsg: dict[str, Any],
    *,
    model_uuid: str | None = None,
    group_ctx: dict[str, Any] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map a Kelivo message object onto the RikkaHub message shape.

    RikkaHub message keys (observed): id, role, parts, annotations,
    createdAt, finishedAt, modelId, usage, translation.  RikkaHub decodes
    messages with ignoreUnknownKeys=true, so a `x_rikkelivo` sidecar carries
    Kelivo-native fields (group chat speaker, group/version identity, original
    content before speaker decoration) through the RikkaHub database for loss
    free round trips.

    `group_ctx` (dict with "speaker_name") marks a group-chat message with a
    Markdown blockquote naming the speaker, per the migration design.
    `tool_events` are Kelivo toolEvents entries for this message, converted
    into RikkaHub tool parts.
    """
    content = kmsg.get("content") or ""
    text, attach_parts = _split_attachments(content)

    extra: dict[str, Any] = {}
    kind = "group" if group_ctx else "normal"
    speaker = kmsg.get("speakerAssistantId") if group_ctx else None
    is_group_msg = bool(group_ctx) and bool(speaker)

    if is_group_msg:
        speaker_name = (group_ctx or {}).get("speaker_name")
        if speaker_name:
            body = text or ""
            lines = body.split("\n")
            quoted = "\n".join(f"> {line}" for line in lines)
            text = f"> **{speaker_name}**:\n{quoted}"
            extra["contentOriginal"] = body

    parts: list[dict[str, Any]] = []
    reasoning = kmsg.get("reasoningText")
    if reasoning:
        parts.append({
            "type": "reasoning",
            "reasoning": reasoning,
            "createdAt": iso_to_utc_z(kmsg.get("reasoningStartAt")),
            "finishedAt": iso_to_utc_z(kmsg.get("reasoningFinishedAt")),
            "metadata": None,
        })
    if text or attach_parts or not parts:
        if attach_parts:
            parts.append({"type": "text", "text": text, "metadata": None})
            parts.extend(attach_parts)
        else:
            parts.append({"type": "text", "text": text, "metadata": None})

    # Kelivo toolEvents entries -> RikkaHub tool parts (placed after text so
    # the reasoning/text flow reads naturally, mirroring RikkaHub's own
    # message layout where tool parts follow the text).
    for event in tool_events or []:
        if not isinstance(event, dict) or not event.get("name"):
            continue
        tool_part: dict[str, Any] = {
            "type": "tool",
            "toolCallId": str(event.get("id") or ""),
            "toolName": str(event["name"]),
            "input": json.dumps(event.get("arguments") or {},
                                ensure_ascii=False),
            "output": [],
            "approvalState": {"type": "auto"},
            "metadata": event.get("metadata") if isinstance(
                event.get("metadata"), dict) else None,
        }
        content = event.get("content")
        if isinstance(content, str) and content:
            tool_part["output"] = [{"type": "text", "text": content,
                                    "metadata": None}]
        parts.append(tool_part)

    usage: dict[str, int] | None = None
    tokens = [
        kmsg.get("promptTokens"),
        kmsg.get("completionTokens"),
        kmsg.get("cachedTokens"),
        kmsg.get("totalTokens"),
    ]
    if any(t is not None for t in tokens):
        usage = {
            "promptTokens": kmsg.get("promptTokens"),
            "completionTokens": kmsg.get("completionTokens"),
            "cachedTokens": kmsg.get("cachedTokens"),
            "totalTokens": kmsg.get("totalTokens"),
        }

    # Carry Kelivo-native identity fields when they differ from defaults.
    if is_group_msg or kmsg.get("groupId") != kmsg["id"] \
            or kmsg.get("subgroupId") is not None \
            or kmsg.get("version", 0) != 0 \
            or kmsg.get("isPreset", False):
        extra["kind"] = kind
        if speaker:
            extra["speaker"] = speaker
        if kmsg.get("groupId") and kmsg["groupId"] != kmsg["id"]:
            extra["groupId"] = kmsg["groupId"]
        if kmsg.get("subgroupId") is not None:
            extra["subgroupId"] = kmsg["subgroupId"]
        if kmsg.get("version", 0) != 0:
            extra["version"] = kmsg["version"]
        if kmsg.get("isPreset", False):
            extra["isPreset"] = True

    # Carry the (provider, model) pair through the sidecar so a round trip
    # through RikkaHub restores the exact provider even when two providers
    # expose the same model name (RikkaHub messages only store the model
    # uuid; Kelivo needs both fields).
    if kmsg.get("modelId"):
        extra["modelId"] = str(kmsg["modelId"])
    if kmsg.get("providerId"):
        extra["providerId"] = str(kmsg["providerId"])

    out = {
        "id": kmsg["id"],
        "role": kmsg.get("role", "user"),
        "parts": parts,
        "annotations": [],
        "createdAt": kmsg.get("timestamp"),
        "finishedAt": None,
        "modelId": model_uuid,
        "usage": usage,
        "translation": kmsg.get("translation"),
    }
    if extra:
        out["x_rikkelivo"] = extra
    return out
