"""The saved-workflow routes, ``/workflows`` (``docs/specs/workflow-designer.md`` §9.2, PR-D.1).

What these pin, in order of what would hurt:

* auth runs before the flag, and every route 404s while it is off;
* the workflow check runs on the generic skill upload whatever the flag says;
* a carrying skill is never active on a turn by itself (opt-in, like every user skill);
* a no-op save writes nothing and leaves ``version`` alone; ``?validate=true`` writes nothing;
* ``fromRun`` fills the steps from the run's row, recording each Agent's published version;
* access is the skill's, and each Agent step is judged as the caller.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.skills import routes as my_skill_routes
from apis.app_api.workflows import routes as workflow_routes
from apis.shared.auth import get_current_user_from_session
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep
from apis.shared.workflows import sheet as workflow_sheet

BROKEN = b"---\ntitle: T\n---\n\n| step | kind | worker | reads |\n|---|---|---|---|\n| a | agent | AST-1 | a |\n"

DEFINITION = {
    "title": "Proposal review",
    "request": "Review this proposal",
    "steps": [
        {"name": "generalist", "agentId": "AST-gen", "agentVersion": 2},
        {"name": "fiscal", "agentId": "AST-fis", "reads": ["files", "request"], "request": "Check the budget"},
    ],
}


def _agent(name: str, *, published: Optional[int] = None, owner: str = "someone-else", model: Optional[str] = None):
    return SimpleNamespace(
        name=name,
        emoji="🤖",
        image_url=None,
        icon_key=None,
        owner_id=owner,
        listing=SimpleNamespace(published_version=published) if published is not None else None,
        model_settings=SimpleNamespace(model_id=model) if model else None,
    )


class World:
    """The agents each user can see, the skills shared with them, and the runs they own."""

    def __init__(self, repository) -> None:
        self.repository = repository
        self.agents: Dict[str, Dict[str, SimpleNamespace]] = {}
        self.shared: Dict[str, List[str]] = {}
        self.runs: Dict[str, WorkflowRun] = {}

    async def accessible(self, user) -> List[str]:
        owned = [s.skill_id for s in await self.repository.list_skills_by_owner(user.user_id)]
        return owned + [s for s in self.shared.get(user.user_id, []) if s not in owned]

    async def agent_lookup(self, assistant_id, user_id, user_email=None):
        agent = self.agents.get(user_id, {}).get(assistant_id)
        return (agent, "viewer") if agent is not None else (None, None)

    def run_store(self):
        world = self

        class _Store:
            def get(self, user_id, run_id):
                run = world.runs.get(run_id)
                return run if run is not None and run.user_id == user_id else None

        return _Store()


@pytest.fixture()
def world(user_skill_service, monkeypatch):
    w = World(user_skill_service.repository)

    async def step_model(agent):
        model = getattr(getattr(agent, "model_settings", None), "model_id", None)
        return (model, 3.0) if model else (None, None)

    for module in (workflow_routes, workflow_sheet):
        monkeypatch.setattr(module, "get_user_skill_service", lambda: user_skill_service)
        monkeypatch.setattr(module, "get_skill_catalog_repository", lambda: user_skill_service.repository)
        monkeypatch.setattr(module, "resolve_accessible_skill_ids", w.accessible)
        monkeypatch.setattr(module, "get_assistant_with_access_check", w.agent_lookup)
    monkeypatch.setattr(workflow_sheet, "step_model", step_model)
    monkeypatch.setattr(workflow_routes, "get_workflow_run_store", w.run_store)
    monkeypatch.setattr(my_skill_routes, "get_user_skill_service", lambda: user_skill_service)
    return w


def _app(user=None) -> FastAPI:
    app = FastAPI()
    app.include_router(workflow_routes.router)
    app.include_router(my_skill_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return app


def _flags(monkeypatch, *, on: bool = True) -> None:
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "true")
    monkeypatch.setenv("WORKFLOWS_ENABLED", "true" if on else "")


@pytest.fixture()
def client(world, author_user, monkeypatch):
    _flags(monkeypatch)
    world.agents[author_user.user_id] = {
        "AST-gen": _agent("Generalist", published=2, model="us.model-a"),
        "AST-fis": _agent("Fiscal reader", published=4),
    }
    with TestClient(_app(author_user)) as c:
        yield c


def _create(client, **body) -> dict:
    resp = client.post("/workflows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["workflow"]


def _resources(client, skill_id) -> list:
    return client.get(f"/skills/mine/{skill_id}/resources").json()["resources"]


class TestFlag:
    def test_auth_runs_before_the_flag(self, world, monkeypatch):
        _flags(monkeypatch, on=False)
        with TestClient(_app()) as anonymous:
            assert anonymous.get("/workflows").status_code == 401
            assert anonymous.post("/workflows", json={}).status_code == 401
            assert anonymous.get("/workflows/s/review").status_code == 401

    def test_off_is_404_for_every_route(self, world, author_user, monkeypatch):
        _flags(monkeypatch, on=False)
        with TestClient(_app(author_user)) as off:
            assert off.get("/workflows").status_code == 404
            assert off.post("/workflows", json={}).status_code == 404
            assert off.get("/workflows/s/review").status_code == 404
            assert off.put("/workflows/s/review", json=DEFINITION).status_code == 404
            assert off.delete("/workflows/s/review").status_code == 404

    def test_needs_workflow_runs_too(self, world, author_user, monkeypatch):
        _flags(monkeypatch)
        monkeypatch.setenv("WORKFLOW_RUNS_ENABLED", "")
        with TestClient(_app(author_user)) as c:
            assert c.get("/workflows").status_code == 404

    def test_the_generic_upload_refuses_a_broken_workflow_with_the_flag_off(
        self, world, author_user, monkeypatch
    ):
        """The flag takes away the routes, never the check (§9.1)."""
        _flags(monkeypatch, on=False)
        with TestClient(_app(author_user)) as off:
            skill_id = off.post("/skills/mine", json={"displayName": "Hand built", "description": "d"}).json()["skillId"]
            resp = off.post(
                f"/skills/mine/{skill_id}/resources",
                files={"file": ("workflow.review.md", BROKEN, "text/markdown")},
                data={"kind": "reference"},
            )
            assert resp.status_code == 400
            assert "reads its own result" in resp.json()["detail"]
            assert _resources(off, skill_id) == []


class TestCreate:
    def test_blank(self, client):
        wf = _create(client)

        assert wf["title"] == "Untitled workflow"
        assert wf["name"] == "untitled-workflow"
        assert wf["version"] == 1 and wf["steps"] == []
        assert [r["filename"] for r in _resources(client, wf["skillId"])] == ["workflow.untitled-workflow.md"]

    def test_the_carrier_is_marked_and_private(self, client, user_skill_service):
        wf = _create(client, title="Proposal review")
        skill = client.get("/skills/mine").json()["skills"][0]

        assert skill["skillId"] == wf["skillId"]
        assert skill["kind"] == "workflow"
        assert skill["displayName"] == "Proposal review"
        assert "Proposal review" not in skill["instructions"]  # never lists the steps (§12)

    def test_from_run_fills_the_steps(self, client, world, author_user):
        world.runs["run-1"] = WorkflowRun(
            run_id="run-1",
            user_id=author_user.user_id,
            session_id="sess-1",
            status="approved",
            request="Review this proposal",
            steps=[
                WorkflowRunStep(step_id="1", handoff_id="h1", agent_id="AST-gen", agent_name="Generalist", request="Review this proposal"),
                WorkflowRunStep(step_id="2", handoff_id="h2", agent_id="AST-fis", agent_name="Fiscal reader", request="Check the budget"),
                WorkflowRunStep(step_id="3", handoff_id="h3", agent_id="AST-x", agent_name="Removed", request="", removed=True),
            ],
            created_at="2026-11-20T00:00:00+00:00",
            updated_at="2026-11-20T00:00:00+00:00",
        )

        wf = _create(client, title="From a run", fromRun={"sessionId": "sess-1", "runId": "run-1"})

        assert wf["request"] == "Review this proposal"
        assert [(s["name"], s["agentId"], s.get("agentVersion"), s["reads"], s.get("request")) for s in wf["steps"]] == [
            ("generalist", "AST-gen", 2, ["request", "files"], None),
            ("fiscal_reader", "AST-fis", 4, ["request", "files"], "Check the budget"),
        ]
        assert all(s["agent"]["changed"] is False for s in wf["steps"])

    def test_from_run_is_the_callers_own(self, client, world, other_user):
        world.runs["run-2"] = WorkflowRun(
            run_id="run-2", user_id=other_user.user_id, session_id="s", status="approved",
            created_at="2026-11-20T00:00:00+00:00", updated_at="2026-11-20T00:00:00+00:00",
        )
        assert client.post("/workflows", json={"fromRun": {"sessionId": "s", "runId": "run-2"}}).status_code == 404

    def test_from_a_run_never_started_is_409(self, client, world, author_user):
        world.runs["run-3"] = WorkflowRun(
            run_id="run-3", user_id=author_user.user_id, session_id="s", status="open",
            created_at="2026-11-20T00:00:00+00:00", updated_at="2026-11-20T00:00:00+00:00",
        )
        assert client.post("/workflows", json={"fromRun": {"sessionId": "s", "runId": "run-3"}}).status_code == 409


class TestNotActiveOnATurn:
    def test_a_carrier_is_listed_off_and_a_turn_without_it_selected_loads_no_skill(
        self, client, world, author_user
    ):
        """A user skill is opt-in (Skills v2 D6): reachable, never active until chosen or bound."""
        import asyncio

        from apis.inference_api.chat.routes import _apply_enabled_skills_filter

        wf = _create(client, title="Proposal review")
        accessible = asyncio.run(world.accessible(author_user))

        assert wf["skillId"] in accessible
        assert _apply_enabled_skills_filter(accessible, None) == []
        assert _apply_enabled_skills_filter(accessible, []) == []

    def test_kind_is_not_a_snapshot_field_and_is_written_only_when_set(self):
        from apis.shared.skills.models import SkillDefinition
        from apis.shared.skills.versions import SNAPSHOT_FIELDS

        plain = SkillDefinition(skill_id="plain", display_name="P", description="d", instructions="")
        assert "kind" not in SNAPSHOT_FIELDS
        assert "kind" not in plain.to_dynamo_item()
        marked = plain.model_copy(update={"kind": "workflow"})
        assert SkillDefinition.from_dynamo_item(marked.to_dynamo_item()).kind == "workflow"


class TestWrite:
    def test_save_then_a_no_op_save_writes_nothing(self, client, user_skill_service):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"

        first = client.put(path, json=DEFINITION)
        assert first.status_code == 200, first.text
        assert first.json()["written"] is True
        assert first.json()["workflow"]["version"] == 2
        before = _resources(client, wf["skillId"])

        calls = []
        original = user_skill_service.add_resource

        async def spy(*args, **kwargs):
            calls.append(args)
            return await original(*args, **kwargs)

        user_skill_service.add_resource = spy
        # Same definition, reads in another order and the body's own version ignored.
        again = dict(DEFINITION, steps=[dict(DEFINITION["steps"][0], reads=["files", "request"]), DEFINITION["steps"][1]])
        second = client.put(path, json=again)

        assert second.status_code == 200
        assert second.json()["written"] is False
        assert second.json()["workflow"]["version"] == 2
        assert calls == []
        assert _resources(client, wf["skillId"]) == before

    def test_a_change_moves_version_by_one(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        client.put(path, json=DEFINITION)

        changed = client.put(path, json=dict(DEFINITION, request="Review it again"))

        assert changed.json()["written"] is True
        assert changed.json()["workflow"]["version"] == 3

    def test_a_save_against_the_stored_version_writes(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"

        resp = client.put(path, json=dict(DEFINITION, expectedVersion=wf["version"]))

        assert resp.status_code == 200, resp.text
        assert resp.json()["written"] is True
        assert resp.json()["workflow"]["version"] == wf["version"] + 1

    def test_a_stale_save_is_409_with_the_current_document_and_writes_nothing(self, client, user_skill_service):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        # Another tab saves first, moving the stored version on.
        other_tab = client.put(path, json=dict(DEFINITION, expectedVersion=wf["version"]))
        assert other_tab.json()["workflow"]["version"] == 2
        before = _resources(client, wf["skillId"])

        calls = []
        original = user_skill_service.add_resource

        async def spy(*args, **kwargs):
            calls.append(args)
            return await original(*args, **kwargs)

        user_skill_service.add_resource = spy
        stale = client.put(path, json=dict(DEFINITION, request="Mine instead", expectedVersion=wf["version"]))

        assert stale.status_code == 409
        body = stale.json()
        assert body["code"] == "version_conflict"
        assert "version 2" in body["detail"]
        assert body["document"]["workflow"]["version"] == 2
        assert body["document"]["workflow"]["request"] == DEFINITION["request"]
        assert body["document"]["sheet"]["startable"] is True
        assert calls == []
        assert _resources(client, wf["skillId"]) == before

    def test_a_stale_no_op_save_is_409_too(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        client.put(path, json=DEFINITION)

        stale = client.put(path, json=dict(DEFINITION, expectedVersion=wf["version"]))

        assert stale.status_code == 409
        assert stale.json()["document"]["workflow"]["version"] == 2

    def test_without_expected_version_a_save_replaces_whatever_is_stored(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        client.put(path, json=DEFINITION)

        resp = client.put(path, json=dict(DEFINITION, request="Last write wins"))

        assert resp.status_code == 200
        assert resp.json()["workflow"]["version"] == 3

    def test_validate_never_checks_the_version(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        client.put(path, json=DEFINITION)

        resp = client.put(path, json=dict(DEFINITION, expectedVersion=1), params={"validate": "true"})

        assert resp.status_code == 200
        assert resp.json()["written"] is False

    def test_a_save_against_a_deleted_workflow_is_409(self, client):
        skill_id = client.post("/skills/mine", json={"displayName": "Hand built", "description": "d"}).json()["skillId"]

        resp = client.put(f"/workflows/{skill_id}/review", json=dict(DEFINITION, expectedVersion=3))

        assert resp.status_code == 409
        assert resp.json()["code"] == "workflow_deleted"
        assert resp.json()["document"] is None
        assert all(not r["filename"].startswith("workflow.") for r in _resources(client, skill_id))

    def test_validate_writes_nothing_and_returns_the_sheet(self, client):
        wf = _create(client, title="Proposal review")
        path = f"/workflows/{wf['skillId']}/{wf['name']}"
        before = _resources(client, wf["skillId"])

        resp = client.put(path, json=DEFINITION, params={"validate": "true"})

        body = resp.json()
        assert resp.status_code == 200 and body["written"] is False
        assert _resources(client, wf["skillId"]) == before
        assert client.get(path).json()["workflow"]["steps"] == []
        gen, fis = body["workflow"]["steps"]
        assert gen["agent"] == {
            "canRun": True,
            "name": "Generalist",
            "emoji": "🤖",
            "hasIcon": False,
            "modelId": "us.model-a",
            "inputPricePerMillionTokens": 3.0,
            "publishedVersion": 2,
            "changed": False,
            "runsDraft": False,
        }
        assert gen["effectiveRequest"] == "Review this proposal"
        assert fis["effectiveRequest"] == "Check the budget"
        # Recorded no version, and version 4 is published now: "this Agent has changed" (§10).
        assert fis["agent"]["changed"] is True
        assert body["sheet"] == {"startable": True, "blockers": []}

    def test_validate_refuses_what_the_envelope_refuses(self, client):
        wf = _create(client)
        bad = dict(DEFINITION, steps=[{"name": "a", "agentId": "AST-gen", "reads": ["a"]}])
        resp = client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=bad, params={"validate": "true"})

        assert resp.status_code == 400
        assert "reads its own result" in resp.json()["detail"]

    def test_reserved_kinds_are_refused(self, client):
        wf = _create(client)
        bad = dict(DEFINITION, steps=[{"name": "a", "kind": "prompt", "agentId": "m"}])
        resp = client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=bad)
        assert resp.status_code == 400
        assert "'prompt' steps are not available yet" in resp.json()["detail"]

    def test_the_sheet_names_what_blocks_start(self, client):
        wf = _create(client)
        body = {
            "title": "T",
            "steps": [
                {"name": "a", "agentId": "AST-gen"},
                {"name": "b", "agentId": "AST-gen", "reads": ["a"]},
                {"name": "c", "agentId": "AST-nobody"},
            ],
            "result": {"mode": "merge"},
        }
        sheet = client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=body, params={"validate": "true"}).json()["sheet"]

        assert sheet["startable"] is False
        assert [(b["code"], b.get("step")) for b in sheet["blockers"]] == [
            ("same_agent_twice", "b"),
            ("step_unavailable", "c"),
            ("merge_result", None),
        ]

    def test_a_hand_written_note_survives_a_structured_save(self, client, user_skill_service):
        skill_id = client.post("/skills/mine", json={"displayName": "Hand built", "description": "d"}).json()["skillId"]
        hand = (
            b"---\ntitle: Review\nversion: 4\naudience: reviewers\n---\n\nRead me first.\n\n"
            b"| step | kind | worker | reads | note |\n|---|---|---|---|---|\n"
            b"| generalist | agent | AST-gen @ 2 | request | ask first |\n"
        )
        assert client.post(
            f"/skills/mine/{skill_id}/resources",
            files={"file": ("workflow.review.md", hand, "text/markdown")},
            data={"kind": "reference"},
        ).status_code == 200

        resp = client.put(f"/workflows/{skill_id}/review", json=DEFINITION)
        assert resp.json()["workflow"]["version"] == 5

        import asyncio

        _ref, content = asyncio.run(user_skill_service.bundle_files.read_resource(skill_id, "workflow.review.md"))
        text = content.decode()
        assert "audience: reviewers" in text and "Read me first." in text
        assert "| generalist | agent | AST-gen @ 2 | request, files | ask first |" in text
        assert "| fiscal | agent | AST-fis | request, files | |" in text


class TestAccess:
    def test_another_users_unshared_workflow_is_not_found(self, client, world, other_user, monkeypatch):
        wf = _create(client, title="Proposal review")
        with TestClient(_app(other_user)) as other:
            assert other.get(f"/workflows/{wf['skillId']}/{wf['name']}").status_code == 404
            assert other.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=DEFINITION).status_code == 404
            assert other.delete(f"/workflows/{wf['skillId']}/{wf['name']}").status_code == 404
            assert other.get("/workflows").json()["workflows"] == []

    def test_a_shared_workflow_reads_but_its_agents_are_judged_as_the_caller(
        self, client, world, other_user
    ):
        wf = _create(client, title="Proposal review")
        client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=DEFINITION)
        world.shared[other_user.user_id] = [wf["skillId"]]
        world.agents[other_user.user_id] = {"AST-gen": _agent("Generalist", published=2)}

        with TestClient(_app(other_user)) as other:
            doc = other.get(f"/workflows/{wf['skillId']}/{wf['name']}").json()
            listed = other.get("/workflows").json()["workflows"]
            put = other.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=DEFINITION)

        gen, fis = doc["workflow"]["steps"]
        assert doc["workflow"]["isOwned"] is False
        assert gen["agent"]["canRun"] is True
        assert fis["agent"] == {"canRun": False, "hasIcon": False, "changed": False, "runsDraft": False}
        assert doc["sheet"]["blockers"] == [
            {
                "code": "step_unavailable",
                "step": "fiscal",
                "message": "You can't use this step's Agent. Remove the step, or ask its owner to share it",
            }
        ]
        assert [w["name"] for w in listed] == ["proposal-review"]
        assert put.status_code == 404  # use is shared; writing is the owner's

    def test_the_owner_runs_their_draft(self, client, world, author_user):
        world.agents[author_user.user_id]["AST-gen"] = _agent("Generalist", published=2, owner=author_user.user_id)
        wf = _create(client)
        doc = client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=DEFINITION, params={"validate": "true"}).json()
        assert doc["workflow"]["steps"][0]["agent"]["runsDraft"] is True


class TestReadAndList:
    def test_list_reads_titles_and_reports_a_broken_file(self, client, user_skill_service):
        wf = _create(client, title="Proposal review")
        client.put(f"/workflows/{wf['skillId']}/{wf['name']}", json=DEFINITION)
        hand = client.post("/skills/mine", json={"displayName": "Hand built", "description": "d"}).json()["skillId"]
        # A file stored before every write path checked workflows.
        import asyncio

        asyncio.run(
            user_skill_service.bundle_files.repository.update_skill(
                hand,
                {
                    "resources": [
                        _store_raw(user_skill_service, hand, "workflow.old.md", BROKEN),
                    ]
                },
                admin_user_id="x",
            )
        )

        listed = client.get("/workflows").json()["workflows"]

        assert [(w["name"], w.get("title"), w.get("stepCount"), w["designerMade"]) for w in listed] == [
            ("old", None, None, False),
            ("proposal-review", "Proposal review", 2, True),
        ]
        assert "reads its own result" in listed[0]["problem"]
        broken = client.get(f"/workflows/{hand}/old")
        assert broken.status_code == 400
        assert broken.json()["markdown"] == BROKEN.decode()

    def test_a_bad_name_is_400(self, client):
        assert client.get("/workflows/s/Not_A_Name").status_code == 400


class TestDelete:
    def test_deletes_the_carrier_with_its_last_file(self, client):
        wf = _create(client, title="Proposal review")
        assert client.delete(f"/workflows/{wf['skillId']}/{wf['name']}").status_code == 204
        assert client.get("/skills/mine").json()["skills"] == []

    def test_a_carrier_holding_another_file_stays(self, client):
        wf = _create(client, title="Proposal review")
        client.post(
            f"/skills/mine/{wf['skillId']}/resources",
            files={"file": ("notes.md", b"notes", "text/markdown")},
            data={"kind": "reference"},
        )
        assert client.delete(f"/workflows/{wf['skillId']}/{wf['name']}").status_code == 204
        assert [r["filename"] for r in _resources(client, wf["skillId"])] == ["notes.md"]

    def test_a_hand_built_skill_stays(self, client):
        skill_id = client.post("/skills/mine", json={"displayName": "Hand built", "description": "d"}).json()["skillId"]
        client.put(f"/workflows/{skill_id}/review", json=DEFINITION)
        assert client.delete(f"/workflows/{skill_id}/review").status_code == 204
        assert len(client.get("/skills/mine").json()["skills"]) == 1


def _store_raw(service, skill_id: str, filename: str, content: bytes):
    """Put bytes in the bundle bypassing every check, as an old write path once could."""
    from apis.shared.skills.models import SkillResourceRef
    from apis.shared.skills.resource_store import compute_content_hash

    key = service.bundle_files.resource_store.put(
        skill_id=skill_id, filename=filename, content=content, content_type="text/markdown", kind="reference", path=filename
    )
    return SkillResourceRef(
        filename=filename,
        content_hash=compute_content_hash(content),
        size=len(content),
        content_type="text/markdown",
        s3_key=key,
        kind="reference",
        path=filename,
    )
