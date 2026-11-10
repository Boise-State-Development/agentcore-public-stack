"""The one place a turn decides how each attachment reaches the model.

``docs/specs/unified-attachment-delivery.md`` §4.2: a pure function from facts to
a plan. ``plan_attachments(files, model, held)`` reads facts the turn path has
already resolved (the upload rows, the decoded size of bytes already in hand,
the model's catalog row) and returns one :class:`PlanEntry` per file: a
``mode``, the file's ``class``, an estimated ``tokens`` and a ``reason`` code.
It does no I/O and parses nothing, so it adds nothing before the first token.

**Profiles, not code paths (§9.1 rule 1).** Every limit the planner applies is
a field of a :class:`LimitsProfile`. Today there is one, ``legacy``, which
reproduces the byte rules the chat route applied before this module existed:

- the per-message file count (``MAX_FILES_PER_MESSAGE``, 5), applied to the
  request's ids before anything is fetched (:func:`cap_message_attachments`);
- reference-class uploads (a document over the inline limit, or a file from an
  attached folder) go ``on_demand``: a digest or stub plus ``document_read``;
- spreadsheets and decks go to their ``tool``s, at any size;
- any other non-image file over ``INLINE_DOCUMENT_MAX_BYTES`` is ``dropped``;
- the inline set is held first-fit, in attachment order, to
  ``INLINE_ATTACHMENTS_MAX_TOTAL_BYTES``; an uploaded document that does not
  fit goes ``on_demand`` (``INLINE_BUDGET_BY_REFERENCE_ENABLED``), anything
  else that does not fit is ``dropped``;
- on a model that reads TEXT only, documents become ``text`` (extracted, with a
  per-document share of ``TEXT_ONLY_DOCUMENTS_MAX_CHARS``) and images are
  ``dropped``;
- on a Converse model with ``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED``, a renderable
  non-PDF document is ``text`` (the per-call projection renders it); on any
  other provider it always is (``provider_needs_text``).

The flags those rules read are inputs to the profile, read once in
:func:`legacy_profile`; nothing here branches on a flag anywhere else.

**The ``token`` profile (phase 3).** The same function with a different
:class:`LimitsProfile` (:func:`token_profile`): the §4.3 ladder against a
per-model ``document_budget`` of half the chat ceiling (§5.2), applied by
:func:`refine_for_model` once the model is known:

- a PDF goes ``native`` within the model's page limit and the budget, on a
  model with image input; ``text`` when it is over the page limit, has a text
  layer and its text fits; otherwise ``on_demand``;
- every other renderable document goes ``text`` within the budget, else
  ``on_demand``; it is never a native block;
- an image is ``native`` and counts against the budget; spreadsheets and decks
  are ``tool``; a reference-class upload stays ``on_demand``;
- a document over the per-file inline limit with a row goes ``on_demand``
  rather than being dropped. Only a file with nothing to read it from later (a
  direct base64 attachment with no row, an image) can still be over a byte
  rule; phase 4's handles remove those.

Each file is costed in the turn model's own tokens (:func:`document_cost`):
the row's ``tokens.text`` times the model's tokenizer factor, plus
``PDF_PAGE_IMAGE_TOKENS`` a page for a native PDF (measured, §2.1). The budget
is spent per turn, in attachment order. The byte rules that protect the
AgentCore Memory event (the per-message count, the combined inline bytes) are
unchanged until phase 4. :func:`current_limits_profile` selects it with
``ATTACHMENT_PLAN_ENABLED``.

**One place decides (§9.1 rule 3).** ``tests/architecture/test_attachment_plan_boundary.py``
fails if a turn-path module outside this one reads a limit constant or a class
predicate to choose a mode.

**The fixture-only override (§8.2).** A file row's ``delivery_override``
(``FileMetadata``, DynamoDB ``deliveryOverride``) forces a mode so an eval can
hold a question fixed and vary only how the file is delivered. Only a direct row
write sets it: no request field and no app-api route can
(``tests/apis/shared/files/test_delivery_override_is_fixture_only.py``). The
``legacy`` profile can force ``native``, ``text`` and ``on_demand``:

- a forced ``native`` file stays a document block on a Converse model even
  with documents-as-text on (the route pins it in the projection,
  ``document_cap.pin_native``); off Converse it is still text, since those
  formatters cannot read the block;
- a forced ``text`` file is the document's text in the message: the
  projection's rendering where it renders that format, else the extracted
  text layer (a PDF, or a Converse model with documents-as-text off), held to
  ``FORCED_TEXT_MAX_CHARS``;
- a forced ``on_demand`` file under the reference size is read from its
  original by ``document_read``, as an over-budget one is
  (:func:`reads_from_original`).

``search`` is not forceable: in the ``legacy`` profile only a prepared
reference-class document is ingested into the attachments KB (phase 5).

**What is recorded.** :meth:`AttachmentPlan.to_row` is written on the turn's
``C#`` cost row as ``attachmentPlan`` after the stream: modes, classes, reason
codes, token estimates and upload ids. Never a filename.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from apis.shared.files.document_read import document_format_for, is_document_class
from apis.shared.files.models import (
    DELIVERY_OVERRIDE_VALUES,
    INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
    INLINE_DOCUMENT_MAX_BYTES,
    LARGE_DOCUMENT_MAX_BYTES,
    MAX_FILES_PER_MESSAGE,
    TEXT_ONLY_DOCUMENTS_MAX_CHARS,
    is_presentation_file,
    is_reference_document,
    is_tabular_file,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: A PDF ``document`` block, an ``image`` block, or (legacy) any other document block.
MODE_NATIVE = "native"
#: The document's text in the message: extracted for a TEXT-only model, or
#: rendered by the Converse projection.
MODE_TEXT = "text"
#: A digest or stub in the message; ``document_read`` reads the content.
MODE_ON_DEMAND = "on_demand"
#: ``on_demand`` plus ``document_read``'s ``query`` (phase 5; not chosen by ``legacy``).
MODE_SEARCH = "search"
#: An inventory line; the spreadsheet or deck tools read the content.
MODE_TOOL = "tool"
#: Not delivered at all, named in the attachment guidance. ``legacy`` only:
#: the ``token`` profile never drops a file (§4.3).
MODE_DROPPED = "dropped"

MODES = (MODE_NATIVE, MODE_TEXT, MODE_ON_DEMAND, MODE_SEARCH, MODE_TOOL, MODE_DROPPED)

#: Modes a file row's ``delivery_override`` may force.
FORCEABLE_MODES = frozenset({MODE_NATIVE, MODE_TEXT, MODE_ON_DEMAND})

#: Characters of extracted text a forced ``text`` file may carry: about 400k
#: estimated tokens, above any fixture the delivery evals attach. A forced
#: arm measures the mode, not a sampling rule.
FORCED_TEXT_MAX_CHARS = 1_600_000

CLASS_IMAGE = "image"
CLASS_PDF = "pdf"
CLASS_DOCUMENT = "document"
CLASS_TABULAR = "tabular"
CLASS_PRESENTATION = "presentation"
CLASS_OTHER = "other"

# Reason codes. Stable strings: they are stored on cost rows and pinned by the
# table test, so a rename is a data change.
REASON_INLINE = "inline"
REASON_REFERENCE_CLASS = "reference_class"
REASON_FORCED = "forced"
REASON_TABULAR = "tabular"
REASON_PRESENTATION = "presentation"
REASON_OVERSIZED = "oversized"
REASON_OVER_BUDGET = "over_budget"
REASON_OVER_BUDGET_REFERENCE = "over_budget_reference"
REASON_DOCUMENTS_AS_TEXT = "documents_as_text"
#: A model off Converse (OpenAI-shaped, Gemini, Mantle): its formatter reads a
#: non-PDF document block as an opaque file, so it always gets the text.
REASON_PROVIDER_NEEDS_TEXT = "provider_needs_text"
REASON_TEXT_ONLY_MODEL = "text_only_model"
REASON_TEXT_ONLY_IMAGE = "text_only_image"
REASON_TEXT_ONLY_UNREADABLE = "text_only_unreadable"
# Outcomes recorded after the plan, when carrying it out failed (never a choice).
REASON_PERSIST_FAILED = "persist_failed"
REASON_TEXT_UNAVAILABLE = "text_unavailable"

# The ``token`` profile's reasons (§4.3). Stable strings, like the rest.
#: Within the turn's ``document_budget``: a PDF ``native``, anything else ``text``.
REASON_WITHIN_BUDGET = "within_budget"
#: Over what is left of the budget, so ``document_read`` reads it.
REASON_OVER_DOCUMENT_BUDGET = "over_document_budget"
#: A PDF past the model's page limit whose text layer fits the budget.
REASON_PDF_PAGE_LIMIT = "pdf_page_limit"
#: Over the budget with no row to read it from later: sent as planned.
REASON_NO_ROW = "no_row"
#: The row has no facts yet (preparation still queued, or a row written before
#: them): the file is sent the way the legacy rules would send it, for this
#: turn only (§4.1), and counted at its bytes estimate.
REASON_FACTS_PENDING = "facts_pending"

PROFILE_LEGACY = "legacy"
#: Phase 3 (§9 / §9.1): selected by ``ATTACHMENT_PLAN_ENABLED``.
PROFILE_TOKEN = "token"

# ---------------------------------------------------------------------------
# Per-model document costs (§2.1, §5.2)
# ---------------------------------------------------------------------------

#: Input tokens one PDF page's image costs, on every model probed: 15,624 to
#: 15,639 for ten near-empty pages on Haiku 4.5, Haiku 5.5 and Sonnet 4.6, and
#: 3,159 for two on Opus 4.7 and Opus 5.5 (dev, 2026-11-10).
PDF_PAGE_IMAGE_TOKENS = 1_565

#: Tokens the model bills per ``tokens.text`` (characters / 4) of extracted
#: text, by model id. Measured on dev on 2026-11-10 with the 32-page
#: ``delivery_report_30k.pdf`` fixture (``tokens.text`` 30,779): its text alone
#: was 29,298 tokens on Haiku 4.5 and Sonnet 4.6 and 43,225 on Haiku 5.5, Opus
#: 4.7 and Opus 5.5; the PDF was its page images plus that text to within 0.1%.
LEGACY_TOKENIZER_FACTOR = 0.95
#: The newer tokenizer, and the default for any model not listed: it
#: over-counts a legacy tokenizer by ~45%, which only sends a file
#: ``on_demand`` sooner (the cheaper mode, equal on quality in phase 0).
DEFAULT_TOKENIZER_FACTOR = 1.40
_LEGACY_TOKENIZER_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-6")

#: PDF pages Bedrock accepts in one block, by model id (the phase 0 probe,
#: #1961): 100 on a 200k model, 600 on a 1M one. An unlisted model gets 600
#: when its window is at least 1M and 100 otherwise, which is the probe's rule.
_PDF_PAGE_LIMITS = (("claude-haiku-4-5", 100), ("claude-haiku-5-5", 600), ("claude-sonnet-4-6", 600))
PDF_PAGE_LIMIT_SMALL_WINDOW = 100
PDF_PAGE_LIMIT_LARGE_WINDOW = 600
LARGE_WINDOW_TOKENS = 1_000_000

#: ``document_budget`` as a share of the chat ceiling (§5.2, the phase 0 readout).
DOCUMENT_BUDGET_CEILING_SHARE = 0.5

#: Floor on one document's share of ``text_only_max_chars``, so five
#: attachments still get a useful excerpt each.
TEXT_ONLY_DOCUMENT_MIN_CHARS = 4_000

#: Estimated tokens for an image block, and per PDF page when the row knows its
#: page count. The same 1,500 as ``compaction_policy.IMAGE_TOKEN_ESTIMATE`` and
#: ``document_tokens.PDF_PAGE_TOKEN_ESTIMATE``; phase 2 replaces both estimates
#: with row facts computed at upload.
IMAGE_TOKENS = 1_500
PDF_PAGE_TOKENS = 1_500
CHARS_PER_TOKEN = 4

#: Formats the Converse projection renders as text (``document_cap.RENDERABLE_FORMATS``),
#: by file extension, the way the route chose them. Kept here, not imported, so
#: this module stays free of the model layer; ``test_attachment_plan.py`` pins the two equal.
DOCUMENTS_AS_TEXT_EXTENSIONS = frozenset({"txt", "md", "csv", "html", "docx"})


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitsProfile:
    """Every limit the planner applies. A profile is data; the planner is one function."""

    name: str
    #: Files per message across direct bytes, upload ids and folders; ``<= 0`` disables.
    max_files_per_message: int
    #: Per-file ceiling for a non-image document sent inline.
    inline_document_max_bytes: int
    #: The combined inline budget for one message; ``<= 0`` disables.
    inline_total_max_bytes: int
    #: ``ATTACHMENT_TURN_GUARD_ENABLED``: the count cap and the combined budget apply.
    turn_guard: bool = True
    #: ``INLINE_BUDGET_BY_REFERENCE_ENABLED``: an uploaded document over the
    #: combined budget goes ``on_demand`` rather than being dropped.
    over_budget_by_reference: bool = True
    #: ``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED``: renderable non-PDF documents are
    #: sent to a Converse model as text.
    documents_as_text: bool = False
    #: Characters of extracted text per turn for a TEXT-only model.
    text_only_max_chars: int = 60_000
    #: How many ids the resolver fetches when the guard is off (its old backstop).
    resolver_backstop_without_guard: int = 5
    #: ``document_budget`` as a share of the model's chat ceiling. ``0`` is the
    #: ``legacy`` profile: no token budget, the byte rules only.
    document_budget_share: float = 0.0


@dataclass(frozen=True)
class FileFacts:
    """What the planner knows about one attachment. No bytes, no text."""

    filename: str
    mime_type: str
    #: Decoded bytes in hand (inline) or the row's size (by reference).
    size_bytes: int = 0
    upload_id: Optional[str] = None
    #: The resolver delivered it without bytes (``delivers_by_reference``).
    by_reference: bool = False
    #: It came from an upload row, so ``document_read`` can read it later.
    has_row: bool = False
    #: The row's fixture-only ``delivery_override``.
    forced_mode: Optional[str] = None
    #: Pages, when the row knows (a prepared reference-class document).
    page_count: Optional[int] = None
    #: The row's facts (§4.1), when current: ``tokens.text``, ``tokens.native``
    #: and ``hasTextLayer``. Read by the ``token`` profile only.
    text_tokens: Optional[int] = None
    native_tokens: Optional[int] = None
    has_text_layer: Optional[bool] = None


@dataclass(frozen=True)
class ModelFacts:
    """What the planner needs to know about the turn's model."""

    #: The catalog row declares TEXT input only.
    text_only: bool = False
    #: The model speaks Bedrock Converse (where the document projection is installed).
    converse: bool = True
    #: Context window in tokens, when known. Read by the ``token`` profile.
    context_window: Optional[int] = None
    #: The turn's model id: picks its tokenizer factor and PDF page limit.
    model_id: Optional[str] = None
    #: The compaction ceiling this model runs under (``CompactionPolicy``),
    #: which ``document_budget`` is a share of. ``None``: no token budget.
    chat_ceiling: Optional[int] = None


@dataclass(frozen=True)
class ConversationFacts:
    """What the conversation already holds. Read by the ``token`` profile."""

    attachment_tokens: int = 0


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanEntry:
    """One file's delivery. ``index`` is its position in the planned list."""

    index: int
    mode: str
    file_class: str
    tokens: int
    reason: str
    upload_id: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "mode": self.mode,
            "class": self.file_class,
            "tokens": int(self.tokens),
            "reason": self.reason,
        }
        if self.upload_id:
            row["uploadId"] = self.upload_id
        return row


@dataclass(frozen=True)
class AttachmentPlan:
    """The decision for every file of one turn, in attachment order."""

    profile: str
    entries: Tuple[PlanEntry, ...] = ()
    #: Attachments the count cap removed before resolution (counted, not planned).
    dropped_over_count: int = 0
    #: Decoded bytes of the inline candidates the combined budget judged, and
    #: whether it moved any out (the over-quota log and metric read these).
    requested_inline_bytes: int = 0
    #: Per-document character share for a TEXT-only model (0 when none).
    text_char_limit: int = 0

    def indices(self, *modes: str, reason: Optional[str] = None) -> List[int]:
        return [
            e.index for e in self.entries if e.mode in modes and (reason is None or e.reason == reason)
        ]

    def with_reason(self, *reasons: str) -> List[int]:
        return [e.index for e in self.entries if e.reason in reasons]

    def with_outcome(self, index: int, reason: str) -> "AttachmentPlan":
        """The plan with file ``index`` recorded as dropped for ``reason``.

        For a plan that could not be carried out (an inline spreadsheet that
        failed to store, a document with no text layer to extract). It records
        what happened; it never chooses.
        """
        entries = tuple(
            replace(e, mode=MODE_DROPPED, reason=reason, tokens=0) if e.index == index else e
            for e in self.entries
        )
        return replace(self, entries=entries)

    def to_row(self) -> Optional[Dict[str, Any]]:
        """``attachmentPlan`` for the turn's ``C#`` row, or ``None`` when nothing was attached."""
        if not self.entries and not self.dropped_over_count:
            return None
        row: Dict[str, Any] = {
            "profile": self.profile,
            "files": [e.to_row() for e in self.entries],
        }
        if self.dropped_over_count:
            row["droppedOverCount"] = int(self.dropped_over_count)
        return row


@dataclass(frozen=True)
class CountCap:
    """What survives the per-message count, before anything is fetched."""

    direct_files: list = field(default_factory=list)
    upload_ids: list = field(default_factory=list)
    folder_ids: list = field(default_factory=list)
    #: Direct files over the cap (their names are known).
    dropped_names: list = field(default_factory=list)
    #: Everything over the cap: direct files, ids and folders.
    dropped_total: int = 0
    #: The resolver's own backstop: ``None`` once the cap has been applied here.
    resolver_max_files: Optional[int] = None


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def legacy_profile() -> LimitsProfile:
    """Today's byte rules, with the flags they read as inputs (§9.1 rule 1)."""
    from apis.shared.feature_flags import (
        attachment_turn_guard_enabled,
        converse_documents_as_text_enabled,
        inline_budget_by_reference_enabled,
    )

    return LimitsProfile(
        name=PROFILE_LEGACY,
        max_files_per_message=MAX_FILES_PER_MESSAGE,
        inline_document_max_bytes=INLINE_DOCUMENT_MAX_BYTES,
        inline_total_max_bytes=INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
        turn_guard=attachment_turn_guard_enabled(),
        over_budget_by_reference=inline_budget_by_reference_enabled(),
        documents_as_text=converse_documents_as_text_enabled(),
        text_only_max_chars=TEXT_ONLY_DOCUMENTS_MAX_CHARS,
    )


def token_profile() -> LimitsProfile:
    """The ``token`` profile (§4.3, §5.2): the legacy byte limits that protect the
    persisted message, plus the per-model document budget, and text for every
    renderable non-PDF document whatever ``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED`` says."""
    return replace(
        legacy_profile(),
        name=PROFILE_TOKEN,
        documents_as_text=True,
        over_budget_by_reference=True,
        document_budget_share=DOCUMENT_BUDGET_CEILING_SHARE,
    )


def current_limits_profile() -> LimitsProfile:
    """The profile this turn plans with: ``token`` with ``ATTACHMENT_PLAN_ENABLED``,
    else ``legacy``. The flag is read here and nowhere else (§9.1 rule 1)."""
    from apis.shared.feature_flags import attachment_plan_enabled

    return token_profile() if attachment_plan_enabled() else legacy_profile()


def documents_as_text() -> bool:
    """Whether the per-call projection renders every renderable non-PDF document
    as text on a Converse model: the active profile's rule, so the projection
    and the plan always agree."""
    return current_limits_profile().documents_as_text


# ---------------------------------------------------------------------------
# Upload limits (admission and ``GET /files/limits``, §4.4)
# ---------------------------------------------------------------------------

#: Defaults for the per-class upload ceilings. The environment overrides are read
#: in :func:`upload_limits` and nowhere else.
UPLOAD_GENERAL_MAX_BYTES = 4 * 1024 * 1024
UPLOAD_PRESENTATION_MAX_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class UploadLimits:
    """What an upload may be, by class: the admission side of the profile.

    app-api admission (``FileUploadService.max_size_for``), the inline persist
    path and ``GET /files/limits`` all read this, so the SPA and the TUI are told
    exactly what presign will accept.
    """

    #: Images, and documents when large-document attachments are off.
    general_max_bytes: int
    presentation_max_bytes: int
    spreadsheet_max_bytes: int
    #: A document-class file (pdf, docx, txt, md, html) attached by reference;
    #: ``None`` while ``LARGE_DOCUMENT_ATTACHMENTS_ENABLED`` is off.
    large_document_max_bytes: Optional[int] = None
    #: Folders one message may carry; ``None`` while folder attachments are off.
    folders_per_message: Optional[int] = None


def upload_limits() -> UploadLimits:
    """The per-class upload ceilings, with the two flags that widen admission."""
    import os

    from apis.shared.feature_flags import (
        folder_attachments_enabled,
        large_document_attachments_enabled,
    )
    from apis.shared.files.folder_attachments import FOLDERS_PER_MESSAGE
    from apis.shared.files.models import spreadsheet_max_file_size

    return UploadLimits(
        general_max_bytes=int(os.environ.get("FILE_UPLOAD_MAX_SIZE_BYTES", UPLOAD_GENERAL_MAX_BYTES)),
        presentation_max_bytes=int(
            os.environ.get("FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION", UPLOAD_PRESENTATION_MAX_BYTES)
        ),
        spreadsheet_max_bytes=spreadsheet_max_file_size(),
        large_document_max_bytes=LARGE_DOCUMENT_MAX_BYTES if large_document_attachments_enabled() else None,
        folders_per_message=FOLDERS_PER_MESSAGE if folder_attachments_enabled() else None,
    )


def upload_size_limit(filename: str, mime_type: str, limits: UploadLimits) -> int:
    """The upload ceiling for one file: decks, then spreadsheets, then large documents."""
    if is_presentation_file(filename, mime_type):
        return limits.presentation_max_bytes
    if is_tabular_file(filename, mime_type):
        return limits.spreadsheet_max_bytes
    if limits.large_document_max_bytes is not None and is_document_class(mime_type, filename):
        return limits.large_document_max_bytes
    return limits.general_max_bytes


def upload_download_ceiling(limits: UploadLimits) -> int:
    """The widest class ceiling: what a server-side import may download before sizing."""
    caps = [limits.general_max_bytes, limits.presentation_max_bytes, limits.spreadsheet_max_bytes]
    if limits.large_document_max_bytes is not None:
        caps.append(limits.large_document_max_bytes)
    return max(caps)


def client_limits(
    profile: Optional[LimitsProfile] = None,
    uploads: Optional[UploadLimits] = None,
) -> Dict[str, Any]:
    """The limits a client enforces before it uploads (``GET /files/limits``).

    Built from the active profile and the upload ceilings, so what the composer
    refuses is what admission and the turn would refuse. Fields the ``token``
    profile adds (``imagesPerMessage``, ``pdfMaxPages``) are in the shape now and
    ``None`` until a profile sets them; ``None`` always means "no limit".
    """
    profile = profile or current_limits_profile()
    uploads = uploads or upload_limits()
    count = profile.max_files_per_message if profile.turn_guard else profile.resolver_backstop_without_guard
    budget = profile.inline_total_max_bytes if profile.turn_guard else 0
    return {
        "profile": profile.name,
        "filesPerMessage": count if count > 0 else None,
        "imagesPerMessage": None,
        "pdfMaxPages": None,
        "inlineDocumentMaxBytes": profile.inline_document_max_bytes,
        "inlineBudgetBytes": budget if budget > 0 else None,
        "maxBytes": {
            "document": uploads.general_max_bytes,
            "image": uploads.general_max_bytes,
            "spreadsheet": uploads.spreadsheet_max_bytes,
            "presentation": uploads.presentation_max_bytes,
            "largeDocument": uploads.large_document_max_bytes,
        },
        "largeDocuments": uploads.large_document_max_bytes is not None,
        "folders": {
            "enabled": uploads.folders_per_message is not None,
            "perMessage": uploads.folders_per_message,
        },
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def file_class(filename: str, mime_type: str) -> str:
    """The planner's class for a file. Spreadsheets and decks before anything else."""
    filename = filename or ""
    mime_type = mime_type or ""
    if is_tabular_file(filename, mime_type):
        return CLASS_TABULAR
    if is_presentation_file(filename, mime_type):
        return CLASS_PRESENTATION
    if mime_type.lower().startswith("image/"):
        return CLASS_IMAGE
    fmt = document_format_for(mime_type, filename)
    if fmt == "pdf":
        return CLASS_PDF
    if fmt is not None:
        return CLASS_DOCUMENT
    return CLASS_OTHER


def delivers_by_reference(meta: Any) -> bool:
    """Whether an upload row is resolved without its bytes (read on demand).

    The resolver asks before any S3 read, so a 50 MB object is never fetched for
    a turn that will not send it. A row's fixture-only ``delivery_override``
    wins; otherwise the reference class decides (``is_reference_document``).
    """
    forced = getattr(meta, "delivery_override", None)
    if forced in (MODE_NATIVE, MODE_TEXT):
        return False
    if forced == MODE_ON_DEMAND:
        return document_format_for(meta.mime_type, meta.filename) is not None
    return is_reference_document(meta)


def reads_from_original(meta: Any) -> bool:
    """Whether a by-reference row is read from its original rather than prepared sidecars.

    True for a row forced ``on_demand`` that is under the reference size: the
    prepare Lambda never prepares it, so it renders like an over-budget
    reference (its upload digest, or a ``ready`` stub) and ``document_read``
    reads the original. Every user row answers ``False``.
    """
    return getattr(meta, "delivery_override", None) == MODE_ON_DEMAND and not is_reference_document(meta)


def _forced(facts: FileFacts) -> Optional[str]:
    return facts.forced_mode if facts.forced_mode in FORCEABLE_MODES else None


def _extension(filename: str) -> str:
    name = (filename or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _estimate_tokens(facts: FileFacts, cls: str) -> int:
    """Legacy estimate from facts in hand: no parse. Phase 2 replaces it with row facts."""
    if cls == CLASS_IMAGE:
        return IMAGE_TOKENS
    byte_tokens = max(0, int(facts.size_bytes)) // CHARS_PER_TOKEN
    if cls == CLASS_PDF and facts.page_count:
        return max(int(facts.page_count) * PDF_PAGE_TOKENS, byte_tokens)
    return byte_tokens


def tokenizer_factor(model_id: Optional[str]) -> float:
    """Tokens the model bills per ``tokens.text``: :data:`LEGACY_TOKENIZER_FACTOR`
    for the listed models, :data:`DEFAULT_TOKENIZER_FACTOR` for every other."""
    mid = (model_id or "").lower()
    if any(key in mid for key in _LEGACY_TOKENIZER_MODELS):
        return LEGACY_TOKENIZER_FACTOR
    return DEFAULT_TOKENIZER_FACTOR


def pdf_page_limit(model: ModelFacts) -> int:
    """Pages one native PDF block may have on this model (§2.1)."""
    mid = (model.model_id or "").lower()
    for key, limit in _PDF_PAGE_LIMITS:
        if key in mid:
            return limit
    if model.context_window and int(model.context_window) >= LARGE_WINDOW_TOKENS:
        return PDF_PAGE_LIMIT_LARGE_WINDOW
    return PDF_PAGE_LIMIT_SMALL_WINDOW


def document_budget(model: ModelFacts, profile: LimitsProfile) -> Optional[int]:
    """Tokens of ``native`` and ``text`` attachments one turn may carry, or ``None``
    when the profile has no token budget or the ceiling is unknown."""
    if profile.document_budget_share <= 0 or not model.chat_ceiling:
        return None
    return int(int(model.chat_ceiling) * profile.document_budget_share)


def document_cost(facts: FileFacts, cls: str, mode: str, model: ModelFacts) -> int:
    """Estimated input tokens for one file in ``mode`` on ``model`` (§5.2).

    From the row's facts when it has them: text at the model's tokenizer factor,
    plus the page images for a native PDF. Without facts, today's estimate from
    the bytes (:func:`_estimate_tokens`), scaled the same way.
    """
    if cls == CLASS_IMAGE:
        return IMAGE_TOKENS
    factor = tokenizer_factor(model.model_id)
    if facts.text_tokens is not None:
        text = int(math.ceil(max(0, int(facts.text_tokens)) * factor))
        if cls == CLASS_PDF and mode == MODE_NATIVE:
            pages = facts.page_count
            images = int(pages) * PDF_PAGE_IMAGE_TOKENS if pages else int(facts.native_tokens or 0)
            return text + images
        return text
    return int(math.ceil(_estimate_tokens(facts, cls) * factor))


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def cap_message_attachments(
    direct_files: Sequence[Any],
    upload_ids: Sequence[str],
    folder_ids: Sequence[str],
    profile: LimitsProfile,
    *,
    folders_dropped: int = 0,
) -> CountCap:
    """Hold a message to the profile's file count, before anything is fetched.

    Direct ``files`` come first (already in the request body), then upload ids
    fill the remainder, then folders (each counting ONE, drive-folder spec §4.4).
    Order is kept, so the first N attached are the N that survive. Ids past the
    budget are counted, not named: resolving them only to name the losers would
    fan out a read per id a client chose to send. ``folders_dropped`` is what the
    folder limit of its own already removed; it is counted whatever the guard.
    """
    direct = list(direct_files)
    ids = list(upload_ids)
    folders = list(folder_ids)
    dropped_total = int(folders_dropped)
    if not profile.turn_guard:
        return CountCap(
            direct_files=direct,
            upload_ids=ids,
            folder_ids=folders,
            dropped_total=dropped_total,
            resolver_max_files=profile.resolver_backstop_without_guard,
        )

    cap = profile.max_files_per_message
    dropped_names: list = []
    if cap > 0:
        kept_direct = direct[:cap]
        dropped_names = [f.filename for f in direct[cap:]]
        id_budget = max(0, cap - len(kept_direct))
        kept_ids = ids[:id_budget]
        dropped_total += len(dropped_names) + (len(ids) - len(kept_ids))
        direct, ids = kept_direct, kept_ids
        folder_budget = max(0, cap - len(direct) - len(ids))
        dropped_total += max(0, len(folders) - folder_budget)
        folders = folders[:folder_budget]
    return CountCap(
        direct_files=direct,
        upload_ids=ids,
        folder_ids=folders,
        dropped_names=dropped_names,
        dropped_total=dropped_total,
        resolver_max_files=None,
    )


def plan_attachments(
    files: Sequence[FileFacts],
    model: Optional[ModelFacts],
    held: Optional[ConversationFacts] = None,
    *,
    profile: LimitsProfile,
    dropped_over_count: int = 0,
) -> AttachmentPlan:
    """Decide every file's delivery, in attachment order (§4.2).

    ``model=None`` plans before the model is known: the byte rules only, which
    in the ``legacy`` profile never depend on the model. The route plans again
    with the model once it is resolved; the same facts give the same byte
    decisions, and the model rules refine the inline set.
    """
    del held  # read by the token profile only
    classes = [file_class(f.filename, f.mime_type) for f in files]
    modes: List[Optional[str]] = [None] * len(files)
    reasons: List[Optional[str]] = [None] * len(files)
    candidates: List[int] = []

    for i, facts in enumerate(files):
        cls = classes[i]
        forced = _forced(facts)
        if facts.by_reference:
            modes[i] = MODE_ON_DEMAND
            reasons[i] = REASON_FORCED if forced == MODE_ON_DEMAND else REASON_REFERENCE_CLASS
            continue
        if cls == CLASS_TABULAR:
            modes[i], reasons[i] = MODE_TOOL, REASON_TABULAR
            continue
        if cls == CLASS_PRESENTATION:
            modes[i], reasons[i] = MODE_TOOL, REASON_PRESENTATION
            continue
        if forced in (MODE_NATIVE, MODE_TEXT) and cls != CLASS_IMAGE:
            modes[i], reasons[i] = forced, REASON_FORCED
            candidates.append(i)
            continue
        # Images have their own (larger) Bedrock limits and become image
        # blocks, which the per-document ceiling does not apply to.
        if cls != CLASS_IMAGE and facts.size_bytes > profile.inline_document_max_bytes:
            if profile.document_budget_share > 0 and facts.has_row and cls in (CLASS_PDF, CLASS_DOCUMENT):
                # The token profile never drops a file it can read later (§4.3).
                modes[i], reasons[i] = MODE_ON_DEMAND, REASON_OVER_BUDGET_REFERENCE
                continue
            modes[i], reasons[i] = MODE_DROPPED, REASON_OVERSIZED
            continue
        modes[i], reasons[i] = MODE_NATIVE, REASON_INLINE
        candidates.append(i)

    # The combined budget: first-fit in attachment order. Images count — they
    # are in the same persisted message. A forced file always stays.
    requested = sum(max(0, files[i].size_bytes) for i in candidates)
    if profile.turn_guard and candidates and profile.inline_total_max_bytes > 0:
        running = 0
        for i in candidates:
            size = max(0, files[i].size_bytes)
            if reasons[i] != REASON_FORCED and running + size > profile.inline_total_max_bytes:
                document_reads_it = files[i].has_row and classes[i] in (CLASS_PDF, CLASS_DOCUMENT)
                if profile.over_budget_by_reference and document_reads_it:
                    modes[i], reasons[i] = MODE_ON_DEMAND, REASON_OVER_BUDGET_REFERENCE
                else:
                    modes[i], reasons[i] = MODE_DROPPED, REASON_OVER_BUDGET
                continue
            running += size

    entries = tuple(
        PlanEntry(
            index=i,
            mode=modes[i] or MODE_DROPPED,
            file_class=classes[i],
            tokens=_estimate_tokens(facts, classes[i]) if modes[i] in (MODE_NATIVE, MODE_TEXT) else 0,
            reason=reasons[i] or REASON_INLINE,
            upload_id=facts.upload_id,
        )
        for i, facts in enumerate(files)
    )
    plan = AttachmentPlan(
        profile=profile.name,
        entries=entries,
        dropped_over_count=int(dropped_over_count),
        requested_inline_bytes=requested,
    )
    if model is None:
        return plan
    return refine_for_model(plan, files, model, profile=profile)


def refine_for_model(
    plan: AttachmentPlan,
    files: Sequence[FileFacts],
    model: ModelFacts,
    *,
    profile: LimitsProfile,
) -> AttachmentPlan:
    """Apply the model's rules to the plan's ``native`` entries.

    The route resolves attachments before the model (map stage B4 runs before
    B11), so it plans the byte rules first and calls this once the model is
    known. Only ``native`` entries change; an outcome already recorded on the
    plan (``with_outcome``) is kept.
    """
    entries = list(plan.entries)
    native = [e for e in entries if e.mode == MODE_NATIVE]
    # Idempotent: a second call finds no native entry left to refine and keeps
    # the share the first one computed.
    text_char_limit = plan.text_char_limit
    if model.text_only:
        readable = []
        for e in native:
            facts = files[e.index]
            if e.file_class == CLASS_IMAGE:
                entries[e.index] = replace(e, mode=MODE_DROPPED, reason=REASON_TEXT_ONLY_IMAGE, tokens=0)
            elif document_format_for(facts.mime_type, facts.filename) is None:
                entries[e.index] = replace(e, mode=MODE_DROPPED, reason=REASON_TEXT_ONLY_UNREADABLE, tokens=0)
            else:
                readable.append(e)
        if readable:
            text_char_limit = max(TEXT_ONLY_DOCUMENT_MIN_CHARS, profile.text_only_max_chars // len(readable))
            for e in readable:
                entries[e.index] = replace(
                    e,
                    mode=MODE_TEXT,
                    reason=REASON_TEXT_ONLY_MODEL,
                    tokens=min(e.tokens, text_char_limit // CHARS_PER_TOKEN),
                )
    elif document_budget(model, profile) is not None:
        entries = _token_ladder(entries, files, model, profile)
    elif not model.converse or profile.documents_as_text:
        # Off Converse the projection always renders (agent_factory installs it
        # with as_text=True for every other provider); on Converse only with
        # CONVERSE_DOCUMENTS_AS_TEXT_ENABLED.
        reason = REASON_DOCUMENTS_AS_TEXT if model.converse else REASON_PROVIDER_NEEDS_TEXT
        for e in native:
            # A forced ``native`` file stays a block on Converse; elsewhere the
            # formatter cannot read it, so it is text whatever the row says.
            if model.converse and e.reason == REASON_FORCED:
                continue
            if e.file_class != CLASS_IMAGE and _extension(files[e.index].filename) in DOCUMENTS_AS_TEXT_EXTENSIONS:
                entries[e.index] = replace(e, mode=MODE_TEXT, reason=reason)
    return replace(plan, entries=tuple(entries), text_char_limit=text_char_limit)


def _token_ladder(
    entries: List[PlanEntry],
    files: Sequence[FileFacts],
    model: ModelFacts,
    profile: LimitsProfile,
) -> List[PlanEntry]:
    """The §4.3 ladder over the plan's ``native`` entries, against the turn's budget.

    In attachment order; each file takes the first mode it qualifies for
    against what is left. A forced entry keeps its mode and is not counted (an
    eval arm measures the mode, not the budget). Only ``native`` entries are
    judged, so a second call finds nothing left to change.
    """
    remaining = document_budget(model, profile) or 0
    page_limit = pdf_page_limit(model)
    for e in list(entries):
        if e.mode != MODE_NATIVE or e.reason == REASON_FORCED:
            continue
        facts = files[e.index]
        cls = e.file_class

        def take(mode: str, reason: str, cost: int) -> None:
            nonlocal remaining
            remaining -= cost
            entries[e.index] = replace(e, mode=mode, reason=reason, tokens=cost)

        def over() -> None:
            if facts.has_row:
                entries[e.index] = replace(e, mode=MODE_ON_DEMAND, reason=REASON_OVER_DOCUMENT_BUDGET, tokens=0)
            else:
                # Nothing to read it from later: send it as the rule says, and say why.
                mode = MODE_TEXT if cls == CLASS_DOCUMENT and _renders_as_text(facts) else MODE_NATIVE
                take(mode, REASON_NO_ROW, document_cost(facts, cls, mode, model))

        if cls == CLASS_IMAGE:
            take(MODE_NATIVE, REASON_INLINE, document_cost(facts, cls, MODE_NATIVE, model))
        elif cls in (CLASS_PDF, CLASS_DOCUMENT) and facts.text_tokens is None:
            # No facts, so no honest cost: the bytes are ~9x low for a .docx
            # and blind to a PDF's pages. Today's rules decide this turn (§4.1);
            # the reason makes the race countable on the cost rows.
            mode = MODE_TEXT if cls == CLASS_DOCUMENT and _renders_as_text(facts) else MODE_NATIVE
            take(mode, REASON_FACTS_PENDING, document_cost(facts, cls, mode, model))
        elif cls == CLASS_PDF:
            pages = facts.page_count
            within_pages = not pages or int(pages) <= page_limit
            native_cost = document_cost(facts, cls, MODE_NATIVE, model)
            text_cost = document_cost(facts, cls, MODE_TEXT, model)
            if within_pages and native_cost <= remaining:
                take(MODE_NATIVE, REASON_WITHIN_BUDGET, native_cost)
            elif not within_pages and facts.has_text_layer and facts.text_tokens is not None and text_cost <= remaining:
                take(MODE_TEXT, REASON_PDF_PAGE_LIMIT, text_cost)
            else:
                over()
        elif cls == CLASS_DOCUMENT and _renders_as_text(facts):
            cost = document_cost(facts, cls, MODE_TEXT, model)
            if cost <= remaining:
                take(MODE_TEXT, REASON_WITHIN_BUDGET, cost)
            else:
                over()
        # Anything else (a format the projection cannot render) keeps the legacy plan.
    return entries


def _renders_as_text(facts: FileFacts) -> bool:
    return _extension(facts.filename) in DOCUMENTS_AS_TEXT_EXTENSIONS


__all__ = [
    "CLASS_DOCUMENT",
    "CLASS_IMAGE",
    "CLASS_OTHER",
    "CLASS_PDF",
    "CLASS_PRESENTATION",
    "CLASS_TABULAR",
    "DELIVERY_OVERRIDE_VALUES",
    "FORCEABLE_MODES",
    "FORCED_TEXT_MAX_CHARS",
    "MODES",
    "MODE_DROPPED",
    "MODE_NATIVE",
    "MODE_ON_DEMAND",
    "MODE_SEARCH",
    "MODE_TEXT",
    "MODE_TOOL",
    "PROFILE_LEGACY",
    "PROFILE_TOKEN",
    "PDF_PAGE_IMAGE_TOKENS",
    "REASON_FACTS_PENDING",
    "REASON_NO_ROW",
    "REASON_OVER_DOCUMENT_BUDGET",
    "REASON_PDF_PAGE_LIMIT",
    "REASON_WITHIN_BUDGET",
    "AttachmentPlan",
    "ConversationFacts",
    "CountCap",
    "FileFacts",
    "LimitsProfile",
    "ModelFacts",
    "PlanEntry",
    "cap_message_attachments",
    "current_limits_profile",
    "document_budget",
    "document_cost",
    "documents_as_text",
    "pdf_page_limit",
    "token_profile",
    "tokenizer_factor",
    "delivers_by_reference",
    "file_class",
    "legacy_profile",
    "plan_attachments",
    "reads_from_original",
    "refine_for_model",
]
