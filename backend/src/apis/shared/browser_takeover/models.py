"""Shapes for the ``request_user_login`` takeover interrupt.

The agent hands the browser to the user, the turn pauses on a Strands
interrupt, the user signs in with their own hands in a live view of the real
browser, and the turn resumes with the session authenticated. Three components
share these shapes:

- ``agents.builtin_tools.browser.takeover_tool`` — takes control and raises the
  interrupt with a :class:`BrowserSessionRef` payload.
- ``agents.main_agent.streaming.stream_coordinator`` — turns the pending
  interrupt into the ``browser_login_required`` SSE event.
- ``apis.shared.sessions`` — persists the same reference on the conversation's
  session-metadata row (spec D4) so app-api can mint a live-view URL without
  reading agent state.

Design notes
------------
* **No URL, anywhere.** ``generate_live_view_url`` signs with SigV4 *query*
  auth and caps at 300 seconds. A URL in a tool result is re-emitted by the
  model truncated at the ``?`` (PR #1101), and a URL on an SSE event is dead
  before a human can react to it. So nothing here carries one: the SPA gets an
  identifier and asks app-api for a fresh URL when it needs one (spec D2).
  :func:`assert_no_url` exists so a test can hold that line.
* **Credentials are never a parameter.** The whole point of takeover is that
  the user types their password into a real browser. Nothing in this contract
  accepts a secret, and nothing should be added that does.
* **The resume payload is tolerant.** Like ``parse_answers`` for
  ``ask_user_question``: an unrecognized shape degrades to "the user didn't
  finish", never to an exception. A paused turn's only route out must not be
  able to fail on a shape mismatch.
* **Cost.** The tool spec is a constant in ``toolConfig`` for granted users;
  the prompt, the live view and the user's keystrokes never enter the
  conversation. Only the one-line outcome below re-enters it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# The outcome the tool reports back to the model.
TakeoverOutcome = Literal["completed", "skipped", "expired", "failed"]

# Free-text note the user may attach when finishing ("couldn't sign in", say).
# Bounded for the same reason every per-turn payload is (CLAUDE.md token tenet).
MAX_NOTE_CHARS = 500

# The reason payload must never carry a URL. Deliberately broad — it matches a
# bare host as well as a scheme — because what we are guarding against is a
# *presigned* URL slipping in, and those are long, opaque and unmistakable.
_URL_PATTERN = re.compile(r"https?://|wss?://", re.IGNORECASE)


class Viewport(BaseModel):
    """Remote browser viewport, in CSS pixels.

    Carried on the event rather than duplicated as a constant in the frontend:
    the DCV viewer's ``remoteWidth``/``remoteHeight`` must match the session's
    viewport or the stream crops, and a second copy of ``1280x800`` in the SPA
    is a copy that will drift (spec D3).
    """

    model_config = ConfigDict(populate_by_name=True)

    width: int = Field(..., description="Viewport width in CSS pixels")
    height: int = Field(..., description="Viewport height in CSS pixels")


class BrowserSessionRef(BaseModel):
    """Identity of the browser session a takeover is happening on.

    This is both the interrupt's payload and the projection written to the
    conversation's session-metadata row (spec D4). It deliberately holds
    identifiers only: app-api resolves them into a short-lived live-view URL
    server-side, per call.
    """

    model_config = ConfigDict(populate_by_name=True)

    browser_session_id: str = Field(
        ...,
        alias="browserSessionId",
        description="AgentCore browser session id",
    )
    browser_id: str = Field(
        ...,
        alias="browserId",
        description="Browser resource identifier the session runs on",
    )
    viewport: Viewport = Field(..., description="Remote viewport the stream renders at")
    control_state: Literal["agent", "user"] = Field(
        default="user",
        alias="controlState",
        description=(
            "Who holds the browser. While 'user' the automation stream is "
            "DISABLED at the service and the idle reaper leaves the session "
            "alone"
        ),
    )
    deadline_at: Optional[str] = Field(
        default=None,
        alias="deadlineAt",
        description=(
            "ISO 8601 instant after which an unfinished takeover is abandoned: "
            "control is released, the session becomes reapable, and the turn "
            "completes with an ordinary error rather than staying pinned"
        ),
    )
    target_url: Optional[str] = Field(
        default=None,
        alias="targetUrl",
        description=(
            "Page the browser is parked on, shown to the user so they know "
            "what they are signing into. A plain navigational URL the agent "
            "already browsed — never a presigned one"
        ),
    )


class BrowserLoginRequiredEvent(BaseModel):
    """SSE event telling the SPA to offer the live view for a paused turn.

    Mirrors ``UserQuestionRequiredEvent``: emitted after ``message_stop`` in the
    same ``done`` block as ``oauth_required`` and ``user_question_required``,
    and answered by POSTing an ``interrupt_responses`` entry keyed by
    ``interruptId``.

    ``sessionId`` is the **conversation** id, as on every other SSE event in
    this codebase; the browser session is ``browserSessionId``. The spec sketch
    wrote a single ambiguous ``sessionId``, which would have collided with that
    convention the moment app-api's live-view route (spec D2) needed the
    conversation id to scope ownership.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = "browser_login_required"
    interrupt_id: str = Field(..., alias="interruptId")
    tool_use_id: str = Field(..., alias="toolUseId")
    session_id: str = Field(..., alias="sessionId", description="Conversation id")
    browser_session_id: str = Field(..., alias="browserSessionId")
    browser_id: str = Field(..., alias="browserId")
    viewport: Viewport
    deadline_at: Optional[str] = Field(default=None, alias="deadlineAt")
    target_url: Optional[str] = Field(default=None, alias="targetUrl")
    reason: Optional[str] = Field(
        default=None,
        description="The agent's one-line explanation of what it needs signed into",
    )
    sandbox_origin: str = Field(
        default="",
        alias="sandboxOrigin",
        description=(
            "Origin the SPA frames the live-view page from — the same "
            "mcp-sandbox origin MCP Apps use, whose CloudFront function locks "
            "`frame-ancestors` to the SPA and composes `connect-src` from the "
            "`?csp=` query. Empty when the sandbox origin is not deployed, in "
            "which case the SPA shows the prompt without a viewer rather than "
            "framing nothing"
        ),
    )

    def to_sse_format(self) -> str:
        payload = self.model_dump(by_alias=True, exclude_none=True)
        return (
            f"event: browser_login_required\n"
            f"data: {json.dumps(payload)}\n\n"
        )


# Fields allowed to hold a URL, and why each is not the thing this guard is for:
#
#   targetUrl     — the page the agent already browsed to, shown so the user
#                   knows what they are signing into. Plain and navigational.
#   sandboxOrigin — a deployment constant (the mcp-sandbox origin). It carries
#                   no credential and is identical for every user of an
#                   environment.
#
# What the guard IS for is a *presigned* live-view URL: SigV4 query-signed,
# short-lived, and a credential in URL form. Add to this set only for a value
# with the same property — constant, public, and not a bearer of authority.
_URL_ALLOWED_KEYS = frozenset(
    {"targetUrl", "target_url", "sandboxOrigin", "sandbox_origin"}
)


def assert_no_url(payload: Any) -> None:
    """Raise if anything in ``payload`` looks like a URL, except the keys in
    :data:`_URL_ALLOWED_KEYS`.

    The rule from PR #1101 is easy to state and easy to regress, because the
    obvious way to make the frontend's job simpler is to put the live-view URL
    on the event. This makes that a test failure rather than a support ticket.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _URL_ALLOWED_KEYS:
                continue
            assert_no_url(value)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            assert_no_url(item)
        return
    if isinstance(payload, str) and _URL_PATTERN.search(payload):
        raise ValueError(
            "A URL reached the browser-takeover payload. Live-view URLs are "
            "minted by app-api per request and must never travel on the "
            "interrupt, the SSE event or the tool result."
        )


def _clean_note(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:MAX_NOTE_CHARS] if text else None


def parse_outcome(response: Any) -> tuple[TakeoverOutcome, Optional[str]]:
    """Map a resume payload onto ``(outcome, note)``.

    Accepted shapes, in the order they are tried:

    * ``{"completed": true}`` — the user signed in and handed the browser back.
    * ``{"skipped": true}`` / ``{"cancelled": true}`` — they declined.
    * ``{"expired": true}`` — the client noticed the deadline lapsed first.
    * ``{"outcome": "completed" | ...}`` — explicit.
    * a bare string naming any of the above.

    Never raises: anything unrecognized reads as ``skipped``, so the turn
    always resumes. ``note`` is the user's optional free-text remark.
    """
    if isinstance(response, str):
        folded = response.strip().casefold()
        if folded in ("completed", "complete", "done", "ok"):
            return "completed", None
        if folded == "expired":
            return "expired", None
        return "skipped", None

    if not isinstance(response, dict):
        return "skipped", None

    note = _clean_note(response.get("note") or response.get("text"))

    explicit = response.get("outcome")
    if isinstance(explicit, str):
        folded = explicit.strip().casefold()
        if folded in ("completed", "skipped", "expired", "failed"):
            return folded, note  # type: ignore[return-value]

    if response.get("completed"):
        return "completed", note
    if response.get("expired"):
        return "expired", note
    return "skipped", note


def format_outcome(outcome: TakeoverOutcome, note: Optional[str] = None) -> str:
    """Render the tool result the model reads.

    One short line. This text re-enters the conversation and is paid for on
    every subsequent turn, so it says what happened and what to do next, and
    nothing else — no page content, no identifiers, and above all no URL.
    """
    lines = {
        "completed": (
            "The user signed in and handed the browser back. The session is "
            "authenticated — continue browsing from the page it is on."
        ),
        "skipped": (
            "The user declined to sign in. Do not ask again; report what you "
            "could not reach and continue with what is publicly available."
        ),
        "expired": (
            "The sign-in window lapsed before the user finished, and the "
            "browser was released. Say so plainly and continue without the "
            "authenticated content; offer to try again if they ask."
        ),
        "failed": (
            "Handing the browser to the user failed. Continue without the "
            "authenticated content and say that sign-in was unavailable."
        ),
    }
    text = lines.get(outcome, lines["skipped"])
    return f"{text} (User note: {note})" if note else text


def encode_ref(ref: BrowserSessionRef) -> str:
    """JSON-encode a session reference for DynamoDB persistence.

    Stored as a string for the same reason ``PendingInterrupt.tool_input`` is:
    DynamoDB coerces floats to ``Decimal`` on the way in and the decoded
    payload is rendered verbatim.
    """
    return json.dumps(ref.model_dump(by_alias=True, exclude_none=True))


def decode_ref(encoded: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort inverse of :func:`encode_ref` for the reload path."""
    if not encoded:
        return None
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None
