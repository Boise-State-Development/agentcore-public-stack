"""BFF chat proxy — forwards browser SSE chat requests to inference-api.

`POST /chat/stream` is the cookie-authenticated chat path for the SPA.
The flow:

  Browser  → CloudFront `/api/*`  → app-api  → inference-api `/invocations`
           (httpOnly session cookie)         (Authorization: Bearer <token>)

`SessionRefreshMiddleware` resolves the cookie and, if the stored Cognito
access token is near expiry, refreshes it before this handler runs. The
handler then forwards `current_user.raw_token` — the freshly-validated
access token — to inference-api, which accepts Cognito Bearer tokens via
`get_current_user_trusted` on `/invocations`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import (
    session_fork_enabled,
    session_prewarm_enabled,
    session_prewarm_prepare_enabled,
    session_prewarm_prepare_holdout_percent,
)
from apis.shared.sessions.prepare_arm import ARM_HOLDOUT, prepare_arm
from apis.shared.observability.emf import emit_chat_stream_upstream_dropped
from apis.shared.harness.runner import (
    apply_runtime_session_header,
    build_invocations_url,
    runtime_session_affinity_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["bff-chat-proxy"])

def _inference_api_url() -> str:
    return os.environ.get("INFERENCE_API_URL", "http://localhost:8001")

# Long enough to cover a full agent turn (model + tool calls), bounded so a
# wedged upstream eventually surfaces.
_PROXY_TIMEOUT_SECONDS = 300.0

# Longest gap between bytes from the Runtime. The Runtime sends nothing while
# the model is silent (a model whose thinking isn't streamed; the Claude 5.x
# models stream a summary, see `_apply_thinking_binding`) or a slow tool runs,
# so this must outlast the Runtime's own model read timeout
# (`RETRY_READ_TIMEOUT`, 600s) by enough for its explained timeout error to
# arrive. Shorter, and the
# proxy would cut the stream first and the user would lose the connection
# instead (`test_read_timeout_budget.py`). Connect, write and pool keep 300s.
_PROXY_READ_TIMEOUT_SECONDS = 660.0

# SSE keepalive cadence. Two hops in front of this response cut an idle
# connection at 60s — CloudFront's `OriginReadTimeout` and the ALB's
# `idle_timeout` — while an agent turn can legitimately go quiet for longer
# than that whenever a slow tool runs (code interpreter, a burst of MCP calls)
# with nothing to stream. The browser then reports a network error, the
# half-finished turn is marked `connection_lost`, and the user's resend races
# the session lease. A comment line is the SSE no-op: a line beginning with
# ':' carries no field, so it puts bytes on the wire — resetting both idle
# timers — without reaching the SPA's event parser.
#
# Exactly ONE trailing newline, deliberately. A blank line is what *dispatches*
# an event, and `@microsoft/fetch-event-source` dispatches on it unconditionally
# rather than suppressing empty messages the way the SSE spec allows — so
# `":…\n\n"` would deliver a phantom event with no name to `parseEventSourceMessage`.
#
# Emitted here rather than on inference-api for two reasons: both timeouts sit
# downstream of app-api, and interposing a task on the agent stream would
# change how client cancellation reaches that turn's interruption handling.
# 20s leaves room for two lost frames inside a 60s window.
_SSE_KEEPALIVE_SECONDS = 20.0
_SSE_KEEPALIVE_FRAME = b": keepalive\n"

# Canonical `/invocations` URL resolution lives in the shared harness
# (`apis.shared.harness.runner.build_invocations_url`) — the headless
# runner, this proxy, and the MCP Apps proxy all share one copy. Kept
# under the historical private name so existing call sites and docstring
# references stay valid.
_build_invocations_url = build_invocations_url


# Copy the SPA renders under its "Already responding" notice. Sent in place of
# the Runtime's opaque 424 body, which carries no usable explanation once the
# container's own 409 detail has been swallowed by the rewrite.
_SESSION_BUSY_DETAIL = (
    "A response is already streaming for this conversation. "
    "Wait for it to finish before sending another message."
)


# How the Runtime's 424 body names the status the container actually sent, e.g.
# `{"message":"Received error (409) from runtime. Please check your CloudWatch
# logs for more information."}` (dev, 2026-11-14). The container raises 409 on
# `/invocations` only for the single-flight conflict.
_RUNTIME_UPSTREAM_STATUS = re.compile(rb"Received error \((\d{3})\)")


async def _resolve_upstream_error_status(
    status_code: int,
    session_id: Optional[str],
    user_id: str,
    error_body: bytes,
) -> tuple[int, Optional[str]]:
    """Undo the AgentCore Runtime's 424 rewrite of the single-flight 409.

    The Runtime data plane maps *any* non-2xx from the inference-api container
    to `424 Failed Dependency`, so the container's deliberate 409 — "a response
    is already streaming for this conversation" — reaches the SPA as a fatal
    424 and surfaces as a "Chat Request Failed" toast, instead of the soft
    "Already responding" notice the SPA already implements for 409.

    The 424's body names the status the container sent, and that decides it
    when present: a named 409 is re-mapped, any other named status keeps the
    424. The lease cannot decide the 409 case: the turn can release it
    between the container's 409 and this look (a resend just after Stop does
    exactly that), which left the fatal 424 standing for a mere conflict. Nor
    the 500 case: a container that fails while another turn streams would be
    disguised as a conflict.

    Only a 424 whose body names no status falls back to the lease as the
    tiebreaker — re-map only when an unexpired turn lease is actually held for
    this session, the exact fact a container 409 would have asserted.
    Best-effort: anything unproven keeps the 424, so a real upstream failure
    is never disguised as a conflict.

    Returns the status to relay and, when re-mapped, the detail to relay with
    it (the Runtime's 424 body no longer explains anything useful).
    """
    if status_code != status.HTTP_424_FAILED_DEPENDENCY:
        return status_code, None
    named = _RUNTIME_UPSTREAM_STATUS.search(error_body)
    if named is not None:
        if int(named.group(1)) != status.HTTP_409_CONFLICT:
            return status_code, None
        logger.info(
            "Upstream 424 names the container's 409 — relaying as 409 "
            "(single-flight conflict)"
        )
        return status.HTTP_409_CONFLICT, _SESSION_BUSY_DETAIL
    if not session_id:
        return status_code, None
    try:
        from apis.shared.sessions.session_lease import is_session_lease_held

        if await is_session_lease_held(session_id, user_id):
            logger.info(
                "Upstream 424 for a session with a live turn lease — "
                "relaying as 409 (single-flight conflict)"
            )
            return status.HTTP_409_CONFLICT, _SESSION_BUSY_DETAIL
    except Exception:  # noqa: BLE001 - explanatory lookup only, never fatal
        logger.debug("Could not check session lease for 424 mapping", exc_info=True)
    return status_code, None


# What the Runtime's inbound JWT authorizer says when it refuses the token we
# forwarded. It answers from the data plane, before the container sees the
# request, so retrying it cannot run a turn twice.
_RUNTIME_TOKEN_REFUSED_MARKER = b"Unauthorized inbound token"

# Lifetime to demand of the token before the one retry. Not a known Runtime
# margin: on dev (2026-10-09) the Runtime refused a token with ~61s left, then
# accepted ones with 61.8s and 60.6s left in timed tests, so the refusal looks
# transient rather than age-based. Refreshing a token this close to expiry is
# still the cheaper bet for the retry, and a token a peer task just refreshed
# (~an hour left) is adopted rather than refreshed again.
_RUNTIME_RETRY_MIN_TOKEN_SECONDS = 300


async def _token_for_retry(
    request: Request, error_body: bytes, forwarded_token: str
) -> Optional[str]:
    """The access token to retry a Runtime token refusal with, or None.

    None only when the 403 is not a token refusal, which is relayed as-is.
    For a refusal, a refreshed token is preferred; when the refresh yields the
    same token, fails, or has no session handle to go through, the forwarded
    token is retried as-is, since a refusal that does not track the token's
    age is most likely transient.
    """
    if _RUNTIME_TOKEN_REFUSED_MARKER not in error_body:
        return None
    refresh = getattr(request.state, "bff_refresh_session", None)
    if refresh is None:
        return forwarded_token
    try:
        record = await refresh(_RUNTIME_RETRY_MIN_TOKEN_SECONDS)
    except Exception:  # noqa: BLE001 - a failed refresh still gets the one retry
        logger.warning("Could not refresh the session after a Runtime 403", exc_info=True)
        return forwarded_token
    if record is None:
        return forwarded_token
    return record.cognito_access_token


# A raw 409 from the AgentCore Runtime data plane. Our container's own 409
# (the single-flight lease) never arrives raw: the data plane rewrites any
# non-2xx from the container to 424 (`_resolve_upstream_error_status`). So a
# raw 409 is the Runtime refusing the invocation before the container sees it,
# which on dev (2026-11-21) is a turn that arrives while another invocation is
# still starting the same runtime session: the composer's prewarm fired on the
# first keystroke, the Runtime began a V2 snapshot restore (2,017 ms), and a
# send 270 ms later was answered 409 and shown as "Already responding". Nothing
# ran, so the turn can be sent again. Only that failing path waits; a turn
# that is accepted first time pays nothing.
_RUNTIME_STARTING_BUDGET_SECONDS = 15.0
# Between polls when no warm call of this task's own is in flight to wait on (the warm call
# went through another app-api task). Capped at 0.5 s: on dev a 1.5 s step left a turn waiting
# 1.8 s after the session had started, and a refused resend is a cheap, fast 409.
_RUNTIME_STARTING_POLL_SECONDS = (0.25, 0.5)


def _apply_oauth_callback_header(headers: Dict[str, str], forwarded_callback: Optional[str]) -> None:
    """Carry the SPA's ``OAuth2CallbackUrl`` header upstream when it supplied one.

    Shared by the chat stream and a prewarm prepare, so both reach the Runtime
    with the same header set.
    """
    if forwarded_callback:
        headers["OAuth2CallbackUrl"] = forwarded_callback


def _is_runtime_target(target_url: str) -> bool:
    """Whether the proxy talks to the AgentCore Runtime data plane (not local inference-api)."""
    return urlsplit(target_url).netloc.startswith("bedrock-agentcore.")


async def _wait_out_runtime_session_start(
    client: httpx.AsyncClient,
    target_url: str,
    headers: Dict[str, str],
    body: bytes,
    response: httpx.Response,
    warm_key: Tuple[str, Optional[str]],
) -> httpx.Response:
    """Resend a turn the Runtime refused with a raw 409 until its session has started.

    Waits on this task's own in-flight warm call for the same runtime session when
    there is one (the usual cause), else polls. Bounded by
    ``_RUNTIME_STARTING_BUDGET_SECONDS``; the last answer is returned either way,
    so a 409 that outlasts the budget is relayed as before.
    """
    started = time.monotonic()
    attempts = 0
    while response.status_code == status.HTTP_409_CONFLICT:
        remaining = _RUNTIME_STARTING_BUDGET_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            break
        await response.aread()
        await response.aclose()
        warm = _prewarm_inflight.get(warm_key)
        if warm is not None and not warm.done():
            # `asyncio.wait` never cancels the warm call it waits on.
            await asyncio.wait({warm}, timeout=remaining)
        else:
            delay = _RUNTIME_STARTING_POLL_SECONDS[min(attempts, len(_RUNTIME_STARTING_POLL_SECONDS) - 1)]
            await asyncio.sleep(min(delay, remaining))
        attempts += 1
        response = await client.send(
            client.build_request("POST", target_url, headers=headers, content=body),
            stream=True,
        )
    logger.info(
        "Runtime session was still starting (409); resent the turn %d times over %d ms -> %s",
        attempts,
        int((time.monotonic() - started) * 1000),
        response.status_code,
    )
    return response


def _build_upstream_client() -> httpx.AsyncClient:
    """Single seam where the proxy's upstream client is constructed.

    Tests substitute a MockTransport-backed client here without having to
    monkey-patch the global `httpx.AsyncClient` symbol — which would also
    intercept any test-side httpx clients running in the same process.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_PROXY_TIMEOUT_SECONDS, read=_PROXY_READ_TIMEOUT_SECONDS)
    )


async def chat_stream(
    request: Request,
    current_user: User = Depends(get_current_user_from_session),
):
    """Relay the request body verbatim to inference-api `/invocations`.

    The body is opaque bytes — validation lives on inference-api so this
    handler stays decoupled from the InvocationRequest schema. SSE chunks
    flow back unmodified; `X-Accel-Buffering: no` defeats proxy buffering
    so streaming events (notably `oauth_required` after `message_stop`)
    reach the browser without being held back by an intermediary.
    """
    target_url = _build_invocations_url(_inference_api_url())
    body = await request.body()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_user.raw_token}",
    }

    # Pin this user's conversation to one microVM so the container it lands on
    # stays warm across turns. Pinned per (user, session), never per session
    # alone: two users on one session id must not share a container and its
    # agent cache (see `runtime_session_id_for`). Measured in dev, steady-state turns:
    #
    #   no pinning, agent-cache miss     ~7.6s
    #   pinned, agent-cache miss         ~4.8s   ← warm container alone
    #   pinned, agent-cache hit          ~3.9s   ← + a reused Agent
    #
    # Note what that split means: most of the win is the **warm container**,
    # which every session gets, not the agent-cache hit, which only cacheable
    # ones get. An earlier note here credited the whole ~7.6→~3.9s to the
    # cache; that conflated the two, and the honest read is that this header
    # helps 100% of traffic while the agent cache adds ~19% on top for the
    # subset that can use it.
    #
    # It does NOT change the prompt-cache token split — that was already
    # stable — so this is a latency fix, not a cost one
    # (docs/specs/agent-cache-extra-tools-bypass.md §8).
    #
    # This is the one place that has to look inside the body, which the proxy
    # otherwise relays verbatim. Read-only and best-effort: a body that isn't
    # JSON, or carries no session_id, simply goes unpinned rather than failing
    # the turn — schema validation still belongs to inference-api.
    #
    # A fork pins to the conversation its family started from instead
    # (`affinity_session_id`, rewind/fork spec §7): its first turn starts from
    # the parent's live history, which exists only in the parent's container,
    # and later turns stay there so they never fall back to a stored copy that
    # lacks the long-term-memory context the cached prefix was written with.
    # Client-supplied and safe to be: the hash includes the user, so a body can
    # only choose among that user's own containers, and the Runtime checks the
    # fork relation on the session row before it uses anything it finds there.
    session_id: Optional[str] = None
    affinity_id: Optional[str] = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            raw = parsed.get("session_id")
            session_id = raw if isinstance(raw, str) and raw else None
            pinned = parsed.get("affinity_session_id")
            affinity_id = pinned if isinstance(pinned, str) and pinned else None
    except (ValueError, TypeError):
        logger.debug("chat proxy: body is not JSON; skipping runtime-session pinning")
    pinned_id = (
        affinity_id
        if affinity_id is not None and session_id is not None and session_fork_enabled()
        else session_id
    )
    apply_runtime_session_header(headers, pinned_id, current_user.user_id)

    # Forward OAuth2CallbackUrl when the SPA supplies it. Inference-api's
    # AgentCoreContextMiddleware reads this header to scope the on-tool
    # OAuth consent landing URL to the SPA's origin (allowlisted via
    # CORS_ORIGINS). Without it, MCP-tool consent flows can't redirect
    # back to the SPA's `/oauth-complete` page and `oauth_required` SSE
    # events are unusable. Forwarded as-is — the inference-api side
    # re-validates against its own CORS_ORIGINS allowlist.
    _apply_oauth_callback_header(headers, request.headers.get("OAuth2CallbackUrl"))

    # The client lifecycle must outlive this handler — closing it via
    # `async with` while a stream is in flight makes httpx drain the upstream
    # response during `__aexit__`, buffering the entire SSE stream before
    # headers reach the browser. Open the client manually and tie its
    # cleanup to the streaming generator's `finally` (or to the early-exit
    # paths below) so headers can flush as soon as the upstream's first
    # response message arrives.
    client = _build_upstream_client()
    try:
        response = await client.send(
            client.build_request("POST", target_url, headers=headers, content=body),
            stream=True,
        )
        if response.status_code == status.HTTP_403_FORBIDDEN:
            # Only a request that has already failed reaches this branch, so
            # the healthy turn path pays nothing for it.
            error_body = await response.aread()
            retry_token = await _token_for_retry(
                request, error_body, current_user.raw_token
            )
            if retry_token is not None:
                await response.aclose()
                token_kind = (
                    "same" if retry_token == current_user.raw_token else "refreshed"
                )
                headers["Authorization"] = f"Bearer {retry_token}"
                response = await client.send(
                    client.build_request(
                        "POST", target_url, headers=headers, content=body
                    ),
                    stream=True,
                )
                # One line per refusal, so the logs answer whether a retry
                # recovers and whether a refreshed token was what it took.
                logger.info(
                    "Runtime refused the forwarded access token; retried once "
                    "with the %s token -> %s",
                    token_kind,
                    response.status_code,
                )
        if response.status_code == status.HTTP_409_CONFLICT and _is_runtime_target(target_url):
            response = await _wait_out_runtime_session_start(
                client, target_url, headers, body, response, (current_user.user_id, pinned_id)
            )
    except httpx.ConnectError:
        await client.aclose()
        logger.error(f"Cannot reach Inference API at {target_url}")
        raise HTTPException(status_code=502, detail="Inference API is unreachable")
    except httpx.TimeoutException:
        await client.aclose()
        logger.error(f"Inference API request timed out: {target_url}")
        raise HTTPException(status_code=504, detail="Inference API request timed out")
    except Exception as exc:
        await client.aclose()
        logger.error(f"BFF chat proxy error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="An unexpected error occurred while proxying to the Inference API",
        )

    if response.status_code >= 400:
        try:
            error_body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        relay_status, relay_detail = await _resolve_upstream_error_status(
            response.status_code, session_id, current_user.user_id, error_body
        )
        raise HTTPException(
            status_code=relay_status,
            detail=(
                relay_detail
                if relay_detail is not None
                else error_body.decode("utf-8", errors="replace")
            ),
        )

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        async def stream_relay():
            # Upstream reads run on their own task so a silent turn can be
            # distinguished from a finished one: on timeout the read stays
            # pending (shielded from `wait_for`'s cancellation) and is awaited
            # again next pass, while we slip a keepalive onto the wire.
            chunks = response.aiter_bytes()
            pending: Optional[asyncio.Future] = None
            # These are raw transport chunks, not parsed frames, so a chunk can
            # end mid-frame; injecting there would corrupt it (`data: {"text":`
            # + `: keepalive` reads as one field). Only inject once the bytes
            # already forwarded end a frame. A stall *inside* a frame therefore
            # goes uncovered — acceptable, since a frame is written upstream in
            # one go, and this is never worse than sending nothing at all.
            at_frame_boundary = True
            last_chunk_at = time.monotonic()
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(chunks.__anext__())
                    try:
                        chunk = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=_SSE_KEEPALIVE_SECONDS
                        )
                    except httpx.TransportError as exc:
                        # The Runtime stream broke mid-turn: our read timeout,
                        # or the connection closed under us. Counted
                        # (`ChatStreamUpstreamDropped`, alarmed) and re-raised,
                        # so the browser sees the same dropped connection as
                        # before. A browser disconnect cancels this generator
                        # instead, so it never lands here.
                        pending = None
                        silent_seconds = time.monotonic() - last_chunk_at
                        logger.warning(
                            "Runtime stream dropped mid-turn after %.0fs of silence "
                            "(session %s): %s: %s",
                            silent_seconds,
                            session_id,
                            type(exc).__name__,
                            exc,
                        )
                        emit_chat_stream_upstream_dropped(
                            reason=type(exc).__name__,
                            silent_seconds=silent_seconds,
                            session_id=session_id,
                        )
                        raise
                    except asyncio.TimeoutError:
                        if at_frame_boundary:
                            yield _SSE_KEEPALIVE_FRAME
                        # Deliberately keep `pending`. A chunk that lands in
                        # the same tick as the timeout is already sitting in
                        # that future; dropping it here to start a fresh read
                        # would silently swallow an SSE frame.
                        continue
                    except StopAsyncIteration:
                        pending = None
                        return
                    pending = None  # consumed — safe to read the next one
                    last_chunk_at = time.monotonic()
                    yield chunk
                    at_frame_boundary = chunk.endswith(b"\n\n")
            finally:
                if pending is not None:
                    pending.cancel()
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_relay(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        response_body = await response.aread()
    finally:
        await response.aclose()
        await client.aclose()
    return StreamingResponse(
        iter([response_body]),
        media_type=content_type or "application/json",
        status_code=response.status_code,
    )


router.add_api_route(
    "/stream",
    chat_stream,
    methods=["POST"],
    summary="Cookie-authenticated SSE proxy to inference-api /invocations",
    operation_id="chat_stream",
    responses={
        401: {"description": "No active BFF session"},
        403: {"description": "CSRF token missing or invalid"},
        502: {"description": "Inference API unreachable"},
        504: {"description": "Inference API request timed out"},
    },
)


# ---------------------------------------------------------------------------
# Session prewarm (docs/specs/agentcore-runtime-v2.md §5a)
# ---------------------------------------------------------------------------
#
# AgentCore starts a conversation's microVM only when an invocation arrives
# carrying its runtime session id, so a new conversation's first send pays the
# start: a V2 snapshot restore (~0.7 s) and the first-turn setup that cannot be
# snapshotted. `POST /chat/prewarm` sends a no-op `warm` invocation with the
# same affinity header the first real turn will carry, as soon as the SPA opens
# the conversation, so that start overlaps the user reading and typing.
#
# The browser never waits on it: the route answers 202 at once and forwards in
# the background. It never fails anything either: an upstream error is logged
# and the first send simply starts the microVM itself, exactly as before.

# Re-warm the same conversation no sooner than this. Inside the Runtime's
# 900 s idle window, so a conversation the user keeps open stays warm across a
# re-warm, while a page reload or two within the window costs nothing extra.
_PREWARM_REWARM_AFTER_SECONDS = 600.0
# Per-user ceiling on forwarded warms per minute, so a script or a burst of
# tabs cannot start microVMs faster than any person could use them.
_PREWARM_MAX_PER_USER_PER_MINUTE = 10
# A restore takes about a second on V2 and a cold boot several on V1; a warm
# call still pending after this is abandoned (the first send starts the VM).
_PREWARM_TIMEOUT_SECONDS = 30.0
# Bound on the in-process ledger, so a long-lived task cannot grow it forever.
_PREWARM_LEDGER_MAX_ENTRIES = 20_000


class PrewarmRequest(BaseModel):
    """The conversation to warm: the id the SPA will send its first turn with."""

    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # A fork's turns run in its root conversation's microVM (see the chat
    # proxy), so that is the one to warm.
    affinity_session_id: Optional[str] = Field(None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # Prepare: also build the turn's agent ahead of the send. Honoured only
    # while SESSION_PREWARM_PREPARE_ENABLED is on; the selection below is the
    # subset of a chat request that decides which agent the turn builds, and
    # only `_PREPARE_SELECTION_FIELDS` ever reaches the Runtime.
    prepare: Optional[bool] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    caching_enabled: Optional[bool] = None
    enabled_tools: Optional[List[str]] = None
    provider: Optional[str] = None
    max_tokens: Optional[int] = None
    inference_params: Optional[Dict[str, Any]] = None
    rag_assistant_id: Optional[str] = None
    agent_type: Optional[str] = None
    enabled_skills: Optional[List[str]] = None
    selected_prompt_id: Optional[str] = None
    incognito: Optional[bool] = None
    agent_mention: Optional[bool] = None
    system_prompt: Optional[str] = None


# The selection fields a prepare may forward. An ALLOWLIST on purpose: a warm
# call must never be able to run a turn, so a field a chat request carries
# (message, files, interrupt responses, handoff and workflow fields, client
# tools, run_mode...) is forwarded only if it is named here, and a field added
# to the request model later is dropped until someone adds it deliberately.
_PREPARE_SELECTION_FIELDS: Tuple[str, ...] = (
    "model_id",
    "temperature",
    "caching_enabled",
    "enabled_tools",
    "provider",
    "max_tokens",
    "inference_params",
    "rag_assistant_id",
    "agent_type",
    "enabled_skills",
    "selected_prompt_id",
    "incognito",
    "agent_mention",
    "system_prompt",
)


def _prepare_upstream_body(body: PrewarmRequest) -> Dict[str, Any]:
    """The Runtime body for a prepare: the warm body plus the allowlisted selection."""
    upstream: Dict[str, Any] = {"session_id": body.session_id, "warm": True, "prepare": True}
    if body.affinity_session_id and session_fork_enabled():
        upstream["affinity_session_id"] = body.affinity_session_id
    for name in _PREPARE_SELECTION_FIELDS:
        value = getattr(body, name)
        if value is not None:
            upstream[name] = value
    return upstream


class _PrewarmLedger:
    """In-process dedupe and rate limit for warm calls.

    Per app-api task, not shared: a warm that slips past it on another task
    costs one idle Runtime session, never data, so a DynamoDB round trip on
    every page load is not worth it (the shared ``RateLimiter`` writes one per
    call).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_warm: Dict[Tuple[str, str], float] = {}
        # A prepare is its own stage: a page-load warm must not suppress the
        # prepare that follows it, but a prepare implies a warm.
        self._last_prepare: Dict[Tuple[str, str], float] = {}
        self._recent_by_user: Dict[str, List[float]] = {}

    def admit(self, user_id: str, session_id: str, now: float, prepare: bool = False) -> str:
        """``"accepted"``, ``"recently_warmed"`` or ``"rate_limited"``.

        Keyed on ``(user, conversation)`` per stage. A prepare is declined only
        by an earlier prepare; a plain warm is declined by an earlier warm or
        prepare. The per-user rate limit is shared by both.
        """
        with self._lock:
            key = (user_id, session_id)
            last = (self._last_prepare if prepare else self._last_warm).get(key)
            if last is not None and now - last < _PREWARM_REWARM_AFTER_SECONDS:
                return "recently_warmed"
            recent = [t for t in self._recent_by_user.get(user_id, []) if now - t < 60.0]
            if len(recent) >= _PREWARM_MAX_PER_USER_PER_MINUTE:
                self._recent_by_user[user_id] = recent
                return "rate_limited"
            recent.append(now)
            self._recent_by_user[user_id] = recent
            self._last_warm[key] = now
            if prepare:
                self._last_prepare[key] = now
            if len(self._last_warm) > _PREWARM_LEDGER_MAX_ENTRIES or len(self._last_prepare) > _PREWARM_LEDGER_MAX_ENTRIES:
                self._prune(now)
            return "accepted"

    def _prune(self, now: float) -> None:
        self._last_warm = {
            key: t for key, t in self._last_warm.items() if now - t < _PREWARM_REWARM_AFTER_SECONDS
        }
        self._last_prepare = {
            key: t for key, t in self._last_prepare.items() if now - t < _PREWARM_REWARM_AFTER_SECONDS
        }
        self._recent_by_user = {
            user: [t for t in times if now - t < 60.0]
            for user, times in self._recent_by_user.items()
            if any(now - t < 60.0 for t in times)
        }

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._last_warm.clear()
            self._last_prepare.clear()
            self._recent_by_user.clear()


_prewarm_ledger = _PrewarmLedger()
# Strong references to in-flight forwards: the event loop holds tasks weakly.
_prewarm_tasks: Set[asyncio.Task] = set()
# This task's warm calls still in flight, by (user, runtime-pinned session id): a turn the Runtime
# refuses while one starts the same session waits on it (`_wait_out_runtime_session_start`).
_prewarm_inflight: Dict[Tuple[str, Optional[str]], asyncio.Task] = {}


def _build_prewarm_client() -> httpx.AsyncClient:
    """Seam for tests, like ``_build_upstream_client``."""
    return httpx.AsyncClient(timeout=httpx.Timeout(_PREWARM_TIMEOUT_SECONDS))


async def _forward_prewarm(
    session_id: str,
    user_id: str,
    access_token: str,
    prepare_body: Optional[Dict[str, Any]] = None,
    oauth_callback_url: Optional[str] = None,
    holdout_session_id: Optional[str] = None,
) -> None:
    """Send the warm invocation and log how long the start took. Never raises.

    ``prepare_body`` (a prepare) replaces the plain warm body and brings the
    headers up to a chat turn's set, because the Runtime's agent build talks to
    MCP servers as the user.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    apply_runtime_session_header(headers, session_id, user_id)
    if prepare_body is not None:
        _apply_oauth_callback_header(headers, oauth_callback_url)
        body = json.dumps(prepare_body)
    elif holdout_session_id is not None:
        # The experiment's holdout arm: a plain warm that says which arm it is. It names the
        # conversation itself (a fork warms under its root's id), because the Runtime keys the
        # arm on the conversation the send will name; a warm reads nothing else from the body.
        body = json.dumps({"session_id": holdout_session_id, "warm": True, "prepare_arm": ARM_HOLDOUT})
    else:
        body = json.dumps({"session_id": session_id, "warm": True})
    started = time.monotonic()
    try:
        async with _build_prewarm_client() as client:
            response = await client.post(
                _build_invocations_url(_inference_api_url()), headers=headers, content=body
            )
        message = "prewarm forwarded -> %s in %d ms"
        args: Tuple[Any, ...] = (response.status_code, int((time.monotonic() - started) * 1000))
        if prepare_body is not None:
            prepared, reason = _read_prepare_outcome(response)
            message += " prepare=true prepared=%s reason=%s"
            args += (prepared, reason)
        elif holdout_session_id is not None:
            message += " prepare=holdout"
        logger.info(message, *args)
    except Exception as exc:  # noqa: BLE001 - a failed warm is a missed optimisation, never an error
        logger.warning(
            "prewarm failed after %d ms: %s",
            int((time.monotonic() - started) * 1000),
            type(exc).__name__,
        )


def _read_prepare_outcome(response: httpx.Response) -> Tuple[str, str]:
    """``(prepared, reason)`` for the log line, from the Runtime's JSON when it parses."""
    try:
        payload = response.json()
    except ValueError:
        return "unknown", "-"
    if not isinstance(payload, dict):
        return "unknown", "-"
    prepared = payload.get("prepared")
    reason = payload.get("reason")
    return (
        "true" if prepared is True else "false" if prepared is False else "unknown",
        reason if isinstance(reason, str) and reason else "-",
    )


async def chat_prewarm(
    request: Request,
    body: PrewarmRequest,
    current_user: User = Depends(get_current_user_from_session),
) -> JSONResponse:
    """Start the conversation's Runtime microVM before its first send.

    Always 202 once admitted or declined, so the SPA has nothing to handle:
    ``status`` says what happened (``accepted``, ``recently_warmed``,
    ``rate_limited``, or ``skipped`` when runtime-session affinity is off and a
    warm call could not reach the microVM the turn will use). 404 while
    ``SESSION_PREWARM_ENABLED`` is off.
    """
    if not session_prewarm_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not runtime_session_affinity_enabled():
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "skipped"})

    warm_id = body.affinity_session_id if body.affinity_session_id and session_fork_enabled() else body.session_id
    requested = body.prepare is True and session_prewarm_prepare_enabled()
    arm = prepare_arm(body.session_id, session_prewarm_prepare_holdout_percent()) if requested else None
    holdout = arm == ARM_HOLDOUT
    prepare = requested and not holdout
    # A holdout is admitted on the prepare stage so repeats dedupe like a treated one.
    outcome = _prewarm_ledger.admit(current_user.user_id, warm_id, time.monotonic(), prepare=requested)
    if outcome == "accepted":
        if prepare:
            forward = _forward_prewarm(
                warm_id,
                current_user.user_id,
                current_user.raw_token,
                prepare_body=_prepare_upstream_body(body),
                oauth_callback_url=request.headers.get("OAuth2CallbackUrl"),
            )
        else:
            forward = _forward_prewarm(
                warm_id,
                current_user.user_id,
                current_user.raw_token,
                holdout_session_id=body.session_id if holdout else None,
            )
        task = asyncio.create_task(forward)
        _prewarm_tasks.add(task)
        task.add_done_callback(_prewarm_tasks.discard)
        key = (current_user.user_id, warm_id)
        _prewarm_inflight[key] = task
        task.add_done_callback(
            lambda done, key=key: _prewarm_inflight.pop(key, None) if _prewarm_inflight.get(key) is done else None
        )
    content: Dict[str, str] = {"status": outcome}
    if arm is not None:
        content["prepareArm"] = arm
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=content)


router.add_api_route(
    "/prewarm",
    chat_prewarm,
    methods=["POST"],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a conversation's AgentCore Runtime session ahead of its first send",
    operation_id="chat_prewarm",
    responses={
        401: {"description": "No active BFF session"},
        404: {"description": "Session prewarm is not enabled"},
    },
)
