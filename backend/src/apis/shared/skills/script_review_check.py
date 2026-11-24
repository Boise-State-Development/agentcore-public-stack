"""The review a save must carry before it binds someone else's scripts (skill-code-sandbox §8.5).

An Agent save, a project skills save and a project Update call
:func:`require_script_reviews` after the bindings are pinned and before anything
is stored. It raises :class:`ScriptsUnreviewedError`, which the route answers as
``409`` with ``code: skill_scripts_unreviewed``, when a binding this save adds
or moves:

- pins a skill **with scripts** that isn't an admin-catalog skill,
- whose scripts the binder **didn't write by hand** (``review_method`` is not
  ``authored``: an imported skill, or someone else's),
- that has **no approved review** for this assistant at that version's hash,
- and that the save neither allowed (``scriptsReviewed: {skillId: hash}``, the
  exact hash) nor declined (``scriptsDeclined: [skillId]``, the sheet's "Add
  without scripts").

Only bindings the save **adds or moves** are checked. A binding left as it was
never asks again. That is what makes "Add without scripts" stick, and it keeps
an unrelated edit (a description) from re-opening the sheet. A binding that is
not reviewed still shows "Scripts not allowed · Review" on its row
(:func:`binding_script_status`), and re-saving it with ``scriptsReviewed``
records the review (PR-2c).

:func:`binding_script_status` is what the binding rows and the badges read: per
bound skill with scripts, whether its scripts run for other people and why.
Nothing here runs on the turn path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import script_reviews
from . import script_trust as st
from .models import SYSTEM_OWNER_ID, skill_provenance
from .pins import binding_adder, binding_pin, pinned_ref, split_pinned_ref
from .resource_paths import manifest_path

logger = logging.getLogger(__name__)

#: ``detail.code`` of the 409. The SPA shows the review sheet and resends.
SCRIPTS_UNREVIEWED_CODE = "skill_scripts_unreviewed"
#: The request field for "Add without scripts": skill ids bound with no review.
SCRIPTS_DECLINED_FIELD = "scriptsDeclined"

CHANGE_NEW = "new"
CHANGE_CHANGED = "changed"
CHANGE_REMOVED = "removed"
CHANGE_SAME = "same"


class ScriptsUnreviewedError(Exception):
    """A save binds scripts nobody has reviewed for this assistant (§8.5)."""

    def __init__(self, skills: List[Dict[str, Any]]) -> None:
        self.skills = skills
        names = ", ".join(s["displayName"] for s in skills)
        super().__init__(f"Review the scripts in {names} before saving.")

    def detail(self) -> Dict[str, Any]:
        return {"code": SCRIPTS_UNREVIEWED_CODE, "message": str(self), "skills": self.skills}


def _pinned(bindings: Optional[Iterable[Any]]) -> Dict[str, Tuple[int, Any]]:
    """``skill_id -> (version, binding)`` for every pinned skill binding."""
    out: Dict[str, Tuple[int, Any]] = {}
    for binding in bindings or []:
        if getattr(binding, "kind", None) != "skill":
            continue
        skill_id, ref_version = split_pinned_ref(binding.ref)
        version = binding_pin(binding.config) or ref_version
        if version:
            out[skill_id] = (int(version), binding)
    return out


def _script_sizes(record: Any) -> Dict[str, int]:
    return {
        manifest_path(ref): int(getattr(ref, "size", 0) or 0)
        for ref in getattr(record, "resources", None) or []
        if getattr(ref, "kind", "") == st.SCRIPT_KIND
    }


def _other_files(record: Any) -> List[Dict[str, Any]]:
    """The skill's other files, which a script may read (§8.2: the sheet lists them beside it)."""
    return sorted(
        (
            {"path": manifest_path(ref), "size": int(getattr(ref, "size", 0) or 0)}
            for ref in getattr(record, "resources", None) or []
            if getattr(ref, "kind", "") != st.SCRIPT_KIND
        ),
        key=lambda f: f["path"],
    )


def script_changes(record: Any, previous: Optional[Any]) -> List[Dict[str, Any]]:
    """Each script of ``record`` (and each one ``previous`` had that it lacks), marked.

    Without ``previous`` (a first binding) every script is ``new``. Sorted by
    path, removed scripts last, so the sheet reads in a stable order.
    """
    now = dict(st.script_entries(record))
    sizes = _script_sizes(record)
    before = dict(st.script_entries(previous)) if previous is not None else {}
    rows: List[Dict[str, Any]] = []
    for path in sorted(now):
        if previous is None or path not in before:
            change = CHANGE_NEW
        elif before[path] != now[path]:
            change = CHANGE_CHANGED
        else:
            change = CHANGE_SAME
        rows.append({"path": path, "size": sizes.get(path, 0), "change": change})
    if previous is not None:
        old_sizes = _script_sizes(previous)
        for path in sorted(set(before) - set(now)):
            rows.append({"path": path, "size": old_sizes.get(path, 0), "change": CHANGE_REMOVED})
    return rows


async def _records(refs: Sequence[str]) -> Dict[str, Any]:
    """Records by the pinned ref (``id@n``) they were loaded for.

    Every caller passes refs naming distinct skills, so a record pairs with its
    ref by skill id (``load_skill_records`` leaves out a missing skill or version).
    """
    from .pinning import load_skill_records

    if not refs:
        return {}
    by_id = {split_pinned_ref(ref)[0]: ref for ref in refs}
    return {by_id[r.skill_id]: r for r in await load_skill_records(list(refs)) if r.skill_id in by_id}


async def unreviewed_script_skills(
    assistant_id: Optional[str],
    before: Optional[Iterable[Any]],
    after: Optional[Iterable[Any]],
    binder_id: str,
    *,
    scripts_reviewed: Optional[Mapping[str, str]] = None,
    scripts_declined: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """The 409 body's ``skills``: bindings this save adds or moves that need a review.

    ``assistant_id`` is ``None`` for an Agent being created (it has no reviews).
    ``binder_id`` is the saver; a project binding's ``addedBy`` wins over it, as
    in :func:`script_reviews.record_binding_reviews`.
    """
    if after is None:
        return []
    allowed = dict(scripts_reviewed or {})
    declined: Set[str] = set(scripts_declined or ())
    old = _pinned(before)
    moved = {
        sid: (version, binding)
        for sid, (version, binding) in _pinned(after).items()
        if old.get(sid, (None, None))[0] != version and sid not in declined
    }
    if not moved:
        return []

    refs = [pinned_ref(sid, v) for sid, (v, _) in sorted(moved.items())]
    previous_refs = [pinned_ref(sid, old[sid][0]) for sid in sorted(moved) if sid in old]
    records, previous = await asyncio.gather(_records(refs), _records(previous_refs))

    candidates: List[Tuple[Any, str, int, Optional[int]]] = []
    for sid, (version, binding) in sorted(moved.items()):
        record = records.get(pinned_ref(sid, version))
        if record is None:
            continue
        digest = st.scripts_hash(record)
        if digest is None or not script_reviews.needs_review(record):
            continue
        binder = binding_adder(binding.config) or binder_id
        if script_reviews.review_method(record, binder) is not None:
            continue
        if allowed.get(sid) == digest:
            continue
        from_version = old[sid][0] if sid in old else None
        candidates.append((record, digest, version, from_version))
    if not candidates:
        return []

    existing: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if assistant_id:
        keys: List[Tuple[str, str]] = []
        for record, digest, _, from_version in candidates:
            keys.append((st.skill_pk(record.skill_id), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, digest)))
            prior = previous.get(pinned_ref(record.skill_id, from_version)) if from_version else None
            prior_digest = st.scripts_hash(prior) if prior is not None else None
            if prior_digest:
                keys.append((st.skill_pk(record.skill_id), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, prior_digest)))
        existing = await asyncio.to_thread(script_reviews._existing_reviews, keys)

    skills: List[Dict[str, Any]] = []
    for record, digest, version, from_version in candidates:
        key = (st.skill_pk(record.skill_id), st.review_sk(st.SCOPE_ASSISTANT, assistant_id or "", digest))
        if assistant_id and _approved(existing.get(key)):
            continue
        prior = previous.get(pinned_ref(record.skill_id, from_version)) if from_version else None
        prior_digest = st.scripts_hash(prior) if prior is not None else None
        prior_review = None
        if assistant_id and prior_digest:
            prior_review = existing.get(
                (st.skill_pk(record.skill_id), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, prior_digest))
            )
        # Changes are shown against a version someone reviewed; against anything
        # else, every script is new to the reviewer.
        compared = prior if _approved(prior_review) else None
        skills.append(
            {
                "skillId": record.skill_id,
                "displayName": record.display_name,
                "scriptsHash": digest,
                "version": version,
                "fromVersion": from_version if compared is not None else None,
                "reviewedAt": str(prior_review.get("reviewedAt")) if compared is not None and prior_review else None,
                "provenance": skill_provenance(record),
                "authorId": None if record.owner_id == SYSTEM_OWNER_ID else record.owner_id,
                "scripts": script_changes(record, compared),
                "files": _other_files(record),
            }
        )
    return skills


def _approved(review: Optional[Mapping[str, Any]]) -> bool:
    return bool(review) and str(review.get("state") or st.REVIEW_APPROVED) == st.REVIEW_APPROVED


async def _display_names(user_ids: Iterable[Optional[str]]) -> Dict[str, Optional[str]]:
    """Display names by user id. A response carries these, never the ids."""
    from apis.shared.users.repository import UserRepository

    ids = sorted({i for i in user_ids if i and i != SYSTEM_OWNER_ID})
    if not ids:
        return {}
    repo = UserRepository()

    async def one(user_id: str) -> Tuple[str, Optional[str]]:
        try:
            profile = await repo.get_user(user_id)
        except Exception:  # noqa: BLE001 - a name is display only
            logger.warning("script review: no display name for a user")
            return user_id, None
        return user_id, (profile.name or None) if profile else None

    return dict(await asyncio.gather(*(one(i) for i in ids)))


async def _with_author_names(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Swap ``authorId`` for the author's display name: the body never carries user ids."""
    names = await _display_names(s.get("authorId") for s in skills)
    out = []
    for skill in skills:
        copy = {k: v for k, v in skill.items() if k != "authorId"}
        copy["author"] = names.get(skill.get("authorId") or "")
        out.append(copy)
    return out


async def require_script_reviews(
    assistant_id: Optional[str],
    before: Optional[Iterable[Any]],
    after: Optional[Iterable[Any]],
    binder_id: str,
    *,
    scripts_reviewed: Optional[Mapping[str, str]] = None,
    scripts_declined: Optional[Iterable[str]] = None,
) -> None:
    """Raise :class:`ScriptsUnreviewedError` when the save needs the review sheet first."""
    skills = await unreviewed_script_skills(
        assistant_id,
        before,
        after,
        binder_id,
        scripts_reviewed=scripts_reviewed,
        scripts_declined=scripts_declined,
    )
    if skills:
        raise ScriptsUnreviewedError(await _with_author_names(skills))


# ---------------------------------------------------------------------------
# Binding rows and badges
# ---------------------------------------------------------------------------

STATE_ALLOWED = "allowed"  # an approved review of someone else's (or imported) scripts
STATE_AUTHORED = "authored"  # the binder's own hand-written scripts ("Shared by")
STATE_ADMIN = "admin"  # an admin-catalog skill
STATE_NOT_ALLOWED = "not_allowed"
STATE_OFF_BY_AUTHOR = "off_by_author"
STATE_OFF_BY_ADMIN = "off_by_admin"
STATE_UNREVIEWABLE = "unreviewable"

#: States in which the scripts run for the people who use the assistant.
RUNNABLE_STATES = frozenset({STATE_ALLOWED, STATE_AUTHORED, STATE_ADMIN})


@dataclass
class BindingScripts:
    """One bound skill with scripts: whether they run for other people, and why."""

    skill_id: str
    display_name: str
    version: Optional[int]
    scripts_hash: Optional[str]
    state: str
    method: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    provenance: Optional[Dict[str, Any]] = None
    #: The live skill's scripts differ from the pinned version's (an Update would ask again).
    scripts_changed: bool = False
    scripts: List[Dict[str, Any]] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)
    author_id: Optional[str] = None


def _read_rows(keys: List[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return script_reviews._existing_reviews(keys)


async def binding_script_status(assistant_id: str, bindings: Optional[Iterable[Any]]) -> List[BindingScripts]:
    """Per bound skill with scripts, its state for this assistant. One BatchGetItem."""
    from .repository import get_skill_catalog_repository

    skill_bindings: List[Tuple[str, Optional[int], Any]] = []
    for binding in bindings or []:
        if getattr(binding, "kind", None) != "skill":
            continue
        skill_id, ref_version = split_pinned_ref(binding.ref)
        skill_bindings.append((skill_id, binding_pin(binding.config) or ref_version, binding))
    if not skill_bindings:
        return []

    refs = [pinned_ref(sid, v) for sid, v, _ in skill_bindings]
    records = await _records(refs)
    live_rows = await get_skill_catalog_repository().batch_get_skills(
        list(dict.fromkeys(sid for sid, _, _ in skill_bindings))
    )
    live = {row.skill_id: row for row in live_rows}

    scripted: List[Tuple[str, Optional[int], Any, Any, Optional[str]]] = []
    for sid, version, binding in skill_bindings:
        record = records.get(pinned_ref(sid, version))
        if record is None or not st.has_scripts(record):
            continue
        scripted.append((sid, version, binding, record, st.scripts_hash(record)))
    if not scripted:
        return []

    keys: List[Tuple[str, str]] = []
    for sid, _, _, _, digest in scripted:
        keys.append((st.skill_pk(sid), st.SWITCH_SK))
        if digest:
            keys.append((st.skill_pk(sid), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, digest)))
    rows = await asyncio.to_thread(_read_rows, keys)

    out: List[BindingScripts] = []
    for sid, version, binding, record, digest in scripted:
        switch_item = rows.get((st.skill_pk(sid), st.SWITCH_SK))
        switch = st.ScriptSwitch.from_item(switch_item) if switch_item else None
        review = rows.get((st.skill_pk(sid), st.review_sk(st.SCOPE_ASSISTANT, assistant_id, digest))) if digest else None
        live_row = live.get(sid)
        live_digest = st.scripts_hash(live_row) if live_row is not None else None
        entry = BindingScripts(
            skill_id=sid,
            display_name=record.display_name,
            version=version,
            scripts_hash=digest,
            state=STATE_NOT_ALLOWED,
            provenance=skill_provenance(record),
            scripts_changed=bool(version) and live_digest is not None and live_digest != digest,
            scripts=script_changes(record, None),
            files=_other_files(record),
            author_id=None if record.owner_id == SYSTEM_OWNER_ID else record.owner_id,
        )
        if switch is not None and switch.applies_to(digest):
            entry.state = STATE_OFF_BY_ADMIN if switch.by == st.SWITCH_BY_ADMIN else STATE_OFF_BY_AUTHOR
        elif record.owner_id == SYSTEM_OWNER_ID:
            entry.state = STATE_ADMIN
        elif digest is None:
            entry.state = STATE_UNREVIEWABLE
        elif _approved(review):
            entry.method = str(review.get("method") or script_reviews.METHOD_REVIEW)
            entry.state = STATE_AUTHORED if entry.method == script_reviews.METHOD_AUTHORED else STATE_ALLOWED
            entry.reviewed_by = str(review.get("reviewedBy") or "") or None
            entry.reviewed_at = str(review.get("reviewedAt") or "") or None
        out.append(entry)
    out.sort(key=lambda e: (e.display_name.lower(), e.skill_id))
    return out


async def script_status_view(entries: List[BindingScripts], *, full: bool) -> List[Dict[str, Any]]:
    """The wire form of :func:`binding_script_status`, with display names for ids.

    ``full`` is for someone who can edit the assistant: every entry, with its
    script and file lists. Anyone else (a member) sees only the skills whose
    scripts run for them, which is what the badge says (§8.6).
    """
    shown = entries if full else [e for e in entries if e.state in RUNNABLE_STATES]
    names = await _display_names([e.author_id for e in shown] + [e.reviewed_by for e in shown])
    out: List[Dict[str, Any]] = []
    for e in shown:
        row: Dict[str, Any] = {
            "skillId": e.skill_id,
            "displayName": e.display_name,
            "version": e.version,
            "scriptsHash": e.scripts_hash,
            "state": e.state,
            "method": e.method,
            "author": names.get(e.author_id or ""),
            "reviewer": names.get(e.reviewed_by or ""),
            "reviewedAt": e.reviewed_at,
            "provenance": e.provenance,
        }
        if full:
            row["scriptsChanged"] = e.scripts_changed
            row["scripts"] = e.scripts
            row["files"] = e.files
        out.append(row)
    return out


__all__ = [
    "BindingScripts",
    "RUNNABLE_STATES",
    "SCRIPTS_DECLINED_FIELD",
    "SCRIPTS_UNREVIEWED_CODE",
    "ScriptsUnreviewedError",
    "binding_script_status",
    "require_script_reviews",
    "script_changes",
    "script_status_view",
    "unreviewed_script_skills",
]
