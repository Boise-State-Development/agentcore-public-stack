"""Transport for ``POST /chat/stream`` — the tool-using agent.

The sibling of :mod:`agentcore_tui.client.converse`, and the same relationship
its dialect has to ``events``: that module speaks ``/chat/api-converse``, a bare
Converse wrapper with one LLM call, no tools, no memory and no server-side
session. This one speaks to the agent, where a single turn may hold several LLM
calls, tool invocations, RAG citations, compaction and artifacts.

Three things about this endpoint shape the code, and none of them is true of
``converse.py``:

**The conversation lives on the server.** app-api relays the body verbatim to
inference-api's ``/invocations``, which restores history from AgentCore Memory
by ``session_id``. So the payload carries **one** message — the new prompt — not
the transcript. Sending the transcript would duplicate every previous turn.

**It is session-authenticated.** There is no API-key path to the agent; the only
credential that works is a real BFF session (see
:class:`~agentcore_tui.client.auth.SessionAuth`). A caller holding only an API
key cannot reach this endpoint at all, which is why the constructor refuses to
guess an auth provider.

**A reopen re-runs the turn.** Never retry a stream transparently: the server has
already committed the user's message to memory and may have executed tools, so a
second attempt double-runs them and corrupts the conversation. Failures are
surfaced, never papered over — there is deliberately no retry logic here.

Parsing belongs to :mod:`agentcore_tui.client.agent_events`; this module is
transport only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from .. import __version__
from ..config import Config
from ..errors import (
    AuthError,
    BadRequestError,
    ConfigError,
    ConnectionFailedError,
    ModelAccessDeniedError,
    RateLimitedError,
    SessionBusyError,
    UpstreamError,
)
from ..logging_setup import redact
from .agent_events import (
    AgentEvent,
    Artifact,
    CitationEvent,
    ErrorEvent,
    Metadata,
    MetadataSummary,
    QuotaExceeded,
    SessionTitle,
    TextDelta,
    ToolResult,
    ToolUse,
    UnknownEvent,
    parse_agent_event,
)
from .auth import AuthProvider
from .endpoints import Endpoints

logger = logging.getLogger(__name__)

USER_AGENT = f"agentcore-tui/{__version__}"

#: Sent as ``client_surface`` on every turn. The server keys its interface
#: guidance off this, and also its agent cache — see the note in
#: ``_create_cache_key``, where the surface is a key dimension because it changes
#: the built system prompt without changing the raw ``system_prompt`` field.
#:
#: Must match a key in the backend's ``SURFACE_GUIDANCE``. An unrecognised value
#: degrades to web rather than failing the turn, so a mismatch is quiet — which
#: is why the value lives here as a named constant rather than inline.
CLIENT_SURFACE = "terminal"


def _server_detail(response: httpx.Response) -> str:
    """A human-readable message from an error response, if there is one."""
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class AgentStreamClient:
    """Streams one agent turn from ``POST {base_url}/chat/stream``.

    ``auth``, ``endpoints`` and ``client`` are injectable so tests drive this
    with ``httpx.MockTransport`` and never open a socket.
    """

    def __init__(
        self,
        config: Config,
        *,
        auth: AuthProvider,
        endpoints: Endpoints | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.base_url:
            raise ConfigError("No base URL configured", hint="Run `agentcore-tui login --base-url https://your-host/api`.")

        # `auth` is required rather than derived from Config, unlike
        # ApiConverseClient. There is exactly one credential this endpoint
        # accepts, and silently constructing the wrong one would produce a 401
        # that looks like an expired session rather than a programming error.
        self._config = config
        self._auth = auth
        self._endpoints = endpoints or Endpoints(config.base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                # A tool-using turn can be quiet for a long time while a tool
                # runs, so the read timeout is the whole-turn budget.
                read=config.timeout_seconds,
                write=30.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )

    async def __aenter__(self) -> AgentStreamClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client, but only if we created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- request construction ------------------------------------------------

    async def _headers(self) -> dict[str, str]:
        # A fresh dict every call: httpx_sse mutates the mapping it is given.
        return {
            **await self._auth.headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
        }

    def _payload(
        self,
        *,
        session_id: str,
        message: str,
        model_id: str | None,
        enabled_tools: Sequence[str] | None,
    ) -> dict[str, Any]:
        """The `InvocationRequest` shape, relayed verbatim by app-api.

        Only `session_id` and `message` are required. Everything else is omitted
        rather than sent as null, so the server's own defaults apply — notably
        `enabled_tools`, where **absent means "every tool my role grants" and an
        empty list means "none"**. Sending `[]` to mean "unset" would silently
        disable the agent's tools.

        `model_id` and `provider` are sent **together**, matching the web UI.
        Omitting the provider is not an error — inference-api resolves it from the
        managed-model registry (`input_data.provider or registry_provider`) — but
        sending it pins the choice to what the catalogue said at selection time
        rather than to registry state at request time. Omitting both is "System
        Default".
        """
        payload: dict[str, Any] = {
            "session_id": session_id,
            "message": message,
            # Tells the agent which client it is talking to, so its interface
            # guidance matches what the user can actually see and press. Without
            # it the server's default is "web", and terminal users get told to
            # click a gear icon that does not exist and handed KaTeX and Mermaid
            # that render here as literal noise.
            "client_surface": CLIENT_SURFACE,
        }
        target = model_id or self._config.model_id
        if target:
            payload["model_id"] = target
            # Only when we actually know it, because a guessed provider is
            # worse than none: absent lets the server resolve the model's own,
            # while a wrong one routes to a provider that has never heard of it.
            if self._config.provider:
                payload["provider"] = self._config.provider
        if self._config.max_tokens:
            payload["max_tokens"] = self._config.max_tokens
        if self._config.system_prompt:
            payload["system_prompt"] = self._config.system_prompt
        if self._config.temperature is not None:
            payload["temperature"] = self._config.temperature
        if enabled_tools is not None:
            payload["enabled_tools"] = list(enabled_tools)
        return payload

    def _map_error(self, response: httpx.Response, model_id: str) -> Exception:
        """Translate an error response into a typed, actionable exception."""
        detail = _server_detail(response)
        match response.status_code:
            case 401:
                return AuthError(
                    detail or "Your session was rejected",
                )
            case 403:
                # 403 on this endpoint is usually model RBAC, but it is also what
                # a missing CSRF exemption would produce. The hint covers the
                # case the user can act on.
                return ModelAccessDeniedError(model_id)
            case 409:
                # The per-session single-flight guard: a turn is already running
                # for this conversation. app-api relays this deliberately, undoing
                # the AgentCore Runtime's rewrite of it to 424.
                #
                # NOT an UpstreamError, which is where this used to land via the
                # catch-all. That carries the hint "retrying usually helps", and
                # here retrying immediately just conflicts again — the honest
                # advice is to wait, or stop the turn that is running.
                return SessionBusyError(detail or "A response is already streaming for this conversation")
            case 429:
                return RateLimitedError(detail or "Rate limit or quota exceeded", retry_after=_retry_after(response))
            case 400 | 422:
                return BadRequestError(detail or "The request was rejected as invalid")
            case _:
                return UpstreamError(detail or f"Server returned HTTP {response.status_code}")

    # -- streaming -----------------------------------------------------------

    async def stream(
        self,
        *,
        session_id: str,
        message: str,
        model_id: str | None = None,
        enabled_tools: Sequence[str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Yield parsed events for one agent turn.

        HTTP-level failures raise the typed errors in
        :mod:`agentcore_tui.errors`. A failure *inside* the stream arrives as an
        :class:`~agentcore_tui.client.agent_events.ErrorEvent`, because the
        server has already committed a 200 by then.
        """
        target = model_id or self._config.model_id
        url = self._endpoints.chat_stream
        payload = self._payload(
            session_id=session_id,
            message=message,
            model_id=model_id,
            enabled_tools=enabled_tools,
        )

        logger.info(
            "agent stream start model=%s session=%s prompt_chars=%d tools=%s url=%s",
            target,
            session_id,
            len(message),
            "all" if enabled_tools is None else len(enabled_tools),
            url,
        )
        logger.debug("prompt: %s", redact(message))

        started = time.monotonic()
        counts: dict[str, int] = {}
        text_chars = 0
        unknown_seen: set[str] = set()

        try:
            async with aconnect_sse(self._client, "POST", url, json=payload, headers=await self._headers()) as source:
                # aconnect_sse does not raise_for_status, and aiter_sse() would
                # fail on an error body's content-type and mask the real cause.
                if source.response.status_code >= 400:
                    await source.response.aread()
                    error = self._map_error(source.response, target)
                    logger.warning("agent stream rejected status=%s -> %s", source.response.status_code, type(error).__name__)
                    raise error

                async for sse in source.aiter_sse():
                    event = parse_agent_event(sse.event, sse.data)
                    name = type(event).__name__
                    counts[name] = counts.get(name, 0) + 1

                    # Per-event tracing is DEBUG: one turn emits hundreds of
                    # deltas and would swamp an INFO log.
                    if isinstance(event, TextDelta):
                        text_chars += len(event.text)
                    elif isinstance(event, ToolUse):
                        logger.info("tool_use name=%s id=%s", event.name, event.tool_use_id)
                    elif isinstance(event, ToolResult):
                        logger.info("tool_result id=%s error=%s", event.tool_use_id, event.is_error)
                    elif isinstance(event, Metadata):
                        # Per LLM call, not per turn — see the dialect module.
                        logger.debug(
                            "call usage input=%s output=%s context_window=%s",
                            event.usage.input_tokens if event.usage else None,
                            event.usage.output_tokens if event.usage else None,
                            event.context_window,
                        )
                    elif isinstance(event, MetadataSummary):
                        logger.info("turn usage summary=%s", event.usage)
                    elif isinstance(event, SessionTitle):
                        logger.info("session title=%r", event.title)
                    elif isinstance(event, QuotaExceeded):
                        logger.warning("quota exceeded: %s", event.message)
                    elif isinstance(event, CitationEvent):
                        logger.debug("citation document=%s file=%s", event.document_id, event.file_name)
                    elif isinstance(event, Artifact):
                        logger.info("artifact id=%s version=%s", event.artifact_id, event.version)
                    elif isinstance(event, ErrorEvent):
                        logger.error("agent stream error event: %s", event.message)
                    elif isinstance(event, UnknownEvent) and event.name not in unknown_seen:
                        # Once per name per turn. A newer server adding events
                        # should be visible without flooding the log — and this
                        # is the signal that found the lifecycle-frame bug.
                        unknown_seen.add(event.name)
                        logger.warning("unknown SSE event from server: %r", event.name)

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
                "agent stream end model=%s elapsed=%.2fs text_chars=%d events=%s",
                target,
                time.monotonic() - started,
                text_chars,
                counts or "{}",
            )

    # -- interrupt -----------------------------------------------------------

    async def interrupt(self, session_id: str) -> bool:
        """Ask the server to stop generating for ``session_id``.

        This is the authoritative carrier of stop intent. Abandoning only the
        local stream leaves the server generating, burning tokens, and holding
        the session lease — which locks the user out of their own conversation
        until it expires.

        Returns whether the server accepted it, and never raises: this runs on
        the cancel path, where the turn is already being torn down and a second
        failure would replace a clean "Stopped" with a traceback. A failure is
        logged and reported as False so a caller may mention it.
        """
        url = self._endpoints.session_interrupt(session_id)
        try:
            response = await self._client.post(url, headers=await self._headers(), timeout=10.0)
        except httpx.HTTPError as exc:
            logger.warning("interrupt failed session=%s: %s", session_id, exc)
            return False

        if response.status_code >= 400:
            logger.warning("interrupt rejected session=%s status=%s", session_id, response.status_code)
            return False

        logger.info("interrupt accepted session=%s", session_id)
        return True
