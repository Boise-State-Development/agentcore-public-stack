"""The SPA's docked-panel height, carried from the chat request to the takeover.

The sign-in handover is sized once, before ``take_control``, because the
service refuses CDP while the user holds the browser (spec D3a). A fixed
height drew the stream at 1:1 only in windows tall enough for it, so the SPA
sends the height its rail would give the frame, and ``request_user_login``
sizes the handover to that.

It travels as **client data, never prompt data.** The route puts it in Strands'
``invocation_state``, which reaches tools through ``ToolContext`` and is never
sent to the model, so it cannot touch the system prompt, ``toolConfig`` or the
history. Strands builds a fresh ``invocation_state`` per invocation, so it is
one request's value and never lingers on a cached agent (#741/#751).

Untrusted and optional. A missing or unusable value means "use the default
height"; it never fails a turn, because a stale tab or another client that
does not know the field must still be able to chat.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# The key in Strands' ``invocation_state``.
FRAME_HEIGHT_STATE_KEY = "browser_takeover_frame_height"

# What a browser window can plausibly report. Anything outside is treated as
# absent rather than clamped: a value this wrong says nothing about the panel.
# The takeover then clamps a plausible one to what it will hand over at.
_MIN_PLAUSIBLE_PX = 1
_MAX_PLAUSIBLE_PX = 10_000


def coerce_frame_height(value: Any) -> Optional[int]:
    """A usable frame height in CSS pixels, or None.

    Accepts an int or a whole float (JSON has one number type). A bool, a
    string, a fraction or an implausible size is None, never an error.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    if not _MIN_PLAUSIBLE_PX <= value <= _MAX_PLAUSIBLE_PX:
        return None
    return value


def frame_height_from(invocation_state: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The frame height this invocation carried, or None."""
    if not isinstance(invocation_state, Mapping):
        return None
    return coerce_frame_height(invocation_state.get(FRAME_HEIGHT_STATE_KEY))
