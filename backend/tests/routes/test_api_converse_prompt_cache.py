"""Bedrock cachePoints on api-converse.

The route adds a point after the system prompt and, on a request of two or
more messages, after the last message — only for a model that may be sent an
explicit point (``apis.shared.models.cache_points``) and whose catalog row
does not turn caching off. A point on any other model is an
AccessDeniedException, so the negative cases matter as much as the positive.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.chat.converse_routes import (
    _build_converse_params,
    _caches_prompt,
    _prefix_fingerprints,
    _with_cache_points,
    router,
)
from apis.app_api.chat.models import ConverseRequest
from apis.shared.auth.api_keys.models import ValidatedApiKey
from apis.shared.models.cache_points import bedrock_cache_points_supported

CLAUDE = "global.anthropic.claude-sonnet-5"
NOVA = "us.amazon.nova-pro-v1:0"
POINT = {"cachePoint": {"type": "default"}}

TWO_TURNS = [
    {"role": "user", "content": "Summarize the attached policy."},
    {"role": "assistant", "content": "It covers travel reimbursement."},
    {"role": "user", "content": "What is the per-diem cap?"},
]


def _request(messages=None, system_prompt="You are a careful analyst.", model_id=CLAUDE, stream=False):
    return ConverseRequest(
        model_id=model_id,
        messages=messages or [{"role": "user", "content": "Hello"}],
        system_prompt=system_prompt,
        stream=stream,
    )


class TestPredicate:
    @pytest.mark.parametrize(
        "model_id",
        [CLAUDE, "us.anthropic.claude-opus-5-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0"],
    )
    def test_claude_family_may_be_sent_a_point(self, model_id):
        assert bedrock_cache_points_supported(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [NOVA, "us.meta.llama3-3-70b-instruct-v1:0", "mistral.mistral-large-2407-v1:0", "us.deepseek.r1-v1:0", ""],
    )
    def test_other_models_are_never_sent_one(self, model_id):
        assert bedrock_cache_points_supported(model_id) is False

    def test_agent_path_reads_the_same_predicate(self):
        from agents.main_agent.core.model_config import ModelConfig

        assert ModelConfig(model_id=CLAUDE).bedrock_cache_points_supported() is True
        assert ModelConfig(model_id=NOVA).bedrock_cache_points_supported() is False


class TestCachesPrompt:
    def test_claude_without_a_catalog_row(self):
        assert _caches_prompt(CLAUDE, None) is True

    def test_catalog_row_can_turn_it_off(self):
        assert _caches_prompt(CLAUDE, SimpleNamespace(supports_caching=False)) is False

    def test_catalog_row_cannot_turn_it_on_for_a_model_that_rejects_points(self):
        assert _caches_prompt(NOVA, SimpleNamespace(supports_caching=True)) is False


class TestWithCachePoints:
    def test_multi_turn_gets_system_and_last_message_points(self):
        params = _with_cache_points(_build_converse_params(_request(TWO_TURNS)))

        assert params["system"] == [{"text": "You are a careful analyst."}, POINT]
        assert params["messages"][-1]["content"] == [{"text": "What is the per-diem cap?"}, POINT]
        assert all(POINT not in m["content"] for m in params["messages"][:-1])

    def test_single_message_gets_the_system_point_only(self):
        params = _with_cache_points(_build_converse_params(_request()))

        assert params["system"][-1] == POINT
        assert params["messages"] == [{"role": "user", "content": [{"text": "Hello"}]}]

    def test_no_system_prompt_adds_no_system_block(self):
        params = _with_cache_points(_build_converse_params(_request(TWO_TURNS, system_prompt=None)))

        assert "system" not in params
        assert params["messages"][-1]["content"][-1] == POINT

    def test_input_params_are_not_mutated(self):
        plain = _build_converse_params(_request(TWO_TURNS))
        snapshot = repr(plain)

        _with_cache_points(plain)

        assert repr(plain) == snapshot

    def test_fingerprints_still_match_the_next_turn(self):
        """The next turn's ``extendsMessagesHashes`` names this request's ``messagesHash``."""
        first = _prefix_fingerprints(_request(TWO_TURNS))
        follow_up = TWO_TURNS + [
            {"role": "assistant", "content": "$68 per day."},
            {"role": "user", "content": "And for international travel?"},
        ]
        second = _prefix_fingerprints(_request(follow_up))

        assert first["messagesHash"] in second["extendsMessagesHashes"]


# ---------------------------------------------------------------------------
# Route level: what actually reaches Bedrock
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def bedrock():
    fake = MagicMock()
    fake.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 2},
        "stopReason": "end_turn",
    }
    fake.converse_stream.return_value = {
        "stream": iter([
            {"messageStart": {"role": "assistant"}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}, "metrics": {}}},
        ])
    }
    with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
        yield fake


@pytest.fixture(autouse=True)
def route_stubs():
    role_service = MagicMock()
    role_service.can_access_model = AsyncMock(return_value=True)
    with patch(
        "apis.app_api.chat.converse_routes._validate_api_key",
        new_callable=AsyncMock,
        return_value=ValidatedApiKey(key_id="k", user_id="u", name="Key"),
    ), patch(
        "apis.app_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled",
        return_value=False,
    ), patch(
        "apis.app_api.chat.converse_routes.get_app_role_service", return_value=role_service,
    ), patch(
        "apis.app_api.chat.converse_routes.resolve_effective_model",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "apis.app_api.chat.converse_routes._resolve_model_routing",
        new_callable=AsyncMock,
        return_value=("bedrock", None, None),
    ), patch(
        "apis.app_api.chat.converse_routes._record_cost", new_callable=AsyncMock,
    ):
        yield


def _post(client, model_id, stream):
    body = {"model_id": model_id, "messages": TWO_TURNS, "system_prompt": "Be brief.", "stream": stream}
    response = client.post("/chat/api-converse", json=body, headers={"X-API-Key": "key"})
    assert response.status_code == 200, response.text
    return response


class TestRoute:
    @pytest.mark.parametrize("stream", [False, True])
    def test_claude_request_carries_both_points(self, client, bedrock, stream):
        _post(client, CLAUDE, stream)

        call = bedrock.converse_stream if stream else bedrock.converse
        params = call.call_args.kwargs
        assert params["system"][-1] == POINT
        assert params["messages"][-1]["content"][-1] == POINT

    @pytest.mark.parametrize("stream", [False, True])
    def test_other_model_request_carries_no_point(self, client, bedrock, stream):
        _post(client, NOVA, stream)

        call = bedrock.converse_stream if stream else bedrock.converse
        params = call.call_args.kwargs
        assert POINT not in params.get("system", [])
        assert all(POINT not in m["content"] for m in params["messages"])

    def test_catalog_row_with_caching_off_carries_no_point(self, client, bedrock):
        effective = SimpleNamespace(
            denied=False, redirected=False, record=SimpleNamespace(supports_caching=False)
        )
        with patch(
            "apis.app_api.chat.converse_routes.resolve_effective_model",
            new_callable=AsyncMock,
            return_value=effective,
        ):
            _post(client, CLAUDE, False)

        params = bedrock.converse.call_args.kwargs
        assert POINT not in params["system"]
        assert all(POINT not in m["content"] for m in params["messages"])
