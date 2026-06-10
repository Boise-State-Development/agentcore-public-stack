"""S3-backed store for a skill's supporting reference files (PR-4).

A skill's reference files (read-only markdown/resources for deep
progressive disclosure) are too large to inline in the DynamoDB
``SkillDefinition`` row (400 KB item limit), so the bytes live in the
``skill-resources`` S3 bucket and the row carries only a lightweight
``SkillResourceRef`` manifest.

This store mirrors the content-hash + dedupe shape used by the MCP-Apps
UI-resource persistence and the artifacts bucket:

  - Objects are **content-addressed**: the key is
    ``skills/{skill_id}/{content_hash}`` where ``content_hash`` is the
    sha256 hex of the bytes. Two reference files with identical content
    within a skill therefore resolve to the same object (dedupe) — a
    re-upload of unchanged bytes is a no-op ``head_object`` instead of a
    re-``put``.
  - The manifest on the catalog row references objects by key; the bytes
    never travel through DynamoDB.

Boundary: this module lives under ``apis/shared/skills/`` and is import-
clean (it never imports ``app_api``/``inference_api``). The admin write
path (app-api) and the future runtime read path (inference-api, PR-6) both
reach it through ``apis.shared``.

Configuration: the bucket name comes from ``S3_SKILL_RESOURCES_BUCKET_NAME``
(set on both compute roles by the CDK ``SkillResourcesConstruct`` wiring).
When boto3 or the bucket name is absent (local dev without AWS), the store
is ``enabled == False`` and every operation raises ``SkillResourceStoreError``
so a misconfigured admin write surfaces loudly rather than silently
"succeeding" with no bytes persisted.
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
# the artifacts/file-upload buckets.
_SSE_ALGORITHM = "AES256"


class SkillResourceStoreError(RuntimeError):
    """Raised when the store is asked to do work it cannot complete.

    Covers both "storage not configured" (no bucket / no boto3) and an
    unexpected S3 failure, so callers (the admin service) have one error
    type to translate.
    """


def content_key(skill_id: str, content_hash: str) -> str:
    """Return the content-addressed object key for a skill's file."""
    return f"skills/{skill_id}/{content_hash}"


def compute_content_hash(content: bytes) -> str:
    """Return the sha256 hex digest used as the content address."""
    return hashlib.sha256(content).hexdigest()


class SkillResourceStore:
    """Put / get / delete a skill's reference-file bytes in S3."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        s3_client: Optional[object] = None,
    ) -> None:
        self.bucket_name = bucket_name or os.environ.get(
            "S3_SKILL_RESOURCES_BUCKET_NAME"
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
                raise SkillResourceStoreError(
                    "skill resource storage unavailable: boto3 is not installed"
                )
            self._s3 = boto3.client("s3")
        return self._s3

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SkillResourceStoreError(
                "skill resource storage is not configured "
                "(S3_SKILL_RESOURCES_BUCKET_NAME is unset)"
            )

    def put(
        self, *, skill_id: str, content: bytes, content_type: str
    ) -> str:
        """Persist file bytes content-addressed; return the object key.

        Computes the sha256 of ``content``, derives the
        ``skills/{skill_id}/{content_hash}`` key, and uploads. If an object
        already exists at that key (same content), the upload is skipped
        (dedupe) — the key is returned either way.
        """
        self._require_enabled()
        digest = compute_content_hash(content)
        key = content_key(skill_id, digest)
        client = self._client()

        if self._object_exists(key):
            logger.info(
                "skill-resources: dedupe hit for skill=%s key=%s (%d bytes)",
                skill_id,
                key,
                len(content),
            )
            return key

        try:
            client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type or "application/octet-stream",
                ServerSideEncryption=_SSE_ALGORITHM,
            )
        except ClientError as e:  # pragma: no cover - network/permission path
            logger.error(
                "skill-resources: put failed for skill=%s key=%s: %s",
                skill_id,
                key,
                e,
            )
            raise SkillResourceStoreError(
                f"failed to store reference file for skill '{skill_id}'"
            ) from e

        logger.info(
            "skill-resources: stored skill=%s key=%s (%d bytes)",
            skill_id,
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
                raise SkillResourceStoreError(
                    f"reference file not found at key '{s3_key}'"
                ) from e
            logger.error("skill-resources: get failed for key=%s: %s", s3_key, e)
            raise SkillResourceStoreError(
                f"failed to read reference file at key '{s3_key}'"
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
                "skill-resources: delete failed for key=%s", s3_key, exc_info=True
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


_store: Optional[SkillResourceStore] = None


def get_skill_resource_store() -> SkillResourceStore:
    """Get or create the process-global skill-resource store."""
    global _store
    if _store is None:
        _store = SkillResourceStore()
    return _store
