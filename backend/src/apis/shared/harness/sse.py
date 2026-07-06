"""Server-side SSE consumption for `/invocations` streams (Unknown 2).

Nothing else in the platform reads an `/invocations` SSE stream server-side —
the app-api chat proxy only relays bytes to a browser. This module parses the
stream into `(event_name, payload)` pairs and accumulates them into the
fields a `RunResult` needs.

Wire format (see `agents/main_agent/streaming/stream_processor.py` and the
SSE table in CLAUDE.md): `event: <name>\\ndata: <json>\\n\\n`. A few legacy
paths emit bare `data:` lines whose JSON carries a `type` field — the parser
falls back to that. The stream interleaves raw Strands passthrough events
(`event: event`) with the typed events; the accumulator only consumes the
typed ones.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from apis.shared.harness.models import OAuthConsentRequired, ToolTraceEntry

logger = logging.getLogger(__name__)

_TOOL_RESULT_PREVIEW_CHARS = 2000


async def iter_sse_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """Parse an SSE line stream into `(event_name, payload)` tuples.

    Follows the SSE framing rules we actually emit: `event:` (optional) and
    `data:` lines terminated by a blank line. Multi-`data:`-line events are
    joined per the SSE spec. Events with unparseable JSON payloads are
    surfaced as `("_unparseable", {"raw": ...})` so callers can count them
    without the reader dying mid-stream.
    """
    event_name: Optional[str] = None
    data_lines: List[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line == "":
            if data_lines or event_name is not None:
                data = "\n".join(data_lines)
                payload: Dict[str, Any]
                try:
                    payload = json.loads(data) if data else {}
                    if not isinstance(payload, dict):
                        payload = {"value": payload}
                except json.JSONDecodeError:
                    yield "_unparseable", {"raw": data[:500]}
                    event_name, data_lines = None, []
                    continue
                # Bare `data:` events carry their name in a `type` field.
                name = event_name or str(payload.get("type") or "message")
                yield name, payload
            event_name, data_lines = None, []
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        # Comments (`:`) and unknown fields are ignored per the SSE spec.


@dataclass
class InvocationStreamAccumulator:
    """Folds the typed event stream into RunResult-shaped state.

    Text accumulation: assistant text arrives as `content_block_delta`
    events (`type == "text"`); a turn with tool use emits several
    `message_start`/`message_stop` cycles, so the *final* assistant message
    is the text of the last completed message (falling back to any
    unterminated buffer, then to the whole-turn transcript).
    """

    done: bool = False
    stop_reason: Optional[str] = None
    error: Optional[str] = None
    title: Optional[str] = None
    tool_trace: List[ToolTraceEntry] = field(default_factory=list)
    oauth_required: List[OAuthConsentRequired] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    events_seen: Dict[str, int] = field(default_factory=dict)

    _current: List[str] = field(default_factory=list)
    _messages: List[str] = field(default_factory=list)
    _trace_by_id: Dict[str, ToolTraceEntry] = field(default_factory=dict)
    _per_message_usage: Dict[str, Any] = field(default_factory=dict)

    @property
    def final_message(self) -> str:
        for text in reversed(self._messages + ["".join(self._current)]):
            if text.strip():
                return text
        return ""

    @property
    def transcript(self) -> str:
        parts = [m for m in self._messages if m.strip()]
        tail = "".join(self._current)
        if tail.strip():
            parts.append(tail)
        return "\n\n".join(parts)

    def handle(self, name: str, payload: Dict[str, Any]) -> None:
        self.events_seen[name] = self.events_seen.get(name, 0) + 1

        if name == "message_start":
            if "".join(self._current).strip():
                self._messages.append("".join(self._current))
            self._current = []
        elif name == "content_block_delta":
            if payload.get("type") == "text" and payload.get("text"):
                self._current.append(str(payload["text"]))
        elif name == "message_stop":
            self.stop_reason = payload.get("stopReason") or self.stop_reason
            if "".join(self._current).strip():
                self._messages.append("".join(self._current))
            self._current = []
        elif name == "tool_use":
            # Two wire shapes: the flat event-formatter payload
            # ({toolUseId, name, input}) and the stream-processor passthrough
            # ({"tool_use": {tool_use_id, name, input}}) where `input` is a
            # *partial JSON string* re-emitted as the model streams the
            # arguments. Upsert by id so a streamed tool call folds into one
            # trace entry whose input is the last parseable prefix.
            data = payload.get("tool_use")
            if not isinstance(data, dict):
                data = payload
            tool_use_id = str(
                data.get("toolUseId") or data.get("tool_use_id") or ""
            )
            entry = self._trace_by_id.get(tool_use_id)
            if entry is None:
                entry = ToolTraceEntry(tool_use_id=tool_use_id, name="")
                self.tool_trace.append(entry)
                if tool_use_id:
                    self._trace_by_id[tool_use_id] = entry
            if data.get("name"):
                entry.name = str(data["name"])
            raw_input = data.get("input")
            if isinstance(raw_input, dict):
                entry.input = raw_input
            elif isinstance(raw_input, str) and raw_input:
                try:
                    parsed = json.loads(raw_input)
                    if isinstance(parsed, dict):
                        entry.input = parsed
                except json.JSONDecodeError:
                    pass  # partial prefix; a later re-emit will complete it
        elif name in ("tool_result", "tool_error"):
            # Flat event-formatter payload ({toolUseId, result}) or the
            # message-shaped passthrough ({"message": {"content":
            # [{"toolResult": {toolUseId, status, content: [{text}]}}]}}).
            tool_results: List[Dict[str, Any]] = []
            message = payload.get("message")
            if isinstance(message, dict):
                for block in message.get("content") or []:
                    if isinstance(block, dict) and isinstance(
                        block.get("toolResult"), dict
                    ):
                        tool_results.append(block["toolResult"])
            if not tool_results:
                tool_results.append(payload)
            for tr in tool_results:
                tool_use_id = str(
                    tr.get("toolUseId") or tr.get("tool_use_id") or ""
                )
                entry = self._trace_by_id.get(tool_use_id)
                if entry is None and self.tool_trace:
                    entry = self.tool_trace[-1]
                if entry is None:
                    continue
                result = tr.get("result") or tr.get("error")
                if result is None and isinstance(tr.get("content"), list):
                    result = "\n".join(
                        str(block.get("text"))
                        for block in tr["content"]
                        if isinstance(block, dict) and "text" in block
                    )
                entry.result_preview = str(result or "")[
                    :_TOOL_RESULT_PREVIEW_CHARS
                ]
                if name == "tool_error" or tr.get("status") == "error":
                    entry.is_error = True
        elif name == "metadata":
            # Per-model-call usage; keep the last as a fallback if the turn
            # summary never arrives (short turns emit both).
            for key in ("usage", "metrics"):
                if key in payload:
                    self._per_message_usage[key] = payload[key]
        elif name == "metadata_summary":
            # Turn-cumulative totals — authoritative for cost attribution.
            self.usage.update(
                {k: v for k, v in payload.items() if k != "type"}
            )
        elif name == "session_title":
            self.title = payload.get("title") or self.title
        elif name == "oauth_required":
            provider = str(
                payload.get("providerId") or payload.get("provider_id") or ""
            )
            url = str(
                payload.get("authorizationUrl")
                or payload.get("authorization_url")
                or ""
            )
            if url:
                self.oauth_required.append(
                    OAuthConsentRequired(provider_id=provider, authorization_url=url)
                )
        elif name in ("stream_error", "error"):
            self.error = str(
                payload.get("message") or payload.get("error") or payload
            )[:2000]
        elif name == "done":
            self.done = True

    def finalize_usage(self) -> Dict[str, Any]:
        if self.usage:
            return self.usage
        return dict(self._per_message_usage)
