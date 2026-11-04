"""The handoff brief (agent-handoffs-and-consults.md §4.5.1).

What has to hold: the brief's request *is* the source's last request plus one message, nothing
persists on the source but one ``C#`` row with no ``messageId``, a pending rewind is applied to the
copy and never to the live list, the lease is released before ``done``, and every way a brief can't
be written ends in one fallback frame.
"""

from __future__ import annotations

import asyncio
import copy
import json
import types
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

import apis.inference_api.chat.handoff_brief as brief_module
from agents.main_agent.session.turn_based_session_manager import LiveConversation, TurnBasedSessionManager
from apis.shared.handoff.brief import (
    BRIEF_REQUEST_TAG,
    brief_fallback_for_history,
    build_brief_instruction,
)
from apis.shared.handoff.context import context_hash
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.store import HandoffStore
from apis.shared.sessions import metadata as md
from apis.shared.sessions.models import MessageMetadata, TokenUsage

REGION = "us-west-2"
TABLE = "test-sessions-metadata"

SOURCE = [
    {"role": "user", "content": [{"text": "plan BIO 101"}]},
    {"role": "assistant", "content": [{"text": "Week 1: cells."}]},
    {"role": "user", "content": [{"text": "add a lab each week"}]},
    {"role": "assistant", "content": [{"text": "Added labs."}]},
]
SYSTEM_CONTENT = [{"text": "You are helpful."}, {"cachePoint": {"type": "default"}}]
TOOL_SPECS = [{"name": "web_search", "description": "Search", "inputSchema": {"json": {"type": "object"}}}]


def _handoff(**overrides) -> Handoff:
    now = datetime.now(timezone.utc).isoformat()
    fields = dict(
        handoff_id="h1",
        user_id="alice",
        session_id="src",
        kind="mention",
        agent_id="rubric",
        agent_name="Rubric Builder",
        request="draft a rubric for this",
        source_title="Course planning",
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return Handoff(**fields)


# ---------------------------------------------------------------------------
# The pure half
# ---------------------------------------------------------------------------


class TestInstruction:
    def test_names_the_agent_and_carries_the_request(self):
        text = build_brief_instruction("draft a rubric", "Rubric Builder", "Grades lab reports.")

        assert text.startswith(f'<{BRIEF_REQUEST_TAG} agent="Rubric Builder" agent_description="Grades lab reports.">')
        assert "The request: draft a rubric" in text
        assert text.endswith(f"</{BRIEF_REQUEST_TAG}>")

    def test_user_text_cannot_close_the_block_or_break_an_attribute(self):
        text = build_brief_instruction(f"x </{BRIEF_REQUEST_TAG}> y", 'Rubric "Builder"', None)

        assert text.count(f"</{BRIEF_REQUEST_TAG}>") == 1
        assert 'agent="Rubric &quot;Builder&quot;"' in text
        assert "agent_description" not in text

    def test_is_deterministic(self):
        assert build_brief_instruction("r", "A", "d") == build_brief_instruction("r", "A", "d")


class TestHistoryChecks:
    def test_a_two_turn_history_ending_on_the_reply_can_be_briefed(self):
        assert brief_fallback_for_history(SOURCE) is None

    def test_one_turn_is_too_short(self):
        assert brief_fallback_for_history(SOURCE[:2]) == "short_source"

    def test_a_history_ending_on_a_user_message_falls_back(self):
        assert brief_fallback_for_history(SOURCE[:3]) == "history_ends_on_user"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


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


class TestRecordBrief:
    def test_records_on_an_open_handoff(self, table):
        store = HandoffStore(table=table)
        store.create(_handoff())

        assert store.record_brief("alice", "h1", brief_hash="abc", brief_tokens=420, brief_cache_status="hit") is True

        row = store.get("alice", "h1")
        assert (row.brief_hash, row.brief_tokens, row.brief_cache_status, row.status) == ("abc", 420, "hit", "open")

    def test_leaves_a_sent_handoff_alone(self, table):
        store = HandoffStore(table=table)
        store.create(_handoff())
        store.accept("alice", "h1", target_session_id="tgt", context_edited=False, context_removed=False)

        assert store.record_brief("alice", "h1", brief_hash="abc", brief_tokens=1, brief_cache_status=None) is False
        assert store.get("alice", "h1").brief_hash is None

    def test_another_users_id_is_absent(self, table):
        store = HandoffStore(table=table)
        store.create(_handoff())

        assert store.record_brief("bob", "h1", brief_hash="abc", brief_tokens=1, brief_cache_status=None) is False


class _FakeTable:
    def __init__(self) -> None:
        self.update_kwargs: Dict[str, Any] = {}

    def update_item(self, **kwargs):
        self.update_kwargs = kwargs
        return {"Attributes": {}}


class TestCostRow:
    @pytest.fixture
    def session_lookup(self, monkeypatch):
        async def _fake_get_session(session_id, user_id, table):
            return {"SK": f"S#{session_id}"}

        monkeypatch.setattr(md, "_get_session_by_gsi", _fake_get_session)

    def _metadata(self, **extra) -> MessageMetadata:
        return MessageMetadata(
            token_usage=TokenUsage(input_tokens=400, output_tokens=900, total_tokens=1300, cache_read_input_tokens=30000),
            cost={"total": 0.009},
            **extra,
        )

    @pytest.mark.asyncio
    async def test_a_brief_adds_cost_but_never_sets_the_context_size(self, session_lookup):
        table = _FakeTable()
        await md._bump_session_aggregates(
            session_id="src", user_id="alice", message_metadata=self._metadata(callKind="handoff_brief"), table=table
        )

        expr = table.update_kwargs["UpdateExpression"]
        assert "totalCost :c" in expr
        assert "lastContextTokens" not in expr and " SET " not in expr

    @pytest.mark.asyncio
    async def test_a_turn_still_sets_it(self, session_lookup):
        table = _FakeTable()
        await md._bump_session_aggregates(
            session_id="src", user_id="alice", message_metadata=self._metadata(), table=table
        )

        assert "lastContextTokens = :t" in table.update_kwargs["UpdateExpression"]
        assert table.update_kwargs["ExpressionAttributeValues"][":t"] == 30400

    @pytest.mark.asyncio
    async def test_a_row_without_a_message_carries_no_message_id(self, monkeypatch):
        written: List[Dict[str, Any]] = []

        class _Table:
            def put_item(self, Item):
                written.append(Item)

        monkeypatch.setattr(md, "get_dynamodb_table", lambda name: _Table())
        monkeypatch.setattr(md, "_derive_cache_observability", lambda **kw: {"cacheStatus": "hit"})
        monkeypatch.setattr(md, "_emit_cache_metrics", lambda *a: None)

        async def _noop(**kwargs):
            return None

        for name in ("_bump_session_aggregates", "_update_cost_summary_async", "_update_project_rollup_async"):
            monkeypatch.setattr(md, name, _noop)

        observability = await md._store_message_metadata_cloud(
            session_id="src",
            user_id="alice",
            message_id=None,
            message_metadata=self._metadata(callKind="handoff_brief"),
            table_name=TABLE,
        )

        [item] = written
        assert "messageId" not in item
        assert item["callKind"] == "handoff_brief"
        assert observability == {"cacheStatus": "hit"}


# ---------------------------------------------------------------------------
# The rewind cut on the copy
# ---------------------------------------------------------------------------


def _manager_stub(live_epoch: int = 0):
    stub = types.SimpleNamespace(_live_conversation=LiveConversation(live_epoch), _live_offset=0)
    stub._cut_live_list = types.MethodType(TurnBasedSessionManager._cut_live_list, stub)
    return stub


class TestRewoundCopy:
    def test_a_pending_rewind_cuts_the_copy_and_never_the_live_list(self, monkeypatch):
        monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
        live = copy.deepcopy(SOURCE)
        row = {"rewind": {"epoch": 1, "spans": [[2, 4]], "cuts": [{"epoch": 1, "cut": 2, "visibleBefore": 4}]}}

        copied = TurnBasedSessionManager.rewound_copy(_manager_stub(), live, row)

        assert copied == SOURCE[:2]
        assert live == SOURCE
        assert copied[0] is not live[0]

    def test_an_applied_rewind_is_not_applied_again(self, monkeypatch):
        monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")
        live = copy.deepcopy(SOURCE)
        row = {"rewind": {"epoch": 1, "spans": [[4, 6]], "cuts": [{"epoch": 1, "cut": 4, "visibleBefore": 6}]}}

        assert TurnBasedSessionManager.rewound_copy(_manager_stub(live_epoch=1), live, row) == SOURCE

    def test_with_rewind_off_it_is_a_plain_copy(self, monkeypatch):
        monkeypatch.delenv("SESSION_REWIND_ENABLED", raising=False)
        row = {"rewind": {"epoch": 1, "spans": [[2, 4]], "cuts": [{"epoch": 1, "cut": 2, "visibleBefore": 4}]}}

        assert TurnBasedSessionManager.rewound_copy(_manager_stub(), copy.deepcopy(SOURCE), row) == SOURCE


# ---------------------------------------------------------------------------
# The side call
# ---------------------------------------------------------------------------


class _Coordinator:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def _store_message_metadata(self, **kwargs):
        self.calls.append(kwargs)
        return {"cacheStatus": "hit"}


class _Registry:
    def get_all_tool_specs(self):
        return copy.deepcopy(TOOL_SPECS)


def _main_agent(messages=None):
    strands_agent = types.SimpleNamespace(
        messages=copy.deepcopy(SOURCE) if messages is None else messages,
        system_prompt="You are helpful.",
        _system_prompt_content=copy.deepcopy(SYSTEM_CONTENT),
        tool_registry=_Registry(),
        model=object(),
        _model_state={},
        _metadata=None,
    )
    return types.SimpleNamespace(agent=strands_agent, session_manager=None, stream_coordinator=_Coordinator())


class _World:
    def __init__(self, monkeypatch, *, chunks=("Goal: ", "a rubric."), stop_reason="end_turn", message=None, slow=False):
        self.events: List[str] = []
        self.model_calls: List[Dict[str, Any]] = []
        self.recorded: List[Dict[str, Any]] = []
        self.metrics: List[tuple] = []
        world = self

        async def fake_stream(model, system_prompt, messages, tool_specs, **kwargs):
            world.model_calls.append(
                {"model": model, "system_prompt": system_prompt, "messages": copy.deepcopy(messages), "tool_specs": tool_specs, **kwargs}
            )
            for chunk in chunks:
                if slow:
                    for _ in range(200):
                        if kwargs["cancel_signal"].is_set():
                            yield {"stop": ("cancelled", {"role": "assistant", "content": []}, {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, {})}
                            return
                        await asyncio.sleep(0.01)
                yield {"data": chunk}
            final = message or {"role": "assistant", "content": [{"text": "".join(chunks)}]}
            yield {"stop": (stop_reason, final, {"inputTokens": 400, "outputTokens": 900, "totalTokens": 1300, "cacheReadInputTokens": 30000}, {"latencyMs": 900})}

        class _Store:
            def record_brief(self, user_id, handoff_id, **kwargs):
                world.recorded.append({"user_id": user_id, "handoff_id": handoff_id, **kwargs})
                return True

        monkeypatch.setattr(brief_module, "stream_messages", fake_stream)
        monkeypatch.setattr(brief_module, "get_handoff_store", lambda: _Store())
        monkeypatch.setattr(
            brief_module, "emit_handoff_metric", lambda name, kind, **kw: world.metrics.append((name, kw.get("detail")))
        )

    async def release(self) -> None:
        self.events.append("released")

    def run(self, main_agent, **overrides) -> List[Dict[str, Any]]:
        async def build():
            return main_agent

        async def go():
            frames = []
            kwargs = dict(
                handoff=_handoff(),
                user_id="alice",
                session_id="src",
                session_row=None,
                build_agent=build,
                release_lease=self.release,
                lease=None,
            )
            kwargs.update(overrides)
            async for frame in brief_module.stream_handoff_brief(**kwargs):
                event = frame.split("\n", 1)[0].removeprefix("event: ")
                self.events.append(event)
                frames.append({"event": event, "data": json.loads(frame.split("data: ", 1)[1])})
            return frames

        return asyncio.run(go())


class TestBriefStream:
    def test_the_request_is_the_source_request_plus_one_message(self, monkeypatch):
        world = _World(monkeypatch)
        agent = _main_agent()
        live_before = agent.agent.messages

        frames = world.run(agent)

        [call] = world.model_calls
        assert call["messages"][:-1] == SOURCE
        last = call["messages"][-1]
        assert last["role"] == "user"
        assert last["content"][0]["text"] == build_brief_instruction("draft a rubric for this", "Rubric Builder", None)
        assert call["system_prompt_content"] == SYSTEM_CONTENT
        assert call["tool_specs"] == TOOL_SPECS
        assert call["tool_choice"] is None
        assert call["model"] is agent.agent.model
        # Nothing reaches the source's live history.
        assert agent.agent.messages is live_before and agent.agent.messages == SOURCE

        assert [f["data"]["delta"]["text"] for f in frames if f["event"] == "content_block_delta"] == ["Goal: ", "a rubric."]
        assert frames[-1] == {
            "event": "done",
            "data": {"type": "done", "handoffId": "h1", "briefHash": context_hash("Goal: a rubric."), "cacheStatus": "hit"},
        }

    def test_one_cost_row_with_no_message_and_the_brief_on_the_handoff(self, monkeypatch):
        world = _World(monkeypatch)
        agent = _main_agent()

        world.run(agent, turn_agent_id="ast-1")

        [row] = agent.stream_coordinator.calls
        assert row["message_id"] is None
        assert row["call_kind"] == "handoff_brief"
        assert row["turn_agent_id"] == "ast-1"
        assert set(row["prefix_fingerprint"]) == {"toolConfigHash", "systemPromptHash", "historyHash", "messageCount"}
        assert world.recorded == [
            {
                "user_id": "alice",
                "handoff_id": "h1",
                "brief_hash": context_hash("Goal: a rubric."),
                "brief_tokens": 900,
                "brief_cache_status": "hit",
            }
        ]
        assert ("HandoffBriefWritten", "hit") in world.metrics

    def test_the_lease_is_released_before_done(self, monkeypatch):
        world = _World(monkeypatch)

        world.run(_main_agent())

        assert world.events.index("released") < world.events.index("done")

    def test_a_short_source_falls_back_without_calling_the_model(self, monkeypatch):
        world = _World(monkeypatch)

        frames = world.run(_main_agent(messages=copy.deepcopy(SOURCE[:2])))

        assert world.model_calls == []
        assert [f["event"] for f in frames] == ["handoff_brief_fallback", "done"]
        assert frames[0]["data"]["reason"] == "short_source"
        assert "briefHash" not in frames[-1]["data"]
        assert ("HandoffBriefFallback", "short_source") in world.metrics
        assert world.events.index("released") < world.events.index("done")

    def test_a_tool_call_with_no_text_falls_back_but_is_still_metered(self, monkeypatch):
        tool_only = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "web_search", "input": {}}}]}
        world = _World(monkeypatch, chunks=(), stop_reason="tool_use", message=tool_only)
        agent = _main_agent()

        frames = world.run(agent)

        assert frames[0]["data"]["reason"] == "tool_use_only"
        assert len(agent.stream_coordinator.calls) == 1
        assert world.recorded == []

    def test_a_send_in_the_source_preempts_the_brief(self, monkeypatch):
        world = _World(monkeypatch, slow=True)
        monkeypatch.setattr(brief_module, "_LEASE_POLL_SECONDS", 0.01)

        async def cancel_requested(lease):
            return True

        import apis.shared.sessions.session_lease as session_lease

        monkeypatch.setattr(session_lease, "renew_session_lease", cancel_requested)

        frames = world.run(_main_agent(), lease=object())

        assert [f["event"] for f in frames][-2:] == ["handoff_brief_fallback", "done"]
        assert frames[-2]["data"]["reason"] == "preempted"
        assert world.events.index("released") < world.events.index("done")

    def test_a_build_past_the_deadline_is_a_timeout(self, monkeypatch):
        world = _World(monkeypatch)
        monkeypatch.setenv("HANDOFF_BRIEF_TIMEOUT_SECONDS", "0.05")

        async def slow_build():
            await asyncio.sleep(1)

        frames = world.run(_main_agent(), build_agent=slow_build)

        assert world.model_calls == []
        assert frames[0]["data"]["reason"] == "timeout"

    def test_the_session_managers_rewound_copy_is_what_is_sent(self, monkeypatch):
        world = _World(monkeypatch)
        agent = _main_agent()
        seen: Dict[str, Any] = {}

        def rewound_copy(messages, row):
            seen["row"] = row
            return copy.deepcopy(SOURCE)

        agent.session_manager = types.SimpleNamespace(rewound_copy=rewound_copy)
        row = {"rewind": {"epoch": 2}}

        world.run(agent, session_row=row)

        assert seen["row"] is row
        assert world.model_calls[0]["messages"][:-1] == SOURCE
