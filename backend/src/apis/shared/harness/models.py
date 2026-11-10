"""Result shapes for headless agent runs.

``RunResult`` is the structured return every trigger consumes (schedule
worker, "Run now" route, future A2A server front). Keep it JSON-friendly:
``asdict(result)`` must serialize cleanly so it can become a run-record
item or an A2A task artifact without translation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

# completed        — stream drained to `done` with no stream-level error
# error            — HTTP error, `stream_error`/`error` event, or transport failure
# timeout          — the SSE stream exceeded the caller's budget
# oauth_required   — the turn finished but at least one connector tool needs
#                    user consent (headless runs cannot pop a consent window;
#                    callers should surface the authorization URL to the user)
RunStatus = Literal["completed", "error", "timeout", "oauth_required", "client_tool_call"]


@dataclass
class ToolTraceEntry:
    """One tool invocation observed on the stream."""

    tool_use_id: str
    name: str
    input: Dict[str, Any] = field(default_factory=dict)
    result_preview: Optional[str] = None
    is_error: bool = False


@dataclass
class OAuthConsentRequired:
    """An `oauth_required` SSE event — a connector needs (re-)consent."""

    provider_id: str
    authorization_url: str


@dataclass
class RunResult:
    """Structured outcome of one headless agent turn."""

    run_id: str
    session_id: str
    user_id: str
    status: RunStatus
    final_message: str = ""
    stop_reason: Optional[str] = None
    error: Optional[str] = None
    title: Optional[str] = None
    tool_trace: List[ToolTraceEntry] = field(default_factory=list)
    # Accumulated usage/metrics from the stream's `metadata_summary` (turn
    # totals) with per-message `metadata` events as fallback. Shape mirrors
    # the SSE payloads: {"usage": {...}, "metrics": {...}}.
    usage: Dict[str, Any] = field(default_factory=dict)
    oauth_required: List[OAuthConsentRequired] = field(default_factory=list)
    #: ``client_tool_call`` frames: a tool the request declared in ``client_tools``
    #: was called and the turn paused for the client to run it. Each is
    #: ``{interruptId, toolUseId, toolName, arguments}``. Only a caller that
    #: sends ``client_tools`` (the eval harness's terminal cases) ever sees one.
    client_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    #: The Runtime's own timing, from the turn's final ``metadata`` frame:
    #: ``turnDurationMs`` (from the invocation reaching the container) and the
    #: coordinator's time to first token. ``None`` when the frame never came.
    turn_duration_ms: Optional[int] = None
    first_token_ms: Optional[int] = None
    #: The stream carried ``quota_exceeded``: the "answer" is the quota message,
    #: not the model's. The status stays ``completed``, as the turn did.
    quota_exceeded: bool = False
    #: The caller's view of the same turn: from sending the request to the
    #: first streamed text delta, and to the stream's end. These include the
    #: network and the Runtime's session routing, which the frame cannot see.
    client_first_token_ms: Optional[int] = None
    client_duration_ms: Optional[int] = None
    # Diagnostic: counts of every SSE event name seen, e.g. {"tool_use": 2}.
    events_seen: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
