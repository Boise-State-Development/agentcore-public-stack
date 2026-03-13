"""Service for packaging and uploading SageMaker training/inference scripts to S3."""

import os
import io
import tarfile
import hashlib
import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Directory containing the SageMaker scripts (relative to this module)
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "sagemaker_scripts")

# Files to include in the source directory tar.gz
SCRIPT_FILES = ["train.py", "inference.py", "requirements.txt"]

# S3 key for the packaged scripts
SCRIPTS_S3_KEY = "scripts/sourcedir.tar.gz"


class ScriptPackagingService:
    """Packages SageMaker training/inference scripts as tar.gz and uploads to S3.

    Uses content-hash-based caching: computes SHA256 of all script files and
    only re-uploads when the hash changes. The hash is stored as S3 object
    metadata for comparison.
    """

    def __init__(self, s3_client=None, bucket_name: Optional[str] = None):
        region = os.environ.get("AWS_REGION", "us-west-2")
        self._s3 = s3_client or boto3.client("s3", region_name=region)
        self._bucket = bucket_name or os.environ.get(
            "S3_FINE_TUNING_BUCKET_NAME", "fine-tuning-data"
        )
        self._cached_s3_uri: Optional[str] = None

    def _compute_content_hash(self) -> str:
        """Compute SHA256 hash of all script file contents."""
        hasher = hashlib.sha256()
        for filename in sorted(SCRIPT_FILES):
            filepath = os.path.join(SCRIPTS_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, "rb") as f:
                    hasher.update(f.read())
        return hasher.hexdigest()

    def _create_tar_gz(self) -> bytes:
        """Create an in-memory tar.gz archive of the scripts.

        Files are added at the root level of the archive (no subdirectory),
        which is what the HuggingFace DLC expects.
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for filename in SCRIPT_FILES:
                filepath = os.path.join(SCRIPTS_DIR, filename)
                if os.path.exists(filepath):
                    tar.add(filepath, arcname=filename)
                else:
                    logger.warning(f"Script file not found: {filepath}")
        buf.seek(0)
        return buf.read()

    def _check_s3_hash(self, content_hash: str) -> bool:
        """Check if the S3 object exists and has a matching content hash."""
        try:
            response = self._s3.head_object(
                Bucket=self._bucket,
                Key=SCRIPTS_S3_KEY,
            )
            s3_hash = response.get("Metadata", {}).get("content-hash", "")
            return s3_hash == content_hash
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def ensure_scripts_uploaded(self) -> str:
        """Ensure scripts tar.gz is uploaded to S3. Returns the S3 URI.

        Uses content-hash caching to skip re-upload if scripts haven't changed.
        Caches the URI in memory after the first successful call.
        """
        if self._cached_s3_uri:
            return self._cached_s3_uri

        content_hash = self._compute_content_hash()

        if not self._check_s3_hash(content_hash):
            tar_bytes = self._create_tar_gz()
            self._s3.put_object(
                Bucket=self._bucket,
                Key=SCRIPTS_S3_KEY,
                Body=tar_bytes,
                Metadata={"content-hash": content_hash},
            )
            logger.info(
                f"Uploaded scripts tar.gz to s3://{self._bucket}/{SCRIPTS_S3_KEY}"
            )
        else:
            logger.debug("Scripts tar.gz already up-to-date in S3")

        self._cached_s3_uri = f"s3://{self._bucket}/{SCRIPTS_S3_KEY}"
        return self._cached_s3_uri


# Singleton access
_packaging_service_instance: Optional[ScriptPackagingService] = None


def get_script_packaging_service() -> ScriptPackagingService:
    """Get or create the global ScriptPackagingService instance."""
    global _packaging_service_instance
    if _packaging_service_instance is None:
        _packaging_service_instance = ScriptPackagingService()
    return _packaging_service_instance
