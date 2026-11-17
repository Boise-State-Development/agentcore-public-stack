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

Voice mode must be switched on for the Agent (``voice_enabled``).

**A project's harness (#2021)** runs as a text project turn does: the project's
instructions under the workspace guidance, personal instructions below them,
project memory (its block and tools), the shared-conversation tools and
``project_files``. Text puts the member note and the file-readiness note on each
user message, to keep one system prompt for every member's cache; a voice session
has one member and no shared cache, so both go in the system prompt once. The
harness has no voice setting of its own: the project's
(``apis.shared.projects.voice``) decides, so voice runs unless the owner turned
it off or a bound tool or the files keep it off. The built-in voice guidelines
always apply, since the harness has no "use only these instructions" setting.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from apis.shared.auth.models import User
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

#: The ``code`` on a ``bidi_error`` frame that ends the session because the
#: Agent cannot run in voice. The SPA shows ``message`` and leaves voice mode.
AGENT_VOICE_UNAVAILABLE_CODE = "agent_voice_unavailable"

AGENT_VOICE_OFF_MESSAGE = "This agent isn't available in voice mode."
AGENT_VOICE_REFUSED_MESSAGE = "This agent isn't available to you right now."
PROJECT_VOICE_OFF_MESSAGE = "Voice mode is turned off for this project."
PROJECT_VOICE_BLOCKED_MESSAGE = (
    "Voice mode is off for this project because it uses tools or files that voice can't handle. "
    "The project's page says which."
)

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
    #: Set when the Agent is a project's harness (#2021).
    project_id: Optional[str] = None
    #: A project's memory block (the ``project`` and ``mine`` indexes).
    memory_context: Optional[str] = None
    #: Tools closed over this session (project memory, shared tasks, ``project_files``).
    extra_tools: List[Any] = field(default_factory=list)


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
    voice switched on, or, for a project's harness, the project's voice status
    allows it (#2021). ``personal_instructions`` go below the Agent's (or the
    project's), as on a text turn (#2020).
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

    assistant = resolved.assistant
    project_id = resolved.project_id
    if project_id:
        await _check_project_voice(resolved, assistant_id, _cancel_all)
    # Read from what runs for this caller: a published Agent's reviewed
    # version, so turning voice on for one goes through review.
    elif not getattr(assistant, "voice_enabled", False):
        _cancel_all()
        raise VoiceAgentRefused(AGENT_VOICE_OFF_MESSAGE)

    system_prompt = compose_turn_system_prompt(
        request, assistant, system_prompt=None, project_id=project_id, personal_instructions=personal_instructions
    )

    knowledge_context = None
    if resolved.kb_full_context_task is not None:
        try:
            knowledge_context = await resolved.kb_full_context_task
        except Exception:  # noqa: BLE001 — the loader never raises; belt and braces
            logger.warning("Voice knowledge base for %s failed to load", scrub_log(assistant_id), exc_info=True)

    memory_context: Optional[str] = None
    extra_tools: List[Any] = []
    if project_id:
        if not knowledge_context:
            await _check_project_files(assistant_id, _cancel_all)
        system_prompt, memory_context, extra_tools = await _project_session(
            resolved, user, assistant_id, system_prompt
        )

    tools = resolved.tools_override
    plan = VoiceAgentPlan(
        assistant_id=assistant_id,
        system_prompt=system_prompt,
        knowledge_context=knowledge_context or None,
        # A harness has no "use only these instructions" setting: the guidelines stay.
        voice_guidelines=True if project_id else not getattr(assistant, "voice_instructions_only", True),
        tool_ids=list(tools.tool_ids) if tools is not None else None,
        owns_toolset=bool(tools is not None and tools.owns_toolset),
        project_id=project_id,
        memory_context=memory_context,
        extra_tools=extra_tools,
    )
    logger.info(
        "Voice session runs agent %s: tools=%s, knowledge_base=%s, guidelines=%s, project=%s, session_tools=%d",
        scrub_log(assistant_id),
        "request" if plan.tool_ids is None else len(plan.tool_ids),
        "whole" if plan.knowledge_context else "none",
        plan.voice_guidelines,
        "yes" if plan.project_id else "no",
        len(plan.extra_tools),
    )
    return plan


async def _check_project_voice(resolved: Any, assistant_id: str, cancel_all) -> None:
    """Refuse a project's harness the project's voice status keeps out of voice.

    The owner's switch and the bound tools, from the same status the project's page
    shows (``apis.shared.projects.voice``). The files are checked once they have
    loaded (:func:`_check_project_files`), so a project whose files reached voice
    costs no second read.
    """
    from apis.shared.projects.voice import project_voice_status

    project = resolved.project
    if project is None:  # the gate always sets it with ``project_id``; never run blind
        cancel_all()
        raise VoiceAgentRefused(AGENT_VOICE_REFUSED_MESSAGE)
    status = await project_voice_status(project, resolved.assistant, check_files=False)
    if not status.enabled:
        cancel_all()
        logger.info("Voice refused for project %s: switched off", scrub_log(resolved.project_id))
        raise VoiceAgentRefused(PROJECT_VOICE_OFF_MESSAGE)
    if status.blocked_tool_ids:
        cancel_all()
        logger.info(
            "Voice refused for project %s: tools not available in voice %s",
            scrub_log(resolved.project_id),
            [scrub_log(t) for t in status.blocked_tool_ids],
        )
        raise VoiceAgentRefused(PROJECT_VOICE_BLOCKED_MESSAGE)


async def _check_project_files(assistant_id: str, cancel_all) -> None:
    """Refuse a project whose files did not reach voice because they are too large."""
    from apis.shared.projects.voice import files_too_large_for_voice

    try:
        too_large = await asyncio.to_thread(files_too_large_for_voice, assistant_id)
    except Exception:  # noqa: BLE001 — unknown is not "too large"; the session runs without files
        logger.warning("Voice files check for %s failed", scrub_log(assistant_id), exc_info=True)
        return
    if too_large is not None:
        cancel_all()
        logger.info("Voice refused for harness %s: files %s", scrub_log(assistant_id), too_large.reason)
        raise VoiceAgentRefused(PROJECT_VOICE_BLOCKED_MESSAGE)


async def _project_session(
    resolved: Any, user: User, assistant_id: str, system_prompt: Optional[str]
) -> "tuple[Optional[str], Optional[str], List[Any]]":
    """A project voice session's prompt additions, memory block and session-bound tools.

    The same pieces a text project turn assembles (``routes._build_turn_tools`` and
    the user-message notes), in voice's shape: the notes join the system prompt.
    """
    from apis.inference_api.chat.project_memory import (
        await_project_memory,
        build_project_files_tools,
        build_project_memory_tools,
        build_shared_task_tools,
    )

    project_id = resolved.project_id
    memory_context: Optional[str] = None
    extra_tools: List[Any] = []
    if resolved.project_memory_task is not None:
        try:
            memory = await await_project_memory(resolved.project_memory_task)
        except Exception:  # noqa: BLE001 — the loader never raises; belt and braces
            logger.warning("Voice project memory for %s failed to load", scrub_log(project_id), exc_info=True)
            memory = None
        if memory is not None:
            memory_context = memory.memory_context or None
            extra_tools += build_project_memory_tools(memory, user) + build_shared_task_tools(project_id, user)
    extra_tools += build_project_files_tools(project_id, assistant_id, user)

    notes = [resolved.project_member_note] if resolved.project_member_note else []
    if resolved.project_files_note_task is not None:
        try:
            files_note = await resolved.project_files_note_task
        except Exception:  # noqa: BLE001 — a missing readiness note costs nothing
            logger.warning("Voice file-readiness note for %s failed", scrub_log(project_id), exc_info=True)
            files_note = None
        if files_note:
            notes.append(files_note)
    if notes:
        system_prompt = "\n\n".join([system_prompt or "", *notes]).lstrip()
    return system_prompt, memory_context, extra_tools
