"""``access_level: "project"`` shares, their ``SHARED_TASK#`` pointer, and forks (shared-projects PR-1.6).

A project share is readable by every member of the task's project, and by no one
else; the pointer on the projects table is what lists it, and always points at
the task's newest project share.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from apis.app_api.shares.models import CreateShareRequest, UpdateShareRequest
from apis.app_api.shares.service import (
    AccessDeniedError,
    ProjectShareError,
    ShareService,
)
from apis.app_api.shares.snapshot_store import ShareSnapshotStore
from apis.shared.auth.models import User
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectService
from apis.shared.sessions.models import SessionMetadata, SessionPreferences

from tests.shared.test_projects import REGION, TABLE, FakeHarness, make_projects_table

SHARES_TABLE = "test-shared-conversations"
BUCKET = "test-shared-conversations-bodies"

OWNER = User(user_id="u-owner", email="owner@example.edu", name="O", roles=["default"])
AUTHOR = User(user_id="u-author", email="author@example.edu", name="A", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="V", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="S", roles=["default"])


def _make_shares_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SHARES_TABLE,
        KeySchema=[{"AttributeName": "share_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "share_id", "AttributeType": "S"},
            {"AttributeName": "session_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[{
            "IndexName": "SessionShareIndex",
            "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )


@pytest.fixture()
def env(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "PROJECTS_ENABLED": "true",
        "SHARED_CONVERSATIONS_TABLE_NAME": SHARES_TABLE,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        _make_shares_table()
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield


@pytest.fixture()
def projects(env) -> ProjectService:
    return ProjectService(repository=ProjectRepository(table_name=TABLE), harness=FakeHarness())


@pytest.fixture()
def project(projects):
    """OWNER's project; AUTHOR (editor) writes tasks in it, VIEWER reads."""
    p = asyncio.run(projects.create_project(OWNER, "Project A"))
    projects.add_members(p.project_id, OWNER, [AUTHOR.email], "editor")
    projects.add_members(p.project_id, OWNER, [VIEWER.email], "viewer")
    return p


@pytest.fixture()
def shares(env, projects) -> ShareService:
    store = ShareSnapshotStore(bucket_name=BUCKET, s3_client=boto3.client("s3", region_name=REGION))
    return ShareService(snapshot_store=store, project_repository=projects.repository)


def _session(session_id: str = "s1", project_id: Optional[str] = None, title: str = "Budget draft") -> SessionMetadata:
    prefs = SessionPreferences(assistantId="ast-h", projectId=project_id) if project_id else None
    return SessionMetadata(
        sessionId=session_id, userId=AUTHOR.user_id, title=title, status="active",
        createdAt="2026-09-01T00:00:00Z", lastMessageAt="2026-09-01T00:00:00Z", messageCount=2,
        preferences=prefs,
    )


def _sources(metadata: Optional[SessionMetadata]):
    return (
        patch("apis.app_api.shares.service.get_session_metadata", new=AsyncMock(return_value=metadata)),
        patch("apis.app_api.shares.service.get_messages", new=AsyncMock(return_value=MagicMock(messages=[]))),
    )


def _share(shares: ShareService, metadata: SessionMetadata, access: str = "project", user: User = AUTHOR):
    meta_patch, msgs_patch = _sources(metadata)
    with meta_patch, msgs_patch:
        return asyncio.run(shares.create_share(metadata.session_id, user, CreateShareRequest(accessLevel=access)))


def _pointers(projects: ProjectService, project_id: str) -> List:
    return projects.repository.list_shared_tasks(project_id)


class TestCreate:
    def test_project_share_records_the_project_and_lists_the_task(self, shares, projects, project):
        response = _share(shares, _session(project_id=project.project_id))

        assert response.access_level == "project"
        assert response.project_id == project.project_id
        [pointer] = _pointers(projects, project.project_id)
        assert (pointer.share_id, pointer.session_id, pointer.title, pointer.owner_email) == (
            response.share_id, "s1", "Budget draft", AUTHOR.email,
        )

    def test_a_task_outside_any_project_cannot_be_shared_to_one(self, shares, project):
        with pytest.raises(ProjectShareError) as e:
            _share(shares, _session(project_id=None))
        assert e.value.status_code == 400

    def test_an_author_no_longer_in_the_project_cannot_share_to_it(self, shares, projects, project):
        projects.remove_member(project.project_id, OWNER, AUTHOR.email)
        with pytest.raises(ProjectShareError) as e:
            _share(shares, _session(project_id=project.project_id))
        assert e.value.status_code == 403
        assert _pointers(projects, project.project_id) == []

    def test_an_archived_project_takes_no_new_shares(self, shares, projects, project):
        asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))
        with pytest.raises(ProjectShareError) as e:
            _share(shares, _session(project_id=project.project_id))
        assert e.value.status_code == 409

    def test_refused_while_projects_are_switched_off(self, shares, project, monkeypatch):
        monkeypatch.setenv("PROJECTS_ENABLED", "false")
        with pytest.raises(ProjectShareError) as e:
            _share(shares, _session(project_id=project.project_id))
        assert e.value.status_code == 400

    def test_pointer_failure_rolls_the_share_back(self, shares, project, monkeypatch):
        monkeypatch.setattr(shares._projects(), "put_shared_task", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _share(shares, _session(project_id=project.project_id))
        assert shares._find_shares_by_session("s1") == []

    def test_other_access_levels_write_no_pointer(self, shares, projects, project):
        response = _share(shares, _session(project_id=project.project_id), access="public")
        assert response.project_id is None
        assert _pointers(projects, project.project_id) == []


class TestReadAccess:
    @pytest.mark.parametrize("user,allowed", [
        (AUTHOR, True), (OWNER, True), (VIEWER, True), (STRANGER, False),
    ], ids=["author", "owner", "viewer", "stranger"])
    def test_membership_is_the_read_check(self, shares, project, user, allowed):
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        if allowed:
            assert asyncio.run(shares.get_shared_conversation(share_id, user)).access_level == "project"
        else:
            with pytest.raises(AccessDeniedError):
                asyncio.run(shares.get_shared_conversation(share_id, user))

    def test_a_member_of_another_project_is_a_stranger(self, shares, projects, project):
        asyncio.run(projects.create_project(STRANGER, "Project B"))  # owner there, nothing here
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        with pytest.raises(AccessDeniedError):
            asyncio.run(shares.get_shared_conversation(share_id, STRANGER))

    def test_leaving_the_project_ends_read_access(self, shares, projects, project):
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        projects.leave(project.project_id, VIEWER)
        with pytest.raises(AccessDeniedError):
            asyncio.run(shares.get_shared_conversation(share_id, VIEWER))

    def test_members_still_read_an_archived_projects_shares(self, shares, projects, project):
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))
        assert asyncio.run(shares.get_shared_conversation(share_id, VIEWER)).share_id == share_id


class TestPointerFollowsTheShares:
    def test_resharing_moves_the_pointer_and_revoking_falls_back(self, shares, projects, project):
        meta = _session(project_id=project.project_id)
        first = _share(shares, meta).share_id
        second = _share(shares, meta.model_copy(update={"title": "Budget final"})).share_id

        [pointer] = _pointers(projects, project.project_id)
        assert (pointer.share_id, pointer.title) == (second, "Budget final")

        asyncio.run(shares.revoke_share(second, AUTHOR))
        [pointer] = _pointers(projects, project.project_id)
        assert (pointer.share_id, pointer.title) == (first, "Budget draft")

        asyncio.run(shares.revoke_share(first, AUTHOR))
        assert _pointers(projects, project.project_id) == []

    def test_switching_a_share_into_and_out_of_the_project(self, shares, projects, project):
        meta = _session(project_id=project.project_id)
        share_id = _share(shares, meta, access="public").share_id

        meta_patch, _ = _sources(meta)
        with meta_patch:
            updated = asyncio.run(shares.update_share(share_id, AUTHOR, UpdateShareRequest(accessLevel="project")))
        assert updated.project_id == project.project_id
        assert [p.share_id for p in _pointers(projects, project.project_id)] == [share_id]

        updated = asyncio.run(shares.update_share(share_id, AUTHOR, UpdateShareRequest(accessLevel="public")))
        assert updated.project_id is None
        assert _pointers(projects, project.project_id) == []

    def test_switching_in_needs_a_project_task(self, shares, project):
        meta = _session(project_id=None)
        share_id = _share(shares, meta, access="public").share_id
        meta_patch, _ = _sources(meta)
        with meta_patch, pytest.raises(ProjectShareError):
            asyncio.run(shares.update_share(share_id, AUTHOR, UpdateShareRequest(accessLevel="project")))

    def test_deleting_the_session_removes_the_pointer(self, shares, projects, project):
        _share(shares, _session(project_id=project.project_id))
        assert asyncio.run(shares.delete_shares_for_session("s1")) == 1
        assert _pointers(projects, project.project_id) == []

    def test_shared_tasks_list_is_member_only_and_newest_first(self, shares, projects, project):
        _share(shares, _session("s1", project_id=project.project_id, title="First"))
        _share(shares, _session("s2", project_id=project.project_id, title="Second"))

        assert [t.title for t in projects.list_shared_tasks(project.project_id, VIEWER)] == ["Second", "First"]
        from apis.shared.projects.service import ProjectNotFoundError
        with pytest.raises(ProjectNotFoundError):
            projects.list_shared_tasks(project.project_id, STRANGER)


class TestForkKeepsTheProject:
    def _fork(self, shares: ShareService, share_id: str, requester: User) -> SessionMetadata:
        stored = AsyncMock()
        with patch.object(shares, "_copy_messages_to_memory", new=AsyncMock(return_value=0)), \
                patch("apis.app_api.shares.service.store_session_metadata", new=stored):
            asyncio.run(shares.export_shared_conversation(share_id, requester))
        return stored.call_args.kwargs["session_metadata"]

    def test_a_members_fork_is_a_task_in_the_same_project(self, shares, project):
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        forked = self._fork(shares, share_id, VIEWER)
        assert forked.preferences.project_id == project.project_id
        # The project's current harness, not whatever the snapshot recorded.
        assert forked.preferences.assistant_id == project.harness_agent_id

    def test_a_non_member_forking_a_public_share_gets_a_plain_session(self, shares, project):
        share_id = _share(shares, _session(project_id=project.project_id), access="public").share_id
        assert self._fork(shares, share_id, STRANGER).preferences is None

    def test_a_fork_of_an_archived_projects_task_is_a_plain_session(self, shares, projects, project):
        share_id = _share(shares, _session(project_id=project.project_id)).share_id
        asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))
        assert self._fork(shares, share_id, VIEWER).preferences is None

    def test_a_fork_of_a_plain_task_stays_plain(self, shares, project):
        share_id = _share(shares, _session(project_id=None), access="public").share_id
        assert self._fork(shares, share_id, VIEWER).preferences is None



class TestTheProjectsTrail:
    def test_sharing_and_unsharing_a_task_is_recorded_on_the_project(self, shares, project):
        from tests.shared.test_project_settings import AuditRecorder

        trail = AuditRecorder()
        shares._audit = trail
        meta = _session(project_id=project.project_id)
        project_share = _share(shares, meta).share_id
        public_share = _share(shares, meta, access="public").share_id

        meta_patch, _ = _sources(meta)
        with meta_patch:
            asyncio.run(shares.update_share(public_share, AUTHOR, UpdateShareRequest(accessLevel="project")))
        asyncio.run(shares.revoke_share(project_share, AUTHOR))

        assert [(r["action"], r["target_id"], r["after"]["shareId"]) for r in trail.records] == [
            ("project.task_shared", project.project_id, project_share),
            ("project.task_shared", project.project_id, public_share),
            ("project.task_unshared", project.project_id, project_share),
        ]
        assert trail.records[0]["after"]["title"] == "Budget draft"
