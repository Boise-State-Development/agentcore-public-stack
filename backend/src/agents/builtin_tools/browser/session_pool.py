"""Browser session lifecycle, keyed per conversation.

One AgentCore Browser session is reused across the tool calls of a
conversation: starting a session costs seconds and dollars, and a browsing
task is inherently multi-step.

Where the key lives matters. Per the "one session can be served by more than
one agent" rule in CLAUDE.md, nothing durable may be cached on an agent
instance — so the *identity* of the browser session (its id) is stored on the
Strands `agent.state`, exactly as `app_context_dispatch` stores app context,
while the live socket is a process-local lookup keyed by that id. A second
agent instance for the same conversation reads the same id from state; if the
socket isn't in this process it reconnects to the same remote session rather
than starting a second one.

`AgentState.get()` deep-copies, so the bag is read-modify-written wholesale
and every value is JSON-serializable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .cdp_client import CdpError, CdpSession

logger = logging.getLogger(__name__)

STATE_KEY = "browser_tool"
_SESSION_SUBKEY = "session"

# AgentCore caps session_timeout at 8h; we deliberately stay near the low end.
# An abandoned session bills until its TTL expires, and a browsing task that
# needs more than 15 minutes of wall clock is a task that should be re-scoped.
SESSION_TIMEOUT_SECONDS = int(os.environ.get("BROWSER_SESSION_TIMEOUT_SECONDS", 900))

# Local reap: drop sockets unused for this long, and stop the remote session
# with them. Shorter than the remote TTL so we usually release first.
IDLE_REAP_SECONDS = int(os.environ.get("BROWSER_IDLE_REAP_SECONDS", 600))

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class _LiveSession:
    """A connected browser session owned by this process."""

    session_id: str
    identifier: str
    client: Any  # bedrock_agentcore BrowserClient
    cdp: CdpSession
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_live: Dict[str, _LiveSession] = {}
_pool_lock = asyncio.Lock()


def _browser_identifier() -> str:
    """Our custom browser from PlatformStack, else the AWS-managed one."""
    return os.environ.get("BROWSER_ID") or "aws.browser.v1"


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def _read_state(agent: Any) -> Optional[Dict[str, Any]]:
    state = getattr(agent, "state", None)
    if state is None:
        return None
    bag = state.get(STATE_KEY) or {}
    entry = bag.get(_SESSION_SUBKEY)
    return entry if isinstance(entry, dict) else None


def _write_state(agent: Any, entry: Optional[Dict[str, Any]]) -> None:
    state = getattr(agent, "state", None)
    if state is None:
        return
    bag = dict(state.get(STATE_KEY) or {})
    if entry is None:
        bag.pop(_SESSION_SUBKEY, None)
    else:
        bag[_SESSION_SUBKEY] = entry
    try:
        state.set(STATE_KEY, bag)
    except ValueError:
        logger.warning("browser: session state not serializable; not persisted")


async def _start_remote_session() -> Tuple[Any, str, str]:
    """Start an AgentCore browser session. Returns (client, identifier, id)."""
    from bedrock_agentcore.tools.browser_client import BrowserClient

    identifier = _browser_identifier()
    client = BrowserClient(region=_region())
    # boto3 is synchronous; keep it off the event loop.
    session_id = await asyncio.to_thread(
        client.start,
        identifier=identifier,
        session_timeout_seconds=SESSION_TIMEOUT_SECONDS,
        viewport=DEFAULT_VIEWPORT,
    )
    logger.info(
        "browser: started session %s on %s (ttl=%ss)",
        session_id, identifier, SESSION_TIMEOUT_SECONDS,
    )
    return client, identifier, session_id


async def _connect(client: Any) -> CdpSession:
    ws_url, headers = await asyncio.to_thread(client.generate_ws_headers)
    return await CdpSession.connect(ws_url, headers)


async def _reap_idle() -> None:
    """Close sockets unused past the idle window, stopping the remote session."""
    now = time.monotonic()
    stale = [
        sid for sid, live in _live.items()
        if now - live.last_used > IDLE_REAP_SECONDS
    ]
    for sid in stale:
        live = _live.pop(sid, None)
        if live is None:
            continue
        logger.info("browser: reaping idle session %s", sid)
        await _teardown(live)


async def _teardown(live: _LiveSession) -> None:
    try:
        await live.cdp.close()
    except Exception:  # noqa: BLE001 - teardown is best effort
        logger.debug("browser: cdp close failed", exc_info=True)
    try:
        await asyncio.to_thread(live.client.stop)
    except Exception:  # noqa: BLE001
        logger.debug("browser: remote stop failed", exc_info=True)


async def acquire(agent: Any) -> _LiveSession:
    """Return this conversation's live session, starting or reconnecting it.

    Reconnection matters: the state may name a remote session this process
    has never seen (a second cached agent, or a container that restarted).
    Reconnecting is strictly cheaper than starting a second browser.
    """
    async with _pool_lock:
        await _reap_idle()

        entry = _read_state(agent)
        if entry:
            session_id = entry.get("sessionId")
            live = _live.get(session_id) if session_id else None
            if live is not None and not live.cdp.closed:
                live.last_used = time.monotonic()
                return live
            if session_id and entry.get("identifier"):
                reconnected = await _try_reconnect(entry)
                if reconnected is not None:
                    return reconnected

        client, identifier, session_id = await _start_remote_session()
        try:
            cdp = await _connect(client)
        except Exception:
            await asyncio.to_thread(client.stop)
            raise

        live = _LiveSession(
            session_id=session_id, identifier=identifier, client=client, cdp=cdp
        )
        _live[session_id] = live
        _write_state(
            agent,
            {
                "sessionId": session_id,
                "identifier": identifier,
                "startedAt": time.time(),
            },
        )
        return live


async def _try_reconnect(entry: Dict[str, Any]) -> Optional[_LiveSession]:
    """Re-attach to a remote session named in state. None if it's gone."""
    from bedrock_agentcore.tools.browser_client import BrowserClient

    session_id = entry["sessionId"]
    identifier = entry["identifier"]
    client = BrowserClient(region=_region())
    client.identifier = identifier
    client.session_id = session_id
    try:
        cdp = await _connect(client)
    except Exception as exc:  # noqa: BLE001 - expired/stopped session
        logger.info("browser: cannot reconnect to %s (%s)", session_id, exc)
        return None

    logger.info("browser: reconnected to session %s", session_id)
    live = _LiveSession(
        session_id=session_id, identifier=identifier, client=client, cdp=cdp
    )
    _live[session_id] = live
    return live


async def release(agent: Any) -> bool:
    """Stop this conversation's browser session. Idempotent."""
    entry = _read_state(agent)
    _write_state(agent, None)
    if not entry:
        return False
    session_id = entry.get("sessionId")
    live = _live.pop(session_id, None) if session_id else None
    if live is not None:
        await _teardown(live)
        return True
    if session_id and entry.get("identifier"):
        # Not ours to close locally, but still billing remotely.
        from bedrock_agentcore.tools.browser_client import BrowserClient

        client = BrowserClient(region=_region())
        client.identifier = entry["identifier"]
        client.session_id = session_id
        try:
            await asyncio.to_thread(client.stop)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("browser: remote stop failed", exc_info=True)
    return False


async def live_view_url(agent: Any) -> Optional[str]:
    """Pre-signed live-view URL for the running session, if there is one."""
    entry = _read_state(agent)
    if not entry:
        return None
    live = _live.get(entry.get("sessionId", ""))
    client = live.client if live else None
    if client is None:
        return None
    try:
        return await asyncio.to_thread(client.generate_live_view_url)
    except Exception:  # noqa: BLE001
        logger.debug("browser: live view url failed", exc_info=True)
        return None
