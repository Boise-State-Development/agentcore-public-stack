"""S3-backed store for a skill's supporting reference files (PR-4).

A skill's reference files (read-only markdown/resources for deep
progressive disclosure) are too large to inline in the DynamoDB
``SkillDefinition`` row (400 KB item limit), so the bytes live in the
``skill-resources`` S3 bucket and the row carries only a lightweight
``SkillResourceRef`` manifest.

Skills v2 stores objects in the **standard agentskills.io bundle layout** so a
skill's S3 prefix is a valid bundle (attachable to the managed-Harness lane,
exportable as-is):

  - ``skills/{skill_id}/SKILL.md`` — write-through projection of the row.
  - ``skills/{skill_id}/{references|scripts|assets}/{filename}`` — supporting
    files, keyed by their bundle directory + filename (NOT content-addressed;
    dedupe is dropped in favor of the readable standard layout — bundles are
    small). Re-uploading the same filename overwrites its object.
  - The manifest on the catalog row references objects by key; the bytes never
    travel through DynamoDB. The manifest still records a ``content_hash`` per
    file for change detection/display.

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


# Resource ``kind`` → bundle subdirectory (agentskills.io standard).
KIND_DIRS: dict = {"reference": "references", "script": "scripts", "asset": "assets"}


def resource_key(skill_id: str, kind: str, filename: str) -> str:
    """Return the standard bundle object key for one of a skill's files.

    ``skills/{skill_id}/{references|scripts|assets}/{filename}``. An unknown
    ``kind`` falls back to ``references`` (defensive; callers pass a validated
    kind).
    """
    subdir = KIND_DIRS.get(kind, "references")
    return f"skills/{skill_id}/{subdir}/{filename}"


def skill_md_key(skill_id: str) -> str:
    """Return the object key for a skill's projected ``SKILL.md``."""
    return f"skills/{skill_id}/SKILL.md"


def compute_content_hash(content: bytes) -> str:
    """Return the sha256 hex digest recorded in the manifest for change detection."""
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
        self,
        *,
        skill_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        kind: str = "reference",
    ) -> str:
        """Persist a supporting file into the standard bundle layout; return the key.

        The key is ``skills/{skill_id}/{references|scripts|assets}/{filename}``
        (not content-addressed). Re-uploading the same ``(kind, filename)``
        overwrites the object in place — the manifest, keyed by filename, stays
        a single entry.
        """
        key = resource_key(skill_id, kind, filename)
        return self._put_bytes(
            skill_id=skill_id, key=key, content=content, content_type=content_type
        )

    def put_skill_md(self, *, skill_id: str, content: str) -> str:
        """Write the projected ``SKILL.md`` for a skill; return the key.

        Text is UTF-8 encoded. Overwrites in place (the row is the source of
        truth, so the projection is always fully rewritten).
        """
        key = skill_md_key(skill_id)
        return self._put_bytes(
            skill_id=skill_id,
            key=key,
            content=content.encode("utf-8"),
            content_type="text/markdown",
        )

    def _put_bytes(
        self, *, skill_id: str, key: str, content: bytes, content_type: str
    ) -> str:
        """Upload bytes to ``key`` (overwrite); return the key."""
        self._require_enabled()
        client = self._client()
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
                f"failed to store object for skill '{skill_id}'"
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


_store: Optional[SkillResourceStore] = None


def get_skill_resource_store() -> SkillResourceStore:
    """Get or create the process-global skill-resource store."""
    global _store
    if _store is None:
        _store = SkillResourceStore()
    return _store
