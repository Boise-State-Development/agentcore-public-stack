"""App-initiated `tools/call` dispatch (MCP Apps PR #5).

`docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision #2. An embedded
MCP App calls a server tool over the postMessage bridge; app-api relays it
to `/invocations` with an `app_tool_call` directive. This module runs that
single tool call WITHOUT a model turn:

1. Rebuild the conversation's agent via `get_agent` (the same path resume
   uses) so the MCP client session + transport auth (SigV4, OIDC
   forwarding, the lazy OAuth token provider) are wired exactly as for a
   model-driven tool call.
1a. Resolve the OAuth token this call needs. A model-driven call gets this
   from `OAuthConsentHook`, which fires on `BeforeToolCallEvent` — an event
   this path never raises, because it calls the MCP client directly instead
   of running the agent's tool loop. So the warm-the-cache half of the hook
   is repeated here explicitly (see `_ensure_oauth_token`).
2. Re-check the tool's `_meta.ui.visibility` includes `"app"` — the spec
   MUST, enforced here as the second gate (app-api is the first).
3. Call the tool against the MCP client that surfaced it (recorded in the
   `UIToolCatalog` during the agent's `tools/list`).
4. Publish synthesized `tool_use` / `tool_result` events to the
   per-session broker so the live conversation stream shows the card, and
   return the `CallToolResult` so app-api can hand it back to the iframe.

Inert unless `AGENTCORE_MCP_APPS_HOST_ENABLED=true` (default true since
PR #7) — the catalog is empty when the flag is off, so every call is
rejected as not app-visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from apis.shared.mcp_apps.broker import get_app_tool_event_broker
from apis.shared.oauth.auth_failure import looks_like_auth_failure

from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)


class AppToolCallError(Exception):
    """Dispatch failed in a way the caller should surface as an error.

    `code` is the status app-api should ultimately answer the SPA with, NOT
    the status this container returns — AgentCore Runtime flattens any
    non-2xx into a 424 and drops the body. The route encodes `code` and
    `message` into a 200 response body and app-api restores the status.
    See `apis/shared/mcp_apps/error_envelope.py`.

    `message` is safe to return to the client (no internals).
    """

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _serialize_content(result: Any) -> List[Dict[str, Any]]:
    """Best-effort MCP tool-result content -> JSON-able blocks.

    Strands' `MCPToolResult.content` is a list of MCP content models;
    `model_dump` is the canonical serialization. Falls back to a text
    block so a quirky server response still round-trips.
    """
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        # Strands' MCPToolResult extends ToolResult, a TypedDict — so a result
        # is a plain dict at runtime and `getattr` finds nothing. Without this
        # every app-initiated tools/call returned `content: []`, leaving the
        # App with no data to render. Mirrors `_is_error`'s dict handling.
        content = result.get("content")
    blocks: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if hasattr(item, "model_dump"):
                try:
                    blocks.append(item.model_dump(by_alias=True, exclude_none=True))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(item, dict):
                blocks.append(item)
            else:
                blocks.append({"type": "text", "text": str(item)})
    return blocks


def _is_error(result: Any) -> bool:
    val = getattr(result, "isError", None)
    if val is None:
        val = getattr(result, "is_error", None)
    if val is None and isinstance(result, dict):
        val = result.get("isError") or result.get("is_error")
    return bool(val)


# Guards the start/stop refcount below. Sessions are cheap to hold but must
# not be torn down under a concurrent call, so overlapping app calls against
# the same client share one revived session.
_revive_lock = threading.Lock()
_revived_users: Dict[int, int] = {}


def _session_is_active(client: Any) -> bool:
    """Whether `client` currently has a live MCP session.

    Strands exposes this only as a private predicate; treat an unexpected
    client shape as "active" so we never start a session we cannot own.
    """
    probe = getattr(client, "_is_session_active", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a broken probe must not block the call
        return True


@contextlib.contextmanager
def _active_session(client: Any):
    """Ensure `client` can serve one out-of-band tools/call, then restore it.

    An app-initiated call arrives *between* turns: the agent it belongs to is
    served from cache, and Strands tore that agent's MCP client sessions down
    when the turn that built them ended. The catalog still holds the client
    object, so calling straight through raises
    `MCPClientInitializationError("the client session is not running")` and the
    App sees a 502.

    Reconnect for the duration of the call and leave the client as we found
    it. A session that is already live — the mid-stream case, where the turn
    is still running — is used as-is and never stopped here, because it
    belongs to that turn.
    """
    key = id(client)
    started_here = False

    with _revive_lock:
        if _revived_users.get(key):
            # Another app call already revived it; join that session.
            _revived_users[key] += 1
        elif _session_is_active(client):
            pass  # Live session owned by an in-flight turn — use, don't touch.
        else:
            client.start()
            _revived_users[key] = 1
            started_here = True

    try:
        yield client
    finally:
        if started_here or _revived_users.get(key):
            with _revive_lock:
                remaining = _revived_users.get(key, 0) - 1
                if remaining > 0:
                    _revived_users[key] = remaining
                else:
                    _revived_users.pop(key, None)
                    try:
                        client.stop(None, None, None)
                    except Exception:  # noqa: BLE001 - best-effort teardown
                        logger.warning(
                            "failed to stop a revived MCP client session",
                            exc_info=True,
                        )


def _resolve_client(agent: Any, tool_name: str):
    """The MCP client that surfaced `tool_name`.

    Primary source is the `UIToolCatalog` (recorded when the agent's MCP
    client ran `tools/list` during build). Lazy import keeps the agent
    layer off inference-api's cold-start path when MCP Apps is disabled.
    """
    from agents.main_agent.integrations.mcp_apps import (
        get_ui_tool_catalog,
        is_mcp_apps_host_enabled,
    )

    if not is_mcp_apps_host_enabled():
        return None, None
    catalog = get_ui_tool_catalog()
    ui_metadata = catalog.get(tool_name)
    client = catalog.get_client(tool_name)
    return ui_metadata, client


def _provider_for_client(client: Any) -> Optional[str]:
    """The OAuth provider_id backing `client`, or None when it isn't gated.

    Reads the same `MCPClient -> provider_id` map `OAuthConsentHook` reaches
    through its injected `provider_lookup`. Lazy import for the same reason
    `_resolve_client` uses one.
    """
    from agents.main_agent.integrations.external_mcp_client import (
        get_external_mcp_integration,
    )

    try:
        return get_external_mcp_integration().provider_for_client(client)
    except Exception:  # noqa: BLE001 - a lookup miss must not block the call
        logger.warning(
            "failed to resolve the OAuth provider for an MCP client",
            exc_info=True,
        )
        return None


async def _user_disconnected(user_id: str, provider_id: str) -> bool:
    """Durable "user pressed Disconnect" intent for (user, provider).

    Mirrors `OAuthConsentHook._is_disconnected`. Without it an App frame
    left open on screen keeps working off the cached token for the rest of
    its TTL after the user disconnects the connector.
    """
    from apis.shared.oauth.disconnect_repository import get_disconnect_repository

    try:
        return bool(
            await get_disconnect_repository().is_disconnected(user_id, provider_id)
        )
    except Exception:  # noqa: BLE001 - fail open, same as the hook's lookup
        logger.warning(
            "failed to read disconnect intent for provider=%s",
            scrub_log(provider_id),
            exc_info=True,
        )
        return False


async def _ensure_oauth_token(client: Any, user_id: str) -> Optional[str]:
    """Guarantee an OAuth token is cached before an app-initiated call.

    A model-driven tool call gets this from `OAuthConsentHook._gate`, which
    fires on `BeforeToolCallEvent`. This path calls the MCP client directly,
    so that event never fires and nothing warms `oauth_token_cache` — the
    lazy token provider then resolves to `None` and the request goes out
    with no `Authorization` header at all. Servers that allow an
    unauthenticated `tools/list` (so the tool still registers and the App
    still renders) answer such a call with their own "you aren't connected"
    text, which reads to the user as the App being broken.

    The cache is per-process, so it is cold on any container that has not
    run a model-driven turn for this (user, provider) — the common case
    after a page reload lands the call on a fresh runtime — and it expires
    on its own TTL well before the App frame does.

    Returns the provider_id when the client is OAuth-gated (whether or not
    the cache was already warm), else None. Raises `AppToolCallError` when
    AgentCore Identity says this user genuinely still has to consent.
    """
    provider_id = _provider_for_client(client)
    if not provider_id:
        return None

    from agents.main_agent.integrations import oauth_token_cache
    from apis.shared.oauth.token_resolution import resolve_token_or_consent_url

    force_reauth = await _user_disconnected(user_id, provider_id)
    if not force_reauth and oauth_token_cache.get(user_id, provider_id):
        return provider_id
    if force_reauth:
        oauth_token_cache.clear_user_provider(user_id, provider_id)

    resolved = await resolve_token_or_consent_url(
        provider_id, user_id, force_authentication=force_reauth
    )
    if resolved is None:
        # Couldn't ask AgentCore at all — that is not evidence of a consent
        # gap, so don't tell the user to connect something they may already
        # have connected. Let the call go out; the server's own error is a
        # truer report than a guess.
        logger.warning(
            "could not resolve a %s token for an app-initiated tools/call; "
            "calling unauthenticated",
            scrub_log(provider_id),
        )
        return provider_id

    if resolved["token"]:
        oauth_token_cache.set(user_id, provider_id, resolved["token"])
        return provider_id

    # No token and a consent URL: the user has not authorized this
    # connector. There is no turn to interrupt here, so surface it as an
    # error the App can render.
    #
    # `code` is NOT this response's HTTP status. AgentCore Runtime rewrites
    # any non-2xx from this container into a generic 424 and discards the
    # body, so returning 409 directly reached the SPA as "Received error
    # (409) from runtime. Please check your CloudWatch logs" — verified
    # live on dev 2026-09-08. The route returns 200 + an envelope carrying
    # this code and message, and app-api restores the real status before
    # replying to the SPA. See `apis/shared/mcp_apps/error_envelope.py`.
    #
    # 409, never 401: the SPA's error interceptor treats *any* 401 as an
    # expired BFF session and redirects to login, so answering "connect
    # your account" with a 401 would sign the user out. 409 is already this
    # codebase's "connector needs connecting" status (the file-source
    # browser and the export dialog both branch on it to show Connect).
    # The envelope reader enforces this independently — it will not relay
    # a 401 even if one is asked for here.
    raise AppToolCallError(
        f"Authorization required for '{provider_id}'. Connect the account, "
        "then try again.",
        code=409,
    )


def _invalidate_oauth_token(user_id: str, provider_id: str, tool_name: str) -> None:
    """Drop a token the MCP server just rejected, so the next call re-asks.

    Deliberately does NOT retry the call, unlike the hook's
    `_handle_auth_failure`. An app-initiated call is whatever button the
    user pressed — `complete_task`, `delete_event` — and re-firing a
    mutation off a regex match could apply the side effect twice. Clearing
    is enough: the next press misses the cache, re-resolves from the vault
    (which refreshes transparently), and either succeeds or reports that
    consent is required.
    """
    from agents.main_agent.integrations import oauth_token_cache

    logger.info(
        "app-initiated tools/call for tool=%s looks like a %s auth failure; "
        "clearing the cached token so the next call re-resolves it",
        scrub_log(tool_name),
        scrub_log(provider_id),
    )
    oauth_token_cache.clear_user_provider(user_id, provider_id)


# --- opportunistic UI-resource revalidation ---------------------------------
# The App HTML the SPA re-mounts on reload is whatever `resources/read`
# returned when the tool first ran, replayed verbatim from the `UIRES#` row. So
# a server that ships a new App version — or tightens the CSP its App runs
# under — never reaches conversations that already exist.
#
# Re-reading needs a live MCP client, and the only path to one is a built
# agent, so revalidating on every conversation open would add a full agent
# rebuild (76% of sessions bypass the agent cache) to a page load that runs no
# model turn. Instead we piggyback: an app-initiated tools/call ALREADY built
# the agent and revived the client, so the read is nearly free here. The
# refreshed shell lands on the NEXT load rather than this one — that is the
# trade, and it converges for the Apps people actually use.
_refresh_lock = threading.Lock()
_refreshed_resources: set = set()
_refresh_tasks: set = set()
# One refresh per resource per process; a chatty App must not re-read its own
# shell on every button press.
_MAX_REFRESHED = 512


def _claim_refresh(session_id: str, tool_use_id: str) -> bool:
    """Whether this process should refresh this resource (once only)."""
    key = f"{session_id}#{tool_use_id}"
    with _refresh_lock:
        if key in _refreshed_resources:
            return False
        if len(_refreshed_resources) >= _MAX_REFRESHED:
            _refreshed_resources.clear()
        _refreshed_resources.add(key)
        return True


def _refresh_ui_resource(user_id: str, session_id: str, tool_use_id: str) -> None:
    """Re-read this App's `ui://` resource and overwrite its stored copy.

    Best-effort and silent: this runs after the App already has its answer,
    so nothing here may raise or slow the call down.
    """
    from apis.shared.mcp_apps.ui_resource_store import get_ui_resource_store

    store = get_ui_resource_store()
    provenance = store.get_provenance(user_id=user_id, tool_use_id=tool_use_id)
    if not provenance:
        return
    # The tool that PRODUCED the frame, which is not the tool the App just
    # called — the resourceUri hangs off the producing tool's catalog entry.
    producing_tool = provenance.get("toolName")
    if not producing_tool:
        return

    from agents.main_agent.integrations.mcp_apps import fetch_ui_resource

    _, client = _resolve_client(None, producing_tool)
    if client is None:
        return
    # `fetch_ui_resource` calls `read_resource_sync` straight through, and
    # between turns Strands has already stopped the client's session — the
    # same reason an app-initiated call needs this wrapper.
    with _active_session(client):
        payload = fetch_ui_resource(producing_tool, tool_use_id)
    if not payload or not payload.get("html"):
        return

    store.store(
        user_id=user_id,
        session_id=session_id,
        tool_use_id=tool_use_id,
        resource_uri=payload.get("resourceUri", ""),
        html=payload["html"],
        mime_type=payload.get("mimeType", ""),
        csp=payload.get("csp") or {},
        permissions=payload.get("permissions") or {},
        sandbox_origin=payload.get("sandboxOrigin", ""),
        server_name=payload.get("serverName", ""),
        icon=payload.get("icon", ""),
        tool_name=producing_tool,
        # Preserved: the anchor belongs to the producing turn, and a refresh
        # must not renumber where the frame sits in the thread.
        produced_by_message_index=provenance.get("producedByMessageIndex"),
    )
    logger.info(
        "mcp-apps: revalidated UI resource (session=%s, toolUseId=%s)",
        scrub_log(session_id),
        scrub_log(tool_use_id),
    )


def _schedule_ui_resource_refresh(
    user_id: str, session_id: str, tool_use_id: str
) -> None:
    """Fire the refresh off the response path; never fail the tool call."""
    if not _claim_refresh(session_id, tool_use_id):
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(
                _refresh_ui_resource, user_id, session_id, tool_use_id
            )
        except Exception:  # noqa: BLE001 - revalidation is best-effort
            logger.warning(
                "mcp-apps: UI resource revalidation failed (session=%s)",
                scrub_log(session_id),
                exc_info=True,
            )

    task = asyncio.create_task(_run())
    # Hold a reference: asyncio only weakly references running tasks, so
    # without this the refresh can be garbage-collected mid-flight.
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


async def dispatch_app_tool_call(
    agent: Any,
    *,
    session_id: str,
    user_id: str,
    tool_use_id: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Execute one app-initiated tool call and publish thread events.

    `agent` is the already-built conversation agent (its MCP clients are
    live). Returns ``{"toolUseId", "result": {content, isError}}`` for the
    JSON response app-api relays to the iframe. Raises `AppToolCallError`
    for visibility / unknown-tool / dispatch failures.
    """
    ui_metadata, client = _resolve_client(agent, tool_name)

    # Spec MUST: reject tools/call from apps for tools whose visibility
    # excludes "app". With the host flag off the catalog is empty, so
    # ui_metadata is None and every proxied call is rejected here.
    if ui_metadata is None or not ui_metadata.visible_to_app():
        raise AppToolCallError(
            f"Tool '{tool_name}' is not callable from an MCP App", code=403
        )
    if client is None:
        raise AppToolCallError(
            f"No live MCP client for tool '{tool_name}'", code=409
        )

    # The consent hook can't run for this call (no BeforeToolCallEvent), so
    # resolve the OAuth token here or the request goes out unauthenticated.
    provider_id = await _ensure_oauth_token(client, user_id)

    # Distinct id for the thread card — the originating tool_use_id is the
    # one that rendered the iframe; this proxied call is its own invocation.
    synth_id = f"app-{tool_use_id}-{uuid.uuid4().hex[:8]}"
    args = dict(arguments or {})

    def _invoke() -> Any:
        # `start()` blocks on the handshake, so revive inside the worker
        # thread rather than on the event loop.
        with _active_session(client):
            return client.call_tool_sync(synth_id, tool_name, args)

    try:
        result = await asyncio.to_thread(_invoke)
    except Exception as exc:  # noqa: BLE001 - surfaced to the App as an error
        logger.warning(
            "app tools/call dispatch failed (tool=%s session=%s): %s",
            scrub_log(tool_name),
            scrub_log(session_id),
            scrub_log(exc),
        )
        raise AppToolCallError(
            f"Tool '{tool_name}' failed to execute", code=502
        ) from exc

    content = _serialize_content(result)
    is_error = _is_error(result)
    status = "error" if is_error else "success"

    if provider_id and looks_like_auth_failure(
        {"status": status, "content": content}
    ):
        _invalidate_oauth_token(user_id, provider_id, tool_name)

    # Surface the call in the conversation thread. Best-effort: a missing
    # listener (no active stream) buffers in the broker for the next turn;
    # never blocks returning the result to the App.
    broker = get_app_tool_event_broker()
    broker.publish(
        session_id,
        {
            "type": "tool_use",
            "data": {
                "tool_use": {
                    "name": tool_name,
                    "tool_use_id": synth_id,
                    "input": args,
                    "origin": "mcp_app",
                }
            },
        },
    )
    broker.publish(
        session_id,
        {
            "type": "tool_result",
            "data": {
                "tool_result": {
                    "toolUseId": synth_id,
                    "status": status,
                    "content": content,
                }
            },
        },
    )

    # The agent is built and the client revived right now — the one moment
    # re-reading this App's shell costs almost nothing. See the note above.
    _schedule_ui_resource_refresh(user_id, session_id, tool_use_id)

    return {
        "toolUseId": tool_use_id,
        "result": {"content": content, "isError": is_error},
    }
