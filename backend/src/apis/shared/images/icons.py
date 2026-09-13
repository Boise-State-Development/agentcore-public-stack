"""Square-icon validation, normalization and object storage.

The owner-specific parts of an icon — what its S3 key looks like, what URL serves
it — belong to whatever owns the record (``apis.shared.assistants.icons`` for
Agents, ``apis.shared.models.model_icons`` for managed models). Everything below
is the part that must not be written twice.

Validation
----------
Everything a caller sends is untrusted, so the ``Content-Type`` header is ignored
and the format is sniffed from the bytes. Beyond the limits (PNG or JPEG,
≤ 400 KB, square), the image is **always re-encoded** even when it already
measures 512×512. That is not redundant work: re-encoding is what strips EXIF,
and someone uploading a phone photo as an icon would otherwise publish its GPS
coordinates to the whole institution.

Storage
-------
:class:`IconStore` is the generic put/get/delete against one bucket. Keys are
**content-addressed** by the caller, which buys two things: re-uploading the same
image is a no-op rather than a new object, and the digest doubles as the cache
version — a serve route hands it out as the ETag and read shapes hang it off
``?v=`` so an icon can be cached ``immutable`` and still change the moment a new
one is uploaded.
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

# D5 limits (Agent Marketplace), reused verbatim for every other icon: the
# ceiling exists because the *record* has a 400 KB DynamoDB item limit, and the
# rendering sizes are the same tiles everywhere.
ICON_MAX_BYTES = 400 * 1024
ICON_SIZE = 512
# Below this, upscaling to 512 produces a soft tile that reads worse than the
# generated gradient it replaced — so we decline rather than accept a downgrade.
ICON_MIN_SOURCE = 256
# A hand-cropped square is often off by a pixel; a 4:3 photo is not.
ICON_SQUARE_TOLERANCE_PX = 2

_FORMAT_EXT = {"PNG": "png", "JPEG": "jpg"}
_EXT_CONTENT_TYPE = {"png": "image/png", "jpg": "image/jpeg"}

# AWS-managed (SSE-S3 / AES256) encryption, matching the bucket default.
_SSE_ALGORITHM = "AES256"


class IconError(ValueError):
    """An icon the caller cannot store, with a message written for that caller.

    Every message names the limit *and* what was actually supplied, because
    "invalid image" sends someone back to a file picker with nothing to change.
    """


class IconStoreError(RuntimeError):
    """Storage is unavailable or the object could not be read/written."""


# ── validation / normalization ───────────────────────────────────────────────


def normalize_icon(content: bytes) -> Tuple[bytes, str, str]:
    """Validate and normalize an uploaded icon.

    Returns ``(bytes, ext, content_type)`` for a 512×512, metadata-free PNG or
    JPEG. Raises :class:`IconError` with a caller-facing message on anything it
    declines.
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
    # Re-encoding always happens — see the module docstring on EXIF. LANCZOS
    # because a 28px tile is an 18× downscale of the stored icon and cheaper
    # filters alias badly.
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

    A downscale to 512 almost always lands well under 400 KB; this ladder exists
    for the input that arrives *already* 512×512 and near the ceiling, where
    re-encoding could push it over. Each rung is deliberate rather than a retry
    loop: JPEG loses quality, an opaque PNG becomes a JPEG, and a transparent PNG
    loses colors but keeps its alpha.
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

    # FASTOCTREE, not the default MEDIANCUT: it is the only method Pillow will
    # quantize an RGBA image with, and this rung exists precisely to keep the alpha.
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


def key_version(icon_key: Optional[str]) -> Optional[str]:
    """The cache version carried by a content-addressed key: its digest segment.

    Used as the ``?v=`` on an icon URL and as the ETag on a serve route, so a
    stored icon can be cached ``immutable`` while a replacement busts it
    immediately.
    """
    if not icon_key:
        return None
    return icon_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]


# ── S3 ───────────────────────────────────────────────────────────────────────


class IconStore:
    """Put / get / delete icon objects in one bucket.

    ``bucket_env`` is read lazily at construction; ``label`` only shapes log
    lines and the "storage is not configured" message, so an operator reading a
    log knows which feature went quiet.
    """

    def __init__(
        self,
        *,
        bucket_env: str = "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME",
        label: str = "icons",
        bucket_name: Optional[str] = None,
        s3_client: Optional[object] = None,
    ) -> None:
        self.bucket_env = bucket_env
        self.label = label
        self.bucket_name = bucket_name or os.environ.get(bucket_env)
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
                f"{self.label}: icon storage is not configured ({self.bucket_env} is unset)"
            )

    def put_object(self, *, key: str, content: bytes, content_type: str) -> str:
        """Store bytes at ``key`` and return it."""
        self._require_enabled()
        try:
            self._client().put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type,
                ServerSideEncryption=_SSE_ALGORITHM,
                # The object is immutable by construction (the key is its digest),
                # so the cache directive belongs on the object as much as on the
                # serve response.
                CacheControl="public, max-age=31536000, immutable",
            )
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.error(f"{self.label}: put failed for key={key}: {e}")
            raise IconStoreError(f"failed to store icon at key '{key}'") from e

        logger.info(f"🖼️ {self.label}: stored key={key} ({len(content)} bytes)")
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
            logger.error(f"{self.label}: get failed for key={icon_key}: {e}")
            raise IconStoreError(f"failed to read icon at key '{icon_key}'") from e

    def delete(self, icon_key: str) -> None:
        """Best-effort delete. Never raises: a replaced icon's old object going
        missing is not a reason to fail the upload that replaced it."""
        if not self.enabled or not icon_key:
            return
        try:
            self._client().delete_object(Bucket=self.bucket_name, Key=icon_key)
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.warning(f"{self.label}: delete failed for key={icon_key}: {e}")
