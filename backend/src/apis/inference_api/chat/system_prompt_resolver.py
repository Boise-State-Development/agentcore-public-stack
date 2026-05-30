"""Resolves and appends the user's selected system prompt to the agent's
base system prompt at invocation time.

This sits in its own module so the gating rules are testable without spinning
up a full agent + invocation stack.

Gating rules (any True ⇒ skip):
  - is_resume:        snapshot system prompt was captured at pause time
  - is_continuation:  re-rendering the system prompt mid-turn invalidates
                      Bedrock prompt caching for the truncated reply
  - is_preview:       live form edits already drive the system prompt
  - has_assistant:    assistants are KB-grounded with their own instructions;
                      a "mode" prompt could directly contradict them

Selection precedence:
  1. ``request_prompt_id`` from the InvocationRequest if provided.
     The frontend always sends the active selection on submit so the
     first turn of a brand-new session works before any metadata row
     exists. The resolver also persists this id back onto session
     preferences so the choice survives refresh / new devices.
  2. ``selected_prompt_id`` from session preferences (older sessions
     and resume / new-device flows where the request id may be absent).

A missing or disabled prompt is silently skipped — the agent runs without
the custom mode rather than failing the turn.
"""

from __future__ import annotations

import logging
from typing import Optional

from apis.shared.sessions.metadata import (
    get_session_metadata,
    set_selected_prompt_id,
)
from apis.shared.system_prompts.service import get_system_prompts_service

logger = logging.getLogger(__name__)


def should_resolve_custom_prompt(
    *,
    is_resume: bool,
    is_continuation: bool,
    is_preview: bool,
    has_assistant: bool,
) -> bool:
    """Return True when the inference path should look up + append a custom
    system prompt for this turn. Pure function — easy to unit test."""
    return not (is_resume or is_continuation or is_preview or has_assistant)


def append_active_prompt(base_system_prompt: str, prompt_name: str, prompt_text: str) -> str:
    """Compose the final system prompt with the active mode appended in a
    consistent format. Kept as a function so the wire format can be asserted
    by tests without invoking the route."""
    return (
        f"{base_system_prompt}\n\n"
        f"## Active Mode: {prompt_name}\n\n"
        f"{prompt_text}"
    )


async def _persist_selection(session_id: str, user_id: str, prompt_id: str) -> None:
    """Best-effort write of the active prompt onto session preferences so
    a refresh / new device can pick up where this turn left off. Never
    raises — failures are logged and swallowed."""
    try:
        await set_selected_prompt_id(session_id, user_id, prompt_id)
    except Exception:
        logger.warning(
            "Failed to persist selected_prompt_id to session preferences",
            exc_info=True,
        )


async def resolve_active_prompt_text(
    *,
    session_id: str,
    user_id: str,
    request_prompt_id: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Look up the user's active prompt for this session.

    Returns a ``(name, prompt_text)`` tuple if there's an enabled prompt
    selected, ``None`` otherwise. Never raises — exceptions are logged and
    converted to ``None`` so a malformed prompt can never break a turn.

    Selection precedence: ``request_prompt_id`` (current turn's choice)
    over the persisted ``selected_prompt_id`` (older sessions / refresh).

    Race window: a user who clears the prompt and submits within the BFF
    persist round-trip (sub-200ms) may have the resolver fall back to the
    not-yet-cleared persisted value for one turn. The cost is one stale
    "mode applied" — bounded and self-healing on the next turn — and we
    deliberately don't widen the wire protocol with a "cleared" sentinel
    to cover it. If this becomes a real complaint, the fix is to await
    the persist on the frontend before allowing submit.
    """
    try:
        active_prompt_id: Optional[str] = request_prompt_id

        if not active_prompt_id:
            session_meta = await get_session_metadata(session_id, user_id)
            active_prompt_id = (
                session_meta.preferences.selected_prompt_id
                if session_meta and session_meta.preferences
                else None
            )

        if not active_prompt_id:
            return None

        custom_prompt = await get_system_prompts_service().get_enabled_prompt(active_prompt_id)
        if not custom_prompt:
            logger.info(
                f"Custom prompt {active_prompt_id!r} not found or disabled — skipping"
            )
            return None

        # When the request explicitly carried the id, mirror it onto the
        # session row so resume / refresh / a different device sees it too.
        if request_prompt_id:
            await _persist_selection(session_id, user_id, request_prompt_id)

        return custom_prompt.name, custom_prompt.prompt_text
    except Exception:
        logger.error(
            "Error resolving custom system prompt — continuing without it",
            exc_info=True,
        )
        return None
