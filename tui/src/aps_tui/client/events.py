"""Typed representation of the ``/chat/api-converse`` SSE event stream.

The server emits exactly these event names (see ``_converse_event_to_sse`` in
``backend/src/apis/app_api/chat/converse_routes.py``)::

    message_start        {"role": "assistant"}
    content_block_start  {"contentBlockIndex": int, "type": "text"|"tool_use", "toolUse"?: {...}}
    content_block_delta  {"contentBlockIndex": int, "type": "text", "text": str}
    reasoning_start      {"contentBlockIndex": int}
    reasoning_delta      {"contentBlockIndex": int, "text": str}
    reasoning_stop       {"contentBlockIndex": int}
    content_block_stop   {"contentBlockIndex": int}
    message_stop         {"stopReason": str}
    metadata             {"usage": {...}, "metrics": {...}}
    error                {"error": str}
    done                 {}

Parsing is kept separate from transport so the fold from events to a finished
message is pure and unit-testable without a socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..usage import Usage


class ConverseEvent:
    """Base class for a parsed SSE event."""


@dataclass(frozen=True, slots=True)
class MessageStart(ConverseEvent):
    role: str = "assistant"


@dataclass(frozen=True, slots=True)
class ContentBlockStart(ConverseEvent):
    index: int
    block_type: str = "text"
    tool_use: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TextDelta(ConverseEvent):
    """An incremental chunk of assistant prose."""

    index: int
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningStart(ConverseEvent):
    index: int


@dataclass(frozen=True, slots=True)
class ReasoningDelta(ConverseEvent):
    """An incremental chunk of extended-thinking content."""

    index: int
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningStop(ConverseEvent):
    index: int


@dataclass(frozen=True, slots=True)
class ContentBlockStop(ConverseEvent):
    index: int


@dataclass(frozen=True, slots=True)
class MessageStop(ConverseEvent):
    stop_reason: str = "end_turn"


@dataclass(frozen=True, slots=True)
class Metadata(ConverseEvent):
    usage: Usage
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ErrorEvent(ConverseEvent):
    """A mid-stream failure reported by the server."""

    message: str


@dataclass(frozen=True, slots=True)
class Done(ConverseEvent):
    """Terminal event — the server has finished, successfully or not."""


@dataclass(frozen=True, slots=True)
class UnknownEvent(ConverseEvent):
    """An event name this client does not know.

    Kept rather than raising so a newer server can add events without breaking
    older clients.
    """

    name: str
    payload: dict[str, Any]


def _as_index(payload: dict[str, Any]) -> int:
    raw = payload.get("contentBlockIndex", 0)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _as_text(payload: dict[str, Any]) -> str:
    raw = payload.get("text", "")
    return raw if isinstance(raw, str) else ""


def parse_event(name: str, data: str) -> ConverseEvent:
    """Turn a raw SSE ``(event, data)`` pair into a :class:`ConverseEvent`.

    Malformed JSON yields an :class:`ErrorEvent` rather than raising, so one bad
    frame degrades the turn instead of tearing down the app.
    """
    if not data:
        payload: dict[str, Any] = {}
    else:
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            return ErrorEvent(f"Server sent malformed JSON on `{name}` event")
        payload = decoded if isinstance(decoded, dict) else {}

    match name:
        case "message_start":
            role = payload.get("role")
            return MessageStart(role=role if isinstance(role, str) else "assistant")
        case "content_block_start":
            block_type = payload.get("type")
            tool_use = payload.get("toolUse")
            return ContentBlockStart(
                index=_as_index(payload),
                block_type=block_type if isinstance(block_type, str) else "text",
                tool_use=tool_use if isinstance(tool_use, dict) else None,
            )
        case "content_block_delta":
            return TextDelta(index=_as_index(payload), text=_as_text(payload))
        case "reasoning_start":
            return ReasoningStart(index=_as_index(payload))
        case "reasoning_delta":
            return ReasoningDelta(index=_as_index(payload), text=_as_text(payload))
        case "reasoning_stop":
            return ReasoningStop(index=_as_index(payload))
        case "content_block_stop":
            return ContentBlockStop(index=_as_index(payload))
        case "message_stop":
            reason = payload.get("stopReason")
            return MessageStop(stop_reason=reason if isinstance(reason, str) else "end_turn")
        case "metadata":
            raw_usage = payload.get("usage")
            metrics = payload.get("metrics")
            return Metadata(
                usage=Usage.from_payload(raw_usage if isinstance(raw_usage, dict) else {}),
                metrics=metrics if isinstance(metrics, dict) else {},
            )
        case "error":
            message = payload.get("error")
            return ErrorEvent(message if isinstance(message, str) and message else "The server reported an unspecified error")
        case "done":
            return Done()
        case _:
            return UnknownEvent(name=name, payload=payload)


@dataclass(slots=True)
class TurnAccumulator:
    """Folds a stream of events into one finished assistant turn.

    Deliberately mutable and transport-free: the widget layer appends deltas for
    live rendering while this keeps the authoritative copy used for the next
    request's message history.
    """

    text: str = ""
    reasoning: str = ""
    usage: Usage | None = None
    stop_reason: str | None = None
    error: str | None = None
    finished: bool = False

    def apply(self, event: ConverseEvent) -> None:
        """Incorporate one event."""
        match event:
            case TextDelta(text=chunk):
                self.text += chunk
            case ReasoningDelta(text=chunk):
                self.reasoning += chunk
            case Metadata(usage=usage):
                self.usage = usage
            case MessageStop(stop_reason=reason):
                self.stop_reason = reason
            case ErrorEvent(message=message):
                self.error = message
            case Done():
                self.finished = True
            case _:
                pass

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it hit the token ceiling."""
        return self.stop_reason == "max_tokens"

    @property
    def ok(self) -> bool:
        """True when the turn produced content and reported no error."""
        return self.error is None and bool(self.text)
