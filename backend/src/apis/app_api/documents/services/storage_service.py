"""S3 storage service for document upload/download

Handles presigned URL generation for client-side uploads and downloads.
"""

import logging
import os
import re
from typing import Tuple

logger = logging.getLogger(__name__)


def _get_documents_bucket() -> str:
    """Get documents S3 bucket name from environment"""
    bucket = os.environ.get("ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise ValueError("ASSISTANTS_DOCUMENTS_BUCKET_NAME environment variable not set")
    return bucket


def _get_s3_key(assistant_id: str, document_id: str, filename: str) -> str:
    """
    Generate S3 key for document storage

    Pattern: assistants/{assistant_id}/documents/{document_id}/{filename}

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        filename: Original filename

    Returns:
        S3 object key
    """
    return f"assistants/{assistant_id}/documents/{document_id}/{filename}"


async def generate_upload_url(assistant_id: str, document_id: str, filename: str, content_type: str, expires_in: int = 3600) -> Tuple[str, str]:
    """
    Generate presigned S3 URL for client-side upload

    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        filename: Original filename
        content_type: MIME type
        expires_in: URL expiration in seconds (default: 1 hour)

    Returns:
        Tuple of (presigned_url, s3_key)
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        logger.error("boto3 is required for S3 operations")
        raise

    # Use region from AWS_REGION env var to ensure presigned URLs use regional endpoint
    # This is critical for CORS - global endpoint redirects break CORS preflight
    # Force SigV4 signing and regional endpoint to avoid CORS issues with global endpoint
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3_config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    s3_client = boto3.client("s3", region_name=region, config=s3_config, endpoint_url=f"https://s3.{region}.amazonaws.com")

    bucket = _get_documents_bucket()

    # Sanitize filename as file keys s3 doesnt allow certain characters, and we want to avoid path traversal attacks
    filename = filename.lower()
    filename = re.sub(r"[^a-zA-Z0-9_.\-\(\)]", "_", filename)

    s3_key = _get_s3_key(assistant_id, document_id, filename)

    presigned_url = s3_client.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": s3_key, "ContentType": content_type}, ExpiresIn=expires_in
    )

    logger.info(f"Generated presigned upload URL for {s3_key} (expires in {expires_in}s)")
    return presigned_url, s3_key


async def generate_download_url(s3_key: str, expires_in: int = 3600) -> str:
    """
    Generate presigned S3 URL for client-side download

    Args:
        s3_key: S3 object key (stored in document record)
        expires_in: URL expiration in seconds (default: 1 hour)

    Returns:
        Presigned URL for download
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        logger.error("boto3 is required for S3 operations")
        raise

    # Use region from AWS_REGION env var to ensure presigned URLs use regional endpoint
    # This is critical for CORS - global endpoint redirects break CORS preflight
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3_config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    s3_client = boto3.client("s3", region_name=region, config=s3_config, endpoint_url=f"https://s3.{region}.amazonaws.com")

    bucket = _get_documents_bucket()

    presigned_url = s3_client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": s3_key}, ExpiresIn=expires_in)

    logger.info(f"Generated presigned download URL for {s3_key} (expires in {expires_in}s)")
    return presigned_url
