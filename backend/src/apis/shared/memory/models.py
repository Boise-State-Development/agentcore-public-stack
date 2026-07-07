"""Pydantic models for the Memory Space primitive (PR-1, data layer).

A **Memory Space** is a named, first-class, per-owner (optionally shared)
markdown "second brain" that agents read and maintain. See
``docs/specs/user-markdown-memory.md``.

Storage shape (dedicated ``memory-spaces`` DynamoDB table, space-keyed so a
shared space cannot live under one user's partition):

  - ``PK=SPACE#{space_id}  SK=META``            → :class:`MemorySpace`
  - ``PK=SPACE#{space_id}  SK=INDEX``           → :class:`MemoryIndex` (manifest)
  - ``PK=SPACE#{space_id}  SK=MEMBER#{email}``  → :class:`SpaceMember`

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
