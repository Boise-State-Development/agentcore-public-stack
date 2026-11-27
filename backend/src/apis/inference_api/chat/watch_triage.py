"""The ``watch_triage`` invocation: replay a watcher's probe and compare (quiet-watcher triage §4.1).

A non-turn invocation, like ``workflow_prompt``: no agent is built, no history restored, no
session row or Memory event written and no lease taken. The route resolves the tools the full
run would carry through the turn's own phases (``_watch_triage_tool_inputs`` in ``routes.py``),
then this module:

1. loads them as the full run's agent would (``agents.main_agent.tool_set.load_tools``) and keeps
   the probe's names, refusing a probe whose tool is missing (RBAC, ``enabled_tools``, an Agent's
   bindings, a server not connected) or no longer read-only by ``runs_in_parallel``;
2. replays every call concurrently, each within ``REPLAY_TIMEOUT_SECONDS``: all are read-only, so
   concurrency is safe by the policy ``ToolConcurrencyPlugin`` applies;
3. fingerprints each result with ``fingerprint_result``, the recorder's own function, and
   compares with the baseline.

Every failure escalates (§2.5): a missing or writing tool is ``probe_refused``; a call that
raises, times out or returns ``status: "error"`` is ``probe_error``. The stream is one
``watch_triage_result`` frame, then ``done``.

Each call is made with an empty invocation state, as nothing here runs inside an agent loop:
a tool that needs the agent's ``ToolContext`` raises and the probe escalates, never guesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Mapping, Optional

from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION, fingerprint_result
from apis.shared.scheduled_prompts.watch_triage import (
    REPLAY_TIMEOUT_SECONDS,
    TriageVerdict,
    WatchTriageRequest,
    compare_fingerprints,
    escalate,
)

logger = logging.getLogger(__name__)

#: ``(tool, arguments, tool_use_id) -> the tool's result``. The real one streams the tool.
CallTool = Callable[[Any, Dict[str, Any], str], Awaitable[Optional[Mapping[str, Any]]]]


async def replay_call(tool: Any, arguments: Dict[str, Any], tool_use_id: str) -> Optional[Mapping[str, Any]]:
    """One call, made the way Strands' executor makes it: the tool's stream, last result wins."""
    from strands.types._events import ToolResultEvent

    tool_use = {"toolUseId": tool_use_id, "name": tool.tool_name, "input": arguments}
    result: Optional[Mapping[str, Any]] = None
    async for event in tool.stream(tool_use, {}):
        if isinstance(event, ToolResultEvent):
            result = event.tool_result
    return result


def _read_only(tool: Any, name: str) -> bool:
    from agents.main_agent.core.tool_concurrency import runs_in_parallel

    try:
        return bool(runs_in_parallel(tool, name))
    except Exception:
        return False


async def triage_probe(
    request: WatchTriageRequest,
    tools_by_name: Mapping[str, Any],
    *,
    call_tool: CallTool = replay_call,
    timeout_seconds: float = REPLAY_TIMEOUT_SECONDS,
) -> TriageVerdict:
    """Tier 1 over tools already resolved: the re-check, the replay and the compare. Never raises."""
    if request.fingerprint_version != FINGERPRINT_VERSION:
        return escalate("probe_refused")

    planned: List[tuple] = []
    for call in request.calls:
        tool = tools_by_name.get(call.name)
        if tool is None or not _read_only(tool, call.name):
            logger.info("watch triage: probe tool %s is gone or no longer read-only", call.name)
            return escalate("probe_refused")
        planned.append((tool, call.arguments()))

    async def _one(index: int, tool: Any, arguments: Dict[str, Any]) -> Optional[Mapping[str, Any]]:
        tool_use_id = f"triage-{uuid.uuid4().hex[:12]}-{index}"
        return await asyncio.wait_for(call_tool(tool, arguments, tool_use_id), timeout_seconds)

    outcomes = await asyncio.gather(
        *(_one(i, tool, arguments) for i, (tool, arguments) in enumerate(planned)),
        return_exceptions=True,
    )

    fingerprints: List[Optional[str]] = []
    failed = False
    for call, outcome in zip(request.calls, outcomes):
        if isinstance(outcome, BaseException) or not isinstance(outcome, Mapping):
            if isinstance(outcome, BaseException):
                logger.info("watch triage: replay of %s failed: %s", call.name, type(outcome).__name__)
            fingerprints.append(None)
            failed = True
            continue
        fingerprints.append(fingerprint_result(outcome))
        if outcome.get("status") == "error":
            failed = True
    if failed:
        return escalate("probe_error", fingerprints)
    return compare_fingerprints(request.baseline, fingerprints)


ResolveTools = Callable[[], Awaitable[Optional[Dict[str, Any]]]]


async def run_watch_triage(
    request: WatchTriageRequest,
    *,
    resolve_inputs: ResolveTools,
    load: Optional[Callable[..., Any]] = None,
    call_tool: CallTool = replay_call,
) -> TriageVerdict:
    """Resolve, load, replay and compare. ``resolve_inputs`` is the route's phases.

    It returns ``load_tools``' keyword arguments, or ``None`` when the turn would not run (the
    Agent refused this caller), which is ``probe_refused``. Never raises.
    """
    registry = None
    try:
        inputs = await resolve_inputs()
        if inputs is None:
            return escalate("probe_refused")
        if load is None:
            from agents.main_agent.tool_set import load_tools as load
        tools_by_name, registry, _gated = await asyncio.to_thread(load, **inputs)
        return await triage_probe(request, tools_by_name, call_tool=call_tool)
    except Exception:
        logger.warning("watch triage: could not resolve or replay the probe", exc_info=True)
        return escalate("probe_error")
    finally:
        if registry is not None:
            try:
                await asyncio.to_thread(registry.cleanup)
            except Exception:
                logger.warning("watch triage: could not release the probe's MCP clients", exc_info=True)


async def stream_watch_triage(
    request: WatchTriageRequest,
    *,
    enabled: bool,
    resolve_inputs: ResolveTools,
    **deps: Any,
) -> AsyncGenerator[str, None]:
    """The invocation's stream: one ``watch_triage_result`` frame, then ``done``."""
    started = time.monotonic()
    if not enabled:
        verdict = escalate("flag_off")
    else:
        verdict = await run_watch_triage(request, resolve_inputs=resolve_inputs, **deps)
    logger.info(
        "watch triage for schedule %s: %s (%s) in %d ms",
        request.schedule_id,
        verdict.verdict,
        verdict.reason,
        int((time.monotonic() - started) * 1000),
    )
    yield verdict.sse_frame()
    yield f"event: done\ndata: {json.dumps({})}\n\n"


__all__ = ["replay_call", "run_watch_triage", "stream_watch_triage", "triage_probe"]
