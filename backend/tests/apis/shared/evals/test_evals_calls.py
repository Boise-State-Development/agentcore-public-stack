"""A sample's model calls from its C# rows, for ``score --explain`` (eval-harness spec §14 q3)."""

from __future__ import annotations

import asyncio

from apis.shared.evals.calls import fetch_session_calls, summarize_calls

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _call(status: str = "hit", total: float = 0.001, model: str = MODEL, **extra):
    return {
        "cacheStatus": status,
        "cost": {"total": total},
        "modelInfo": {"modelId": model},
        "prefixFingerprints": {"systemPromptHash": "3cbe722b4ea04526", "toolConfigHash": "d110f374ca6a3839"},
        **extra,
    }


def test_summary_counts_cache_and_compaction_and_keeps_the_first_hashes():
    rows = [
        _call("first_write", windowRemovedMessages=0),
        _call("hit", compactionEvents=[{"kind": "applied", "summaryTokens": 812}], windowRemovedMessages=4),
        _call("hit", windowRemovedMessages=2),
    ]
    summary = summarize_calls(rows, model_id=MODEL)
    assert summary.calls == 3
    assert summary.cache_statuses == (("first_write", 1), ("hit", 2))
    assert summary.compaction_events == (("applied", 1),)
    assert summary.trimmed_messages == 4
    assert summary.other_models == ()
    assert summary.cost_usd == 0.003
    line = summary.line()
    assert "3 calls" in line and "compaction applied 1" in line and "window trimmed 4" in line
    assert "sys 3cbe72 tools d110f3" in line and "$0.0030" in line


def test_a_call_on_another_model_and_the_switch_flags_are_named():
    rows = [_call(), _call(model="us.amazon.nova-micro-v1:0", usageEstimated={"outputTokens": "estimate"},
                           modelSwitched=True)]
    line = summarize_calls(rows, model_id=MODEL).line()
    assert "ran on us.amazon.nova-micro-v1:0" in line
    assert "usage estimated" in line and "model switched" in line
    assert "agent switched" not in line


def test_no_rows_and_malformed_fields_never_raise():
    assert summarize_calls([], model_id=MODEL).line() == "no cost rows"
    summary = summarize_calls(
        [{"cost": "n/a", "modelInfo": None, "compactionEvents": ["x", {}], "windowRemovedMessages": "4"}],
        model_id=MODEL,
    )
    assert summary.calls == 1 and summary.cost_usd == 0.0
    assert summary.cache_statuses == (("unknown", 1),)
    assert summary.compaction_events == () and summary.trimmed_messages == 0
    assert summary.system_prompt_hash is None


class _Storage:
    def __init__(self):
        self.reads = []

    async def get_session_cost_records(self, session_id):
        self.reads.append(session_id)
        if session_id == "broken":
            raise RuntimeError("throttled")
        return [_call()]


def test_fetch_reads_each_session_once_and_leaves_out_a_failed_read():
    storage = _Storage()
    found = asyncio.run(fetch_session_calls(["s1", "s1", "", "broken", "s2"], storage))
    assert storage.reads == ["s1", "broken", "s2"]
    assert sorted(found) == ["s1", "s2"]


# --------------------------------------------------------------------------- document fields (§8.5)

from decimal import Decimal  # noqa: E402

from apis.shared.evals.calls import DocumentReads, PlannedFile  # noqa: E402


def test_document_fields_take_the_peak_and_sum_the_reads():
    rows = [
        _call("first_write", documentsAttached=Decimal(2), documentTokens=Decimal(31000), documentDigests=0,
              documentMime={"pdf": Decimal(1), "md": 1}, documentReads={"calls": 1, "pages": 4, "bytes": 900},
              turnDurationMs=4000),
        _call("hit", documentsAttached=0, documentTokens=29000, documentDigests=1, documentMime={"pdf": 1},
              documentReads={"calls": Decimal(2), "pages": 6, "bytes": 100}, turnDurationMs=2000),
    ]
    summary = summarize_calls(rows, model_id=MODEL)
    assert summary.documents_attached == 2
    assert summary.document_tokens == 31000
    assert summary.document_digests == 1
    assert summary.document_reads == DocumentReads(calls=3, pages=10, bytes=1000)
    assert summary.document_mime == (("md", 1), ("pdf", 1))
    line = summary.line()
    assert "documents 2 attached, ~31,000 tokens, 1 digests" in line and "document_read 3× 10p" in line


def test_turn_cache_statuses_read_the_first_call_of_each_turn():
    # A turn's last call carries turnDurationMs; a tool round in between does not.
    rows = [
        _call("first_write"), _call("hit", turnDurationMs=5000),
        _call("partial_miss"), _call("hit"), _call("hit", turnDurationMs=3000),
        _call("miss", turnDurationMs=1000),
    ]
    assert summarize_calls(rows, model_id=MODEL).turn_cache_statuses == ("first_write", "partial_miss", "miss")


def test_cuts_and_offloads_are_counted_by_reason():
    rows = [
        _call(compactionEvents=[{"kind": "checkpoint", "checkpoint": 4}, {"kind": "forced"}]),
        _call(compactionEvents=[{"kind": "applied", "truncatedToolResults": 3}, {"kind": "document_rehydrated"}]),
        _call(compactionEvents=[{"kind": "applied", "truncatedToolResults": 3}, {"kind": "truncation_anchor"},
                                {"kind": "document_offload", "documents": 1}]),
    ]
    summary = summarize_calls(rows, model_id=MODEL)
    assert summary.cuts == (("checkpoint", 1), ("forced", 1), ("truncation_anchor", 1))
    # ``applied`` repeats per restore of one cut, so its truncations count once.
    assert summary.offloads == (("document_offload", 1), ("document_rehydrated", 1), ("tool_results_truncated", 3))
    assert summary.cut_count == 3 and summary.offload_count == 5
    assert "cuts checkpoint 1" in summary.line() and "offloads document_offload 1" in summary.line()


def test_attachment_plan_is_read_from_the_first_row_that_has_one_in_any_container():
    entry = {"mode": "text", "class": "inline", "tokens": Decimal(30500), "reason": "under_budget"}
    for raw in ([entry], {"files": [entry]}, {"1730000000000-abc": entry}):
        rows = [_call(), _call(attachmentPlan=raw), _call(attachmentPlan=[{"mode": "native"}])]
        summary = summarize_calls(rows, model_id=MODEL)
        assert summary.attachment_plan == (PlannedFile("text", "inline", 30500, "under_budget"),)
        assert "plan text 1" in summary.line()
        assert "1730000000000-abc" not in str(summary.to_dict())


def test_absent_document_fields_stay_untracked_not_zero():
    summary = summarize_calls([_call()], model_id=MODEL)
    assert summary.documents_attached is None and summary.document_tokens is None
    assert summary.document_reads is None and summary.attachment_plan == ()
    assert summary.to_dict()["documentReads"] is None
    assert "documents" not in summary.line()
