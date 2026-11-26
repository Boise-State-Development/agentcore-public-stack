"""``GET /notifications/{id}/prompts`` and ``POST /notifications/{id}/respond`` (answer-from-inbox §3.4–3.5),
and the list's settling of waits that went another way (§3.3).

moto DynamoDB for the inbox with the real NotificationService; the session's ``META`` read is a fake
keyed on ``(session, user)``, the audit recorder a fake, and the grant and the worker invoke are
recorded rather than called.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import boto3
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.notifications import routes
from apis.app_api.runs import routes as runs_routes
from apis.shared.auth.models import User
from apis.shared.notifications import NotificationService
from apis.shared.scheduled_prompts import inbox_answer

REGION = "us-east-1"
TABLE = "test-projects"
ALICE = User(email="alice@example.edu", user_id="u-alice", name="Alice", roles=[])
BOB = User(email="bob@example.edu", user_id="u-bob", name="Bob", roles=[])
SESSION = "headless-abc"


def _in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


APPROVAL = {
    "interruptId": "int-1",
    "kind": "tool_approval",
    "toolName": "send_message",
    "toolInput": json.dumps({"to": "someone", "body": "the drafted reply"}),
    "message": "Approve sending?",
    "createdAt": "2026-11-25T01:00:00Z",
}
QUESTION = {
    "interruptId": "int-2",
    "kind": "user_question",
    "questions": json.dumps([{"header": "Calendar", "question": "Which?", "options": [{"label": "Work"}]}]),
    "createdAt": "2026-11-25T01:00:00Z",
}


class FakeAudit:
    def __init__(self) -> None:
        self.started: List[Dict[str, Any]] = []
        self.ended: List[Any] = []
        self.fail = False

    def record_start(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("audit down")
        self.started.append(kwargs)

    def record_end(self, *, result: Any) -> None:
        self.ended.append(result)


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
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
        service = NotificationService(table_name=TABLE)
        monkeypatch.setattr(routes, "_service", service)

        metas: Dict[tuple, Dict[str, Any]] = {}

        async def load_meta(session_id: str, user_id: str):
            return SimpleNamespace(row=metas.get((session_id, user_id)))

        monkeypatch.setattr("apis.shared.sessions.metadata.load_session_meta", load_meta)

        audit = FakeAudit()
        monkeypatch.setattr(runs_routes, "get_run_audit_recorder", lambda: audit)
        grants: List[str] = []
        grant_error: List[Optional[HTTPException]] = [None]

        async def resolve_grant(request, user):
            if grant_error[0] is not None:
                raise grant_error[0]
            grants.append(user.user_id)

        monkeypatch.setattr(runs_routes, "_resolve_grant", resolve_grant)
        invokes: List[Dict[str, Any]] = []
        invoke_error: List[Optional[Exception]] = [None]

        def invoke(payload):
            if invoke_error[0] is not None:
                raise invoke_error[0]
            invokes.append(payload)

        monkeypatch.setattr(inbox_answer, "invoke_inbox_resume", invoke)
        marked: List[tuple] = []

        def mark(user_id, schedule_id, *, run_finished_at, run_id):
            marked.append((user_id, schedule_id, run_finished_at, run_id))
            return True

        monkeypatch.setattr("apis.shared.scheduled_prompts.runs.record_schedule_run_answered_elsewhere", mark)
        yield SimpleNamespace(
            service=service, metas=metas, audit=audit, grants=grants, grant_error=grant_error,
            invokes=invokes, invoke_error=invoke_error, marked=marked,
        )


def client(user: User = ALICE) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user_from_session] = lambda: user
    return TestClient(app)


def _wait(
    world, *crumbs, user: User = ALICE, expires: Optional[str] = None, answerable: bool = True, live=True,
    session: str = SESSION,
):
    """A needs-user row for ``user``, and (when ``live``) its paused session under that user."""
    crumbs = crumbs or (APPROVAL,)
    expires = expires or _in(7)
    row = world.service.notify_own(
        recipient_email=user.email,
        kind="agent_needs_approval" if all(c["kind"] == "tool_approval" for c in crumbs) else "agent_needs_input",
        payload={
            "scheduleId": "sch-1", "runId": "run-abc", "runFinishedAt": "2026-11-25T01:00:00Z",
            "sessionId": session, "label": "Watcher", "expiresAt": expires, "answerable": answerable,
            "prompts": [{"interruptId": c["interruptId"], "kind": c["kind"]} for c in crumbs],
        },
    )
    if live:
        world.metas[(session, user.user_id)] = {
            "pendingInterrupts": list(crumbs),
            "pausedTurn": {"expiresAt": expires},
        }
    return row


def _stored(world, row, user: User = ALICE):
    return world.service.get(user.email, row.notification_id)


def _respond(row, responses, user: User = ALICE):
    return client(user).post(f"/notifications/{row.notification_id}/respond", json={"responses": responses})


APPROVE = [{"interruptId": "int-1", "response": "approved"}]


class TestPrompts:
    def test_returns_the_live_prompts_with_the_full_input(self, world):
        row = _wait(world)
        resp = client().get(f"/notifications/{row.notification_id}/prompts")
        assert resp.status_code == 200
        (prompt,) = resp.json()["prompts"]
        assert prompt["interruptId"] == "int-1" and "the drafted reply" in prompt["toolInput"]

    def test_flag_off_is_404(self, world, monkeypatch):
        row = _wait(world)
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "")
        assert client().get(f"/notifications/{row.notification_id}/prompts").status_code == 404
        assert _respond(row, APPROVE).status_code == 404

    def test_someone_elses_row_or_another_kind_is_404(self, world):
        row = _wait(world)
        assert client(BOB).get(f"/notifications/{row.notification_id}/prompts").status_code == 404
        other = world.service.notify_own(recipient_email=ALICE.email, kind="agent_item", payload={"title": "x"})
        assert client().get(f"/notifications/{other.notification_id}/prompts").status_code == 404

    def test_a_forged_session_reads_nothing(self, world):
        # Bob's own row names a session only Alice has: under (session, Bob) there is no pause.
        world.metas[(SESSION, ALICE.user_id)] = {"pendingInterrupts": [APPROVAL], "pausedTurn": {"expiresAt": _in(7)}}
        row = _wait(world, user=BOB, live=False)
        resp = client(BOB).get(f"/notifications/{row.notification_id}/prompts")
        assert resp.status_code == 409
        assert "drafted" not in resp.text

    def test_a_gone_pause_is_409_and_resolves_the_row(self, world):
        row = _wait(world, live=False)
        resp = client().get(f"/notifications/{row.notification_id}/prompts")
        assert (resp.status_code, resp.json()["detail"]["code"]) == (409, "already_answered")
        stored = _stored(world, row)
        assert stored.resolution.state == "answered_elsewhere" and stored.read_at
        # The run's history row learns of it too (§3.8).
        assert world.marked == [(ALICE.user_id, "sch-1", "2026-11-25T01:00:00Z", "run-abc")]

    def test_an_expired_pause_is_410_and_resolves_the_row(self, world):
        row = _wait(world)
        world.metas[(SESSION, ALICE.user_id)]["pausedTurn"]["expiresAt"] = _in(-1)
        resp = client().get(f"/notifications/{row.notification_id}/prompts")
        assert (resp.status_code, resp.json()["detail"]["code"]) == (410, "expired")
        assert _stored(world, row).resolution.state == "expired"


class TestRespond:
    def test_claims_audits_and_invokes_the_worker(self, world):
        row = _wait(world)
        resp = _respond(row, APPROVE)

        assert resp.status_code == 202
        body = resp.json()
        resume_run_id = body["resumeRunId"]
        assert body["resolution"]["state"] == "applying" and body["resolution"]["decision"] == "approved"
        stored = _stored(world, row)
        assert stored.resolution.state == "applying" and stored.read_at
        assert world.grants == [ALICE.user_id]
        (start,) = world.audit.started
        assert (start["run_id"], start["trigger"], start["session_id"]) == (resume_run_id, "inbox_respond", SESSION)
        assert start["extra"] == {"resumesRunId": "run-abc", "notificationId": row.notification_id, "decisions": ["approved"]}
        (invoke,) = world.invokes
        assert invoke == {
            "userId": ALICE.user_id, "email": ALICE.email, "notificationId": row.notification_id,
            "resumeRunId": resume_run_id, "sessionId": SESSION, "scheduleId": "sch-1", "responses": APPROVE,
        }
        # No bearer travels in the event.
        assert "token" not in json.dumps(invoke).lower() and "bearer" not in json.dumps(invoke).lower()

    def test_a_second_answer_loses_the_race(self, world):
        row = _wait(world)
        assert _respond(row, APPROVE).status_code == 202
        resp = _respond(row, [{"interruptId": "int-1", "response": "declined"}])
        assert (resp.status_code, resp.json()["detail"]["code"]) == (409, "already_answered")
        assert len(world.invokes) == 1

    def test_a_stale_claim_is_retaken(self, world):
        row = _wait(world)
        assert _respond(row, APPROVE).status_code == 202
        world.service.table.update_item(
            Key={"PK": f"INBOX#{ALICE.email}", "SK": f"NOTIF#{row.notification_id}"},
            UpdateExpression="SET #r.#at = :old",
            ExpressionAttributeNames={"#r": "resolution", "#at": "at"},
            ExpressionAttributeValues={":old": "2026-01-01T00:00:00Z"},
        )
        assert _respond(row, APPROVE).status_code == 202
        assert len(world.invokes) == 2

    def test_a_started_worker_holds_its_claim(self, world):
        row = _wait(world)
        resume_run_id = _respond(row, APPROVE).json()["resumeRunId"]
        assert world.service.mark_worker_started(ALICE.email, row.notification_id, resume_run_id=resume_run_id)
        assert _respond(row, APPROVE).status_code == 409

    @pytest.mark.parametrize(
        "responses",
        [
            [{"interruptId": "nope", "response": "approved"}],
            [{"interruptId": "int-1", "response": None}],
            [{"interruptId": "int-1", "response": "yes"}],
            [{"interruptId": "int-1", "response": "approved"}, {"interruptId": "int-1", "response": "approved"}],
        ],
    )
    def test_answers_that_dont_fit_are_422(self, world, responses):
        row = _wait(world)
        resp = _respond(row, responses)
        assert (resp.status_code, resp.json()["detail"]["code"]) == (422, "invalid_answers")
        assert _stored(world, row).resolution is None and world.invokes == []

    def test_every_prompt_needs_an_answer(self, world):
        row = _wait(world, APPROVAL, QUESTION)
        assert _respond(row, APPROVE).status_code == 422
        resp = _respond(
            row,
            [APPROVE[0], {"interruptId": "int-2", "response": {"answers": {"Calendar": {"selected": ["Work"]}}}}],
        )
        assert resp.status_code == 202 and resp.json()["resolution"]["decision"] == "answered"

    def test_a_row_answerable_only_in_its_conversation_is_422(self, world):
        row = _wait(world, answerable=False)
        assert _respond(row, APPROVE).status_code == 422

    def test_no_grant_gives_the_row_back(self, world):
        row = _wait(world)
        world.grant_error[0] = HTTPException(status_code=409, detail="No headless grant")
        assert _respond(row, APPROVE).status_code == 409
        stored = _stored(world, row)
        assert stored.resolution.state == "retry" and stored.read_at is None
        assert world.invokes == [] and world.audit.started == []

    def test_a_failed_audit_gives_the_row_back(self, world):
        row = _wait(world)
        world.audit.fail = True
        assert _respond(row, APPROVE).status_code == 503
        assert _stored(world, row).resolution.state == "retry" and world.invokes == []

    def test_a_refused_invoke_gives_the_row_back_and_ends_the_audit(self, world):
        row = _wait(world)
        world.invoke_error[0] = RuntimeError("throttled")
        resp = _respond(row, APPROVE)
        assert (resp.status_code, resp.json()["detail"]["code"]) == (503, "unavailable")
        assert _stored(world, row).resolution.state == "retry"
        (end,) = world.audit.ended
        assert end.status == "error"
        # The buttons are back: answering again works.
        world.invoke_error[0] = None
        assert _respond(row, APPROVE).status_code == 202

    def test_an_answered_row_is_409_and_an_expired_one_410(self, world):
        row = _wait(world, live=False)
        assert _respond(row, APPROVE).status_code == 409
        lapsed = _wait(world, expires=_in(-1))
        assert _respond(lapsed, APPROVE).status_code == 410


class TestReadState:
    def test_listing_settles_a_wait_answered_in_its_conversation(self, world):
        answered = _wait(world, live=False, session="headless-answered")  # its breadcrumbs are gone
        live = _wait(world)
        world.service.notify_own(recipient_email=ALICE.email, kind="agent_item", payload={"title": "x"})

        body = client().get("/notifications").json()

        by_id = {n["notificationId"]: n for n in body["notifications"]}
        assert by_id[answered.notification_id]["resolution"]["state"] == "answered_elsewhere"
        assert by_id[answered.notification_id]["readAt"]
        assert by_id[live.notification_id].get("resolution") is None
        assert not by_id[live.notification_id].get("readAt")
        # The live wait and the finding count; the answered wait no longer does.
        assert body["unreadCount"] == 2
        # Only the answered wait's run is marked in the history; a second list marks nothing more.
        assert world.marked == [(ALICE.user_id, "sch-1", "2026-11-25T01:00:00Z", "run-abc")]
        client().get("/notifications")
        assert len(world.marked) == 1

    def test_listing_an_expired_wait_leaves_the_history_row_to_its_own_expiry(self, world):
        lapsed = _wait(world, expires=_in(-1))
        body = client().get("/notifications").json()
        (n,) = [n for n in body["notifications"] if n["notificationId"] == lapsed.notification_id]
        assert n["resolution"]["state"] == "expired"
        assert world.marked == []

    def test_listing_with_the_flag_off_reads_no_session(self, world, monkeypatch):
        answered = _wait(world, live=False)
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "false")
        body = client().get("/notifications").json()
        (n,) = [n for n in body["notifications"] if n["notificationId"] == answered.notification_id]
        assert n.get("resolution") is None
