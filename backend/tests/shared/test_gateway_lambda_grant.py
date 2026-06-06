"""Tests for the per-target Lambda invoke grant (#419 admin self-service)."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.shared.tools.gateway_lambda_grant import (
    GatewayLambdaGrantError,
    grant_gateway_invoke,
    is_lambda_function_url,
    revoke_gateway_invoke,
    statement_id_for,
)

ROLE = "arn:aws:iam::111122223333:role/gw-role"
URL = "https://abc123.lambda-url.us-west-2.on.aws/mcp"
# GetFunctionUrlConfig returns the *base* URL (no path) — the endpoint adds /mcp.
BASE_URL = "https://abc123.lambda-url.us-west-2.on.aws/"


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


def _lambda(url=BASE_URL):
    lam = MagicMock()
    lam.get_function_url_config.return_value = {"FunctionUrl": url}
    # First-time grant: no prior statement to remove.
    lam.remove_permission.side_effect = _client_error("ResourceNotFoundException")
    return lam


def test_is_lambda_function_url():
    assert is_lambda_function_url(URL) is True
    assert is_lambda_function_url("https://x.execute-api.us-west-2.amazonaws.com/p") is False
    assert is_lambda_function_url("") is False
    assert is_lambda_function_url(None) is False


def test_statement_id_sanitizes():
    assert statement_id_for("gateway-class-search") == "agentcore-gw-gateway-class-search"
    # disallowed chars collapse to '-'
    assert statement_id_for("a b/c") == "agentcore-gw-a-b-c"


def test_grant_adds_role_principal_resource_grant():
    lam = _lambda()
    grant_gateway_invoke(
        function_name="mcp-class-search-dev",
        endpoint_url=URL,
        gateway_role_arn=ROLE,
        statement_seed="gateway-class-search",
        region="us-west-2",
        lambda_client=lam,
    )
    lam.add_permission.assert_called_once_with(
        FunctionName="mcp-class-search-dev",
        StatementId="agentcore-gw-gateway-class-search",
        Action="lambda:InvokeFunctionUrl",
        Principal=ROLE,
        FunctionUrlAuthType="AWS_IAM",
    )


def test_grant_is_idempotent_removes_stale_statement_first():
    lam = _lambda()
    lam.remove_permission.side_effect = None  # a prior statement exists
    grant_gateway_invoke(
        function_name="fn", endpoint_url=URL, gateway_role_arn=ROLE,
        statement_seed="t", region="us-west-2", lambda_client=lam,
    )
    lam.remove_permission.assert_called_once()
    lam.add_permission.assert_called_once()


def test_grant_rejects_cross_account_arn():
    lam = _lambda()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111122223333"}
    with pytest.raises(GatewayLambdaGrantError, match="Cross-account"):
        grant_gateway_invoke(
            function_name="arn:aws:lambda:us-west-2:999988887777:function:foo",
            endpoint_url=URL, gateway_role_arn=ROLE, statement_seed="t",
            region="us-west-2", lambda_client=lam, sts_client=sts,
        )
    lam.add_permission.assert_not_called()


def test_grant_rejects_function_not_in_account():
    lam = MagicMock()
    lam.get_function_url_config.side_effect = _client_error("ResourceNotFoundException")
    with pytest.raises(GatewayLambdaGrantError, match="not found in this account"):
        grant_gateway_invoke(
            function_name="ghost", endpoint_url=URL, gateway_role_arn=ROLE,
            statement_seed="t", region="us-west-2", lambda_client=lam,
        )


def test_grant_rejects_url_mismatch():
    lam = _lambda(url="https://OTHER.lambda-url.us-west-2.on.aws/")
    with pytest.raises(GatewayLambdaGrantError, match="doesn't match"):
        grant_gateway_invoke(
            function_name="fn", endpoint_url=URL, gateway_role_arn=ROLE,
            statement_seed="t", region="us-west-2", lambda_client=lam,
        )


def test_revoke_removes_statement_and_tolerates_missing():
    lam = MagicMock()
    revoke_gateway_invoke(function_name="fn", statement_seed="t", region="us-west-2", lambda_client=lam)
    lam.remove_permission.assert_called_once_with(
        FunctionName="fn", StatementId="agentcore-gw-t"
    )
    # A missing statement is swallowed (idempotent).
    lam.remove_permission.side_effect = _client_error("ResourceNotFoundException")
    revoke_gateway_invoke(function_name="fn", statement_seed="t", region="us-west-2", lambda_client=lam)
