"""Moving an Agent's skill pin to the skill's current content
(docs/specs/skill-version-history.md D9).

Until now an Agent's pin moved only if someone turned the skill off and on again in
the Agent form, and nothing said a newer version existed. This is the Agent twin of
a project's ``HarnessSettingsService.pin_skill`` (the "Update" on its Skills page):

* ``pin_agent_skill`` re-pins one bound, pinned skill, for the Agent's owner or an
  editor, after checking the caller may still bind the skill;
* ``agent_skill_pins`` reports each pinned skill binding's version and whether the
  live skill has moved on, for the Agent form.

A project harness is refused: it is edited through its project, which versions
every save. A store Agent's update lands on its draft, like any edit; users run
the published snapshot until the owner resubmits.

An Update that moves the pin onto scripts nobody has reviewed for this Agent
raises ``ScriptsUnreviewedError`` before the pin is stored, unless the caller
allows them (``scripts_reviewed``); a review it carries is recorded
(skill-code-sandbox §8.3, §8.5). The routes are not behind version history's
flag: every Agent binding of a skill with scripts is pinned, so its owner needs
Update wherever scripts run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from apis.shared.assistants.models import AgentBinding, Assistant
from apis.shared.assistants.service import (
    PROJECT_HARNESS_EDIT_MESSAGE,
    is_project_harness,
    resolve_assistant_permission,
    update_assistant,
)
from apis.shared.auth.models import User
from apis.shared.skills.pinning import SkillPinError, pin_current, pin_status
from apis.shared.skills.pins import PIN_CONFIG_KEY, binding_pin, split_pinned_ref
from apis.shared.skills.resource_store import SkillResourceStoreError
from apis.shared.skills.script_review_check import require_script_reviews
from apis.shared.skills.script_reviews import record_binding_reviews

from .binding_validation import BindingValidationError, validate_agent_write

logger = logging.getLogger(__name__)

SKILL = "skill"


class AgentSkillPinError(Exception):
    """A pin that can't move, with the HTTP status a route should answer."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AgentSkillPin:
    skill_id: str
    version: int
    update_available: bool


def _skill_binding(assistant: Assistant, skill_id: str) -> Optional[AgentBinding]:
    for binding in assistant.bindings or []:
        if binding.kind == SKILL and split_pinned_ref(binding.ref)[0] == skill_id:
            return binding
    return None


async def pin_agent_skill(
    agent_id: str, user: User, skill_id: str, *, scripts_reviewed: Optional[Dict[str, str]] = None
) -> Tuple[Assistant, int]:
    """Move ``agent_id``'s pin on ``skill_id`` to the skill's current content.

    Returns the updated Agent and the version it now pins. Pinning an unchanged
    skill reuses its latest version, so it is a no-op in effect.

    Raises:
        AgentSkillPinError: 404 no such Agent or binding; 403 not the owner or an
            editor; 409 a project harness, or a binding that is not pinned (it
            already runs the current version); the binding validator's status when
            the caller may no longer bind the skill; 503 when its files couldn't
            be frozen. ``ScriptsUnreviewedError`` when the new version's
            scripts need a review (§8.5).
    """
    assistant, permission = await resolve_assistant_permission(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if not assistant:
        raise AgentSkillPinError(f"Agent not found: {agent_id}", 404)
    if permission not in ("owner", "editor"):
        raise AgentSkillPinError("You do not have permission to edit this agent", 403)
    if is_project_harness(assistant):
        raise AgentSkillPinError(PROJECT_HARNESS_EDIT_MESSAGE, 409)
    binding = _skill_binding(assistant, skill_id)
    if binding is None:
        raise AgentSkillPinError("That skill is not one of this agent's skills", 404)
    if binding_pin(binding.config) is None and split_pinned_ref(binding.ref)[1] is None:
        raise AgentSkillPinError("This agent already runs the skill's current version", 409)

    try:
        await validate_agent_write(user, bindings=[AgentBinding(kind=SKILL, ref=skill_id)])
    except BindingValidationError as e:
        raise AgentSkillPinError(e.message, e.status_code) from e
    try:
        version = await pin_current(skill_id, pinned_by=user.user_id)
    except SkillPinError as e:
        raise AgentSkillPinError(str(e), 404) from e
    except SkillResourceStoreError as e:
        logger.error("Could not freeze a skill's files to move an agent's pin: %s", e)
        raise AgentSkillPinError(
            "This skill's files couldn't be saved for the new version. Try again.", 503
        ) from e

    bindings = [
        AgentBinding(
            kind=SKILL,
            ref=skill_id,
            config={**(b.config or {}), PIN_CONFIG_KEY: version.version},
        )
        if b is binding
        else b
        for b in assistant.bindings or []
    ]
    await require_script_reviews(
        agent_id, assistant.bindings, bindings, user.user_id, scripts_reviewed=scripts_reviewed
    )
    updated = await update_assistant(
        assistant_id=agent_id, owner_id=assistant.owner_id, bindings=bindings
    )
    if not updated:
        raise AgentSkillPinError(f"Agent not found: {agent_id}", 404)
    await record_binding_reviews(
        agent_id, assistant.bindings, updated.bindings, user, scripts_reviewed=scripts_reviewed
    )
    logger.info(
        "Moved an agent's skill pin",
        extra={"event": "agent_skill_pin_moved", "agent_id": agent_id, "version": version.version},
    )
    return updated, version.version or 0


async def agent_skill_pins(assistant: Assistant) -> List[AgentSkillPin]:
    """Each pinned skill binding's version, and whether the live skill has moved on."""
    pairs: List[Tuple[str, int]] = []
    for binding in assistant.bindings or []:
        if binding.kind != SKILL:
            continue
        skill_id, ref_pin = split_pinned_ref(binding.ref)
        pinned = binding_pin(binding.config) or ref_pin
        if pinned:
            pairs.append((skill_id, pinned))
    if not pairs:
        return []
    statuses = await pin_status(pairs)
    return [
        AgentSkillPin(
            skill_id=skill_id,
            version=version,
            update_available=statuses[skill_id].update_available if skill_id in statuses else False,
        )
        for skill_id, version in pairs
    ]
