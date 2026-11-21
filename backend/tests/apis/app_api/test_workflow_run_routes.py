"""Route-level tests for ``/sessions/{id}/workflow-runs`` (workflow-runs spec §9, §19 PR-W.1).

The stores are real, over one moto table, so owner scoping and both state machines are the stores'
own. The session, Agent, message and model lookups are stubbed, as in ``test_handoff_routes``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.handoffs import routes, workflow_runs
from apis.shared.auth.models import User
from apis.shared.handoff.context import build_handoff_message, wrap_handoff_context
from apis.shared.handoff.runs import WorkflowRunStore
from apis.shared.handoff.store import HandoffStore
from apis.shared.models.retirement import EffectiveModel
from apis.shared.sessions.models import MessageResponse, MessagesListResponse
from apis.shared.workflows import sheet as workflow_sheet

REGION = "us-west-2"
TABLE = "test-sessions-metadata"

ALICE = User(email="alice@example.edu", user_id="alice", name="Alice", roles=["User"])
BOB = User(email="bob@example.edu", user_id="bob", name="Bob", roles=["User"])


def _message(index: int, role: str, text: str) -> MessageResponse:
    return MessageResponse(
        id=f"msg-src-{index}",
        role=role,
        content=[{"type": "text", "text": text}],
        created_at="2026-11-20T00:00:00Z",
    )


def _agent(name: str, model_id: Optional[str] = None) -> Any:
    settings = SimpleNamespace(model_id=model_id) if model_id else None
    return SimpleNamespace(name=name, model_settings=settings)


class _World:
    def __init__(self) -> None:
        self.sessions: Dict[tuple, Any] = {("src", "alice"): SimpleNamespace(title="Proposal review", preferences=None)}
        self.agents: Dict[str, Any] = {
            "generalist": _agent("Generalist", "us.sonnet"),
            "colleague": _agent("Colleague", "us.retired"),
            "outsider": _agent("Emotional outsider"),
            "fiscal": _agent("Fiscal outsider", "us.unlisted"),
        }
        self.agent_users = {(a, "alice") for a in self.agents}
        self.messages: List[MessageResponse] = [
            _message(0, "user", "here is the proposal"),
            _message(1, "assistant", "I read the proposal."),
        ]
        self.rates = {"us.sonnet": 3.0, "us.haiku": 1.0}


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
def world(monkeypatch) -> _World:
    w = _World()

    async def get_session_metadata(session_id: str, user_id: str):
        return w.sessions.get((session_id, user_id))

    async def get_assistant_with_access_check(assistant_id: str, user_id: str, user_email: str = None):
        if (assistant_id, user_id) in w.agent_users:
            return w.agents[assistant_id], "viewer"
        return None, None

    async def get_messages(session_id: str, user_id: str, limit=None, next_token=None):
        return MessagesListResponse(messages=w.messages)

    async def get_session_compaction(session_id: str, user_id: str):
        return 0, None

    async def resolve_effective_model(model_id: Optional[str]):
        if model_id == "us.retired":
            model_id = "us.haiku"  # redirected to its successor
        rate = w.rates.get(model_id)
        record = SimpleNamespace(input_price_per_million_tokens=rate) if rate is not None else None
        return EffectiveModel(requested_id=model_id, model_id=model_id, record=record)

    monkeypatch.setattr(routes, "get_session_metadata", get_session_metadata)
    monkeypatch.setattr(routes, "get_assistant_with_access_check", get_assistant_with_access_check)
    monkeypatch.setattr(routes, "get_messages", get_messages)
    monkeypatch.setattr(routes, "get_session_compaction", get_session_compaction)
    monkeypatch.setattr(workflow_sheet, "resolve_effective_model", resolve_effective_model)
    return w


@pytest.fixture()
def stores(table):
    return HandoffStore(table=table), WorkflowRunStore(table=table)


@pytest.fixture()
def metrics(monkeypatch) -> List[tuple]:
    seen: List[tuple] = []
    monkeypatch.setattr(workflow_runs, "emit_workflow_run_metric", lambda name, **kw: seen.append((name, kw)))
    return seen


@pytest.fixture()
def make_client(monkeypatch, stores, world, metrics):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "true")
    handoffs, runs = stores

    def build(user: User = ALICE) -> TestClient:
        app = FastAPI()
        app.include_router(workflow_runs.router)
        app.dependency_overrides[workflow_runs.get_current_user_from_session] = lambda: user
        app.dependency_overrides[workflow_runs.get_handoff_store] = lambda: handoffs
        app.dependency_overrides[workflow_runs.get_workflow_run_store] = lambda: runs
        return TestClient(app)

    return build


def _create(client: TestClient, agents=("generalist", "colleague", "outsider"), **body: Any):
    payload: Dict[str, Any] = {"request": " review this proposal ", "steps": [{"agentId": a} for a in agents]}
    payload.update(body)
    return client.post("/sessions/src/workflow-runs", json=payload)


# ---------------------------------------------------------------- create


def test_create_renders_the_card_once_and_writes_a_mention_row_per_step(make_client, stores, metrics):
    handoffs, runs = stores
    resp = _create(make_client())

    assert resp.status_code == 201
    data = resp.json()
    assert data["excerpt"].startswith("<conversation_excerpt")
    assert data["contextBlock"] == wrap_handoff_context(data["excerpt"], "Proposal review")
    assert data["sourceTitle"] == "Proposal review" and data["sourceMessageIndex"] == 1
    run_id = data["run"]["runId"]
    assert data["run"]["status"] == "open"
    assert data["run"]["limits"] == {"maxSteps": 8, "concurrency": 4}

    steps = data["steps"]
    assert [s["agentName"] for s in steps] == ["Generalist", "Colleague", "Emotional outsider"]
    for step in steps:
        assert step["request"] == "review this proposal"
        assert step["prompt"] == build_handoff_message(data["contextBlock"], "review this proposal")
        row = handoffs.get("alice", step["handoffId"])
        assert (row.kind, row.status, row.run_id, row.step_id) == ("mention", "open", run_id, step["stepId"])
        assert row.excerpt_hash == data["excerptHash"] and row.source_message_index == 1

    stored = runs.get("alice", run_id)
    assert [s.handoff_id for s in stored.steps] == [s["handoffId"] for s in steps]
    assert metrics == [("WorkflowRunCreated", {"session_id": "src", "run_id": run_id, "step_count": 3})]


def test_create_carries_each_steps_model_and_input_rate_for_the_estimate(make_client):
    data = _create(make_client(), agents=("generalist", "colleague", "outsider", "fiscal")).json()
    priced = [(s.get("modelId"), s.get("inputPricePerMillionTokens")) for s in data["steps"]]
    # Its own model; a retired one's successor; none (the send's model); a model with no catalog row.
    assert priced == [("us.sonnet", 3.0), ("us.haiku", 1.0), (None, None), ("us.unlisted", None)]
    assert data["excerptTokens"] > 0


def test_a_steps_own_request_wins_over_the_shared_one(make_client):
    data = _create(
        make_client(), steps=[{"agentId": "generalist", "request": " only the budget "}, {"agentId": "colleague"}]
    ).json()
    assert [s["request"] for s in data["steps"]] == ["only the budget", "review this proposal"]


@pytest.mark.parametrize(
    "agents",
    [("generalist",), ("generalist", "colleague", "outsider", "fiscal", "a", "b", "c", "d", "e"), ("generalist", "generalist")],
)
def test_two_to_eight_distinct_agents_or_422(make_client, stores, agents):
    resp = _create(make_client(), agents=agents)
    assert resp.status_code == 422
    assert stores[0].list_for_session("alice", "src") == []


def test_eight_steps_are_allowed(make_client, world):
    for name in "abcd":
        world.agents[name] = _agent(name)
        world.agent_users.add((name, "alice"))
    resp = _create(make_client(), agents=("generalist", "colleague", "outsider", "fiscal", "a", "b", "c", "d"))
    assert resp.status_code == 201 and len(resp.json()["steps"]) == 8


def test_a_shared_project_task_is_refused_like_a_delivery(make_client, world, stores):
    world.sessions[("src", "alice")] = SimpleNamespace(title="Task", preferences=SimpleNamespace(project_id="p1"))
    resp = _create(make_client())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Results can't be sent into or out of a shared project"
    assert stores[0].list_for_session("alice", "src") == []


def test_another_users_conversation_or_an_agent_they_cant_use_is_404(make_client, world, stores):
    assert _create(make_client(BOB)).status_code == 404
    world.agent_users.discard(("outsider", "alice"))
    resp = _create(make_client())
    assert resp.status_code == 404
    assert stores[0].list_for_session("alice", "src") == []


# ---------------------------------------------------------------- approve


def _approve(client: TestClient, run_id: str, **body: Any):
    return client.post(f"/sessions/src/workflow-runs/{run_id}/approve", json=body)


def test_approve_dismisses_removed_steps_edits_requests_and_marks_the_rest(make_client, stores, metrics):
    handoffs, runs = stores
    client = make_client()
    created = _create(client).json()
    run_id = created["run"]["runId"]
    s1, s2, s3 = created["steps"]
    metrics.clear()

    resp = _approve(
        client,
        run_id,
        steps=[{"stepId": s2["stepId"], "removed": True}, {"stepId": s3["stepId"], "request": "argue the other side"}],
        fileIds=["f1", "f1", "f2"],
        filesUnticked=1,
    )

    assert resp.status_code == 200
    view = resp.json()
    assert view["status"] == "approved" and view["approvedAt"] and view["fileIds"] == ["f1", "f2"]
    assert [(s["removed"], s["request"]) for s in view["steps"]] == [
        (False, "review this proposal"), (True, "review this proposal"), (False, "argue the other side"),
    ]
    assert [s["handoff"]["status"] for s in view["steps"]] == ["open", "dismissed", "open"]
    kept = handoffs.get("alice", s3["handoffId"])
    assert (kept.request, kept.run_approved_at is not None) == ("argue the other side", True)
    assert handoffs.get("alice", s1["handoffId"]).run_approved_at is not None
    assert runs.get("alice", run_id).edits == ["request", "step_removed", "file_unticked"]
    assert [(name, kw.get("detail"), kw.get("step_count")) for name, kw in metrics] == [
        ("WorkflowRunApproved", None, 2),
        ("WorkflowRunEdited", "request", None),
        ("WorkflowRunEdited", "step_removed", None),
        ("WorkflowRunEdited", "file_unticked", None),
    ]


def test_an_edited_shared_request_reaches_the_steps_still_on_it(make_client, stores):
    client = make_client()
    created = _create(client, steps=[{"agentId": "generalist", "request": "own"}, {"agentId": "colleague"}]).json()
    view = _approve(client, created["run"]["runId"], request="  review section 4  ").json()
    assert view["request"] == "review section 4"
    assert [s["request"] for s in view["steps"]] == ["own", "review section 4"]
    assert stores[0].get("alice", created["steps"][1]["handoffId"]).request == "review section 4"


def test_an_unedited_approval_counts_no_edit(make_client, metrics):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    metrics.clear()
    assert _approve(client, run_id).status_code == 200
    assert [name for name, _ in metrics] == ["WorkflowRunApproved"]


def test_approve_keeps_the_card_as_reviewed_and_get_returns_it(make_client, stores):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    resp = _approve(client, run_id, card={"text": "  the card\r\nas edited  "})
    assert resp.status_code == 200
    # Normalized as the Runtime normalizes a card, so a step started from it reads the same.
    assert resp.json()["card"] == {"text": "the card\nas edited", "removed": False}
    assert client.get(f"/sessions/src/workflow-runs/{run_id}").json()["card"] == {
        "text": "the card\nas edited", "removed": False,
    }


@pytest.mark.parametrize("card", [{"removed": True}, {"text": "   "}, {"text": "kept", "removed": True}])
def test_a_removed_or_blank_card_is_kept_as_removed(make_client, stores, card):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert _approve(client, run_id, card=card).json()["card"] == {"removed": True}
    assert stores[1].get("alice", run_id).card.text is None


def test_the_card_is_capped_as_a_card_is_and_bounded_as_a_field(make_client, stores):
    from apis.shared.handoff.turn import HANDOFF_CONTEXT_MAX_CHARS

    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert _approve(client, run_id, card={"text": "x" * 200_001}).status_code == 422
    stored = _approve(client, run_id, card={"text": "y" * (HANDOFF_CONTEXT_MAX_CHARS * 2)}).json()["card"]["text"]
    assert len(stored) <= HANDOFF_CONTEXT_MAX_CHARS


def test_an_approval_without_a_card_stores_none(make_client):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert "card" not in _approve(client, run_id).json()


def test_approve_happens_once(make_client):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert _approve(client, run_id).status_code == 200
    assert _approve(client, run_id).status_code == 409


def test_at_least_one_step_must_remain(make_client, stores):
    client = make_client()
    created = _create(client, agents=("generalist", "colleague")).json()
    resp = _approve(client, created["run"]["runId"], steps=[{"stepId": s["stepId"], "removed": True} for s in created["steps"]])
    assert resp.status_code == 422
    assert stores[1].get("alice", created["run"]["runId"]).status == "open"


def test_an_unknown_or_repeated_step_is_422(make_client):
    client = make_client()
    created = _create(client).json()
    run_id, step = created["run"]["runId"], created["steps"][0]["stepId"]
    assert _approve(client, run_id, steps=[{"stepId": "nope"}]).status_code == 422
    assert _approve(client, run_id, steps=[{"stepId": step}, {"stepId": step}]).status_code == 422


def test_too_many_files_is_422(make_client):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert _approve(client, run_id, fileIds=[f"f{i}" for i in range(6)]).status_code == 422


def test_an_approved_runs_unstarted_steps_never_read_abandoned(make_client, stores, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from apis.shared.handoff import models as models_module

    client = make_client()
    created = _create(client).json()
    run_id = created["run"]["runId"]
    _approve(client, run_id)
    # A second run left at its sheet, to show the rule still holds for it.
    left = _create(client, agents=("fiscal", "outsider")).json()

    later = datetime.now(timezone.utc) + timedelta(days=2)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(models_module, "datetime", Later)
    approved = client.get(f"/sessions/src/workflow-runs/{run_id}").json()
    abandoned = client.get(f"/sessions/src/workflow-runs/{left['run']['runId']}").json()
    assert {s["handoff"]["status"] for s in approved["steps"]} == {"open"}
    assert {s["handoff"]["status"] for s in abandoned["steps"]} == {"abandoned"}


# ---------------------------------------------------------------- read, stop, dismiss


def test_get_reads_the_run_with_its_steps_current_rows(make_client, stores):
    handoffs, _ = stores
    client = make_client()
    created = _create(client).json()
    run_id = created["run"]["runId"]
    _approve(client, run_id)
    handoffs.accept("alice", created["steps"][0]["handoffId"], target_session_id="tgt", context_edited=False, context_removed=False)

    view = client.get(f"/sessions/src/workflow-runs/{run_id}").json()
    assert [s["handoff"]["status"] for s in view["steps"]] == ["running", "open", "open"]
    assert view["steps"][0]["handoff"]["targetSessionId"] == "tgt"


def test_a_run_is_owner_scoped_and_bound_to_its_conversation(make_client, world):
    run_id = _create(make_client()).json()["run"]["runId"]
    assert make_client(BOB).get(f"/sessions/src/workflow-runs/{run_id}").status_code == 404
    assert make_client().get(f"/sessions/other/workflow-runs/{run_id}").status_code == 404
    assert make_client(BOB).post(f"/sessions/src/workflow-runs/{run_id}/dismiss").status_code == 404


def test_stop_closes_an_approved_run_once(make_client, stores, metrics):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/stop").status_code == 409
    _approve(client, run_id)
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/stop").status_code == 204
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/stop").status_code == 204
    assert stores[1].get("alice", run_id).status == "stopped"
    assert [name for name, _ in metrics].count("WorkflowRunStopped") == 1


def test_dismiss_cancels_the_sheet_and_every_open_step(make_client, stores, metrics):
    handoffs, runs = stores
    client = make_client()
    created = _create(client).json()
    run_id = created["run"]["runId"]

    assert client.post(f"/sessions/src/workflow-runs/{run_id}/dismiss").status_code == 204
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/dismiss").status_code == 204
    assert runs.get("alice", run_id).status == "dismissed"
    assert {handoffs.get("alice", s["handoffId"]).status for s in created["steps"]} == {"dismissed"}
    assert [name for name, _ in metrics].count("WorkflowRunCancelled") == 1
    assert _approve(client, run_id).status_code == 409


def test_an_approved_run_cannot_be_dismissed(make_client):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    _approve(client, run_id)
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/dismiss").status_code == 409


# ---------------------------------------------------------------- the flag


@pytest.mark.parametrize("handoffs_flag, runs_flag", [("true", ""), ("true", "false"), ("false", "true"), ("", "true")])
def test_every_route_is_404_after_auth_while_off(make_client, monkeypatch, handoffs_flag, runs_flag):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    monkeypatch.setenv("HANDOFFS_ENABLED", handoffs_flag)
    monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", runs_flag)
    base = "/sessions/src/workflow-runs"
    calls = [
        client.post(base, json={"request": "x", "steps": [{"agentId": "generalist"}]}),  # invalid too: 404 first
        client.get(f"{base}/{run_id}"),
        client.post(f"{base}/{run_id}/approve", json={}),
        client.post(f"{base}/{run_id}/stop"),
        client.post(f"{base}/{run_id}/dismiss"),
    ]
    assert [c.status_code for c in calls] == [404] * 5


def test_unauthenticated_is_401_while_off(monkeypatch, stores):
    monkeypatch.delenv("WORKFLOW_RUNS_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(workflow_runs.router)
    resp = TestClient(app).get("/sessions/src/workflow-runs/r1")
    assert resp.status_code == 401


# ---------------------------------------------------------------- collect (PR-W.2)


class _Steps:
    """The step conversations a collect reads: their META rows, answers, artifacts and own files."""

    def __init__(self) -> None:
        self.meta: Dict[tuple, Dict[str, Any]] = {("src", "alice"): {"title": "Proposal review"}}
        self.messages: Dict[str, List[MessageResponse]] = {}
        self.heads: Dict[tuple, List[Dict[str, Any]]] = {}
        self.files: Dict[str, List[Any]] = {}

    def target(self, session_id: str, handoff_id: str, title: str, answers: List[tuple]) -> None:
        self.meta[(session_id, "alice")] = {"title": title, "spawnedFrom": {"sessionId": "src", "handoffId": handoff_id}}
        self.messages[session_id] = [
            MessageResponse(
                id=f"msg-{session_id}-{index}", role="assistant", content=[{"type": "text", "text": text}], created_at=at
            )
            for index, text, at in answers
        ]


@pytest.fixture()
def steps(monkeypatch, world) -> _Steps:
    from apis.shared.handoff import send_back

    s = _Steps()

    async def load_session_meta(session_id: str, user_id: str):
        return SimpleNamespace(row=s.meta.get((session_id, user_id)), owned_by_other=False)

    async def get_messages(session_id: str, user_id: str, limit=None, next_token=None):
        if session_id == "src":
            return MessagesListResponse(messages=world.messages)
        return MessagesListResponse(messages=s.messages.get(session_id, []) if user_id == "alice" else [])

    repo = SimpleNamespace(
        list_session_files_sync=lambda session_id, status=None: list(s.files.get(session_id, [])),
        batch_get_files_sync=lambda user, ids: {
            f.upload_id: f for rows in s.files.values() for f in rows if f.upload_id in ids and f.user_id == user
        },
    )
    monkeypatch.setattr("apis.shared.sessions.metadata.load_session_meta", load_session_meta)
    monkeypatch.setattr("apis.shared.sessions.messages.get_messages", get_messages)
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
    monkeypatch.setattr(send_back, "_artifact_heads", lambda user, session: s.heads.get((user, session), []))
    return s


@pytest.fixture()
def collect_client(make_client, table, monkeypatch):
    from apis.shared.handoff.delivery import DeliveryStore

    deliveries = DeliveryStore(table=table)
    monkeypatch.setattr("apis.shared.handoff.send_back.get_delivery_store", lambda: deliveries)

    def build(user: User = ALICE) -> TestClient:
        client = make_client(user)
        client.app.dependency_overrides[workflow_runs.get_delivery_store] = lambda: deliveries
        return client

    build.deliveries = deliveries  # type: ignore[attr-defined]
    return build


def _file(upload_id: str, session_id: str, created_at: str) -> Any:
    from apis.shared.files.models import FileMetadata, FileStatus

    return FileMetadata(
        upload_id=upload_id, user_id="alice", session_id=session_id, filename=f"{upload_id}.pdf",
        mime_type="application/pdf", size_bytes=2048, s3_key=f"user_files/alice/{session_id}/{upload_id}.pdf",
        s3_bucket="bucket", status=FileStatus.READY, created_at=created_at, updated_at=created_at,
    )


def _run_with_steps(client: TestClient, stores, steps: _Steps, agents=("generalist", "colleague", "outsider", "fiscal")):
    """An approved run whose steps finished, stopped, wait, and never started, in that order."""
    handoffs, _runs = stores
    created = _create(client, agents=agents).json()
    run_id = created["run"]["runId"]
    assert _approve(client, run_id).status_code == 200
    rows = created["steps"]
    for index, step in enumerate(rows[:3]):
        target = f"t{index}"
        handoffs.accept("alice", step["handoffId"], target_session_id=target, context_edited=False, context_removed=False)
        steps.target(target, step["handoffId"], "Review of the proposal", [])
    finished = handoffs.transition("alice", rows[0]["handoffId"], "done")
    steps.target(
        "t0", rows[0]["handoffId"], "Review of the proposal",
        [(1, "Strong aims; the budget is thin.", "2026-01-01T00:00:00Z"), (3, "Later work in this chat.", "2999-01-01T00:00:00Z")],
    )
    steps.heads[("alice", "t0")] = [
        {"artifact_id": "memo", "version": 2, "title": "Memo", "content_type": "text/markdown", "produced_by_message_index": 1},
        {"artifact_id": "later", "version": 1, "title": "Later", "content_type": "text/html", "produced_by_message_index": 3},
    ]
    steps.files["t0"] = [_file("own-1", "t0", "2026-01-01T00:00:00Z"), _file("after", "t0", "2999-01-01T00:00:00Z")]
    handoffs.transition("alice", rows[1]["handoffId"], "stopped", stoppedReason="user_stopped")
    handoffs.transition("alice", rows[2]["handoffId"], "needs_input")
    return run_id, rows, finished


def _collect_run(client: TestClient, run_id: str):
    return client.post(f"/sessions/src/workflow-runs/{run_id}/collect")


def test_collect_makes_one_card_with_a_part_per_kept_step(collect_client, stores, steps, metrics):
    client = collect_client()
    run_id, rows, _ = _run_with_steps(client, stores, steps)

    resp = _collect_run(client, run_id)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["created"], data["status"]) == (True, "pending")
    parts = data["delivery"]["parts"]
    assert [p["stepId"] for p in parts] == [r["stepId"] for r in rows]
    assert [(p["agentName"], p["status"]) for p in parts] == [
        ("Generalist", "done"), ("Colleague", "stopped"), ("Emotional outsider", "needs_input"), ("Fiscal outsider", "running"),
    ]
    done = parts[0]
    # The answer as it stood when the step finished, with what that answer made and the files made by then.
    assert done["text"] == "Strong aims; the budget is thin."
    assert [a["artifactId"] for a in done["artifacts"]] == ["memo"]
    assert [f["uploadId"] for f in done["files"]] == ["own-1"]
    assert (done["fromSessionId"], done["fromTitle"]) == ("t0", "Review of the proposal")
    assert all(p["text"] == "" and p["artifacts"] == [] and p["files"] == [] for p in parts[1:])

    run = stores[1].get("alice", run_id)
    assert run.result_delivery_id == data["deliveryId"]
    assert collect_client.deliveries.get("alice", data["deliveryId"]).run_id == run_id
    assert ("WorkflowRunCollected", {"session_id": "src", "run_id": run_id, "step_count": 1, "detail": "partial"}) in metrics


def test_collect_is_exactly_once_and_returns_the_card_again(collect_client, stores, steps):
    client = collect_client()
    run_id, _rows, _ = _run_with_steps(client, stores, steps)

    first = _collect_run(client, run_id).json()
    again = _collect_run(client, run_id).json()

    assert again["deliveryId"] == first["deliveryId"] and again["created"] is False
    assert len(collect_client.deliveries.list_pending("alice", "src")) == 1
    # Fixed when collected: a step that finishes later doesn't change the card.
    stores[0].transition("alice", _rows[2]["handoffId"], "done")
    assert _collect_run(client, run_id).json()["delivery"]["parts"][2]["status"] == "needs_input"


def test_a_lost_race_for_the_result_hands_back_the_winners_card(collect_client, stores, steps, monkeypatch):
    client = collect_client()
    run_id, _rows, _ = _run_with_steps(client, stores, steps)
    runs = stores[1]
    real = runs.claim_result

    def another_collect_won(user_id, rid, delivery_id):
        real(user_id, rid, "winner")  # the other request's claim lands first
        return real(user_id, rid, delivery_id)

    monkeypatch.setattr(runs, "claim_result", another_collect_won)

    data = _collect_run(client, run_id).json()

    assert (data["deliveryId"], data["created"]) == ("winner", False)
    assert collect_client.deliveries.list_pending("alice", "src") == []  # the loser wrote nothing


def test_a_failed_write_releases_the_claim_so_collect_can_be_retried(collect_client, stores, steps, monkeypatch):
    client = collect_client()
    run_id, _rows, _ = _run_with_steps(client, stores, steps)

    async def boom(row, *, store=None):
        raise RuntimeError("throttled")

    monkeypatch.setattr(workflow_runs, "write_delivery", boom)
    assert _collect_run(client, run_id).status_code == 503
    assert stores[1].get("alice", run_id).result_delivery_id is None


def test_a_stopped_run_is_collectable_and_its_unstarted_step_reads_stopped(collect_client, stores, steps):
    client = collect_client()
    run_id, _rows, _ = _run_with_steps(client, stores, steps)
    assert client.post(f"/sessions/src/workflow-runs/{run_id}/stop").status_code == 204

    parts = _collect_run(client, run_id).json()["delivery"]["parts"]

    assert parts[3]["status"] == "stopped"


def test_a_removed_step_has_no_part(collect_client, stores, steps):
    client = collect_client()
    handoffs, _ = stores
    created = _create(client, agents=("generalist", "colleague")).json()
    run_id = created["run"]["runId"]
    first, second = created["steps"]
    _approve(client, run_id, steps=[{"stepId": second["stepId"], "removed": True}])
    handoffs.accept("alice", first["handoffId"], target_session_id="t0", context_edited=False, context_removed=False)
    handoffs.transition("alice", first["handoffId"], "done")
    steps.target("t0", first["handoffId"], "Review", [(1, "Fine.", "2026-01-01T00:00:00Z")])

    parts = _collect_run(client, run_id).json()["delivery"]["parts"]

    assert [p["stepId"] for p in parts] == [first["stepId"]]


@pytest.mark.parametrize("state", ["open", "dismissed"])
def test_a_run_never_approved_cant_be_collected(collect_client, stores, steps, state):
    client = collect_client()
    run_id = _create(client).json()["run"]["runId"]
    if state == "dismissed":
        client.post(f"/sessions/src/workflow-runs/{run_id}/dismiss")

    assert _collect_run(client, run_id).status_code == 409


def test_nothing_finished_yet_is_422_and_claims_nothing(collect_client, stores, steps):
    client = collect_client()
    run_id = _create(client).json()["run"]["runId"]
    _approve(client, run_id)

    resp = _collect_run(client, run_id)

    assert resp.status_code == 422
    assert stores[1].get("alice", run_id).result_delivery_id is None


def test_the_run_wide_file_cap_keeps_the_earliest_steps_files(collect_client, stores, steps):
    client = collect_client()
    handoffs, _ = stores
    created = _create(client, agents=("generalist", "colleague", "outsider")).json()
    run_id = created["run"]["runId"]
    _approve(client, run_id)
    for index, step in enumerate(created["steps"]):
        target = f"t{index}"
        handoffs.accept("alice", step["handoffId"], target_session_id=target, context_edited=False, context_removed=False)
        handoffs.transition("alice", step["handoffId"], "done")
        steps.target(target, step["handoffId"], "Review", [(1, f"answer {index}", "2026-01-01T00:00:00Z")])
        steps.files[target] = [_file(f"{target}-f{i}", target, f"2026-01-01T00:00:0{i}Z") for i in range(7)]

    parts = _collect_run(client, run_id).json()["delivery"]["parts"]

    assert [len(p["files"]) for p in parts] == [5, 5, 0]


def test_collect_is_scoped_to_the_runs_owner_and_conversation(collect_client, stores, steps):
    run_id, _rows, _ = _run_with_steps(collect_client(), stores, steps)

    assert _collect_run(collect_client(BOB), run_id).status_code == 404
    assert collect_client().post(f"/sessions/other/workflow-runs/{run_id}/collect").status_code == 404


def test_collect_is_404_with_the_flag_off(collect_client, stores, steps, monkeypatch):
    client = collect_client()
    run_id, _rows, _ = _run_with_steps(client, stores, steps)
    monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "false")

    assert _collect_run(client, run_id).status_code == 404


def test_an_answer_stamped_just_after_the_done_write_still_counts_as_the_steps_result():
    """Memory may stamp a turn's events when it flushes them, just after the row's ``done`` write."""
    cutoff = workflow_runs._finished_by("2026-11-20T10:00:00+00:00")

    assert cutoff == "2026-11-20T10:01:00+00:00"
    assert workflow_runs._finished_by("not a time") == "not a time"


# ---------------------------------------------------------------- start a saved workflow (PR-D.2)

REVIEW_FILE = """---
title: Proposal review
version: 3
files: one
---

| step       | kind  | worker          | reads          |
|------------|-------|-----------------|----------------|
| generalist | agent | generalist @ 5  | request, files |
| colleague  | agent | colleague @ 2   | request, files |
| outsider   | agent | outsider        | request, files |

## Request

Review this proposal against the solicitation

## Step: colleague

Read it as a colleague would
"""


class _Skills:
    """The skill catalog, as each user reaches it: an owner, a file, and who it is shared with."""

    def __init__(self) -> None:
        self.files: Dict[str, str] = {"wf-key": REVIEW_FILE}
        self.skill = SimpleNamespace(
            skill_id="skill-1",
            owner_id="alice",
            status="active",
            resources=[SimpleNamespace(kind="reference", filename="workflow.review.md", path="workflow.review.md", s3_key="wf-key")],
        )
        self.shared_with: set = set()

    async def get_skill(self, skill_id: str):
        return self.skill if skill_id == self.skill.skill_id else None

    async def accessible(self, user) -> List[str]:
        return [self.skill.skill_id] if user.user_id in self.shared_with else []


@pytest.fixture()
def skills(monkeypatch, world) -> _Skills:
    from apis.shared.workflows import sheet

    s = _Skills()
    store = SimpleNamespace(get=lambda key: s.files[key].encode("utf-8"))
    service = SimpleNamespace(bundle_files=SimpleNamespace(resource_store=store))

    async def lookup(assistant_id: str, user_id: str, user_email: str = None):
        if (assistant_id, user_id) in world.agent_users:
            return world.agents[assistant_id], "viewer"
        return None, None

    monkeypatch.setattr(sheet, "get_skill_catalog_repository", lambda: s)
    monkeypatch.setattr(sheet, "resolve_accessible_skill_ids", s.accessible)
    monkeypatch.setattr(sheet, "get_user_skill_service", lambda: service)
    monkeypatch.setattr(sheet, "get_assistant_with_access_check", lambda **kw: lookup(**kw))
    monkeypatch.setenv("WORKFLOWS_ENABLED", "true")
    world.agents["generalist"].listing = SimpleNamespace(published_version=6)
    world.agents["colleague"].listing = SimpleNamespace(published_version=2)
    return s


def _start(client: TestClient, **workflow: Any):
    ref: Dict[str, Any] = {"skillId": "skill-1", "name": "review"}
    ref.update(workflow)
    return client.post("/sessions/src/workflow-runs", json={"workflow": ref})


def test_a_saved_workflow_builds_the_files_steps_and_records_where_it_came_from(make_client, stores, skills, metrics):
    from apis.shared.workflows.format import read_workflow_file, workflow_fingerprint

    handoffs, runs = stores
    resp = _start(make_client())

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert [s["agentName"] for s in data["steps"]] == ["Generalist", "Colleague", "Emotional outsider"]
    shared = "Review this proposal against the solicitation"
    assert [s["request"] for s in data["steps"]] == [shared, "Read it as a colleague would", shared]
    assert data["run"]["request"] == shared
    # Priced exactly as a run of mentions prices a step.
    assert [(s.get("modelId"), s.get("inputPricePerMillionTokens")) for s in data["steps"]] == [
        ("us.sonnet", 3.0),
        ("us.haiku", 1.0),
        (None, None),
    ]

    stored = runs.get("alice", data["run"]["runId"])
    workflow, _ = read_workflow_file(REVIEW_FILE, name="review")
    assert stored.workflow.model_dump(by_alias=True, exclude_none=True) == {
        "skillId": "skill-1",
        "name": "review",
        "title": "Proposal review",
        "fingerprint": workflow_fingerprint(workflow),
        "version": 3,
        "files": "one",
    }
    # Each step's name in the file, and the Agent version the run was started with (§10).
    assert [(s.step_name, s.agent_version) for s in stored.steps] == [("generalist", 6), ("colleague", 2), ("outsider", None)]
    assert data["run"]["workflow"]["title"] == "Proposal review"
    assert [s.get("stepName") for s in data["run"]["steps"]] == ["generalist", "colleague", "outsider"]
    for step in data["steps"]:
        assert handoffs.get("alice", step["handoffId"]).run_id == stored.run_id
    assert metrics[-1] == (
        "WorkflowRunCreated",
        {"session_id": "src", "run_id": stored.run_id, "step_count": 3, "run_source": "workflow"},
    )


def test_a_step_the_caller_cant_run_refuses_with_the_sheets_blocker_until_it_is_removed(make_client, stores, skills, world):
    world.agent_users.discard(("outsider", "alice"))
    client = make_client()

    refused = _start(client)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "workflow_blocked"
    assert detail["blockers"] == [
        {
            "code": "step_unavailable",
            "step": "outsider",
            "message": "You can't use this step's Agent. Remove the step, or ask its owner to share it",
        }
    ]
    assert detail["message"].startswith("You can't use this step's Agent")
    assert stores[0].list_for_session("alice", "src") == []

    started = _start(client, removedSteps=["outsider"])
    assert started.status_code == 201
    assert [s["agentName"] for s in started.json()["steps"]] == ["Generalist", "Colleague"]
    assert started.json()["run"]["workflow"]["removedSteps"] == ["outsider"]


def test_removing_a_step_removes_every_step_that_reads_it():
    from apis.shared.workflows.format import WorkflowResult, Workflow, WorkflowStep
    from apis.shared.workflows.sheet import without_steps

    workflow = Workflow(
        name="w",
        title="W",
        steps=(
            WorkflowStep(name="a", agent_id="A"),
            WorkflowStep(name="b", agent_id="B", reads=("request", "a")),
            WorkflowStep(name="c", agent_id="C", reads=("b",)),
            WorkflowStep(name="d", agent_id="D"),
        ),
        result=WorkflowResult(reads=("c", "d")),
    )
    trimmed, removed = without_steps(workflow, ["a", "nope"])

    assert removed == ("a", "b", "c")
    assert [s.name for s in trimmed.steps] == ["d"]
    assert trimmed.result.reads == ("d",)


@pytest.mark.parametrize(
    "file_text, code",
    [
        (REVIEW_FILE.replace("files: one", "files: one\nresult: merge"), "merge_result"),
        (REVIEW_FILE.replace("colleague @ 2", "generalist"), "same_agent_twice"),
    ],
)
def test_what_step_one_cant_run_is_refused_at_create(make_client, stores, skills, file_text, code):
    skills.files["wf-key"] = file_text
    resp = _start(make_client())
    assert resp.status_code == 422
    assert code in [b["code"] for b in resp.json()["detail"]["blockers"]]
    assert stores[0].list_for_session("alice", "src") == []


def test_too_few_steps_left_after_removal_is_refused(make_client, skills):
    resp = _start(make_client(), removedSteps=["colleague", "outsider"])
    assert resp.status_code == 422
    assert [b["code"] for b in resp.json()["detail"]["blockers"]] == ["too_few_steps"]


def test_the_workflow_is_read_as_the_caller(make_client, world, skills):
    """Another user's private workflow is a 404, as one that doesn't exist is; shared, it starts."""
    world.sessions[("src", "bob")] = SimpleNamespace(title="Bob's thread", preferences=None)
    world.agent_users |= {(a, "bob") for a in world.agents}

    assert _start(make_client(BOB)).status_code == 404
    assert _start(make_client(), name="no-such").status_code == 404
    assert _start(make_client(), name="Bad Name").status_code == 404

    skills.shared_with.add("bob")
    assert _start(make_client(BOB)).status_code == 201


def test_a_shared_workflow_judges_each_agent_as_the_starter(make_client, world, skills):
    world.sessions[("src", "bob")] = SimpleNamespace(title="Bob's thread", preferences=None)
    world.agent_users |= {("generalist", "bob"), ("colleague", "bob")}
    skills.shared_with.add("bob")

    resp = _start(make_client(BOB))
    assert resp.status_code == 422
    assert [(b["code"], b["step"]) for b in resp.json()["detail"]["blockers"]] == [("step_unavailable", "outsider")]


def test_a_workflow_start_is_404_while_workflows_is_off(make_client, skills, monkeypatch):
    monkeypatch.setenv("WORKFLOWS_ENABLED", "")
    assert _start(make_client()).status_code == 404
    # A run of mentions is step 1's, and does not need the flag.
    assert _create(make_client()).status_code == 201


def test_steps_and_a_workflow_together_are_422(make_client, skills):
    resp = make_client().post(
        "/sessions/src/workflow-runs",
        json={"workflow": {"skillId": "skill-1", "name": "review"}, "steps": [{"agentId": "generalist"}]},
    )
    assert resp.status_code == 422


def test_a_file_that_no_longer_parses_is_422(make_client, skills):
    skills.files["wf-key"] = "not a workflow"
    resp = _start(make_client())
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "files, file_ids, ok",
    [
        ("one", [], False),
        ("one", ["f1"], True),
        ("one", ["f1", "f2"], False),
        ("none", ["f1"], False),
        ("none", [], True),
        ("any", ["f1", "f2"], True),
    ],
)
def test_the_files_rule_is_enforced_at_approve(make_client, skills, files, file_ids, ok):
    skills.files["wf-key"] = REVIEW_FILE.replace("files: one", f"files: {files}")
    client = make_client()
    run_id = _start(client).json()["run"]["runId"]

    resp = _approve(client, run_id, fileIds=file_ids)

    assert resp.status_code == (200 if ok else 422), resp.text
    if not ok:
        # Refused before the write: the sheet can try again.
        assert _approve(client, run_id, fileIds=["f1"] if files == "one" else []).status_code == 200


def test_a_run_of_mentions_has_no_files_rule(make_client):
    client = make_client()
    run_id = _create(client).json()["run"]["runId"]
    assert _approve(client, run_id, fileIds=[]).status_code == 200


def test_a_run_row_without_the_workflow_fields_still_reads(table):
    """Rows written before PR-D.2 carry no ``workflow`` and no step ``stepName``/``agentVersion``."""
    from apis.shared.handoff.runs import WorkflowRunView

    table.put_item(
        Item={
            "PK": "USER#alice",
            "SK": "WFRUN#old",
            "GSI_PK": "SESSION#src",
            "GSI_SK": "WFRUN#2026-11-01T00:00:00+00:00",
            "runId": "old",
            "userId": "alice",
            "sessionId": "src",
            "status": "approved",
            "request": "r",
            "steps": [{"stepId": "s1", "handoffId": "h1", "agentId": "a1", "agentName": "A", "request": "r", "removed": False}],
            "fileIds": [],
            "limits": {"maxSteps": 8, "concurrency": 4},
            "sourceMessageIndex": 1,
            "createdAt": "2026-11-01T00:00:00+00:00",
            "updatedAt": "2026-11-01T00:00:00+00:00",
        }
    )
    run = WorkflowRunStore(table=table).get("alice", "old")

    assert run.workflow is None and run.steps[0].step_name is None and run.steps[0].agent_version is None
    view = WorkflowRunView.from_row(run).model_dump(by_alias=True, exclude_none=True)
    assert "workflow" not in view and "stepName" not in view["steps"][0]


# ---------------------------------------------------------------- stages (PR-D.5a)

STAGED_FILE = """---
title: Panel review
version: 1
---

| step       | kind  | worker     | reads                           |
|------------|-------|------------|---------------------------------|
| generalist | agent | generalist | request, files                  |
| colleague  | agent | colleague  | request, files                  |
| summary    | agent | fiscal     | request, generalist, colleague  |

## Request

Review this proposal

## Step: summary

Summarize the reviews

## Result

Reads: summary
"""


@pytest.fixture()
def staged(skills):
    skills.files["wf-key"] = STAGED_FILE
    return skills


def _prepare(client: TestClient, run_id: str, step_id: str):
    return client.post(f"/sessions/src/workflow-runs/{run_id}/steps/{step_id}/prepare")


def test_a_step_that_reads_another_starts_and_keeps_its_reads(make_client, stores, staged):
    resp = _start(make_client())

    assert resp.status_code == 201, resp.text
    run = stores[1].get("alice", resp.json()["run"]["runId"])
    assert [(s.step_name, s.reads) for s in run.steps] == [
        ("generalist", None), ("colleague", None), ("summary", ["generalist", "colleague"]),
    ]
    assert run.workflow.result_reads == ["summary"]
    assert [s.get("reads") for s in resp.json()["run"]["steps"]] == [None, None, ["generalist", "colleague"]]


def test_the_sheet_no_longer_blocks_a_step_that_reads_another():
    from apis.shared.workflows.format import read_workflow_file
    from apis.shared.workflows.sheet import AgentView, sheet_for

    workflow, _ = read_workflow_file(STAGED_FILE, name="review")
    agents = {s.agent_id: AgentView(can_run=True, name=s.agent_id) for s in workflow.steps}
    assert sheet_for(workflow, agents).model_dump() == {"startable": True, "blockers": []}


def test_a_mention_run_has_no_reads_and_its_row_is_unchanged(make_client, stores):
    created = _create(make_client()).json()

    item = stores[1]._table.get_item(Key={"PK": "USER#alice", "SK": f"WFRUN#{created['run']['runId']}"})["Item"]
    assert all("reads" not in step for step in item["steps"])
    assert "workflow" not in item
    assert all("reads" not in step for step in created["run"]["steps"])


def test_approve_refuses_a_kept_step_that_reads_a_removed_one(make_client, stores, staged):
    client = make_client()
    created = _start(client).json()
    rows = created["steps"]

    refused = _approve(client, created["run"]["runId"], steps=[{"stepId": rows[0]["stepId"], "removed": True}])
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Remove 'summary' too: it reads 'generalist', which was removed"
    assert stores[1].get("alice", created["run"]["runId"]).status == "open"

    ok = _approve(
        client,
        created["run"]["runId"],
        steps=[{"stepId": rows[0]["stepId"], "removed": True}, {"stepId": rows[2]["stepId"], "removed": True}],
    )
    assert ok.status_code == 200, ok.text


def _staged_run(client: TestClient, stores, steps: _Steps):
    """An approved staged run: the generalist finished, the colleague stopped, the summary waits."""
    handoffs, _runs = stores
    created = _start(client).json()
    run_id = created["run"]["runId"]
    assert _approve(client, run_id).status_code == 200
    rows = created["steps"]
    for index, step in enumerate(rows[:2]):
        handoffs.accept("alice", step["handoffId"], target_session_id=f"t{index}", context_edited=False, context_removed=False)
    steps.target(
        "t0", rows[0]["handoffId"], "Generalist · Review of the proposal",
        [(1, "Strong aims; the budget is thin.", "2026-01-01T00:00:00Z"), (3, "Later work in this chat.", "2999-01-01T00:00:00Z")],
    )
    steps.target("t1", rows[1]["handoffId"], "Colleague · Review", [])
    steps.heads[("alice", "t0")] = [
        {"artifact_id": "memo", "version": 2, "title": "Memo", "content_type": "text/markdown", "produced_by_message_index": 1},
    ]
    steps.files["t0"] = [_file("own-1", "t0", "2026-01-01T00:00:00Z")]
    return run_id, rows


def test_prepare_is_409_until_every_read_step_has_settled(collect_client, stores, steps, staged):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)

    waiting = _prepare(client, run_id, rows[2]["stepId"])
    assert waiting.status_code == 409
    assert waiting.json()["detail"] == "Waiting for Generalist, Colleague to finish"

    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "needs_input")
    assert _prepare(client, run_id, rows[2]["stepId"]).json()["detail"] == "Waiting for Colleague to finish"


def test_prepare_renders_the_read_results_bounded_in_step_order_with_a_stopped_step_named(collect_client, stores, steps, staged):
    from apis.shared.handoff.context import DELIVERY_PART_STATUS_NOTES
    from apis.shared.handoff.delivery import SEND_BACK_MAX_CHARS

    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    long_answer = "x" * (SEND_BACK_MAX_CHARS * 2)
    steps.messages["t0"][0] = MessageResponse(
        id="msg-t0-1", role="assistant", content=[{"type": "text", "text": long_answer}], created_at="2026-01-01T00:00:00Z"
    )
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "stopped", stoppedReason="user_stopped")

    resp = _prepare(client, run_id, rows[2]["stepId"])

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [(p["stepName"], p["agentName"], p["status"]) for p in data["parts"]] == [
        ("generalist", "Generalist", "done"), ("colleague", "Colleague", "stopped"),
    ]
    block = data["readsBlock"]
    first, second = block.split("\n\n<handoff_context ")
    assert first.startswith('<handoff_context from_conversation="Generalist · Review of the proposal">')
    assert len(first) < SEND_BACK_MAX_CHARS + 400  # bounded as a send-back's text is
    assert "Later work in this chat." not in block  # the answer as it stood when the step finished
    assert second.startswith('from_conversation="Colleague">')
    assert DELIVERY_PART_STATUS_NOTES["stopped"] in second
    # Text only: the artifacts and files are references the reading conversation couldn't open.
    assert "<artifact " not in block and "<file " not in block
    assert data["message"] == f"{block}\n\nSummarize the reviews"


def test_prepare_uses_the_same_blocks_collect_would(collect_client, stores, steps, staged):
    from apis.shared.handoff.context import DeliveryPartBlock, render_delivery_parts

    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "stopped")

    block = _prepare(client, run_id, rows[2]["stepId"]).json()["readsBlock"]

    expected = render_delivery_parts(
        [
            DeliveryPartBlock(
                text="Strong aims; the budget is thin.",
                agent_name="Generalist",
                from_title="Generalist · Review of the proposal",
                status="done",
            ),
            DeliveryPartBlock(text="", agent_name="Colleague", from_title="", status="stopped"),
        ]
    )
    assert block == expected


def test_prepare_is_idempotent_and_writes_nothing(collect_client, stores, steps, staged, table):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "done")
    before = table.scan()["Items"]

    first = _prepare(client, run_id, rows[2]["stepId"])
    second = _prepare(client, run_id, rows[2]["stepId"])

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert sorted(map(str, table.scan()["Items"])) == sorted(map(str, before))


def test_every_read_step_stopped_still_names_them(collect_client, stores, steps, staged):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "stopped")
    stores[0].transition("alice", rows[1]["handoffId"], "stopped")

    data = _prepare(client, run_id, rows[2]["stepId"]).json()

    assert [p["status"] for p in data["parts"]] == ["stopped", "stopped"]
    assert data["readsBlock"].count("<handoff_context ") == 2


def test_a_step_that_reads_nothing_prepares_as_its_request(collect_client, stores, steps, staged):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)

    data = _prepare(client, run_id, rows[0]["stepId"]).json()

    assert data == {"stepId": rows[0]["stepId"], "message": "Review this proposal", "readsBlock": "", "parts": []}


def test_prepare_is_owner_scoped_bound_to_its_conversation_and_flag_gated(collect_client, stores, steps, staged, monkeypatch):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "done")

    assert _prepare(collect_client(BOB), run_id, rows[2]["stepId"]).status_code == 404
    assert client.post(f"/sessions/other/workflow-runs/{run_id}/steps/{rows[2]['stepId']}/prepare").status_code == 404
    assert _prepare(client, run_id, "no-such-step").status_code == 404
    monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "false")
    assert _prepare(client, run_id, rows[2]["stepId"]).status_code == 404


def test_prepare_refuses_a_run_never_approved(make_client, staged):
    client = make_client()
    created = _start(client).json()
    assert _prepare(client, created["run"]["runId"], created["steps"][2]["stepId"]).status_code == 409


def test_collect_takes_only_the_steps_the_result_reads(collect_client, stores, steps, staged):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "done")
    stores[0].accept("alice", rows[2]["handoffId"], target_session_id="t2", context_edited=False, context_removed=False)
    steps.target("t2", rows[2]["handoffId"], "Fiscal outsider · Summary", [(1, "Both reviews agree.", "2026-01-01T00:00:00Z")])
    stores[0].transition("alice", rows[2]["handoffId"], "done")

    parts = _collect_run(client, run_id).json()["delivery"]["parts"]

    assert [(p["stepId"], p["status"], p["text"]) for p in parts] == [(rows[2]["stepId"], "done", "Both reviews agree.")]


def test_collect_takes_every_kept_step_when_the_result_names_none(collect_client, stores, steps, staged):
    staged.files["wf-key"] = STAGED_FILE.replace("\nReads: summary\n", "\n")
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")

    parts = _collect_run(client, run_id).json()["delivery"]["parts"]

    assert [p["stepId"] for p in parts] == [r["stepId"] for r in rows]


def test_prepare_reads_only_the_answers_never_the_artifacts_or_files(collect_client, stores, steps, staged, monkeypatch):
    client = collect_client()
    run_id, rows = _staged_run(client, stores, steps)
    stores[0].transition("alice", rows[0]["handoffId"], "done")
    stores[0].transition("alice", rows[1]["handoffId"], "done")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prepare carries no artifact or file, so it never looks them up")

    monkeypatch.setattr(workflow_runs, "offered_files", refuse)
    monkeypatch.setattr(workflow_runs, "offered_artifacts", refuse)

    resp = _prepare(client, run_id, rows[2]["stepId"])

    assert resp.status_code == 200, resp.text
    assert "Strong aims; the budget is thin." in resp.json()["readsBlock"]
