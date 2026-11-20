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
    monkeypatch.setattr(workflow_runs, "resolve_effective_model", resolve_effective_model)
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
