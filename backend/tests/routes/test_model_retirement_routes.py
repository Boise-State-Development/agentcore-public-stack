"""Model retirement at the runtime entry points — docs/specs/model-retirement.md §7.

A retired model is resolved *before* the grant-only access check: a redirect is
access-checked (and would be invoked) as its successor, and a retired model with
no successor is refused for everyone — as a conversational message on
``/invocations``, as 410 Gone on the API-key route.
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.models.retirement import resolve_from_catalog

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _row(model_id: str, *, status="active", replaced_by=None, name=None) -> ManagedModel:
    return ManagedModel(
        id=f"uuid-{model_id}", modelId=model_id, modelName=name or model_id, provider="bedrock",
        providerName="Amazon Bedrock", inputModalities=["text"], outputModalities=["text"],
        maxInputTokens=200000, enabled=True, inputPricePerMillionTokens=1.0,
        outputPricePerMillionTokens=5.0, status=status, replacedBy=replaced_by,
        createdAt=NOW, updatedAt=NOW,
    )


CATALOG = [
    _row("old-redirected", status="retired", replaced_by="successor"),
    _row("successor"),
    _row("old-gone", status="retired", name="Claude Gone"),
]


async def _resolve(model_id):
    return resolve_from_catalog(model_id, CATALOG) if model_id else None


def _user() -> User:
    return User(email="test@example.com", user_id="user-001", name="Test User", roles=["User"], raw_token="t")


def _role_service(can_access: bool) -> MagicMock:
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=can_access)
    return svc


def _invocations_app() -> FastAPI:
    from apis.inference_api.chat.routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_trusted] = _user
    return app


class TestInvocations:
    def test_redirect_is_access_checked_as_the_successor(self):
        svc = _role_service(can_access=False)
        with patch("apis.inference_api.chat.routes.resolve_effective_model", _resolve), \
             patch("apis.inference_api.chat.routes.get_app_role_service", return_value=svc), \
             patch("apis.inference_api.chat.routes.is_quota_enforcement_enabled", return_value=False):
            resp = TestClient(_invocations_app(), raise_server_exceptions=False).post(
                "/invocations",
                json={"session_id": "s-1", "message": "hi", "model_id": "old-redirected", "provider": "bedrock"},
            )

        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied to model: successor"

    def test_retired_without_successor_streams_a_message_even_for_a_wildcard_holder(self):
        svc = _role_service(can_access=True)
        with patch("apis.inference_api.chat.routes.resolve_effective_model", _resolve), \
             patch("apis.inference_api.chat.routes.get_app_role_service", return_value=svc), \
             patch("apis.inference_api.chat.routes.is_quota_enforcement_enabled", return_value=False):
            resp = TestClient(_invocations_app(), raise_server_exceptions=False).post(
                "/invocations",
                json={"session_id": "s-1", "message": "hi", "model_id": "old-gone"},
            )

        # Errors stream as assistant messages, never a bare HTTP error.
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "Claude Gone" in resp.text and "has been retired" in resp.text
        svc.can_access_model.assert_not_awaited()


class TestSavedDefault:
    @pytest.mark.asyncio
    async def test_saved_default_follows_the_successor(self):
        from apis.inference_api.chat import routes

        repo = MagicMock(enabled=True)
        repo.get_settings = AsyncMock(return_value={"defaultModelId": "old-redirected"})
        with patch.object(routes, "UserSettingsRepository", return_value=repo), \
             patch.object(routes, "resolve_effective_model", _resolve):
            assert await routes._resolve_user_default_model("user-001") == ("successor", "bedrock")

    @pytest.mark.asyncio
    async def test_saved_default_without_successor_reads_as_unset(self):
        from apis.inference_api.chat import routes

        repo = MagicMock(enabled=True)
        repo.get_settings = AsyncMock(return_value={"defaultModelId": "old-gone"})
        with patch.object(routes, "UserSettingsRepository", return_value=repo), \
             patch.object(routes, "resolve_effective_model", _resolve):
            assert await routes._resolve_user_default_model("user-001") == (None, None)


class TestApiConverse:
    def _post(self, model_id: str, svc: MagicMock):
        from apis.app_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)
        key = MagicMock(user_id="user-001", key_id="key-001")
        key.name = "Test Key"
        limiter = MagicMock()
        limiter.check_rate_limit = AsyncMock(return_value=True)
        with patch("apis.app_api.chat.converse_routes._validate_api_key", new_callable=AsyncMock, return_value=key), \
             patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=svc), \
             patch("apis.app_api.chat.converse_routes.resolve_effective_model", _resolve), \
             patch("apis.shared.rate_limit.get_rate_limiter", return_value=limiter), \
             patch("apis.shared.quota.is_quota_enforcement_enabled", return_value=False):
            return TestClient(app, raise_server_exceptions=False).post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={"model_id": model_id, "messages": [{"role": "user", "content": "hi"}]},
            )

    def test_redirect_is_access_checked_as_the_successor(self):
        resp = self._post("old-redirected", _role_service(can_access=False))
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied to model: successor"

    def test_retired_without_successor_is_gone(self):
        svc = _role_service(can_access=True)
        resp = self._post("old-gone", svc)
        assert resp.status_code == 410
        assert "has been retired" in resp.json()["detail"]
        svc.can_access_model.assert_not_awaited()
