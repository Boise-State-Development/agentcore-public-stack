"""Per-target Lambda invoke grants for AgentCore Gateway IAM targets.

When an admin registers an `mcpServer` Gateway target backed by an IAM-protected
Lambda Function URL, the gateway's execution role must be authorized to invoke
that specific function. Instead of a standing wildcard on the gateway role
(which forces an infra change per off-convention server), the platform grants
the gateway role `InvokeFunctionUrl` on exactly the registered function via the
function's resource policy (`lambda:AddPermission`) at registration, and revokes
it on delete. Verified: a role-specific resource grant authorizes the gateway
role on its own — no identity-side grant required (same-account).

Same-account only. A cross-account function can't be auto-authorized by us; the
caller gets an actionable error telling the admin to make the endpoint public or
use a credential provider instead.
"""

import re
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

# Lambda Function URL host: https://<url-id>.lambda-url.<region>.on.aws/...
_LAMBDA_URL_RE = re.compile(
    r"^https://[a-z0-9]+\.lambda-url\.[a-z0-9-]+\.on\.aws", re.IGNORECASE
)


class GatewayLambdaGrantError(ValueError):
    """The target Lambda can't be auto-authorized — message is admin-actionable."""


_CROSS_ACCOUNT_HINT = (
    "Cross-account Lambda targets can't be auto-authorized for the gateway — make "
    "the Function URL public (AuthType=NONE) with outbound credential 'None', or "
    "front it with an API-key/OAuth credential provider."
)


def is_lambda_function_url(endpoint_url: Optional[str]) -> bool:
    """True if the endpoint is a Lambda Function URL (the auto-grant applies)."""
    return bool(endpoint_url and _LAMBDA_URL_RE.match(endpoint_url))


def statement_id_for(seed: str) -> str:
    """Stable resource-policy Sid for a target, from a seed (its target name).

    Derived from the target name (known before the target id exists, so the
    grant can be placed *before* CreateGatewayTarget and the gateway's async
    connect never races a missing permission). Sids allow only [A-Za-z0-9-_],
    so non-conforming characters collapse to '-'.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", seed)
    return f"agentcore-gw-{safe}"


def _account_of_arn(arn: str) -> Optional[str]:
    parts = arn.split(":")
    return parts[4] if len(parts) >= 5 and parts[0] == "arn" else None


def grant_gateway_invoke(
    *,
    function_name: str,
    endpoint_url: str,
    gateway_role_arn: str,
    statement_seed: str,
    region: str,
    lambda_client: Any = None,
    sts_client: Any = None,
) -> None:
    """Authorize the gateway role to `InvokeFunctionUrl` on `function_name`.

    Validates the function is in THIS account and its Function URL matches
    `endpoint_url`, then adds (idempotently) a resource-policy statement granting
    the gateway role. Raises `GatewayLambdaGrantError` with an admin-actionable
    message for the cross-account / not-found / url-mismatch cases.
    """
    lam = lambda_client or boto3.client("lambda", region_name=region)
    statement_id = statement_id_for(statement_seed)

    # A pasted cross-account ARN is detectable without an API call.
    if function_name.startswith("arn:"):
        acct = _account_of_arn(function_name)
        this_acct = (
            sts_client or boto3.client("sts", region_name=region)
        ).get_caller_identity()["Account"]
        if acct and acct != this_acct:
            raise GatewayLambdaGrantError(
                f"Lambda '{function_name}' is in account {acct}, not this account "
                f"({this_acct}). {_CROSS_ACCOUNT_HINT}"
            )

    # Validate the function is ours and its URL matches what the admin entered.
    try:
        url_cfg = lam.get_function_url_config(FunctionName=function_name)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code")
        if code in ("ResourceNotFoundException", "AccessDeniedException"):
            raise GatewayLambdaGrantError(
                f"Lambda '{function_name}' was not found in this account (or isn't "
                f"accessible). {_CROSS_ACCOUNT_HINT}"
            ) from err
        raise

    # GetFunctionUrlConfig returns the base URL (e.g. https://<id>.lambda-url
    # .../) while the gateway endpoint includes a path (e.g. .../mcp), so the
    # endpoint must be *under* the function's URL (same scheme+host), not equal.
    configured = (url_cfg.get("FunctionUrl") or "").rstrip("/")
    if configured and not endpoint_url.rstrip("/").startswith(configured):
        raise GatewayLambdaGrantError(
            f"The endpoint URL doesn't match the Function URL of '{function_name}' "
            f"({configured}). Check the Lambda function name."
        )

    # Idempotent: drop any stale statement with our Sid, then (re)add.
    try:
        lam.remove_permission(FunctionName=function_name, StatementId=statement_id)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise

    lam.add_permission(
        FunctionName=function_name,
        StatementId=statement_id,
        Action="lambda:InvokeFunctionUrl",
        Principal=gateway_role_arn,
        FunctionUrlAuthType="AWS_IAM",
    )


def revoke_gateway_invoke(
    *,
    function_name: str,
    statement_seed: str,
    region: str,
    lambda_client: Any = None,
) -> None:
    """Remove the gateway-role grant for a target. Idempotent — a missing
    statement (or a deleted function) is treated as already revoked so target
    deletion never blocks on cleanup."""
    lam = lambda_client or boto3.client("lambda", region_name=region)
    try:
        lam.remove_permission(
            FunctionName=function_name,
            StatementId=statement_id_for(statement_seed),
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
