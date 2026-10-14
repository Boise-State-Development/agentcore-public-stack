"""The paid arms: one fresh agent turn per (arm, question, sample).

The only difference between the arms is the tool the model gets:

* ``tier1`` — ``document_read`` as a deployment without the attachments KB has
  it (digest, ``pattern``, ``page_range``, index and text modes);
* ``tier2`` — the same tool with the ``query`` argument
  (``make_document_read_tool(..., searchable=True)``);
* ``tier2-guided`` — ``tier2`` with the attachment guidance naming search first,
  which is what production sends when the deployment has the attachments KB
  (``_build_attachment_guidance(search_available=True)``). ``tier2`` keeps the
  earlier "first with ``pattern``" sentence, which the second readout measured
  against it.

Everything else is held fixed and taken from production code: the tool itself,
reading the deployed rows, sidecars and knowledge base; the user message, built
the way the chat route builds it for a reference attachment (the question, then
``_build_attachment_guidance``, then ``render_references_section`` over the live
row); the model and its settings; and a short system prompt. The product's full
system prompt is not used. It is the same in both arms, and the arms differ only
in the tool.

A turn is capped at ``MAX_TOOL_CALLS`` tool calls. Over that, the hook cancels
further calls and the model has to answer with what it has, which is how a turn
that wanders is scored rather than left running. Every row records the tool
calls by mode, the token usage and the wall time.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from compaction_quality.score import is_correct

from .fixtures import Question

ARMS = ("tier1", "tier2", "tier2-guided")

MAX_TOOL_CALLS = 10

SYSTEM_PROMPT = (
    "You are a helpful assistant. The user has attached a document to this conversation. "
    "Answer their question from the document. End your reply with one final line of the "
    "form 'Answer: <the specific value, name or date they asked for>', with nothing else "
    "on that line. If the document does not say, end with 'Answer: not stated'."
)

#: The scored line. Prose before it may cite a distractor as context ("casual staff
#: get 3 days"); only this line has to name the right value and no other.
_ANSWER_LINE = re.compile(r"^[\s*_#>-]*answer[\s*_]*:[\s*_]*(.+?)[\s*_.]*$", re.IGNORECASE)


def final_answer(text: str) -> Optional[str]:
    """The last ``Answer: …`` line of a reply, or ``None`` when there is none."""
    for line in reversed((text or "").splitlines()):
        match = _ANSWER_LINE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None

#: Per-MTok input/output, Regional (us.*) rates. Source of truth is curated-models.ts;
#: these only size the pre-flight estimate and the report's cost column.
RATES: Dict[str, Tuple[float, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "us.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": (3.30, 16.50),
}

#: Pre-flight sizing per turn. A page read carries page images (~1.5k tokens a
#: page), and a turn often makes two or three calls, each re-sending the context.
ESTIMATE_INPUT_TOKENS = 40_000
ESTIMATE_OUTPUT_TOKENS = 700


@dataclass(frozen=True)
class Job:
    arm: str
    question: Question
    sample: int

    @property
    def key(self) -> str:
        return f"{self.arm}|{self.question.qid}|{self.sample}"


def jobs(questions: List[Question], arms: List[str], k: int, done: set) -> List[Job]:
    out = [Job(arm, q, s) for q in questions for arm in arms for s in range(k)]
    return [j for j in out if j.key not in done]


def estimate_usd(n_jobs: int, model_id: str) -> Optional[float]:
    rates = RATES.get(model_id)
    if rates is None:
        return None
    return n_jobs * (ESTIMATE_INPUT_TOKENS * rates[0] + ESTIMATE_OUTPUT_TOKENS * rates[1]) / 1e6


def cost_usd(usage: Dict[str, int], model_id: str) -> Optional[float]:
    rates = RATES.get(model_id)
    if rates is None:
        return None
    return (usage.get("inputTokens", 0) * rates[0] + usage.get("outputTokens", 0) * rates[1]) / 1e6


def _mode(tool_input: Dict[str, Any]) -> str:
    if (tool_input.get("query") or "").strip():
        return "query"
    if not tool_input.get("upload_id"):
        return "list"
    if (tool_input.get("page_range") or "").strip():
        return "page_range"
    if (tool_input.get("pattern") or "").strip():
        return "pattern"
    return "index"


async def user_message(question: Question, doc: Dict[str, Any], *, guided: bool = False) -> str:
    """The question as the chat route would send it with this attachment on the turn."""
    from apis.inference_api.chat.routes import _build_attachment_guidance
    from apis.shared.files.reference_attachments import ReferenceAttachment, render_references_section
    from apis.shared.files.repository import get_file_upload_repository

    meta = await get_file_upload_repository().get_file(doc["userId"], doc["uploadId"])
    refs = [ReferenceAttachment(meta=meta)]
    guidance = _build_attachment_guidance(
        [], [], [], [], references=refs, document_read_available=True, search_available=guided
    )
    section = render_references_section(refs, include_handle=True)
    return "\n\n".join(part for part in (question.question, guidance, section) if part)


async def run_job(job: Job, doc: Dict[str, Any], model_id: str, region: str,
                  temperature: Optional[float]) -> Dict[str, Any]:
    from strands import Agent
    from strands.hooks import BeforeToolCallEvent
    from strands.models.bedrock import BedrockModel

    from agents.builtin_tools.document_read_tool import make_document_read_tool

    tool = make_document_read_tool(doc["sessionId"], doc["userId"], searchable=job.arm != "tier1")
    config: Dict[str, Any] = {"model_id": model_id, "max_tokens": 2048}
    if temperature is not None:
        config["temperature"] = temperature
    agent = Agent(
        model=BedrockModel(region_name=region, **config),
        tools=[tool],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )
    calls: List[Dict[str, Any]] = []

    def on_tool(event: BeforeToolCallEvent) -> None:
        tool_input = dict(event.tool_use.get("input") or {})
        calls.append({"mode": _mode(tool_input), "input": {k: v for k, v in tool_input.items() if v not in ("", None)}})
        if len(calls) > MAX_TOOL_CALLS:
            event.cancel_tool = "Tool call limit reached for this question. Answer with what you have."

    agent.hooks.add_callback(BeforeToolCallEvent, on_tool)

    message = await user_message(job.question, doc, guided=job.arm == "tier2-guided")
    started = time.time()
    error = None
    answer = ""
    usage: Dict[str, int] = {}
    try:
        result = await agent.invoke_async(message)
        answer = str(result).strip()
        accumulated = getattr(result.metrics, "accumulated_usage", {}) or {}
        usage = {k: int(accumulated.get(k, 0)) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                          "cacheWriteInputTokens")}
    except Exception as exc:  # noqa: BLE001 - a failed turn is a scored row, not a crash
        error = f"{type(exc).__name__}: {exc}"[:300]

    q = job.question
    line = final_answer(answer)
    # No Answer line: score the whole reply, and say so in the row.
    scored = line if line is not None else answer
    return {
        "key": job.key, "arm": job.arm, "qid": q.qid, "doc": q.doc, "wording": q.wording, "sample": job.sample,
        "answer": answer, "answerLine": line,
        "correct": bool(scored) and is_correct(scored, q.expected, q.forbidden),
        "toolCalls": calls, "modes": [c["mode"] for c in calls], "usage": usage,
        "costUsd": cost_usd(usage, model_id), "seconds": round(time.time() - started, 2),
        "error": error, "modelId": model_id,
    }


async def run_all(pending: List[Job], seeded: Dict[str, Dict[str, Any]], *, model_id: str, region: str,
                  temperature: Optional[float], concurrency: int, on_row) -> None:
    gate = asyncio.Semaphore(concurrency)

    async def one(job: Job) -> None:
        async with gate:
            row = await run_job(job, seeded[job.question.doc], model_id, region, temperature)
        on_row(row)

    await asyncio.gather(*(one(j) for j in pending))
