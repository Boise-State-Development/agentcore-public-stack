"""Tests for the export-target resolve + token helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

import pytest
from fastapi import HTTPException

from apis.shared.auth.models import User
from apis.shared.oauth.agentcore_identity import WorkloadTokenUnavailableError
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType

from apis.app_api.export_targets import service
from apis.app_api.export_targets.service import (
    require_export_target_token,
    resolve_export_target,
)


def _user() -> User:
    return User(
        user_id="u1",
        email="u1@example.com",
        name="U",
        roles=["user"],
        raw_token="tok",
    )


def _provider(**kw) -> OAuthProvider:
    defaults = dict(
        provider_id="gdrive",
        display_name="Google Drive",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["https://www.googleapis.com/auth/drive.file"],
        allowed_roles=[],
        enabled=True,
        export_target_adapter_id="google-drive",
    )
    defaults.update(kw)
    return OAuthProvider(**defaults)


class _Repo:
    def __init__(self, provider: Optional[OAuthProvider]):
        self._p = provider

    async def get_provider(self, provider_id: str):
        return self._p


class _Roles:
    def __init__(self, app_roles: List[str]):
        self._roles = app_roles

    async def resolve_user_permissions(self, user: User):
        return SimpleNamespace(app_roles=self._roles)


class TestResolveExportTarget:
    @pytest.mark.asyncio
    async def test_resolves_provider_and_adapter(self):
        provider, adapter = await resolve_export_target(
            "gdrive", _user(), _Repo(_provider()), _Roles([])
        )
        assert provider.provider_id == "gdrive"
        assert adapter.metadata.key == "google-drive"

    @pytest.mark.asyncio
    async def test_missing_connector_404(self):
        with pytest.raises(HTTPException) as exc:
            await resolve_export_target("gdrive", _user(), _Repo(None), _Roles([]))
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_not_an_export_target_404(self):
        provider = _provider(export_target_adapter_id=None)
        with pytest.raises(HTTPException) as exc:
            await resolve_export_target("gdrive", _user(), _Repo(provider), _Roles([]))
        assert exc.value.status_code == 404
        assert "not configured as an export target" in exc.value.detail

    @pytest.mark.asyncio
    async def test_unknown_adapter_key_404(self):
        provider = _provider(export_target_adapter_id="dropbox")
        with pytest.raises(HTTPException) as exc:
            await resolve_export_target("gdrive", _user(), _Repo(provider), _Roles([]))
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_rbac_denied_403(self):
        provider = _provider(allowed_roles=["admins-only"])
        with pytest.raises(HTTPException) as exc:
            await resolve_export_target(
                "gdrive", _user(), _Repo(provider), _Roles(["plain-user"])
            )
        assert exc.value.status_code == 403


class TestRequireExportTargetToken:
    @pytest.mark.asyncio
    async def test_returns_token_when_connected(self, monkeypatch):
        async def fake(provider, user_id):
            return SimpleNamespace(requires_consent=False, access_token="the-token")

        monkeypatch.setattr(service, "resolve_export_target_token", fake)
        token = await require_export_target_token(_provider(), "u1")
        assert token == "the-token"

    @pytest.mark.asyncio
    async def test_consent_required_409(self, monkeypatch):
        async def fake(provider, user_id):
            return SimpleNamespace(requires_consent=True, access_token=None)

        monkeypatch.setattr(service, "resolve_export_target_token", fake)
        with pytest.raises(HTTPException) as exc:
            await require_export_target_token(_provider(), "u1")
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_workload_unavailable_503(self, monkeypatch):
        async def fake(provider, user_id):
            raise WorkloadTokenUnavailableError("no workload")

        monkeypatch.setattr(service, "resolve_export_target_token", fake)
        with pytest.raises(HTTPException) as exc:
            await require_export_target_token(_provider(), "u1")
        assert exc.value.status_code == 503
