"""Minimal SSE reader for the chat stream.

Only what the chat path actually emits, which is named events produced by
``agents/main_agent/streaming/stream_coordinator.py``:

    event: message_start        data: {"role": "assistant"}
    event: content_block_start  data: {"contentBlockIndex": 0, "type": "text"}
    event: content_block_delta  data: {"contentBlockIndex": 0, "type": "text", ...}
    event: content_block_stop   data: {"contentBlockIndex": 0}
    event: message_stop         data: {"stopReason": "end_turn"}
    event: done                 data: {}

Side channels (``metadata``, ``compaction``, ``artifact``, ``ui_resource``,
``oauth_required``) ride the same stream, and some arrive *after*
``message_stop`` by design. ``done`` is therefore the only reliable
end-of-stream marker — stopping at ``message_stop`` would truncate a stream
the server is still writing to and show up as a client-side error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

# The first delta is the user-visible "the assistant is answering" moment, so
# it is what time-to-first-token should measure.
FIRST_TOKEN_EVENT = "content_block_delta"
TURN_END_EVENT = "done"
MESSAGE_STOP_EVENT = "message_stop"


@dataclass(frozen=True)
class SseEvent:
    name: str
    data: dict

    @property
    def is_first_token_candidate(self) -> bool:
        return self.name == FIRST_TOKEN_EVENT

    @property
    def text(self) -> str:
        value = self.data.get("text")
        return value if isinstance(value, str) else ""


def iter_sse_events(lines: Iterator[str]) -> Iterator[SseEvent]:
    """Turn a stream of decoded lines into events.

    Accumulates ``event:``/``data:`` fields and dispatches on the blank line
    that terminates each SSE block. Unparseable ``data:`` payloads are yielded
    with an empty dict rather than raising — a malformed side-channel event
    should not abort a turn that is otherwise streaming fine.
    """
    event_name: str | None = None
    data_parts: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r")

        if line == "":
            if event_name is not None:
                yield SseEvent(name=event_name, data=_parse_data(data_parts))
            event_name = None
            data_parts = []
            continue

        if line.startswith(":"):
            # Comment / keepalive. app-api sends these to stop intermediaries
            # from cutting an idle stream.
            continue

        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value

        if field == "event":
            event_name = value
        elif field == "data":
            data_parts.append(value)

    # A stream that ends without its terminating blank line still has a
    # complete event buffered; emitting it keeps a hard client disconnect from
    # silently losing the final `done`.
    if event_name is not None:
        yield SseEvent(name=event_name, data=_parse_data(data_parts))


def _parse_data(parts: list[str]) -> dict:
    if not parts:
        return {}
    try:
        parsed = json.loads("\n".join(parts))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
