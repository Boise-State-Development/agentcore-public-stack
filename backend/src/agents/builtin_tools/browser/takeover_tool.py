"""`request_user_login` — hand the browser to the user so they can sign in.

The agent hits a login wall, calls this, and the turn pauses. The user drives
the real browser through a live view, signs in with their own hands, and hands
it back; the turn resumes with the session authenticated and the agent carries
on browsing. See `docs/specs/authenticated-web-assessment.md`.

Why this is its own tool, not a `browse_web` action
---------------------------------------------------
RBAC granularity in this codebase is exactly one `tool_id`: a role's
`grantedTools` holds catalog keys, those roll up through parents in
`_compute_effective_permissions`, and the result becomes `enabled_tool_ids`
for `ToolFilter`. There is no sub-tool gate. An *action* on `browse_web` would
therefore ship an interactive browser inside our AWS account to everyone who
can browse, students included. As a separate catalog entry it is granted on its
own, and a user without the grant never has it registered — so it never reaches
`toolConfig`, the model cannot offer it, and that user pays zero prefix tokens
for it (spec D1).

Why an interrupt rather than a new endpoint
-------------------------------------------
The turn genuinely must stop: the agent has nothing to do until a human
finishes, and a turn that polls for login completion burns model calls to learn
nothing. `ask_user_question` already proved the pause/snapshot/resume path, and
Strands routes tool-raised and hook-raised interrupts through the same
`_stop_for_interrupts`, so the `PausedTurnSnapshot`, the resume route and the
breadcrumb need no special case (spec D1b).

⚠️ **Strands re-executes an interrupted tool from the top on resume**
(`strands/types/interrupt.py`), so everything above the `interrupt()` call runs
twice. Two separate things keep that safe, and they are not interchangeable:
`session_pool.take_control` is idempotent, which prevents a second
`UpdateBrowserStream` flip; the takeover marker on `agent.state` preserves the
*descriptor* — the original deadline above all — so the resume is judged
against the window the user was actually given rather than a fresh one minted
on the way back in. The marker also keeps a resume that lands in a container
which has lost the session from calling `acquire` and silently starting a
second, unauthenticated browser.

Credentials
-----------
Nothing here accepts a secret, and nothing should be added that does. The
entire point is that the password is typed into a real browser by the person it
belongs to — a credential passed as a tool argument would land in the prompt,
in AgentCore Memory and in the cacheable prefix, and be re-read on every
subsequent turn for the life of the conversation.

Cost
----
The tool spec is a constant in `toolConfig` for granted users (~200 tokens,
paid once per cache write). A complete login round trip adds one short line to
the conversation: the live view is a video stream the model never sees, the
login page and the user's keystrokes never enter the prompt, and no URL travels
anywhere near the model (spec D2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from strands import tool
from strands.types.tools import ToolContext

from apis.shared.browser_takeover import (
    BrowserSessionRef,
    assert_no_url,
    format_outcome,
    frame_height_from,
    parse_outcome,
)
from apis.shared.feature_flags import browser_takeover_enabled

from . import session_pool

logger = logging.getLogger(__name__)

# Interrupt name. Scoped by toolUseId upstream — `ToolContext._interrupt_id`
# builds `v1:tool_call:{toolUseId}:{uuid5(name)}` — so a second takeover later
# in the same turn is a distinct interrupt.
INTERRUPT_NAME = "request_user_login"

MAX_REASON_CHARS = 200


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _success(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "success"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _deadline_lapsed(deadline_at: Optional[str]) -> bool:
    """Whether a recorded deadline is past, grace included.

    Unparseable reads as *not* lapsed: the failure mode of guessing "expired"
    is throwing away a sign-in the user actually completed, which is strictly
    worse than trusting a client that just told us it finished.
    """
    if not deadline_at:
        return False
    try:
        deadline = datetime.fromisoformat(deadline_at)
    except (TypeError, ValueError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    elapsed = (_now() - deadline).total_seconds()
    return elapsed > session_pool.TAKEOVER_GRACE_SECONDS


@tool(context=True)
async def request_user_login(
    tool_context: ToolContext,
    reason: str,
) -> Dict[str, Any]:
    """Hand the browser to the user so they can sign in, then continue.

    Use this when `browse_web` has reached a page that requires authentication
    — a sign-in form, a paywall, an SSO redirect, an institutional subscription
    wall — and the content behind it is what the task needs. The user takes
    control of the same browser you are driving, signs in themselves, and hands
    it back; your next `browse_web` call continues on the authenticated page.

    Never ask the user for a username, password, one-time code or any other
    credential in your reply, and never type one into a page yourself. You
    cannot be given credentials and you do not need them: that is what this
    tool is for.

    Navigate to the page that needs the sign-in *before* calling this, so the
    user arrives where they expect to be rather than on a blank tab.

    Call it once per sign-in. If the user declines or the window lapses, say
    what you could not reach and carry on with what is public — do not ask
    again in the same turn.

    Args:
        reason: One short sentence, shown to the user, naming what you need
            signed into and why. For example: "Sign in to JSTOR so I can check
            whether the search results page is screen-reader accessible."

    Returns:
        Whether the user signed in, declined, or let the window lapse.
    """
    if not browser_takeover_enabled():
        return _error(
            "Handing the browser to the user is disabled in this environment. "
            "Continue with publicly reachable pages and say what you could not "
            "get to."
        )

    agent = getattr(tool_context, "agent", None)
    if agent is None:
        return _error("❌ Browser takeover has no agent context.")

    reason_text = (reason or "").strip()[:MAX_REASON_CHARS]
    if not reason_text:
        return _error(
            "❌ `reason` is required: tell the user what they are signing into "
            "and why, in one sentence."
        )

    tool_use_id = (tool_context.tool_use or {}).get("toolUseId", "")

    # Resume lands here too (Strands re-runs the tool from the top), so a
    # marker for *this* tool_use means control was already handed over on the
    # first pass. Reusing the recorded descriptor rather than re-deriving one
    # is what keeps the original deadline authoritative, and what stops a
    # resume in a container that has lost the session from calling `acquire`
    # and starting a second, unauthenticated browser.
    pending = session_pool.read_takeover(agent)
    resuming = bool(pending) and pending.get("toolUseId") == tool_use_id

    if resuming:
        descriptor = dict(pending.get("ref") or {})
    else:
        if not session_pool.has_session(agent):
            # Handing over a browser that has never navigated anywhere gives
            # the user a blank tab. Erroring costs nothing; the alternative is
            # billing a browser session and pausing the turn to achieve it.
            return _error(
                "❌ There is no browser session yet. Use `browse_web` to "
                "navigate to the page that needs the sign-in first, then call "
                "this so the user arrives where they expect to be."
            )

        # Read the page before handing over: once the automation stream is
        # DISABLED our CDP commands are refused at the service, so this is the
        # last moment we can name the page the user is about to sign into.
        target_url = await _current_url(agent)
        try:
            # The height the user's panel gives the stream, if the chat
            # request carried one: client data on `invocation_state`, never
            # anything the model wrote, so it cannot be steered by a prompt.
            descriptor = await session_pool.take_control(
                agent,
                frame_height=frame_height_from(
                    getattr(tool_context, "invocation_state", None)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason, not a traceback
            logger.error("browser: could not hand over control: %s", exc, exc_info=True)
            return _error(
                f"❌ Could not hand the browser to the user: {exc}. Continue "
                "without the authenticated content."
            )

        descriptor["targetUrl"] = target_url
        session_pool.write_takeover(
            agent,
            {
                "toolUseId": tool_use_id,
                "reason": reason_text,
                "startedAt": _now().isoformat(),
                "ref": descriptor,
            },
        )

    try:
        ref = BrowserSessionRef.model_validate(descriptor)
    except Exception as exc:  # noqa: BLE001
        logger.error("browser: takeover descriptor is unusable: %s", exc)
        await session_pool.release_control(agent)
        session_pool.write_takeover(agent, None)
        return _error(
            "❌ The browser session could not be described for handover. "
            "Continue without the authenticated content."
        )

    payload: Dict[str, Any] = {
        "type": "browser_login_required",
        "toolUseId": tool_use_id,
        "reason": reason_text,
        **ref.model_dump(by_alias=True, exclude_none=True),
    }
    # The rule from PR #1101, enforced rather than remembered: no presigned URL
    # may travel on this interrupt. app-api mints one per request (spec D2).
    assert_no_url(payload)

    logger.info(
        "Pausing for browser sign-in: browser_session=%s tool_use_id=%s",
        ref.browser_session_id,
        tool_use_id,
    )

    # Raises InterruptException on the first pass; returns the client's payload
    # when the turn resumes. Strands only treats a **non-None** response as an
    # answer, so the SPA must always post an object — "Skip" sends
    # `{"skipped": true}`, never null, or the interrupt would re-raise forever.
    response = tool_context.interrupt(name=INTERRUPT_NAME, reason=payload)

    outcome, note = parse_outcome(response)
    if outcome == "completed" and _deadline_lapsed(ref.deadline_at):
        # The sign-in window closed and the session was released and made
        # reapable, so we cannot honestly report an authenticated browser.
        logger.info(
            "browser: takeover for %s reported complete past its deadline",
            ref.browser_session_id,
        )
        outcome = "expired"

    # Always release, whatever the outcome: leaving the automation stream
    # DISABLED would brick every later `browse_web` call in the conversation.
    released = await session_pool.release_control(agent)
    session_pool.write_takeover(agent, None)
    if outcome == "completed" and not released:
        logger.warning(
            "browser: could not re-enable automation for %s after sign-in",
            ref.browser_session_id,
        )

    return _success(format_outcome(outcome, note))


async def _current_url(agent: Any) -> Optional[str]:
    """The page the browser is parked on, for the user's benefit.

    Best-effort and deliberately quiet: this is a label on a prompt, not a
    correctness input, and the CDP read happens after the automation stream is
    already DISABLED, so a refusal here is expected rather than exceptional.
    """
    try:
        live = await session_pool.acquire(agent)
        value = await live.cdp.evaluate("location.href")
    except Exception:  # noqa: BLE001
        logger.debug("browser: could not read current url for takeover", exc_info=True)
        return None
    text = str(value).strip() if value else ""
    return text[:500] or None
