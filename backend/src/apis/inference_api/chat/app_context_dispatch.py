"""App-pushed model context (`ui/update-model-context`, MCP Apps PR #6).

`docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision #3. An embedded
MCP App pushes structured/text context to the host over the postMessage
bridge; app-api relays it to `/invocations` with an `app_context_update`
directive. Like PR #5's `app_tool_call` this runs WITHOUT a model turn —
it stashes the payload for the conversation, keyed by the App's bound
resource URI, and the next real turn reads it.

This is the only route by which an App's outcome reaches the model: an
App-initiated `tools/call` is published to the live thread for display
only. GitHub's `issue_write` App depends on it — the model's own call
returns an "awaiting user submission" stub that promises the real result
"will be delivered to your context automatically".

Storage: a process-level store keyed by `(session, user)`
(`_conversation_owner_key`), NOT the agent instance. The pending context
used to live on the Strands `agent.state` of whatever agent the dispatch
got, but the dispatch reads the agent cache with a key that rarely matches
the real turn's (different system prompt, memory context, document and
injected tools), so it built a throwaway agent, stored the context there
and dropped it (dev, 2026-10-18: an issue the App created was reported as
not created on the next turn). One session can be served by more than one
agent, so per-session state must never live on one (CLAUDE.md).

Why in process, not durable: Runtime affinity (`runtime_session_id_for`)
pins a `(user, session)` pair to one microVM, app-api sends this push with
that same header, and the container runs a single uvicorn worker — so the
push and the next turn meet in this process. The read sits on the turn
path before the first token, and a durable store would put a network read
there on every turn of every session for an event most turns never see. A
microVM recycled between the push and the next turn loses the update; that
is the accepted cost.

Read path: `merge_and_clear_pending_context` is called once before each
real user turn (not resume / continuation / a directive call). It renders
every pending update into one delimited block and clears them, and the
caller prepends the block to that turn's user message. It rides in the new
user message — after the cached prefix — so the system prompt, `toolConfig`
and restored history are untouched (prompt-cache contract).

Inert unless `AGENTCORE_MCP_APPS_HOST_ENABLED=true` — app-api still relays,
but with no live App nothing ever calls this.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from apis.inference_api.chat.service import _conversation_owner_key
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

# Bounds on the in-process store. A conversation holds one entry per App
# resource (last-write-wins), so the per-conversation cap only bites on a
# misbehaving App. The TTL is the AgentCore Runtime session's maximum
# lifetime: no later turn can reach this process anyway.
_MAX_CONVERSATIONS = 512
_MAX_RESOURCES_PER_CONVERSATION = 32
_PENDING_TTL_SECONDS = 8 * 60 * 60

_Owner = Tuple[str, str]


class AppContextUpdateError(Exception):
    """Dispatch failed in a way the caller should surface as an error.

    `code` is an app-api HTTP status hint; `message` is safe to return to
    the client (no internals).
    """

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class PendingAppContextStore:
    """Pending App context per conversation, until the next real turn.

    LRU over conversations, last-write-wins per resource URI. Entries are
    stored as JSON round-trips, so a caller mutating its payload afterwards
    never changes what the model is shown.
    """

    def __init__(
        self,
        *,
        max_conversations: int = _MAX_CONVERSATIONS,
        max_resources: int = _MAX_RESOURCES_PER_CONVERSATION,
        ttl_seconds: float = _PENDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_conversations = max_conversations
        self._max_resources = max_resources
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # owner -> (last write, resource_uri -> entry)
        self._pending: "OrderedDict[_Owner, Tuple[float, OrderedDict[str, Dict[str, Any]]]]" = OrderedDict()

    def put(self, owner: _Owner, resource_uri: str, entry: Dict[str, Any]) -> int:
        """Store one update; returns how many resources are now pending."""
        now = self._clock()
        with self._lock:
            held = self._pending.pop(owner, None)
            entries = held[1] if held and not self._expired(held[0], now) else OrderedDict()
            entries[resource_uri] = entry
            while len(entries) > self._max_resources:
                entries.popitem(last=False)
            self._pending[owner] = (now, entries)
            while len(self._pending) > self._max_conversations:
                self._pending.popitem(last=False)
            return len(entries)

    def drain(self, owner: _Owner) -> Optional["OrderedDict[str, Dict[str, Any]]"]:
        """Remove and return this conversation's pending updates, if any."""
        with self._lock:
            held = self._pending.pop(owner, None)
        if held is None or self._expired(held[0], self._clock()):
            return None
        return held[1] or None

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def _expired(self, written_at: float, now: float) -> bool:
        return now - written_at > self._ttl_seconds


_store = PendingAppContextStore()


def get_pending_app_context_store() -> PendingAppContextStore:
    return _store


def dispatch_app_context_update(
    *,
    session_id: str,
    user_id: str,
    resource_uri: str,
    content: Optional[List[Dict[str, Any]]],
    structured_content: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Hold one app-pushed context update for this conversation's next turn.

    Last-write-wins per `resource_uri` (the dedupe key). Returns a small
    JSON-able ack for app-api to relay to the iframe. Raises
    `AppContextUpdateError` on a missing/un-serializable payload.
    """
    if content is None and structured_content is None:
        raise AppContextUpdateError(
            "ui/update-model-context requires content or structuredContent",
            code=400,
        )

    entry: Dict[str, Any] = {
        "resourceUri": resource_uri,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if content is not None:
        entry["content"] = content
    if structured_content is not None:
        entry["structuredContent"] = structured_content

    try:
        entry = json.loads(json.dumps(entry))
    except (TypeError, ValueError) as exc:
        raise AppContextUpdateError(
            "ui/update-model-context payload is not JSON serializable",
            code=400,
        ) from exc

    pending = _store.put(
        _conversation_owner_key(session_id, user_id), resource_uri, entry
    )
    logger.info(
        "mcp-apps: stored model context (resource=%s, pending=%d)",
        scrub_log(resource_uri),
        pending,
    )
    return {"resourceUri": resource_uri, "status": "stored", "pending": pending}


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


def merge_and_clear_pending_context(
    session_id: str, user_id: Optional[str]
) -> Optional[str]:
    """Drain this conversation's pending app-pushed context into one block.

    Returns a delimited block to prepend to the current turn's prompt, or
    `None` when nothing is pending. Clears the updates so each reaches the
    model exactly once. Never raises into the turn — context is best-effort
    and must not break a conversation.
    """
    try:
        ctx = _store.drain(_conversation_owner_key(session_id, user_id))
        if not ctx:
            return None
        rendered = "\n".join(
            _render_entry(uri, entry) for uri, entry in ctx.items()
        )
    except Exception:  # noqa: BLE001 - context is best-effort, never fatal
        logger.warning(
            "mcp-apps: failed to merge pending model context", exc_info=True
        )
        return None

    logger.info("mcp-apps: merged model context (resources=%d)", len(ctx))
    return (
        "<mcp_app_context>\n"
        "The user's embedded app(s) provided this context for the request "
        "that follows. Treat it as authoritative app state, not as the "
        "user's words.\n"
        f"{rendered}\n"
        "</mcp_app_context>"
    )
