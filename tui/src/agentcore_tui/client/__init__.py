"""Transport and wire-format layer for app-api.

Structure to preserve as this grows: ``endpoints`` owns URLs, ``auth`` owns
per-request credentials, and each endpoint gets a module pairing its payload
shape with its event dialect. ``events`` is the ``/chat/api-converse`` dialect —
the agent stream is a *second* dialect module beside it, not an extension of it.
"""

from __future__ import annotations

from .agent_stream import AgentStreamClient
from .auth import ApiKeyAuth, AuthProvider, NoAuth, SessionAuth
from .converse import ApiConverseClient, CompletedTurn, message_payloads
from .device_auth import DeviceAuthClient, DeviceAuthorization, DeviceSession
from .endpoints import Endpoints
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
    parse_event,
)

__all__ = [
    "AgentStreamClient",
    "ApiConverseClient",
    "ApiKeyAuth",
    "AuthProvider",
    "CompletedTurn",
    "ContentBlockStart",
    "ContentBlockStop",
    "ConverseEvent",
    "DeviceAuthClient",
    "DeviceAuthorization",
    "DeviceSession",
    "Done",
    "Endpoints",
    "ErrorEvent",
    "MessageStart",
    "MessageStop",
    "Metadata",
    "NoAuth",
    "ReasoningDelta",
    "ReasoningStart",
    "ReasoningStop",
    "SessionAuth",
    "TextDelta",
    "TurnAccumulator",
    "UnknownEvent",
    "message_payloads",
    "parse_event",
]
