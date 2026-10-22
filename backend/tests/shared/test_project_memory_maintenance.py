"""Maintenance runs on a project's shared memory (Shared Projects 2.6a).

Real ``MemorySpaceService``, ``ProjectService`` and ``NotificationService``
over moto, as in the 2.5a suite. The model is replaced by a scripted planner,
and the worker invoke by a recorder: what is under test is everything around
the model call — the trigger's guards, the snapshot, the verifier's verdicts
reaching the run, the compaction proposal, metering, notifications, and an
approval that applies ops by anchor to a file that may have moved on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.memory.maintenance.planner import Plan, Planner, PlannerError
from apis.shared.memory.maintenance.runner import MaintenanceRunner
from apis.shared.memory.maintenance.verify import PlannedChange
from apis.shared.memory.service import (
    MemorySpaceConcurrencyError,
    MemoryValidationError,
)
from apis.shared.notifications.service import NotificationService
from apis.shared.projects.memory_maintenance import (
    LOCK_LEASE_SECONDS,
    MaintenanceModel,
    MaintenanceRequestError,
    ProjectMemoryMaintenance,
)
from apis.shared.projects.memory_proposals import ProjectMemoryProposals

from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    _team,
    env,
    gateway,
    memory,
    projects,
)
from tests.shared.test_project_settings import AuditRecorder
from tests.shared.test_projects import TABLE as PROJECTS_TABLE

NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
MODEL = MaintenanceModel(model_id="us.anthropic.test-model", input_price_per_million_tokens=1.0,
                         output_price_per_million_tokens=5.0)
FILE = (
    "- Batch Canvas enrollment calls in groups of 50.\n"
    "- Canvas enrollment calls go in batches of 50.\n"
    "- Kickoff meeting with Priya on 2026-09-14.\n"
    "- Term codes are YYYYTT because the SIS requires it.\n"
)
MERGED = "Batch Canvas enrollment calls in groups of 50."
TIDY = [
    PlannedChange(type="merge", ids=(1, 2), text=MERGED, why="The same rule twice"),
    PlannedChange(type="prune", ids=(3,), reason="expired", why="The meeting happened"),
]


class ScriptedPlanner:
    """Answers every file with ``changes`` (or raises), and records what it was shown."""

    def __init__(self, changes: Optional[List[PlannedChange]] = None, error: Optional[PlannerError] = None):
        self.changes = list(changes or [])
        self.error = error
        self.seen: List[str] = []
        self.sizes: List[object] = []

    def plan(self, slug, description, items, *, pinned=(), provenance=None, today, size=None):
        self.seen.append(slug)
        self.sizes.append(size)
        if self.error is not None:
            raise self.error
        return Plan(changes=list(self.changes), input_tokens=1_000, output_tokens=200)


@pytest.fixture()
def inbox(env) -> NotificationService:
    return NotificationService(table_name=PROJECTS_TABLE)


@pytest.fixture()
def team(projects):
    return _team(projects)


@pytest.fixture()
def invoked() -> List[tuple]:
    return []


@pytest.fixture()
def maintenance(projects, memory, invoked, monkeypatch) -> ProjectMemoryMaintenance:
    monkeypatch.setenv("MEMORY_MAINTENANCE_FUNCTION_NAME", "maintenance-worker")
    return ProjectMemoryMaintenance(
        repository=projects.repository, memory=memory, audit=AuditRecorder(),
        invoke=lambda name, payload: invoked.append((name, payload)), clock=lambda: NOW,
    )


@pytest.fixture()
def proposals(projects, memory, inbox) -> ProjectMemoryProposals:
    return ProjectMemoryProposals(repository=projects.repository, memory=memory, notifications=inbox, audit=AuditRecorder())


def runner_for(memory, projects, inbox, planner) -> MaintenanceRunner:
    return MaintenanceRunner(
        memory=memory, projects=projects.repository, notifications=inbox,
        planner_factory=lambda model_id: planner, clock=lambda: NOW, concurrency=2,
    )


def _save(memory, team, slug, text, user: User = EDITOR):
    return memory.save_entry(team.shared_space_id, user.user_id, user.email, slug, text)


def _run(maintenance, memory, projects, inbox, team, planner, *, slug=None, user: User = EDITOR):
    queued = maintenance.start(team.project_id, user, MODEL, slug=slug)
    return runner_for(memory, projects, inbox, planner).run(team.shared_space_id, queued.run_id)


def _kinds(inbox, user: User, kind: str) -> List[dict]:
    return [n.payload for n in inbox.list(user.email)[0] if n.kind == kind]


def _texts(memory, team, slug="canvas") -> List[str]:
    _, items, _ = memory.read_file_items(team.shared_space_id, OWNER.user_id, OWNER.email, slug)
    return [i.text for i in items]


class TestStart:
    def test_an_editor_queues_a_run_and_the_worker_is_invoked(self, maintenance, memory, invoked, team):
        run = maintenance.start(team.project_id, EDITOR, MODEL)
        assert (run.state, run.requested_by_email, run.model_id, run.slug) == ("queued", EDITOR.email, MODEL.model_id, None)
        assert invoked == [("maintenance-worker", {"spaceId": team.shared_space_id, "runId": run.run_id})]
        stored = memory.repository.get_maintenance_run(team.shared_space_id, run.run_id)
        assert stored.input_price_per_million_tokens == 1.0
        assert maintenance.audit.records[-1]["action"] == "project.memory_maintenance_started"

    @pytest.mark.parametrize("user, status", [(VIEWER, 403), (STRANGER, 404)])
    def test_only_the_owner_and_editors(self, maintenance, team, user, status):
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.start(team.project_id, user, MODEL)
        assert e.value.status_code == status

    def test_one_run_at_a_time_and_an_archived_project_takes_none(self, maintenance, projects, team):
        maintenance.start(team.project_id, EDITOR, MODEL)
        with pytest.raises(MaintenanceRequestError, match="already running") as e:
            maintenance.start(team.project_id, OWNER, MODEL)
        assert e.value.status_code == 409
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.start(team.project_id, OWNER, MODEL)
        assert e.value.status_code == 409

    def test_a_named_file_must_exist(self, maintenance, team):
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.start(team.project_id, EDITOR, MODEL, slug="nope")
        assert e.value.status_code == 404

    def test_without_a_worker_nothing_is_written(self, maintenance, memory, team, monkeypatch):
        monkeypatch.delenv("MEMORY_MAINTENANCE_FUNCTION_NAME")
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.start(team.project_id, EDITOR, MODEL)
        assert e.value.status_code == 503
        assert memory.repository.list_maintenance_runs(team.shared_space_id, 10) == []

    def test_a_failed_invoke_fails_the_run_and_frees_the_slot(self, projects, memory, team, monkeypatch):
        monkeypatch.setenv("MEMORY_MAINTENANCE_FUNCTION_NAME", "maintenance-worker")

        def refuse(name, payload):
            raise RuntimeError("throttled")

        broken = ProjectMemoryMaintenance(repository=projects.repository, memory=memory, invoke=refuse, clock=lambda: NOW)
        with pytest.raises(MaintenanceRequestError) as e:
            broken.start(team.project_id, EDITOR, MODEL)
        assert e.value.status_code == 503
        [run] = memory.repository.list_maintenance_runs(team.shared_space_id, 10)
        assert run.state == "failed"
        assert memory.repository.acquire_maintenance_lock(team.shared_space_id, "next", now=int(NOW.timestamp()), lease_seconds=60)

    def test_a_run_that_never_finished_reads_as_failed_after_its_lease(self, maintenance, team):
        run = maintenance.start(team.project_id, EDITOR, MODEL)
        assert maintenance.get_run(team.project_id, OWNER, run.run_id).state == "queued"
        maintenance._clock = lambda: NOW + timedelta(seconds=LOCK_LEASE_SECONDS + 1)
        late = maintenance.get_run(team.project_id, OWNER, run.run_id)
        assert late.state == "failed" and "didn't finish" in late.error


class TestRun:
    def test_snapshot_plan_verify_and_propose(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        _save(memory, team, "tiny", "- Only one item here.\n")
        planner = ScriptedPlanner(TIDY)
        run = _run(maintenance, memory, projects, inbox, team, planner)

        assert run.state == "done" and run.error is None
        assert sorted(e.slug for e in run.snapshot.entries) == ["canvas", "tiny"]
        results = {r.slug: r for r in run.results}
        assert results["tiny"].outcome == "nothing_to_do" and planner.seen == ["canvas"]
        assert (results["canvas"].outcome, results["canvas"].planned, results["canvas"].kept) == ("proposed", 2, 2)

        proposal = memory.repository.get_proposal(team.shared_space_id, results["canvas"].proposal_id)
        assert (proposal.kind, proposal.proposer_kind, proposal.proposer_email, proposal.run_id) == (
            "compaction", "maintenance", EDITOR.email, run.run_id,
        )
        assert [op.type for op in proposal.ops] == ["merge", "prune"]
        assert "Canvas enrollment calls go in batches" not in proposal.text and "Kickoff" not in proposal.text
        assert _texts(memory, team) == [l[2:] for l in FILE.splitlines()]  # memory itself is untouched

    def test_the_run_is_metered_announced_and_frees_the_slot(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        run = _run(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))

        assert (run.input_tokens, run.output_tokens) == (1_000, 200)
        assert run.cost == pytest.approx(0.002)
        month = projects.repository._table.get_item(
            Key={"PK": f"PROJECT#{team.project_id}", "SK": "COST#2026-10"}
        )["Item"]
        assert month["totalCost"] == Decimal("0.002") and month["inputTokens"] == 1_000
        mine = projects.repository._table.get_item(
            Key={"PK": f"PROJECT#{team.project_id}", "SK": f"COST#2026-10#USER#{EDITOR.user_id}"}
        )["Item"]
        assert mine["outputTokens"] == 200

        assert _kinds(inbox, OWNER, "project_memory_maintenance") == [
            {"runId": run.run_id, "fileCount": 1, "slugs": ["canvas"]}
        ]
        assert _kinds(inbox, EDITOR, "project_memory_maintenance") == []  # who ran it
        assert _kinds(inbox, VIEWER, "project_memory_maintenance") == []
        assert maintenance.start(team.project_id, OWNER, MODEL).state == "queued"

    def test_a_file_with_maintenance_waiting_is_skipped(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        _run(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))
        planner = ScriptedPlanner(TIDY)
        again = _run(maintenance, memory, projects, inbox, team, planner)
        assert [r.outcome for r in again.results] == ["pending_review"] and planner.seen == []

    def test_dropped_ops_and_a_failed_call_never_reach_a_reviewer(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        invented = ScriptedPlanner([PlannedChange(type="merge", ids=(1, 2), text="Batch calls in groups of 100.")])
        run = _run(maintenance, memory, projects, inbox, team, invented)
        [result] = run.results
        assert (result.outcome, result.planned, result.dropped) == ("nothing_to_do", 1, 1)

        failing = ScriptedPlanner(error=PlannerError("The planner's answer was cut off.", input_tokens=500, output_tokens=4_000))
        run = _run(maintenance, memory, projects, inbox, team, failing)
        assert [r.outcome for r in run.results] == ["failed"] and run.input_tokens == 500
        assert memory.repository.list_proposals(team.shared_space_id) == []

    def test_one_file_runs_alone(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        _save(memory, team, "other", FILE)
        planner = ScriptedPlanner([])
        run = _run(maintenance, memory, projects, inbox, team, planner, slug="other")
        assert (run.slug, planner.seen) == ("other", ["other"])

    def test_files_left_when_time_runs_out_are_not_reached(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        queued = maintenance.start(team.project_id, EDITOR, MODEL)
        planner = ScriptedPlanner(TIDY)
        run = runner_for(memory, projects, inbox, planner).run(
            team.shared_space_id, queued.run_id, remaining_seconds=lambda: 60.0
        )
        assert run.state == "done" and [r.outcome for r in run.results] == ["not_reached"] and planner.seen == []

    def test_a_retried_invoke_does_nothing(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        run = _run(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))
        planner = ScriptedPlanner(TIDY)
        again = runner_for(memory, projects, inbox, planner).run(team.shared_space_id, run.run_id)
        assert again.state == "done" and planner.seen == []

    def test_an_archived_project_fails_the_run(self, maintenance, memory, projects, inbox, team):
        queued = maintenance.start(team.project_id, EDITOR, MODEL)
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        run = runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(team.shared_space_id, queued.run_id)
        assert run.state == "failed" and "archived" in run.error


class TestApprove:
    def _proposal(self, maintenance, memory, projects, inbox, team):
        _save(memory, team, "canvas", FILE)
        run = _run(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))
        return memory.repository.get_proposal(team.shared_space_id, run.results[0].proposal_id)

    def test_approving_applies_every_op_and_archives_with_reasons(self, maintenance, memory, projects, inbox, proposals, team):
        proposal = self._proposal(maintenance, memory, projects, inbox, team)
        decided, result = proposals.approve(team.project_id, OWNER, proposal.proposal_id)

        assert decided.applied_ops == [0, 1] and decided.state == "approved"
        assert _texts(memory, team) == [MERGED, "Term codes are YYYYTT because the SIS requires it."]
        latest = memory.list_file_versions(team.shared_space_id, OWNER.user_id, OWNER.email, "canvas")[0]
        assert (latest.reason, latest.proposal_id, latest.run_id) == ("maintenance", proposal.proposal_id, proposal.run_id)
        archived = {a.text: (a.reason, a.superseded_by) for a in memory.list_archived_items(team.shared_space_id, OWNER.user_id, OWNER.email)}
        keep = proposal.ops[0].keep
        assert archived == {
            "Canvas enrollment calls go in batches of 50.": ("merged", keep),
            "Kickoff meeting with Priya on 2026-09-14.": ("pruned", None),
        }
        assert _kinds(inbox, EDITOR, "project_proposal_decided")[0]["decision"] == "approved"
        assert proposals.audit.records[-1]["after"]["appliedOps"] == 2

    def test_a_reviewer_picks_ops(self, maintenance, memory, projects, inbox, proposals, team):
        proposal = self._proposal(maintenance, memory, projects, inbox, team)
        for bad in ([7], []):
            with pytest.raises(MemoryValidationError):
                proposals.approve(team.project_id, EDITOR, proposal.proposal_id, ops=bad)
        decided, _ = proposals.approve(team.project_id, EDITOR, proposal.proposal_id, ops=[1])
        assert decided.applied_ops == [1]
        assert "Canvas enrollment calls go in batches of 50." in _texts(memory, team)
        assert "Kickoff meeting with Priya on 2026-09-14." not in _texts(memory, team)

    def test_an_edit_since_the_run_keeps_the_edit_and_skips_what_it_touched(
        self, maintenance, memory, projects, inbox, proposals, team
    ):
        proposal = self._proposal(maintenance, memory, projects, inbox, team)
        _, items, _ = memory.read_file_items(team.shared_space_id, OWNER.user_id, OWNER.email, "canvas")
        edited = "".join(
            f"- {'Canvas enrollment calls go in batches of 25.' if 'batches' in i.text else i.text} <!-- e:{i.anchor} -->\n"
            for i in items
        ) + "- A brand new fact.\n"
        _save(memory, team, "canvas", edited)

        [(listed, stale)] = proposals.list_with_staleness(team.project_id, OWNER, state="pending")
        assert stale is False and "A brand new fact." in listed.text and "Kickoff" not in listed.text

        decided, _ = proposals.approve(team.project_id, OWNER, proposal.proposal_id)
        assert decided.applied_ops == [1]
        assert _texts(memory, team) == [
            "Batch Canvas enrollment calls in groups of 50.",
            "Canvas enrollment calls go in batches of 25.",
            "Term codes are YYYYTT because the SIS requires it.",
            "A brand new fact.",
        ]

    def test_nothing_left_to_apply_is_stale_and_a_conflict(self, maintenance, memory, projects, inbox, proposals, team):
        proposal = self._proposal(maintenance, memory, projects, inbox, team)
        _save(memory, team, "canvas", "- Something else entirely.\n")
        _, stale = proposals.get(team.project_id, OWNER, proposal.proposal_id)
        assert stale is True
        with pytest.raises(MemorySpaceConcurrencyError, match="none of these changes"):
            proposals.approve(team.project_id, OWNER, proposal.proposal_id)

    def test_a_save_from_a_stale_base_is_a_conflict_not_an_overwrite(self, memory, team):
        first = _save(memory, team, "canvas", FILE)
        _save(memory, team, "canvas", FILE + "- A newer fact.\n")
        with pytest.raises(MemorySpaceConcurrencyError):
            memory.save_entry(
                team.shared_space_id, OWNER.user_id, OWNER.email, "canvas", "- Derived from the old file.\n",
                base_content_hash=first.ref.content_hash,
            )
        assert "A newer fact." in _texts(memory, team)

    def test_ops_are_only_for_maintenance_proposals(self, proposals, memory, team):
        entry = proposals.propose(team.project_id, VIEWER, "sis", "- A fact.\n")[0]
        with pytest.raises(MemoryValidationError):
            proposals.approve(team.project_id, OWNER, entry.proposal_id, ops=[0])


class TestRoutes:
    @pytest.fixture()
    def client_for(self, projects, memory, inbox, invoked, monkeypatch):
        from apis.app_api.projects import maintenance_routes, memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setenv("MEMORY_MAINTENANCE_FUNCTION_NAME", "maintenance-worker")
        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(
            maintenance_routes, "_maintenance",
            lambda: ProjectMemoryMaintenance(
                repository=projects.repository, memory=memory,
                invoke=lambda name, payload: invoked.append(payload),
            ),
        )

        async def model():
            return MODEL

        monkeypatch.setattr(maintenance_routes, "resolve_maintenance_model", model)
        monkeypatch.setattr(
            memory_routes, "_proposals",
            lambda: ProjectMemoryProposals(repository=projects.repository, memory=memory, notifications=inbox),
        )
        monkeypatch.setattr(memory_routes, "display_names", lambda emails: {})

        def make(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(maintenance_routes.router)
            app.include_router(memory_routes.router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_start_poll_review(self, client_for, memory, projects, inbox, invoked, team):
        _save(memory, team, "canvas", FILE)
        base = f"/projects/{team.project_id}/memory/maintenance"
        assert client_for(VIEWER).post(base, json={}).status_code == 403
        started = client_for(EDITOR).post(base, json={"slug": "canvas"})
        assert started.status_code == 202 and started.json()["state"] == "queued"
        assert client_for(OWNER).post(base, json={}).status_code == 409
        run_id = started.json()["runId"]

        runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(invoked[0]["spaceId"], invoked[0]["runId"])
        body = client_for(OWNER).get(f"{base}/{run_id}").json()
        assert body["state"] == "done" and body["results"][0]["outcome"] == "proposed"
        assert "snapshot" not in body and "modelId" not in body and "inputPricePerMillionTokens" not in body
        assert [r["runId"] for r in client_for(EDITOR).get(base).json()["runs"]] == [run_id]
        assert client_for(VIEWER).get(base).status_code == 403

        proposals_base = f"/projects/{team.project_id}/memory/proposals"
        [row] = client_for(OWNER).get(f"{proposals_base}?state=pending").json()["proposals"]
        assert row["kind"] == "compaction" and row["runId"] == run_id
        assert [op["type"] for op in row["ops"]] == ["merge", "prune"] and row["ops"][0]["why"]
        approved = client_for(OWNER).post(f"{proposals_base}/{row['proposalId']}/approve", json={"ops": [0]})
        assert approved.status_code == 200 and approved.json()["appliedOps"] == [0]


class TestPlanner:
    class Client:
        def __init__(self, response):
            self.response = response
            self.calls: List[dict] = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            return self.response

    def _response(self, content, stop="tool_use"):
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 900, "outputTokens": 80},
        }

    def test_a_forced_tool_call_becomes_planned_changes(self):
        from apis.shared.memory.format import Item

        tool = {"toolUse": {"toolUseId": "t1", "name": "propose_changes", "input": {"changes": [
            {"type": "merge", "items": [1, "2"], "text": "x", "why": "dup"}, "junk",
        ]}}}
        client = self.Client(self._response([tool]))
        plan = Planner("model", client=client).plan(
            "canvas", "About Canvas", (Item("a", "aaaaaaaa"), Item("b", "bbbbbbbb")), pinned=["bbbbbbbb"], today=NOW.date(),
        )
        assert [(c.type, c.ids, c.text) for c in plan.changes] == [("merge", (1, 2), "x")]
        assert (plan.input_tokens, plan.output_tokens) == (900, 80)
        call = client.calls[0]
        assert call["toolConfig"]["toolChoice"] == {"any": {}}
        change_schema = call["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["changes"]["items"]
        assert "text" in change_schema["required"]  # optional, Haiku sometimes left it out of a merge
        message = call["messages"][0]["content"][0]["text"]
        assert "[1] a" in message and "[2] (pinned) b" in message and "Today is 2026-10-07." in message

    @pytest.mark.parametrize("content, stop", [([{"text": "No changes."}], "end_turn"), ([], "max_tokens")])
    def test_an_answer_without_the_tool_fails_but_is_still_metered(self, content, stop):
        with pytest.raises(PlannerError) as e:
            Planner("model", client=self.Client(self._response(content, stop))).plan("canvas", "", (), today=NOW.date())
        assert (e.value.input_tokens, e.value.output_tokens) == (900, 80)


class TestModel:
    def _catalog(self, monkeypatch, rows, default=None):
        from apis.shared.models import managed_models

        async def all_models():
            return rows

        async def default_model():
            return default

        monkeypatch.setattr(managed_models, "list_all_managed_models", all_models)
        monkeypatch.setattr(managed_models, "get_default_managed_model", default_model)

    def _row(self, model_id, provider="bedrock", enabled=True):
        from types import SimpleNamespace

        return SimpleNamespace(model_id=model_id, provider=provider, enabled=enabled,
                               input_price_per_million_tokens=1.1, output_price_per_million_tokens=5.5)

    def test_the_catalog_default_with_its_prices(self, monkeypatch):
        from apis.shared.projects.memory_maintenance import resolve_maintenance_model

        monkeypatch.delenv("MEMORY_MAINTENANCE_MODEL_ID", raising=False)
        self._catalog(monkeypatch, [], default=self._row("us.anthropic.haiku"))
        model = asyncio.run(resolve_maintenance_model())
        assert (model.model_id, model.input_price_per_million_tokens) == ("us.anthropic.haiku", 1.1)

    @pytest.mark.parametrize("override, rows, default", [
        ("", [], None),
        ("us.missing", [], None),
        ("", [], "mantle"),
    ])
    def test_refused_without_a_priced_bedrock_model(self, monkeypatch, override, rows, default):
        from apis.shared.projects.memory_maintenance import resolve_maintenance_model

        monkeypatch.setenv("MEMORY_MAINTENANCE_MODEL_ID", override)
        self._catalog(monkeypatch, rows, default=self._row("openai.gpt", provider=default) if default else None)
        with pytest.raises(MaintenanceRequestError) as e:
            asyncio.run(resolve_maintenance_model())
        assert e.value.status_code == 503


class TestHandler:
    def test_the_lambda_entry_point(self, monkeypatch):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "src/lambdas/memory_maintenance_worker/worker.py"
        spec = importlib.util.spec_from_file_location("memory_maintenance_worker", path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        assert worker.lambda_handler({}, None) == {"result": "invalid_event"}

        calls: List[tuple] = []

        class Runner:
            def run(self, space_id, run_id, *, remaining_seconds: Callable[[], float]):
                calls.append((space_id, run_id, remaining_seconds()))
                raise RuntimeError("boom")

        class Context:
            def get_remaining_time_in_millis(self):
                return 600_000

        monkeypatch.setattr(worker, "MaintenanceRunner", Runner)
        assert worker.lambda_handler({"spaceId": "s", "runId": "r"}, Context()) == {"result": "error", "runId": "r"}
        assert calls == [("s", "r", 600.0)]
