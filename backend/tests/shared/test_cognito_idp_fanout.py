"""Tests for fanning federated IdP wiring across every Cognito app client.

The pool has more than one app client — the confidential BFF client used by
app-api and the public PKCE client used by the terminal client — and they must
offer the same federated providers. A provider added to only one of them still
works for that client's users, so the gap stays invisible until someone signs in
from the other surface.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from unittest.mock import patch

AWS_REGION = "us-east-1"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def two_client_pool(aws_env):
    """A pool with two app clients, mirroring the deployed shape."""
    client = boto3.client("cognito-idp", region_name=AWS_REGION)
    pool_id = client.create_user_pool(PoolName="test-pool")["UserPool"]["Id"]

    def make_client(name: str, secret: bool) -> str:
        return client.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=name,
            GenerateSecret=secret,
            SupportedIdentityProviders=["COGNITO"],
            AllowedOAuthFlows=["code"],
            AllowedOAuthScopes=["openid", "profile", "email"],
            CallbackURLs=["http://localhost:4200/auth/callback"],
        )["UserPoolClient"]["ClientId"]

    bff = make_client("bff-app-client", True)
    cli = make_client("cli-app-client", False)
    return {"pool_id": pool_id, "bff": bff, "cli": cli, "boto_client": client}


def make_service(pool, client_ids):
    from apis.shared.auth_providers.cognito_idp_service import (
        CognitoIdentityProviderService,
    )

    return CognitoIdentityProviderService(
        user_pool_id=pool["pool_id"],
        app_client_ids=client_ids,
        region=AWS_REGION,
    )


def providers_on(pool, client_id):
    resp = pool["boto_client"].describe_user_pool_client(UserPoolId=pool["pool_id"], ClientId=client_id)
    return resp["UserPoolClient"].get("SupportedIdentityProviders", [])


class TestClientIdResolution:
    def test_explicit_list_is_used_in_order(self, aws_env):
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        svc = CognitoIdentityProviderService(user_pool_id="pool", app_client_ids=["a", "b"], region=AWS_REGION)
        assert svc.app_client_ids == ["a", "b"]

    def test_falls_back_to_both_environment_clients(self, aws_env, monkeypatch):
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "bff-id")
        monkeypatch.setenv("COGNITO_CLI_APP_CLIENT_ID", "cli-id")

        svc = CognitoIdentityProviderService(region=AWS_REGION)
        assert svc.app_client_ids == ["bff-id", "cli-id"]

    def test_blank_cli_client_is_dropped(self, aws_env, monkeypatch):
        """The CLI client id is '' when that client is not deployed."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "bff-id")
        monkeypatch.setenv("COGNITO_CLI_APP_CLIENT_ID", "")

        svc = CognitoIdentityProviderService(region=AWS_REGION)
        assert svc.app_client_ids == ["bff-id"]
        assert svc.enabled is True

    def test_duplicate_ids_are_collapsed(self, aws_env, monkeypatch):
        """Deployed envs set COGNITO_APP_CLIENT_ID == COGNITO_BFF_APP_CLIENT_ID."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
        monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "same")
        monkeypatch.setenv("COGNITO_CLI_APP_CLIENT_ID", "same")

        svc = CognitoIdentityProviderService(region=AWS_REGION)
        assert svc.app_client_ids == ["same"]

    def test_no_clients_disables_the_service(self, aws_env, monkeypatch):
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        monkeypatch.delenv("COGNITO_APP_CLIENT_ID", raising=False)
        monkeypatch.delenv("COGNITO_CLI_APP_CLIENT_ID", raising=False)
        svc = CognitoIdentityProviderService(user_pool_id="pool", region=AWS_REGION)
        assert svc.enabled is False
        assert svc.app_client_ids == []


class TestFanOut:
    def test_add_reaches_every_client(self, two_client_pool):
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        svc.add_provider_to_app_client("okta-1")

        assert "okta-1" in providers_on(two_client_pool, two_client_pool["bff"])
        assert "okta-1" in providers_on(two_client_pool, two_client_pool["cli"])

    def test_remove_reaches_every_client(self, two_client_pool):
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        svc.add_provider_to_app_client("okta-1")
        svc.remove_provider_from_app_client("okta-1")

        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["bff"])
        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["cli"])

    def test_add_is_idempotent(self, two_client_pool):
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        svc.add_provider_to_app_client("okta-1")
        svc.add_provider_to_app_client("okta-1")

        for client_id in (two_client_pool["bff"], two_client_pool["cli"]):
            assert providers_on(two_client_pool, client_id).count("okta-1") == 1

    def test_existing_client_settings_survive(self, two_client_pool):
        """UpdateUserPoolClient replaces the whole config, so this matters."""
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        svc.add_provider_to_app_client("okta-1")

        resp = two_client_pool["boto_client"].describe_user_pool_client(UserPoolId=two_client_pool["pool_id"], ClientId=two_client_pool["cli"])
        cfg = resp["UserPoolClient"]
        assert cfg["ClientName"] == "cli-app-client"
        assert cfg["CallbackURLs"] == ["http://localhost:4200/auth/callback"]
        assert cfg["AllowedOAuthFlows"] == ["code"]
        assert "COGNITO" in cfg["SupportedIdentityProviders"]

    def test_single_client_still_works(self, two_client_pool):
        """Backward compatibility: envs without the CLI client."""
        svc = make_service(two_client_pool, [two_client_pool["bff"]])
        svc.add_provider_to_app_client("okta-1")

        assert "okta-1" in providers_on(two_client_pool, two_client_pool["bff"])
        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["cli"])


class TestPartialFailureUnwinds:
    def test_failure_on_second_client_reverts_the_first(self, two_client_pool):
        """A client left listing a provider the caller then deletes breaks its
        hosted UI, so the add must be all-or-nothing across clients."""
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        real_update = svc._client.update_user_pool_client
        calls = {"n": 0}

        def flaky_update(**kwargs):
            calls["n"] += 1
            # First call = BFF client succeeds, second = CLI client fails.
            if calls["n"] == 2:
                raise ClientError(
                    {"Error": {"Code": "InternalErrorException", "Message": "boom"}},
                    "UpdateUserPoolClient",
                )
            return real_update(**kwargs)

        with patch.object(svc._client, "update_user_pool_client", side_effect=flaky_update):
            with pytest.raises(ClientError):
                svc.add_provider_to_app_client("okta-1")

        # The revert is itself an update call, so the first client must be clean.
        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["bff"])
        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["cli"])

    def test_remove_tries_every_client_before_raising(self, two_client_pool):
        """Cleanup must not stop at the first error and strand the rest."""
        svc = make_service(two_client_pool, [two_client_pool["bff"], two_client_pool["cli"]])
        svc.add_provider_to_app_client("okta-1")

        real_update = svc._client.update_user_pool_client
        calls = {"n": 0}

        def flaky_update(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClientError(
                    {"Error": {"Code": "InternalErrorException", "Message": "boom"}},
                    "UpdateUserPoolClient",
                )
            return real_update(**kwargs)

        with patch.object(svc._client, "update_user_pool_client", side_effect=flaky_update):
            with pytest.raises(ClientError):
                svc.remove_provider_from_app_client("okta-1")

        # First client failed, but the second was still attempted and cleaned.
        assert "okta-1" in providers_on(two_client_pool, two_client_pool["bff"])
        assert "okta-1" not in providers_on(two_client_pool, two_client_pool["cli"])
