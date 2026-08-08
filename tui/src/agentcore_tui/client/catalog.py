"""The JSON half of app-api: catalogues, preferences and conversations.

The two existing client modules speak SSE — ``converse`` and ``agent_stream``,
each paired with its event dialect. This one speaks the ordinary request/response
API behind everything else the web UI does: which models and tools exist, which
of them the user has turned on, and what conversations they have.

Kept as one module rather than one per resource because there is nothing to
separate: every method is a session-authenticated JSON call with the same error
mapping, and splitting them would multiply the boilerplate without isolating
anything. If a resource grows its own wire dialect — as chat did — it earns its
own module then.

Two wire facts this module absorbs so callers never see them:

**Casing is inconsistent across app-api.** `/chat/*`, `/artifacts/*`,
`/auth/api-keys` and `/system/*` are snake_case; the rest is camelCase with
`populate_by_name` aliases. Reading a field under the wrong convention fails
silently — you get a default, not an error — so each parser here names the wire
key explicitly rather than trusting a transform.

**Absent and empty mean different things** for `enabled_tools` and
`enabled_skills`. Absent is "everything my role grants"; `[]` is "nothing". That
distinction lives in the domain types below (``None`` vs ``[]``) and is preserved
all the way to the payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import __version__
from ..errors import (
    ApiError,
    AuthError,
    BadRequestError,
    ConfigError,
    ConnectionFailedError,
    RateLimitedError,
    UpstreamError,
)
from .auth import AuthProvider
from .endpoints import Endpoints

logger = logging.getLogger(__name__)

USER_AGENT = f"agentcore-tui/{__version__}"

#: Conversations per page. The server caps `limit` at 1000; this is a screenful
#: of history at a time so a long list opens promptly.
SESSION_PAGE_SIZE = 50

#: Messages per page when restoring a conversation. Generous because a partial
#: transcript is worse than a slow one — the user is looking at their own words.
MESSAGE_PAGE_SIZE = 200

REQUEST_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Model:
    """One model the signed-in user is allowed to use.

    ``model_id`` and ``provider`` travel **together**, which is why
    :meth:`selection` returns the pair. The server can resolve a missing provider
    from its own registry, so this is about sending what the catalogue said rather
    than about avoiding a failure.
    """

    model_id: str
    provider: str
    name: str
    provider_name: str = ""
    max_input_tokens: int = 0
    max_output_tokens: int | None = None
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    supports_images: bool = False

    @property
    def label(self) -> str:
        """What a picker shows. Falls back to the id when unnamed."""
        return self.name or self.model_id

    @property
    def selection(self) -> tuple[str, str]:
        """The ``(model_id, provider)`` pair a turn must send."""
        return self.model_id, self.provider

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Model:
        modalities = payload.get("inputModalities") or []
        return cls(
            model_id=str(payload.get("modelId", "")),
            provider=str(payload.get("provider", "")),
            name=str(payload.get("modelName", "")),
            provider_name=str(payload.get("providerName", "")),
            max_input_tokens=int(payload.get("maxInputTokens") or 0),
            max_output_tokens=payload.get("maxOutputTokens"),
            input_price_per_million=float(payload.get("inputPricePerMillionTokens") or 0.0),
            output_price_per_million=float(payload.get("outputPricePerMillionTokens") or 0.0),
            supports_images=any(str(m).lower() == "image" for m in modalities),
        )


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool, and whether this user currently has it on.

    ``enabled`` is the server's resolved answer (``isEnabled``), which already
    folds in the role default and any user override — so a picker shows
    ``enabled`` and writes back a full set, rather than trying to reason about
    defaults itself.
    """

    tool_id: str
    name: str
    description: str = ""
    category: str = ""
    protocol: str = ""
    status: str = ""
    enabled: bool = False
    enabled_by_default: bool = False
    requires_oauth_provider: str | None = None

    @property
    def available(self) -> bool:
        """False for a tool the server lists but reports as unhealthy."""
        return self.status.lower() in {"", "active", "healthy", "available", "ok"}

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Tool:
        return cls(
            tool_id=str(payload.get("toolId", "")),
            name=str(payload.get("displayName") or payload.get("toolId", "")),
            description=str(payload.get("description", "")),
            category=str(payload.get("category", "")),
            protocol=str(payload.get("protocol", "")),
            status=str(payload.get("status", "")),
            enabled=bool(payload.get("isEnabled", False)),
            enabled_by_default=bool(payload.get("enabledByDefault", False)),
            requires_oauth_provider=payload.get("requiresOauthProvider"),
        )


@dataclass(frozen=True, slots=True)
class Skill:
    """One skill the user can bring to a turn."""

    skill_id: str
    name: str
    description: str = ""
    category: str | None = None
    enabled: bool = False

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Skill:
        return cls(
            skill_id=str(payload.get("skillId", "")),
            name=str(payload.get("displayName") or payload.get("skillId", "")),
            description=str(payload.get("description", "")),
            category=payload.get("category"),
            enabled=bool(payload.get("isEnabled", False)),
        )


@dataclass(frozen=True, slots=True)
class SystemPromptOption:
    """A "conversation mode" — a named system prompt the user can select.

    Note the snake_case ``prompt_id``: this endpoint does not follow the
    camelCase convention the rest of the catalogue uses.
    """

    prompt_id: str
    name: str
    description: str = ""

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> SystemPromptOption:
        return cls(
            prompt_id=str(payload.get("prompt_id", "")),
            name=str(payload.get("name", "")),
            description=str(payload.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """One row of the conversation list."""

    session_id: str
    title: str
    message_count: int = 0
    last_message_at: str = ""
    created_at: str = ""
    unread: bool = False
    total_cost: float | None = None
    context_tokens: int | None = None
    context_window: int | None = None
    last_turn_continuable: bool = False

    @property
    def context_percent(self) -> int | None:
        """How full the context window was after the last turn."""
        if not self.context_tokens or not self.context_window:
            return None
        return round(100 * self.context_tokens / self.context_window)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ConversationSummary:
        return cls(
            session_id=str(payload.get("sessionId", "")),
            title=str(payload.get("title") or "Untitled"),
            message_count=int(payload.get("messageCount") or 0),
            last_message_at=str(payload.get("lastMessageAt", "")),
            created_at=str(payload.get("createdAt", "")),
            unread=bool(payload.get("unread", False)),
            total_cost=payload.get("totalCost"),
            context_tokens=payload.get("lastContextTokens"),
            context_window=payload.get("contextWindow"),
            last_turn_continuable=bool(payload.get("lastTurnContinuable", False)),
        )


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a cursor-paginated list."""

    items: list[Any] = field(default_factory=list)
    next_token: str | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.next_token)


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """One stored message, flattened for display.

    The wire form is a list of typed content blocks (`text`, `toolUse`,
    `toolResult`, `image`, `document`, `reasoningContent`, and the schema says
    "etc."), so this keeps the prose and counts the rest rather than pretending
    to render every kind. An unknown block type must not lose the message.
    """

    message_id: str
    role: str
    text: str = ""
    reasoning: str = ""
    created_at: str = ""
    tool_blocks: int = 0
    attachment_blocks: int = 0
    other_blocks: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> HistoryMessage:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_blocks = 0
        attachments = 0
        other: list[str] = []

        blocks = payload.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", ""))
            if kind == "text" or "text" in block:
                value = block.get("text")
                if isinstance(value, str):
                    text_parts.append(value)
            elif kind == "reasoningContent" or "reasoningContent" in block:
                value = _reasoning_text(block)
                if value:
                    reasoning_parts.append(value)
            elif kind in {"toolUse", "toolResult"} or "toolUse" in block or "toolResult" in block:
                tool_blocks += 1
            elif kind in {"image", "document"} or "image" in block or "document" in block:
                attachments += 1
            elif kind:
                other.append(kind)

        return cls(
            message_id=str(payload.get("id", "")),
            role=str(payload.get("role", "")),
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            created_at=str(payload.get("createdAt", "")),
            tool_blocks=tool_blocks,
            attachment_blocks=attachments,
            other_blocks=tuple(other),
        )


def _reasoning_text(block: dict[str, Any]) -> str:
    """Pull prose out of a reasoning block, whichever shape it takes."""
    inner = block.get("reasoningContent")
    if isinstance(inner, dict):
        for key in ("text", "reasoningText"):
            value = inner.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                return str(value["text"])
    value = block.get("text")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CatalogClient:
    """Reads and writes the JSON API for one deployment.

    Session-authenticated throughout: every endpoint here is behind
    ``get_current_user_from_session``, so an API key reaches none of it. That is
    the whole reason the web UI has features the API-key path does not.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthProvider,
        endpoints: Endpoints | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ConfigError("No base URL configured", hint="Run `agentcore-tui login --base-url https://your-host/api`.")
        self._auth = auth
        self._endpoints = endpoints or Endpoints(base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)

    async def __aenter__(self) -> CatalogClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def endpoints(self) -> Endpoints:
        return self._endpoints

    # -- catalogues ----------------------------------------------------------

    async def models(self) -> list[Model]:
        """Every model this user's roles allow, newest server truth.

        Replaces the hand-maintained list the client shipped with, which could
        not know what a deployment had enabled.
        """
        payload = await self._get(self._endpoints.models)
        return [Model.from_wire(item) for item in _items(payload, "models")]

    async def tools(self) -> list[Tool]:
        payload = await self._get(self._endpoints.tools)
        return [Tool.from_wire(item) for item in _items(payload, "tools")]

    async def skills(self) -> list[Skill]:
        payload = await self._get(self._endpoints.skills)
        return [Skill.from_wire(item) for item in _items(payload, "skills")]

    async def system_prompts(self) -> list[SystemPromptOption]:
        payload = await self._get(self._endpoints.system_prompts)
        return [SystemPromptOption.from_wire(item) for item in _items(payload, "prompts")]

    # -- preferences ---------------------------------------------------------

    async def save_tool_preferences(self, preferences: dict[str, bool]) -> None:
        """Persist the user's tool choices server-side.

        The body is a **map of tool id to enabled state**, not a list of enabled
        ids — the endpoint records an explicit per-tool decision, which is how a
        user can turn *off* something their role enables by default. Sending only
        the enabled ids would make "off" indistinguishable from "not mentioned".

        Preferences are what make a choice outlive the process; the per-turn
        ``enabled_tools`` field is what makes it apply now. The UI does both.
        """
        await self._put(self._endpoints.tool_preferences, {"preferences": preferences})

    async def save_skill_preferences(self, preferences: dict[str, bool]) -> None:
        """Persist skill choices. Same map-not-list shape as tools."""
        await self._put(self._endpoints.skill_preferences, {"preferences": preferences})

    # -- conversations -------------------------------------------------------

    async def conversations(self, *, limit: int = SESSION_PAGE_SIZE, next_token: str | None = None) -> Page:
        """One page of the user's conversations, newest first."""
        params: dict[str, Any] = {"limit": limit}
        if next_token:
            params["next_token"] = next_token
        payload = await self._get(self._endpoints.sessions, params=params)
        return Page(
            items=[ConversationSummary.from_wire(item) for item in _items(payload, "sessions")],
            next_token=payload.get("nextToken"),
        )

    async def history(self, session_id: str, *, limit: int = MESSAGE_PAGE_SIZE, next_token: str | None = None) -> Page:
        """One page of stored messages for a conversation."""
        params: dict[str, Any] = {"limit": limit}
        if next_token:
            params["next_token"] = next_token
        payload = await self._get(self._endpoints.session_messages(session_id), params=params)
        return Page(
            items=[HistoryMessage.from_wire(item) for item in _items(payload, "messages")],
            next_token=payload.get("nextToken"),
        )

    async def rename(self, session_id: str, title: str) -> None:
        await self._put(self._endpoints.session_metadata(session_id), {"title": title})

    async def delete(self, session_id: str) -> None:
        await self._request("DELETE", self._endpoints.session(session_id))

    async def delete_many(self, session_ids: list[str]) -> None:
        await self._post(self._endpoints.sessions_bulk_delete, {"sessionIds": session_ids})

    async def mark_read(self, session_id: str, *, read: bool = True) -> bool:
        """Best-effort, like the web UI: a failure here must not block anything.

        Returns whether the server accepted it so a caller can decide, but never
        raises — the SPA logs these failures and moves on, and a terminal that
        refused to open a conversation because a read receipt failed would be
        worse than one that quietly disagrees about a bold row.
        """
        url = self._endpoints.session_read(session_id) if read else self._endpoints.session_unread(session_id)
        try:
            await self._post(url, {})
        except (ApiError, ConnectionFailedError) as exc:
            logger.warning("mark_read failed session=%s read=%s: %s", session_id, read, exc)
            return False
        return True

    async def generate_title(self, session_id: str, message: str) -> str | None:
        """Ask the server to name a conversation. None when it declines.

        Costs an extra model call, so callers should do this once per
        conversation — after the first turn, as the web app does. Note the
        snake_case body and the field name ``input``, not ``message``: this
        endpoint follows the `/chat/*` convention rather than the catalogue's.
        """
        try:
            payload = await self._post(self._endpoints.generate_title, {"session_id": session_id, "input": message})
        except (ApiError, ConnectionFailedError) as exc:
            logger.warning("generate_title failed session=%s: %s", session_id, exc)
            return None
        title = payload.get("title")
        return str(title) if isinstance(title, str) and title else None

    # -- transport -----------------------------------------------------------

    async def _headers(self) -> dict[str, str]:
        return {
            **await self._auth.headers(),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    async def _get(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", url, params=params)

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", url, json=body)

    async def _put(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", url, json=body)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, url, params=params, json=json, headers=await self._headers())
        except httpx.HTTPError as exc:
            raise ConnectionFailedError(self._endpoints.base, f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise _map_error(response, method, url)

        # 204 and an empty 200 are both legitimate for the writes here.
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            logger.warning("non-JSON response from %s %s", method, url)
            return {}
        return body if isinstance(body, dict) else {"items": body}


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The list under ``key``, tolerating a bare array response.

    Every catalogue endpoint wraps its list in a named key today, but a bare
    array is a plausible future shape and costs one branch to accept.
    """
    value = payload.get(key)
    if value is None:
        value = payload.get("items")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _map_error(response: httpx.Response, method: str, url: str) -> Exception:
    detail = _detail(response)
    match response.status_code:
        case 401:
            return AuthError(detail or "Your session was rejected")
        case 403:
            return ApiError(
                detail or "You are not permitted to do that",
                status_code=403,
                hint="Your role may not grant access to this feature. Ask an administrator.",
            )
        case 404:
            return ApiError(
                detail or "Not found",
                status_code=404,
                hint="It may have been deleted in the web app, or from another terminal.",
            )
        case 429:
            return RateLimitedError(detail or "Rate limited")
        case 400 | 422:
            return BadRequestError(detail or "The request was rejected as invalid")
        case _:
            logger.warning("unexpected %s from %s %s", response.status_code, method, url)
            return UpstreamError(detail or f"Server returned HTTP {response.status_code}")


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("detail", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
