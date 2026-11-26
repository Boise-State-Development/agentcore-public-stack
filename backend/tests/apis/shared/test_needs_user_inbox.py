"""Needs-user inbox rows (docs/specs/answer-from-inbox.md §3.3, §3.8).

The display payload built from breadcrumbs, the badge that ignores an expired wait, and the
list's lazy expiry. moto DynamoDB with the real notifications code.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from apis.shared.notifications import NotificationService
from apis.shared.notifications.models import Notification
from apis.shared.scheduled_prompts.needs_user import (
    MAX_HEADLINE_CHARS,
    MAX_INBOX_PROMPTS,
    clip_line,
    live_pending_prompts,
    needs_user_kind,
    needs_user_payload,
    normalize_expiry,
)
from apis.shared.sessions.models import PendingInterrupt

REGION = "us-west-2"
TABLE = "test-projects"
EMAIL = "member@example.edu"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _crumb(interrupt_id: str, kind: str = "tool_approval", **fields) -> dict:
    return {"interruptId": interrupt_id, "kind": kind, "createdAt": "2026-11-25T01:00:00Z", **fields}


def _row(*crumbs: dict, expires: datetime = NOW + timedelta(days=7)) -> dict:
    return {"pendingInterrupts": list(crumbs), "pausedTurn": {"expiresAt": _iso(expires)}}


class TestPrompts:
    def test_only_the_runs_own_live_prompts_count(self):
        row = _row(
            _crumb("a", toolName="send_message"),
            _crumb("o", kind="oauth", providerId="github"),
            _crumb("other", toolName="something_else"),
        )
        prompts, expires_at = live_pending_prompts(row, ["a", "o", "missing"], now=NOW)
        assert [p.interrupt_id for p in prompts] == ["a"]
        assert expires_at == "2026-12-02T12:00:00Z"

    def test_an_expired_or_missing_snapshot_is_no_pause(self):
        assert live_pending_prompts(_row(_crumb("a"), expires=NOW - timedelta(seconds=1)), ["a"], now=NOW) == ([], None)
        assert live_pending_prompts({"pendingInterrupts": [_crumb("a")]}, ["a"], now=NOW) == ([], None)
        assert live_pending_prompts(None, ["a"], now=NOW) == ([], None)

    def test_kinds(self):
        approval = PendingInterrupt.model_validate(_crumb("a"))
        question = PendingInterrupt.model_validate(_crumb("q", kind="user_question"))
        assert needs_user_kind([approval, approval]) == "agent_needs_approval"
        assert needs_user_kind([approval, question]) == "agent_needs_input"

    def test_the_payload_holds_display_lines_only(self):
        form = _crumb(
            "f",
            kind="mcp_elicitation",
            elicitation=json.dumps({"serverName": "GitHub", "message": "Pick a repo " * 30, "fields": [{"name": "x"}]}),
        )
        prompts = [PendingInterrupt.model_validate(c) for c in (
            _crumb("a", toolName="send_message", toolInput='{"body": "secret draft"}', message="Send?"),
            form,
        )]
        payload = needs_user_payload(
            schedule_id="s1", run_id="run-1", run_finished_at="2026-11-25T01:00:00Z", session_id="headless-1",
            label="Watcher", expires_at="2026-12-02T12:00:00Z", prompts=prompts,
        )
        assert payload["answerable"] is True
        assert payload["prompts"][0] == {"interruptId": "a", "kind": "tool_approval", "toolName": "send_message"}
        assert payload["prompts"][1]["serverName"] == "GitHub"
        assert len(payload["prompts"][1]["headline"]) == MAX_HEADLINE_CHARS
        text = json.dumps(payload)
        assert "secret draft" not in text and "Send?" not in text and '"fields"' not in text

    def test_more_than_five_prompts_is_listed_but_not_answerable(self):
        prompts = [PendingInterrupt.model_validate(_crumb(f"i{n}")) for n in range(MAX_INBOX_PROMPTS + 1)]
        payload = needs_user_payload(
            schedule_id="s", run_id="r", run_finished_at="t", session_id="headless-1",
            label=None, expires_at=None, prompts=prompts,
        )
        assert len(payload["prompts"]) == MAX_INBOX_PROMPTS
        assert payload["answerable"] is False and "expiresAt" not in payload

    def test_clip_and_expiry_formats(self):
        assert clip_line("  two\n lines  ", 50) == "two lines"
        assert clip_line("abcdef", 4) == "abc…"
        assert clip_line("   ", 10) is None
        assert normalize_expiry("2026-12-02T12:00:00.123456+00:00") == "2026-12-02T12:00:00Z"
        assert normalize_expiry("not a date") is None


@pytest.fixture()
def service(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield NotificationService(table_name=TABLE)


def _wait(service: NotificationService, expires_at: str) -> Notification:
    return service.notify_own(
        recipient_email=EMAIL,
        kind="agent_needs_approval",
        payload={"sessionId": "headless-1", "expiresAt": expires_at, "prompts": []},
    )


class TestInbox:
    def test_an_expired_wait_never_holds_the_badge_up(self, service):
        _wait(service, "2026-12-02T12:00:00Z")
        _wait(service, "2026-11-25T11:59:59Z")
        service.notify_own(recipient_email=EMAIL, kind="agent_item", payload={"title": "x"})
        assert service.unread_count(EMAIL, now="2026-11-25T12:00:00Z") == 2

    def test_the_list_marks_expired_waits_and_reads_them(self, service):
        live = _wait(service, "2026-12-02T12:00:00Z")
        lapsed = _wait(service, "2026-11-25T11:59:59Z")
        items, _ = service.list(EMAIL)

        assert service.expire_lapsed(EMAIL, items, now="2026-11-25T12:00:00Z") == 1
        by_id = {n.notification_id: n for n in items}
        assert by_id[lapsed.notification_id].resolution.state == "expired"
        assert by_id[lapsed.notification_id].read_at
        assert by_id[live.notification_id].resolution is None

        stored, _ = service.list(EMAIL)
        stored_by_id = {n.notification_id: n for n in stored}
        assert stored_by_id[lapsed.notification_id].resolution.state == "expired"
        assert stored_by_id[lapsed.notification_id].read_at
        # Idempotent: a second listing writes nothing.
        assert service.expire_lapsed(EMAIL, stored, now="2026-11-25T12:00:00Z") == 0

    def test_a_resolved_row_is_never_overwritten_by_expiry(self, service):
        row = _wait(service, "2026-11-25T11:59:59Z")
        service.table.update_item(
            Key={"PK": f"INBOX#{EMAIL}", "SK": f"NOTIF#{row.notification_id}"},
            UpdateExpression="SET #r = :r",
            ExpressionAttributeNames={"#r": "resolution"},
            ExpressionAttributeValues={":r": {"state": "answered", "decision": "approved", "at": "t"}},
        )
        # A stale page that has not seen the answer yet.
        stale = [row]
        assert service.expire_lapsed(EMAIL, stale, now="2026-11-25T12:00:00Z") == 0
        (stored,), _ = service.list(EMAIL)
        assert (stored.resolution.state, stored.resolution.decision) == ("answered", "approved")
