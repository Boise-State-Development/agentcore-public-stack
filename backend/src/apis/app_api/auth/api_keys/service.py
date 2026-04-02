"""API Key service — business logic between routes and repository."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import ApiKeyInfo, CreateApiKeyRequest, CreateApiKeyResponse, ValidatedApiKey
from .repository import ApiKeyRepository, get_api_key_repository

logger = logging.getLogger(__name__)

EXPIRATION_DAYS = 90


class ApiKeyService:
    """Orchestrates API key lifecycle operations."""

    def __init__(self, repo: Optional[ApiKeyRepository] = None):
        self.repo = repo or get_api_key_repository()

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_key(
        self, user_id: str, request: CreateApiKeyRequest
    ) -> CreateApiKeyResponse:
        """Generate a new API key for the user.

        If the user already has a key, it is deleted first — only one key
        per user is allowed.  The raw key is a UUID4 string; we store its
        SHA-256 hash.  Expiration is always 90 days.
        """
        # Delete any existing key for this user
        existing = await self.repo.get_key_for_user(user_id)
        if existing:
            await self.repo.delete_key(user_id, existing["keyId"])

        key_id = str(uuid.uuid4())
        raw_key = str(uuid.uuid4())
        key_hash = self.repo.hash_key(raw_key)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=EXPIRATION_DAYS)).isoformat()

        item = {
            "PK": f"USER#{user_id}",
            "SK": f"KEY#{key_id}",
            "keyId": key_id,
            "userId": user_id,
            "name": request.name,
            "keyHash": key_hash,
            "createdAt": now.isoformat(),
            "expiresAt": expires_at,
            "lastUsedAt": None,
        }

        await self.repo.create_key(item)

        return CreateApiKeyResponse(
            key_id=key_id,
            name=request.name,
            key=raw_key,
            created_at=now.isoformat(),
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_key(self, user_id: str, key_id: str) -> bool:
        """Delete a key belonging to the requesting user."""
        deleted = await self.repo.delete_key(user_id, key_id)
        if deleted:
            logger.info("API key deleted")
        else:
            logger.info("API key not found (no-op)")
        return deleted

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get_key(self, user_id: str) -> Optional[ApiKeyInfo]:
        """Return metadata for the user's API key, or None if they don't have one."""
        item = await self.repo.get_key_for_user(user_id)
        if not item:
            return None
        return ApiKeyInfo(
            key_id=item["keyId"],
            name=item["name"],
            created_at=item["createdAt"],
            expires_at=item.get("expiresAt", ""),
            last_used_at=item.get("lastUsedAt"),
        )

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    async def validate_key(self, raw_key: str) -> Optional[ValidatedApiKey]:
        """Validate a raw API key and return the associated user info.

        Returns None if the key is invalid, not found, or expired.
        On success, updates lastUsedAt in the background.
        """
        key_hash = self.repo.hash_key(raw_key)
        item = await self.repo.get_key_by_hash(key_hash)

        if not item:
            return None

        # Check expiration
        expires_at = item.get("expiresAt")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if datetime.now(timezone.utc) > exp:
                    logger.info(f"API key {item['keyId']} is expired")
                    return None
            except (ValueError, TypeError):
                logger.warning(f"Invalid expiresAt format for key {item['keyId']}")

        # Fire-and-forget last-used update
        try:
            await self.repo.update_last_used(item["userId"], item["keyId"])
        except Exception:
            pass  # non-critical

        return ValidatedApiKey(
            key_id=item["keyId"],
            user_id=item["userId"],
            name=item["name"],
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional[ApiKeyService] = None


def get_api_key_service() -> ApiKeyService:
    global _service
    if _service is None:
        _service = ApiKeyService()
    return _service
