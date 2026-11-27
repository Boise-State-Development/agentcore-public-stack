"""The action audit (docs/specs/proactive-trust.md §7, PR 4.2).

- the three classes, by positive evidence only, and the three injected tools left out;
- an approval answered in this invocation is read from its ``before_tool_call`` id;
- the middleware is inert without a run in the invocation's state: an interactive
  turn writes nothing and calls through;
- an audited call is recorded before it runs, and its outcome after;
- a failed audit write returns the error **without calling the tool**;
- an MCP form's pause and replay of one call keep **one** row (``rounds: 2``), and a
  resume's approval upgrades it;
- the stream's ``tool_result`` frame of an audited call says so.

The recorder runs against moto; the tool chain is a stub ``next_fn``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws
from strands.interrupt import Interrupt, InterruptException
from strands.types.events import ToolResultEvent

from agents.main_agent.core.action_audit import (
    REFUSED_TEXT,
    RUN_STATE_KEY,
    HeadlessActionAudit,
    ToolFacts,
    UnattendedRun,
    approved_tool_use_ids,
    audited_class,
    classify_call,
)
from agents.main_agent.streaming.stream_coordinator import _mark_audited
from apis.shared.harness.governance import RunAuditRecorder
from apis.shared.scheduled_prompts.actions import list_schedule_actions

pytestmark = pytest.mark.asyncio

TABLE = "test-sessions-metadata"
REGION = "us-west-2"
USER = "user-1"


def _mcp_tool(name: str, *, read_only=None, destructive=None):
    annotations = SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)
    return SimpleNamespace(tool_name=name, mcp_tool=SimpleNamespace(name=name, annotations=annotations), mcp_client=SimpleNamespace(context_label="Mail"))


# --------------------------------------------------------------------------- classes


@pytest.mark.parametrize(
    "tool, name, facts, expected",
    [
        (None, "whoami", None, None),
        (None, "report", None, None),
        (None, "ask_user_question", None, None),
        (None, "request_user_login", None, None),
        (_mcp_tool("list_mail", read_only=True), "list_mail", None, None),
        (_mcp_tool("send", destructive=True), "send", None, ("write", "destructive_hint")),
        (_mcp_tool("send", read_only=True, destructive=True), "send", None, ("write", "destructive_hint")),
        (_mcp_tool("archive"), "archive", ToolFacts(approval_names=lambda t: {"archive"}), ("write", "needs_approval")),
        (_mcp_tool("draft"), "draft", ToolFacts(write_names=lambda t: {"draft"}), ("write", "catalog_writes")),
        (None, "my_skill_create", ToolFacts(skill_write_names=frozenset({"my_skill_create"})), ("write", "skill_write")),
        (_mcp_tool("list_files"), "list_files", None, ("unclassified", "unannotated")),
        (None, "browse_web", None, ("unclassified", "unannotated")),
    ],
)
async def test_classes(tool, name, facts, expected):
    verdict = classify_call(tool, name, facts)
    assert (None if verdict is None else (verdict.call_class, verdict.why)) == expected


async def test_a_lookup_that_raises_never_breaks_classification():
    def boom(tool):
        raise RuntimeError("no")

    verdict = classify_call(_mcp_tool("x"), "x", ToolFacts(approval_names=boom, write_names=boom))
    assert (verdict.call_class, verdict.why) == ("unclassified", "unannotated")


async def test_approvals_are_read_from_before_tool_call_ids():
    responses = [
        {"interruptId": "v1:before_tool_call:tu-1:0f0f", "response": "approved"},
        {"interruptId": "v1:before_tool_call:tu-2:0f0f", "response": "declined"},
        {"interruptId": "v1:middleware_execute_tool:tu-3:abcd", "response": "approved"},
        SimpleNamespace(interruptId="v1:before_tool_call:tu-4:0f0f", response="approved"),
    ]
    assert approved_tool_use_ids(responses) == {"tu-1", "tu-4"}


# --------------------------------------------------------------------------- the middleware


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", TABLE)
    with mock_aws():
        from apis.shared import aws_clients

        # The recorder reads through the process-cached resource; start it inside moto.
        monkeypatch.setattr(aws_clients, "_resources", {})
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI_PK", "GSI_SK")],
            GlobalSecondaryIndexes=[{
                "IndexName": "SessionLookupIndex",
                "KeySchema": [{"AttributeName": "GSI_PK", "KeyType": "HASH"}, {"AttributeName": "GSI_SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _rows(table) -> List[dict]:
    return table.query(KeyConditionExpression=Key("PK").eq(f"USER#{USER}") & Key("SK").begins_with("RUNACT#"))["Items"]


def _context(tool_name: str, *, run=None, tool=None, tool_use_id="tu-1", tool_input=None):
    state = {RUN_STATE_KEY: run} if run is not None else {}
    return SimpleNamespace(
        tool=tool,
        tool_use={"toolUseId": tool_use_id, "name": tool_name, "input": tool_input or {"to": "a"}},
        invocation_state=state,
        agent=None,
    )


def _run(**overrides) -> UnattendedRun:
    base = dict(run_id="run-1", user_id=USER, session_id="headless-1", trigger="schedule", schedule_id="sched-1")
    base.update(overrides)
    return UnattendedRun(**base)


class Chain:
    """A stub tool chain: records that it ran, then yields a result, raises or interrupts."""

    def __init__(self, status: str = "success", raise_interrupt: bool = False, raise_error: bool = False):
        self.calls = 0
        self.status = status
        self.raise_interrupt = raise_interrupt
        self.raise_error = raise_error

    async def __call__(self, context) -> Any:
        self.calls += 1
        if self.raise_interrupt:
            raise InterruptException(Interrupt(id="v1:middleware_execute_tool:tu-1:x", name="form", reason={}))
        if self.raise_error:
            raise RuntimeError("tool blew up")
        yield ToolResultEvent({"toolUseId": "tu-1", "status": self.status, "content": [{"text": "done"}]})


async def _drain(middleware, context, chain) -> list:
    return [event async for event in middleware(context, chain)]


async def test_an_interactive_turn_writes_nothing_and_calls_through(table):
    audit = HeadlessActionAudit(recorder=lambda: pytest.fail("an interactive turn must never reach the recorder"))
    chain = Chain()
    events = await _drain(audit.middleware, _context("send", tool=_mcp_tool("send", destructive=True)), chain)
    assert chain.calls == 1 and len(events) == 1
    assert _rows(table) == []


async def test_a_read_only_call_is_not_recorded(table):
    run = _run()
    chain = Chain()
    await _drain(HeadlessActionAudit().middleware, _context("whoami", run=run), chain)
    assert chain.calls == 1 and _rows(table) == [] and run.audited == {}


async def test_a_write_is_recorded_before_it_runs_and_its_outcome_after(table):
    run = _run()
    seen_before = []

    class Watching(Chain):
        async def __call__(self, context):
            seen_before.append([r["outcome"] for r in _rows(table)])
            async for event in super().__call__(context):
                yield event

    chain = Watching()
    await _drain(HeadlessActionAudit().middleware, _context("send", run=run, tool=_mcp_tool("send", destructive=True), tool_input={"to": "x", "body": "hi"}), chain)
    assert seen_before == [["started"]]  # the row existed when the tool ran
    [row] = _rows(table)
    assert row["SK"] == "RUNACT#run-1#tu-1"
    assert row["GSI_PK"] == f"RUNACTS#{USER}#sched-1"
    assert (row["class"], row["why"], row["approval"], row["outcome"], row["rounds"]) == (
        "write", "destructive_hint", "not_required", "ok", 1,
    )
    assert row["serverName"] == "Mail" and row["trigger"] == "schedule" and row["scheduleId"] == "sched-1"
    assert len(row["inputSha256"]) == 64 and row["inputChars"] > 0
    # No input on the row: neither its values nor its keys, only the hash and length.
    assert not {"input", "toolInput", "body"} & set(row)
    assert all(value not in ("x", "hi") for value in row.values())
    assert int(row["ttl"]) > 0
    assert run.audited == {"tu-1": "write"}


async def test_an_error_result_records_error(table):
    run = _run()
    await _drain(HeadlessActionAudit().middleware, _context("list_files", run=run, tool=_mcp_tool("list_files")), Chain(status="error"))
    [row] = _rows(table)
    assert (row["class"], row["outcome"]) == ("unclassified", "error")


async def test_a_raising_tool_records_error_and_re_raises(table):
    with pytest.raises(RuntimeError):
        await _drain(HeadlessActionAudit().middleware, _context("send", run=_run(), tool=_mcp_tool("send", destructive=True)), Chain(raise_error=True))
    assert _rows(table)[0]["outcome"] == "error"


async def test_a_failed_audit_write_returns_the_error_without_calling_the_tool(table):
    class Broken:
        def record_action(self, **kwargs):
            raise RuntimeError("DynamoDB is down")

    run = _run()
    chain = Chain()
    events = await _drain(HeadlessActionAudit(recorder=Broken).middleware, _context("send", run=run, tool=_mcp_tool("send", destructive=True)), chain)
    assert chain.calls == 0  # the tool never ran
    [event] = events
    assert event.tool_result["status"] == "error"
    assert event.tool_result["content"] == [{"text": REFUSED_TEXT}]
    assert run.audited == {}


async def test_a_form_pause_and_replay_keep_one_row_and_a_resume_approval_upgrades_it(table):
    tool = _mcp_tool("create_issue")
    first = _run()
    with pytest.raises(InterruptException):
        await _drain(HeadlessActionAudit().middleware, _context("create_issue", run=first, tool=tool), Chain(raise_interrupt=True))
    [paused] = _rows(table)
    assert (paused["outcome"], paused["rounds"]) == ("started", 1)  # the pause records no outcome

    # The resume, from the bell: the same run (from the snapshot) and the same call.
    resumed = _run(approved=frozenset({"tu-1"}), approved_via="inbox", approving_run_id="resume-9")
    await _drain(HeadlessActionAudit().middleware, _context("create_issue", run=resumed, tool=tool), Chain())
    [row] = _rows(table)
    assert (row["rounds"], row["outcome"], row["approval"], row["approvedVia"], row["approvingRunId"]) == (
        2, "ok", "approved", "inbox", "resume-9",
    )


async def test_an_approval_answered_in_the_conversation_says_so(table):
    run = _run(approved=frozenset({"tu-1"}), approved_via="conversation")
    await _drain(HeadlessActionAudit().middleware, _context("archive", run=run, tool=_mcp_tool("archive")), Chain())
    [row] = _rows(table)
    assert (row["approval"], row["approvedVia"]) == ("approved", "conversation")
    assert "approvingRunId" not in row


async def test_run_now_rows_list_under_their_own_partition_and_listing_is_per_schedule(table):
    await _drain(HeadlessActionAudit().middleware, _context("send", run=_run(schedule_id=None, trigger="run_now"), tool=_mcp_tool("send", destructive=True)), Chain())
    await _drain(HeadlessActionAudit().middleware, _context("send", run=_run(run_id="run-2"), tool=_mcp_tool("send", destructive=True), tool_use_id="tu-9"), Chain())
    assert [a.run_id for a in list_schedule_actions(USER, "sched-1")] == ["run-2"]
    assert [a.run_id for a in list_schedule_actions(USER, "run_now")] == ["run-1"]
    assert list_schedule_actions("someone-else", "sched-1") == []
    [action] = list_schedule_actions(USER, "sched-1")
    assert (action.call_class, action.approval, action.outcome, action.tool_name) == ("write", "not_required", "ok", "send")


# --------------------------------------------------------------------------- the frame


async def test_an_audited_calls_tool_result_frame_says_so():
    run = _run()
    run.audited["tu-1"] = "write"
    state = {RUN_STATE_KEY: run}
    event = {"type": "tool_result", "data": {"tool_result": {"toolUseId": "tu-1", "status": "success"}}}
    _mark_audited(event, state)
    assert event["data"]["audited"] == "write"
    other = {"type": "tool_result", "data": {"tool_result": {"toolUseId": "tu-2"}}}
    _mark_audited(other, state)
    assert "audited" not in other["data"]
    assert audited_class({}, "tu-1") is None


async def test_the_recorder_refuses_without_a_table(monkeypatch):
    monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
    with pytest.raises(RuntimeError):
        RunAuditRecorder(table_name="").record_action(
            user_id=USER, run_id="r", session_id="s", tool_use_id="t", trigger="schedule",
            tool_name="x", call_class="write", why="destructive_hint", approval="not_required",
        )
