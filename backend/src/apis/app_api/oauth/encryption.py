"""KMS encryption service for OAuth tokens."""

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class TokenEncryptionService:
    """
    Service for encrypting/decrypting OAuth tokens using AWS KMS.

    Tokens are encrypted before storage in DynamoDB and decrypted on retrieval.
    Uses AWS KMS with a customer-managed key for secure key management.
    """

    def __init__(
        self,
        key_arn: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize the encryption service.

        Args:
            key_arn: KMS key ARN for encryption (defaults to env var)
            region: AWS region (defaults to env var)
        """
        self._key_arn = key_arn or os.getenv("OAUTH_TOKEN_ENCRYPTION_KEY_ARN")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._client = None
        self._enabled = bool(self._key_arn)

        if not self._enabled:
            logger.warning(
                "OAUTH_TOKEN_ENCRYPTION_KEY_ARN not set. "
                "Token encryption is disabled (development mode only)."
            )

    @property
    def enabled(self) -> bool:
        """Check if encryption is enabled."""
        return self._enabled

    def _get_client(self):
        """Lazy initialization of KMS client."""
        if self._client is None:
            import boto3

            profile = os.getenv("AWS_PROFILE")
            if profile:
                session = boto3.Session(profile_name=profile)
                self._client = session.client("kms", region_name=self._region)
            else:
                self._client = boto3.client("kms", region_name=self._region)
        return self._client

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a plaintext string using KMS.

        Args:
            plaintext: The string to encrypt

        Returns:
            Base64-encoded ciphertext

        Raises:
            RuntimeError: If encryption fails
        """
        if not self._enabled:
            # Development mode: return base64-encoded plaintext (NOT secure!)
            logger.warning("Using development mode encryption (NOT SECURE)")
            return f"DEV:{base64.b64encode(plaintext.encode()).decode()}"

        try:
            client = self._get_client()
            response = client.encrypt(
                KeyId=self._key_arn,
                Plaintext=plaintext.encode("utf-8"),
            )
            ciphertext = base64.b64encode(response["CiphertextBlob"]).decode("utf-8")
            logger.debug(f"Encrypted token (length={len(plaintext)} -> {len(ciphertext)})")
            return ciphertext

        except Exception as e:
            logger.error(f"Failed to encrypt token: {e}")
            raise RuntimeError(f"Token encryption failed: {e}") from e

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a ciphertext string using KMS.

        Args:
            ciphertext: Base64-encoded ciphertext

        Returns:
            Decrypted plaintext string

        Raises:
            RuntimeError: If decryption fails
        """
        if not self._enabled:
            # Development mode: decode base64 plaintext
            if ciphertext.startswith("DEV:"):
                logger.warning("Using development mode decryption (NOT SECURE)")
                return base64.b64decode(ciphertext[4:]).decode()
            else:
                raise RuntimeError("Cannot decrypt production ciphertext without KMS key")

        try:
            client = self._get_client()
            ciphertext_blob = base64.b64decode(ciphertext)
            response = client.decrypt(
                CiphertextBlob=ciphertext_blob,
                KeyId=self._key_arn,
            )
            plaintext = response["Plaintext"].decode("utf-8")
            logger.debug(f"Decrypted token (length={len(ciphertext)} -> {len(plaintext)})")
            return plaintext

        except Exception as e:
            logger.error(f"Failed to decrypt token: {e}")
            raise RuntimeError(f"Token decryption failed: {e}") from e


# Singleton instance
_encryption_service: Optional[TokenEncryptionService] = None


def get_token_encryption_service() -> TokenEncryptionService:
    """Get the token encryption service singleton."""
    global _encryption_service
    if _encryption_service is None:
        _encryption_service = TokenEncryptionService()
    return _encryption_service
