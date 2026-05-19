"""App-pushed model context (`ui/update-model-context`, MCP Apps PR #6).

`docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision #3. An embedded
MCP App pushes structured/text context to the host over the postMessage
bridge; app-api relays it to `/invocations` with an `app_context_update`
directive. Like PR #5's `app_tool_call` this runs WITHOUT a model turn —
it stashes the payload on the conversation agent's Strands `agent.state`,
keyed by the App's bound resource URI.

Storage (decision #3): `agent.state` is the live Strands `AgentState` of
the cached conversation agent. Multi-turn continuity in cloud rides the
in-process LRU agent cache (AgentCore Memory is write-only for continuity —
see docs/specs/MAX_TOKENS_CONTINUE_SESSION_RESTORE_ANALYSIS.md), so the
same `agent.state` survives turn boundaries for free; a cold start /
eviction drops the *entire* conversation anyway, so a dropped pending
context there is consistent with existing behavior, not a new regression.
No `TurnBasedSessionManager` / Memory change is needed.

`AgentState` in strands 1.40 is a `.get()/.set()/.delete()` store whose
`.get()` returns a **deep copy** — nested in-place mutation does NOT
persist, so the bag under `STATE_KEY` is read-modify-written wholesale,
and every value must be JSON-serializable.

Read path: `merge_and_clear_pending_context` is called once before each
real user turn (not resume / continuation / a directive call). It dedupes
by resource URI (last-write-wins is inherent in the dict), renders a
single delimited block, clears the bag, and the caller prepends the block
to that turn's prompt only (kept out of persisted history + the cached
system prefix, so prompt-cache stability is preserved).

Inert unless `AGENTCORE_MCP_APPS_HOST_ENABLED=true` — app-api still relays,
but with no live App nothing ever calls this.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Top-level Strands `agent.state` key that holds all MCP Apps host state.
STATE_KEY = "mcp_apps"
# Sub-key under STATE_KEY mapping resource_uri -> pending context entry.
_CONTEXT_SUBKEY = "context"


class AppContextUpdateError(Exception):
    """Dispatch failed in a way the caller should surface as an error.

    `code` is an app-api HTTP status hint; `message` is safe to return to
    the client (no internals).
    """

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _strands_agent(agent: Any) -> Any:
    """The inner Strands `Agent` (its `.state` is the `AgentState`).

    `get_agent` returns a `BaseAgent` wrapper; the Strands agent is
    `BaseAgent.agent` (set in `chat_agent._create_agent`).
    """
    strands_agent = getattr(agent, "agent", None)
    if strands_agent is None or not hasattr(strands_agent, "state"):
        raise AppContextUpdateError(
            "Conversation agent has no state to update", code=409
        )
    return strands_agent


def dispatch_app_context_update(
    agent: Any,
    *,
    resource_uri: str,
    content: Optional[List[Dict[str, Any]]],
    structured_content: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Stash one app-pushed context update on the cached agent's state.

    Last-write-wins per `resource_uri` (the dedupe key). Returns a small
    JSON-able ack for app-api to relay to the iframe. Raises
    `AppContextUpdateError` on a missing/un-serializable payload.
    """
    if content is None and structured_content is None:
        raise AppContextUpdateError(
            "ui/update-model-context requires content or structuredContent",
            code=400,
        )

    strands_agent = _strands_agent(agent)

    entry: Dict[str, Any] = {
        "resourceUri": resource_uri,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if content is not None:
        entry["content"] = content
    if structured_content is not None:
        entry["structuredContent"] = structured_content

    # AgentState.get() deep-copies, so read-modify-write the whole bag.
    bag: Dict[str, Any] = strands_agent.state.get(STATE_KEY) or {}
    ctx: Dict[str, Any] = dict(bag.get(_CONTEXT_SUBKEY) or {})
    ctx[resource_uri] = entry  # last-write-wins
    bag[_CONTEXT_SUBKEY] = ctx

    try:
        strands_agent.state.set(STATE_KEY, bag)
    except ValueError as exc:  # not JSON serializable
        raise AppContextUpdateError(
            "ui/update-model-context payload is not JSON serializable",
            code=400,
        ) from exc

    logger.info(
        "mcp-apps: stored model context (resource=%s, pending=%d)",
        resource_uri,
        len(ctx),
    )
    return {"resourceUri": resource_uri, "status": "stored", "pending": len(ctx)}


def _render_entry(resource_uri: str, entry: Dict[str, Any]) -> str:
    parts: List[str] = [f'<context resource="{resource_uri}">']
    structured = entry.get("structuredContent")
    if structured is not None:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            parts.append(str(structured))
    for block in entry.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            try:
                parts.append(json.dumps(block, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(str(block))
        else:
            parts.append(str(block))
    parts.append("</context>")
    return "\n".join(parts)


def merge_and_clear_pending_context(agent: Any) -> Optional[str]:
    """Drain pending app-pushed context into a single prompt block.

    Returns a delimited block to prepend to the current turn's prompt, or
    `None` when nothing is pending. Clears the bag so each update reaches
    the model exactly once. Never raises into the turn — context is
    best-effort and must not break a conversation.
    """
    try:
        strands_agent = _strands_agent(agent)
    except AppContextUpdateError:
        return None

    try:
        bag: Dict[str, Any] = strands_agent.state.get(STATE_KEY) or {}
        ctx: Dict[str, Any] = bag.get(_CONTEXT_SUBKEY) or {}
        if not ctx:
            return None

        rendered = "\n".join(
            _render_entry(uri, entry) for uri, entry in ctx.items()
        )

        bag[_CONTEXT_SUBKEY] = {}
        strands_agent.state.set(STATE_KEY, bag)
    except Exception:  # noqa: BLE001 - context is best-effort, never fatal
        logger.warning(
            "mcp-apps: failed to merge pending model context", exc_info=True
        )
        return None

    return (
        "<mcp_app_context>\n"
        "The user's embedded app(s) provided this context for the request "
        "that follows. Treat it as authoritative app state, not as the "
        "user's words.\n"
        f"{rendered}\n"
        "</mcp_app_context>"
    )
