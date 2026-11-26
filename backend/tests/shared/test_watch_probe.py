"""The quiet watcher's probe, the pure half (docs/specs/quiet-watcher-triage.md §2.1, §3.2, §3.3).

The fingerprint's version-1 normalization, the coverage rules that decide whether a full run
leaves a probe, the worker's join of the ``watch_probe`` frame with the tool trace, and the
``wakeAt`` clamp.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apis.shared.harness.models import ToolTraceEntry
from apis.shared.scheduled_prompts import watch_probe as wp
from apis.shared.scheduled_prompts.watch_probe import (
    FINGERPRINT_VERSION,
    MAX_PROBE_CALLS,
    MAX_PROBE_INPUT_CHARS,
    ProbeCall,
    decide_probe,
    fingerprint_result,
    full_run_triage,
    next_wake_at,
    probe_from_run,
    probe_input_json,
    report_wake_ats,
    watch_probe_payload,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _text(t: str, status: str = "success") -> dict:
    return {"status": status, "content": [{"text": t}]}


def _call(i: int = 1, *, read_only: bool = True, input_chars: int = 10, name: str = "list_things") -> ProbeCall:
    return ProbeCall(tool_use_id=f"t{i}", name=name, read_only=read_only, fingerprint=f"fp{i}", input_chars=input_chars)


class TestFingerprint:
    def test_is_32_hex_characters(self):
        fp = fingerprint_result(_text("hello"))
        assert len(fp) == 32 and int(fp, 16) >= 0

    def test_is_deterministic(self):
        assert fingerprint_result(_text("a b")) == fingerprint_result(_text("a b"))

    def test_collapses_whitespace_runs_in_text(self):
        assert fingerprint_result(_text("a  b\n\n c ")) == fingerprint_result(_text("a b c"))

    def test_any_character_change_in_text_changes_it(self):
        assert fingerprint_result(_text("Budget $25,000")) != fingerprint_result(_text("Budget $30,000"))

    def test_json_is_canonical_by_key_order(self):
        a = {"status": "success", "content": [{"json": {"a": 1, "b": [1, 2]}}]}
        b = {"status": "success", "content": [{"json": {"b": [1, 2], "a": 1}}]}
        assert fingerprint_result(a) == fingerprint_result(b)

    def test_json_list_order_is_content(self):
        a = {"status": "success", "content": [{"json": [1, 2]}]}
        b = {"status": "success", "content": [{"json": [2, 1]}]}
        assert fingerprint_result(a) != fingerprint_result(b)

    def test_the_error_flag_is_included(self):
        assert fingerprint_result(_text("x")) != fingerprint_result(_text("x", status="error"))

    def test_block_order_is_content(self):
        a = {"status": "success", "content": [{"text": "one"}, {"text": "two"}]}
        b = {"status": "success", "content": [{"text": "two"}, {"text": "one"}]}
        assert fingerprint_result(a) != fingerprint_result(b)

    def test_text_and_json_of_the_same_characters_differ(self):
        assert fingerprint_result(_text('"x"')) != fingerprint_result({"status": "success", "content": [{"json": "x"}]})

    def test_structured_content_is_included(self):
        a = {**_text("3 items"), "structuredContent": {"count": 3}}
        b = {**_text("3 items"), "structuredContent": {"count": 4}}
        assert fingerprint_result(a) != fingerprint_result(b)
        assert fingerprint_result(a) != fingerprint_result(_text("3 items"))

    def test_image_bytes_are_hashed_not_dropped(self):
        def image(data: bytes) -> dict:
            return {"status": "success", "content": [{"image": {"format": "png", "source": {"bytes": data}}}]}

        assert fingerprint_result(image(b"\x89PNG-a")) == fingerprint_result(image(b"\x89PNG-a"))
        assert fingerprint_result(image(b"\x89PNG-a")) != fingerprint_result(image(b"\x89PNG-b"))

    @pytest.mark.parametrize("odd", [None, "plain", 3, {"content": "not a list"}, {"content": [None, 5]}])
    def test_is_total(self, odd):
        assert len(fingerprint_result(odd)) == 32

    def test_the_module_is_stdlib_only(self):
        """The Runtime and the worker share it; it must not pull in boto3, pydantic or Strands."""
        tree = ast.parse(Path(wp.__file__).read_text())
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "datetime", "typing"}


class TestCoverageRules:
    def test_no_calls_is_no_probe(self):
        assert decide_probe([]) == wp.ProbeDecision(False, "no_calls")

    def test_one_write_is_no_probe(self):
        assert decide_probe([_call(1), _call(2, read_only=False)]) == wp.ProbeDecision(False, "write_tool")

    def test_a_write_wins_over_every_other_reason(self):
        calls = [_call(i, read_only=i != 3, input_chars=MAX_PROBE_INPUT_CHARS + 1) for i in range(MAX_PROBE_CALLS + 2)]
        assert decide_probe(calls).reason == "write_tool"

    def test_up_to_five_calls(self):
        assert decide_probe([_call(i) for i in range(MAX_PROBE_CALLS)]).probeable is True
        assert decide_probe([_call(i) for i in range(MAX_PROBE_CALLS + 1)]) == wp.ProbeDecision(False, "too_many_calls")

    def test_input_bound(self):
        assert decide_probe([_call(input_chars=MAX_PROBE_INPUT_CHARS)]).probeable is True
        assert decide_probe([_call(input_chars=MAX_PROBE_INPUT_CHARS + 1)]) == wp.ProbeDecision(False, "input_too_large")

    def test_every_reason_is_named(self):
        assert set(wp.PROBE_REASONS) == {"write_tool", "too_many_calls", "input_too_large", "no_calls"}

    def test_the_frame_carries_calls_only_when_probeable(self):
        assert watch_probe_payload([_call(1)]) == {
            "type": "watch_probe",
            "fingerprintVersion": FINGERPRINT_VERSION,
            "probeable": True,
            "calls": [{"toolUseId": "t1", "name": "list_things", "fingerprint": "fp1"}],
        }
        assert watch_probe_payload([_call(1, read_only=False)]) == {
            "type": "watch_probe",
            "fingerprintVersion": FINGERPRINT_VERSION,
            "probeable": False,
            "reason": "write_tool",
            "calls": [],
        }

    def test_input_json_is_canonical(self):
        assert probe_input_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
        assert probe_input_json(None) == "{}"


def _entry(tool_use_id: str, name: str, **tool_input) -> ToolTraceEntry:
    return ToolTraceEntry(tool_use_id=tool_use_id, name=name, input=tool_input)


def _frame(*calls, probeable=True, version=FINGERPRINT_VERSION) -> dict:
    return {
        "fingerprintVersion": version,
        "probeable": probeable,
        "calls": [{"toolUseId": i, "name": n, "fingerprint": f"fp-{i}"} for i, n in calls],
    }


class TestProbeFromRun:
    TRACE = [
        _entry("t1", "list_prs", repo="r"),
        _entry("t2", "get_reviews", repo="r", number=41),
        _entry("r1", "report", outcome="nothing_new"),
    ]

    def test_joins_the_frame_with_the_trace_inputs_in_call_order(self):
        # The hook saw t2 finish first (a concurrent batch); the probe is in the order asked.
        probe = probe_from_run(_frame(("t2", "get_reviews"), ("t1", "list_prs")), self.TRACE)
        assert probe == [
            {"name": "list_prs", "input": '{"repo":"r"}', "fingerprint": "fp-t1"},
            {"name": "get_reviews", "input": '{"number":41,"repo":"r"}', "fingerprint": "fp-t2"},
        ]

    @pytest.mark.parametrize(
        "frame",
        [
            None,
            {},
            _frame(("t1", "list_prs"), ("t2", "get_reviews"), probeable=False),
            _frame(("t1", "list_prs"), ("t2", "get_reviews"), version=FINGERPRINT_VERSION + 1),
            _frame(),
        ],
    )
    def test_no_frame_or_an_unusable_one_is_no_probe(self, frame):
        assert probe_from_run(frame, self.TRACE) is None

    def test_a_call_the_hook_missed_is_no_probe(self):
        assert probe_from_run(_frame(("t1", "list_prs")), self.TRACE) is None

    def test_a_call_the_stream_missed_is_no_probe(self):
        frame = _frame(("t1", "list_prs"), ("t2", "get_reviews"), ("t3", "list_prs"))
        assert probe_from_run(frame, self.TRACE) is None

    def test_a_name_mismatch_is_no_probe(self):
        assert probe_from_run(_frame(("t1", "list_prs"), ("t2", "list_prs")), self.TRACE) is None

    def test_an_input_over_the_bound_is_no_probe(self):
        trace = [_entry("t1", "search", q="x" * MAX_PROBE_INPUT_CHARS)]
        assert probe_from_run(_frame(("t1", "search")), trace) is None

    def test_more_than_five_calls_is_no_probe(self):
        trace = [_entry(f"t{i}", "list") for i in range(MAX_PROBE_CALLS + 1)]
        assert probe_from_run(_frame(*[(f"t{i}", "list") for i in range(MAX_PROBE_CALLS + 1)]), trace) is None


class TestWakeAt:
    def test_keeps_the_earliest_future_time(self):
        assert next_wake_at(["2026-11-27T09:00:00Z", "2026-11-26T09:00:00Z"], NOW) == "2026-11-26T09:00:00Z"

    def test_drops_a_past_time_and_now(self):
        assert next_wake_at(["2026-11-24T09:00:00Z", NOW.isoformat()], NOW) is None

    def test_clamps_beyond_thirty_days(self):
        assert next_wake_at(["2027-06-01T00:00:00Z"], NOW) == wp.iso_utc(NOW + timedelta(days=30))

    def test_normalizes_offsets_to_utc(self):
        assert next_wake_at(["2026-11-26T09:00:00-07:00"], NOW) == "2026-11-26T16:00:00Z"

    def test_reads_a_naive_time_as_utc(self):
        assert next_wake_at(["2026-11-26T09:00:00"], NOW) == "2026-11-26T09:00:00Z"

    @pytest.mark.parametrize("junk", ["tomorrow", "", 5, None, {"at": "x"}])
    def test_drops_what_does_not_parse(self, junk):
        assert next_wake_at([junk], NOW) is None

    def test_reads_every_report_call(self):
        trace = [
            _entry("r1", "report", outcome="nothing_new", state={"wakeAt": "2026-11-28T00:00:00Z"}),
            _entry("t1", "list", state={"wakeAt": "2026-11-26T00:00:00Z"}),
            _entry("r2", "report", outcome="nothing_new", state={"cursors": {}}),
            _entry("r3", "report", outcome="nothing_new", state={"wakeAt": "2026-11-27T00:00:00Z"}),
        ]
        assert report_wake_ats(trace) == ["2026-11-28T00:00:00Z", "2026-11-27T00:00:00Z"]


class TestFullRunTriage:
    def test_a_probeable_run(self):
        trace = [
            _entry("t1", "list_prs", repo="r"),
            _entry("r1", "report", outcome="nothing_new", state={"wakeAt": "2026-11-26T09:00:00Z"}),
        ]
        triage = full_run_triage(
            frame=_frame(("t1", "list_prs")), tool_trace=trace, run_id="run-1", finished_at="2026-11-25T12:00:05Z", now=NOW
        )
        assert triage.probe == [{"name": "list_prs", "input": '{"repo":"r"}', "fingerprint": "fp-t1"}]
        assert (triage.fingerprint_version, triage.probe_run_id) == (FINGERPRINT_VERSION, "run-1")
        assert (triage.last_full_run_at, triage.wake_at) == ("2026-11-25T12:00:05Z", "2026-11-26T09:00:00Z")

    def test_a_run_with_no_probe_still_records_the_full_run(self):
        triage = full_run_triage(frame=None, tool_trace=[], run_id="run-1", finished_at="", now=NOW)
        assert (triage.probe, triage.fingerprint_version, triage.probe_run_id, triage.wake_at) == (None, None, None, None)
        assert triage.last_full_run_at == wp.iso_utc(NOW)
