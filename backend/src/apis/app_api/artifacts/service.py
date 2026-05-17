"""Render-token minting service.

Mints the HS256 JWT that the artifact render Lambda verifies. The claim
shape, signing key, and DynamoDB lookup keys are a frozen cross-PR
contract with `backend/src/lambdas/artifact_render/handler.py` — any
change here must be mirrored in that verifier (and vice versa).

SECURITY: the minted token is a bearer credential carried in a URL.
Never log the token or the assembled URL — log identifiers only.
"""

from __future__ import annotations

import base64
import logging
import os
import re
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
_s3_lock = threading.Lock()
_cached_signing_key: Optional[str] = None
_secrets_client = None
_ddb_table = None
_s3_client = None
_cached_bucket: Optional[str] = None

# Inline code-view ceiling. Past this the SPA shows a "too large to
# preview — download instead" affordance rather than highlighting a
# multi-MB blob in the DOM.
_MAX_CONTENT_BYTES = 2 * 1024 * 1024

# Bare Markdown MIME types. Duplicated (not imported) from the agent
# writer: the import-boundary rule forbids app_api importing from
# agents/, and this set rarely changes.
_MARKDOWN_MIME_TYPES = frozenset({"text/markdown", "text/x-markdown"})

# The writer embeds the authored Markdown as base64 in this exact script
# tag inside the rendered HTML wrapper (agents/builtin_tools/artifacts
# _MARKDOWN_RENDER_TEMPLATE). We unwrap it back to source for code view.
_MD_SRC_RE = re.compile(
    r'<script type="application/x-markdown-base64" id="md-src">'
    r"(?P<b64>[^<]*)</script>"
)


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


class ArtifactTooLargeError(RenderTokenError):
    """The artifact body exceeds the inline code-view cap. The caller
    should fall back to the download path rather than streaming a huge
    blob into the SPA's DOM for syntax highlighting."""


def _reset_caches_for_tests() -> None:
    """Drop process-wide singletons so test order can't leak a stale
    signing key, secrets client, or DDB table handle."""
    global _cached_signing_key, _secrets_client, _ddb_table
    global _s3_client, _cached_bucket
    _s3_client = None
    _cached_bucket = None
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


def _bucket_name() -> str:
    """The artifacts S3 bucket. Set by app-api-stack alongside the table
    name; an empty value means a broken artifacts deploy, not a disabled
    feature, so fail closed with a 500."""
    global _cached_bucket
    if _cached_bucket is not None:
        return _cached_bucket
    with _s3_lock:
        if _cached_bucket is not None:
            return _cached_bucket
        name = os.environ.get("S3_ARTIFACTS_BUCKET_NAME", "")
        if not name:
            raise RenderTokenConfigError(
                "S3_ARTIFACTS_BUCKET_NAME is not set"
            )
        _cached_bucket = name
        return name


def _s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_lock:
        if _s3_client is None:
            _s3_client = boto3.client("s3", region_name=_region())
        return _s3_client


def _get_version_item(
    user_id: str, artifact_id: str, version: int
) -> dict:
    """Fetch the exact version row, scoped to the authenticated user.

    Building the PK from the session user's id is what prevents reading
    another user's artifact. SK zero-pad matches the writer/verifier
    `V#{version:05d}` contract."""
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _table().get_item(
            Key={"PK": f"USER#{user_id}", "SK": sk}
        )
    except ClientError as exc:
        raise ArtifactQueryError(
            "artifact metadata lookup failed"
        ) from exc
    item = result.get("Item")
    if not item:
        raise ArtifactNotFoundError("artifact version not found")
    return item


def _is_markdown(content_type: str) -> bool:
    bare = (content_type or "").split(";")[0].strip().lower()
    return bare in _MARKDOWN_MIME_TYPES


def _unwrap_markdown(html_body: str) -> Optional[str]:
    """Recover the authored Markdown from the writer's HTML wrapper.

    Markdown artifacts are stored as a self-contained HTML render
    scaffold with the original source base64-embedded in a fixed
    `<script id="md-src">` tag. Returns the decoded Markdown, or None if
    the tag is absent / undecodable (legacy object or a future template
    change) so the caller can fall back to the raw bytes."""
    match = _MD_SRC_RE.search(html_body)
    if not match:
        return None
    try:
        return base64.b64decode(match.group("b64")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


class ArtifactContentService:
    """Return one artifact version's raw source for the panel code view.

    Ownership is enforced by the PK lookup. For Markdown the stored S3
    object is a rendered HTML wrapper; we unwrap it back to the authored
    Markdown so code view shows what the model actually wrote, and
    normalize `content_type` to `text/markdown` to match. Anything that
    can't be unwrapped falls back to the raw stored bytes + real type so
    the view still shows something truthful instead of erroring."""

    def get(
        self, *, user_id: str, artifact_id: str, version: int
    ) -> tuple[str, str]:
        bucket = _bucket_name()
        item = _get_version_item(user_id, artifact_id, version)
        content_key = item.get("content_key")
        stored_type = item.get(
            "content_type", "text/html; charset=utf-8"
        )
        if not content_key:
            raise ArtifactNotFoundError("artifact has no stored content")

        try:
            obj = _s3().get_object(Bucket=bucket, Key=content_key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise ArtifactNotFoundError(
                    "artifact content not found"
                ) from exc
            raise ArtifactQueryError(
                "artifact content fetch failed"
            ) from exc

        if obj.get("ContentLength", 0) > _MAX_CONTENT_BYTES:
            raise ArtifactTooLargeError("artifact too large for code view")

        raw = obj["Body"].read(_MAX_CONTENT_BYTES + 1)
        if len(raw) > _MAX_CONTENT_BYTES:
            raise ArtifactTooLargeError("artifact too large for code view")
        body = raw.decode("utf-8", errors="replace")

        if _is_markdown(stored_type):
            unwrapped = _unwrap_markdown(body)
            if unwrapped is not None:
                return unwrapped, "text/markdown"
        return body, stored_type


def get_artifact_content_service() -> ArtifactContentService:
    return ArtifactContentService()
