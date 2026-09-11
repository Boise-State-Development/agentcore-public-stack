"""Turn a finished batch of tool calls into one line a person would say.

The SPA can already describe a tool call deterministically ("Listed 4
assignments") from the input and result it holds. What it cannot do is read
the *content* of those results and say which thing was found — "Found the
Syllabus Acknowledgment assignment in BIO 101 (25 pts, due Aug 29)". That
needs a model, and this is the cheapest honest way to get one.

SIDE-CHANNEL, NOT AGENT CONTEXT
-------------------------------
Structured exactly like `generate_conversation_title`: its own Bedrock call,
on its own messages, run as an asyncio task concurrent with the agent stream.
It never touches `agent.messages`, never appends to the conversation, and
therefore adds **nothing** to the cacheable prefix — the CLAUDE.md
prompt-cache contract is untouched and no turn pays a cache re-write for a
display string.

Its own spend is bounded by construction: Nova Micro, a hard cap on the number
of calls described, and per-call input/result truncation applied twice (once at
capture in `AgentStatusHook`, once here). A wide batch of large results costs
roughly the same as a narrow one.

FAILURE IS A DOWNGRADE, NOT AN ERROR
------------------------------------
Every failure path returns `None`. The SPA then keeps the deterministic line
it has been showing since the tools started — the user sees a slightly less
specific sentence and nothing else changes. That is why this is safe to leave
on by default, and why nothing here is allowed to raise into the stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Nova Micro: the cheapest Bedrock model that reliably follows a one-line
# instruction. Same model the conversation-title side-channel uses.
_MODEL_ID = "us.amazon.nova-micro-v1:0"
# The summary is one short sentence; anything longer is the model ignoring the
# brief, and the store clamps it again on write.
_MAX_OUTPUT_TOKENS = 60
# Second-stage truncation, applied to what the hook already captured. Keeps a
# wide batch's prompt flat regardless of how chatty the tools were.
_MAX_INPUT_CHARS = 300
_MAX_RESULT_CHARS = 700
_MAX_CALLS = 8

_SUMMARY_SYSTEM_PROMPT = """You describe what an AI assistant just did, in one short past-tense line.

You are given the tool calls the assistant made and what they returned. Write a single line a non-technical person would understand, naming the specific things that were found.

Guidelines:
- One line, maximum 90 characters. No trailing period.
- Past tense, starting with a verb: "Found", "Listed", "Searched", "Created", "Updated", "Read".
- Name the specific result: the course, the file, the assignment, the count. That specificity is the entire point.
- Never mention tool names, parameters, IDs, JSON, or "the API".
- Never mention that you are an AI or that tools were used.
- If a call failed, say so plainly: "Couldn't reach Canvas".

Examples:
Calls: list_courses -> 3 courses (BIO 101, HIST 210, CS 121)
Output: Found 3 active courses

Calls: list_assignments -> 12 assignments; get_assignment_details -> "Syllabus Acknowledgment", 25 pts, due Aug 29
Output: Found the Syllabus Acknowledgment assignment in BIO 101

Calls: search_knowledge_base -> 4 passages about parking permits
Output: Searched the knowledge base for parking permit rules

Calls: create_rubric -> error: 422 rubric association required
Output: Couldn't create the rubric — Canvas rejected it"""


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _build_prompt(calls: List[Dict[str, Any]]) -> str:
    """Render the batch as the compact "Calls: ..." block the prompt models."""
    lines: List[str] = []
    for call in calls[:_MAX_CALLS]:
        name = str(call.get("toolName") or "tool")
        tool_input = _truncate(str(call.get("input") or ""), _MAX_INPUT_CHARS)
        result = _truncate(str(call.get("result") or ""), _MAX_RESULT_CHARS)
        if not call.get("ok", True):
            lines.append(f"{name}({tool_input}) -> FAILED: {result}")
        else:
            lines.append(f"{name}({tool_input}) -> {result}")
    overflow = len(calls) - _MAX_CALLS
    if overflow > 0:
        lines.append(f"...and {overflow} more call(s)")
    return "Calls:\n" + "\n".join(lines)


def _clean(text: str) -> str:
    """Strip the wrappers small models like to add around a one-liner."""
    summary = (text or "").strip()
    # Models sometimes answer with "Output: ..." because the prompt shows it.
    for prefix in ("Output:", "Summary:", "Line:"):
        if summary.lower().startswith(prefix.lower()):
            summary = summary[len(prefix) :].strip()
    summary = summary.strip().strip('"').strip("'").strip()
    # One line only — a model that explains itself gets its first sentence used.
    summary = summary.splitlines()[0].strip() if summary else ""
    return summary.rstrip(".").strip()


async def summarize_tool_batch(calls: List[Dict[str, Any]]) -> Optional[str]:
    """Summarize one finished tool batch, or return ``None``.

    ``calls`` are the bounded records captured by ``AgentStatusHook`` —
    ``{toolUseId, toolName, input, result, ok, durationMs}``.

    Returns ``None`` on an empty batch, a disabled flag, a missing Bedrock
    client, a model error, or an empty generation. Every one of those means
    "keep showing the deterministic line", which is why they share a return
    value instead of raising.
    """
    from apis.shared.feature_flags import tool_summaries_enabled

    if not calls or not tool_summaries_enabled():
        return None

    try:
        import boto3
    except ImportError:  # pragma: no cover - dev without boto3
        return None

    try:
        region = os.environ.get("AWS_REGION", "us-west-2")
        client = boto3.client("bedrock-runtime", region_name=region)

        # boto3's converse() is synchronous — awaited inline it would block
        # the event loop for the whole Nova round trip, stalling the agent
        # stream this task runs concurrently with.
        response = await asyncio.to_thread(
            client.converse,
            modelId=_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": _build_prompt(calls)}]}],
            system=[{"text": _SUMMARY_SYSTEM_PROMPT}],
            inferenceConfig={
                "temperature": 0.2,
                "maxTokens": _MAX_OUTPUT_TOKENS,
                "topP": 0.9,
            },
        )
        summary = _clean(response["output"]["message"]["content"][0]["text"])
        if not summary:
            return None
        return summary
    except Exception:  # noqa: BLE001 - a summary is never worth an error
        logger.debug("Tool-batch summary generation skipped", exc_info=True)
        return None
