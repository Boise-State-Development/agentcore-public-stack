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
from boto3.dynamodb.conditions import Key
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


class ArtifactQueryError(RenderTokenError):
    """A backing-store query failed at runtime (throttle, timeout,
    transient DynamoDB error) — distinct from a misconfiguration: the
    feature is set up correctly, the request just couldn't be served."""


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


# Frozen contract — the HEAD row + SessionIndex keys the artifact writer
# (backend/src/agents/builtin_tools/artifacts/service.py) emits.
_SESSION_INDEX = "SessionIndex"


class ArtifactListService:
    """List a session's artifacts from the SessionIndex GSI.

    The GSI projects only HEAD rows (the writer attaches GSI1PK/GSI1SK to
    the HEAD put only), so a query already returns one row per artifact —
    its current version — newest-first. GSI1PK is `SESSION#{sid}`, which
    is NOT user-scoped, so every returned row is re-checked against the
    authenticated user's id: a caller passing a borrowed session id can
    never enumerate another user's artifacts.
    """

    def list_for_session(
        self, *, user_id: str, session_id: str
    ) -> list[dict]:
        table = _table()
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": _SESSION_INDEX,
            "KeyConditionExpression": Key("GSI1PK").eq(
                f"SESSION#{session_id}"
            ),
            "ScanIndexForward": False,  # GSI1SK embeds updated_at → newest first
        }
        try:
            while True:
                resp = table.query(**kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except ClientError as exc:
            raise ArtifactQueryError(
                "artifact list query failed"
            ) from exc

        summaries: list[dict] = []
        for item in items:
            if item.get("user_id") != user_id:
                continue
            summaries.append(
                {
                    "artifact_id": item.get("artifact_id", ""),
                    "version": int(item.get("version", 0)),
                    "title": item.get("title", ""),
                    "content_type": item.get(
                        "content_type", "text/html; charset=utf-8"
                    ),
                    "updated_at": item.get("updated_at", ""),
                    "created_at": item.get("created_at"),
                    "produced_by_message_index": item.get(
                        "produced_by_message_index"
                    ),
                }
            )
        return summaries


def get_artifact_list_service() -> ArtifactListService:
    return ArtifactListService()
