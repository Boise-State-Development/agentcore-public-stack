"""DynamoDB repository for admin-managed system prompts."""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import SystemPrompt, SystemPromptCreate, SystemPromptUpdate

logger = logging.getLogger(__name__)


class SystemPromptsRepository:
    """CRUD for system prompts in DynamoDB.

    PK: ``PROMPT#<uuid>``, SK: ``METADATA``.
    Full Scan is used for listing — the catalog is expected to be small
    (tens of items at most), so Scan is appropriate and avoids a GSI.
    """

    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        self._table_name = table_name or os.getenv("DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME not set. "
                "System prompts repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized system prompts repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def list_prompts(self, enabled_only: bool = False) -> List[SystemPrompt]:
        """Return all prompts, optionally filtered to enabled ones only."""
        if not self._enabled:
            return []

        try:
            response = self._table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "METADATA"},
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    FilterExpression="SK = :sk",
                    ExpressionAttributeValues={":sk": "METADATA"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
        except ClientError:
            logger.error("Error listing system prompts", exc_info=True)
            raise

        prompts = [SystemPrompt.from_dynamo_item(item) for item in items]
        if enabled_only:
            prompts = [p for p in prompts if p.status == "enabled"]
        prompts.sort(key=lambda p: p.name.lower())
        return prompts

    async def get_prompt(self, prompt_id: str) -> Optional[SystemPrompt]:
        """Return a single prompt by ID, or None if not found."""
        if not self._enabled:
            return None
        try:
            response = self._table.get_item(
                Key={"PK": f"PROMPT#{prompt_id}", "SK": "METADATA"}
            )
            item = response.get("Item")
            if not item:
                return None
            return SystemPrompt.from_dynamo_item(item)
        except ClientError:
            logger.error("Error getting system prompt", exc_info=True)
            raise

    async def create_prompt(
        self, data: SystemPromptCreate, created_by: Optional[str] = None
    ) -> SystemPrompt:
        """Create a new prompt and return it."""
        if not self._enabled:
            raise RuntimeError("System prompts repository is not enabled")

        now = datetime.now(timezone.utc).isoformat() + "Z"
        prompt = SystemPrompt(
            prompt_id=str(uuid.uuid4()),
            name=data.name,
            description=data.description,
            prompt_text=data.prompt_text,
            status=data.status,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        try:
            self._table.put_item(
                Item=prompt.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError:
            logger.error("Error creating system prompt", exc_info=True)
            raise

        logger.info(f"Created system prompt: {prompt.prompt_id} name={prompt.name!r}")
        return prompt

    async def update_prompt(
        self, prompt_id: str, updates: SystemPromptUpdate
    ) -> Optional[SystemPrompt]:
        """Apply a partial update to an existing prompt. Returns None if not found.

        Uses a conditional put to guard against TOCTOU resurrection — if another
        admin deletes the row between our read and write, the put fails rather
        than recreating the deleted row.
        """
        if not self._enabled:
            return None

        existing = await self.get_prompt(prompt_id)
        if not existing:
            return None

        update_fields = updates.model_dump(exclude_none=True)
        for field_name, value in update_fields.items():
            setattr(existing, field_name, value)
        existing.updated_at = datetime.now(timezone.utc).isoformat() + "Z"

        try:
            self._table.put_item(
                Item=existing.to_dynamo_item(),
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Row was deleted between our read and write. Treat as not-found.
                logger.warning(f"System prompt {prompt_id} disappeared during update")
                return None
            logger.error("Error updating system prompt", exc_info=True)
            raise

        logger.info(f"Updated system prompt: {prompt_id}")
        return existing

    async def delete_prompt(self, prompt_id: str) -> bool:
        """Delete a prompt. Returns True if deleted, False if not found."""
        if not self._enabled:
            return False
        existing = await self.get_prompt(prompt_id)
        if not existing:
            return False
        try:
            self._table.delete_item(
                Key={"PK": f"PROMPT#{prompt_id}", "SK": "METADATA"}
            )
        except ClientError:
            logger.error("Error deleting system prompt", exc_info=True)
            raise
        logger.info(f"Deleted system prompt: {prompt_id}")
        return True


_repository: Optional[SystemPromptsRepository] = None


def get_system_prompts_repository() -> SystemPromptsRepository:
    global _repository
    if _repository is None:
        _repository = SystemPromptsRepository()
    return _repository
