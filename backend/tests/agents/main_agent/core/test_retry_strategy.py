"""Tests for TransientModelRetryStrategy, the single retry layer for every provider.

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
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    EventStreamError,
    ReadTimeoutError,
)
from google.genai import errors as genai_errors
from strands import ModelRetryStrategy
from strands.models import OpenAIResponsesModel
from strands.models.openai import OpenAIModel
from strands.types.exceptions import ModelThrottledException

from agents.main_agent.core.agent_factory import AgentFactory
from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig
from agents.main_agent.core.retry_strategy import (
    RETRYABLE_BEDROCK_ERROR_CODES,
    TransientModelRetryStrategy,
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
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(_client_error("ServiceUnavailableException")) is True

    @pytest.mark.parametrize("code", sorted(RETRYABLE_BEDROCK_ERROR_CODES))
    def test_all_declared_transient_codes_are_retryable(self, code):
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(_client_error(code)) is True

    def test_throttled_exception_still_retryable(self):
        """Never narrows the stock behavior it inherits."""
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(ModelThrottledException("slow down")) is True

    def test_stock_strategy_does_not_retry_service_unavailable(self):
        """Documents the gap this subclass exists to close."""
        assert ModelRetryStrategy().is_retryable(_client_error("ServiceUnavailableException")) is False

    @pytest.mark.parametrize(
        "code",
        ["ValidationException", "AccessDeniedException", "ResourceNotFoundException"],
    )
    def test_non_transient_client_errors_are_not_retryable(self, code):
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(_client_error(code)) is False

    def test_mid_stream_failure_is_not_retryable(self):
        """EventStreamError means chunks may already be on the wire — a retry
        would restart generation and duplicate visible output."""
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(_event_stream_error("ServiceUnavailableException")) is False

    def test_unrelated_exception_is_not_retryable(self):
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(ValueError("nope")) is False

    def test_malformed_client_error_response_is_not_retryable(self):
        err = _client_error("ServiceUnavailableException")
        err.response = {}
        assert TransientModelRetryStrategy().is_retryable(err) is False


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
        widened = TransientModelRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
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
        assert isinstance(strategy, TransientModelRetryStrategy)

# ---------------------------------------------------------------------------
# OpenAI family — GPT-6 on bedrock-runtime
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


class TestOpenAIFaults:
    def test_mid_stream_server_error_event_is_retryable(self):
        """The 10-03 prod failure: an error event inside a 200 stream, raised by
        the OpenAI SDK as a bare APIError with no status."""
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(openai.APIError(_SERVER_ERROR_TEXT, _REQUEST, body=None)) is True

    def test_throttled_unavailable_is_retryable(self):
        """The 10-06 prod failure: Strands maps it to ModelThrottledException,
        which nothing retried because the provider had no strategy at all."""
        strategy = TransientModelRetryStrategy()
        assert strategy.is_retryable(ModelThrottledException(_UNAVAILABLE_TEXT)) is True

    def test_response_failed_with_server_error_code_is_retryable(self):
        strategy = TransientModelRetryStrategy()
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
        assert TransientModelRetryStrategy().is_retryable(error) is True

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
        assert TransientModelRetryStrategy().is_retryable(error) is False

    @pytest.mark.parametrize(
        "error",
        [
            openai.APIError(_SERVER_ERROR_TEXT, _REQUEST, body=None),
            ModelThrottledException(_UNAVAILABLE_TEXT),
        ],
    )
    def test_failure_after_visible_output_is_not_retried(self, error):
        """A restart would print the abandoned prefix then a whole new answer."""
        assert TransientModelRetryStrategy().is_retryable(_partial(error)) is False

    def test_backoff_inherited_unchanged(self):
        ours = TransientModelRetryStrategy(max_attempts=4, initial_delay=2, max_delay=16)
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
        assert isinstance(strategy, TransientModelRetryStrategy)
        assert strategy._max_attempts == 3

    def test_no_retry_config_means_no_strategy(self, monkeypatch):
        assert self._build(None, monkeypatch) is None


# ---------------------------------------------------------------------------
# Bedrock faults the transport used to retry
# ---------------------------------------------------------------------------
class TestBedrockTransportFaults:
    """botocore no longer retries while the strategy is on, so the strategy
    has to cover what its standard mode did."""

    @pytest.mark.parametrize(
        "error",
        [
            EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com"),
            ConnectTimeoutError(endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com"),
            ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com"),
        ],
    )
    def test_connection_failures_and_timeouts_are_retryable(self, error):
        assert TransientModelRetryStrategy().is_retryable(error) is True

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_unmodeled_5xx_is_retryable(self, status):
        err = ClientError(
            {"Error": {"Code": "InternalFailure"}, "ResponseMetadata": {"HTTPStatusCode": status}},
            "ConverseStream",
        )
        assert TransientModelRetryStrategy().is_retryable(err) is True

    def test_4xx_with_unmodeled_code_is_not_retryable(self):
        err = ClientError(
            {"Error": {"Code": "SomethingNew"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
            "ConverseStream",
        )
        assert TransientModelRetryStrategy().is_retryable(err) is False

    def test_throttle_after_visible_output_is_not_retried(self):
        """The stock strategy retried a throttle wherever it was raised."""
        assert TransientModelRetryStrategy().is_retryable(_partial(ModelThrottledException("slow"))) is False


class TestGeminiFaults:
    def test_server_error_is_retryable(self):
        err = genai_errors.ServerError(500, {"error": {"message": "internal", "status": "INTERNAL"}})
        assert TransientModelRetryStrategy().is_retryable(err) is True

    def test_client_error_is_not_retryable(self):
        err = genai_errors.ClientError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
        assert TransientModelRetryStrategy().is_retryable(err) is False


# ---------------------------------------------------------------------------
# Every provider gets the strategy, and retries never compound
# ---------------------------------------------------------------------------
def _openai_model() -> OpenAIModel:
    return OpenAIModel(client_args={"api_key": "test-key"}, model_id="gpt-4o")


def _responses_model() -> OpenAIResponsesModel:
    return OpenAIResponsesModel(client_args={"api_key": "test-key"}, model_id="gpt-6-sol")


class TestBuildRetryStrategy:
    @pytest.mark.parametrize("provider", list(ModelProvider))
    def test_every_provider_gets_the_strategy(self, provider):
        """None means retries OFF in Strands, so no provider may be left on it."""
        cfg = ModelConfig(model_id="m", provider=provider, retry_config=RetryConfig(sdk_max_attempts=3))
        strategy = AgentFactory._build_retry_strategy(cfg, MagicMock())
        assert isinstance(strategy, TransientModelRetryStrategy)
        assert strategy._max_attempts == 3

    def test_no_retry_config_means_no_strategy(self):
        cfg = ModelConfig(model_id="m", provider=ModelProvider.OPENAI, retry_config=None)
        assert AgentFactory._build_retry_strategy(cfg, MagicMock()) is None

    def test_strategy_tags_the_model_stream(self):
        model = _openai_model()
        original = model.stream
        cfg = ModelConfig(model_id="m", provider=ModelProvider.OPENAI, retry_config=RetryConfig())
        AgentFactory._build_retry_strategy(cfg, model)
        assert model.stream is not original


class TestRetriesDoNotStack:
    """The transport makes exactly one attempt. The layers used to multiply:
    3 botocore attempts x 4 SDK attempts = 12 Bedrock calls."""

    def test_bedrock_transport_makes_one_attempt(self):
        cfg = ModelConfig(model_id="anthropic.claude-3-sonnet", retry_config=RetryConfig())
        boto = cfg.to_bedrock_config()["boto_client_config"]
        assert boto.retries["max_attempts"] == 1

    @pytest.mark.parametrize("factory", [_openai_model, _responses_model])
    def test_openai_client_makes_one_attempt(self, factory):
        model = factory()
        cfg = ModelConfig(model_id="m", provider=ModelProvider.OPENAI, retry_config=RetryConfig())
        AgentFactory._build_retry_strategy(cfg, model)
        assert model.client_args["max_retries"] == 0
        assert model.client_args["api_key"] == "test-key"

    def test_builder_client_args_are_not_mutated(self):
        """A builder shared with api-converse must not inherit max_retries=0."""
        shared = {"api_key": "test-key"}
        model = OpenAIModel(client_args=shared, model_id="gpt-4o")
        cfg = ModelConfig(model_id="m", provider=ModelProvider.OPENAI, retry_config=RetryConfig())
        AgentFactory._build_retry_strategy(cfg, model)
        assert "max_retries" not in shared

    def test_resolved_client_args_carry_the_setting(self):
        """Strands builds the AsyncOpenAI client from _resolve_client_args per request."""
        model = _responses_model()
        cfg = ModelConfig(model_id="m", provider=ModelProvider.BEDROCK_RESPONSES, retry_config=RetryConfig())
        AgentFactory._build_retry_strategy(cfg, model)
        assert model._resolve_client_args()["max_retries"] == 0

    def test_bedrock_runtime_responses_model_resolves_one_attempt(self):
        """The GPT-6 path: the token-minting subclass must keep max_retries."""
        from apis.shared.models.bedrock_responses import build_bedrock_responses_model

        model = build_bedrock_responses_model(model_id="global.openai.gpt-6-sol", region="us-west-2")
        cfg = ModelConfig(model_id="m", provider=ModelProvider.BEDROCK_RESPONSES, retry_config=RetryConfig())
        AgentFactory._build_retry_strategy(cfg, model)

        with patch("apis.shared.bedrock.bearer_token.generate_bedrock_bearer_token", return_value="minted"):
            args = model._resolve_client_args()

        assert args["max_retries"] == 0
        assert args["api_key"] == "minted"
