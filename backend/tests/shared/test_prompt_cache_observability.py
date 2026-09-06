"""Prompt-cache observability primitives: classification, waste pricing,
prefix fingerprints, and EMF record shape."""

import io
import json
import logging

from apis.shared.observability import (
    CACHE_TTL_SECONDS,
    DEFAULT_CACHE_TTL_SECONDS,
    OPENAI_RESPONSES_CACHE_TTL_SECONDS,
    PARTIAL_MISS_WRITE_READ_RATIO,
    CacheStatus,
    cache_ttl_seconds_for,
    classify_cache_status,
    compute_wasted_usd,
    emit_prompt_cache_metrics,
    emit_session_cache_rollup,
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


class TestPartialMiss:
    """A nonzero read is not proof the prefix was cached.

    The 2026-08-05 compaction spiral read an 11k tools+system segment and
    re-wrote 190k of history on every turn — 56 calls, all classified `hit`,
    all priced at $0 wasted. `partial_miss` is the status that separates
    "read the prefix, wrote the tail" from "read a sliver, wrote the prefix".
    """

    def test_a_sliver_read_against_a_prefix_rewrite_is_a_partial_miss(self):
        assert (
            classify_cache_status(
                11_278, 190_000, previous_call_exists=True, gap_seconds=60
            )
            is CacheStatus.PARTIAL_MISS
        )

    def test_an_ordinary_turn_appending_a_tail_stays_a_hit(self):
        # The healthy shape: read the whole prefix, write only what was added.
        assert (
            classify_cache_status(
                190_000, 4_000, previous_call_exists=True, gap_seconds=60
            )
            is CacheStatus.HIT
        )

    def test_the_ratio_boundary_is_exclusive(self):
        read = 10_000
        at_ratio = read * PARTIAL_MISS_WRITE_READ_RATIO
        assert (
            classify_cache_status(read, at_ratio, previous_call_exists=True, gap_seconds=60)
            is CacheStatus.HIT
        )
        assert (
            classify_cache_status(read, at_ratio + 1, previous_call_exists=True, gap_seconds=60)
            is CacheStatus.PARTIAL_MISS
        )

    def test_a_cold_prefix_past_the_ttl_is_not_waste(self):
        # Past the TTL the entry is gone, so re-writing it was unavoidable —
        # same standard `miss_avoidable` is held to.
        assert (
            classify_cache_status(
                11_278,
                190_000,
                previous_call_exists=True,
                gap_seconds=CACHE_TTL_SECONDS + 1,
            )
            is CacheStatus.HIT
        )

    def test_an_unknown_gap_stays_a_hit(self):
        # No same-prefix predecessor in the lookback window: under-report
        # rather than cry wolf (#753).
        assert (
            classify_cache_status(
                11_278, 190_000, previous_call_exists=True, gap_seconds=None
            )
            is CacheStatus.HIT
        )

    def test_the_first_call_of_a_session_is_never_a_partial_miss(self):
        assert (
            classify_cache_status(
                11_278, 190_000, previous_call_exists=False, gap_seconds=None
            )
            is CacheStatus.HIT
        )

    def test_a_zero_read_is_a_full_miss_not_a_partial_one(self):
        # write > 3 × 0 is trivially true; the read is what makes it partial.
        assert (
            classify_cache_status(0, 190_000, previous_call_exists=True, gap_seconds=60)
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

    def test_a_partial_miss_is_priced_like_a_full_one(self):
        # Same dollars: both re-wrote prefix bytes a live entry already held.
        assert compute_wasted_usd(
            CacheStatus.PARTIAL_MISS,
            cache_write_tokens=1_000_000,
            previous_cached_prefix_tokens=3_000_000,
            pricing_snapshot=PRICING,
        ) == (3.75 - 0.30)

    def test_tokens_read_this_call_are_not_also_counted_as_re_written(self):
        # The previous call cached 200k; this call read 10k of it and re-wrote
        # the other 190k. The 10k it read cannot also be waste.
        wasted = compute_wasted_usd(
            CacheStatus.PARTIAL_MISS,
            cache_write_tokens=1_000_000,
            previous_cached_prefix_tokens=200_000,
            pricing_snapshot=PRICING,
            cache_read_tokens=10_000,
        )
        assert wasted == (190_000 / 1_000_000) * (3.75 - 0.30)

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
            "PartialMiss",
            "AgentSwitchMiss",
            "WastedUsd",
            "PartialMissUsd",
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
        assert record["PartialMiss"] == 0
        assert record["WastedUsd"] == 0.0
        assert record["PartialMissUsd"] == 0.0
        assert "modelId" not in record

    def test_a_partial_miss_counts_separately_and_splits_the_dollars(self):
        """Its own metric, not a roll-in to `AvoidableMiss` — the existing alarm keeps
        its meaning, and `PartialMissUsd` names the share of `WastedUsd` it caused."""
        record = json.loads(
            self._capture(
                cache_read_tokens=11_278,
                cache_write_tokens=190_000,
                avoidable_miss=False,
                partial_miss=True,
                wasted_usd=0.437,
                cache_status="partial_miss",
            )
        )
        assert record["PartialMiss"] == 1
        assert record["AvoidableMiss"] == 0
        assert record["WastedUsd"] == 0.437
        assert record["PartialMissUsd"] == 0.437

    def test_a_full_miss_contributes_no_partial_miss_dollars(self):
        record = json.loads(
            self._capture(
                cache_read_tokens=0,
                cache_write_tokens=5000,
                avoidable_miss=True,
                wasted_usd=0.02,
            )
        )
        assert record["WastedUsd"] == 0.02
        assert record["PartialMissUsd"] == 0.0


class TestSessionRollupEmission:
    """`SessionPartialMissUsd` — the per-session accumulation the $5/24h alarm reads.

    A fleet-wide sum cannot see one conversation spending $0.43 a turn for five
    days, which is exactly how the motivating incident stayed invisible.
    """

    def _capture(self, **kwargs):
        from apis.shared.observability import emf

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        emf._emf_logger.addHandler(handler)
        try:
            emit_session_cache_rollup(**kwargs)
        finally:
            emf._emf_logger.removeHandler(handler)
        return stream.getvalue().strip()

    def test_carries_the_running_total_with_the_session_as_a_property(self):
        record = json.loads(
            self._capture(session_id="sess-1", partial_miss_usd=6.25, partial_miss_count=14)
        )
        directive = record["_aws"]["CloudWatchMetrics"][0]
        assert [m["Name"] for m in directive["Metrics"]] == ["SessionPartialMissUsd"]
        assert record["SessionPartialMissUsd"] == 6.25
        # sessionId stays a log property: as a dimension its cardinality is
        # unbounded. The alarm says a session crossed; Logs Insights says which.
        assert directive["Dimensions"] == [[]]
        assert record["sessionId"] == "sess-1"
        assert record["sessionPartialMissCount"] == 14


class TestIncidentReplay:
    """Acceptance for #833 PR-1, replayed against the shape of the session that
    motivated it (prod `c94a3172…`, 2026-08-05).

    56 model calls over 5 days, $30.45, of which $27.39 was cache writes: every
    turn read the same ~11,278-token tools+system segment and re-wrote the full
    ~190k-token history. Eight of the calls followed a real >1h gap, where the
    entry had legitimately expired and the re-write was unavoidable; the other
    48 were 60–120s apart with identical toolConfig and systemPrompt hashes.

    The classifier called all 56 of them `hit` with `wastedUsd = 0`.
    """

    # Pricing implied by the incident's own numbers: 10.95M write tokens billed
    # at $27.39 → $2.50/MTok write, against a $0.20/MTok read.
    INCIDENT_PRICING = {
        "cacheWritePricePerMtok": 2.50,
        "cacheReadPricePerMtok": 0.20,
    }
    CACHE_READ = 11_278
    CACHE_WRITE = 190_000
    # Every call re-writes the same prefix, so the previous call's cached
    # total is what this one both read a sliver of and re-wrote the rest of.
    PREVIOUS_PREFIX = CACHE_READ + CACHE_WRITE

    def _replay(self):
        """(status, wastedUsd) per call: 48 intra-burst turns, 8 after a >1h gap."""
        gaps = [90.0] * 48 + [3600.0] * 8
        out = []
        for gap in gaps:
            status = classify_cache_status(
                self.CACHE_READ,
                self.CACHE_WRITE,
                previous_call_exists=True,
                gap_seconds=gap,
                previous_cached_prefix_tokens=self.PREVIOUS_PREFIX,
            )
            out.append((
                status,
                compute_wasted_usd(
                    status,
                    cache_write_tokens=self.CACHE_WRITE,
                    previous_cached_prefix_tokens=self.PREVIOUS_PREFIX,
                    pricing_snapshot=self.INCIDENT_PRICING,
                    cache_read_tokens=self.CACHE_READ,
                ),
            ))
        return out

    def test_at_least_47_of_the_56_calls_classify_as_partial_miss(self):
        statuses = [status for status, _ in self._replay()]
        assert sum(s is CacheStatus.PARTIAL_MISS for s in statuses) >= 47
        # The cold ones are not waste and must not be counted as such.
        assert sum(s is CacheStatus.PARTIAL_MISS for s in statuses) == 48

    def test_the_session_prices_out_at_about_twenty_dollars_of_waste(self):
        total = sum(wasted for _, wasted in self._replay())
        # §1's counterfactual: ~$20 of the month's $30.45 was avoidable.
        assert 17.0 <= total <= 23.0

    def test_the_old_classifier_would_have_called_every_one_of_them_a_hit(self):
        """The regression guard. This is what `cacheStatus` reported for 5 days."""
        for status, wasted in self._replay():
            if status is CacheStatus.HIT:
                assert wasted == 0.0
        assert any(status is CacheStatus.PARTIAL_MISS for status, _ in self._replay())


class TestCacheTtlResolution:
    """The TTL is a property of the model, not of the module.

    Bedrock/Anthropic prompt caching is a ~5-minute sliding window; the OpenAI
    Responses API on bedrock-runtime holds entries for 30 minutes. A single
    hardcoded 300s was wrong by 6x for GPT-5.6 — and wrong in the direction
    that hides waste.
    """

    def test_bedrock_responses_gets_the_thirty_minute_window(self):
        assert (
            cache_ttl_seconds_for(provider="bedrock-responses")
            == OPENAI_RESPONSES_CACHE_TTL_SECONDS
            == 1800
        )

    def test_provider_match_is_case_insensitive(self):
        assert cache_ttl_seconds_for(provider="Bedrock-Responses") == 1800

    def test_bedrock_keeps_the_five_minute_window(self):
        assert cache_ttl_seconds_for(provider="bedrock") == DEFAULT_CACHE_TTL_SECONDS == 300

    def test_mantle_is_deliberately_not_widened(self):
        """openai.gpt-5.4 on Mantle is implicit-only; AWS documents no 30m TTL.

        Guessing here would over-report waste — the opposite error, but the
        same class of mistake.
        """
        assert cache_ttl_seconds_for(provider="mantle") == DEFAULT_CACHE_TTL_SECONDS

    def test_unknown_provider_falls_back_to_the_default(self):
        assert cache_ttl_seconds_for(provider="something-new") == DEFAULT_CACHE_TTL_SECONDS
        assert cache_ttl_seconds_for() == DEFAULT_CACHE_TTL_SECONDS

    def test_model_id_fallback_for_rows_written_before_provider_existed(self):
        assert cache_ttl_seconds_for(model_id="us.openai.gpt-5.6-sol") == 1800
        assert cache_ttl_seconds_for(model_id="global.openai.gpt-5.6-luna") == 1800
        assert (
            cache_ttl_seconds_for(model_id="us.anthropic.claude-haiku-4-5")
            == DEFAULT_CACHE_TTL_SECONDS
        )

    def test_provider_wins_over_the_model_id_fallback(self):
        # An explicit provider is authoritative; the id sniff is only for rows
        # that predate the field.
        assert (
            cache_ttl_seconds_for(provider="bedrock", model_id="us.openai.gpt-5.6-sol")
            == DEFAULT_CACHE_TTL_SECONDS
        )


class TestTtlAwareClassification:
    """The dollars-visible consequence of the TTL being right."""

    GAP = 900  # 15 min: past the Bedrock window, inside the OpenAI one.

    def test_gap_inside_the_openai_ttl_is_avoidable_waste(self):
        assert (
            classify_cache_status(
                0,
                30_000,
                previous_call_exists=True,
                gap_seconds=self.GAP,
                previous_cached_prefix_tokens=30_000,
                ttl_seconds=OPENAI_RESPONSES_CACHE_TTL_SECONDS,
            )
            is CacheStatus.MISS_AVOIDABLE
        )

    def test_the_same_gap_under_the_old_constant_looked_unavoidable(self):
        """This is the bug: a live entry reported as a legitimate expiry."""
        assert (
            classify_cache_status(
                0,
                30_000,
                previous_call_exists=True,
                gap_seconds=self.GAP,
                previous_cached_prefix_tokens=30_000,
                ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            )
            is CacheStatus.MISS_TTL_EXPIRED
        )

    def test_partial_miss_survives_a_gap_inside_the_openai_ttl(self):
        assert (
            classify_cache_status(
                11_000,
                190_000,
                previous_call_exists=True,
                gap_seconds=self.GAP,
                ttl_seconds=OPENAI_RESPONSES_CACHE_TTL_SECONDS,
            )
            is CacheStatus.PARTIAL_MISS
        )

    def test_partial_miss_degrades_to_hit_under_the_wrong_ttl(self):
        """The compaction-spiral shape, mislabelled — wastedUsd goes to $0."""
        assert (
            classify_cache_status(
                11_000,
                190_000,
                previous_call_exists=True,
                gap_seconds=self.GAP,
                ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            )
            is CacheStatus.HIT
        )

    def test_beyond_the_openai_ttl_is_still_a_real_expiry(self):
        assert (
            classify_cache_status(
                0,
                30_000,
                previous_call_exists=True,
                gap_seconds=OPENAI_RESPONSES_CACHE_TTL_SECONDS + 1,
                previous_cached_prefix_tokens=30_000,
                ttl_seconds=OPENAI_RESPONSES_CACHE_TTL_SECONDS,
            )
            is CacheStatus.MISS_TTL_EXPIRED
        )

    def test_default_is_unchanged_for_every_existing_caller(self):
        """Omitting ttl_seconds must behave exactly as before this change."""
        for gap, expected in (
            (CACHE_TTL_SECONDS, CacheStatus.MISS_AVOIDABLE),
            (CACHE_TTL_SECONDS + 1, CacheStatus.MISS_TTL_EXPIRED),
        ):
            assert (
                classify_cache_status(
                    0,
                    5_000,
                    previous_call_exists=True,
                    gap_seconds=gap,
                    previous_cached_prefix_tokens=5_000,
                )
                is expected
            )
