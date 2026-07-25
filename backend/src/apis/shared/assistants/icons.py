"""Square-icon storage for Agents (Marketplace Phase 4, D5).

An Agent's identity in the store is a 512×512 square icon. The **bytes live in S3 and
the record carries only the object key** — the same rule MCP App icons learned the hard
way against the 400 KB DynamoDB item limit, except here the limit would be hit by design
rather than by accident, since the icon ceiling *is* 400 KB.

Storage
-------
Objects land in the existing assistants asset bucket
(``S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME`` → ``{prefix}-rag-documents``), under the same
``assistants/{agent_id}/`` prefix that assistant documents already use:

    assistants/{agent_id}/icons/{sha256[:16]}.{png|jpg}

The key is **content-addressed**, which buys two things: re-uploading the same image is a
no-op rather than a new object, and the digest doubles as the cache version — the serve
route hands it out as the ETag and the read shapes hang it off ``?v=`` so an icon can be
cached ``immutable`` and still change the moment a new one is uploaded.

Validation
----------
Everything a caller sends is untrusted, so the ``Content-Type`` header is ignored and the
format is sniffed from the bytes. Beyond the D5 limits (PNG or JPEG, ≤ 400 KB, square),
the image is **always re-encoded** even when it already measures 512×512. That is not
redundant work: re-encoding is what strips EXIF, and an author uploading a phone photo as
an icon would otherwise publish its GPS coordinates to the whole institution.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Optional, Tuple

try:  # boto3 is absent in some local-dev setups
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    ClientError = Exception  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# D5 limits.
ICON_MAX_BYTES = 400 * 1024
ICON_SIZE = 512
# Below this, upscaling to 512 produces a soft tile that reads worse than the generated
# gradient it replaced — so we decline rather than accept a downgrade.
ICON_MIN_SOURCE = 256
# A hand-cropped square is often off by a pixel; a 4:3 photo is not.
ICON_SQUARE_TOLERANCE_PX = 2

_FORMAT_EXT = {"PNG": "png", "JPEG": "jpg"}
_EXT_CONTENT_TYPE = {"png": "image/png", "jpg": "image/jpeg"}

# AWS-managed (SSE-S3 / AES256) encryption, matching the bucket default.
_SSE_ALGORITHM = "AES256"


class IconError(ValueError):
    """An icon the author cannot publish, with a message written for the author.

    Every message names the limit *and* what was actually supplied, because "invalid
    image" sends someone back to a file picker with nothing to change.
    """


class IconStoreError(RuntimeError):
    """Storage is unavailable or the object could not be read/written."""


# ── keys and URLs ────────────────────────────────────────────────────────────────────


def build_icon_key(agent_id: str, digest: str, ext: str) -> str:
    """``assistants/{agent_id}/icons/{digest}.{ext}`` — the content-addressed key."""
    return f"assistants/{agent_id}/icons/{digest}.{ext}"


def icon_version(icon_key: Optional[str]) -> Optional[str]:
    """The cache version carried by a key: its digest segment.

    Used as the ``?v=`` on ``iconUrl`` and as the ETag on the serve route, so a stored
    icon can be cached ``immutable`` while a replacement busts it immediately.
    """
    if not icon_key:
        return None
    return icon_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def icon_url(agent_id: str, icon_key: Optional[str]) -> Optional[str]:
    """Resolve the read-shape ``iconUrl`` for a stored key, or ``None`` when unset.

    A **relative app-api path**, not a presigned S3 URL and not a CloudFront path:

    * Presigning would hand out a different string on every response, so a browsing user
      re-downloads every shelf icon on every page view and the JSON stops being
      cacheable — for an asset whose whole job is to be fetched repeatedly.
    * A CloudFront path would need its own origin + behavior over the documents bucket,
      which is a CDK deploy for something the existing same-origin ``/api/*`` behavior
      already reaches. The URL shape here is a stable path, so adding that behavior later
      is a pure infra change with no contract break.

    Relative because the SPA prefixes ``config.appApiUrl()`` (``/api`` in prod), and the
    container has no reliable knowledge of its own public origin.
    """
    version = icon_version(icon_key)
    if not version:
        return None
    return f"/agents/{agent_id}/icon?v={version}"


# ── validation / normalization ───────────────────────────────────────────────────────


def normalize_icon(content: bytes) -> Tuple[bytes, str, str]:
    """Validate and normalize an uploaded icon.

    Returns ``(bytes, ext, content_type)`` for a 512×512, metadata-free PNG or JPEG.
    Raises :class:`IconError` with an author-facing message on anything it declines.
    """
    from PIL import Image, UnidentifiedImageError  # lazy: keeps PIL off every importer

    if not content:
        raise IconError("The uploaded file is empty.")
    if len(content) > ICON_MAX_BYTES:
        raise IconError(
            f"Icons must be {ICON_MAX_BYTES // 1024} KB or smaller "
            f"(this one is {len(content) // 1024} KB)."
        )

    try:
        image = Image.open(io.BytesIO(content))
        source_format = image.format
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise IconError("Icons must be a PNG or JPEG image.") from e

    if source_format not in _FORMAT_EXT:
        raise IconError(
            f"Icons must be a PNG or JPEG image (this one is {source_format or 'an unknown format'})."
        )

    width, height = image.size
    if abs(width - height) > ICON_SQUARE_TOLERANCE_PX:
        raise IconError(f"Icons must be square (this one is {width}×{height}).")
    if min(width, height) < ICON_MIN_SOURCE:
        raise IconError(
            f"Icons must be at least {ICON_MIN_SOURCE}×{ICON_MIN_SOURCE} "
            f"(this one is {width}×{height})."
        )

    ext = _FORMAT_EXT[source_format]
    # Re-encoding always happens — see the module docstring on EXIF. LANCZOS because a
    # 28px tile is a 18× downscale of the stored icon and cheaper filters alias badly.
    image = image.convert("RGBA" if ext == "png" else "RGB")
    if (width, height) != (ICON_SIZE, ICON_SIZE):
        image = image.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)

    encoded = _encode_within_limit(image, ext)
    if encoded is None:
        raise IconError(
            f"This icon could not be stored under {ICON_MAX_BYTES // 1024} KB. "
            "Try a simpler image or fewer colors."
        )
    data, ext = encoded
    return data, ext, _EXT_CONTENT_TYPE[ext]


def _encode_within_limit(image, ext: str) -> Optional[Tuple[bytes, str]]:
    """Encode at 512×512 under the size ceiling, degrading in defined steps.

    A downscale to 512 almost always lands well under 400 KB; this ladder exists for the
    input that arrives *already* 512×512 and near the ceiling, where re-encoding could
    push it over. Each rung is deliberate rather than a retry loop: JPEG loses quality,
    an opaque PNG becomes a JPEG, and a transparent PNG loses colors but keeps its alpha.
    """
    from PIL import Image

    if ext == "jpg":
        for quality in (92, 85, 78):
            data = _save(image, "JPEG", quality=quality, optimize=True, progressive=True)
            if len(data) <= ICON_MAX_BYTES:
                return data, "jpg"
        return None

    data = _save(image, "PNG", optimize=True)
    if len(data) <= ICON_MAX_BYTES:
        return data, "png"

    has_alpha = image.getchannel("A").getextrema()[0] < 255
    if not has_alpha:
        for quality in (92, 85):
            data = _save(image.convert("RGB"), "JPEG", quality=quality, optimize=True)
            if len(data) <= ICON_MAX_BYTES:
                return data, "jpg"
        return None

    # FASTOCTREE, not the default MEDIANCUT: it is the only method Pillow will quantize
    # an RGBA image with, and this rung exists precisely to keep the alpha.
    quantized = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    data = _save(quantized, "PNG", optimize=True)
    return (data, "png") if len(data) <= ICON_MAX_BYTES else None


def _save(image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def content_digest(content: bytes) -> str:
    """The 16-hex-char content address used in the key and as the cache version."""
    return hashlib.sha256(content).hexdigest()[:16]


# ── S3 ───────────────────────────────────────────────────────────────────────────────


class AgentIconStore:
    """Put / get / delete agent icons in the assistants asset bucket."""

    def __init__(self, bucket_name: Optional[str] = None, s3_client: Optional[object] = None) -> None:
        self.bucket_name = bucket_name or os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
        # Lazily constructed so importing this module never needs AWS credentials;
        # tests inject a client.
        self._s3 = s3_client

    @property
    def enabled(self) -> bool:
        return bool(self.bucket_name) and boto3 is not None

    def _client(self):
        if self._s3 is None:
            if boto3 is None:  # pragma: no cover - import-guarded above
                raise IconStoreError("icon storage unavailable: boto3 is not installed")
            self._s3 = boto3.client("s3")
        return self._s3

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise IconStoreError(
                "icon storage is not configured (S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME is unset)"
            )

    def put(self, *, agent_id: str, content: bytes, ext: str, content_type: str) -> str:
        """Store normalized bytes and return the content-addressed key."""
        self._require_enabled()
        key = build_icon_key(agent_id, content_digest(content), ext)
        try:
            self._client().put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type,
                ServerSideEncryption=_SSE_ALGORITHM,
                # The object is immutable by construction (the key is its digest), so the
                # cache directive belongs on the object as much as on the serve response.
                CacheControl="public, max-age=31536000, immutable",
            )
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.error(f"agent-icons: put failed for agent={agent_id} key={key}: {e}")
            raise IconStoreError(f"failed to store icon for agent '{agent_id}'") from e

        logger.info(f"🖼️ agent-icons: stored agent={agent_id} key={key} ({len(content)} bytes)")
        return key

    def get(self, icon_key: str) -> Tuple[bytes, str]:
        """Return ``(bytes, content_type)`` for a stored icon."""
        self._require_enabled()
        try:
            response = self._client().get_object(Bucket=self.bucket_name, Key=icon_key)
            body = response["Body"].read()
            return body, response.get("ContentType") or "application/octet-stream"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise IconStoreError(f"icon not found at key '{icon_key}'") from e
            logger.error(f"agent-icons: get failed for key={icon_key}: {e}")
            raise IconStoreError(f"failed to read icon at key '{icon_key}'") from e

    def delete(self, icon_key: str) -> None:
        """Best-effort delete. Never raises: a replaced icon's old object going missing
        is not a reason to fail the upload that replaced it."""
        if not self.enabled or not icon_key:
            return
        try:
            self._client().delete_object(Bucket=self.bucket_name, Key=icon_key)
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.warning(f"agent-icons: delete failed for key={icon_key}: {e}")


_store: Optional[AgentIconStore] = None


def get_icon_store() -> AgentIconStore:
    """Process-wide store, bound on first use (the env is set by the time routes run)."""
    global _store
    if _store is None:
        _store = AgentIconStore()
    return _store
