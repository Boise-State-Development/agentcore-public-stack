"""Tests for the cross-cache role version watermark.

The user-profile cache (in ``apis.shared.auth.dependencies``) reads roles
from DynamoDB once and reuses them for the configured TTL. When admin
mutates a role definition, every user's cached profile must be invalidated
on the next request, not after the TTL expires. The watermark is the
mechanism that makes this happen: each mutation bumps a global counter,
and cache reads compare the entry's stored counter to the current global
value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.version import bump_roles_version, get_roles_version


def test_version_starts_nonzero() -> None:
    v = get_roles_version()
    assert isinstance(v, int)
    assert v >= 0


def test_bump_increases_version_monotonically() -> None:
    a = get_roles_version()
    b = bump_roles_version()
    c = get_roles_version()
    assert b > a
    assert c == b


def test_bump_is_threadsafe_under_concurrency() -> None:
    """Many concurrent bumps must not lose any updates."""
    import threading

    iterations = 200
    threads = 8
    start = get_roles_version()
    barrier = threading.Barrier(threads)

    def _worker() -> None:
        barrier.wait()
        for _ in range(iterations):
            bump_roles_version()

    workers = [threading.Thread(target=_worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert get_roles_version() == start + iterations * threads


@pytest.mark.asyncio
async def test_user_profile_cache_invalidated_when_version_bumped(monkeypatch) -> None:
    """A bumped version must cause _enrich_user_from_store to re-query DynamoDB."""
    from apis.shared.auth import dependencies as deps

    deps._user_profile_cache.clear()

    user = User(user_id="u1", email="u1@example.com", name="User One", roles=["default"])

    stored_user_a = MagicMock()
    stored_user_a.email = "u1@example.com"
    stored_user_a.name = "User One"
    stored_user_a.roles = ["default"]

    stored_user_b = MagicMock()
    stored_user_b.email = "u1@example.com"
    stored_user_b.name = "User One"
    stored_user_b.roles = ["system_admin"]

    repo = MagicMock()
    repo.enabled = True
    repo.get_user_by_user_id = AsyncMock(side_effect=[stored_user_a, stored_user_b])
    monkeypatch.setattr(deps, "_get_user_repository", lambda: repo)

    # First request — cache miss, hits DynamoDB, stores roles=['default'].
    await deps._enrich_user_from_store(user)
    assert user.roles == ["default"]
    assert repo.get_user_by_user_id.await_count == 1

    # Second request — cache hit, same roles, no DynamoDB call.
    user2 = User(user_id="u1", email="u1@example.com", name="User One", roles=["default"])
    await deps._enrich_user_from_store(user2)
    assert user2.roles == ["default"]
    assert repo.get_user_by_user_id.await_count == 1

    # Admin bumps the version (e.g. mutated a role definition or invalidated cache).
    bump_roles_version()

    # Third request — cache entry is now considered stale; re-queries DynamoDB
    # and picks up the new roles.
    user3 = User(user_id="u1", email="u1@example.com", name="User One", roles=["default"])
    await deps._enrich_user_from_store(user3)
    assert user3.roles == ["system_admin"]
    assert repo.get_user_by_user_id.await_count == 2


@pytest.mark.asyncio
async def test_role_cache_invalidate_all_bumps_version() -> None:
    """The global cache.invalidate_all() must also bump the version."""
    from apis.shared.rbac.cache import AppRoleCache

    cache = AppRoleCache()
    before = get_roles_version()
    await cache.invalidate_all()
    after = get_roles_version()
    assert after > before


@pytest.mark.asyncio
async def test_role_mutation_bumps_version(mock_app_role_repo, mock_app_role_cache, make_app_role) -> None:
    """Updating a role must bump the version so cached user profiles refresh."""
    from apis.shared.rbac.admin_service import AppRoleAdminService

    service = AppRoleAdminService(repository=mock_app_role_repo, cache=mock_app_role_cache)
    role = make_app_role(role_id="custom", is_system_role=False, jwt_role_mappings=["custom"])
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    from apis.shared.rbac.models import AppRoleUpdate

    admin = User(email="a@x", user_id="a-1", name="A", roles=["Admin"])

    before = get_roles_version()
    await service.update_role("custom", AppRoleUpdate(display_name="New"), admin)
    after = get_roles_version()
    assert after > before
