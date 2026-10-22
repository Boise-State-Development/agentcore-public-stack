"""What the cost row says about the model call a Stop cut off.

The reproduction (dev session e31c51ad, 2026-10-20): a long answer stopped at
~9s after ~200 words had rendered. The row read 39,992 input tokens, **zero**
output, no cache buckets and ``cacheStatus: "uncached"``, priced at the plain
uncached input rate — while the next call in that same conversation reported
``cacheReadInputTokens: 39,972``, proving a cache *write* had happened on the
cut call and been billed at 1.25x base. About $0.013 of a real $0.055 went
unrecorded, and the same figure feeds the user's quota.

These cover the three parts separately: the pure bucket attribution, the
output count, and what the whole thing reports with and without a provider
usage block.
"""

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from agents.main_agent.streaming import interrupted_usage as iu


class TestAttributeInputTokens:
    """With cachePoints on the request, plain `inputTokens` is the one bucket
    that cannot be right: Bedrock bills an uncached prefix as a write and a
    cached one as a read, never as plain input."""

    def test_no_cache_points_keeps_plain_input(self):
        buckets, method = iu.attribute_input_tokens(
            40_000, cache_points_sent=False, prior_cached_prefix=None
        )
        assert buckets == {
            "inputTokens": 40_000,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
        assert method == iu.METHOD_UNCACHED

    def test_a_prompt_below_the_minimum_cacheable_prefix_is_plain_input(self):
        """A cachePoint on a short prompt is ignored and the prompt is billed
        as plain input, so the cache buckets must stay empty."""
        buckets, method = iu.attribute_input_tokens(
            iu.MIN_CACHEABLE_PREFIX_TOKENS - 1,
            cache_points_sent=True,
            prior_cached_prefix=None,
        )
        assert buckets["inputTokens"] == iu.MIN_CACHEABLE_PREFIX_TOKENS - 1
        assert buckets["cacheWriteInputTokens"] == 0
        assert method == iu.METHOD_UNCACHED

    def test_the_reproduced_case_is_a_cache_write(self):
        """No call in this process reported usage — a first turn — so the
        prefix was not in cache and the request wrote it."""
        buckets, method = iu.attribute_input_tokens(
            39_992, cache_points_sent=True, prior_cached_prefix=None
        )
        assert buckets == {
            "inputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 39_992,
        }
        assert method == iu.METHOD_FIRST_WRITE

    def test_a_prior_call_splits_the_prompt_into_read_plus_appended_tail(self):
        buckets, method = iu.attribute_input_tokens(
            40_500, cache_points_sent=True, prior_cached_prefix=39_972
        )
        assert buckets == {
            "inputTokens": 0,
            "cacheReadInputTokens": 39_972,
            "cacheWriteInputTokens": 528,
        }
        assert method == iu.METHOD_PRIOR_CALL

    def test_a_prior_prefix_larger_than_the_prompt_is_clamped(self):
        """Never a negative bucket, which the calculator would credit against
        the bill."""
        buckets, _ = iu.attribute_input_tokens(
            5_000, cache_points_sent=True, prior_cached_prefix=9_000
        )
        assert buckets["cacheReadInputTokens"] == 5_000
        assert buckets["cacheWriteInputTokens"] == 0


class TestLastReportedCachedPrefix:
    def _assistant(self, **usage: int) -> dict:
        return {"role": "assistant", "content": [], "metadata": {"usage": usage}}

    def test_none_without_any_reported_usage(self):
        messages = [{"role": "user", "content": []}, {"role": "assistant", "content": []}]
        assert iu.last_reported_cached_prefix(messages) is None

    def test_reads_the_newest_reporting_assistant_message(self):
        messages = [
            self._assistant(cacheReadInputTokens=0, cacheWriteInputTokens=1_000),
            {"role": "user", "content": []},
            self._assistant(cacheReadInputTokens=1_000, cacheWriteInputTokens=400),
            {"role": "user", "content": []},
        ]
        # The prefix Bedrock held once that call answered: read + write.
        assert iu.last_reported_cached_prefix(messages) == 1_400

    def test_a_call_with_no_cache_activity_reports_no_prefix(self):
        messages = [self._assistant(inputTokens=800, outputTokens=20)]
        assert iu.last_reported_cached_prefix(messages) is None

    def test_a_non_list_is_tolerated(self):
        assert iu.last_reported_cached_prefix(None) is None


class _CountingModel:
    """Stands in for ``CountTokensBedrockModel``: an authoritative counter
    whose answers are scripted."""

    def __init__(self, answers: List[Optional[int]]) -> None:
        self.config = {"model_id": "anthropic.fake-v1", "use_native_token_count": True}
        self._answers = list(answers)
        self.calls: List[Any] = []

    def native_count_tokens(self, messages, tool_specs=None, system_prompt_content=None):
        self.calls.append(messages)
        return self._answers.pop(0) if self._answers else None


@pytest.fixture(autouse=True)
def _no_probe_memo():
    """The probe weight is memoised process-wide per model id; these tests
    script different answers for the same id."""
    from agents.main_agent.session.hooks import context_attribution

    context_attribution.clear_probe_baselines()
    yield
    context_attribution.clear_probe_baselines()


class TestCountOutputTokens:
    @pytest.mark.asyncio
    async def test_a_native_count_subtracts_the_message_framing(self, monkeypatch):
        from agents.main_agent.session.hooks import context_attribution

        monkeypatch.setattr(
            context_attribution, "_token_count_is_authoritative", lambda model: True
        )
        # The framed partial, then the probe's own weight.
        model = _CountingModel([312, 12])

        tokens, method = await iu._count_output_tokens(
            SimpleNamespace(model=model), "a stopped answer"
        )

        assert (tokens, method) == (300, iu.METHOD_NATIVE_COUNT)

    @pytest.mark.asyncio
    async def test_a_count_that_cannot_be_had_falls_back_to_the_estimate(self, monkeypatch):
        from agents.main_agent.session.hooks import context_attribution

        monkeypatch.setattr(
            context_attribution, "_token_count_is_authoritative", lambda model: True
        )
        model = _CountingModel([None])

        text = "x" * 400
        tokens, method = await iu._count_output_tokens(SimpleNamespace(model=model), text)

        assert (tokens, method) == (100, iu.METHOD_ESTIMATE)

    @pytest.mark.asyncio
    async def test_no_partial_means_no_output(self):
        assert await iu._count_output_tokens(SimpleNamespace(model=None), "") == (
            0,
            iu.METHOD_STREAMED_TEXT,
        )


class TestBuildInterruptedCallUsage:
    def _agent(self, messages=None):
        return SimpleNamespace(model=None, messages=messages or [])

    def _wrapper(self, caching: bool):
        return SimpleNamespace(
            model_config=SimpleNamespace(
                bedrock_cache_points_supported=lambda: caching,
                caching_enabled=caching,
            )
        )

    @pytest.mark.asyncio
    async def test_the_reproduced_row_after_the_fix(self, monkeypatch):
        monkeypatch.setattr(
            "agents.main_agent.session.hooks.context_attribution.get_projected_input_tokens",
            lambda agent: 39_992,
        )

        result = await iu.build_interrupted_call_usage(
            strands_agent=self._agent(),
            wrapper=self._wrapper(caching=True),
            partial_text="x" * 800,
        )

        # The prompt sits in the bucket Bedrock billed it from, and the
        # answer the user watched render is no longer zero.
        assert result["usage"] == {
            "inputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 39_992,
            "outputTokens": 200,
            "totalTokens": 40_192,
        }
        # Every figure names what produced it; nothing here is presented as
        # a measurement.
        assert result["usageEstimate"] == {
            "reason": "interrupted",
            "inputTokens": iu.METHOD_PROJECTION,
            "outputTokens": iu.METHOD_ESTIMATE,
            "cacheSplit": iu.METHOD_FIRST_WRITE,
        }

    @pytest.mark.asyncio
    async def test_a_mid_turn_stop_reads_the_prefix_its_predecessor_left(self, monkeypatch):
        monkeypatch.setattr(
            "agents.main_agent.session.hooks.context_attribution.get_projected_input_tokens",
            lambda agent: 40_500,
        )
        messages = [{
            "role": "assistant",
            "content": [],
            "metadata": {"usage": {"cacheReadInputTokens": 39_972, "cacheWriteInputTokens": 28}},
        }]

        result = await iu.build_interrupted_call_usage(
            strands_agent=self._agent(messages),
            wrapper=self._wrapper(caching=True),
            partial_text="",
        )

        assert result["usage"]["cacheReadInputTokens"] == 40_000
        assert result["usage"]["cacheWriteInputTokens"] == 500
        assert result["usage"]["outputTokens"] == 0
        assert result["usageEstimate"]["cacheSplit"] == iu.METHOD_PRIOR_CALL

    @pytest.mark.asyncio
    async def test_no_projection_means_no_row(self, monkeypatch):
        """Nothing establishes the prompt size, so there is no honest row to
        write — the pre-existing behaviour, kept."""
        monkeypatch.setattr(
            "agents.main_agent.session.hooks.context_attribution.get_projected_input_tokens",
            lambda agent: None,
        )

        assert await iu.build_interrupted_call_usage(
            strands_agent=self._agent(),
            wrapper=self._wrapper(caching=True),
            partial_text="some words",
        ) is None

    @pytest.mark.asyncio
    async def test_a_wrapper_with_no_model_config_claims_no_cache_activity(self, monkeypatch):
        monkeypatch.setattr(
            "agents.main_agent.session.hooks.context_attribution.get_projected_input_tokens",
            lambda agent: 5_000,
        )

        result = await iu.build_interrupted_call_usage(
            strands_agent=self._agent(),
            wrapper=SimpleNamespace(),
            partial_text="",
        )

        assert result["usage"]["inputTokens"] == 5_000
        assert result["usageEstimate"]["cacheSplit"] == iu.METHOD_UNCACHED
