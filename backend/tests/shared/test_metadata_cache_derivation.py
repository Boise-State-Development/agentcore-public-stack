"""Write-time cache derivation in apis.shared.sessions.metadata:
``_derive_cache_observability`` (previous-row classification) and the
cache-efficiency counters ``_bump_session_aggregates`` adds to the session row.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apis.shared.sessions import metadata as md
from apis.shared.sessions.models import (
    MessageMetadata,
    ModelInfo,
    PricingSnapshot,
    TokenUsage,
)


NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def _metadata(input_t=100, output_t=50, cache_read=0, cache_write=0, with_pricing=True):
    pricing = None
    if with_pricing:
        pricing = PricingSnapshot(
            input_price_per_mtok=3.0,
            output_price_per_mtok=15.0,
            cache_write_price_per_mtok=3.75,
            cache_read_price_per_mtok=0.30,
            snapshot_at=NOW.isoformat(),
        )
    return MessageMetadata(
        token_usage=TokenUsage(
            input_tokens=input_t,
            output_tokens=output_t,
            total_tokens=input_t + output_t,
            cache_read_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
        ),
        model_info=ModelInfo(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            model_name="Claude Sonnet 4.5",
            pricing_snapshot=pricing,
        ),
    )


class _FakeTable:
    """Fake DynamoDB table: query() serves the previous-cost-row lookup."""

    def __init__(self, prev_items=None):
        self._prev_items = prev_items or []
        self.query_kwargs = None
        self.update_kwargs = None

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        return {"Items": self._prev_items}

    def update_item(self, **kwargs):
        self.update_kwargs = kwargs
        return {}


def _prev_row(seconds_ago, cache_read=0, cache_write=0):
    ts = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    return {
        "timestamp": ts,
        "tokenUsage": {
            "cacheReadInputTokens": Decimal(cache_read),
            "cacheWriteInputTokens": Decimal(cache_write),
        },
    }


class TestDeriveCacheObservability:
    def _derive(self, table, meta):
        return md._derive_cache_observability(
            session_id="sess-1",
            table=table,
            timestamp=NOW.isoformat(),
            message_metadata=meta,
        )

    def test_no_token_usage_returns_empty(self):
        meta = MessageMetadata(token_usage=None)
        assert self._derive(_FakeTable(), meta) == {}

    def test_first_write_without_previous_row(self):
        result = self._derive(_FakeTable(), _metadata(cache_write=5000))
        assert result["cacheStatus"] == "first_write"
        assert result["wastedUsd"] == 0.0
        assert "cacheGapSeconds" not in result

    def test_hit_with_previous_row(self):
        table = _FakeTable([_prev_row(seconds_ago=30, cache_write=5000)])
        result = self._derive(table, _metadata(cache_read=5000, cache_write=100))
        assert result["cacheStatus"] == "hit"
        assert result["cacheGapSeconds"] == 30
        assert result["wastedUsd"] == 0.0

    def test_avoidable_miss_within_ttl_prices_waste(self):
        # Previous call cached 4000 + 1000 tokens; this call re-writes 6000
        # with zero read, 60s later → avoidable. Waste = min(6000, 5000)
        # tokens at the (3.75 - 0.30)/Mtok premium.
        table = _FakeTable([_prev_row(seconds_ago=60, cache_read=4000, cache_write=1000)])
        result = self._derive(table, _metadata(cache_write=6000))
        assert result["cacheStatus"] == "miss_avoidable"
        assert result["cacheGapSeconds"] == 60
        assert result["wastedUsd"] == round((5000 / 1_000_000) * 3.45, 6)

    def test_ttl_expired_miss_beyond_gap(self):
        table = _FakeTable([_prev_row(seconds_ago=1200, cache_write=5000)])
        result = self._derive(table, _metadata(cache_write=6000))
        assert result["cacheStatus"] == "miss_ttl_expired"
        assert result["wastedUsd"] == 0.0

    def test_uncached_when_no_cache_activity(self):
        table = _FakeTable([_prev_row(seconds_ago=30)])
        result = self._derive(table, _metadata())
        assert result["cacheStatus"] == "uncached"

    def test_query_targets_previous_row_descending_limit_1(self):
        table = _FakeTable()
        self._derive(table, _metadata(cache_write=100))
        assert table.query_kwargs["IndexName"] == "SessionLookupIndex"
        assert table.query_kwargs["ScanIndexForward"] is False
        assert table.query_kwargs["Limit"] == 1

    def test_failure_returns_empty_never_raises(self):
        class _Boom:
            def query(self, **kwargs):
                raise RuntimeError("dynamo down")

        assert self._derive(_Boom(), _metadata(cache_write=100)) == {}


class TestBumpSessionAggregatesCacheCounters:
    @pytest.fixture
    def session_lookup(self, monkeypatch):
        async def _fake_get_session(session_id, user_id, table):
            return {"SK": f"S#{session_id}"}

        monkeypatch.setattr(md, "_get_session_by_gsi", _fake_get_session)

    async def _bump(self, table, meta, observability):
        await md._bump_session_aggregates(
            session_id="sess-1",
            user_id="user-1",
            message_metadata=meta,
            table=table,
            cache_observability=observability,
        )

    @pytest.mark.asyncio
    async def test_adds_cache_counters_next_to_total_cost(self, session_lookup):
        table = _FakeTable()
        meta = _metadata(cache_read=4000, cache_write=1000)
        await self._bump(
            table, meta, {"cacheStatus": "miss_avoidable", "wastedUsd": 0.01725}
        )

        expr = table.update_kwargs["UpdateExpression"]
        values = table.update_kwargs["ExpressionAttributeValues"]
        for attr in (
            "totalCost :c",
            "totalCacheReadTokens :cacheRead",
            "totalCacheWriteTokens :cacheWrite",
            "avoidableMissCount :avoidableMiss",
            "wastedUsd :wasted",
        ):
            assert attr in expr
        assert values[":cacheRead"] == 4000
        assert values[":cacheWrite"] == 1000
        assert values[":avoidableMiss"] == 1
        assert values[":wasted"] == Decimal("0.01725")

    @pytest.mark.asyncio
    async def test_zero_deltas_when_not_avoidable(self, session_lookup):
        table = _FakeTable()
        await self._bump(table, _metadata(cache_read=4000), {"cacheStatus": "hit", "wastedUsd": 0.0})
        values = table.update_kwargs["ExpressionAttributeValues"]
        assert values[":avoidableMiss"] == 0
        assert values[":wasted"] == Decimal("0")

    @pytest.mark.asyncio
    async def test_missing_observability_defaults_to_zeroes(self, session_lookup):
        table = _FakeTable()
        await self._bump(table, _metadata(), None)
        values = table.update_kwargs["ExpressionAttributeValues"]
        assert values[":cacheRead"] == 0
        assert values[":cacheWrite"] == 0
        assert values[":avoidableMiss"] == 0


class TestKillSwitch:
    """PROMPT_CACHE_OBSERVABILITY_ENABLED=false disables derivation and EMF."""

    def test_flag_default_and_parsing(self, monkeypatch):
        from apis.shared.observability import (
            PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV,
            prompt_cache_observability_enabled,
        )

        monkeypatch.delenv(PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV, raising=False)
        assert prompt_cache_observability_enabled() is True
        # Empty string (workflow env vars can materialize as "") stays enabled
        monkeypatch.setenv(PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV, "")
        assert prompt_cache_observability_enabled() is True
        monkeypatch.setenv(PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV, "FALSE")
        assert prompt_cache_observability_enabled() is False
        monkeypatch.setenv(PROMPT_CACHE_OBSERVABILITY_ENABLED_ENV, "true")
        assert prompt_cache_observability_enabled() is True

    def test_derivation_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", "false")
        table = _FakeTable([_prev_row(seconds_ago=30, cache_write=5000)])
        result = md._derive_cache_observability(
            session_id="sess-1",
            table=table,
            timestamp=NOW.isoformat(),
            message_metadata=_metadata(cache_read=5000, cache_write=100),
        )
        assert result == {}

    def test_emf_disabled_emits_nothing(self, monkeypatch, capsys):
        monkeypatch.setenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", "false")
        md._emit_cache_metrics(
            session_id="sess-1",
            message_metadata=_metadata(cache_read=1000, cache_write=200),
            cache_observability={"cacheStatus": "miss_avoidable", "wastedUsd": 0.01},
        )
        assert capsys.readouterr().out == ""
