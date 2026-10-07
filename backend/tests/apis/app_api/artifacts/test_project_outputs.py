"""Artifacts shared with a Shared Project: the ``project`` access level and Outputs (Shared Projects 3.3).

Real artifact share rows and ``OUTPUT#`` pointers over moto, through the real
routes. Only session metadata (which project a task belongs to) and the audit
sink are faked.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.artifacts import service as token_service
from apis.app_api.artifacts.service import ArtifactShareService
from apis.app_api.artifacts.shares import artifact_shares_router, shared_artifacts_router
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService
from apis.shared.sessions.models import SessionMetadata, SessionPreferences

from tests.shared.test_project_settings import AuditRecorder
from tests.shared.test_projects import TABLE as PROJECTS_TABLE
from tests.shared.test_projects import FakeHarness, make_projects_table

ARTIFACTS = "test-user-artifacts"
REGION = "us-east-1"

OWNER = User(user_id="u-owner", email="owner@example.edu", name="O", roles=["default"])
AUTHOR = User(user_id="u-author", email="author@example.edu", name="A", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="V", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="S", roles=["default"])


def _make_artifacts_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=ARTIFACTS,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK")
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[{
            "IndexName": "SessionIndex",
            "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    token_service._reset_caches_for_tests()


@pytest.fixture()
def world(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PROJECTS_ENABLED": "true",
        "DYNAMODB_PROJECTS_TABLE_NAME": PROJECTS_TABLE,
        "DYNAMODB_ARTIFACTS_TABLE_NAME": ARTIFACTS,
        "ARTIFACTS_ORIGIN": "https://a.test.example.com",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        _make_artifacts_table()
        trail = AuditRecorder()
        monkeypatch.setattr("apis.shared.audit.get_audit_service", lambda: trail)
        projects = ProjectService(repository=ProjectRepository(table_name=PROJECTS_TABLE), harness=FakeHarness(), audit=trail)
        project = asyncio.run(projects.create_project(OWNER, "Enrollment Sync"))
        projects.add_members(project.project_id, OWNER, [AUTHOR.email], "editor")
        projects.add_members(project.project_id, OWNER, [VIEWER.email], "viewer")

        sessions: Dict[str, Optional[str]] = {"s-proj": project.project_id, "s-plain": None}

        async def metadata(session_id: str, user_id: str):
            if session_id not in sessions:
                return None
            pid = sessions[session_id]
            return SessionMetadata(
                sessionId=session_id, userId=user_id, title="t", status="active",
                createdAt="2026-10-01T00:00:00Z", lastMessageAt="2026-10-01T00:00:00Z", messageCount=1,
                preferences=SessionPreferences(assistantId="ast-h", projectId=pid) if pid else None,
            )

        monkeypatch.setattr("apis.shared.sessions.metadata.get_session_metadata", AsyncMock(side_effect=metadata))

        from apis.app_api.projects import output_routes, routes as project_routes

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(output_routes, "display_names", lambda emails: {})

        def client_for(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(artifact_shares_router)
            app.include_router(shared_artifacts_router)
            app.include_router(output_routes.router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        yield {"projects": projects, "project": project, "client": client_for, "trail": trail}


def _put_artifact(artifact: str, session_id: str, versions=(1,), title="Roster diff", user=AUTHOR) -> None:
    table = boto3.resource("dynamodb", region_name=REGION).Table(ARTIFACTS)
    table.put_item(Item={"PK": f"USER#{user.user_id}", "SK": f"ARTIFACT#{artifact}#HEAD",
                         "artifact_id": artifact, "session_id": session_id, "title": title})
    for v in versions:
        table.put_item(Item={
            "PK": f"USER#{user.user_id}", "SK": f"ARTIFACT#{artifact}#V#{v:05d}", "artifact_id": artifact,
            "user_id": user.user_id, "version": v, "storage": "s3", "content_key": f"k/{artifact}/{v}",
            "content_type": "text/html; charset=utf-8", "title": title, "session_id": session_id,
        })


def _share(world, artifact="art-1", version=1, access="project", user=AUTHOR):
    return world["client"](user).post(f"/artifacts/{artifact}/shares", json={"version": version, "accessLevel": access})


def _outputs(world, user=VIEWER):
    return world["client"](user).get(f"/projects/{world['project'].project_id}/outputs").json()["outputs"]


class TestOffering:
    def test_offered_for_an_artifact_from_a_project_task(self, world):
        _put_artifact("art-1", "s-proj")
        body = world["client"](AUTHOR).get("/artifacts/art-1/shares").json()
        assert (body["projectId"], body["projectName"]) == (world["project"].project_id, "Enrollment Sync")

    def test_not_offered_for_an_ordinary_chat_or_with_projects_off(self, world, monkeypatch):
        _put_artifact("art-2", "s-plain")
        assert world["client"](AUTHOR).get("/artifacts/art-2/shares").json()["projectId"] is None
        _put_artifact("art-1", "s-proj")
        monkeypatch.setenv("PROJECTS_ENABLED", "false")
        assert world["client"](AUTHOR).get("/artifacts/art-1/shares").json()["projectId"] is None


class TestSharing:
    def test_a_project_share_is_listed_for_every_member(self, world):
        _put_artifact("art-1", "s-proj")
        share = _share(world).json()
        assert (share["accessLevel"], share["projectId"]) == ("project", world["project"].project_id)

        [out] = _outputs(world, VIEWER)
        assert (out["artifactId"], out["shareId"], out["title"], out["sharedByEmail"], out["shareUrl"]) == (
            "art-1", share["shareId"], "Roster diff", AUTHOR.email, f"/shared-artifact/{share['shareId']}",
        )
        assert (out["isMine"], out["canRemove"]) == (False, False)
        assert "ownerId" not in out and "sessionId" not in out
        mine = _outputs(world, AUTHOR)[0]
        assert (mine["isMine"], mine["canRemove"]) == (True, True)
        assert _outputs(world, OWNER)[0]["canRemove"] is True
        assert world["trail"].records[-1]["action"] == "project.output_shared"

    def test_members_open_it_and_nobody_else_does(self, world):
        _put_artifact("art-1", "s-proj")
        share_id = _share(world).json()["shareId"]
        assert world["client"](VIEWER).get(f"/shared-artifacts/{share_id}").status_code == 200
        assert world["client"](STRANGER).get(f"/shared-artifacts/{share_id}").status_code == 403
        world["projects"].remove_member(world["project"].project_id, OWNER, VIEWER.email)
        assert world["client"](VIEWER).get(f"/shared-artifacts/{share_id}").status_code == 403

    @pytest.mark.parametrize("setup,code", [("plain", 400), ("left", 403), ("archived", 409)])
    def test_refused(self, world, setup, code):
        _put_artifact("art-1", "s-plain" if setup == "plain" else "s-proj")
        if setup == "left":
            world["projects"].remove_member(world["project"].project_id, OWNER, AUTHOR.email)
        if setup == "archived":
            asyncio.run(world["projects"].update_project(world["project"].project_id, OWNER, status="archived"))
        assert _share(world).status_code == code
        assert ArtifactShareService().list_for_artifact(owner_id=AUTHOR.user_id, artifact_id="art-1") == []

    def test_an_unlisted_share_is_rolled_back(self, world, monkeypatch):
        _put_artifact("art-1", "s-proj")
        monkeypatch.setattr(ProjectRepository, "put_output", lambda self, o: (_ for _ in ()).throw(RuntimeError("down")))
        assert _share(world).status_code == 503
        assert ArtifactShareService().list_for_artifact(owner_id=AUTHOR.user_id, artifact_id="art-1") == []


class TestThePointerFollowsTheShares:
    def test_newest_version_wins_and_revoking_falls_back(self, world):
        _put_artifact("art-1", "s-proj", versions=(1, 2))
        v1 = _share(world, version=1).json()["shareId"]
        v2 = _share(world, version=2).json()["shareId"]
        assert [(o["shareId"], o["version"]) for o in _outputs(world)] == [(v2, 2)]

        assert world["client"](AUTHOR).delete(f"/artifacts/shares/{v2}").status_code == 204
        assert [o["shareId"] for o in _outputs(world)] == [v1]
        world["client"](AUTHOR).delete(f"/artifacts/shares/{v1}")
        assert _outputs(world) == []
        assert world["trail"].records[-1]["action"] == "project.output_removed"

    def test_switching_in_and_out_of_the_project(self, world):
        _put_artifact("art-1", "s-proj")
        share_id = _share(world, access="public").json()["shareId"]
        assert _outputs(world) == []
        patched = world["client"](AUTHOR).patch(f"/artifacts/shares/{share_id}", json={"accessLevel": "project"}).json()
        assert patched["projectId"] == world["project"].project_id and len(_outputs(world)) == 1
        world["client"](AUTHOR).patch(f"/artifacts/shares/{share_id}", json={"accessLevel": "public"})
        assert _outputs(world) == []

    def test_deleting_the_artifact_or_its_task_removes_it(self, world):
        _put_artifact("art-1", "s-proj")
        _share(world)
        ArtifactShareService().revoke_for_artifact(owner_id=AUTHOR.user_id, artifact_id="art-1")
        assert _outputs(world) == []

    def test_a_rename_reaches_the_list(self, world):
        _put_artifact("art-1", "s-proj")
        _share(world)
        ArtifactShareService().retitle_for_artifact(owner_id=AUTHOR.user_id, artifact_id="art-1", title="Roster v2")
        assert _outputs(world)[0]["title"] == "Roster v2"


class TestRemoving:
    def test_a_viewer_cannot_remove_someone_elses(self, world):
        _put_artifact("art-1", "s-proj")
        _share(world)
        resp = world["client"](VIEWER).delete(f"/projects/{world['project'].project_id}/outputs/art-1")
        assert resp.status_code == 403

    def test_an_editor_removes_it_and_the_link_stops_working(self, world):
        _put_artifact("art-1", "s-proj")
        share_id = _share(world).json()["shareId"]
        resp = world["client"](OWNER).delete(f"/projects/{world['project'].project_id}/outputs/art-1")
        assert resp.status_code == 204
        assert _outputs(world) == []
        assert world["client"](VIEWER).get(f"/shared-artifacts/{share_id}").status_code == 404
        assert world["trail"].records[-1]["action"] == "project.output_removed"

    def test_the_sharer_may_remove_their_own_even_when_archived(self, world):
        _put_artifact("art-1", "s-proj")
        _share(world)
        asyncio.run(world["projects"].update_project(world["project"].project_id, OWNER, status="archived"))
        base = f"/projects/{world['project'].project_id}/outputs/art-1"
        assert world["client"](AUTHOR).delete(base).status_code == 204

    def test_unknown_and_strangers_are_not_found(self, world):
        base = f"/projects/{world['project'].project_id}/outputs"
        assert world["client"](OWNER).delete(f"{base}/nope").status_code == 404
        assert world["client"](STRANGER).get(base).status_code == 404

    def test_purging_the_project_purges_its_outputs(self, world):
        _put_artifact("art-1", "s-proj")
        _share(world)
        pid = world["project"].project_id
        asyncio.run(world["projects"].update_project(pid, OWNER, status="archived"))
        asyncio.run(world["projects"].purge_project(pid, OWNER))
        assert world["projects"].repository.list_outputs(pid) == []
