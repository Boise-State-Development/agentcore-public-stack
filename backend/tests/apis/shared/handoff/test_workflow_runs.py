"""Workflow runs' store, the step rows they write, and the replay (workflow-runs spec §9, PR-W.1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import turn as turn_module
from apis.shared.handoff.context import context_hash, wrap_handoff_context
from apis.shared.handoff.excerpt import render_excerpt
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.replay import load_handoff_replay, load_workflow_run_replay
from apis.shared.handoff.runs import (
    WorkflowRun,
    WorkflowRunCard,
    WorkflowRunStep,
    WorkflowRunStore,
    WorkflowRunView,
)
from apis.shared.handoff.store import HandoffStore
from apis.shared.handoff.turn import HandoffContextRequest, HandoffTurn

REGION = "us-west-2"
TABLE = "test-sessions-metadata"
EXCERPT = render_excerpt(
    [
        {"role": "user", "content": [{"text": "review this proposal"}]},
        {"role": "assistant", "content": [{"text": "Here is a summary of the proposal."}]},
    ]
).text


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
        "WORKFLOW_RUNS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionLookupIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def handoffs(table) -> HandoffStore:
    return HandoffStore(table=table)


@pytest.fixture()
def runs(table) -> WorkflowRunStore:
    return WorkflowRunStore(table=table)


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def _step_row(handoff_id: str, agent_id: str, *, run_id: str = "r1", step_id: str = "s1", age: timedelta = timedelta(0)) -> Handoff:
    return Handoff(
        handoff_id=handoff_id,
        user_id="alice",
        session_id="src",
        kind="mention",
        agent_id=agent_id,
        agent_name=agent_id.title(),
        request="review this proposal",
        source_title="Proposal",
        source_message_index=1,
        excerpt_hash=context_hash(EXCERPT),
        excerpt_tokens=40,
        created_at=_iso(age),
        updated_at=_iso(age),
        run_id=run_id,
        step_id=step_id,
    )


def _run(*, run_id: str = "r1", user_id: str = "alice", age: timedelta = timedelta(0)) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        user_id=user_id,
        session_id="src",
        request="review this proposal",
        steps=[
            WorkflowRunStep(step_id="s1", handoff_id="h1", agent_id="generalist", agent_name="Generalist", request="review this proposal"),
            WorkflowRunStep(step_id="s2", handoff_id="h2", agent_id="colleague", agent_name="Colleague", request="review this proposal"),
        ],
        source_message_index=1,
        created_at=_iso(age),
        updated_at=_iso(age),
    )


# ---------------------------------------------------------------- the step row's abandoned rule


class TestAbandoned:
    def test_an_ordinary_mention_a_day_old_still_reads_abandoned(self):
        row = _step_row("h1", "generalist", age=timedelta(hours=25)).model_copy(update={"run_id": None, "step_id": None})
        assert row.effective_status() == "abandoned"

    def test_a_step_of_a_run_still_at_its_sheet_ages_like_a_mention(self):
        assert _step_row("h1", "generalist", age=timedelta(hours=25)).effective_status() == "abandoned"

    def test_a_step_of_an_approved_run_never_reads_abandoned(self, handoffs):
        handoffs.create(_step_row("h1", "generalist", age=timedelta(hours=25)))
        handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso())
        row = handoffs.get("alice", "h1")
        assert row.status == "open"
        assert row.effective_status(now=datetime.now(timezone.utc) + timedelta(days=30)) == "open"

    def test_approving_a_step_records_the_runs_kept_count(self, handoffs):
        handoffs.create(_step_row("h1", "generalist"))
        assert handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso(), run_steps=4).run_steps == 4
        assert handoffs.get("alice", "h1").run_steps == 4
        handoffs.create(_step_row("h2", "colleague"))
        assert handoffs.approve_run_step("alice", "h2", run_id="r1", approved_at=_iso()).run_steps is None

    def test_approving_a_step_sets_its_edited_request(self, handoffs):
        handoffs.create(_step_row("h1", "generalist"))
        updated = handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso(), request="only the budget")
        assert (updated.request, updated.run_approved_at is not None) == ("only the budget", True)

    def test_only_a_step_of_that_run_can_be_approved(self, handoffs):
        handoffs.create(_step_row("h1", "generalist").model_copy(update={"run_id": None}))
        handoffs.create(_step_row("h2", "colleague", run_id="other"))
        assert handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso()) is None
        assert handoffs.approve_run_step("alice", "h2", run_id="r1", approved_at=_iso()) is None
        assert handoffs.approve_run_step("bob", "h2", run_id="other", approved_at=_iso()) is None

    def test_a_sent_step_is_not_approved_again(self, handoffs):
        handoffs.create(_step_row("h1", "generalist"))
        handoffs.accept("alice", "h1", target_session_id="tgt", context_edited=False, context_removed=False)
        assert handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso()) is None


# ---------------------------------------------------------------- acceptance through HandoffTurn


def test_a_step_row_is_accepted_on_its_first_turn_by_the_unchanged_handoff_turn(handoffs, monkeypatch):
    """A step is a mention with a ``runId``: the Runtime's acceptance needs nothing new (spec §3.1)."""
    import apis.shared.sessions.metadata as metadata

    spawned: List[Any] = []
    monkeypatch.setattr(metadata, "set_session_spawned_from", lambda *a: spawned.append(a) or True)
    monkeypatch.setattr(metadata, "settle_session_spawned_from", lambda *a: None)
    handoffs.create(_step_row("h1", "generalist"))
    handoffs.approve_run_step("alice", "h1", run_id="r1", approved_at=_iso(), run_steps=3)

    async def run() -> str:
        turn = HandoffTurn.for_turn(
            user_id="alice",
            session_id="tgt",
            agent_id="generalist",
            handoff_context=HandoffContextRequest(handoff_id="h1", text=EXCERPT),
            session_row=None,
            is_resume=False,
            is_continuation=False,
            store=handoffs,
        )
        assert turn is not None
        turn.start()
        block = await turn.context_block()
        # PR-W.4c: the link (now with runId) is written in the background after acceptance. The
        # context block, which the first token waits on, is back before that write has run.
        written_before_block_returned = list(spawned)
        pending = list(turn_module._background)
        if pending:
            await asyncio.gather(*pending)
        return block, written_before_block_returned, len(pending)

    block, before, pending = asyncio.run(run())

    assert block == wrap_handoff_context(EXCERPT, "Proposal")
    row = handoffs.get("alice", "h1")
    assert (row.status, row.target_session_id, row.run_id, row.step_id) == ("running", "tgt", "r1", "s1")
    assert (before, pending) == ([], 1)
    # The step's spawnedFrom gains runId, and the run's kept count read from the accepted row, in
    # the same SET as before (workflow-runs spec §19 PR-W.4c, §20.4).
    assert spawned == [("tgt", "alice", "src", "h1", "r1", 3)]


# ---------------------------------------------------------------- the run store


class TestRunStore:
    def test_create_get_and_owner_scoping(self, runs):
        runs.create(_run())
        got = runs.get("alice", "r1")
        assert (got.status, [s.step_id for s in got.steps], got.limits.max_steps, got.limits.concurrency) == (
            "open", ["s1", "s2"], 8, 4,
        )
        assert got.result_delivery_id is None and got.brief_hash is None
        assert runs.get("bob", "r1") is None

    def test_the_row_is_keyed_like_a_handoff(self, runs, table):
        runs.create(_run())
        item = table.get_item(Key={"PK": "USER#alice", "SK": "WFRUN#r1"})["Item"]
        assert item["GSI_PK"] == "SESSION#src"
        assert item["GSI_SK"].startswith("WFRUN#")
        assert int(item["ttl"]) > int(datetime.now(timezone.utc).timestamp()) + 364 * 86400

    def test_list_for_session_is_the_callers_only_and_skips_handoff_rows(self, runs, handoffs):
        runs.create(_run())
        runs.create(_run(run_id="r2", user_id="bob"))
        handoffs.create(_step_row("h1", "generalist"))
        assert [r.run_id for r in runs.list_for_session("alice", "src")] == ["r1"]
        assert [h.handoff_id for h in handoffs.list_for_session("alice", "src")] == ["h1"]

    def test_approve_happens_once_with_the_envelope(self, runs):
        runs.create(_run())
        steps = [s.model_copy(update={"removed": s.step_id == "s2"}) for s in _run().steps]
        approved = runs.approve(
            "alice", "r1", request="r", steps=steps, file_ids=["f1"], edits=["step_removed"], approved_at=_iso()
        )
        assert (approved.status, approved.file_ids, approved.edits, approved.steps[1].removed) == (
            "approved", ["f1"], ["step_removed"], True,
        )
        assert runs.approve("alice", "r1", request="r", steps=steps, file_ids=[], edits=[], approved_at=_iso()) is None

    def test_approve_keeps_the_card_as_reviewed(self, runs, table):
        runs.create(_run())
        approved = runs.approve(
            "alice", "r1", request="r", steps=_run().steps, file_ids=[], edits=[], approved_at=_iso(),
            card=WorkflowRunCard(text="the card as reviewed"),
        )
        assert approved.card == WorkflowRunCard(text="the card as reviewed")
        item = table.get_item(Key={"PK": "USER#alice", "SK": "WFRUN#r1"})["Item"]
        assert item["card"] == {"text": "the card as reviewed", "removed": False}
        assert runs.get("alice", "r1").card.text == "the card as reviewed"

    def test_a_run_approved_without_a_card_has_none(self, runs):
        runs.create(_run())
        runs.approve("alice", "r1", request="r", steps=_run().steps, file_ids=[], edits=[], approved_at=_iso())
        assert runs.get("alice", "r1").card is None

    @pytest.mark.parametrize(
        "first, then, allowed",
        [
            ("dismissed", "approved", False),
            ("dismissed", "stopped", False),
            (None, "stopped", False),
            ("approved", "dismissed", False),
            ("approved", "stopped", True),
        ],
    )
    def test_transitions(self, runs, first, then, allowed):
        runs.create(_run())
        if first == "approved":
            runs.approve("alice", "r1", request="r", steps=_run().steps, file_ids=[], edits=[], approved_at=_iso())
        elif first:
            runs.transition("alice", "r1", first)
        if then == "approved":
            moved = runs.approve("alice", "r1", request="r", steps=_run().steps, file_ids=[], edits=[], approved_at=_iso())
        else:
            moved = runs.transition("alice", "r1", then)
        assert (moved is not None) is allowed

    def test_transition_refuses_approval(self, runs):
        with pytest.raises(ValueError):
            runs.transition("alice", "r1", "approved")

    def test_an_open_run_a_day_old_reads_abandoned_and_an_approved_one_never_does(self):
        assert _run(age=timedelta(hours=25)).effective_status() == "abandoned"
        approved = _run(age=timedelta(days=3)).model_copy(update={"status": "approved"})
        assert approved.effective_status() == "approved"


# ---------------------------------------------------------------- replay


class TestReplay:
    def test_runs_replay_with_their_steps_rows_and_sent_steps_stay_in_handoffs(self, runs, handoffs):
        runs.create(_run())
        handoffs.create(_step_row("h1", "generalist", step_id="s1"))
        handoffs.create(_step_row("h2", "colleague", step_id="s2"))
        handoffs.accept("alice", "h1", target_session_id="tgt", context_edited=False, context_removed=False)

        replayed = asyncio.run(load_workflow_run_replay("src", "alice", run_store=runs, handoff_store=handoffs))
        sent, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=handoffs))

        assert [r["runId"] for r in replayed] == ["r1"]
        steps = replayed[0]["steps"]
        assert [(s["stepId"], s["handoff"]["status"]) for s in steps] == [("s1", "running"), ("s2", "open")]
        assert steps[0]["handoff"]["runId"] == "r1" and "relayedPrompts" not in steps[0]["handoff"]
        assert [(h["handoffId"], h["runId"], h["stepId"]) for h in sent] == [("h1", "r1", "s1")]

    def test_the_card_as_reviewed_replays_with_the_run(self, runs, handoffs):
        runs.create(_run())
        runs.approve(
            "alice", "r1", request="r", steps=_run().steps, file_ids=[], edits=[], approved_at=_iso(),
            card=WorkflowRunCard(removed=True),
        )
        replayed = asyncio.run(load_workflow_run_replay("src", "alice", run_store=runs, handoff_store=handoffs))
        assert replayed[0]["card"] == {"removed": True}

    def test_nothing_while_the_flag_is_off(self, runs, handoffs, monkeypatch):
        runs.create(_run())
        monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "")
        assert asyncio.run(load_workflow_run_replay("src", "alice", run_store=runs, handoff_store=handoffs)) == []
        monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "true")
        monkeypatch.setenv("HANDOFFS_ENABLED", "false")
        assert asyncio.run(load_workflow_run_replay("src", "alice", run_store=runs, handoff_store=handoffs)) == []

    def test_a_conversation_without_runs_reads_no_handoff_rows(self, runs):
        class Exploding:
            def list_for_session(self, *a: Any) -> None:
                raise AssertionError("read handoff rows for a conversation with no runs")

        assert asyncio.run(load_workflow_run_replay("src", "alice", run_store=runs, handoff_store=Exploding())) == []

    def test_a_failed_read_costs_only_the_runs(self, handoffs):
        class Broken:
            def list_for_session(self, *a: Any) -> None:
                raise RuntimeError("boom")

        assert asyncio.run(load_workflow_run_replay("src", "alice", run_store=Broken(), handoff_store=handoffs)) == []

    def test_an_ordinary_handoff_view_has_no_run_fields(self, handoffs):
        handoffs.create(_step_row("h9", "generalist").model_copy(update={"run_id": None, "step_id": None}))
        handoffs.accept("alice", "h9", target_session_id="tgt", context_edited=False, context_removed=False)
        sent, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=handoffs))
        assert "runId" not in sent[0] and "stepId" not in sent[0]

    def test_a_step_whose_row_is_gone_has_no_handoff_view(self):
        view = WorkflowRunView.from_row(_run(), {})
        dumped = view.model_dump(by_alias=True, exclude_none=True)
        assert [s.get("handoff") for s in dumped["steps"]] == [None, None]


# ---------------------------------------------------------------- the session delete cascade


def test_the_delete_cascade_removes_run_rows_with_the_handoff_rows(table, monkeypatch):
    from apis.app_api.sessions.services import delete_cascade
    from apis.shared.handoff import delivery as delivery_module
    from apis.shared.handoff import runs as runs_module
    from apis.shared.handoff import store as store_module

    handoffs, runs = HandoffStore(table=table), WorkflowRunStore(table=table)
    monkeypatch.setattr(store_module, "_store", handoffs)
    monkeypatch.setattr(runs_module, "_store", runs)
    monkeypatch.setattr(delivery_module, "_store", delivery_module.DeliveryStore(table=table), raising=False)
    runs.create(_run())
    runs.create(_run(run_id="r2", user_id="bob"))
    handoffs.create(_step_row("h1", "generalist"))

    deleted = delete_cascade.delete_session_handoffs("alice", "src")

    assert deleted == 2
    assert runs.get("alice", "r1") is None and handoffs.get("alice", "h1") is None
    assert runs.get("bob", "r2") is not None
