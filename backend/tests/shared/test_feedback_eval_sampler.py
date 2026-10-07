"""Eval sampling (response-feedback spec §11 PR-4): routing by reason, the
content-free verdict, tool-failure corroboration, and the batch over a real
(moto) table with a fake judge — the AgentCore adapter is a seam, not a
dependency of these tests.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.feedback_eval import sampler
from apis.shared.sessions import feedback as fb

OWNER = "user-owner"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table("test-sessions-metadata")


def _seed_session(session_id, user_id=OWNER):
    _table().put_item(Item={
        "PK": f"USER#{user_id}", "SK": f"S#{session_id}", "GSI_PK": f"SESSION#{session_id}", "GSI_SK": "META",
        "sessionId": session_id, "userId": user_id, "status": "active",
    })


def _row(session_id, message_id, user_id=OWNER):
    return _table().get_item(Key={"PK": f"USER#{user_id}", "SK": f"F#{session_id}#{message_id}"}).get("Item")


class FakeJudge:
    def __init__(self, results: List[Dict[str, Any]] | None = None, fail_for: str | None = None):
        self.results = results or []
        self.calls: List[tuple] = []
        self.users: List[str] = []
        self.fail_for = fail_for

    def judge(self, session_id: str, user_id: str, evaluator_ids: Sequence[str]) -> List[Dict[str, Any]]:
        self.calls.append((session_id, tuple(evaluator_ids)))
        self.users.append(user_id)
        if session_id == self.fail_for:
            raise RuntimeError("judge exploded")
        return self.results


RAW = [
    {"evaluatorId": "Builtin.Correctness", "value": 1.0, "label": "Perfectly Correct",
     "explanation": "The agent correctly reported that SECRET tool call 502'd", "tokenUsage": {"totalTokens": 1728},
     "context": {"spanContext": {"sessionId": "sid-x", "traceId": "t1"}}},
    {"evaluatorId": "Builtin.Correctness", "value": 0.0, "label": "Incorrect", "explanation": "SECRET", "tokenUsage": {"totalTokens": 900}},
    {"evaluatorId": "Builtin.Faithfulness", "value": 0.5, "label": "Partly", "explanation": "SECRET"},
    {"evaluatorId": "Builtin.Faithfulness", "errorCode": "ThrottlingException", "errorMessage": "slow down"},
]


def test_routing_follows_the_spec_buckets():
    assert sampler.evaluators_for("wrong") == ("Builtin.Correctness", "Builtin.Faithfulness")
    assert sampler.evaluators_for("instructions") == ("Builtin.InstructionFollowing",)
    assert sampler.evaluators_for("length") == ("Builtin.Conciseness",)
    assert sampler.evaluators_for("tool_failed") == ()
    assert sampler.evaluators_for("outdated") == ()
    assert sampler.evaluators_for("other") == ("Builtin.Helpfulness",)
    assert sampler.evaluators_for(None) == ("Builtin.Helpfulness",)
    assert sampler.evaluators_for("not-a-code") == ("Builtin.Helpfulness",)


def test_summarize_keeps_numbers_and_labels_and_drops_the_explanation():
    scores = sampler.summarize(RAW)
    assert scores == {
        "Builtin.Correctness": {"value": 0.5, "n": 2, "tokens": 2628, "rating": "Incorrect"},
        "Builtin.Faithfulness": {"value": 0.5, "n": 1, "tokens": 0, "rating": "Partly"},
    }
    assert "SECRET" not in repr(scores) and "explanation" not in repr(scores)


def test_corroboration_reads_the_call_census():
    assert sampler.corroborate_tool_failure(None) is None
    assert sampler.corroborate_tool_failure({"cost": {"total": 1}}) is None
    assert sampler.corroborate_tool_failure({"toolCalls": {"web_search": {"calls": 2, "errors": 0}}}) is False
    assert sampler.corroborate_tool_failure({"toolCalls": {"web_search": {"calls": 2, "errors": 1}}}) is True


def test_build_verdict_shapes():
    v = sampler.build_verdict("wrong", RAW, None)
    assert v["reason"] == "wrong" and v["evaluators"] == ["Builtin.Correctness", "Builtin.Faithfulness"]
    assert set(v["scores"]) == {"Builtin.Correctness", "Builtin.Faithfulness"}
    assert "toolFailureCorroborated" not in v
    v = sampler.build_verdict("tool_failed", [], {"toolCalls": {"x": {"calls": 1, "errors": 1}}})
    assert v == {"reason": "tool_failed", "evaluators": [], "toolFailureCorroborated": True}
    assert sampler.build_verdict(None, [], None) == {"reason": "none", "evaluators": ["Builtin.Helpfulness"]}


@pytest.fixture()
def table(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    return _table()


@pytest.mark.asyncio
async def test_down_thumbs_are_queued_on_the_timestamp_index_and_up_thumbs_leave_it(table):
    _seed_session("s1")
    await fb.put_message_feedback("s1", OWNER, 1, -1, reason="wrong")
    await fb.put_message_feedback("s1", OWNER, 3, -1)
    await fb.put_message_feedback("s1", OWNER, 5, 1)
    queue = fb.list_recent_down_thumbs(table, limit=10)
    assert sorted(int(r["messageId"]) for r in queue) == [1, 3]
    assert all(r["GSI1PK"] == "FEEDBACK#down" and r["GSI1SK"] == r["updatedAt"] for r in queue)
    # Flipping to up moves the row out of the down queue.
    await fb.put_message_feedback("s1", OWNER, 1, 1)
    assert [int(r["messageId"]) for r in fb.list_recent_down_thumbs(table, limit=10)] == [3]


@pytest.mark.asyncio
async def test_store_evaluation_refuses_prose_and_requires_the_row(table):
    _seed_session("s1")
    await fb.put_message_feedback("s1", OWNER, 1, -1, reason="wrong")
    with pytest.raises(ValueError):
        fb.store_evaluation(table, OWNER, "s1", 1, {"scores": {"x": {"value": 1, "explanation": "SECRET"}}})
    fb.store_evaluation(table, OWNER, "s1", 1, {"reason": "wrong", "scores": {"Builtin.Correctness": {"value": 1, "n": 1}}})
    row = _row("s1", 1)
    assert row["evaluatedAt"] and row["evaluation"]["reason"] == "wrong"
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        fb.store_evaluation(table, OWNER, "s1", 99, {"reason": "wrong"})


@pytest.mark.asyncio
async def test_batch_judges_by_reason_skips_judged_and_survives_one_failure(table):
    for sid in ("s-wrong", "s-tool", "s-boom", "s-done"):
        _seed_session(sid)
    await fb.put_message_feedback("s-wrong", OWNER, 1, -1, reason="wrong")
    await fb.put_message_feedback("s-tool", OWNER, 2, -1, reason="tool_failed")
    await fb.put_message_feedback("s-boom", OWNER, 3, -1, reason="instructions")
    await fb.put_message_feedback("s-done", OWNER, 4, -1, reason="length")
    fb.store_evaluation(table, OWNER, "s-done", 4, {"reason": "length"})
    judge = FakeJudge(results=RAW, fail_for="s-boom")

    def lookup(session_id, message_id):
        return {"toolCalls": {"web_search": {"calls": 1, "errors": 1}}} if session_id == "s-tool" else None

    report = await sampler.run_sampling_batch(table, judge, limit=10, cost_row_lookup=lookup)

    assert (report.candidates, report.judged, report.skipped_no_judge, report.skipped_already, report.failed) == (3, 1, 1, 1, 1)
    assert report.corroborated == 1 and report.errors == ["RuntimeError"]
    assert report.tokens == 2628
    assert sorted(judge.calls) == [("s-boom", ("Builtin.InstructionFollowing",)), ("s-wrong", ("Builtin.Correctness", "Builtin.Faithfulness"))]
    # Spans are pinned per (user, session), so the judge needs the thumb's user.
    assert judge.users == [OWNER, OWNER]

    wrong = _row("s-wrong", 1)
    assert wrong["evaluation"]["scores"]["Builtin.Correctness"]["rating"] == "Incorrect"
    assert "SECRET" not in repr(wrong)
    tool = _row("s-tool", 2)
    assert tool["evaluation"] == {"reason": "tool_failed", "evaluators": [], "toolFailureCorroborated": True}
    assert "evaluation" not in _row("s-boom", 3)
    # The judged row was left alone (its evaluatedAt is the earlier one).
    assert _row("s-done", 4)["evaluation"] == {"reason": "length"}

    # A second batch finds nothing new to judge except the failed one.
    report = await sampler.run_sampling_batch(table, FakeJudge(results=RAW), limit=10)
    assert (report.candidates, report.judged, report.skipped_already) == (1, 1, 3)


def test_agentcore_judge_requires_the_log_group(monkeypatch):
    monkeypatch.delenv(sampler.RUNTIME_LOG_GROUP_ENV, raising=False)
    with pytest.raises(RuntimeError):
        sampler.AgentCoreJudge().judge("s1", "u1", ["Builtin.Helpfulness"])


def _fake_evaluation_client(monkeypatch, results_by_session):
    calls = {"session_ids": []}

    class FakeClient:
        def __init__(self, region_name=None):
            calls["region"] = region_name

        def run(self, **kwargs):
            calls.update(kwargs)
            calls["session_ids"].append(kwargs["session_id"])
            return results_by_session.get(kwargs["session_id"], [])

    import types, sys
    fake_mod = types.ModuleType("bedrock_agentcore.evaluation")
    fake_mod.EvaluationClient = FakeClient
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.evaluation", fake_mod)
    return calls


def test_agentcore_judge_sends_the_runtime_session_id(monkeypatch):
    from apis.shared.harness.runner import runtime_session_id_for

    pinned = runtime_session_id_for("sess-1", "user-1")
    calls = _fake_evaluation_client(
        monkeypatch, {pinned: [{"evaluatorId": "Builtin.Helpfulness", "value": 1.0}]}
    )

    judge = sampler.AgentCoreJudge(log_group_name="/aws/bedrock-agentcore/runtimes/rt-DEFAULT", region_name="us-west-2")
    out = judge.judge("sess-1", "user-1", ["Builtin.Helpfulness"])
    assert out[0]["value"] == 1.0
    assert calls["session_ids"] == [pinned]
    assert calls["log_group_name"] == "/aws/bedrock-agentcore/runtimes/rt-DEFAULT" and calls["region"] == "us-west-2"
    assert calls["evaluator_ids"] == ["Builtin.Helpfulness"]


def test_agentcore_judge_falls_back_to_spans_from_before_per_user_pinning(monkeypatch):
    from apis.shared.harness.runner import legacy_runtime_session_id_for, runtime_session_id_for

    legacy = legacy_runtime_session_id_for("sess-1")
    calls = _fake_evaluation_client(
        monkeypatch, {legacy: [{"evaluatorId": "Builtin.Helpfulness", "value": 0.5}]}
    )

    judge = sampler.AgentCoreJudge(log_group_name="/aws/bedrock-agentcore/runtimes/rt-DEFAULT", region_name="us-west-2")
    out = judge.judge("sess-1", "user-1", ["Builtin.Helpfulness"])
    assert out[0]["value"] == 0.5
    assert calls["session_ids"] == [runtime_session_id_for("sess-1", "user-1"), legacy]
