"""Tests for BedrockTransientRetryStrategy and ResponsesTransientRetryStrategy.

Regression cover for the prod failure on session ``5f34d2b0`` (2026-08-31): a
``ConverseStream`` call failed with ``ServiceUnavailableException`` after 95.6s
and was never retried, because Strands' stock ``ModelRetryStrategy`` only
treats ``ModelThrottledException`` as retryable.

And for GPT-6 on bedrock-runtime (2026-09/10): the Responses provider had no
strategy at all, so every transient fault reached the user as "Agent
force-stopped" on the first attempt.
"""

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
from botocore.exceptions import ClientError, EventStreamError
from strands import ModelRetryStrategy
from strands.types.exceptions import ModelThrottledException

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig
from agents.main_agent.core.retry_strategy import (
    RETRYABLE_BEDROCK_ERROR_CODES,
    BedrockTransientRetryStrategy,
    ResponsesTransientRetryStrategy,
    bedrock_error_code,
    is_transient_openai_fault,
)
from apis.shared.models.stream_output import mark_partial_output


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


# ---------------------------------------------------------------------------
# ResponsesTransientRetryStrategy — GPT-6 on bedrock-runtime
# ---------------------------------------------------------------------------
_REQUEST = httpx.Request("POST", "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/responses")

# The exact texts prod surfaced as "Agent force-stopped: ..." (2026-09/10).
_SERVER_ERROR_TEXT = "The server had an error while processing your request. Sorry about that!"
_UNAVAILABLE_TEXT = "The service is temporarily unavailable."


def _status_error(cls, status: int, message: str = "boom"):
    return cls(message, response=httpx.Response(status, request=_REQUEST), body=None)


class _StreamError(RuntimeError):
    """Shape of Strands' private _OpenAIResponsesStreamError: a message and a code."""

    def __init__(self, message: str, code: str | None) -> None:
        super().__init__(message)
        self.code = code


def _partial(error: Exception) -> Exception:
    mark_partial_output(error)
    return error


class TestResponsesIsRetryable:
    def test_mid_stream_server_error_event_is_retryable(self):
        """The 10-03 prod failure: an error event inside a 200 stream, raised by
        the OpenAI SDK as a bare APIError with no status."""
        strategy = ResponsesTransientRetryStrategy()
        assert strategy.is_retryable(openai.APIError(_SERVER_ERROR_TEXT, _REQUEST, body=None)) is True

    def test_throttled_unavailable_is_retryable(self):
        """The 10-06 prod failure: Strands maps it to ModelThrottledException,
        which nothing retried because the provider had no strategy at all."""
        strategy = ResponsesTransientRetryStrategy()
        assert strategy.is_retryable(ModelThrottledException(_UNAVAILABLE_TEXT)) is True

    def test_response_failed_with_server_error_code_is_retryable(self):
        strategy = ResponsesTransientRetryStrategy()
        assert strategy.is_retryable(_StreamError("response failed", "server_error")) is True

    @pytest.mark.parametrize(
        "error",
        [
            _status_error(openai.InternalServerError, 500),
            openai.APIConnectionError(request=_REQUEST),
            openai.APITimeoutError(request=_REQUEST),
        ],
    )
    def test_pre_stream_transport_faults_are_retryable(self, error):
        assert ResponsesTransientRetryStrategy().is_retryable(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            _status_error(openai.BadRequestError, 400, _SERVER_ERROR_TEXT),
            _status_error(openai.AuthenticationError, 401),
            _status_error(openai.PermissionDeniedError, 403),
            openai.APIError("Invalid value for 'input'", _REQUEST, body=None),
            _StreamError("bad request", "invalid_request_error"),
            ValueError("nope"),
        ],
    )
    def test_request_errors_are_not_retryable(self, error):
        """A 4xx is the request's fault, whatever its message says."""
        assert ResponsesTransientRetryStrategy().is_retryable(error) is False

    @pytest.mark.parametrize(
        "error",
        [
            openai.APIError(_SERVER_ERROR_TEXT, _REQUEST, body=None),
            ModelThrottledException(_UNAVAILABLE_TEXT),
        ],
    )
    def test_failure_after_visible_output_is_not_retried(self, error):
        """A restart would print the abandoned prefix then a whole new answer."""
        assert ResponsesTransientRetryStrategy().is_retryable(_partial(error)) is False

    def test_backoff_inherited_unchanged(self):
        ours = ResponsesTransientRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
        stock = ModelRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
        assert [ours._calculate_delay(i) for i in range(5)] == [stock._calculate_delay(i) for i in range(5)]


class TestIsTransientOpenAIFault:
    def test_bedrock_client_error_is_not_an_openai_fault(self):
        assert is_transient_openai_fault(_client_error("ValidationException")) is False


class TestResponsesFactoryWiring:
    _COMMON = dict(system_prompt="hi", tools=[], session_manager=MagicMock())

    def _build(self, retry_config, monkeypatch):
        from agents.main_agent.core.agent_factory import AgentFactory

        monkeypatch.setenv("AWS_REGION", "us-west-2")
        cfg = ModelConfig(
            model_id="global.openai.gpt-6-sol",
            provider=ModelProvider.BEDROCK_RESPONSES,
            retry_config=retry_config,
        )
        with patch("agents.main_agent.core.agent_factory.Agent") as agent_cls, patch(
            "agents.main_agent.core.agent_factory.build_bedrock_responses_model"
        ):
            AgentFactory.create_agent(model_config=cfg, **self._COMMON)
        return agent_cls.call_args.kwargs["retry_strategy"]

    def test_responses_provider_gets_a_retry_strategy(self, monkeypatch):
        """None here means retries OFF in Strands, not "use the default"."""
        strategy = self._build(RetryConfig(sdk_max_attempts=3), monkeypatch)
        assert isinstance(strategy, ResponsesTransientRetryStrategy)
        assert strategy._max_attempts == 3

    def test_kill_switch_restores_no_retry(self, monkeypatch):
        strategy = self._build(RetryConfig(retry_transient_service_errors=False), monkeypatch)
        assert strategy is None

    def test_no_retry_config_means_no_strategy(self, monkeypatch):
        assert self._build(None, monkeypatch) is None
