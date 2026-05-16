"""Artifact render Lambda.

Fronts the `artifacts.{domain}` CloudFront origin. Request flow:

  1. CloudFront forwards a GET carrying a render-token JWT (`?t=...`).
  2. Verify the JWT (HS256) against the HMAC key in Secrets Manager
     (RENDER_TOKEN_SECRET_ARN). The token pins one immutable artifact
     version: `{sub, aid, ver, sid, iss, aud, iat, exp}`.
  3. Read the version record from DynamoDB (ARTIFACTS_TABLE):
       PK = USER#{sub}
       SK = ARTIFACT#{aid}#V#{ver:05d}
  4. Fetch the content blob from S3 (ARTIFACTS_BUCKET) using the
     `content_key` stored on the record (the writer owns key
     construction; the verifier never reconstructs it).
  5. Return those exact bytes with strict security headers. The CDN's
     response-headers-policy also stamps the CSP, so the policy holds
     even if this handler is buggy (defense in depth).

This Lambda is a thin authenticated gate + header stamper, not a
templating layer: S3 holds the complete document to serve, and the
artifact writer owns all rendering. `#HEAD` is never read — the token
pins an exact version.

Markdown serve-type mapping: a Markdown artifact's version row carries
the authored `content_type` (`text/markdown`) so the SPA card/list stay
truthful, but S3 holds the writer's self-contained HTML render wrapper.
So records typed as Markdown are served with a `text/html` HTTP
content type — still the exact S3 bytes, only the response header is
mapped (header stamping, not templating). Must stay in sync with the
writer (`agents/builtin_tools/artifacts/service.py`).

No third-party dependencies: HS256 is HMAC-SHA256, verified with the
standard library. boto3 is provided by the Lambda runtime.

Boundary: this Lambda runs OUTSIDE the apis/* import boundary
(test_import_boundaries.py) — it's a standalone deployable, not part of
app-api or inference-api. Do not import from apis/ here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qs

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Pinned at deploy time via ArtifactsStack environment block. Read at
# module load; emptiness is checked at request time so a missing var
# becomes a clean runtime 500 with a log line rather than an import crash.
_FRAME_ANCESTOR = os.environ.get("FRAME_ANCESTOR_ORIGIN", "")
_CSP_SCRIPT_SRC = os.environ.get(
    "CSP_SCRIPT_SRC",
    "'self' 'unsafe-inline'",
)
_ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
_ARTIFACTS_TABLE = os.environ.get("ARTIFACTS_TABLE", "")
_RENDER_TOKEN_SECRET_ARN = os.environ.get("RENDER_TOKEN_SECRET_ARN", "")

_EXPECTED_ISS = "app-api"
_EXPECTED_AUD = "artifact-render"
# Tolerance for clock skew between the app-api minter and this Lambda.
# Both run in AWS so skew is sub-second; keep it tight.
_LEEWAY_SECONDS = 5
# Upper bound on token lifetime. The minter issues ~60–120s tokens; a
# token claiming a far-future exp is a minter bug or a forgery attempt,
# so cap the blast radius. `iat` is mandatory, so this always applies.
_MAX_TOKEN_LIFETIME_SECONDS = 600
# Cap content size to stay within the Lambda's 5s / 512MB envelope and
# to keep a single response bounded. Oversized blobs are a writer bug.
_MAX_CONTENT_BYTES = 5 * 1024 * 1024

# Module-scoped for container reuse across invocations.
_secrets_client = None
_s3_client = None
_ddb_table = None
_cached_signing_key: str | None = None


class _TokenError(Exception):
    """Render token is missing, malformed, or fails verification."""


class _ArtifactNotFound(Exception):
    """No version record or no backing object for the requested artifact."""


class _RenderConfigError(Exception):
    """Required environment / AWS configuration is missing or unusable."""


class _UnsupportedStorage(Exception):
    """Version record uses a storage class this handler can't serve yet."""


def _csp_header() -> str:
    """Build the artifact-origin CSP. Mirrors the CloudFront response-
    headers-policy so the policy is identical whether CloudFront sets it
    or the Lambda does (defense in depth)."""
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src {_CSP_SCRIPT_SRC}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self' data:",
            "connect-src 'none'",
            f"frame-ancestors {_FRAME_ANCESTOR}",
            "form-action 'none'",
            "base-uri 'none'",
        ]
    )


# Authored types whose S3 body is a writer-produced HTML render wrapper
# (see module docstring). Mirrors `_MARKDOWN_MIME_TYPES` in the writer's
# service.py — this Lambda is standalone (no apis/* imports) so the small
# duplication is by design; keep the two in sync.
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
_MARKDOWN_MIME_TYPES = frozenset({"text/markdown", "text/x-markdown"})


def _serve_content_type(stored: str) -> str:
    """HTTP content type to emit for a stored authored type.

    Markdown records hold an HTML render wrapper in S3, so they are
    served as HTML; every other type is served exactly as stored."""
    bare = (stored or "").split(";")[0].strip().lower()
    if bare in _MARKDOWN_MIME_TYPES:
        return _HTML_CONTENT_TYPE
    return stored


def _security_headers(content_type: str) -> dict[str, str]:
    return {
        "content-type": content_type,
        "content-security-policy": _csp_header(),
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }


def _error_html(message: str) -> str:
    """Generic error page. Never reflects token or claim values — keeps
    the surface free of injected content even though the CSP would
    neutralize it anyway."""
    return (
        "<!doctype html>"
        "<html><head>"
        "<meta charset='utf-8'>"
        "<title>Artifact unavailable</title>"
        "<style>body{font:14px system-ui;padding:2rem;color:#444}</style>"
        "</head><body>"
        "<h1>Artifact unavailable</h1>"
        f"<p>{message}</p>"
        "</body></html>"
    )


def _response(status: int, body: str, content_type: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": _security_headers(content_type),
        "body": body,
    }


def _error_response(status: int, message: str) -> dict[str, Any]:
    return _response(status, _error_html(message), "text/html; charset=utf-8")


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url JWT segment, restoring the stripped padding."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _signing_key() -> str:
    """Fetch and cache the HMAC signing key. The secret is a plain
    string (Secrets Manager `generateSecretString`, no JSON wrapper) —
    same shape as the BFF cookie data key. Cached for the container
    lifetime; on rotation the container eventually recycles, which is
    acceptable for short-lived render tokens."""
    global _secrets_client, _cached_signing_key
    if _cached_signing_key is not None:
        return _cached_signing_key
    if not _RENDER_TOKEN_SECRET_ARN:
        raise _RenderConfigError("RENDER_TOKEN_SECRET_ARN is not set")
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    try:
        secret = _secrets_client.get_secret_value(SecretId=_RENDER_TOKEN_SECRET_ARN)
    except ClientError as exc:
        raise _RenderConfigError("could not read render token secret") from exc
    key = secret.get("SecretString")
    if not key:
        raise _RenderConfigError("render token secret is empty")
    _cached_signing_key = key
    return key


def _verify_token(token: str) -> dict[str, Any]:
    """Verify an HS256 render token and return its validated claims.

    Implemented against the stdlib rather than PyJWT so the Lambda asset
    stays dependency-free. `alg` is pinned to HS256 explicitly to reject
    the `none` algorithm and HS/RS confusion."""
    parts = token.split(".")
    if len(parts) != 3:
        raise _TokenError("malformed token")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise _TokenError("unreadable header") from exc
    if not isinstance(header, dict):
        raise _TokenError("malformed header")
    if header.get("alg") != "HS256":
        raise _TokenError("unexpected token algorithm")

    expected_sig = hmac.new(
        _signing_key().encode("utf-8"),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided_sig = _b64url_decode(signature_b64)
    except ValueError as exc:
        raise _TokenError("unreadable signature") from exc
    # Constant-time compare — never short-circuit on the first byte.
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise _TokenError("signature mismatch")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise _TokenError("unreadable payload") from exc
    if not isinstance(claims, dict):
        raise _TokenError("malformed payload")

    if claims.get("iss") != _EXPECTED_ISS:
        raise _TokenError("unexpected issuer")
    if claims.get("aud") != _EXPECTED_AUD:
        raise _TokenError("unexpected audience")

    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise _TokenError("missing exp")
    if now > exp + _LEEWAY_SECONDS:
        raise _TokenError("token expired")

    # `iat` is mandatory: the lifetime cap is the blast-radius control for
    # a minter bug, and it can only be enforced relative to `iat`. The
    # cross-PR contract requires the minter to send it, so a missing `iat`
    # is itself a contract violation — reject rather than skip the cap.
    # `bool` is an `int` subclass — exclude it explicitly.
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        raise _TokenError("missing iat")
    if iat > now + _LEEWAY_SECONDS:
        raise _TokenError("token issued in the future")
    if exp - iat > _MAX_TOKEN_LIFETIME_SECONDS:
        raise _TokenError("token lifetime too long")

    sub = claims.get("sub")
    aid = claims.get("aid")
    ver = claims.get("ver")
    if not isinstance(sub, str) or not sub:
        raise _TokenError("missing sub")
    if not isinstance(aid, str) or not aid:
        raise _TokenError("missing aid")
    # `bool` is an `int` subclass — exclude it explicitly.
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise _TokenError("invalid ver")

    return claims


def _get_version_record(user_id: str, artifact_id: str, version: int) -> dict[str, Any]:
    global _ddb_table
    if not _ARTIFACTS_TABLE:
        raise _RenderConfigError("ARTIFACTS_TABLE is not set")
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(_ARTIFACTS_TABLE)
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _ddb_table.get_item(Key={"PK": f"USER#{user_id}", "SK": sk})
    except ClientError as exc:
        raise _RenderConfigError("artifact metadata lookup failed") from exc
    item = result.get("Item")
    if not item:
        raise _ArtifactNotFound("version record not found")
    return item


def _fetch_content(content_key: str) -> str:
    global _s3_client
    if not _ARTIFACTS_BUCKET:
        raise _RenderConfigError("ARTIFACTS_BUCKET is not set")
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    try:
        obj = _s3_client.get_object(Bucket=_ARTIFACTS_BUCKET, Key=content_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise _ArtifactNotFound("content object missing") from exc
        raise _RenderConfigError("content fetch failed") from exc
    content_length = obj.get("ContentLength")
    if isinstance(content_length, int) and content_length > _MAX_CONTENT_BYTES:
        raise _UnsupportedStorage("content exceeds size limit")
    raw = obj["Body"].read(_MAX_CONTENT_BYTES + 1)
    if len(raw) > _MAX_CONTENT_BYTES:
        raise _UnsupportedStorage("content exceeds size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _UnsupportedStorage("content is not valid utf-8") from exc


def _extract_token(event: dict[str, Any]) -> str:
    params = event.get("queryStringParameters") or {}
    token = params.get("t")
    if not token:
        raw = event.get("rawQueryString") or ""
        token = (parse_qs(raw).get("t") or [None])[0]
    if not token:
        raise _TokenError("missing render token")
    return token


def _request_method(event: dict[str, Any]) -> str:
    return (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", "GET")
        .upper()
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda Function URL handler. Payload format v2.0.

    SECURITY: never log `event`, `rawQueryString`, `queryStringParameters`,
    or the raw token — the render token is a bearer credential carried in
    the URL query string. Log identifiers (sub/aid/ver/sid) only.
    """
    method = _request_method(event)
    if method not in ("GET", "HEAD"):
        return _error_response(405, "Method not allowed.")

    try:
        token = _extract_token(event)
        claims = _verify_token(token)
    except _TokenError as exc:
        logger.warning("render token rejected: %s", exc)
        return _error_response(403, "This artifact link is invalid or has expired.")
    except _RenderConfigError as exc:
        logger.error("render config error during verification: %s", exc)
        return _error_response(500, "The artifact service is misconfigured.")

    user_id = claims["sub"]
    artifact_id = claims["aid"]
    version = claims["ver"]
    logger.info(
        "render request user=%s artifact=%s v=%s sid=%s",
        user_id,
        artifact_id,
        version,
        claims.get("sid"),
    )

    try:
        record = _get_version_record(user_id, artifact_id, version)
        storage = record.get("storage")
        if storage != "s3":
            raise _UnsupportedStorage(f"storage class {storage!r} not supported")
        content_key = record.get("content_key")
        if not isinstance(content_key, str) or not content_key:
            raise _ArtifactNotFound("version record has no content pointer")
        stored_content_type = record.get("content_type") or _HTML_CONTENT_TYPE
        content_type = _serve_content_type(stored_content_type)
        body = _fetch_content(content_key)
    except _ArtifactNotFound as exc:
        logger.warning(
            "artifact not found user=%s artifact=%s v=%s: %s",
            user_id,
            artifact_id,
            version,
            exc,
        )
        return _error_response(404, "This artifact could not be found.")
    except _UnsupportedStorage as exc:
        logger.error(
            "unsupported artifact content user=%s artifact=%s v=%s: %s",
            user_id,
            artifact_id,
            version,
            exc,
        )
        return _error_response(500, "This artifact could not be rendered.")
    except _RenderConfigError as exc:
        logger.error("render config error during fetch: %s", exc)
        return _error_response(500, "The artifact service is misconfigured.")

    if method == "HEAD":
        return _response(200, "", content_type)
    return _response(200, body, content_type)


# Local smoke test: `python handler.py` exercises the missing-token path
# (returns 403) with zero AWS calls — the token check precedes any client.
if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
