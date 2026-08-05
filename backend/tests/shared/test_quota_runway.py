"""Quota runway: the warning ladder, the per-session notice, and the replay
of the incident that motivated both (#833 PR-5).

`docs/specs/compaction-over-threshold-cache-spiral.md` §2 D5: in the
2026-08-05 prod incident every warning event — 80% and 90% — fired on the
same day the hard block landed, and nothing ever said "this one conversation
has cost $28". These tests hold the fix to the spec's §3 acceptance criterion
by replaying the incident session's recorded cost rows.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from agents.main_agent.quota.models import QuotaTier
from agents.main_agent.quota.thresholds import (
    DEFAULT_SESSION_NOTICE_PERCENTAGE,
    resolve_session_notice_percentage,
    resolve_warning_thresholds,
    select_warning_level,
    session_notice_threshold_usd,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "quota_incident_cost_curve.json"


def _tier(**overrides) -> QuotaTier:
    base = dict(
        tier_id="faculty",
        tier_name="Faculty",
        monthly_cost_limit=Decimal("30.0"),
        period_type="monthly",
        action_on_limit="block",
        enabled=True,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        created_by="admin",
    )
    base.update(overrides)
    return QuotaTier(**base)


class TestWarningLadder:
    def test_default_ladder_adds_early_rungs_without_dropping_old_ones(self):
        thresholds = resolve_warning_thresholds(_tier())
        assert thresholds == [50.0, 75.0, 80.0, 90.0]

    def test_tier_soft_limit_participates(self):
        thresholds = resolve_warning_thresholds(
            _tier(soft_limit_percentage=Decimal("60"))
        )
        assert thresholds == [50.0, 60.0, 75.0, 90.0]

    def test_soft_limit_equal_to_an_early_rung_collapses(self):
        thresholds = resolve_warning_thresholds(
            _tier(soft_limit_percentage=Decimal("75"))
        )
        assert thresholds == [50.0, 75.0, 90.0]

    def test_empty_list_opts_a_tier_out_of_early_rungs(self):
        thresholds = resolve_warning_thresholds(
            _tier(early_warning_percentages=[])
        )
        assert thresholds == [80.0, 90.0]

    def test_tier_can_choose_its_own_rungs(self):
        thresholds = resolve_warning_thresholds(
            _tier(early_warning_percentages=[Decimal("25"), Decimal("65")])
        )
        assert thresholds == [25.0, 65.0, 80.0, 90.0]

    def test_kill_switch_restores_legacy_behavior(self, monkeypatch):
        monkeypatch.setenv("QUOTA_RUNWAY_ENABLED", "false")
        assert resolve_warning_thresholds(_tier()) == [80.0, 90.0]
        assert session_notice_threshold_usd(30.0, _tier()) is None

    @pytest.mark.parametrize(
        "percentage,expected",
        [
            (0.0, "none"),
            (49.9, "none"),
            (50.0, "50%"),
            (74.0, "50%"),
            (75.0, "75%"),
            (81.2, "80%"),
            (95.0, "90%"),
            # One turn can cross several rungs; the user is told the highest.
            (100.0, "90%"),
        ],
    )
    def test_select_warning_level(self, percentage, expected):
        assert select_warning_level(percentage, [50.0, 75.0, 80.0, 90.0]) == expected

    def test_fractional_rung_label_is_not_truncated(self):
        assert select_warning_level(88.0, [87.5]) == "87.5%"


class TestSessionNoticeThreshold:
    def test_default_share_is_a_quarter_of_the_monthly_limit(self):
        tier = _tier()
        assert resolve_session_notice_percentage(tier) == DEFAULT_SESSION_NOTICE_PERCENTAGE
        assert session_notice_threshold_usd(30.0, tier) == pytest.approx(7.5)

    def test_zero_disables_the_notice(self):
        tier = _tier(session_notice_percentage=Decimal("0"))
        assert session_notice_threshold_usd(30.0, tier) is None

    def test_tier_can_raise_the_share(self):
        tier = _tier(session_notice_percentage=Decimal("40"))
        assert session_notice_threshold_usd(30.0, tier) == pytest.approx(12.0)


class TestSessionNoticeEvent:
    def _result(self, **kw):
        from agents.main_agent.quota.models import QuotaCheckResult

        base = dict(
            allowed=True,
            message="Within quota",
            current_usage=Decimal("12.0"),
            quota_limit=Decimal("30.0"),
            percentage_used=Decimal("40.0"),
        )
        base.update(kw)
        return QuotaCheckResult(**base)

    def test_no_event_when_the_checker_found_no_heavy_session(self):
        from apis.shared.quota import build_quota_session_notice_event

        assert build_quota_session_notice_event(self._result()) is None

    def test_event_carries_the_dollars_the_user_needs_to_see(self):
        from apis.shared.quota import build_quota_session_notice_event

        event = build_quota_session_notice_event(self._result(
            session_id="c94a3172",
            session_cost=Decimal("7.58"),
            session_percentage_of_limit=Decimal("25.27"),
            session_notice_threshold=Decimal("25.0"),
        ))

        assert event is not None
        assert event.session_id == "c94a3172"
        assert event.message == (
            "This conversation has used $7.58 of your $30.00 monthly quota."
        )
        sse = event.to_sse_format()
        assert "event: quota_session_notice" in sse
        assert '"sessionPercentageOfLimit"' in sse

    def test_a_daily_tier_is_not_told_its_budget_is_a_month(self):
        from apis.shared.quota import build_quota_session_notice_event

        event = build_quota_session_notice_event(self._result(
            tier=_tier(period_type="daily", daily_cost_limit=Decimal("5.0")),
            quota_limit=Decimal("5.0"),
            session_id="s1",
            session_cost=Decimal("1.5"),
            session_percentage_of_limit=Decimal("30.0"),
            session_notice_threshold=Decimal("25.0"),
        ))

        assert event is not None
        assert "daily quota" in event.message


class TestIncidentReplay:
    """§3 PR-5 acceptance: replay the incident's cost curve.

    Fixture is a content-free projection (timestamp + cost) of the 105 ``C#``
    rows of session ``c94a3172-e1fb-4a1d-b375-6e51a56c75ad``, read from prod
    on 2026-08-05. Two clocks run over it, exactly as production would:

    - the **user's period usage** (August only) drives the warning ladder,
      because that is what ``CostAggregator`` returns for a monthly tier;
    - the **session's lifetime cost** drives the notice, because that is what
      ``totalCost`` on the session row holds. The distinction is the whole
      finding: this conversation opened on 2026-07-30 and had already spent
      20% of the month's budget before August began.
    """

    LIMIT = 30.0

    @pytest.fixture(scope="class")
    def curve(self):
        with FIXTURE.open() as f:
            return json.load(f)

    @pytest.fixture(scope="class")
    def replay(self, curve):
        tier = _tier()
        thresholds = resolve_warning_thresholds(tier)
        notice_usd = session_notice_threshold_usd(self.LIMIT, tier)
        period_start = curve["periodStart"]

        session_lifetime = 0.0
        period_usage = 0.0
        first_warning_at = {}
        first_notice_at = None
        limit_reached_at = None

        for call in curve["calls"]:
            # The check runs *before* the turn, so the state it sees is the
            # cost accumulated up to (not including) this call.
            level = select_warning_level(period_usage / self.LIMIT * 100, thresholds)
            if level != "none" and level not in first_warning_at:
                first_warning_at[level] = call["timestamp"]

            if first_notice_at is None and session_lifetime >= notice_usd:
                first_notice_at = call["timestamp"]

            session_lifetime += call["cost"]
            if call["timestamp"] >= period_start:
                period_usage += call["cost"]

            # The moment the budget was actually gone — the call that pushed
            # the period over the limit, which is when the block became
            # inevitable (the block event itself lands on the next attempt,
            # and a blocked attempt writes no cost row to replay).
            if limit_reached_at is None and period_usage >= self.LIMIT:
                limit_reached_at = call["timestamp"]

        return {
            "warnings": first_warning_at,
            "notice": first_notice_at,
            "limit_reached": limit_reached_at,
            "session_total": session_lifetime,
            "period_total": period_usage,
        }

    def test_fixture_matches_the_spec_figures(self, curve):
        """§1: 56 calls totalling $30.45 across 2026-08-01..08-04."""
        incident = [
            c for c in curve["calls"]
            if "2026-08-01" <= c["timestamp"][:10] <= "2026-08-04"
        ]
        assert len(incident) == 56
        assert sum(c["cost"] for c in incident) == pytest.approx(30.45, abs=0.01)

    def test_session_notice_fires_on_august_1(self, replay):
        """The acceptance criterion, met: 'a session notice on Aug 1'."""
        assert replay["notice"] is not None
        assert replay["notice"][:10] == "2026-08-01"

    def test_session_notice_precedes_every_per_user_warning(self, replay):
        """It is the *first* signal the user gets — by three days."""
        earliest_warning = min(replay["warnings"].values())
        assert replay["notice"] < earliest_warning
        assert replay["notice"] < replay["limit_reached"]

    def test_early_rungs_fire_before_the_old_ones(self, replay):
        """50% and 75% both land ahead of the 80% that shipped before."""
        warnings = replay["warnings"]
        assert warnings["50%"] < warnings["80%"]
        assert warnings["75%"] < warnings["80%"]
        assert warnings["80%"] < warnings["90%"] < replay["limit_reached"]

    def test_measured_warning_days(self, replay):
        """The measured days, recorded so a regression has to argue with them.

        §3 predicted 50%/75% on Aug 2–3. Replayed against the real rows they
        land on Aug 3 (UTC) and Aug 4 00:00–02:00 UTC — i.e. both on Aug 3
        in the deployment's own timezone, one day later than the spec
        estimated. See the 'As shipped' note in §3 PR-5: the runway in this
        incident comes from the session notice, not from the extra rungs.
        """
        warnings = replay["warnings"]
        assert warnings["50%"][:10] == "2026-08-03"
        assert warnings["75%"][:10] == "2026-08-04"
        assert warnings["80%"][:10] == "2026-08-04"
        assert replay["limit_reached"][:10] == "2026-08-04"

    def test_todays_config_would_still_have_warned_only_on_block_day(self, curve):
        """The counterfactual: without the ladder, nothing fires before Aug 4."""
        legacy_thresholds = resolve_warning_thresholds(
            _tier(early_warning_percentages=[])
        )
        period_usage = 0.0
        first = None
        for call in curve["calls"]:
            level = select_warning_level(
                period_usage / self.LIMIT * 100, legacy_thresholds
            )
            if level != "none":
                first = call["timestamp"]
                break
            if call["timestamp"] >= curve["periodStart"]:
                period_usage += call["cost"]

        assert first is not None
        assert first[:10] == "2026-08-04"
