"""Tests for CountTokensBedrockModel and base_foundation_model_id.

The subclass exists because Bedrock's CountTokens API rejects cross-region
inference-profile model ids (``us.anthropic.…``) but on-demand invocation
requires them. The subclass de-prefixes for the count call only.
"""

from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from strands.models.bedrock import _SKIP_COUNT_TOKENS_MODELS, _clear_skip_count_tokens_cache
from strands.models.model import Model

from agents.main_agent.core.bedrock_count_tokens import (
    COUNT_TOKENS_MAX_ATTEMPTS_ENV,
    COUNT_TOKENS_TIMEOUT_ENV,
    CountTokensBedrockModel,
    base_foundation_model_id,
    build_count_tokens_client_config,
    count_tokens_max_attempts,
    count_tokens_timeout_seconds,
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
            ("au.anthropic.claude-x", "anthropic.claude-x"),
            ("jp.anthropic.claude-x", "anthropic.claude-x"),
            # Global CRIS — what prod runs every model on.
            ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("global.anthropic.claude-x", "anthropic.claude-x"),
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
        assert base_foundation_model_id("globalx.anthropic.x") == "globalx.anthropic.x"
        # Undocumented geography codes are left alone rather than guessed at.
        assert base_foundation_model_id("ca.anthropic.x") == "ca.anthropic.x"


@pytest.fixture
def _aws_region(monkeypatch):
    """boto3 needs a region to construct the bedrock-runtime client (no creds
    needed for construction, no network calls in these tests)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


PROFILE_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BASE_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"


class FakeCountClient:
    """Stands in for the dedicated CountTokens boto client."""

    def __init__(self, result=4242, raise_with=None):
        self.result = result
        self.raise_with = raise_with
        self.calls = []

    def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_with is not None:
            raise self.raise_with
        return {"inputTokens": self.result}


def _client_error(code, message="nope"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "CountTokens")


@pytest.fixture(autouse=True)
def _clean_skip_cache():
    _clear_skip_count_tokens_cache()
    yield
    _clear_skip_count_tokens_cache()


def _model(model_id=PROFILE_ID, native=True, client=None):
    model = CountTokensBedrockModel(model_id=model_id, use_native_token_count=native)
    if client is not None:
        model._count_client = client
    return model


class TestCountTokensModelId:
    """The count goes to the API with the base id; the profile id in config is
    never touched, before, during or after."""

    @pytest.mark.asyncio
    async def test_counts_against_base_id_without_mutating_config(self, _aws_region):
        client = FakeCountClient(result=4242)
        model = _model(client=client)

        result = await model.count_tokens([], system_prompt="hi")

        assert result == 4242
        assert client.calls[0]["modelId"] == BASE_ID
        assert client.calls[0]["input"]["converse"]["system"] == [{"text": "hi"}]
        assert model.config["model_id"] == PROFILE_ID

    @pytest.mark.asyncio
    async def test_config_is_not_mutated_even_mid_call(self, _aws_region):
        """A concurrent stream() must never observe the base id — that was the
        hazard of the old swap-and-restore approach."""
        model = _model()
        seen = {}

        class SpyClient(FakeCountClient):
            def count_tokens(self, **kwargs):
                seen["model_id_during_count"] = model.config["model_id"]
                return super().count_tokens(**kwargs)

        model._count_client = SpyClient(result=1)
        await model.count_tokens([])

        assert seen["model_id_during_count"] == PROFILE_ID

    @pytest.mark.asyncio
    async def test_base_id_passes_through_unchanged(self, _aws_region):
        client = FakeCountClient(result=7)
        model = _model(model_id="anthropic.claude-3-5-sonnet-20241022-v2:0", client=client)

        await model.count_tokens([])

        assert client.calls[0]["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    @pytest.mark.asyncio
    async def test_native_flag_off_never_builds_a_client(self, _aws_region):
        model = _model(native=False)

        with patch.object(CountTokensBedrockModel, "_get_count_client") as get_client:
            result = await model.count_tokens([{"role": "user", "content": [{"text": "hello world"}]}])

        get_client.assert_not_called()
        assert isinstance(result, int) and result > 0


class TestCountTokensBounded:
    """A throttle costs one failed attempt and falls back to the heuristic;
    it is never retried and never raised into the turn."""

    @pytest.mark.asyncio
    async def test_throttle_falls_back_to_heuristic_after_one_attempt(self, _aws_region):
        client = FakeCountClient(raise_with=_client_error("ThrottlingException"))
        model = _model(client=client)
        messages = [{"role": "user", "content": [{"text": "x" * 400}]}]

        result = await model.count_tokens(messages)

        assert len(client.calls) == 1
        # chars/4 heuristic, not the native answer and not an exception.
        assert result == await Model.count_tokens(model, messages)
        # A throttle is transient: the model is NOT added to the skip list.
        assert BASE_ID not in _SKIP_COUNT_TOKENS_MODELS

    @pytest.mark.asyncio
    async def test_access_denied_is_remembered_and_skips_the_client(self, _aws_region):
        client = FakeCountClient(raise_with=_client_error("AccessDeniedException"))
        model = _model(client=client)

        await model.count_tokens([])
        await model.count_tokens([])

        assert len(client.calls) == 1
        assert BASE_ID in _SKIP_COUNT_TOKENS_MODELS

    @pytest.mark.asyncio
    async def test_unsupported_model_is_remembered(self, _aws_region):
        client = FakeCountClient(
            raise_with=_client_error("ValidationException", "The provided model doesn't support counting tokens.")
        )
        model = _model(client=client)

        await model.count_tokens([])

        assert BASE_ID in _SKIP_COUNT_TOKENS_MODELS

    @pytest.mark.asyncio
    async def test_unexpected_error_falls_back_without_raising(self, _aws_region):
        client = FakeCountClient(raise_with=RuntimeError("boom"))
        model = _model(client=client)

        result = await model.count_tokens([{"role": "user", "content": [{"text": "hi"}]}])

        assert isinstance(result, int)
        assert model.config["model_id"] == PROFILE_ID

    @pytest.mark.asyncio
    async def test_missing_input_tokens_falls_back(self, _aws_region):
        class NoneClient(FakeCountClient):
            def count_tokens(self, **kwargs):
                self.calls.append(kwargs)
                return {}

        model = _model(client=NoneClient())

        result = await model.count_tokens([{"role": "user", "content": [{"text": "hi"}]}])

        assert isinstance(result, int)


class TestCountIsNativeOrSaysSo:
    """Callers that build arithmetic on counts (the context-attribution hook's
    tools residual) must be able to tell a native count from the heuristic:
    Claude Sonnet 5 has no CountTokens at all, and any count can fall back."""

    @pytest.mark.asyncio
    async def test_a_native_count_is_not_a_fallback(self, _aws_region):
        model = _model(client=FakeCountClient(result=77))
        assert await model.count_tokens([]) == 77
        assert model.heuristic_count_fallbacks == 0
        assert model.token_count_is_authoritative is True

    @pytest.mark.asyncio
    async def test_a_throttle_counts_as_a_fallback_but_stays_authoritative(self, _aws_region):
        model = _model(client=FakeCountClient(raise_with=_client_error("ThrottlingException")))
        await model.count_tokens([])
        assert model.heuristic_count_fallbacks == 1
        # Transient: the next count may well be native.
        assert model.token_count_is_authoritative is True

    @pytest.mark.asyncio
    async def test_an_unsupported_model_stops_being_authoritative(self, _aws_region):
        client = FakeCountClient(
            raise_with=_client_error("ValidationException", "The provided model doesn't support counting tokens.")
        )
        model = _model(client=client)
        await model.count_tokens([])
        await model.count_tokens([])
        assert model.heuristic_count_fallbacks == 2
        assert model.token_count_is_authoritative is False

    @pytest.mark.asyncio
    async def test_native_counting_off_is_a_fallback_every_time(self, _aws_region):
        model = _model(native=False)
        await model.count_tokens([])
        assert model.heuristic_count_fallbacks == 1
        assert model.token_count_is_authoritative is False


class TestCountTokensClientConfig:
    """The dedicated client is what bounds the cost of a throttle."""

    def test_default_is_one_attempt_and_a_two_second_timeout(self, monkeypatch):
        monkeypatch.delenv(COUNT_TOKENS_TIMEOUT_ENV, raising=False)
        monkeypatch.delenv(COUNT_TOKENS_MAX_ATTEMPTS_ENV, raising=False)

        cfg = build_count_tokens_client_config()

        assert cfg.retries == {"total_max_attempts": 1, "mode": "standard"}
        assert cfg.read_timeout == 2.0
        assert cfg.connect_timeout == 2.0

    def test_env_overrides_are_honoured(self, monkeypatch):
        monkeypatch.setenv(COUNT_TOKENS_TIMEOUT_ENV, "0.5")
        monkeypatch.setenv(COUNT_TOKENS_MAX_ATTEMPTS_ENV, "3")

        cfg = build_count_tokens_client_config()

        assert cfg.retries["total_max_attempts"] == 3
        assert cfg.read_timeout == 0.5

    def test_garbage_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv(COUNT_TOKENS_TIMEOUT_ENV, "soon")
        monkeypatch.setenv(COUNT_TOKENS_MAX_ATTEMPTS_ENV, "0")

        assert count_tokens_timeout_seconds() == 2.0
        assert count_tokens_max_attempts() == 1

    def test_client_is_built_lazily_in_the_invocation_region(self, _aws_region):
        model = _model()
        assert model._count_client is None

        with patch("agents.main_agent.core.bedrock_count_tokens.boto3.client") as boto_client:
            boto_client.return_value = object()
            first = model._get_count_client()
            second = model._get_count_client()

        assert first is second
        boto_client.assert_called_once()
        kwargs = boto_client.call_args.kwargs
        assert boto_client.call_args.args == ("bedrock-runtime",)
        assert kwargs["region_name"] == "us-east-1"
        assert kwargs["config"].retries["total_max_attempts"] == 1


class TestNativeCountTokens:
    """The counter the context-attribution hook calls from its background
    task: a native answer or ``None`` — never a heuristic — with the same
    skip-list bookkeeping as ``count_tokens``."""

    def test_counts_against_the_base_id(self, _aws_region):
        client = FakeCountClient(result=777)
        model = _model(model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0", client=client)

        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) == 777
        assert client.calls[0]["modelId"] == BASE_ID

    def test_native_counting_off_answers_none_without_a_request(self, _aws_region):
        client = FakeCountClient()
        model = _model(native=False, client=client)

        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) is None
        assert client.calls == []

    def test_a_skip_listed_model_answers_none_without_a_request(self, _aws_region):
        client = FakeCountClient()
        model = _model(client=client)
        _SKIP_COUNT_TOKENS_MODELS.add(BASE_ID)

        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) is None
        assert client.calls == []

    def test_unsupported_answers_none_and_skip_lists_the_base_id(self, _aws_region):
        client = FakeCountClient(
            raise_with=_client_error("ValidationException", "The provided model doesn't support counting tokens.")
        )
        model = _model(client=client)

        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) is None
        assert BASE_ID in _SKIP_COUNT_TOKENS_MODELS
        assert model.token_count_is_authoritative is False

    def test_a_throttle_answers_none_and_does_not_skip_list(self, _aws_region):
        client = FakeCountClient(raise_with=_client_error("ThrottlingException"))
        model = _model(client=client)

        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) is None
        assert BASE_ID not in _SKIP_COUNT_TOKENS_MODELS
        assert model.heuristic_count_fallbacks == 0, "no heuristic was answered"


class TestNativeProjectionOff:
    """``native_projection=False`` — how the factory builds every Converse
    model — keeps the count Strands awaits before each model call local."""

    @pytest.mark.asyncio
    async def test_count_tokens_never_reaches_the_network(self, _aws_region):
        client = FakeCountClient()
        model = CountTokensBedrockModel(model_id=PROFILE_ID, use_native_token_count=True, native_projection=False)
        model._count_client = client

        with patch.object(Model, "count_tokens", return_value=42) as heuristic:
            result = await model.count_tokens([{"role": "user", "content": [{"text": "hi"}]}], system_prompt="s")

        assert result == 42
        heuristic.assert_called_once()
        assert client.calls == []

    def test_native_counts_stay_available_to_the_hook(self, _aws_region):
        client = FakeCountClient(result=99)
        model = CountTokensBedrockModel(model_id=PROFILE_ID, use_native_token_count=True, native_projection=False)
        model._count_client = client

        assert model.token_count_is_authoritative is True
        assert model.native_count_tokens([{"role": "user", "content": [{"text": "hi"}]}]) == 99

    def test_the_factory_builds_models_with_the_projection_local(self, _aws_region):
        from agents.main_agent.core.agent_factory import AgentFactory
        from agents.main_agent.core.model_config import ModelConfig

        model = AgentFactory._create_bedrock_model(ModelConfig(model_id=PROFILE_ID))

        assert isinstance(model, CountTokensBedrockModel)
        assert model._native_projection is False
        assert model.config["use_native_token_count"] is True
