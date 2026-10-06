"""Unit tests for the shared bedrock-runtime OpenAI Responses builder.

Covers the construction contract both consumers depend on — the agent factory
and the API-key converse handler — plus the two things that distinguish this
transport from the Mantle one:

- the bearer token is minted **per request**, not frozen at construction;
- usage still arrives in disjoint Bedrock-Converse buckets, because the
  Responses API reports an inclusive ``input_tokens``.

The token test is the load-bearing one. Our microVMs live 18-50 minutes
against a 12-hour token cap, so a token frozen at construction would work by
luck in dev and expire in prod under any longer-lived process.
"""

from unittest.mock import patch

import pytest

from apis.shared.models.bedrock_responses import (
    BEDROCK_RESPONSES_PARAM_MAP,
    BEDROCK_RUNTIME_OPENAI_PATH,
    build_bedrock_responses_model,
    get_bedrock_runtime_openai_base_url,
)
from apis.shared.models.usage_normalization import usage_normalized

_TOKEN_FN = "apis.shared.bedrock.bearer_token.generate_bedrock_bearer_token"


class TestBaseUrl:
    def test_regional_openai_path(self):
        assert (
            get_bedrock_runtime_openai_base_url("us-west-2")
            == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
        )

    def test_path_is_fixed_not_model_derived(self):
        """Unlike Mantle, bedrock-runtime serves every OpenAI model from one path."""
        assert BEDROCK_RUNTIME_OPENAI_PATH == "/openai/v1"

    def test_falls_back_to_ambient_region(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        assert "bedrock-runtime.us-east-1." in get_bedrock_runtime_openai_base_url()

    def test_raises_without_a_region(self, monkeypatch):
        # Loud rather than defaulting to some other region: a wrong-region
        # endpoint fails as an opaque auth error at the first turn.
        monkeypatch.delenv("AWS_REGION", raising=False)

        with pytest.raises(ValueError, match="No AWS region"):
            get_bedrock_runtime_openai_base_url()


class TestBuildBedrockResponsesModel:
    def test_builds_a_responses_model_on_the_runtime_endpoint(self):
        from strands.models import OpenAIResponsesModel

        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        assert isinstance(model, OpenAIResponsesModel)
        assert model.client_args["base_url"] == (
            "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
        )
        assert model.get_config()["model_id"] == "us.openai.gpt-5.6-sol"

    def test_does_not_use_bedrock_mantle_config(self):
        """The Mantle config hardcodes the Mantle host and rejects our base_url."""
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        assert getattr(model, "_bedrock_mantle_config", None) is None

    def test_params_are_forwarded(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol",
            region="us-west-2",
            params={"max_output_tokens": 512, "temperature": 0.4},
        )

        assert model.get_config()["params"] == {
            "max_output_tokens": 512,
            "temperature": 0.4,
        }

    def test_no_params_key_when_none_given(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        assert "params" not in model.get_config()

    def test_region_pins_both_url_and_token_signature(self):
        """One resolved value drives both, so they cannot disagree."""
        model = build_bedrock_responses_model(
            model_id="global.openai.gpt-5.6-sol", region="us-east-1"
        )

        assert "bedrock-runtime.us-east-1." in model.client_args["base_url"]
        with patch(_TOKEN_FN, return_value="bedrock-api-key-x") as mint:
            model._resolve_client_args()
        mint.assert_called_once_with("us-east-1")

    def test_raises_without_a_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)

        with pytest.raises(ValueError, match="No AWS region"):
            build_bedrock_responses_model(model_id="us.openai.gpt-5.6-sol")

    def test_model_class_is_memoized(self):
        first = build_bedrock_responses_model("us.openai.gpt-5.6-sol", region="us-west-2")
        second = build_bedrock_responses_model("global.openai.gpt-5.6-sol", region="us-east-1")

        assert type(first) is type(second)

    def test_param_map_is_the_responses_vocabulary(self):
        """Native names belong to the API, not the transport — shared, not copied."""
        from apis.shared.models.mantle import MANTLE_RESPONSES_PARAM_MAP

        assert BEDROCK_RESPONSES_PARAM_MAP is MANTLE_RESPONSES_PARAM_MAP
        assert BEDROCK_RESPONSES_PARAM_MAP["max_tokens"] == "max_output_tokens"


class TestInferenceProfileWarning:
    def test_warns_when_the_id_names_no_inference_profile(self, caplog):
        # bedrock-runtime does not offer in-Region inference for GPT-5.6, so a
        # bare `openai.` id is a misconfiguration worth surfacing.
        with caplog.at_level("WARNING"):
            build_bedrock_responses_model("openai.gpt-5.6-sol", region="us-west-2")

        assert "names no inference profile" in caplog.text

    @pytest.mark.parametrize(
        "model_id",
        ["us.openai.gpt-5.6-sol", "global.openai.gpt-5.6-sol", "eu.openai.gpt-5.6-luna"],
    )
    def test_silent_for_profile_prefixed_ids(self, model_id, caplog):
        with caplog.at_level("WARNING"):
            build_bedrock_responses_model(model_id, region="us-west-2")

        assert "names no inference profile" not in caplog.text

    def test_warning_does_not_block_construction(self, caplog):
        """A future in-Region model must not need a code change to run."""
        with caplog.at_level("WARNING"):
            model = build_bedrock_responses_model("openai.some-future-model", region="us-west-2")

        assert model.get_config()["model_id"] == "openai.some-future-model"


class TestPerRequestTokenMint:
    """The reason this transport gets its own model class."""

    def test_token_is_minted_on_every_resolve(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        with patch(_TOKEN_FN, side_effect=["token-1", "token-2"]) as mint:
            first = model._resolve_client_args()
            second = model._resolve_client_args()

        assert mint.call_count == 2
        assert first["api_key"] == "token-1"
        assert second["api_key"] == "token-2"

    def test_construction_does_not_mint(self):
        """Nothing is signed until a request actually needs a credential."""
        with patch(_TOKEN_FN) as mint:
            build_bedrock_responses_model(
                model_id="us.openai.gpt-5.6-sol", region="us-west-2"
            )

        mint.assert_not_called()

    def test_placeholder_key_never_survives_to_a_request(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )
        placeholder = model.client_args["api_key"]

        with patch(_TOKEN_FN, return_value="bedrock-api-key-real"):
            resolved = model._resolve_client_args()

        assert resolved["api_key"] == "bedrock-api-key-real"
        assert resolved["api_key"] != placeholder

    def test_base_url_survives_the_token_swap(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        with patch(_TOKEN_FN, return_value="bedrock-api-key-x"):
            resolved = model._resolve_client_args()

        assert resolved["base_url"] == (
            "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
        )

    def test_resolve_does_not_mutate_the_stored_client_args(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )
        before = dict(model.client_args)

        with patch(_TOKEN_FN, return_value="bedrock-api-key-x"):
            model._resolve_client_args()

        assert model.client_args == before


class TestUsageNormalizationApplies:
    """This transport is an OpenAI surface, so its usage needs normalizing too."""

    def test_model_class_is_usage_normalized(self):
        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )

        assert type(model).__name__.startswith("UsageNormalized")
        # The wrapper sits directly over our token-refreshing subclass.
        assert type(model) is usage_normalized(type(model).__mro__[1])

    def test_metadata_chunk_reports_disjoint_buckets(self):
        from openai.types.responses.response_usage import ResponseUsage

        model = build_bedrock_responses_model(
            model_id="us.openai.gpt-5.6-sol", region="us-west-2"
        )
        usage_obj = ResponseUsage.model_validate(
            {
                "input_tokens": 30_500,
                "input_tokens_details": {"cached_tokens": 30_000, "cache_write_tokens": 400},
                "output_tokens": 120,
                "output_tokens_details": {"reasoning_tokens": 64},
                "total_tokens": 30_620,
            }
        )

        usage = model._format_chunk({"chunk_type": "metadata", "data": usage_obj})[
            "metadata"
        ]["usage"]

        assert usage["inputTokens"] == 100
        assert usage["cacheReadInputTokens"] == 30_000
        assert usage["cacheWriteInputTokens"] == 400
        assert (
            usage["inputTokens"]
            + usage["cacheReadInputTokens"]
            + usage["cacheWriteInputTokens"]
        ) == usage["totalTokens"] - usage["outputTokens"]
