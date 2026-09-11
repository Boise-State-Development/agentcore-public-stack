"""Route tests for the discovery-change guard on `PATCH /admin/oauth-providers/{id}`.

A discovery-config change requires a credential rotation, because AgentCore's
update API demands the full config and never echoes the stored client secret
back. The guard must fire on a *real* change only: clients that round-trip the
whole record resend the unchanged discovery URL on every save, and rejecting
those makes metadata-only edits (scopes, display name, icon, enabled)
impossible for any provider that has one — the admin has no way to satisfy the
rotation requirement. See `apis/app_api/admin/oauth/routes.py`.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from apis.app_api.admin.oauth import routes
from apis.shared.auth.models import User
from apis.shared.oauth.agentcore_registrar import CredentialProviderInfo
from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderType,
    OAuthProviderUpdate,
)

_DISCOVERY_URL = "https://boisestate.instructure.com/.well-known/openid-configuration"


def _admin() -> User:
    return User(
        email="admin@example.edu",
        user_id="u1",
        name="Admin",
        roles=["system_admin"],
        raw_token="admin-tok",
    )


def _provider(**overrides) -> OAuthProvider:
    defaults = dict(
        provider_id="canvas-faculty",
        display_name="Canvas (Faculty)",
        provider_type=OAuthProviderType.CANVAS,
        scopes=["url:GET|/api/v1/courses"],
        allowed_roles=["faculty"],
        oauth_discovery_url=_DISCOVERY_URL,
        credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:1:provider/canvas",
    )
    defaults.update(overrides)
    return OAuthProvider(**defaults)


class _FakeProviderRepo:
    """Minimal stand-in for `OAuthProviderRepository`.

    `apply_metadata_update` mirrors the real repository closely enough for
    the guard's purposes: it copies the populated fields onto the record and
    stamps `updated_at`.
    """

    def __init__(self, provider: OAuthProvider | None):
        self._provider = provider
        self.metadata_updates: list[OAuthProviderUpdate] = []

    async def get_provider(self, provider_id: str) -> OAuthProvider | None:
        return self._provider

    async def apply_metadata_update(
        self, provider_id: str, updates: OAuthProviderUpdate
    ) -> OAuthProvider | None:
        self.metadata_updates.append(updates)
        if self._provider is None:
            return None
        if updates.scopes is not None:
            self._provider.scopes = updates.scopes
        if updates.display_name is not None:
            self._provider.display_name = updates.display_name
        if updates.oauth_discovery_url is not None:
            self._provider.oauth_discovery_url = updates.oauth_discovery_url
        self._provider.updated_at = "2026-09-10T00:00:00Z"
        return self._provider

    async def put_provider(self, provider: OAuthProvider) -> None:
        self._provider = provider


class _FakeRegistrar:
    def __init__(self):
        self.update_calls: list[dict] = []

    def update_credential_provider(self, **kwargs) -> CredentialProviderInfo:
        self.update_calls.append(kwargs)
        return CredentialProviderInfo(
            provider_id=kwargs["provider_id"],
            vendor="CustomOauth2",
            credential_provider_arn="arn:aws:bedrock-agentcore:us-west-2:1:provider/new",
            client_secret_arn="arn:aws:secretsmanager:us-west-2:1:secret/new",
            callback_url="https://example.edu/auth/callback",
        )


async def _update(updates: OAuthProviderUpdate, repo, registrar):
    return await routes.update_provider(
        provider_id="canvas-faculty",
        updates=updates,
        admin=_admin(),
        provider_repo=repo,
        registrar=registrar,
    )


class TestDiscoveryGuard:
    @pytest.mark.asyncio
    async def test_unchanged_discovery_url_allows_metadata_only_edit(self):
        """The SPA resends the discovery URL verbatim; that must not 400."""
        repo = _FakeProviderRepo(_provider())
        registrar = _FakeRegistrar()

        response = await _update(
            OAuthProviderUpdate(
                display_name="Canvas (Faculty)",
                scopes=["url:GET|/api/v1/courses", "url:GET|/api/v1/users/:id"],
                oauth_discovery_url=_DISCOVERY_URL,
            ),
            repo,
            registrar,
        )

        assert response.scopes == [
            "url:GET|/api/v1/courses",
            "url:GET|/api/v1/users/:id",
        ]
        # No AgentCore call — nothing about the credential config changed.
        assert registrar.update_calls == []

    @pytest.mark.asyncio
    async def test_scopes_only_edit_succeeds(self):
        """The documented workaround payload — no discovery field at all."""
        repo = _FakeProviderRepo(_provider())
        registrar = _FakeRegistrar()

        response = await _update(
            OAuthProviderUpdate(scopes=["url:GET|/api/v1/courses"]), repo, registrar
        )

        assert response.scopes == ["url:GET|/api/v1/courses"]
        assert registrar.update_calls == []

    @pytest.mark.asyncio
    async def test_changed_discovery_url_without_credentials_still_400s(self):
        repo = _FakeProviderRepo(_provider())
        registrar = _FakeRegistrar()

        with pytest.raises(HTTPException) as exc:
            await _update(
                OAuthProviderUpdate(
                    oauth_discovery_url="https://other.example.edu/.well-known/openid-configuration"
                ),
                repo,
                registrar,
            )

        assert exc.value.status_code == 400
        assert "credential rotation" in exc.value.detail
        assert registrar.update_calls == []

    @pytest.mark.asyncio
    async def test_unchanged_metadata_dict_allows_metadata_only_edit(self):
        """Same rule for the explicit-metadata flavor of the discovery config."""
        metadata = {"issuer": "https://idp.example.edu", "authorization_endpoint": "/a"}
        repo = _FakeProviderRepo(
            _provider(oauth_discovery_url=None, authorization_server_metadata=metadata)
        )
        registrar = _FakeRegistrar()

        response = await _update(
            OAuthProviderUpdate(
                scopes=["openid"],
                authorization_server_metadata=dict(metadata),
            ),
            repo,
            registrar,
        )

        assert response.scopes == ["openid"]
        assert registrar.update_calls == []

    @pytest.mark.asyncio
    async def test_changed_metadata_dict_without_credentials_still_400s(self):
        repo = _FakeProviderRepo(
            _provider(
                oauth_discovery_url=None,
                authorization_server_metadata={"issuer": "https://idp.example.edu"},
            )
        )
        registrar = _FakeRegistrar()

        with pytest.raises(HTTPException) as exc:
            await _update(
                OAuthProviderUpdate(
                    authorization_server_metadata={"issuer": "https://new.example.edu"}
                ),
                repo,
                registrar,
            )

        assert exc.value.status_code == 400
        assert registrar.update_calls == []

    @pytest.mark.asyncio
    async def test_changed_discovery_url_with_rotation_reaches_agentcore(self):
        repo = _FakeProviderRepo(_provider())
        registrar = _FakeRegistrar()
        new_url = "https://other.example.edu/.well-known/openid-configuration"

        response = await _update(
            OAuthProviderUpdate(
                client_id="new-client",
                client_secret="new-secret",
                oauth_discovery_url=new_url,
            ),
            repo,
            registrar,
        )

        assert len(registrar.update_calls) == 1
        assert registrar.update_calls[0]["discovery_url"] == new_url
        assert registrar.update_calls[0]["client_id"] == "new-client"
        assert response.callback_url == "https://example.edu/auth/callback"

    @pytest.mark.asyncio
    async def test_rotation_alone_carries_the_existing_discovery_url_forward(self):
        """AgentCore needs the full config, so the stored URL is resent."""
        repo = _FakeProviderRepo(_provider())
        registrar = _FakeRegistrar()

        await _update(
            OAuthProviderUpdate(client_id="new-client", client_secret="new-secret"),
            repo,
            registrar,
        )

        assert len(registrar.update_calls) == 1
        assert registrar.update_calls[0]["discovery_url"] == _DISCOVERY_URL
