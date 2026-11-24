"""The admin's view of one skill's scripts, and the admin switch (skill-code-sandbox §8.4, §8.10).

``admin.skill_scripts`` (``apis/app_api/admin/skill_scripts/``) reads and writes
through this module:

- :func:`load_script_detail` gathers what the page shows: the skill's author and
  provenance, its scripts with read-only source, every reviewed version and
  where it was reviewed, and the switch.
- :func:`set_admin_switch` writes the ``SCRIPTS_SWITCH`` row with ``by: admin``,
  for every version or only the listed hashes. An admin can undo an author's
  switch; only an admin can undo an admin's, which
  ``script_reviews.set_author_switch`` already refuses to do.

The key shapes and the switch row's reading come from ``script_trust``, so the
Runtime's verdict reads exactly what this writes. Every switch is audited as
``skill.scripts_switched``: ids, hashes and the admin's reason, never script
text. Nothing here runs on the turn path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from apis.shared.audit.models import TARGET_SKILL, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User

from . import script_trust as st
from .models import SYSTEM_OWNER_ID, skill_provenance

logger = logging.getLogger(__name__)

#: A script's source is shown up to this many bytes; scripts may be up to 1 MiB.
SOURCE_MAX_BYTES = 256 * 1024
#: At most this many hashes in one per-version switch.
MAX_SWITCH_HASHES = 50
REASON_MAX_CHARS = 500

_HASH = re.compile(r"^[0-9a-f]{64}$")


class InvalidSwitchError(ValueError):
    """The switch request names a hash that isn't a script-set hash."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table() -> Any:
    import boto3

    name = os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles")
    return boto3.resource("dynamodb").Table(name)


# ---------------------------------------------------------------------------
# Rows (blocking; callers run them in a worker thread)
# ---------------------------------------------------------------------------


def read_switch_item(skill_id: str, table: Any = None) -> Optional[Dict[str, Any]]:
    """The raw switch row, with its ``at`` and ``reason``, or ``None``."""
    key = {"PK": st.skill_pk(skill_id), "SK": st.SWITCH_SK}
    return (table or _table()).get_item(Key=key).get("Item")


def query_review_rows(skill_id: str, table: Any = None) -> List[Dict[str, Any]]:
    """Every review and acceptance row of ``skill_id``: one Query on ``SCRIPTS_REVIEW#``."""
    from boto3.dynamodb.conditions import Key

    tbl = table or _table()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(st.skill_pk(skill_id)) & Key("SK").begins_with(st.REVIEW_SK_PREFIX)
    }
    rows: List[Dict[str, Any]] = []
    while True:
        response = tbl.query(**kwargs)
        rows.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return rows
        kwargs["ExclusiveStartKey"] = last


def _put_admin_switch(
    skill_id: str,
    *,
    off: bool,
    by_user_id: str,
    hashes: Sequence[str],
    reason: str,
    table: Any = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "PK": st.skill_pk(skill_id),
        "SK": st.SWITCH_SK,
        "skillId": skill_id,
        "off": off,
        "by": st.SWITCH_BY_ADMIN,
        "byUserId": by_user_id,
        "at": _now(),
        "reason": reason,
    }
    if off and hashes:
        item["hashes"] = list(hashes)
    (table or _table()).put_item(Item=item)
    return item


# ---------------------------------------------------------------------------
# The admin switch
# ---------------------------------------------------------------------------


def normalize_hashes(hashes: Optional[Iterable[str]]) -> List[str]:
    """Lower-cased, de-duplicated, sorted; raises :class:`InvalidSwitchError` on a bad one."""
    out = sorted({str(h).strip().lower() for h in hashes or () if str(h).strip()})
    bad = [h for h in out if not _HASH.match(h)]
    if bad:
        raise InvalidSwitchError("Each hash must be a full 64-character script-set hash.")
    if len(out) > MAX_SWITCH_HASHES:
        raise InvalidSwitchError(f"At most {MAX_SWITCH_HASHES} hashes can be switched off at once.")
    return out


def _switch_state(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The audit's ``before``: what was in force, ids and hashes only."""
    if not item:
        return {"off": False}
    switch = st.ScriptSwitch.from_item(item)
    return {"off": switch.off, "by": switch.by, "hashes": list(switch.hashes)}


def _audit(actor: User, skill_id: str, before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """One ``skill.scripts_switched`` record; ``True`` when stored. Never raises."""
    try:
        return get_audit_service().record(
            action=AuditAction.SKILL_SCRIPTS_SWITCHED,
            actor=actor,
            target_id=skill_id,
            target_type=TARGET_SKILL,
            changes=sorted(after),
            before=before,
            after=after,
            reason=after.get("reason") or None,
        ) is not None
    except Exception:  # noqa: BLE001 - an audit gap never undoes the switch
        logger.exception("skill.scripts_switched audit record could not be written")
        return False


async def set_admin_switch(
    skill_id: str,
    actor: User,
    *,
    off: bool,
    hashes: Optional[Iterable[str]] = None,
    reason: str = "",
) -> st.ScriptSwitch:
    """An admin turns a skill's scripts off (all versions, or ``hashes``) or back on.

    The caller has already checked the scope and that the skill exists. Turning
    scripts on writes ``off: false, by: admin``, which clears an author's switch
    too and leaves the author free to switch again. ``hashes`` only matters when
    ``off``. A request that changes nothing writes nothing and is not audited.
    """
    wanted = normalize_hashes(hashes) if off else []
    reason = (reason or "").strip()[:REASON_MAX_CHARS]
    current = await asyncio.to_thread(read_switch_item, skill_id)
    before = _switch_state(current)
    if current is None and not off:
        return st.ScriptSwitch(off=False)
    if current is not None:
        existing = st.ScriptSwitch.from_item(current)
        unchanged = existing.off == off and (
            not off or (existing.by == st.SWITCH_BY_ADMIN and sorted(existing.hashes) == wanted)
        )
        if unchanged:
            return existing

    await asyncio.to_thread(
        _put_admin_switch, skill_id, off=off, by_user_id=actor.user_id, hashes=wanted, reason=reason
    )
    after = {"off": off, "by": st.SWITCH_BY_ADMIN, "hashes": wanted, "reason": reason}
    await asyncio.to_thread(_audit, actor, skill_id, before, after)
    return st.ScriptSwitch(off=off, by=st.SWITCH_BY_ADMIN, by_user_id=actor.user_id, hashes=tuple(wanted))


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------


@dataclass
class ScriptFile:
    path: str
    content_hash: str
    size: int
    source: Optional[str] = None
    truncated: bool = False
    unreadable: bool = False


@dataclass
class ReviewPlace:
    scope_type: str
    scope_id: str
    #: ``agent``, ``project`` or ``user`` (an acceptance).
    scope_kind: str
    scope_name: Optional[str]
    scope_missing: bool
    reviewed_by: str
    reviewer_name: Optional[str]
    reviewed_at: Optional[str]
    method: str
    state: str
    version: Optional[int]


@dataclass
class ReviewedVersion:
    scripts_hash: str
    is_current: bool
    switched_off: bool
    scripts: Dict[str, str]
    reviews: List[ReviewPlace] = field(default_factory=list)


@dataclass
class SwitchDetail:
    off: bool
    by: Optional[str] = None
    by_user_id: Optional[str] = None
    by_name: Optional[str] = None
    at: Optional[str] = None
    reason: Optional[str] = None
    hashes: List[str] = field(default_factory=list)


@dataclass
class ScriptDetail:
    skill_id: str
    display_name: str
    status: str
    owner_id: str
    author_name: Optional[str]
    admin_catalog: bool
    provenance: Optional[Dict[str, Any]]
    scripts_hash: Optional[str]
    current_switched_off: bool
    scripts: List[ScriptFile]
    versions: List[ReviewedVersion]
    switch: SwitchDetail


def _read_source(s3_key: str) -> Tuple[Optional[str], bool, bool]:
    """``(source, truncated, unreadable)`` for one script. Never raises."""
    from .resource_store import get_skill_resource_store

    try:
        data = get_skill_resource_store().get(s3_key)
    except Exception:  # noqa: BLE001 - one unreadable file never hides the rest
        logger.warning("admin skill scripts: a script's source could not be read")
        return None, False, True
    truncated = len(data) > SOURCE_MAX_BYTES
    return data[:SOURCE_MAX_BYTES].decode("utf-8", errors="replace"), truncated, False


async def _script_files(record: Any) -> List[ScriptFile]:
    from .resource_paths import manifest_path

    refs = sorted(
        (r for r in getattr(record, "resources", None) or [] if getattr(r, "kind", "") == st.SCRIPT_KIND),
        key=manifest_path,
    )
    sources = await asyncio.gather(*(asyncio.to_thread(_read_source, r.s3_key) for r in refs))
    return [
        ScriptFile(
            path=manifest_path(ref),
            content_hash=str(ref.content_hash or ""),
            size=int(ref.size or 0),
            source=source,
            truncated=truncated,
            unreadable=unreadable,
        )
        for ref, (source, truncated, unreadable) in zip(refs, sources)
    ]


async def _display_name(user_id: Optional[str]) -> Optional[str]:
    if not user_id or user_id == SYSTEM_OWNER_ID:
        return None
    try:
        from apis.shared.users.repository import UserRepository

        profile = await UserRepository().get_user(user_id)
    except Exception:  # noqa: BLE001 - a name is display only
        logger.warning("admin skill scripts: no display name for a user")
        return None
    return (profile.name or None) if profile else None


async def _assistant(assistant_id: str) -> Tuple[Optional[str], Optional[str], bool]:
    """``(name, kind, missing)`` for an Agent or project harness."""
    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        return None, None, True
    try:
        from apis.shared.assistants.service import _get_assistant_cloud_without_ownership_check

        assistant = await _get_assistant_cloud_without_ownership_check(assistant_id, table_name)
    except Exception:  # noqa: BLE001 - the name is display only
        logger.warning("admin skill scripts: an assistant could not be read")
        return None, None, True
    if assistant is None:
        return None, None, True
    return assistant.name, getattr(assistant, "kind", None), False


async def _names(ids: Iterable[str], lookup) -> Dict[str, Any]:
    unique = sorted({i for i in ids if i})
    found = await asyncio.gather(*(lookup(i) for i in unique))
    return dict(zip(unique, found))


def _review_places(rows: List[Dict[str, Any]], assistants: Dict[str, Any], people: Dict[str, Any]) -> Dict[str, List[ReviewPlace]]:
    by_hash: Dict[str, List[ReviewPlace]] = {}
    for row in rows:
        digest = str(row.get("scriptsHash") or row["SK"].rsplit("#", 1)[-1])
        scope_type = str(row.get("scopeType") or row["SK"].split("#")[1])
        scope_id = str(row.get("scopeId") or row["SK"].split("#")[2])
        if scope_type == st.SCOPE_ASSISTANT:
            name, kind, missing = assistants.get(scope_id, (None, None, True))
            scope_kind = "project" if kind == "project" else "agent"
        else:
            name, missing, scope_kind = people.get(scope_id), False, "user"
        version = row.get("version")
        reviewer = str(row.get("reviewedBy") or "")
        by_hash.setdefault(digest, []).append(
            ReviewPlace(
                scope_type=scope_type,
                scope_id=scope_id,
                scope_kind=scope_kind,
                scope_name=name,
                scope_missing=missing,
                reviewed_by=reviewer,
                reviewer_name=people.get(reviewer),
                reviewed_at=str(row["reviewedAt"]) if row.get("reviewedAt") else None,
                method=str(row.get("method") or "review"),
                state=str(row.get("state") or st.REVIEW_APPROVED),
                version=int(version) if version is not None else None,
            )
        )
    for places in by_hash.values():
        places.sort(key=lambda p: p.reviewed_at or "", reverse=True)
    return by_hash


async def load_script_detail(skill_id: str) -> Optional[ScriptDetail]:
    """Everything the admin page shows for one skill, or ``None`` when no such skill exists.

    Reads the skill row from the catalog table whoever owns it, which is why it
    sits behind its own scope (§8.10): ``admin.skills`` reads user-authored
    skills as 404 by design.
    """
    from .repository import get_skill_catalog_repository

    record = await get_skill_catalog_repository().get_skill(skill_id)
    if record is None:
        return None

    rows, switch_item, scripts = await asyncio.gather(
        asyncio.to_thread(query_review_rows, skill_id),
        asyncio.to_thread(read_switch_item, skill_id),
        _script_files(record),
    )
    switch = st.ScriptSwitch.from_item(switch_item) if switch_item else None

    assistant_ids = [str(r.get("scopeId") or "") for r in rows if str(r.get("scopeType") or "") == st.SCOPE_ASSISTANT]
    user_ids = [str(r.get("reviewedBy") or "") for r in rows]
    user_ids += [str(r.get("scopeId") or "") for r in rows if str(r.get("scopeType") or "") == st.SCOPE_USER]
    user_ids += [record.owner_id, switch.by_user_id if switch else ""]
    assistants, people = await asyncio.gather(_names(assistant_ids, _assistant), _names(user_ids, _display_name))

    digest = st.scripts_hash(record)
    places = _review_places(rows, assistants, people)
    scripts_by_hash = {str(r.get("scriptsHash") or ""): dict(r.get("scripts") or {}) for r in rows}
    hashes = set(places)
    if digest:
        hashes.add(digest)
    versions = [
        ReviewedVersion(
            scripts_hash=h,
            is_current=h == digest,
            switched_off=bool(switch and switch.applies_to(h)),
            scripts=scripts_by_hash.get(h) or (dict(st.script_entries(record)) if h == digest else {}),
            reviews=places.get(h, []),
        )
        for h in hashes
    ]
    # The live version first, then the most recently reviewed (two stable sorts).
    versions.sort(key=lambda v: (v.reviews[0].reviewed_at or "") if v.reviews else "", reverse=True)
    versions.sort(key=lambda v: not v.is_current)

    status = record.status.value if hasattr(record.status, "value") else str(record.status)
    owner = record.owner_id or SYSTEM_OWNER_ID
    return ScriptDetail(
        skill_id=record.skill_id,
        display_name=record.display_name,
        status=status,
        owner_id=owner,
        author_name=people.get(owner),
        admin_catalog=owner == SYSTEM_OWNER_ID,
        provenance=skill_provenance(record),
        scripts_hash=digest,
        current_switched_off=bool(switch and switch.applies_to(digest)),
        scripts=scripts,
        versions=versions,
        switch=SwitchDetail(
            off=bool(switch and switch.off),
            by=switch.by if switch else None,
            by_user_id=(switch.by_user_id or None) if switch else None,
            by_name=people.get(switch.by_user_id) if switch else None,
            at=str(switch_item.get("at")) if switch_item and switch_item.get("at") else None,
            reason=str(switch_item.get("reason")) if switch_item and switch_item.get("reason") else None,
            hashes=list(switch.hashes) if switch else [],
        ),
    )


__all__ = [
    "InvalidSwitchError",
    "MAX_SWITCH_HASHES",
    "ReviewPlace",
    "ReviewedVersion",
    "ScriptDetail",
    "ScriptFile",
    "SwitchDetail",
    "load_script_detail",
    "normalize_hashes",
    "query_review_rows",
    "read_switch_item",
    "set_admin_switch",
]
