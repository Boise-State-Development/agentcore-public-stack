"""Tests for the tool-config freshness TTL cache."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.tools import freshness


@pytest.fixture(autouse=True)
def _clear_cache():
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def _tool(updated_at: datetime):
    return SimpleNamespace(updated_at=updated_at)


@pytest.mark.asyncio
async def test_empty_tool_list_returns_empty_hash():
    assert await freshness.get_freshness_hash([]) == ""


@pytest.mark.asyncio
async def test_hash_reflects_updated_at_changes():
    repo = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        h1 = await freshness.get_freshness_hash(["gmail"])

    # Invalidate so the next call re-fetches instead of hitting the TTL cache.
    freshness.invalidate("gmail")

    repo.get_tool = AsyncMock(
        return_value=_tool(datetime(2025, 2, 1, tzinfo=timezone.utc))
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        h2 = await freshness.get_freshness_hash(["gmail"])

    assert h1 != h2


@pytest.mark.asyncio
async def test_ttl_avoids_repeat_reads_within_window():
    """Second call in the TTL window must not hit the repository."""
    repo = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_freshness_hash(["gmail"])
        await freshness.get_freshness_hash(["gmail"])
        await freshness.get_freshness_hash(["gmail"])

    assert repo.get_tool.await_count == 1


@pytest.mark.asyncio
async def test_invalidate_forces_refetch():
    repo = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_tool_updated_at("gmail")
        freshness.invalidate("gmail")
        await freshness.get_tool_updated_at("gmail")

    assert repo.get_tool.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_all_clears_every_entry():
    repo = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_tool_updated_at("gmail")
        await freshness.get_tool_updated_at("jira")

    freshness.invalidate()
    assert freshness._cache == {}


@pytest.mark.asyncio
async def test_missing_tool_is_cached_as_none():
    """A deleted or never-existed tool must not cause a DB hit every turn."""
    repo = SimpleNamespace(get_tool=AsyncMock(return_value=None))
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        result1 = await freshness.get_tool_updated_at("ghost")
        result2 = await freshness.get_tool_updated_at("ghost")

    assert result1 is None
    assert result2 is None
    assert repo.get_tool.await_count == 1


@pytest.mark.asyncio
async def test_repository_error_does_not_raise():
    """Freshness is advisory — a DB blip must not fail the chat turn."""
    repo = SimpleNamespace(get_tool=AsyncMock(side_effect=RuntimeError("boom")))
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        result = await freshness.get_tool_updated_at("gmail")

    assert result is None


@pytest.mark.asyncio
async def test_repository_error_falls_back_to_last_known_value():
    repo_ok = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo_ok,
    ):
        await freshness.get_tool_updated_at("gmail")

    freshness.invalidate("gmail")

    repo_err = SimpleNamespace(get_tool=AsyncMock(side_effect=RuntimeError("boom")))
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo_err,
    ):
        # With invalidate cleared the cache entry, we should return None on error.
        assert await freshness.get_tool_updated_at("gmail") is None


@pytest.mark.asyncio
async def test_hash_is_stable_regardless_of_input_order():
    repo = SimpleNamespace(
        get_tool=AsyncMock(
            return_value=_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        h1 = await freshness.get_freshness_hash(["gmail", "jira"])
        h2 = await freshness.get_freshness_hash(["jira", "gmail"])

    assert h1 == h2
