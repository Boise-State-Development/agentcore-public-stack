"""Typed representation of the ``POST /chat/stream`` agent SSE event stream.

A **sibling** of :mod:`agentcore_tui.client.events`, not an extension of it. That
module speaks the ``/chat/api-converse`` dialect: eleven events, one LLM call,
no tools. This one speaks the agent dialect, where a single turn may contain
several LLM calls, tool invocations, RAG citations, quota notices, compaction
and artifacts. The event *names* overlap and the payloads do not always agree,
so folding them into one parser would mean a function whose behaviour depends on
which endpoint the caller happened to use.

Sources of truth for the wire shapes, none of which the TUI can import:

* ``frontend/.../stream-parser/stream-parser-types.ts`` — the SPA's interfaces,
  which are the authoritative *client-side* contract.
* ``backend/src/apis/shared/harness/sse.py`` — the server-side reader, and the
  only other non-browser consumer of this stream.
* the SSE table in ``CLAUDE.MD``.

Three things about this stream that are easy to get wrong:

**Passthrough events must be ignored.** The stream interleaves raw Strands
frames (``event``, ``message``, ``result``) with the typed events. They restate
content that the typed events already carry, so handling them duplicates every
assistant response. They are parsed to :class:`IgnoredEvent` deliberately —
routing them to :class:`UnknownEvent` would make an unrelated "log the unknown
events" change start doubling output.

**``metadata`` is per-LLM-call.** A turn that calls two tools emits three
``metadata`` events. Only ``metadata_summary`` is a whole-turn total, so the
accumulator keeps them apart and prefers the summary.

**Assistant text arrives in several messages.** Each tool round trip closes a
message and opens another, so "the answer" is the last non-empty message rather
than the concatenation of everything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..usage import Usage

#: Raw Strands frames the agent runtime passes through alongside the typed
#: events. Everything they carry is already covered by a typed event, so
#: consuming them duplicates assistant output. See the module docstring.
PASSTHROUGH_EVENT_NAMES = frozenset({"event", "message", "result"})

#: How much of a tool result to retain. Terminal transcripts are the wrong place
#: for a 200KB search dump, and the full text is not needed after rendering.
TOOL_RESULT_PREVIEW_CHARS = 2000


class AgentEvent:
    """Base class for a parsed agent-stream event."""


# ---------------------------------------------------------------------------
# Message and content-block lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageStart(AgentEvent):
    """A new assistant message. Several per turn when tools are used."""

    role: str = "assistant"


@dataclass(frozen=True, slots=True)
class ContentBlockStart(AgentEvent):
    index: int
    block_type: str = "text"
    tool_use_id: str = ""
    tool_name: str = ""


@dataclass(frozen=True, slots=True)
class TextDelta(AgentEvent):
    """An incremental chunk of assistant prose."""

    index: int
    text: str


@dataclass(frozen=True, slots=True)
class ToolInputDelta(AgentEvent):
    """A chunk of a tool call's arguments, streamed as partial JSON.

    Kept distinct from :class:`TextDelta` because the SPA infers block type from
    which field is present (``text`` vs ``input``) rather than from ``type``.
    Rendering this as prose would leak raw JSON into the answer.
    """

    index: int
    partial_json: str


@dataclass(frozen=True, slots=True)
class ContentBlockStop(AgentEvent):
    index: int


@dataclass(frozen=True, slots=True)
class MessageStop(AgentEvent):
    stop_reason: str = "end_turn"


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reasoning(AgentEvent):
    """Extended-thinking content.

    The agent dialect sends whole chunks on a ``reasoning`` event rather than
    the api-converse dialect's ``reasoning_start``/``delta``/``stop`` triple.
    """

    text: str


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolUse(AgentEvent):
    """A tool invocation.

    Re-emitted as the model streams its arguments, so ``arguments`` is whatever
    was parseable at the time and the same ``tool_use_id`` will arrive again.
    """

    tool_use_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    partial: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult(AgentEvent):
    """The outcome of a tool invocation."""

    tool_use_id: str
    text: str = ""
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolProgress(AgentEvent):
    """A human-readable progress line from a long-running tool."""

    message: str
    tool_name: str = ""
    tool_use_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolApprovalRequired(AgentEvent):
    """The turn is paused until the user approves a tool call."""

    interrupt_id: str
    tool_use_id: str
    tool_name: str
    message: str = ""
    tool_input: str = ""


@dataclass(frozen=True, slots=True)
class OAuthRequired(AgentEvent):
    """An external MCP tool needs the user to grant consent in a browser."""

    provider_id: str
    authorization_url: str
    interrupt_id: str = ""


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CitationEvent(AgentEvent):
    """A knowledge-base excerpt the answer drew on."""

    document_id: str
    file_name: str
    text: str = ""
    assistant_id: str = ""


# ---------------------------------------------------------------------------
# Usage and accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metadata(AgentEvent):
    """Usage for **one** LLM call. Not a whole-turn total — see the docstring."""

    usage: Usage
    metrics: dict[str, Any] = field(default_factory=dict)
    context_window: int | None = None


@dataclass(frozen=True, slots=True)
class MetadataSummary(AgentEvent):
    """Turn-cumulative totals. Authoritative for what the turn actually cost."""

    usage: Usage
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Compaction(AgentEvent):
    """Older turns were rolled into a summary to fit the context window.

    ``summarized_turns`` is the delta for this event, not a running total.
    """

    summarized_turns: int = 0
    previous_checkpoint: int = 0
    new_checkpoint: int = 0
    input_tokens: int = 0


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuotaWarning(AgentEvent):
    """Spending has crossed a soft threshold. The turn still runs."""

    message: str
    warning_level: str = ""
    current_usage: float = 0.0
    quota_limit: float = 0.0
    percentage_used: float = 0.0
    remaining: float = 0.0


@dataclass(frozen=True, slots=True)
class QuotaSessionNotice(AgentEvent):
    """*This conversation* is a large share of the monthly limit."""

    message: str
    session_id: str = ""
    session_cost: float = 0.0
    quota_limit: float = 0.0
    session_percentage_of_limit: float = 0.0
    threshold_percentage: float = 0.0


@dataclass(frozen=True, slots=True)
class QuotaExceeded(AgentEvent):
    """A hard stop. The turn does not run."""

    message: str
    current_usage: float = 0.0
    quota_limit: float = 0.0
    percentage_used: float = 0.0
    period_type: str = ""
    reset_info: str = ""
    tier_name: str = ""


# ---------------------------------------------------------------------------
# Side channels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Artifact(AgentEvent):
    """An artifact was created or updated.

    The content is never on the wire — it lives in S3 and the SPA frames it in a
    sandboxed iframe. A terminal cannot render that, so the TUI reports its
    existence and identity only.
    """

    artifact_id: str
    title: str
    content_type: str = ""
    version: int = 1
    action: str = "created"
    session_id: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class SessionTitle(AgentEvent):
    """A server-generated title for the conversation, on its first turn.

    May arrive after ``done``; callers must not gate it on turn completion.
    """

    session_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ErrorEvent(AgentEvent):
    """A failure reported by the server. Covers `error` and `stream_error`."""

    message: str
    code: str = ""
    recoverable: bool = False


@dataclass(frozen=True, slots=True)
class Done(AgentEvent):
    """Terminal event — the server has finished, successfully or not."""


@dataclass(frozen=True, slots=True)
class IgnoredEvent(AgentEvent):
    """A frame this client deliberately drops. See ``PASSTHROUGH_EVENT_NAMES``.

    Distinct from :class:`UnknownEvent` so "ignored on purpose" and "we have not
    implemented this" never get conflated. MCP App UI events land here too: they
    describe an HTML iframe, which has no terminal rendering.
    """

    name: str
    reason: str = "passthrough"


@dataclass(frozen=True, slots=True)
class UnknownEvent(AgentEvent):
    """An event name this client does not know.

    Kept rather than raising so a newer server can add events without breaking
    older clients.
    """

    name: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    return float(value) if isinstance(value, (int, float)) else default


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    """First non-empty string among ``keys``.

    The stream carries both camelCase (event formatter) and snake_case (Strands
    passthrough) spellings of the same fields depending on which code path
    emitted the frame, so every id lookup has to accept both.
    """
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_arguments(raw: Any) -> tuple[dict[str, Any], bool]:
    """Coerce a tool ``input`` field into ``(arguments, partial)``.

    Already-decoded objects arrive as dicts. While the model is still streaming
    the call, ``input`` is a *prefix* of a JSON document, which does not parse —
    that is reported as partial rather than as an error, because a later re-emit
    of the same ``tool_use_id`` completes it.
    """
    if isinstance(raw, dict):
        return raw, False
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}, True
        return (decoded, False) if isinstance(decoded, dict) else ({}, False)
    return {}, False


def _tool_result_text(payload: dict[str, Any]) -> str:
    """Extract result text from either of the two wire shapes.

    Flat (``{result}`` / ``{error}``) from the event formatter, or a list of
    content blocks (``{content: [{text}, ...]}``) from the passthrough path.
    """
    for key in ("result", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    content = payload.get("content")
    if isinstance(content, list):
        parts = [_as_str(block.get("text")) for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
        # A JSON-only result still deserves a rendering.
        for block in content:
            if isinstance(block, dict) and "json" in block:
                return json.dumps(block["json"], indent=2, default=str)
    return ""


def _unwrap_tool_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Find the tool-result objects in a ``tool_result`` payload.

    Three nestings occur: ``{tool_result: {...}}`` (SPA's declared shape),
    ``{message: {content: [{toolResult: {...}}]}}`` (Strands message shape), and
    the bare payload itself (flat event-formatter shape).
    """
    inner = payload.get("tool_result")
    if isinstance(inner, dict):
        return [inner]

    message = payload.get("message")
    if isinstance(message, dict):
        found = [
            block["toolResult"] for block in message.get("content") or [] if isinstance(block, dict) and isinstance(block.get("toolResult"), dict)
        ]
        if found:
            return found

    return [payload]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_agent_event(name: str, data: str) -> AgentEvent:
    """Turn a raw SSE ``(event, data)`` pair into an :class:`AgentEvent`.

    Malformed JSON yields an :class:`ErrorEvent` rather than raising, so one bad
    frame degrades the turn instead of tearing down the app.

    A frame with no ``event:`` line carries its name in the payload's ``type``
    field; pass ``name=""`` and this resolves it.
    """
    if not data:
        payload: dict[str, Any] = {}
    else:
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            return ErrorEvent(f"Server sent malformed JSON on `{name or 'unnamed'}` event")
        payload = decoded if isinstance(decoded, dict) else {}

    # Bare `data:` frames name themselves in `type`.
    resolved = name or _as_str(payload.get("type"))

    if resolved in PASSTHROUGH_EVENT_NAMES:
        return IgnoredEvent(name=resolved)

    match resolved:
        case "message_start":
            return MessageStart(role=_as_str(payload.get("role"), "assistant"))

        case "content_block_start":
            tool_use = _as_dict(payload.get("toolUse"))
            declared = _as_str(payload.get("type"), "text")
            # `type` on this event is the *block* type, but a bare frame's
            # `type` is the event name — so when they collide, trust `toolUse`.
            block_type = "tool_use" if tool_use else ("text" if declared == "content_block_start" else declared)
            return ContentBlockStart(
                index=_as_int(payload.get("contentBlockIndex")),
                block_type=block_type,
                tool_use_id=_first_str(tool_use, "toolUseId", "tool_use_id"),
                tool_name=_as_str(tool_use.get("name")),
            )

        case "content_block_delta":
            index = _as_int(payload.get("contentBlockIndex"))
            # Block type is inferred from which field is present, exactly as the
            # SPA does — `type` is unreliable on this event.
            if isinstance(payload.get("input"), str):
                return ToolInputDelta(index=index, partial_json=_as_str(payload.get("input")))
            return TextDelta(index=index, text=_as_str(payload.get("text")))

        case "content_block_stop":
            return ContentBlockStop(index=_as_int(payload.get("contentBlockIndex")))

        case "message_stop":
            return MessageStop(stop_reason=_as_str(payload.get("stopReason"), "end_turn"))

        case "reasoning":
            return Reasoning(text=_first_str(payload, "reasoningText", "text"))

        case "tool_use":
            data_obj = _as_dict(payload.get("tool_use")) or payload
            arguments, partial = _tool_arguments(data_obj.get("input"))
            return ToolUse(
                tool_use_id=_first_str(data_obj, "toolUseId", "tool_use_id"),
                name=_as_str(data_obj.get("name")),
                arguments=arguments,
                partial=partial,
            )

        case "tool_result" | "tool_error":
            results = _unwrap_tool_results(payload)
            first = results[0] if results else {}
            return ToolResult(
                tool_use_id=_first_str(first, "toolUseId", "tool_use_id"),
                text=_tool_result_text(first)[:TOOL_RESULT_PREVIEW_CHARS],
                is_error=resolved == "tool_error" or first.get("status") == "error",
            )

        case "tool_progress":
            return ToolProgress(
                message=_first_str(payload, "message", "status"),
                tool_name=_first_str(payload, "toolName", "tool_name"),
                tool_use_id=_first_str(payload, "toolUseId", "tool_use_id"),
            )

        case "tool_approval_required":
            return ToolApprovalRequired(
                interrupt_id=_first_str(payload, "interruptId", "interrupt_id"),
                tool_use_id=_first_str(payload, "toolUseId", "tool_use_id"),
                tool_name=_first_str(payload, "toolName", "tool_name"),
                message=_as_str(payload.get("message")),
                tool_input=_as_str(payload.get("toolInput")),
            )

        case "oauth_required":
            return OAuthRequired(
                provider_id=_first_str(payload, "providerId", "provider_id"),
                authorization_url=_first_str(payload, "authorizationUrl", "authorization_url"),
                interrupt_id=_first_str(payload, "interruptId", "interrupt_id"),
            )

        case "citation":
            return CitationEvent(
                document_id=_first_str(payload, "documentId", "document_id"),
                file_name=_first_str(payload, "fileName", "file_name"),
                text=_as_str(payload.get("text")),
                assistant_id=_first_str(payload, "assistantId", "assistant_id"),
            )

        case "metadata":
            return Metadata(
                usage=Usage.from_payload(_as_dict(payload.get("usage"))),
                metrics=_as_dict(payload.get("metrics")),
                context_window=(_as_int(payload.get("contextWindow")) if "contextWindow" in payload else None),
            )

        case "metadata_summary":
            return MetadataSummary(
                usage=Usage.from_payload(_as_dict(payload.get("usage"))),
                payload={k: v for k, v in payload.items() if k != "type"},
            )

        case "compaction":
            return Compaction(
                summarized_turns=_as_int(payload.get("summarizedTurns")),
                previous_checkpoint=_as_int(payload.get("previousCheckpoint")),
                new_checkpoint=_as_int(payload.get("newCheckpoint")),
                input_tokens=_as_int(payload.get("inputTokens")),
            )

        case "quota_warning":
            return QuotaWarning(
                message=_as_str(payload.get("message")),
                warning_level=_first_str(payload, "warningLevel", "warning_level"),
                current_usage=_as_float(payload.get("currentUsage")),
                quota_limit=_as_float(payload.get("quotaLimit")),
                percentage_used=_as_float(payload.get("percentageUsed")),
                remaining=_as_float(payload.get("remaining")),
            )

        case "quota_session_notice":
            return QuotaSessionNotice(
                message=_as_str(payload.get("message")),
                session_id=_first_str(payload, "sessionId", "session_id"),
                session_cost=_as_float(payload.get("sessionCost")),
                quota_limit=_as_float(payload.get("quotaLimit")),
                session_percentage_of_limit=_as_float(payload.get("sessionPercentageOfLimit")),
                threshold_percentage=_as_float(payload.get("thresholdPercentage")),
            )

        case "quota_exceeded":
            return QuotaExceeded(
                message=_as_str(payload.get("message")),
                current_usage=_as_float(payload.get("currentUsage")),
                quota_limit=_as_float(payload.get("quotaLimit")),
                percentage_used=_as_float(payload.get("percentageUsed")),
                period_type=_first_str(payload, "periodType", "period_type"),
                reset_info=_first_str(payload, "resetInfo", "reset_info"),
                tier_name=_first_str(payload, "tierName", "tier_name"),
            )

        case "artifact":
            return Artifact(
                artifact_id=_first_str(payload, "artifactId", "artifact_id"),
                title=_as_str(payload.get("title")),
                content_type=_first_str(payload, "contentType", "content_type"),
                version=_as_int(payload.get("version"), 1),
                action=_as_str(payload.get("action"), "created"),
                session_id=_first_str(payload, "sessionId", "session_id"),
                updated_at=_first_str(payload, "updatedAt", "updated_at"),
            )

        case "session_title":
            return SessionTitle(
                session_id=_first_str(payload, "sessionId", "session_id"),
                title=_as_str(payload.get("title")),
            )

        case "ui_resource" | "ui_tool_input_partial":
            # MCP App UI is an HTML iframe framed in a sandbox origin. There is
            # no terminal rendering, and the correlated `tool_use` /
            # `tool_result` already give the user the tool's substance.
            return IgnoredEvent(name=resolved, reason="no terminal rendering")

        case "error" | "stream_error":
            message = _first_str(payload, "message", "error")
            return ErrorEvent(
                message=message or "The server reported an unspecified error",
                code=_as_str(payload.get("code")),
                recoverable=bool(payload.get("recoverable", False)),
            )

        case "done" | "complete":
            return Done()

        case _:
            return UnknownEvent(name=resolved, payload=payload)


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCallRecord:
    """One tool invocation and its outcome, folded from several events."""

    tool_use_id: str
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    is_error: bool = False
    progress: str = ""

    @property
    def finished(self) -> bool:
        return self.result is not None


@dataclass(slots=True)
class AgentTurnAccumulator:
    """Folds a stream of agent events into one finished turn.

    Deliberately mutable and transport-free: the widget layer renders deltas
    live while this keeps the authoritative copy.

    Text handling is the subtle part. Every tool round trip closes an assistant
    message and opens a new one, so this keeps completed messages separately and
    exposes the *last non-empty* one as :attr:`text`. Concatenating them would
    splice the model's pre-tool narration ("Let me search for that...") onto the
    real answer.
    """

    reasoning: str = ""
    stop_reason: str | None = None
    error: str | None = None
    finished: bool = False

    title: str | None = None
    usage: Usage | None = None
    context_window: int | None = None

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    citations: list[CitationEvent] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    quota_notices: list[AgentEvent] = field(default_factory=list)
    oauth_required: list[OAuthRequired] = field(default_factory=list)
    approvals_required: list[ToolApprovalRequired] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)

    events_seen: dict[str, int] = field(default_factory=dict)

    _messages: list[str] = field(default_factory=list)
    _current: list[str] = field(default_factory=list)
    _by_tool_id: dict[str, ToolCallRecord] = field(default_factory=dict)
    _turn_usage: Usage | None = None
    _call_usage: Usage | None = None

    # -- derived -------------------------------------------------------------

    @property
    def text(self) -> str:
        """The answer: the last message that had content."""
        for candidate in reversed([*self._messages, "".join(self._current)]):
            if candidate.strip():
                return candidate
        return ""

    @property
    def transcript(self) -> str:
        """Every assistant message this turn produced, in order.

        Useful for logs and for a "show the model's full working" view; not what
        gets stored as the answer.
        """
        parts = [message for message in self._messages if message.strip()]
        tail = "".join(self._current)
        if tail.strip():
            parts.append(tail)
        return "\n\n".join(parts)

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it hit the token ceiling."""
        return self.stop_reason == "max_tokens"

    @property
    def blocked(self) -> bool:
        """True when quota refused the turn outright."""
        return any(isinstance(notice, QuotaExceeded) for notice in self.quota_notices)

    @property
    def interrupted(self) -> bool:
        """True when the turn is waiting on the user rather than finished.

        A turn that stops here has no answer and is not an error: something has
        to be approved or authorized before it can continue.
        """
        return bool(self.oauth_required or self.approvals_required)

    @property
    def summarized_turns(self) -> int:
        """Total turns rolled into a summary during this stream."""
        return sum(event.summarized_turns for event in self.compactions)

    @property
    def ok(self) -> bool:
        """True when the turn produced an answer and reported no error."""
        return self.error is None and not self.blocked and bool(self.text)

    # -- folding -------------------------------------------------------------

    def apply(self, event: AgentEvent) -> None:  # noqa: C901 - a dispatch table
        """Incorporate one event."""
        self._count(event)

        match event:
            case MessageStart():
                self._close_message()
            case TextDelta(text=chunk):
                self._current.append(chunk)
            case Reasoning(text=chunk):
                self.reasoning += chunk
            case MessageStop(stop_reason=reason):
                self.stop_reason = reason
                self._close_message()
            case ContentBlockStart(block_type="tool_use", tool_use_id=tool_id, tool_name=name) if tool_id:
                self._upsert_tool(tool_id, name=name)
            case ToolUse(tool_use_id=tool_id, name=name, arguments=arguments, partial=partial):
                record = self._upsert_tool(tool_id, name=name)
                # A partial re-emit carries no usable arguments; keeping the
                # previous ones means the rendered call does not flicker empty.
                if not partial and arguments:
                    record.arguments = arguments
            case ToolResult(tool_use_id=tool_id, text=text, is_error=is_error):
                existing = self._resolve_tool(tool_id)
                if existing is not None:
                    existing.result = text
                    existing.is_error = is_error
            case ToolProgress(message=message, tool_use_id=tool_id):
                existing = self._resolve_tool(tool_id)
                if existing is not None:
                    existing.progress = message
            case ToolApprovalRequired():
                self.approvals_required.append(event)
            case OAuthRequired():
                self.oauth_required.append(event)
            case CitationEvent():
                self.citations.append(event)
            case Metadata(usage=usage, context_window=window):
                # Per-LLM-call. Held only as a fallback for turns that never
                # send a summary.
                self._call_usage = usage
                if window is not None:
                    self.context_window = window
                self._settle_usage()
            case MetadataSummary(usage=usage):
                self._turn_usage = usage
                self._settle_usage()
            case Compaction():
                self.compactions.append(event)
            case QuotaWarning() | QuotaSessionNotice() | QuotaExceeded():
                self.quota_notices.append(event)
            case Artifact():
                self._upsert_artifact(event)
            case SessionTitle(title=title) if title:
                self.title = title
            case ErrorEvent(message=message):
                self.error = message
            case Done():
                self._close_message()
                self.finished = True
            case _:
                # IgnoredEvent, UnknownEvent, ContentBlockStop, ToolInputDelta.
                # Tool arguments are folded from `tool_use` re-emits rather than
                # from the partial-JSON deltas, so the deltas need no state.
                pass

    # -- internals -----------------------------------------------------------

    def _count(self, event: AgentEvent) -> None:
        name = getattr(event, "name", None) or type(event).__name__
        self.events_seen[name] = self.events_seen.get(name, 0) + 1

    def _close_message(self) -> None:
        joined = "".join(self._current)
        if joined.strip():
            self._messages.append(joined)
        self._current = []

    def _upsert_tool(self, tool_use_id: str, *, name: str = "") -> ToolCallRecord:
        """Find or create the record for a tool id, filling in a known name."""
        record = self._by_tool_id.get(tool_use_id)
        if record is None:
            record = ToolCallRecord(tool_use_id=tool_use_id, name=name)
            self.tool_calls.append(record)
            if tool_use_id:
                self._by_tool_id[tool_use_id] = record
        elif name and not record.name:
            record.name = name
        return record

    def _resolve_tool(self, tool_use_id: str) -> ToolCallRecord | None:
        """The record a result belongs to.

        Falls back to the most recent unfinished call when the id is missing,
        which some passthrough shapes omit. Without the fallback a result would
        be dropped and the call would render as perpetually running.
        """
        record = self._by_tool_id.get(tool_use_id)
        if record is not None:
            return record
        for candidate in reversed(self.tool_calls):
            if not candidate.finished:
                return candidate
        return None

    def _upsert_artifact(self, event: Artifact) -> None:
        """Keep one entry per artifact id, at the highest version seen."""
        for index, existing in enumerate(self.artifacts):
            if existing.artifact_id == event.artifact_id:
                if event.version >= existing.version:
                    self.artifacts[index] = event
                return
        self.artifacts.append(event)

    def _settle_usage(self) -> None:
        """Prefer the whole-turn summary; fall back to the last call."""
        self.usage = self._turn_usage or self._call_usage
