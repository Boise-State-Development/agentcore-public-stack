"""Startup warm-up for the inference-api container.

Every new conversation lands on a fresh AgentCore micro-VM. Whatever the first
message has to do before its first token — import the symbolic-math library
behind the calculator tool, load botocore's service models for the clients the
turn will build — is paid on that user's first-word latency. Splitting cold
first-turns into phases on a 40k-user load run put that residual at 3.6 s
(docs/specs/load-test-assessment-2026-09.md §1 fix 2). Precompiled bytecode
(the Dockerfile) removes the compile; this module moves the rest to container
start, where it overlaps the Runtime's own readiness wait instead of the
user's request.

Runs on a daemon thread so ``/ping`` answers immediately. Python's import lock
makes a first request that arrives mid-warm-up wait for the in-progress import
rather than redo it, so there is no double work. Best-effort throughout: a
failure here is logged and the first turn simply pays what it always paid.

Gated by ``INFERENCE_WARMUP_ENABLED`` (default on; ``=false`` is the kill
switch, per the flags convention in CLAUDE.md).
"""

import importlib
import logging
import os
import threading
import time
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

WARMUP_ENABLED_ENV = "INFERENCE_WARMUP_ENABLED"

# Modules the first turn imports lazily. Ordered heaviest-first so a request
# that arrives mid-warm-up finds the expensive ones already in progress.
WARM_MODULES: tuple[str, ...] = (
    # sympy, via the calculator tool (default-on in the registry) — the single
    # largest lazy import on the first-turn path.
    "strands_tools.calculator",
    # Agent construction path: model factory, session manager, hooks.
    "agents.main_agent.core.agent_factory",
    "agents.main_agent.base_agent",
    "agents.main_agent.session.session_factory",
    "apis.inference_api.chat.service",
)

# boto3 clients the turn builds. Creating one loads and parses the service's
# JSON model into the default session's loader cache, which every later client
# for the same service reuses — that parse, not the socket, is the cost.
WARM_BOTO_SERVICES: tuple[str, ...] = (
    "bedrock-runtime",
    "bedrock-agentcore",
    "dynamodb",
    "s3",
)

# The parse above is per *session*, and the two SDKs on the agent build (the
# AgentCore Memory session manager, Strands' BedrockModel) build their clients
# on the process-wide session from `apis.shared.aws_clients`
# (`agent_build_shared_session_enabled`, default on). Build those clients on
# it here, so the first turn finds them parsed.
WARM_SHARED_SESSION_SERVICES: tuple[str, ...] = (
    "bedrock-agentcore",
    "bedrock-agentcore-control",
    "bedrock-runtime",
)


def warmup_enabled() -> bool:
    """Whether startup warm-up runs. Empty/unset means on."""
    return os.environ.get(WARMUP_ENABLED_ENV, "").strip().lower() != "false"


def _timed(label: str, fn: Callable[[], None]) -> None:
    started = time.perf_counter()
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - warm-up must never fail startup
        logger.info("warmup step=%s outcome=skipped error=%s", label, e)
        return
    logger.info("warmup step=%s outcome=ok ms=%d", label, int((time.perf_counter() - started) * 1000))


def warm_modules(modules: Iterable[str] = WARM_MODULES) -> None:
    """Import each module once; a missing optional dependency is logged, not raised."""
    for name in modules:
        _timed(f"import:{name}", lambda name=name: importlib.import_module(name))


def warm_boto_clients(services: Iterable[str] = WARM_BOTO_SERVICES) -> None:
    """Build one client per service so the service model is parsed and cached."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        logger.info("warmup step=boto outcome=skipped error=no region configured")
        return
    import boto3

    for service in services:
        _timed(f"boto:{service}", lambda service=service: boto3.client(service, region_name=region))


def warm_shared_session(services: Iterable[str] = WARM_SHARED_SESSION_SERVICES) -> None:
    """Build the agent build's shared boto3 session, its clients, and the memory strategy ids.

    Everything here is client construction — no sockets — except the last
    step. ``warm_strategy_ids`` is a control-plane read of the memory's
    strategy ids (static configuration, cached for the life of the process),
    and it opens the one connection warm-up otherwise avoids. That is
    accepted (docs/specs/turn-path-ttft.md §5 P2): the alternative is paying
    the read on the first turn, which is the turn this exists to shorten; the
    call is idempotent, so botocore retries a connection error; and on
    Runtime V2, where a warmed process is snapshotted and restored, the
    restore's first turn is the thing to watch for a pool holding a socket
    that did not survive.
    """
    from apis.shared.feature_flags import agent_build_shared_session_enabled

    if not agent_build_shared_session_enabled():
        logger.info("warmup step=shared outcome=skipped error=AGENT_BUILD_SHARED_SESSION_ENABLED=false")
        return
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        logger.info("warmup step=shared outcome=skipped error=no region configured")
        return
    from apis.shared.aws_clients import shared_boto_session

    session = shared_boto_session()
    for service in services:
        _timed(f"shared:{service}", lambda service=service: session.client(service, region_name=region))

    from agents.main_agent.session.session_factory import warm_strategy_ids

    _timed("shared:strategy_ids", warm_strategy_ids)


def run_warmup() -> None:
    """The whole warm-up, synchronously. Exposed for tests and for callers that
    want it inline."""
    started = time.perf_counter()
    warm_modules()
    warm_boto_clients()
    warm_shared_session()
    logger.info("warmup complete ms=%d", int((time.perf_counter() - started) * 1000))


def start_warmup_in_background() -> threading.Thread | None:
    """Kick off :func:`run_warmup` on a daemon thread. Returns the thread, or
    ``None`` when the kill switch is set."""
    if not warmup_enabled():
        logger.info("warmup disabled via %s", WARMUP_ENABLED_ENV)
        return None
    thread = threading.Thread(target=run_warmup, name="inference-warmup", daemon=True)
    thread.start()
    return thread
