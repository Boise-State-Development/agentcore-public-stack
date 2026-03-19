"""Property-based preservation tests for model access enforcement.

**Property 2: Preservation** — Authorized Model Access Proceeds Normally

These tests observe baseline behavior on UNFIXED code and confirm that
authorized requests, wildcard access, null/empty model_id passthrough,
quota precedence, and rate-limit precedence all work as expected.

All tests MUST PASS on unfixed code (confirming behavior to preserve).

**Validates: Requirements 2.3, 3.1, 3.2, 3.3, 3.5, 3.6**
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

model_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=60,
).filter(lambda s: s.strip() != "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_user() -> User:
    return User(
        email="test@example.com",
        user_id="user-001",
        name="Test User",
        roles=["User"],
        raw_token="fake-jwt-token",
    )


def _make_mock_app_role_service(can_access: bool) -> MagicMock:
    """Return a mock AppRoleService where can_access_model returns *can_access*."""
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=can_access)
    return svc


def _make_mock_agent():
    """Return a mock agent with a fake streaming response."""
    mock_agent = MagicMock()

    async def fake_stream(*a, **kw):
        yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
        yield 'event: content_block_start\ndata: {"contentBlockIndex": 0, "type": "text"}\n\n'
        yield 'event: content_block_delta\ndata: {"contentBlockIndex": 0, "type": "text", "text": "Hello"}\n\n'
        yield 'event: content_block_stop\ndata: {"contentBlockIndex": 0}\n\n'
        yield 'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
        yield "event: done\ndata: {}\n\n"

    mock_agent.stream_async = fake_stream
    return mock_agent


def _make_mock_validated_key():
    """Return a mock ValidatedApiKey."""
    key = MagicMock()
    key.user_id = "user-001"
    key.key_id = "key-001"
    key.name = "Test Key"
    return key


def _make_mock_bedrock_client():
    """Return a mock Bedrock client with a valid converse response."""
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hello"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
        "stopReason": "end_turn",
    }
    return client


def _make_mock_rate_limiter(allowed: bool = True):
    """Return a mock rate limiter."""
    limiter = MagicMock()
    limiter.check_rate_limit = AsyncMock(return_value=allowed)
    return limiter


# ---------------------------------------------------------------------------
# Property 2a — Authorized access (invocations)
# ---------------------------------------------------------------------------

class TestAuthorizedAccessInvocations:
    """Preservation: authorized model access proceeds to agent creation.

    **Validates: Requirements 3.1**
    """

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_authorized_model_proceeds_invocations(self, model_id: str):
        """For all model_id strings, when can_access_model returns True,
        the endpoint proceeds to agent creation (status != 403).

        **Validates: Requirements 3.1**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        mock_svc = _make_mock_app_role_service(can_access=True)
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
                    "model_id": model_id,
                },
            )

        assert resp.status_code != 403, (
            f"Authorized model_id={model_id!r} should NOT get 403, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Property 2b — Authorized access (api-converse)
# ---------------------------------------------------------------------------

class TestAuthorizedAccessApiConverse:
    """Preservation: authorized model access proceeds to Bedrock.

    **Validates: Requirements 3.2**
    """

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_authorized_model_proceeds_api_converse(self, model_id: str):
        """For all model_id strings, when can_access_model returns True,
        the endpoint proceeds to Bedrock (status != 403).

        **Validates: Requirements 3.2**
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_mock_app_role_service(can_access=True)
        mock_validated_key = _make_mock_validated_key()
        mock_limiter = _make_mock_rate_limiter(allowed=True)
        mock_bedrock = _make_mock_bedrock_client()

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_validated_key,
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
                    "model_id": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code != 403, (
            f"Authorized model_id={model_id!r} should NOT get 403, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Property 2c — Wildcard access
# ---------------------------------------------------------------------------

class TestWildcardAccess:
    """Preservation: wildcard '*' model access allows any model on both endpoints.

    **Validates: Requirements 3.3**
    """

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_wildcard_access_invocations(self, model_id: str):
        """When user permissions include '*' in models, invocations proceeds.

        **Validates: Requirements 3.3**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        # Wildcard means can_access_model returns True for any model
        mock_svc = _make_mock_app_role_service(can_access=True)
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
                    "model_id": model_id,
                },
            )

        assert resp.status_code != 403, (
            f"Wildcard access model_id={model_id!r} should NOT get 403, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_wildcard_access_api_converse(self, model_id: str):
        """When user permissions include '*' in models, api-converse proceeds.

        **Validates: Requirements 3.3**
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_mock_app_role_service(can_access=True)
        mock_validated_key = _make_mock_validated_key()
        mock_limiter = _make_mock_rate_limiter(allowed=True)
        mock_bedrock = _make_mock_bedrock_client()

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_validated_key,
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
                    "model_id": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code != 403, (
            f"Wildcard access model_id={model_id!r} should NOT get 403, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Property 2d — Null/empty model_id passthrough (invocations only)
# ---------------------------------------------------------------------------

class TestNullEmptyModelIdPassthrough:
    """Preservation: null or empty model_id skips access check entirely.

    **Validates: Requirements 2.3**
    """

    def test_none_model_id_proceeds_without_access_check(self):
        """When model_id is None, the endpoint proceeds without calling
        can_access_model at all.

        **Validates: Requirements 2.3**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        mock_svc = _make_mock_app_role_service(can_access=False)
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

        assert resp.status_code != 403, (
            f"None model_id should NOT get 403, got {resp.status_code}"
        )
        # can_access_model should NOT have been called
        mock_svc.can_access_model.assert_not_called()

    def test_empty_model_id_proceeds_without_access_check(self):
        """When model_id is '', the endpoint proceeds without calling
        can_access_model at all.

        **Validates: Requirements 2.3**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        mock_svc = _make_mock_app_role_service(can_access=False)
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

        assert resp.status_code != 403, (
            f"Empty model_id should NOT get 403, got {resp.status_code}"
        )
        # can_access_model should NOT have been called
        mock_svc.can_access_model.assert_not_called()


# ---------------------------------------------------------------------------
# Property 2e — Quota precedence
# ---------------------------------------------------------------------------

class TestQuotaPrecedence:
    """Preservation: quota exceeded returns 429 regardless of model authorization.

    For the invocations endpoint, quota exceeded is streamed as a 200 SSE
    response (better UX). For api-converse, it raises HTTP 429.

    **Validates: Requirements 3.5**
    """

    @given(model_authorized=st.booleans())
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_quota_exceeded_takes_precedence_invocations(self, model_authorized: bool):
        """When quota is exceeded on invocations, the response is a 200
        streaming quota message regardless of model authorization.
        (Quota check runs before model access check.)

        **Validates: Requirements 3.5**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        mock_svc = _make_mock_app_role_service(can_access=model_authorized)

        # Mock quota checker that says quota is exceeded
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
                    "model_id": "some-model",
                },
            )

        # Invocations streams quota exceeded as 200 SSE (not 403)
        assert resp.status_code == 200, (
            f"Quota exceeded should return 200 streaming, got {resp.status_code}"
        )
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "quota_exceeded" in resp.text or "quota" in resp.text.lower()

    @given(model_authorized=st.booleans())
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_quota_exceeded_takes_precedence_api_converse(self, model_authorized: bool):
        """When quota is exceeded on api-converse, the response is 429
        regardless of model authorization.

        **Validates: Requirements 3.5**
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_mock_app_role_service(can_access=model_authorized)
        mock_validated_key = _make_mock_validated_key()
        mock_limiter = _make_mock_rate_limiter(allowed=True)

        # Mock quota checker that says quota is exceeded
        mock_quota_result = MagicMock()
        mock_quota_result.allowed = False
        mock_quota_result.message = "Quota exceeded"
        mock_quota_result.quota_limit = 100

        mock_quota_checker = MagicMock()
        mock_quota_checker.check_quota = AsyncMock(return_value=mock_quota_result)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_validated_key,
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
                    "model_id": "some-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 429, (
            f"Quota exceeded should return 429, got {resp.status_code}: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Property 2f — Rate limit precedence (api-converse)
# ---------------------------------------------------------------------------

class TestRateLimitPrecedence:
    """Preservation: rate limited returns 429 regardless of model authorization.

    **Validates: Requirements 3.6**
    """

    @given(model_authorized=st.booleans())
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_rate_limit_takes_precedence_api_converse(self, model_authorized: bool):
        """When rate limited on api-converse, the response is 429
        regardless of model authorization.

        **Validates: Requirements 3.6**
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_mock_app_role_service(can_access=model_authorized)
        mock_validated_key = _make_mock_validated_key()
        # Rate limiter denies the request
        mock_limiter = _make_mock_rate_limiter(allowed=False)

        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            return_value=mock_validated_key,
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
                    "model_id": "some-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert resp.status_code == 429, (
            f"Rate limited should return 429, got {resp.status_code}: {resp.text[:200]}"
        )
