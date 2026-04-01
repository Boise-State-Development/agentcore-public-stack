"""
File Resolver Service

Resolves file upload IDs to FileContent objects with base64-encoded bytes.
Used by chat endpoints to fetch files from S3 before passing to agent.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .repository import get_file_upload_repository
from .models import FileStatus

logger = logging.getLogger(__name__)


@dataclass
class ResolvedFileContent:
    """
    Resolved file content with base64-encoded bytes.
    Compatible with FileContent from inference_api.chat.models.
    """
    filename: str
    content_type: str
    bytes: str  # base64-encoded


class FileResolverError(Exception):
    """Error resolving file content."""
    pass


class FileResolver:
    """
    Resolves file upload IDs to FileContent objects.

    Fetches file metadata from DynamoDB and content from S3,
    then encodes as base64 for the agent.
    """

    def __init__(self, s3_client=None):
        self._s3_client = s3_client or boto3.client("s3")
        self._file_repository = get_file_upload_repository()

    async def resolve_files(
        self,
        user_id: str,
        upload_ids: List[str],
        max_files: int = 5
    ) -> List[ResolvedFileContent]:
        """
        Resolve upload IDs to file content objects.

        Args:
            user_id: Owner user ID (for authorization)
            upload_ids: List of upload IDs to resolve
            max_files: Maximum files to process (Bedrock limit is 5)

        Returns:
            List of ResolvedFileContent objects with base64-encoded bytes

        Raises:
            FileResolverError: If file not found or access denied
        """
        resolved_files = []

        for upload_id in upload_ids[:max_files]:
            try:
                file_content = await self._resolve_single_file(user_id, upload_id)
                if file_content:
                    resolved_files.append(file_content)
            except Exception as e:
                logger.warning(f"Failed to resolve file {upload_id}: {e}")
                # Continue with other files rather than failing entirely
                continue

        return resolved_files

    async def _resolve_single_file(
        self,
        user_id: str,
        upload_id: str
    ) -> Optional[ResolvedFileContent]:
        """Resolve a single file upload ID."""

        # Get file metadata
        file_meta = await self._file_repository.get_file(user_id, upload_id)

        if not file_meta:
            logger.warning(f"File {upload_id} not found for user {user_id}")
            return None

        if file_meta.status != FileStatus.READY:
            logger.warning(f"File {upload_id} not ready: {file_meta.status}")
            return None

        # Fetch content from S3
        try:
            response = self._s3_client.get_object(
                Bucket=file_meta.s3_bucket,
                Key=file_meta.s3_key
            )
            file_bytes = response["Body"].read()
        except ClientError as e:
            logger.error(f"Failed to fetch file {upload_id} from S3: {e}")
            return None

        # Encode as base64
        base64_content = base64.b64encode(file_bytes).decode("utf-8")

        return ResolvedFileContent(
            filename=file_meta.filename,
            content_type=file_meta.mime_type,
            bytes=base64_content
        )


# Global instance
_resolver_instance: Optional[FileResolver] = None


def get_file_resolver() -> FileResolver:
    """Get or create global FileResolver instance."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = FileResolver()
    return _resolver_instance
