"""WebSocket relay between the SPA and the upstream AgentCore Runtime.

The browser's WebSocket sees only ``wss://<frontend>/api/voice/stream`` (the
BFF endpoint). The upstream WebSocket talks to either:

* **Cloud:** ``wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws``
  with the Cognito access token in ``Sec-WebSocket-Protocol`` so the
  AgentCore Runtime's JWT Authorizer accepts the upgrade.
* **Local dev:** ``ws://localhost:8001/voice/stream`` — a plain FastAPI
  WebSocket on inference-api with no upstream auth gate.

The relay is symmetric and fully duplex: client→upstream and upstream→client
run as concurrent tasks; the first to complete cancels the other so the
proxy doesn't leak half-open sockets. Frames are forwarded as text (JSON)
or binary unchanged. The ticket auth is enforced once, on upgrade — past
that, the BFF is just a pipe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional
from urllib.parse import quote, urlsplit

import aiohttp
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

# Connect timeout for the upstream upgrade. Generous because the AgentCore
# proxy can take a few seconds to validate the JWT and route to the runtime.
_UPSTREAM_CONNECT_TIMEOUT = 15.0
# Receive/heartbeat. The voice stream is chatty (audio chunks every ~100ms);
# anything past 60s of silence is a stalled upstream and worth surfacing.
_UPSTREAM_HEARTBEAT = 30.0


def _inference_api_url() -> str:
    return os.environ.get("INFERENCE_API_URL", "http://localhost:8001")


def build_upstream_ws_url(base_url: str) -> str:
    """Resolve the upstream WS URL from ``INFERENCE_API_URL``.

    Mirrors ``proxy_routes._build_invocations_url`` for the WebSocket path:
    cloud routes go to ``/runtimes/<encoded-arn>/ws``, local dev routes to
    ``/voice/stream`` directly. The ARN must be percent-encoded as a single
    path segment per AgentCore's data-plane contract.
    """
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    prefix = "/runtimes/"
    if parts.netloc.startswith("bedrock-agentcore.") and parts.path.startswith(prefix):
        arn = parts.path[len(prefix):]
        encoded_arn = quote(arn, safe="")
        # AgentCore Runtime serves WebSocket at /ws on the encoded-ARN path.
        # No `qualifier` query param is documented for WS, but the runtime
        # rejects extras, so leave it off.
        return f"{scheme}://{parts.netloc}/runtimes/{encoded_arn}/ws"
    return f"{scheme}://{parts.netloc}/voice/stream"


def _bearer_subprotocol(access_token: str) -> str:
    """Pack a Cognito access token into AgentCore's accepted subprotocol form.

    The runtime's JWT Authorizer reads ``base64UrlBearerAuthorization.<b64url>``
    from ``Sec-WebSocket-Protocol`` on the upgrade. Standard base64url with
    padding stripped — same shape the legacy SPA used pre-BFF cutover.
    """
    import base64

    b64 = base64.urlsafe_b64encode(access_token.encode("utf-8")).decode("ascii")
    return f"base64UrlBearerAuthorization.{b64.rstrip('=')}"


async def relay_voice_stream(
    *,
    client_ws: WebSocket,
    cognito_access_token: str,
    user_id: str,
) -> None:
    """Open the upstream WS and relay frames in both directions.

    Caller must have already accepted ``client_ws``. Returns once either
    side closes; the caller is responsible for closing ``client_ws`` in its
    finally block. Failures connecting upstream are reported back to the
    client as a ``bidi_error`` JSON frame before this function returns.
    """
    upstream_url = build_upstream_ws_url(_inference_api_url())

    parts = urlsplit(upstream_url)
    use_subprotocol = parts.netloc.startswith("bedrock-agentcore.")
    protocols: list[str] = []
    headers: dict[str, str] = {}

    if use_subprotocol:
        # Cloud: AgentCore proxy auths via Sec-WebSocket-Protocol.
        protocols = [_bearer_subprotocol(cognito_access_token), "base64UrlBearerAuthorization"]
    else:
        # Local dev: inference-api accepts a plain Authorization header on
        # the upgrade, and reads the auth_token from the first config
        # message. We forward the bearer header here so dev parity holds.
        headers["Authorization"] = f"Bearer {cognito_access_token}"

    timeout = aiohttp.ClientTimeout(total=None, connect=_UPSTREAM_CONNECT_TIMEOUT)
    session = aiohttp.ClientSession(timeout=timeout)
    upstream_ws: Optional[aiohttp.ClientWebSocketResponse] = None
    try:
        try:
            upstream_ws = await session.ws_connect(
                upstream_url,
                protocols=protocols or (),
                headers=headers,
                heartbeat=_UPSTREAM_HEARTBEAT,
                max_msg_size=0,  # unbounded — voice frames can be large
            )
        except aiohttp.WSServerHandshakeError as exc:
            logger.error("Upstream voice WS handshake failed: %s", exc)
            await _safe_send_error(client_ws, f"upstream rejected ({exc.status})")
            return
        except Exception as exc:
            logger.error("Upstream voice WS connect failed: %s", exc, exc_info=True)
            await _safe_send_error(client_ws, "upstream unreachable")
            return

        forward_to_upstream = asyncio.create_task(
            _pump_client_to_upstream(
                client_ws,
                upstream_ws,
                cognito_access_token=cognito_access_token,
                user_id=user_id,
            )
        )
        forward_to_client = asyncio.create_task(_pump_upstream_to_client(upstream_ws, client_ws))

        done, pending = await asyncio.wait(
            [forward_to_upstream, forward_to_client],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Expected after explicit cancellation.
                pass
            except Exception as exc:
                logger.debug("Ignored exception while cancelling relay task: %s", exc, exc_info=True)
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (asyncio.CancelledError, WebSocketDisconnect)):
                logger.warning("Voice relay task error: %s", exc)
    finally:
        if upstream_ws is not None and not upstream_ws.closed:
            try:
                await upstream_ws.close()
            except Exception as exc:
                logger.debug("Ignoring error while closing upstream voice WS: %s", exc, exc_info=True)
        try:
            await session.close()
        except Exception as exc:
            logger.warning("Failed to close upstream aiohttp session cleanly: %s", exc, exc_info=True)


async def _pump_client_to_upstream(
    client_ws: WebSocket,
    upstream_ws: aiohttp.ClientWebSocketResponse,
    *,
    cognito_access_token: str,
    user_id: str,
) -> None:
    """Forward every frame from the SPA to the AgentCore upstream.

    Every text frame is screened for a JSON config payload and, if matched,
    has its ``auth_token`` and ``user_id`` overwritten with the BFF-pinned
    values before being forwarded. The SPA must not be able to influence
    either field — both pin the identity inference-api attributes the
    session to. Non-config frames (audio chunks, control messages, anything
    that isn't a JSON object with ``type == "config"``) pass through
    unchanged.
    """
    try:
        while True:
            message = await client_ws.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                return

            text_frame = message.get("text")
            byte_frame = message.get("bytes")

            if text_frame is not None:
                text_frame = _inject_config_auth(
                    text_frame,
                    access_token=cognito_access_token,
                    user_id=user_id,
                )
                await upstream_ws.send_str(text_frame)
            elif byte_frame is not None:
                # Voice payloads are JSON-with-base64, not binary frames, but
                # forward bytes too for protocol forward-compat.
                await upstream_ws.send_bytes(byte_frame)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("client→upstream pump exited: %s", exc)


def _inject_config_auth(text_frame: str, *, access_token: str, user_id: str) -> str:
    """Overwrite ``auth_token`` and ``user_id`` on a JSON config frame.

    The SPA must not be able to influence either field — they pin the user
    identity that inference-api attributes the session to, so any client-
    supplied value is replaced with the BFF-authenticated one. Anything
    other than a JSON object with ``type == "config"`` is forwarded
    untouched (binary audio frames, control messages, malformed payloads).
    """
    try:
        parsed = json.loads(text_frame)
    except (json.JSONDecodeError, TypeError):
        return text_frame
    if not isinstance(parsed, dict):
        return text_frame
    if parsed.get("type") != "config":
        return text_frame
    parsed["auth_token"] = access_token
    parsed["user_id"] = user_id
    return json.dumps(parsed, separators=(",", ":"))


async def _pump_upstream_to_client(
    upstream_ws: aiohttp.ClientWebSocketResponse,
    client_ws: WebSocket,
) -> None:
    """Forward every frame from the AgentCore upstream to the SPA."""
    try:
        async for msg in upstream_ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await client_ws.send_text(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await client_ws.send_bytes(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                return
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("Upstream voice WS error frame: %s", upstream_ws.exception())
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("upstream→client pump exited: %s", exc)


async def _safe_send_error(client_ws: WebSocket, message: str) -> None:
    """Best-effort error frame to the SPA — swallow failures from a half-closed socket."""
    if client_ws.application_state != WebSocketState.CONNECTED:
        return
    try:
        await client_ws.send_json({"type": "bidi_error", "message": message})
    except Exception as exc:
        logger.debug("Failed to send bidi_error frame to client: %s", exc)
