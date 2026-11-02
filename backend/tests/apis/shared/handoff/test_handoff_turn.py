"""A handoff on the invocation path (handoffs spec §4.2–§4.3, §10 "handoff_context", "Status writes")."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import turn as turn_module
from apis.shared.handoff.context import build_handoff_message, context_hash, wrap_handoff_context
from apis.shared.handoff.excerpt import render_excerpt
from apis.shared.handoff.models import Handoff, WaitingOn
from apis.shared.handoff.store import HandoffStore
from apis.shared.handoff.turn import (
    HANDOFF_CONTEXT_MAX_CHARS,
    HandoffContextRequest,
    HandoffTurn,
    HandoffTurnObserver,
    TurnOutcome,
)

REGION = "us-west-2"
TABLE = "test-sessions-metadata"
EXCERPT = render_excerpt(
    [
        {"role": "user", "content": [{"text": "plan BIO 101"}]},
        {"role": "assistant", "content": [{"text": "Here is a week-by-week plan."}]},
    ]
).text


def frame(event: str, data: Dict[str, Any] | None = None) -> str:
    return f"event: {event}\ndata: {json.dumps(data or {})}\n\n"


# ---------------------------------------------------------------- observer


class TestObserver:
    def outcome(self, *chunks: str, disconnected: bool = False) -> TurnOutcome:
        observer = HandoffTurnObserver()
        for chunk in chunks:
            observer.observe(chunk)
        return observer.outcome(disconnected=disconnected)

    def test_a_completed_turn_is_done_with_the_latest_tool_summary(self):
        result = self.outcome(
            frame("tool_group_summary", {"summary": "Listed 4 assignments"}),
            frame("tool_group_summary", {"summary": "Found the lab report assignment in BIO 101"}),
            frame("message_stop", {"stopReason": "end_turn"}),
            frame("done"),
        )
        assert result == TurnOutcome("done", status_line="Found the lab report assignment in BIO 101")

    def test_a_completed_turn_without_tools_is_done_with_no_status_line(self):
        assert self.outcome(frame("message_stop", {"stopReason": "end_turn"}), frame("done")) == TurnOutcome("done")

    @pytest.mark.parametrize(
        "event, data, waiting",
        [
            ("oauth_required", {"providerId": "canvas", "interruptId": "i1", "authorizationUrl": "u"},
             WaitingOn(kind="oauth_consent", name="canvas")),
            ("tool_approval_required", {"interruptId": "i1", "toolName": "send_email", "message": "m"},
             WaitingOn(kind="tool_approval", name="send_email")),
            ("user_question_required", {"interruptId": "i1", "questions": [{"header": "Course level"}]},
             WaitingOn(kind="user_question", name="Course level")),
            ("browser_login_required", {"interruptId": "i1", "browserSessionId": "b"},
             WaitingOn(kind="browser_login", name="")),
        ],
    )
    def test_each_pause_is_needs_input_naming_what_it_waits_on(self, event, data, waiting):
        result = self.outcome(frame("message_stop", {"stopReason": "interrupt"}), frame(event, data), frame("done"))
        assert result == TurnOutcome("needs_input", waiting_on=waiting)

    def test_a_pre_flight_consent_paused_nothing(self):
        """No interruptId: the tool never registered and the turn went on (CLAUDE.md, oauth_required)."""
        result = self.outcome(frame("oauth_required", {"providerId": "canvas", "authorizationUrl": "u"}), frame("done"))
        assert result.status == "done"

    def test_a_stream_error_is_stopped(self):
        result = self.outcome(frame("stream_error", {"message": "x"}), frame("done"))
        assert result == TurnOutcome("stopped", stopped_reason="error")

    def test_a_cooperative_stop_is_stopped_by_the_user(self):
        result = self.outcome(frame("message_stop", {"stopReason": "stopped"}), frame("done"))
        assert result == TurnOutcome("stopped", stopped_reason="user_stopped")

    def test_a_dropped_stream_is_stopped_connection_lost(self):
        result = self.outcome(frame("tool_group_summary", {"summary": "x"}), disconnected=True)
        assert result == TurnOutcome("stopped", stopped_reason="connection_lost")

    def test_a_stream_that_never_reached_done_is_stopped(self):
        assert self.outcome(frame("message_start")).status == "stopped"

    def test_several_frames_in_one_chunk_and_junk_are_handled(self):
        chunk = frame("tool_group_summary", {"summary": "Did a thing"}) + "event: done\ndata: {}\n\n" + ": keepalive\n\n"
        observer = HandoffTurnObserver()
        observer.observe(chunk)
        observer.observe("event: tool_group_summary\ndata: {not json\n\n")
        observer.observe("")
        assert observer.outcome(disconnected=False) == TurnOutcome("done", status_line="Did a thing")


# ---------------------------------------------------------------- for_turn


class TestForTurn:
    def build(self, **overrides: Any):
        kwargs: Dict[str, Any] = dict(
            user_id="alice",
            session_id="tgt",
            agent_id="rubric",
            handoff_context=HandoffContextRequest(handoff_id="h1", text=EXCERPT),
            session_row=None,
            is_resume=False,
            is_continuation=False,
            store=HandoffStore(table=object()),
        )
        kwargs.update(overrides)
        return HandoffTurn.for_turn(**kwargs)

    def test_off_ignores_the_field_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("HANDOFFS_ENABLED", raising=False)
        assert self.build() is None
        assert "HANDOFFS_ENABLED is off" in caplog.text

    def test_on_takes_the_request(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        turn = self.build()
        assert turn.request.handoff_id == "h1"
        assert turn.handoff_id == "h1"

    def test_a_turn_with_neither_is_not_a_handoff_turn(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        assert self.build(handoff_context=None) is None

    def test_a_preview_session_is_never_a_target(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        assert self.build(is_preview=True) is None

    @pytest.mark.parametrize("flag", ["is_resume", "is_continuation"])
    def test_a_resume_or_continuation_ignores_the_request(self, monkeypatch, flag):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        assert self.build(**{flag: True}) is None

    def test_a_later_turn_of_a_target_reports_on_its_handoff(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        turn = self.build(handoff_context=None, session_row={"spawnedFrom": {"sessionId": "src", "handoffId": "h0"}})
        assert turn.request is None
        assert turn.handoff_id == "h0"

    def test_a_target_never_takes_a_second_handoff(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        turn = self.build(session_row={"spawnedFrom": {"sessionId": "src", "handoffId": "h0"}})
        assert turn.request is None
        assert turn.handoff_id == "h0"

    def test_a_settled_link_is_no_longer_followed(self, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        row = {"spawnedFrom": {"sessionId": "src", "handoffId": "h0", "settled": True}}
        assert self.build(handoff_context=None, session_row=row) is None


def test_an_oversized_field_is_refused_at_the_schema():
    with pytest.raises(Exception):
        HandoffContextRequest(handoff_id="h1", text="x" * 200_001)


# ---------------------------------------------------------------- acceptance and status, over moto


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
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
def store(table) -> HandoffStore:
    s = HandoffStore(table=table)
    now = datetime.now(timezone.utc).isoformat()
    s.create(
        Handoff(
            handoff_id="h1",
            user_id="alice",
            session_id="src",
            kind="mention",
            agent_id="rubric",
            agent_name="Rubric Builder",
            request="draft a rubric for this",
            source_title="Course planning",
            source_message_index=1,
            excerpt_hash=context_hash(EXCERPT),
            excerpt_tokens=40,
            created_at=now,
            updated_at=now,
        )
    )
    return s


@pytest.fixture()
def meta(monkeypatch) -> Dict[str, List[Any]]:
    """Record the target META writes instead of making them."""
    calls: Dict[str, List[Any]] = {"spawned": [], "settled": []}
    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "set_session_spawned_from", lambda *a: calls["spawned"].append(a) or True)
    monkeypatch.setattr(metadata, "settle_session_spawned_from", lambda *a: calls["settled"].append(a))
    return calls


def first_turn(store: HandoffStore, text: str | None = EXCERPT, agent_id: str = "rubric") -> HandoffTurn:
    turn = HandoffTurn.for_turn(
        user_id="alice",
        session_id="tgt",
        agent_id=agent_id,
        handoff_context=HandoffContextRequest(handoff_id="h1", text=text),
        session_row=None,
        is_resume=False,
        is_continuation=False,
        store=store,
    )
    assert turn is not None
    return turn


async def drain() -> None:
    """Let the background writes finish."""
    pending = list(turn_module._background)
    if pending:
        await asyncio.gather(*pending)


class TestFirstTurn:
    def test_accepts_once_and_returns_the_card_wrapped_with_the_source_title(self, store, meta):
        async def run():
            turn = first_turn(store)
            turn.start()
            block = await turn.context_block()
            await drain()
            return block

        block = asyncio.run(run())

        assert block == wrap_handoff_context(EXCERPT, "Course planning")
        row = store.get("alice", "h1")
        assert (row.status, row.target_session_id, row.context_edited, row.context_removed) == (
            "running", "tgt", False, False,
        )
        assert meta["spawned"] == [("tgt", "alice", "src", "h1")]

    def test_the_model_message_matches_the_review_sheets_preview_byte_for_byte(self, store, meta):
        """Prompt preview parity (§10): app-api's ``prompt`` and the turn's message are one join."""
        request = "draft a rubric for this"
        preview = build_handoff_message(wrap_handoff_context(EXCERPT, "Course planning"), request)

        async def run():
            return build_handoff_message(await first_turn(store).context_block(), request)

        assert asyncio.run(run()) == preview

    def test_an_edited_card_is_recorded_as_edited(self, store, meta):
        edited = EXCERPT.replace("week-by-week", "two-week")
        block = asyncio.run(first_turn(store, text=edited).context_block())

        assert "two-week" in block
        assert store.get("alice", "h1").context_edited is True

    def test_a_card_opened_and_closed_unchanged_is_not_edited(self, store, meta):
        asyncio.run(first_turn(store, text=EXCERPT.replace("\n", "\r\n") + "\n").context_block())
        assert store.get("alice", "h1").context_edited is False

    def test_a_removed_card_still_sends_the_handoff(self, store, meta):
        block = asyncio.run(first_turn(store, text=None).context_block())

        assert block == ""
        row = store.get("alice", "h1")
        assert (row.status, row.context_removed, row.context_edited) == ("running", True, False)

    def test_the_server_re_applies_the_cap(self, store, meta):
        huge = EXCERPT + "\n" + "x" * 100_000
        block = asyncio.run(first_turn(store, text=huge).context_block())

        body = block.split("\n", 2)[2].removesuffix("\n</handoff_context>")
        # A defused closing tag inside the clipped card costs a backslash or two, nothing more.
        assert len(body) <= HANDOFF_CONTEXT_MAX_CHARS + 8
        assert "characters omitted" in block

    def test_a_handoff_for_another_agent_is_ignored(self, store, meta):
        assert asyncio.run(first_turn(store, agent_id="someone-else").context_block()) == ""
        assert store.get("alice", "h1").status == "open"

    def test_a_second_send_gets_no_context(self, store, meta):
        asyncio.run(first_turn(store).context_block())
        second = first_turn(store)
        second.session_id = "tgt-2"

        assert asyncio.run(second.context_block()) == ""
        assert store.get("alice", "h1").target_session_id == "tgt"

    def test_another_users_handoff_id_is_ignored(self, store, meta):
        turn = first_turn(store)
        turn.user_id = "bob"
        assert asyncio.run(turn.context_block()) == ""

    def test_a_store_failure_runs_the_turn_without_context(self, meta, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")

        class _Broken:
            def get(self, *a, **k):
                raise RuntimeError("throttled")

        turn = first_turn(_Broken())  # type: ignore[arg-type]
        assert asyncio.run(turn.context_block()) == ""


class TestStatusWrites:
    def run_turn(self, turn: HandoffTurn, *chunks: str, disconnected: bool = False) -> None:
        async def run():
            turn.start()
            await turn.context_block()
            for chunk in chunks:
                turn.observe(chunk)
            turn.schedule_status_write(disconnected=disconnected)
            await drain()

        asyncio.run(run())

    def later_turn(self, store: HandoffStore) -> HandoffTurn:
        turn = HandoffTurn.for_turn(
            user_id="alice",
            session_id="tgt",
            agent_id="rubric",
            handoff_context=None,
            session_row={"spawnedFrom": {"sessionId": "src", "handoffId": "h1"}},
            is_resume=True,
            is_continuation=False,
            store=store,
        )
        assert turn is not None
        return turn

    def test_a_first_turn_that_pauses_is_needs_input(self, store, meta):
        self.run_turn(
            first_turn(store),
            frame("oauth_required", {"providerId": "canvas", "interruptId": "i1", "authorizationUrl": "u"}),
            frame("done"),
        )
        row = store.get("alice", "h1")
        assert row.status == "needs_input"
        assert row.waiting_on == WaitingOn(kind="oauth_consent", name="canvas")
        assert meta["settled"] == []

    def test_the_resumed_turn_finishes_it_and_settles_the_link(self, store, meta):
        self.run_turn(first_turn(store), frame("user_question_required", {"interruptId": "i", "questions": []}), frame("done"))

        self.run_turn(
            self.later_turn(store),
            frame("tool_group_summary", {"summary": "Drafted a 4-row rubric"}),
            frame("done"),
        )

        row = store.get("alice", "h1")
        assert (row.status, row.status_line) == ("done", "Drafted a 4-row rubric")
        assert meta["settled"] == [("tgt", "alice")]

    def test_a_dropped_first_turn_is_stopped_connection_lost(self, store, meta):
        self.run_turn(first_turn(store), frame("message_start"), disconnected=True)
        row = store.get("alice", "h1")
        assert (row.status, row.stopped_reason) == ("stopped", "connection_lost")

    def test_a_stopped_handoff_follows_the_targets_next_turn(self, store, meta):
        self.run_turn(first_turn(store), disconnected=True)
        self.run_turn(self.later_turn(store), frame("done"))
        assert store.get("alice", "h1").status == "done"

    def test_a_turn_that_did_not_take_the_handoff_writes_nothing(self, store, meta):
        self.run_turn(first_turn(store, agent_id="someone-else"), frame("done"))
        assert store.get("alice", "h1").status == "open"

    def test_a_turn_after_done_settles_rather_than_retrying_forever(self, store, meta):
        self.run_turn(first_turn(store), frame("done"))
        meta["settled"].clear()

        self.run_turn(self.later_turn(store), frame("stream_error"), frame("done"))

        assert store.get("alice", "h1").status == "done"
        assert meta["settled"] == [("tgt", "alice")]

    def test_a_failed_status_write_never_raises(self, meta, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")

        class _Broken:
            def transition(self, *a, **k):
                raise RuntimeError("throttled")

        self.run_turn(self.later_turn(_Broken()), frame("done"))  # type: ignore[arg-type]
