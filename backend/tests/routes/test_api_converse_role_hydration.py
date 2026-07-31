"""Regression tests: /chat/api-converse resolves the key owner's REAL roles.

An API key record stores only ``key_id``/``user_id``/``name`` — never roles.
The handler previously synthesized ``roles=["user"]``, a JWT role no AppRole
maps, so permission resolution matched nothing, fell back to the ``default``
role (which grants no models in prod), and every request 403'd regardless of
the owner's actual grants.

These tests pin the contract: the roles handed to RBAC come from the Users
table row, and a key with no profile row is refused rather than silently
degraded to ``default``.
"""

import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.users import UserProfile


def _make_validated_key():
    key = MagicMock()
    key.user_id = "user-001"
    key.key_id = "key-001"
    key.name = "Model Traning"
    return key


def _make_profile(roles):
    return UserProfile(
        userId="user-001",
        email="ravitejaseera@example.edu",
        name="Ravi Teja Seera",
        roles=roles,
        emailDomain="example.edu",
        createdAt="2026-01-01T00:00:00Z",
        lastLoginAt="2026-01-01T00:00:00Z",
    )


def _make_repo(profile):
    repo = AsyncMock()
    repo.get_user = AsyncMock(return_value=profile)
    return repo


def _make_app_role_service(can_access=True):
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=can_access)
    return svc


def _post(app_role_svc, repo):
    """Drive one api-converse request, returning (response, patched svc)."""
    from apis.app_api.chat.converse_routes import router

    app = FastAPI()
    app.include_router(router)

    limiter = MagicMock()
    limiter.check_rate_limit = AsyncMock(return_value=True)

    bedrock = MagicMock()
    bedrock.converse.return_value = {
        "output": {"message": {"content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "stopReason": "end_turn",
    }

    with patch(
        "apis.app_api.chat.converse_routes._validate_api_key",
        new_callable=AsyncMock,
        return_value=_make_validated_key(),
    ), patch(
        "apis.app_api.chat.converse_routes.get_user_repository",
        return_value=repo,
    ), patch(
        "apis.app_api.chat.converse_routes.get_app_role_service",
        return_value=app_role_svc,
    ), patch(
        "apis.app_api.chat.converse_routes._get_bedrock_client",
        return_value=bedrock,
    ), patch(
        "apis.app_api.chat.converse_routes._record_cost",
        new_callable=AsyncMock,
    ), patch(
        "apis.shared.rate_limit.get_rate_limiter",
        return_value=limiter,
    ), patch(
        "apis.shared.quota.is_quota_enforcement_enabled",
        return_value=False,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        return client.post(
            "/chat/api-converse",
            headers={"X-API-Key": "test-api-key-123"},
            json={
                "model_id": "global.anthropic.claude-sonnet-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )


class TestRolesComeFromTheUsersTable:
    def test_stored_roles_are_passed_to_rbac(self):
        """The User handed to can_access_model carries the stored IdP roles."""
        svc = _make_app_role_service(can_access=True)
        repo = _make_repo(_make_profile(["Staff", "OFFICE365"]))

        resp = _post(svc, repo)

        assert resp.status_code == 200
        svc.can_access_model.assert_awaited_once()
        user_arg = svc.can_access_model.await_args.args[0]
        assert user_arg.roles == ["Staff", "OFFICE365"]

    def test_placeholder_role_is_never_synthesized(self):
        """Guards the exact regression: roles=["user"] reaching RBAC."""
        svc = _make_app_role_service(can_access=True)
        repo = _make_repo(_make_profile(["Staff"]))

        _post(svc, repo)

        user_arg = svc.can_access_model.await_args.args[0]
        assert "user" not in user_arg.roles

    def test_profile_identity_replaces_synthetic_email(self):
        """Email/name come from the profile, not f"{user_id}@api-key"."""
        svc = _make_app_role_service(can_access=True)
        repo = _make_repo(_make_profile(["Staff"]))

        _post(svc, repo)

        user_arg = svc.can_access_model.await_args.args[0]
        assert user_arg.email == "ravitejaseera@example.edu"
        assert user_arg.name == "Ravi Teja Seera"
        assert user_arg.user_id == "user-001"

    def test_repo_is_queried_by_the_keys_user_id(self):
        svc = _make_app_role_service(can_access=True)
        repo = _make_repo(_make_profile(["Staff"]))

        _post(svc, repo)

        repo.get_user.assert_awaited_once_with("user-001")


class TestFailsClosed:
    def test_missing_profile_returns_401_not_default_role(self):
        """A key whose owner has no profile row is refused outright.

        Falling through to ``default`` would be the old bug in reverse:
        a silent, wrong-permissions request instead of a clear failure.
        """
        svc = _make_app_role_service(can_access=True)
        repo = _make_repo(None)

        resp = _post(svc, repo)

        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or expired API key"
        svc.can_access_model.assert_not_called()

    def test_unconfigured_repository_returns_401(self):
        """No Users table configured → refuse, don't degrade."""
        svc = _make_app_role_service(can_access=True)

        resp = _post(svc, None)

        assert resp.status_code == 401
        svc.can_access_model.assert_not_called()
