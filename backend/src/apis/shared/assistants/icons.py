"""Square-icon storage for Agents (Marketplace Phase 4, D5).

An Agent's identity in the store is a 512×512 square icon. The **bytes live in S3 and
the record carries only the object key** — the same rule MCP App icons learned the hard
way against the 400 KB DynamoDB item limit, except here the limit would be hit by design
rather than by accident, since the icon ceiling *is* 400 KB.

Validation, normalization (including the EXIF-stripping re-encode) and the S3 put/get/
delete live in :mod:`apis.shared.images.icons` — managed models attach icons the same
way, and one validator is the point. What stays here is the part that is about Agents:
the key layout and the URL that serves it.

Storage
-------
Objects land in the existing assistants asset bucket
(``S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME`` → ``{prefix}-rag-documents``), under the same
``assistants/{agent_id}/`` prefix that assistant documents already use:

    assistants/{agent_id}/icons/{sha256[:16]}.{png|jpg}
"""

from __future__ import annotations

import logging
from typing import Optional

from apis.shared.images.icons import (  # noqa: F401 - re-exported for existing importers
    ICON_MAX_BYTES,
    ICON_MIN_SOURCE,
    ICON_SIZE,
    ICON_SQUARE_TOLERANCE_PX,
    IconError,
    IconStore,
    IconStoreError,
    content_digest,
    key_version,
    normalize_icon,
)

logger = logging.getLogger(__name__)


# ── keys and URLs ────────────────────────────────────────────────────────────────────


def build_icon_key(agent_id: str, digest: str, ext: str) -> str:
    """``assistants/{agent_id}/icons/{digest}.{ext}`` — the content-addressed key."""
    return f"assistants/{agent_id}/icons/{digest}.{ext}"


def icon_version(icon_key: Optional[str]) -> Optional[str]:
    """The cache version carried by a key: its digest segment.

    Used as the ``?v=`` on ``iconUrl`` and as the ETag on the serve route, so a stored
    icon can be cached ``immutable`` while a replacement busts it immediately.
    """
    return key_version(icon_key)


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


# ── S3 ───────────────────────────────────────────────────────────────────────────────


class AgentIconStore(IconStore):
    """Put / get / delete agent icons in the assistants asset bucket."""

    def __init__(self, bucket_name: Optional[str] = None, s3_client: Optional[object] = None) -> None:
        super().__init__(label="agent-icons", bucket_name=bucket_name, s3_client=s3_client)

    def put(self, *, agent_id: str, content: bytes, ext: str, content_type: str) -> str:
        """Store normalized bytes and return the content-addressed key."""
        key = build_icon_key(agent_id, content_digest(content), ext)
        return self.put_object(key=key, content=content, content_type=content_type)


_store: Optional[AgentIconStore] = None


def get_icon_store() -> AgentIconStore:
    """Process-wide store, bound on first use (the env is set by the time routes run)."""
    global _store
    if _store is None:
        _store = AgentIconStore()
    return _store
