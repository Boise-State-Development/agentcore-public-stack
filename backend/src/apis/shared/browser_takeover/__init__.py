"""Wire contract for handing the browser to the user so they can sign in.

Shared by the producer (``agents.builtin_tools.browser.takeover_tool``), the
transport (``agents.main_agent.streaming.stream_coordinator``) and the
persistence layer (``apis.shared.sessions``), which is why it lives here rather
than in any one of them — see the import-boundary rule in CLAUDE.md.
"""

from .frame_height import (
    FRAME_HEIGHT_STATE_KEY,
    coerce_frame_height,
    frame_height_from,
)
from .models import (
    BrowserLoginRequiredEvent,
    BrowserSessionRef,
    TakeoverOutcome,
    Viewport,
    assert_no_url,
    decode_ref,
    encode_ref,
    format_outcome,
    parse_outcome,
)

__all__ = [
    "FRAME_HEIGHT_STATE_KEY",
    "coerce_frame_height",
    "frame_height_from",
    "BrowserLoginRequiredEvent",
    "BrowserSessionRef",
    "TakeoverOutcome",
    "Viewport",
    "assert_no_url",
    "decode_ref",
    "encode_ref",
    "format_outcome",
    "parse_outcome",
]
