"""Which Agent a voice session runs, and what it brings with it (#625).

A voice session on an Agent's conversation used to run the generic assistant:
the config frame named no Agent and ``VoiceAgent`` was built with no system
prompt. This module decides the Agent and resolves it the way a text turn does,
so access checks, published versions, refusals and tool bindings cannot drift
between the two surfaces:

* **Which Agent.** The Agent saved on the session wins over the one the browser
  names. The browser's id is only a claim, used to bind a conversation that has
  no messages yet, the same rule a text turn's binding follows.
* **Access, version, tools.** :func:`apis.inference_api.chat.routes.resolve_turn_agent`,
  the resolver every text turn calls. Its refusals come back as a
  ``StreamingResponse`` that voice never iterates, so nothing is persisted.
* **Prompt.** :func:`apis.inference_api.chat.routes.compose_turn_system_prompt`,
  so voice composes the same Agent prompt a text turn would, with the user's
  personal instructions below the Agent's.
* **Knowledge base.** Whole, under voice's own ceiling
  (:func:`apis.shared.assistants.kb_full_context.voice_max_tokens`). Voice has no
  retrieval step, so a knowledge base reaches it whole or not at all.

Voice mode must be switched on for the Agent (``voice_enabled``), and is not
offered for a Shared Project's harness.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from apis.shared.auth.models import User
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

#: The ``code`` on a ``bidi_error`` frame that ends the session because the
#: Agent cannot run in voice. The SPA shows ``message`` and leaves voice mode.
AGENT_VOICE_UNAVAILABLE_CODE = "agent_voice_unavailable"

AGENT_VOICE_OFF_MESSAGE = "This agent isn't available in voice mode."
AGENT_VOICE_REFUSED_MESSAGE = "This agent isn't available to you right now."
PROJECT_VOICE_MESSAGE = "Voice mode isn't available in project conversations yet."

#: Longest Agent id accepted from the config frame. Agent ids are short
#: generated strings; anything longer is not one.
_MAX_AGENT_ID_CHARS = 128


class VoiceAgentRefused(Exception):
    """The session's Agent cannot run in voice; ``message`` is for the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class VoiceAgentPlan:
    """What a voice session on an Agent runs with."""

    assistant_id: str
    system_prompt: Optional[str]
    knowledge_context: Optional[str]
    #: Append the built-in voice guidelines (the author turned them on).
    voice_guidelines: bool
    #: The Agent's tool ids, or ``None`` when the Agent surface is off and the
    #: request's tools apply as for a plain voice chat.
    tool_ids: Optional[List[str]]
    #: The Agent binds its own tools, so admin pins are not added (D4).
    owns_toolset: bool


def clean_agent_claim(value: Any) -> Optional[str]:
    """The config frame's ``agent_id``, or ``None`` when it is absent or malformed."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_AGENT_ID_CHARS:
        return None
    return value


def session_agent_id(existing_metadata: Any, claimed_agent_id: Optional[str]) -> Optional[str]:
    """The Agent this voice session runs, from the session row and the browser's claim.

    The session's saved Agent wins: the browser cannot move a conversation to
    another Agent, or out of one, by naming a different id. A session with no
    saved Agent takes the claim only while it has no messages, which is when a
    text turn would bind it too. A session with no row yet is new, so the claim
    binds it.
    """
    if existing_metadata is None:
        return claimed_agent_id

    prefs = getattr(existing_metadata, "preferences", None)
    stored = getattr(prefs, "assistant_id", None) if prefs is not None else None
    if stored:
        if claimed_agent_id and claimed_agent_id != stored:
            logger.warning("Voice config named a different agent than the session's; using the session's")
        return stored

    if claimed_agent_id and not (getattr(existing_metadata, "message_count", 0) or 0):
        return claimed_agent_id
    if claimed_agent_id:
        logger.warning("Voice config named an agent for a conversation that already has messages; ignoring it")
    return None


def _cancel(task: Optional["asyncio.Task[Any]"]) -> None:
    if task is not None and not task.done():
        task.cancel()


async def resolve_voice_agent(
    session_id: str, user: User, assistant_id: str, *, personal_instructions: Optional[str] = None
) -> VoiceAgentPlan:
    """Resolve ``assistant_id`` for a voice session, or raise :class:`VoiceAgentRefused`.

    Same access check, version resolution, project gate and tool bindings as a
    text turn (``resolve_turn_agent``), then the voice-only rules: the Agent has
    voice switched on, and it is not a project harness. ``personal_instructions``
    go below the Agent's, as on a text turn (#2020).
    """
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    from apis.inference_api.chat.models import InvocationRequest
    from apis.inference_api.chat.routes import compose_turn_system_prompt, resolve_turn_agent
    from apis.shared.assistants.kb_full_context import voice_max_tokens

    request = InvocationRequest(session_id=session_id, rag_assistant_id=assistant_id)
    try:
        resolved = await resolve_turn_agent(
            request,
            user,
            user.user_id,
            is_continuation=False,
            kb_budget_tokens=voice_max_tokens(),
        )
    except HTTPException as exc:
        logger.warning("Voice agent %s could not be resolved: %s", scrub_log(assistant_id), exc.status_code)
        raise VoiceAgentRefused(AGENT_VOICE_REFUSED_MESSAGE) from exc

    if isinstance(resolved, StreamingResponse):
        # The text path's conversational refusal (no access, deleted, blocked
        # binding, archived project). Never iterated, so nothing is persisted.
        logger.info("Voice agent %s refused by the turn resolver", scrub_log(assistant_id))
        raise VoiceAgentRefused(AGENT_VOICE_REFUSED_MESSAGE)

    def _cancel_all() -> None:
        for task in (
            resolved.kb_full_context_task,
            resolved.project_files_note_task,
            resolved.project_memory_task,
        ):
            _cancel(task)

    if resolved.project_id:
        _cancel_all()
        raise VoiceAgentRefused(PROJECT_VOICE_MESSAGE)

    assistant = resolved.assistant
    # Read from what runs for this caller: a published Agent's reviewed
    # version, so turning voice on for one goes through review.
    if not getattr(assistant, "voice_enabled", False):
        _cancel_all()
        raise VoiceAgentRefused(AGENT_VOICE_OFF_MESSAGE)

    system_prompt = compose_turn_system_prompt(
        request, assistant, system_prompt=None, project_id=None, personal_instructions=personal_instructions
    )

    knowledge_context = None
    if resolved.kb_full_context_task is not None:
        try:
            knowledge_context = await resolved.kb_full_context_task
        except Exception:  # noqa: BLE001 — the loader never raises; belt and braces
            logger.warning("Voice knowledge base for %s failed to load", scrub_log(assistant_id), exc_info=True)

    tools = resolved.tools_override
    plan = VoiceAgentPlan(
        assistant_id=assistant_id,
        system_prompt=system_prompt,
        knowledge_context=knowledge_context or None,
        voice_guidelines=not getattr(assistant, "voice_instructions_only", True),
        tool_ids=list(tools.tool_ids) if tools is not None else None,
        owns_toolset=bool(tools is not None and tools.owns_toolset),
    )
    logger.info(
        "Voice session runs agent %s: tools=%s, knowledge_base=%s, guidelines=%s",
        scrub_log(assistant_id),
        "request" if plan.tool_ids is None else len(plan.tool_ids),
        "whole" if plan.knowledge_context else "none",
        plan.voice_guidelines,
    )
    return plan
