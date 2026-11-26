"""A scheduled run that paused on the user (docs/specs/answer-from-inbox.md §3.2–3.3).

A run ends ``needs_user`` when its turn paused on a tool approval, a question,
an MCP form or a browser sign-in. The harness reports only the interrupt ids;
the prompts themselves are the session's ``PendingInterrupt`` breadcrumbs.
This module reads those breadcrumbs once and turns them into the inbox row's
payload: ids plus one bounded display line per prompt, never tool input,
question options, form fields or any answer.

Pure apart from :func:`live_pending_prompts`'s input, which the caller reads
(one ``META`` read, ``load_session_meta``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import ValidationError

from apis.shared.sessions.models import PendingInterrupt, derive_awaiting_input

logger = logging.getLogger(__name__)

#: The most prompts one inbox row lists. A pause with more is not answerable
#: from the inbox: every prompt in a pause is answered together.
MAX_INBOX_PROMPTS = 5
#: A tool or server name on the row.
MAX_NAME_CHARS = 64
#: Text the model or a server wrote (a question, a form's message, a sign-in reason).
MAX_HEADLINE_CHARS = 120

#: Breadcrumb kinds a scheduled run can pause on for the user. ``oauth`` has its
#: own status (``oauth_required``) and ``client_tool`` belongs to the terminal.
USER_PROMPT_KINDS = frozenset({"tool_approval", "user_question", "mcp_elicitation", "browser_login"})
#: What the inbox reads as a row's pause: the user's prompts, plus an OAuth consent, which a
#: resume can run into (§3.6) and which only the conversation can answer.
INBOX_PROMPT_KINDS = USER_PROMPT_KINDS | {"oauth"}
#: Kinds the inbox's review dialog can answer. A browser sign-in is that
#: conversation's live view, so its row offers Open conversation only.
ANSWERABLE_KINDS = frozenset({"tool_approval", "user_question", "mcp_elicitation"})

#: The scheduled-runs worker's event for a resume answered from the inbox (§3.5). An event
#: without a ``kind`` is a scheduled run.
INBOX_RESUME_KIND = "inbox_resume"

NeedsUserKind = Literal["agent_needs_approval", "agent_needs_input"]
NEEDS_USER_KINDS: Tuple[NeedsUserKind, ...] = ("agent_needs_approval", "agent_needs_input")


def normalize_expiry(value: Optional[str]) -> Optional[str]:
    """``value`` as ``YYYY-MM-DDTHH:MM:SSZ``, or ``None`` when it does not parse.

    One fixed format, so an inbox filter can compare it to now as a string.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_expiry_format(now: Optional[datetime] = None) -> str:
    """Now, in :func:`normalize_expiry`'s format."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip_line(value: Any, limit: int) -> Optional[str]:
    """One line of at most ``limit`` characters, ending in "…" when cut. ``None`` when empty."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def live_pending_prompts(
    row: Optional[Dict[str, Any]],
    interrupt_ids: Iterable[str],
    now: Optional[datetime] = None,
) -> Tuple[List[PendingInterrupt], Optional[str]]:
    """The run's prompts still waiting on the user, and the pause's expiry.

    Only breadcrumbs whose ids the run reported, of a kind the user answers,
    under an unexpired ``pausedTurn`` (``derive_awaiting_input``'s rule) count.
    Anything else, a pause answered or cleared first included, is ``([], None)``.
    """
    wanted = [i for i in interrupt_ids if i]
    if not row or not wanted:
        return [], None
    by_id: Dict[str, PendingInterrupt] = {}
    for item in row.get("pendingInterrupts") or []:
        try:
            interrupt = PendingInterrupt.model_validate(item)
        except ValidationError:
            logger.warning("Skipping an unreadable pending interrupt on a paused scheduled run")
            continue
        if interrupt.kind in USER_PROMPT_KINDS:
            by_id[interrupt.interrupt_id] = interrupt
    prompts = [by_id[i] for i in dict.fromkeys(wanted) if i in by_id]
    paused = row.get("pausedTurn") or {}
    expires_at = paused.get("expiresAt") if isinstance(paused, dict) else None
    if not prompts or derive_awaiting_input((p.kind for p in prompts), expires_at, now) is None:
        return [], None
    return prompts, normalize_expiry(expires_at)


def _json(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _question_headline(questions: Optional[str]) -> Optional[str]:
    parsed = _json(questions)
    if not isinstance(parsed, list) or not parsed:
        return None
    first = parsed[0] if isinstance(parsed[0], dict) else {}
    text = first.get("question") or first.get("header")
    more = len(parsed) - 1
    suffix = f" (+{more} more)" if more > 0 else ""
    headline = clip_line(text, MAX_HEADLINE_CHARS - len(suffix))
    return f"{headline}{suffix}" if headline else None


def inbox_prompt(interrupt: PendingInterrupt) -> Dict[str, Any]:
    """One prompt as the inbox row lists it: its id, its kind and one display line."""
    entry: Dict[str, Any] = {"interruptId": interrupt.interrupt_id, "kind": interrupt.kind}
    if interrupt.kind == "tool_approval":
        name = clip_line(interrupt.tool_name, MAX_NAME_CHARS)
        if name:
            entry["toolName"] = name
    elif interrupt.kind == "user_question":
        headline = _question_headline(interrupt.questions)
        if headline:
            entry["headline"] = headline
    elif interrupt.kind == "mcp_elicitation":
        form = _json(interrupt.elicitation)
        if isinstance(form, dict):
            server = clip_line(form.get("serverName"), MAX_NAME_CHARS)
            headline = clip_line(form.get("message"), MAX_HEADLINE_CHARS)
            if server:
                entry["serverName"] = server
            if headline:
                entry["headline"] = headline
    elif interrupt.kind == "browser_login":
        headline = clip_line(interrupt.reason, MAX_HEADLINE_CHARS)
        if headline:
            entry["headline"] = headline
    elif interrupt.kind == "oauth":
        # Only a resume writes one (§3.6): the run it continued needs a connection first.
        provider = clip_line(interrupt.provider_id, MAX_NAME_CHARS)
        if provider:
            entry["serverName"] = provider
            entry["headline"] = clip_line(f"Connect {provider} to continue", MAX_HEADLINE_CHARS)
    return entry


def needs_user_kind(prompts: Iterable[PendingInterrupt]) -> NeedsUserKind:
    """``agent_needs_approval`` when every prompt is a tool approval, else ``agent_needs_input``."""
    kinds = {p.kind for p in prompts}
    return "agent_needs_approval" if kinds == {"tool_approval"} else "agent_needs_input"


def needs_user_payload(
    *,
    schedule_id: str,
    run_id: str,
    run_finished_at: str,
    session_id: str,
    label: Optional[str],
    expires_at: Optional[str],
    prompts: List[PendingInterrupt],
) -> Dict[str, Any]:
    """The inbox row's payload (§3.3). ``runFinishedAt`` names the history row a resume updates."""
    listed = prompts[:MAX_INBOX_PROMPTS]
    answerable = len(prompts) <= MAX_INBOX_PROMPTS and all(p.kind in ANSWERABLE_KINDS for p in prompts)
    payload: Dict[str, Any] = {
        "scheduleId": schedule_id,
        "runId": run_id,
        "runFinishedAt": run_finished_at,
        "sessionId": session_id,
        "label": label or "",
        "answerable": answerable,
        "prompts": [inbox_prompt(p) for p in listed],
    }
    if expires_at:
        payload["expiresAt"] = expires_at
    return payload
