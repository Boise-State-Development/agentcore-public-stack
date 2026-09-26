"""A project's Memory Spaces (Shared Projects §3.3, Phase 2.4a).

Real ``MemorySpaceService`` and real ``ProjectService`` over moto (the memory
table, its bucket and the projects table), with only the harness faked. The
point is the seam between them: a project space takes every role from the
project, so the matrix below runs through ``resolve_permission`` exactly as
app-api and the Runtime will.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from moto import mock_aws

from apis.shared.auth.models import User
from apis.shared.memory.repository import MemorySpaceRepository
from apis.shared.memory.service import (
    MemorySpaceError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)
from apis.shared.memory.store import MemorySpaceStore
from apis.shared.memory.tokens import TokenCount
from apis.shared.projects.repository import ProjectRepository
from apis.shared.projects.service import ProjectConflictError, ProjectNotFoundError, ProjectService
from tests.shared.test_memory_spaces import TABLE as MEMORY_TABLE
from tests.shared.test_projects import TABLE as PROJECTS_TABLE
from tests.shared.test_projects import FakeHarness, make_projects_table

REGION = "us-east-1"
BUCKET = "test-project-memory"

OWNER = User(user_id="u-owner", email="owner@example.edu", name="Olive", roles=["default"])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="Ed", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="Vi", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="St", roles=["default"])

ITEMS = "- Batch enrollment calls in groups of 50.\n"


def _make_memory_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=MEMORY_TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": name,
                "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }
            for name, pk, sk in (("OwnerIndex", "GSI1PK", "GSI1SK"), ("MemberIndex", "GSI2PK", "GSI2SK"))
        ],
    )


class ServiceMemoryGateway:
    """``ProjectMemoryGateway`` over the test's own ``MemorySpaceService``.

    The default gateway builds a service from the environment; this one uses
    the moto-backed instance so both sides see the same table and bucket.
    """

    def __init__(self, service: MemorySpaceService, enabled: bool = True) -> None:
        self.service = service
        self.enabled = enabled
        self.fail_purge = False

    def create_space(self, *, project_id, scope, owner_id, owner_email, name, user_id=None) -> str:
        return self.service.create_project_space(
            project_id=project_id, scope=scope, owner_id=owner_id, owner_email=owner_email, name=name, user_id=user_id
        ).space_id

    def rename_space(self, space_id, name) -> None:
        self.service.rename_project_space(space_id, name)

    def purge_space(self, space_id) -> None:
        if self.fail_purge:
            raise RuntimeError("bucket unavailable")
        self.service.purge_project_space(space_id)


@pytest.fixture()
def env(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        # resolve_project_role builds its own ProjectRepository from the environment.
        "DYNAMODB_PROJECTS_TABLE_NAME": PROJECTS_TABLE,
        "PROJECTS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        make_projects_table()
        _make_memory_table()
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        yield s3


@pytest.fixture()
def memory(env) -> MemorySpaceService:
    return MemorySpaceService(
        repository=MemorySpaceRepository(table_name=MEMORY_TABLE),
        store=MemorySpaceStore(bucket_name=BUCKET, s3_client=env),
        token_counter=lambda text: TokenCount(len(text) // 4, "count"),
    )


@pytest.fixture()
def gateway(memory) -> ServiceMemoryGateway:
    return ServiceMemoryGateway(memory)


@pytest.fixture()
def projects(env, gateway) -> ProjectService:
    return ProjectService(repository=ProjectRepository(table_name=PROJECTS_TABLE), harness=FakeHarness(), memory=gateway)


def _team(projects: ProjectService, name: str = "Enrollment Sync"):
    project = asyncio.run(projects.create_project(OWNER, name, "Canvas enrollment work"))
    projects.add_members(project.project_id, OWNER, [EDITOR.email], "editor")
    projects.add_members(project.project_id, OWNER, [VIEWER.email], "viewer")
    return project


def _role(memory: MemorySpaceService, space_id: str, user: User):
    space, role = memory.resolve_permission(space_id, user.user_id, user.email)
    return role if space is not None else "missing"


def _objects(s3, space_id: str) -> int:
    return s3.list_objects_v2(Bucket=BUCKET, Prefix=f"spaces/{space_id}/").get("KeyCount", 0)


# ── the shared space ────────────────────────────────────────────────────


def test_a_new_project_gets_a_canonical_shared_space(projects, memory):
    project = _team(projects)
    assert project.shared_space_id

    space = memory.repository.get_space(project.shared_space_id)
    assert (space.scope, space.project_id, space.user_id) == ("shared", project.project_id, None)
    assert space.file_format == "canonical"
    assert space.name == "Enrollment Sync"


def test_project_spaces_stay_out_of_everyones_own_space_list(projects, memory):
    """Kept out of OwnerIndex, so neither the Memory page nor the agent binding
    picker (both read ``list_spaces_for_user``) ever offers a project's space."""
    _team(projects)
    for user in (OWNER, EDITOR, VIEWER):
        assert memory.list_spaces_for_user(user.user_id, user.email) == []


def test_shared_space_roles_follow_the_project(projects, memory):
    project = _team(projects)
    other = asyncio.run(projects.create_project(STRANGER, "Other"))
    sid = project.shared_space_id

    assert _role(memory, sid, OWNER) == "editor"  # nobody resolves owner on a project space
    assert _role(memory, sid, EDITOR) == "editor"
    assert _role(memory, sid, VIEWER) == "viewer"
    assert _role(memory, sid, STRANGER) == "missing"  # owner of a different project
    assert _role(memory, other.shared_space_id, OWNER) == "missing"


def test_membership_changes_apply_at_once(projects, memory):
    project = _team(projects)
    sid = project.shared_space_id

    projects.update_member_role(project.project_id, OWNER, VIEWER.email, "editor")
    assert _role(memory, sid, VIEWER) == "editor"
    projects.remove_member(project.project_id, OWNER, EDITOR.email)
    assert _role(memory, sid, EDITOR) == "missing"


def test_editors_save_and_viewers_read(projects, memory):
    project = _team(projects)
    sid = project.shared_space_id

    saved = memory.save_entry(sid, EDITOR.user_id, EDITOR.email, "canvas", ITEMS, description="Canvas")
    assert saved.ref.version == 1 and saved.ref.item_count == 1
    assert "<!-- e:" in memory.read_entry(sid, VIEWER.user_id, VIEWER.email, "canvas")
    with pytest.raises(MemorySpacePermissionError):
        memory.save_entry(sid, VIEWER.user_id, VIEWER.email, "canvas", ITEMS)
    with pytest.raises(MemorySpaceNotFoundError):
        memory.read_entry(sid, STRANGER.user_id, STRANGER.email, "canvas")


def test_an_archived_projects_memory_is_read_only(projects, memory):
    project = _team(projects)
    sid = project.shared_space_id
    asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))

    assert {_role(memory, sid, u) for u in (OWNER, EDITOR, VIEWER)} == {"viewer"}
    with pytest.raises(MemorySpacePermissionError):
        memory.save_entry(sid, OWNER.user_id, OWNER.email, "canvas", ITEMS)


def test_project_spaces_resolve_for_no_one_while_projects_are_off(projects, memory, monkeypatch):
    project = _team(projects)
    monkeypatch.setenv("PROJECTS_ENABLED", "false")
    assert _role(memory, project.shared_space_id, OWNER) == "missing"


@pytest.mark.parametrize(
    "act",
    [
        lambda m, sid, u: m.share(sid, u.user_id, u.email, "friend@example.edu", "editor"),
        lambda m, sid, u: m.update_share(sid, u.user_id, u.email, "friend@example.edu", "viewer"),
        lambda m, sid, u: m.revoke(sid, u.user_id, u.email, "friend@example.edu"),
        lambda m, sid, u: m.leave_space(sid, u.user_id, u.email),
        lambda m, sid, u: m.delete_space(sid, u.user_id, u.email),
    ],
    ids=["share", "update_share", "revoke", "leave", "delete"],
)
def test_sharing_leaving_and_deleting_belong_to_the_project(projects, memory, act):
    project = _team(projects)
    sid = project.shared_space_id

    with pytest.raises(MemorySpaceError) as ei:
        act(memory, sid, OWNER)
    assert "belongs to a project" in str(ei.value)
    with pytest.raises(MemorySpaceNotFoundError):  # outsiders learn nothing
        act(memory, sid, STRANGER)
    assert memory.repository.get_space(sid) is not None
    assert memory.repository.list_members(sid) == []


def test_a_rename_reaches_the_shared_space(projects, memory):
    project = _team(projects)
    asyncio.run(projects.update_project(project.project_id, EDITOR, name="Enrollment Sync v2"))
    assert memory.repository.get_space(project.shared_space_id).name == "Enrollment Sync v2"


# ── backfill (projects made before 2.4, or while memory was off) ───────


def test_a_project_without_a_space_gets_one_on_first_look(projects, gateway, memory):
    gateway.enabled = False
    project = _team(projects)
    assert project.shared_space_id is None

    gateway.enabled = True
    first = projects.get_memory_spaces(project.project_id, VIEWER)
    again = projects.get_memory_spaces(project.project_id, EDITOR)
    assert first.shared_space_id and again.shared_space_id == first.shared_space_id
    assert projects.repository.get_project(project.project_id).shared_space_id == first.shared_space_id
    assert _role(memory, first.shared_space_id, VIEWER) == "viewer"


def test_racing_backfills_keep_one_space_and_delete_the_other(projects, gateway, memory, env):
    gateway.enabled = False
    project = _team(projects)
    gateway.enabled = True
    stale = projects.repository.get_project(project.project_id)  # read before either writer

    winner = projects.get_memory_spaces(project.project_id, OWNER).shared_space_id
    loser = projects._attach_shared_space(stale)

    assert loser == winner
    spaces = memory.repository._table.scan()["Items"]
    assert {i["spaceId"] for i in spaces if i["SK"] == "META"} == {winner}


def test_backfill_bumps_the_version_so_a_stale_meta_write_cannot_drop_the_pointer(projects, gateway):
    gateway.enabled = False
    project = _team(projects)
    gateway.enabled = True
    before = projects.repository.get_project(project.project_id)
    projects.get_memory_spaces(project.project_id, OWNER)
    assert projects.repository.get_project(project.project_id).version == before.version + 1


# ── personal-in-project spaces ─────────────────────────────────────────


def test_each_member_gets_one_personal_space_on_request(projects, memory):
    project = _team(projects)
    assert projects.get_memory_spaces(project.project_id, VIEWER).personal_space_id is None

    mine = projects.get_or_create_personal_space(project.project_id, VIEWER)
    assert projects.get_or_create_personal_space(project.project_id, VIEWER) == mine
    assert projects.get_memory_spaces(project.project_id, VIEWER).personal_space_id == mine

    space = memory.repository.get_space(mine)
    assert (space.scope, space.project_id, space.user_id) == ("personal_in_project", project.project_id, VIEWER.user_id)
    assert space.file_format == "canonical"


def test_a_personal_space_is_its_members_alone(projects, memory):
    """A viewer of the project still edits their own memory in it; nobody else,
    not even the project owner, resolves any role on it."""
    project = _team(projects)
    mine = projects.get_or_create_personal_space(project.project_id, VIEWER)

    assert _role(memory, mine, VIEWER) == "editor"
    for other in (OWNER, EDITOR, STRANGER):
        assert _role(memory, mine, other) == "missing"
    memory.save_entry(mine, VIEWER.user_id, VIEWER.email, "prefs", ITEMS)

    projects.remove_member(project.project_id, OWNER, VIEWER.email)
    assert _role(memory, mine, VIEWER) == "missing"


def test_racing_personal_creates_converge_on_one_space(projects, memory):
    project = _team(projects)
    first = projects.get_or_create_personal_space(project.project_id, EDITOR)
    # A second writer that missed the pointer on its read creates its own space
    # and loses the conditional claim.
    projects.repository.get_personal_space_id = lambda *_: None
    assert projects.get_or_create_personal_space(project.project_id, EDITOR) == first
    metas = [i for i in memory.repository._table.scan()["Items"] if i["SK"] == "META"]
    assert sorted(i.get("scope") for i in metas) == ["personal_in_project", "shared"]


def test_personal_spaces_need_membership_and_an_active_project(projects):
    project = _team(projects)
    with pytest.raises(ProjectNotFoundError):
        projects.get_or_create_personal_space(project.project_id, STRANGER)
    asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))
    with pytest.raises(ProjectConflictError):
        projects.get_or_create_personal_space(project.project_id, EDITOR)


# ── rollback and purge ─────────────────────────────────────────────────


def test_a_failed_create_deletes_the_shared_space_too(projects, memory, monkeypatch):
    def boom(project):
        raise RuntimeError("META write failed")

    monkeypatch.setattr(projects.repository, "create_project", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(projects.create_project(OWNER, "Doomed"))
    assert memory.repository._table.scan()["Items"] == []


def test_purge_deletes_every_space_the_project_owns(projects, memory, env):
    project = _team(projects)
    sid = project.shared_space_id
    memory.save_entry(sid, EDITOR.user_id, EDITOR.email, "canvas", ITEMS)
    mine = projects.get_or_create_personal_space(project.project_id, VIEWER)
    memory.save_entry(mine, VIEWER.user_id, VIEWER.email, "prefs", ITEMS)
    unrelated = asyncio.run(projects.create_project(OWNER, "Unrelated")).shared_space_id

    asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))
    asyncio.run(projects.purge_project(project.project_id, OWNER))

    for space_id in (sid, mine):
        assert memory.repository.get_space(space_id) is None
        assert _objects(env, space_id) == 0
    assert memory.repository.get_space(unrelated) is not None


def test_a_failed_space_purge_leaves_the_project_for_a_retry(projects, gateway, memory):
    project = _team(projects)
    asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))

    gateway.fail_purge = True
    with pytest.raises(RuntimeError):
        asyncio.run(projects.purge_project(project.project_id, OWNER))
    assert projects.repository.get_project(project.project_id) is not None

    gateway.fail_purge = False
    asyncio.run(projects.purge_project(project.project_id, OWNER))
    assert memory.repository.get_space(project.shared_space_id) is None


def test_the_unchecked_purge_refuses_a_personal_space(memory):
    own = memory.create_space(OWNER.user_id, OWNER.email, "Mine")
    with pytest.raises(MemorySpaceError):
        memory.purge_project_space(own.space_id)
    memory.purge_project_space("spc_already_gone")  # a retried purge passes through
