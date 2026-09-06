"""Tests for the announcement stats funnel (PR-6, spec §9).

The counters are the whole subject. They exist because ``/stats`` needs a
count of acks across users and the key shape does not support one — so the
announcement item carries per-(revision, action) tallies that the ack write
bumps. Two properties matter and both are easy to break:

1. They count **users, not clicks**. A user who goes ``seen`` → ``dismissed``
   adds one to each, not two to ``seen``.
2. They are a **funnel, not a partition**. ``acknowledged`` implies
   ``dismissed`` implies ``seen``, so the three are always non-increasing.
"""

import boto3
import pytest

from apis.shared.announcements.models import (
    AnnouncementCreate,
    ack_count_attr,
)
from apis.shared.announcements.repository import AnnouncementsRepository
from apis.shared.announcements.service import AnnouncementsService

AWS_REGION = "us-west-2"
TABLE_NAME = "test-announcements-stats"

PAST = "2020-01-01T00:00:00Z"
# `expiresAt` is required whenever a loud surface is selected.
FUTURE = "2099-01-01T00:00:00Z"


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
        title="Acceptable use policy update",
        body_markdown="# Policy",
        publish_at=PAST,
        expires_at=FUTURE,
        surfaces=["panel", "modal"],
    )
    defaults.update(kw)
    return AnnouncementCreate(**defaults)


async def _ack(service, announcement, user_id, action, surface="modal"):
    return await service.record_ack(
        user_id=user_id,
        announcement=announcement,
        action=action,
        surface=surface,
    )


class TestAckCounters:
    @pytest.mark.asyncio
    async def test_first_ack_counts_one_user(self, service, repo):
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "seen")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 0, "acknowledged": 0}

    @pytest.mark.asyncio
    async def test_rising_through_ranks_counts_the_user_once_per_rank(
        self, service, repo
    ):
        """The `UPDATED_OLD` read is what makes this true.

        Without the previous rank, `seen` then `dismissed` would add two to
        the seen total — counting clicks instead of people.
        """
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "seen")
        await _ack(service, announcement, "u1", "dismissed")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 1, "acknowledged": 0}

    @pytest.mark.asyncio
    async def test_jumping_straight_to_acknowledged_fills_the_funnel(
        self, service, repo
    ):
        """A `requiresAck` modal writes `seen` then `acknowledged`, but a user
        who never saw the intermediate state must still count at every rung —
        otherwise `seen` would understate reach."""
        announcement = await service.create_announcement(
            _create(requires_ack=True)
        )
        await _ack(service, announcement, "u1", "acknowledged")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 1, "acknowledged": 1}

    @pytest.mark.asyncio
    async def test_a_weaker_late_ack_does_not_double_count(self, service, repo):
        """§D2's straggler: `seen` arriving after `dismissed` is rejected by
        the conditional write, so it must not touch the counters either."""
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "dismissed")
        raised = await _ack(service, announcement, "u1", "seen")

        assert raised is False
        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 1, "acknowledged": 0}

    @pytest.mark.asyncio
    async def test_repeating_the_same_ack_is_idempotent(self, service, repo):
        announcement = await service.create_announcement(_create())
        for _ in range(4):
            await _ack(service, announcement, "u1", "seen")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts["seen"] == 1

    @pytest.mark.asyncio
    async def test_counts_are_per_user(self, service, repo):
        announcement = await service.create_announcement(_create())
        for user in ("u1", "u2", "u3"):
            await _ack(service, announcement, user, "seen")
        await _ack(service, announcement, "u2", "dismissed")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 3, "dismissed": 1, "acknowledged": 0}

    @pytest.mark.asyncio
    async def test_funnel_is_never_increasing(self, service, repo):
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "acknowledged")
        await _ack(service, announcement, "u2", "dismissed")
        await _ack(service, announcement, "u3", "seen")

        c = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert c["seen"] >= c["dismissed"] >= c["acknowledged"]
        assert c == {"seen": 3, "dismissed": 2, "acknowledged": 1}


class TestRevisionScoping:
    @pytest.mark.asyncio
    async def test_revise_starts_a_fresh_count(self, service, repo):
        """"Show again" is a deliberate re-broadcast (§D4).

        Rolling the new revision's acks into the old totals would inflate them
        and make the numbers lie about the version people actually saw.
        """
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "dismissed")

        revised = await service.revise(announcement.announcement_id)
        assert revised.revision == 2

        assert await repo.get_ack_counts(announcement.announcement_id, 2) == {
            "seen": 0,
            "dismissed": 0,
            "acknowledged": 0,
        }
        # The old revision's history is untouched and still readable.
        assert await repo.get_ack_counts(announcement.announcement_id, 1) == {
            "seen": 1,
            "dismissed": 1,
            "acknowledged": 0,
        }

    @pytest.mark.asyncio
    async def test_counters_live_on_the_announcement_item(
        self, service, announcements_table
    ):
        """No GSI and no scan — that is the point of the design."""
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "seen")

        item = announcements_table.get_item(
            Key={
                "PK": "ANNOUNCEMENTS",
                "SK": f"ANNOUNCEMENT#{announcement.announcement_id}",
            }
        )["Item"]
        assert item[ack_count_attr(1, "seen")] == 1


class TestCountersSurviveAdminWrites:
    """Every admin mutation is a full `put_item` of the Announcement model.

    So any attribute the model does not carry is destroyed by it. These are
    the regression: without `Announcement.ack_counts`, publishing an
    announcement — the single most common admin action — silently zeroed
    every stat the feature exists to report.
    """

    @pytest.mark.asyncio
    async def test_publishing_preserves_counts(self, service, repo):
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "seen")

        await service.publish(announcement.announcement_id)

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts["seen"] == 1

    @pytest.mark.asyncio
    async def test_archiving_preserves_counts(self, service, repo):
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "acknowledged")

        await service.publish(announcement.announcement_id)
        await service.archive(announcement.announcement_id)

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 1, "acknowledged": 1}

    @pytest.mark.asyncio
    async def test_editing_the_body_preserves_counts(self, service, repo):
        from apis.shared.announcements.models import AnnouncementUpdate

        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "dismissed")

        await service.update_announcement(
            announcement.announcement_id,
            AnnouncementUpdate(title="Acceptable use policy update (typo fix)"),
        )

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts == {"seen": 1, "dismissed": 1, "acknowledged": 0}

    @pytest.mark.asyncio
    async def test_acks_keep_accruing_after_an_admin_write(self, service, repo):
        """The counters must stay live, not merely survive once."""
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "seen")

        published = await service.publish(announcement.announcement_id)
        await _ack(service, published, "u2", "seen")

        counts = await repo.get_ack_counts(announcement.announcement_id, 1)
        assert counts["seen"] == 2


class TestGetStats:
    @pytest.mark.asyncio
    async def test_reports_the_current_revision(self, service):
        announcement = await service.create_announcement(_create())
        await _ack(service, announcement, "u1", "acknowledged")

        stats = await service.get_stats(announcement.announcement_id)
        assert stats.announcement_id == announcement.announcement_id
        assert stats.revision == 1
        assert (stats.seen, stats.dismissed, stats.acknowledged) == (1, 1, 1)

    @pytest.mark.asyncio
    async def test_unknown_announcement_is_none(self, service):
        assert await service.get_stats("nope") is None

    @pytest.mark.asyncio
    async def test_zero_filled_before_anyone_acks(self, service):
        announcement = await service.create_announcement(_create())
        stats = await service.get_stats(announcement.announcement_id)
        assert (stats.seen, stats.dismissed, stats.acknowledged) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_targeted_is_none_for_a_role_scoped_audience(self, service):
        """None means "not estimated", never zero.

        The users table's StatusLoginIndex does not project `roles`, so a
        role-filtered count has nothing to evaluate against — and the UI must
        say the audience is unknown rather than imply nobody is targeted.
        """
        announcement = await service.create_announcement(
            _create(target_roles=["faculty"])
        )
        stats = await service.get_stats(announcement.announcement_id)
        assert stats.targeted is None

    @pytest.mark.asyncio
    async def test_targeted_is_none_when_the_user_directory_is_unavailable(
        self, service, monkeypatch
    ):
        """A directory blip must not be reported as an audience of zero."""
        monkeypatch.delenv("DYNAMODB_USERS_TABLE_NAME", raising=False)
        announcement = await service.create_announcement(
            _create(target_roles=["*"])
        )
        stats = await service.get_stats(announcement.announcement_id)
        assert stats.targeted is None
