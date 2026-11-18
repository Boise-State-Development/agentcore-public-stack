"""S3 storage service for document upload/download

Handles presigned URL generation for client-side uploads and downloads.
"""

import codecs
import functools
import logging
import os
import re
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)


def _get_documents_bucket() -> str:
    """Get documents S3 bucket name from environment"""
    bucket = os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise ValueError("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME environment variable not set")
    return bucket


def _sanitize_filename(filename: str) -> str:
    """Sanitize filename for S3 key usage.
    
    Lowercases and replaces disallowed characters with underscores.
    Must be used consistently wherever s3_key is generated.
    """
    filename = filename.lower()
    filename = re.sub(r"[^a-zA-Z0-9_.\-\(\)]", "_", filename)
    return filename


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

    # Sanitize filename using shared helper
    filename = _sanitize_filename(filename)

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


#: Largest slice of a document's staged text the content view returns. A crawled
#: page is a few to a few hundred KB; past this the reader gets the head and a
#: ``truncated`` flag rather than an unbounded response.
DOCUMENT_CONTENT_MAX_BYTES = 1024 * 1024


def read_text_object(get_object: Callable[..., Dict[str, Any]], bucket: str, key: str, max_bytes: int) -> Tuple[str, bool]:
    """Read at most ``max_bytes`` of a UTF-8 object. Returns ``(text, truncated)``.

    One ranged GET asks for one byte past the cap, so a larger object is never
    read whole and ``truncated`` needs no separate HEAD. A multi-byte character
    the cap splits is dropped rather than shown as a replacement character.
    ``get_object`` is the S3 client's bound method, so this runs in a worker
    thread with only the network call inside it.
    """
    response = get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes}")
    body = response["Body"]
    try:
        data = body.read(max_bytes + 1)
    finally:
        body.close()
    truncated = len(data) > max_bytes
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data[:max_bytes], final=not truncated)
    return text, truncated


@functools.lru_cache(maxsize=1)
def content_s3_client() -> Any:
    """One S3 client per process for the content view's reads (built once, reused)."""
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
