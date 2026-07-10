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


class TestParamMapFor:
    def test_chat_mode_map(self):
        assert param_map_for(MantleApiMode.CHAT_COMPLETIONS) is MANTLE_CHAT_PARAM_MAP
        assert MANTLE_CHAT_PARAM_MAP["max_tokens"] == "max_tokens"

    def test_responses_mode_map(self):
        assert param_map_for(MantleApiMode.RESPONSES) is MANTLE_RESPONSES_PARAM_MAP
        # Responses API renames the output cap and nests reasoning effort.
        assert MANTLE_RESPONSES_PARAM_MAP["max_tokens"] == "max_output_tokens"
        assert MANTLE_RESPONSES_PARAM_MAP["reasoning_effort"] == "reasoning.effort"
