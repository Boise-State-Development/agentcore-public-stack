"""A sample's turns as a strands-evals ``Session`` (eval-harness spec §6.3).

The output is a plain dict in the exact shape of
``strands_evals.types.trace.Session`` (verified against strands-agents-evals
1.7.0), so ``Session.model_validate(build_session(...))`` is the whole adapter
and this module needs no import of the package. The span union there has no
discriminator; each dict carries exactly the required fields of one span type,
so validation picks it unambiguously.

One ``Trace`` per turn, holding:

* one ``AgentInvocationSpan`` — the prompt, the final answer and the tools the
  case offered. ``system_prompt`` is left out: the runner never sees the real
  one, and a made-up one would mislead an instruction-following judge;
* one ``ToolExecutionSpan`` per ``tool_trace`` entry, owned by that agent span.

``RunResult`` carries no per-tool timing, so tool spans get synthetic, strictly
increasing times inside the turn's window. The tool-level extractor orders a
tool's "prior executions" by end time, and equal times would make every call
look concurrent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _window(run: Mapping[str, Any], fallback_start: datetime, n_tools: int) -> Tuple[datetime, datetime]:
    start = _parse_time(run.get("started_at")) or fallback_start
    end = _parse_time(run.get("finished_at"))
    if end is None or end <= start:
        end = start + timedelta(seconds=n_tools + 1)
    return start, end


def _arguments(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value in (None, ""):
        return {}
    return {"raw": str(value)}


def build_session(
    *,
    session_id: str,
    turns: Sequence[Tuple[str, Mapping[str, Any]]],
    available_tools: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """``turns`` is ``(prompt, RunResult.to_dict())`` in order; returns a ``Session``-shaped dict.

    ``available_tools`` are ``{"name", "description"?, "parameters"?}``; the
    harness passes the case's ``enabled_tools`` by name, and the judge adapter
    may enrich them with catalog descriptions.
    """
    tools = [
        {
            "name": str(t["name"]),
            "description": t.get("description"),
            "parameters": t.get("parameters"),
        }
        for t in available_tools
    ]

    traces: List[Dict[str, Any]] = []
    cursor = _EPOCH
    for index, (prompt, run) in enumerate(turns):
        trace_id = f"{session_id}-t{index}"
        agent_span_id = f"{trace_id}-agent"
        trace_entries = list(run.get("tool_trace") or [])
        start, end = _window(run, cursor, len(trace_entries))
        cursor = end + timedelta(seconds=1)

        spans: List[Dict[str, Any]] = [
            {
                "span_info": {
                    "trace_id": trace_id,
                    "span_id": agent_span_id,
                    "session_id": session_id,
                    "parent_span_id": None,
                    "start_time": _iso(start),
                    "end_time": _iso(end),
                },
                "span_type": "invoke_agent",
                "user_prompt": prompt,
                "agent_response": str(run.get("final_message") or ""),
                "available_tools": tools,
            }
        ]

        step = (end - start) / (len(trace_entries) + 1)
        for position, entry in enumerate(trace_entries):
            tool_start = start + step * (position + 0.5)
            tool_end = start + step * (position + 1)
            spans.append(
                {
                    "span_info": {
                        "trace_id": trace_id,
                        "span_id": f"{trace_id}-tool-{position}",
                        "session_id": session_id,
                        "parent_span_id": agent_span_id,
                        "start_time": _iso(tool_start),
                        "end_time": _iso(tool_end),
                    },
                    "span_type": "execute_tool",
                    "tool_call": {
                        "name": str(entry.get("name") or ""),
                        "arguments": _arguments(entry.get("input")),
                        "tool_call_id": entry.get("tool_use_id"),
                    },
                    "tool_result": {
                        "content": str(entry.get("result_preview") or ""),
                        "error": "the tool reported an error" if entry.get("is_error") else None,
                        "tool_call_id": entry.get("tool_use_id"),
                    },
                    "agent_span_id": agent_span_id,
                }
            )

        traces.append({"trace_id": trace_id, "session_id": session_id, "spans": spans})

    return {"session_id": session_id, "traces": traces}
