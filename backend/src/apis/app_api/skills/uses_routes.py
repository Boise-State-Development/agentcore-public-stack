"""Where a user's skill is used, and moving behind pins to its current content
(docs/specs/skill-version-history.md D9, §6.3).

Two routes, owner-scoped and 404 after authentication while
``SKILL_VERSION_HISTORY_ENABLED`` is off:

* ``GET /skills/mine/{skill_id}/uses`` — the owner's Agents and projects that bind
  the skill, split into those running its current content and those pinned to an
  older version (``apis.shared.skills.uses``).
* ``POST /skills/mine/{skill_id}/uses/update`` — move the named pins to the
  current content, one target at a time, through each place's own pin operation
  (a project's ``pin_skill``, an Agent's ``pin_agent_skill``), which re-checks
  access. A target must be one ``uses`` lists as behind and updatable, so this
  route can't pin a binding that runs the live skill. One failing target is
  reported on its own row; the others still update.
"""

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apis.shared.skills.script_review_check import ScriptsUnreviewedError
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.skills.reach import SkillBinding
from apis.shared.skills.user_service import UserSkillError, get_user_skill_service
from apis.shared.skills.uses import classify_uses

from .routes import _user_skill_error
from .version_history_core import require_history_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])

MAX_TARGETS = 25

UseKind = Literal["agent", "project"]


class SkillUse(BaseModel):
    kind: UseKind
    # The Agent's id, or the project's for a project.
    id: str
    name: str
    pinned_version: Optional[int] = Field(None, alias="pinnedVersion")
    can_update: bool = Field(True, alias="canUpdate")
    # Runs for someone other than the owner.
    shared: bool = False
    # A store Agent: an update lands on its draft until it is resubmitted.
    published: bool = False

    model_config = {"populate_by_name": True}

    @classmethod
    def from_binding(cls, b: SkillBinding) -> "SkillUse":
        project = b.is_project and b.project_id
        return cls(
            kind="project" if project else "agent",
            id=b.project_id if project else b.agent_id,
            name=b.name,
            pinned_version=b.pinned_version,
            can_update=b.can_update,
            shared=b.shared,
            published=b.published and not b.is_project,
        )


class SkillUsesResponse(BaseModel):
    current: List[SkillUse]
    behind: List[SkillUse]
    others_behind: int = Field(0, alias="othersBehind")
    complete: bool = True

    model_config = {"populate_by_name": True}


class UseTarget(BaseModel):
    kind: UseKind
    id: str


class UpdateUsesRequest(BaseModel):
    targets: List[UseTarget] = Field(..., max_length=MAX_TARGETS)


class UpdateResult(BaseModel):
    kind: UseKind
    id: str
    ok: bool
    version: Optional[int] = None
    error: Optional[str] = None


class UpdateUsesResponse(BaseModel):
    results: List[UpdateResult]


async def _uses(skill_id: str, user: User):
    try:
        live = await get_user_skill_service().require_owned(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)
    return await classify_uses(live, user.user_id)


@router.get(
    "/mine/{skill_id}/uses", response_model=SkillUsesResponse, response_model_by_alias=True
)
async def get_my_skill_uses(
    skill_id: str, user: User = Depends(get_current_user_from_session)
) -> SkillUsesResponse:
    """The owner's Agents and projects that bind the skill, current and behind."""
    require_history_enabled()
    uses = await _uses(skill_id, user)
    return SkillUsesResponse(
        current=[SkillUse.from_binding(b) for b in uses.current],
        behind=[SkillUse.from_binding(b) for b in uses.behind],
        others_behind=uses.others_behind,
        complete=uses.complete,
    )


@router.post("/mine/{skill_id}/uses/update", response_model=UpdateUsesResponse)
async def update_my_skill_uses(
    skill_id: str,
    body: UpdateUsesRequest,
    user: User = Depends(get_current_user_from_session),
) -> UpdateUsesResponse:
    """Move each named pin to the skill's current content, reporting each target."""
    require_history_enabled()
    uses = await _uses(skill_id, user)
    behind = {(u.kind, u.id) for u in map(SkillUse.from_binding, uses.behind)}
    logger.info(
        f"User {scrub_log(user.email)} updating {len(body.targets)} pin(s) of {scrub_log(skill_id)}"
    )
    results: List[UpdateResult] = []
    for target in body.targets:
        if (target.kind, target.id) not in behind:
            results.append(
                UpdateResult(
                    kind=target.kind,
                    id=target.id,
                    ok=False,
                    error="This place doesn't pin an older version you can update.",
                )
            )
            continue
        results.append(await _update(target, skill_id, user))
    return UpdateUsesResponse(results=results)


async def _update(target: UseTarget, skill_id: str, user: User) -> UpdateResult:
    try:
        if target.kind == "project":
            version = await _pin_project(target.id, skill_id, user)
        else:
            version = await _pin_agent(target.id, skill_id, user)
    except HTTPException as e:
        return UpdateResult(kind=target.kind, id=target.id, ok=False, error=str(e.detail))
    except ScriptsUnreviewedError:
        # The new version's scripts need the review sheet, which a bulk update can't
        # show (skill-code-sandbox §8.5): the place keeps running what was reviewed.
        where = "project's skills" if target.kind == "project" else "agent's settings"
        return UpdateResult(
            kind=target.kind,
            id=target.id,
            ok=False,
            error=f"Its scripts changed. Update it from the {where} to review them.",
        )
    except Exception:  # noqa: BLE001 - one target never fails the others
        logger.error("Updating a skill pin failed", exc_info=True)
        return UpdateResult(
            kind=target.kind, id=target.id, ok=False, error="This one couldn't be updated. Try again."
        )
    return UpdateResult(kind=target.kind, id=target.id, ok=True, version=version)


async def _pin_project(project_id: str, skill_id: str, user: User) -> Optional[int]:
    """A project's Update (``HarnessSettingsService.pin_skill``): editor, versioned."""
    from apis.app_api.agent_designer.services.binding_validation import BindingValidationError
    from apis.app_api.projects.routes import _settings, _translate
    from apis.shared.projects.service import ProjectError
    from apis.shared.skills.pins import binding_pin, split_pinned_ref

    try:
        view = await _settings().pin_skill(project_id, user, skill_id)
    except ProjectError as e:
        raise _translate(e)
    except BindingValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    for binding in view.harness.bindings or []:
        if binding.kind == "skill" and split_pinned_ref(binding.ref)[0] == skill_id:
            return binding_pin(binding.config)
    return None


async def _pin_agent(agent_id: str, skill_id: str, user: User) -> int:
    from apis.app_api.agent_designer.services.skill_pins import (
        AgentSkillPinError,
        pin_agent_skill,
    )

    try:
        _, version = await pin_agent_skill(agent_id, user, skill_id)
    except AgentSkillPinError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return version
