"""Suggested handoffs (handoffs spec §4.4, PR-0.5): the row, the limits, the chip event and its replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import suggestions
from apis.shared.handoff.models import Handoff, HandoffView
from apis.shared.handoff.replay import load_handoff_replay
from apis.shared.handoff.store import HandoffStore
from apis.shared.handoff.suggestions import (
    LIMIT_REACHED,
    SHOWN,
    SuggestionRefused,
    normalize_suggestion,
    record_suggestion,
    take_suggestions,
)

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
def store(table):
    return HandoffStore(table=table)


@pytest.fixture(autouse=True)
def empty_broker():
    suggestions._pending.clear()
    yield
    suggestions._pending.clear()


ARGS = normalize_suggestion(
    "Draft the BIO 101 rubric",
    "The rubric is its own deliverable.",
    "Draft a four-level rubric for the BIO 101 lab report.",
    "Rubric Builder",
)


def _record(store, *, user="alice", session="src", tool_use="tu-1", turn_count=0, args=ARGS):
    return record_suggestion(
        user_id=user, session_id=session, tool_use_id=tool_use, args=args, turn_count=turn_count, store=store
    )


class TestNormalize:
    def test_trims_and_flattens_display_text(self):
        args = normalize_suggestion("  Draft\nthe rubric ", " Because\n it is separate. ", " Do it. ", "  ")
        assert (args.title, args.tldr, args.prompt, args.agent_hint) == (
            "Draft the rubric",
            "Because it is separate.",
            "Do it.",
            None,
        )

    def test_cuts_long_display_text_but_never_the_prompt(self):
        args = normalize_suggestion("t" * 100, "r" * 300, "p" * 4000)
        assert (len(args.title), len(args.tldr), len(args.prompt)) == (60, 200, 4000)
        with pytest.raises(SuggestionRefused, match="under 4000"):
            normalize_suggestion("t", "r", "p" * 4001)

    @pytest.mark.parametrize("missing", ["title", "tldr", "prompt"])
    def test_refuses_a_missing_field(self, missing):
        values = {"title": "t", "tldr": "r", "prompt": "p", missing: "  "}
        with pytest.raises(SuggestionRefused, match="required"):
            normalize_suggestion(values["title"], values["tldr"], values["prompt"])


class TestRecord:
    def test_writes_an_open_suggested_row_and_queues_its_chip(self, store):
        assert _record(store) == SHOWN

        [row] = store.list_for_session("alice", "src")
        assert (row.kind, row.status, row.agent_id, row.source_message_index) == ("suggested", "open", "", -1)
        assert (row.title, row.tldr, row.agent_hint, row.tool_use_id) == (
            "Draft the BIO 101 rubric",
            "The rubric is its own deliverable.",
            "Rubric Builder",
            "tu-1",
        )
        assert row.request == ARGS.prompt

        [event] = take_suggestions("alice", "src")
        assert event == {
            "sessionId": "src",
            "handoffId": row.handoff_id,
            "toolUseId": "tu-1",
            "title": "Draft the BIO 101 rubric",
            "tldr": "The rubric is its own deliverable.",
            "agentName": "Rubric Builder",
        }
        assert take_suggestions("alice", "src") == []

    def test_the_event_never_carries_the_prompt(self, store):
        _record(store)
        [event] = take_suggestions("alice", "src")
        assert ARGS.prompt not in json.dumps(event)

    def test_no_agent_hint_leaves_agent_name_off_the_event(self, store):
        _record(store, args=normalize_suggestion("t", "r", "p"))
        [event] = take_suggestions("alice", "src")
        assert "agentName" not in event

    def test_one_per_turn(self, store):
        assert _record(store, turn_count=1) == LIMIT_REACHED
        assert store.list_for_session("alice", "src") == []
        assert take_suggestions("alice", "src") == []

    def test_three_per_conversation_counting_dismissed_ones(self, store):
        for index in range(3):
            assert _record(store, tool_use=f"tu-{index}") == SHOWN
        first = store.list_for_session("alice", "src")[0]
        store.transition("alice", first.handoff_id, "dismissed")

        assert _record(store, tool_use="tu-4") == LIMIT_REACHED
        assert len(store.list_for_session("alice", "src")) == 3

    def test_the_limit_is_per_user_on_one_session_id(self, store):
        for index in range(3):
            _record(store, tool_use=f"tu-{index}")
        assert _record(store, user="bob", tool_use="tu-b") == SHOWN

    def test_chips_are_keyed_by_user_and_session(self, store):
        _record(store)
        assert take_suggestions("bob", "src") == []
        assert take_suggestions("alice", "other") == []
        assert len(take_suggestions("alice", "src")) == 1

    def test_mentions_do_not_count_toward_the_limit(self, store):
        now = datetime.now(timezone.utc).isoformat()
        for index in range(3):
            store.create(
                Handoff(
                    handoff_id=f"m{index}", user_id="alice", session_id="src", kind="mention",
                    agent_id="a", created_at=now, updated_at=now,
                )
            )
        assert _record(store) == SHOWN

    def test_without_a_table_the_model_is_told_to_say_it_instead(self):
        assert record_suggestion(
            user_id="alice", session_id="src", tool_use_id="tu", args=ARGS, turn_count=0, store=HandoffStore(table=None)
        ) == suggestions.UNAVAILABLE


class TestRowSemantics:
    def test_an_old_suggestion_is_still_open_while_an_old_mention_is_abandoned(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        base = dict(handoff_id="h", user_id="u", session_id="s", agent_id="", created_at=old, updated_at=old)
        assert Handoff(kind="suggested", **base).effective_status() == "open"
        assert Handoff(kind="mention", **base).effective_status() == "abandoned"

    def test_start_gives_the_row_its_agent_and_card_and_can_run_again(self, store):
        _record(store)
        [row] = store.list_for_session("alice", "src")

        started = store.start_suggested(
            "alice", row.handoff_id, agent_id="rubric", agent_name="Rubric Builder", source_title="Course planning",
            source_message_index=5, excerpt_hash="x1", excerpt_tokens=100,
            turns_covered={"first": 1, "last": 3, "of": 3, "omitted": 0}, source_project_id=None,
        )
        assert (started.status, started.agent_id, started.source_message_index, started.excerpt_hash) == (
            "open", "rubric", 5, "x1"
        )
        again = store.start_suggested(
            "alice", row.handoff_id, agent_id="other", agent_name="Other", source_title="Course planning",
            source_message_index=5, excerpt_hash="x2", excerpt_tokens=120, turns_covered=None, source_project_id=None,
        )
        assert (again.agent_id, again.excerpt_hash, again.turns_covered) == ("other", "x2", None)

    def test_start_refuses_a_mention_a_sent_suggestion_or_another_users_row(self, store):
        now = datetime.now(timezone.utc).isoformat()
        store.create(Handoff(handoff_id="m", user_id="alice", session_id="src", kind="mention", agent_id="a",
                             created_at=now, updated_at=now))
        _record(store)
        suggested = next(r for r in store.list_for_session("alice", "src") if r.kind == "suggested")
        kwargs = dict(agent_id="a", agent_name="A", source_title="", source_message_index=0, excerpt_hash="",
                      excerpt_tokens=0, turns_covered=None, source_project_id=None)

        assert store.start_suggested("alice", "m", **kwargs) is None
        assert store.start_suggested("bob", suggested.handoff_id, **kwargs) is None
        store.start_suggested("alice", suggested.handoff_id, **kwargs)
        store.accept("alice", suggested.handoff_id, target_session_id="t", context_edited=False, context_removed=False)
        assert store.start_suggested("alice", suggested.handoff_id, **kwargs) is None


class TestReplay:
    def test_an_open_chip_replays_without_its_prompt_and_a_dismissed_one_does_not(self, store, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        _record(store, tool_use="tu-1")
        _record(store, tool_use="tu-2", turn_count=0)
        rows = store.list_for_session("alice", "src")
        store.transition("alice", rows[1].handoff_id, "dismissed")

        views, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=store))

        assert [v["toolUseId"] for v in views] == ["tu-1"]
        chip = views[0]
        assert (chip["kind"], chip["status"], chip["title"], chip["agentHint"]) == (
            "suggested", "open", "Draft the BIO 101 rubric", "Rubric Builder"
        )
        assert chip["request"] == ""

    def test_a_sent_suggestion_replays_as_a_row_with_its_request(self, store, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        _record(store)
        [row] = store.list_for_session("alice", "src")
        store.start_suggested(
            "alice", row.handoff_id, agent_id="rubric", agent_name="Rubric Builder", source_title="t",
            source_message_index=3, excerpt_hash="x", excerpt_tokens=1, turns_covered=None, source_project_id=None,
        )
        store.accept("alice", row.handoff_id, target_session_id="tgt", context_edited=False, context_removed=False)

        views, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=store))

        [view] = views
        assert (view["status"], view["targetSessionId"], view["request"]) == ("running", "tgt", ARGS.prompt)

    def test_the_view_carries_the_chip_fields(self):
        now = datetime.now(timezone.utc).isoformat()
        row = Handoff(handoff_id="h", user_id="u", session_id="s", kind="suggested", agent_id="", created_at=now,
                      updated_at=now, title="T", tldr="R", agent_hint="A", tool_use_id="tu")
        dumped = HandoffView.from_row(row).model_dump(by_alias=True, exclude_none=True)
        assert {k: dumped[k] for k in ("title", "tldr", "agentHint", "toolUseId")} == {
            "title": "T", "tldr": "R", "agentHint": "A", "toolUseId": "tu"
        }


class TestTool:
    @pytest.fixture()
    def on(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        monkeypatch.setenv("HANDOFF_SUGGESTIONS_ENABLED", "true")

    def _names(self, enabled_tools):
        from apis.inference_api.chat.routes import _build_suggest_handoff_tools

        return [t.tool_name for t in _build_suggest_handoff_tools(enabled_tools, "s1", "u1")]

    def test_the_catalog_id_builds_it(self, on):
        assert self._names(["web_search", "suggest_handoff"]) == ["suggest_handoff"]

    @pytest.mark.parametrize("enabled_tools", [None, [], ["web_search"]])
    def test_without_the_id_nothing_is_built(self, on, enabled_tools):
        assert self._names(enabled_tools) == []

    @pytest.mark.parametrize(
        "env", [{"HANDOFF_SUGGESTIONS_ENABLED": ""}, {"HANDOFF_SUGGESTIONS_ENABLED": "yes"}, {"HANDOFFS_ENABLED": ""}]
    )
    def test_either_flag_off_builds_nothing(self, on, monkeypatch, env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert self._names(["suggest_handoff"]) == []

    def test_the_spec_is_pinned(self):
        """The spec sits in the cached ``toolConfig`` of every turn that has it, so a change is one
        deliberate prefix rewrite per deployment. If a change is intended, re-measure with
        scripts/measure_tool_spec_tokens.py, state the growth in the PR, and update the digest."""
        from agents.builtin_tools.suggest_handoff_tool import make_suggest_handoff_tool

        spec = make_suggest_handoff_tool("s", "u").tool_spec
        assert "enum" not in json.dumps(spec)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert digest == "819628e0735f64a79dbd6f0ab2371279643835fef0ea4625ec9d264c216c7efa"

    def test_the_family_is_cache_key_described(self):
        from apis.shared.tools.injected import INJECTED_TOOL_IDS, injected_tools_are_key_described

        assert "suggest_handoff" in INJECTED_TOOL_IDS
        assert injected_tools_are_key_described(["suggest_handoff", "create_artifact"])

    def test_a_call_writes_the_row_and_the_second_call_in_a_turn_is_limited(self, on, store, monkeypatch):
        from agents.builtin_tools import suggest_handoff_tool as module

        monkeypatch.setattr(suggestions, "get_handoff_store", lambda: store)
        tool = module.make_suggest_handoff_tool("src", "alice")
        state: dict = {}

        def call(tool_use_id):
            context = type("Ctx", (), {"invocation_state": state, "tool_use": {"toolUseId": tool_use_id}})()
            return asyncio.run(
                tool._tool_func(title="Draft it", tldr="Separate.", prompt="Draft it.", tool_context=context)
            )

        first = call("tu-1")
        second = call("tu-2")

        assert first["content"][0]["text"] == SHOWN and first["status"] == "success"
        assert second["content"][0]["text"] == LIMIT_REACHED
        assert [r.tool_use_id for r in store.list_for_session("alice", "src")] == ["tu-1"]

    def test_a_refused_argument_is_an_error_the_model_can_fix(self, on):
        from agents.builtin_tools.suggest_handoff_tool import make_suggest_handoff_tool

        tool = make_suggest_handoff_tool("src", "alice")
        context = type("Ctx", (), {"invocation_state": {}, "tool_use": {"toolUseId": "tu"}})()
        result = asyncio.run(tool._tool_func(title="", tldr="r", prompt="p", tool_context=context))
        assert result["status"] == "error" and "required" in result["content"][0]["text"]


def test_the_coordinator_drains_chips_as_handoff_suggested_events(store):
    from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

    _record(store)
    frames = StreamCoordinator._drain_handoff_suggestions("src", "alice")

    [frame] = frames
    assert frame.startswith("event: handoff_suggested\ndata: ")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["type"] == "handoff_suggested" and payload["toolUseId"] == "tu-1"
    assert StreamCoordinator._drain_handoff_suggestions("src", "alice") == []
