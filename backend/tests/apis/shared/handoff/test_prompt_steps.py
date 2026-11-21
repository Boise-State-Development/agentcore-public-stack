"""A prompt step's row writes: claim, finish and stop (workflow-designer.md §21.3)."""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from apis.shared.handoff.context import context_hash
from apis.shared.handoff.delivery import SEND_BACK_MAX_CHARS
from apis.shared.handoff.prompt_steps import PromptStepStore, WorkflowPromptRequest

REGION = "us-west-2"
TABLE = "test-sessions-metadata"


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def store(table):
    return PromptStepStore(table=table)


def _put(table, handoff_id="h1", *, user_id="user-a", kind="prompt", status="open", run_id="r1"):
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"HANDOFF#{handoff_id}",
            "handoffId": handoff_id,
            "userId": user_id,
            "sessionId": "src",
            "kind": kind,
            "status": status,
            "runId": run_id,
            "stepId": "s1",
            "modelId": "us.anthropic.claude-haiku-5-5",
            "request": "Score it.",
            "createdAt": now,
            "updatedAt": now,
        }
    )


def _row(table, handoff_id="h1", user_id="user-a"):
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"HANDOFF#{handoff_id}"})["Item"]


def test_a_step_is_claimed_once(table, store):
    _put(table)

    assert store.claim("user-a", "h1", run_id="r1") is True
    assert store.claim("user-a", "h1", run_id="r1") is False
    assert _row(table)["status"] == "running"


@pytest.mark.parametrize(
    "overrides, claimant, run_id",
    [
        ({"kind": "mention"}, "user-a", "r1"),
        ({"run_id": "r2"}, "user-a", "r1"),
        ({}, "user-b", "r1"),
        ({"status": "done"}, "user-a", "r1"),
    ],
    ids=["an-agent-step", "another-run", "another-user", "already-done"],
)
def test_the_claim_refuses_anything_but_this_runs_open_prompt_step(table, store, overrides, claimant, run_id):
    _put(table, **overrides)

    assert store.claim(claimant, "h1", run_id=run_id) is False


def test_the_claim_is_one_conditioned_write():
    """The guard lives in the write: one ``update_item`` conditioned on ``open`` and ``prompt``, no read."""
    calls = []

    class _Table:
        def update_item(self, **kwargs):
            calls.append(kwargs)
            return {}

        def get_item(self, **kwargs):  # pragma: no cover - a read here would be the bug
            raise AssertionError("the claim must not read before writing")

    assert PromptStepStore(table=_Table()).claim("user-a", "h1", run_id="r1") is True

    assert len(calls) == 1
    assert calls[0]["ConditionExpression"] == "attribute_exists(SK) AND #status = :from0 AND #kind = :kind AND #runId = :runId"
    values = calls[0]["ExpressionAttributeValues"]
    assert (values[":from0"], values[":kind"], values[":runId"], values[":status"]) == ("open", "prompt", "r1", "running")


def test_finish_writes_the_capped_answer_its_hash_and_tokens(table, store):
    _put(table)
    store.claim("user-a", "h1", run_id="r1")
    long_answer = "A finding. " * 2_000

    assert store.finish("user-a", "h1", output_text=long_answer, output_tokens=812) is True

    row = _row(table)
    assert row["status"] == "done"
    assert len(row["outputText"]) <= SEND_BACK_MAX_CHARS
    assert row["outputText"].endswith("[truncated]")
    assert row["outputHash"] == context_hash(row["outputText"])
    assert int(row["outputTokens"]) == 812
    assert store.finish("user-a", "h1", output_text="again", output_tokens=1) is False


def test_finish_needs_a_claim(table, store):
    _put(table)

    assert store.finish("user-a", "h1", output_text="x", output_tokens=1) is False
    assert _row(table)["status"] == "open"


def test_stop_records_the_reason_and_only_from_running(table, store):
    _put(table)
    assert store.stop("user-a", "h1", "too_large") is False

    store.claim("user-a", "h1", run_id="r1")
    assert store.stop("user-a", "h1", "too_large") is True
    assert (_row(table)["status"], _row(table)["stoppedReason"]) == ("stopped", "too_large")
    assert store.finish("user-a", "h1", output_text="late", output_tokens=1) is False


def test_stop_refuses_an_unknown_reason(store):
    with pytest.raises(ValueError):
        store.stop("user-a", "h1", "bored")  # type: ignore[arg-type]


def test_the_request_holds_at_most_eight_distinct_ids():
    request = WorkflowPromptRequest.model_validate({"runId": "r1", "stepIds": ["s1", "s2", "s1"]})
    assert request.step_ids == ["s1", "s2"]

    with pytest.raises(ValidationError):
        WorkflowPromptRequest.model_validate({"runId": "r1", "stepIds": [f"s{n}" for n in range(9)]})
    with pytest.raises(ValidationError):
        WorkflowPromptRequest.model_validate({"runId": "r1", "stepIds": []})
