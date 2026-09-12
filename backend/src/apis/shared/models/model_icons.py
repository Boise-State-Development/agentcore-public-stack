"""Icons for managed models — the built-in logo slug and the uploaded override.

A model in the chat picker gets a left-aligned avatar, and there are two ways an
admin supplies one:

**A built-in logo slug** (``iconSlug``). The SPA already ships a light/dark SVG
pair per vendor under ``public/img/provider-logos/{slug}/``, which is what the
admin model catalog's quick-add cards render. Pointing at one of those is a
string on the record and costs no storage, no upload and no serve round trip —
and it stays a crisp, theme-correct vector, which a stored raster cannot be.
That is why this is not merely the fallback: for the vendors we ship, it is the
*better* answer.

**An uploaded image** (``iconKey`` → ``iconUrl``). For everything else — a
fine-tuned in-house model, a vendor we ship no logo for — the bytes go to S3 and
the record carries only the content-addressed key, exactly like Agent icons
(:mod:`apis.shared.assistants.icons`). Validation and storage are the shared
machinery in :mod:`apis.shared.images.icons`.

Precedence is upload → slug → (client-side) provider-name match → generic glyph.
An upload wins because it is the more specific, more deliberate act: an admin who
uploaded a file after choosing a slug meant the file.

Storage
-------
Objects land in the assistants asset bucket
(``S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME`` → ``{prefix}-rag-documents``), under a
prefix of their own:

    models/{model_id}/icons/{sha256[:16]}.{png|jpg}

``model_id`` here is the record's UUID (``ManagedModel.id``), not the Bedrock
model identifier — the latter contains ``:`` and ``.`` and changes when an admin
re-points a record at a new model version, which would orphan the object.
"""

from __future__ import annotations

import logging
from typing import Optional

from apis.shared.images.icons import (  # noqa: F401 - part of this module's surface
    ICON_MAX_BYTES,
    ICON_MIN_SOURCE,
    ICON_SIZE,
    IconError,
    IconStore,
    IconStoreError,
    content_digest,
    key_version,
    normalize_icon,
)

logger = logging.getLogger(__name__)

#: Built-in vendor logos shipped with the SPA. Each slug must have a
#: ``frontend/ai.client/public/img/provider-logos/{slug}/{light,dark}.svg`` pair —
#: adding a vendor means dropping the pair in *and* listing it here and in the
#: SPA's ``BUILTIN_MODEL_ICONS``. Validated on write so a typo is rejected at the
#: admin form rather than rendering an invisible tile for every user.
BUILTIN_MODEL_ICONS: tuple[str, ...] = ("amazon", "anthropic", "meta", "openai")


def is_builtin_icon_slug(slug: Optional[str]) -> bool:
    """Whether ``slug`` names a logo the SPA actually ships. Empty/None is valid
    input meaning "no built-in icon" — see :func:`normalize_icon_slug`."""
    return slug in BUILTIN_MODEL_ICONS


def normalize_icon_slug(slug: Optional[str], *, keep_clear_sentinel: bool = False) -> Optional[str]:
    """Validate a built-in slug, raising :class:`ValueError` for an unknown one.

    ``""`` is the wire value for *clear this*, and on a PATCH it must stay ``""``
    rather than collapse to ``None``: the update path drops ``None`` fields
    (``exclude_none``), so an admin who set a slug could otherwise never remove
    it. Hence ``keep_clear_sentinel`` — on for the update model, off everywhere
    else, where an absent slug and a cleared one mean the same thing.
    """
    if slug is None:
        return None
    if slug.strip() == "":
        return "" if keep_clear_sentinel else None
    slug = slug.strip().lower()
    if not is_builtin_icon_slug(slug):
        raise ValueError(
            f"Unknown icon '{slug}'. Built-in icons are: {', '.join(BUILTIN_MODEL_ICONS)}."
        )
    return slug


# ── keys and URLs ────────────────────────────────────────────────────────────


def build_model_icon_key(model_id: str, digest: str, ext: str) -> str:
    """``models/{model_id}/icons/{digest}.{ext}`` — the content-addressed key."""
    return f"models/{model_id}/icons/{digest}.{ext}"


def model_icon_version(icon_key: Optional[str]) -> Optional[str]:
    """The key's digest segment, served as the ETag and as ``iconUrl``'s ``?v=``."""
    return key_version(icon_key)


def model_icon_url(model_id: str, icon_key: Optional[str]) -> Optional[str]:
    """The read-shape ``iconUrl`` for a stored key, or ``None`` when unset.

    A relative app-api path (the SPA prefixes ``config.appApiUrl()``), pointing at
    the *user-facing* serve route rather than an admin one: every signed-in user
    renders these in the model picker, and only admins can reach ``/admin/*``.
    The ``?v=`` digest is what lets the response be cached ``immutable`` while a
    replacement takes effect immediately.
    """
    version = model_icon_version(icon_key)
    if not version:
        return None
    return f"/models/{model_id}/icon?v={version}"


# ── S3 ───────────────────────────────────────────────────────────────────────


class ModelIconStore(IconStore):
    """Put / get / delete managed-model icons in the assistants asset bucket."""

    def __init__(self, bucket_name: Optional[str] = None, s3_client: Optional[object] = None) -> None:
        super().__init__(label="model-icons", bucket_name=bucket_name, s3_client=s3_client)

    def put(self, *, model_id: str, content: bytes, ext: str, content_type: str) -> str:
        """Store normalized bytes and return the content-addressed key."""
        key = build_model_icon_key(model_id, content_digest(content), ext)
        return self.put_object(key=key, content=content, content_type=content_type)


_store: Optional[ModelIconStore] = None


def get_model_icon_store() -> ModelIconStore:
    """Process-wide store, bound on first use (the env is set by the time routes run)."""
    global _store
    if _store is None:
        _store = ModelIconStore()
    return _store
