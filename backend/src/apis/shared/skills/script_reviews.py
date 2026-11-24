"""Writing the rows that let other people run a skill's scripts (skill-code-sandbox §8.4–§8.5).

``script_trust`` reads these rows on every script call; this module writes them,
from app-api's Agent and project saves and from the author's switch. It shares
the key shapes with ``script_trust`` so the two can't drift.

**Reviews** (``SCRIPTS_REVIEW#AST#{assistantId}#{scriptsHash}``) are recorded by
:func:`record_binding_reviews` after an Agent or a project's skills are saved:

- A skill the binder **wrote by hand** (``owner_id`` is the binder, no
  ``import_source``) gets an ``authored`` review with no sheet (D17).
- An **imported** skill, or (with direct sharing) someone else's, gets a
  ``review`` only when the save carries ``scriptsReviewed: {skillId: scriptsHash}``
  for the exact hash it is pinned at. Until PR-2e's sheet, nothing requires it.
- Only **pinned** bindings are reviewed: a review always names a frozen version
  (§8.3). A live binding with scripts is the backfill's to pin (PR-2d).
- A skill **removed** from the assistant loses that assistant's review rows.

**The switch** (``SCRIPTS_SWITCH``) is one row per skill, written by the author
(:func:`set_author_switch`) or an admin (PR-2f). Only an admin can undo an
admin's switch.

Every write is audited (``skill.scripts_reviewed``,
``skill.scripts_review_withdrawn``, ``skill.scripts_switched``): ids and hashes
only. Nothing here runs on the turn path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from apis.shared.audit.models import TARGET_SKILL, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User

from . import script_trust as st
from .models import SYSTEM_OWNER_ID
from .pins import binding_adder, binding_pin, pinned_ref, split_pinned_ref

logger = logging.getLogger(__name__)

#: The request field a save carries reviews in (``{skillId: scriptsHash}``).
SCRIPTS_REVIEWED_FIELD = "scriptsReviewed"

METHOD_REVIEW = "review"
METHOD_AUTHORED = "authored"
METHOD_UPDATE = "update"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table() -> Any:
    import boto3

    name = os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles")
    return boto3.resource("dynamodb").Table(name)


# ---------------------------------------------------------------------------
# Rows (blocking; callers run them in a worker thread)
# ---------------------------------------------------------------------------


def _put_review(
    skill_id: str,
    scope_id: str,
    digest: str,
    *,
    reviewed_by: str,
    method: str,
    version: Optional[int],
    scripts: Dict[str, str],
    table: Any = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "PK": st.skill_pk(skill_id),
        "SK": st.review_sk(st.SCOPE_ASSISTANT, scope_id, digest),
        "skillId": skill_id,
        "scopeType": st.SCOPE_ASSISTANT,
        "scopeId": scope_id,
        "scriptsHash": digest,
        "reviewedBy": reviewed_by,
        "reviewedAt": _now(),
        "method": method,
        "state": st.REVIEW_APPROVED,
        "scripts": scripts,
    }
    if version is not None:
        item["version"] = version
    (table or _table()).put_item(Item=item)
    return item


def _existing_reviews(keys: List[Tuple[str, str]], table: Any = None) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """The review rows among ``keys`` (``(PK, SK)`` pairs) that exist, by key."""
    if not keys:
        return {}
    tbl = table or _table()
    client = tbl.meta.client
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), 100):
        request = {tbl.name: {"Keys": [{"PK": pk, "SK": sk} for pk, sk in unique[start:start + 100]]}}
        for _ in range(3):
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(tbl.name, []):
                found[(item["PK"], item["SK"])] = item
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
    return found


def _delete_scope_reviews(skill_id: str, scope_id: str, table: Any = None) -> List[str]:
    """Delete every review of ``skill_id`` for one assistant; returns the hashes removed."""
    from boto3.dynamodb.conditions import Key

    tbl = table or _table()
    prefix = f"{st.REVIEW_SK_PREFIX}{st.SCOPE_ASSISTANT}#{scope_id}#"
    removed: List[str] = []
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(st.skill_pk(skill_id)) & Key("SK").begins_with(prefix)
    }
    while True:
        response = tbl.query(**kwargs)
        for item in response.get("Items", []):
            tbl.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            removed.append(str(item.get("scriptsHash") or item["SK"][len(prefix):]))
        last = response.get("LastEvaluatedKey")
        if not last:
            return removed
        kwargs["ExclusiveStartKey"] = last


def read_switch(skill_id: str, table: Any = None) -> Optional[st.ScriptSwitch]:
    item = (table or _table()).get_item(Key={"PK": st.skill_pk(skill_id), "SK": st.SWITCH_SK}).get("Item")
    return st.ScriptSwitch.from_item(item) if item else None


def read_review(skill_id: str, scope_id: str, digest: str, table: Any = None) -> Optional[Dict[str, Any]]:
    key = {"PK": st.skill_pk(skill_id), "SK": st.review_sk(st.SCOPE_ASSISTANT, scope_id, digest)}
    return (table or _table()).get_item(Key=key).get("Item")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(action: str, actor: User, skill_id: str, after: Dict[str, Any]) -> None:
    try:
        get_audit_service().record(
            action=action,
            actor=actor,
            target_id=skill_id,
            target_type=TARGET_SKILL,
            changes=sorted(after),
            after=after,
        )
    except Exception:  # noqa: BLE001 - an audit gap never fails the save
        logger.exception("script review audit record could not be written", extra={"action": action})


# ---------------------------------------------------------------------------
# Reviews on save
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedReview:
    """One review a save wrote."""

    skill_id: str
    scripts_hash: str
    method: str
    version: Optional[int]


def _skill_bindings(bindings: Optional[Iterable[Any]]) -> List[Any]:
    return [b for b in bindings or [] if getattr(b, "kind", None) == "skill"]


def _bound_ids(bindings: Optional[Iterable[Any]]) -> set:
    return {split_pinned_ref(b.ref)[0] for b in _skill_bindings(bindings)}


def _pinned_refs(bindings: Iterable[Any]) -> Dict[str, Tuple[int, Any]]:
    """``skill_id -> (version, binding)`` for every pinned skill binding."""
    out: Dict[str, Tuple[int, Any]] = {}
    for binding in _skill_bindings(bindings):
        skill_id, ref_version = split_pinned_ref(binding.ref)
        version = binding_pin(binding.config) or ref_version
        if version:
            out[skill_id] = (version, binding)
    return out


def review_method(record: Any, binder_id: str) -> Optional[str]:
    """``authored`` when the binder wrote this skill's scripts by hand, else ``None``.

    The test is provenance, not ownership: a skill with an ``import_source``
    carries imported scripts even if the binder owns it, so it needs the sheet.
    """
    owner = getattr(record, "owner_id", SYSTEM_OWNER_ID) or SYSTEM_OWNER_ID
    if owner == binder_id and getattr(record, "import_source", None) is None:
        return METHOD_AUTHORED
    return None


def needs_review(record: Any) -> bool:
    """A skill whose scripts only run for others with a review (not a catalog skill)."""
    owner = getattr(record, "owner_id", SYSTEM_OWNER_ID) or SYSTEM_OWNER_ID
    return owner != SYSTEM_OWNER_ID and st.has_scripts(record)


async def record_binding_reviews(
    assistant_id: str,
    before: Optional[Iterable[Any]],
    after: Optional[Iterable[Any]],
    actor: User,
    *,
    scripts_reviewed: Optional[Mapping[str, str]] = None,
    scope_kind: str = "agent",
) -> List[RecordedReview]:
    """Record the reviews an Agent or project save implies, and drop removed skills' reviews.

    ``before`` / ``after`` are the assistant's bindings either side of the save
    (``after`` is what was stored). ``scripts_reviewed`` maps a skill id to the
    full ``scriptsHash`` the binder was shown. A binding's binder is its
    ``addedBy`` (projects) or else the saver. Never raises: a failed review
    write leaves the scripts refusing for members, which is the safe side, and
    is logged.
    """
    if after is None:
        return []
    try:
        return await _record_binding_reviews(
            assistant_id, before, list(after), actor, dict(scripts_reviewed or {}), scope_kind
        )
    except Exception:  # noqa: BLE001 - the save already succeeded
        logger.exception("script reviews for assistant %s could not be recorded", assistant_id)
        return []


async def _record_binding_reviews(
    assistant_id: str,
    before: Optional[Iterable[Any]],
    after: List[Any],
    actor: User,
    scripts_reviewed: Dict[str, str],
    scope_kind: str,
) -> List[RecordedReview]:
    from .pinning import load_skill_records

    removed = _bound_ids(before) - _bound_ids(after)
    for skill_id in sorted(removed):
        hashes = await asyncio.to_thread(_delete_scope_reviews, skill_id, assistant_id)
        if hashes:
            await asyncio.to_thread(
                _audit,
                AuditAction.SKILL_SCRIPTS_REVIEW_WITHDRAWN,
                actor,
                skill_id,
                {"scopeType": scope_kind, "scopeId": assistant_id, "scriptsHashes": sorted(hashes), "reason": "unbound"},
            )

    pinned = _pinned_refs(after)
    if not pinned:
        return []
    records = await load_skill_records([pinned_ref(sid, v) for sid, (v, _) in sorted(pinned.items())])
    candidates = []
    for record in records:
        digest = st.scripts_hash(record)
        if digest is None or not needs_review(record):
            continue
        candidates.append((record, digest))
    if not candidates:
        return []

    keys = [(st.skill_pk(r.skill_id), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, d)) for r, d in candidates]
    existing = await asyncio.to_thread(_existing_reviews, keys)
    written: List[RecordedReview] = []
    for (record, digest), key in zip(candidates, keys):
        if key in existing:
            continue
        version, binding = pinned[record.skill_id]
        binder = binding_adder(binding.config) or actor.user_id
        method = review_method(record, binder)
        reviewer = binder
        if method is None:
            if scripts_reviewed.get(record.skill_id) != digest:
                continue
            method, reviewer = METHOD_REVIEW, actor.user_id
        scripts = dict(st.script_entries(record))
        await asyncio.to_thread(
            _put_review,
            record.skill_id,
            assistant_id,
            digest,
            reviewed_by=reviewer,
            method=method,
            version=version,
            scripts=scripts,
        )
        await asyncio.to_thread(
            _audit,
            AuditAction.SKILL_SCRIPTS_REVIEWED,
            actor,
            record.skill_id,
            {
                "scriptsHash": digest,
                "scripts": scripts,
                "scopeType": scope_kind,
                "scopeId": assistant_id,
                "version": version,
                "method": method,
                "state": st.REVIEW_APPROVED,
                "reviewedBy": reviewer,
            },
        )
        written.append(RecordedReview(record.skill_id, digest, method, version))
    return written


# ---------------------------------------------------------------------------
# The author's switch
# ---------------------------------------------------------------------------


class ScriptSwitchLockedError(Exception):
    """An admin turned these scripts off; only an admin can turn them back on."""


def _put_switch(skill_id: str, *, off: bool, by: str, by_user_id: str, reason: str, table: Any = None) -> None:
    (table or _table()).put_item(
        Item={
            "PK": st.skill_pk(skill_id),
            "SK": st.SWITCH_SK,
            "skillId": skill_id,
            "off": off,
            "by": by,
            "byUserId": by_user_id,
            "at": _now(),
            "reason": reason,
        }
    )


async def set_author_switch(skill_id: str, actor: User, *, off: bool, reason: str = "") -> st.ScriptSwitch:
    """The author turns their skill's scripts off or on, everywhere (§8.4).

    The caller has already checked that ``actor`` owns the skill. Raises
    :class:`ScriptSwitchLockedError` when an admin's switch is in place.
    """
    current = await asyncio.to_thread(read_switch, skill_id)
    if current is not None and current.off and current.by == st.SWITCH_BY_ADMIN:
        raise ScriptSwitchLockedError("An administrator turned this skill's scripts off.")
    if current is not None and current.off == off and current.by == st.SWITCH_BY_AUTHOR:
        return current
    if current is None and not off:
        return st.ScriptSwitch(off=False)
    await asyncio.to_thread(
        _put_switch, skill_id, off=off, by=st.SWITCH_BY_AUTHOR, by_user_id=actor.user_id, reason=reason[:500]
    )
    await asyncio.to_thread(
        _audit, AuditAction.SKILL_SCRIPTS_SWITCHED, actor, skill_id, {"off": off, "by": st.SWITCH_BY_AUTHOR}
    )
    return st.ScriptSwitch(off=off, by=st.SWITCH_BY_AUTHOR, by_user_id=actor.user_id)


__all__ = [
    "METHOD_AUTHORED",
    "METHOD_REVIEW",
    "RecordedReview",
    "SCRIPTS_REVIEWED_FIELD",
    "ScriptSwitchLockedError",
    "needs_review",
    "read_review",
    "read_switch",
    "record_binding_reviews",
    "review_method",
    "set_author_switch",
]
