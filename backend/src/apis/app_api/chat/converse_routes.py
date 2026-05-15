"""Proxy for the API-key authenticated Bedrock Converse endpoint.

Forwards /chat/api-converse requests to the Inference API, which handles
cost accounting, quota enforcement, and the actual Bedrock call. This
ensures a single code path for all API-key traffic regardless of which
URL external consumers use.

In production the Inference API lives on a separate Fargate service
(AgentCore Runtime) reachable via INFERENCE_API_URL. Locally it defaults
to http://localhost:8001.
"""

import logging
import os

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["api-converse"])

_INFERENCE_API_URL = os.environ.get("INFERENCE_API_URL", "http://localhost:8001")

_PROXY_TIMEOUT_SECONDS = 300.0


def _build_upstream_client() -> httpx.AsyncClient:
    """Single seam where the proxy's upstream client is constructed.

    Tests substitute a MockTransport-backed client here without having to
    monkey-patch the global `httpx.AsyncClient` symbol — which would also
    intercept any test-side httpx clients running in the same process.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(_PROXY_TIMEOUT_SECONDS))


@router.post(
    "/api-converse",
    summary="Converse with a Bedrock model via API key (proxied to Inference API)",
    responses={
        401: {"description": "Invalid or expired API key"},
        502: {"description": "Inference API unreachable"},
    },
)
async def api_converse_proxy(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """Thin proxy that forwards the request to the Inference API.

    The Inference API handles API-key validation, quota checks, Bedrock
    invocation, and cost recording. This proxy simply relays the request
    and response (including SSE streams) so that external consumers can
    use the App API URL for everything.
    """
    target_url = f"{_INFERENCE_API_URL}/chat/api-converse"
    body = await request.body()

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": x_api_key,
    }

    logger.info(f"Proxying api-converse to {target_url}")

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
        logger.error(f"Proxy error: {exc}", exc_info=True)
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
