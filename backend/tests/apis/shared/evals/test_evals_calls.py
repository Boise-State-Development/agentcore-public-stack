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
