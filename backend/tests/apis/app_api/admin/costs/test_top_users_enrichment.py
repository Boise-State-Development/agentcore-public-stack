"""Top-users enrichment — email, tier and quota share on the cost table.

The table exists so the user about to hit their quota is visible without
opening every row, so `quotaPercentage` is the column that matters. The
enrichment is best-effort by design: a fork without the users table, an
unlimited tier, or one failing lookup must each degrade to `None` on that
row and leave the rest of the page intact.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.costs.service import AdminCostService


def _profile(user_id, email="u@x.edu"):
    return SimpleNamespace(user_id=user_id, email=email, name="U", roles=["faculty"])


def _resolved(limit):
    return SimpleNamespace(tier=SimpleNamespace(tier_name="Faculty", monthly_cost_limit=limit))


def _service(users, *, enabled=True, profiles=None, resolver=None):
    service = AdminCostService.__new__(AdminCostService)
    service.storage = AsyncMock()
    service.storage.get_top_users_by_cost = AsyncMock(return_value=[
        {"userId": uid, "totalCost": cost, "totalRequests": 1, "lastUpdated": "t"}
        for uid, cost in users
    ])
    profiles = profiles or {}
    service._user_repository = SimpleNamespace(
        enabled=enabled,
        get_user_by_user_id=AsyncMock(side_effect=lambda uid: profiles.get(uid)),
    )
    service._quota_resolver = resolver or SimpleNamespace(
        resolve_user_quota=AsyncMock(return_value=_resolved(30.0))
    )
    return service


@pytest.mark.asyncio
async def test_rows_gain_email_tier_and_quota_share():
    service = _service([("u1", 22.5), ("u2", 3.0)], profiles={"u1": _profile("u1"), "u2": _profile("u2", "b@x.edu")})
    rows = await service.get_top_users(period="2026-09")
    by_id = {r.user_id: r for r in rows}
    assert by_id["u1"].email == "u@x.edu" and by_id["u1"].tier_name == "Faculty"
    assert by_id["u1"].quota_limit == 30.0 and by_id["u1"].quota_percentage == 75.0
    assert by_id["u2"].quota_percentage == 10.0
    # Cost ordering from storage is untouched by enrichment.
    assert [r.user_id for r in rows] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_an_unlimited_tier_reports_no_share():
    service = _service([("u1", 22.5)], profiles={"u1": _profile("u1")},
                       resolver=SimpleNamespace(resolve_user_quota=AsyncMock(return_value=_resolved(float("inf")))))
    row = (await service.get_top_users())[0]
    assert row.email == "u@x.edu" and row.tier_name == "Faculty"
    assert row.quota_limit is None and row.quota_percentage is None


@pytest.mark.asyncio
async def test_a_disabled_users_table_leaves_every_row_unenriched():
    service = _service([("u1", 22.5)], enabled=False, profiles={"u1": _profile("u1")})
    row = (await service.get_top_users())[0]
    assert row.email is None and row.tier_name is None and row.quota_percentage is None
    service._user_repository.get_user_by_user_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_user_id_is_skipped_not_fatal():
    service = _service([("ghost", 1.0), ("u1", 2.0)], profiles={"u1": _profile("u1")})
    rows = await service.get_top_users()
    by_id = {r.user_id: r for r in rows}
    assert by_id["ghost"].email is None
    assert by_id["u1"].quota_percentage == pytest.approx(6.7)


@pytest.mark.asyncio
async def test_one_failing_lookup_does_not_blank_the_others():
    profiles = {"u1": _profile("u1"), "u2": _profile("u2")}
    service = _service([("u1", 3.0), ("u2", 6.0)], profiles=profiles)

    async def resolve(user):
        if user.user_id == "u1":
            raise RuntimeError("quota table hiccup")
        return _resolved(30.0)

    service._quota_resolver = SimpleNamespace(resolve_user_quota=AsyncMock(side_effect=resolve))
    rows = await service.get_top_users()
    by_id = {r.user_id: r for r in rows}
    assert by_id["u1"].email == "u@x.edu" and by_id["u1"].quota_percentage is None
    assert by_id["u2"].quota_percentage == 20.0


@pytest.mark.asyncio
async def test_an_unconstructible_user_repository_is_not_an_error():
    service = _service([("u1", 1.0)])
    # Simulate a fork where constructing the repository itself blows up.
    def boom():
        raise RuntimeError("no boto3 session")
    service._user_repository = None
    service._users = boom  # type: ignore[method-assign]
    rows = await service.get_top_users()
    assert rows[0].email is None
