"""Shared Projects data layer and service (shared-projects §3.1, §5; PR-1.2).

The harness Agent is faked here so orchestration, rollback and purge ordering
can be asserted without the assistants table; the harness's own rules (hidden
from lists, access through membership, refusals) are covered in
``test_project_harness_rules.py`` against real assistants code.
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from apis.shared.auth.models import User
from apis.shared.projects.access import resolve_project_role
from apis.shared.projects.models import ProjectMember
from apis.shared.projects.repository import ProjectRepository, ProjectWriteConflict
from apis.shared.projects.service import (
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionError,
    ProjectService,
)

REGION = "us-east-1"
TABLE = "test-projects"

OWNER = User(user_id="u-owner", email="Owner@Example.edu", name="Olive Owner", roles=["default"])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="Ed", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="Vi", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="St", roles=["default"])


def make_projects_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "MemberIndex",
                "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )


class FakeHarness:
    def __init__(self) -> None:
        self.created: List[Tuple[str, str]] = []
        self.deleted: List[str] = []

    async def create(self, *, project_id, owner_id, owner_name, name, description) -> str:
        agent_id = f"ast-fake{len(self.created)}"
        self.created.append((agent_id, project_id))
        return agent_id

    async def delete(self, agent_id: str) -> None:
        self.deleted.append(agent_id)


@pytest.fixture()
def repo(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        yield ProjectRepository(table_name=TABLE)


@pytest.fixture()
def harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture()
def service(repo, harness) -> ProjectService:
    return ProjectService(repository=repo, harness=harness)


def create(service: ProjectService, user: User = OWNER, name: str = "Enrollment Sync"):
    return asyncio.run(service.create_project(user, name, "Canvas enrollment work"))


def with_members(service: ProjectService):
    project = create(service)
    service.add_members(project.project_id, OWNER, [EDITOR.email], "editor")
    service.add_members(project.project_id, OWNER, [VIEWER.email], "viewer")
    return project


# ── create / rollback ───────────────────────────────────────────────────


def test_create_writes_meta_pointing_at_a_new_harness(service, harness, repo):
    project = create(service)

    assert harness.created == [(project.harness_agent_id, project.project_id)]
    stored = repo.get_project(project.project_id)
    assert stored.owner_id == OWNER.user_id
    assert stored.owner_email == "owner@example.edu"  # normalized
    assert stored.status == "active" and stored.member_count == 0 and stored.version == 1
    assert stored.settings.editors_manage_members is True
    assert stored.shared_space_id is None  # created in Phase 2.4


def test_failed_meta_write_deletes_the_harness(service, harness, repo, monkeypatch):
    def boom(project):
        raise RuntimeError("DynamoDB unavailable")

    monkeypatch.setattr(repo, "create_project", boom)
    with pytest.raises(RuntimeError):
        create(service)

    assert len(harness.created) == 1
    assert harness.deleted == [harness.created[0][0]]


def test_project_ids_are_never_reused(service, repo):
    project = create(service)
    with pytest.raises(ProjectWriteConflict):
        repo.create_project(project)


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_create_rejects_bad_names(service, harness, name):
    with pytest.raises(ProjectError):
        asyncio.run(service.create_project(OWNER, name))
    assert harness.created == []  # validated before anything is created


# ── roles ───────────────────────────────────────────────────────────────


def test_roles_resolve_from_meta_and_member_rows(service, repo):
    project = with_members(service)
    pid = project.project_id

    assert resolve_project_role(pid, OWNER.user_id, OWNER.email, repo)[1] == "owner"
    assert resolve_project_role(pid, EDITOR.user_id, EDITOR.email, repo)[1] == "editor"
    assert resolve_project_role(pid, VIEWER.user_id, "VIEWER@example.edu", repo)[1] == "viewer"
    assert resolve_project_role(pid, STRANGER.user_id, STRANGER.email, repo)[1] is None
    assert resolve_project_role("prj_missing", OWNER.user_id, OWNER.email, repo) == (None, None)


def test_first_resolve_backfills_user_id_and_never_overwrites(service, repo):
    project = with_members(service)
    assert repo.get_member(project.project_id, EDITOR.email).user_id is None

    resolve_project_role(project.project_id, EDITOR.user_id, EDITOR.email, repo)
    assert repo.get_member(project.project_id, EDITOR.email).user_id == EDITOR.user_id

    resolve_project_role(project.project_id, "someone-else", EDITOR.email, repo)
    assert repo.get_member(project.project_id, EDITOR.email).user_id == EDITOR.user_id


def test_non_members_are_told_not_found_never_forbidden(service):
    project = with_members(service)
    with pytest.raises(ProjectNotFoundError):
        service.get_project(project.project_id, STRANGER)
    with pytest.raises(ProjectNotFoundError):
        service.get_project("prj_does_not_exist", STRANGER)


# ── listing ─────────────────────────────────────────────────────────────


def test_list_merges_owned_and_shared_with_the_callers_role(service):
    mine = create(service, OWNER, "Mine")
    theirs = create(service, EDITOR, "Theirs")
    service.add_members(theirs.project_id, EDITOR, [OWNER.email], "viewer")
    create(service, STRANGER, "Not mine")

    listed = {p.name: role for p, role in service.list_projects(OWNER)}
    assert listed == {"Mine": "owner", "Theirs": "viewer"}
    assert mine.project_id != theirs.project_id


def test_archived_projects_are_listed_only_on_request(service):
    project = create(service)
    service.update_project(project.project_id, OWNER, status="archived")

    assert service.list_projects(OWNER) == []
    assert [p.project_id for p, _ in service.list_projects(OWNER, include_archived=True)] == [project.project_id]


# ── update / archive / purge ────────────────────────────────────────────


def test_editor_edits_content_but_not_settings_or_status(service):
    project = with_members(service)
    updated, role = service.update_project(project.project_id, EDITOR, name="Renamed")
    assert (updated.name, role) == ("Renamed", "editor")

    with pytest.raises(ProjectPermissionError):
        service.update_project(project.project_id, EDITOR, editors_manage_members=False)
    with pytest.raises(ProjectPermissionError):
        service.update_project(project.project_id, EDITOR, status="archived")
    with pytest.raises(ProjectPermissionError):
        service.update_project(project.project_id, VIEWER, name="Nope")


def test_archived_project_is_read_only_until_the_owner_restores_it(service):
    project = with_members(service)
    service.update_project(project.project_id, OWNER, status="archived")

    with pytest.raises(ProjectConflictError):
        service.update_project(project.project_id, EDITOR, name="Blocked")
    with pytest.raises(ProjectConflictError):
        service.add_members(project.project_id, OWNER, ["new@example.edu"], "viewer")
    assert service.get_project(project.project_id, VIEWER)[0].status == "archived"  # still readable

    restored, _ = service.update_project(project.project_id, OWNER, status="active")
    assert restored.status == "active"


def test_stale_write_is_refused_instead_of_clobbering_member_count(service, repo):
    project = create(service)
    service.add_members(project.project_id, OWNER, ["a@example.edu"], "viewer")  # version moves on

    with pytest.raises(ProjectWriteConflict):
        repo.put_project(project.model_copy(update={"name": "stale"}), expected_version=project.version)
    assert repo.get_project(project.project_id).member_count == 1


def test_purge_requires_archive_and_deletes_harness_then_rows(service, harness, repo):
    project = with_members(service)
    with pytest.raises(ProjectConflictError):
        asyncio.run(service.purge_project(project.project_id, OWNER))

    service.update_project(project.project_id, OWNER, status="archived")
    with pytest.raises(ProjectPermissionError):
        asyncio.run(service.purge_project(project.project_id, EDITOR))

    asyncio.run(service.purge_project(project.project_id, OWNER))
    assert harness.deleted == [project.harness_agent_id]
    assert repo.get_project(project.project_id) is None
    assert repo.list_members(project.project_id) == []
    assert repo.list_memberships(EDITOR.email) == []


def test_purge_leaves_the_project_retryable_if_the_harness_delete_fails(service, harness, repo):
    project = create(service)
    service.update_project(project.project_id, OWNER, status="archived")

    async def fail(agent_id):
        raise RuntimeError("assistants table unavailable")

    harness.delete = fail
    with pytest.raises(RuntimeError):
        asyncio.run(service.purge_project(project.project_id, OWNER))
    assert repo.get_project(project.project_id) is not None


# ── members ─────────────────────────────────────────────────────────────


def test_bulk_add_sorts_every_email_into_one_bucket(service, repo):
    project = create(service)
    service.add_members(project.project_id, OWNER, ["dup@example.edu"], "viewer")

    result = service.add_members(
        project.project_id,
        OWNER,
        ["New@Example.edu", "new@example.edu", "dup@example.edu", "not-an-email", "owner@example.edu", "  "],
        "editor",
    )
    assert [m.email for m in result.added] == ["new@example.edu"]
    assert result.already_members == ["dup@example.edu", "owner@example.edu"]
    assert result.invalid == ["not-an-email"]
    assert repo.get_project(project.project_id).member_count == 2


def test_member_cap_is_enforced_atomically(service, repo, monkeypatch):
    monkeypatch.setenv("PROJECTS_MAX_MEMBERS", "2")
    project = create(service)

    result = service.add_members(
        project.project_id, OWNER, ["a@example.edu", "b@example.edu", "c@example.edu", "d@example.edu"], "viewer"
    )
    assert [m.email for m in result.added] == ["a@example.edu", "b@example.edu"]
    assert result.over_capacity == ["c@example.edu", "d@example.edu"]
    assert repo.get_project(project.project_id).member_count == 2


def test_editors_manage_members_unless_the_owner_turns_it_off(service):
    project = with_members(service)
    service.add_members(project.project_id, EDITOR, ["x@example.edu"], "viewer")

    service.update_project(project.project_id, OWNER, editors_manage_members=False)
    with pytest.raises(ProjectPermissionError):
        service.add_members(project.project_id, EDITOR, ["y@example.edu"], "viewer")
    with pytest.raises(ProjectPermissionError):
        service.add_members(project.project_id, VIEWER, ["z@example.edu"], "viewer")


def test_nobody_can_change_or_remove_the_owner_through_members(service):
    project = with_members(service)
    with pytest.raises(ProjectError):
        service.update_member_role(project.project_id, EDITOR, OWNER.email, "viewer")
    with pytest.raises(ProjectError):
        service.remove_member(project.project_id, EDITOR, OWNER.email)


def test_remove_and_leave_keep_member_count_exact(service, repo):
    project = with_members(service)
    service.remove_member(project.project_id, OWNER, VIEWER.email)
    service.leave(project.project_id, EDITOR)

    assert repo.list_members(project.project_id) == []
    assert repo.get_project(project.project_id).member_count == 0
    with pytest.raises(ProjectNotFoundError):
        service.remove_member(project.project_id, OWNER, VIEWER.email)


def test_owner_cannot_leave(service):
    project = create(service)
    with pytest.raises(ProjectConflictError):
        service.leave(project.project_id, OWNER)


def test_removing_yourself_is_leaving_even_without_manage_rights(service):
    project = with_members(service)
    service.remove_member(project.project_id, VIEWER, VIEWER.email)
    with pytest.raises(ProjectNotFoundError):
        service.get_project(project.project_id, VIEWER)


# ── transfer ────────────────────────────────────────────────────────────


def test_transfer_swaps_owner_and_editor(service, repo):
    project = with_members(service)
    service.get_project(project.project_id, EDITOR)  # first visit back-fills userId

    transferred = service.transfer_ownership(project.project_id, OWNER, EDITOR.email)
    assert (transferred.owner_id, transferred.owner_email) == (EDITOR.user_id, EDITOR.email)
    assert transferred.member_count == 2  # unchanged: the old owner in, the new owner out

    assert service.get_project(project.project_id, OWNER)[1] == "editor"
    assert service.get_project(project.project_id, EDITOR)[1] == "owner"
    assert repo.get_member(project.project_id, EDITOR.email) is None
    assert [p.project_id for p in repo.list_owned(EDITOR.user_id)] == [project.project_id]
    assert repo.list_owned(OWNER.user_id) == []


def test_transfer_only_to_a_signed_in_editor(service):
    project = with_members(service)
    with pytest.raises(ProjectConflictError, match="haven't opened"):
        service.transfer_ownership(project.project_id, OWNER, EDITOR.email)

    service.get_project(project.project_id, VIEWER)
    with pytest.raises(ProjectConflictError, match="editor"):
        service.transfer_ownership(project.project_id, OWNER, VIEWER.email)
    with pytest.raises(ProjectNotFoundError):
        service.transfer_ownership(project.project_id, OWNER, STRANGER.email)
    with pytest.raises(ProjectPermissionError):
        service.transfer_ownership(project.project_id, EDITOR, EDITOR.email)


# ── isolation ───────────────────────────────────────────────────────────


def test_a_role_in_one_project_grants_nothing_in_another(service):
    a = with_members(service)
    b = create(service, STRANGER, "Project B")

    with pytest.raises(ProjectNotFoundError):
        service.get_project(b.project_id, EDITOR)
    with pytest.raises(ProjectNotFoundError):
        service.list_members(b.project_id, EDITOR)
    with pytest.raises(ProjectNotFoundError):
        service.add_members(b.project_id, EDITOR, ["x@example.edu"], "viewer")
    assert service.get_project(a.project_id, EDITOR)[1] == "editor"


def test_member_rows_round_trip_through_the_repository(repo):
    member = ProjectMember(
        project_id="prj_x", email="A@B.edu", role="viewer", invited_by="u1", created_at="t", updated_at="t"
    )
    assert repo._member_to_item(member)["SK"] == "MEMBER#a@b.edu"
    assert repo._member_to_item(member)["GSI2PK"] == "MEMBER#a@b.edu"


def test_a_cancellation_that_is_not_a_condition_failure_is_an_error_not_a_verdict(service, repo, monkeypatch):
    """A throttled or invalid transaction must surface, never be read as "full".

    The service turns ``ProjectWriteConflict`` into "already a member" or "over
    capacity"; handing it an infrastructure failure would silently drop invitees.
    """
    project = create(service)

    def cancelled(**kwargs):
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                "CancellationReasons": [{"Code": "ThrottlingError"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(repo._client, "transact_write_items", cancelled)
    with pytest.raises(ClientError):
        service.add_members(project.project_id, OWNER, ["a@example.edu"], "viewer")
