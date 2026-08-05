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

import json
import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
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

# Canonical `/invocations` URL resolution lives in the shared harness
# (`apis.shared.harness.runner.build_invocations_url`) — the headless
# runner, this proxy, and the MCP Apps proxy all share one copy. Kept
# under the historical private name so existing call sites and docstring
# references stay valid.
_build_invocations_url = build_invocations_url


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

    # Pin this conversation to one microVM so inference-api's in-process caches
    # survive between turns. Measured in dev: without it every turn is an agent
    # cache miss and pays a full `initialize()` (~7.5-8.1s/turn); with it, turns
    # after the first hit and run ~3.1s. It does NOT change the prompt-cache
    # token split — that was already stable — so this is a latency fix, not a
    # cost one (docs/specs/agent-cache-extra-tools-bypass.md §6 read).
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
        raise HTTPException(
            status_code=response.status_code,
            detail=error_body.decode("utf-8", errors="replace"),
        )

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        async def stream_relay():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
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
