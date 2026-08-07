"""Transcript widgets for content only the agent stream produces.

``messages.py`` covers what both dialects emit: a user turn, a streaming
assistant turn, an inline notice. This module covers the rest of the agent
stream — tool calls, RAG citations, quota notices, compaction, artifacts, and
the two interrupts that pause a turn until the user acts.

Two constraints shape everything here.

**Every container sets ``height: auto``.** Textual's ``Vertical`` defaults to
``height: 1fr``, so a widget between ``#transcript`` and its text expands to the
viewport instead of sizing to content. The transcript's virtual size then never
exceeds one screen and long answers are clipped and unreachable. That bug
shipped once; see the comment on ``.message`` in ``app.tcss``.

**A terminal is not a browser.** Artifacts and MCP App UI are HTML rendered in
sandboxed iframes. Rather than pretend, these widgets name what was produced and
show the identity needed to find it in the web app.
"""

from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Label, Static

from ..client.agent_events import (
    Artifact,
    CitationEvent,
    OAuthRequired,
    QuotaExceeded,
    QuotaSessionNotice,
    QuotaWarning,
    ToolApprovalRequired,
    ToolCallRecord,
)

#: Tool results are already capped by the dialect layer; this is the further cut
#: for the collapsed transcript view, where the point is to confirm what came
#: back rather than to read all of it.
RESULT_DISPLAY_CHARS = 1200

#: Rendered arguments longer than this collapse to a single summary line. A
#: tool called with a whole document as an argument should not push the answer
#: off screen.
ARGS_DISPLAY_CHARS = 600


def _format_arguments(arguments: dict[str, Any]) -> str:
    """Render tool arguments as compact, readable JSON.

    ``default=str`` because arguments come straight off the wire and may hold
    values ``json`` cannot serialise; a repr beats an exception mid-transcript.
    """
    if not arguments:
        return ""
    try:
        rendered = json.dumps(arguments, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(arguments)
    if len(rendered) > ARGS_DISPLAY_CHARS:
        return f"{rendered[:ARGS_DISPLAY_CHARS]}\n… ({len(rendered):,} characters total)"
    return rendered


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… ({len(text):,} characters total)"


class ToolCall(Vertical):
    """One tool invocation: what was called, with what, and what came back.

    Mounted when the call starts and updated in place, so the transcript shows
    work in progress rather than jumping from silence to a finished answer.
    Arguments and results live in a collapsed pane: the fact that a search ran
    is useful at a glance, its 2KB payload is not.
    """

    def __init__(self, record: ToolCallRecord) -> None:
        super().__init__(classes="message tool-call")
        self._record = record
        self._header: Label | None = None
        self._detail: Collapsible | None = None
        self._args_body: Static | None = None
        self._result_body: Static | None = None

    def compose(self) -> ComposeResult:
        self._header = Label(self._header_text(), classes="tool-call-header")
        yield self._header

        self._args_body = Static(self._args_text(), markup=False, classes="tool-call-args")
        self._result_body = Static(self._result_text(), markup=False, classes="tool-call-result")
        detail = Collapsible(
            self._args_body,
            self._result_body,
            title="Details",
            collapsed=True,
            classes="tool-call-detail",
        )
        self._detail = detail
        yield detail

    # -- rendering -----------------------------------------------------------

    def _status_glyph(self) -> str:
        if self._record.is_error:
            return "✗"
        if self._record.finished:
            return "✓"
        return "…"

    def _header_text(self) -> str:
        name = self._record.name or "tool"
        parts = [f"{self._status_glyph()} {name}"]
        if not self._record.finished and self._record.progress:
            parts.append(f"— {self._record.progress}")
        elif self._record.is_error:
            parts.append("— failed")
        return " ".join(parts)

    def _args_text(self) -> str:
        rendered = _format_arguments(self._record.arguments)
        return f"Arguments:\n{rendered}" if rendered else "Arguments: none"

    def _result_text(self) -> str:
        if self._record.result is None:
            return "Result: still running…"
        if not self._record.result:
            return "Result: empty"
        label = "Error" if self._record.is_error else "Result"
        return f"{label}:\n{_truncate(self._record.result, RESULT_DISPLAY_CHARS)}"

    # -- updates -------------------------------------------------------------

    def refresh_from_record(self) -> None:
        """Re-render after the accumulator folded more events into the record.

        The widget holds the same :class:`ToolCallRecord` the accumulator
        mutates, so callers update the record and call this — no second copy of
        the state to keep in step.
        """
        if self._header is not None:
            self._header.update(self._header_text())
        if self._args_body is not None:
            self._args_body.update(self._args_text())
        if self._result_body is not None:
            self._result_body.update(self._result_text())
        self.set_class(self._record.is_error, "-error")
        self.set_class(self._record.finished, "-finished")


class Citations(Vertical):
    """Knowledge-base sources an answer drew on.

    Grouped into one widget rather than one per citation: several excerpts from
    the same document is the common case, and a stack of near-identical rules
    reads as noise.
    """

    def __init__(self, citations: list[CitationEvent]) -> None:
        super().__init__(classes="message citations")
        self._citations = citations

    def compose(self) -> ComposeResult:
        count = len(self._citations)
        yield Label(f"{count} source{'s' if count != 1 else ''}", classes="message-role")

        seen: set[str] = set()
        for citation in self._citations:
            name = citation.file_name or citation.document_id or "unknown source"
            if name in seen:
                continue
            seen.add(name)
            yield Static(f"• {name}", markup=False, classes="citation-source")

        excerpts = [c for c in self._citations if c.text.strip()]
        if excerpts:
            body = "\n\n".join(f"{c.file_name or c.document_id}:\n{_truncate(c.text.strip(), 400)}" for c in excerpts)
            yield Collapsible(
                Static(body, markup=False),
                title="Excerpts",
                collapsed=True,
                classes="citations-detail",
            )


class QuotaNotice(Vertical):
    """A spending warning, session notice, or hard stop.

    One widget for all three because the user-facing difference is severity and
    wording, both of which the server already supplies in ``message``. The
    numbers are shown underneath so "you are near your limit" is actionable.
    """

    def __init__(self, event: QuotaWarning | QuotaSessionNotice | QuotaExceeded) -> None:
        blocking = isinstance(event, QuotaExceeded)
        classes = "notice quota-notice"
        if blocking:
            classes += " -error"
        super().__init__(classes=classes)
        self._event = event
        self._blocking = blocking

    def compose(self) -> ComposeResult:
        yield Label(self._title(), classes="message-role")
        yield Static(self._event.message or self._title(), markup=False)
        detail = self._detail()
        if detail:
            yield Static(detail, markup=False, classes="notice-hint")

    def _title(self) -> str:
        match self._event:
            case QuotaExceeded():
                return "Quota reached"
            case QuotaSessionNotice():
                return "This conversation is using a large share of your quota"
            case _:
                return "Approaching your quota"

    def _detail(self) -> str:
        event = self._event
        match event:
            case QuotaExceeded(current_usage=used, quota_limit=limit, reset_info=reset):
                parts = [f"${used:,.2f} of ${limit:,.2f} used"]
                if reset:
                    parts.append(f"resets {reset}")
                return " · ".join(parts)
            case QuotaSessionNotice(session_cost=cost, quota_limit=limit, session_percentage_of_limit=share):
                return f"${cost:,.2f} in this conversation — {share:,.0f}% of your ${limit:,.2f} limit"
            case QuotaWarning(current_usage=used, quota_limit=limit, remaining=remaining):
                return f"${used:,.2f} of ${limit:,.2f} used · ${remaining:,.2f} remaining"
            case _:
                return ""


class CompactionNotice(Vertical):
    """Earlier turns were summarised to keep the conversation inside the window.

    Worth surfacing because it explains a real behaviour change: the model can
    no longer quote the exact wording of turns it now only has a summary of.
    """

    def __init__(self, summarized_turns: int) -> None:
        super().__init__(classes="notice compaction-notice")
        self._turns = summarized_turns

    def compose(self) -> ComposeResult:
        turns = self._turns
        plural = "s" if turns != 1 else ""
        yield Static(f"Earlier messages summarized ({turns} turn{plural})", markup=False)
        yield Static(
            "The model keeps a summary of those turns rather than their full text.",
            markup=False,
            classes="notice-hint",
        )


class ArtifactCard(Vertical):
    """An artifact the turn produced.

    Deliberately does not pretend to render it: artifact content is HTML held in
    S3 and shown in a sandboxed iframe. What a terminal can usefully give is the
    title, type, version, and the id needed to find it in the web app.
    """

    def __init__(self, artifact: Artifact) -> None:
        super().__init__(classes="message artifact-card")
        self._artifact = artifact

    def compose(self) -> ComposeResult:
        verb = "Updated" if self._artifact.action == "updated" else "Created"
        yield Label(f"{verb} artifact", classes="message-role")
        yield Static(self._artifact.title or "Untitled", markup=False, classes="artifact-title")

        details = [self._artifact.content_type or "unknown type", f"v{self._artifact.version}"]
        yield Static(" · ".join(details), markup=False, classes="notice-hint")
        yield Static(
            "Open it in the web app to view — artifacts render as HTML.",
            markup=False,
            classes="notice-hint",
        )


class InterruptNotice(Vertical):
    """The turn is paused waiting on the user.

    Both cases are actionable and neither is an error, so they get a distinct
    look from :class:`~agentcore_tui.widgets.messages.Notice`. The consent URL is
    rendered unwrapped on its own line so a terminal's click-to-open works and a
    copy-paste picks up the whole thing.
    """

    def __init__(self, event: OAuthRequired | ToolApprovalRequired) -> None:
        super().__init__(classes="notice interrupt-notice")
        self._event = event

    def compose(self) -> ComposeResult:
        match self._event:
            case OAuthRequired(provider_id=provider, authorization_url=url):
                name = provider or "an external service"
                yield Label("Authorization needed", classes="message-role")
                yield Static(
                    f"A tool needs your permission to use {name}.",
                    markup=False,
                )
                yield Static(url, markup=False, classes="interrupt-url")
                yield Static(
                    "Open that URL, grant access, then send your message again.",
                    markup=False,
                    classes="notice-hint",
                )
            case ToolApprovalRequired(tool_name=tool, message=message):
                yield Label("Approval needed", classes="message-role")
                yield Static(
                    message or f"The agent wants to run `{tool or 'a tool'}`.",
                    markup=False,
                )
                yield Static(
                    "Approving tool calls is not supported in the terminal yet — " "continue this turn in the web app.",
                    markup=False,
                    classes="notice-hint",
                )


def quota_notice_for(event: object) -> QuotaNotice | None:
    """Build a :class:`QuotaNotice` for a quota event, or None for anything else.

    Exists so a caller folding an accumulator's ``quota_notices`` list — typed as
    the event base class — does not need its own isinstance ladder.
    """
    if isinstance(event, (QuotaWarning, QuotaSessionNotice, QuotaExceeded)):
        return QuotaNotice(event)
    return None
