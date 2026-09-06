"""Unit tests for provider-aware token-usage normalization.

The invariant under test is the one ``CostCalculator`` and the context-size
sum in the stream coordinator both depend on: ``inputTokens``,
``cacheReadInputTokens`` and ``cacheWriteInputTokens`` are **disjoint**, and
their sum is the call's total input.

Bedrock Converse already satisfies it. The OpenAI family does not — per AWS's
GPT-5.6 prompt-caching guidance ``input_tokens = cached_tokens +
cache_write_tokens + non-cached remainder`` — so its usage is rewritten at the
model seam.

The SDK-shape tests below deliberately drive the *real* ``strands`` model
classes with *real* ``openai`` usage objects rather than hand-rolled stubs. A
stub that merely matches the broken behavior would hide the bug, and the whole
mapping hangs off two SDK details (the chunk-formatter method name, and the
fact that Strands drops ``cache_write_tokens``) that a version bump can move.
"""

import pytest

from apis.shared.models.usage_normalization import (
    UsageProvider,
    normalize_usage,
    openai_cache_write_tokens,
    usage_normalized,
)


# Wire-shaped usage payloads, as the provider returns them.
#
# GPT-5.6 Responses: a 30k stable prefix served from cache, a 400-token
# increment written to cache, ~100 tokens of genuinely new input.
OPENAI_RESPONSES_WIRE_USAGE = {
    "input_tokens": 30_500,
    "input_tokens_details": {"cached_tokens": 30_000, "cache_write_tokens": 400},
    "output_tokens": 120,
    "output_tokens_details": {"reasoning_tokens": 64},
    "total_tokens": 30_620,
}

# GPT-5.4 Chat Completions: implicit caching only, no write bucket exists.
OPENAI_CHAT_WIRE_USAGE = {
    "prompt_tokens": 5_000,
    "completion_tokens": 50,
    "total_tokens": 5_050,
    "prompt_tokens_details": {"cached_tokens": 4_096},
}


def _assert_disjoint(usage: dict) -> None:
    """Assert the three input buckets partition the call's total input."""
    total_input = (
        (usage.get("inputTokens") or 0)
        + (usage.get("cacheReadInputTokens") or 0)
        + (usage.get("cacheWriteInputTokens") or 0)
    )
    assert total_input == (usage["totalTokens"] - usage["outputTokens"]), (
        "input buckets must partition total input; overlapping buckets are "
        "double-billed by CostCalculator"
    )


class TestNormalizeUsageDisjointInvariant:
    """The per-provider contract: what gets rewritten and what must not."""

    def test_bedrock_usage_is_already_disjoint_and_untouched(self):
        # Converse reports the three buckets pre-partitioned.
        usage = {
            "inputTokens": 100,
            "outputTokens": 120,
            "totalTokens": 30_620,
            "cacheReadInputTokens": 30_000,
            "cacheWriteInputTokens": 400,
        }
        _assert_disjoint(usage)

        normalized = normalize_usage(usage, UsageProvider.BEDROCK)

        assert normalized == usage
        _assert_disjoint(normalized)

    def test_openai_usage_is_normalized_to_disjoint(self):
        # What Strands hands us for GPT-5.6 once cache_write_tokens is mapped:
        # inputTokens is the *inclusive* total.
        usage = {
            "inputTokens": 30_500,
            "outputTokens": 120,
            "totalTokens": 30_620,
            "cacheReadInputTokens": 30_000,
            "cacheWriteInputTokens": 400,
        }
        with pytest.raises(AssertionError):
            _assert_disjoint(usage)

        normalized = normalize_usage(usage, UsageProvider.OPENAI)

        assert normalized["inputTokens"] == 100
        assert normalized["cacheReadInputTokens"] == 30_000
        assert normalized["cacheWriteInputTokens"] == 400
        _assert_disjoint(normalized)

    def test_openai_read_only_call_subtracts_only_the_read_bucket(self):
        usage = {
            "inputTokens": 5_000,
            "outputTokens": 50,
            "totalTokens": 5_050,
            "cacheReadInputTokens": 4_096,
        }
        normalized = normalize_usage(usage, UsageProvider.OPENAI)

        assert normalized["inputTokens"] == 904
        _assert_disjoint(normalized)

    def test_openai_uncached_call_is_unchanged(self):
        usage = {"inputTokens": 1_000, "outputTokens": 50, "totalTokens": 1_050}

        assert normalize_usage(usage, UsageProvider.OPENAI) == usage

    def test_negative_result_is_clamped_at_zero(self):
        # Guards against the upstream reporting bug where the cache buckets
        # summed to more than the inclusive total: a negative bucket would
        # credit dollars back against the bill.
        usage = {
            "inputTokens": 4_583,
            "outputTokens": 10,
            "totalTokens": 4_593,
            "cacheReadInputTokens": 3_945,
            "cacheWriteInputTokens": 4_580,
        }
        assert normalize_usage(usage, UsageProvider.OPENAI)["inputTokens"] == 0

    def test_none_valued_buckets_are_treated_as_zero(self):
        usage = {
            "inputTokens": None,
            "outputTokens": 10,
            "totalTokens": 10,
            "cacheReadInputTokens": None,
            "cacheWriteInputTokens": None,
        }
        assert normalize_usage(usage, UsageProvider.OPENAI) == usage

    def test_returns_a_copy_and_never_mutates_the_input(self):
        usage = {
            "inputTokens": 30_500,
            "outputTokens": 120,
            "totalTokens": 30_620,
            "cacheReadInputTokens": 30_000,
        }
        normalized = normalize_usage(usage, UsageProvider.OPENAI)

        assert normalized is not usage
        assert usage["inputTokens"] == 30_500


class TestOpenAICacheWriteExtraction:
    """`cache_write_tokens` recovery, against the real openai usage models."""

    def test_reads_from_input_tokens_details(self):
        from openai.types.responses.response_usage import ResponseUsage

        usage_obj = ResponseUsage.model_validate(OPENAI_RESPONSES_WIRE_USAGE)

        assert openai_cache_write_tokens(usage_obj) == 400

    def test_reads_from_top_level_when_a_gateway_hoists_it(self):
        from openai.types.responses.response_usage import ResponseUsage

        payload = dict(OPENAI_RESPONSES_WIRE_USAGE)
        payload["input_tokens_details"] = {"cached_tokens": 30_000}
        payload["cache_write_tokens"] = 400

        assert openai_cache_write_tokens(ResponseUsage.model_validate(payload)) == 400

    def test_absent_field_returns_none(self):
        from openai.types.completion_usage import CompletionUsage

        usage_obj = CompletionUsage.model_validate(OPENAI_CHAT_WIRE_USAGE)

        assert openai_cache_write_tokens(usage_obj) is None

    def test_none_usage_object_returns_none(self):
        assert openai_cache_write_tokens(None) is None

    def test_bool_is_rejected_rather_than_coerced(self):
        class _Usage:
            cache_write_tokens = True

        assert openai_cache_write_tokens(_Usage()) is None


class TestStrandsSdkContract:
    """Pin the two SDK facts the shim is built on.

    If a Strands bump breaks either, these fail loudly here rather than
    silently double-billing every OpenAI-family token in production.
    """

    def test_chunk_formatter_seams_still_exist(self):
        from strands.models import OpenAIResponsesModel
        from strands.models.openai import OpenAIModel

        # The Responses model formats through a private seam, the Chat
        # Completions model through a public one. usage_normalized() picks
        # whichever exists; it raises TypeError if neither does.
        assert hasattr(OpenAIResponsesModel, "_format_chunk")
        assert hasattr(OpenAIModel, "format_chunk")

    def test_sdk_still_reports_inclusive_input_and_drops_cache_writes(self):
        """The bug this module exists to fix, asserted against the real SDK."""
        from openai.types.responses.response_usage import ResponseUsage
        from strands.models import OpenAIResponsesModel

        model = OpenAIResponsesModel(
            bedrock_mantle_config={"region": "us-west-2"},
            model_id="openai.gpt-5.6-sol",
        )
        usage_obj = ResponseUsage.model_validate(OPENAI_RESPONSES_WIRE_USAGE)

        raw = model._format_chunk({"chunk_type": "metadata", "data": usage_obj})
        usage = raw["metadata"]["usage"]

        # inputTokens is the inclusive total, not the uncached remainder...
        assert usage["inputTokens"] == 30_500
        assert usage["cacheReadInputTokens"] == 30_000
        # ...and the write bucket never makes it out of the SDK.
        assert "cacheWriteInputTokens" not in usage

        with pytest.raises(AssertionError):
            _assert_disjoint(usage)

    def test_usage_normalized_raises_when_the_seam_disappears(self):
        class _Seamless:
            pass

        with pytest.raises(TypeError, match="chunk-formatting seam moved"):
            usage_normalized(_Seamless)


class TestUsageNormalizedModelClass:
    """End-to-end through the wrapped model classes."""

    def test_responses_model_emits_disjoint_usage_with_cache_writes(self):
        from openai.types.responses.response_usage import ResponseUsage
        from strands.models import OpenAIResponsesModel

        model = usage_normalized(OpenAIResponsesModel)(
            bedrock_mantle_config={"region": "us-west-2"},
            model_id="openai.gpt-5.6-sol",
        )
        usage_obj = ResponseUsage.model_validate(OPENAI_RESPONSES_WIRE_USAGE)

        chunk = model._format_chunk({"chunk_type": "metadata", "data": usage_obj})
        usage = chunk["metadata"]["usage"]

        assert usage["inputTokens"] == 100
        assert usage["cacheReadInputTokens"] == 30_000
        assert usage["cacheWriteInputTokens"] == 400
        _assert_disjoint(usage)

    def test_chat_completions_model_emits_disjoint_usage(self):
        from openai.types.completion_usage import CompletionUsage
        from strands.models.openai import OpenAIModel

        model = usage_normalized(OpenAIModel)(
            client_args={"api_key": "test-key"}, model_id="openai.gpt-5.4"
        )
        usage_obj = CompletionUsage.model_validate(OPENAI_CHAT_WIRE_USAGE)

        chunk = model.format_chunk({"chunk_type": "metadata", "data": usage_obj})
        usage = chunk["metadata"]["usage"]

        assert usage["inputTokens"] == 904
        assert usage["cacheReadInputTokens"] == 4_096
        # No write bucket exists on Chat Completions — don't invent one.
        assert "cacheWriteInputTokens" not in usage
        _assert_disjoint(usage)

    def test_non_metadata_chunks_pass_through_untouched(self):
        from strands.models import OpenAIResponsesModel

        model = usage_normalized(OpenAIResponsesModel)(
            bedrock_mantle_config={"region": "us-west-2"},
            model_id="openai.gpt-5.6-sol",
        )
        chunk = model._format_chunk(
            {"chunk_type": "content_delta", "data_type": "text", "data": "hi"}
        )

        assert chunk == {"contentBlockDelta": {"delta": {"text": "hi"}}}

    def test_subclass_is_memoized_and_keeps_isinstance(self):
        from strands.models import OpenAIResponsesModel

        first = usage_normalized(OpenAIResponsesModel)
        second = usage_normalized(OpenAIResponsesModel)

        assert first is second
        assert issubclass(first, OpenAIResponsesModel)

    def test_non_class_passes_through(self):
        """`unittest.mock.patch` swaps a class for a non-type; don't crash."""
        from unittest.mock import MagicMock

        mock_cls = MagicMock()

        assert usage_normalized(mock_cls) is mock_cls


class TestBuildMantleModelInstallsNormalization:
    """The shared builder is the seam both `agents/` and `app_api` go through."""

    def test_responses_mode_model_is_normalized(self):
        from strands.models import OpenAIResponsesModel

        from apis.shared.models.mantle import MantleApiMode, build_mantle_model

        model = build_mantle_model(
            model_id="openai.gpt-5.6-sol",
            api_mode=MantleApiMode.RESPONSES,
            region="us-west-2",
        )

        assert isinstance(model, OpenAIResponsesModel)
        assert type(model) is usage_normalized(OpenAIResponsesModel)

    def test_chat_mode_model_is_normalized(self):
        from strands.models.openai import OpenAIModel

        from apis.shared.models.mantle import MantleApiMode, build_mantle_model

        model = build_mantle_model(
            model_id="openai.gpt-oss-120b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
            region="us-west-2",
        )

        assert isinstance(model, OpenAIModel)
        assert type(model) is usage_normalized(OpenAIModel)
