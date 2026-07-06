"""`run_agent_headless` — the F1 headless run entrypoint.

Given ``(user_id, prompt)`` and no live session: mint a per-owner bearer,
POST the AgentCore Runtime ``/invocations`` data plane, drain the SSE stream
server-side, pass the governance floor, and land the result as a retrievable
session. Every trigger (schedule worker, "Run now" route, A2A server,
webhook) is just a caller of this function.

A2A-readiness: the seam is ``(user_id, prompt, resolved config) → RunResult``
plus the optional ``on_event`` callback, which relays every typed SSE event
as it arrives — an A2A server front can map that stream onto task status
updates without a rewrite. Reminder from CLAUDE.md: if this is ever exposed
as an A2A server, its advertised ``capabilities`` MUST include
``streaming=True``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx

from apis.shared.harness.auth import BearerAuthStrategy, HeadlessAuthError
from apis.shared.harness.governance import GovernanceFloor
from apis.shared.harness.models import RunResult
from apis.shared.harness.sse import InvocationStreamAccumulator, iter_sse_events

logger = logging.getLogger(__name__)

# Matches the app-api chat proxy's budget for a full agent turn; the runtime
# data plane itself enforces a hard cap in the same range.
DEFAULT_TIMEOUT_SECONDS = 300.0

OnEvent = Callable[[str, Dict[str, Any]], Awaitable[None]]


def _build_http_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Single seam where the runner's upstream client is constructed.

    Tests substitute a MockTransport-backed client here without patching
    the global ``httpx.AsyncClient`` symbol (same pattern as the chat
    proxy's ``_build_upstream_client``).
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_invocations_url(base_url: str) -> str:
    """Resolve the upstream `/invocations` URL from an INFERENCE_API_URL-style base.

    This is the single canonical resolver — the app-api chat proxy
    (`proxy_routes.py`) and the MCP Apps proxy import it from here.

    Cloud: the base is the AgentCore Runtime data plane
    (`https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<ARN>`),
    where `<ARN>` is unencoded in SSM. The data-plane route is
    `POST /runtimes/{agentRuntimeArn}/invocations?qualifier={qualifier}`
    with `{agentRuntimeArn}` as a single URL-encoded path segment — the
    ARN's literal `/` and `:` must be percent-encoded or AWS returns 404.
    A `qualifier` is also required; we use `DEFAULT`.

    Local dev: the base is `http://localhost:8001`, where `/invocations`
    is a real FastAPI route on inference-api directly. No encoding or
    qualifier needed.
    """
    parts = urlsplit(base_url)
    prefix = "/runtimes/"
    if parts.netloc.startswith("bedrock-agentcore.") and parts.path.startswith(prefix):
        arn = parts.path[len(prefix):]
        encoded_arn = quote(arn, safe="")
        return (
            f"{parts.scheme}://{parts.netloc}/runtimes/{encoded_arn}"
            "/invocations?qualifier=DEFAULT"
        )
    return f"{base_url}/invocations"


async def run_agent_headless(
    *,
    user_id: str,
    prompt: str,
    auth: BearerAuthStrategy,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    title: Optional[str] = None,
    model_id: Optional[str] = None,
    rag_assistant_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    agent_type: Optional[str] = None,
    inference_params: Optional[Dict[str, Any]] = None,
    trigger: str = "manual",
    invocations_base_url: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    governance: Optional[GovernanceFloor] = None,
    on_event: Optional[OnEvent] = None,
) -> RunResult:
    """Run one agent turn as ``user_id`` with no live session.

    The run-config parameters (``model_id``, ``rag_assistant_id``,
    ``enabled_tools``, ``agent_type``, ``inference_params``) mirror the
    existing ``InvocationRequest`` contract — the entrypoint resolves *no*
    new config type (scheduled-agent-runs.md decision #6).

    ``enabled_tools=None`` means "the user's defaults" — inference-api
    resolves it to **all RBAC-allowed tools** for the owner, exactly as an
    attended chat turn would. That is the right semantic for the attended
    "Run now" surface; *schedules* should instead snapshot an explicit tool
    list at creation time so a sleeping schedule's behavior doesn't shift
    when the catalog or the owner's grants change underneath it
    (spike-findings punch list #7 — enforce in the Phase B schedule model).

    Returns a ``RunResult`` in all outcomes except audit-start failure and
    ``HeadlessAuthError`` (both raise: a run we cannot audit or authenticate
    must not execute).
    """
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    session_id = session_id or f"headless-{uuid.uuid4().hex[:16]}"
    governance = governance or GovernanceFloor()
    started_at = _now_iso()

    # F6a checkpoints 1 + 2 — before any token mint or model spend.
    await governance.on_run_start(
        run_id=run_id,
        user_id=user_id,
        session_id=session_id,
        trigger=trigger,
        prompt=prompt,
    )
    await governance.check_input(prompt=prompt, user_id=user_id)

    result = RunResult(
        run_id=run_id,
        session_id=session_id,
        user_id=user_id,
        status="error",
        started_at=started_at,
    )

    try:
        bearer = await auth.mint_bearer_for_user(user_id)
    except HeadlessAuthError:
        result.finished_at = _now_iso()
        result.error = "auth: could not mint a bearer for the user"
        await governance.on_run_end(result=result)
        raise

    base_url = invocations_base_url or os.environ.get(
        "INFERENCE_API_URL", "http://localhost:8001"
    )
    url = build_invocations_url(base_url)
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "message": prompt,
    }
    if model_id is not None:
        payload["model_id"] = model_id
    if rag_assistant_id is not None:
        payload["rag_assistant_id"] = rag_assistant_id
    if enabled_tools is not None:
        payload["enabled_tools"] = enabled_tools
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if inference_params is not None:
        payload["inference_params"] = inference_params

    acc = InvocationStreamAccumulator()
    try:
        async with _build_http_client(timeout_seconds) as client:
            async with client.stream(
                "POST",
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {bearer}",
                },
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    result.error = (
                        f"HTTP {response.status_code}: "
                        f"{body.decode('utf-8', errors='replace')[:500]}"
                    )
                else:
                    async for name, data in iter_sse_events(
                        response.aiter_lines()
                    ):
                        acc.handle(name, data)
                        if on_event is not None:
                            await on_event(name, data)
    except httpx.TimeoutException:
        result.status = "timeout"
        result.error = f"stream exceeded {timeout_seconds:.0f}s budget"
    except httpx.HTTPError as exc:
        result.error = f"transport: {type(exc).__name__}: {exc}"

    result.final_message = acc.final_message
    result.stop_reason = acc.stop_reason
    result.tool_trace = acc.tool_trace
    result.usage = acc.finalize_usage()
    result.oauth_required = acc.oauth_required
    result.title = acc.title
    result.events_seen = acc.events_seen
    result.error = result.error or acc.error
    if result.status != "timeout":
        if result.error:
            result.status = "error"
        elif acc.oauth_required:
            result.status = "oauth_required"
        elif acc.done:
            result.status = "completed"
        else:
            result.status = "error"
            result.error = "stream ended without a done event"
    result.finished_at = _now_iso()

    # F6a checkpoint 3 — classify before delivery.
    await governance.classify_output(result=result)

    # Delivery (minimal F2): the runtime already persisted the session row,
    # messages, and an auto-generated title during the turn. Make the row's
    # existence unconditional (idempotent belt-and-braces for error paths)
    # and let an explicit caller title win over the generated one.
    try:
        from apis.shared.sessions.metadata import (
            ensure_session_metadata_exists,
            update_session_title,
        )

        await ensure_session_metadata_exists(session_id, user_id)
        if title:
            await update_session_title(session_id, user_id, title)
            result.title = title
    except Exception:
        logger.error(
            "Headless run %s: result delivery failed", run_id, exc_info=True
        )

    # F6a checkpoint 4 — outcome audit (best-effort inside).
    await governance.on_run_end(result=result)
    return result
