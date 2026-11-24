"""Whose skill scripts run: one verdict for every tool that runs one.

Spec: ``docs/specs/skill-code-sandbox.md`` §8 (trust at adoption), D10–D17.

A skill script runs for the invoker when **one trust path holds for the exact
script set it belongs to** and **no switch has turned it off**:

- ``own``: the invoker wrote the skill.
- ``imported``: the invoker imported it (``import_source`` is set).
- ``admin``: an admin-catalog skill (``owner_id == "system"``).
- ``reviewed``: someone else's skill, bound to the turn's Agent or project,
  with an approved review row for that assistant at the script set's hash.
- ``accepted``: someone else's skill in plain chat, with an acceptance row for
  the invoker (direct skill sharing; nothing writes one yet).

The **script set** is every manifest entry of kind ``script``; its hash
(:func:`scripts_hash`) is the sha256 of canonical JSON over ``[path,
contentHash]`` pairs sorted by manifest path. It is computed from the manifest
the turn already holds, never from S3.

Rows live in the AppRoles table beside the skill (no new table or index):

- review: ``PK=SKILL#{id}``, ``SK=SCRIPTS_REVIEW#AST#{assistantId}#{hash}``
  (or ``#USER#{userId}#{hash}`` for an acceptance)
- switch: ``PK=SKILL#{id}``, ``SK=SCRIPTS_SWITCH``

**Read per call, never cached** (:func:`read_trust_rows`, one
``BatchGetItem``). The agent cache keys on configuration, and a revocation
changes no configuration, so anything held on an agent, a tool or the process
would outlive it. Nothing here runs before the first token.

:func:`script_verdict` is pure: the Runtime and app-api share it, and tests
feed it rows directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.skills.models import SYSTEM_OWNER_ID
from apis.shared.skills.resource_paths import manifest_path

logger = logging.getLogger(__name__)

SCRIPT_KIND = "script"

#: Sort-key prefix of a review or acceptance row.
REVIEW_SK_PREFIX = "SCRIPTS_REVIEW#"
#: Sort key of a skill's one switch row.
SWITCH_SK = "SCRIPTS_SWITCH"
#: Scope types in a review key: an Agent or project harness (both are
#: ``Assistant`` rows), or a user (an acceptance).
SCOPE_ASSISTANT = "AST"
SCOPE_USER = "USER"

#: How many hex characters of the hash a person is shown ("version 3f2a9c1e").
SHORT_HASH_CHARS = 8

# Trust paths (``trustedBy``).
TRUSTED_OWN = "own"
TRUSTED_IMPORTED = "imported"
TRUSTED_ADMIN = "admin"
TRUSTED_REVIEWED = "reviewed"
TRUSTED_ACCEPTED = "accepted"

# Refusal codes (``refusedReason``), §8.7.
REFUSED_NOT_REVIEWED = "not_reviewed"
REFUSED_PENDING_ADMIN_REVIEW = "pending_admin_review"
REFUSED_OFF_BY_AUTHOR = "off_by_author"
REFUSED_OFF_BY_ADMIN = "off_by_admin"
REFUSED_NOT_TRUSTED = "not_trusted"
REFUSED_CHANGED = "changed"
REFUSED_UNREVIEWABLE = "unreviewable"
REFUSED_CHECK_FAILED = "check_failed"

REVIEW_APPROVED = "approved"
SWITCH_BY_ADMIN = "admin"
SWITCH_BY_AUTHOR = "author"


# ---------------------------------------------------------------------------
# The script set
# ---------------------------------------------------------------------------


def script_entries(record: Any) -> List[Tuple[str, str]]:
    """``[(manifest path, contentHash)]`` for the record's scripts, sorted by path."""
    entries = [
        (manifest_path(ref), str(getattr(ref, "content_hash", "") or ""))
        for ref in getattr(record, "resources", None) or []
        if getattr(ref, "kind", "") == SCRIPT_KIND
    ]
    return sorted(entries)


def has_scripts(record: Any) -> bool:
    return bool(script_entries(record))


def scripts_hash(record: Any) -> Optional[str]:
    """The script set's hash, or ``None`` when it has no scripts or one has no ``contentHash``.

    Canonical form: ``json.dumps([[path, sha256], …], separators=(",", ":"))``
    over the entries sorted by path, UTF-8, sha256 hex. Pinned by a test,
    because a change here invalidates every review ever recorded.
    """
    entries = script_entries(record)
    if not entries or any(not sha for _, sha in entries):
        return None
    canonical = json.dumps([[path, sha] for path, sha in entries], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scripts_provenance(resources: Iterable[Any]) -> Dict[str, Any]:
    """``{"scripts_hash", "scripts"}`` for an import's resources, or ``{}`` with no scripts.

    What ``SkillImportSource`` records as the importer's acknowledgement (§8.4).
    """
    holder = _Resources(list(resources or []))
    digest = scripts_hash(holder)
    if digest is None:
        return {}
    return {"scripts_hash": digest, "scripts": dict(script_entries(holder))}


@dataclass(frozen=True)
class _Resources:
    resources: List[Any]


def short_hash(value: Optional[str]) -> str:
    """The form a person is shown: the first eight hex characters."""
    return (value or "")[:SHORT_HASH_CHARS]


def changed_scripts(record: Any, staged_sha256: Dict[str, str]) -> List[str]:
    """Manifest paths whose staged bytes don't match their manifest hash (§8.2).

    ``staged_sha256`` maps manifest path to the sha256 of the bytes about to
    run. An entry with no manifest hash is skipped: there is nothing to check
    it against, and the verdict already refused it where a hash is needed.
    A script missing from ``staged_sha256`` counts as changed.
    """
    return [
        path
        for path, expected in script_entries(record)
        if expected and staged_sha256.get(path) != expected
    ]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def skill_pk(skill_id: str) -> str:
    return f"SKILL#{skill_id}"


def review_sk(scope_type: str, scope_id: str, scripts_hash_value: str) -> str:
    return f"{REVIEW_SK_PREFIX}{scope_type}#{scope_id}#{scripts_hash_value}"


@dataclass(frozen=True)
class ScriptSwitch:
    """A skill's switch row: its scripts turned off by the author or an admin."""

    off: bool
    by: str = SWITCH_BY_AUTHOR
    by_user_id: str = ""
    hashes: Tuple[str, ...] = ()

    def applies_to(self, scripts_hash_value: Optional[str]) -> bool:
        """Off for this script set: off with no ``hashes``, or with this hash among them."""
        if not self.off:
            return False
        return not self.hashes or (scripts_hash_value or "") in self.hashes

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "ScriptSwitch":
        return cls(
            off=bool(item.get("off")),
            by=str(item.get("by") or SWITCH_BY_AUTHOR),
            by_user_id=str(item.get("byUserId") or ""),
            hashes=tuple(str(h) for h in item.get("hashes") or ()),
        )


@dataclass(frozen=True)
class ScriptReview:
    """A review (or acceptance) row for one scope at one script-set hash."""

    reviewed_by: str
    state: str = REVIEW_APPROVED
    method: str = "review"
    version: Optional[int] = None

    @property
    def approved(self) -> bool:
        return self.state == REVIEW_APPROVED

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "ScriptReview":
        version = item.get("version")
        return cls(
            reviewed_by=str(item.get("reviewedBy") or ""),
            state=str(item.get("state") or REVIEW_APPROVED),
            method=str(item.get("method") or "review"),
            version=int(version) if version is not None else None,
        )


@dataclass(frozen=True)
class TrustRows:
    """What :func:`read_trust_rows` found for one call."""

    switch: Optional[ScriptSwitch] = None
    review: Optional[ScriptReview] = None


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptVerdict:
    """Whether a skill's scripts may run for this invoker, and why."""

    allowed: bool
    scripts_hash: Optional[str]
    trusted_by: Optional[str] = None
    refused_reason: Optional[str] = None
    reviewed_by: Optional[str] = None
    scope_id: Optional[str] = None
    version: Optional[int] = None

    def audit_fields(self) -> Dict[str, Any]:
        """The fields ``skill.script_run`` gains (§9). Ids and hashes only."""
        fields: Dict[str, Any] = {"scriptsHash": self.scripts_hash or ""}
        if self.trusted_by:
            fields["trustedBy"] = self.trusted_by
        if self.refused_reason:
            fields["refusedReason"] = self.refused_reason
        if self.reviewed_by:
            fields["reviewedBy"] = self.reviewed_by
        if self.scope_id:
            fields["scopeId"] = self.scope_id
        if self.version is not None:
            fields["version"] = self.version
        return fields

    @property
    def label(self) -> str:
        """``trustedBy`` when allowed, else the refusal code (the shadow ``verdict``)."""
        return (self.trusted_by if self.allowed else self.refused_reason) or ""


def _needs_review(record: Any, invoker_id: str) -> bool:
    owner = getattr(record, "owner_id", SYSTEM_OWNER_ID) or SYSTEM_OWNER_ID
    return owner not in (SYSTEM_OWNER_ID, invoker_id)


def review_key(record: Any, invoker_id: str, scope_id: Optional[str]) -> Optional[Tuple[str, str]]:
    """The review row this call needs, as ``(PK, SK)``, or ``None`` when no path needs one.

    Only someone else's skill needs a review, and only one whose script set
    has a hash. The scope is the turn's assistant; with no assistant (plain
    chat) it is the invoker's acceptance.
    """
    if not _needs_review(record, invoker_id):
        return None
    digest = scripts_hash(record)
    if digest is None:
        return None
    skill_id = getattr(record, "skill_id", "") or ""
    if scope_id:
        return skill_pk(skill_id), review_sk(SCOPE_ASSISTANT, scope_id, digest)
    return skill_pk(skill_id), review_sk(SCOPE_USER, invoker_id, digest)


def script_verdict(
    record: Any,
    invoker_id: str,
    scope_id: Optional[str],
    rows: TrustRows,
) -> ScriptVerdict:
    """Decide from the record and the rows already read. Pure: no I/O.

    Order: a switch that applies refuses first, whoever owns the skill; then
    the admin catalog, the invoker's own and imported skills; then someone
    else's, which needs a hash and an approved review for this scope.
    """
    digest = scripts_hash(record)
    if rows.switch is not None and rows.switch.applies_to(digest):
        reason = REFUSED_OFF_BY_ADMIN if rows.switch.by == SWITCH_BY_ADMIN else REFUSED_OFF_BY_AUTHOR
        return ScriptVerdict(allowed=False, scripts_hash=digest, refused_reason=reason, scope_id=scope_id)

    owner = getattr(record, "owner_id", SYSTEM_OWNER_ID) or SYSTEM_OWNER_ID
    if owner == SYSTEM_OWNER_ID:
        return ScriptVerdict(allowed=True, scripts_hash=digest, trusted_by=TRUSTED_ADMIN, scope_id=scope_id)
    if owner == invoker_id:
        path = TRUSTED_IMPORTED if getattr(record, "import_source", None) else TRUSTED_OWN
        return ScriptVerdict(allowed=True, scripts_hash=digest, trusted_by=path, scope_id=scope_id)

    if digest is None:
        return ScriptVerdict(
            allowed=False, scripts_hash=None, refused_reason=REFUSED_UNREVIEWABLE, scope_id=scope_id
        )
    review = rows.review
    if review is None:
        reason = REFUSED_NOT_REVIEWED if scope_id else REFUSED_NOT_TRUSTED
        return ScriptVerdict(allowed=False, scripts_hash=digest, refused_reason=reason, scope_id=scope_id)
    if not review.approved:
        return ScriptVerdict(
            allowed=False,
            scripts_hash=digest,
            refused_reason=REFUSED_PENDING_ADMIN_REVIEW,
            reviewed_by=review.reviewed_by or None,
            scope_id=scope_id,
            version=review.version,
        )
    return ScriptVerdict(
        allowed=True,
        scripts_hash=digest,
        trusted_by=TRUSTED_REVIEWED if scope_id else TRUSTED_ACCEPTED,
        reviewed_by=review.reviewed_by or None,
        scope_id=scope_id,
        version=review.version,
    )


def check_failed_verdict(record: Any, scope_id: Optional[str]) -> ScriptVerdict:
    """The verdict when the rows could not be read: refuse, never guess."""
    return ScriptVerdict(
        allowed=False,
        scripts_hash=scripts_hash(record),
        refused_reason=REFUSED_CHECK_FAILED,
        scope_id=scope_id,
    )


# ---------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------


def _table_name() -> str:
    return os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles")


_client: Any = None


def _dynamodb_client() -> Any:
    # A boto3 client is thread-safe and holds only connection state, never a
    # trust answer, so keeping one per process caches nothing a revocation
    # could outlive.
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("dynamodb")
    return _client


def _plain(item: Dict[str, Any]) -> Dict[str, Any]:
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def read_trust_rows(
    record: Any,
    invoker_id: str,
    scope_id: Optional[str],
    *,
    client: Any = None,
    table_name: Optional[str] = None,
) -> TrustRows:
    """The switch row and, when the call needs one, the review row: one ``BatchGetItem``.

    Blocking; call it with ``asyncio.to_thread``. Raises on a DynamoDB error,
    so the caller can refuse (:func:`check_failed_verdict`) rather than run.
    Read on every call and never cached (module docstring).
    """
    skill_id = getattr(record, "skill_id", "") or ""
    keys: List[Tuple[str, str]] = [(skill_pk(skill_id), SWITCH_SK)]
    wanted_review = review_key(record, invoker_id, scope_id)
    if wanted_review is not None:
        keys.append(wanted_review)

    name = table_name or _table_name()
    db = client or _dynamodb_client()
    request = {name: {"Keys": [{"PK": {"S": pk}, "SK": {"S": sk}} for pk, sk in keys]}}
    found: List[Dict[str, Any]] = []
    for _ in range(3):
        response = db.batch_get_item(RequestItems=request)
        found.extend(response.get("Responses", {}).get(name, []))
        request = response.get("UnprocessedKeys") or {}
        if not request:
            break
    else:
        raise RuntimeError("script trust rows: keys left unprocessed after retries")

    switch: Optional[ScriptSwitch] = None
    review: Optional[ScriptReview] = None
    for raw in found:
        item = _plain(raw)
        sk = item.get("SK")
        if sk == SWITCH_SK:
            switch = ScriptSwitch.from_item(item)
        elif wanted_review is not None and sk == wanted_review[1]:
            review = ScriptReview.from_item(item)
    return TrustRows(switch=switch, review=review)


async def decide(record: Any, invoker_id: str, scope_id: Optional[str]) -> ScriptVerdict:
    """Read the rows and decide, for a tool call. Never raises.

    A read that fails refuses with ``check_failed``: a script whose trust
    could not be checked does not run.
    """
    import asyncio

    try:
        rows = await asyncio.to_thread(read_trust_rows, record, invoker_id, scope_id)
    except Exception:  # noqa: BLE001 - refuse, never guess
        logger.exception("script trust: rows for skill %s could not be read", getattr(record, "skill_id", "?"))
        return check_failed_verdict(record, scope_id)
    return script_verdict(record, invoker_id, scope_id, rows)


__all__ = [
    "REFUSED_CHANGED",
    "REFUSED_CHECK_FAILED",
    "REFUSED_NOT_REVIEWED",
    "REFUSED_NOT_TRUSTED",
    "REFUSED_OFF_BY_ADMIN",
    "REFUSED_OFF_BY_AUTHOR",
    "REFUSED_PENDING_ADMIN_REVIEW",
    "REFUSED_UNREVIEWABLE",
    "ScriptReview",
    "ScriptSwitch",
    "ScriptVerdict",
    "TrustRows",
    "changed_scripts",
    "check_failed_verdict",
    "decide",
    "has_scripts",
    "read_trust_rows",
    "review_key",
    "review_sk",
    "script_entries",
    "script_verdict",
    "scripts_hash",
    "short_hash",
    "skill_pk",
]
