"""Tests for the announcements shared module (models + repository + service).

The first class is the one that matters. Everything else here is ordinary CRUD
coverage; ``TestMonotonicAck`` is the §D2 regression, and the reason the write
is a conditional ``update_item`` rather than a ``put_item``.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from apis.shared.announcements.models import (
    ACTION_RANKS,
    Announcement,
    AnnouncementAck,
    AnnouncementCreate,
    AnnouncementUpdate,
)
from apis.shared.announcements.repository import AnnouncementsRepository
from apis.shared.announcements.service import AnnouncementsService
from apis.shared.timestamps import from_iso

AWS_REGION = "us-west-2"
TABLE_NAME = "test-announcements"

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


@pytest.fixture()
def announcements_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("DYNAMODB_ANNOUNCEMENTS_TABLE_NAME", TABLE_NAME)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


@pytest.fixture()
def repo(announcements_table):
    return AnnouncementsRepository(table_name=TABLE_NAME, region=AWS_REGION)


@pytest.fixture()
def service(repo):
    return AnnouncementsService(repo)


def _create(**kw) -> AnnouncementCreate:
    defaults = dict(
        title="Skills are here",
        body_markdown="# Skills\n\nTry them.",
        publish_at=PAST,
    )
    defaults.update(kw)
    return AnnouncementCreate(**defaults)


# ======================================================================
# §D2 — the monotonic ack guard. Written first; it is the regression.
# ======================================================================


class TestMonotonicAck:
    @pytest.mark.asyncio
    async def test_seen_after_dismissed_leaves_dismissed_intact(self, service):
        """`seen` is written on render and races the user's ✕ click.

        Without the conditional write, the late `seen` wins and the banner is
        back on the next load. Same failure class as #741 / #751.
        """
        announcement = await service.create_announcement(_create())

        assert await service.record_ack(
            user_id="u1",
            announcement=announcement,
            action="dismissed",
            surface="banner",
        )

        # The straggler.
        raised = await service.record_ack(
            user_id="u1",
            announcement=announcement,
            action="seen",
            surface="banner",
        )

        assert raised is False, "a weaker action must not raise the stored rank"

        ack = await service.get_ack(
            "u1", announcement.announcement_id, announcement.revision
        )
        assert ack.action == "dismissed"
        assert ack.action_rank == ACTION_RANKS["dismissed"]

    @pytest.mark.asyncio
    async def test_a_stronger_action_does_raise_the_rank(self, service):
        announcement = await service.create_announcement(_create())

        await service.record_ack(
            user_id="u1", announcement=announcement, action="seen", surface="panel"
        )
        raised = await service.record_ack(
            user_id="u1",
            announcement=announcement,
            action="acknowledged",
            surface="modal",
        )

        assert raised is True
        ack = await service.get_ack(
            "u1", announcement.announcement_id, announcement.revision
        )
        assert ack.action == "acknowledged"
        assert ack.surface == "modal"

    @pytest.mark.asyncio
    async def test_repeating_the_same_action_is_a_no_op(self, service):
        """Idempotent: `dismissed` twice must not error and must not downgrade."""
        announcement = await service.create_announcement(_create())

        assert await service.record_ack(
            user_id="u1",
            announcement=announcement,
            action="dismissed",
            surface="banner",
        )
        assert not await service.record_ack(
            user_id="u1",
            announcement=announcement,
            action="dismissed",
            surface="banner",
        )

        ack = await service.get_ack(
            "u1", announcement.announcement_id, announcement.revision
        )
        assert ack.action == "dismissed"

    @pytest.mark.asyncio
    async def test_acks_are_scoped_to_the_user(self, service):
        announcement = await service.create_announcement(_create())
        await service.record_ack(
            user_id="u1", announcement=announcement, action="dismissed", surface="banner"
        )

        assert (
            await service.get_ack(
                "u2", announcement.announcement_id, announcement.revision
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_unknown_action_is_rejected(self, service):
        announcement = await service.create_announcement(_create())
        with pytest.raises(ValueError):
            await service.record_ack(
                user_id="u1",
                announcement=announcement,
                action="skimmed",
                surface="panel",
            )


# ======================================================================
# §D4 — revision keying
# ======================================================================


class TestRevisionKeying:
    @pytest.mark.asyncio
    async def test_bumping_revision_leaves_the_new_slot_unacked(self, service):
        announcement = await service.create_announcement(_create())
        await service.record_ack(
            user_id="u1", announcement=announcement, action="dismissed", surface="modal"
        )

        revised = await service.revise(announcement.announcement_id)
        assert revised.revision == 2

        # The R2 slot is empty — the user's suppression has lapsed.
        assert await service.get_ack("u1", revised.announcement_id, 2) is None
        # …and the R1 history is still readable, which is what lets the panel
        # mark the entry "Updated" rather than plain unread.
        r1 = await service.get_ack("u1", revised.announcement_id, 1)
        assert r1 is not None and r1.action == "dismissed"

        acks = await service.list_acks("u1")
        assert {a.revision for a in acks} == {1}

    @pytest.mark.asyncio
    async def test_an_edit_does_not_bump_the_revision(self, service):
        """A typo fix must not re-fire a modal at the whole user base."""
        announcement = await service.create_announcement(_create())
        updated = await service.update_announcement(
            announcement.announcement_id, AnnouncementUpdate(title="Skills are here!")
        )
        assert updated.title == "Skills are here!"
        assert updated.revision == 1

    @pytest.mark.asyncio
    async def test_ack_after_revise_records_the_new_revision(self, service):
        announcement = await service.create_announcement(_create())
        await service.record_ack(
            user_id="u1", announcement=announcement, action="seen", surface="panel"
        )
        revised = await service.revise(announcement.announcement_id)

        await service.record_ack(
            user_id="u1", announcement=revised, action="dismissed", surface="banner"
        )

        acks = {a.revision: a.action for a in await service.list_acks("u1")}
        assert acks == {1: "seen", 2: "dismissed"}


# ======================================================================
# Validation
# ======================================================================


class TestValidation:
    def test_banner_requires_expires_at(self):
        with pytest.raises(ValidationError, match="expiresAt is required"):
            _create(surfaces=["panel", "banner"])

    def test_modal_requires_expires_at(self):
        with pytest.raises(ValidationError, match="expiresAt is required"):
            _create(surfaces=["modal"])

    def test_banner_with_expires_at_is_accepted(self):
        data = _create(surfaces=["panel", "banner"], expires_at=FUTURE)
        assert "banner" in data.surfaces

    def test_panel_only_needs_no_expiry(self):
        assert _create(surfaces=["panel"]).expires_at is None

    def test_cta_url_rejects_javascript_scheme(self):
        with pytest.raises(ValidationError):
            _create(cta_label="Learn more", cta_url="javascript:alert(1)")

    def test_cta_url_and_label_travel_together(self):
        with pytest.raises(ValidationError, match="cta_label is required"):
            _create(cta_url="https://example.test/x")
        with pytest.raises(ValidationError, match="cta_url is required"):
            _create(cta_label="Learn more")

    def test_title_is_capped_at_140(self):
        with pytest.raises(ValidationError):
            _create(title="x" * 141)

    def test_body_is_capped_at_16kb(self):
        with pytest.raises(ValidationError):
            _create(body_markdown="x" * (16 * 1024 + 1))

    def test_body_cap_counts_bytes_not_characters(self):
        """A 3-byte character must count as three."""
        with pytest.raises(ValidationError):
            _create(body_markdown="✅" * 6000)

    def test_expiry_must_follow_publish(self):
        with pytest.raises(ValidationError, match="after publish_at"):
            _create(surfaces=["banner"], publish_at=FUTURE, expires_at=PAST)

    def test_unparseable_timestamp_is_rejected(self):
        with pytest.raises(ValidationError, match="ISO-8601"):
            _create(publish_at="last tuesday")

    @pytest.mark.asyncio
    async def test_patch_that_makes_the_record_invalid_raises(self, service):
        """Adding `banner` to a record with no `expiresAt` is individually
        valid and jointly not — so the merged record is re-validated."""
        announcement = await service.create_announcement(_create())
        with pytest.raises(ValueError, match="expiresAt is required"):
            await service.update_announcement(
                announcement.announcement_id,
                AnnouncementUpdate(surfaces=["panel", "banner"]),
            )


# ======================================================================
# Surfaces, state, and storage
# ======================================================================


class TestSurfaces:
    @pytest.mark.asyncio
    async def test_panel_is_forced_on(self, service):
        """Dismissing a loud surface can never destroy the information (§D1)."""
        created = await service.create_announcement(
            _create(surfaces=["banner"], expires_at=FUTURE)
        )
        assert created.surfaces == ["panel", "banner"]

    @pytest.mark.asyncio
    async def test_surfaces_are_stored_in_canonical_order(self, service):
        created = await service.create_announcement(
            _create(surfaces=["modal", "banner", "panel"], expires_at=FUTURE)
        )
        assert created.surfaces == ["panel", "banner", "modal"]

    @pytest.mark.asyncio
    async def test_patched_surfaces_also_get_panel_forced_on(self, service):
        announcement = await service.create_announcement(_create())
        updated = await service.update_announcement(
            announcement.announcement_id,
            AnnouncementUpdate(surfaces=["modal"], expires_at=FUTURE),
        )
        assert updated.surfaces == ["panel", "modal"]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_create_defaults_to_draft(self, service):
        created = await service.create_announcement(_create())
        assert created.state == "draft"
        assert created.revision == 1
        assert created.target_roles == ["*"]
        assert created.show_to_new_users is False

    @pytest.mark.asyncio
    async def test_publish_then_archive(self, service):
        created = await service.create_announcement(_create())
        published = await service.publish(created.announcement_id)
        assert published.state == "published"
        archived = await service.archive(created.announcement_id)
        assert archived.state == "archived"

    @pytest.mark.asyncio
    async def test_publishing_an_archived_announcement_is_refused(self, service):
        created = await service.create_announcement(_create())
        await service.archive(created.announcement_id)
        with pytest.raises(ValueError, match="cannot publish"):
            await service.publish(created.announcement_id)

    @pytest.mark.asyncio
    async def test_archive_keeps_acks(self, service):
        """The record of who saw what outlives the notice."""
        created = await service.create_announcement(_create())
        await service.record_ack(
            user_id="u1", announcement=created, action="acknowledged", surface="modal"
        )
        await service.archive(created.announcement_id)
        assert len(await service.list_acks("u1")) == 1

    @pytest.mark.asyncio
    async def test_missing_id_returns_none_everywhere(self, service):
        assert await service.get_announcement("nope") is None
        assert await service.update_announcement("nope", AnnouncementUpdate()) is None
        assert await service.publish("nope") is None
        assert await service.archive("nope") is None
        assert await service.revise("nope") is None
        assert await service.delete_announcement("nope") is False

    @pytest.mark.asyncio
    async def test_delete_then_get_is_none(self, service):
        created = await service.create_announcement(_create())
        assert await service.delete_announcement(created.announcement_id) is True
        assert await service.get_announcement(created.announcement_id) is None

    @pytest.mark.asyncio
    async def test_list_filters_by_state_and_sorts_newest_first(self, service):
        old = await service.create_announcement(
            _create(title="Old", publish_at="2021-01-01T00:00:00Z")
        )
        new = await service.create_announcement(
            _create(title="New", publish_at="2026-01-01T00:00:00Z")
        )
        await service.publish(new.announcement_id)

        every = await service.list_announcements()
        assert [a.title for a in every] == ["New", "Old"]

        drafts = await service.list_announcements(states=["draft"])
        assert [a.announcement_id for a in drafts] == [old.announcement_id]


class TestStorageShape:
    @pytest.mark.asyncio
    async def test_announcement_key_shape(self, service, announcements_table):
        created = await service.create_announcement(_create())
        item = announcements_table.get_item(
            Key={
                "PK": "ANNOUNCEMENTS",
                "SK": f"ANNOUNCEMENT#{created.announcement_id}",
            }
        )["Item"]
        assert item["title"] == "Skills are here"
        assert "ttl" not in item, "announcement rows must never expire"

    @pytest.mark.asyncio
    async def test_ack_key_shape(self, service, announcements_table):
        created = await service.create_announcement(_create())
        await service.record_ack(
            user_id="u1", announcement=created, action="seen", surface="panel"
        )
        item = announcements_table.get_item(
            Key={
                "PK": "USER#u1",
                "SK": f"ACK#{created.announcement_id}#R1",
            }
        )["Item"]
        assert item["action"] == "seen"
        assert int(item["actionRank"]) == 1
        assert int(item["revision"]) == 1

    def test_round_trip_preserves_fields(self):
        a = Announcement(
            announcement_id="a1",
            title="T",
            body_markdown="B",
            created_at=PAST,
            updated_at=PAST,
            publish_at=PAST,
            summary="S",
            surfaces=["panel", "modal"],
            severity="warning",
            state="published",
            expires_at=FUTURE,
            target_roles=["faculty"],
            show_to_new_users=True,
            requires_ack=True,
            cta_label="Read",
            cta_url="https://example.test/policy",
            revision=3,
            created_by="admin@example.test",
        )
        assert Announcement.from_dynamo_item(a.to_dynamo_item()) == a

    def test_ack_partition_and_sort_keys(self):
        assert AnnouncementAck.partition_key("u1") == "USER#u1"
        assert AnnouncementAck.sort_key("a1", 2) == "ACK#a1#R2"


class TestAckTtl:
    def _announcement(self, **kw) -> Announcement:
        defaults = dict(
            announcement_id="a1",
            title="T",
            body_markdown="B",
            created_at=PAST,
            updated_at=PAST,
            publish_at=PAST,
        )
        defaults.update(kw)
        return Announcement(**defaults)

    def test_expiring_announcement_ttl_is_expiry_plus_90_days(self):
        a = self._announcement(expires_at="2030-01-01T00:00:00Z")
        assert a.ack_ttl("seen") == int(from_iso("2030-04-01T00:00:00Z").timestamp())

    def test_open_ended_announcement_ttl_is_publish_plus_two_years(self):
        a = self._announcement(publish_at="2030-01-01T00:00:00Z")
        assert a.ack_ttl("dismissed") == int(
            from_iso("2032-01-01T00:00:00Z").timestamp()
        )

    def test_compliance_ack_is_never_expired(self):
        """A `requiresAck` acknowledgement is a record, so it keeps no TTL."""
        a = self._announcement(requires_ack=True, expires_at="2030-01-01T00:00:00Z")
        assert a.ack_ttl("acknowledged") is None
        # A weaker action on the same announcement still expires.
        assert a.ack_ttl("seen") is not None

    @pytest.mark.asyncio
    async def test_upgrading_to_a_compliance_ack_clears_the_ttl(
        self, service, announcements_table
    ):
        """`seen` sets a TTL; the later `acknowledged` must remove it, or the
        compliance record silently evaporates on the earlier schedule."""
        created = await service.create_announcement(
            _create(surfaces=["modal"], expires_at=FUTURE, requires_ack=True)
        )
        await service.record_ack(
            user_id="u1", announcement=created, action="seen", surface="modal"
        )
        key = {"PK": "USER#u1", "SK": f"ACK#{created.announcement_id}#R1"}
        assert "ttl" in announcements_table.get_item(Key=key)["Item"]

        await service.record_ack(
            user_id="u1", announcement=created, action="acknowledged", surface="modal"
        )
        assert "ttl" not in announcements_table.get_item(Key=key)["Item"]


class TestDisabledRepository:
    """No table configured is a disabled repository, not a crash — the table
    ships in platform.yml while this code ships in backend.yml."""

    @pytest.mark.asyncio
    async def test_reads_are_empty_and_writes_refuse(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_ANNOUNCEMENTS_TABLE_NAME", raising=False)
        repo = AnnouncementsRepository()
        assert repo.enabled is False
        assert await repo.list_announcements() == []
        assert await repo.get_announcement("a1") is None
        assert await repo.list_acks("u1") == []
        assert (
            await repo.record_ack(
                user_id="u1",
                announcement_id="a1",
                revision=1,
                action="seen",
                surface="panel",
            )
            is False
        )
        with pytest.raises(RuntimeError):
            await repo.create_announcement(_create())


class TestRepositoryErrorPropagation:
    @pytest.mark.asyncio
    async def test_a_non_conditional_client_error_is_not_swallowed(self, repo):
        """Only ConditionalCheckFailedException means "already acked". Any
        other ClientError is a real fault and must surface."""

        class _Boom:
            def update_item(self, **_):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                    "UpdateItem",
                )

        repo._table = _Boom()
        with pytest.raises(ClientError):
            await repo.record_ack(
                user_id="u1",
                announcement_id="a1",
                revision=1,
                action="seen",
                surface="panel",
            )
