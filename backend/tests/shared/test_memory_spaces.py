"""Tests for the Memory Spaces repository + service (PR-1, data layer).

moto-backed DynamoDB (with the OwnerIndex/MemberIndex GSIs) + S3. Exercises
row (de)serialization, GSI listings, and the full permission-gated service
API: create/get/list/delete, share/revoke, index + entry I/O, and the
role-gating (viewer reads, editor writes, owner shares/deletes).
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.memory import repository as memory_repository
from apis.shared.memory.format import parse_file
from apis.shared.memory.models import MemoryEntryRef, MemoryIndex, MemorySpace, SpaceMember
from apis.shared.memory.repository import MemorySpaceRepository, OptimisticLockError
from apis.shared.memory.service import (
    MemorySpaceError,
    MemoryValidationError,
    MemorySpaceConcurrencyError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)
from apis.shared.memory.store import MemorySpaceStore, compute_content_hash
from apis.shared.memory.tokens import TokenCount

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

    def test_update_share_changes_role_preserving_origin(self, space, service):
        created = service.share(
            space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer"
        )
        updated = service.update_share(
            space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor"
        )
        assert updated.permission == "editor"
        assert updated.created_at == created.created_at
        _, role = service.resolve_permission(space.space_id, FRIEND, FRIEND_EMAIL)
        assert role == "editor"

    def test_update_share_unknown_member_raises(self, space, service):
        with pytest.raises(MemorySpaceNotFoundError):
            service.update_share(
                space.space_id, OWNER, OWNER_EMAIL, STRANGER_EMAIL, "editor"
            )

    def test_update_share_requires_owner(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "editor")
        with pytest.raises(MemorySpacePermissionError):
            service.update_share(
                space.space_id, FRIEND, FRIEND_EMAIL, FRIEND_EMAIL, "viewer"
            )

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

    def test_write_replaces_same_slug_and_keeps_old_as_history(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v1")
        old_ref = service._find_ref(space.space_id, "n")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v2")
        # manifest has a single entry with the new content
        entries = service.list_entries(space.space_id, OWNER, OWNER_EMAIL)
        assert len(entries) == 1
        assert service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "n") == "v2"
        # the old object is kept: its FILEVER row still references it
        assert service.store.get(old_ref.s3_key) == b"v1"

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


# ==================== service: manifest concurrency (A4) ====================


class TestConsolidation:
    def test_reports_healthy_space(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "one")
        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.entry_count == 1
        assert report.over_cap is False
        assert report.duplicate_groups == []
        assert report.dead_links == []
        assert report.orphans_deleted == 0

    def test_gc_deletes_orphaned_objects(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "one")
        # Simulate a leaked object (crashed write) directly in the store.
        orphan_key = service.store.put(
            space_id=space.space_id, content=b"leaked", content_type="text/markdown"
        )
        assert orphan_key in service.store.list_keys(space.space_id)

        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.orphans_deleted == 1
        assert orphan_key not in service.store.list_keys(space.space_id)
        # the live entry's object is untouched
        assert service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "a") == "one"

    def test_gc_can_be_skipped(self, space, service):
        service.store.put(
            space_id=space.space_id, content=b"leaked", content_type="text/markdown"
        )
        report = service.consolidate(
            space.space_id, OWNER, OWNER_EMAIL, apply_gc=False
        )
        assert report.orphans_deleted == 0

    def test_reports_duplicate_content_without_merging(self, space, service):
        # Two different slugs, byte-identical bodies → same content hash.
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "same body")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "b", "same body")
        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.duplicate_groups == [["a", "b"]]
        # both entries still exist — consolidation never auto-merges
        assert len(service.list_entries(space.space_id, OWNER, OWNER_EMAIL)) == 2

    def test_reports_dead_wikilinks(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "jane", "hi")
        service.update_index(
            space.space_id, OWNER, OWNER_EMAIL, "- [[jane]]\n- [[ghost]]\n"
        )
        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.dead_links == ["ghost"]
        assert report.stripped_dead_links is False
        # index untouched unless stripping is requested
        assert "[[ghost]]" in service.read_index(space.space_id, OWNER, OWNER_EMAIL)

    def test_strip_dead_links_unlinks_but_keeps_prose(self, space, service):
        service.update_index(
            space.space_id, OWNER, OWNER_EMAIL, "See [[ghost]] for details.\n"
        )
        report = service.consolidate(
            space.space_id, OWNER, OWNER_EMAIL, strip_dead_links=True
        )
        assert report.stripped_dead_links is True
        text = service.read_index(space.space_id, OWNER, OWNER_EMAIL)
        assert "[[ghost]]" not in text
        assert "See ghost for details." in text

    def test_over_cap_flag(self, space, service, monkeypatch):
        monkeypatch.setenv("MEMORY_SPACE_INDEX_CAP", "2")
        for slug in ("a", "b", "c"):
            service.write_entry(space.space_id, OWNER, OWNER_EMAIL, slug, slug)
        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.index_cap == 2
        assert report.over_cap is True
        # over-cap is reported, never auto-evicted
        assert len(service.list_entries(space.space_id, OWNER, OWNER_EMAIL)) == 3

    def test_requires_editor(self, space, service):
        service.share(space.space_id, OWNER, OWNER_EMAIL, STRANGER_EMAIL, "viewer")
        with pytest.raises(MemorySpacePermissionError):
            service.consolidate(space.space_id, STRANGER, STRANGER_EMAIL)


class TestManifestConcurrency:
    def test_version_increments_per_write(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "b", "2")
        service.delete_entry(space.space_id, OWNER, OWNER_EMAIL, "a")
        # three manifest mutations from the seeded version 0
        assert service.repository.get_index(space.space_id).version == 3

    def test_repository_rejects_stale_conditional_write(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")  # -> v1
        stale = service.repository.get_index(space.space_id)
        # a writer that read v0 tries to commit against the now-v1 row
        with pytest.raises(OptimisticLockError):
            service.repository.put_index(stale, expected_version=0)

    def test_write_retries_and_converges_on_transient_conflict(self, space, service):
        real_put = service.repository.put_index
        calls = {"n": 0}

        def flaky(index, *, expected_version=None):
            calls["n"] += 1
            if calls["n"] == 1:  # first attempt loses the race, then converges
                raise OptimisticLockError("transient")
            return real_put(index, expected_version=expected_version)

        service.repository.put_index = flaky
        ref = service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        assert ref.slug == "a"
        assert calls["n"] >= 2
        assert [
            e.slug for e in service.list_entries(space.space_id, OWNER, OWNER_EMAIL)
        ] == ["a"]

    def test_write_gives_up_after_max_retries(self, space, service):
        def always_conflict(index, *, expected_version=None):
            raise OptimisticLockError("perpetual")

        service.repository.put_index = always_conflict
        with pytest.raises(MemorySpaceConcurrencyError):
            service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")


# ============================ file history (Shared Projects 2.3) ============


def _versions(service, space_id, slug):
    return service.list_file_versions(space_id, OWNER, OWNER_EMAIL, slug)


class TestFileHistory:
    def test_every_save_writes_a_version_row(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v1")
        ref = service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v2", reason="save")
        assert ref.version == 2
        assert ref.tokens == 1 and ref.tokens_method == "estimate"
        versions = _versions(service, space.space_id, "n")
        assert [(v.version, v.reason) for v in versions] == [(2, "save"), (1, "edit")]
        assert versions[1].content_hash == compute_content_hash(b"v1")
        assert versions[0].updated_by == OWNER
        row, text = service.read_file_version(space.space_id, OWNER, OWNER_EMAIL, "n", 1)
        assert (row.version, text) == (1, "v1")

    def test_an_entry_from_before_history_gets_a_baseline(self, space, service):
        key = service.store.put(space_id=space.space_id, content=b"old", content_type="text/markdown")
        legacy = MemoryEntryRef(
            slug="n", content_hash=compute_content_hash(b"old"), size=3, s3_key=key,
            updated="2026-01-01T00:00:00+00:00", updated_by="someone-else",
        )
        service.repository.put_index(MemoryIndex(space_id=space.space_id, entries=[legacy], version=1))
        ref = service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "new")
        assert ref.version == 2
        versions = _versions(service, space.space_id, "n")
        assert [(v.version, v.reason, v.updated_by) for v in versions] == [
            (2, "edit", OWNER),
            (1, "baseline", "someone-else"),
        ]
        assert service.read_file_version(space.space_id, OWNER, OWNER_EMAIL, "n", 1)[1] == "old"

    def test_history_needs_viewer_and_an_existing_entry(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "v1")
        service.share(space.space_id, OWNER, OWNER_EMAIL, FRIEND_EMAIL, "viewer")
        assert len(service.list_file_versions(space.space_id, FRIEND, FRIEND_EMAIL, "n")) == 1
        with pytest.raises(MemorySpacePermissionError):
            service.list_file_versions(space.space_id, STRANGER, STRANGER_EMAIL, "n")
        with pytest.raises(MemorySpaceNotFoundError):
            _versions(service, space.space_id, "ghost")
        with pytest.raises(MemorySpaceNotFoundError):
            service.read_file_version(space.space_id, OWNER, OWNER_EMAIL, "n", 9)

    def test_a_slug_prefix_does_not_leak_into_another_history(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a#b", "2")
        assert [v.slug for v in _versions(service, space.space_id, "a")] == ["a"]

    def test_delete_entry_purges_history_but_not_shared_objects(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "shared")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "only-a")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "b", "shared")
        service.delete_entry(space.space_id, OWNER, OWNER_EMAIL, "a")
        assert service.repository.list_file_versions(space.space_id, "a") == []
        keys = set(service.store.list_keys(space.space_id))
        assert f"spaces/{space.space_id}/{compute_content_hash(b'shared')}" in keys
        assert f"spaces/{space.space_id}/{compute_content_hash(b'only-a')}" not in keys

    def test_delete_space_purges_every_object_and_row(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "2")
        service.store.put(space_id=space.space_id, content=b"orphan", content_type="text/markdown")
        service.delete_space(space.space_id, OWNER, OWNER_EMAIL)
        assert service.store.list_keys(space.space_id) == []
        assert service.repository.list_file_versions(space.space_id) == []
        assert service.repository.get_space(space.space_id) is None

    def test_consolidate_does_not_collect_history(self, space, service):
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "2")
        report = service.consolidate(space.space_id, OWNER, OWNER_EMAIL)
        assert report.orphans_deleted == 0
        assert service.read_file_version(space.space_id, OWNER, OWNER_EMAIL, "a", 1)[1] == "1"

    def test_a_failed_version_row_does_not_fail_the_save(self, space, service, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("dynamodb down")

        monkeypatch.setattr(service.repository, "put_file_version", boom)
        ref = service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "1")
        assert service.read_entry(space.space_id, OWNER, OWNER_EMAIL, "a") == "1"
        assert ref.version == 1


class TestFreeformSavePipeline:
    @pytest.mark.parametrize("slug", ["MEMORY.md", "memory.md"])
    def test_the_index_slug_is_reserved(self, space, service, slug):
        with pytest.raises(MemoryValidationError) as e:
            service.write_entry(space.space_id, OWNER, OWNER_EMAIL, slug, "x")
        assert e.value.code == "reserved_slug"
        assert service.repository.get_index(space.space_id).entries == []

    def test_counted_tokens_are_recorded(self, table, store):
        service = MemorySpaceService(repository=table, store=store, token_counter=lambda t: TokenCount(1234, "count"))
        space = service.create_space(OWNER, OWNER_EMAIL, "S")
        ref = service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "text")
        assert (ref.tokens, ref.tokens_method) == (1234, "count")
        stored = service._find_ref(space.space_id, "a")
        assert (stored.tokens, stored.tokens_method, stored.version) == (1234, "count", 1)
        assert _versions(service, space.space_id, "a")[0].tokens == 1234

    def test_freeform_only_warns(self, space, service, monkeypatch):
        monkeypatch.setenv("MEMORY_FILE_HARD_CAP_TOKENS", "10")
        result = service.save_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "x" * 200 + " [[nowhere]]")
        assert any("nowhere" in w for w in result.warnings)
        assert any("limit per file is 10" in w for w in result.warnings)
        assert result.over_soft_threshold is True

    def test_freeform_rejects_aliases(self, space, service):
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "x", aliases=["b"])
        assert e.value.code == "aliases_unsupported"

    def test_manifest_size_guard(self, space, service, monkeypatch):
        monkeypatch.setattr(memory_repository, "MANIFEST_MAX_BYTES", 200)
        with pytest.raises(MemoryValidationError) as e:
            service.write_entry(space.space_id, OWNER, OWNER_EMAIL, "a", "x")
        assert e.value.code == "manifest_too_large"


# ============================ canonical spaces ============================


@pytest.fixture()
def canonical(service):
    return service.create_space(OWNER, OWNER_EMAIL, "Project memory", file_format="canonical")


def _items(service, space_id, slug):
    return parse_file(service.read_entry(space_id, OWNER, OWNER_EMAIL, slug)).items


class TestCanonicalSpaces:
    def test_file_format_is_stored(self, canonical, service):
        assert service.get_space(canonical.space_id, OWNER, OWNER_EMAIL).file_format == "canonical"
        with pytest.raises(MemorySpaceError):
            service.create_space(OWNER, OWNER_EMAIL, "X", file_format="yaml")

    def test_save_renders_frontmatter_and_mints_anchors(self, canonical, service):
        result = service.save_entry(
            canonical.space_id, OWNER, OWNER_EMAIL, "canvas", "- one\n- two, see [[MEMORY.md]]\n",
            description="Canvas notes", aliases=["LMS"],
        )
        parsed = parse_file(service.read_entry(canonical.space_id, OWNER, OWNER_EMAIL, "canvas"))
        fm = parsed.frontmatter
        assert (fm["name"], fm["description"], fm["aliases"], fm["version"]) == ("canvas", "Canvas notes", ["LMS"], 1)
        assert fm["created"] == fm["updated"] == result.ref.updated
        assert [i.text for i in parsed.items] == ["one", "two, see [[MEMORY.md]]"]
        assert result.minted_anchors == [i.anchor for i in parsed.items]
        assert (result.ref.item_count, result.ref.aliases, result.ref.version) == (2, ["LMS"], 1)

    def test_resave_keeps_anchors_created_and_description(self, canonical, service):
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- one\n- two\n", description="d")
        first = service.read_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n")
        one, two = parse_file(first).items
        edited = first.replace("- one", "- one, edited").replace(f"- two <!-- e:{two.anchor} -->\n", "") + "- three\n"
        result = service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", edited)
        items = _items(service, canonical.space_id, "n")
        assert items[0].anchor == one.anchor and items[0].text == "one, edited"
        assert result.removed_anchors == [two.anchor]
        assert len(result.minted_anchors) == 1
        fm = parse_file(service.read_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n")).frontmatter
        assert (fm["version"], fm["description"]) == (2, "d")
        assert fm["created"] == parse_file(first).frontmatter["created"]
        assert [v.version for v in _versions(service, canonical.space_id, "n")] == [2, 1]

    def test_prose_is_rejected_and_nothing_is_written(self, canonical, service):
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "Just some prose.")
        assert e.value.code == "prose_in_body"
        assert service.repository.get_index(canonical.space_id).entries == []
        assert service.repository.list_file_versions(canonical.space_id) == []
        assert service.store.list_keys(canonical.space_id) == [canonical.index_s3_key]

    def test_hard_cap_rejects_and_soft_threshold_warns(self, table, store):
        size = {"n": 9_000}
        service = MemorySpaceService(repository=table, store=store, token_counter=lambda t: TokenCount(size["n"], "count"))
        space = service.create_space(OWNER, OWNER_EMAIL, "S", file_format="canonical")
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "- x\n")
        assert e.value.code == "over_hard_cap"
        assert service.repository.get_index(space.space_id).entries == []
        size["n"] = 6_500
        result = service.save_entry(space.space_id, OWNER, OWNER_EMAIL, "n", "- x\n")
        assert result.over_soft_threshold and "close to the 8,000-token limit" in result.warnings[0]

    def test_a_stale_echo_is_rejected(self, canonical, service):
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- one\n")
        stale = service.read_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n")
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", stale + "- two\n")
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", stale + "- three\n")
        assert e.value.code == "stale_version"

    def test_a_concurrent_save_of_the_same_file_conflicts(self, canonical, service, monkeypatch):
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- one\n")
        snapshot = service.repository.get_index(canonical.space_id)
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- one\n- two\n", description="moved")
        real = service.repository.get_index
        calls = {"n": 0}

        def stale_first(space_id):
            calls["n"] += 1
            return snapshot if calls["n"] == 1 else real(space_id)

        monkeypatch.setattr(service.repository, "get_index", stale_first)
        with pytest.raises(MemorySpaceConcurrencyError):
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- three\n")

    def test_names_and_aliases_are_unique_across_files(self, canonical, service):
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "canvas", "- x\n", aliases=["lms"])
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "notes", "- y\n", aliases=["LMS"])
        assert e.value.code == "alias_collision"
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "lms", "- y\n")
        assert e.value.code == "name_collision"

    def test_links_new_dead_ones_fail_archived_ones_resolve(self, canonical, service):
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- see [[old]]\n")
        assert e.value.code == "dead_link"
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "old", "- x\n")
        index = service.repository.get_index(canonical.space_id)
        index.entries[0].archived = True
        service.repository.put_index(index)
        result = service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- see [[old]]\n")
        assert result.archived_links == ["old"]

    def test_deleting_a_link_target_does_not_block_edits(self, canonical, service):
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "old", "- x\n")
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", "- see [[old]]\n")
        service.delete_entry(canonical.space_id, OWNER, OWNER_EMAIL, "old")
        text = service.read_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n")
        result = service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "n", text + "- more\n")
        assert any("old" in w for w in result.warnings)

    def test_canonical_names_are_checked(self, canonical, service):
        with pytest.raises(MemoryValidationError) as e:
            service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "Bad Name", "- x\n")
        assert e.value.code == "slug_invalid"

    def test_index_links_are_checked_only_in_canonical_spaces(self, canonical, space, service):
        with pytest.raises(MemoryValidationError) as e:
            service.update_index(canonical.space_id, OWNER, OWNER_EMAIL, "# Memory\n[[nowhere]]\n")
        assert e.value.code == "dead_link"
        service.update_index(space.space_id, OWNER, OWNER_EMAIL, "# Memory\n[[nowhere]]\n")
        service.save_entry(canonical.space_id, OWNER, OWNER_EMAIL, "somewhere", "- x\n")
        service.update_index(canonical.space_id, OWNER, OWNER_EMAIL, "# Memory\n[[Somewhere]]\n")
