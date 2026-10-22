"""The arithmetic invariant behind `prefixTokens`: the static prefix is a
subset of the prompt, so `system + tools` can never exceed what the provider
billed. Both sides of the import boundary apply the same rule — `agents/`
before persisting a row, `app_api/` when reading rows written before it.
"""

import pytest

from apis.shared.observability.prefix_tokens import (
    prefix_split_is_plausible,
    prompt_tokens_from_usage,
)


class TestPromptTokensFromUsage:
    def test_sums_the_three_provider_reported_components(self):
        usage = {
            "inputTokens": 2,
            "outputTokens": 7_695,          # output is NOT part of the prompt
            "cacheReadInputTokens": 55_732,
            "cacheWriteInputTokens": 49,
        }
        assert prompt_tokens_from_usage(usage) == 55_783

    def test_missing_components_count_as_zero(self):
        assert prompt_tokens_from_usage({"inputTokens": 41_656}) == 41_656

    @pytest.mark.parametrize("usage", [None, {}, {"inputTokens": 0}, "not-a-mapping"])
    def test_unknown_total_reads_none_not_zero(self, usage):
        """A zero sum means "no usage reported", not "empty prompt" — and an
        unknown total must never be used to reject a split."""
        assert prompt_tokens_from_usage(usage) is None


class TestPrefixSplitIsPlausible:
    def test_a_split_that_fits_inside_the_prompt_is_kept(self):
        assert prefix_split_is_plausible(2_455, 12_744, 226_370) is True

    def test_a_split_exactly_filling_the_prompt_is_kept(self):
        """A first turn is almost all static prefix; equality is legitimate."""
        assert prefix_split_is_plausible(2_455, 10_289, 12_744) is True

    def test_a_split_larger_than_the_prompt_is_rejected(self):
        """Prod session 7f5f207f, 2026-09-21: tools=223,782 reported against a
        55,783-token prompt — the tool schemas alone claiming 4x the prompt
        they sit inside."""
        assert prefix_split_is_plausible(2_455, 223_782, 55_783) is False

    def test_an_unknown_prompt_total_proves_nothing_so_the_split_is_kept(self):
        assert prefix_split_is_plausible(2_455, 223_782, None) is True

    @pytest.mark.parametrize("system,tools", [(-1, 10), (10, -1)])
    def test_negative_partitions_are_rejected_outright(self, system, tools):
        """The hook clamps at 0, so a negative arrived from somewhere else."""
        assert prefix_split_is_plausible(system, tools, 100_000) is False

    def test_a_merely_suspicious_residual_still_passes(self):
        """Deliberately only the provably-impossible case is caught. The same
        user's 13,606 -> 4,624 swing on a byte-identical system prompt is
        wrong, but arithmetic cannot tell it from a real tool-set change —
        narrowing that needs the root cause, not a tighter bound here."""
        assert prefix_split_is_plausible(1_757, 13_606, 100_000) is True
        assert prefix_split_is_plausible(1_757, 4_624, 100_000) is True
