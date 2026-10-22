"""What sharing an Agent gives away — the share dialog's half of D7.1.

Invoke-through (Skills v2 §6/D7) means a skill the Agent's owner wrote and bound resolves
for anyone who can open the Agent, and a person who can run a skill can get the model to
show its instructions. Sharing an Agent, or leaving it PUBLIC, therefore shares those
skills too. The marketplace submit dialog has always said so; this module lets the share
dialog say the same thing, from the same rule.

The answer is ``listing_service.exposed_skills`` — the helper the submit dialog and
``submit_listing`` already use — applied to the configuration a *share recipient* runs.
That is the one thing this module adds: a recipient of a published Agent runs the
published snapshot, not the owner's draft (``resolve_invocation_agent``), so the draft's
bindings are not what sharing exposes today. A skill bound only on the draft reaches
recipients when a new version is approved, and the submit dialog discloses it then.

⚠️ This is a read-out for the owner, not an access decision. Nothing here touches
``resolve_accessible_skill_ids`` (the viewer's own picker) or ``resolve_invocable_skill_ids``
(the turn path), and nothing here widens either.
"""

import logging
from typing import List

from apis.app_api.agent_designer.services.listing_service import exposed_skills
from apis.shared.assistants.models import Assistant, SkillExposure
from apis.shared.assistants.service import is_project_harness
from apis.shared.assistants.version_resolution import (
    AgentVersionUnavailableError,
    resolve_invocation_agent,
)

logger = logging.getLogger(__name__)


async def skills_exposed_by_sharing(assistant: Assistant) -> List[SkillExposure]:
    """The owner-authored skills anyone the Agent is shared with can use (§6/D7).

    The caller has already established that the requester owns ``assistant``. The skills
    named are the owner's own by construction, so the list tells the owner nothing about
    anyone else's work.

    A project's harness has no shares (its audience is the project's membership), so it
    exposes nothing through this door.

    If a published snapshot cannot be read, the live record is disclosed instead. The turn
    path refuses to run in that state, so no recipient can use anything right now — but
    over-disclosing to the owner is the safe direction for a warning, and the draft is the
    best available answer to "what will they get once it loads".
    """
    if is_project_harness(assistant):
        return []
    try:
        # ``user_id=None`` asks the question as a recipient: only the owner runs the draft.
        shared_view, _ = await resolve_invocation_agent(assistant, None)
    except AgentVersionUnavailableError:
        logger.warning(
            "Published snapshot unavailable; disclosing the live record's skills instead",
            exc_info=True,
        )
        shared_view = assistant
    return await exposed_skills(shared_view)
