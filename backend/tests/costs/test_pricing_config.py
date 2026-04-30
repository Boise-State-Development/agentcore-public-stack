"""Tests for pricing_config module — model lookup, pricing retrieval, and snapshots."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from apis.shared.costs.pricing_config import (
    get_model_by_model_id,
    get_model_pricing,
    create_pricing_snapshot,
)

BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
OPENAI_MODEL_ID = "gpt-4o"
GEMINI_MODEL_ID = "gemini-2.0-flash"


@pytest.fixture
def mock_list_models(sample_managed_models):
    with patch(
        "apis.shared.costs.pricing_config.list_managed_models",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = sample_managed_models
        yield mock


# ── get_model_by_model_id ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_model_by_model_id_found(mock_list_models):
    model = await get_model_by_model_id(BEDROCK_MODEL_ID)
    assert model is not None
    assert model.model_id == BEDROCK_MODEL_ID


@pytest.mark.asyncio
async def test_get_model_by_model_id_not_found(mock_list_models):
    result = await get_model_by_model_id("nonexistent-model")
    assert result is None


@pytest.mark.asyncio
async def test_get_model_by_model_id_calls_list_with_no_roles(mock_list_models):
    await get_model_by_model_id(BEDROCK_MODEL_ID)
    mock_list_models.assert_awaited_once_with(user_roles=None)


# ── get_model_pricing ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_model_pricing_bedrock_has_all_keys(mock_list_models):
    pricing = await get_model_pricing(BEDROCK_MODEL_ID)
    assert pricing is not None
    assert set(pricing.keys()) == {
        "inputPricePerMtok",
        "outputPricePerMtok",
        "cacheWritePricePerMtok",
        "cacheReadPricePerMtok",
    }


@pytest.mark.asyncio
async def test_get_model_pricing_bedrock_correct_values(mock_list_models):
    pricing = await get_model_pricing(BEDROCK_MODEL_ID)
    assert pricing["inputPricePerMtok"] == 3.0
    assert pricing["outputPricePerMtok"] == 15.0
    assert pricing["cacheWritePricePerMtok"] == 3.75
    assert pricing["cacheReadPricePerMtok"] == 0.30


@pytest.mark.asyncio
async def test_get_model_pricing_openai_no_cache_keys(mock_list_models):
    pricing = await get_model_pricing(OPENAI_MODEL_ID)
    assert pricing is not None
    assert set(pricing.keys()) == {"inputPricePerMtok", "outputPricePerMtok"}


@pytest.mark.asyncio
async def test_get_model_pricing_not_found(mock_list_models):
    result = await get_model_pricing("nonexistent-model")
    assert result is None


# ── create_pricing_snapshot ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pricing_snapshot_bedrock_includes_all_fields(mock_list_models):
    snapshot = await create_pricing_snapshot(BEDROCK_MODEL_ID)
    assert snapshot is not None
    assert snapshot["currency"] == "USD"
    assert "snapshotAt" in snapshot
    assert "inputPricePerMtok" in snapshot
    assert "outputPricePerMtok" in snapshot
    assert "cacheWritePricePerMtok" in snapshot
    assert "cacheReadPricePerMtok" in snapshot


@pytest.mark.asyncio
async def test_create_pricing_snapshot_openai_no_cache_fields(mock_list_models):
    snapshot = await create_pricing_snapshot(OPENAI_MODEL_ID)
    assert snapshot is not None
    assert snapshot["currency"] == "USD"
    assert "cacheWritePricePerMtok" not in snapshot
    assert "cacheReadPricePerMtok" not in snapshot


@pytest.mark.asyncio
async def test_create_pricing_snapshot_not_found(mock_list_models):
    result = await create_pricing_snapshot("nonexistent-model")
    assert result is None


@pytest.mark.asyncio
async def test_create_pricing_snapshot_timestamp_ends_with_z(mock_list_models):
    snapshot = await create_pricing_snapshot(BEDROCK_MODEL_ID)
    assert snapshot["snapshotAt"].endswith("Z")


@pytest.mark.asyncio
async def test_create_pricing_snapshot_timestamp_is_valid_iso(mock_list_models):
    snapshot = await create_pricing_snapshot(BEDROCK_MODEL_ID)
    ts = snapshot["snapshotAt"].replace("Z", "+00:00")
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_create_pricing_snapshot_currency_always_usd(mock_list_models):
    for model_id in (BEDROCK_MODEL_ID, OPENAI_MODEL_ID, GEMINI_MODEL_ID):
        snapshot = await create_pricing_snapshot(model_id)
        assert snapshot["currency"] == "USD"
