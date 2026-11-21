"""A workflow run's prompt steps: one model call each over the run's files, never a turn.

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
from apis.shared.feature_flags import prompt_steps_enabled
from apis.shared.handoff.models import PromptStopReason
from apis.shared.handoff.prompt_steps import PromptStepStore, WorkflowPromptRequest, get_prompt_step_store
from apis.shared.handoff.run_reads import StepReadsNotSettled, render_step_reads
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep, WorkflowRunStore, get_workflow_run_store
from apis.shared.handoff.send_back import DeliveryRefused
from apis.shared.models.cache_points import bedrock_cache_points_supported

logger = logging.getLogger(__name__)

#: The ``callKind`` on each step's cost row.
CALL_KIND = "workflow_prompt"

#: Output tokens a step may write (§21.3). The stored answer is capped lower, at the delivery cap.
MAX_OUTPUT_TOKENS = 4_000

#: One step's call, from request to its last token.
STEP_TIMEOUT_SECONDS = 120.0

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


# --- Frames ---------------------------------------------------------------------------------------


def _frame(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def step_frame(run_id: str, step_id: str, status: str, *, stopped_reason: Optional[str] = None, reason: Optional[str] = None) -> str:
    """One step settled: ``done``, ``stopped`` (with ``stoppedReason``), or ``skipped`` (with ``reason``).

    ``skipped`` is a step this invocation did not run and did not write: not a prompt step of the
    run, not ``open`` (another invocation claimed it, or it settled), one whose reads have not
    settled, or one whose reads a delivery rule refused. A client treats it as not started.
    """
    data: Dict[str, Any] = {"type": "workflow_prompt_step", "runId": run_id, "stepId": step_id, "status": status}
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
    prefix: Sequence[Dict[str, Any]], reads_block: str, instruction: str, *, cache_point: bool
) -> List[Dict[str, Any]]:
    """One step's user message: the shared prefix, a cachePoint after it, its reads, its instruction.

    The cachePoint goes only where there is a prefix to cache and the model takes one. Everything
    after it is the step's own, so each step writes only its own tail.
    """
    content: List[Dict[str, Any]] = list(prefix)
    if prefix and cache_point:
        content.append({"cachePoint": {"type": "default"}})
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


CallModel = Callable[[Any, List[Dict[str, Any]], threading.Event], Awaitable[CallResult]]
RenderReads = Callable[[str, str, WorkflowRun, WorkflowRunStep], Awaitable[str]]


def _reported(usage: Optional[Dict[str, Any]]) -> bool:
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    return bool(usage) and any((usage or {}).get(key) for key in keys)


async def call_bedrock(model: Any, messages: List[Dict[str, Any]], cancel: threading.Event) -> CallResult:
    """Stream one Converse call through Strands, with no tools and the constant system prompt."""
    from strands.event_loop.streaming import stream_messages

    result = CallResult(stop_reason="error")
    parts: List[str] = []
    stop: Optional[tuple] = None
    async for event in stream_messages(
        model,
        None,
        messages,
        [],
        tool_choice=None,
        system_prompt_content=[{"text": SYSTEM_PROMPT}],
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


async def resolve_model_plan(user: User, model_id: str) -> ModelPlan:
    """Retirement first, then access as the caller, then a catalog row, then this path's limits.

    A model with no row is refused (it would price to nothing), as is one this call path cannot send
    to: a provider other than Bedrock Converse, or a TEXT-only model, which takes no PDF.
    """
    from apis.shared.files.attachment_plan import ModelFacts, document_budget, pdf_page_limit, token_profile
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
    modalities = {str(m).strip().upper() for m in (record.input_modalities or [])}
    if (record.provider or "").lower() != "bedrock" or modalities == {"TEXT"}:
        return ModelPlan(model_id=effective.model_id, refusal="model_refused")

    context_window = int(record.max_input_tokens) if record.max_input_tokens else None
    profile = token_profile()
    ceiling = _chat_ceiling(context_window)
    facts = ModelFacts(model_id=effective.model_id, context_window=context_window, chat_ceiling=ceiling or context_window)
    return ModelPlan(
        model_id=effective.model_id,
        context_window=context_window,
        document_budget=document_budget(facts, profile),
        pdf_page_limit=pdf_page_limit(facts),
        cache_point=bedrock_cache_points_supported(effective.model_id),
    )


def _chat_ceiling(context_window: Optional[int]) -> Optional[int]:
    """The compaction ceiling the model runs under in chat, which the document budget is a share of."""
    try:
        from agents.main_agent.session.compaction_models import CompactionConfig
        from agents.main_agent.session.compaction_policy import CompactionPolicy

        config = CompactionConfig.from_env()
        if not config.enabled:
            return None
        return CompactionPolicy.resolve(config, context_window).ceiling
    except Exception:  # noqa: BLE001 - the context window stands in
        logger.warning("workflow prompt: compaction ceiling unavailable", exc_info=True)
        return None


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
) -> Optional[str]:
    """Write one step call's ``C#`` row on the source session. Returns its ``cacheStatus``. Never raises.

    Priced from the catalog like ``document_extract``'s call, with the coordinator's long-context
    rule (``CostCalculator`` applies it). ``callKind`` and no ``messageId`` keep it off the turn
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
                model_id, "calculation_failed" if snapshot else "no_pricing", surface=CALL_KIND, session_id=session_id
            )

        latency_ms = metrics.get("latencyMs")
        ttfb_ms = metrics.get("timeToFirstByteMs")
        extra: Dict[str, Any] = {"callKind": CALL_KIND, **attributes}
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


def _fingerprint(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    try:
        from agents.main_agent.session.hooks.prefix_fingerprint import compute_prefix_fingerprint

        return compute_prefix_fingerprint([], [{"text": SYSTEM_PROMPT}], messages)
    except Exception:  # noqa: BLE001 - a row without fingerprints falls back to the previous call
        logger.debug("workflow prompt: fingerprint failed", exc_info=True)
        return None


# --- The run --------------------------------------------------------------------------------------


@dataclass
class StepPlan:
    """A claimed step, ready to call."""

    step: WorkflowRunStep
    reads_block: str

    @property
    def step_id(self) -> str:
        return self.step.step_id

    @property
    def handoff_id(self) -> str:
        return self.step.handoff_id


class PromptStepRun:
    """One ``workflow_prompt`` invocation's work: checks, claims, calls and writes, frames out."""

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
        self.frames: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._cancels: Dict[str, threading.Event] = {}
        self._stopped = False
        self._settled: Set[str] = set()
        self._claimed: List[StepPlan] = []
        self._slots = asyncio.Semaphore(1)
        #: Order the calls started in, by step id (warm-then-fan-out is observable here).
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

    async def _candidates(self, run: WorkflowRun) -> List[StepPlan]:
        """The requested steps this invocation may claim, each with its reads rendered."""
        kept = {step.step_id: step for step in run.kept_steps()}
        plans: List[StepPlan] = []
        for step_id in self.request.step_ids:
            step = kept.get(step_id)
            if step is None:
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="not_in_run"))
                continue
            if not step.is_prompt:
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="not_prompt"))
                continue
            # Checked before the claim: a step whose reads haven't settled is left ``open`` for
            # the queue to send again, never stopped.
            try:
                block = await self._render_reads(self.user.user_id, self.session_id, run, step)
            except StepReadsNotSettled:
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_pending"))
                continue
            except DeliveryRefused as exc:
                logger.info("workflow prompt: a step's reads were refused (%s)", getattr(exc, "reason", ""))
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_refused"))
                continue
            except Exception:
                # A failed read (a store error) is a retry later, like a step not ready yet.
                logger.warning("workflow prompt: rendering a step's reads failed", exc_info=True)
                self._emit(step_frame(self.run_id, step_id, "skipped", reason="reads_pending"))
                continue
            plans.append(StepPlan(step=step, reads_block=block))
        return plans

    async def _claim(self, run: WorkflowRun, plans: List[StepPlan]) -> List[StepPlan]:
        results = await asyncio.gather(
            *(asyncio.to_thread(self._steps.claim, self.user.user_id, p.handoff_id, run_id=run.run_id) for p in plans),
            return_exceptions=True,
        )
        claimed: List[StepPlan] = []
        for plan, result in zip(plans, results):
            if result is True:
                claimed.append(plan)
                continue
            if isinstance(result, BaseException):
                logger.warning("workflow prompt: claiming a step failed", exc_info=result)
            self._emit(step_frame(self.run_id, plan.step_id, "skipped", reason="not_open"))
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
        self._emit(step_frame(self.run_id, plan.step_id, "stopped", stopped_reason=reason))

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
            self._emit(step_frame(self.run_id, plan.step_id, "done"))
        else:
            self._emit(step_frame(self.run_id, plan.step_id, "stopped", stopped_reason="error"))

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

        try:
            documents = list(
                await asyncio.gather(
                    *(
                        self._read_document(self.user.user_id, self.session_id, file_id, reader="workflow_prompt")
                        for file_id in run.file_ids
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

        # One group per model, side by side: each model has its own cache, so nothing is shared
        # between groups and running them in turn would only add a group's wall time. One
        # semaphore across the groups keeps every call in flight within the run's concurrency.
        limit = max(1, int(run.limits.concurrency or 1))
        self._slots = asyncio.Semaphore(limit)
        groups: Dict[str, List[StepPlan]] = {}
        for plan in claimed:
            groups.setdefault(plan.step.model_id or "", []).append(plan)

        async def _group(model_id: str, plans: List[StepPlan]) -> None:
            model_plan = await self._model_plan(self.user, model_id)
            if model_plan.refusal is not None:
                for plan in plans:
                    await self._stop(plan, model_plan.refusal)
                return
            if too_large(documents, model_plan):
                for plan in plans:
                    await self._stop(plan, "too_large")
                return
            await self._run_group(run, model_plan, documents, prefix, plans, limit=limit)

        outcomes = await asyncio.gather(
            *(_group(model_id, plans) for model_id, plans in groups.items()), return_exceptions=True
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
        warm: Dict[str, Optional[int]] = {"prefix": None}

        first, rest = plans[0], plans[1:]
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
        warm: Dict[str, Optional[int]],
    ) -> None:
        async with self._slots:
            await self._call_step(model, model_plan, prefix, doc_tokens, plan, warm)

    async def _call_step(
        self,
        model: Any,
        model_plan: ModelPlan,
        prefix: List[Dict[str, Any]],
        doc_tokens: int,
        plan: StepPlan,
        warm: Dict[str, Optional[int]],
    ) -> None:
        if self._stopped:
            await self._stop(plan, "user_stopped")
            return
        content = step_content(prefix, plan.reads_block, plan.step.request, cache_point=model_plan.cache_point)
        messages = [{"role": "user", "content": content}]
        cancel = threading.Event()
        self._cancels[plan.step_id] = cancel
        timed_out = {"value": False}

        async def _deadline() -> None:
            await asyncio.sleep(self._step_timeout)
            timed_out["value"] = True
            cancel.set()

        timer = asyncio.ensure_future(_deadline())
        started = time.time()
        self.call_order.append(plan.step_id)
        try:
            try:
                result = await asyncio.wait_for(
                    self._call_model(model, messages, cancel), timeout=self._step_timeout + 15
                )
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
            self._cancels.pop(plan.step_id, None)
        ended = time.time()

        usage, estimate = result.usage, None
        if not _reported(usage) and result.stop_reason == "cancelled" and result.answered:
            prompt_tokens = doc_tokens + math.ceil(
                (len(SYSTEM_PROMPT) + len(plan.reads_block) + len(plan.step.request)) / 4
            )
            usage, estimate = estimate_cancelled_usage(
                model_id=model_plan.model_id,
                prompt_tokens=prompt_tokens,
                cached_prefix=warm["prefix"],
                cache_point=model_plan.cache_point,
                partial_text=result.text,
            )
        if _reported(usage):
            if warm["prefix"] is None and estimate is None:
                warm["prefix"] = int(usage.get("cacheWriteInputTokens") or 0) + int(
                    usage.get("cacheReadInputTokens") or 0
                )
            await self._meter(
                user_id=self.user.user_id,
                session_id=self.session_id,
                model_id=model_plan.model_id,
                usage=usage,
                metrics=result.metrics,
                started=started,
                ended=ended,
                fingerprint=_fingerprint(messages),
                attributes={"runId": self.run_id, "stepId": plan.step_id},
                usage_estimate=estimate,
            )

        if result.stop_reason == "cancelled":
            await self._stop(plan, "user_stopped" if self._stopped and not timed_out["value"] else "timeout")
            return
        if result.stop_reason in ("guardrail_intervened", "content_filtered", "error"):
            await self._stop(plan, "error")
            return
        text = (result.text or "").strip()
        if not text:
            await self._stop(plan, "error")
            return
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
