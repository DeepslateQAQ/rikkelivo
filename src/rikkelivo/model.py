"""Internal data model shared by the format adapters.

Nothing here is written to a backup as-is: every structure either mirrors a
native payload (``RikkaPayload``/``KelivoPayload`` and their tables) or is a
short-lived projection used while one family is mapped onto another
(``Snapshot`` and friends).  The distinction matters because the projected
output must be a genuine native payload -- never a sidecar of converter-only
fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

App = Literal["rikkahub", "kelivo", "cuplivo"]

APPS: tuple[App, ...] = ("rikkahub", "kelivo", "cuplivo")

APP_FAMILY: dict[str, str] = {
    "rikkahub": "rikkahub",
    "kelivo": "kelivo",
    "cuplivo": "kelivo",
}

APP_LABEL: dict[str, str] = {
    "rikkahub": "RikkaHub",
    "kelivo": "Kelivo",
    "cuplivo": "Cuplivo",
}

#: The fifteen portable backup categories (Kelivo/Cuplivo ``BackupCategory``).
SCOPE_CATEGORIES: tuple[str, ...] = (
    "chats",
    "files",
    "assistants",
    "providers",
    "mcp",
    "environmentVariables",
    "skills",
    "worldBooks",
    "memories",
    "quickPhrases",
    "instructions",
    "workspaces",
    "searchServices",
    "speechServices",
    "settings",
)

#: Archive payload kinds understood by :func:`rikkelivo.archive.detect_archive`.
KIND_RH_SQLITE = "rh-sqlite"
KIND_KELIVO_ARCHIVE = "kelivo-archive"
KIND_KELIVO_JSON = "kelivo-json"
KIND_CUPLIVO_JSONL = "cuplivo-jsonl"
KIND_SETTINGS_ONLY = "settings-only"

#: How a format encodes "which revision is selected".
SELECTION_ACTUAL = "actual"
SELECTION_ORDINAL = "ordinal"
SELECTION_NODE_INDEX = "node-index"
SELECTION_UNDETERMINED = "undetermined"

# Diagnostic codes.  Tests assert on these codes and their categories, never
# on prose.
DIAGNOSTIC_CODES: frozenset[str] = frozenset({
    "unsupported_data",
    "source_file_missing",
    "memory_visibility_changed",
    "memory_blank_content_unsupported",
    "usage_history_reconstructed",
    "search_tokenizer_changed",
    "source_completeness_unknown",
    "forward_extensions_dropped",
    "value_defaulted",
    "historical_type_normalized",
    "context_boundary_approximated",
    "identity_reference_unresolved",
    "deprecated_command",
    "workspace_metadata_only",
    "selection_unavailable",
})


@dataclass(frozen=True)
class Diagnostic:
    """One reported data loss, reconstruction or compatibility notice."""

    code: str
    category: str
    count: int
    detail: str

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTIC_CODES:
            raise ValueError(f"unknown diagnostic code: {self.code}")


def merge_diagnostics(items) -> tuple[Diagnostic, ...]:
    """Combine diagnostics with the same (code, category, detail) report."""
    merged: dict[tuple[str, str, str], int] = {}
    order: list[tuple[str, str, str]] = []
    for item in items:
        if item.count <= 0:
            continue
        key = (item.code, item.category, item.detail)
        if key not in merged:
            merged[key] = 0
            order.append(key)
        merged[key] += item.count
    return tuple(Diagnostic(code, category, merged[(code, category, detail)], detail)
                 for code, category, detail in order)


@dataclass(frozen=True)
class FormatInfo:
    """What a source file actually is, as established by reading it."""

    family: str
    producer: str | None
    kind: str
    format_version: int | None
    schema_version: int | None
    settings_present: bool
    database_present: bool
    scope: frozenset[str]
    selection_encoding: str
    selection_evidence: str
    app_version: str | None = None

    @property
    def producer_label(self) -> str:
        return APP_LABEL.get(self.producer or "", "未知应用")

    def describe(self) -> str:
        parts = [self.producer_label if self.producer else "Kelivo 家族"]
        if self.format_version is not None:
            parts.append(f"archive {self.format_version}")
        if self.schema_version is not None:
            parts.append(f"SQLite {self.schema_version}")
        if self.kind == KIND_RH_SQLITE:
            parts = ["RikkaHub", f"Room {self.schema_version}"]
        return " · ".join(parts)


@dataclass(frozen=True)
class TargetProfile:
    """The exact native format one conversion writes."""

    app: App
    family: str
    kind: str
    format_version: int | None
    schema_version: int | None
    legacy: bool = False


@dataclass
class TableRows:
    """Native columns and values, preserving column order and NULLs."""

    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def index(self, column: str) -> int:
        return self.columns.index(column)

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class Asset:
    """A file carried by the backup, staged outside the output archive."""

    entry: str
    staged_path: Path
    size: int
    sha256: str
    mtime: int


@dataclass
class RevisionGroup:
    """One revision lineage as the source encoded it.

    Version numbers and the selected revision identity are separate facts: a
    version number is not an index into the group.
    """

    conversation_id: str
    group_id: str
    subgroup_id: str | None
    message_ids: list[str]
    version_numbers: list[int]
    selected_message_id: str | None
    speaker_id: str | None = None
    #: False when this lineage is a split sibling that the source selection
    #: did not point at; the target then uses its own latest-revision default.
    selection_explicit: bool = True
    #: True when a parallel (group, subgroup, speaker) lineage received a new
    #: target group id, so even a one-revision lineage keeps its selection.
    lineage_split: bool = False


@dataclass
class LegacyChatData:
    """Native historical chat payload (JSON1/JSON2/JSONL2).

    Holds the source's own group/version/subgroup/speaker fields and its
    message order.  It never becomes a hidden field of an output backup.
    """

    root_version: int
    format_version: int | None
    conversations: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    gemini_sigs: dict[str, str] = field(default_factory=dict)
    group_chats: list[dict[str, Any]] = field(default_factory=list)
    group_members: list[dict[str, Any]] = field(default_factory=list)
    jsonl: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    #: Real evidence that this payload came from the Cuplivo app in its
    #: historical actual-version selection dialect.
    cuplivo_evidence: tuple[str, ...] = ()
    #: Historical ``deleted.json`` document (id-only tombstones).
    deleted_document: dict[str, Any] | None = None

    def revision_groups(self) -> list[RevisionGroup]:
        by_conversation: dict[str, list[dict[str, Any]]] = {}
        for message in self.messages:
            if not isinstance(message, dict):
                continue
            by_conversation.setdefault(
                str(message.get("conversationId") or ""), []).append(message)
        groups: list[RevisionGroup] = []
        for conversation in self.conversations:
            conv_id = str(conversation.get("id") or "")
            candidates = by_conversation.get(conv_id, [])
            grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
            for message in candidates:
                group_id = str(message.get("groupId") or message.get("id") or "")
                subgroup = message.get("subgroupId")
                subgroup_id = str(subgroup) if isinstance(subgroup, str) and subgroup else None
                grouped.setdefault((group_id, subgroup_id), []).append(message)
            for (group_id, subgroup_id), members in grouped.items():
                groups.append(RevisionGroup(
                    conversation_id=conv_id,
                    group_id=group_id,
                    subgroup_id=subgroup_id,
                    message_ids=[str(m.get("id") or "") for m in members],
                    version_numbers=[
                        int(m.get("version") or 0)
                        if isinstance(m.get("version"), int) else 0
                        for m in members
                    ],
                    selected_message_id=None,
                ))
        return groups


@dataclass
class RikkaPayload:
    """Native RikkaHub payload: settings JSON plus SQLite tables."""

    schema_version: int
    settings: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, TableRows] = field(default_factory=dict)


@dataclass
class KelivoPayload:
    """Native Kelivo/Cuplivo payload: settings JSON, SQLite tables or legacy chat."""

    settings: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, TableRows] = field(default_factory=dict)
    legacy_chat: LegacyChatData | None = None
    scope: frozenset[str] | None = None
    app_version: str | None = None


@dataclass
class Backup:
    """A fully read source backup, ready for projection."""

    info: FormatInfo
    payload: RikkaPayload | KelivoPayload
    assets: list[Asset] = field(default_factory=list)
    diagnostics: tuple[Diagnostic, ...] = ()
    #: Root names present in the source archive (used for scope derivation).
    roots: tuple[str, ...] = ()
    #: True when the source may be a partial/incremental export.
    incremental: bool = False

    @property
    def family(self) -> str:
        return self.info.family


# ---------------------------------------------------------------------------
# Neutral projection model (cross-family and legacy<->native conversion)
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    total: int = 0

    @classmethod
    def from_native(cls, value: Any) -> "Usage | None":
        if not isinstance(value, dict):
            return None
        return cls(
            prompt=_int(value.get("promptTokens")),
            completion=_int(value.get("completionTokens")),
            cached=_int(value.get("cachedTokens")),
            total=_int(value.get("totalTokens")),
        )

    def plus(self, other: "Usage") -> "Usage":
        return Usage(self.prompt + other.prompt, self.completion + other.completion,
                     self.cached + other.cached, self.total + other.total)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass
class Part:
    """One message part in the union of both families' documented kinds."""

    kind: str
    text: str | None = None
    created_ms: int | None = None
    finished_ms: int | None = None
    reasoning_type: str | None = None
    call_id: str | None = None
    name: str | None = None
    input_json: str | None = None
    output: list["Part"] | None = None
    approval: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    status: str | None = None
    uri: str | None = None
    mime: str | None = None
    asset_id: str | None = None
    unavailable: bool = False
    raw_kind: str | None = None
    raw_payload: str | None = None
    #: Native passthrough for same-family writes (extra keys of the source).
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class Artifact:
    kind: str
    payload: str
    created_ms: int
    updated_ms: int


@dataclass
class Msg:
    id: str
    role: str
    parts: list[Part] = field(default_factory=list)
    created_ms: int | None = None
    finished_ms: int | None = None
    model_id: str | None = None
    provider_id: str | None = None
    model_snapshot: dict[str, Any] | None = None
    usage: Usage | None = None
    last_usage: Usage | None = None
    generation_time_ms: int | None = None
    step_count: int | None = None
    translation: str | None = None
    is_context_checkpoint: bool = False
    group_id: str | None = None
    subgroup_id: str | None = None
    version: int | None = None
    speaker_id: str | None = None
    is_preset: bool = False
    duration_ms: int | None = None
    reasoning_segments_json: str | None = None
    annotations: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conv:
    id: str
    title: str
    created_ms: int = 0
    updated_ms: int = 0
    assistant_id: str | None = None
    is_pinned: bool = False
    mcp_ids: list[str] = field(default_factory=list)
    summary: str | None = None
    last_summarized_count: int = 0
    suggestions: list[str] = field(default_factory=list)
    truncate_slot: int | None = None
    chat_model_provider: str | None = None
    chat_model_id: str | None = None
    injected_memory_hash: str | None = None
    last_memory_extracted_order: int | None = None
    conversation_kind: str = "normal"
    extras: dict[str, Any] = field(default_factory=dict)
    messages: list[Msg] = field(default_factory=list)
    groups: list[RevisionGroup] = field(default_factory=list)
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class GroupChat:
    id: str
    conversation_id: str
    members: list[dict[str, Any]] = field(default_factory=list)
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryDoc:
    """One RikkaHub markdown memory file."""

    assistant_id: str
    path: str
    content: str
    updated_ms: int
    pinned: bool = True


@dataclass
class MemoryEntry:
    """One Kelivo/Cuplivo typed memory entry."""

    id: str
    scope: str
    assistant_id: str | None
    type: str
    status: str
    content: str
    source: str = "manual"
    created_us: int = 0
    updated_us: int = 0
    related_ids: list[str] = field(default_factory=list)
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantMemoryFlags:
    """Native memory permissions of one assistant."""

    use_global: bool = False
    enable: bool = True


@dataclass
class Workspace:
    id: str
    name: str
    created_ms: int = 0
    updated_ms: int = 0
    last_used_ms: int | None = None
    kind: str | None = None
    host_path: str | None = None
    shell_needs_approval: bool | None = None
    default_cwd: str = ""
    disabled_tools: list[str] = field(default_factory=list)
    root: str | None = None
    shell_status: str | None = None
    tool_approvals: str | None = None
    shell_compatibility_mode: int | None = None
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    id: str
    name: str
    entries: list[str] = field(default_factory=list)
    native: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    """Neutral view of everything a family can carry."""

    conversations: list[Conv] = field(default_factory=list)
    #: Native scope of the source (what the file claims to carry).
    scope: frozenset[str] | None = None
    group_chats: list[GroupChat] = field(default_factory=list)
    memory_docs: list[MemoryDoc] = field(default_factory=list)
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    assistant_flags: dict[str, AssistantMemoryFlags] = field(default_factory=dict)
    workspaces: list[Workspace] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    media: dict[str, TableRows] = field(default_factory=dict)
    translations: TableRows | None = None
    usage: TableRows | None = None
    tombstones: TableRows | None = None
    extensions: TableRows | None = None
    favorites: TableRows | None = None
    folders: TableRows | None = None
    gen_media: TableRows | None = None
    consolidation: TableRows | None = None
    #: Per-conversation MCP server id lists, in source order.
    mcp_by_conversation: dict[str, list[str]] = field(default_factory=dict)
    #: Native settings payload of the source family (for settings conversion).
    settings: dict[str, Any] = field(default_factory=dict)
    #: Assets that belong to the conversation tree, keyed by source entry.
    asset_entries: list[str] = field(default_factory=list)
    #: Whether the source actually carries a chat domain (an empty database is
    #: a present, zero-row chat domain; a settings-only backup is not).
    has_chat_domain: bool = False
    #: Historical ``deleted.json`` payload, preserved only for a legacy target.
    deleted_document: dict[str, Any] | None = None
    #: Native generation runs (Kelivo family); nothing else can express them.
    generation_runs: TableRows | None = None
    #: Summary counters for the CLI.
    counts: dict[str, int] = field(default_factory=dict)
    #: Diagnostics discovered while decoding (legacy selection, missing refs).
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass
class SettingsResult:
    """Settings produced for a target plus the id maps the payload needs."""

    settings: dict[str, Any]
    provider_ids: dict[str, str] = field(default_factory=dict)
    model_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    assistant_ids: dict[str, str] = field(default_factory=dict)
    workspace_ids: dict[str, str] = field(default_factory=dict)
    skill_ids: dict[str, str] = field(default_factory=dict)
    models_by_rh_uuid: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Generic id maps for other entity kinds (mcp, instruction, worldBook, ...).
    entity_ids: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Memory projected for a RikkaHub target.
    memory_docs: list[MemoryDoc] = field(default_factory=list)
    #: Memory projected for a Kelivo/Cuplivo target.
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    #: Per-assistant feature permissions that gate conversation fields.
    assistant_permissions: dict[str, dict[str, bool]] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass
class ConversionPlan:
    """A fully validated conversion whose output is staged but unpublished."""

    target: TargetProfile
    backup: Backup
    diagnostics: tuple[Diagnostic, ...]
    counts: dict[str, int]
    write: Any  # Callable[[Path], None]
    source: Any = None


@dataclass
class ConversionResult:
    output: Path
    counts: dict[str, int]
    diagnostics: tuple[Diagnostic, ...]


