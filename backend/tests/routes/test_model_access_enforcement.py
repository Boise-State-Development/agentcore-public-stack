"""Unit tests for model access enforcement on inference endpoints.

Verifies that the inline ``AppRoleService.can_access_model()`` check in
``POST /invocations`` and ``POST /chat/api-converse`` correctly:
- Returns 403 for unauthorized model access
- Allows authorized requests to proceed
- Skips the check for null/empty model_id (invocations only)
- Respects wildcard ``"*"`` access
- Preserves quota and rate-limit precedence

Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.5, 3.6
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user() -> User:
    return User(
        email="test@example.com",
        user_id="user-001",
        name="Test User",
        roles=["User"],
        raw_token="fake-jwt-token",
    )


def _make_app_role_service(can_access: bool) -> MagicMock:
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=can_access)
    return svc


def _make_mock_agent():
    agent = MagicMock()

    async def fake_stream(*a, **kw):
        yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
        yield 'event: content_block_start\ndata: {"contentBlockIndex": 0, "type": "text"}\n\n'
        yield 'event: content_block_delta\ndata: {"contentBlockIndex": 0, "type": "text", "text": "Hi"}\n\n'
        yield 'event: content_block_stop\ndata: {"contentBlockIndex": 0}\n\n'
        yield 'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    return agent


def _make_validated_key():
    key = MagicMock()
    key.user_id = "user-001"
    key.key_id = "key-001"
    key.name = "Test Key"
    return key


def _make_bedrock_client():
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hello"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
        "stopReason": "end_turn",
    }
    return client


def _make_rate_limiter(allowed: bool = True):
    limiter = MagicMock()
    limiter.check_rate_limit = AsyncMock(return_value=allowed)
    return limiter


# ===================================================================
# Invocations endpoint tests
# ===================================================================

class TestInvocationsAccessDenied:
    """POST /invocations returns 403 when can_access_model is False.

    Requirements: 1.1, 2.1
    """

    def test_returns_403_for_unauthorized_model(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        mock_svc = _make_app_role_service(can_access=False)

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": "restricted-model",
                },
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"] == "Access denied to model: restricted-model"


class TestInvocationsAccessAllowed:
    """POST /invocations proceeds (200 streaming) when can_access_model is True.

    Requirements: 3.1
    """

    def test_returns_200_streaming_for_authorized_model(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        mock_svc = _make_app_role_service(can_access=True)
        mock_agent = _make_mock_agent()

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes._resolve_caching_enabled",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": "allowed-model",
                },
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


class TestInvocationsNullEmptyModelId:
    """POST /invocations skips access check when model_id is None or empty.

    Requirements: 2.3
    """

    def test_none_model_id_skips_access_check(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        mock_svc = _make_app_role_service(can_access=False)
        mock_agent = _make_mock_agent()

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes._resolve_caching_enabled",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": None,
                },
            )

        assert resp.status_code != 403
        mock_svc.can_access_model.assert_not_called()

    def test_empty_model_id_skips_access_check(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        mock_svc = _make_app_role_service(can_access=False)
        mock_agent = _make_mock_agent()

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes._resolve_caching_enabled",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": "",
                },
            )

        assert resp.status_code != 403
        mock_svc.can_access_model.assert_not_called()


class TestInvocationsWildcardAccess:
    """POST /invocations allows any model when wildcard access is granted.

    Requirements: 3.3
    """

    def test_wildcard_access_allows_any_model(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        # Wildcard is handled inside the service — it returns True
        mock_svc = _make_app_role_service(can_access=True)
        mock_agent = _make_mock_agent()

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.routes.get_agent",
            return_value=mock_agent,
        ), patch(
            "apis.inference_api.chat.routes._resolve_caching_enabled",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": "any-model-at-all",
                },
            )

        assert resp.status_code != 403
        mock_svc.can_access_model.assert_called_once()


class TestInvocationsQuotaPrecedence:
    """Quota exceeded takes precedence over model access denial on /invocations.

    Requirements: 3.5
    """

    def test_quota_exceeded_returns_200_streaming_not_403(self):
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user_trusted] = _make_user

        mock_svc = _make_app_role_service(can_access=False)

        mock_tier = MagicMock()
        mock_tier.period_type = "monthly"
        mock_tier.tier_name = "basic"

        mock_quota_result = MagicMock()
        mock_quota_result.allowed = False
        mock_quota_result.message = "Quota exceeded"
        mock_quota_result.tier = mock_tier
        mock_quota_result.quota_limit = 100
        mock_quota_result.current_usage = 101
        mock_quota_result.percentage_used = 101.0
        mock_quota_result.warning_level = None

        mock_quota_checker = MagicMock()
        mock_quota_checker.check_quota = AsyncMock(return_value=mock_quota_result)

        with patch(
            "apis.inference_api.chat.routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.inference_api.chat.routes.is_quota_enforcement_enabled",
            return_value=True,
        ), patch(
            "apis.inference_api.chat.routes.get_quota_checker",
            return_value=mock_quota_checker,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/invocations",
                json={
                    "session_id": "test-session",
                    "message": "hi",
                    "model_id": "restricted-model",
                },
            )

        # Quota exceeded streams as 200 SSE (not 403)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "quota" in resp.text.lower()


# ===================================================================
# Api-converse endpoint tests
# ===================================================================


class TestApiConverseAccessDenied:
    """POST /chat/api-converse returns 403 when can_access_model is False.

    Requirements: 1.2, 2.2
    """

    def test_returns_403_for_unauthorized_model(self):
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=False)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=True)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=False,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "restricted-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"] == "Access denied to model: restricted-model"

    def test_403_response_is_json_not_sse(self):
        """403 response body is proper JSON error, not an SSE stream.

        Requirements: 2.2
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=False)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=True)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=False,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "restricted-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 403
        assert "application/json" in resp.headers.get("content-type", "")
        # Verify it parses as JSON with expected structure
        body = resp.json()
        assert "detail" in body


class TestApiConverseAccessAllowed:
    """POST /chat/api-converse proceeds (200) when can_access_model is True.

    Requirements: 3.2
    """

    def test_returns_200_for_authorized_model(self):
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=True)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=True)
        mock_bedrock = _make_bedrock_client()

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=mock_bedrock,
        ), patch(
            "apis.inference_api.chat.converse_routes._record_cost",
            new_callable=AsyncMock,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "allowed-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 200


class TestApiConverseWildcardAccess:
    """POST /chat/api-converse allows any model with wildcard access.

    Requirements: 3.3
    """

    def test_wildcard_access_allows_any_model(self):
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=True)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=True)
        mock_bedrock = _make_bedrock_client()

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=False,
        ), patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=mock_bedrock,
        ), patch(
            "apis.inference_api.chat.converse_routes._record_cost",
            new_callable=AsyncMock,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "any-model-at-all",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code != 403
        mock_svc.can_access_model.assert_called_once()


class TestApiConverseRateLimitPrecedence:
    """Rate limit exceeded takes precedence over model access denial.

    Requirements: 3.6
    """

    def test_rate_limit_returns_429_not_403(self):
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=False)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=False)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=False,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "restricted-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 429


class TestApiConverseQuotaPrecedence:
    """Quota exceeded takes precedence over model access denial on api-converse.

    Requirements: 3.5
    """

    def test_quota_exceeded_returns_429_not_403(self):
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_app_role_service(can_access=False)
        mock_key = _make_validated_key()
        mock_limiter = _make_rate_limiter(allowed=True)

        mock_quota_result = MagicMock()
        mock_quota_result.allowed = False
        mock_quota_result.message = "Quota exceeded"
        mock_quota_result.quota_limit = 100

        mock_quota_checker = MagicMock()
        mock_quota_checker.check_quota = AsyncMock(return_value=mock_quota_result)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_key,
        ), patch(
            "apis.inference_api.chat.converse_routes.get_app_role_service",
            return_value=mock_svc,
        ), patch(
            "apis.shared.rate_limit.get_rate_limiter",
            return_value=mock_limiter,
        ), patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=True,
        ), patch(
            "apis.shared.quota.get_quota_checker",
            return_value=mock_quota_checker,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/chat/api-converse",
                headers={"X-API-Key": "test-api-key-123"},
                json={
                    "model_id": "restricted-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 429
