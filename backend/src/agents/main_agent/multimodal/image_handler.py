"""
Image format detection and content block creation
"""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ImageHandler:
    """Handles image format detection and ContentBlock creation"""

    # Supported image formats by AWS Bedrock
    SUPPORTED_FORMATS = {"png", "jpeg", "jpg", "gif", "webp"}

    @staticmethod
    def get_image_format(content_type: str, filename: str) -> str:
        """
        Determine image format from content type or filename

        Args:
            content_type: MIME type (e.g., "image/png")
            filename: Filename with extension

        Returns:
            str: Image format (png, jpeg, gif, webp)
        """
        # The content type decides when it names a format: an image's stored
        # model copy can be a different format from its filename (a large PNG
        # sent as WebP), and the block must name the bytes, not the name.
        content_type_lower = (content_type or "").lower()
        for token, fmt in (("png", "png"), ("jpeg", "jpeg"), ("jpg", "jpeg"), ("gif", "gif"), ("webp", "webp")):
            if token in content_type_lower:
                return fmt
        filename_lower = (filename or "").lower()
        if filename_lower.endswith(".png"):
            return "png"
        if filename_lower.endswith((".jpg", ".jpeg")):
            return "jpeg"
        if filename_lower.endswith(".gif"):
            return "gif"
        if filename_lower.endswith(".webp"):
            return "webp"
        return "png"  # default

    @staticmethod
    def is_image(content_type: str, filename: str) -> bool:
        """
        Check if file is an image

        Args:
            content_type: MIME type
            filename: Filename with extension

        Returns:
            bool: True if file is an image
        """
        return (
            content_type.lower().startswith("image/") or
            filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        )

    @staticmethod
    def create_content_block(
        file_bytes: bytes,
        content_type: str,
        filename: str
    ) -> Dict[str, Any]:
        """
        Create image ContentBlock for Strands Agent

        Args:
            file_bytes: Raw file bytes
            content_type: MIME type
            filename: Original filename

        Returns:
            dict: ContentBlock with image data
        """
        image_format = ImageHandler.get_image_format(content_type, filename)

        content_block = {
            "image": {
                "format": image_format,
                "source": {
                    "bytes": file_bytes
                }
            }
        }

        logger.info(f"Created image content block: {filename} (format: {image_format})")
        return content_block
