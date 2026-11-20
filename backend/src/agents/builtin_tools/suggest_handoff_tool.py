"""``suggest_handoff`` — offer the user a separate conversation for a piece of work (handoffs §4.4).

The model calls it when part of the request would sidetrack this conversation: "this rubric
deserves its own thread with Rubric Builder". The user sees a chip under the answer with **Start**
and **Dismiss**; Start opens the same review sheet a mid-thread ``@``-mention does. The logic lives
in ``apis.shared.handoff.suggestions``; this module is the Strands wrapper.

Design notes
------------
* **Non-blocking.** The handler writes the row, parks the chip event and returns at once. It never
  pauses through ``ToolContext.interrupt``: this is not ``ask_user_question``, and a turn that
  stopped to offer a chip would make the user answer a question nobody asked.
* **A constant spec.** ``_INPUT_SCHEMA`` names no Agents: an ``enum`` of the user's Agents would
  change whenever a pin changed, and each change would re-write ``toolConfig``, the first
  cachePoint. ``agent`` is free text the SPA matches.
* **Identity by closure** (``make_suggest_handoff_tool``), never from ``invocation_state`` or the
  model's input, like ``verify_sources``. The closure holds ``(session_id, user_id)``, both
  agent-cache key elements, so the family is in ``KEY_DESCRIBED_INJECTED_TOOL_IDS``.
* **Per-turn limit in the invocation state**, which Strands creates for each invocation and shares
  across its tool calls, so a cached agent starts each turn at zero. The per-conversation limit is
  read from the rows.
* **Nowhere near the first token**: no hook, and nothing runs until the model calls it.
* **No system-prompt clause yet** (spec §4.4, Steering). Ship the tool alone, measure how often it
  is used, and add a clause only if it is under-used: a base-prompt change is a one-time cache
  re-write for every user.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from strands import tool
from strands.types.tools import ToolContext

from apis.shared.feature_flags import handoff_suggestions_enabled
from apis.shared.handoff.models import (
    SUGGESTION_PROMPT_MAX_CHARS,
    SUGGESTION_TITLE_MAX_CHARS,
    SUGGESTION_TLDR_MAX_CHARS,
)
from apis.shared.handoff.suggestions import (
    UNAVAILABLE,
    SuggestionRefused,
    normalize_suggestion,
    record_suggestion,
)

logger = logging.getLogger(__name__)

#: The tool's name, and its catalog id (the gate key in ``SUGGEST_HANDOFF_TOOL_IDS``).
SUGGEST_HANDOFF_TOOL_NAME = "suggest_handoff"

#: How many suggestions this turn has made, in Strands' per-invocation state.
_TURN_COUNT_KEY = "_suggest_handoff_count"

_DESCRIPTION = (
    "Offer the user a separate conversation for a piece of work that would sidetrack this one, "
    "such as a self-contained deliverable or a different task. The user sees a chip with Start "
    "and Dismiss under your reply; nothing starts unless they press Start. It returns at once and "
    "your turn continues: keep answering what was asked here. Suggest only when the work is "
    "clearly separate and substantial, at most once a turn. Never use it for the request itself."
)

_INPUT_SCHEMA: Dict[str, Any] = {
    "json": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "maxLength": SUGGESTION_TITLE_MAX_CHARS,
                "description": "Imperative, e.g. 'Draft the BIO 101 rubric'",
            },
            "tldr": {
                "type": "string",
                "maxLength": SUGGESTION_TLDR_MAX_CHARS,
                "description": "Why now, in one or two plain sentences",
            },
            "prompt": {
                "type": "string",
                "maxLength": SUGGESTION_PROMPT_MAX_CHARS,
                "description": "Self-contained first message for the new conversation",
            },
            "agent": {
                "type": "string",
                "description": "Optional: the name of one of the user's Agents to run it",
            },
        },
        "required": ["title", "tldr", "prompt"],
    }
}


def _result(text: str, status: str = "success") -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": status}


def make_suggest_handoff_tool(session_id: str, user_id: str):
    """Create a ``suggest_handoff`` tool bound to the given conversation."""
    if not session_id or not user_id:
        raise ValueError("suggest_handoff requires a session_id and a user_id")

    @tool(
        name=SUGGEST_HANDOFF_TOOL_NAME,
        description=_DESCRIPTION,
        inputSchema=_INPUT_SCHEMA,
        context=True,
    )
    async def suggest_handoff(
        title: str,
        tldr: str,
        prompt: str,
        tool_context: ToolContext,
        agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not handoff_suggestions_enabled():
            return _result(UNAVAILABLE)
        try:
            args = normalize_suggestion(title, tldr, prompt, agent)
        except SuggestionRefused as exc:
            return _result(f"{exc} Fix the arguments and call again, or mention it in your reply.", "error")

        state = tool_context.invocation_state if isinstance(tool_context.invocation_state, dict) else {}
        turn_count = int(state.get(_TURN_COUNT_KEY, 0) or 0)
        tool_use_id = str((tool_context.tool_use or {}).get("toolUseId", "") or "")
        message = await asyncio.to_thread(
            record_suggestion,
            user_id=user_id,
            session_id=session_id,
            tool_use_id=tool_use_id,
            args=args,
            turn_count=turn_count,
        )
        state[_TURN_COUNT_KEY] = turn_count + 1
        return _result(message)

    return suggest_handoff


__all__ = ["SUGGEST_HANDOFF_TOOL_NAME", "make_suggest_handoff_tool"]
