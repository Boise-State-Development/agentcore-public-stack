"""The conversation archive backfill (scripts/backfill_conversation_archive.py).

DynamoDB (with ``SessionLookupIndex``) and S3 are moto. Memory is a dict of
histories, because what matters is what lands in the archive, and that it is
the same bytes the runtime writes for the same history.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_conversation_archive as backfill  # noqa: E402

from apis.shared.conversation_archive import ArchivedTurn, archive_key, drain_pending  # noqa: E402

REGION = "us-east-1"
TABLE = "test-sessions-metadata"
BUCKET = "test-conversation-archive"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _ago(days: float = 0, minutes: float = 0) -> str:
    return (NOW - timedelta(days=days, minutes=minutes)).isoformat()


def _user(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text: str) -> Dict[str, Any]:
    return {"role": "assistant", "content": [{"text": text}]}


_TOOL_CALL = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x", "input": {}}}]}
_TOOL_RESULT = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": [{"text": "raw tool data"}]}}]}


def _history(*messages: Dict[str, Any], start_days_ago: float = 30) -> List[backfill.HistoryMessage]:
    """Messages one minute apart, starting ``start_days_ago``."""
    return [(m, _ago(days=start_days_ago, minutes=-i)) for i, m in enumerate(messages)]


@pytest.fixture()
def world(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")
            ],
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
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        yield World(boto3.resource("dynamodb", region_name=REGION).Table(TABLE), s3)


class World:
    def __init__(self, table, s3) -> None:
        self.table = table
        self.s3 = s3
        self.histories: Dict[str, List[backfill.HistoryMessage]] = {}
        self.memory_errors: set = set()
        self.sleeps: List[float] = []

    def session(self, session_id: str, history=None, user_id: str = "user-a", status: str = "active",
                last: Optional[str] = None, created: Optional[str] = None, **extra) -> None:
        self.table.put_item(Item={
            "PK": f"USER#{user_id}",
            "SK": f"S#{session_id}",
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": "META",
            "userId": user_id,
            "sessionId": session_id,
            "status": status,
            "createdAt": created or _ago(days=30),
            "lastMessageAt": last or _ago(days=1),
            **extra,
        })
        if history is not None:
            self.histories[session_id] = history

    def display_text(self, session_id: str, message_id: int, text: str, user_id: str = "user-a") -> None:
        self.table.put_item(Item={
            "PK": f"USER#{user_id}", "SK": f"D#{session_id}#{message_id}",
            "GSI_PK": f"SESSION#{session_id}", "GSI_SK": f"D#{message_id}",
            "userId": user_id, "sessionId": session_id, "messageId": message_id, "displayText": text,
        })

    def cost_row(self, session_id: str, message_id: int, user_id: str = "user-a", **ids) -> None:
        self.table.put_item(Item={
            "PK": f"USER#{user_id}", "SK": f"C#{_ago()}#{message_id}",
            "GSI_PK": f"SESSION#{session_id}", "GSI_SK": f"C#{_ago()}#{message_id}",
            "userId": user_id, "sessionId": session_id, "messageId": message_id, **ids,
        })

    def history(self, user_id: str, session_id: str):
        if session_id in self.memory_errors:
            raise RuntimeError("memory unavailable")
        return self.histories.get(session_id, [])

    def keys(self) -> List[str]:
        return sorted(o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))

    def body(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def run(self, apply: bool = False, **overrides) -> backfill.BackfillReport:
        kwargs: Dict[str, Any] = dict(
            rows=backfill.scan_live_sessions(self.table),
            history=self.history,
            context=lambda u, s: backfill.load_turn_context(self.table, u, s),
            existing=lambda u, s: backfill.existing_keys(self.s3, BUCKET, u, s),
            put=lambda turn: backfill.put_if_absent(self.s3, BUCKET, turn),
            apply=apply,
            now=NOW,
            sleep_seconds=0.2,
            sleep=self.sleeps.append,
        )
        kwargs.update(overrides)
        return backfill.run(**kwargs)


TWO_TURNS = _history(_user("first question"), _assistant("first answer"), _user("second"), _assistant("second answer"))


# ── dry run, apply, idempotence ──────────────────────────────────────────────
def test_a_dry_run_reports_and_writes_nothing(world):
    world.session("s1", TWO_TURNS)
    report = world.run()

    assert world.keys() == []
    assert report.mode == "dry-run"
    assert (report.sessions_with_turns, report.turns, report.to_write, report.written) == (1, 2, 2, 0)
    assert report.bytes_to_write > 0
    # One read and two puts an apply would make, at 0.2 s each.
    assert report.estimated_apply_minutes == pytest.approx(3 * 0.2 / 60)


def test_apply_writes_one_object_per_turn_and_a_rerun_writes_nothing(world):
    world.session("s1", TWO_TURNS)
    first = world.run(apply=True)

    assert world.keys() == [archive_key("user-a", "s1", 0), archive_key("user-a", "s1", 2)]
    assert first.written == 2
    turn = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 2)))
    assert (turn.user_text, turn.assistant_text) == ("second", "second answer")

    again = world.run(apply=True)
    assert (again.turns, again.already_archived, again.to_write, again.written) == (2, 2, 0, 0)


def test_an_existing_object_is_never_overwritten(world):
    world.session("s1", TWO_TURNS)
    key = archive_key("user-a", "s1", 0)
    world.s3.put_object(Bucket=BUCKET, Key=key, Body=b"written by the runtime")

    report = world.run(apply=True)

    assert world.body(key) == b"written by the runtime"
    assert (report.already_archived, report.written) == (1, 1)


def test_a_turn_archived_between_the_listing_and_the_put_is_left_alone(world):
    """The runtime can finish a turn mid-pass; the conditional put loses the race politely."""
    world.session("s1", TWO_TURNS)
    key = archive_key("user-a", "s1", 0)
    world.s3.put_object(Bucket=BUCKET, Key=key, Body=b"fresh from the runtime")

    report = world.run(apply=True, existing=lambda u, s: set())

    assert world.body(key) == b"fresh from the runtime"
    assert (report.written, report.raced) == (1, 1)


def test_sleeps_after_every_read_and_every_put(world):
    world.session("s1", TWO_TURNS)
    world.run(apply=True)
    assert world.sleeps == [0.2, 0.2, 0.2]


# ── which sessions ───────────────────────────────────────────────────────────
def test_user_scopes_the_pass_to_one_owner(world):
    world.session("mine", TWO_TURNS, user_id="user-a")
    world.session("theirs", TWO_TURNS, user_id="user-b")

    report = world.run(apply=True, user="user-b")

    assert world.keys() == [archive_key("user-b", "theirs", 0), archive_key("user-b", "theirs", 2)]
    assert report.sessions_read == 1


def test_preview_sessions_are_never_archived(world):
    world.session("preview-abc", TWO_TURNS)
    report = world.run(apply=True)
    assert world.keys() == []
    assert report.held == {"preview": 1}


def test_archived_sessions_are_backfilled_and_deleted_ones_are_not(world):
    world.session("kept", TWO_TURNS, status="archived")
    world.session("gone", TWO_TURNS, status="deleted", deleted=True)

    world.run(apply=True)

    assert {key.split("/")[2] for key in world.keys()} == {"kept"}


def test_a_session_that_just_moved_waits_for_the_next_run(world):
    world.session("busy", TWO_TURNS, last=_ago(minutes=3))
    report = world.run(apply=True)
    assert world.keys() == []
    assert report.held == {"recently_active": 1}


def test_sessions_without_events_are_counted_not_written(world):
    world.session("expired")
    report = world.run(apply=True)
    assert (report.sessions_read, report.sessions_without_events, report.written) == (1, 1, 0)


def test_oldest_sessions_go_first_and_limit_caps_the_run(world):
    world.session("new", TWO_TURNS, created=_ago(days=5))
    world.session("old", TWO_TURNS, created=_ago(days=80))

    report = world.run(apply=True, limit=1)

    assert {key.split("/")[2] for key in world.keys()} == {"old"}
    assert report.limit_reached


def test_a_failed_memory_read_skips_that_session_only(world):
    world.session("broken", TWO_TURNS)
    world.session("fine", TWO_TURNS)
    world.memory_errors.add("broken")

    report = world.run(apply=True)

    assert {key.split("/")[2] for key in world.keys()} == {"fine"}
    assert report.failed_sessions == 1


def test_out_lists_the_plan_per_session(world):
    world.session("s1", TWO_TURNS, status="archived")
    plan: List[Dict[str, Any]] = []
    world.run(out=plan)
    assert plan == [{"userId": "user-a", "sessionId": "s1", "status": "archived", "turns": 2, "toWrite": 2}]


# ── what each object holds ───────────────────────────────────────────────────
def test_the_users_words_come_from_display_text_and_never_the_injected_context(world):
    history = _history(
        {"role": "user", "content": [{"text": "<user_context>likes tea</user_context>"}, {"text": "hello\n\n[Attached files: a.pdf]"}]},
        _assistant("hi"),
        _user("<kb excerpts>... what is BIO 101?"),
        _assistant("A course."),
    )
    world.session("s1", history)
    world.display_text("s1", 2, "what is BIO 101?")

    world.run(apply=True)

    first = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 0)))
    second = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 2)))
    assert first.user_text == "hello"
    assert second.user_text == "what is BIO 101?"


def test_another_users_display_text_row_is_ignored(world):
    world.session("s1", _history(_user("mine"), _assistant("ok")))
    world.display_text("s1", 0, "not theirs", user_id="user-b")
    world.run(apply=True)
    assert ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 0))).user_text == "mine"


def test_project_and_agent_come_from_the_turns_cost_rows_then_preferences(world):
    world.session(
        "s1", TWO_TURNS, preferences={"projectId": "proj-1", "assistantId": "ast-session"}
    )
    world.cost_row("s1", 3, turnAgentId="ast-mentioned")

    world.run(apply=True)

    first = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 0)))
    second = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 2)))
    assert (first.project_id, first.assistant_id) == ("proj-1", "ast-session")
    assert (second.project_id, second.assistant_id) == ("proj-1", "ast-mentioned")


def test_created_at_is_the_time_the_turn_ended(world):
    world.session("s1", TWO_TURNS)
    world.run(apply=True)
    turn = ArchivedTurn.from_json(world.body(archive_key("user-a", "s1", 0)))
    assert turn.created_at == TWO_TURNS[1][1]


def test_a_fork_is_archived_under_the_forker_from_its_own_history(world):
    """A fork's copied events already carry the display text; positions count from 0."""
    world.session("fork-1", _history(_user("shared question"), _assistant("shared answer")), user_id="forker")
    world.run(apply=True)
    turn = ArchivedTurn.from_json(world.body(archive_key("forker", "fork-1", 0)))
    assert (turn.user_id, turn.session_id, turn.user_text) == ("forker", "fork-1", "shared question")


# ── byte-identical to the runtime ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_objects_are_byte_identical_to_what_the_runtime_writes(world, monkeypatch):
    """Replay the runtime's after-``done`` archive over each turn of a history and
    compare its bytes, key by key, with what the backfill writes for the whole
    history. Covers tool round-trips, a mid-turn steer, injected context, an
    augmented prompt with its displayText row, and an @-mention turn."""
    from apis.shared.conversation_archive import live

    messages = [
        {"role": "user", "content": [{"text": "<user_context>prefers metric</user_context>"}, {"text": "plan a trip"}]},
        _TOOL_CALL,
        _TOOL_RESULT,
        _assistant("Here is a plan."),
        _user("<kb excerpts> ... what is BIO 101?\n\n[Attached files: syllabus.pdf]"),
        _assistant("A course."),
        _user("look it up"),
        _TOOL_CALL,
        {"role": "user", "content": [_TOOL_RESULT["content"][0], {"text": "also check BIO 102"}]},
        _assistant("Both found."),
    ]
    history = _history(*messages)
    display = {4: "what is BIO 101?"}
    agents = {0: "ast-session", 4: "ast-session", 6: "ast-mentioned"}

    world.session("s1", history, preferences={"projectId": "proj-1", "assistantId": "ast-session"})
    world.display_text("s1", 4, display[4])
    world.cost_row("s1", 9, turnAgentId="ast-mentioned")
    world.run(apply=True)

    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    runtime: Dict[str, bytes] = {}

    async def capture(turns):
        runtime.update({t.key: t.to_json() for t in turns})
        return len(turns)

    for start, end in [(0, 4), (4, 6), (6, 10)]:
        ended_at = datetime.fromisoformat(history[end - 1][1])
        clock = MagicMock(wraps=datetime)
        clock.now.return_value = ended_at
        with patch.object(live, "write_turns", capture), patch.object(live, "datetime", clock):
            assert live.schedule_turn_archive(
                messages=messages[:end],
                user_id="user-a",
                session_id="s1",
                last_message_index=end - 1,
                turn_first_index=start,
                original_message=display.get(start),
                project_id="proj-1",
                assistant_id=agents[start],
            )
            await drain_pending()

    assert sorted(runtime) == world.keys()
    for key, body in runtime.items():
        assert world.body(key) == body, key


# ── Memory reader ────────────────────────────────────────────────────────────
def test_read_history_decodes_events_like_the_runtime():
    from strands.types.session import SessionMessage

    stored = SessionMessage.from_message(_user("hello"), index=0)
    client = MagicMock()
    client.list_events.return_value = [
        {"payload": [{"conversational": {"content": {"text": json.dumps(stored.to_dict())}, "role": "USER"}}]}
    ]

    history = backfill.read_history(client, "mem", "user-a", "s1")

    assert history == [(_user("hello"), stored.created_at)]
    assert client.list_events.call_args.kwargs["max_results"] == backfill.MAX_EVENTS


def test_read_history_unknown_session_is_empty_and_other_errors_raise():
    client = MagicMock()
    client.list_events.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "ListEvents")
    assert backfill.read_history(client, "mem", "u", "s") == []

    client.list_events.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "ListEvents")
    with pytest.raises(ClientError):
        backfill.read_history(client, "mem", "u", "s")


# ── CLI guards ───────────────────────────────────────────────────────────────
def test_apply_requires_the_confirm_prefix():
    with pytest.raises(SystemExit):
        backfill.main(["--project-prefix", "dev-x", "--apply"])
    with pytest.raises(SystemExit):
        backfill.main(["--project-prefix", "dev-x", "--apply", "--confirm-prefix", "prod-x"])


def _deployment(monkeypatch, index_enabled: str) -> Dict[str, Any]:
    """A deployment whose app-api environment has the index flag set as given; ``run`` captured."""
    import bedrock_agentcore.memory
    import prune_sessions_without_events as prune

    environment = {
        "CONVERSATION_INDEX_ENABLED": index_enabled,
        "AGENTCORE_MEMORY_ID": "mem",
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": TABLE,
        "CONVERSATION_ARCHIVE_BUCKET_NAME": BUCKET,
    }
    for name in environment:  # restored after the test, though main() overwrites them
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(prune, "load_task_environment", lambda ecs, family: environment)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: MagicMock())
    monkeypatch.setattr(bedrock_agentcore.memory, "MemoryClient", MagicMock())
    captured: Dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> backfill.BackfillReport:
        captured.update(kwargs)
        return backfill.BackfillReport(mode="apply" if kwargs["apply"] else "dry-run")

    monkeypatch.setattr(backfill, "run", fake_run)
    return captured


def test_apply_is_refused_where_the_deployment_has_indexing_off(monkeypatch, capsys):
    captured = _deployment(monkeypatch, "false")
    with pytest.raises(SystemExit):
        backfill.main(["--project-prefix", "dev-x", "--apply", "--confirm-prefix", "dev-x"])
    assert "CONVERSATION_INDEX_ENABLED off" in capsys.readouterr().err
    assert captured == {}


def test_archive_without_index_lets_apply_run_where_indexing_is_off(monkeypatch):
    captured = _deployment(monkeypatch, "false")
    argv = ["--project-prefix", "dev-x", "--apply", "--confirm-prefix", "dev-x", "--archive-without-index"]
    assert backfill.main(argv) == 0
    assert captured["apply"] is True


def test_archive_without_index_still_needs_the_confirm_prefix(monkeypatch):
    captured = _deployment(monkeypatch, "false")
    with pytest.raises(SystemExit):
        backfill.main(["--project-prefix", "dev-x", "--apply", "--archive-without-index"])
    assert captured == {}


def test_a_dry_run_needs_no_override_where_indexing_is_off(monkeypatch):
    captured = _deployment(monkeypatch, "false")
    assert backfill.main(["--project-prefix", "dev-x"]) == 0
    assert captured["apply"] is False
