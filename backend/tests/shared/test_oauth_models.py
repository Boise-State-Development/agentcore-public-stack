"""DynamoDB serde tests for `OAuthProvider`.

Focused on the round-trip — admins paste vendor-specific OAuth params
into the form, we persist them, and they must come back unchanged so
the merge with the hardcoded baseline at runtime stays correct.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from apis.shared.oauth.models import (
    OAuthProvider,
    OAuthProviderCreate,
    OAuthProviderType,
    OAuthProviderUpdate,
)


def _provider(**overrides) -> OAuthProvider:
    base = dict(
        provider_id="google-workspace",
        display_name="Google Workspace",
        provider_type=OAuthProviderType.GOOGLE,
        scopes=["openid", "email"],
        allowed_roles=[],
    )
    base.update(overrides)
    return OAuthProvider(**base)


class TestCustomParametersRoundTrip:
    def test_round_trip_preserves_custom_parameters(self) -> None:
        original = _provider(
            custom_parameters={"hd": "mycompany.com", "prompt": "consent"},
        )
        revived = OAuthProvider.from_dynamo_item(original.to_dynamo_item())
        assert revived.custom_parameters == {
            "hd": "mycompany.com",
            "prompt": "consent",
        }

    def test_none_round_trips_as_none(self) -> None:
        # Default state for vendors with no admin-supplied extras.
        original = _provider(custom_parameters=None)
        item = original.to_dynamo_item()
        # Persisted as None — the route layer treats `{}` as "explicitly
        # cleared" and converts it to None before save.
        assert item["customParameters"] is None
        revived = OAuthProvider.from_dynamo_item(item)
        assert revived.custom_parameters is None

    def test_legacy_item_without_field_loads_as_none(self) -> None:
        # Simulates a pre-migration row in DynamoDB. New code must not
        # KeyError on the missing attribute.
        item = _provider(custom_parameters={"foo": "bar"}).to_dynamo_item()
        del item["customParameters"]
        revived = OAuthProvider.from_dynamo_item(item)
        assert revived.custom_parameters is None


class TestTenantProviderCreate:
    """Tenant-hosted vendors need their endpoints; the rest must not send them."""

    _base = dict(provider_id="p", display_name="P", client_id="c", client_secret="s")
    _metadata = {
        "issuer": "https://tenant.example.com",
        "authorizationEndpoint": "https://tenant.example.com/authorize",
        "tokenEndpoint": "https://tenant.example.com/token",
    }

    def test_accepts_full_metadata(self) -> None:
        created = OAuthProviderCreate(
            **self._base,
            provider_type=OAuthProviderType.OKTA,
            authorization_server_metadata=self._metadata,
        )
        assert created.authorization_server_metadata == self._metadata

    def test_rejects_missing_or_blank_endpoint(self) -> None:
        with pytest.raises(ValidationError, match="issuer"):
            OAuthProviderCreate(
                **self._base,
                provider_type=OAuthProviderType.COGNITO,
                authorization_server_metadata={**self._metadata, "issuer": "  "},
            )

    def test_rejects_discovery_url(self) -> None:
        with pytest.raises(ValidationError):
            OAuthProviderCreate(
                **self._base,
                provider_type=OAuthProviderType.PING,
                oauth_discovery_url="https://x.example/.well-known/openid-configuration",
                authorization_server_metadata=self._metadata,
            )

    def test_credentials_only_vendor_rejects_metadata(self) -> None:
        with pytest.raises(ValidationError, match="only valid for custom"):
            OAuthProviderCreate(
                **self._base,
                provider_type=OAuthProviderType.ATLASSIAN,
                authorization_server_metadata=self._metadata,
            )


class TestCredentialWhitespace:
    """A pasted client ID or secret is stored without surrounding whitespace.

    Stored as pasted, a trailing newline made GitHub refuse AgentCore's token
    exchange, which surfaced only as a message-less ValidationException from
    ``CompleteResourceTokenAuth`` after the user had authorized.
    """

    def test_create_strips_client_id_and_secret(self) -> None:
        created = OAuthProviderCreate(
            provider_id="github",
            display_name="GitHub",
            provider_type=OAuthProviderType.GITHUB,
            client_id="  Ov23liExample\n",
            client_secret="\tsecret-value \n",
        )
        assert created.client_id == "Ov23liExample"
        assert created.client_secret == "secret-value"

    def test_create_rejects_whitespace_only_secret(self) -> None:
        with pytest.raises(ValidationError):
            OAuthProviderCreate(
                provider_id="github",
                display_name="GitHub",
                provider_type=OAuthProviderType.GITHUB,
                client_id="Ov23liExample",
                client_secret="   ",
            )

    def test_update_strips_a_rotation(self) -> None:
        update = OAuthProviderUpdate(client_id=" Ov23liExample ", client_secret="secret-value\r\n")
        assert update.client_id == "Ov23liExample"
        assert update.client_secret == "secret-value"

    def test_update_whitespace_only_secret_is_not_a_rotation(self) -> None:
        with pytest.raises(ValidationError, match="provided together"):
            OAuthProviderUpdate(client_id="Ov23liExample", client_secret="  ")

    def test_update_without_credentials_is_unchanged(self) -> None:
        update = OAuthProviderUpdate(display_name="GitHub")
        assert update.client_id is None
        assert update.client_secret is None
