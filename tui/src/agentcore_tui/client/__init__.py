"""Transport and wire-format layer for the api-converse endpoint."""

from __future__ import annotations

from .converse import ApiConverseClient, ChatMessage, CompletedTurn
from .events import (
    ContentBlockStart,
    ContentBlockStop,
    ConverseEvent,
    Done,
    ErrorEvent,
    MessageStart,
    MessageStop,
    Metadata,
    ReasoningDelta,
    ReasoningStart,
    ReasoningStop,
    TextDelta,
    TurnAccumulator,
    UnknownEvent,
    Usage,
    parse_event,
)

__all__ = [
    "ApiConverseClient",
    "ChatMessage",
    "CompletedTurn",
    "ContentBlockStart",
    "ContentBlockStop",
    "ConverseEvent",
    "Done",
    "ErrorEvent",
    "MessageStart",
    "MessageStop",
    "Metadata",
    "ReasoningDelta",
    "ReasoningStart",
    "ReasoningStop",
    "TextDelta",
    "TurnAccumulator",
    "UnknownEvent",
    "Usage",
    "parse_event",
]
