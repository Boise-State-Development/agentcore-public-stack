"""Tests for the Bedrock Mantle path of app-api /chat/api-converse.

Mantle models (provider="mantle") don't speak Bedrock Converse — the handler
routes them through the shared Strands builder and invokes the bare model's
`.stream()`, which yields Converse-shaped events. These tests mock the shared
builder + routing so no network/AWS is touched.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.chat.converse_routes import router
from apis.shared.auth.api_keys.models import ValidatedApiKey
from apis.shared.models.mantle import MantleApiMode


VALID_KEY = "test-key"
MOCK_KEY = ValidatedApiKey(key_id="k1", user_id="u1", name="Test Key")

# Converse-shaped events, exactly what Strands' model.stream yields.
MANTLE_EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hello "}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "mantle"}}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {"messageStop": {"stopReason": "end_turn"}},
    {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 3}, "metrics": {}}},
]


class _FakeMantleModel:
    """Stand-in for a Strands OpenAIModel/OpenAIResponsesModel."""

    def __init__(self, events):
        self._events = events
        self.stream_calls = []

    def stream(self, messages, system_prompt=None, **kwargs):
        self.stream_calls.append({"messages": messages, "system_prompt": system_prompt})

        async def _gen():
            for event in self._events:
                yield event

        return _gen()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _role_service(can_access=True):
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=can_access)
    return svc


def _mantle_patches(routing, fake_model, record_cost):
    """Common patch stack for the mantle path."""
    return [
        patch("apis.app_api.chat.converse_routes._validate_api_key", AsyncMock(return_value=MOCK_KEY)),
        patch("apis.app_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled", return_value=False),
        patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=_role_service()),
        patch("apis.app_api.chat.converse_routes._resolve_model_routing", AsyncMock(return_value=routing)),
        patch("apis.app_api.chat.converse_routes.build_mantle_model", return_value=fake_model),
        patch("apis.app_api.chat.converse_routes._record_cost", record_cost),
    ]


class TestMantleNonStreaming:
    def test_returns_aggregated_text_and_records_mantle_cost(self):
        fake_model = _FakeMantleModel(MANTLE_EVENTS)
        record_cost = AsyncMock()
        patches = _mantle_patches(("mantle", "chat", "us-east-1"), fake_model, record_cost)
        for p in patches:
            p.start()
        try:
            resp = _client().post(
                "/chat/api-converse",
                headers={"X-API-Key": VALID_KEY},
                json={"model_id": "openai.gpt-oss-120b", "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "hello mantle"
        assert body["stop_reason"] == "end_turn"
        assert body["usage"] == {"inputTokens": 10, "outputTokens": 3}
        # Cost recorded against the mantle provider, not bedrock.
        record_cost.assert_awaited_once()
        assert record_cost.await_args.kwargs["provider"] == "mantle"

    def test_responses_mode_forwarded_to_builder(self):
        fake_model = _FakeMantleModel(MANTLE_EVENTS)
        build = MagicMock(return_value=fake_model)
        patches = [
            patch("apis.app_api.chat.converse_routes._validate_api_key", AsyncMock(return_value=MOCK_KEY)),
            patch("apis.app_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled", return_value=False),
            patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=_role_service()),
            patch("apis.app_api.chat.converse_routes._resolve_model_routing",
                  AsyncMock(return_value=("mantle", "responses", "us-east-1"))),
            patch("apis.app_api.chat.converse_routes.build_mantle_model", build),
            patch("apis.app_api.chat.converse_routes._record_cost", AsyncMock()),
        ]
        for p in patches:
            p.start()
        try:
            resp = _client().post(
                "/chat/api-converse",
                headers={"X-API-Key": VALID_KEY},
                json={"model_id": "openai.gpt-5.4", "max_tokens": 256,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200
        kwargs = build.call_args.kwargs
        assert kwargs["api_mode"] == MantleApiMode.RESPONSES
        assert kwargs["region"] == "us-east-1"
        # Responses API renames the output cap.
        assert kwargs["params"] == {"max_output_tokens": 256}


class TestMantleStreaming:
    def test_streams_sse_and_records_cost(self):
        fake_model = _FakeMantleModel(MANTLE_EVENTS)
        record_cost = AsyncMock()
        patches = _mantle_patches(("mantle", "chat", None), fake_model, record_cost)
        for p in patches:
            p.start()
        try:
            resp = _client().post(
                "/chat/api-converse",
                headers={"X-API-Key": VALID_KEY},
                json={"model_id": "openai.gpt-oss-120b", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]},
            )
            body = resp.text
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event: message_start" in body
        assert "event: content_block_delta" in body
        assert "event: metadata" in body
        assert "event: done" in body
        record_cost.assert_awaited_once()
        assert record_cost.await_args.kwargs["provider"] == "mantle"


class TestResolveModelRouting:
    @pytest.mark.asyncio
    async def test_returns_mantle_fields_for_mantle_model(self):
        from apis.app_api.chat.converse_routes import _resolve_model_routing

        m = MagicMock()
        m.model_id = "openai.gpt-5.4"
        m.provider = "mantle"
        m.mantle_api_mode = "responses"
        m.mantle_region = "us-east-1"
        with patch("apis.shared.models.managed_models.list_managed_models",
                   AsyncMock(return_value=[m])):
            provider, api_mode, region = await _resolve_model_routing("openai.gpt-5.4")
        assert (provider, api_mode, region) == ("mantle", "responses", "us-east-1")

    @pytest.mark.asyncio
    async def test_defaults_to_bedrock_when_not_found(self):
        from apis.app_api.chat.converse_routes import _resolve_model_routing

        with patch("apis.shared.models.managed_models.list_managed_models",
                   AsyncMock(return_value=[])):
            assert await _resolve_model_routing("unknown.model") == ("bedrock", None, None)

    @pytest.mark.asyncio
    async def test_defaults_to_bedrock_on_lookup_error(self):
        from apis.app_api.chat.converse_routes import _resolve_model_routing

        with patch("apis.shared.models.managed_models.list_managed_models",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            assert await _resolve_model_routing("openai.gpt-5.4") == ("bedrock", None, None)


class TestBedrockRuntimeResponsesRouting:
    """provider="bedrock-responses" rides the same handler, different builder.

    Everything downstream of model construction — SSE translation, usage and
    cost accounting — is shared with the Mantle path; only the transport
    differs. These pin that the two never cross.
    """

    def _patches(self, routing, build_runtime, build_mantle, record_cost):
        return [
            patch("apis.app_api.chat.converse_routes._validate_api_key", AsyncMock(return_value=MOCK_KEY)),
            patch("apis.app_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled", return_value=False),
            patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=_role_service()),
            patch("apis.app_api.chat.converse_routes._resolve_model_routing", AsyncMock(return_value=routing)),
            patch("apis.app_api.chat.converse_routes.build_bedrock_responses_model", build_runtime),
            patch("apis.app_api.chat.converse_routes.build_mantle_model", build_mantle),
            patch("apis.app_api.chat.converse_routes._record_cost", record_cost),
        ]

    def _post(self, patches, payload):
        for p in patches:
            p.start()
        try:
            return _client().post(
                "/chat/api-converse",
                headers={"X-API-Key": VALID_KEY},
                json=payload,
            )
        finally:
            for p in patches:
                p.stop()

    def test_builds_via_the_runtime_transport_not_mantle(self):
        build_runtime = MagicMock(return_value=_FakeMantleModel(MANTLE_EVENTS))
        build_mantle = MagicMock()
        record_cost = AsyncMock()
        resp = self._post(
            self._patches(
                ("bedrock-responses", "responses", "us-west-2"),
                build_runtime, build_mantle, record_cost,
            ),
            {"model_id": "us.openai.gpt-5.6-sol", "max_tokens": 256,
             "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        assert resp.json()["content"] == "hello mantle"
        build_mantle.assert_not_called()
        kwargs = build_runtime.call_args.kwargs
        assert kwargs["model_id"] == "us.openai.gpt-5.6-sol"
        assert kwargs["region"] == "us-west-2"
        # Responses API renames the output cap; no api_mode on this transport.
        assert kwargs["params"] == {"max_output_tokens": 256}
        assert "api_mode" not in kwargs

    def test_records_cost_against_the_right_provider(self):
        record_cost = AsyncMock()
        resp = self._post(
            self._patches(
                ("bedrock-responses", "responses", None),
                MagicMock(return_value=_FakeMantleModel(MANTLE_EVENTS)),
                MagicMock(), record_cost,
            ),
            {"model_id": "us.openai.gpt-5.6-sol",
             "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        record_cost.assert_awaited_once()
        assert record_cost.await_args.kwargs["provider"] == "bedrock-responses"

    def test_a_stored_chat_api_mode_cannot_downgrade_the_surface(self):
        """A legacy row saying 'chat' must not cost us prompt caching."""
        build_runtime = MagicMock(return_value=_FakeMantleModel(MANTLE_EVENTS))
        resp = self._post(
            self._patches(
                ("bedrock-responses", "chat", "us-west-2"),
                build_runtime, MagicMock(), AsyncMock(),
            ),
            {"model_id": "us.openai.gpt-5.6-sol", "max_tokens": 256,
             "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        # Responses-native name proves the surface stayed on Responses.
        assert build_runtime.call_args.kwargs["params"] == {"max_output_tokens": 256}

    def test_streams_sse_like_the_mantle_path(self):
        record_cost = AsyncMock()
        resp = self._post(
            self._patches(
                ("bedrock-responses", "responses", "us-west-2"),
                MagicMock(return_value=_FakeMantleModel(MANTLE_EVENTS)),
                MagicMock(), record_cost,
            ),
            {"model_id": "us.openai.gpt-5.6-sol", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert "event: message_start" in resp.text
        assert "event: done" in resp.text
        assert record_cost.await_args.kwargs["provider"] == "bedrock-responses"

    def test_bedrock_models_still_take_the_converse_path(self):
        """Regression guard: the new branch must not capture plain Bedrock."""
        build_runtime = MagicMock()
        build_mantle = MagicMock()
        boto_client = MagicMock()
        boto_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello bedrock"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 3},
            "stopReason": "end_turn",
        }
        patches = self._patches(
            ("bedrock", None, None), build_runtime, build_mantle, AsyncMock()
        )
        patches.append(
            patch("apis.app_api.chat.converse_routes._get_bedrock_client",
                  return_value=boto_client)
        )
        resp = self._post(
            patches,
            {"model_id": "us.anthropic.claude-haiku-4-5",
             "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        assert resp.json()["content"] == "hello bedrock"
        boto_client.converse.assert_called_once()
        build_runtime.assert_not_called()
        build_mantle.assert_not_called()
