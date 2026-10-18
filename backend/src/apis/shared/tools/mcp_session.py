"""Health of a shared Strands MCPClient's session, and bringing a dead one back.

One MCPClient per (user, MCP server) is shared process-wide: by every cached
Agent built over it, and by the App-initiated `tools/call` dispatch. Its
session can die on its own. The MCP SDK's streamable-HTTP transport treats any
non-2xx answer to a POST as fatal for the whole session (mcp 1.30.0,
`_handle_post_request` → `raise_for_status()`); GitHub's MCP endpoint answering
a `tools/call` with 503 ends it (dev, 2026-10-18). Strands' background thread
then exits, but nothing calls `stop()`, so the client keeps the dead thread, a
resolved init future and the stored error:

* every `MCPAgentTool` call fails with "the client session is not running";
* `start()` returns before any handshake (the old init future is resolved);
* `load_tools()` (`_tool_provider_started` still True) hands a new agent the
  old tool list on the dead session.

Nothing here re-runs a call. A call whose session died under it may well have
been performed by the server, so it is reported as an unknown outcome
(`connection_lost_message`) and the next call reconnects.

Both callers sit on either side of the import boundary — the agent's
`MCPSessionHealHook` (`agents/`) and the App dispatch (`inference_api/`) — so
this lives in `apis.shared`. Every reader is duck-typed against Strands'
private fields and treats an unexpected client shape as healthy, so a Strands
change degrades to today's behavior rather than to a broken call.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

# Held across every start, stop and revive of a shared client's session, by
# the App dispatch's refcount and by the agent heal alike, so two callers never
# restart one client at once.
SESSION_LOCK = threading.Lock()


def session_is_active(client: Any) -> bool:
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


def session_crashed(client: Any) -> bool:
    """Whether `client`'s session died on its own and was never stopped."""
    if getattr(client, "_background_thread", None) is None:
        return False
    return not session_is_active(client)


def is_tool_provider(client: Any) -> bool:
    """Whether agents hold `client` as their tool provider.

    Set by `load_tools()` (the agent build's pre-flight) and cleared by
    `stop()`. A provider's session belongs to the agents built over it, so
    nothing that borrows the client may stop it.
    """
    return getattr(client, "_tool_provider_started", False) is True


def reset_crashed_session(client: Any) -> None:
    """Return a crashed client to its stopped state so it can reconnect.

    `stop()` on a dead session joins a finished thread and resets every
    field, then re-raises the error that ended the session; that error is
    already handled by whoever was mid-call when it happened, so it is
    logged here, not raised.

    Caller holds `SESSION_LOCK`.
    """
    if not session_crashed(client):
        return
    try:
        client.stop(None, None, None)
    except Exception as exc:  # noqa: BLE001 - the error that killed the session
        logger.info("reset a crashed MCP client session (%s)", scrub_log(exc))


def revive_provider_session(client: Any) -> bool:
    """Bring a tool provider's dead session back, keeping its bookkeeping.

    Returns True when it started a session, False when one was already live.
    Raises what `start()` raises when the server still cannot be reached.

    `stop()` (inside the reset) clears more than the session: the consumer
    set that Strands' registries use to decide when the last agent is gone,
    the provider flag, and the listed tools. Those describe the agents, not
    the connection, so they are put back once the new session is up:

    * consumers — without them the first cached agent to be collected would
      stop the session under every other agent still holding the client;
    * the provider flag — without it the next agent build's `load_tools()`
      calls `start()` on a running session and fails;
    * the tool list — a new agent gets exactly the `toolConfig` the cached
      ones already have (an exact-prefix cache key) without another
      `tools/list`.

    If `start()` fails they are put back as they were, so the client looks
    as it did before the attempt and the next call tries again.

    Caller holds `SESSION_LOCK`.
    """
    if session_is_active(client):
        return False
    was_provider = getattr(client, "_tool_provider_started", False)
    consumers = set(getattr(client, "_consumers", ()) or ())
    loaded_tools = getattr(client, "_loaded_tools", None)

    reset_crashed_session(client)
    try:
        client.start()
    except Exception:
        _restore(client, was_provider, consumers, loaded_tools)
        raise
    _restore(client, True, consumers, loaded_tools)
    return True


def _restore(
    client: Any, provider: bool, consumers: set, loaded_tools: Any
) -> None:
    client._tool_provider_started = provider
    current = getattr(client, "_consumers", None)
    if isinstance(current, set):
        current.update(consumers)
    if loaded_tools is not None and getattr(client, "_loaded_tools", None) is None:
        client._loaded_tools = loaded_tools


def connection_lost_message(tool_name: str) -> str:
    """What a caller is told when the session ended while its call was out."""
    return (
        f"The connection to the server closed before '{tool_name}' returned, "
        "so it may have completed anyway. Check whether it took effect "
        "before trying again."
    )
