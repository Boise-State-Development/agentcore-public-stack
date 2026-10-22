"""Maintenance on a member's own memory in a project, and its undo (Shared Projects 2.6b).

Same harness as the 2.6a suite: real services over moto, a scripted planner,
and a recorder in place of the worker invoke. What is new here: a run on a
``personal_in_project`` space saves its changes instead of proposing them,
records what it did on the run row, and can be undone file by file from its
snapshot, leaving alone any file saved since.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.memory.maintenance import runner as runner_module
from apis.shared.memory.models import MaintenanceFileResult, MaintenanceOp, OpSource
from apis.shared.memory.repository import MemorySpaceRepository
from apis.shared.memory.service import MemorySpaceService
from apis.shared.projects.memory_maintenance import MaintenanceRequestError, ProjectMemoryMaintenance

from tests.shared.test_memory_spaces import TABLE as MEMORY_TABLE
from tests.shared.test_project_memory_maintenance import (  # noqa: F401 (fixtures)
    FILE,
    MERGED,
    MODEL,
    NOW,
    TIDY,
    ScriptedPlanner,
    inbox,
    invoked,
    maintenance,
    runner_for,
    team,
)
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    BUCKET,
    EDITOR,
    OWNER,
    VIEWER,
    env,
    gateway,
    memory,
    projects,
)

TERM = "Term codes are YYYYTT because the SIS requires it."


def _mine(projects, memory, team, user: User = VIEWER, slug: str = "canvas", text: str = FILE) -> str:
    space_id = projects.get_or_create_personal_space(team.project_id, user)
    memory.save_entry(space_id, user.user_id, user.email, slug, text)
    return space_id


def _texts(memory, space_id, user: User = VIEWER, slug="canvas") -> List[str]:
    _, items, _ = memory.read_file_items(space_id, user.user_id, user.email, slug)
    return [i.text for i in items]


def _run_mine(maintenance, memory, projects, inbox, team, planner, *, user: User = VIEWER, slug=None):
    queued = maintenance.start(team.project_id, user, MODEL, slug=slug, scope="mine")
    space_id = projects.repository.get_personal_space_id(team.project_id, user.user_id)
    return runner_for(memory, projects, inbox, planner).run(space_id, queued.run_id)


class TestStart:
    def test_any_member_tidies_their_own_memory_and_nobody_elses(self, maintenance, memory, projects, team, invoked):
        space_id = _mine(projects, memory, team)
        run = maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")
        assert (run.state, run.scope, run.space_id) == ("queued", "personal_in_project", space_id)
        assert invoked[-1][1] == {"spaceId": space_id, "runId": run.run_id}
        # A member's own memory is theirs and isn't audited (2.8b).
        assert maintenance.audit.records == []
        # The editor has no space of their own here, and can't reach the viewer's.
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.start(team.project_id, EDITOR, MODEL, scope="mine")
        assert e.value.status_code == 409
        assert maintenance.list_runs(team.project_id, VIEWER, scope="mine")[0].run_id == run.run_id
        with pytest.raises(MaintenanceRequestError):
            maintenance.list_runs(team.project_id, EDITOR, scope="mine")

    def test_one_run_at_a_time_on_a_members_memory(self, maintenance, memory, projects, team):
        _mine(projects, memory, team)
        maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")
        with pytest.raises(MaintenanceRequestError, match="your memory") as e:
            maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")
        assert e.value.status_code == 409
        # The shared memory has its own slot.
        assert maintenance.start(team.project_id, EDITOR, MODEL).state == "queued"


class TestApply:
    def test_the_changes_are_saved_archived_and_recorded(self, maintenance, memory, projects, inbox, team):
        space_id = _mine(projects, memory, team)
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))

        assert run.state == "done"
        [result] = run.results
        assert (result.outcome, result.planned, result.kept, result.version) == ("applied", 2, 2, 2)
        assert [op.type for op in result.ops] == ["merge", "prune"]
        assert _texts(memory, space_id) == [MERGED, TERM]
        ref = next(e for e in memory.repository.get_index(space_id).entries if e.slug == "canvas")
        assert result.content_hash == ref.content_hash

        latest = memory.list_file_versions(space_id, VIEWER.user_id, VIEWER.email, "canvas")[0]
        assert (latest.reason, latest.run_id, latest.updated_by) == ("maintenance", run.run_id, VIEWER.user_id)
        archived = {a.text: a.reason for a in memory.list_archived_items(space_id, VIEWER.user_id, VIEWER.email)}
        assert archived == {
            "Canvas enrollment calls go in batches of 50.": "merged",
            "Kickoff meeting with Priya on 2026-09-14.": "pruned",
        }
        # No review queue and no notification: the member is the only person affected.
        assert memory.repository.list_proposals(space_id) == []
        assert [n for u in (OWNER, EDITOR, VIEWER) for n in inbox.list(u.email)[0] if "memory" in n.kind] == []
        # Metered to the project as the member's share, like any run.
        mine = projects.repository._table.get_item(
            Key={"PK": f"PROJECT#{team.project_id}", "SK": f"COST#2026-10#USER#{VIEWER.user_id}"}
        )["Item"]
        assert mine["inputTokens"] == 1_000

    def test_the_shared_memory_still_only_proposes(self, maintenance, memory, projects, inbox, team):
        memory.save_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "canvas", FILE)
        queued = maintenance.start(team.project_id, EDITOR, MODEL)
        run = runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(team.shared_space_id, queued.run_id)
        assert [r.outcome for r in run.results] == ["proposed"] and run.results[0].ops is None
        assert _texts(memory, team.shared_space_id, EDITOR) == [line[2:] for line in FILE.splitlines()]

    def test_a_file_saved_while_the_run_planned_is_left_alone(self, maintenance, memory, projects, inbox, team):
        space_id = _mine(projects, memory, team)

        class EditingPlanner(ScriptedPlanner):
            def plan(self, slug, *args, **kwargs):
                # Its own service: boto3 resources aren't shared across threads.
                other = MemorySpaceService(
                    repository=MemorySpaceRepository(table_name=MEMORY_TABLE), store=memory.store,
                    token_counter=memory._count_tokens,
                )
                other.save_entry(space_id, VIEWER.user_id, VIEWER.email, slug, FILE + "- Added mid-run.\n")
                return super().plan(slug, *args, **kwargs)

        run = _run_mine(maintenance, memory, projects, inbox, team, EditingPlanner(TIDY))
        [result] = run.results
        assert result.outcome == "changed" and "left as it is" in result.error, result.error
        assert "Added mid-run." in _texts(memory, space_id)
        assert "Kickoff meeting with Priya on 2026-09-14." in _texts(memory, space_id)

    def test_a_member_who_left_gets_no_changes(self, maintenance, memory, projects, inbox, team):
        space_id = _mine(projects, memory, team)
        queued = maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")
        projects.remove_member(team.project_id, OWNER, VIEWER.email)
        run = runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(space_id, queued.run_id)
        assert run.state == "failed" and "no longer a member" in run.error
        assert len(memory.repository.list_file_versions(space_id, "canvas")) == 1

    def test_a_run_whose_requester_doesnt_own_the_space_is_refused(self, maintenance, memory, projects, inbox, team):
        space_id = _mine(projects, memory, team)
        queued = maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")
        stored = memory.repository.get_maintenance_run(space_id, queued.run_id)
        stored.requested_by = EDITOR.user_id
        memory.repository.put_maintenance_run(stored, 0)
        run = runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(space_id, queued.run_id)
        assert run.state == "failed" and "Only the member" in run.error

    def test_the_run_row_expires_with_the_personal_archive(self, maintenance, memory, projects, inbox, team):
        space_id = _mine(projects, memory, team)
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))
        item = memory.repository._table.get_item(Key={"PK": f"SPACE#{space_id}", "SK": f"SNAPSHOT#{run.run_id}"})["Item"]
        assert int(item["ttl"]) == int((NOW + timedelta(days=30)).timestamp())

    def test_a_large_run_keeps_counts_once_its_summaries_are_full(self, monkeypatch):
        monkeypatch.setattr(runner_module, "_SUMMARY_BUDGET_BYTES", 400)
        op = MaintenanceOp(type="prune", sources=[OpSource(anchor="a1b2c3d4", text="x" * 200)], reason="expired")
        results = [MaintenanceFileResult(slug=s, outcome="applied", ops=[op]) for s in ("a", "b", "c")]
        runner_module._bound_summaries(results)
        assert [(r.ops is not None, r.ops_omitted) for r in results] == [(True, False), (False, True), (False, True)]


class TestUndo:
    def _applied(self, maintenance, memory, projects, inbox, team, *, files=("canvas",)):
        space_id = None
        for slug in files:
            space_id = _mine(projects, memory, team, slug=slug)
        return space_id, _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner(TIDY))

    def test_undo_puts_the_files_back_as_new_versions(self, maintenance, memory, projects, inbox, team):
        space_id, run = self._applied(maintenance, memory, projects, inbox, team)
        seen = maintenance.get_run(team.project_id, VIEWER, run.run_id, scope="mine")
        assert maintenance.undoable_until(seen) == (NOW + timedelta(days=30)).isoformat()

        undone = maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert undone.undone_at == NOW.isoformat() and undone.undone_by == VIEWER.email
        assert [(r.undo, r.undo_version) for r in undone.results] == [("restored", 3)]
        assert _texts(memory, space_id) == [line[2:] for line in FILE.splitlines()]
        versions = memory.list_file_versions(space_id, VIEWER.user_id, VIEWER.email, "canvas")
        assert [(v.version, v.reason) for v in versions] == [(3, "restore"), (2, "maintenance"), (1, "edit")]
        assert versions[0].run_id == run.run_id
        # The items it brought back left the archive, with the provenance they had.
        assert memory.list_archived_items(space_id, VIEWER.user_id, VIEWER.email) == []
        _, items, provenance = memory.read_file_items(space_id, VIEWER.user_id, VIEWER.email, "canvas")
        kickoff = next(i for i in items if "Kickoff" in i.text)
        assert provenance[kickoff.anchor].added_by == VIEWER.email

        assert maintenance.undoable_until(undone) is None
        with pytest.raises(MaintenanceRequestError, match="already undone") as e:
            maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert e.value.status_code == 409

    def test_a_file_saved_since_the_run_is_left_as_it_is(self, maintenance, memory, projects, inbox, team):
        space_id, run = self._applied(maintenance, memory, projects, inbox, team, files=("canvas", "other"))
        assert sorted(r.outcome for r in run.results) == ["applied", "applied"]
        _, items, _ = memory.read_file_items(space_id, VIEWER.user_id, VIEWER.email, "other")
        edited = "".join(f"- {i.text} <!-- e:{i.anchor} -->\n" for i in items) + "- Written after the tidy-up.\n"
        memory.save_entry(space_id, VIEWER.user_id, VIEWER.email, "other", edited)

        undone = maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert {r.slug: r.undo for r in undone.results} == {"canvas": "restored", "other": "changed"}
        assert "Written after the tidy-up." in _texts(memory, space_id, slug="other")
        assert "Kickoff meeting with Priya on 2026-09-14." not in _texts(memory, space_id, slug="other")
        assert "Kickoff meeting with Priya on 2026-09-14." in _texts(memory, space_id)

    def test_a_file_deleted_since_the_run_is_missing(self, maintenance, memory, projects, inbox, team):
        space_id, run = self._applied(maintenance, memory, projects, inbox, team)
        memory.delete_entry(space_id, VIEWER.user_id, VIEWER.email, "canvas")
        undone = maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert [r.undo for r in undone.results] == ["missing"]

    def test_only_the_member_within_retention_and_never_during_a_run(self, maintenance, memory, projects, inbox, team):
        space_id, run = self._applied(maintenance, memory, projects, inbox, team)
        with pytest.raises(MaintenanceRequestError) as e:
            maintenance.undo(team.project_id, EDITOR, run.run_id)  # no space of their own here
        assert e.value.status_code == 409

        maintenance.start(team.project_id, VIEWER, MODEL, scope="mine")  # holds the slot
        with pytest.raises(MaintenanceRequestError, match="running") as e:
            maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert e.value.status_code == 409
        memory.repository.release_maintenance_lock(space_id, maintenance.list_runs(team.project_id, VIEWER, scope="mine")[0].run_id)

        maintenance._clock = lambda: NOW + timedelta(days=30, seconds=1)
        with pytest.raises(MaintenanceRequestError, match="too old") as e:
            maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert e.value.status_code == 409

    def test_a_run_that_changed_nothing_has_nothing_to_undo(self, maintenance, memory, projects, inbox, team):
        _mine(projects, memory, team)
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner([]))
        assert maintenance.undoable_until(run) is None
        with pytest.raises(MaintenanceRequestError, match="didn't change anything") as e:
            maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert e.value.status_code == 409

    def test_an_archived_project_is_read_only(self, maintenance, memory, projects, inbox, team):
        _, run = self._applied(maintenance, memory, projects, inbox, team)
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        with pytest.raises(MaintenanceRequestError, match="archived") as e:
            maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert e.value.status_code == 409


class TestRoutes:
    @pytest.fixture()
    def client_for(self, projects, memory, invoked, monkeypatch):
        from apis.app_api.projects import maintenance_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setenv("MEMORY_MAINTENANCE_FUNCTION_NAME", "maintenance-worker")
        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(
            maintenance_routes, "_maintenance",
            lambda: ProjectMemoryMaintenance(
                repository=projects.repository, memory=memory, invoke=lambda name, payload: invoked.append(payload),
            ),
        )

        async def model():
            return MODEL

        monkeypatch.setattr(maintenance_routes, "resolve_maintenance_model", model)

        def make(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(maintenance_routes.router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_start_poll_and_undo(self, client_for, memory, projects, inbox, invoked, team):
        space_id = _mine(projects, memory, team)
        base = f"/projects/{team.project_id}/memory/maintenance"
        started = client_for(VIEWER).post(f"{base}?scope=mine", json={})
        assert started.status_code == 202 and started.json()["scope"] == "mine"
        run_id = started.json()["runId"]

        runner_for(memory, projects, inbox, ScriptedPlanner(TIDY)).run(invoked[0]["spaceId"], invoked[0]["runId"])
        body = client_for(VIEWER).get(f"{base}/{run_id}?scope=mine").json()
        [result] = body["results"]
        assert (body["state"], result["outcome"], result["version"]) == ("done", "applied", 2)
        assert result["ops"][0]["sources"][1]["text"] == "Canvas enrollment calls go in batches of 50."
        assert "contentHash" not in result and "snapshot" not in body and body["undoableUntil"]
        # The run is in the member's space: the shared scope doesn't know it.
        assert client_for(OWNER).get(f"{base}/{run_id}").status_code == 404
        assert client_for(OWNER).post(f"{base}/{run_id}/undo").status_code == 409  # no space of their own

        undone = client_for(VIEWER).post(f"{base}/{run_id}/undo")
        assert undone.status_code == 200
        assert undone.json()["undoneAt"] and undone.json()["undoableUntil"] is None
        assert undone.json()["results"][0]["undo"] == "restored"
        assert client_for(VIEWER).post(f"{base}/{run_id}/undo").status_code == 409
        assert "Kickoff meeting with Priya on 2026-09-14." in _texts(memory, space_id)
