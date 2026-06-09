"""Tests for the skill-catalog freshness TTL cache.

Mirrors backend/tests/apis/app_api/tools/test_freshness.py.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.shared.skills import freshness


@pytest.fixture(autouse=True)
def _clear_cache():
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def _skill(updated_at: datetime):
    return SimpleNamespace(updated_at=updated_at)


@pytest.mark.asyncio
async def test_empty_skill_list_returns_empty_hash():
    assert await freshness.get_freshness_hash([]) == ""


@pytest.mark.asyncio
async def test_hash_reflects_updated_at_changes():
    repo = SimpleNamespace(
        get_skill=AsyncMock(
            return_value=_skill(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        h1 = await freshness.get_freshness_hash(["pdf_workflows"])

    freshness.invalidate("pdf_workflows")

    repo.get_skill = AsyncMock(
        return_value=_skill(datetime(2026, 2, 1, tzinfo=timezone.utc))
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        h2 = await freshness.get_freshness_hash(["pdf_workflows"])

    assert h1 != h2


@pytest.mark.asyncio
async def test_ttl_avoids_repeat_reads_within_window():
    repo = SimpleNamespace(
        get_skill=AsyncMock(
            return_value=_skill(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_freshness_hash(["pdf_workflows"])
        await freshness.get_freshness_hash(["pdf_workflows"])
        await freshness.get_freshness_hash(["pdf_workflows"])

    assert repo.get_skill.await_count == 1


@pytest.mark.asyncio
async def test_invalidate_forces_refetch():
    repo = SimpleNamespace(
        get_skill=AsyncMock(
            return_value=_skill(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_skill_updated_at("pdf_workflows")
        freshness.invalidate("pdf_workflows")
        await freshness.get_skill_updated_at("pdf_workflows")

    assert repo.get_skill.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_all_clears_every_entry():
    repo = SimpleNamespace(
        get_skill=AsyncMock(
            return_value=_skill(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        await freshness.get_skill_updated_at("pdf_workflows")
        await freshness.get_skill_updated_at("doc_basics")

    freshness.invalidate()
    assert freshness._cache == {}


@pytest.mark.asyncio
async def test_missing_skill_is_cached_as_none():
    repo = SimpleNamespace(get_skill=AsyncMock(return_value=None))
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        result1 = await freshness.get_skill_updated_at("ghost")
        result2 = await freshness.get_skill_updated_at("ghost")

    assert result1 is None
    assert result2 is None
    assert repo.get_skill.await_count == 1


@pytest.mark.asyncio
async def test_repository_error_does_not_raise():
    repo = SimpleNamespace(get_skill=AsyncMock(side_effect=RuntimeError("boom")))
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        result = await freshness.get_skill_updated_at("pdf_workflows")

    assert result is None


@pytest.mark.asyncio
async def test_hash_is_stable_regardless_of_input_order():
    repo = SimpleNamespace(
        get_skill=AsyncMock(
            return_value=_skill(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        h1 = await freshness.get_freshness_hash(["pdf_workflows", "doc_basics"])
        h2 = await freshness.get_freshness_hash(["doc_basics", "pdf_workflows"])

    assert h1 == h2


@pytest.mark.asyncio
async def test_get_all_skill_ids_caches_and_invalidates():
    repo = SimpleNamespace(
        list_skills=AsyncMock(
            return_value=[
                SimpleNamespace(skill_id="pdf_workflows"),
                SimpleNamespace(skill_id="doc_basics"),
            ]
        )
    )
    with patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    ):
        ids1 = await freshness.get_all_skill_ids()
        await freshness.get_all_skill_ids()  # served from TTL cache

        assert ids1 == frozenset({"pdf_workflows", "doc_basics"})
        assert repo.list_skills.await_count == 1

        freshness.invalidate()
        await freshness.get_all_skill_ids()  # forced refetch
        assert repo.list_skills.await_count == 2
