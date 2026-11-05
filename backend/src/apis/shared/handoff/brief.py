"""The handoff brief: what the source conversation's own model is asked, and how the answer travels.

``docs/specs/agent-handoffs-and-consults.md`` §4.5.1. The Runtime re-sends the source's last request
with one user message appended, so the history reads from the source's prompt cache, and streams the
answer into the review sheet's context card. This module is the pure half: the request field, the
appended instruction, the checks that decide whether a brief can run, and the SSE frames. The call
itself lives with the turn path (``apis/inference_api/chat/handoff_brief.py``), because it has to use
the agent the turn path builds.

Nothing here reaches the source conversation. The instruction and the brief exist only in the side
call's request and the card.
"""

import json
from typing import Any, Dict, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.handoff.excerpt import count_turns

#: Bumped whenever the instruction's wording changes, and recorded on the ``C#`` row, so a change in
#: brief length or ``contextEdited`` can be traced to the wording that produced it.
BRIEF_INSTRUCTION_VERSION = 1

#: The closing tag the instruction block is wrapped in. Defused in the request and the Agent's
#: name and description, like the excerpt's tags (``context.neutralize_close_tags``).
BRIEF_REQUEST_TAG = "handoff_brief_request"

#: A source with fewer turns than this uses the excerpt: below the model's minimum cacheable prefix
#: there is nothing to reuse, and little to brief.
BRIEF_MIN_SOURCE_TURNS = 2

#: The Agent's description, as the instruction names it. Descriptions are user-written, so bounded.
BRIEF_AGENT_DESCRIPTION_MAX_CHARS = 600

#: Why a brief was not written. Each is a ``HandoffBriefFallback`` metric reason and the ``reason``
#: on the fallback frame; the SPA shows the excerpt for all of them.
BriefFallbackReason = Literal[
    "flag_off",
    "not_open",
    "busy",
    "preempted",
    "quota",
    "refused",
    "timeout",
    "error",
    "tool_use_only",
    "short_source",
    "history_ends_on_user",
]

_INSTRUCTION = """\
Write a brief for another of the user's agents, which will continue this work in a new conversation. \
It will not see this conversation, only your brief and the user's request below.

Write it for that request. Cover, in this order and only where there is something to say:
- Goal: what the user is asking for, and why, as this conversation shows it.
- Background: what matters from this conversation for the request, stated plainly.
- Decisions and constraints: what has been settled, and limits the user has given.
- Files and artifacts: what is in play, by name.
- Open questions: what is still undecided.

Keep it under about 800 words. Write in plain prose with short headed sections. Answer in text \
only: do not call any tool. Do not address the user, and do not answer the request yourself."""


class HandoffBriefRequest(BaseModel):
    """``handoff_brief`` on ``/invocations``: the open handoff whose card the brief is for."""

    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId", min_length=1, max_length=64)


def _defuse(text: str) -> str:
    return text.replace(f"</{BRIEF_REQUEST_TAG}", f"<\\/{BRIEF_REQUEST_TAG}")


def _attr(text: str) -> str:
    return _defuse(text).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace("\n", " ")


def build_brief_instruction(request: str, agent_name: str, agent_description: Optional[str] = None) -> str:
    """The one user message appended to the source's history for the brief.

    A constant instruction, versioned by ``BRIEF_INSTRUCTION_VERSION``, around the only three things that
    vary: the Agent's name and description, and the user's request as the review sheet holds it.
    """
    description = (agent_description or "").strip()[:BRIEF_AGENT_DESCRIPTION_MAX_CHARS]
    attrs = f'agent="{_attr(agent_name.strip())}"'
    if description:
        attrs += f' agent_description="{_attr(description)}"'
    return (
        f"<{BRIEF_REQUEST_TAG} {attrs}>\n"
        f"{_INSTRUCTION}\n\n"
        f"The request: {_defuse(request.strip())}\n"
        f"</{BRIEF_REQUEST_TAG}>"
    )


def brief_fallback_for_history(messages: Sequence[Mapping[str, Any]]) -> Optional[BriefFallbackReason]:
    """Why this history cannot be briefed from the cache, or ``None`` when it can.

    A history that ends on a user message (a failed or interrupted turn with no reply) would need the
    instruction appended to that message to keep Converse's alternation, and whether that keeps the
    read is unverified (§4.5.1), so it falls back until it is.
    """
    if count_turns(messages) < BRIEF_MIN_SOURCE_TURNS:
        return "short_source"
    if messages and messages[-1].get("role") == "user":
        return "history_ends_on_user"
    return None


def _frame(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def brief_text_frame(text: str) -> str:
    """A piece of the brief as it streams, in the turn stream's ``content_block_delta`` shape."""
    return _frame("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})


def brief_fallback_frame(handoff_id: str, reason: BriefFallbackReason) -> str:
    """The brief was not written: the card stays the excerpt. Never carries text."""
    return _frame("handoff_brief_fallback", {"type": "handoff_brief_fallback", "handoffId": handoff_id, "reason": reason})


def brief_done_frame(handoff_id: str, *, brief_hash: Optional[str], cache_status: Optional[str]) -> str:
    """The end of a brief stream. ``briefHash`` is set only when a brief was written."""
    data: Dict[str, Any] = {"type": "done", "handoffId": handoff_id}
    if brief_hash:
        data["briefHash"] = brief_hash
    if cache_status:
        data["cacheStatus"] = cache_status
    return _frame("done", data)
