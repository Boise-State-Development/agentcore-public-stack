"""Tests for the Memory Spaces repository + service (PR-1, data layer).

moto-backed DynamoDB (with the OwnerIndex/MemberIndex GSIs) + S3. Exercises
row (de)serialization, GSI listings, and the full permission-gated service
API: create/get/list/delete, share/revoke, index + entry I/O, and the
role-gating (viewer reads, editor writes, owner shares/deletes).
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.memory.models import MemoryIndex, MemorySpace, SpaceMember
from apis.shared.memory.repository import MemorySpaceRepository
from apis.shared.memory.service import (
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)
from apis.shared.memory.store import MemorySpaceStore

AWS_REGION = "us-east-1"
BUCKET = "test-memory-spaces"
TABLE = "test-memory-spaces"

OWNER = "user-owner"
OWNER_EMAIL = "owner@example.edu"
FRIEND = "user-friend"
FRIEND_EMAIL = "friend@example.edu"
STRANGER = "user-stranger"
STRANGER_EMAIL = "stranger@example.edu"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def table(aws_env):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "MemberIndex",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    return MemorySpaceRepository(table_name=TABLE)


@pytest.fixture()
def store(aws_env):
    client = boto3.client("s3", region_name=AWS_REGION)
    client.create_bucket(Bucket=BUCKET)
    return MemorySpaceStore(bucket_name=BUCKET, s3_client=client)


@pytest.fixture()
def service(table, store):
    return MemorySpaceService(repository=table, store=store)


@pytest.fixture()
def space(service):
    return service.create_space(OWNER, OWNER_EMAIL, "My Brain", template="blank")


# ============================ repository ============================


class TestRepository:
    def test_space_round_trip(self, table):
        s = MemorySpace(
            space_id="spc_1",
            name="X",
            template="blank",
            owner_id=OWNER,
            owner_email=OWNER_EMAIL,
            created_at="t0",
            updated_at="t0",
            index_s3_key="spaces/spc_1/abc",
            index_content_hash="abc",
        )
        table.put_space(s)
        got = table.get_space("spc_1")
        assert got is not None
        assert got.name == "X"
        assert got.owner_id == OWNER
        assert got.index_s3_key == "spaces/spc_1/abc"

    def test_get_missing_space_returns_none(self, table):
        assert table.get_space("nope") is None

    def test_list_owned_via_gsi(self, table):
        for i in range(3):
            table.put_space(
                MemorySpace(
                    space_id=f"spc_{i}",
                    name=f"S{i}",
                    owner_id=OWNER,
                    created_at=f"t{i}",
                    updated_at=f"t{i}",
                )
            )
        table.put_space(
            MemorySpace(space_id="other", name="O", owner_id="someone-else")
        )
        owned = table.list_owned(OWNER)
        assert {s.space_id for s in owned} == {"spc_0", "spc_1", "spc_2"}

    def test_index_default_empty(self, table):
        idx = table.get_index("spc_new")
        assert idx.entries == []
        assert idx.version == 0

    def test_index_round_trip_with_indexed_numbers(self, table):
        idx = MemoryIndex(space_id="spc_1", version=2)
        from apis.shared.memory.models import MemoryEntryRef

        idx.entries.append(
            MemoryEntryRef(
                slug="jane",
                entry_type="entity",
                description="VP",
                content_hash="h",
                size=123,
                s3_key="spaces/spc_1/h",
                updated="t",
                updated_by=OWNER,
                indexed={"open": True, "count": 3, "ratio": 0.5},
            )
        )
        table.put_index(idx)
        got = table.get_index("spc_1")
        assert got.version == 2
        assert len(got.entries) == 1
        e = got.entries[0]
        assert e.size == 123 and isinstance(e.size, int)
        assert e.indexed == {"open": True, "count": 3, "ratio": 0.5}

    def test_member_round_trip_and_listing(self, table):
        table.put_member("spc_1", SpaceMember(email=FRIEND_EMAIL, permission="editor"))
        got = table.get_member("spc_1", FRIEND_EMAIL)
        assert got is not None and got.permission == "editor"
        assert [m.email for m in table.list_members("spc_1")] == [FRIEND_EMAIL]
        assert table.list_member_space_ids(FRIEND_EMAIL) == ["spc_1"]

    def test_member_email_normalized(self, table):
        table.put_member("spc_1", SpaceMember(email="MiXeD@Example.EDU"))
        assert table.get_member("spc_1", "mixed@example.edu") is not None

    def test_delete_member(self, table):
        table.put_member("spc_1", SpaceMember(email=FRIEND_EMAIL))
        table.delete_member("spc_1", FRIEND_EMAIL)
        assert table.get_member("spc_1", FRIEND_EMAIL) is None

    def test_delete_space_removes_all_rows(self, table):
        table.put_space(MemorySpace(space_id="spc_1", name="X", owner_id=OWNER))
        table.put_index(MemoryIndex(space_id="spc_1"))
        table.put_member("spc_1", SpaceMember(email=FRIEND_EMAIL))
        table.delete_space("spc_1")
        assert table.get_space("spc_1") is None
        assert table.get_member("spc_1", FRIEND_EMAIL) is None
        assert table.get_index("spc_1").entries == []


# ============================ service: lifecycle ============================


class TestCreateAndList:
    def test_create_seeds_index_and_rows(self, service):
        s = service.create_space(OWNER, OWNER_EMAIL, "Brain", template="chief-of-staff")
        assert s.space_id.startswith("spc_")
        assert s.template == "chief-of-staff"
        assert s.index_s3_key
        # index text is seeded from the template
        text = service.read_index(s.space_id, OWNER, OWNER_EMAIL)
        assert "Strategic priorities" in text

    def test_create_rejects_unknown_template(self, service):
        with pytest.raises(Exception):
            service.create_space(OWNER, OWNER_EMAIL, "X", template="does-not-exist")

    def test_create_rejects_blank_name(self, service):
        with pytest.raises(Exception):
            service.create_space(OWNER, OWNER_EMAIL, "   ", template="blank")

    def test_list_owned_and_shared(self, service):
        a = service.create_space(OWNER, OWNER_EMAIL, "A")
        b = service.create_space(OWNER, OWNER_EMAIL, "B")
        # a third space owned by someone else, shared with FRIEND
        other = service.create_space(STRANGER, STRANGER_EMAIL, "Shared")
        service.share(other.space_id, STRANGER, STRANGER_EMAIL, FRIEND_EMAIL, "viewer")

        owner_spaces = {
            s.space_id for s, _ in service.list_spaces_for_user(OWNER, OWNER_EMAIL)
        }
        assert owner_spaces == {a.space_id, b.space_id}

        friend = service.list_spaces_for_user(FRIEND, FRIEND_EMAIL)
        assert {s.space_id for s, _ in friend} == {other.space_id}
        # shared-in space carries the member's actual grant, not a placeholder
        assert friend[0][1] == "viewer"

    def test_delete_space_owner_only(self, space, service):
        with pytest.raises(MemorySpacePermissionError):
            service.delete_space(space.space_id, STRANGER, STRANGER_EMAIL)
        service.delete_space(space.space_id, OWNER, OWNER_EMAIL)
        with pytest.raises(MemorySpaceNotFoundError):
            service.get_space(space.space_id, OWNER, OWNER_EMAIL)


# ============================ service: permissions ============================


class TestPermissions:
    def test_owner_resolves(self, space, service):
        _, role = service.resolve_permission(space.space_id, OWNER, OWNER_EMAIL)
        assert role == "owner"

    def test_member_resolves(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor")
        _, role = service.resolve_permission(space.space_id, FRIEND, FRIEND_EMAIL)
        assert role == "editor"

    def test_stranger_has_no_role(self, space, service):
        s, role = service.resolve_permission(space.space_id, STRANGER, STRANGER_EMAIL)
        assert s is not None and role is None

    def test_missing_space_resolves_none(self, service):
        s, role = service.resolve_permission("nope", OWNER, OWNER_EMAIL)
        assert s is None and role is None

    def test_get_space_denied_for_stranger(self, space, service):
        with pytest.raises(MemorySpacePermissionError):
            service.get_space(space.space_id, STRANGER, STRANGER_EMAIL)

    def test_get_missing_space_raises_not_found(self, service):
        with pytest.raises(MemorySpaceNotFoundError):
            service.get_space("nope", OWNER, OWNER_EMAIL)


# ============================ service: sharing ============================


class TestSharing:
    def test_share_requires_owner(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        # an editor cannot re-share
        with pytest.raises(MemorySpacePermissionError):
            service.share(space.space_id, FRIEND, FRIEND_EMAIL, STRANGER_EMAIL, "viewer")

    def test_list_members_editor_ok_viewer_denied(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor")
        service.share(space.space_id, OWNER, OWNER_EMAIL, STRANGER_EMAIL, "viewer")
        assert len(service.list_members(space.space_id, OWNER, OWNER_EMAIL)) == 2
        # editor may view members
        assert len(service.list_members(space.space_id, FRIEND, FRIEND_EMAIL)) == 2
        # viewer may not
        with pytest.raises(MemorySpacePermissionError):
            service.list_members(space.space_id, STRANGER, STRANGER_EMAIL)

    def test_revoke(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        service.revoke(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL)
        _, role = service.resolve_permission(space.space_id, FRIEND, FRIEND_EMAIL)
        assert role is None

    def test_member_can_leave(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor")
        service.leave_space(space.space_id, FRIEND, FRIEND_EMAIL)
        _, role = service.resolve_permission(space.space_id, FRIEND, FRIEND_EMAIL)
        assert role is None
        # the space itself still exists for the owner
        assert service.get_space(space.space_id, OWNER, OWNER_EMAIL) is not None

    def test_owner_cannot_leave(self, space, service):
        with pytest.raises(Exception):
            service.leave_space(space.space_id, OWNER, OWNER_EMAIL)

    def test_non_member_cannot_leave(self, space, service):
        with pytest.raises(MemorySpacePermissionError):
            service.leave_space(space.space_id, STRANGER, STRANGER_EMAIL)


# ============================ service: index + entries ============================


class TestIndexAndEntries:
    def test_update_and_read_index(self, space, service):
        service.update_index(space.space_id, OWNER, OWNER_EMAIL, "# New index\n")
        assert service.read_index(space.space_id, OWNER, OWNER_EMAIL) == "# New index\n"

    def test_update_index_requires_editor(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        with pytest.raises(MemorySpacePermissionError):
            service.update_index(space.space_id, FRIEND, FRIEND_EMAIL, "nope")

    def test_write_and_read_entry(self, space, service):
        ref = service.write_entry(
            space.space_id,
            OWNER,
            OWNER_EMAIL,
            "jane-doe",
            "# Jane\nVP Research",
            entry_type="entity",
            description="VP Research",
            indexed={"status": "active"},
        )
        assert ref.slug == "jane-doe"
        assert ref.updated_by == OWNER
        body = service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "jane-doe")
        assert "VP Research" in body

    def test_write_entry_requires_editor(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        with pytest.raises(MemorySpacePermissionError):
            service.write_entry(space.space_id, FRIEND, FRIEND_EMAIL, "x", "body")

    def test_editor_can_write(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor")
        ref = service.write_entry(space.space_id, FRIEND, FRIEND_EMAIL, "note", "hi")
        assert ref.updated_by == FRIEND

    def test_write_replaces_same_slug_and_gcs_old(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v1")
        old_ref = service._find_ref(space.space_id, "n")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v2")
        # manifest has a single entry with the new content
        entries = service.list_entries(space.space_id, OWNER, OWNER_EMAIL)
        assert len(entries) == 1
        assert service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "n") == "v2"
        # old content-addressed object was garbage collected
        from apis.shared.memory.store import MemorySpaceStoreError

        with pytest.raises(MemorySpaceStoreError):
            service.store.get(old_ref.s3_key)

    def test_list_entries_filter_by_type_and_where(self, space, service):
        service.write_entry(
            space.space_id, OWNER, OWNER_EMAIL, "p1", "b",
            entry_type="entity", indexed={"open": True},
        )
        service.write_entry(
            space.space_id, OWNER, OWNER_EMAIL, "p2", "b",
            entry_type="entity", indexed={"open": False},
        )
        service.write_entry(
            space.space_id, OWNER, OWNER_EMAIL, "f1", "b", entry_type="fact"
        )
        entities = service.list_entries(
            space.space_id, OWNER, OWNER_EMAIL, entry_type="entity"
        )
        assert {e.slug for e in entities} == {"p1", "p2"}
        open_ones = service.list_entries(
            space.space_id, OWNER, OWNER_EMAIL, where={"open": True}
        )
        assert {e.slug for e in open_ones} == {"p1"}

    def test_read_missing_entry_raises(self, space, service):
        with pytest.raises(MemorySpaceNotFoundError):
            service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "ghost")

    def test_delete_entry(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v")
        ref = service._find_ref(space.space_id, "n")
        service.delete_entry(space.space_id, OWNER, OWNER_EMAIL, "n")
        assert service.list_entries(space.space_id, OWNER, OWNER_EMAIL) == []
        from apis.shared.memory.store import MemorySpaceStoreError

        with pytest.raises(MemorySpaceStoreError):
            service.store.get(ref.s3_key)

    def test_delete_missing_entry_raises(self, space, service):
        with pytest.raises(MemorySpaceNotFoundError):
            service.delete_entry(space.space_id, OWNER, OWNER_EMAIL, "ghost")

    def test_viewer_can_read_entry(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "shared body")
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        assert (
            service.read_entry(space.space_id, FRIEND, FRIEND_EMAIL, "n")
            == "shared body"
        )
