"""The attachment_delivery readout (unified-attachment-delivery §8.5)."""

from __future__ import annotations

import pytest

from apis.shared.evals.delivery_readout import delivery_readout, format_delivery_readout
from apis.shared.evals.report import ReportError, merge_family_baseline

MODEL = "us.anthropic.claude-haiku-5-5"


def _row(case, sample, passed, session, *, status="completed", first=1000, turn=4000, upload=300):
    return {"case_id": f"attachment_delivery.{case}", "family": "attachment_delivery", "sample": sample,
            "passed": passed, "status": status, "session_id": session, "model_id": MODEL, "upload_ms": upload,
            "timing": [{"firstTokenMs": first, "turnDurationMs": turn, "clientFirstTokenMs": first + 200,
                        "clientDurationMs": turn + 200}]}


def _call(status, cost, **extra):
    return {"cacheStatus": status, "cost": {"total": cost}, "modelInfo": {"modelId": MODEL}, **extra}


def test_readout_per_case_pass_rate_cost_timing_and_context():
    rows = [
        _row("follow_up_30k", 0, True, "s0", first=1000),
        _row("follow_up_30k", 1, True, "s1", first=3000),
        _row("follow_up_30k", 2, False, "s2", first=2000, status="error"),
    ]
    two_turns = [
        _call("first_write", 0.01, documentTokens=30500, documentsAttached=1, turnDurationMs=4000),
        _call("hit", 0.002, documentTokens=30500, turnDurationMs=2000,
              compactionEvents=[{"kind": "document_offload"}]),
    ]
    calls = {"s0": two_turns, "s1": two_turns, "s2": [_call("first_write", 0.01, turnDurationMs=1)]}
    (readout,) = delivery_readout(rows, calls)
    assert (readout.samples, readout.passes) == (3, 2)
    assert readout.cost_per_sample == pytest.approx((0.012 * 2 + 0.01) / 3)
    assert readout.first_token_ms == 2000 and readout.turn_duration_ms == 4000 and readout.upload_ms == 300
    assert readout.document_tokens == 30500
    assert readout.delivery == {"inline": 2, "none": 1}
    assert readout.second_turn_cache == {"hit": 2}
    assert readout.offloads == {"document_offload": 2} and readout.offloads_per_sample == pytest.approx(2 / 3)
    assert readout.failures == {"error": 1}
    table = format_delivery_readout([readout], title="Haiku 5.5")
    assert "| follow_up_30k | — | — | 2/3 |" in table and "2.0s" in table and "30,500" in table
    assert "s0" not in table and "s0" not in str(readout.to_dict())


def test_readout_without_cost_rows_still_reports_quality_and_timing():
    (readout,) = delivery_readout([_row("lookup_md_5k", 0, False, "s0")], None)
    assert readout.cost_per_sample is None and readout.failures == {"wrong answer": 1}
    assert "—" in format_delivery_readout([readout])


def test_the_plan_names_the_delivery_when_rows_carry_one():
    calls = {"s0": [_call("first_write", 0.01, attachmentPlan=[{"mode": "on_demand", "tokens": 300000}],
                          documentReads={"calls": 2, "pages": 9, "bytes": 1})]}
    (readout,) = delivery_readout([_row("lookup_md_300k", 0, True, "s0")], calls)
    assert readout.delivery == {"on_demand": 1}


def _baseline(families, cases, **extra):
    return {"schemaVersion": 1, "modelId": MODEL, "k": 3, "families": families, "promotedAt": "t",
            "source": {"gitCommit": "abc"}, "cases": cases, **extra}


def test_merging_a_family_replaces_only_that_familys_cases():
    existing = _baseline(["document_qa", "attachment_delivery"], {
        "document_qa.a": {"family": "document_qa", "passed": True},
        "attachment_delivery.gone": {"family": "attachment_delivery", "passed": False},
    }, judgeScores={"document_qa.a": {"grounded": 1.0}})
    promoted = _baseline(["attachment_delivery"], {
        "attachment_delivery.lookup_md_5k": {"family": "attachment_delivery", "passed": True}})
    merged = merge_family_baseline(existing, promoted)
    assert sorted(merged["cases"]) == ["attachment_delivery.lookup_md_5k", "document_qa.a"]
    assert merged["families"] == ["document_qa", "attachment_delivery"]
    assert merged["judgeScores"] == {"document_qa.a": {"grounded": 1.0}}
    assert merged["familyPromotions"]["attachment_delivery"]["source"] == {"gitCommit": "abc"}
    assert existing["cases"]["attachment_delivery.gone"]  # the input is not mutated


def test_merging_refuses_another_model_or_k():
    with pytest.raises(ReportError, match="the baseline is for"):
        merge_family_baseline(_baseline([], {}), {**_baseline([], {}), "modelId": "other"})
    with pytest.raises(ReportError, match="k=3"):
        merge_family_baseline(_baseline([], {}), {**_baseline([], {}), "k": 1})
