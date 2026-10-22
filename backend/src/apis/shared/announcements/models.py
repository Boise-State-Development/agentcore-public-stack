"""Models for admin-authored feature announcements and per-user acknowledgements.

See ``docs/specs/feature-announcements.md``.

Two item shapes share one table:

  - **Announcement** — ``PK: ANNOUNCEMENTS``, ``SK: ANNOUNCEMENT#<uuid>``.
    A single fixed partition, exactly as ``user_menu_links`` uses, because the
    data is global / single-tenant. When per-org scoping is needed the PK
    becomes ``ANNOUNCEMENTS#<org_id>`` without touching the SK shape.
  - **Acknowledgement** — ``PK: USER#<user_id>``,
    ``SK: ACK#<announcement_id>#R<revision>``. The same per-user partition
    shape ``user_settings`` established. Revision-keyed (§D4) so editing an
    announcement does not un-dismiss it for everybody, while an explicit
    "show this again" does.

The whole field set is modelled here even though PR-1 consumes only part of it:
the table is the expensive thing to change, the routes are not.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from apis.shared.timestamps import from_iso, to_iso, utc_now_iso
# One implementation of the http(s)-only check, not two — announcements
# validate ``ctaUrl`` for exactly the reason ``user_menu_links`` validates
# ``url`` (Angular's DomSanitizer never sees a request made with curl).
from apis.shared.user_menu_links.models import validate_http_url

AnnouncementSurface = Literal["panel", "banner", "modal"]
AnnouncementSeverity = Literal["info", "success", "warning"]
AnnouncementState = Literal["draft", "scheduled", "published", "archived"]
AckAction = Literal["seen", "dismissed", "acknowledged"]

ANNOUNCEMENTS_PK = "ANNOUNCEMENTS"
ANNOUNCEMENT_SK_PREFIX = "ANNOUNCEMENT#"
ACK_SK_PREFIX = "ACK#"

#: ``targetRoles`` entry meaning "everyone" (§D9). A display filter, never a grant.
TARGET_EVERYONE = "*"

TITLE_MAX_LENGTH = 140
BODY_MAX_BYTES = 16 * 1024

#: Acknowledgement actions are *ranked*, and the stored rank only ever
#: increases (§D2). ``seen`` is written automatically on render, so it races
#: the user's click on the ✕; without a monotonic guard a late ``seen`` write
#: would clobber ``dismissed`` and the banner would come back. Same failure
#: class as #741 / #751 — per-user state moving backwards — so it gets the
#: same discipline: the guard lives in the DynamoDB condition expression, not
#: in application ordering.
ACTION_RANKS: Dict[str, int] = {"seen": 1, "dismissed": 2, "acknowledged": 3}

#: A rank at or above this suppresses the loud surfaces (banner, modal).
SUPPRESSING_RANK = ACTION_RANKS["dismissed"]

#: Ack TTLs (§5). Bounded forever without a sweeper.
ACK_TTL_AFTER_EXPIRY = timedelta(days=90)
ACK_TTL_OPEN_ENDED = timedelta(days=730)

#: Ack counters live as **top-level** attributes on the announcement item,
#: one per (revision, action) — ``ackCountsR1Seen`` and friends.
#:
#: Top-level rather than a nested ``ackCounts`` map for one reason:
#: DynamoDB's ``ADD`` only works on top-level attributes, and it creates the
#: attribute (treating a missing one as 0) in the same atomic write. A nested
#: map needs ``SET path = if_not_exists(path, :zero) + :one``, which raises
#: ValidationException until the parent map exists — so every announcement
#: authored before this shipped would need an init-then-retry path around
#: every ack. One atomic write with no fallback beats a tidier shape with a
#: repair branch on the hot path.
#:
#: Keyed by revision because "Show again" (§D4) is a deliberate re-broadcast:
#: rolling its acks into the previous revision's totals would silently inflate
#: them and make the numbers lie about the version people actually saw.
ACK_COUNT_ATTR_PREFIX = "ackCounts"


def ack_count_attr(revision: int, action: str) -> str:
    """Attribute name holding the count of users who reached ``action``."""
    return f"{ACK_COUNT_ATTR_PREFIX}R{int(revision)}{action.capitalize()}"


_LOUD_SURFACES = frozenset({"banner", "modal"})


def action_rank(action: str) -> int:
    """Rank for an ack action; raises on an unknown one."""
    try:
        return ACTION_RANKS[action]
    except KeyError:
        raise ValueError(f"unknown acknowledgement action: {action!r}") from None


def _validate_iso(value: Optional[str], field_name: str) -> Optional[str]:
    """Normalize an ISO-8601 timestamp, or raise.

    Stored normalized (``…Z``) so string comparison against ``utc_now_iso()``
    in the visibility filter is a valid instant comparison.
    """
    if value is None or value == "":
        return None
    try:
        return to_iso(from_iso(value))
    except (ValueError, TypeError) as e:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from e


def _validate_surfaces_expiry(
    surfaces: List[str], expires_at: Optional[str]
) -> None:
    """A loud surface must say when it stops being loud (§5, §11).

    An unbounded banner or modal is the announcement-fatigue failure mode with
    no backstop, so the requirement is enforced at the model layer rather than
    left to the admin form.
    """
    if expires_at:
        return
    loud = sorted(_LOUD_SURFACES.intersection(surfaces))
    if loud:
        raise ValueError(
            "expiresAt is required when surfaces include " + ", ".join(loud)
        )


def _validate_body_size(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    if len(value.encode("utf-8")) > BODY_MAX_BYTES:
        raise ValueError(f"body_markdown must be at most {BODY_MAX_BYTES} bytes")
    return value


# =============================================================================
# Stored items
# =============================================================================


@dataclass
class Announcement:
    """One admin-authored announcement stored in DynamoDB."""

    announcement_id: str
    title: str
    body_markdown: str
    created_at: str
    updated_at: str
    publish_at: str
    summary: Optional[str] = None
    surfaces: List[str] = field(default_factory=lambda: ["panel"])
    severity: str = "info"
    state: str = "draft"
    expires_at: Optional[str] = None
    # ⚠️ **Display filter, not an RBAC grant (§D9).** CLAUDE.md's rule that a
    # role list on a resource must be written through to each role's
    # ``granted*`` list governs tools, models, and skills — things with a
    # ``can_access_*`` predicate behind them. Announcement visibility is not
    # access control: there is no capability conferred and nothing to inherit,
    # so this list is matched against ``User.roles`` at read time and lives
    # **only** on this item. Do not "fix" it into ``apis/shared/rbac/``; doing
    # so would put display metadata into the access-decision path.
    target_roles: List[str] = field(default_factory=lambda: ["*"])
    show_to_new_users: bool = False
    requires_ack: bool = False
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None
    revision: int = 1
    created_by: Optional[str] = None
    #: Ack funnel counters, carried through read → write.
    #:
    #: **Not domain state — a projection that must survive.** Every admin
    #: mutation (``update_announcement``, ``set_state``, ``bump_revision``)
    #: is a full ``put_item`` of this dataclass, so any attribute the model
    #: does not know about is destroyed by it. Without this field, publishing
    #: or archiving an announcement — or hitting "Show again" — would silently
    #: zero every stat the feature exists to report.
    #:
    #: The read-modify-write does mean an ack landing in the same instant as
    #: an admin edit can lose its increment. That is the documented cost of
    #: approximate O(1) counters (§9); admin writes are rare, and the ack
    #: itself is never at risk because it is a different item.
    ack_counts: Dict[str, int] = field(default_factory=dict)

    def to_dynamo_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "PK": ANNOUNCEMENTS_PK,
            "SK": f"{ANNOUNCEMENT_SK_PREFIX}{self.announcement_id}",
            "announcementId": self.announcement_id,
            "title": self.title,
            "bodyMarkdown": self.body_markdown,
            "surfaces": list(self.surfaces),
            "severity": self.severity,
            "state": self.state,
            "publishAt": self.publish_at,
            "targetRoles": list(self.target_roles),
            "showToNewUsers": self.show_to_new_users,
            "requiresAck": self.requires_ack,
            "revision": int(self.revision),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.summary:
            item["summary"] = self.summary
        if self.expires_at:
            item["expiresAt"] = self.expires_at
        if self.cta_label:
            item["ctaLabel"] = self.cta_label
        if self.cta_url:
            item["ctaUrl"] = self.cta_url
        if self.created_by:
            item["createdBy"] = self.created_by
        # Carried forward verbatim so a full-item put cannot destroy them.
        for attr, value in (self.ack_counts or {}).items():
            item[attr] = int(value)
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "Announcement":
        try:
            created_at = item["createdAt"]
            updated_at = item["updatedAt"]
            publish_at = item["publishAt"]
        except KeyError as e:
            raise ValueError(
                f"Announcement item {item.get('SK', '?')} is missing required "
                f"field: {e.args[0]}"
            ) from e
        return cls(
            announcement_id=item["announcementId"],
            title=item["title"],
            body_markdown=item.get("bodyMarkdown", ""),
            summary=item.get("summary"),
            surfaces=list(item.get("surfaces") or ["panel"]),
            severity=item.get("severity", "info"),
            state=item.get("state", "draft"),
            publish_at=publish_at,
            expires_at=item.get("expiresAt"),
            target_roles=list(item.get("targetRoles") or ["*"]),
            show_to_new_users=bool(item.get("showToNewUsers", False)),
            requires_ack=bool(item.get("requiresAck", False)),
            cta_label=item.get("ctaLabel"),
            cta_url=item.get("ctaUrl"),
            revision=int(item.get("revision", 1)),
            created_at=created_at,
            updated_at=updated_at,
            created_by=item.get("createdBy"),
            ack_counts={
                key: int(value)
                for key, value in item.items()
                if key.startswith(ACK_COUNT_ATTR_PREFIX)
            },
        )

    def ack_ttl(self, action: str) -> Optional[int]:
        """Epoch-seconds TTL for an ack on this announcement, or None to keep it.

        ``acknowledged`` on a ``requiresAck`` announcement is a compliance
        record, so it is deliberately **not** expired — the archive path
        disposes of those on purpose (§5).
        """
        if self.requires_ack and action == "acknowledged":
            return None
        if self.expires_at:
            anchor = from_iso(self.expires_at) + ACK_TTL_AFTER_EXPIRY
        else:
            anchor = from_iso(self.publish_at) + ACK_TTL_OPEN_ENDED
        return int(anchor.timestamp())


@dataclass
class AnnouncementAck:
    """One user's acknowledgement of one revision of one announcement."""

    user_id: str
    announcement_id: str
    revision: int
    action: str
    action_at: str
    surface: str
    action_rank: int = 0
    ttl: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.action_rank:
            self.action_rank = action_rank(self.action)

    @staticmethod
    def sort_key(announcement_id: str, revision: int) -> str:
        return f"{ACK_SK_PREFIX}{announcement_id}#R{int(revision)}"

    @staticmethod
    def partition_key(user_id: str) -> str:
        return f"USER#{user_id}"

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "AnnouncementAck":
        pk = item.get("PK", "")
        return cls(
            user_id=pk[len("USER#"):] if pk.startswith("USER#") else pk,
            announcement_id=item["announcementId"],
            revision=int(item["revision"]),
            action=item["action"],
            action_rank=int(item.get("actionRank", 0)),
            action_at=item.get("actionAt", ""),
            surface=item.get("surface", ""),
            ttl=int(item["ttl"]) if item.get("ttl") is not None else None,
        )


# =============================================================================
# Pydantic request/response models
# =============================================================================


class AnnouncementCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH)
    body_markdown: str = Field(..., min_length=1)
    summary: Optional[str] = Field(None, max_length=280)
    surfaces: List[AnnouncementSurface] = Field(default_factory=lambda: ["panel"])
    severity: AnnouncementSeverity = "info"
    # Only an *unpublished* state may be chosen at create time. Going live is
    # its own action, so an announcement can never be published by the same
    # call that is still validating its body.
    state: Literal["draft", "scheduled"] = "draft"
    publish_at: Optional[str] = None
    expires_at: Optional[str] = None
    target_roles: List[str] = Field(default_factory=lambda: ["*"])
    show_to_new_users: bool = False
    requires_ack: bool = False
    cta_label: Optional[str] = Field(None, max_length=64)
    cta_url: Optional[str] = Field(None, max_length=2048)

    @field_validator("body_markdown")
    @classmethod
    def _check_body_size(cls, v: str) -> str:
        return _validate_body_size(v)

    @field_validator("cta_url")
    @classmethod
    def _check_cta_url(cls, v: Optional[str]) -> Optional[str]:
        return validate_http_url(v)

    @field_validator("publish_at")
    @classmethod
    def _check_publish_at(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso(v, "publish_at")

    @field_validator("expires_at")
    @classmethod
    def _check_expires_at(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso(v, "expires_at")

    @model_validator(mode="after")
    def _check_invariants(self) -> "AnnouncementCreate":
        validate_announcement_invariants(
            surfaces=list(self.surfaces),
            expires_at=self.expires_at,
            publish_at=self.publish_at,
            cta_label=self.cta_label,
            cta_url=self.cta_url,
        )
        return self


class AnnouncementUpdate(BaseModel):
    """Partial update — all fields optional.

    Two fields are deliberately **absent**, because both are transitions rather
    than content:

    * ``state`` — ``/publish`` and ``/archive`` own it. Accepting it here would
      make the publish guard decorative: an archived announcement could be put
      back on screen by a PATCH that looks like an ordinary body edit.
    * ``revision`` — bumping it re-shows the announcement to everyone who
      already dismissed it, so it is the explicit ``/revise`` action (§D4) and
      never a side effect of fixing a typo.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=TITLE_MAX_LENGTH)
    body_markdown: Optional[str] = Field(None, min_length=1)
    summary: Optional[str] = Field(None, max_length=280)
    surfaces: Optional[List[AnnouncementSurface]] = None
    severity: Optional[AnnouncementSeverity] = None
    publish_at: Optional[str] = None
    expires_at: Optional[str] = None
    target_roles: Optional[List[str]] = None
    show_to_new_users: Optional[bool] = None
    requires_ack: Optional[bool] = None
    cta_label: Optional[str] = Field(None, max_length=64)
    cta_url: Optional[str] = Field(None, max_length=2048)

    @field_validator("body_markdown")
    @classmethod
    def _check_body_size(cls, v: Optional[str]) -> Optional[str]:
        return _validate_body_size(v)

    @field_validator("cta_url")
    @classmethod
    def _check_cta_url(cls, v: Optional[str]) -> Optional[str]:
        return validate_http_url(v)

    @field_validator("publish_at")
    @classmethod
    def _check_publish_at(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso(v, "publish_at")

    @field_validator("expires_at")
    @classmethod
    def _check_expires_at(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso(v, "expires_at")


class AnnouncementAckRequest(BaseModel):
    """Body of ``POST /announcements/{id}/ack`` (consumed in PR-2)."""

    action: AckAction
    surface: AnnouncementSurface


class AnnouncementResponse(BaseModel):
    announcement_id: str
    title: str
    body_markdown: str
    summary: Optional[str] = None
    surfaces: List[str]
    severity: str
    state: str
    publish_at: str
    expires_at: Optional[str] = None
    target_roles: List[str]
    show_to_new_users: bool
    requires_ack: bool
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None
    revision: int
    created_at: str
    updated_at: str
    created_by: Optional[str] = None

    @classmethod
    def from_announcement(cls, a: Announcement) -> "AnnouncementResponse":
        return cls(
            announcement_id=a.announcement_id,
            title=a.title,
            body_markdown=a.body_markdown,
            summary=a.summary,
            surfaces=list(a.surfaces),
            severity=a.severity,
            state=a.state,
            publish_at=a.publish_at,
            expires_at=a.expires_at,
            target_roles=list(a.target_roles),
            show_to_new_users=a.show_to_new_users,
            requires_ack=a.requires_ack,
            cta_label=a.cta_label,
            cta_url=a.cta_url,
            revision=a.revision,
            created_at=a.created_at,
            updated_at=a.updated_at,
            created_by=a.created_by,
        )


class AnnouncementListResponse(BaseModel):
    announcements: List[AnnouncementResponse]
    total: int


class AnnouncementStatsResponse(BaseModel):
    """Reach for one announcement, at its **current** revision.

    The three counts are a **funnel, not a partition**: a user who
    acknowledged also counts as dismissed and as seen, because the stored rank
    only ever rises through them (§D2). So ``seen >= dismissed >=
    acknowledged`` always holds, and "how many only ever saw it" is
    ``seen - dismissed``. Reading them as disjoint buckets would understate
    every stage.

    Everything here is approximate by construction and must be labelled that
    way in the UI (§11):

    - the counts are incremented on a **second** write after the ack itself
      lands, so a failure between the two under-counts by one. That is the
      documented trade for O(1) stats with no GSI and no scan.
    - ``targeted`` is a denominator that moves as people join and roles
      change. **Do not build compliance reporting on it.**
    - **Nothing is backfilled.** The counters are incremented by the ack write
      path, so acks recorded before this shipped are invisible here — an
      existing environment starts every announcement at zero on deploy day
      even where people have already read and dismissed it. The ack rows
      themselves are intact; only the tallies begin at the deploy. There is no
      cheap repair for this (counting the existing rows is the scan the design
      exists to avoid), so read early numbers as "reach since stats shipped".
    """

    announcement_id: str
    revision: int
    seen: int
    dismissed: int
    acknowledged: int
    #: Active users this announcement is aimed at, or None when the audience
    #: cannot be counted — see ``AnnouncementsService.get_stats``.
    targeted: Optional[int] = None


# =============================================================================
# User-facing response models
#
# Deliberately NOT a subset alias of ``AnnouncementResponse``. That model is
# the admin view and carries ``state``, ``targetRoles``, ``showToNewUsers``,
# ``createdBy`` and the audit timestamps — telling a user which roles a notice
# was aimed at, or that one exists in a state they cannot see, is a small
# information leak with no upside. Two explicit models means adding an admin
# field can never widen the user payload by accident.
# =============================================================================


class UserAnnouncement(BaseModel):
    """One announcement as a user sees it."""

    announcement_id: str
    title: str
    body_markdown: str
    summary: Optional[str] = None
    surfaces: List[str]
    severity: str
    publish_at: str
    expires_at: Optional[str] = None
    requires_ack: bool
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None
    revision: int
    #: No acknowledgement recorded at this revision — drives the unread dot.
    is_unread: bool
    #: Acked an earlier revision but not this one, so the panel says "Updated"
    #: rather than "New" (§D4).
    is_updated: bool

    @classmethod
    def from_announcement(
        cls, a: Announcement, *, is_unread: bool, is_updated: bool
    ) -> "UserAnnouncement":
        return cls(
            announcement_id=a.announcement_id,
            title=a.title,
            body_markdown=a.body_markdown,
            summary=a.summary,
            surfaces=list(a.surfaces),
            severity=a.severity,
            publish_at=a.publish_at,
            expires_at=a.expires_at,
            requires_ack=a.requires_ack,
            cta_label=a.cta_label,
            cta_url=a.cta_url,
            revision=a.revision,
            is_unread=is_unread,
            is_updated=is_updated,
        )


class AnnouncementFeedResponse(BaseModel):
    """``GET /announcements`` — already filtered and capped (§D5, §D7).

    ``banner`` and ``modal`` are populated from PR-2 onward even though no SPA
    surface renders them until PR-4 / PR-5; the contract is complete so those
    PRs are pure frontend.
    """

    panel: List[UserAnnouncement]
    banner: Optional[UserAnnouncement] = None
    modal: Optional[UserAnnouncement] = None
    unread_count: int


def validate_announcement_invariants(
    *,
    surfaces: List[str],
    expires_at: Optional[str],
    publish_at: Optional[str] = None,
    cta_label: Optional[str] = None,
    cta_url: Optional[str] = None,
) -> None:
    """Cross-field rules, shared by create validation and post-merge update.

    Raised as ``ValueError`` so pydantic reports it as a 422 on create and the
    route maps it to a 400 on a partial update whose *merged* result is
    invalid — the same split ``user_menu_links`` uses.
    """
    _validate_surfaces_expiry(surfaces, expires_at)
    if cta_url and not cta_label:
        raise ValueError("cta_label is required when cta_url is set")
    if cta_label and not cta_url:
        raise ValueError("cta_url is required when cta_label is set")
    if publish_at and expires_at and expires_at <= publish_at:
        raise ValueError("expires_at must be after publish_at")
