"""Tests for CountTokensBedrockModel and base_foundation_model_id.

The subclass exists because Bedrock's CountTokens API rejects cross-region
inference-profile model ids (``us.anthropic.…``) but on-demand invocation
requires them. The subclass de-prefixes for the count call only.
"""

from unittest.mock import patch

import pytest
from strands.models import BedrockModel

from agents.main_agent.core.bedrock_count_tokens import (
    CountTokensBedrockModel,
    base_foundation_model_id,
)


class TestBaseFoundationModelId:
    """The pure de-prefix helper."""

    @pytest.mark.parametrize(
        "profile_id,expected",
        [
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
            ("eu.anthropic.claude-x", "anthropic.claude-x"),
            ("apac.anthropic.claude-x", "anthropic.claude-x"),
            ("us-gov.anthropic.claude-x", "anthropic.claude-x"),
        ],
    )
    def test_strips_known_geography_prefixes(self, profile_id, expected):
        assert base_foundation_model_id(profile_id) == expected

    @pytest.mark.parametrize(
        "base_id",
        [
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
            "amazon.nova-2-sonic-v1:0",
            "gpt-4o",
        ],
    )
    def test_noop_for_ids_without_a_geography_prefix(self, base_id):
        assert base_foundation_model_id(base_id) == base_id

    def test_strips_only_the_leading_prefix_once(self):
        # A model name that merely contains "anthropic" is never mangled, and
        # only a single leading geo segment is removed.
        assert base_foundation_model_id("us.anthropic.claude") == "anthropic.claude"
        # "us" as part of a longer first segment is not a prefix to strip.
        assert base_foundation_model_id("uswest.anthropic.x") == "uswest.anthropic.x"


@pytest.fixture
def _aws_region(monkeypatch):
    """boto3 needs a region to construct the bedrock-runtime client (no creds
    needed for construction, no network calls in these tests)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class TestCountTokensModelIdSwap:
    """count_tokens must count against the base id, then restore the profile id
    so invocation keeps using the inference profile."""

    @pytest.mark.asyncio
    async def test_counts_against_base_id_and_restores_profile_id(self, _aws_region):
        model = CountTokensBedrockModel(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            use_native_token_count=True,
        )

        seen = {}

        async def fake_super_count(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
            seen["model_id_during_count"] = self.config["model_id"]
            return 4242

        with patch.object(BedrockModel, "count_tokens", fake_super_count):
            result = await model.count_tokens([], system_prompt="hi")

        assert result == 4242
        # The base id was used for the CountTokens call...
        assert seen["model_id_during_count"] == "anthropic.claude-haiku-4-5-20251001-v1:0"
        # ...and the profile id is restored for invocation afterward.
        assert model.config["model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    @pytest.mark.asyncio
    async def test_no_swap_when_already_base_id(self, _aws_region):
        model = CountTokensBedrockModel(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            use_native_token_count=True,
        )

        seen = {}

        async def fake_super_count(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
            seen["model_id_during_count"] = self.config["model_id"]
            return 7

        with patch.object(BedrockModel, "count_tokens", fake_super_count):
            await model.count_tokens([])

        assert seen["model_id_during_count"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert model.config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    @pytest.mark.asyncio
    async def test_profile_id_restored_even_when_count_raises(self, _aws_region):
        model = CountTokensBedrockModel(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            use_native_token_count=True,
        )

        async def fake_super_count(self, *args, **kwargs):
            raise RuntimeError("boom")

        with patch.object(BedrockModel, "count_tokens", fake_super_count):
            with pytest.raises(RuntimeError, match="boom"):
                await model.count_tokens([])

        assert model.config["model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
