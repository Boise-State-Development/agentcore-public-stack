"""Service for packaging and uploading SageMaker training/inference scripts to S3."""

import os
import io
import tarfile
import hashlib
import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from . import task_types

logger = logging.getLogger(__name__)

# Directory containing the SageMaker scripts (relative to this module)
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "sagemaker_scripts")

# Directory containing modules shared between the app-api and the training
# container.  task_types.py is the task registry, and both sides have to agree
# on it, so it is packaged rather than duplicated.
PACKAGE_DIR = os.path.dirname(__file__)

# Files to include in the source directory tar.gz, flattened to the archive
# root.  The task modules travel with the dispatcher because train.py resolves
# the task type at runtime and imports whichever module implements it.
SCRIPT_FILES = [
    "train.py",
    "inference.py",
    "task_common.py",
    "task_text_classification.py",
    "task_image_classification.py",
    "task_image_text_classification.py",
    "task_image_text_to_text.py",
]

# Files pulled from the package directory rather than sagemaker_scripts/.
SHARED_FILES = ["task_types.py"]

# The dependency file each DLC family gets, packaged into the archive AS
# ``requirements.txt`` — which is the only name the DLC installs from.
#
# One shared file cannot serve all three.  The VLM family needs peft and
# bitsandbytes, and bitsandbytes requires torch>=2.4 while the text container
# is torch 2.1: adding them to the shared file would break dependency
# installation for every existing text job.  Selecting the file per family is
# the same reasoning that already keys the DLC image by family.
REQUIREMENTS_BY_FAMILY = {
    task_types.DLC_FAMILY_TEXT: "requirements.txt",
    task_types.DLC_FAMILY_VISION: "requirements.txt",
    task_types.DLC_FAMILY_VLM: "requirements-vlm.txt",
}

#: Name the requirements file must have inside the archive.
PACKAGED_REQUIREMENTS_NAME = "requirements.txt"


def scripts_s3_key(dlc_family: str) -> str:
    """S3 key for one family's source directory.

    Keyed by family because the archives differ only in their requirements
    file; a single key would make the last job to run overwrite the
    dependency set of the other families.
    """
    return f"scripts/sourcedir-{dlc_family}.tar.gz"


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
        self._cached_s3_uris: dict = {}

    @staticmethod
    def _source_paths(dlc_family: str) -> list:
        """Return (archive name, source path) for every packaged file.

        Ordered deterministically so the content hash is stable across calls —
        an unstable hash would re-upload the source dir on every job.
        """
        paths = [(name, os.path.join(SCRIPTS_DIR, name)) for name in SCRIPT_FILES]
        paths += [(name, os.path.join(PACKAGE_DIR, name)) for name in SHARED_FILES]

        requirements = REQUIREMENTS_BY_FAMILY.get(
            dlc_family, REQUIREMENTS_BY_FAMILY[task_types.DLC_FAMILY_TEXT]
        )
        paths.append(
            (PACKAGED_REQUIREMENTS_NAME, os.path.join(SCRIPTS_DIR, requirements))
        )
        return sorted(paths, key=lambda pair: pair[0])

    def _compute_content_hash(self, dlc_family: str) -> str:
        """Compute SHA256 hash of all script file contents."""
        hasher = hashlib.sha256()
        for name, filepath in self._source_paths(dlc_family):
            if os.path.exists(filepath):
                hasher.update(name.encode("utf-8"))
                with open(filepath, "rb") as f:
                    hasher.update(f.read())
        return hasher.hexdigest()

    def _create_tar_gz(self, dlc_family: str) -> bytes:
        """Create an in-memory tar.gz archive of the scripts.

        Files are added at the root level of the archive (no subdirectory),
        which is what the HuggingFace DLC expects.
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, filepath in self._source_paths(dlc_family):
                if os.path.exists(filepath):
                    tar.add(filepath, arcname=name)
                else:
                    logger.warning(f"Script file not found: {filepath}")
        buf.seek(0)
        return buf.read()

    def _check_s3_hash(self, content_hash: str, key: str) -> bool:
        """Check if the S3 object exists and has a matching content hash."""
        try:
            response = self._s3.head_object(
                Bucket=self._bucket,
                Key=key,
            )
            s3_hash = response.get("Metadata", {}).get("content-hash", "")
            return s3_hash == content_hash
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def ensure_scripts_uploaded(self, task_type: Optional[str] = None) -> str:
        """Ensure the source dir for ``task_type``'s family is in S3.

        Uses content-hash caching to skip re-upload if scripts haven't changed.
        Caches the URI per family in memory after the first successful call.
        """
        dlc_family = task_types.get_task_spec(task_type).dlc_family

        cached = self._cached_s3_uris.get(dlc_family)
        if cached:
            return cached

        key = scripts_s3_key(dlc_family)
        content_hash = self._compute_content_hash(dlc_family)

        if not self._check_s3_hash(content_hash, key):
            tar_bytes = self._create_tar_gz(dlc_family)
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=tar_bytes,
                Metadata={"content-hash": content_hash},
            )
            logger.info(f"Uploaded scripts tar.gz to s3://{self._bucket}/{key}")
        else:
            logger.debug(f"Scripts tar.gz already up-to-date in S3 for {dlc_family}")

        uri = f"s3://{self._bucket}/{key}"
        self._cached_s3_uris[dlc_family] = uri
        return uri


# Singleton access
_packaging_service_instance: Optional[ScriptPackagingService] = None


def get_script_packaging_service() -> ScriptPackagingService:
    """Get or create the global ScriptPackagingService instance."""
    global _packaging_service_instance
    if _packaging_service_instance is None:
        _packaging_service_instance = ScriptPackagingService()
    return _packaging_service_instance
