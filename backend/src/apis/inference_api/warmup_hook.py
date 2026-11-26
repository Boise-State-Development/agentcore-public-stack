"""The AgentCore Runtime V2 build warmup hook (``POST /warmup``).

On platform version V2 a Runtime configured with
``hooksConfiguration.build.warmup`` is called twice while AgentCore prepares a
new version (every ``CreateAgentRuntime`` and ``UpdateAgentRuntime``), after the
container passes ``/ping`` and before the Runtime reaches ``READY``:

1. ``memory``, before the snapshot. Whatever the process holds when this call
   returns is what every session starts with.
2. ``disk``, after the snapshot. The files read during this call are the ones
   AgentCore makes fast for new sessions; without it, a file in the snapshot is
   slow the first time each session reads it.

The warmup type arrives in ``X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type``.
The two calls share a fixed 120 seconds, a call that does not answer ``2xx``
fails the create or update, and ``/ping`` must keep answering throughout
(docs: *Configure lifecycle hooks for AgentCore Runtime*, *Optimize your agent
for AgentCore Runtime V2*; ours: docs/specs/agentcore-runtime-v2.md §5b).

What each call does here:

* ``memory`` runs :func:`apis.inference_api.warmup.run_warmup` again. Startup
  already ran it before the first ``/ping`` (the hold-``/ping`` contract in
  ``warmup.py``, which stays), so this is a few milliseconds of cached imports
  and client construction. It is here so the snapshot is warm even if a later
  change to startup breaks that ordering, and it loads nothing new: nothing
  time-bounded, per-user or able to change without a deploy may be in the
  snapshot (TTL caches, tokens, the Gateway tool list).
* ``disk`` does once what a session's first request does, as far as that can
  be done with no user, no token and no network: it builds one throwaway
  ``ChatAgent`` (prompt builder, tool registry, hooks, plugins, the Strands
  ``Agent`` and its Bedrock model) and discards it, formats one traceback, and
  reads the source files of the loaded modules, which is what a session's
  first traceback reads.

Deploy safety. A failed hook call fails the deploy and leaves the Runtime in
``UPDATE_FAILED``, so this handler fails one only when the container cannot
serve the route at all. Every step is best-effort and logged; a step that
raises is skipped; a phase that outlives its own bound answers ``200`` and
finishes on its thread; an unknown or missing warmup type answers ``200``
without doing anything, so a third type AWS adds later cannot fail a deploy of
this image. The price of a skipped step is the one paid before the hook
existed: the first turn of a new session does that work itself.

Nothing here runs on a turn. The hook is called during a deploy, before any
session exists.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

# Must match `hooksConfiguration.build.warmup.action.httpPost.path` on the
# Runtime (inference-agentcore-construct.ts; a CDK test reads this constant).
# No port is configured, so AgentCore calls the serving port.
WARMUP_HOOK_PATH = "/warmup"

WARMUP_TYPE_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type"
WARMUP_TYPE_MEMORY = "memory"
WARMUP_TYPE_DISK = "disk"

# The two calls share 120 s, fixed by the platform. These bounds leave room
# for both even when each runs to its own limit; past one the call answers 200
# and the work finishes on its thread, as `warm_before_ready` does at startup.
MEMORY_PHASE_TIMEOUT_SECONDS = 45.0
DISK_PHASE_TIMEOUT_SECONDS = 45.0

# Ceiling on reading module sources within the disk phase. A restored microVM
# measured about 1 s on its first traceback, which is `linecache` reading the
# source of every frame for the first time.
SOURCE_READ_BUDGET_SECONDS = 15.0

# First-party code first: it is on most frames of any traceback this service
# logs, so it is what a short budget should cover.
_FIRST_PARTY_PACKAGES: Tuple[str, ...] = ("apis", "agents")

_WARMUP_SESSION_SUFFIX = "warmup-hook"
_WARMUP_USER_ID = "warmup-hook"

router = APIRouter(tags=["agentcore-runtime"])

# One call at a time. AgentCore sends the two in order; this only makes a
# stray concurrent call (a retry, a curl in a local container) wait its turn.
_hook_lock = asyncio.Lock()


def _step(label: str, fn: Callable[[], Any]) -> Tuple[bool, Any]:
    """Run one best-effort step. Returns ``(ok, result)``; never raises."""
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - a warm-up step must never fail a deploy
        logger.warning("warmup_hook step=%s outcome=skipped error=%s: %s", label, type(e).__name__, e)
        return False, None
    logger.info("warmup_hook step=%s outcome=ok ms=%d", label, int((time.perf_counter() - started) * 1000))
    return True, result


def build_throwaway_agent() -> Dict[str, int]:
    """Build one ``ChatAgent`` the way a first turn does, offline, and drop it.

    Returns the build's stage timings in milliseconds (the same marks a turn's
    ``turn_prelude`` line carries under ``agent_build``).

    What keeps it offline:

    * A preview session id, so ``SessionFactory`` returns the in-memory
      ``PreviewSessionManager`` and no AgentCore Memory call is made.
    * ``enabled_tools=None``, so the tool catalog is never read and no Gateway
      or external MCP client is built. The local tools are passed as
      ``extra_tools`` instead, so the Strands ``Agent`` still registers real
      tool specs.
    * No skills, no user, no auth token.


    It goes through ``agent_types.create_agent``, the call
    ``chat.service.get_agent`` makes on a cache miss, and not through
    ``get_agent`` itself: that would put this agent in the process's agent
    cache and move its build counter, both of which belong to real sessions.

    Not covered, because it cannot be done without the network: the AgentCore
    Memory session manager and its restore (``agent_build.session_mgr`` and
    ``finalize`` on a turn), and the Gateway and MCP tool stages.
    """
    from agents.main_agent.agent_types import create_agent
    from agents.main_agent.config.constants import Prefixes
    from agents.main_agent.tools.tool_registry import create_default_registry
    from apis.shared.observability.build_stages import reset_stage_recorder, set_stage_recorder

    registry = create_default_registry()
    local_tools = [registry.get_tool(tool_id) for tool_id in registry.get_all_tool_ids()]

    stages: Dict[str, int] = {}
    last = time.perf_counter()

    def record(stage: str) -> None:
        nonlocal last
        now = time.perf_counter()
        stages[stage] = stages.get(stage, 0) + int((now - last) * 1000)
        last = now

    token = set_stage_recorder(record)
    try:
        create_agent(
            agent_type="chat",
            session_id=f"{Prefixes.PREVIEW_SESSION}{_WARMUP_SESSION_SUFFIX}",
            user_id=_WARMUP_USER_ID,
            enabled_tools=None,
            extra_tools=local_tools,
        )
    finally:
        reset_stage_recorder(token)
    return stages


def format_one_traceback() -> int:
    """Raise, catch and format one exception; returns the formatted length.

    The first ``traceback.format_exc()`` in a process pays for ``traceback``
    and ``linecache``'s own first use on top of the source reads.
    """
    try:
        raise RuntimeError("warm-up traceback probe")
    except RuntimeError:
        return len(traceback.format_exc())


def _loaded_source_files() -> List[str]:
    """Source paths of every loaded module, first-party packages first."""
    first_party: List[str] = []
    others: List[str] = []
    seen: set[str] = set()
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not isinstance(path, str) or not path.endswith(".py") or path in seen:
            continue
        seen.add(path)
        (first_party if name.split(".", 1)[0] in _FIRST_PARTY_PACKAGES else others).append(path)
    return sorted(first_party) + sorted(others)


def read_loaded_sources(budget_seconds: float = SOURCE_READ_BUDGET_SECONDS) -> Dict[str, Any]:
    """Read the source file of each loaded module, within a time budget.

    A traceback reads the source of every frame through ``linecache``. These
    are plain reads whose bytes are dropped: the point is that the files were
    read during the ``disk`` call, not that anything is kept. Stops at the
    budget and says so; an unreadable file is skipped.
    """
    paths = _loaded_source_files()
    deadline = time.perf_counter() + budget_seconds
    files = 0
    total_bytes = 0
    for path in paths:
        if time.perf_counter() >= deadline:
            break
        try:
            with open(path, "rb") as handle:
                total_bytes += len(handle.read())
        except OSError:
            continue
        files += 1
    return {"files": files, "of": len(paths), "bytes": total_bytes, "complete": files == len(paths)}


def run_memory_phase() -> None:
    """The ``memory`` call: the startup warm-up again, idempotently.

    What it loads stays referenced without help from here: imported modules
    live in ``sys.modules``, and the parsed service models live on boto3's
    default session and on the process-wide shared session, which is what
    AWS's "keep what you load" asks for.
    """
    from apis.inference_api.warmup import run_warmup, warmup_enabled

    if not warmup_enabled():
        logger.info("warmup_hook step=warmup outcome=skipped error=INFERENCE_WARMUP_ENABLED=false")
        return
    _step("warmup", run_warmup)


def run_disk_phase() -> None:
    """The ``disk`` call: one session's first-request work, with no network."""
    ok, stages = _step("agent_build", build_throwaway_agent)
    if ok:
        logger.info("warmup_hook agent_build stages_ms=%s", stages)
    _step("traceback", format_one_traceback)
    ok, sources = _step("sources", read_loaded_sources)
    if ok:
        logger.info("warmup_hook sources %s", sources)


_PHASES: Dict[str, Tuple[Callable[[], None], float]] = {
    WARMUP_TYPE_MEMORY: (run_memory_phase, MEMORY_PHASE_TIMEOUT_SECONDS),
    WARMUP_TYPE_DISK: (run_disk_phase, DISK_PHASE_TIMEOUT_SECONDS),
}


async def run_phase(warmup_type: Optional[str]) -> Dict[str, Any]:
    """Run one warmup phase off the event loop and report how it went.

    Never raises. ``outcome`` is ``ok``, ``timeout`` (the phase is still
    running on its thread), ``error`` (the phase raised outside its own
    steps) or ``ignored`` (no phase by that name).
    """
    phase = _PHASES.get((warmup_type or "").strip().lower())
    if phase is None:
        logger.warning("warmup_hook type=%r outcome=ignored", warmup_type)
        return {"warmupType": warmup_type, "outcome": "ignored", "ms": 0}

    fn, timeout = phase
    name = (warmup_type or "").strip().lower()
    async with _hook_lock:
        started = time.perf_counter()
        outcome = "ok"
        try:
            # A worker thread, so the loop keeps answering /ping meanwhile.
            await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
        except asyncio.TimeoutError:
            outcome = "timeout"
        except Exception as e:  # noqa: BLE001 - see "Deploy safety" above
            outcome = "error"
            logger.warning("warmup_hook type=%s raised %s: %s", name, type(e).__name__, e)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("warmup_hook type=%s outcome=%s ms=%d", name, outcome, elapsed_ms)
    return {"warmupType": name, "outcome": outcome, "ms": elapsed_ms}


@router.post(WARMUP_HOOK_PATH)
async def warmup_hook(request: Request) -> Dict[str, Any]:
    """AgentCore Runtime V2 build warmup hook.

    Allowed under CLAUDE.md's *Inference API boundary*, which keeps routes off
    this service because the Runtime data plane proxies only ``/invocations``
    and ``/ping``: this route is not meant to be reached through the data
    plane. AgentCore calls it itself, inside the container, while it prepares
    a Runtime version during create or update. No user request can reach it,
    it reads no body, and it does nothing a user could ask for.

    Always answers ``200`` once the phase has run (see the module docstring
    for why no failure here fails a deploy).
    """
    return await run_phase(request.headers.get(WARMUP_TYPE_HEADER))
