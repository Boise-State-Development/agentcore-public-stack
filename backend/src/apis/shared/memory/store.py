"""S3-backed store for a Memory Space's markdown bytes (PR-1).

A Memory Space is a per-owner (optionally shared) markdown "second brain":
an always-loaded index (``MEMORY.md``) plus a set of typed entry files. The
bytes are too large / too many to inline in DynamoDB (400 KB item limit), so
they live in the ``memory-spaces`` S3 bucket and the DynamoDB rows carry only
lightweight manifests (``MemoryEntryRef``) plus a pointer to the index object.

This store is a faithful sibling of ``apis/shared/skills/resource_store.py``
(the skills reference-file / progressive-disclosure mechanism the Memory
Spaces spec re-scopes from per-skill to per-space):

  - Objects are **content-addressed**: the key is
    ``spaces/{space_id}/{content_hash}`` where ``content_hash`` is the sha256
    hex of the bytes. Identical content within a space dedupes to one object,
    and every write produces an immutable object — a new edit is a new object
    plus a manifest-pointer swap, which keeps concurrency clean (PR-6) and
    makes the readable-path export (spec §9) a pure function of the manifest.
  - The manifest / index pointer references objects by key; the bytes never
    travel through DynamoDB.

Boundary: this module lives under ``apis/shared/memory/`` and is import-clean
(it never imports ``app_api``/``inference_api``). The user-facing CRUD path
(app-api, PR-5) and the runtime read/write path (inference-api, PR-2/PR-4)
both reach it through ``apis.shared``.

Configuration: the bucket name comes from ``S3_MEMORY_SPACES_BUCKET_NAME``
(set on both compute roles by the CDK ``MemorySpacesConstruct`` wiring). When
boto3 or the bucket name is absent (local dev without AWS), the store is
``enabled == False`` and every operation raises ``MemorySpaceStoreError`` so a
misconfigured write surfaces loudly rather than silently "succeeding" with no
bytes persisted.
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

# AWS-managed (SSE-S3 / AES256) encryption, matching the bucket default and
# the skills/artifacts/file-upload buckets.
_SSE_ALGORITHM = "AES256"


class MemorySpaceStoreError(RuntimeError):
    """Raised when the store is asked to do work it cannot complete.

    Covers both "storage not configured" (no bucket / no boto3) and an
    unexpected S3 failure, so callers have one error type to translate.
    """


def content_key(space_id: str, content_hash: str) -> str:
    """Return the content-addressed object key for a space's markdown file."""
    return f"spaces/{space_id}/{content_hash}"


def compute_content_hash(content: bytes) -> str:
    """Return the sha256 hex digest used as the content address."""
    return hashlib.sha256(content).hexdigest()


class MemorySpaceStore:
    """Put / get / delete a Memory Space's markdown bytes in S3."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        s3_client: Optional[object] = None,
    ) -> None:
        self.bucket_name = bucket_name or os.environ.get(
            "S3_MEMORY_SPACES_BUCKET_NAME"
        )
        # Allow an explicit client (tests inject a moto client); otherwise it
        # is created lazily on first use so importing the module never needs
        # AWS creds.
        self._s3 = s3_client

    @property
    def enabled(self) -> bool:
        """True when a bucket is configured and boto3 is importable."""
        return bool(self.bucket_name) and boto3 is not None

    def _client(self):
        if self._s3 is None:
            if boto3 is None:  # pragma: no cover - import-guarded above
                raise MemorySpaceStoreError(
                    "memory space storage unavailable: boto3 is not installed"
                )
            self._s3 = boto3.client("s3")
        return self._s3

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise MemorySpaceStoreError(
                "memory space storage is not configured "
                "(S3_MEMORY_SPACES_BUCKET_NAME is unset)"
            )

    def put(self, *, space_id: str, content: bytes, content_type: str) -> str:
        """Persist markdown bytes content-addressed; return the object key.

        Computes the sha256 of ``content``, derives the
        ``spaces/{space_id}/{content_hash}`` key, and uploads. If an object
        already exists at that key (same content), the upload is skipped
        (dedupe) — the key is returned either way.
        """
        self._require_enabled()
        digest = compute_content_hash(content)
        key = content_key(space_id, digest)
        client = self._client()

        if self._object_exists(key):
            logger.info(
                "memory-spaces: dedupe hit for space=%s key=%s (%d bytes)",
                space_id,
                key,
                len(content),
            )
            return key

        try:
            client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type or "text/markdown",
                ServerSideEncryption=_SSE_ALGORITHM,
            )
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.error(
                "memory-spaces: put failed for space=%s key=%s: %s",
                space_id,
                key,
                e,
            )
            raise MemorySpaceStoreError(
                f"failed to store memory bytes for space '{space_id}'"
            ) from e

        logger.info(
            "memory-spaces: stored space=%s key=%s (%d bytes)",
            space_id,
            key,
            len(content),
        )
        return key

    def get(self, s3_key: str) -> bytes:
        """Return the bytes for an object key. Raises if missing/unavailable."""
        self._require_enabled()
        client = self._client()
        try:
            response = client.get_object(Bucket=self.bucket_name, Key=s3_key)
            return response["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise MemorySpaceStoreError(
                    f"memory file not found at key '{s3_key}'"
                ) from e
            logger.error("memory-spaces: get failed for key=%s: %s", s3_key, e)
            raise MemorySpaceStoreError(
                f"failed to read memory file at key '{s3_key}'"
            ) from e

    def delete(self, s3_key: str) -> None:
        """Delete an object key. Best-effort — never raises on the storage
        miss path (deleting an already-absent object is a no-op in S3)."""
        if not self.enabled:
            return
        client = self._client()
        try:
            client.delete_object(Bucket=self.bucket_name, Key=s3_key)
        except ClientError:  # pragma: no cover - best-effort cleanup
            logger.warning(
                "memory-spaces: delete failed for key=%s", s3_key, exc_info=True
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


_store: Optional[MemorySpaceStore] = None


def get_memory_space_store() -> MemorySpaceStore:
    """Get or create the process-global memory-space store."""
    global _store
    if _store is None:
        _store = MemorySpaceStore()
    return _store
