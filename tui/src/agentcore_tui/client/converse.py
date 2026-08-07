"""HTTP client for the API-key authenticated ``/chat/api-converse`` endpoint.

Transport only: SSE frames are handed to :mod:`agentcore_tui.client.events` for
parsing, and HTTP failures are translated into the typed errors in
:mod:`agentcore_tui.errors` so the UI layer never inspects a status code.

The client is constructor-injectable with an ``httpx.AsyncClient`` so tests can
drive it with ``httpx.MockTransport`` and never open a socket.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from .. import __version__
from ..config import Config
from ..conversation import Message
from ..errors import (
    AuthError,
    BadRequestError,
    ConfigError,
    ConnectionFailedError,
    ModelAccessDeniedError,
    RateLimitedError,
    UpstreamError,
)
from ..logging_setup import redact
from ..usage import Usage
from .auth import ApiKeyAuth, AuthProvider
from .endpoints import Endpoints
from .events import ConverseEvent, ErrorEvent, Metadata, MessageStop, TextDelta, parse_event

logger = logging.getLogger(__name__)

USER_AGENT = f"agentcore-tui/{__version__}"

#: Event names this client understands. Anything else is logged once as a
#: warning — a newer server adding events should be visible, not silent.
_KNOWN_EVENT_NAMES = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "reasoning_start",
        "reasoning_delta",
        "reasoning_stop",
        "content_block_stop",
        "message_stop",
        "metadata",
        "error",
        "done",
    }
)


def message_payloads(messages: Sequence[Message]) -> list[dict[str, str]]:
    """Domain messages in the shape this endpoint expects.

    Here rather than on :class:`~agentcore_tui.conversation.Message` so the
    domain type carries no endpoint's wire format. A second endpoint with a
    different message shape gets its own mapping beside its own client.
    """
    return [{"role": message.role, "content": message.content} for message in messages]


@dataclass(frozen=True, slots=True)
class CompletedTurn:
    """The finished result of one assistant turn."""

    text: str
    reasoning: str = ""
    usage: Usage | None = None
    stop_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _server_detail(response: httpx.Response) -> str:
    """Best-effort extraction of FastAPI's ``{"detail": ...}`` error body.

    Assumes the body has already been read; streaming responses must be
    ``aread()`` first.
    """
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
    return ""


class ApiConverseClient:
    """Talks to ``POST {base_url}/chat/api-converse``.

    ``auth`` and ``endpoints`` are injectable so the transport concerns are not
    welded to ``Config``: this client keeps Config only for the sampling
    parameters that genuinely are configuration (model, token ceiling,
    temperature, timeout).
    """

    def __init__(
        self,
        config: Config,
        *,
        auth: AuthProvider | None = None,
        endpoints: Endpoints | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.base_url:
            raise ConfigError("No base URL configured", hint="Run `agentcore-tui login --base-url https://your-host/api`.")
        if auth is None:
            if not config.api_key:
                raise ConfigError("No API key configured", hint="Run `agentcore-tui login` to store a key.")
            auth = ApiKeyAuth(config.api_key)

        self._config = config
        self._auth = auth
        self._endpoints = endpoints or Endpoints(config.base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=config.timeout_seconds,
                write=30.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )

    async def __aenter__(self) -> ApiConverseClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client, but only if we created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- request construction ------------------------------------------------

    async def _headers(self) -> dict[str, str]:
        # A fresh dict every call: httpx_sse mutates the mapping it is given,
        # and the auth provider may have refreshed a token since the last one.
        return {
            **await self._auth.headers(),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _payload(self, messages: Sequence[Message], *, model_id: str | None, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_id": model_id or self._config.model_id,
            "messages": message_payloads(messages),
            "stream": stream,
            "max_tokens": self._config.max_tokens,
        }
        if self._config.system_prompt:
            payload["system_prompt"] = self._config.system_prompt
        if self._config.temperature is not None:
            payload["temperature"] = self._config.temperature
        if self._config.top_p is not None:
            payload["top_p"] = self._config.top_p
        return payload

    def _map_error(self, response: httpx.Response, model_id: str) -> Exception:
        """Translate an error response into a typed, actionable exception."""
        detail = _server_detail(response)
        match response.status_code:
            case 401:
                return AuthError(detail or "API key was rejected")
            case 403:
                return ModelAccessDeniedError(model_id)
            case 429:
                return RateLimitedError(detail or "Rate limit or quota exceeded", retry_after=_retry_after(response))
            case 400 | 422:
                return BadRequestError(detail or "The request was rejected as invalid")
            case _:
                return UpstreamError(detail or f"Server returned HTTP {response.status_code}")

    # -- streaming -----------------------------------------------------------

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model_id: str | None = None,
    ) -> AsyncIterator[ConverseEvent]:
        """Yield parsed events for one turn.

        Raises the typed errors from :mod:`agentcore_tui.errors` for HTTP-level
        failures. A failure *inside* the stream arrives as an
        :class:`~agentcore_tui.client.events.ErrorEvent` instead, because the
        server has already committed a 200 by then.
        """
        target = model_id or self._config.model_id
        url = self._endpoints.api_converse
        payload = self._payload(messages, model_id=target, stream=True)

        prompt_chars = sum(len(message.content) for message in messages)
        logger.info(
            "stream start model=%s turns=%d prompt_chars=%d max_tokens=%s url=%s",
            target,
            len(messages),
            prompt_chars,
            self._config.max_tokens,
            url,
        )
        if messages:
            logger.debug("last user message: %s", redact(messages[-1].content))

        started = time.monotonic()
        counts: dict[str, int] = {}
        text_chars = 0

        try:
            async with aconnect_sse(self._client, "POST", url, json=payload, headers=await self._headers()) as source:
                # aconnect_sse does not raise_for_status, so check before
                # iterating — aiter_sse() would fail on the content-type of an
                # error body and mask the real cause.
                if source.response.status_code >= 400:
                    await source.response.aread()
                    error = self._map_error(source.response, target)
                    logger.warning("stream rejected status=%s -> %s", source.response.status_code, type(error).__name__)
                    raise error

                async for sse in source.aiter_sse():
                    event = parse_event(sse.event, sse.data)
                    name = type(event).__name__
                    counts[name] = counts.get(name, 0) + 1

                    # Per-event tracing is DEBUG-only: a long turn emits
                    # hundreds of deltas and would swamp an INFO log.
                    if isinstance(event, TextDelta):
                        text_chars += len(event.text)
                        logger.debug("delta idx=%d chars=%d", event.index, len(event.text))
                    elif isinstance(event, ErrorEvent):
                        logger.error("stream error event: %s", event.message)
                    elif isinstance(event, Metadata):
                        logger.info(
                            "usage input=%d output=%d cache_read=%s cache_write=%s",
                            event.usage.input_tokens,
                            event.usage.output_tokens,
                            event.usage.cache_read_input_tokens,
                            event.usage.cache_write_input_tokens,
                        )
                    elif isinstance(event, MessageStop):
                        logger.info("message_stop reason=%s", event.stop_reason)
                    elif sse.event not in _KNOWN_EVENT_NAMES:
                        logger.warning("unknown SSE event from server: %r", sse.event)

                    yield event
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.error("connect failed url=%s: %s", url, exc)
            raise ConnectionFailedError(self._config.base_url, str(exc) or type(exc).__name__) from exc
        except httpx.ReadTimeout as exc:
            logger.error("read timeout after %.1fs url=%s", self._config.timeout_seconds, url)
            raise ConnectionFailedError(
                self._config.base_url,
                f"no data for {self._config.timeout_seconds:.0f}s",
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("http error url=%s: %s", url, exc, exc_info=True)
            raise ConnectionFailedError(self._config.base_url, str(exc) or type(exc).__name__) from exc
        finally:
            logger.info(
                "stream end model=%s elapsed=%.2fs text_chars=%d events=%s",
                target,
                time.monotonic() - started,
                text_chars,
                counts or "{}",
            )

    # -- non-streaming -------------------------------------------------------

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model_id: str | None = None,
    ) -> CompletedTurn:
        """Run one turn without streaming and return the whole response."""
        target = model_id or self._config.model_id
        payload = self._payload(messages, model_id=target, stream=False)

        try:
            response = await self._client.post(self._endpoints.api_converse, json=payload, headers=await self._headers())
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise ConnectionFailedError(self._config.base_url, str(exc) or type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise ConnectionFailedError(self._config.base_url, str(exc) or type(exc).__name__) from exc

        if response.status_code >= 400:
            raise self._map_error(response, target)

        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError("Server returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise UpstreamError("Server returned an unexpected response shape")

        raw_usage = body.get("usage")
        content = body.get("content")
        reasoning = body.get("reasoning")
        stop_reason = body.get("stop_reason")
        return CompletedTurn(
            text=content if isinstance(content, str) else "",
            reasoning=reasoning if isinstance(reasoning, str) else "",
            usage=Usage.from_payload(raw_usage) if isinstance(raw_usage, dict) else None,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        )
