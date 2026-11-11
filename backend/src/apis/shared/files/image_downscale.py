"""The image a model is sent: downscaled once, at upload, deterministically.

``docs/specs/unified-attachment-delivery.md`` §2.1, §4.3. An image is
downscaled server-side when its upload completes, to the largest size any
curated model reads, so no image is refused for its size and none exceeds the
Bedrock image limit. The result is stored once, beside the original, and every
turn (and phase 4's restore from a handle) sends those stored bytes.

**The target.** Claude 4.7 and later read images up to a 2,576 px long edge and
4,784 visual tokens (one token per 28 x 28 px patch); every other curated model
reads less and resizes further on its side (Claude's standard tier is 1,568 px;
the hosted OpenAI models fit 2,048 px). So an image larger than either limit is
scaled, aspect kept, to the largest size inside both. That also keeps every
image under the 3,000 px limit Anthropic documents for requests with more than
20 image and document blocks, which is what lets a message carry 20 images.

**The byte cap.** Bedrock refuses an image over 5 MB *base64-encoded*, which is
3,932,160 raw bytes. :data:`MODEL_IMAGE_MAX_BYTES` sits under that with margin.

**Deterministic.** The same input bytes give the same output bytes, so a
re-run (a replaced file, a backfill) writes what the first run wrote, and the
stored object is what restore re-reads: fixed resampling, fixed encoder
settings, a fixed quality ladder, no timestamps, EXIF dropped after its
orientation is applied. The ICC profile is kept, since it is the input's own
bytes. (Byte-identity holds for one Pillow and codec build; the image pin in
``pyproject.toml`` is what keeps it across deploys.)

**Two copies.** ``high`` (above) is what a turn sends by default; ``compact``
is Claude's standard tier (1,568 px, 1,568 tokens) under 340 KB, which the
planner sends when the high copy does not fit the message's budget. Twenty
compact copies fit any budget, so an image is never dropped for its bytes.

**What is kept as uploaded.** An image already inside both limits, under the
byte cap, in a format every model reads, and upright, is sent as uploaded:
:func:`plan_model_image` returns ``None`` for it, and the row records its
dimensions with the original as its model image.

Pure: bytes in, bytes out. No I/O, no AWS.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Optional, Tuple

#: Claude 4.7+'s high-resolution tier (Anthropic vision docs, read 2026-11-10).
MAX_LONG_EDGE_PX = 2_576
MAX_VISUAL_TOKENS = 4_784
#: One visual token per patch of this many pixels square.
VISUAL_PATCH_PX = 28

#: Under Bedrock's 5 MB base64 image limit (3,932,160 raw bytes), with margin.
MODEL_IMAGE_MAX_BYTES = 3_700_000

#: The compact copy: Claude's standard tier (every model before 4.7 reads at
#: most 1,568 px and 1,568 visual tokens), under a byte cap small enough that a
#: message's 20 images always fit the planner's combined budget (20 x 340 KB is
#: 6.8 MB, under the 7.0 MB that protects the 10 MB AgentCore Memory event, and
#: far under the 32 MB request).
COMPACT_LONG_EDGE_PX = 1_568
COMPACT_VISUAL_TOKENS = 1_568
COMPACT_IMAGE_MAX_BYTES = 340_000

#: Refuse to decode anything larger: a decompression bomb, or a panorama app-api
#: cannot hold. A JPEG is decoded at a reduced scale (``draft``) before this
#: matters; a PNG of this size is ~400 MB decoded.
MAX_SOURCE_PIXELS = 100_000_000

#: Encoder settings. Changing any of them changes every stored model image, so
#: bump :data:`MODEL_IMAGE_VERSION` with them.
MODEL_IMAGE_VERSION = 1
_JPEG_QUALITIES = (85, 75, 62, 50)
_WEBP_QUALITIES = (85, 72, 60)
_PNG_COMPRESS_LEVEL = 6
#: When no quality fits the byte cap, shrink by this factor and try again.
_SHRINK = 0.8
_MAX_SHRINKS = 6

_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_EXIF_ORIENTATION = 0x0112


@dataclass(frozen=True)
class Tier:
    """A size a model copy is made at: long edge, visual tokens, encoded bytes."""

    name: str
    long_edge: int
    visual_tokens: int
    max_bytes: int


#: What Claude 4.7+ reads: the copy a turn sends by default.
HIGH = Tier("high", MAX_LONG_EDGE_PX, MAX_VISUAL_TOKENS, MODEL_IMAGE_MAX_BYTES)
#: What every earlier model reads, small enough for 20 to a message.
COMPACT = Tier("compact", COMPACT_LONG_EDGE_PX, COMPACT_VISUAL_TOKENS, COMPACT_IMAGE_MAX_BYTES)


class ImageUnreadableError(ValueError):
    """The bytes are not an image this module can decode, or are too large to."""


@dataclass(frozen=True)
class ModelImage:
    """The bytes a model is sent for one image, and what they are."""

    data: bytes
    mime_type: str
    width: int
    height: int

    @property
    def extension(self) -> str:
        return _EXT_BY_MIME[self.mime_type]


@dataclass(frozen=True)
class ImageInfo:
    """The uploaded image's own size and format, read without a full decode."""

    width: int
    height: int
    mime_type: Optional[str]
    #: An EXIF orientation that rotates or mirrors the pixels.
    needs_transpose: bool


def visual_tokens(width: int, height: int) -> int:
    """Claude's visual-token count for an image of this size (one per 28 px patch)."""
    return math.ceil(max(1, width) / VISUAL_PATCH_PX) * math.ceil(max(1, height) / VISUAL_PATCH_PX)


def target_size(width: int, height: int, tier: "Tier" = None) -> Tuple[int, int]:
    """The largest size, aspect kept, inside the tier's long-edge and visual-token limits.

    An image already inside both comes back unchanged. Never upscales.
    """
    tier = tier or HIGH
    width, height = max(1, int(width)), max(1, int(height))
    scale = min(1.0, tier.long_edge / max(width, height))
    w, h = _scaled(width, height, scale)
    # The token limit is on patches, so step down until the patch count fits;
    # the closed form lands within a step or two of the answer.
    if visual_tokens(w, h) > tier.visual_tokens:
        scale = min(scale, math.sqrt(tier.visual_tokens * VISUAL_PATCH_PX**2 / (width * height)))
        w, h = _scaled(width, height, scale)
        while visual_tokens(w, h) > tier.visual_tokens and scale > 0:
            scale *= 0.995
            w, h = _scaled(width, height, scale)
    return w, h


def _scaled(width: int, height: int, scale: float) -> Tuple[int, int]:
    return max(1, int(width * scale)), max(1, int(height * scale))


def inspect_image(raw: bytes) -> ImageInfo:
    """The image's size, format and orientation, from its header. Raises :class:`ImageUnreadableError`."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            orientation = img.getexif().get(_EXIF_ORIENTATION, 1) if hasattr(img, "getexif") else 1
            mime = _MIME_BY_FORMAT.get(img.format or "")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise ImageUnreadableError(str(e)) from e
    return ImageInfo(width=width, height=height, mime_type=mime, needs_transpose=orientation not in (None, 1))


def needs_model_copy(info: ImageInfo, size_bytes: int, declared_mime: str, tier: "Tier" = None) -> bool:
    """Whether the uploaded bytes cannot be sent as they are at this tier."""
    tier = tier or HIGH
    if info.mime_type is None or info.mime_type != (declared_mime or "").lower():
        return True
    if info.needs_transpose or size_bytes > tier.max_bytes:
        return True
    return target_size(info.width, info.height, tier) != (info.width, info.height)


def plan_model_image(raw: bytes, declared_mime: str) -> Tuple[ImageInfo, Optional[ModelImage]]:
    """The uploaded image's info and, when it cannot be sent as is, its model copy.

    Raises :class:`ImageUnreadableError` for bytes that are not a readable image.
    """
    info = inspect_image(raw)
    if not needs_model_copy(info, len(raw), declared_mime):
        return info, None
    return info, downscale(raw)


def plan_model_images(
    raw: bytes, declared_mime: str
) -> Tuple[ImageInfo, Optional[ModelImage], Optional[ModelImage]]:
    """The image's info, its ``high`` copy and its ``compact`` copy.

    Each copy is ``None`` when the uploaded bytes already satisfy that tier, so
    the original is sent as is. Both are made from the uploaded bytes, never one
    from the other, so each is a pure function of the input.
    """
    info = inspect_image(raw)
    high = downscale(raw, HIGH) if needs_model_copy(info, len(raw), declared_mime, HIGH) else None
    compact = downscale(raw, COMPACT) if needs_model_copy(info, len(raw), declared_mime, COMPACT) else None
    return info, high, compact


def downscale(raw: bytes, tier: "Tier" = None) -> ModelImage:
    """The model copy of an image: upright, inside both limits, under the byte cap.

    A JPEG stays JPEG. Anything else is PNG while it fits (lossless, so text in
    a screenshot stays sharp), then JPEG when it has no transparency or WebP
    when it does. Raises :class:`ImageUnreadableError`.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    tier = tier or HIGH
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_SOURCE_PIXELS and img.format != "JPEG":
            raise ImageUnreadableError(f"Image is {img.width}x{img.height}, over {MAX_SOURCE_PIXELS} pixels")
        source_format = img.format
        icc = img.info.get("icc_profile")
        orientation = img.getexif().get(_EXIF_ORIENTATION, 1)
        w, h = target_size(*_oriented(img.size, orientation), tier)
        if source_format == "JPEG":
            # Decode at the smallest power-of-two scale that is still at least
            # the target: a 100 MP photo is never held at full size.
            draft_size = (w, h) if orientation in (1, 2, 3, 4) else (h, w)
            img.draft("RGB", draft_size)
        img.load()
    except ImageUnreadableError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as e:
        raise ImageUnreadableError(str(e)) from e

    img = ImageOps.exif_transpose(img) or img
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    alpha = _has_alpha(img)
    img = img.convert("RGBA" if alpha else "RGB")

    for _ in range(_MAX_SHRINKS + 1):
        frame = img if img.size == (w, h) else img.resize((w, h), Image.Resampling.LANCZOS)
        encoded = _encode_within_cap(frame, source_format, alpha, icc, tier.max_bytes)
        if encoded is not None:
            data, mime = encoded
            return ModelImage(data=data, mime_type=mime, width=frame.width, height=frame.height)
        w, h = _scaled(w, h, _SHRINK)
    raise ImageUnreadableError("Image could not be encoded under the model byte limit")


def _oriented(size: Tuple[int, int], orientation: int) -> Tuple[int, int]:
    # Orientations 5-8 swap width and height.
    return (size[1], size[0]) if orientation in (5, 6, 7, 8) else size


def _has_alpha(img) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        return img.getchannel("A").getextrema()[0] < 255
    if img.mode == "P" and "transparency" in img.info:
        return True
    return False


def _encode_within_cap(frame, source_format: Optional[str], alpha: bool, icc: Optional[bytes], cap: int):
    if source_format != "JPEG":
        data = _save(frame, "PNG", compress_level=_PNG_COMPRESS_LEVEL, optimize=False, icc_profile=icc)
        if len(data) <= cap:
            return data, "image/png"
    if alpha:
        for quality in _WEBP_QUALITIES:
            data = _save(frame, "WEBP", quality=quality, method=4, exact=False, icc_profile=icc)
            if len(data) <= cap:
                return data, "image/webp"
        return None
    for quality in _JPEG_QUALITIES:
        data = _save(
            frame, "JPEG", quality=quality, subsampling=2, optimize=False, progressive=False, icc_profile=icc
        )
        if len(data) <= cap:
            return data, "image/jpeg"
    return None


def _save(frame, fmt: str, **params) -> bytes:
    if params.get("icc_profile") is None:
        params.pop("icc_profile", None)
    buf = io.BytesIO()
    frame.save(buf, format=fmt, **params)
    return buf.getvalue()


def model_image_key(s3_key: str, mime_type: str, tier: "Tier" = None) -> str:
    """Where an upload's model copy is stored: beside its other ``_derived/`` sidecars."""
    from apis.shared.files.large_document import derived_prefix

    suffix = "" if (tier or HIGH) is HIGH else f"-{tier.name}"
    return f"{derived_prefix(s3_key)}model-image{suffix}.{_EXT_BY_MIME[mime_type]}"


def model_image_record(
    *,
    key: str,
    mime_type: str,
    size_bytes: int,
    width: int,
    height: int,
    source: ImageInfo,
    downscaled: bool,
    compact: Optional[dict] = None,
) -> dict:
    """The row's ``modelImage`` map: where the model's bytes are and what they are.

    Numbers, a key and a MIME type only. ``downscaled`` is false when the
    original is sent as uploaded (``key`` is then the original's).
    """
    return {
        "version": MODEL_IMAGE_VERSION,
        "key": key,
        "mimeType": mime_type,
        "sizeBytes": int(size_bytes),
        "width": int(width),
        "height": int(height),
        "sourceWidth": int(source.width),
        "sourceHeight": int(source.height),
        "downscaled": bool(downscaled),
        # The ``compact`` copy: ``{key, mimeType, sizeBytes, width, height}``.
        "compact": dict(compact) if compact else None,
    }


__all__ = [
    "COMPACT",
    "COMPACT_IMAGE_MAX_BYTES",
    "HIGH",
    "Tier",
    "plan_model_images",
    "ImageInfo",
    "ImageUnreadableError",
    "MAX_LONG_EDGE_PX",
    "MAX_VISUAL_TOKENS",
    "MODEL_IMAGE_MAX_BYTES",
    "MODEL_IMAGE_VERSION",
    "ModelImage",
    "downscale",
    "inspect_image",
    "model_image_key",
    "model_image_record",
    "needs_model_copy",
    "plan_model_image",
    "target_size",
    "visual_tokens",
]
