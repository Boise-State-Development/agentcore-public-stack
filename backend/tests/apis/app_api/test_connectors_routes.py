"""Route-level tests for the app-API connectors endpoints.

Covers `complete-consent` (forwards to AgentCore, surfaces errors), and
the side-effect-free `GET /{provider_id}/status`.

External boundaries (AgentCore control-plane client, identity client,
provider repository, role service) are patched — we test our gating and
response shape, not the downstream calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.connectors import routes
from apis.shared.auth.models import User
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    TokenResult,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.disconnect_repository import get_disconnect_repository
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import get_provider_repository
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import get_app_role_service


@pytest.fixture(autouse=True)
def _reset_control_client():
    """`_agentcore_control_client` is `lru_cache`d; reset between tests."""
    routes._agentcore_control_client.cache_clear()
    yield
    routes._agentcore_control_client.cache_clear()


def _make_user(user_id: str) -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.capitalize(),
        roles=[],
        raw_token="test-token",
    )


@pytest.fixture
def app_for_user():
    """Build a minimal FastAPI app with the connectors router mounted and
    the `get_current_user` dependency stubbed to a specific user.
    Returns a factory so each test picks the caller's identity.
    """

    def _build(user_id: str) -> FastAPI:
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user] = lambda: _make_user(user_id)
        return app

    return _build


class TestCompleteConsent:
    """`complete-consent` is a thin wrapper around AgentCore's
    `CompleteResourceTokenAuth`. The auth boundary is `current_user`
    (verified by `get_current_user`) — we forward that identity
    as `userIdentifier` and AgentCore's own binding rejects mismatches.
    """

    def test_forwards_caller_identity_to_agentcore(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_client.complete_resource_token_auth.assert_called_once_with(
            userIdentifier={"userId": "alice"},
            sessionUri="uri-abc",
        )

    def test_surfaces_agentcore_error_as_502(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        mock_client.complete_resource_token_auth.side_effect = RuntimeError("agentcore down")
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 502
        assert "agentcore down" in response.json()["detail"]

    @pytest.mark.parametrize(
        "bad_provider_id",
        [
            "google\nFAKE LOG ENTRY",  # newline → log injection
            "google\rINJECT",  # carriage return → log injection
            "Google",  # uppercase rejected by [a-z0-9-]
            "google!",  # punctuation rejected
            "g" * 65,  # over max_length
            "",  # empty
        ],
    )
    def test_rejects_malformed_provider_id_with_422(
        self, app_for_user, monkeypatch, bad_provider_id
    ):
        # The provider_id field is echoed into log lines on success and
        # failure paths. Constraining it at the request boundary prevents
        # CWE-117 log injection from authenticated callers.
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": bad_provider_id},
        )

        assert response.status_code == 422
        # The downstream call must not have fired — Pydantic rejected the
        # request before we ever reached the handler body.
        mock_client.complete_resource_token_auth.assert_not_called()


def _make_provider(
    provider_id: str = "google",
    *,
    enabled: bool = True,
    allowed_roles: list[str] | None = None,
    custom_parameters: dict[str, str] | None = None,
) -> OAuthProvider:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return OAuthProvider(
        provider_id=provider_id,
        display_name=provider_id.capitalize(),
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["openid", "email"],
        allowed_roles=allowed_roles or [],
        enabled=enabled,
        custom_parameters=custom_parameters,
        created_at=now,
        updated_at=now,
    )


def _make_permissions(user_id: str, *, roles: list[str] | None = None) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id=user_id,
        app_roles=roles or [],
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at=datetime.now(timezone.utc).isoformat() + "Z",
    )


class _FakeDisconnectRepo:
    """In-memory stand-in for the durable DDB-backed disconnect repository."""

    def __init__(self) -> None:
        self.disconnected: set[tuple[str, str]] = set()

    async def is_disconnected(self, user_id: str, provider_id: str) -> bool:
        return (user_id, provider_id) in self.disconnected

    async def mark_disconnected(self, user_id: str, provider_id: str) -> None:
        self.disconnected.add((user_id, provider_id))

    async def clear_disconnected(self, user_id: str, provider_id: str) -> None:
        self.disconnected.discard((user_id, provider_id))


@pytest.fixture
def app_with_deps(app_for_user, monkeypatch):
    """Mount the router and stub provider repo, role service, identity client.

    Returns a builder so each test wires the specific responses it needs.
    """

    def _build(
        user_id: str,
        *,
        provider: OAuthProvider | None,
        permissions: UserEffectivePermissions | None = None,
        identity_result: TokenResult | None = None,
        identity_raises: Exception | None = None,
        disconnect_repo: _FakeDisconnectRepo | None = None,
    ) -> tuple[FastAPI, MagicMock, _FakeDisconnectRepo]:
        app = app_for_user(user_id)

        repo = MagicMock()
        repo.get_provider = AsyncMock(return_value=provider)
        app.dependency_overrides[get_provider_repository] = lambda: repo

        role_service = MagicMock()
        role_service.resolve_user_permissions = AsyncMock(
            return_value=permissions or _make_permissions(user_id),
        )
        app.dependency_overrides[get_app_role_service] = lambda: role_service

        disconnect_repo = disconnect_repo or _FakeDisconnectRepo()
        app.dependency_overrides[get_disconnect_repository] = lambda: disconnect_repo

        identity = MagicMock()
        if identity_raises is not None:
            identity.get_token_for_user = AsyncMock(side_effect=identity_raises)
        else:
            identity.get_token_for_user = AsyncMock(
                return_value=identity_result
                or TokenResult(access_token="vault-token"),
            )
        monkeypatch.setattr(routes, "get_agentcore_identity_client", lambda: identity)

        return app, identity, disconnect_repo

    return _build


class TestConnectorStatus:
    def test_returns_connected_when_vault_has_token(self, app_with_deps):
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(access_token="vault-token"),
        )
        response = TestClient(app).get("/connectors/google/status")

        assert response.status_code == 200
        assert response.json() == {"connected": True}
        identity.get_token_for_user.assert_called_once()

    def test_returns_not_connected_when_vault_empty(self, app_with_deps):
        # The point of /status: when the vault is empty we report it as
        # {connected: false} and discard the auth URL — the listing UI
        # only wants the badge, not to start a flow.
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(
                authorization_url="https://example.com/auth?request_uri=abc",
            ),
        )
        response = TestClient(app).get("/connectors/google/status")

        assert response.status_code == 200
        assert response.json() == {"connected": False}
        # The auth URL is intentionally NOT echoed back.
        assert "authorization_url" not in response.json()
        assert "authorizationUrl" not in response.json()

    def test_404_when_provider_missing(self, app_with_deps):
        app, identity, _ = app_with_deps("alice", provider=None)
        response = TestClient(app).get("/connectors/google/status")

        assert response.status_code == 404
        identity.get_token_for_user.assert_not_called()

    def test_404_when_provider_disabled(self, app_with_deps):
        app, identity, _ = app_with_deps(
            "alice", provider=_make_provider(enabled=False)
        )
        response = TestClient(app).get("/connectors/google/status")

        # Disabled providers are indistinguishable from missing to the user.
        assert response.status_code == 404
        identity.get_token_for_user.assert_not_called()

    def test_403_when_user_lacks_role(self, app_with_deps):
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(allowed_roles=["admins"]),
            permissions=_make_permissions("alice", roles=["users"]),
        )
        response = TestClient(app).get("/connectors/google/status")

        assert response.status_code == 403
        identity.get_token_for_user.assert_not_called()

    def test_503_when_workload_token_unavailable(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_raises=WorkloadTokenUnavailableError("no workload token"),
        )
        response = TestClient(app).get("/connectors/google/status")
        assert response.status_code == 503
        assert "no workload token" in response.json()["detail"]

    def test_503_when_callback_url_unavailable(self, app_with_deps):
        app, _, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_raises=CallbackUrlUnavailableError("no callback URL"),
        )
        response = TestClient(app).get("/connectors/google/status")
        assert response.status_code == 503
        assert "no callback URL" in response.json()["detail"]

    def test_disconnected_overrides_vault_state(self, app_with_deps):
        # After a disconnect, the user is "not connected" even if AgentCore's
        # vault still holds a valid token — and AgentCore is not consulted,
        # so a stale vault entry can't accidentally flip the badge back on.
        # The flag lives in the DDB-backed disconnect repository so a
        # disconnect on one replica is honored on every subsequent request,
        # even if it lands on a different replica.
        repo = _FakeDisconnectRepo()
        repo.disconnected.add(("alice", "google"))
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(access_token="vault-token"),
            disconnect_repo=repo,
        )
        response = TestClient(app).get("/connectors/google/status")

        assert response.status_code == 200
        assert response.json() == {"connected": False}
        identity.get_token_for_user.assert_not_called()


class TestDisconnect:
    def test_marks_provider_disconnected_durably(self, app_with_deps):
        app, _, repo = app_with_deps("alice", provider=_make_provider())
        assert ("alice", "google") not in repo.disconnected

        response = TestClient(app).delete("/connectors/google/connection")

        assert response.status_code == 204
        assert ("alice", "google") in repo.disconnected

    # Note: the inference-api process keeps a per-replica in-memory token
    # cache; we used to clear it inline here, but app-api can't reach into
    # another process's cache. The disconnect-repo flag is the durable
    # cross-process signal — the consent hook on inference-api consults it
    # on every gate call, so the next tool invocation rejects the cached
    # token regardless. Cache-clearing behavior is covered in the consent
    # hook's tests, not here.

    def test_404_when_provider_missing(self, app_with_deps):
        app, _, repo = app_with_deps("alice", provider=None)
        response = TestClient(app).delete("/connectors/google/connection")

        assert response.status_code == 404
        assert ("alice", "google") not in repo.disconnected

    def test_403_when_user_lacks_role(self, app_with_deps):
        app, _, repo = app_with_deps(
            "alice",
            provider=_make_provider(allowed_roles=["admins"]),
            permissions=_make_permissions("alice", roles=["users"]),
        )
        response = TestClient(app).delete("/connectors/google/connection")

        assert response.status_code == 403
        assert ("alice", "google") not in repo.disconnected


class TestForceReauthLifecycle:
    """End-to-end-ish: disconnect → initiate-consent → complete-consent."""

    def test_initiate_consent_forces_auth_after_disconnect(self, app_with_deps):
        # disconnect → next initiate-consent must pass force_authentication
        # so AgentCore returns a fresh authorize URL instead of the cached
        # token (which we just told the user we'd stop using).
        repo = _FakeDisconnectRepo()
        repo.disconnected.add(("alice", "google"))
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(
                authorization_url="https://example.com/auth?request_uri=abc",
            ),
            disconnect_repo=repo,
        )
        TestClient(app).post("/connectors/google/initiate-consent")

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["force_authentication"] is True

    def test_initiate_consent_does_not_force_when_not_disconnected(self, app_with_deps):
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(access_token="vault-token"),
        )
        TestClient(app).post("/connectors/google/initiate-consent")

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["force_authentication"] is False

    def test_status_forwards_google_access_type_offline(self, app_with_deps):
        # Per AgentCore docs, Google needs `access_type=offline` in
        # customParameters so the vault gets a refresh token.
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),  # default is OAuthProviderType.GOOGLE
            identity_result=TokenResult(access_token="vault-token"),
        )
        TestClient(app).get("/connectors/google/status")

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["custom_parameters"] == {
            "access_type": "offline",
        }

    def test_initiate_consent_forwards_google_access_type_offline(self, app_with_deps):
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(),
            identity_result=TokenResult(access_token="vault-token"),
        )
        TestClient(app).post("/connectors/google/initiate-consent")

        identity.get_token_for_user.assert_called_once()
        assert identity.get_token_for_user.call_args.kwargs["custom_parameters"] == {
            "access_type": "offline",
        }

    def test_admin_custom_parameters_merge_with_google_baseline(self, app_with_deps):
        # Admin set Workspace domain restriction. The route must merge
        # admin extras with the hardcoded baseline before forwarding to
        # AgentCore — and the baseline still wins on key conflict.
        app, identity, _ = app_with_deps(
            "alice",
            provider=_make_provider(
                custom_parameters={
                    "hd": "mycompany.com",
                    "access_type": "online",  # admin tries to override; ignored
                },
            ),
            identity_result=TokenResult(access_token="vault-token"),
        )
        TestClient(app).get("/connectors/google/status")

        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["custom_parameters"] == {
            "access_type": "offline",  # baseline wins
            "hd": "mycompany.com",
        }

    def test_complete_consent_clears_disconnect_flag(self, app_for_user, monkeypatch):
        # After a successful re-consent the disconnect intent is satisfied —
        # the next status check should report connected without waiting for
        # the agent loop to warm the cache.
        repo = _FakeDisconnectRepo()
        repo.disconnected.add(("alice", "google"))

        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        app.dependency_overrides[get_disconnect_repository] = lambda: repo

        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 200
        assert ("alice", "google") not in repo.disconnected
