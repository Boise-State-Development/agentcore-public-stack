"""Property-based exploration test for model access enforcement bug.

**Property 1: Bug Condition** — Unauthorized Model Access Returns 403

For any request to `/invocations` or `/chat/api-converse` where the user's
AppRole permissions do NOT include the requested `model_id` (and `model_id`
is non-null/non-empty), the endpoint SHALL return HTTP 403 Forbidden with
detail "Access denied to model: {model_id}".

This test is EXPECTED TO FAIL on unfixed code — failure confirms the bug
exists (endpoints proceed to Bedrock instead of returning 403).

**Validates: Requirements 1.1, 1.2, 2.1, 2.2**
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
# Strategy: non-empty printable model_id strings (1-60 chars)
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


# ---------------------------------------------------------------------------
# Property 1 — /invocations endpoint
# ---------------------------------------------------------------------------

class TestInvocationsUnauthorizedModel:
    """Bug condition: POST /invocations with unauthorized model_id should 403.

    **Validates: Requirements 1.1, 2.1**
    """

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_invocations_returns_403_for_unauthorized_model(self, model_id: str):
        """Property 1 (invocations): Unauthorized model access returns 403.

        **Validates: Requirements 1.1, 2.1**
        """
        from apis.inference_api.chat.routes import router

        app = FastAPI()
        app.include_router(router)

        # Override auth to return a mock user
        mock_user = _make_mock_user()
        app.dependency_overrides[get_current_user_trusted] = lambda: mock_user

        mock_svc = _make_mock_app_role_service(can_access=False)

        # Mock agent to avoid AWS calls (the unfixed code reaches get_agent)
        mock_agent = MagicMock()

        async def fake_stream(*a, **kw):
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            yield "event: done\ndata: {}\n\n"

        mock_agent.stream_async = fake_stream

        # Patch where the function is looked up in the route module
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

        assert resp.status_code == 403, (
            f"Expected 403 for unauthorized model_id={model_id!r}, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )
        assert "Access denied to model" in resp.text, (
            f"Expected 'Access denied to model' in response body, got: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Property 1 — /chat/api-converse endpoint
# ---------------------------------------------------------------------------

class TestApiConverseUnauthorizedModel:
    """Bug condition: POST /chat/api-converse with unauthorized model_id should 403.

    **Validates: Requirements 1.2, 2.2**
    """

    @given(model_id=model_id_strategy)
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_api_converse_returns_403_for_unauthorized_model(self, model_id: str):
        """Property 1 (api-converse): Unauthorized model access returns 403.

        **Validates: Requirements 1.2, 2.2**
        """
        from apis.inference_api.chat.converse_routes import router

        app = FastAPI()
        app.include_router(router)

        mock_svc = _make_mock_app_role_service(can_access=False)

        # Mock ValidatedApiKey
        mock_validated_key = MagicMock()
        mock_validated_key.user_id = "user-001"
        mock_validated_key.key_id = "key-001"
        mock_validated_key.name = "Test Key"

        # Mock rate limiter that allows all requests
        mock_limiter = MagicMock()
        mock_limiter.check_rate_limit = AsyncMock(return_value=True)

        # Mock Bedrock client to avoid real AWS calls
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }

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

        assert resp.status_code == 403, (
            f"Expected 403 for unauthorized model_id={model_id!r}, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )
        assert "Access denied to model" in resp.text, (
            f"Expected 'Access denied to model' in response body, got: {resp.text[:200]}"
        )
