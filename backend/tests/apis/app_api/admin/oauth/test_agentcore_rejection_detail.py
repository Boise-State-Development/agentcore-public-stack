"""A failed connector create/update tells the admin why AgentCore rejected it.

The global ClientError handler answers a generic 400/502 and hides the vendor
message. These routes are admin-only, so a validation-type rejection is echoed
(bounded, control characters stripped); every other code keeps the generic path.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from apis.app_api.admin.oauth import routes
from apis.shared.auth.models import User
from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderCreate,
    OAuthProviderType,
    OAuthProviderUpdate,
)


def _admin() -> User:
    return User(
        email="admin@example.edu",
        user_id="u1",
        name="Admin",
        roles=["system_admin"],
        raw_token="t",
    )


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateOauth2CredentialProvider")


class _Repo:
    def __init__(self, existing: OAuthProvider | None = None):
        self._existing = existing

    async def get_provider(self, provider_id: str):
        return self._existing


class _Registrar:
    def __init__(self, err: Exception):
        self._err = err

    def create_credential_provider(self, **kwargs):
        raise self._err

    def update_credential_provider(self, **kwargs):
        raise self._err


def _create_payload() -> OAuthProviderCreate:
    return OAuthProviderCreate(
        provider_id="okta-main",
        display_name="Okta",
        provider_type=OAuthProviderType.CUSTOM,
        scopes=["openid"],
        client_id="cid",
        client_secret="secret",
        oauth_discovery_url="https://idp.example.edu/.well-known/openid-configuration",
    )


async def _create(err: Exception):
    return await routes.create_provider(
        provider_data=_create_payload(),
        admin=_admin(),
        provider_repo=_Repo(),
        registrar=_Registrar(err),
    )


async def _update(err: Exception):
    existing = OAuthProvider(
        provider_id="okta-main",
        display_name="Okta",
        provider_type=OAuthProviderType.CUSTOM,
        scopes=["openid"],
        allowed_roles=[],
        oauth_discovery_url="https://idp.example.edu/.well-known/openid-configuration",
    )
    return await routes.update_provider(
        provider_id="okta-main",
        updates=OAuthProviderUpdate(client_id="cid", client_secret="secret"),
        admin=_admin(),
        provider_repo=_Repo(existing),
        registrar=_Registrar(err),
    )


@pytest.mark.parametrize("call", [_create, _update])
class TestAgentCoreRejection:
    @pytest.mark.asyncio
    async def test_validation_exception_is_echoed_as_400(self, call):
        err = _client_error("ValidationException", "TokenEndpoint is not a valid URL")
        with pytest.raises(HTTPException) as exc:
            await call(err)
        assert exc.value.status_code == 400
        assert exc.value.detail == (
            "AgentCore rejected the configuration: TokenEndpoint is not a valid URL"
        )

    @pytest.mark.asyncio
    async def test_message_is_sanitised_and_bounded(self, call):
        err = _client_error("ValidationException", "bad\n\x00value " + "x" * 1000)
        with pytest.raises(HTTPException) as exc:
            await call(err)
        assert exc.value.status_code == 400
        assert "\n" not in exc.value.detail and "\x00" not in exc.value.detail
        assert len(exc.value.detail) < 400

    @pytest.mark.asyncio
    async def test_non_validation_code_is_reraised_for_generic_502(self, call):
        err = _client_error("ThrottlingException", "secret internal detail")
        with pytest.raises(ClientError) as exc:
            await call(err)
        assert exc.value is err
