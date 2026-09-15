"""Ask the user structured clarifying questions and wait for the answer.

The agent calls this when a request is genuinely ambiguous and guessing would
send the whole turn in the wrong direction. The tool pauses the turn on a
Strands interrupt; the SPA renders a picker; the user's selections come back as
this call's return value and the turn continues in place.

Why a tool interrupt rather than a hook
---------------------------------------
``MCPExternalApprovalHook`` gates *someone else's* tool, so it has to interrupt
from ``BeforeToolCallEvent``. Here the pause **is** the tool, so it raises its
own interrupt through ``ToolContext`` (which implements ``_Interruptible``,
``strands/types/tools.py``). Same machinery downstream — Strands routes both
through ``_stop_for_interrupts``, so the paused-turn snapshot, the SSE
extraction and the resume route need no special case for this flavor.

Cost notes (CLAUDE.md token-effectiveness tenet)
------------------------------------------------
* The tool spec is a **constant** in ``toolConfig``. It is registered once at
  startup, never injected per-turn, so it is part of the stable cacheable
  prefix and never re-writes it. Do not make its presence conditional on
  conversation state.
* This is a **static** registry tool, not an ``extra_tools`` injection: it
  captures no session/user identity, so it does not touch the injected-tool
  agent-cache bypass.
* The questions themselves ride the SSE channel, not the prompt. Only the
  compact formatted answer block re-enters the conversation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from strands import tool
from strands.types.tools import ToolContext

from apis.shared.feature_flags import ask_user_question_enabled
from apis.shared.user_questions.models import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    MIN_OPTIONS,
    UserQuestionError,
    format_answers,
    normalize_questions,
    parse_answers,
)

logger = logging.getLogger(__name__)

# Appended to the system prompt of any turn whose effective tool list contains
# this tool. Measured, not guessed: with the app's real system prompt and a
# production-shaped tool set, the model called this tool on 4/24 deliberately
# ambiguous requests. With these sentences added it called it on 24/24, and
# still on 0/18 unambiguous ones — the lift comes without turning clear
# requests into interrogations.
#
# ⚠️ Do not reword without re-running the harness. The same *idea* placed in
# this tool's own description measured no better than baseline (44-56% across
# variants, against a baseline band of 17-44%), so the effect belongs to these
# sentences in this position, not to the concept. Nor is it explained by the
# story a model tells about itself: removing the system prompt's "Cost
# Awareness" clause — the thing the model blamed when asked — moved the rate
# far less than this does.
SYSTEM_PROMPT_GUIDANCE = (
    "When gathering structured input upfront would be more efficient than "
    "presenting multiple scenarios and hoping the user clarifies, use the "
    "ask_user_question tool. Prefer it over long prose when the user's answer "
    "would let you skip 50%+ of your response."
)

# Interrupt name. Scoped by toolUseId upstream — `ToolContext._interrupt_id`
# builds `v1:tool_call:{toolUseId}:{uuid5(name)}` — so two parallel calls in one
# turn produce distinct interrupts the SPA can correlate per prompt.
INTERRUPT_NAME = "ask_user_question"

# Hand-written because Strands' generated schema drops `$defs` when it cleans
# the Pydantic output (`_clean_pydantic_schema`), which would leave nested
# models as dangling `$ref`s. Writing it out keeps the nesting intact and lets
# the descriptions carry the usage guidance the model actually needs.
_INPUT_SCHEMA: Dict[str, Any] = {
    "json": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    f"One to {MAX_QUESTIONS} questions to ask at once. Ask "
                    "everything you need in a single call — several calls in a "
                    "row read as an interrogation."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {
                            "type": "string",
                            "description": (
                                "Very short label shown as a chip, max 12 "
                                'characters. Examples: "Scope", "Format", '
                                '"Audience".'
                            ),
                        },
                        "question": {
                            "type": "string",
                            "description": (
                                "The full question. Be specific and end with a "
                                "question mark."
                            ),
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": (
                                "True when the choices are not mutually "
                                "exclusive and the user may pick several."
                            ),
                        },
                        "options": {
                            "type": "array",
                            "minItems": MIN_OPTIONS,
                            "maxItems": MAX_OPTIONS,
                            "description": (
                                f"{MIN_OPTIONS}-{MAX_OPTIONS} distinct choices. "
                                "Do not add an 'Other' or 'Skip' choice — the "
                                "interface always offers both."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Concise choice text, 1-5 words.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            "One line on what this choice means "
                                            "or what happens if it is picked."
                                        ),
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["header", "question", "options"],
                },
            }
        },
        "required": ["questions"],
    }
}


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _success(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "success"}


@tool(inputSchema=_INPUT_SCHEMA, context=True)
def ask_user_question(
    questions: List[Dict[str, Any]], tool_context: ToolContext
) -> Dict[str, Any]:
    """Ask the user multiple-choice questions and wait for their answer.

    Use this only when you are blocked on a decision that is genuinely the
    user's to make — one you cannot resolve from the request, the conversation,
    or a sensible default. Prefer picking the obvious option and saying so.

    Good reasons to ask: the request has two plausible readings that lead to
    materially different work; a choice depends on preference or context you
    cannot see. Bad reasons: confirming something already stated, asking for
    permission to continue, or offering a menu when one answer is clearly
    right.

    The interface adds an "Other" free-text field and a "Skip" control to every
    question, so never include those as options — an "Other" you supply
    yourself records no free text and tells you nothing. If the user skips,
    proceed on your best judgement and say what you assumed.

    Ask once. Put everything you need in this one call, then do the work with
    what you get back. Do not call this tool again in the same turn to refine
    an answer you just received: a second round reads as an interrogation, and
    the user came here for the work, not the questionnaire.

    Args:
        questions: The questions to ask. Each needs a short `header`, the full
            `question` text, and 2-4 `options` with a `label` and an optional
            `description`. Set `multiSelect` when several answers may apply.

    Returns:
        The user's selections, one line per question.
    """
    if not ask_user_question_enabled():
        return _error(
            "Asking the user structured questions is disabled in this "
            "environment. Ask your question in your reply instead."
        )

    try:
        normalized = normalize_questions(questions)
    except UserQuestionError as exc:
        logger.info("ask_user_question rejected a malformed payload: %s", exc)
        return _error(
            f"{exc} Fix the arguments and call again, or just ask in your reply."
        )

    tool_use_id = (tool_context.tool_use or {}).get("toolUseId", "")
    logger.info(
        "Pausing for user questions: count=%d tool_use_id=%s",
        len(normalized),
        tool_use_id,
    )

    # Raises InterruptException on the first pass; returns the client's payload
    # when the turn resumes. Strands only treats a **non-None** response as an
    # answer, so the SPA must always post something — "Skip" sends
    # `{"skipped": true}`, never null, or the interrupt would re-raise forever.
    response = tool_context.interrupt(
        name=INTERRUPT_NAME,
        reason={
            "type": "user_question_required",
            "toolUseId": tool_use_id,
            "questions": [
                q.model_dump(by_alias=True, exclude_none=True) for q in normalized
            ],
        },
    )

    return _success(format_answers(parse_answers(normalized, response)))
