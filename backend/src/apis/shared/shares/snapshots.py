"""Read a conversation share's row and snapshot body.

The share row lives in the ``shared-conversations`` table (``share_id`` hash
key) and its body in the bucket of the same name, or inline on rows written
before the S3 offload. ``ShareService`` reads through :func:`load_snapshot_raw`
so app-api and the Runtime read a snapshot the same way.

**Names.** app-api has ``SHARED_CONVERSATIONS_TABLE_NAME`` and
``SHARED_CONVERSATIONS_BUCKET_NAME``. The Runtime has neither: its environment
is at 49 of AWS's 50 variables. Both resources are named
``{PROJECT_PREFIX}-shared-conversations`` by CDK (``getResourceName``), and the
Runtime has ``PROJECT_PREFIX``, so the names are derived from it when the
explicit variable is absent.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Optional

from .snapshot_store import ShareSnapshotStore, ShareSnapshotStoreError

logger = logging.getLogger(__name__)

_RESOURCE_SUFFIX = "shared-conversations"


class SnapshotUnreadableError(RuntimeError):
    """A share row whose body cannot be read (missing object, bad JSON, or no body at all)."""


def _derived_name(env_var: str) -> str:
    explicit = os.environ.get(env_var, "")
    if explicit:
        return explicit
    prefix = os.environ.get("PROJECT_PREFIX", "")
    return f"{prefix}-{_RESOURCE_SUFFIX}" if prefix else ""


def shared_conversations_table_name() -> str:
    """The shares table: ``SHARED_CONVERSATIONS_TABLE_NAME``, else derived from ``PROJECT_PREFIX``."""
    return _derived_name("SHARED_CONVERSATIONS_TABLE_NAME")


def shared_conversations_bucket_name() -> str:
    """The snapshot bucket: ``SHARED_CONVERSATIONS_BUCKET_NAME``, else derived from ``PROJECT_PREFIX``."""
    return _derived_name("SHARED_CONVERSATIONS_BUCKET_NAME")


def convert_decimals_to_float(obj: Any) -> Any:
    """Recursively convert DynamoDB ``Decimal`` values back to native types.

    Legacy inline shares were written with floats as ``Decimal``. Convert them
    back (``int`` when integral, else ``float``) so the legacy read path yields
    the same plain-JSON shape as the S3-backed path.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: convert_decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimals_to_float(v) for v in obj]
    return obj


def load_snapshot_raw(item: dict, store: ShareSnapshotStore) -> dict:
    """Return the whole snapshot body for a share row.

    Three row shapes, for backward compatibility:

      - **New** (``body_ref`` present): fetch the JSON body from S3.
      - **Legacy inline** (``messages`` present, no ``body_ref``): read the
        body straight off the DynamoDB item, exactly as before the S3
        offload. Existing shares predate the offload and stay readable
        with no migration.
      - **Malformed** (neither): ``SnapshotUnreadableError``.

    Callers must treat every key as optional. Bodies written before a key
    existed simply do not have it, and there is no migration.
    """
    body_ref = item.get("body_ref")
    if body_ref:
        key = body_ref.get("bucket_key")
        try:
            body = json.loads(store.get(key))
        except (ShareSnapshotStoreError, ValueError) as e:
            raise SnapshotUnreadableError(f"snapshot body unreadable at key={key}: {e}") from e
        return body if isinstance(body, dict) else {}

    if item.get("messages") is not None:
        # Legacy inline shares predate artifacts entirely, so there is no
        # `artifacts` key to recover here.
        return {
            "metadata": convert_decimals_to_float(item.get("metadata", {}) or {}),
            "messages": convert_decimals_to_float(item.get("messages", [])),
        }

    raise SnapshotUnreadableError("share has neither body_ref nor inline messages")


class ShareReader:
    """Read-only access to project shares, for the Runtime.

    One ``GetItem`` on the share row and one ``GetObject`` for its body. The
    Runtime role is granted exactly those two (CDK ``SharedConversationsRead``).
    """

    def __init__(self, table_name: Optional[str] = None, store: Optional[ShareSnapshotStore] = None):
        self.table_name = table_name if table_name is not None else shared_conversations_table_name()
        self._store = store
        self._table = None

    @property
    def enabled(self) -> bool:
        return bool(self.table_name)

    @property
    def store(self) -> ShareSnapshotStore:
        if self._store is None:
            self._store = ShareSnapshotStore(bucket_name=shared_conversations_bucket_name())
        return self._store

    def get_share(self, share_id: str) -> Optional[dict]:
        if not self.enabled or not share_id:
            return None
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb").Table(self.table_name)
        return self._table.get_item(Key={"share_id": share_id}).get("Item")

    def load_body(self, item: dict) -> dict:
        return load_snapshot_raw(item, self.store)
