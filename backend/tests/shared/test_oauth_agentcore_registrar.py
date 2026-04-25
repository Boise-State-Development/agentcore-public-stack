"""AgentCore Identity credential-provider registrar tests.

Mocks the `bedrock-agentcore-control` boto3 client directly — these tests
verify our translation layer (our OAuthProviderType → AgentCore vendor +
config shape), not AWS behaviour.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.shared.oauth.agentcore_registrar import (
    AgentCoreRegistrar,
    CredentialProviderConflictError,
    CredentialProviderNotFoundError,
    InvalidCustomProviderConfigError,
)
from apis.shared.oauth.models import OAuthProviderType


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": code}},
        operation_name="op",
    )


def _create_response(
    *, arn="arn:aws:acps:us-east-1:123:token-vault/default/oauth2credentialprovider/p",
    secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:s",
    callback="https://example.invalid/callback/p",
    config_output=None,
):
    return {
        "credentialProviderArn": arn,
        "clientSecretArn": {"secretArn": secret_arn},
        "callbackUrl": callback,
        "name": "p",
        "oauth2ProviderConfigOutput": config_output or {},
    }


@pytest.fixture
def boto_client():
    return MagicMock()


@pytest.fixture
def registrar(boto_client):
    return AgentCoreRegistrar(client=boto_client, region="us-east-1")


class TestCreateCredentialProvider:
    def test_google_uses_google_vendor_and_config_key(self, registrar, boto_client):
        boto_client.create_oauth2_credential_provider.return_value = _create_response()

        info = registrar.create_credential_provider(
            provider_id="google-workspace",
            provider_type=OAuthProviderType.GOOGLE,
            client_id="cid",
            client_secret="sec",
        )

        boto_client.create_oauth2_credential_provider.assert_called_once_with(
            name="google-workspace",
            credentialProviderVendor="GoogleOauth2",
            oauth2ProviderConfigInput={
                "googleOauth2ProviderConfig": {"clientId": "cid", "clientSecret": "sec"}
            },
        )
        assert info.vendor == "GoogleOauth2"
        assert info.callback_url.endswith("/callback/p")

    @pytest.mark.parametrize(
        "provider_type,expected_vendor,expected_key",
        [
            (OAuthProviderType.MICROSOFT, "MicrosoftOauth2", "microsoftOauth2ProviderConfig"),
            (OAuthProviderType.GITHUB, "GithubOauth2", "githubOauth2ProviderConfig"),
            (OAuthProviderType.SLACK, "SlackOauth2", "slackOauth2ProviderConfig"),
            (
                OAuthProviderType.SALESFORCE,
                "SalesforceOauth2",
                "salesforceOauth2ProviderConfig",
            ),
            # Zoom is a first-class vendor but uses the shared
            # `includedOauth2ProviderConfig` slot rather than its own
            # config struct — see the SDK's Oauth2ProviderConfigInput
            # shape for the authoritative list.
            (OAuthProviderType.ZOOM, "ZoomOauth2", "includedOauth2ProviderConfig"),
        ],
    )
    def test_other_known_vendors(
        self, registrar, boto_client, provider_type, expected_vendor, expected_key
    ):
        boto_client.create_oauth2_credential_provider.return_value = _create_response()

        registrar.create_credential_provider(
            provider_id="p",
            provider_type=provider_type,
            client_id="cid",
            client_secret="sec",
        )

        call = boto_client.create_oauth2_credential_provider.call_args.kwargs
        assert call["credentialProviderVendor"] == expected_vendor
        assert expected_key in call["oauth2ProviderConfigInput"]
        # And no `oauthDiscovery` block — that's customOauth2-only.
        config = call["oauth2ProviderConfigInput"][expected_key]
        assert "oauthDiscovery" not in config

    def test_custom_requires_discovery(self, registrar):
        with pytest.raises(InvalidCustomProviderConfigError):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.CUSTOM,
                client_id="cid",
                client_secret="sec",
            )

    def test_custom_rejects_both_discovery_modes(self, registrar):
        with pytest.raises(InvalidCustomProviderConfigError):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.CUSTOM,
                client_id="cid",
                client_secret="sec",
                discovery_url="https://idp.example/.well-known/openid-configuration",
                authorization_server_metadata={"authorizationEndpoint": "https://idp/auth"},
            )

    def test_custom_with_discovery_url(self, registrar, boto_client):
        boto_client.create_oauth2_credential_provider.return_value = _create_response()

        registrar.create_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.CUSTOM,
            client_id="cid",
            client_secret="sec",
            discovery_url="https://idp.example/.well-known/openid-configuration",
        )

        config = boto_client.create_oauth2_credential_provider.call_args.kwargs[
            "oauth2ProviderConfigInput"
        ]["customOauth2ProviderConfig"]
        assert config["oauthDiscovery"] == {
            "discoveryUrl": "https://idp.example/.well-known/openid-configuration"
        }

    def test_canvas_routes_through_custom_vendor(self, registrar, boto_client):
        boto_client.create_oauth2_credential_provider.return_value = _create_response()

        registrar.create_credential_provider(
            provider_id="canvas",
            provider_type=OAuthProviderType.CANVAS,
            client_id="cid",
            client_secret="sec",
            authorization_server_metadata={
                "authorizationEndpoint": "https://canvas.example/login/oauth2/auth",
                "tokenEndpoint": "https://canvas.example/login/oauth2/token",
            },
        )

        call = boto_client.create_oauth2_credential_provider.call_args.kwargs
        assert call["credentialProviderVendor"] == "CustomOauth2"
        assert "customOauth2ProviderConfig" in call["oauth2ProviderConfigInput"]

    @pytest.mark.parametrize(
        "provider_type",
        [
            OAuthProviderType.GOOGLE,
            OAuthProviderType.MICROSOFT,
            OAuthProviderType.GITHUB,
            OAuthProviderType.SLACK,
            OAuthProviderType.SALESFORCE,
            OAuthProviderType.ZOOM,
        ],
    )
    def test_known_vendor_rejects_discovery_params(self, registrar, provider_type):
        # Every first-class vendor (Google, Microsoft, GitHub, Slack,
        # Salesforce, Zoom) has its endpoints baked in by AgentCore. A
        # discovery URL only makes sense for the CustomOauth2 path.
        with pytest.raises(ValueError, match="only valid for CustomOauth2"):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=provider_type,
                client_id="cid",
                client_secret="sec",
                discovery_url="https://idp.example/.well-known/openid-configuration",
            )

    def test_conflict_maps_to_domain_error(self, registrar, boto_client):
        boto_client.create_oauth2_credential_provider.side_effect = _client_error(
            "ConflictException"
        )

        with pytest.raises(CredentialProviderConflictError):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="sec",
            )

    def test_surfaces_client_id_when_echoed(self, registrar, boto_client):
        boto_client.create_oauth2_credential_provider.return_value = _create_response(
            config_output={"googleOauth2ProviderConfig": {"clientId": "cid"}},
        )

        info = registrar.create_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.GOOGLE,
            client_id="cid",
            client_secret="sec",
        )

        assert info.client_id == "cid"


class TestUpdateCredentialProvider:
    def test_sends_full_config(self, registrar, boto_client):
        boto_client.update_oauth2_credential_provider.return_value = _create_response()

        registrar.update_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.GITHUB,
            client_id="new-cid",
            client_secret="new-sec",
        )

        call = boto_client.update_oauth2_credential_provider.call_args.kwargs
        assert call["credentialProviderVendor"] == "GithubOauth2"
        assert call["oauth2ProviderConfigInput"]["githubOauth2ProviderConfig"] == {
            "clientId": "new-cid",
            "clientSecret": "new-sec",
        }

    def test_not_found_maps_to_domain_error(self, registrar, boto_client):
        boto_client.update_oauth2_credential_provider.side_effect = _client_error(
            "ResourceNotFoundException"
        )

        with pytest.raises(CredentialProviderNotFoundError):
            registrar.update_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="sec",
            )

    def test_uses_fallback_arn_when_update_response_omits_it(
        self, registrar, boto_client
    ):
        """AWS's UpdateOauth2CredentialProvider response doesn't include
        credentialProviderArn. The caller passes the known-immutable ARN
        via `fallback_arn` so the returned info is well-formed."""
        update_response = _create_response()
        update_response.pop("credentialProviderArn")
        boto_client.update_oauth2_credential_provider.return_value = update_response

        info = registrar.update_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.GOOGLE,
            client_id="cid",
            client_secret="sec",
            fallback_arn="arn:aws:acps:us-east-1:123:token-vault/default/oauth2credentialprovider/p",
        )

        assert info.credential_provider_arn == (
            "arn:aws:acps:us-east-1:123:token-vault/default/oauth2credentialprovider/p"
        )

    def test_raises_when_response_lacks_arn_and_no_fallback(
        self, registrar, boto_client
    ):
        update_response = _create_response()
        update_response.pop("credentialProviderArn")
        boto_client.update_oauth2_credential_provider.return_value = update_response

        with pytest.raises(TypeError, match="credentialProviderArn"):
            registrar.update_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="sec",
            )


class TestGetCredentialProvider:
    def test_returns_info_including_callback_url(self, registrar, boto_client):
        boto_client.get_oauth2_credential_provider.return_value = {
            **_create_response(
                config_output={"googleOauth2ProviderConfig": {"clientId": "cid"}},
            ),
            "credentialProviderVendor": "GoogleOauth2",
        }

        info = registrar.get_credential_provider("p")

        assert info.vendor == "GoogleOauth2"
        assert info.client_id == "cid"
        assert info.callback_url.endswith("/callback/p")

    def test_not_found(self, registrar, boto_client):
        boto_client.get_oauth2_credential_provider.side_effect = _client_error(
            "ResourceNotFoundException"
        )

        with pytest.raises(CredentialProviderNotFoundError):
            registrar.get_credential_provider("missing")


class TestDeleteCredentialProvider:
    def test_calls_boto(self, registrar, boto_client):
        registrar.delete_credential_provider("p")
        boto_client.delete_oauth2_credential_provider.assert_called_once_with(name="p")

    def test_not_found_is_success(self, registrar, boto_client):
        boto_client.delete_oauth2_credential_provider.side_effect = _client_error(
            "ResourceNotFoundException"
        )
        registrar.delete_credential_provider("missing")  # no raise

    def test_other_errors_bubble(self, registrar, boto_client):
        boto_client.delete_oauth2_credential_provider.side_effect = _client_error(
            "AccessDeniedException"
        )
        with pytest.raises(ClientError):
            registrar.delete_credential_provider("p")


class TestResponseParsing:
    """`_info_from_response` must fail loudly on contract violations and
    tolerate documented variations (missing secret, missing clientId)."""

    def test_tolerates_missing_client_secret_arn(self, registrar, boto_client):
        response = _create_response()
        response.pop("clientSecretArn")
        boto_client.create_oauth2_credential_provider.return_value = response

        info = registrar.create_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.GOOGLE,
            client_id="cid",
            client_secret="csec",
        )
        assert info.client_secret_arn == ""

    def test_rejects_client_secret_arn_as_string(self, registrar, boto_client):
        """AgentCore contract is `{secretArn: str}`; a raw string signals
        an API contract change we should fail on loudly."""
        response = _create_response()
        response["clientSecretArn"] = "arn:aws:secretsmanager:...:secret:s"
        boto_client.create_oauth2_credential_provider.return_value = response

        with pytest.raises(TypeError, match="clientSecretArn of unexpected type"):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="csec",
            )

    def test_rejects_non_string_nested_secret_arn(self, registrar, boto_client):
        response = _create_response()
        response["clientSecretArn"] = {"secretArn": 12345}
        boto_client.create_oauth2_credential_provider.return_value = response

        with pytest.raises(TypeError, match="secretArn of unexpected type"):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="csec",
            )

    def test_rejects_missing_credential_provider_arn(self, registrar, boto_client):
        response = _create_response()
        response.pop("credentialProviderArn")
        boto_client.create_oauth2_credential_provider.return_value = response

        with pytest.raises(TypeError, match="credentialProviderArn"):
            registrar.create_credential_provider(
                provider_id="p",
                provider_type=OAuthProviderType.GOOGLE,
                client_id="cid",
                client_secret="csec",
            )

    def test_tolerates_missing_callback_url(self, registrar, boto_client):
        """Callback URL absence falls back to empty string — non-fatal for
        vendors that don't declare one yet."""
        response = _create_response()
        response["callbackUrl"] = None
        boto_client.create_oauth2_credential_provider.return_value = response

        info = registrar.create_credential_provider(
            provider_id="p",
            provider_type=OAuthProviderType.GOOGLE,
            client_id="cid",
            client_secret="csec",
        )
        assert info.callback_url == ""
