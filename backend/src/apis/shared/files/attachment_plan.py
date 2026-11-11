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

The ``token`` profile can also force ``search``: the file is delivered as a
forced ``on_demand`` one, it is indexed into the attachments KB whatever its size
(:func:`indexes_for_search`), and the conversation's guidance prefers
``document_read``'s ``query`` (:func:`plan_conversation`). The ``legacy`` profile
plans a forced ``search`` as ``on_demand``.

**Overflow (phase 5, §4.3's ``search`` row).** Under the ``token`` profile a
document the planner would send ``on_demand`` on its own, on the smallest
document budget any curated model gets, is indexed into the attachments KB at
upload (:func:`indexes_for_search`, read by ``attachment_index``), not only a
reference-class one. A file that is ``on_demand`` and whose row says it is
searchable is planned ``search``; one not indexed yet stays ``on_demand`` and
nothing waits for it. :func:`plan_conversation` then looks at the
conversation's whole on-demand set (earlier attachments, from the session's file
rows, plus this turn's) and decides whether the turn's guidance steers the
model to search across them first. The steering is a line in the turn's user
message, never the system prompt or a tool spec, so the cacheable prefix does
not move.

**What is recorded.** :meth:`AttachmentPlan.to_row` is written on the turn's
``C#`` cost row as ``attachmentPlan`` after the stream: modes, classes, reason
codes, token estimates and upload ids. Never a filename.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from apis.shared.files.document_read import document_format_for, is_document_class
from apis.shared.files.tokenizer_costs import (  # noqa: F401 - re-exported
    DEFAULT_TOKENIZER_FACTOR,
    LEGACY_TOKENIZER_FACTOR,
    PDF_PAGE_IMAGE_TOKENS,
    native_pdf_cost,
    text_cost,
    tokenizer_factor,
)
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
#: ``on_demand`` plus ``document_read``'s ``query``: the file is indexed in the
#: attachments KB (phase 5; never chosen by ``legacy``).
MODE_SEARCH = "search"
#: An inventory line; the spreadsheet or deck tools read the content.
MODE_TOOL = "tool"
#: Not delivered at all, named in the attachment guidance. ``legacy`` only:
#: the ``token`` profile never drops a file (§4.3).
MODE_DROPPED = "dropped"

MODES = (MODE_NATIVE, MODE_TEXT, MODE_ON_DEMAND, MODE_SEARCH, MODE_TOOL, MODE_DROPPED)

#: The modes ``document_read`` reads a file in: its content is not in the message.
ON_DEMAND_MODES = (MODE_ON_DEMAND, MODE_SEARCH)

#: Modes a file row's ``delivery_override`` may force.
FORCEABLE_MODES = frozenset({MODE_NATIVE, MODE_TEXT, MODE_ON_DEMAND, MODE_SEARCH})
#: Forced modes delivered by reference (``document_read`` reads the original).
_FORCED_BY_REFERENCE = frozenset({MODE_ON_DEMAND, MODE_SEARCH})

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
#: ``token``: a native PDF would take the turn's native pages past the model's
#: per-request page limit, so it goes ``text`` (or ``on_demand``) instead.
REASON_REQUEST_PAGE_LIMIT = "request_page_limit"

# Conversation-level steering (phase 5, :class:`ConversationPlan`). Stable strings.
#: The on-demand set reached ``SEARCH_STEER_MIN_DOCUMENTS`` and some of it is indexed.
STEER_SET_SIZE = "set_size"
#: Under the threshold: the per-file guidance (``pattern`` first) stands.
STEER_BELOW_THRESHOLD = "below_threshold"
#: Nothing in the set is indexed yet: search would answer "not searchable".
STEER_NONE_SEARCHABLE = "none_searchable"
#: An eval arm forced the set's steering (a ``search`` or ``on_demand`` row).
STEER_FORCED = "forced"

#: An image's stored copies (``image_downscale``).
VARIANT_HIGH = "high"
VARIANT_COMPACT = "compact"

PROFILE_LEGACY = "legacy"
#: Phase 3 (§9 / §9.1): selected by ``ATTACHMENT_PLAN_ENABLED``.
PROFILE_TOKEN = "token"

# ---------------------------------------------------------------------------
# Per-model document costs (§2.1, §5.2)
# ---------------------------------------------------------------------------

# The page-image cost and tokenizer factors live in ``tokenizer_costs`` so
# compaction's estimate reads the same numbers (§2.1); re-exported here.

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
    #: Images are downscaled at upload to the largest size any curated model
    #: reads (``image_downscale``), and that copy is what a turn sends (§4.3).
    downscale_images: bool = False
    #: Per-file upload ceiling for an image, before downscaling. ``None`` keeps
    #: the general upload limit (the ``legacy`` profile).
    image_upload_max_bytes: Optional[int] = None
    #: Images one message may carry; ``None`` is no separate image cap.
    images_per_message: Optional[int] = None
    #: A PDF over this many pages is refused at admission, and its count is
    #: recorded on the row. ``None``: no page rule (the ``legacy`` profile).
    pdf_max_pages: Optional[int] = None
    #: Raw bytes of native attachments one request may carry: the probed 32 MB
    #: request payload (§2.1) is base64, so 24 MB raw, less headroom for the
    #: history, system prompt and tools. ``0`` disables.
    request_attachment_max_bytes: int = 0


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
    #: An image's ``modelImage`` (``image_downscale``): the high copy's pixels
    #: (``size_bytes`` is its bytes) and the compact copy's bytes and pixels.
    #: Read by the ``token`` profile only.
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    compact_bytes: Optional[int] = None
    compact_width: Optional[int] = None
    compact_height: Optional[int] = None
    #: The row says the attachments KB can search it (``attachment_index``'s
    #: ``searchable``). Read by the ``token`` profile only: an ``on_demand``
    #: file that is searchable is planned ``search`` (phase 5).
    searchable: bool = False


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
    #: The conversation's earlier document attachments, from its file rows
    #: (phase 5): what :func:`plan_conversation` judges the on-demand set from.
    earlier: Tuple["FileFacts", ...] = ()


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
    #: Which stored copy of an image is sent: ``high`` or ``compact``
    #: (``image_downscale``). ``None`` for every other file, and for an image
    #: the ``legacy`` profile plans (sent as uploaded).
    variant: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "mode": self.mode,
            "class": self.file_class,
            "tokens": int(self.tokens),
            "reason": self.reason,
        }
        if self.upload_id:
            row["uploadId"] = self.upload_id
        if self.variant:
            row["variant"] = self.variant
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
    #: The conversation's on-demand set and whether the turn steers it to
    #: search (phase 5, :func:`plan_conversation`); ``None`` when it has none.
    conversation: Optional["ConversationPlan"] = None

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
        """``attachmentPlan`` for the turn's ``C#`` row, or ``None`` when nothing
        was attached and the conversation has no on-demand set."""
        if not self.entries and not self.dropped_over_count and self.conversation is None:
            return None
        row: Dict[str, Any] = {
            "profile": self.profile,
            "files": [e.to_row() for e in self.entries],
        }
        if self.dropped_over_count:
            row["droppedOverCount"] = int(self.dropped_over_count)
        if self.conversation is not None:
            row["conversation"] = self.conversation.to_row()
        return row


@dataclass(frozen=True)
class ConversationPlan:
    """The conversation's on-demand set and the turn's steering (phase 5).

    Counts and numbers only. ``documents`` is every document in the set: earlier
    attachments the turn's model could not hold on their own, reference-class
    ones, and this turn's ``on_demand`` and ``search`` files. ``searchable`` is
    how many of them the attachments KB can search now. ``tokens`` is their text
    at the model's tokenizer factor, what holding them all would cost.
    """

    documents: int
    searchable: int
    tokens: int
    steer: bool
    reason: str

    def to_row(self) -> Dict[str, Any]:
        return {
            "documents": int(self.documents),
            "searchable": int(self.searchable),
            "tokens": int(self.tokens),
            "steer": MODE_SEARCH if self.steer else MODE_ON_DEMAND,
            "reason": self.reason,
        }


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
        downscale_images=True,
        image_upload_max_bytes=TOKEN_IMAGE_UPLOAD_MAX_BYTES,
        max_files_per_message=TOKEN_FILES_PER_MESSAGE,
        images_per_message=TOKEN_IMAGES_PER_MESSAGE,
        pdf_max_pages=TOKEN_PDF_MAX_PAGES,
        request_attachment_max_bytes=TOKEN_REQUEST_ATTACHMENT_MAX_BYTES,
    )


def current_limits_profile() -> LimitsProfile:
    """The profile this turn plans with: ``token`` with ``ATTACHMENT_PLAN_ENABLED``,
    else ``legacy``. The flag is read here and nowhere else (§9.1 rule 1)."""
    from apis.shared.feature_flags import attachment_plan_enabled

    return token_profile() if attachment_plan_enabled() else legacy_profile()


def persist_document_handles() -> bool:
    """Whether the user message written to AgentCore Memory carries a handle
    (upload id, name, size, class) in place of each document's bytes (§6.2):
    the ``token`` profile's rule. The live message keeps the bytes; restore
    reads a handle exactly as it reads bytes (``attachment_handle``)."""
    return current_limits_profile().name == PROFILE_TOKEN


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
#: An image the ``token`` profile accepts before downscaling (§4.4). The stored
#: model copy is always under ``image_downscale.MODEL_IMAGE_MAX_BYTES``.
TOKEN_IMAGE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
#: The Claude apps' counts and page limit (§2, §4.4).
TOKEN_FILES_PER_MESSAGE = 20
TOKEN_IMAGES_PER_MESSAGE = 20
TOKEN_PDF_MAX_PAGES = 1_000
#: 16 MB raw of native blocks per request: 21.3 MB on the wire, leaving ~10 MB
#: of the probed 32 MB payload for the history, system prompt and tools.
TOKEN_REQUEST_ATTACHMENT_MAX_BYTES = 16 * 1024 * 1024


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
    #: An image, before downscaling; ``None`` means ``general_max_bytes``.
    image_max_bytes: Optional[int] = None


def upload_limits(profile: Optional[LimitsProfile] = None) -> UploadLimits:
    """The per-class upload ceilings, with the two flags that widen admission
    and the active profile's image ceiling."""
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
        image_max_bytes=(profile or current_limits_profile()).image_upload_max_bytes,
    )


def upload_size_limit(filename: str, mime_type: str, limits: UploadLimits) -> int:
    """The upload ceiling for one file: decks, then spreadsheets, then large documents."""
    if is_presentation_file(filename, mime_type):
        return limits.presentation_max_bytes
    if is_tabular_file(filename, mime_type):
        return limits.spreadsheet_max_bytes
    if limits.large_document_max_bytes is not None and is_document_class(mime_type, filename):
        return limits.large_document_max_bytes
    if limits.image_max_bytes is not None and (mime_type or "").lower().startswith("image/"):
        return limits.image_max_bytes
    return limits.general_max_bytes


def upload_download_ceiling(limits: UploadLimits) -> int:
    """The widest class ceiling: what a server-side import may download before sizing."""
    caps = [limits.general_max_bytes, limits.presentation_max_bytes, limits.spreadsheet_max_bytes]
    for extra in (limits.large_document_max_bytes, limits.image_max_bytes):
        if extra is not None:
            caps.append(extra)
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
    uploads = uploads or upload_limits(profile)
    count = profile.max_files_per_message if profile.turn_guard else profile.resolver_backstop_without_guard
    budget = profile.inline_total_max_bytes if profile.turn_guard else 0
    return {
        "profile": profile.name,
        "filesPerMessage": count if count > 0 else None,
        "imagesPerMessage": profile.images_per_message,
        "pdfMaxPages": profile.pdf_max_pages,
        "inlineDocumentMaxBytes": profile.inline_document_max_bytes,
        "inlineBudgetBytes": budget if budget > 0 else None,
        "maxBytes": {
            "document": uploads.general_max_bytes,
            "image": uploads.image_max_bytes or uploads.general_max_bytes,
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
    if forced in _FORCED_BY_REFERENCE:
        return document_format_for(meta.mime_type, meta.filename) is not None
    return is_reference_document(meta)


def reads_from_original(meta: Any) -> bool:
    """Whether a by-reference row is read from its original rather than prepared sidecars.

    True for a row forced ``on_demand`` (or ``search``) that is under the
    reference size: the prepare Lambda never prepares it, so it renders like an
    over-budget reference (its upload digest, or a ``ready`` stub) and
    ``document_read`` reads the original. Every user row answers ``False``.
    """
    return getattr(meta, "delivery_override", None) in _FORCED_BY_REFERENCE and not is_reference_document(meta)


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
        if cls == CLASS_PDF and mode == MODE_NATIVE:
            return native_pdf_cost(facts.text_tokens, facts.page_count, facts.native_tokens or 0, factor)
        return text_cost(facts.text_tokens, factor)
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
            modes[i] = _on_demand_mode(facts, profile)
            reasons[i] = REASON_FORCED if forced in _FORCED_BY_REFERENCE else REASON_REFERENCE_CLASS
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
                modes[i], reasons[i] = _on_demand_mode(facts, profile), REASON_OVER_BUDGET_REFERENCE
                continue
            modes[i], reasons[i] = MODE_DROPPED, REASON_OVERSIZED
            continue
        modes[i], reasons[i] = MODE_NATIVE, REASON_INLINE
        candidates.append(i)

    # The combined budget: first-fit in attachment order. Images count — they
    # are in the same persisted message. A forced file always stays.
    requested = sum(max(0, files[i].size_bytes) for i in candidates)
    variants: Dict[int, str] = {}
    budget = _request_byte_budget(profile)
    if profile.downscale_images and candidates and budget > 0:
        # The ``token`` profile: images first, since an image has no other
        # mode, then documents, which fall back to on_demand (§4.4.1).
        _images_first_budget(files, classes, candidates, modes, reasons, variants, budget)
    elif profile.turn_guard and candidates and profile.inline_total_max_bytes > 0:
        running = 0
        for i in candidates:
            size = max(0, files[i].size_bytes)
            if reasons[i] != REASON_FORCED and running + size > profile.inline_total_max_bytes:
                document_reads_it = files[i].has_row and classes[i] in (CLASS_PDF, CLASS_DOCUMENT)
                if profile.over_budget_by_reference and document_reads_it:
                    modes[i], reasons[i] = _on_demand_mode(facts, profile), REASON_OVER_BUDGET_REFERENCE
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
            variant=variants.get(i) if modes[i] == MODE_NATIVE else None,
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


def _on_demand_mode(facts: FileFacts, profile: LimitsProfile) -> str:
    """``search`` for an ``on_demand`` file the KB can search, on the ``token``
    profile; ``on_demand`` otherwise. A file forced ``on_demand`` stays it."""
    if profile.document_budget_share > 0 and facts.searchable and _forced(facts) != MODE_ON_DEMAND:
        return MODE_SEARCH
    return MODE_ON_DEMAND


def _request_byte_budget(profile: LimitsProfile) -> int:
    """Raw bytes of native blocks one turn may carry: the smaller of the persisted
    message's budget and the request's, ``0`` when neither applies."""
    caps = []
    if profile.turn_guard and profile.inline_total_max_bytes > 0:
        caps.append(profile.inline_total_max_bytes)
    if profile.request_attachment_max_bytes > 0:
        caps.append(profile.request_attachment_max_bytes)
    return min(caps) if caps else 0


def _has_model_copy(facts: FileFacts) -> bool:
    return facts.image_width is not None


def _images_first_budget(
    files: Sequence[FileFacts],
    classes: Sequence[str],
    candidates: Sequence[int],
    modes: List[Optional[str]],
    reasons: List[Optional[str]],
    variants: Dict[int, str],
    budget: int,
) -> None:
    """The ``token`` profile's byte pass (§4.4.1).

    Images are placed first: every one at its ``high`` copy when they all fit,
    else every one at its ``compact`` copy (twenty compact copies are under
    10 MB, so an uploaded image always fits). Then documents first-fit in
    attachment order; one that does not fit goes ``on_demand`` when it has a
    row to read it from, and is dropped only when it has nothing. A forced file
    always stays.
    """
    images = [i for i in candidates if classes[i] == CLASS_IMAGE]
    others = [i for i in candidates if classes[i] != CLASS_IMAGE]
    high_total = sum(max(0, files[i].size_bytes) for i in images)
    use_compact = high_total > budget
    running = 0
    for i in images:
        facts = files[i]
        size = max(0, facts.size_bytes)
        variant = VARIANT_HIGH if _has_model_copy(facts) else None
        if use_compact and facts.compact_bytes is not None:
            size, variant = max(0, int(facts.compact_bytes)), VARIANT_COMPACT
        if running + size > budget:
            modes[i], reasons[i] = MODE_DROPPED, REASON_OVER_BUDGET
            continue
        running += size
        if variant:
            variants[i] = variant
    for i in others:
        size = max(0, files[i].size_bytes)
        if reasons[i] != REASON_FORCED and running + size > budget:
            if files[i].has_row and classes[i] in (CLASS_PDF, CLASS_DOCUMENT):
                modes[i], reasons[i] = MODE_ON_DEMAND, REASON_OVER_BUDGET_REFERENCE
                if files[i].searchable:
                    modes[i] = MODE_SEARCH
            else:
                modes[i], reasons[i] = MODE_DROPPED, REASON_OVER_BUDGET
            continue
        running += size


#: Claude models that read images at the standard tier (1,568 px, 1,568 visual
#: tokens): every Claude model before 4.7 (Anthropic vision docs). They are
#: sent the ``compact`` copy, which is what they would downscale to anyway.
_STANDARD_IMAGE_TIER_MODELS = (
    "claude-3",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-1",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-sonnet-4-2",
    "claude-opus-4-2",
)


def reads_standard_image_tier(model: ModelFacts) -> bool:
    """Whether this model reads at most Claude's standard image tier."""
    mid = (model.model_id or "").lower()
    return any(key in mid for key in _STANDARD_IMAGE_TIER_MODELS)


def image_cost(facts: FileFacts, variant: Optional[str], model: ModelFacts) -> int:
    """An image's visual tokens on this model, from the copy sent (§4.3.1).

    One token per 28 px patch of the sent copy, capped at what the model reads
    (4,784 at Claude's high tier, 1,568 at the standard tier). Without a copy's
    size, the cap itself.
    """
    from apis.shared.files.image_downscale import COMPACT_VISUAL_TOKENS, MAX_VISUAL_TOKENS, visual_tokens

    cap = COMPACT_VISUAL_TOKENS if reads_standard_image_tier(model) else MAX_VISUAL_TOKENS
    if variant == VARIANT_COMPACT and facts.compact_width and facts.compact_height:
        w, h = facts.compact_width, facts.compact_height
    elif facts.image_width and facts.image_height:
        w, h = facts.image_width, facts.image_height
    else:
        return cap
    return min(cap, visual_tokens(int(w), int(h)))


#: A PDF whose facts are not ready is costed as if each page carried this
#: much text: the measured prose page (32-page fixture, 30,779 ``tokens.text``)
#: rounded up, so a pending PDF is never judged cheaper than it is.
PENDING_PDF_TEXT_TOKENS_PER_PAGE = 1_000
#: Without a page count either, pages are estimated from the bytes at this
#: many bytes a page. Low on purpose (a text page is 3-30 KB), so the estimate
#: errs towards more pages and ``on_demand``.
PENDING_PDF_BYTES_PER_PAGE = 25_000


def pending_pdf_pages(facts: FileFacts) -> int:
    """A PDF's pages when its facts are pending: the admission count, else the bytes."""
    if facts.page_count:
        return int(facts.page_count)
    return max(1, math.ceil(max(0, int(facts.size_bytes)) / PENDING_PDF_BYTES_PER_PAGE))


def pending_pdf_cost(facts: FileFacts, model: ModelFacts) -> int:
    """A conservative native cost for a PDF without facts (§4.4.1)."""
    pages = pending_pdf_pages(facts)
    per_page = PDF_PAGE_IMAGE_TOKENS + PENDING_PDF_TEXT_TOKENS_PER_PAGE * tokenizer_factor(model.model_id)
    return int(math.ceil(pages * per_page))


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
    # Native PDF pages this turn: the per-request page limit is the model's
    # (§2.1), so a second native PDF must not take the turn past it.
    native_pages = 0
    standard_tier = reads_standard_image_tier(model)
    for e in list(entries):
        if e.mode != MODE_NATIVE or e.reason == REASON_FORCED:
            continue
        facts = files[e.index]
        cls = e.file_class

        def take(mode: str, reason: str, cost: int, variant: Optional[str] = None) -> None:
            nonlocal remaining
            remaining -= cost
            entries[e.index] = replace(e, mode=mode, reason=reason, tokens=cost, variant=variant)

        def over(reason: str = REASON_OVER_DOCUMENT_BUDGET) -> None:
            if facts.has_row:
                entries[e.index] = replace(e, mode=_on_demand_mode(facts, profile), reason=reason, tokens=0)
            else:
                # Nothing to read it from later: send it as the rule says, and say why.
                mode = MODE_TEXT if cls == CLASS_DOCUMENT and _renders_as_text(facts) else MODE_NATIVE
                take(mode, REASON_NO_ROW, document_cost(facts, cls, mode, model))

        if cls == CLASS_IMAGE:
            variant = e.variant
            if variant == VARIANT_HIGH and standard_tier and facts.compact_bytes is not None:
                # The model reads no more than the compact copy: fewer bytes, same tokens.
                variant = VARIANT_COMPACT
            take(MODE_NATIVE, REASON_INLINE, image_cost(facts, variant, model), variant)
        elif cls == CLASS_PDF and facts.text_tokens is None:
            # No facts, so no measured cost. Today's rule sent a 159-page PDF
            # whole; judge it instead by its admission page count (else its
            # bytes), costed per page at the measured page image plus a full
            # prose page of text, which over-states it (§4.4.1).
            pages = pending_pdf_pages(facts)
            cost = pending_pdf_cost(facts, model)
            if pages <= page_limit and native_pages + pages <= page_limit and cost <= remaining:
                native_pages += pages
                take(MODE_NATIVE, REASON_FACTS_PENDING, cost)
            elif facts.has_row:
                entries[e.index] = replace(
                    e, mode=_on_demand_mode(facts, profile), reason=REASON_FACTS_PENDING, tokens=0
                )
            else:
                native_pages += pages
                take(MODE_NATIVE, REASON_NO_ROW, cost)
        elif cls == CLASS_DOCUMENT and facts.text_tokens is None:
            # No facts, so no honest cost: the bytes are ~9x low for a .docx.
            # Today's rules decide this turn (§4.1); the reason makes the race
            # countable on the cost rows.
            mode = MODE_TEXT if _renders_as_text(facts) else MODE_NATIVE
            take(mode, REASON_FACTS_PENDING, document_cost(facts, cls, mode, model))
        elif cls == CLASS_PDF:
            pages = facts.page_count
            within_pages = not pages or int(pages) <= page_limit
            within_request = not pages or native_pages + int(pages) <= page_limit
            native_cost = document_cost(facts, cls, MODE_NATIVE, model)
            text_cost = document_cost(facts, cls, MODE_TEXT, model)
            text_ok = facts.has_text_layer and facts.text_tokens is not None and text_cost <= remaining
            if within_pages and within_request and native_cost <= remaining:
                native_pages += int(pages or 0)
                take(MODE_NATIVE, REASON_WITHIN_BUDGET, native_cost)
            elif not within_pages and text_ok:
                take(MODE_TEXT, REASON_PDF_PAGE_LIMIT, text_cost)
            elif within_pages and not within_request and text_ok:
                take(MODE_TEXT, REASON_REQUEST_PAGE_LIMIT, text_cost)
            elif within_pages and not within_request:
                over(REASON_REQUEST_PAGE_LIMIT)
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


# ---------------------------------------------------------------------------
# Overflow: indexing and the conversation's on-demand set (phase 5)
# ---------------------------------------------------------------------------

#: The model a document is judged against at upload, when the turn's model is
#: not known: the smallest ``document_budget`` any curated model gets (Haiku
#: 5.5's 25k, half its 50k chat ceiling) at the default tokenizer factor (1.40)
#: and the small-window page limit. A document this model could not hold on its
#: own is one the planner sends ``on_demand`` on some curated model, so it is
#: indexed. No model id, so the factor and page limit are the defaults, which
#: err towards indexing.
SEARCH_INDEX_REFERENCE_MODEL = ModelFacts(chat_ceiling=50_000)

#: Documents in a conversation's on-demand set from which the turn's guidance
#: steers the model to ``document_read``'s ``query`` first, when some of them are
#: indexed. Set by the phase 5 eval (``docs/testing/attachment-delivery-arms-2026-11.md``).
SEARCH_STEER_MIN_DOCUMENTS = 3


def _row_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def facts_from_row(meta: Any, *, searchable: bool = False) -> FileFacts:
    """The planner's facts for a file row read by someone else. No read, no parse.

    The fields a document's delivery depends on: its size, class, whether it is
    delivered by reference, its fixture override, its pages and its §4.1 facts
    when current. ``searchable`` is the caller's (``attachment_index.search_status``).
    """
    from apis.shared.files.file_facts import facts_current

    out: Dict[str, Any] = {}
    preparation = getattr(meta, "preparation", None)
    pages = _row_int(preparation.get("pageCount")) if isinstance(preparation, dict) else None
    pages = pages or _row_int(getattr(meta, "page_count", None))
    if facts_current(meta):
        facts = meta.facts
        tokens = facts.get("tokens") if isinstance(facts.get("tokens"), dict) else {}
        out["text_tokens"] = _row_int(tokens.get("text"))
        out["native_tokens"] = _row_int(tokens.get("native"))
        layer = facts.get("hasTextLayer")
        out["has_text_layer"] = layer if isinstance(layer, bool) else None
        pages = _row_int(facts.get("pages")) or pages
    forced = getattr(meta, "delivery_override", None)
    return FileFacts(
        filename=meta.filename,
        mime_type=meta.mime_type,
        size_bytes=int(getattr(meta, "size_bytes", 0) or 0),
        upload_id=getattr(meta, "upload_id", None),
        by_reference=delivers_by_reference(meta),
        has_row=True,
        forced_mode=forced if forced in FORCEABLE_MODES else None,
        page_count=pages or None,
        searchable=bool(searchable),
        **out,
    )


def indexes_for_search(facts: FileFacts, profile: LimitsProfile) -> bool:
    """Whether a document is indexed into the attachments KB under this profile.

    Only the ``token`` profile widens indexing (§10: eligibility "stays as it is
    until phase 5"): a document whose row facts say the planner would send it
    ``on_demand`` on its own on :data:`SEARCH_INDEX_REFERENCE_MODEL`, or one an
    eval forced ``search``. A reference-class document is indexed under either
    profile by ``attachment_index``'s own rule. Without facts, nothing is
    claimed: the facts run that writes them is the one that ingests.
    """
    if profile.document_budget_share <= 0:
        return False
    if file_class(facts.filename, facts.mime_type) not in (CLASS_PDF, CLASS_DOCUMENT):
        return False
    if facts.forced_mode == MODE_SEARCH:
        return True
    if facts.forced_mode in FORCEABLE_MODES or facts.text_tokens is None:
        return False
    alone = replace(facts, by_reference=False, has_row=True, forced_mode=None, searchable=False)
    plan = plan_attachments([alone], SEARCH_INDEX_REFERENCE_MODEL, profile=profile)
    return plan.entries[0].mode in ON_DEMAND_MODES


def _in_on_demand_set(facts: FileFacts, model: ModelFacts, profile: LimitsProfile) -> bool:
    """Whether an earlier attachment is read on demand on this turn's model:
    reference-class or forced by reference, or a document the model could not
    hold on its own. One the turn that attached it sent ``on_demand`` only
    because the budget was shared is counted as held; the estimate errs low."""
    if file_class(facts.filename, facts.mime_type) not in (CLASS_PDF, CLASS_DOCUMENT):
        return False
    if facts.by_reference or _forced(facts) in _FORCED_BY_REFERENCE:
        return True
    if _forced(facts) is not None:
        return False
    alone = replace(facts, has_row=True)
    plan = plan_attachments([alone], model, profile=profile)
    return plan.entries[0].mode in ON_DEMAND_MODES


def plan_conversation(
    plan: Optional[AttachmentPlan],
    files: Sequence[FileFacts],
    held: ConversationFacts,
    model: ModelFacts,
    *,
    profile: LimitsProfile,
) -> Optional[ConversationPlan]:
    """The conversation's on-demand set, and whether this turn steers it to search.

    ``files`` and ``plan`` are this turn's attachments; ``held.earlier`` the
    conversation's other document rows (a file attached this turn is counted
    once, from the plan). ``None`` on the ``legacy`` profile, without a token
    budget, and when the set is empty, so a conversation with no documents
    records nothing. The steer is on when an eval forced it (a ``search`` row
    in the set turns it on, an ``on_demand`` one off, ``search`` winning), else
    when the set has at least :data:`SEARCH_STEER_MIN_DOCUMENTS` documents and
    any of them is searchable. Pure: the rows were read by the caller.
    """
    if profile.document_budget_share <= 0 or document_budget(model, profile) is None or model.text_only:
        return None
    this_turn = {f.upload_id for f in files if f.upload_id}
    members: List[FileFacts] = []
    if plan is not None:
        members.extend(files[e.index] for e in plan.entries if e.mode in ON_DEMAND_MODES)
    for facts in held.earlier:
        if facts.upload_id and facts.upload_id in this_turn:
            continue
        if _in_on_demand_set(facts, model, profile):
            members.append(facts)
    if not members:
        return None
    factor = tokenizer_factor(model.model_id)
    tokens = sum(text_cost(m.text_tokens, factor) for m in members if m.text_tokens)
    searchable = sum(1 for m in members if m.searchable)
    forced = {_forced(m) for m in members}
    if searchable == 0:
        steer, reason = False, STEER_NONE_SEARCHABLE
    elif MODE_SEARCH in forced:
        steer, reason = True, STEER_FORCED
    elif MODE_ON_DEMAND in forced:
        steer, reason = False, STEER_FORCED
    elif len(members) >= SEARCH_STEER_MIN_DOCUMENTS:
        steer, reason = True, STEER_SET_SIZE
    else:
        steer, reason = False, STEER_BELOW_THRESHOLD
    return ConversationPlan(
        documents=len(members), searchable=searchable, tokens=tokens, steer=steer, reason=reason
    )


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
    "ON_DEMAND_MODES",
    "SEARCH_INDEX_REFERENCE_MODEL",
    "SEARCH_STEER_MIN_DOCUMENTS",
    "STEER_BELOW_THRESHOLD",
    "STEER_FORCED",
    "STEER_NONE_SEARCHABLE",
    "STEER_SET_SIZE",
    "ConversationPlan",
    "facts_from_row",
    "indexes_for_search",
    "plan_conversation",
    "MODE_SEARCH",
    "MODE_TEXT",
    "MODE_TOOL",
    "PROFILE_LEGACY",
    "PROFILE_TOKEN",
    "PDF_PAGE_IMAGE_TOKENS",
    "TOKEN_IMAGE_UPLOAD_MAX_BYTES",
    "REASON_FACTS_PENDING",
    "REASON_NO_ROW",
    "REASON_OVER_DOCUMENT_BUDGET",
    "REASON_PDF_PAGE_LIMIT",
    "REASON_REQUEST_PAGE_LIMIT",
    "VARIANT_COMPACT",
    "VARIANT_HIGH",
    "image_cost",
    "pending_pdf_cost",
    "reads_standard_image_tier",
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
    "persist_document_handles",
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
