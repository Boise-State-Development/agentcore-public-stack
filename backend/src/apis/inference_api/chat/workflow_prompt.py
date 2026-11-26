"""A workflow run's prompt steps (and checklist steps): model calls over the run's files, never a turn.

``docs/specs/workflow-designer.md`` §21.1, §21.3. A ``workflow_prompt`` invocation names prompt
steps of one approved run, on the run's **source** session. It does not go through
``_run_invocation``: no agent is built, no history is restored, no tools are offered, none of the
source's system prompt is sent, and no session lease is taken. Each step's claim on its
``HANDOFF#`` row (``open → running``, conditional) is what keeps a step from running twice.

The request layout is the one §21.1 measured, and it is what makes a run of N steps cheap:

    system:   one constant per deployment (``SYSTEM_PROMPT``)
    messages: [ user: documents…, cachePoint, earlier results (if any), instruction ]

The documents are the run's ``fileIds`` in order, under names fixed by position, so the prefix up
to the cachePoint is byte-identical across the run's steps. The first claimed step on a model is
called **alone**, which writes that prefix to the cache; the rest then run through ``run_bounded``
at the run's ``limits.concurrency`` and read it. A request sent while the warm call is still in
flight reads nothing, so the warm call must finish first: the price is one step's latency, the
saving 3.6–3.9× on input (§21.1).

Nothing here is on a turn's path, and nothing reaches the source conversation but a ``C#`` row
per call (``callKind: "workflow_prompt"``, no ``messageId``, ``runId`` and ``stepId``), which never
counts as a turn and never moves the context meter. A step's answer reaches the source later as a
delivery part, appended like any other.

The stream carries ``workflow_prompt_step`` frames as each step settles, then ``done``; no text
is streamed. A closed tab does not cancel: the work runs in a task of its own, finishes, and writes
its rows, and the run refresh reads them. A stopped run does: the run row is re-read every
``RUN_POLL_SECONDS``, and on ``stopped`` the calls in flight are cancelled and every claimed step
stops ``user_stopped``.

**Checklist steps** (§22.3) ride the same invocation while ``checklist_steps_enabled()``: the same
checks, claim and stop, with one call per field set. Their calls carry a constant tool and frame of
their own, so they run as their own warm group per model (``workflow_checklist.ChecklistBranch``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from apis.shared.auth.models import User
from apis.shared.extraction.batch import run_bounded
from apis.shared.files.whole_document import WholeDocument, WholeDocumentError, read_whole_document
from apis.shared.feature_flags import checklist_steps_enabled, prompt_steps_enabled
from apis.shared.handoff.models import KIND_CHECKLIST_STEP, KIND_PROMPT_STEP, PromptStopReason
from apis.shared.handoff.prompt_models import prompt_model_supported
from apis.shared.handoff.prompt_steps import (
    PromptStepStore,
    WorkflowPromptRequest,
    get_prompt_step_store,
    mark_cut_off,
)
from apis.shared.handoff.run_reads import StepReadsNotSettled, render_step_reads
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep, WorkflowRunStore, get_workflow_run_store
from apis.shared.handoff.send_back import DeliveryRefused
from apis.shared.models.cache_points import bedrock_cache_points_supported
from apis.shared.workflows.format import READ_FILES, READ_REQUEST

logger = logging.getLogger(__name__)

#: The ``callKind`` on each step's cost row.
CALL_KIND = "workflow_prompt"

#: Output tokens a step may write (§21.3). The stored answer is capped lower, at the delivery cap.
#:
#: **Reasoning counts against it.** Haiku 5.5 and Sonnet 5.5 reason at their default effort, and at
#: 4,000 three of ten NSF lens steps on dev were cut off mid-sentence: one Haiku step spent all
#: 4,000 tokens for 653 characters of answer. 16,000 leaves room for the reasoning and an answer the
#: system prompt keeps under 1,200 words. An answer that still reaches it is kept and marked
#: (``mark_cut_off``), and its cost row says ``truncated``.
MAX_OUTPUT_TOKENS = 16_000

#: One step's call, from request to its last token, sized from the measured output rate.
#:
#: The slowest whole-call rate on dev was Sonnet 5.5's 116 output tokens a second (reasoning and the
#: time to first token included; Haiku 5.5's slowest was 184). 16,000 tokens at that rate is 138 s,
#: so 180 s with a 30% margin. That keeps a run's longest shape (8 steps on one model at the default
#: concurrency of 4: the warm call, then two waves) at 540 s, inside the 600 s the Runtime stream
#: allows. A run that takes longer still finishes: a closed stream does not cancel the work, and the
#: run refresh reads the rows.
STEP_TIMEOUT_SECONDS = 180.0

#: How often a running invocation re-reads its run row for a stop.
RUN_POLL_SECONDS = 2.0

#: The system prompt of every prompt step, the same for every run and every deployment, so it sits
#: in the cached prefix ahead of the documents. It names the task and bounds the answer to what the
#: step's row keeps (``SEND_BACK_MAX_TOKENS``).
SYSTEM_PROMPT = (
    "You are carrying out one step of a workflow the user started. The user's message gives you the "
    "documents the workflow shares, then any results from earlier steps, then this step's instruction. "
    "Follow the instruction, working only from what the message gives you, and answer once, in "
    "Markdown. Your answer is passed on to the user, and may be read by a later step or combined with "
    "other steps' answers, so make it complete on its own and do not address the workflow itself. "
    "Keep it under 1,200 words."
)

#: Ahead of an instruction, so the model can tell this step's own ask from the material it reads.
INSTRUCTION_HEADING = "This step's instruction:"

#: Ahead of the user's request for the whole run, for a step that reads it and has its own instruction.
RUN_REQUEST_HEADING = "The user's request for this workflow:"


def run_request_for(run: WorkflowRun, step: WorkflowRunStep) -> str:
    """The run's request as a step's message carries it: only when the step reads it
    (``reads_input``) and its own instruction is not that same request (a step with no
    instruction of its own is sent it as one)."""
    shared = (run.request or "").strip()
    if not step.reads_input(READ_REQUEST) or not shared or shared == (step.request or "").strip():
        return ""
    return shared


# --- Frames ---------------------------------------------------------------------------------------


def _frame(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def step_frame(
    run_id: str,
    step_id: str,
    status: str,
    *,
    stopped_reason: Optional[str] = None,
    reason: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """One step settled: ``done``, ``stopped`` (with ``stoppedReason``), or ``skipped`` (with ``reason``).

    ``skipped`` is a step this invocation did not run and did not write: not a prompt or checklist
    step of the run (or a checklist step while those are off), not ``open`` (another invocation
    claimed it, or it settled), one whose reads have not settled, or one whose reads a delivery rule
    refused. A client treats it as not started.

    ``kind`` (``prompt`` or ``checklist``, §22.3) is additive: present whenever the step is one of
    the run's, absent on a ``not_in_run`` skip.
    """
    data: Dict[str, Any] = {"type": "workflow_prompt_step", "runId": run_id, "stepId": step_id, "status": status}
    if kind:
        data["kind"] = kind
    if stopped_reason:
        data["stoppedReason"] = stopped_reason
    if reason:
        data["reason"] = reason
    return _frame("workflow_prompt_step", data)


def refused_frame(run_id: str, reason: str) -> str:
    """The whole request was refused; nothing was claimed or written."""
    return _frame("workflow_prompt_refused", {"type": "workflow_prompt_refused", "runId": run_id, "reason": reason})


def done_frame(run_id: str) -> str:
    return _frame("done", {"type": "done", "runId": run_id})


# --- The request layout ---------------------------------------------------------------------------


def document_name(index: int, filename: str) -> str:
    """A document block's name: its position and its filename, in the characters Bedrock accepts."""
    from agents.main_agent.multimodal.file_sanitizer import FileSanitizer

    stem = filename.rsplit(".", 1)[0] if "." in (filename or "") else (filename or "")
    cleaned = FileSanitizer.sanitize_filename(stem)[:80].strip()
    return f"{index + 1} {cleaned}".strip() if cleaned else f"Document {index + 1}"


def document_blocks(documents: Sequence[WholeDocument]) -> List[Dict[str, Any]]:
    """The run's documents as content blocks, in order. Pure: the same documents give the same blocks."""
    blocks: List[Dict[str, Any]] = []
    for index, document in enumerate(documents):
        if document.native:
            blocks.append(
                {
                    "document": {
                        "format": "pdf",
                        "name": document_name(index, document.filename),
                        "source": {"bytes": document.pdf_bytes},
                    }
                }
            )
        elif document.text:
            blocks.append({"text": document.text})
    return blocks


def step_content(
    prefix: Sequence[Dict[str, Any]],
    reads_block: str,
    instruction: str,
    *,
    cache_point: bool,
    run_request: str = "",
) -> List[Dict[str, Any]]:
    """One step's user message: the shared prefix, a cachePoint after it, then the step's own tail.

    The tail is the user's run request (when the step reads it and it differs from the step's own
    instruction), the results it reads, and its instruction. The cachePoint goes only where there
    is a prefix to cache and the model takes one, so each step writes only its own tail.
    """
    content: List[Dict[str, Any]] = list(prefix)
    if prefix and cache_point:
        content.append({"cachePoint": {"type": "default"}})
    if run_request.strip():
        content.append({"text": f"{RUN_REQUEST_HEADING}\n\n{run_request.strip()}"})
    if reads_block.strip():
        content.append({"text": reads_block.strip()})
    content.append({"text": f"{INSTRUCTION_HEADING}\n\n{instruction.strip()}"})
    return content


# --- What one call returns ------------------------------------------------------------------------


@dataclass
class CallResult:
    """One model call, as the stream ended it."""

    stop_reason: str
    text: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    #: The provider accepted the request, so a cancel after this still billed a prompt.
    answered: bool = False
    first_token_at: Optional[float] = None
    #: The reply's content blocks as the stream ended it (a checklist call's ``toolUse`` is here).
    content: List[Dict[str, Any]] = field(default_factory=list)


CallModel = Callable[[Any, List[Dict[str, Any]], threading.Event], Awaitable[CallResult]]
RenderReads = Callable[[str, str, WorkflowRun, WorkflowRunStep], Awaitable[str]]


def _reported(usage: Optional[Dict[str, Any]]) -> bool:
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    return bool(usage) and any((usage or {}).get(key) for key in keys)


async def call_bedrock(
    model: Any,
    messages: List[Dict[str, Any]],
    cancel: threading.Event,
    *,
    tool_specs: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Dict[str, Any]] = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> CallResult:
    """Stream one Converse call through Strands: a prompt step's (no tools, its constant system prompt) by default."""
    from strands.event_loop.streaming import stream_messages

    result = CallResult(stop_reason="error")
    parts: List[str] = []
    stop: Optional[tuple] = None
    async for event in stream_messages(
        model,
        None,
        messages,
        list(tool_specs or []),
        tool_choice=tool_choice,
        system_prompt_content=[{"text": system_prompt}],
        invocation_state={},
        model_state={},
        cancel_signal=cancel,
    ):
        if "stop" in event:
            stop = event["stop"]
            continue
        chunk = event.get("event") if isinstance(event, dict) else None
        if isinstance(chunk, dict) and "messageStart" in chunk:
            result.answered = True
        text = event.get("data") if isinstance(event, dict) else None
        if isinstance(text, str) and text:
            if result.first_token_at is None:
                result.first_token_at = time.time()
            result.answered = True
            parts.append(text)
    if stop is None:
        result.text = "".join(parts)
        return result
    stop_reason, message, usage, metrics = stop
    result.stop_reason = str(stop_reason)
    result.usage = dict(usage or {})
    result.metrics = dict(metrics or {})
    result.content = [block for block in (message or {}).get("content") or [] if isinstance(block, dict)]
    result.text = "".join(parts) if parts else "".join(
        block.get("text", "") for block in (message or {}).get("content") or [] if isinstance(block, dict)
    )
    return result


def build_bedrock_model(model_id: str) -> Any:
    """A ``BedrockModel`` for one model's steps: ``maxTokens`` 4,000, the model's default effort, no cache config.

    With no ``cache_config`` Strands places no cachePoint of its own; the one ``step_content`` puts
    after the documents is the only one sent.
    """
    from botocore.config import Config as BotocoreConfig
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        boto_client_config=BotocoreConfig(
            read_timeout=int(STEP_TIMEOUT_SECONDS),
            connect_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


# --- Checks ---------------------------------------------------------------------------------------


async def render_reads(user_id: str, session_id: str, run: WorkflowRun, step: WorkflowRunStep) -> str:
    """The results ``step`` reads, as app-api's ``…/prepare`` renders them (``render_step_reads``).

    Raises ``StepReadsNotSettled`` while a read step is still running, and ``DeliveryRefused`` when
    a read part breaks a delivery rule; the caller skips the step unclaimed on either.
    """
    block, _parts = await render_step_reads(user_id, session_id, run, step)
    return block


@dataclass
class ModelPlan:
    """One model's checks and limits, resolved once for every step on it."""

    model_id: str
    refusal: Optional[PromptStopReason] = None
    context_window: Optional[int] = None
    document_budget: Optional[int] = None
    pdf_page_limit: Optional[int] = None
    cache_point: bool = False
    #: False for a TEXT-only model: it runs a step that reads only earlier steps, never one that
    #: sends the documents (``prompt_model_supported(reads_files=True)``).
    reads_documents: bool = True


async def resolve_model_plan(user: User, model_id: str) -> ModelPlan:
    """Retirement first, then access as the caller, then a catalog row, then this path's limits.

    A model with no row is refused (it would price to nothing), as is one this call path cannot send
    to (``prompt_model_supported``, the rule the run sheet blocks by too).
    """
    from apis.shared.files.attachment_plan import ModelFacts, pdf_page_limit
    from apis.shared.models.retirement import resolve_effective_model
    from apis.shared.rbac.service import get_app_role_service

    if not model_id:
        return ModelPlan(model_id="", refusal="model_refused")
    effective = await resolve_effective_model(model_id)
    if effective is None or effective.denied:
        return ModelPlan(model_id=model_id, refusal="model_refused")
    record = effective.record
    if record is None:
        # No row: refused when the catalog was read; unknown when it was not, and a call that
        # could not be priced is still refused here, because nothing could meter it.
        return ModelPlan(model_id=effective.model_id, refusal="model_refused")
    allowed = await get_app_role_service().can_access_model(
        user, effective.model_id, record=record, catalog_read=effective.catalog_read
    )
    if not allowed:
        return ModelPlan(model_id=effective.model_id, refusal="model_refused")
    if not prompt_model_supported(record, reads_files=False):
        return ModelPlan(model_id=effective.model_id, refusal="model_refused")

    context_window = int(record.max_input_tokens) if record.max_input_tokens else None
    facts = ModelFacts(model_id=effective.model_id, context_window=context_window)
    return ModelPlan(
        model_id=effective.model_id,
        context_window=context_window,
        document_budget=single_call_document_budget(context_window, record.long_context_threshold_tokens),
        pdf_page_limit=pdf_page_limit(facts),
        cache_point=bedrock_cache_points_supported(effective.model_id),
        reads_documents=prompt_model_supported(record, reads_files=True),
    )


#: Share of a model's single-call input limit the documents may take. The rest holds the system
#: prompt, the run request, the instruction, earlier steps' results (each capped at
#: ``SEND_BACK_MAX_TOKENS``) and, on a checklist step, the ``record_checklist`` tool.
SINGLE_CALL_DOCUMENT_SHARE = 0.75


def single_call_document_budget(context_window: Optional[int], long_context_threshold: Optional[int]) -> Optional[int]:
    """Tokens of documents one prompt or checklist call may send, or ``None`` when the row names no limit.

    A step is one call with no history, so the chat compaction ceiling (which bounds a conversation
    that grows every turn) is not its limit. Its limit is the model's input window, or the
    long-context line where the row declares one (Haiku 5.5's 5x rate card past 100K), whichever
    is lower, so a step never crosses onto the second rate card.
    """
    limits = [int(v) for v in (context_window, long_context_threshold) if v]
    if not limits:
        return None
    return int(min(limits) * SINGLE_CALL_DOCUMENT_SHARE)


def document_tokens(documents: Sequence[WholeDocument], model_id: str) -> int:
    """The documents' input tokens on ``model_id``: a native PDF's text and pages, or the text sent."""
    from apis.shared.files.attachment_plan import CLASS_PDF, MODE_NATIVE, ModelFacts, document_cost
    from apis.shared.files.tokenizer_costs import text_cost, tokenizer_factor

    facts_model = ModelFacts(model_id=model_id)
    factor = tokenizer_factor(model_id)
    total = 0
    for document in documents:
        if document.native and document.facts is not None:
            total += document_cost(document.facts, CLASS_PDF, MODE_NATIVE, facts_model)
        else:
            total += text_cost(math.ceil(len(document.text or "") / 4), factor)
    return total


def too_large(documents: Sequence[WholeDocument], plan: ModelPlan) -> bool:
    """Over the document budget or a PDF over the model's page limit. Never truncated (§21.3)."""
    if plan.pdf_page_limit and any(d.native and (d.pages or 0) > plan.pdf_page_limit for d in documents):
        return True
    if plan.document_budget is not None and document_tokens(documents, plan.model_id) > plan.document_budget:
        return True
    return False


async def check_quota(user: User, session_id: str) -> bool:
    """The turn's quota check, for the whole request. Fails open, as a turn's does."""
    from apis.shared.quota import get_quota_checker, is_quota_enforcement_enabled

    if not is_quota_enforcement_enabled():
        return True
    try:
        result = await get_quota_checker().check_quota(user=user, session_id=session_id, session_total_cost=None)
    except Exception:  # noqa: BLE001 - a quota-service error lets the work run
        logger.error("workflow prompt: quota check failed", exc_info=True)
        return True
    return bool(result.allowed)


# --- Metering -------------------------------------------------------------------------------------


async def meter_call(
    *,
    user_id: str,
    session_id: str,
    model_id: str,
    usage: Dict[str, Any],
    metrics: Dict[str, Any],
    started: float,
    ended: float,
    fingerprint: Optional[Dict[str, Any]],
    attributes: Dict[str, Any],
    usage_estimate: Optional[Dict[str, Any]] = None,
    call_kind: str = CALL_KIND,
) -> Optional[str]:
    """Write one step call's ``C#`` row on the source session. Returns its ``cacheStatus``. Never raises.

    Priced from the catalog like ``document_extract``'s call, with the coordinator's long-context
    rule (``CostCalculator`` applies it). ``callKind`` (``workflow_prompt``, or
    ``workflow_checklist`` for a checklist step's call) and no ``messageId`` keep it off the turn
    counts and the context meter.
    """
    from apis.shared.costs.calculator import CostCalculator
    from apis.shared.costs.pricing_config import create_pricing_snapshot
    from apis.shared.observability.emf import emit_unmetered_model_call
    from apis.shared.sessions.metadata import store_message_metadata
    from apis.shared.sessions.models import (
        Attribution,
        LatencyMetrics,
        MessageMetadata,
        ModelInfo,
        PricingSnapshot,
        TokenUsage,
    )

    try:
        snapshot = None
        try:
            raw = await create_pricing_snapshot(model_id)
            snapshot = PricingSnapshot.model_validate(raw) if raw else None
        except Exception:  # noqa: BLE001
            logger.warning("workflow prompt: pricing lookup failed", exc_info=True)

        cost = None
        long_context = False
        if snapshot is not None:
            pricing = snapshot.model_dump(by_alias=True)
            try:
                total, breakdown = CostCalculator.calculate_message_cost(usage, pricing)
                cost = {
                    "total": total,
                    "inputCost": breakdown.input_cost,
                    "outputCost": breakdown.output_cost,
                    "cacheReadCost": breakdown.cache_read_cost,
                    "cacheWriteCost": breakdown.cache_write_cost,
                }
                long_context = CostCalculator.is_long_context(usage, pricing)
            except Exception:  # noqa: BLE001
                logger.warning("workflow prompt: cost calculation failed", exc_info=True)
        if cost is None:
            emit_unmetered_model_call(
                model_id, "calculation_failed" if snapshot else "no_pricing", surface=call_kind, session_id=session_id
            )

        latency_ms = metrics.get("latencyMs")
        ttfb_ms = metrics.get("timeToFirstByteMs")
        extra: Dict[str, Any] = {"callKind": call_kind, **attributes}
        if fingerprint:
            extra["prefixFingerprints"] = fingerprint
        if usage_estimate:
            extra["usageEstimated"] = usage_estimate
        if long_context:
            extra["longContext"] = True
        metadata = MessageMetadata(
            latency=LatencyMetrics(
                time_to_first_token=int(ttfb_ms) if ttfb_ms else None,
                end_to_end_latency=int(latency_ms) if latency_ms else int((ended - started) * 1000),
            ),
            token_usage=TokenUsage(
                input_tokens=int(usage.get("inputTokens") or 0),
                output_tokens=int(usage.get("outputTokens") or 0),
                total_tokens=int(usage.get("totalTokens") or 0),
                cache_read_input_tokens=usage.get("cacheReadInputTokens"),
                cache_write_input_tokens=usage.get("cacheWriteInputTokens"),
            ),
            model_info=ModelInfo(model_id=model_id, model_name=model_id, provider="bedrock", pricing_snapshot=snapshot),
            attribution=Attribution(
                user_id=user_id, session_id=session_id, timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=cost,
            **extra,
        )
        observability = await store_message_metadata(
            session_id=session_id, user_id=user_id, message_id=None, message_metadata=metadata
        )
        return (observability or {}).get("cacheStatus")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - metering must never fail a step that ran
        logger.warning("workflow prompt: metering the call failed", exc_info=True)
        return None


def estimate_cancelled_usage(
    *,
    model_id: str,
    prompt_tokens: int,
    cached_prefix: Optional[int],
    cache_point: bool,
    partial_text: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A cancelled call's usage, from the request we built: ``(usage, usageEstimated)``.

    The provider billed the prompt and what it streamed, but its terminal usage never arrived. The
    prompt is our own estimate of what we sent; the cache split comes from the warm call's reported
    prefix when there was one, like a cancelled brief's (``build_side_call_usage``).
    """
    from agents.main_agent.streaming.interrupted_usage import (
        METHOD_ESTIMATE,
        attribute_input_tokens,
        estimate_text_tokens,
        min_cacheable_prefix_tokens,
    )

    buckets, cache_method = attribute_input_tokens(
        prompt_tokens,
        cache_points_sent=cache_point,
        prior_cached_prefix=cached_prefix or None,
        min_cacheable_tokens=min_cacheable_prefix_tokens(model_id),
    )
    output_tokens = estimate_text_tokens(partial_text or "")
    usage = {
        **buckets,
        "outputTokens": output_tokens,
        "totalTokens": sum(buckets.values()) + output_tokens,
    }
    estimate = {
        "reason": "interrupted",
        "inputTokens": METHOD_ESTIMATE,
        "outputTokens": METHOD_ESTIMATE,
        "cacheSplit": cache_method,
    }
    return usage, estimate


def prefix_key(
    model_id: str,
    prefix: Sequence[Dict[str, Any]],
    *,
    system: str = SYSTEM_PROMPT,
    tools: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """What a step call's cached prefix is made of: the model, the tools, the system prompt and the documents.

    Every prompt-step call has the same ``toolConfigHash`` (no tools) and ``systemPromptHash`` (one
    constant), so those two alone would make any earlier prompt-step row a step's predecessor: a
    Sonnet group's warm write would read as a missed read of the Haiku group's entry, and a run
    over a new document as a missed read of the last run's. Neither entry was there to read: each
    model has its own cache, and a different document is a different prefix. This key is the
    prefix the cache is keyed on, so only a row that wrote the same one can be a predecessor.

    A document's bytes are hashed directly, never rendered into the key's JSON.

    A checklist call (§22.3) passes its tool and its own system prompt, so its key is never a prompt
    step's: the tool list renders ahead of the documents, so the two groups on one model write two
    entries, and neither can be read as a missed read of the other. A prompt step's key is exactly
    what it was before checklist steps (no ``tools`` member), so its rows still match each other.
    """
    import hashlib

    from apis.shared.observability.prompt_cache import fingerprint_canonical_json

    parts: List[Dict[str, str]] = []
    for block in prefix:
        document = block.get("document") if isinstance(block, dict) else None
        if isinstance(document, dict):
            raw = (document.get("source") or {}).get("bytes") or b""
            parts.append({"document": str(document.get("name") or ""), "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            text = str(block.get("text") or "") if isinstance(block, dict) else ""
            parts.append({"text": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    key: Dict[str, Any] = {"modelId": model_id, "system": system, "prefix": parts}
    if tools:
        key["tools"] = fingerprint_canonical_json(list(tools))
    return fingerprint_canonical_json(key)


def _fingerprint(
    messages: List[Dict[str, Any]],
    key: Optional[str] = None,
    *,
    tools: Sequence[Dict[str, Any]] = (),
    system: str = SYSTEM_PROMPT,
) -> Optional[Dict[str, Any]]:
    """This call's prefix hashes, marked stateless (``extendsMessagesHashes``) with ``key``.

    The stateless match is the one api-converse uses: a predecessor counts only if its
    ``messagesHash`` is in this call's ``extendsMessagesHashes`` (``_derive_cache_observability``).
    Here both are the call's ``prefix_key``, so a step's predecessor is a call that wrote its prefix.
    """
    try:
        from agents.main_agent.session.hooks.prefix_fingerprint import compute_prefix_fingerprint

        fingerprint = compute_prefix_fingerprint(list(tools), [{"text": system}], messages)
    except Exception:  # noqa: BLE001 - a row without fingerprints falls back to the previous call
        logger.debug("workflow prompt: fingerprint failed", exc_info=True)
        return None
    if key:
        fingerprint["messagesHash"] = key
        fingerprint["extendsMessagesHashes"] = [key]
    return fingerprint


# --- The run --------------------------------------------------------------------------------------


@dataclass
class StepPlan:
    """A claimed step, ready to call."""

    step: WorkflowRunStep
    reads_block: str
    run_request: str = ""
    reads_files: bool = True

    @property
    def step_id(self) -> str:
        return self.step.step_id

    @property
    def handoff_id(self) -> str:
        return self.step.handoff_id

    @property
    def kind(self) -> str:
        """``prompt`` or ``checklist``: the row kind the claim holds the step to."""
        return KIND_CHECKLIST_STEP if self.step.is_checklist else KIND_PROMPT_STEP


@dataclass
class TimedCall:
    """One model call as ``PromptStepRun._timed_call`` ended it."""

    result: CallResult
    timed_out: bool
    started: float
    ended: float


class PromptStepRun:
    """One ``workflow_prompt`` invocation's work: checks, claims, calls and writes, frames out.

    It runs the run's prompt steps (§21.3) and, while ``checklist_steps_enabled()``, its checklist
    steps (§22.3), whose calls ``workflow_checklist.run_checklist_group`` makes on this object's
    slots, cancels, claims and meter.
    """

    def __init__(
        self,
        request: WorkflowPromptRequest,
        user: User,
        session_id: str,
        *,
        runs: Optional[WorkflowRunStore] = None,
        steps: Optional[PromptStepStore] = None,
        call_model: CallModel = call_bedrock,
        build_model: Callable[[str], Any] = build_bedrock_model,
        render_reads: RenderReads = render_reads,
        read_document: Callable[..., Awaitable[WholeDocument]] = read_whole_document,
        model_plan: Callable[[User, str], Awaitable[ModelPlan]] = resolve_model_plan,
        quota: Callable[[User, str], Awaitable[bool]] = check_quota,
        meter: Callable[..., Awaitable[Optional[str]]] = meter_call,
        enabled: Callable[[], bool] = prompt_steps_enabled,
        step_timeout: float = STEP_TIMEOUT_SECONDS,
        poll_seconds: float = RUN_POLL_SECONDS,
        checklist_enabled: Callable[[], bool] = checklist_steps_enabled,
        call_checklist: Optional[CallModel] = None,
        build_checklist_model: Optional[Callable[[str], Any]] = None,
        load_field_sets: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        provenance_units: Optional[Callable[..., Awaitable[Any]]] = None,
        put_checklist_set: Optional[Callable[..., bool]] = None,
    ) -> None:
        self.request = request
        self.user = user
        self.session_id = session_id
        self.run_id = request.run_id
        self._runs = runs or get_workflow_run_store()
        self._steps = steps or get_prompt_step_store()
        self._call_model = call_model
        self._build_model = build_model
        self._render_reads = render_reads
        self._read_document = read_document
        self._model_plan = model_plan
        self._quota = quota
        self._meter = meter
        self._enabled = enabled
        self._step_timeout = step_timeout
        self._poll_seconds = poll_seconds
        self._checklist_enabled = checklist_enabled
        # The checklist branch's own dependencies, defaulted lazily from its module.
        self._checklist_deps = {
            "call_checklist": call_checklist,
            "build_checklist_model": build_checklist_model,
            "load_field_sets": load_field_sets,
            "provenance_units": provenance_units,
            "put_checklist_set": put_checklist_set,
        }
        self.frames: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._cancels: Dict[str, threading.Event] = {}
        self._stopped = False
        self._settled: Set[str] = set()
        self._claimed: List[StepPlan] = []
        self._slots = asyncio.Semaphore(1)
        #: Order the calls started in: a prompt step's id, or ``<step id>:<field set>`` for a
        #: checklist call (warm-then-fan-out is observable here).
        self.call_order: List[str] = []

    # The whole invocation, frames to ``self.frames``, ``None`` last.
    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("workflow prompt: the invocation failed", exc_info=True)
            # A claimed step must never be left ``running``: nothing else would ever settle it.
            for plan in self._claimed:
                if plan.step_id not in self._settled:
                    await self._stop(plan, "error")
        finally:
            self.frames.put_nowait(done_frame(self.run_id))
            self.frames.put_nowait(None)

    def _emit(self, frame: str) -> None:
        self.frames.put_nowait(frame)

    async def _run(self) -> None:
        if not self._enabled():
            self._emit(refused_frame(self.run_id, "flag_off"))
            return
        try:
            run = await asyncio.to_thread(self._runs.get, self.user.user_id, self.run_id)
        except Exception:
            logger.warning("workflow prompt: reading the run failed", exc_info=True)
            self._emit(refused_frame(self.run_id, "unavailable"))
            return
        if run is None or run.session_id != self.session_id or run.user_id != self.user.user_id:
            self._emit(refused_frame(self.run_id, "not_found"))
            return
        if run.effective_status() != "approved":
            self._emit(refused_frame(self.run_id, "not_approved"))
            return

        candidates = await self._candidates(run)
        if not candidates:
            return
        claimed = await self._claim(run, candidates)
        self._claimed = claimed
        if not claimed:
            return

        watcher = asyncio.ensure_future(self._watch_run())
        try:
            await self._run_claimed(run, claimed)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def _runs_kind(self, step: WorkflowRunStep) -> bool:
        """Whether this invocation runs ``step``'s kind: a prompt step, or a checklist step while those are on."""
        if step.is_prompt:
            return True
        return step.is_checklist and self._checklist_enabled()

    async def _candidates(self, run: WorkflowRun) -> List[StepPlan]:
        """The requested steps this invocation may claim, each with its reads rendered."""
        kept = {step.step_id: step for step in run.kept_steps()}
        plans: List[StepPlan] = []
        for step_id in self.request.step_ids:
            step = kept.get(step_id)
            if step is None:
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="not_in_run"))
                continue
            kind = step.kind or "agent"
            if not self._runs_kind(step):
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="not_prompt", kind=kind))
                continue
            # Checked before the claim: a step whose reads haven't settled is left ``open`` for
            # the queue to send again, never stopped.
            try:
                block = await self._render_reads(self.user.user_id, self.session_id, run, step)
            except StepReadsNotSettled:
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_pending", kind=kind))
                continue
            except DeliveryRefused as exc:
                logger.info("workflow prompt: a step's reads were refused (%s)", getattr(exc, "reason", ""))
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_refused", kind=kind))
                continue
            except Exception:
                # A failed read (a store error) is a retry later, like a step not ready yet.
                logger.warning("workflow prompt: rendering a step's reads failed", exc_info=True)
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_pending", kind=kind))
                continue
            plans.append(
                StepPlan(
                    step=step,
                    reads_block=block,
                    run_request=run_request_for(run, step),
                    reads_files=step.reads_input(READ_FILES),
                )
            )
        return plans

    async def _claim(self, run: WorkflowRun, plans: List[StepPlan]) -> List[StepPlan]:
        results = await asyncio.gather(
            *(
                asyncio.to_thread(self._steps.claim, self.user.user_id, p.handoff_id, run_id=run.run_id, kind=p.kind)
                for p in plans
            ),
            return_exceptions=True,
        )
        claimed: List[StepPlan] = []
        for plan, result in zip(plans, results):
            if result is True:
                claimed.append(plan)
                continue
            if isinstance(result, BaseException):
                logger.warning("workflow prompt: claiming a step failed", exc_info=result)
            self._emit(step_frame(self.run_id, plan.step_id, "skipped", reason="not_open", kind=plan.kind))
        return claimed

    async def _stop(self, plan: StepPlan, reason: PromptStopReason) -> None:
        if plan.step_id in self._settled:
            return
        self._settled.add(plan.step_id)
        try:
            await asyncio.to_thread(self._steps.stop, self.user.user_id, plan.handoff_id, reason)
        except Exception:
            logger.warning("workflow prompt: stopping a step failed", exc_info=True)
        logger.info("workflow prompt: step stopped (%s)", reason)
        self._emit(step_frame(self.run_id, plan.step_id, "stopped", stopped_reason=reason, kind=plan.kind))

    async def _finish(self, plan: StepPlan, text: str, output_tokens: int) -> None:
        self._settled.add(plan.step_id)
        try:
            written = await asyncio.to_thread(
                self._steps.finish, self.user.user_id, plan.handoff_id, output_text=text, output_tokens=output_tokens
            )
        except Exception:
            logger.warning("workflow prompt: finishing a step failed", exc_info=True)
            written = False
        if written:
            self._emit(step_frame(self.run_id, plan.step_id, "done", kind=plan.kind))
        else:
            self._emit(step_frame(self.run_id, plan.step_id, "stopped", stopped_reason="error", kind=plan.kind))

    async def _watch_run(self) -> None:
        """Re-read the run every ``poll_seconds``; on ``stopped`` cancel every call in flight."""
        while not self._stopped:
            await asyncio.sleep(self._poll_seconds)
            try:
                run = await asyncio.to_thread(self._runs.get, self.user.user_id, self.run_id)
            except Exception:
                logger.debug("workflow prompt: re-reading the run failed", exc_info=True)
                continue
            if run is not None and run.status == "stopped":
                self._stopped = True
                for cancel in self._cancels.values():
                    cancel.set()
                return

    async def _run_claimed(self, run: WorkflowRun, claimed: List[StepPlan]) -> None:
        if not await self._quota(self.user, self.session_id):
            for plan in claimed:
                await self._stop(plan, "quota")
            return

        file_ids = run.file_ids if any(plan.reads_files for plan in claimed) else []
        try:
            documents = list(
                await asyncio.gather(
                    *(
                        self._read_document(self.user.user_id, self.session_id, file_id, reader="workflow_prompt")
                        for file_id in file_ids
                    )
                )
            )
        except WholeDocumentError as exc:
            reason: PromptStopReason = "too_large" if exc.reason == "too_large" else "error"
            logger.info("workflow prompt: a run file can't be sent whole (%s)", exc.reason)
            for plan in claimed:
                await self._stop(plan, reason)
            return
        except Exception:
            logger.warning("workflow prompt: reading the run's files failed", exc_info=True)
            for plan in claimed:
                await self._stop(plan, "error")
            return
        prefix = document_blocks(documents)

        checklist = None
        if any(plan.step.is_checklist for plan in claimed):
            from apis.inference_api.chat.workflow_checklist import ChecklistBranch

            checklist = ChecklistBranch(self, run, **self._checklist_deps)
            await checklist.load([plan for plan in claimed if plan.step.is_checklist])

        # One group per (kind, model), side by side: each model has its own cache, and on one
        # model a checklist call's tool list differs from a prompt step's (none), so the two kinds
        # write separate entries and neither can warm the other. Running the groups in turn would
        # only add a group's wall time. One semaphore across the groups keeps every call in flight
        # within the run's concurrency.
        limit = max(1, int(run.limits.concurrency or 1))
        self._slots = asyncio.Semaphore(limit)
        groups: Dict[Tuple[str, str], List[StepPlan]] = {}
        for plan in claimed:
            groups.setdefault((plan.kind, plan.step.model_id or ""), []).append(plan)
        model_plans: Dict[str, "asyncio.Future[ModelPlan]"] = {}

        def _plan_for(model_id: str) -> "asyncio.Future[ModelPlan]":
            # Resolved once per model, however many groups run on it.
            if model_id not in model_plans:
                model_plans[model_id] = asyncio.ensure_future(self._model_plan(self.user, model_id))
            return model_plans[model_id]

        async def _group(kind: str, model_id: str, plans: List[StepPlan]) -> None:
            model_plan = await _plan_for(model_id)
            if model_plan.refusal is not None:
                for plan in plans:
                    await self._stop(plan, model_plan.refusal)
                return
            if not model_plan.reads_documents:
                for plan in [p for p in plans if p.reads_files]:
                    await self._stop(plan, "model_refused")
                plans = [p for p in plans if not p.reads_files]
                if not plans:
                    return
            if any(plan.reads_files for plan in plans) and too_large(documents, model_plan):
                # Only the steps that send the documents are over the budget; a step that reads
                # only earlier steps sends none of them, so it still runs.
                for plan in [p for p in plans if p.reads_files]:
                    await self._stop(plan, "too_large")
                plans = [p for p in plans if not p.reads_files]
                if not plans:
                    return
            if kind == KIND_CHECKLIST_STEP and checklist is not None:
                await checklist.run_group(model_plan, documents, prefix, plans, limit=limit)
                return
            await self._run_group(run, model_plan, documents, prefix, plans, limit=limit)

        outcomes = await asyncio.gather(
            *(_group(kind, model_id, plans) for (kind, model_id), plans in groups.items()), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.warning("workflow prompt: a model group failed", exc_info=outcome)

        for plan in claimed:
            if plan.step_id not in self._settled:
                await self._stop(plan, "user_stopped" if self._stopped else "error")

    async def _run_group(
        self,
        run: WorkflowRun,
        model_plan: ModelPlan,
        documents: Sequence[WholeDocument],
        prefix: List[Dict[str, Any]],
        plans: List[StepPlan],
        *,
        limit: int,
    ) -> None:
        """Warm, then fan out: the first step alone writes the prefix; the rest read it.

        "Alone" is within the group: another model's group may be calling at the same time, since
        it has a cache of its own. Every call takes a slot from the run's shared semaphore.
        """
        model = self._build_model(model_plan.model_id)
        doc_tokens = document_tokens(documents, model_plan.model_id)
        # The group's two prefixes: with the documents, for a step that reads files, and without.
        warm: Dict[str, Any] = {
            "prefix": None,
            "key": prefix_key(model_plan.model_id, prefix),
            "bare_key": prefix_key(model_plan.model_id, []),
        }

        # The warm call is a step that sends the documents, so the rest read them; a step that
        # reads no files has no shared prefix and only joins the fan-out.
        first = next((plan for plan in plans if plan.reads_files and prefix), plans[0])
        rest = [plan for plan in plans if plan is not first]
        await self._run_step(model, model_plan, prefix, doc_tokens, first, warm)
        if not rest or self._stopped:
            return
        results = await run_bounded(
            [
                (lambda plan=plan: self._run_step(model, model_plan, prefix, doc_tokens, plan, warm))
                for plan in rest
            ],
            limit=limit,
        )
        for plan, result in zip(rest, results):
            if isinstance(result, BaseException):
                logger.warning("workflow prompt: a step failed", exc_info=result)
                await self._stop(plan, "error")

    async def _run_step(
        self,
        model: Any,
        model_plan: ModelPlan,
        prefix: List[Dict[str, Any]],
        doc_tokens: int,
        plan: StepPlan,
        warm: Dict[str, Any],
    ) -> None:
        async with self._slots:
            await self._call_step(model, model_plan, prefix, doc_tokens, plan, warm)

    async def _timed_call(self, model: Any, messages: List[Dict[str, Any]], key: str, call: CallModel) -> TimedCall:
        """One model call under the step timeout, cancellable by a stopped run (``self._cancels[key]``)."""
        cancel = threading.Event()
        self._cancels[key] = cancel
        timed_out = {"value": False}

        async def _deadline() -> None:
            await asyncio.sleep(self._step_timeout)
            timed_out["value"] = True
            cancel.set()

        timer = asyncio.ensure_future(_deadline())
        started = time.time()
        self.call_order.append(key)
        try:
            try:
                result = await asyncio.wait_for(call(model, messages, cancel), timeout=self._step_timeout + 15)
            except asyncio.TimeoutError:
                timed_out["value"] = True
                result = CallResult(stop_reason="cancelled")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("workflow prompt: a step's call failed", exc_info=True)
            result = CallResult(stop_reason="error")
        finally:
            timer.cancel()
            self._cancels.pop(key, None)
        return TimedCall(result=result, timed_out=timed_out["value"], started=started, ended=time.time())

    async def _meter_result(
        self,
        model_plan: ModelPlan,
        call: TimedCall,
        messages: List[Dict[str, Any]],
        *,
        prompt_tokens: int,
        warm: Dict[str, Any],
        key: Optional[str],
        attributes: Dict[str, Any],
        call_kind: str = CALL_KIND,
        tools: Sequence[Dict[str, Any]] = (),
        system: str = SYSTEM_PROMPT,
    ) -> None:
        """Meter one call: its reported usage, or an estimate for one cancelled after the provider answered.

        ``warm["prefix"]`` keeps the first reported call's cached prefix, which a later cancelled
        call's estimate splits its input by.
        """
        result = call.result
        usage, estimate = result.usage, None
        if not _reported(usage) and result.stop_reason == "cancelled" and result.answered:
            usage, estimate = estimate_cancelled_usage(
                model_id=model_plan.model_id,
                prompt_tokens=prompt_tokens,
                cached_prefix=warm["prefix"],
                cache_point=model_plan.cache_point,
                partial_text=result.text,
            )
        if not _reported(usage):
            return
        if warm["prefix"] is None and estimate is None:
            warm["prefix"] = int(usage.get("cacheWriteInputTokens") or 0) + int(usage.get("cacheReadInputTokens") or 0)
        await self._meter(
            user_id=self.user.user_id,
            session_id=self.session_id,
            model_id=model_plan.model_id,
            usage=usage,
            metrics=result.metrics,
            started=call.started,
            ended=call.ended,
            fingerprint=_fingerprint(messages, key, tools=tools, system=system),
            attributes={
                **attributes,
                **({"truncated": True} if result.stop_reason == "max_tokens" else {}),
            },
            usage_estimate=estimate,
            call_kind=call_kind,
        )

    async def _call_step(
        self,
        model: Any,
        model_plan: ModelPlan,
        prefix: List[Dict[str, Any]],
        doc_tokens: int,
        plan: StepPlan,
        warm: Dict[str, Any],
    ) -> None:
        if self._stopped:
            await self._stop(plan, "user_stopped")
            return
        content = step_content(
            prefix if plan.reads_files else [],
            plan.reads_block,
            plan.step.request,
            cache_point=model_plan.cache_point,
            run_request=plan.run_request,
        )
        messages = [{"role": "user", "content": content}]
        call = await self._timed_call(model, messages, plan.step_id, self._call_model)
        result = call.result
        prompt_tokens = (doc_tokens if plan.reads_files else 0) + math.ceil(
            (len(SYSTEM_PROMPT) + len(plan.run_request) + len(plan.reads_block) + len(plan.step.request)) / 4
        )
        await self._meter_result(
            model_plan,
            call,
            messages,
            prompt_tokens=prompt_tokens,
            warm=warm,
            key=warm.get("key" if plan.reads_files else "bare_key"),
            attributes={"runId": self.run_id, "stepId": plan.step_id},
        )

        if result.stop_reason == "cancelled":
            await self._stop(plan, "user_stopped" if self._stopped and not call.timed_out else "timeout")
            return
        if result.stop_reason in ("guardrail_intervened", "content_filtered", "error"):
            await self._stop(plan, "error")
            return
        text = (result.text or "").strip()
        if not text:
            await self._stop(plan, "error")
            return
        if result.stop_reason == "max_tokens":
            # Kept, not stopped: the answer so far is worth having, as long as it says it is cut.
            text = mark_cut_off(text)
        await self._finish(plan, text, int((result.usage or {}).get("outputTokens") or 0))


# --- Entry point ----------------------------------------------------------------------------------

#: Invocations whose client has gone: held here so the task is not collected before it writes.
_BACKGROUND: Set["asyncio.Task[None]"] = set()


async def stream_workflow_prompt(
    request: WorkflowPromptRequest, user: User, session_id: str, **deps: Any
) -> AsyncGenerator[str, None]:
    """The invocation's SSE stream. The work runs in a task of its own, so a disconnect does not cancel it."""
    from apis.inference_api.runtime_health import tracker

    work = PromptStepRun(request, user, session_id, **deps)
    # The request's own activity ends when its stream does, which a closed tab cuts short; the
    # work's runs on until its rows are written, and the container reports busy until then.
    tracker.enter()
    task = asyncio.ensure_future(work.run())
    _BACKGROUND.add(task)

    def _done(finished: "asyncio.Task[None]") -> None:
        _BACKGROUND.discard(finished)
        tracker.exit()

    task.add_done_callback(_done)
    while True:
        frame = await work.frames.get()
        if frame is None:
            return
        yield frame


__all__ = [
    "CALL_KIND",
    "CallResult",
    "INSTRUCTION_HEADING",
    "MAX_OUTPUT_TOKENS",
    "ModelPlan",
    "PromptStepRun",
    "RUN_POLL_SECONDS",
    "STEP_TIMEOUT_SECONDS",
    "SYSTEM_PROMPT",
    "document_blocks",
    "document_name",
    "done_frame",
    "refused_frame",
    "step_content",
    "step_frame",
    "stream_workflow_prompt",
]
