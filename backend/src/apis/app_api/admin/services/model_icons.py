"""Upload / remove a managed model's icon.

The one place the three storage concerns meet: validation
(:mod:`apis.shared.images.icons`), the object write (S3), and the ``iconKey``
attribute write (``write_model_icon_key``).

Writing is admin-only — it rides the ``admin.models`` scope like every other
mutation on the model catalog. *Reading* is not: the icon renders in every
signed-in user's model picker, which is why the serve route lives on the
user-facing ``/models`` router instead (``apis.app_api.models.routes``).
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from apis.shared.models.managed_models import get_managed_model, write_model_icon_key
from apis.shared.models.model_icons import (
    IconError,
    IconStoreError,
    get_model_icon_store,
    model_icon_url,
    model_icon_version,
    normalize_icon,
)

logger = logging.getLogger(__name__)


class ModelIconError(Exception):
    """An icon operation we cannot complete, with a message written for the admin.

    ``status_code`` maps to the HTTP response: 404 missing model or missing icon,
    400 an image that fails the limits, 503 storage unconfigured.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _require_model(model_id: str):
    model = await get_managed_model(model_id)
    if not model:
        raise ModelIconError(f"Model not found: {model_id}", status_code=404)
    return model


async def upload_model_icon(model_id: str, content: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Validate, store and record a new icon; return ``(icon_key, icon_url)``.

    The old object is deleted only *after* the record points at the new one, so a
    failure in the middle leaves the model with its previous icon rather than
    none. The key is content-addressed, which makes re-uploading the same image
    idempotent — and makes the delete a no-op in exactly that case, which is why
    it is skipped when the key is unchanged.
    """
    model = await _require_model(model_id)

    try:
        data, ext, content_type = normalize_icon(content)
    except IconError as e:
        raise ModelIconError(str(e), status_code=400) from e

    store = get_model_icon_store()
    try:
        key = store.put(model_id=model_id, content=data, ext=ext, content_type=content_type)
    except IconStoreError as e:
        logger.error(f"Icon storage unavailable for model {model_id}: {e}")
        raise ModelIconError("Icon storage is unavailable.", status_code=503) from e

    previous = model.icon_key
    await write_model_icon_key(model_id, key)
    if previous and previous != key:
        store.delete(previous)

    logger.info(f"🖼️ model-icons: uploaded icon for model {model_id}")
    return key, model_icon_url(model_id, key)


async def remove_model_icon(model_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Clear the uploaded icon, returning the model to its ``iconSlug`` (or the
    client-side provider fallback when it has none)."""
    model = await _require_model(model_id)
    previous = model.icon_key

    await write_model_icon_key(model_id, None)
    if previous:
        get_model_icon_store().delete(previous)

    logger.info(f"🖼️ model-icons: removed icon for model {model_id}")
    return None, None


async def read_model_icon(model_id: str) -> Tuple[bytes, str, str]:
    """Return ``(bytes, content_type, version)`` for a model's uploaded icon.

    No access check beyond being signed in: the catalog already hands every user
    this model's name, provider and pricing, so its logo is no new disclosure —
    and gating it on the per-role model grant would render a broken tile in the
    admin form for a model the admin's own roles happen not to grant.
    """
    model = await _require_model(model_id)
    if not model.icon_key:
        raise ModelIconError("This model has no icon.", status_code=404)

    try:
        data, content_type = get_model_icon_store().get(model.icon_key)
    except IconStoreError as e:
        # A key that outlived its object: 404 rather than 500, so the SPA's <img>
        # error path falls through to the slug/provider fallback instead of
        # showing a broken tile.
        logger.warning(f"Icon object missing for model {model_id}: {e}")
        raise ModelIconError("This model has no icon.", status_code=404) from e

    return data, content_type, model_icon_version(model.icon_key) or ""
