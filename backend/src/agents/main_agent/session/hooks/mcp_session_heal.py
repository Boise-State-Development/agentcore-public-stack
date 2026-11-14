"""Reconnect a shared MCP client whose session died, before the next call on it.

Agents are cached across turns, and the MCP clients under them are shared by
every agent built over the same (user, server) (`ExternalMCPIntegration`). When
such a client's session dies on its own (a dropped connection or transport
error), nothing on the turn path noticed: every model-driven call on it failed
with "the client session is not running" until each agent holding it was
evicted. See `apis.shared.tools.mcp_session`.

``BeforeToolCallEvent``
    If the selected tool is an MCP tool whose client has no live session,
    restart that session (`revive_provider_session`) before the call runs.
    The heal is lazy: it costs a reconnect only on the call that needs one,
    and a healthy call pays one attribute probe. It runs here rather than at
    turn start (`_load_server`'s cached branch, the cache-hit path) so it adds
    nothing before the first token, also catches a session that dies
    mid-turn, and only reconnects servers the model actually calls. A failed
    reconnect is logged and the call goes ahead to fail as it would have —
    the next call tries again.

``AfterToolCallEvent``
    Two kinds of failed call may well have been performed by the server, so
    their result says the outcome is unknown instead of "Tool execution
    failed": a call whose session ended while it was out, and a call the
    server answered with an HTTP 5xx on a session that stayed up. GitHub
    created the issue, then answered 503; mcp 2.x reports that as the second
    kind. A model told only "failed" would retry the write and duplicate it.
    The failed call itself is never re-run here.

Registered after the consent and approval gates, so a call they cancel is not
reconnected for nothing. Nothing reaches the cacheable prefix except that one
rewritten tool result, which is deterministic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from strands.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.tools.mcp import MCPAgentTool

from apis.shared.security.log_sanitize import scrub_log
from apis.shared.tools.mcp_session import (
    SESSION_LOCK,
    connection_lost_message,
    revive_provider_session,
    server_error_outcome_unknown,
    session_is_active,
)

logger = logging.getLogger(__name__)


def _revive(client: Any) -> bool:
    with SESSION_LOCK:
        return revive_provider_session(client)


class MCPSessionHealHook(HookProvider):
    """Restart a dead MCP session before a call on it; flag calls it dropped."""

    def __init__(self) -> None:
        # toolUseId -> the client's session thread when the call began, so a
        # session that died under the call is told apart from one that was
        # already dead, even if another agent has reconnected it since.
        self._session_at_call: dict[str, Any] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    async def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        client = _mcp_client(event.selected_tool)
        if client is None or event.cancel_tool:
            return
        if not session_is_active(client):
            tool_name = event.tool_use.get("name")
            try:
                # `start()` blocks on the server's handshake; keep it off the
                # loop that is streaming this turn.
                if await asyncio.to_thread(_revive, client):
                    logger.info(
                        "reconnected a dead MCP session before calling %s",
                        scrub_log(tool_name),
                    )
            except Exception as exc:  # noqa: BLE001 - the call reports its own failure
                logger.warning(
                    "could not reconnect a dead MCP session before calling %s: %s",
                    scrub_log(tool_name),
                    scrub_log(exc),
                )
                return
        tool_use_id = event.tool_use.get("toolUseId")
        if tool_use_id:
            self._session_at_call[tool_use_id] = getattr(
                client, "_background_thread", None
            )

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_use_id = event.tool_use.get("toolUseId")
        if not tool_use_id or tool_use_id not in self._session_at_call:
            return
        thread = self._session_at_call.pop(tool_use_id)
        client = _mcp_client(event.selected_tool)
        result = event.result
        if (
            client is None
            or thread is None
            or not isinstance(result, dict)
            or result.get("status") != "error"
        ):
            return
        still_up = (
            getattr(client, "_background_thread", None) is thread
            and session_is_active(client)
        )
        tool_name = event.tool_use.get("name") or ""
        if still_up:
            if not server_error_outcome_unknown(result):
                return  # An ordinary tool error on a healthy session.
            logger.warning(
                "MCP server answered %s with a 5xx; outcome unknown",
                scrub_log(tool_name),
            )
        else:
            logger.warning(
                "MCP session ended while %s was out; outcome unknown",
                scrub_log(tool_name),
            )
        event.result = {
            **result,
            "content": [{"text": connection_lost_message(tool_name)}],
        }


def _mcp_client(tool: Any) -> Optional[Any]:
    if isinstance(tool, MCPAgentTool):
        return tool.mcp_client
    return None
