"""Wire contract for the ``ask_user_question`` mid-turn clarification prompt.

The agent asks the user one to four multiple-choice questions, the turn pauses
on a Strands interrupt, the SPA renders a picker, and the user's selections
come back as the tool's result. Three components share these shapes:

- ``agents.builtin_tools.ask_user_question`` — raises the interrupt with a
  normalized ``questions`` payload and formats the answers for the model.
- ``agents.main_agent.streaming.stream_coordinator`` — turns a pending
  interrupt into the ``user_question_required`` SSE event.
- ``apis.shared.sessions.models.PendingInterrupt`` — persists the same
  questions (JSON-encoded) so a browser refresh rehydrates the picker.

Design notes
------------
* **The model's payload is untrusted input.** ``normalize_questions`` clamps
  counts, trims strings, drops malformed entries and de-duplicates option
  labels, raising :class:`UserQuestionError` when nothing renderable survives.
  A tool-visible error is strictly better than an SSE event the SPA cannot
  render — that would strand the turn paused with no prompt on screen.
* **Answers arrive in whatever shape the client sent.** ``parse_answers`` is
  deliberately tolerant (dict-keyed, positional, or a bare string) because a
  strict parser that rejects an answer leaves the user's only route out of a
  paused turn broken. Anything unrecognized degrades to "skipped", never to an
  exception.
* **Cost.** The tool spec is a constant in ``toolConfig`` and the questions
  travel on the SSE channel, not in the prompt. Only the formatted answer
  block re-enters the conversation, which is why ``format_answers`` writes one
  compact line per question rather than echoing the option catalog back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Bounds mirror the rendered UI: the SPA paginates questions ("1 of 4") and
# lays options out vertically. Beyond these the picker stops being a quick
# choice and the model should just ask in prose instead.
MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_HEADER_CHARS = 12
MAX_QUESTION_CHARS = 200
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 240
# Free-text ("Other") answers are user-authored and land in the prompt. Bounded
# for the same reason every other per-turn payload is (CLAUDE.md token tenet).
MAX_FREE_TEXT_CHARS = 2000

# The picker always renders its own "Other" (with a free-text field) and
# "Skip". A model-supplied lookalike is worse than a duplicate: picking it
# records the bare string "Other" as the answer with no way to say what, so the
# model learns nothing and the user cannot tell the two chips apart. The tool
# description says not to emit these; measured against real models it is not
# reliably obeyed (Haiku 4.5 added "Other" on roughly half of sampled turns),
# so they are stripped here rather than trusted away.
BANNED_OPTION_LABELS = frozenset(
    {
        "other",
        "other (please specify)",
        "something else",
        "skip",
        "skip this",
        "none",
        "none of the above",
        "no preference",
        "not sure",
        "let you decide",
        "you decide",
    }
)


class UserQuestionError(ValueError):
    """The model's ``questions`` argument could not be rendered as a prompt."""


class QuestionOption(BaseModel):
    """One selectable answer."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="Short display text for the choice")
    description: Optional[str] = Field(
        default=None,
        description="Optional one-line explanation of what the choice means",
    )


class UserQuestion(BaseModel):
    """One question in the prompt.

    ``header`` is the short chip label the SPA shows above the question and the
    key answers are correlated by; ``question`` is the full sentence.
    """

    model_config = ConfigDict(populate_by_name=True)

    header: str = Field(..., description="Short chip label, <= 12 chars")
    question: str = Field(..., description="The full question text")
    multi_select: bool = Field(
        default=False,
        alias="multiSelect",
        description="Whether more than one option may be chosen",
    )
    options: List[QuestionOption] = Field(..., description="2-4 choices")


class UserQuestionAnswer(BaseModel):
    """The user's response to one question, as parsed from the resume payload."""

    model_config = ConfigDict(populate_by_name=True)

    header: str
    selected: List[str] = Field(default_factory=list)
    text: Optional[str] = Field(
        default=None,
        description='Free-text answer from the "Other" affordance',
    )
    skipped: bool = Field(default=False)


class UserQuestionRequiredEvent(BaseModel):
    """SSE event telling the SPA to render the picker for a paused turn.

    Mirrors ``apis.shared.tool_approval.models.ToolApprovalRequiredEvent``: the
    SPA renders the prompt, the user answers, and the decision POSTs back to
    ``/invocations`` as an ``interrupt_responses`` entry keyed by
    ``interruptId``.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = "user_question_required"
    interrupt_id: str = Field(..., alias="interruptId")
    tool_use_id: str = Field(..., alias="toolUseId")
    questions: List[UserQuestion]

    def to_sse_format(self) -> str:
        payload = self.model_dump(by_alias=True, exclude_none=True)
        return (
            f"event: user_question_required\n"
            f"data: {json.dumps(payload)}\n\n"
        )


# ---------------------------------------------------------------------------
# Normalization (model output -> renderable questions)
# ---------------------------------------------------------------------------


def _clean(value: Any, limit: int) -> str:
    """Coerce to a trimmed, length-capped single-line string."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text[:limit]


def _clean_header(value: Any) -> str:
    """Trim a header to the chip budget without cutting mid-word.

    A hard slice is what shipped "DASHBOARD PU" to the UI when the model wrote
    "Dashboard Purpose" — the chip is a label the user reads, and a severed
    word reads as a rendering bug. Drop whole trailing words instead, and fall
    back to the hard slice only when the very first word is itself over budget.
    """
    text = str(value).strip() if value is not None else ""
    if not text or len(text) <= MAX_HEADER_CHARS:
        return text

    words = text.split()
    kept: List[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if len(candidate) > MAX_HEADER_CHARS:
            break
        kept.append(word)

    return " ".join(kept) if kept else text[:MAX_HEADER_CHARS]


def _normalize_options(raw: Any) -> List[QuestionOption]:
    """Build the option list, dropping blanks, duplicates and Other/Skip clones.

    Duplicate labels are dropped rather than renamed: two identical choices are
    indistinguishable to the user, and the answer payload correlates by label.

    Options that restate the picker's own Other/Skip affordances are dropped
    too — see :data:`BANNED_OPTION_LABELS`. A question that falls below
    ``MIN_OPTIONS`` as a result is discarded by the caller: the model padded a
    binary choice with an escape hatch the interface already provides, and the
    real question is one the picker can still express.
    """
    if not isinstance(raw, list):
        return []

    options: List[QuestionOption] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            label, description = _clean(entry, MAX_LABEL_CHARS), None
        elif isinstance(entry, dict):
            label = _clean(entry.get("label"), MAX_LABEL_CHARS)
            description = _clean(entry.get("description"), MAX_DESCRIPTION_CHARS) or None
        else:
            continue

        if not label:
            continue
        folded = label.casefold()
        if folded in seen:
            continue
        if folded.rstrip(".!") in BANNED_OPTION_LABELS:
            logger.debug("Dropped model-supplied escape-hatch option: %r", label)
            continue
        seen.add(folded)
        options.append(QuestionOption(label=label, description=description))
        if len(options) == MAX_OPTIONS:
            break

    return options


def normalize_questions(raw: Any) -> List[UserQuestion]:
    """Validate and clamp the model's ``questions`` argument.

    Args:
        raw: The tool's ``questions`` parameter, as the model produced it.

    Returns:
        Between 1 and ``MAX_QUESTIONS`` renderable questions.

    Raises:
        UserQuestionError: If no question survives normalization. The tool
            surfaces this to the model as a tool error so it can retry or fall
            back to asking in prose — far better than pausing the turn behind
            a prompt the SPA cannot draw.
    """
    if not isinstance(raw, list) or not raw:
        raise UserQuestionError(
            "`questions` must be a non-empty list of question objects."
        )

    questions: List[UserQuestion] = []
    headers: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue

        question_text = _clean(entry.get("question"), MAX_QUESTION_CHARS)
        if not question_text:
            continue

        options = _normalize_options(entry.get("options"))
        if len(options) < MIN_OPTIONS:
            continue

        # Header is a display convenience; fall back to a positional label so a
        # model that omits it still gets a renderable (and correlatable) prompt.
        header = _clean_header(entry.get("header"))
        if not header:
            header = f"Q{len(questions) + 1}"
        # Answers correlate by header, so collisions must not survive.
        base, suffix = header, 2
        while header.casefold() in headers:
            header = f"{base[: MAX_HEADER_CHARS - 2]} {suffix}"
            suffix += 1
        headers.add(header.casefold())

        questions.append(
            UserQuestion(
                header=header,
                question=question_text,
                multi_select=bool(entry.get("multiSelect") or entry.get("multi_select")),
                options=options,
            )
        )
        if len(questions) == MAX_QUESTIONS:
            break

    if not questions:
        raise UserQuestionError(
            "No renderable question found: each entry needs a `question` string "
            f"and at least {MIN_OPTIONS} options with non-empty labels."
        )

    return questions


# ---------------------------------------------------------------------------
# Answer parsing (client resume payload -> answers)
# ---------------------------------------------------------------------------


def _coerce_one_answer(header: str, raw: Any) -> UserQuestionAnswer:
    """Interpret a single question's answer, however the client shaped it."""
    if raw is None:
        return UserQuestionAnswer(header=header, skipped=True)

    if isinstance(raw, str):
        text = _clean(raw, MAX_FREE_TEXT_CHARS)
        return UserQuestionAnswer(
            header=header, selected=[text] if text else [], skipped=not text
        )

    if isinstance(raw, list):
        selected = [s for s in (_clean(v, MAX_LABEL_CHARS) for v in raw) if s]
        return UserQuestionAnswer(
            header=header, selected=selected, skipped=not selected
        )

    if isinstance(raw, dict):
        if raw.get("skipped"):
            return UserQuestionAnswer(header=header, skipped=True)

        raw_selected = raw.get("selected", raw.get("labels"))
        if isinstance(raw_selected, str):
            raw_selected = [raw_selected]
        selected = [
            s
            for s in (
                _clean(v, MAX_LABEL_CHARS)
                for v in (raw_selected if isinstance(raw_selected, list) else [])
            )
            if s
        ]
        text = _clean(raw.get("text", raw.get("other")), MAX_FREE_TEXT_CHARS) or None
        return UserQuestionAnswer(
            header=header,
            selected=selected,
            text=text,
            skipped=not selected and not text,
        )

    return UserQuestionAnswer(header=header, skipped=True)


def parse_answers(
    questions: List[UserQuestion], response: Any
) -> List[UserQuestionAnswer]:
    """Map a resume payload onto the questions that were asked.

    Accepted shapes, in the order they are tried:

    * ``{"answers": {"<header>": ...}}`` — the canonical form the SPA sends.
    * ``{"answers": [...]}`` — positional, aligned with ``questions``.
    * ``{"skipped": true}`` / ``"skipped"`` — the user dismissed the prompt.
    * a bare string — a free-text reply, attributed to the first question.

    Never raises. An unrecognized payload yields all-skipped answers, so the
    turn always resumes: the user's escape hatch out of a pause must not be
    able to fail on a shape mismatch.
    """
    headers = [q.header for q in questions]

    if isinstance(response, str):
        stripped = response.strip()
        if stripped.casefold() in {"skipped", "skip", ""}:
            return [UserQuestionAnswer(header=h, skipped=True) for h in headers]
        return [
            _coerce_one_answer(headers[0], stripped),
            *(UserQuestionAnswer(header=h, skipped=True) for h in headers[1:]),
        ]

    if not isinstance(response, dict):
        return [UserQuestionAnswer(header=h, skipped=True) for h in headers]

    if response.get("skipped") and "answers" not in response:
        return [UserQuestionAnswer(header=h, skipped=True) for h in headers]

    answers = response.get("answers", response)

    if isinstance(answers, dict):
        lowered = {str(k).casefold(): v for k, v in answers.items()}
        return [
            _coerce_one_answer(h, lowered.get(h.casefold()))
            for h in headers
        ]

    if isinstance(answers, list):
        return [
            _coerce_one_answer(h, answers[i] if i < len(answers) else None)
            for i, h in enumerate(headers)
        ]

    return [UserQuestionAnswer(header=h, skipped=True) for h in headers]


def format_answers(answers: List[UserQuestionAnswer]) -> str:
    """Render the answers as the tool result the model reads.

    One line per question, and nothing else: the option catalog the model
    already wrote is not echoed back, because this text re-enters the
    conversation and is paid for on every subsequent turn.
    """
    lines: List[str] = []
    for answer in answers:
        if answer.skipped:
            lines.append(f"- {answer.header}: (no answer — the user skipped this)")
            continue
        parts = list(answer.selected)
        if answer.text:
            parts.append(f'"{answer.text}"')
        lines.append(f"- {answer.header}: {', '.join(parts)}")

    if all(a.skipped for a in answers):
        return (
            "The user skipped the questions. Proceed with your best judgement "
            "and state the assumptions you made; do not ask again.\n"
            + "\n".join(lines)
        )

    return "The user answered:\n" + "\n".join(lines)


def encode_questions(questions: List[UserQuestion]) -> str:
    """JSON-encode questions for DynamoDB persistence.

    Stored as a string for the same reason ``PendingInterrupt.tool_input`` is:
    DynamoDB coerces floats to ``Decimal`` on the way in, and the SPA renders
    the decoded payload verbatim.
    """
    return json.dumps([q.model_dump(by_alias=True, exclude_none=True) for q in questions])


def decode_questions(encoded: Optional[str]) -> List[Dict[str, Any]]:
    """Best-effort inverse of :func:`encode_questions` for the reload path."""
    if not encoded:
        return []
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []
