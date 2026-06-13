"""Tests for the shared Bedrock bearer-token generator (Mantle auth)."""

import base64
from unittest.mock import patch, MagicMock

import pytest
from botocore.credentials import Credentials

from apis.shared.bedrock.bearer_token import (
    generate_bedrock_bearer_token,
    get_mantle_base_url,
)


def _fake_session_with(credentials):
    session = MagicMock()
    session.get_credentials.return_value = credentials
    return session


class TestGetMantleBaseUrl:
    def test_explicit_region(self):
        assert (
            get_mantle_base_url("us-west-2")
            == "https://bedrock-mantle.us-west-2.api.aws/v1"
        )

    def test_falls_back_to_aws_region_env(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert (
            get_mantle_base_url()
            == "https://bedrock-mantle.eu-west-1.api.aws/v1"
        )

    def test_defaults_to_us_east_1(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        assert (
            get_mantle_base_url()
            == "https://bedrock-mantle.us-east-1.api.aws/v1"
        )


class TestGenerateBedrockBearerToken:
    def test_token_shape_matches_aws_generator(self):
        creds = Credentials(access_key="AKIATEST", secret_key="secret")
        with patch(
            "apis.shared.bedrock.bearer_token.boto3.Session",
            return_value=_fake_session_with(creds),
        ):
            token = generate_bedrock_bearer_token("us-west-2")

        assert token.startswith("bedrock-api-key-")
        decoded = base64.b64decode(token[len("bedrock-api-key-"):]).decode("utf-8")
        # Presigned CallWithBearerToken request against the global bedrock host,
        # with the trailing token version marker.
        assert decoded.startswith("bedrock.amazonaws.com/")
        assert "Action=CallWithBearerToken" in decoded
        assert "X-Amz-Signature=" in decoded
        assert "X-Amz-Expires=43200" in decoded
        assert decoded.endswith("&Version=1")

    def test_session_token_included_for_temporary_credentials(self):
        creds = Credentials(
            access_key="ASIATEST", secret_key="secret", token="session-token"
        )
        with patch(
            "apis.shared.bedrock.bearer_token.boto3.Session",
            return_value=_fake_session_with(creds),
        ):
            token = generate_bedrock_bearer_token("us-west-2")

        decoded = base64.b64decode(token[len("bedrock-api-key-"):]).decode("utf-8")
        assert "X-Amz-Security-Token=" in decoded

    def test_raises_without_credentials(self):
        with patch(
            "apis.shared.bedrock.bearer_token.boto3.Session",
            return_value=_fake_session_with(None),
        ):
            with pytest.raises(ValueError, match="No AWS credentials"):
                generate_bedrock_bearer_token("us-west-2")
