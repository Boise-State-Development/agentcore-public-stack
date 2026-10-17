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

from apis.shared.feature_flags import browser_takeover_fit_enabled

from .cdp_client import CdpError, CdpSession

logger = logging.getLogger(__name__)

STATE_KEY = "browser_tool"
_SESSION_SUBKEY = "session"
_TAKEOVER_SUBKEY = "takeover"

# AgentCore caps session_timeout at 8h; we deliberately stay near the low end.
# An abandoned session bills until its TTL expires, and a browsing task that
# needs more than 15 minutes of wall clock is a task that should be re-scoped.
SESSION_TIMEOUT_SECONDS = int(os.environ.get("BROWSER_SESSION_TIMEOUT_SECONDS", 900))

# Local reap: drop sockets unused for this long, and stop the remote session
# with them. Shorter than the remote TTL so we usually release first.
IDLE_REAP_SECONDS = int(os.environ.get("BROWSER_IDLE_REAP_SECONDS", 600))

# How long a human gets to finish signing in before the takeover is abandoned.
# "Idle" during a takeover is measured in agent tool calls, of which there are
# none by construction, so without this a handed-over session would be pinned
# against the reaper until the remote TTL killed it. Kept comfortably under
# SESSION_TIMEOUT_SECONDS so we release before the service does — an abandoned
# takeover should end in a session we stopped, not one that expired under us.
TAKEOVER_DEADLINE_SECONDS = int(
    os.environ.get("BROWSER_TAKEOVER_DEADLINE_SECONDS", 480)
)

# Slack on the resume side of the deadline. The deadline governs when we stop
# *waiting*; a user who finished at 7:59 whose POST lands at 8:01 should not be
# told their sign-in expired and have the work thrown away. Only past the grace
# window do we insist the takeover lapsed.
TAKEOVER_GRACE_SECONDS = int(os.environ.get("BROWSER_TAKEOVER_GRACE_SECONDS", 60))

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}

# The size the browser is handed to a human at. The agent browses at
# DEFAULT_VIEWPORT, but the docked panel the user signs in through is ~800px
# wide, so a 1280x800 stream drew the login page at about half size: tiny text
# and clicks that land a few pixels off. 768 wide fits that rail at 1:1 and is
# still the width most sites serve their tablet/desktop layout at, rather than
# the phone one. The height is the panel's: the SPA sends the height its rail
# gives the frame on every chat request, and `take_control` uses it (clamped by
# `snap_display_size`). This height is the fallback when no request carried
# one (an older tab, a scheduled run); the rail scales down, never up, when the
# user's window is shorter.
TAKEOVER_VIEWPORT = {"width": 768, "height": 832}

# What the remote display accepts (measured on dev 2026-10-17): DCV's
# `requestDisplayLayout` refuses a width under 640 and snaps both sides DOWN to
# a multiple of 8. The window is sized to the snapped value so the screen the
# viewer pins and the window Chrome draws are the same rectangle.
_DISPLAY_MIN_WIDTH = 640
_DISPLAY_STEP = 8
_TAKEOVER_MIN_HEIGHT = 600
_TAKEOVER_MAX_HEIGHT = 1000


def snap_display_size(width: int, height: int) -> Dict[str, int]:
    """A takeover size the remote display will accept exactly as given."""
    w = max(_DISPLAY_MIN_WIDTH, int(width))
    h = min(_TAKEOVER_MAX_HEIGHT, max(_TAKEOVER_MIN_HEIGHT, int(height)))
    return {
        "width": w - w % _DISPLAY_STEP,
        "height": h - h % _DISPLAY_STEP,
    }


@dataclass
class _LiveSession:
    """A connected browser session owned by this process."""

    session_id: str
    identifier: str
    client: Any  # bedrock_agentcore BrowserClient
    cdp: CdpSession
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Who holds the browser. While "user", the automation stream is DISABLED
    # at the service — the agent provably cannot act, rather than merely
    # agreeing not to. `control_deadline` is monotonic; past it the pin lapses
    # and the session is reapable again.
    control_state: str = "agent"
    control_deadline: float = 0.0
    # The viewport this session was actually STARTED with, not whatever
    # DEFAULT_VIEWPORT says now. DCV's remoteWidth/remoteHeight must match the
    # real one or the stream crops, and a session can outlive a config change
    # or be reconnected to from a container whose default differs.
    viewport: Dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_VIEWPORT)
    )
    # The window size a human was handed, while they hold it. Kept apart from
    # `viewport` on purpose: that is the agent's size, the one the window goes
    # back to on release. None when the window was never refit (switch off,
    # or the resize failed and the handover went ahead at the agent's size).
    takeover_viewport: Optional[Dict[str, int]] = None

    def user_controlled(self, now: Optional[float] = None) -> bool:
        """True while a human holds the browser and the deadline has not passed."""
        if self.control_state != "user":
            return False
        return (now if now is not None else time.monotonic()) < self.control_deadline


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


# Session-level policies are RECOMMENDED-only. The API's `type` enum accepts
# MANAGED, but the service rejects it:
#
#   ValidationException ... Invalid value for parameter 'type'.
#   MANAGED is not supported for session-level policies.
#
# Measured on dev 2026-09-19. Sending MANAGED here does not degrade — it fails
# `StartBrowserSession` outright, so EVERY browser session dies and `browse_web`
# stops working entirely. Do not "try" MANAGED here again.
_SESSION_POLICY_TYPE = "RECOMMENDED"


def _enterprise_policies() -> Optional[list]:
    """The Chromium URL policy to start every browser session with.

    ⚠️ **This is NOT the security control it was designed to be, and must not
    be relied on as one.** See `docs/specs/authenticated-web-assessment.md` D6.

    The intent was a MANAGED policy — mandated and un-overridable — so that a
    human holding the browser during a takeover could not navigate to the LMS
    and have an agent act as them. That requires `CreateBrowser`, because the
    service refuses MANAGED at session level (see `_SESSION_POLICY_TYPE`).

    What a RECOMMENDED policy still buys, honestly stated:

    * It constrains the **agent**, which drives through CDP and never opens
      Chromium's settings, so the blocklist holds for ordinary `browse_web`.
    * It does **not** constrain a **human** in a takeover, who has a fully
      interactive browser and can override a recommended policy.

    So `request_user_login` must stay ungranted until the MANAGED policy is
    applied at `CreateBrowser`. The RBAC grant and `BROWSER_TAKEOVER_ENABLED`
    are what hold that line in the meantime.

    Returns None when unconfigured, which starts the session with no policy at
    all — correct for a local dev box with no bucket.
    """
    location = os.environ.get("BROWSER_POLICY_S3", "").strip()
    if not location:
        return None

    if not location.startswith("s3://"):
        logger.error(
            "browser: BROWSER_POLICY_S3 is not an s3:// URI (%r); "
            "starting sessions with NO url policy",
            location,
        )
        return None

    bucket, _, key = location[len("s3://"):].partition("/")
    if not bucket or not key:
        logger.error(
            "browser: BROWSER_POLICY_S3 names no bucket/key (%r); "
            "starting sessions with NO url policy",
            location,
        )
        return None

    return [
        {
            "type": _SESSION_POLICY_TYPE,
            "location": {"s3": {"bucket": bucket, "prefix": key}},
        }
    ]


async def _start_remote_session() -> Tuple[Any, str, str]:
    """Start an AgentCore browser session. Returns (client, identifier, id)."""
    from bedrock_agentcore.tools.browser_client import BrowserClient

    identifier = _browser_identifier()
    client = BrowserClient(region=_region())
    policies = _enterprise_policies()
    start_kwargs: Dict[str, Any] = {
        "identifier": identifier,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
        "viewport": DEFAULT_VIEWPORT,
    }
    if policies:
        start_kwargs["enterprise_policies"] = policies
    # boto3 is synchronous; keep it off the event loop.
    session_id = await asyncio.to_thread(client.start, **start_kwargs)
    logger.info(
        "browser: started session %s on %s (ttl=%ss, url_policy=%s)",
        session_id, identifier, SESSION_TIMEOUT_SECONDS,
        _SESSION_POLICY_TYPE.lower() if policies else "NONE",
    )
    return client, identifier, session_id


async def _connect(client: Any) -> CdpSession:
    ws_url, headers = await asyncio.to_thread(client.generate_ws_headers)
    return await CdpSession.connect(ws_url, headers)


async def _reap_idle() -> None:
    """Close sockets unused past the idle window, stopping the remote session.

    A session a human is driving is exempt: idleness here is measured in agent
    tool calls, and during a takeover there are none by construction, so the
    plain rule would reap the browser out from under someone mid-login. The
    exemption is bounded by the takeover deadline rather than open-ended — an
    abandoned takeover becomes ordinarily reapable once the deadline lapses,
    which is what stops a walked-away-from session billing to its TTL.

    Note this runs from `acquire`, so the sweep that collects an abandoned
    session is usually some *other* conversation's next browser call in the
    same container. That is enough: the remote TTL is the backstop.
    """
    now = time.monotonic()
    stale = [
        sid for sid, live in _live.items()
        if now - live.last_used > IDLE_REAP_SECONDS and not live.user_controlled(now)
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
            if live is not None and live.user_controlled():
                # A human is driving. The service has DISABLED the automation
                # stream, so our socket is dead *by design* — reconnecting
                # would fail and then start a second browser, orphaning the
                # one the user is signing into. Hand the pinned session back
                # and let the caller's command fail, which is the point.
                return live
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
            session_id=session_id,
            identifier=identifier,
            client=client,
            cdp=cdp,
            viewport=dict(DEFAULT_VIEWPORT),
        )
        _live[session_id] = live
        _write_state(
            agent,
            {
                "sessionId": session_id,
                "identifier": identifier,
                "startedAt": time.time(),
                # Persisted so a reconnect in another container reports the
                # viewport the browser is really running at.
                "viewport": dict(DEFAULT_VIEWPORT),
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
    viewport = entry.get("viewport")
    live = _LiveSession(
        session_id=session_id,
        identifier=identifier,
        client=client,
        cdp=cdp,
        viewport=dict(viewport) if isinstance(viewport, dict) else dict(DEFAULT_VIEWPORT),
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


# ---------------------------------------------------------------------------
# Human takeover
#
# `take_control` / `release_control` are thin wrappers over
# `UpdateBrowserStream`, which flips the automation stream DISABLED/ENABLED.
# That is a **service-side mutex**, not a convention in this code: while the
# stream is DISABLED the agent's CDP commands are refused by AWS, so "the agent
# cannot act while the human drives" is enforced somewhere we do not control.
#
# There is deliberately no URL-minting function here. The live-view URL is
# signed with SigV4 query auth and expires in at most 300 seconds, so it is
# minted per request by app-api and never travels through the agent (spec D2,
# and PR #1101's rule about presigned URLs in tool results). The `live_view`
# action that used to return one from here is gone.
# ---------------------------------------------------------------------------


def _describe(live: _LiveSession) -> Dict[str, Any]:
    """The identifiers a takeover needs, and nothing more.

    `viewport` is the size the stream is really drawn at right now: the
    takeover size from the refit until the handback restores it, else the
    agent's. The
    viewer pins DCV's display to it, so it must be the window's actual size.
    """
    return {
        "browserSessionId": live.session_id,
        "browserId": live.identifier,
        "viewport": dict(live.takeover_viewport or live.viewport),
        "controlState": live.control_state,
    }


async def _fit_for_takeover(
    live: _LiveSession, frame_height: Optional[int] = None
) -> Optional[Dict[str, int]]:
    """Resize the window to the sign-in size. Returns it, or None if not.

    ``frame_height`` is the height the user's panel gives the stream, from the
    chat request; without one the window gets ``TAKEOVER_VIEWPORT``'s height.
    Either way ``snap_display_size`` clamps it to 600-1000 and the display's
    step, so an untrusted value can only pick a size inside that range.

    Must run BEFORE `take_control`: disabling the automation stream makes the
    service refuse CDP outright (HTTP 403 on the socket), and Chrome will not
    follow a screen resize on its own. So this is the only moment the window
    can be fitted to the panel; nothing can re-fit it during the takeover
    without re-enabling the agent, which is the property the takeover exists
    to guarantee.

    Best-effort: a failure hands the browser over at the agent's size, which
    is exactly the pre-fit behaviour (scaled down in the panel, but working).
    """
    if not browser_takeover_fit_enabled():
        return None
    size = snap_display_size(
        TAKEOVER_VIEWPORT["width"],
        frame_height if frame_height is not None else TAKEOVER_VIEWPORT["height"],
    )
    try:
        await live.cdp.set_window_size(size["width"], size["height"])
    except Exception:  # noqa: BLE001 - the handover still works unfitted
        logger.warning(
            "browser: could not fit session %s for takeover; handing over at %sx%s",
            live.session_id, live.viewport["width"], live.viewport["height"],
            exc_info=True,
        )
        return None
    return size


async def _restore_agent_window(live: _LiveSession) -> None:
    """Put the window back to the agent's size after a handback.

    Needs a live socket, and the one we had died when the stream was disabled,
    so this reconnects to the same remote browser and leaves the new socket on
    the pool entry: the agent's next `browse_web` would have reconnected
    anyway, so the reconnect is not extra work, only earlier.

    Best-effort and logged. The resume must not fail on it: the user has
    signed in, and an agent browsing at 768 wide is a worse page, not a broken
    one. The agent's screenshots follow the window, not the screen, so the
    display size DCV left behind does not matter here.
    """
    try:
        if live.cdp.closed:
            live.cdp = await _connect(live.client)
        await live.cdp.set_window_size(live.viewport["width"], live.viewport["height"])
    except Exception:  # noqa: BLE001
        logger.warning(
            "browser: could not restore the agent window on %s to %sx%s",
            live.session_id, live.viewport["width"], live.viewport["height"],
            exc_info=True,
        )
        return
    logger.info(
        "browser: restored the agent window on %s to %sx%s",
        live.session_id, live.viewport["width"], live.viewport["height"],
    )


async def take_control(
    agent: Any,
    deadline_seconds: int = TAKEOVER_DEADLINE_SECONDS,
    frame_height: Optional[int] = None,
) -> Dict[str, Any]:
    """Hand this conversation's browser to the user. Idempotent.

    Idempotence is not a nicety here: Strands re-executes an interrupted tool
    from the top when the turn resumes (see ``strands/types/interrupt.py``), so
    ``request_user_login`` calls this a second time on the way back in. Taking
    control of an already-user-controlled session must therefore be a cheap
    no-op rather than a second ``UpdateBrowserStream`` call — and must not
    silently extend the deadline, which would make an abandoned takeover
    unreapable for as long as resumes kept arriving.

    ``frame_height`` sizes the handover to the user's panel (see
    ``_fit_for_takeover``). It is ignored on the idempotent repeat: the window
    was fitted once, and the stream is already pinned to that size.

    Returns the session descriptor (ids + viewport + deadline). Raises if there
    is no session to hand over, or if the service refuses the stream update —
    the caller turns that into a tool error rather than pausing the turn.
    """
    live = await acquire(agent)
    now = time.monotonic()

    if live.user_controlled(now):
        return {**_describe(live), "deadlineAt": _deadline_iso(live, now)}

    # Order matters: fit while CDP still answers, then disable it.
    fitted = await _fit_for_takeover(live, frame_height)
    try:
        await asyncio.to_thread(live.client.take_control)
    except Exception:
        # The agent keeps the browser, so it must keep its own window size.
        if fitted is not None:
            await _restore_agent_window(live)
        raise
    live.takeover_viewport = fitted
    live.control_state = "user"
    live.control_deadline = now + max(deadline_seconds, 1)
    live.last_used = now
    logger.info(
        "browser: handed session %s to the user (deadline=%ss)",
        live.session_id, deadline_seconds,
    )
    return {**_describe(live), "deadlineAt": _deadline_iso(live, now)}


async def release_control(agent: Any) -> bool:
    """Take the browser back from the user. Idempotent; True if it flipped.

    Best-effort by design: if the remote call fails the local pin is dropped
    anyway, because leaving a session pinned against the reaper on the strength
    of a failed API call is how an abandoned browser bills to its TTL.
    """
    entry = _read_state(agent)
    if not entry:
        return False
    session_id = entry.get("sessionId")
    identifier = entry.get("identifier")
    if not session_id or not identifier:
        return False

    live = _live.get(session_id)
    # Whether the window was refit for the human and so needs putting back.
    # Known here when this process did the handover; a handback in another
    # container cannot tell, so it restores whenever fitting is on — setting
    # the agent's own size on a window already at it is a no-op.
    refit = browser_takeover_fit_enabled()
    if live is not None:
        if live.control_state != "user":
            return False
        refit = live.takeover_viewport is not None
        live.control_state = "agent"
        live.control_deadline = 0.0
        live.takeover_viewport = None
        live.last_used = time.monotonic()
        client = live.client
    else:
        # The resume landed in a different container from the takeover, or this
        # one restarted. The stream is still DISABLED at the service, and
        # nothing local knows it — so re-enable it by id rather than returning
        # early. Skipping this would leave the agent permanently unable to
        # drive a browser it still holds state for.
        from bedrock_agentcore.tools.browser_client import BrowserClient

        client = BrowserClient(region=_region())
        client.identifier = identifier
        client.session_id = session_id

    try:
        await asyncio.to_thread(client.release_control)
    except Exception:  # noqa: BLE001
        logger.warning(
            "browser: release_control failed for %s; pin dropped locally anyway",
            session_id, exc_info=True,
        )
        await _close_socket(session_id)
        return False
    logger.info("browser: took session %s back from the user", session_id)
    # Re-enabling the stream does not revive the socket that disabling it
    # killed: the service closed our CDP connection when the human took over,
    # and this one is a corpse. Drop it so the next `acquire` reconnects to
    # the *same* remote browser — which is the whole point, since that browser
    # is where the user's login now lives. The remote session is untouched.
    await _close_socket(session_id)
    if refit:
        # CDP answers again now that the stream is re-enabled, so this is the
        # first moment the window can go back to the agent's size.
        if live is None:
            async with _pool_lock:
                live = _live.get(session_id) or await _try_reconnect(entry)
        if live is not None:
            await _restore_agent_window(live)
    return True


async def _close_socket(session_id: str) -> None:
    """Close this session's CDP socket, leaving the remote browser alive.

    Deliberately not `_teardown`: that stops the remote session too, which
    after a sign-in would throw away the authenticated browser we just got
    back. The socket is disposable; the session is not — `acquire` sees a
    closed socket and reconnects to the same browser.

    The pool entry stays, and that is not an accident: dropping it would make
    a second `release_control` look like a handback from another container and
    re-issue the remote call, turning an idempotent no-op into an API round
    trip on every re-entry.
    """
    live = _live.get(session_id)
    if live is None:
        return
    try:
        await live.cdp.close()
    except Exception:  # noqa: BLE001 - the socket is already dead
        logger.debug("browser: cdp close failed during control handback", exc_info=True)


def has_session(agent: Any) -> bool:
    """Whether this conversation already has a browser session.

    Distinct from `acquire`, which *starts* one. A takeover on a browser that
    has never navigated anywhere hands the user a blank tab, so the tool checks
    this first: the alternative is billing an AgentCore browser session and
    pausing the turn to achieve nothing.
    """
    entry = _read_state(agent)
    return bool(entry and entry.get("sessionId"))


def read_takeover(agent: Any) -> Optional[Dict[str, Any]]:
    """The in-flight takeover recorded on agent state, if any.

    Strands re-executes an interrupted tool from the top on resume, so the tool
    needs to tell "first pass" from "coming back" without re-running its side
    effects. This marker is that signal, and it rides `agent.state` rather than
    a process-local dict on purpose: the resume can land in a different
    container from the pause, and a marker that did not survive that would make
    the resume look like a fresh takeover and re-take a browser the user had
    already handed back.
    """
    state = getattr(agent, "state", None)
    if state is None:
        return None
    bag = state.get(STATE_KEY) or {}
    entry = bag.get(_TAKEOVER_SUBKEY)
    return entry if isinstance(entry, dict) else None


def write_takeover(agent: Any, entry: Optional[Dict[str, Any]]) -> None:
    """Record (or clear) the in-flight takeover marker. `None` clears."""
    state = getattr(agent, "state", None)
    if state is None:
        return
    bag = dict(state.get(STATE_KEY) or {})
    if entry is None:
        bag.pop(_TAKEOVER_SUBKEY, None)
    else:
        bag[_TAKEOVER_SUBKEY] = entry
    try:
        state.set(STATE_KEY, bag)
    except ValueError:
        logger.warning("browser: takeover marker not serializable; not persisted")


def control_state(agent: Any) -> Optional[Dict[str, Any]]:
    """Who holds this conversation's browser, if this process knows.

    None when there is no session, or when it lives in another container —
    process-local by design, because the thing it guards (the in-process idle
    reaper) is process-local too.
    """
    entry = _read_state(agent)
    if not entry:
        return None
    live = _live.get(entry.get("sessionId", ""))
    if live is None:
        return None
    now = time.monotonic()
    return {
        **_describe(live),
        "userControlled": live.user_controlled(now),
        "deadlineAt": _deadline_iso(live, now),
    }


def _deadline_iso(live: _LiveSession, now: float) -> Optional[str]:
    """The takeover deadline as a wall-clock ISO instant.

    `control_deadline` is monotonic because that is what the reaper compares
    against; the client needs wall clock. Converting at read time rather than
    storing both keeps the one authoritative value monotonic, so a clock change
    cannot un-expire a takeover.
    """
    if live.control_deadline <= 0:
        return None
    from datetime import datetime, timedelta, timezone

    remaining = max(live.control_deadline - now, 0)
    return (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()
