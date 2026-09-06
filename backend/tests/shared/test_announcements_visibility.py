"""The visibility filter — state, dates, roles, new-user suppression, acks, caps.

This is where the logic is, so this is where the tests are (spec §10).
``compute_feed`` is pure, so none of this needs moto: the cases below are the
rules stated as data.
"""

from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.announcements.models import Announcement, AnnouncementAck
from apis.shared.announcements.visibility import SEVERITY_ORDER, compute_feed

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


LAST_WEEK = _iso(NOW - timedelta(days=7))
YESTERDAY = _iso(NOW - timedelta(days=1))
TOMORROW = _iso(NOW + timedelta(days=1))
NEXT_YEAR = _iso(NOW + timedelta(days=365))
TWO_YEARS_AGO = _iso(NOW - timedelta(days=730))


def _announcement(announcement_id: str = "a1", **kw) -> Announcement:
    defaults = dict(
        announcement_id=announcement_id,
        title=f"Title {announcement_id}",
        body_markdown="Body",
        created_at=LAST_WEEK,
        updated_at=LAST_WEEK,
        publish_at=LAST_WEEK,
        state="published",
        surfaces=["panel"],
    )
    defaults.update(kw)
    return Announcement(**defaults)


def _ack(announcement_id: str, action: str, revision: int = 1) -> AnnouncementAck:
    return AnnouncementAck(
        user_id="u1",
        announcement_id=announcement_id,
        revision=revision,
        action=action,
        action_at=YESTERDAY,
        surface="panel",
    )


def _feed(announcements, *, roles=("user",), acks=(), created_at=None, now=NOW):
    return compute_feed(
        announcements=list(announcements),
        user_roles=list(roles),
        acks=list(acks),
        now=now,
        user_created_at=created_at,
    )


def _panel_ids(feed) -> list:
    return [v.announcement.announcement_id for v in feed.panel]


# ======================================================================
# Steps 1-2: state and the publish window
# ======================================================================


class TestStateAndDates:
    @pytest.mark.parametrize("state", ["draft", "scheduled", "archived"])
    def test_only_published_announcements_are_visible(self, state):
        assert _panel_ids(_feed([_announcement(state=state)])) == []

    def test_published_is_visible(self):
        assert _panel_ids(_feed([_announcement()])) == ["a1"]

    def test_a_future_publish_at_is_not_yet_visible(self):
        assert _panel_ids(_feed([_announcement(publish_at=TOMORROW)])) == []

    def test_an_expired_announcement_is_gone(self):
        assert (
            _panel_ids(_feed([_announcement(publish_at=TWO_YEARS_AGO, expires_at=YESTERDAY)]))
            == []
        )

    def test_an_unexpired_announcement_is_visible(self):
        assert _panel_ids(_feed([_announcement(expires_at=TOMORROW)])) == ["a1"]

    def test_no_expiry_means_never_expires(self):
        assert _panel_ids(_feed([_announcement(expires_at=None)])) == ["a1"]

    def test_expiry_exactly_now_is_expired(self):
        """`expiresAt > now` per the spec — the boundary closes the window."""
        assert _panel_ids(_feed([_announcement(expires_at=_iso(NOW))])) == []

    def test_publish_exactly_now_is_live(self):
        """`publishAt <= now` — the boundary opens the window."""
        assert _panel_ids(_feed([_announcement(publish_at=_iso(NOW))])) == ["a1"]

    def test_a_naive_now_is_normalized_rather_than_raising(self):
        """Everything else here is tz-aware, so a naive `now` would blow up on
        the first comparison instead of merely mis-sorting."""
        naive = NOW.replace(tzinfo=None)
        assert _panel_ids(_feed([_announcement()], now=naive)) == ["a1"]

    def test_an_unparseable_date_fails_toward_showing(self):
        """A hand-written row must not silently disappear."""
        a = _announcement()
        a.publish_at = "not a date"
        assert _panel_ids(_feed([a])) == ["a1"]


# ======================================================================
# Step 3: targetRoles — a display filter, not an RBAC grant (§D9)
# ======================================================================


class TestTargetRoles:
    def test_wildcard_targets_everyone(self):
        assert _panel_ids(_feed([_announcement(target_roles=["*"])], roles=["anything"])) == ["a1"]

    def test_a_matching_role_sees_it(self):
        assert _panel_ids(
            _feed([_announcement(target_roles=["faculty"])], roles=["staff", "faculty"])
        ) == ["a1"]

    def test_a_non_matching_role_does_not(self):
        assert _panel_ids(_feed([_announcement(target_roles=["faculty"])], roles=["student"])) == []

    def test_a_user_with_no_roles_still_sees_wildcard_items(self):
        assert _panel_ids(_feed([_announcement()], roles=[])) == ["a1"]

    def test_a_user_with_no_roles_sees_no_targeted_items(self):
        assert _panel_ids(_feed([_announcement(target_roles=["faculty"])], roles=[])) == []

    def test_empty_target_roles_is_treated_as_everyone(self):
        """A hand-written row with no targeting must not vanish."""
        assert _panel_ids(_feed([_announcement(target_roles=[])], roles=["student"])) == ["a1"]


# ======================================================================
# Step 4: new-user backfill suppression (§D6)
# ======================================================================


class TestNewUserSuppression:
    def test_a_user_who_joined_after_publication_sees_nothing(self):
        """The most common failure mode of announcement systems: a first login
        that opens onto a queue of notices about features that, to this user,
        have always existed."""
        assert _panel_ids(_feed([_announcement(publish_at=LAST_WEEK)], created_at=YESTERDAY)) == []

    def test_a_user_who_joined_before_publication_sees_it(self):
        assert _panel_ids(
            _feed([_announcement(publish_at=YESTERDAY)], created_at=LAST_WEEK)
        ) == ["a1"]

    def test_show_to_new_users_overrides_it(self):
        """The standing-policy exception."""
        assert _panel_ids(
            _feed(
                [_announcement(publish_at=LAST_WEEK, show_to_new_users=True)],
                created_at=YESTERDAY,
            )
        ) == ["a1"]

    def test_a_missing_created_at_fails_toward_showing(self):
        assert _panel_ids(_feed([_announcement()], created_at=None)) == ["a1"]

    def test_a_malformed_created_at_fails_toward_showing(self):
        assert _panel_ids(_feed([_announcement()], created_at="tuesday")) == ["a1"]

    def test_a_legacy_offset_and_z_created_at_still_parses(self):
        """Rows written before the timestamp fix carry `+00:00Z` forever."""
        legacy = (NOW - timedelta(days=30)).isoformat() + "Z"
        assert _panel_ids(_feed([_announcement(publish_at=YESTERDAY)], created_at=legacy)) == ["a1"]


# ======================================================================
# Acks: suppression is loud-surface-only; the panel is durable
# ======================================================================


class TestAckSuppression:
    def test_a_dismissed_announcement_stays_in_the_panel(self):
        """§D1/§D2 — dismissing a loud surface must never destroy the record.

        The spec's filter chain lists the ack check before the caps, which read
        literally would drop the entry from the panel too. D1 and D2 are
        explicit that it stays, so suppression applies only to banner/modal.
        """
        feed = _feed(
            [_announcement(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)],
            acks=[_ack("a1", "dismissed")],
        )
        assert _panel_ids(feed) == ["a1"]
        assert feed.banner is None

    def test_seen_does_not_suppress_the_banner(self):
        """`seen` clears the unread dot and nothing else."""
        feed = _feed(
            [_announcement(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)],
            acks=[_ack("a1", "seen")],
        )
        assert feed.banner is not None
        assert feed.unread_count == 0

    def test_acknowledged_suppresses_the_modal(self):
        feed = _feed(
            [_announcement(surfaces=["panel", "modal"], expires_at=NEXT_YEAR)],
            acks=[_ack("a1", "acknowledged")],
        )
        assert feed.modal is None

    def test_an_ack_at_an_older_revision_does_not_suppress(self):
        """§D4 — bumping the revision lapses everyone's suppression at once."""
        feed = _feed(
            [
                _announcement(
                    surfaces=["panel", "banner"], expires_at=NEXT_YEAR, revision=2
                )
            ],
            acks=[_ack("a1", "dismissed", revision=1)],
        )
        assert feed.banner is not None

    def test_an_ack_belonging_to_another_announcement_is_ignored(self):
        feed = _feed(
            [_announcement("a1", surfaces=["panel", "banner"], expires_at=NEXT_YEAR)],
            acks=[_ack("a2", "dismissed")],
        )
        assert feed.banner is not None


class TestUnreadAndUpdated:
    def test_a_never_acked_announcement_is_unread_and_not_updated(self):
        feed = _feed([_announcement()])
        assert feed.panel[0].is_unread is True
        assert feed.panel[0].is_updated is False
        assert feed.unread_count == 1

    def test_acking_the_current_revision_clears_unread(self):
        feed = _feed([_announcement()], acks=[_ack("a1", "seen")])
        assert feed.panel[0].is_unread is False
        assert feed.unread_count == 0

    def test_a_bumped_revision_reads_as_updated_not_merely_new(self):
        """Acked R1, now on R2 — the panel says *Updated*, which is the whole
        reason acks are keyed by revision."""
        feed = _feed([_announcement(revision=2)], acks=[_ack("a1", "dismissed", revision=1)])
        assert feed.panel[0].is_unread is True
        assert feed.panel[0].is_updated is True

    def test_unread_count_counts_only_unacked_items(self):
        feed = _feed(
            [_announcement("a1"), _announcement("a2"), _announcement("a3")],
            acks=[_ack("a2", "seen")],
        )
        assert feed.unread_count == 2


# ======================================================================
# Step 6: the caps (§D7)
# ======================================================================


class TestCaps:
    def test_five_eligible_yield_five_panel_one_banner_one_modal(self):
        loud = dict(surfaces=["panel", "banner", "modal"], expires_at=NEXT_YEAR)
        feed = _feed([_announcement(f"a{i}", **loud) for i in range(5)])

        assert len(feed.panel) == 5
        assert feed.banner is not None
        assert feed.modal is not None

    def test_requires_ack_wins_the_modal_slot(self):
        """A blocking notice is never queued behind an informational one, even
        when the informational one is older and more severe."""
        loud = dict(surfaces=["panel", "modal"], expires_at=NEXT_YEAR)
        informational = _announcement(
            "info", severity="warning", publish_at=TWO_YEARS_AGO, **loud
        )
        blocking = _announcement("blocking", requires_ack=True, publish_at=YESTERDAY, **loud)

        feed = _feed([informational, blocking])
        assert feed.modal.announcement.announcement_id == "blocking"

    def test_banner_prefers_the_higher_severity(self):
        loud = dict(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)
        feed = _feed(
            [
                _announcement("info-item", severity="info", **loud),
                _announcement("warn-item", severity="warning", **loud),
            ]
        )
        assert feed.banner.announcement.announcement_id == "warn-item"

    def test_banner_ties_break_on_oldest_first(self):
        """Oldest-first drains the queue in the order things happened."""
        loud = dict(surfaces=["panel", "banner"], severity="info", expires_at=NEXT_YEAR)
        feed = _feed(
            [
                _announcement("newer", publish_at=YESTERDAY, **loud),
                _announcement("older", publish_at=TWO_YEARS_AGO, **loud),
            ]
        )
        assert feed.banner.announcement.announcement_id == "older"

    def test_a_panel_only_announcement_fills_no_loud_slot(self):
        feed = _feed([_announcement(surfaces=["panel"])])
        assert feed.banner is None and feed.modal is None
        assert len(feed.panel) == 1

    def test_the_panel_is_newest_first(self):
        feed = _feed(
            [
                _announcement("oldest", publish_at=TWO_YEARS_AGO),
                _announcement("newest", publish_at=YESTERDAY),
                _announcement("middle", publish_at=LAST_WEEK),
            ]
        )
        assert _panel_ids(feed) == ["newest", "middle", "oldest"]

    def test_the_loser_of_a_slot_stays_in_the_panel(self):
        """Whatever loses the cap stays eligible for the next page load."""
        loud = dict(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)
        feed = _feed([_announcement("a1", **loud), _announcement("a2", **loud)])
        assert len(feed.panel) == 2
        assert feed.banner is not None


class TestFeedHelpers:
    def test_contains_and_get_answer_over_the_panel(self):
        feed = _feed([_announcement("a1")])
        assert feed.contains("a1") is True
        assert feed.get("a1").announcement_id == "a1"
        assert feed.contains("nope") is False
        assert feed.get("nope") is None

    def test_a_dismissed_item_is_still_ackable(self):
        """A user who dismissed can ack again — idempotent, not a 404."""
        feed = _feed(
            [_announcement(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)],
            acks=[_ack("a1", "dismissed")],
        )
        assert feed.contains("a1") is True

    def test_an_empty_feed_is_empty(self):
        feed = _feed([])
        assert feed.panel == [] and feed.banner is None and feed.modal is None
        assert feed.unread_count == 0

    def test_an_unknown_severity_sorts_last_rather_than_crashing(self):
        loud = dict(surfaces=["panel", "banner"], expires_at=NEXT_YEAR)
        feed = _feed(
            [
                _announcement("weird", severity="chartreuse", **loud),
                _announcement("known", severity="info", **loud),
            ]
        )
        assert feed.banner.announcement.announcement_id == "known"
        assert "chartreuse" not in SEVERITY_ORDER
