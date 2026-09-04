"""Tests for BedrockTransientRetryStrategy.

Regression cover for the prod failure on session ``5f34d2b0`` (2026-08-31): a
``ConverseStream`` call failed with ``ServiceUnavailableException`` after 95.6s
and was never retried, because Strands' stock ``ModelRetryStrategy`` only
treats ``ModelThrottledException`` as retryable.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EventStreamError
from strands import ModelRetryStrategy
from strands.types.exceptions import ModelThrottledException

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig
from agents.main_agent.core.retry_strategy import (
    RETRYABLE_BEDROCK_ERROR_CODES,
    BedrockTransientRetryStrategy,
    bedrock_error_code,
)


def _client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "ConverseStream"
    )


def _event_stream_error(code: str) -> EventStreamError:
    return EventStreamError(
        {"Error": {"Code": code, "Message": "mid-stream"}}, "ConverseStream"
    )


class TestIsRetryable:
    def test_service_unavailable_is_retryable(self):
        """The exact prod failure: 503 before the stream opens → retry."""
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(_client_error("ServiceUnavailableException")) is True

    @pytest.mark.parametrize("code", sorted(RETRYABLE_BEDROCK_ERROR_CODES))
    def test_all_declared_transient_codes_are_retryable(self, code):
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(_client_error(code)) is True

    def test_throttled_exception_still_retryable(self):
        """Never narrows the stock behavior it inherits."""
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(ModelThrottledException("slow down")) is True

    def test_stock_strategy_does_not_retry_service_unavailable(self):
        """Documents the gap this subclass exists to close."""
        assert ModelRetryStrategy().is_retryable(_client_error("ServiceUnavailableException")) is False

    @pytest.mark.parametrize(
        "code",
        ["ValidationException", "AccessDeniedException", "ResourceNotFoundException"],
    )
    def test_non_transient_client_errors_are_not_retryable(self, code):
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(_client_error(code)) is False

    def test_mid_stream_failure_is_not_retryable(self):
        """EventStreamError means chunks may already be on the wire — a retry
        would restart generation and duplicate visible output."""
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(_event_stream_error("ServiceUnavailableException")) is False

    def test_unrelated_exception_is_not_retryable(self):
        strategy = BedrockTransientRetryStrategy()
        assert strategy.is_retryable(ValueError("nope")) is False

    def test_malformed_client_error_response_is_not_retryable(self):
        err = _client_error("ServiceUnavailableException")
        err.response = {}
        assert BedrockTransientRetryStrategy().is_retryable(err) is False


class TestBedrockErrorCode:
    def test_reads_modeled_code(self):
        assert bedrock_error_code(_client_error("InternalServerException")) == "InternalServerException"

    def test_non_client_error_returns_none(self):
        assert bedrock_error_code(RuntimeError("x")) is None

    def test_non_string_code_returns_none(self):
        err = _client_error("x")
        err.response = {"Error": {"Code": 503}}
        assert bedrock_error_code(err) is None


class TestBackoffInheritedUnchanged:
    def test_delay_schedule_matches_stock_strategy(self):
        """Only the predicate is overridden; backoff policy is inherited."""
        widened = BedrockTransientRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
        stock = ModelRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
        assert [widened._calculate_delay(i) for i in range(5)] == [
            stock._calculate_delay(i) for i in range(5)
        ]


class TestFactoryWiring:
    _COMMON = dict(system_prompt="hi", tools=[], session_manager=MagicMock())

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_default_uses_widened_strategy(self, _model_cls, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory

        cfg = ModelConfig(
            model_id="anthropic.claude-3-sonnet",
            provider=ModelProvider.BEDROCK,
            retry_config=RetryConfig(),
            caching_enabled=False,
        )
        AgentFactory.create_agent(model_config=cfg, **self._COMMON)

        strategy = mock_agent_cls.call_args.kwargs["retry_strategy"]
        assert isinstance(strategy, BedrockTransientRetryStrategy)

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_kill_switch_falls_back_to_stock_strategy(self, _model_cls, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory

        cfg = ModelConfig(
            model_id="anthropic.claude-3-sonnet",
            provider=ModelProvider.BEDROCK,
            retry_config=RetryConfig(retry_transient_service_errors=False),
            caching_enabled=False,
        )
        AgentFactory.create_agent(model_config=cfg, **self._COMMON)

        strategy = mock_agent_cls.call_args.kwargs["retry_strategy"]
        assert isinstance(strategy, ModelRetryStrategy)
        assert not isinstance(strategy, BedrockTransientRetryStrategy)


class TestConfigFromEnv:
    def test_defaults_on(self, monkeypatch):
        monkeypatch.delenv("RETRY_TRANSIENT_SERVICE_ERRORS", raising=False)
        assert RetryConfig.from_env().retry_transient_service_errors is True

    def test_empty_string_stays_on(self, monkeypatch):
        """A workflow that injects an unset var as "" must not disable it."""
        monkeypatch.setenv("RETRY_TRANSIENT_SERVICE_ERRORS", "")
        assert RetryConfig.from_env().retry_transient_service_errors is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "False"])
    def test_literal_false_disables(self, monkeypatch, value):
        monkeypatch.setenv("RETRY_TRANSIENT_SERVICE_ERRORS", value)
        assert RetryConfig.from_env().retry_transient_service_errors is False
