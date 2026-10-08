"""Pydantic models for the Memory Space primitive (PR-1, data layer).

A **Memory Space** is a named, first-class, per-owner (optionally shared)
markdown "second brain" that agents read and maintain. See
``docs/specs/user-markdown-memory.md``.

Storage shape (dedicated ``memory-spaces`` DynamoDB table, space-keyed so a
shared space cannot live under one user's partition):

  - ``PK=SPACE#{space_id}  SK=META``            → :class:`MemorySpace`
  - ``PK=SPACE#{space_id}  SK=INDEX``           → :class:`MemoryIndex` (manifest)
  - ``PK=SPACE#{space_id}  SK=MEMBER#{email}``  → :class:`SpaceMember`
  - ``PK=SPACE#{space_id}  SK=FILEVER#{slug}#{n:06d}`` → :class:`FileVersion`

Two GSIs list a user's spaces (mirroring assistant sharing — owned and
shared-in are separate indexes unioned in code):

  - ``OwnerIndex``  ``GSI1PK=OWNER#{owner_id}   GSI1SK=SPACE#{space_id}``
  - ``MemberIndex`` ``GSI2PK=MEMBER#{email}     GSI2SK=SPACE#{space_id}``

Roles mirror assistant sharing: the owner is stored on the space row
(``owner_id``); share records only ever carry ``viewer``/``editor``.

The markdown bytes (``MEMORY.md`` and each entry) live in S3
(``apis/shared/memory/store.py``); these rows carry only pointers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Full permission set: owner is implicit (stored on the space row); shares
# only carry the two grantable roles.
Role = Literal["owner", "editor", "viewer"]
ShareRole = Literal["viewer", "editor"]

# Entry kinds. ``entity`` = a mutable record keyed by subject (a person, a
# project). ``episodic`` = an append-only dated record (a daily log, a brief).
# ``fact`` = a flat distilled fact (the catch-all).
EntryType = Literal["entity", "episodic", "fact"]

# How a space's entries are written. ``freeform`` entries are stored as the
# caller wrote them (every space before Shared Projects 2.3). ``canonical``
# entries are item lists with system-rendered frontmatter and anchors, checked
# on every save (``format.py``, ``validation.py``).
FileFormat = Literal["freeform", "canonical"]

# Who a space belongs to (Shared Projects §3.3). ``personal`` spaces are owned
# and shared by one user, as every space before 2.4 is. The two project scopes
# take their permissions from the project and are never shared directly:
# ``shared`` is the project's memory, ``personal_in_project`` is one member's
# memory within one project.
MemoryScope = Literal["personal", "shared", "personal_in_project"]
PROJECT_SCOPES = ("shared", "personal_in_project")

# Why a file version was written. ``baseline`` records the content an entry
# had before history existed, the first time such an entry is replaced.
FileVersionReason = Literal["edit", "save", "proposal", "maintenance", "restore", "baseline"]

TokenMethod = Literal["count", "estimate"]


class MemoryEntryRef(BaseModel):
    """Manifest entry for one markdown file in a Memory Space.

    The bytes live content-addressed in the ``memory-spaces`` S3 bucket
    (``spaces/{space_id}/{content_hash}``); this lightweight ref lives in the
    space's ``INDEX`` row. ``indexed`` copies a small allowlist of frontmatter
    fields (e.g. ``status``, ``commitments.due``) so relational/temporal
    queries ("who owes what") are a manifest lookup, not a full-corpus scan.

    camelCase aliases round-trip the future API response (FastAPI serializes
    by alias) while ``populate_by_name`` allows construction from snake_case.
    DynamoDB (de)serialization is handled explicitly in the repository.
    """

    model_config = ConfigDict(populate_by_name=True)

    slug: str = Field(..., description="Stable id within the space, e.g. 'jane-doe'")
    entry_type: EntryType = Field(
        "fact", alias="type", description="entity | episodic | fact"
    )
    description: str = Field(
        "", description="One-line summary shown in the index catalog"
    )
    content_hash: str = Field(
        ..., alias="contentHash", description="sha256 hex of the file bytes"
    )
    size: int = Field(..., description="Size of the file in bytes")
    s3_key: str = Field(
        ...,
        alias="s3Key",
        description="Object key in the memory-spaces bucket "
        "(spaces/{space_id}/{content_hash})",
    )
    updated: str = Field("", description="ISO-8601 timestamp of the last write")
    updated_by: str = Field(
        "", alias="updatedBy", description="user_id of the last writer (attribution)"
    )
    indexed: Dict[str, Any] = Field(
        default_factory=dict,
        description="Allowlisted frontmatter fields copied out for querying",
    )
    aliases: List[str] = Field(
        default_factory=list, description="Other names [[links]] may use (canonical files)"
    )
    tokens: Optional[int] = Field(
        None, description="Token size of the stored file, counted at save time"
    )
    tokens_method: Optional[TokenMethod] = Field(
        None, alias="tokensMethod", description="count (CountTokens) | estimate (chars/4)"
    )
    item_count: Optional[int] = Field(
        None, alias="itemCount", description="Number of items (canonical files)"
    )
    archived: bool = Field(False, description="Archived files still resolve as link targets")
    pinned: List[str] = Field(
        default_factory=list,
        description="Anchors of pinned items: a save may not drop them (Shared Projects 2.5a-2)",
    )
    version: int = Field(
        0,
        description="Number of FILEVER rows for this slug; 0 = written before history existed",
    )


class FileVersion(BaseModel):
    """A ``FILEVER#{slug}#{n:06d}`` row: one saved version of one file.

    Written after the manifest swap that commits the version, so ``version``
    numbers can never collide. The bytes stay in the content-addressed store
    for as long as a version row references them.
    """

    model_config = ConfigDict(populate_by_name=True)

    slug: str
    version: int
    content_hash: str = Field(..., alias="contentHash")
    size: int = 0
    tokens: Optional[int] = None
    tokens_method: Optional[TokenMethod] = Field(None, alias="tokensMethod")
    updated_by: str = Field("", alias="updatedBy")
    updated_at: str = Field("", alias="updatedAt")
    reason: FileVersionReason = "edit"
    proposal_id: Optional[str] = Field(None, alias="proposalId")
    run_id: Optional[str] = Field(None, alias="runId")


class ItemProvenance(BaseModel):
    """Where one item came from (Shared Projects 2.5a-2). People are emails, never user ids.

    ``added*`` is fixed when the item first appears; ``updated*`` moves on every
    save that changes its text. ``source_session_id`` is the task the item was
    saved or proposed from, when a task's assistant wrote it. ``proposal_id``,
    ``proposed_by`` and ``approved_by`` are set when it arrived through a
    proposal; ``restored_*`` when it came back from the archive.
    """

    model_config = ConfigDict(populate_by_name=True)

    added_by: str = Field("", alias="addedBy")
    added_at: str = Field("", alias="addedAt")
    updated_by: Optional[str] = Field(None, alias="updatedBy")
    updated_at: Optional[str] = Field(None, alias="updatedAt")
    source_session_id: Optional[str] = Field(None, alias="sourceSessionId")
    proposal_id: Optional[str] = Field(None, alias="proposalId")
    proposed_by: Optional[str] = Field(None, alias="proposedBy")
    approved_by: Optional[str] = Field(None, alias="approvedBy")
    restored_by: Optional[str] = Field(None, alias="restoredBy")
    restored_at: Optional[str] = Field(None, alias="restoredAt")


ArchiveReason = Literal["removed", "deleted", "merged", "superseded", "pruned"]


class ArchivedItem(BaseModel):
    """An ``ARCHIVE#{archivedAt}#{anchor}`` row: an item that left its file, restorable until ``ttl``.

    ``removed``: a save left it out. ``deleted``: its whole file was deleted.
    ``merged``, ``superseded`` and ``pruned``: an approved maintenance change
    took it out (Shared Projects 2.6); ``superseded_by`` names the item that
    took its place (the merged item, or the newer one). The text and
    provenance travel with it, so a restore needs nothing else.
    """

    model_config = ConfigDict(populate_by_name=True)

    archive_id: str = Field(..., alias="archiveId")
    slug: str
    anchor: str
    text: str
    reason: ArchiveReason
    archived_by: str = Field("", alias="archivedBy")
    archived_at: str = Field(..., alias="archivedAt")
    restorable_until: str = Field(..., alias="restorableUntil")
    provenance: Optional[ItemProvenance] = None
    superseded_by: Optional[str] = Field(None, alias="supersededBy")


# ---- maintenance (Shared Projects 2.6) ------------------------------------

# ``merge``: two or more items that say the same thing become one, written only
# from what they say. ``supersede``: a newer item replaces an older one it
# contradicts or updates. ``prune``: an item whose dates have all passed and
# that holds nothing lasting.
MaintenanceOpType = Literal["merge", "supersede", "prune"]
PruneReason = Literal["expired"]


class OpSource(BaseModel):
    """An item an op reads, with its text when the plan was made.

    An op applies only while every source still reads the same, so a file
    edited since the run keeps the edit and loses only the ops it touched.
    """

    model_config = ConfigDict(populate_by_name=True)

    anchor: str
    text: str


class MaintenanceOp(BaseModel):
    """One change a maintenance run proposes to one file.

    ``sources``: merge, every item merged (``keep`` takes the merged ``text``,
    the rest leave the file); supersede, ``[old, new]`` (old leaves, new is
    untouched); prune, the one item. ``why`` is the planner's reason, shown to
    the reviewer.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: MaintenanceOpType
    sources: List[OpSource]
    text: Optional[str] = None
    keep: Optional[str] = None
    reason: Optional[PruneReason] = None
    why: str = ""

    @property
    def anchors(self) -> List[str]:
        return [s.anchor for s in self.sources]

    @property
    def removed(self) -> List[str]:
        """The anchors this op takes out of the file."""
        if self.type == "merge":
            return [a for a in self.anchors if a != self.keep]
        return self.anchors[:1]


class DroppedOp(BaseModel):
    """A planned op the verifier refused, and why (``code`` is stable, for metrics)."""

    model_config = ConfigDict(populate_by_name=True)

    type: str
    code: str
    detail: str = ""


class MaintenanceVerification(BaseModel):
    """What the deterministic verifier did with one file's plan."""

    model_config = ConfigDict(populate_by_name=True)

    planned: int = 0
    kept: int = 0
    dropped: List[DroppedOp] = Field(default_factory=list)


ProposalState = Literal["pending", "approved", "rejected", "withdrawn"]
# ``member``: through the API; ``agent``: a task's assistant on the member's behalf
# (``memory_propose``); ``schedule``: a scheduled run (3.2); ``maintenance``: a
# maintenance run a member started (2.6), which proposes in their name.
ProposerKind = Literal["member", "agent", "schedule", "maintenance"]
ProposalKind = Literal["entry", "compaction"]


class MemoryProposal(BaseModel):
    """A ``PROPOSAL#{proposalId}`` row: a change to a file, waiting for an editor (Shared Projects 2.5a).

    ``text`` is the whole proposed file in the same form ``save_entry`` takes,
    validated when proposed. ``base_version``/``base_content_hash`` record the
    file it was written against (0 and "" for a new file), so an approval can
    tell that the file has moved on since. Decided rows are kept as the review
    record and go with the space.

    A ``compaction`` proposal (2.6) comes from a maintenance run: ``ops`` are
    its changes, addressed by anchor, and ``text`` is the file with all of them
    applied. It is approved op by op against the file as it is by then.
    """

    model_config = ConfigDict(populate_by_name=True)

    proposal_id: str = Field(..., alias="proposalId")
    kind: ProposalKind = "entry"
    state: ProposalState = "pending"
    slug: str
    text: str
    description: Optional[str] = None
    aliases: Optional[List[str]] = None
    base_version: int = Field(0, alias="baseVersion")
    base_content_hash: str = Field("", alias="baseContentHash")
    tokens: Optional[int] = None
    proposer_id: str = Field(..., alias="proposerId")
    proposer_email: str = Field("", alias="proposerEmail")
    proposer_kind: ProposerKind = Field("member", alias="proposerKind")
    # The task it was proposed from, carried into the approved items' provenance.
    source_session_id: Optional[str] = Field(None, alias="sourceSessionId")
    created_at: str = Field(..., alias="createdAt")
    decided_by: Optional[str] = Field(None, alias="decidedBy")
    decided_by_email: Optional[str] = Field(None, alias="decidedByEmail")
    decided_at: Optional[str] = Field(None, alias="decidedAt")
    note: Optional[str] = None
    # The FILEVER version an approval wrote, and whether the reviewer edited the text first.
    result_version: Optional[int] = Field(None, alias="resultVersion")
    edited: bool = False
    # Compaction only: the changes, what the verifier dropped, the run that
    # planned them, and which ops an approval applied (by index into ``ops``).
    ops: Optional[List[MaintenanceOp]] = None
    verification: Optional[MaintenanceVerification] = None
    run_id: Optional[str] = Field(None, alias="runId")
    applied_ops: Optional[List[int]] = Field(None, alias="appliedOps")


MaintenanceRunState = Literal["queued", "running", "done", "failed"]
# ``proposed``: a compaction proposal is waiting for review. ``nothing_to_do``:
# the planner found nothing, or the verifier dropped all of it.
# ``pending_review``: the file already has a maintenance proposal waiting.
# ``not_reached``: the run ran out of time first.
MaintenanceFileOutcome = Literal["proposed", "nothing_to_do", "pending_review", "failed", "not_reached"]


class MaintenanceFileResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str
    outcome: MaintenanceFileOutcome
    proposal_id: Optional[str] = Field(None, alias="proposalId")
    planned: int = 0
    kept: int = 0
    dropped: int = 0
    error: Optional[str] = None


class MaintenanceSnapshot(BaseModel):
    """The space as the run found it (§4.6 step 2): the manifest and ``MEMORY.md``'s hash.

    Objects are content-addressed and every version row keeps its object, so
    this is enough to put the space back as it was.
    """

    model_config = ConfigDict(populate_by_name=True)

    manifest_version: int = Field(0, alias="manifestVersion")
    index_content_hash: Optional[str] = Field(None, alias="indexContentHash")
    entries: List[MemoryEntryRef] = Field(default_factory=list)
    taken_at: str = Field("", alias="takenAt")


class MaintenanceRun(BaseModel):
    """A ``SNAPSHOT#{runId}`` row: one maintenance run and the snapshot it worked from (Shared Projects 2.6).

    app-api writes it ``queued`` with the model and its prices; the worker
    takes the snapshot, plans, verifies and proposes, then marks it ``done``
    or ``failed``. It expires with the space's archive retention.
    """

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    space_id: str = Field(..., alias="spaceId")
    project_id: Optional[str] = Field(None, alias="projectId")
    state: MaintenanceRunState = "queued"
    slug: Optional[str] = Field(None, description="The one file to maintain; None for every file")
    requested_by: str = Field(..., alias="requestedBy")
    requested_by_email: str = Field("", alias="requestedByEmail")
    requested_by_name: Optional[str] = Field(None, alias="requestedByName")
    model_id: str = Field(..., alias="modelId")
    input_price_per_million_tokens: Optional[float] = Field(None, alias="inputPricePerMillionTokens")
    output_price_per_million_tokens: Optional[float] = Field(None, alias="outputPricePerMillionTokens")
    created_at: str = Field(..., alias="createdAt")
    started_at: Optional[str] = Field(None, alias="startedAt")
    finished_at: Optional[str] = Field(None, alias="finishedAt")
    snapshot: Optional[MaintenanceSnapshot] = None
    results: List[MaintenanceFileResult] = Field(default_factory=list)
    input_tokens: int = Field(0, alias="inputTokens")
    output_tokens: int = Field(0, alias="outputTokens")
    cost: Optional[float] = None
    error: Optional[str] = None


class MemoryIndex(BaseModel):
    """The ``INDEX`` row: the machine manifest of a space's entries.

    Distinct from the human-readable ``MEMORY.md`` index text (which lives in
    S3 and is pointed to by :attr:`MemorySpace.index_s3_key`). ``version`` is a
    monotonically-increasing counter reserved for optimistic-concurrency
    control on shared spaces (PR-6); PR-1 writes it but does not yet gate on it.
    """

    model_config = ConfigDict(populate_by_name=True)

    space_id: str = Field(..., alias="spaceId")
    entries: List[MemoryEntryRef] = Field(default_factory=list)
    version: int = Field(0, description="Optimistic-concurrency counter (PR-6)")


class SpaceMember(BaseModel):
    """A ``MEMBER#{email}`` row: one shared grant on a space.

    Email-keyed (you invite by email before the grantee has necessarily
    logged in), mirroring assistant ``ShareEntry``. The owner is NOT a member
    row — ownership is stored on the space itself.
    """

    model_config = ConfigDict(populate_by_name=True)

    email: str = Field(..., description="Grantee email (normalized lowercase)")
    permission: ShareRole = Field("viewer", description="viewer | editor")
    created_at: str = Field("", alias="createdAt")


class MemorySpace(BaseModel):
    """The ``META`` row: a Memory Space's identity + ownership + index pointer.

    The entries manifest lives on the separate ``INDEX`` row (:class:`MemoryIndex`)
    and shared grants on ``MEMBER#`` rows (:class:`SpaceMember`); this row is
    what permission checks and space listings read.
    """

    model_config = ConfigDict(populate_by_name=True)

    space_id: str = Field(..., alias="spaceId")
    name: str = Field(..., description="User-facing space name")
    template: str = Field("blank", description="Template id the space was seeded from")
    owner_id: str = Field(..., alias="ownerId", description="user_id of the owner")
    owner_email: str = Field("", alias="ownerEmail")
    created_at: str = Field("", alias="createdAt")
    updated_at: str = Field("", alias="updatedAt")
    # Pointer to the MEMORY.md index text in S3 (content-addressed). Optional
    # only transiently during creation; always set on a persisted space.
    index_s3_key: Optional[str] = Field(None, alias="indexS3Key")
    index_content_hash: Optional[str] = Field(None, alias="indexContentHash")
    file_format: FileFormat = Field("freeform", alias="fileFormat")
    scope: MemoryScope = Field("personal")
    # Set on the two project scopes; ``user_id`` names the member a
    # ``personal_in_project`` space belongs to.
    project_id: Optional[str] = Field(None, alias="projectId")
    user_id: Optional[str] = Field(None, alias="userId")

    @property
    def is_project_space(self) -> bool:
        return self.scope in PROJECT_SCOPES
