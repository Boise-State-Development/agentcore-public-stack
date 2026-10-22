"""Mint a short-lived Live View URL for a conversation's browser session.

Spec D2 (`docs/specs/authenticated-web-assessment.md`). The agent never sees
this URL and it is never persisted; it is minted here, per request, against the
browser session the conversation's metadata row names (the D4 projection
written in PR 1).

Why a route rather than a value on the SSE event
------------------------------------------------
`generate_live_view_url` signs with `SigV4QueryAuth` — the signature lives in
the query string — and caps at **300 seconds**. So a URL minted when the turn
paused is dead before a human has found their password manager, dead again on
every reload of the thread, and (per PR #1101) re-emitted truncated at the `?`
if it ever passes through a tool result. Minting per request is the only shape
that survives a twenty-minute login.

Why app-api
-----------
The AgentCore Runtime data plane proxies only `/invocations` and `/ping`, so a
route on inference-api would 404 in cloud no matter how well it worked locally
(CLAUDE.md, "Inference API boundary"). app-api also owns the session-metadata
row and the cookie session, which is what makes the ownership check real.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The service's own hard cap (`MAX_LIVE_VIEW_PRESIGNED_URL_TIMEOUT`). Asking
# for more raises rather than clamping, so we clamp here.
MAX_EXPIRY_SECONDS = 300

LIVE_VIEW_EXPIRY_SECONDS = min(
    int(os.environ.get("BROWSER_LIVE_VIEW_EXPIRY_SECONDS", MAX_EXPIRY_SECONDS)),
    MAX_EXPIRY_SECONDS,
)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


class LiveViewUnavailable(Exception):
    """No live view can be minted, with a reason the caller maps to a status.

    ``code`` mirrors the HTTP status the route should return:
    404 when the conversation has no browser session at all, 409 when it names
    one the service can no longer stream.
    """

    def __init__(self, message: str, code: int = 404) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def _viewport(ref: Dict[str, Any]) -> Dict[str, int]:
    """The session's real viewport, falling back to the default.

    Carried through rather than re-declared in the viewer, because DCV's
    `remoteWidth`/`remoteHeight` must match the browser session's viewport or
    the stream crops — and a session can outlive a config change.
    """
    raw = ref.get("viewport")
    if isinstance(raw, dict):
        try:
            return {"width": int(raw["width"]), "height": int(raw["height"])}
        except (KeyError, TypeError, ValueError):
            logger.warning("browser live view: unusable viewport %r; using default", raw)
    return dict(DEFAULT_VIEWPORT)


async def mint_live_view(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Return `{url, expiresAt, viewport, controlState}` for a browser session.

    `ref` is the conversation's `browserSession` projection. Raises
    :class:`LiveViewUnavailable` when it is missing fields or the service
    refuses — an expired or stopped session is the common case and reads as
    409, not as an error worth a 500.
    """
    browser_session_id = ref.get("browserSessionId")
    browser_id = ref.get("browserId")
    if not browser_session_id or not browser_id:
        raise LiveViewUnavailable(
            "This conversation has no browser session to view.", code=404
        )

    from bedrock_agentcore.tools.browser_client import BrowserClient

    client = BrowserClient(region=_region())
    client.identifier = browser_id
    client.session_id = browser_session_id

    requested_at = datetime.now(timezone.utc)
    try:
        # boto3 is synchronous; keep it off the event loop.
        url = await asyncio.to_thread(
            client.generate_live_view_url, LIVE_VIEW_EXPIRY_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - the reason is the useful part
        logger.info(
            "browser live view: could not sign %s (%s)", browser_session_id, exc
        )
        raise LiveViewUnavailable(
            "The browser session is no longer available. It may have ended or "
            "timed out.",
            code=409,
        ) from exc

    if not url:
        raise LiveViewUnavailable(
            "The browser session returned no live view.", code=409
        )

    # Deliberately computed from when we asked, not from when the client reads
    # it: the viewer refreshes against this instant, and a value derived later
    # would drift past the signature's real expiry.
    expires_at = requested_at + timedelta(seconds=LIVE_VIEW_EXPIRY_SECONDS)

    return {
        "url": url,
        "expiresAt": expires_at.isoformat(),
        "viewport": _viewport(ref),
        "controlState": ref.get("controlState") or "agent",
    }


def summarize_for_log(ref: Optional[Dict[str, Any]]) -> str:
    """A log-safe description of a browser session reference.

    Never includes the minted URL: it carries a live SigV4 signature, and a
    signed URL in CloudWatch is a credential in CloudWatch.
    """
    if not ref:
        return "none"
    return f"browser={ref.get('browserId')} session={ref.get('browserSessionId')}"
