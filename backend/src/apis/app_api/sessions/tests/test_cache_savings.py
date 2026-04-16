"""Unit tests for cache savings calculation in metadata service"""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from apis.app_api.messages.models import (
    MessageMetadata,
    TokenUsage,
    ModelInfo,
    PricingSnapshot,
    Attribution,
)


class TestCacheSavingsCalculation:
    """Test suite for cache savings calculation in _update_cost_summary_async"""

    @pytest.fixture
    def mock_storage(self):
        """Create mock storage backend"""
        storage = AsyncMock()
        storage.update_user_cost_summary = AsyncMock()
        return storage

    @pytest.fixture
    def sample_pricing_snapshot(self):
        """Create sample pricing snapshot with cache pricing"""
        return PricingSnapshot(
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_write_price_per_mtok=3.75,
            cache_read_price_per_mtok=0.30,
            currency="USD",
            snapshot_at=datetime.now(timezone.utc).isoformat()
        )

    @pytest.fixture
    def sample_token_usage_with_cache(self):
        """Create sample token usage with cache reads"""
        return TokenUsage(
            input_tokens=500,
            output_tokens=1000,
            total_tokens=6500,
            cache_read_input_tokens=5000,
            cache_write_input_tokens=0
        )

    @pytest.fixture
    def sample_model_info(self, sample_pricing_snapshot):
        """Create sample model info with pricing"""
        return ModelInfo(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            model_name="Claude Sonnet 4.5",
            pricing_snapshot=sample_pricing_snapshot
        )

    @pytest.fixture
    def sample_message_metadata(self, sample_token_usage_with_cache, sample_model_info):
        """Create sample message metadata with full data"""
        return MessageMetadata(
            token_usage=sample_token_usage_with_cache,
            model_info=sample_model_info,
            attribution=Attribution(
                user_id="test_user",
                session_id="test_session",
                timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=0.018  # Calculated cost
        )

    @pytest.mark.asyncio
    async def test_cache_savings_calculation(self, mock_storage, sample_message_metadata):
        """Test that cache savings are calculated correctly"""
        with patch(
            'apis.app_api.storage.get_metadata_storage',
            return_value=mock_storage
        ):
            from apis.app_api.sessions.services.metadata import _update_cost_summary_async

            # Call the function
            await _update_cost_summary_async(
                user_id="test_user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message_metadata=sample_message_metadata
            )

            # Verify update_user_cost_summary was called
            mock_storage.update_user_cost_summary.assert_called_once()

            # Get the call arguments
            call_kwargs = mock_storage.update_user_cost_summary.call_args.kwargs

            # Expected cache savings:
            # cache_read_tokens = 5000
            # input_price = 3.0
            # cache_read_price = 0.30
            # standard_cost = (5000 / 1_000_000) * 3.0 = 0.015
            # actual_cost = (5000 / 1_000_000) * 0.30 = 0.0015
            # savings = 0.015 - 0.0015 = 0.0135
            expected_savings = 0.0135

            assert call_kwargs.get("cache_savings_delta") == pytest.approx(expected_savings, rel=1e-6)

    @pytest.mark.asyncio
    async def test_cache_savings_zero_when_no_cache_reads(self, mock_storage):
        """Test that cache savings are 0 when there are no cache reads"""
        # Create metadata without cache reads
        token_usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cache_read_input_tokens=0,
            cache_write_input_tokens=0
        )
        pricing = PricingSnapshot(
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_write_price_per_mtok=3.75,
            cache_read_price_per_mtok=0.30,
            currency="USD",
            snapshot_at=datetime.now(timezone.utc).isoformat()
        )
        model_info = ModelInfo(
            model_id="test-model",
            model_name="Test Model",
            pricing_snapshot=pricing
        )
        metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=Attribution(
                user_id="test_user",
                session_id="test_session",
                timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=0.0105
        )

        with patch(
            'apis.app_api.storage.get_metadata_storage',
            return_value=mock_storage
        ):
            from apis.app_api.sessions.services.metadata import _update_cost_summary_async

            await _update_cost_summary_async(
                user_id="test_user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message_metadata=metadata
            )

            call_kwargs = mock_storage.update_user_cost_summary.call_args.kwargs
            assert call_kwargs.get("cache_savings_delta") == 0.0

    @pytest.mark.asyncio
    async def test_cache_savings_zero_when_no_pricing_snapshot(self, mock_storage):
        """Test that cache savings are 0 when pricing snapshot is missing"""
        token_usage = TokenUsage(
            input_tokens=500,
            output_tokens=1000,
            total_tokens=6500,
            cache_read_input_tokens=5000,  # Has cache reads
            cache_write_input_tokens=0
        )
        # Model info without pricing snapshot
        model_info = ModelInfo(
            model_id="test-model",
            model_name="Test Model",
            pricing_snapshot=None  # No pricing
        )
        metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=Attribution(
                user_id="test_user",
                session_id="test_session",
                timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=0.0
        )

        with patch(
            'apis.app_api.storage.get_metadata_storage',
            return_value=mock_storage
        ):
            from apis.app_api.sessions.services.metadata import _update_cost_summary_async

            await _update_cost_summary_async(
                user_id="test_user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message_metadata=metadata
            )

            call_kwargs = mock_storage.update_user_cost_summary.call_args.kwargs
            # Should be 0 because no pricing to calculate from
            assert call_kwargs.get("cache_savings_delta") == 0.0

    @pytest.mark.asyncio
    async def test_cache_savings_large_cache_hit(self, mock_storage):
        """Test cache savings with large cache hit (1M tokens)"""
        token_usage = TokenUsage(
            input_tokens=0,
            output_tokens=1000,
            total_tokens=1_001_000,
            cache_read_input_tokens=1_000_000,  # 1M tokens from cache
            cache_write_input_tokens=0
        )
        pricing = PricingSnapshot(
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_write_price_per_mtok=3.75,
            cache_read_price_per_mtok=0.30,
            currency="USD",
            snapshot_at=datetime.now(timezone.utc).isoformat()
        )
        model_info = ModelInfo(
            model_id="test-model",
            model_name="Test Model",
            pricing_snapshot=pricing
        )
        metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=Attribution(
                user_id="test_user",
                session_id="test_session",
                timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=0.315  # 0.30 + 0.015
        )

        with patch(
            'apis.app_api.storage.get_metadata_storage',
            return_value=mock_storage
        ):
            from apis.app_api.sessions.services.metadata import _update_cost_summary_async

            await _update_cost_summary_async(
                user_id="test_user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message_metadata=metadata
            )

            call_kwargs = mock_storage.update_user_cost_summary.call_args.kwargs

            # Expected savings:
            # standard_cost = (1_000_000 / 1_000_000) * 3.0 = 3.0
            # actual_cost = (1_000_000 / 1_000_000) * 0.30 = 0.30
            # savings = 3.0 - 0.30 = 2.70
            expected_savings = 2.70

            assert call_kwargs.get("cache_savings_delta") == pytest.approx(expected_savings, rel=1e-6)

    @pytest.mark.asyncio
    async def test_cache_savings_with_haiku_pricing(self, mock_storage):
        """Test cache savings with Haiku model pricing (lower prices)"""
        token_usage = TokenUsage(
            input_tokens=500,
            output_tokens=1000,
            total_tokens=6500,
            cache_read_input_tokens=5000,
            cache_write_input_tokens=0
        )
        # Haiku pricing
        pricing = PricingSnapshot(
            input_price_per_mtok=1.0,  # Haiku: $1/Mtok input
            output_price_per_mtok=5.0,  # Haiku: $5/Mtok output
            cache_write_price_per_mtok=1.25,  # 25% markup
            cache_read_price_per_mtok=0.10,  # 90% discount
            currency="USD",
            snapshot_at=datetime.now(timezone.utc).isoformat()
        )
        model_info = ModelInfo(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            model_name="Claude Haiku 4.5",
            pricing_snapshot=pricing
        )
        metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=Attribution(
                user_id="test_user",
                session_id="test_session",
                timestamp=datetime.now(timezone.utc).isoformat()
            ),
            cost=0.006  # Lower cost with Haiku
        )

        with patch(
            'apis.app_api.storage.get_metadata_storage',
            return_value=mock_storage
        ):
            from apis.app_api.sessions.services.metadata import _update_cost_summary_async

            await _update_cost_summary_async(
                user_id="test_user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                message_metadata=metadata
            )

            call_kwargs = mock_storage.update_user_cost_summary.call_args.kwargs

            # Expected savings:
            # standard_cost = (5000 / 1_000_000) * 1.0 = 0.005
            # actual_cost = (5000 / 1_000_000) * 0.10 = 0.0005
            # savings = 0.005 - 0.0005 = 0.0045
            expected_savings = 0.0045

            assert call_kwargs.get("cache_savings_delta") == pytest.approx(expected_savings, rel=1e-6)


class TestPricingSnapshotSerialization:
    """Test that PricingSnapshot serializes correctly for cache savings calculation"""

    def test_pricing_snapshot_model_dump_by_alias(self):
        """Test that PricingSnapshot.model_dump(by_alias=True) produces correct keys"""
        pricing = PricingSnapshot(
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_write_price_per_mtok=3.75,
            cache_read_price_per_mtok=0.30,
            currency="USD",
            snapshot_at="2025-01-15T10:30:00Z"
        )

        pricing_dict = pricing.model_dump(by_alias=True)

        # Verify aliased keys are used
        assert "inputPricePerMtok" in pricing_dict
        assert "outputPricePerMtok" in pricing_dict
        assert "cacheWritePricePerMtok" in pricing_dict
        assert "cacheReadPricePerMtok" in pricing_dict

        # Verify values
        assert pricing_dict["inputPricePerMtok"] == 3.0
        assert pricing_dict["outputPricePerMtok"] == 15.0
        assert pricing_dict["cacheWritePricePerMtok"] == 3.75
        assert pricing_dict["cacheReadPricePerMtok"] == 0.30

    def test_pricing_snapshot_none_cache_prices(self):
        """Test PricingSnapshot with None cache prices (OpenAI scenario)"""
        pricing = PricingSnapshot(
            input_price_per_mtok=30.0,
            output_price_per_mtok=60.0,
            cache_write_price_per_mtok=None,
            cache_read_price_per_mtok=None,
            currency="USD",
            snapshot_at="2025-01-15T10:30:00Z"
        )

        pricing_dict = pricing.model_dump(by_alias=True, exclude_none=True)

        # Cache prices should be excluded
        assert "cacheWritePricePerMtok" not in pricing_dict
        assert "cacheReadPricePerMtok" not in pricing_dict

        # Other prices should be present
        assert pricing_dict["inputPricePerMtok"] == 30.0
        assert pricing_dict["outputPricePerMtok"] == 60.0
