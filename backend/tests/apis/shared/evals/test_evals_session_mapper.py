"""``RunResult`` dicts → a strands-evals ``Session``, and the scrub a judge reads through.

The package is not a dependency yet (phase 1 PR-2 adds it), so the shape is
pinned here field by field against strands-agents-evals 1.7.0's
``types/trace.py``. When the package is installed, the last test validates the
real model too.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from apis.shared.evals.scrub import DOCUMENT_RESULT_PLACEHOLDER, MAX_TOOL_RESULT_CHARS, scrub_session, scrub_text
from apis.shared.evals.session_mapper import build_session


def _run(final="done", tools=(), started="2026-10-29T10:00:00Z", finished="2026-10-29T10:00:10Z"):
    return {
        "final_message": final,
        "started_at": started,
        "finished_at": finished,
        "tool_trace": [
            {"tool_use_id": f"tu-{i}", "name": name, "input": inp, "result_preview": res, "is_error": err}
            for i, (name, inp, res, err) in enumerate(tools)
        ],
    }


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_one_trace_per_turn_with_an_agent_span_first():
    session = build_session(
        session_id="eval-1",
        turns=[("first", _run("a")), ("second", _run("b"))],
        available_tools=[{"name": "calculator"}],
    )
    assert session["session_id"] == "eval-1"
    assert [t["trace_id"] for t in session["traces"]] == ["eval-1-t0", "eval-1-t1"]
    agent = session["traces"][1]["spans"][0]
    assert agent["span_type"] == "invoke_agent"
    assert agent["user_prompt"] == "second" and agent["agent_response"] == "b"
    assert agent["available_tools"] == [{"name": "calculator", "description": None, "parameters": None}]
    assert "system_prompt" not in agent


def test_tool_spans_are_owned_by_the_agent_span_and_strictly_ordered():
    tools = [
        ("calculator", {"expression": "2+2"}, "4", False),
        ("whoami", {}, "", True),
        ("broken", "partial-json", None, False),
    ]
    trace = build_session(session_id="s", turns=[("q", _run(tools=tools))])["traces"][0]
    agent, *tool_spans = trace["spans"]
    assert [s["tool_call"]["name"] for s in tool_spans] == ["calculator", "whoami", "broken"]
    for span in tool_spans:
        assert span["span_type"] == "execute_tool"
        assert span["agent_span_id"] == agent["span_info"]["span_id"]
        assert span["span_info"]["parent_span_id"] == agent["span_info"]["span_id"]
    ends = [_parse(s["span_info"]["end_time"]) for s in tool_spans]
    assert ends == sorted(ends) and len(set(ends)) == len(ends)
    assert _parse(agent["span_info"]["start_time"]) < ends[0] <= _parse(agent["span_info"]["end_time"])
    assert tool_spans[1]["tool_result"]["error"]
    assert tool_spans[0]["tool_result"]["error"] is None
    assert tool_spans[2]["tool_call"]["arguments"] == {"raw": "partial-json"}
    assert tool_spans[2]["tool_result"]["content"] == ""


def test_missing_or_inverted_timestamps_still_give_a_valid_window():
    session = build_session(
        session_id="s",
        turns=[("q", _run(tools=[("x", {}, "", False)], started="", finished="")),
               ("r", _run(started="2026-10-29T10:00:05Z", finished="2026-10-29T10:00:01Z"))],
    )
    for trace in session["traces"]:
        info = trace["spans"][0]["span_info"]
        assert _parse(info["start_time"]) < _parse(info["end_time"])


def test_validates_against_the_strands_evals_model_when_installed():
    trace_types = pytest.importorskip("strands_evals.types.trace")
    data = build_session(
        session_id="s",
        turns=[("q", _run(tools=[("calculator", {"expression": "1+1"}, "2", False)]))],
        available_tools=[{"name": "calculator"}],
    )
    session = trace_types.Session.model_validate(data)
    spans = session.traces[0].spans
    assert isinstance(spans[0], trace_types.AgentInvocationSpan)
    assert isinstance(spans[1], trace_types.ToolExecutionSpan)


class TestScrub:
    def test_digest_placeholder_and_attachment_marker_are_removed(self):
        text = (
            "Summarise it.\n\n[Attached files: memo.md]"
            '<document-digest name="memo.md" handle="u1">\nsecret body\n</document-digest>'
            " and [Document placeholder: memo.md]"
        )
        scrubbed = scrub_text(text)
        assert "secret body" not in scrubbed and "Attached files" not in scrubbed
        assert "[document digest omitted]" in scrubbed and "[document omitted]" in scrubbed

    def test_document_tool_results_are_hidden_and_others_capped(self):
        session = build_session(
            session_id="s",
            turns=[("q", _run(tools=[
                ("document_read", {"upload_id": "u1"}, "page text", False),
                ("calculator", {}, "x" * (MAX_TOOL_RESULT_CHARS + 50), False),
            ]))],
        )
        scrubbed = scrub_session(session)
        _, doc, calc = scrubbed["traces"][0]["spans"]
        assert doc["tool_result"]["content"] == DOCUMENT_RESULT_PLACEHOLDER
        assert doc["tool_call"]["arguments"] == {"upload_id": "u1"}
        assert calc["tool_result"]["content"].endswith("[50 chars omitted]")

    def test_the_input_is_not_mutated(self):
        session = build_session(session_id="s", turns=[("q", _run(tools=[("document_read", {}, "body", False)]))])
        scrub_session(session)
        assert session["traces"][0]["spans"][1]["tool_result"]["content"] == "body"
