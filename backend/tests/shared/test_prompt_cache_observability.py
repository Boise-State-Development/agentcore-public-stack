"""Prompt-cache observability primitives: classification, waste pricing,
prefix fingerprints, and EMF record shape."""

import io
import json
import logging

from apis.shared.observability import (
    CACHE_TTL_SECONDS,
    CacheStatus,
    classify_cache_status,
    compute_wasted_usd,
    emit_prompt_cache_metrics,
    fingerprint_canonical_json,
    fingerprint_text,
)


PRICING = {
    "inputPricePerMtok": 3.0,
    "outputPricePerMtok": 15.0,
    "cacheWritePricePerMtok": 3.75,
    "cacheReadPricePerMtok": 0.30,
}


class TestClassifyCacheStatus:
    def test_first_write(self):
        assert (
            classify_cache_status(0, 5000, previous_call_exists=False, gap_seconds=None)
            is CacheStatus.FIRST_WRITE
        )

    def test_hit_when_any_cache_read(self):
        assert (
            classify_cache_status(4000, 200, previous_call_exists=True, gap_seconds=10)
            is CacheStatus.HIT
        )

    def test_hit_wins_even_without_previous_row(self):
        # A read implies the cache was warm regardless of what rows we kept.
        assert (
            classify_cache_status(4000, 0, previous_call_exists=False, gap_seconds=None)
            is CacheStatus.HIT
        )

    def test_miss_avoidable_within_ttl(self):
        assert (
            classify_cache_status(0, 5000, previous_call_exists=True, gap_seconds=45)
            is CacheStatus.MISS_AVOIDABLE
        )

    def test_miss_ttl_expired_beyond_ttl(self):
        assert (
            classify_cache_status(
                0, 5000, previous_call_exists=True, gap_seconds=CACHE_TTL_SECONDS + 1
            )
            is CacheStatus.MISS_TTL_EXPIRED
        )

    def test_boundary_gap_exactly_ttl_is_avoidable(self):
        assert (
            classify_cache_status(
                0, 5000, previous_call_exists=True, gap_seconds=CACHE_TTL_SECONDS
            )
            is CacheStatus.MISS_AVOIDABLE
        )

    def test_unknown_gap_is_conservatively_expired(self):
        assert (
            classify_cache_status(0, 5000, previous_call_exists=True, gap_seconds=None)
            is CacheStatus.MISS_TTL_EXPIRED
        )

    def test_uncached_when_no_cache_activity(self):
        assert (
            classify_cache_status(0, 0, previous_call_exists=True, gap_seconds=10)
            is CacheStatus.UNCACHED
        )

    def test_below_threshold_then_crossing_is_first_write(self):
        # Prior calls were below the minimum cacheable prefix (uncached), so
        # no cache entry existed — the first call that crosses the threshold
        # is the expected initial population, not an avoidable miss.
        assert (
            classify_cache_status(
                0,
                4122,
                previous_call_exists=True,
                gap_seconds=45,
                previous_cached_prefix_tokens=0,
            )
            is CacheStatus.FIRST_WRITE
        )

    def test_unknown_previous_prefix_stays_avoidable(self):
        # None means we couldn't see the previous call's cache split — keep
        # the pre-existing (avoidable) classification rather than masking.
        assert (
            classify_cache_status(
                0,
                5000,
                previous_call_exists=True,
                gap_seconds=45,
                previous_cached_prefix_tokens=None,
            )
            is CacheStatus.MISS_AVOIDABLE
        )


class TestComputeWastedUsd:
    def test_avoidable_miss_priced_at_write_read_premium(self):
        # 1M re-written tokens, all previously cached → full premium.
        wasted = compute_wasted_usd(
            CacheStatus.MISS_AVOIDABLE,
            cache_write_tokens=1_000_000,
            previous_cached_prefix_tokens=2_000_000,
            pricing_snapshot=PRICING,
        )
        assert wasted == (3.75 - 0.30)

    def test_rewritten_capped_at_previous_cached_prefix(self):
        # Only 100k of the 1M written were cached before — the new suffix
        # would have been written under a hit too, so it isn't waste.
        wasted = compute_wasted_usd(
            CacheStatus.MISS_AVOIDABLE,
            cache_write_tokens=1_000_000,
            previous_cached_prefix_tokens=100_000,
            pricing_snapshot=PRICING,
        )
        assert wasted == (100_000 / 1_000_000) * (3.75 - 0.30)

    def test_unknown_previous_prefix_uses_full_write(self):
        wasted = compute_wasted_usd(
            CacheStatus.MISS_AVOIDABLE,
            cache_write_tokens=500_000,
            previous_cached_prefix_tokens=None,
            pricing_snapshot=PRICING,
        )
        assert wasted == (500_000 / 1_000_000) * (3.75 - 0.30)

    def test_zero_for_non_avoidable_statuses(self):
        for status in (
            CacheStatus.FIRST_WRITE,
            CacheStatus.HIT,
            CacheStatus.MISS_TTL_EXPIRED,
            CacheStatus.UNCACHED,
        ):
            assert (
                compute_wasted_usd(status, 1_000_000, 1_000_000, PRICING) == 0.0
            )

    def test_zero_without_pricing_or_with_none_prices(self):
        assert compute_wasted_usd(CacheStatus.MISS_AVOIDABLE, 1000, 1000, None) == 0.0
        # Rows can store explicit None prices (managed models).
        assert (
            compute_wasted_usd(
                CacheStatus.MISS_AVOIDABLE,
                1000,
                1000,
                {"cacheWritePricePerMtok": None, "cacheReadPricePerMtok": None},
            )
            == 0.0
        )


class TestFingerprints:
    def test_text_fingerprint_stable_and_short(self):
        assert fingerprint_text("hello") == fingerprint_text("hello")
        assert fingerprint_text("hello") != fingerprint_text("hello!")
        assert len(fingerprint_text("hello")) == 16
        assert fingerprint_text(None) == fingerprint_text("")

    def test_dict_key_order_does_not_matter(self):
        a = {"b": 1, "a": [1, 2]}
        b = {"a": [1, 2], "b": 1}
        assert fingerprint_canonical_json(a) == fingerprint_canonical_json(b)

    def test_list_order_matters(self):
        # Deliberate: tool-spec / message order is what Bedrock's
        # exact-prefix match is sensitive to.
        assert fingerprint_canonical_json([1, 2]) != fingerprint_canonical_json([2, 1])

    def test_non_json_values_do_not_raise(self):
        assert isinstance(fingerprint_canonical_json({"x": object()}), str)


class TestEmfEmission:
    def _capture(self, **kwargs):
        from apis.shared.observability import emf

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        emf._emf_logger.addHandler(handler)
        try:
            emit_prompt_cache_metrics(**kwargs)
        finally:
            emf._emf_logger.removeHandler(handler)
        return stream.getvalue().strip()

    def test_record_is_raw_json_with_emf_directive(self):
        line = self._capture(
            cache_read_tokens=1000,
            cache_write_tokens=200,
            avoidable_miss=True,
            wasted_usd=0.0123456789,
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            session_id="sess-1",
            cache_status="miss_avoidable",
        )
        record = json.loads(line)  # the whole line must be the JSON object
        directive = record["_aws"]["CloudWatchMetrics"][0]
        metric_names = {m["Name"] for m in directive["Metrics"]}
        assert metric_names == {
            "CacheReadTokens",
            "CacheWriteTokens",
            "AvoidableMiss",
            "AgentSwitchMiss",
            "WastedUsd",
        }
        assert record["CacheReadTokens"] == 1000
        assert record["CacheWriteTokens"] == 200
        assert record["AvoidableMiss"] == 1
        assert record["WastedUsd"] == 0.012346  # rounded to 6 places
        assert record["modelId"].startswith("us.anthropic")
        assert record["cacheStatus"] == "miss_avoidable"

    def test_an_unexplained_avoidable_miss_emits_no_agent_switch(self):
        """The default. `AvoidableMiss` minus `AgentSwitchMiss` is unexplained waste."""
        record = json.loads(
            self._capture(
                cache_read_tokens=0, cache_write_tokens=5000, avoidable_miss=True
            )
        )
        assert record["AvoidableMiss"] == 1
        assert record["AgentSwitchMiss"] == 0

    def test_an_agent_switch_miss_is_counted_in_both(self):
        """A subset, not a reclassification (#756).

        The turn really did pay for a prefix re-write, so it stays in `AvoidableMiss`
        and `WastedUsd`; `AgentSwitchMiss` is what lets a dashboard subtract the part an
        `@`-mention explains rather than pretend it did not happen.
        """
        record = json.loads(
            self._capture(
                cache_read_tokens=0,
                cache_write_tokens=5000,
                avoidable_miss=True,
                wasted_usd=0.01,
                agent_switched=True,
            )
        )
        assert record["AvoidableMiss"] == 1
        assert record["AgentSwitchMiss"] == 1
        assert record["WastedUsd"] == 0.01

    def test_an_agent_switch_without_a_miss_counts_nothing(self):
        """A switch that still hit the cache is not waste and must not read as any."""
        record = json.loads(
            self._capture(
                cache_read_tokens=5000,
                cache_write_tokens=0,
                avoidable_miss=False,
                agent_switched=True,
            )
        )
        assert record["AvoidableMiss"] == 0
        assert record["AgentSwitchMiss"] == 0

    def test_no_miss_and_defaults(self):
        line = self._capture(
            cache_read_tokens=0, cache_write_tokens=0, avoidable_miss=False
        )
        record = json.loads(line)
        assert record["AvoidableMiss"] == 0
        assert record["WastedUsd"] == 0.0
        assert "modelId" not in record
