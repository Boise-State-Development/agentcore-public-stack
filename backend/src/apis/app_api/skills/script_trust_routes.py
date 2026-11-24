"""Who allowed a skill's scripts, and the author's switch (docs/specs/skill-code-sandbox.md §8).

Two routes:

* ``GET /skills/{skill_id}/script-trust?assistantId=&hash=`` — what the tool rail
  and the badges show for a script run: the skill, its author, the version, and
  why it may run (own, imported, reviewed by whom and when, admin catalog), or
  that its scripts are off. Read when the rail entry is expanded, so a
  collapsed rail costs nothing. Display names only, never user ids.
* ``PUT /skills/mine/{skill_id}/scripts`` — the author turns their skill's
  scripts off or on, everywhere. An admin's switch can't be undone here.

Access for the read follows what the caller can already reach: with
``assistantId``, any Agent or project the caller can use that binds the skill;
without it, a skill the caller can reach directly (their own or the catalog).
Anything else is a 404, so the route never discloses a skill.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.skills import script_reviews
from apis.shared.skills import script_trust as st
from apis.shared.skills.models import SYSTEM_OWNER_ID, skill_provenance
from apis.shared.skills.pins import binding_pin, pinned_ref, split_pinned_ref
from apis.shared.skills.user_service import UserSkillError, get_user_skill_service

from .routes import _require_accessible_skill, _user_skill_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class ScriptSwitchRequest(BaseModel):
    off: bool
    reason: str = Field("", max_length=500)


class ScriptSwitchResponse(BaseModel):
    off: bool
    by: Optional[str] = None


class ScriptsOff(BaseModel):
    by: str
    #: When it was turned off, for "An administrator turned off this skill's scripts on {date}".
    at: Optional[str] = None


class ScriptTrustResponse(BaseModel):
    """The rail's and badge's view of one skill's scripts. Display names only."""

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    #: The author's name, or None for an admin-catalog skill.
    author: Optional[str] = None
    scripts_hash: Optional[str] = Field(None, alias="scriptsHash")
    version: Optional[str] = None
    #: ``own``, ``imported``, ``reviewed`` or ``admin``; None when nothing allows it.
    trusted_by: Optional[str] = Field(None, alias="trustedBy")
    #: ``review``, ``authored`` ("Shared by") or ``backfill``, for ``reviewed``.
    method: Optional[str] = None
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = Field(None, alias="reviewedAt")
    provenance: Optional[Dict[str, str]] = None
    scripts_off: Optional[ScriptsOff] = Field(None, alias="scriptsOff")

    model_config = {"populate_by_name": True}


async def _display_name(user_id: Optional[str]) -> Optional[str]:
    if not user_id or user_id == SYSTEM_OWNER_ID:
        return None
    try:
        from apis.shared.users.repository import UserRepository

        profile = await UserRepository().get_user(user_id)
    except Exception:  # noqa: BLE001 - a name is display only
        logger.warning("script trust: no display name for a user")
        return None
    return (profile.name or None) if profile else None


async def _bound_record(skill_id: str, assistant_id: str, user: User) -> Any:
    """The record the Agent or project runs for ``skill_id``, if the caller can use it."""
    from apis.shared.assistants.service import get_assistant_with_access_check, resolve_assistant_permission
    from apis.shared.skills.pinning import load_skill_records

    assistant, permission = await resolve_assistant_permission(assistant_id, user.user_id, user.email)
    if assistant is not None and permission is None:
        assistant, permission = await get_assistant_with_access_check(assistant_id, user.user_id, user.email)
    if assistant is None or permission is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    for binding in assistant.bindings or []:
        if binding.kind != "skill":
            continue
        bound_id, ref_version = split_pinned_ref(binding.ref)
        if bound_id != skill_id:
            continue
        records = await load_skill_records([pinned_ref(skill_id, binding_pin(binding.config) or ref_version)])
        if records:
            return records[0]
    raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")


def _trust(record: Any, user: User, review: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    owner = getattr(record, "owner_id", SYSTEM_OWNER_ID) or SYSTEM_OWNER_ID
    if owner == SYSTEM_OWNER_ID:
        return st.TRUSTED_ADMIN, None
    if owner == user.user_id:
        return (st.TRUSTED_IMPORTED if record.import_source is not None else st.TRUSTED_OWN), None
    if review and str(review.get("state") or st.REVIEW_APPROVED) == st.REVIEW_APPROVED:
        return st.TRUSTED_REVIEWED, str(review.get("method") or script_reviews.METHOD_REVIEW)
    return None, None


async def _trust_response(
    record: Any,
    user: User,
    assistant_id: Optional[str],
    digest: Optional[str],
    shown_version: Optional[str] = None,
) -> ScriptTrustResponse:
    """What the rail and the badges show for ``record``'s scripts at ``digest``."""
    review = None
    if assistant_id and digest:
        review = await asyncio.to_thread(script_reviews.read_review, record.skill_id, assistant_id, digest)
    switch = await asyncio.to_thread(script_reviews.read_switch, record.skill_id)
    trusted_by, method = _trust(record, user, review)
    if digest is None and trusted_by == st.TRUSTED_REVIEWED:
        trusted_by, method = None, None
    author, reviewer = await asyncio.gather(
        _display_name(record.owner_id),
        _display_name(str(review.get("reviewedBy")) if review and trusted_by == st.TRUSTED_REVIEWED else None),
    )
    off = switch is not None and switch.applies_to(digest)
    return ScriptTrustResponse(
        skill_id=record.skill_id,
        display_name=record.display_name,
        author=author,
        scripts_hash=digest,
        version=st.short_hash(digest) or shown_version or None,
        trusted_by=trusted_by,
        method=method,
        reviewer=reviewer,
        reviewed_at=str(review.get("reviewedAt")) if review and trusted_by == st.TRUSTED_REVIEWED else None,
        provenance=skill_provenance(record),
        scripts_off=ScriptsOff(by=switch.by, at=getattr(switch, "at", "") or None) if off else None,
    )


@router.get("/scripts/trust", response_model=ScriptTrustResponse, response_model_by_alias=True)
async def get_script_run_trust(
    name: str = Query(..., min_length=1, max_length=128),
    session_id: str = Query(..., alias="sessionId", min_length=1, max_length=128),
    version: Optional[str] = Query(None, max_length=st.SHORT_HASH_CHARS),
    user: User = Depends(get_current_user_from_session),
) -> ScriptTrustResponse:
    """Who let a script that ran in this conversation run: the rail's opened row (§8.6).

    ``name`` is the run's ``skill_name`` and ``version`` its ``Script version:``.
    When that version is not the one bound now (the pin moved since), the answer
    still names the skill and its author, and says nothing about a review it
    can't match. Declared before ``/{skill_id}/script-trust`` so ``scripts`` is
    never read as a skill id.
    """
    record, assistant_id = await _session_skill_record(name, session_id, user)
    digest = st.scripts_hash(record)
    matches = not version or (digest is not None and st.short_hash(digest) == version)
    return await _trust_response(record, user, assistant_id, digest if matches else None, version)


async def _session_skill_record(name: str, session_id: str, user: User) -> Tuple[Any, Optional[str]]:
    """The record a script run in this conversation named, and the assistant it ran as.

    The rail knows only what the model passed (``skill_name``, the skill's
    activation name) and the conversation. The conversation is looked up for the
    caller only, so another user's session is a 404. Its Agent or project's
    bindings are searched first, at the version they pin, then the skills the
    caller reaches directly (their own, imported or the catalog).
    """
    from apis.shared.assistants.service import get_assistant_with_access_check, resolve_assistant_permission
    from apis.shared.sessions.metadata import get_session_metadata
    from apis.shared.skills.access import resolve_accessible_skill_ids
    from apis.shared.skills.bundle import slugify_skill_name
    from apis.shared.skills.pinning import load_skill_records

    not_found = HTTPException(status_code=404, detail="Skill not found")
    session = await get_session_metadata(session_id, user.user_id)
    if session is None:
        raise not_found
    wanted = name.strip()
    assistant_id = getattr(session, "assistant_id", None)
    if assistant_id:
        assistant, permission = await resolve_assistant_permission(assistant_id, user.user_id, user.email)
        if assistant is not None and permission is None:
            assistant, permission = await get_assistant_with_access_check(assistant_id, user.user_id, user.email)
        bindings = (assistant.bindings or []) if assistant is not None and permission is not None else []
        for binding in bindings:
            if binding.kind != "skill":
                continue
            skill_id, ref_version = split_pinned_ref(binding.ref)
            if slugify_skill_name(skill_id) != wanted:
                continue
            records = await load_skill_records([pinned_ref(skill_id, binding_pin(binding.config) or ref_version)])
            if records:
                return records[0], assistant_id
    for skill_id in sorted(await resolve_accessible_skill_ids(user)):
        if slugify_skill_name(skill_id) == wanted:
            return await _require_accessible_skill(skill_id, user), None
    raise not_found


@router.get("/{skill_id}/script-trust", response_model=ScriptTrustResponse, response_model_by_alias=True)
async def get_script_trust(
    skill_id: str,
    assistant_id: Optional[str] = Query(None, alias="assistantId", max_length=128),
    scripts_hash: Optional[str] = Query(None, alias="hash", max_length=64),
    user: User = Depends(get_current_user_from_session),
) -> ScriptTrustResponse:
    """Who allowed this skill's scripts, for the rail and the badges (§8.6)."""
    if assistant_id:
        record = await _bound_record(skill_id, assistant_id, user)
    else:
        record = await _require_accessible_skill(skill_id, user)
    return await _trust_response(record, user, assistant_id, scripts_hash or st.scripts_hash(record))


@router.put("/mine/{skill_id}/scripts", response_model=ScriptSwitchResponse)
async def set_my_skill_scripts(
    skill_id: str,
    body: ScriptSwitchRequest,
    user: User = Depends(get_current_user_from_session),
) -> ScriptSwitchResponse:
    """The author turns their skill's scripts off or on, everywhere it runs (§8.4)."""
    try:
        await get_user_skill_service().require_owned(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)
    try:
        switch = await script_reviews.set_author_switch(skill_id, user, off=body.off, reason=body.reason)
    except script_reviews.ScriptSwitchLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ScriptSwitchResponse(off=switch.off, by=switch.by if switch.off else None)
