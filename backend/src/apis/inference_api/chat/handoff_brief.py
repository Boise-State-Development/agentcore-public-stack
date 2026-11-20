"""The handoff brief's side call: the source's last request plus one message, never a turn.

``docs/specs/agent-handoffs-and-consults.md`` §4.5.1. The route builds the source session's agent
exactly as a turn would (same phases, same ``get_agent`` call), takes the session's lease, and hands
this module the build. Here the agent's own model is called through Strands' ``stream_messages``
with the agent's own system prompt content, tool specs and model state, over a **copy** of its
history with the brief's instruction appended. So the request is the cached prefix byte for byte up
to the new message, and the history reads from the source's prompt cache.

Nothing reaches the source: the copy is never assigned back, no hook runs, the session manager is
never written, and the live history, compaction state and rewind epoch are left for the next turn.
The one write on the source is the ``C#`` cost row, marked ``callKind: "handoff_brief"`` with no
``messageId``.

A workflow run's brief (``docs/specs/workflow-runs.md`` §6.3) is this same call with one difference:
the appended message is the run variant of the instruction, naming every kept step's Agent and
request. It is recorded on the run row and on every kept step's ``HANDOFF#`` row, so each step's
acceptance compares its card against it exactly as a single handoff's does.
"""

import asyncio
import copy
import logging
import os
import threading
import time
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Sequence

from strands.event_loop.streaming import stream_messages
from strands.types.content import split_system_prompt

from agents.main_agent.session.hooks.prefix_fingerprint import compute_prefix_fingerprint
from apis.shared.handoff.brief import (
    BRIEF_INSTRUCTION_VERSION,
    BRIEF_RUN_INSTRUCTION_VERSION,
    BriefFallbackReason,
    RunBriefStep,
    brief_done_frame,
    brief_fallback_for_history,
    brief_fallback_frame,
    brief_text_frame,
    build_brief_instruction,
    build_run_brief_instruction,
)
from apis.shared.handoff.context import context_hash
from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.runs import WorkflowRun, get_workflow_run_store
from apis.shared.handoff.store import get_handoff_store

logger = logging.getLogger(__name__)

#: The ``callKind`` on the brief's cost row.
CALL_KIND = "handoff_brief"

#: How often the brief checks its lease for a cancel (a send in the source preempting it, §4.5.1).
#: Shorter than a turn's 10s heartbeat because the user's own send is waiting on it.
_LEASE_POLL_SECONDS = 1.0


def _timeout_seconds() -> float:
    """``HANDOFF_BRIEF_TIMEOUT_SECONDS``, default 30: the agent build and the call together."""
    try:
        value = float(os.environ.get("HANDOFF_BRIEF_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


def _message_text(message: Dict[str, Any]) -> str:
    return "".join(block.get("text", "") for block in message.get("content") or [] if isinstance(block, dict))


def _has_tool_use(message: Dict[str, Any]) -> bool:
    return any(isinstance(block, dict) and "toolUse" in block for block in message.get("content") or [])


def _reported_tokens(usage: Optional[Dict[str, Any]]) -> bool:
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    return bool(usage) and any((usage or {}).get(key) for key in keys)


async def _watch(
    cancel: threading.Event,
    lease: Any,
    deadline: float,
    outcome: Dict[str, Optional[str]],
) -> None:
    """Set ``cancel`` when the lease reports a cancel request or the deadline passes."""
    from apis.shared.sessions.session_lease import renew_session_lease

    while not cancel.is_set():
        await asyncio.sleep(_LEASE_POLL_SECONDS)
        if time.monotonic() >= deadline:
            outcome["reason"] = "timeout"
            cancel.set()
            return
        if lease is not None and await renew_session_lease(lease):
            outcome["reason"] = "preempted"
            cancel.set()
            return


async def stream_handoff_brief(
    *,
    handoff: Handoff,
    user_id: str,
    session_id: str,
    session_row: Optional[Dict[str, Any]],
    build_agent: Callable[[], Awaitable[Any]],
    release_lease: Callable[[], Awaitable[None]],
    lease: Any,
    agent_description: "Optional[asyncio.Task[Optional[str]]]" = None,
    turn_agent_id: Optional[str] = None,
    turn_project_id: Optional[str] = None,
    run: Optional[WorkflowRun] = None,
    run_descriptions: "Optional[asyncio.Task[List[Optional[str]]]]" = None,
) -> AsyncGenerator[str, None]:
    """Stream the brief for ``handoff`` as SSE: text frames, or one fallback frame, then ``done``.

    With ``run`` (already checked by the route: the caller's, this session's, ``open``, and
    ``handoff`` one of its kept steps) the brief is the run's: ``run_descriptions`` resolves to one
    description per kept step, in order, and the brief is recorded on the run and every kept step.

    The lease is released before the closing ``done`` frame, so a send in the source that
    preempted the brief is never refused by a lease this stream still held (§4.5.1).
    """
    run_id = run.run_id if run is not None else None
    run_step_count = len(run.kept_steps()) if run is not None else None
    started = time.monotonic()
    deadline = started + _timeout_seconds()
    cancel = threading.Event()
    outcome: Dict[str, Optional[str]] = {"reason": None}
    watcher: Optional["asyncio.Task[None]"] = None
    reason: Optional[BriefFallbackReason] = None
    brief_hash: Optional[str] = None
    cache_status: Optional[str] = None
    released = False

    async def _release() -> None:
        nonlocal released
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if not released:
            released = True
            await release_lease()

    try:
        watcher = asyncio.ensure_future(_watch(cancel, lease, deadline, outcome))
        try:
            main_agent = await asyncio.wait_for(build_agent(), timeout=max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            reason = "timeout"
            main_agent = None

        if main_agent is not None and not cancel.is_set():
            strands_agent = main_agent.agent
            session_manager = getattr(main_agent, "session_manager", None)
            live = list(getattr(strands_agent, "messages", None) or [])
            if session_manager is not None and hasattr(session_manager, "rewound_copy"):
                messages = session_manager.rewound_copy(live, session_row)
            else:
                messages = copy.deepcopy(live)
            reason = brief_fallback_for_history(messages)

            if reason is None:
                if run is not None:
                    instruction = build_run_brief_instruction(
                        run.request, await _run_steps(run, run_descriptions)
                    )
                    attributes: Dict[str, Any] = {
                        "briefInstruction": "run",
                        "briefInstructionVersion": BRIEF_RUN_INSTRUCTION_VERSION,
                        "runStepCount": run_step_count,
                    }
                else:
                    description = None
                    if agent_description is not None:
                        try:
                            description = await agent_description
                        except Exception:  # noqa: BLE001 - the description is optional
                            description = None
                    instruction = build_brief_instruction(handoff.request, handoff.agent_name, description)
                    attributes = {"briefInstruction": "handoff", "briefInstructionVersion": BRIEF_INSTRUCTION_VERSION}
                messages.append({"role": "user", "content": [{"text": instruction}]})
                async for frame in _call_model(
                    main_agent,
                    strands_agent,
                    messages,
                    handoff_id=handoff.handoff_id,
                    cancel=cancel,
                    outcome=outcome,
                    user_id=user_id,
                    session_id=session_id,
                    turn_agent_id=turn_agent_id,
                    turn_project_id=turn_project_id,
                    run=run,
                    call_attributes=attributes,
                ):
                    if isinstance(frame, str):
                        yield frame
                    else:
                        reason, brief_hash, cache_status = frame
        elif reason is None:
            reason = outcome["reason"] or "timeout"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("handoff brief failed", exc_info=True)
        reason = "error"
    finally:
        await _release()

    if reason is not None:
        emit_handoff_metric(
            "HandoffBriefFallback", handoff.kind, session_id=session_id, detail=reason, run_step_count=run_step_count
        )
        yield brief_fallback_frame(handoff.handoff_id, reason, run_id=run_id)
        yield brief_done_frame(handoff.handoff_id, brief_hash=None, cache_status=cache_status, run_id=run_id)
        return

    emit_handoff_metric(
        "HandoffBriefWritten", handoff.kind, session_id=session_id, detail=cache_status, run_step_count=run_step_count
    )
    yield brief_done_frame(handoff.handoff_id, brief_hash=brief_hash, cache_status=cache_status, run_id=run_id)


async def _run_steps(
    run: WorkflowRun, descriptions: "Optional[asyncio.Task[List[Optional[str]]]]"
) -> List[RunBriefStep]:
    """The run's kept steps as the run instruction names them, each with its Agent's description."""
    kept = run.kept_steps()
    found: Sequence[Optional[str]] = []
    if descriptions is not None:
        try:
            found = await descriptions
        except Exception:  # noqa: BLE001 - descriptions are optional
            found = []
    return [
        RunBriefStep(
            agent_name=step.agent_name,
            request=step.request,
            agent_description=found[index] if index < len(found) else None,
        )
        for index, step in enumerate(kept)
    ]


async def _record_brief(
    user_id: str,
    handoff_id: str,
    run: Optional[WorkflowRun],
    *,
    brief_hash: str,
    brief_tokens: int,
    cache_status: Optional[str],
) -> None:
    """Record the brief on its handoff row, or for a run on the run row and every kept step's row.

    Concurrent, each write conditioned on its row still being ``open``. A failed write leaves that
    step's acceptance comparing against its excerpt hash, as a single handoff's does.
    """
    facts = {"brief_hash": brief_hash, "brief_tokens": brief_tokens, "brief_cache_status": cache_status}
    handoff_ids = [step.handoff_id for step in run.kept_steps()] if run is not None else [handoff_id]
    writes = [asyncio.to_thread(get_handoff_store().record_brief, user_id, step_id, **facts) for step_id in handoff_ids]
    if run is not None:
        writes.append(asyncio.to_thread(get_workflow_run_store().record_brief, user_id, run.run_id, **facts))
    for result in await asyncio.gather(*writes, return_exceptions=True):
        if isinstance(result, BaseException):
            logger.warning("handoff brief: recording the brief failed", exc_info=result)


async def _estimate_cancelled_usage(
    main_agent: Any, strands_agent: Any, messages: list, partial_text: str
) -> Optional[Dict[str, Any]]:
    """The cancelled brief's estimated usage, or ``None`` when it can't be sized honestly."""
    try:
        from agents.main_agent.streaming.interrupted_usage import build_side_call_usage

        estimate = await build_side_call_usage(
            strands_agent=strands_agent,
            wrapper=main_agent,
            history=messages[:-1],
            appended_text=_message_text(messages[-1]),
            partial_text=partial_text,
        )
    except Exception:  # noqa: BLE001 - metering must never break the brief's fallback
        logger.warning("handoff brief: estimating the cancelled call's usage failed", exc_info=True)
        return None
    if estimate is None:
        return None
    return {**estimate, "metrics": {}}


async def _call_model(
    main_agent: Any,
    strands_agent: Any,
    messages: list,
    *,
    handoff_id: str,
    cancel: threading.Event,
    outcome: Dict[str, Optional[str]],
    user_id: str,
    session_id: str,
    turn_agent_id: Optional[str],
    turn_project_id: Optional[str],
    run: Optional[WorkflowRun] = None,
    call_attributes: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Any, None]:
    """Stream the model's brief as text frames; finish with ``(reason, brief_hash, cache_status)``.

    ``reason`` is None when a brief was written. A fallback can follow text frames (a cancel
    mid-brief), so the SPA discards streamed text whenever a fallback frame arrives.

    The request is what Strands' event loop sends for this agent: its model (and the wrappers on
    it, so the TEXT-only projection applies the same way), the system prompt in content-block
    form (which carries the cachePoints), the registry's tool specs in registration order,
    ``tool_choice=None`` and a snapshot of the model state.
    """
    system_prompt = (
        strands_agent._system_prompt_content
        if getattr(strands_agent, "_system_prompt_content", None) is not None
        else strands_agent.system_prompt
    )
    system_prompt_str, system_prompt_content = split_system_prompt(copy.deepcopy(system_prompt))
    tool_specs = strands_agent.tool_registry.get_all_tool_specs()
    fingerprint = compute_prefix_fingerprint(tool_specs, getattr(strands_agent, "system_prompt", None), messages)

    stream_start = time.time()
    first_token: Optional[float] = None
    text_parts: list = []
    stop: Optional[tuple] = None
    # The provider accepted the request: from here a cancel still leaves a prompt it billed.
    answered = False
    async for event in stream_messages(
        strands_agent.model,
        system_prompt_str,
        messages,
        copy.deepcopy(tool_specs),
        tool_choice=None,
        system_prompt_content=system_prompt_content,
        invocation_state={},
        model_state=copy.deepcopy(getattr(strands_agent, "_model_state", None) or {}),
        cancel_signal=cancel,
        agent_metadata=getattr(strands_agent, "_metadata", None),
    ):
        if "stop" in event:
            stop = event["stop"]
            continue
        chunk = event.get("event") if isinstance(event, dict) else None
        if isinstance(chunk, dict) and "messageStart" in chunk:
            answered = True
        text = event.get("data") if isinstance(event, dict) else None
        if isinstance(text, str) and text:
            if first_token is None:
                first_token = time.time()
            answered = True
            text_parts.append(text)
            yield brief_text_frame(text)

    if stop is None:
        yield ("error", None, None)
        return
    stop_reason, message, usage, metrics = stop

    cache_status: Optional[str] = None
    accumulated: Optional[Dict[str, Any]] = None
    if _reported_tokens(usage):
        accumulated = {"usage": usage, "metrics": metrics or {}}
    elif stop_reason == "cancelled" and answered:
        # Strands reports a zero-filled usage on a cancel: the provider's terminal usage never
        # arrived. It still billed the prompt and what it streamed, so the row is estimated, as a
        # stopped turn's is, and labelled so (`usageEstimated`, `cacheStatus: estimated`).
        accumulated = await _estimate_cancelled_usage(main_agent, strands_agent, messages, "".join(text_parts))
    if accumulated is not None:
        observability = await main_agent.stream_coordinator._store_message_metadata(
            session_id=session_id,
            user_id=user_id,
            message_id=None,
            accumulated_metadata=accumulated,
            stream_start_time=stream_start,
            stream_end_time=time.time(),
            first_token_time=first_token,
            agent=main_agent,
            turn_agent_id=turn_agent_id,
            turn_project_id=turn_project_id,
            call_kind=CALL_KIND,
            prefix_fingerprint=fingerprint,
            call_attributes=call_attributes,
        )
        cache_status = (observability or {}).get("cacheStatus")

    if stop_reason == "cancelled":
        yield (outcome["reason"] or "preempted", None, cache_status)
        return
    text = "".join(text_parts).strip() if text_parts else _message_text(message).strip()
    if not text:
        yield ("tool_use_only" if _has_tool_use(message) else "error", None, cache_status)
        return
    if stop_reason in ("guardrail_intervened", "content_filtered"):
        logger.info("handoff brief stopped by the model: %s", stop_reason)
        yield ("error", None, cache_status)
        return

    brief_hash = context_hash(text)
    output_tokens = int((usage or {}).get("outputTokens") or 0)
    # The brief still shows if a write fails; that row's acceptance falls back to excerptHash.
    await _record_brief(
        user_id, handoff_id, run, brief_hash=brief_hash, brief_tokens=output_tokens, cache_status=cache_status
    )
    yield (None, brief_hash, cache_status)
