"""Short-term Bedrock bearer tokens for OpenAI-compatible Bedrock surfaces.

Bedrock Mantle (`https://bedrock-mantle.<region>.api.aws/v1`) speaks the
OpenAI wire protocol and authenticates with a bearer token instead of SigV4.
The short-term token is a SigV4 *presigned* `CallWithBearerToken` request,
base64-encoded and prefixed — the same construction as AWS's
`aws-bedrock-token-generator` package, inlined here so we don't carry a
dependency for ~20 lines of botocore. The token authorizes as whatever
principal signed it (task role locally on ECS, runtime role in AgentCore).
The presign signs the standard `bedrock` service (matching AWS's official
generator), but the Mantle service authorizes the signer against its own
namespace — `bedrock-mantle:CallWithBearerToken` (and
`bedrock-mantle:CreateInference` for chat completions). Mantle must also be
enabled for the account in the target region; IAM alone can't grant a
disabled service (the API returns a "not enabled for this account" 401).

Tokens expire with the signing credentials, capped at 12 hours. We generate
one per client construction (per admin browse call / per agent turn) rather
than caching, so credential rotation on the task role is a non-issue.
"""

import base64
import logging
import os
from typing import Optional

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)

_BEDROCK_HOST = "bedrock.amazonaws.com"
_SERVICE_NAME = "bedrock"
_AUTH_PREFIX = "bedrock-api-key-"
_TOKEN_VERSION = "&Version=1"
_TOKEN_DURATION_SECONDS = 43200  # 12 hours — the service-side maximum

# Mantle is a regional endpoint on the api.aws TLD (not amazonaws.com).
_MANTLE_HOST_TEMPLATE = "https://bedrock-mantle.{region}.api.aws"
# Default OpenAI-compatible path. Some models (e.g. Gemma 4) are served on
# `/openai/v1` instead — see `ManagedModel.mantle_endpoint_path`.
_MANTLE_DEFAULT_PATH = "/v1"


def get_mantle_base_url(
    region: Optional[str] = None, endpoint_path: Optional[str] = None
) -> str:
    """OpenAI-compatible base URL for Bedrock Mantle in ``region``.

    ``region`` falls back to AWS_REGION, then us-east-1 (Mantle's broadest
    region). ``endpoint_path`` is the per-model path segment — ``/v1`` (the
    default, OpenAI Chat Completions) or ``/openai/v1`` (e.g. Gemma 4). Mantle
    exposes no API to discover a model's path, so the caller supplies the
    value recorded on the managed model.
    """
    resolved_region = region or os.environ.get("AWS_REGION") or "us-east-1"
    path = endpoint_path or _MANTLE_DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    return _MANTLE_HOST_TEMPLATE.format(region=resolved_region) + path


def generate_bedrock_bearer_token(region: Optional[str] = None) -> str:
    """Mint a short-term Bedrock bearer token from the ambient AWS credentials.

    Raises:
        ValueError: when no AWS credentials are resolvable in this environment.
    """
    resolved_region = region or os.environ.get("AWS_REGION") or "us-east-1"
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise ValueError(
            "No AWS credentials available to mint a Bedrock bearer token. "
            "Ensure the task/runtime role (or local AWS profile) is configured."
        )

    request = AWSRequest(
        method="POST",
        url=f"https://{_BEDROCK_HOST}/",
        headers={"host": _BEDROCK_HOST},
        params={"Action": "CallWithBearerToken"},
    )
    auth = SigV4QueryAuth(
        credentials.get_frozen_credentials(),
        _SERVICE_NAME,
        resolved_region,
        expires=_TOKEN_DURATION_SECONDS,
    )
    auth.add_auth(request)

    presigned = request.url.replace("https://", "") + _TOKEN_VERSION
    return _AUTH_PREFIX + base64.b64encode(presigned.encode("utf-8")).decode("utf-8")
