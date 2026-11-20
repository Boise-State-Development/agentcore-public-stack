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
from dataclasses import dataclass
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

#: The run variant's own version (``docs/specs/workflow-runs.md`` §6.3), recorded on the ``C#`` row
#: beside ``briefInstruction: "run"`` as the single variant's is beside ``"handoff"``.
BRIEF_RUN_INSTRUCTION_VERSION = 1

#: The run variant names up to ``WORKFLOW_RUN_MAX_STEPS`` (8) Agents, so each part is bounded more
#: tightly than the single variant's: the shared request, and per step the Agent's name, its
#: description and its own request when it differs from the shared one. With the constant text and
#: the markup that bounds the whole instruction at ``BRIEF_RUN_INSTRUCTION_MAX_CHARS`` (about 4.5k
#: tokens at 8 steps with every part at its cap; a typical run of four is well under 1k).
BRIEF_RUN_REQUEST_MAX_CHARS = 4_000
BRIEF_RUN_STEP_NAME_MAX_CHARS = 120
BRIEF_RUN_STEP_DESCRIPTION_MAX_CHARS = 300
BRIEF_RUN_STEP_REQUEST_MAX_CHARS = 1_000
BRIEF_RUN_MAX_STEPS = 8
BRIEF_RUN_INSTRUCTION_MAX_CHARS = 18_000

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


_RUN_INSTRUCTION = """\
Write one brief for several of the user's agents. Each will continue this work in a new conversation \
of its own, and all of them start from this same brief. None of them will see this conversation, only \
your brief and its own request.

Write it for the work as a whole, as the request below and the agents' requests describe it, not for \
any one agent. Cover, in this order and only where there is something to say:
- Goal: what the user is asking for, and why, as this conversation shows it.
- Background: what matters from this conversation for the request, stated plainly.
- Decisions and constraints: what has been settled, and limits the user has given.
- Files and artifacts: what is in play, by name.
- Open questions: what is still undecided.

Do not divide the work between the agents or write a section for each: each gets its own request \
separately. Keep it under about 800 words. Write in plain prose with short headed sections. Answer in \
text only: do not call any tool. Do not address the user, and do not answer the request yourself."""


class HandoffBriefRequest(BaseModel):
    """``handoff_brief`` on ``/invocations``: the open handoff whose card the brief is for.

    ``runId`` (optional, additive) asks for the one brief of a workflow run whose kept steps include
    ``handoffId`` (``docs/specs/workflow-runs.md`` §6.3). Without it the brief is the single
    handoff's, byte for byte as before.
    """

    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId", min_length=1, max_length=64)
    run_id: Optional[str] = Field(None, alias="runId", min_length=1, max_length=64)


@dataclass(frozen=True)
class RunBriefStep:
    """One kept step of a run, as the run instruction names it."""

    agent_name: str
    request: str
    agent_description: Optional[str] = None


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


def _bounded(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _line(text: str) -> str:
    return " ".join(_defuse(text).split())


def build_run_brief_instruction(request: str, steps: Sequence[RunBriefStep]) -> str:
    """The one user message appended to the source's history for a workflow run's brief.

    The run variant of ``build_brief_instruction``: a constant instruction, versioned by
    ``BRIEF_RUN_INSTRUCTION_VERSION``, around the shared request and each kept step's Agent (name and
    description) and its own request when that differs from the shared one. Every part is capped
    (``BRIEF_RUN_*_MAX_CHARS``), so the whole is at most ``BRIEF_RUN_INSTRUCTION_MAX_CHARS``.
    """
    shared = _bounded(request, BRIEF_RUN_REQUEST_MAX_CHARS)
    lines = []
    for step in list(steps)[:BRIEF_RUN_MAX_STEPS]:
        line = f"- {_line(_bounded(step.agent_name, BRIEF_RUN_STEP_NAME_MAX_CHARS))}"
        description = _bounded(step.agent_description or "", BRIEF_RUN_STEP_DESCRIPTION_MAX_CHARS)
        if description:
            line += f": {_line(description)}"
        own = step.request.strip()
        if own and own != request.strip():
            line += f"\n  Its own request: {_line(_bounded(own, BRIEF_RUN_STEP_REQUEST_MAX_CHARS))}"
        lines.append(line)
    agents = "\n".join(lines)
    return (
        f'<{BRIEF_REQUEST_TAG} agents="{len(lines)}">\n'
        f"{_RUN_INSTRUCTION}\n\n"
        f"The request: {_defuse(shared)}\n\n"
        f"The agents:\n{agents}\n"
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


def brief_fallback_frame(handoff_id: str, reason: BriefFallbackReason, *, run_id: Optional[str] = None) -> str:
    """The brief was not written: the card stays the excerpt. Never carries text.

    ``runId`` is added, additively, only on a run brief's frame.
    """
    data: Dict[str, Any] = {"type": "handoff_brief_fallback", "handoffId": handoff_id, "reason": reason}
    if run_id:
        data["runId"] = run_id
    return _frame("handoff_brief_fallback", data)


def brief_done_frame(
    handoff_id: str, *, brief_hash: Optional[str], cache_status: Optional[str], run_id: Optional[str] = None
) -> str:
    """The end of a brief stream. ``briefHash`` is set only when a brief was written; ``runId`` only on a run's."""
    data: Dict[str, Any] = {"type": "done", "handoffId": handoff_id}
    if run_id:
        data["runId"] = run_id
    if brief_hash:
        data["briefHash"] = brief_hash
    if cache_status:
        data["cacheStatus"] = cache_status
    return _frame("done", data)
