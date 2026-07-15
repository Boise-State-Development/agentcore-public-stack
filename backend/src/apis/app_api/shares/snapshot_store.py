"""S3-backed store for a conversation share's snapshot body.

A share is a point-in-time snapshot of a conversation. The body — the full
message list plus the session-metadata snapshot — is too large to inline in a
single DynamoDB item (400 KB limit; a long conversation with tool results,
images, or documents blows past it and PutItem fails with a
``ValidationException``). So the body lives in the ``shared-conversations`` S3
bucket and the DynamoDB row carries only the small control fields plus a
pointer (``body_ref``) to the object.

This store is a faithful sibling of ``apis/shared/memory/store.py`` (the Memory
Spaces S3 offload) and ``apis/shared/skills/resource_store.py``:

  - Objects are **content-addressed**: the key is
    ``shares/{share_id}/{content_hash}`` where ``content_hash`` is the sha256
    hex of the bytes. A share is immutable once created (``update_share`` only
    touches access-control fields, never the body), so there is exactly one
    object per share; content-addressing makes a retried ``create_share``
    idempotent.
  - The DynamoDB row references the object by key; the bytes never travel
    through DynamoDB.

It lives under ``app_api/shares/`` rather than ``apis/shared/`` because app-api
is the only consumer (create writes; view/export read; revoke deletes). If a
second consumer ever appears it moves to ``apis.shared`` per the import
boundary rule.

Configuration: the bucket name comes from ``SHARED_CONVERSATIONS_BUCKET_NAME``
(set on the app-api role by the CDK ``SharedConversationsConstruct`` wiring).
When boto3 or the bucket name is absent (local dev without AWS), the store is
``enabled == False`` and every write raises ``ShareSnapshotStoreError`` so a
misconfigured deploy surfaces loudly rather than silently reintroducing the
400 KB inline cliff.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

try:  # boto3 is absent in some local-dev setups
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    ClientError = Exception  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# AWS-managed (SSE-S3 / AES256) encryption, matching the bucket default and the
# memory-spaces / skills / artifacts / file-upload buckets.
_SSE_ALGORITHM = "AES256"


class ShareSnapshotStoreError(RuntimeError):
    """Raised when the store cannot complete a requested operation.

    Covers both "storage not configured" (no bucket / no boto3) and an
    unexpected S3 failure, so callers have one error type to translate.
    """


def content_key(share_id: str, content_hash: str) -> str:
    """Return the content-addressed object key for a share's snapshot body."""
    return f"shares/{share_id}/{content_hash}"


def compute_content_hash(content: bytes) -> str:
    """Return the sha256 hex digest used as the content address."""
    return hashlib.sha256(content).hexdigest()


class ShareSnapshotStore:
    """Put / get / delete a share's snapshot-body bytes in S3."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        s3_client: Optional[object] = None,
    ) -> None:
        self.bucket_name = bucket_name or os.environ.get(
            "SHARED_CONVERSATIONS_BUCKET_NAME"
        )
        # Allow an explicit client (tests inject a moto client); otherwise it is
        # created lazily on first use so importing the module never needs creds.
        self._s3 = s3_client

    @property
    def enabled(self) -> bool:
        """True when a bucket is configured and boto3 is importable."""
        return bool(self.bucket_name) and boto3 is not None

    def _client(self):
        if self._s3 is None:
            if boto3 is None:  # pragma: no cover - import-guarded above
                raise ShareSnapshotStoreError(
                    "share snapshot storage unavailable: boto3 is not installed"
                )
            self._s3 = boto3.client("s3")
        return self._s3

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ShareSnapshotStoreError(
                "share snapshot storage is not configured "
                "(SHARED_CONVERSATIONS_BUCKET_NAME is unset)"
            )

    def put(self, *, share_id: str, body: bytes) -> str:
        """Persist snapshot-body bytes content-addressed; return the object key.

        Computes the sha256 of ``body``, derives the
        ``shares/{share_id}/{content_hash}`` key, and uploads. If an object
        already exists at that key (same content — a retried create), the
        upload is skipped (dedupe); the key is returned either way.
        """
        self._require_enabled()
        digest = compute_content_hash(body)
        key = content_key(share_id, digest)
        client = self._client()

        if self._object_exists(key):
            logger.info(
                "shared-conversations: dedupe hit for share=%s key=%s (%d bytes)",
                share_id,
                key,
                len(body),
            )
            return key

        try:
            client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption=_SSE_ALGORITHM,
            )
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.error(
                "shared-conversations: put failed for share=%s key=%s: %s",
                share_id,
                key,
                e,
            )
            raise ShareSnapshotStoreError(
                f"failed to store snapshot body for share '{share_id}'"
            ) from e

        logger.info(
            "shared-conversations: stored share=%s key=%s (%d bytes)",
            share_id,
            key,
            len(body),
        )
        return key

    def get(self, bucket_key: str) -> bytes:
        """Return the bytes for an object key. Raises if missing/unavailable."""
        self._require_enabled()
        client = self._client()
        try:
            response = client.get_object(Bucket=self.bucket_name, Key=bucket_key)
            return response["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise ShareSnapshotStoreError(
                    f"snapshot body not found at key '{bucket_key}'"
                ) from e
            logger.error(
                "shared-conversations: get failed for key=%s: %s", bucket_key, e
            )
            raise ShareSnapshotStoreError(
                f"failed to read snapshot body at key '{bucket_key}'"
            ) from e

    def delete(self, bucket_key: str) -> None:
        """Delete an object key. Best-effort — never raises on the storage
        miss path (deleting an already-absent object is a no-op in S3)."""
        if not self.enabled:
            return
        client = self._client()
        try:
            client.delete_object(Bucket=self.bucket_name, Key=bucket_key)
        except ClientError:  # pragma: no cover - best-effort cleanup
            logger.warning(
                "shared-conversations: delete failed for key=%s",
                bucket_key,
                exc_info=True,
            )

    def _object_exists(self, key: str) -> bool:
        client = self._client()
        try:
            client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            # Any other error (permissions, throttling) is real — surface it
            # rather than masquerading as "absent" and double-uploading.
            raise


_store: Optional[ShareSnapshotStore] = None


def get_share_snapshot_store() -> ShareSnapshotStore:
    """Get or create the process-global share snapshot store."""
    global _store
    if _store is None:
        _store = ShareSnapshotStore()
    return _store
