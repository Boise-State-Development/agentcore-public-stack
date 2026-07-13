"""Unit tests for the shared Bedrock Mantle model builder.

These cover the class-pick / region / params construction contract that both
the agent factory and the API-key converse handler depend on. The builder
imports the Strands model classes lazily, so patches target their source
modules (`strands.models.openai` / `strands.models`).
"""

from unittest.mock import MagicMock, patch

from apis.shared.models.mantle import (
    MantleApiMode,
    MANTLE_CHAT_PARAM_MAP,
    MANTLE_RESPONSES_PARAM_MAP,
    build_mantle_model,
    param_map_for,
)


class TestBuildMantleModel:
    @patch("strands.models.openai.OpenAIModel")
    def test_chat_mode_builds_openai_model_with_region(self, mock_openai_cls):
        instance = MagicMock()
        mock_openai_cls.return_value = instance

        result = build_mantle_model(
            model_id="openai.gpt-oss-120b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
            region="us-west-2",
        )

        assert result is instance
        mock_openai_cls.assert_called_once()
        kwargs = mock_openai_cls.call_args.kwargs
        assert kwargs["model_id"] == "openai.gpt-oss-120b"
        assert kwargs["bedrock_mantle_config"] == {"region": "us-west-2"}
        # The SDK owns base_url + token; we hand it region only.
        assert "client_args" not in kwargs
        assert "params" not in kwargs

    @patch("strands.models.OpenAIResponsesModel")
    @patch("strands.models.openai.OpenAIModel")
    def test_responses_mode_builds_responses_model(self, mock_openai_cls, mock_responses_cls):
        build_mantle_model(
            model_id="openai.gpt-5.4",
            api_mode=MantleApiMode.RESPONSES,
            region="us-east-1",
        )

        mock_responses_cls.assert_called_once()
        mock_openai_cls.assert_not_called()
        kwargs = mock_responses_cls.call_args.kwargs
        assert kwargs["model_id"] == "openai.gpt-5.4"
        assert kwargs["bedrock_mantle_config"] == {"region": "us-east-1"}

    @patch("strands.models.openai.OpenAIModel")
    def test_no_region_leaves_bedrock_mantle_config_empty(self, mock_openai_cls):
        build_mantle_model(model_id="openai.gpt-oss-120b", api_mode=MantleApiMode.CHAT_COMPLETIONS)
        assert mock_openai_cls.call_args.kwargs["bedrock_mantle_config"] == {}

    @patch("strands.models.openai.OpenAIModel")
    def test_params_forwarded(self, mock_openai_cls):
        build_mantle_model(
            model_id="openai.gpt-oss-120b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
            region="us-west-2",
            params={"temperature": 0.5, "max_tokens": 128},
        )
        assert mock_openai_cls.call_args.kwargs["params"] == {"temperature": 0.5, "max_tokens": 128}


class TestGemma4Routing:
    """The build path teaches the SDK to serve google.gemma-4-* from /openai/v1.

    Gemma 4's model card pins it to the Mantle /openai/v1 base path, but the
    SDK only lists openai.gpt-5. — so the builder appends the family prefix.
    """

    def _prefixes(self):
        from strands.models import _openai_bedrock as sdk

        return sdk._OPENAI_PATH_MODEL_PREFIXES

    @patch("strands.models.openai.OpenAIModel")
    def test_build_registers_gemma4_prefix(self, _mock_openai_cls):
        build_mantle_model(
            model_id="google.gemma-4-31b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
            region="us-east-1",
        )
        assert "google.gemma-4-" in self._prefixes()

    @patch("strands.models.openai.OpenAIModel")
    def test_gemma4_variants_route_to_openai_v1(self, _mock_openai_cls):
        from strands.models._openai_bedrock import _resolve_mantle_base_path

        build_mantle_model(
            model_id="google.gemma-4-31b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
        )
        for model_id in (
            "google.gemma-4-31b",
            "google.gemma-4-26b-a4b",
            "google.gemma-4-e2b",
        ):
            assert _resolve_mantle_base_path(model_id) == "/openai/v1"

    @patch("strands.models.openai.OpenAIModel")
    def test_gemma3_stays_on_v1(self, _mock_openai_cls):
        # Gemma 3 is served on /v1 — the narrower prefix must not reroute it.
        from strands.models._openai_bedrock import _resolve_mantle_base_path

        build_mantle_model(
            model_id="google.gemma-4-31b",
            api_mode=MantleApiMode.CHAT_COMPLETIONS,
        )
        assert _resolve_mantle_base_path("google.gemma-3-27b-it") == "/v1"

    @patch("strands.models.openai.OpenAIModel")
    def test_registration_is_idempotent(self, _mock_openai_cls):
        for _ in range(3):
            build_mantle_model(
                model_id="google.gemma-4-31b",
                api_mode=MantleApiMode.CHAT_COMPLETIONS,
            )
        assert self._prefixes().count("google.gemma-4-") == 1


class TestParamMapFor:
    def test_chat_mode_map(self):
        assert param_map_for(MantleApiMode.CHAT_COMPLETIONS) is MANTLE_CHAT_PARAM_MAP
        assert MANTLE_CHAT_PARAM_MAP["max_tokens"] == "max_tokens"

    def test_responses_mode_map(self):
        assert param_map_for(MantleApiMode.RESPONSES) is MANTLE_RESPONSES_PARAM_MAP
        # Responses API renames the output cap and nests reasoning effort.
        assert MANTLE_RESPONSES_PARAM_MAP["max_tokens"] == "max_output_tokens"
        assert MANTLE_RESPONSES_PARAM_MAP["reasoning_effort"] == "reasoning.effort"
