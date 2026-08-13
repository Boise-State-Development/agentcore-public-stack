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
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.harness.runner import (
    apply_runtime_session_header,
    build_invocations_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["bff-chat-proxy"])

def _inference_api_url() -> str:
    return os.environ.get("INFERENCE_API_URL", "http://localhost:8001")

# Long enough to cover a full agent turn (model + tool calls), bounded so a
# wedged upstream eventually surfaces.
_PROXY_TIMEOUT_SECONDS = 300.0

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


async def _resolve_upstream_error_status(
    status_code: int, session_id: Optional[str], user_id: str
) -> tuple[int, Optional[str]]:
    """Undo the AgentCore Runtime's 424 rewrite of the single-flight 409.

    The Runtime data plane maps *any* non-2xx from the inference-api container
    to `424 Failed Dependency`, so the container's deliberate 409 — "a response
    is already streaming for this conversation" — reaches the SPA as a fatal
    424 and surfaces as a "Chat Request Failed" toast, instead of the soft
    "Already responding" notice the SPA already implements for 409.

    A 424 is ambiguous on its own: a genuine container 500 looks identical. So
    the lease itself is the tiebreaker — re-map only when an unexpired turn
    lease is actually held for this session, which is the exact fact the
    container's 409 asserted a moment earlier. Best-effort: anything unproven
    keeps the 424, so a real upstream failure is never disguised as a conflict.

    Returns the status to relay and, when re-mapped, the detail to relay with
    it (the Runtime's 424 body no longer explains anything useful).
    """
    if status_code != status.HTTP_424_FAILED_DEPENDENCY or not session_id:
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


def _build_upstream_client() -> httpx.AsyncClient:
    """Single seam where the proxy's upstream client is constructed.

    Tests substitute a MockTransport-backed client here without having to
    monkey-patch the global `httpx.AsyncClient` symbol — which would also
    intercept any test-side httpx clients running in the same process.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(_PROXY_TIMEOUT_SECONDS))


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

    # Pin this conversation to one microVM so the container it lands on stays
    # warm across turns. Measured in dev, steady-state turns:
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
    session_id: Optional[str] = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            raw = parsed.get("session_id")
            session_id = raw if isinstance(raw, str) and raw else None
    except (ValueError, TypeError):
        logger.debug("chat proxy: body is not JSON; skipping runtime-session pinning")
    apply_runtime_session_header(headers, session_id)

    # Forward OAuth2CallbackUrl when the SPA supplies it. Inference-api's
    # AgentCoreContextMiddleware reads this header to scope the on-tool
    # OAuth consent landing URL to the SPA's origin (allowlisted via
    # CORS_ORIGINS). Without it, MCP-tool consent flows can't redirect
    # back to the SPA's `/oauth-complete` page and `oauth_required` SSE
    # events are unusable. Forwarded as-is — the inference-api side
    # re-validates against its own CORS_ORIGINS allowlist.
    forwarded_callback = request.headers.get("OAuth2CallbackUrl")
    if forwarded_callback:
        headers["OAuth2CallbackUrl"] = forwarded_callback

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
            response.status_code, session_id, current_user.user_id
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
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(chunks.__anext__())
                    try:
                        chunk = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=_SSE_KEEPALIVE_SECONDS
                        )
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
