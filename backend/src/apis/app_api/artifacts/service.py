"""Render-token minting service.

Mints the HS256 JWT that the artifact render Lambda verifies. The claim
shape, signing key, and DynamoDB lookup keys are a frozen cross-PR
contract with `backend/src/lambdas/artifact_render/handler.py` — any
change here must be mirrored in that verifier (and vice versa).

SECURITY: the minted token is a bearer credential carried in a URL.
Never log the token or the assembled URL — log identifiers only.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import boto3
import jwt
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Frozen contract — must match the render Lambda's _verify_token.
_ISS = "app-api"
_AUD = "artifact-render"
# The verifier hard-caps exp - iat at 600s. 120s comfortably covers an
# iframe load while keeping a leaked-in-a-log token useless almost
# immediately.
_TTL_SECONDS = 120

_secret_lock = threading.Lock()
_table_lock = threading.Lock()
_cached_signing_key: Optional[str] = None
_secrets_client = None
_ddb_table = None


class RenderTokenError(Exception):
    """Base class for render-token failures."""


class ArtifactNotFoundError(RenderTokenError):
    """No version record for the requested (user, artifact, version)."""


class RenderTokenConfigError(RenderTokenError):
    """Required environment / AWS configuration is missing or unusable."""


def _reset_caches_for_tests() -> None:
    """Drop process-wide singletons so test order can't leak a stale
    signing key, secrets client, or DDB table handle."""
    global _cached_signing_key, _secrets_client, _ddb_table
    _cached_signing_key = None
    _secrets_client = None
    _ddb_table = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _signing_key() -> str:
    """Fetch and cache the HMAC signing key. The secret is a plain
    string (Secrets Manager generateSecretString, no JSON wrapper) —
    same shape as the BFF cookie data key."""
    global _cached_signing_key, _secrets_client
    if _cached_signing_key is not None:
        return _cached_signing_key
    with _secret_lock:
        if _cached_signing_key is not None:
            return _cached_signing_key
        arn = os.environ.get("ARTIFACTS_RENDER_TOKEN_SECRET_ARN", "")
        if not arn:
            raise RenderTokenConfigError(
                "ARTIFACTS_RENDER_TOKEN_SECRET_ARN is not set"
            )
        if _secrets_client is None:
            _secrets_client = boto3.client(
                "secretsmanager", region_name=_region()
            )
        try:
            response = _secrets_client.get_secret_value(SecretId=arn)
        except ClientError as exc:
            raise RenderTokenConfigError(
                "could not read render token secret"
            ) from exc
        key = response.get("SecretString") or ""
        if not key:
            raise RenderTokenConfigError("render token secret is empty")
        _cached_signing_key = key
        return key


def _table():
    global _ddb_table
    if _ddb_table is not None:
        return _ddb_table
    with _table_lock:
        if _ddb_table is not None:
            return _ddb_table
        name = os.environ.get("DYNAMODB_ARTIFACTS_TABLE_NAME", "")
        if not name:
            raise RenderTokenConfigError(
                "DYNAMODB_ARTIFACTS_TABLE_NAME is not set"
            )
        _ddb_table = boto3.resource(
            "dynamodb", region_name=_region()
        ).Table(name)
        return _ddb_table


def _origin() -> str:
    """The artifact origin the render token is bound to.

    Validated like the signing key and table so a misconfigured deploy
    fails closed with a 500 — never returns a usable token embedded in a
    relative, unloadable URL. Infra sets this env var alongside the
    secret ARN and table name, so an empty value here means a broken
    artifacts deploy, not a disabled feature."""
    origin = os.environ.get("ARTIFACTS_ORIGIN", "").strip().rstrip("/")
    if not origin:
        raise RenderTokenConfigError("ARTIFACTS_ORIGIN is not set")
    return origin


def _assert_version_exists(
    user_id: str, artifact_id: str, version: int
) -> None:
    """Confirm the exact version row exists and belongs to this user.

    Building the PK from the authenticated user's id is what scopes the
    token: a caller can never mint for another user's artifact. The
    SK zero-pad must match the verifier's `V#{version:05d}`."""
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _table().get_item(
            Key={"PK": f"USER#{user_id}", "SK": sk}
        )
    except ClientError as exc:
        raise RenderTokenConfigError(
            "artifact metadata lookup failed"
        ) from exc
    if "Item" not in result:
        raise ArtifactNotFoundError("artifact version not found")


class RenderTokenService:
    def mint(
        self,
        *,
        user_id: str,
        artifact_id: str,
        version: int,
        session_id: Optional[str],
    ) -> tuple[str, int]:
        """Validate config + ownership/existence, then mint a token.

        Returns (render_url, exp_unix). Raises ArtifactNotFoundError or
        RenderTokenConfigError. Origin is resolved first so a misconfig
        fails closed before any DDB call or credential is generated."""
        origin = _origin()
        _assert_version_exists(user_id, artifact_id, version)
        now = int(time.time())
        exp = now + _TTL_SECONDS
        claims = {
            "iss": _ISS,
            "aud": _AUD,
            "sub": user_id,
            "aid": artifact_id,
            "ver": version,
            "sid": session_id or "",
            "iat": now,
            "exp": exp,
        }
        token = jwt.encode(claims, _signing_key(), algorithm="HS256")
        logger.info(
            "minted render token user=%s artifact=%s v=%s",
            user_id,
            artifact_id,
            version,
        )
        return f"{origin}/?t={token}", exp


def get_render_token_service() -> RenderTokenService:
    return RenderTokenService()
