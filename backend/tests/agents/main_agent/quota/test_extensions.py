"""Quota extensions: stacking in the checker, fresh-on-block, the resolver read, and the pure maths.

docs/specs/quota-requests-and-extensions.md §16 (checker, resolver, ledger key).
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from agents.main_agent.quota.checker import QuotaChecker
from agents.main_agent.quota.event_recorder import QuotaEventRecorder
from agents.main_agent.quota.extensions import (
    attribute_overage,
    compute_funding_key,
    consumption_order,
    periods_overlapping,
)
from agents.main_agent.quota.models import (
    FundingSource,
    QuotaAssignment,
    QuotaAssignmentType,
    QuotaExtension,
    QuotaOverride,
    QuotaTier,
    ResolvedQuota,
)
from agents.main_agent.quota.repository import QuotaRepository
from agents.main_agent.quota.resolver import QuotaResolver
from apis.shared.auth.models import User
from apis.shared.quota import build_quota_funding_notice_event, build_quota_session_notice_event

PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-12-31T23:59:59.999999Z"


def _tier(limit=30.0, action="block", **kw) -> QuotaTier:
    return QuotaTier(
        tier_id="t", tier_name="Faculty", monthly_cost_limit=limit, action_on_limit=action,
        session_notice_percentage=kw.pop("session_notice_percentage", 25),
        created_at=PAST, updated_at=PAST, created_by="admin", **kw,
    )


def _ext(ext_id="e1", amount=20.0, mode="central", standing=False, valid_until=FUTURE,
         valid_from=PAST, created_at="2026-10-01T00:00:00Z", label=None, **kw) -> QuotaExtension:
    source = FundingSource(label=label or "Fund 100000", fields={"fund": "100000"}) if mode == "chargeback" else None
    return QuotaExtension(
        extension_id=ext_id, user_id="u1", standing=standing, amount=amount,
        valid_from=valid_from, valid_until=valid_until, funding_mode=mode,
        funding_source=source, created_by="admin", created_at=created_at, **kw,
    )


USER = User(user_id="u1", email="u1@example.com", name="", roles=[])


def _checker(resolved_seq, usage: float, session_cost=None):
    resolver = Mock(spec=QuotaResolver)
    resolver.resolve_user_quota = AsyncMock(side_effect=list(resolved_seq))
    aggregator = Mock()
    aggregator.get_user_cost_summary = AsyncMock(return_value=Mock(total_cost=usage))
    recorder = Mock(spec=QuotaEventRecorder)
    recorder.record_block = AsyncMock()
    recorder.record_warning_if_needed = AsyncMock()
    recorder.record_session_notice_if_needed = AsyncMock()
    return QuotaChecker(resolver, aggregator, recorder), resolver, aggregator


def _resolved(tier=None, extensions=()):
    return ResolvedQuota(user_id="u1", tier=tier or _tier(), matched_by="default_tier", extensions=list(extensions))


# ── checker ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_effective_limit_sums_active_extensions_and_quiets_the_ladder():
    resolved = _resolved(extensions=[_ext("a", 10), _ext("b", 10)])
    checker, resolver, _ = _checker([resolved], usage=28.5)

    result = await checker.check_quota(USER)

    assert result.allowed
    assert float(result.quota_limit) == 50.0
    assert float(result.base_limit) == 30.0
    assert round(float(result.percentage_used)) == 57
    assert result.warning_level == "50%"  # was 95% on the base alone
    resolver.resolve_user_quota.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_extension_is_excluded():
    resolved = _resolved(extensions=[_ext(valid_until="2021-01-01T00:00:00Z")])
    checker, _, _ = _checker([resolved, resolved], usage=10)

    result = await checker.check_quota(USER)

    assert float(result.quota_limit) == 30.0


@pytest.mark.asyncio
async def test_unlimited_base_ignores_extensions():
    resolved = _resolved(tier=_tier(limit=999999), extensions=[_ext()])
    checker, _, aggregator = _checker([resolved], usage=1000)

    result = await checker.check_quota(USER)

    assert result.allowed and result.message == "Unlimited quota"
    aggregator.get_user_cost_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_on_block_reresolves_once_and_decides_on_the_fresh_result():
    stale = _resolved()
    fresh = _resolved(extensions=[_ext(amount=20)])
    checker, resolver, aggregator = _checker([stale, fresh], usage=31)

    result = await checker.check_quota(USER)

    assert result.allowed
    assert float(result.quota_limit) == 50.0
    assert resolver.resolve_user_quota.await_count == 2
    assert resolver.resolve_user_quota.await_args_list[1].kwargs == {"bypass_cache": True}
    # The spend read is reused, not repeated.
    aggregator.get_user_cost_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_on_block_still_blocks_when_nothing_changed():
    stale = _resolved()
    checker, resolver, _ = _checker([stale, stale], usage=31)

    result = await checker.check_quota(USER)

    assert not result.allowed
    assert resolver.resolve_user_quota.await_count == 2
    checker.event_recorder.record_block.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_refresh_on_a_warn_tier_or_under_the_limit():
    for resolved, usage in ((_resolved(tier=_tier(action="warn")), 40), (_resolved(), 10)):
        checker, resolver, _ = _checker([resolved], usage=usage)
        result = await checker.check_quota(USER)
        assert result.allowed
        resolver.resolve_user_quota.assert_awaited_once()


@pytest.mark.asyncio
async def test_capped_standing_extension_blocks_at_base_plus_cap():
    resolved = _resolved(extensions=[_ext(amount=200, mode="chargeback", standing=True)])
    checker, _, _ = _checker([resolved, resolved], usage=230)

    result = await checker.check_quota(USER)

    assert not result.allowed
    assert float(result.quota_limit) == 230.0


@pytest.mark.asyncio
async def test_uncapped_standing_never_blocks_but_measures_and_notifies():
    resolved = _resolved(extensions=[_ext(amount=None, mode="chargeback", standing=True)])
    checker, _, aggregator = _checker([resolved], usage=500)

    result = await checker.check_quota(USER, session_id="s1", session_total_cost=12.0)

    assert result.allowed
    assert result.quota_limit is None
    aggregator.get_user_cost_summary.assert_awaited_once()
    # Session notice scaled to the base limit: 12 / 30 = 40%, over the 25% share.
    assert float(result.session_percentage_of_limit) == pytest.approx(40.0)
    event = build_quota_session_notice_event(result)
    assert event is not None and event.quota_limit == 30.0
    assert result.funding_notice is not None
    assert float(result.funding_notice.charged_this_period) == pytest.approx(470.0)
    assert result.funding_notice.cap is None


@pytest.mark.asyncio
async def test_funding_notice_only_when_overage_lands_on_chargeback():
    central_only = _resolved(extensions=[_ext("c", 20, "central")])
    checker, _, _ = _checker([central_only], usage=40)
    assert (await checker.check_quota(USER)).funding_notice is None

    both = _resolved(extensions=[_ext("c", 20, "central"), _ext("s", 100, "chargeback", standing=True)])
    checker, _, _ = _checker([both], usage=45)
    result = await checker.check_quota(USER)
    # 15 over the base: central absorbs it, the fund is untouched.
    assert result.funding_notice is None

    checker, _, _ = _checker([both], usage=62.5)
    notice = (await checker.check_quota(USER)).funding_notice
    assert notice.extension_id == "s"
    assert float(notice.charged_this_period) == pytest.approx(12.5)
    assert float(notice.remaining_under_cap) == pytest.approx(87.5)
    event = build_quota_funding_notice_event(
        (await _checker([both], usage=62.5)[0].check_quota(USER))
    )
    assert "Fund 100000" in event.message and "$12.50" in event.message
    assert '"type": "quota_funding_notice"' in event.to_sse_format()


# ── pure maths ──────────────────────────────────────────────────────────────


def test_consumption_order_is_central_then_capped_chargeback_then_standing():
    standing = _ext("s", None, "chargeback", standing=True, valid_until="2026-11-01T00:00:00Z")
    capped = _ext("k", 10, "chargeback", valid_until="2026-12-31T00:00:00Z")
    late_central = _ext("c2", 5, valid_until="2027-01-31T00:00:00Z")
    early_central = _ext("c1", 5, valid_until="2026-11-30T00:00:00Z")

    ordered = [e.extension_id for e in consumption_order([standing, capped, late_central, early_central])]

    assert ordered == ["c1", "c2", "k", "s"]


def test_attribute_overage_waterfall():
    exts = [_ext("c", 10, "central"), _ext("k", 25, "chargeback"), _ext("s", None, "chargeback", standing=True)]
    attributed = {e.extension_id: v for e, v in attribute_overage(50, exts)}
    assert attributed == {"c": 10, "k": 25, "s": 15}


def test_funding_key_is_canonical():
    a = compute_funding_key({"org": " 4210 ", "fund": "100000", "program": ""}, ["fund", "org", "program"])
    b = compute_funding_key({"fund": "100000", "org": "4210"}, ["fund", "org", "program"])
    assert a == b == "fund=100000|org=4210"
    assert compute_funding_key({"fund": "100001", "org": "4210"}, ["fund", "org"]) != a
    assert compute_funding_key({}, ["fund"]) is None


def test_periods_overlapping():
    assert periods_overlapping("2026-10-30T00:00:00Z", "2027-01-31T23:59:59Z") == [
        "2026-10", "2026-11", "2026-12", "2027-01",
    ]


# ── resolver ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_live_app_role_service():
    from unittest.mock import patch

    with patch("agents.main_agent.quota.resolver._get_app_role_service", return_value=None):
        yield


def _repo():
    repo = Mock(spec=QuotaRepository)
    repo.query_user_assignment = AsyncMock(return_value=None)
    repo.query_role_assignments = AsyncMock(return_value=[])
    repo.list_assignments_by_type = AsyncMock(return_value=[])
    repo.get_tier = AsyncMock(return_value=_tier())
    return repo


@pytest.mark.asyncio
async def test_resolver_reads_extensions_concurrently_with_the_override():
    repo = _repo()
    started = []
    release = asyncio.Event()

    async def override(_uid):
        started.append("override")
        await release.wait()
        return QuotaOverride(
            override_id="o", user_id="u1", override_type="custom_limit", monthly_cost_limit=40,
            valid_from=PAST, valid_until=FUTURE, reason="r", created_by="a", created_at=PAST,
        )

    async def extensions(_uid):
        started.append("extensions")
        release.set()  # would deadlock if the reads ran one after the other
        return [_ext()]

    repo.get_active_override = override
    repo.get_active_extensions = extensions

    resolved = await asyncio.wait_for(QuotaResolver(repo).resolve_user_quota(USER), timeout=2)

    assert set(started) == {"override", "extensions"}
    assert resolved.matched_by == "override"
    assert [e.extension_id for e in resolved.extensions] == ["e1"]


@pytest.mark.asyncio
async def test_resolver_degrades_to_no_extensions_on_a_failed_read_and_bypass_refreshes():
    repo = _repo()
    repo.get_active_override = AsyncMock(return_value=None)
    repo.get_active_extensions = AsyncMock(side_effect=RuntimeError("boom"))
    default = QuotaAssignment(
        assignment_id="d", tier_id="t", assignment_type=QuotaAssignmentType.DEFAULT_TIER,
        created_at=PAST, updated_at=PAST, created_by="a",
    )
    repo.list_assignments_by_type = AsyncMock(
        side_effect=lambda assignment_type, enabled_only: [default] if assignment_type == "default_tier" else []
    )
    resolver = QuotaResolver(repo)

    resolved = await resolver.resolve_user_quota(USER)
    assert resolved.extensions == []

    repo.get_active_extensions = AsyncMock(return_value=[_ext()])
    assert (await resolver.resolve_user_quota(USER)).extensions == []  # cached
    assert len((await resolver.resolve_user_quota(USER, bypass_cache=True)).extensions) == 1
    assert len((await resolver.resolve_user_quota(USER)).extensions) == 1  # cache refreshed
