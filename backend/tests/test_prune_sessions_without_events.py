"""The one-off 90-day-cliff prune (scripts/prune_sessions_without_events.py).

DynamoDB and S3 are moto. Memory is a dict of which sessions still have events,
because what matters is which rows reach the cascade, and when.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import prune_sessions_without_events as cliff  # noqa: E402

REGION = "us-east-1"
TABLE = "test-sessions-metadata"
BUCKET = "test-conversation-archive"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture()
def table(monkeypatch):
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
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _session(table, session_id: str, last: Optional[str], user_id: str = "user-a", **extra) -> None:
    item: Dict[str, Any] = {
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "userId": user_id,
        "sessionId": session_id,
        "status": "active",
        **extra,
    }
    if last is not None:
        item["lastMessageAt"] = last
    table.put_item(Item=item)


class World:
    """Memory as a set of sessions with events; the archive as real S3."""

    def __init__(self, table) -> None:
        self.table = table
        self.with_events = {"live"}
        self.memory_errors: set = set()
        self.deleted: List[tuple] = []
        self.cascaded: List[tuple] = []
        self.sleeps: List[float] = []
        self.before_recheck = None

    def memory(self, user_id, session_id):
        if session_id in self.memory_errors:
            return None
        return session_id in self.with_events

    def archive(self, user_id, session_id):
        return cliff.archive_has_objects(boto3.client("s3", region_name=REGION), BUCKET, user_id, session_id)

    async def recheck(self, session_id, user_id):
        if self.before_recheck:
            self.before_recheck(session_id)
        return self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"}).get("Item")

    async def delete(self, user_id, session_id):
        self.deleted.append((user_id, session_id))
        return True

    async def cascade(self, user_id, session_id):
        self.cascaded.append((user_id, session_id))
        return []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def run(self, apply=True, **kwargs):
        return asyncio.run(
            cliff.run(
                table=self.table,
                memory=self.memory,
                archive=self.archive,
                recheck=self.recheck,
                delete=self.delete,
                cascade=self.cascade,
                apply=apply,
                now=NOW,
                sleep=self.sleep,
                **kwargs,
            )
        )


@pytest.fixture()
def world(table):
    _session(table, "live", _ago(1))  # the canary: recent, with events
    return World(table)


def _archive(user_id: str, session_id: str) -> None:
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key=f"conversations/{user_id}/{session_id}/000000.json", Body=b"{}"
    )


class TestWhatGoes:
    def test_old_and_empty_everywhere_is_deleted_through_the_cascade(self, world, table):
        _session(table, "empty", _ago(120))

        report = world.run()

        assert world.deleted == [("user-a", "empty")]
        assert world.cascaded == [("user-a", "empty")]
        assert report.deleted == 1

    def test_a_session_with_events_is_kept(self, world, table):
        _session(table, "has-events", _ago(120))
        world.with_events.add("has-events")

        report = world.run()

        assert world.deleted == []
        assert report.held == {"has_events": 1}

    def test_a_session_the_archive_still_holds_is_kept(self, world, table):
        """PR-3's backfill may have rescued it; this never deletes what it saved."""
        _session(table, "rescued", _ago(120))
        _archive("user-a", "rescued")

        report = world.run()

        assert world.deleted == []
        assert report.held == {"archived": 1}

    def test_an_unanswerable_memory_check_keeps_the_session(self, world, table):
        _session(table, "flaky", _ago(120))
        world.memory_errors.add("flaky")

        report = world.run()

        assert world.deleted == []
        assert report.held == {"memory_unverifiable": 1}

    def test_younger_than_the_old_expiry_is_not_considered(self, world, table):
        _session(table, "eighty-days", _ago(80))

        report = world.run()

        assert world.deleted == []
        assert report.old_enough == 0

    @pytest.mark.parametrize(
        "extra,reason",
        [({"lastTurnContinuable": True}, "continuable"), ({"pendingInterrupts": [{"id": "i"}]}, "pending_interrupt")],
    )
    def test_the_pruner_guards_apply(self, world, table, extra, reason):
        _session(table, "guarded", _ago(120), **extra)

        report = world.run()

        assert world.deleted == []
        assert report.held == {reason: 1}

    def test_missing_last_message_at_is_never_old(self, world, table):
        _session(table, "unknown", None)

        world.run()

        assert world.deleted == []


class TestSafety:
    def test_dry_run_reports_and_lists_but_deletes_nothing(self, world, table):
        _session(table, "a", _ago(200))
        _session(table, "b", _ago(120))
        out: List[Dict[str, str]] = []

        report = world.run(apply=False, out=out)

        assert world.deleted == [] and world.cascaded == []
        assert report.mode == "dry-run"
        assert report.candidates == 2
        assert report.oldest_last_message_at == _ago(200)
        assert report.newest_last_message_at == _ago(120)
        assert [row["sessionId"] for row in out] == ["a", "b"]

    def test_a_failed_canary_stops_everything(self, world, table):
        _session(table, "empty", _ago(120))
        world.with_events.clear()  # as if the memory id were wrong

        report = world.run()

        assert report.aborted and "canary" in report.aborted
        assert world.deleted == []

    def test_the_canary_needs_one_of_the_newest_few_not_the_very_newest(self, world, table):
        _session(table, "fresh-no-turns-yet", _ago(0.1))
        _session(table, "empty", _ago(120))

        report = world.run()

        assert report.aborted is None
        assert world.deleted == [("user-a", "empty")]

    def test_events_that_reappear_before_the_delete_keep_the_session(self, world, table):
        _session(table, "empty", _ago(120))
        world.before_recheck = lambda sid: world.with_events.add(sid)

        report = world.run()

        assert world.deleted == []
        assert report.skipped_on_recheck == 1

    def test_one_owner_only(self, world, table):
        _session(table, "mine", _ago(120), user_id="user-a")
        _session(table, "theirs", _ago(120), user_id="user-b")

        world.run(user="user-b")

        assert world.deleted == [("user-b", "theirs")]

    def test_oldest_first_capped_and_paced(self, world, table):
        _session(table, "a", _ago(150))
        _session(table, "b", _ago(300))
        _session(table, "c", _ago(200))

        report = world.run(limit=2, sleep_seconds=0.5)

        assert [sid for _, sid in world.deleted] == ["b", "c"]
        assert world.sleeps == [0.5]
        assert report.limit_reached is True


class TestChecks:
    def test_memory_not_found_means_no_events(self):
        client = MagicMock()
        client.list_events.side_effect = type("E", (Exception,), {})()
        client.list_events.side_effect.response = {"Error": {"Code": "ResourceNotFoundException"}}
        assert cliff.memory_has_events(client, "mem", "u", "s") is False

    def test_any_other_memory_error_is_unknown(self):
        client = MagicMock()
        client.list_events.side_effect = RuntimeError("throttled")
        assert cliff.memory_has_events(client, "mem", "u", "s") is None

    def test_memory_events_present(self):
        client = MagicMock()
        client.list_events.return_value = {"events": [{"eventId": "e"}]}
        assert cliff.memory_has_events(client, "mem", "u", "s") is True
        client.list_events.assert_called_once_with(memoryId="mem", actorId="u", sessionId="s", maxResults=1)

    def test_archive_check(self, table):
        s3 = boto3.client("s3", region_name=REGION)
        assert cliff.archive_has_objects(s3, BUCKET, "user-a", "s1") is False
        _archive("user-a", "s1")
        assert cliff.archive_has_objects(s3, BUCKET, "user-a", "s1") is True
        assert cliff.archive_has_objects(s3, None, "user-a", "s1") is False
        assert cliff.archive_has_objects(s3, "no-such-bucket", "user-a", "s1") is None


class TestEnvironment:
    def test_loads_the_app_api_container_environment(self):
        ecs = MagicMock()
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {"name": "sidecar", "environment": [{"name": "X", "value": "no"}]},
                    {"name": "app-api", "environment": [{"name": "AGENTCORE_MEMORY_ID", "value": "mem-1"}]},
                ]
            }
        }
        assert cliff.load_task_environment(ecs, "p-app-api-task") == {"AGENTCORE_MEMORY_ID": "mem-1"}
        ecs.describe_task_definition.assert_called_once_with(taskDefinition="p-app-api-task")

    def test_keeps_the_operators_aws_settings(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.delenv("CLIFF_TEST_VAR", raising=False)
        cliff.apply_environment({"AWS_REGION": "eu-west-1", "CLIFF_TEST_VAR": "set"})
        assert os.environ["AWS_REGION"] == "us-west-2"
        assert os.environ["CLIFF_TEST_VAR"] == "set"
        monkeypatch.delenv("CLIFF_TEST_VAR")
