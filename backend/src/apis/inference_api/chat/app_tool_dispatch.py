"""App-initiated `tools/call` dispatch (MCP Apps PR #5).

`docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision #2. An embedded
MCP App calls a server tool over the postMessage bridge; app-api relays it
to `/invocations` with an `app_tool_call` directive. This module runs that
single tool call WITHOUT a model turn:

1. Rebuild the conversation's agent via `get_agent` (the same path resume
   uses) so the MCP client session + auth (OAuth token cache, SigV4,
   consent hook) are wired exactly as for a model-driven tool call.
2. Re-check the tool's `_meta.ui.visibility` includes `"app"` — the spec
   MUST, enforced here as the second gate (app-api is the first).
3. Call the tool against the MCP client that surfaced it (recorded in the
   `UIToolCatalog` during the agent's `tools/list`).
4. Publish synthesized `tool_use` / `tool_result` events to the
   per-session broker so the live conversation stream shows the card, and
   return the `CallToolResult` so app-api can hand it back to the iframe.

Inert unless `AGENTCORE_MCP_APPS_HOST_ENABLED=true` (default false) — the
catalog is empty otherwise, so every call is rejected as not app-visible.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from apis.shared.mcp_apps.broker import get_app_tool_event_broker

logger = logging.getLogger(__name__)


class AppToolCallError(Exception):
    """Dispatch failed in a way the caller should surface as an error.

    `code` is an app-api HTTP status hint; `message` is safe to return to
    the client (no internals).
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

    # Distinct id for the thread card — the originating tool_use_id is the
    # one that rendered the iframe; this proxied call is its own invocation.
    synth_id = f"app-{tool_use_id}-{uuid.uuid4().hex[:8]}"
    args = dict(arguments or {})

    try:
        result = await asyncio.to_thread(
            client.call_tool_sync, synth_id, tool_name, args
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the App as an error
        logger.warning(
            "app tools/call dispatch failed (tool=%s session=%s): %s",
            tool_name,
            session_id,
            exc,
        )
        raise AppToolCallError(
            f"Tool '{tool_name}' failed to execute", code=502
        ) from exc

    content = _serialize_content(result)
    is_error = _is_error(result)
    status = "error" if is_error else "success"

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

    return {
        "toolUseId": tool_use_id,
        "result": {"content": content, "isError": is_error},
    }
