"""
Skill Catalog Repository

DynamoDB operations for the admin-managed Skill catalog. Mirrors
``ToolCatalogRepository`` and reuses the same table as AppRoles
(``DYNAMODB_APP_ROLES_TABLE_NAME``) with a distinct PK pattern:

  - Skill: PK=SKILL#{skill_id}, SK=METADATA

``SkillOwnerIndex`` (GSI4: GSI4PK=OWNER#{owner_id}, GSI4SK=SKILL#{skill_id})
is provisioned for a Phase-2 "list my skills" query; v1 admin lists scan by
``begins_with(PK, "SKILL#")``. See
``docs/specs/admin-skills-rbac-tool-binding.md`` (§5).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from .models import SkillDefinition, SkillStatus, UserSkillPreference

logger = logging.getLogger(__name__)


class SkillCatalogRepository:
    """
    Repository for Skill Catalog CRUD operations in DynamoDB.

    Uses the AppRoles table with a distinct PK pattern:
    - Skill: PK=SKILL#{skill_id}, SK=METADATA
    """

    def __init__(self, table_name: Optional[str] = None):
        """Initialize repository with DynamoDB table."""
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # =========================================================================
    # Skill CRUD Operations
    # =========================================================================

    async def get_skill(self, skill_id: str) -> Optional[SkillDefinition]:
        """
        Get a skill by ID.

        Args:
            skill_id: The skill identifier

        Returns:
            SkillDefinition if found, None otherwise
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"SKILL#{skill_id}", "SK": "METADATA"}
            )
            item = response.get("Item")
            if not item:
                return None
            return SkillDefinition.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting skill {skill_id}: {e}")
            raise

    async def list_skills(
        self, status: Optional[str] = None
    ) -> List[SkillDefinition]:
        """
        List all skills, optionally filtered by status.

        Args:
            status: Optional status filter (active, draft, disabled)

        Returns:
            List of SkillDefinition objects
        """
        try:
            filter_expr = "begins_with(PK, :pk_prefix) AND SK = :sk"
            expr_values = {":pk_prefix": "SKILL#", ":sk": "METADATA"}

            response = self._table.scan(
                FilterExpression=filter_expr,
                ExpressionAttributeValues=expr_values,
            )
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    FilterExpression=filter_expr,
                    ExpressionAttributeValues=expr_values,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            skills = [SkillDefinition.from_dynamo_item(item) for item in items]

            # Apply status filter if provided
            if status:
                skills = [s for s in skills if s.status == status]

            # Sort by category then display_name
            skills.sort(key=lambda s: (s.category or "", s.display_name))

            return skills

        except ClientError as e:
            logger.error(f"Error listing skills: {e}")
            raise

    async def create_skill(self, skill: SkillDefinition) -> SkillDefinition:
        """
        Create a new skill catalog entry.

        Args:
            skill: The SkillDefinition to create

        Returns:
            The created SkillDefinition

        Raises:
            ValueError: If skill already exists
        """
        try:
            # Check if skill already exists
            existing = await self.get_skill(skill.skill_id)
            if existing:
                raise ValueError(f"Skill '{skill.skill_id}' already exists")

            # Set timestamps
            now = datetime.now(timezone.utc)
            skill.created_at = now
            skill.updated_at = now

            # Create item
            item = skill.to_dynamo_item()
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )

            logger.info(f"Created skill: {skill.skill_id}")
            return skill

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Skill '{skill.skill_id}' already exists")
            logger.error(f"Error creating skill {skill.skill_id}: {e}")
            raise

    async def update_skill(
        self,
        skill_id: str,
        updates: Dict[str, Any],
        admin_user_id: Optional[str] = None,
    ) -> Optional[SkillDefinition]:
        """
        Update a skill's metadata.

        Args:
            skill_id: The skill identifier
            updates: Dictionary of fields to update
            admin_user_id: ID of admin performing the update

        Returns:
            Updated SkillDefinition or None if not found
        """
        try:
            existing = await self.get_skill(skill_id)
            if not existing:
                return None

            # Apply updates
            for field, value in updates.items():
                if hasattr(existing, field) and value is not None:
                    setattr(existing, field, value)

            # Update audit fields
            existing.updated_at = datetime.now(timezone.utc)
            if admin_user_id:
                existing.updated_by = admin_user_id

            # Save
            item = existing.to_dynamo_item()
            self._table.put_item(Item=item)

            logger.info(f"Updated skill: {skill_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error updating skill {skill_id}: {e}")
            raise

    async def delete_skill(self, skill_id: str) -> bool:
        """
        Hard delete a skill from the catalog.

        Args:
            skill_id: The skill identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            existing = await self.get_skill(skill_id)
            if not existing:
                return False

            self._table.delete_item(
                Key={"PK": f"SKILL#{skill_id}", "SK": "METADATA"}
            )

            logger.info(f"Deleted skill: {skill_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting skill {skill_id}: {e}")
            raise

    async def soft_delete_skill(
        self, skill_id: str, admin_user_id: Optional[str] = None
    ) -> Optional[SkillDefinition]:
        """
        Soft delete a skill by setting status to DISABLED.

        Args:
            skill_id: The skill identifier
            admin_user_id: ID of admin performing the deletion

        Returns:
            Updated SkillDefinition or None if not found
        """
        return await self.update_skill(
            skill_id,
            {"status": SkillStatus.DISABLED},
            admin_user_id=admin_user_id,
        )

    async def skill_exists(self, skill_id: str) -> bool:
        """Check if a skill exists in the catalog."""
        skill = await self.get_skill(skill_id)
        return skill is not None

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def batch_get_skills(
        self, skill_ids: List[str]
    ) -> List[SkillDefinition]:
        """
        Get multiple skills by ID.

        Args:
            skill_ids: List of skill identifiers

        Returns:
            List of SkillDefinition objects (may be shorter if some not found)
        """
        if not skill_ids:
            return []

        try:
            # DynamoDB batch_get_item limit is 100
            skills: List[SkillDefinition] = []
            for i in range(0, len(skill_ids), 100):
                batch_ids = skill_ids[i : i + 100]
                keys = [
                    {"PK": f"SKILL#{sid}", "SK": "METADATA"} for sid in batch_ids
                ]

                response = self._dynamodb.meta.client.batch_get_item(
                    RequestItems={self.table_name: {"Keys": keys}}
                )

                items = response.get("Responses", {}).get(self.table_name, [])
                skills.extend(
                    [SkillDefinition.from_dynamo_item(item) for item in items]
                )

            return skills

        except ClientError as e:
            logger.error(f"Error batch getting skills: {e}")
            raise

    # =========================================================================
    # User Preferences (mirrors ToolCatalogRepository)
    # =========================================================================

    async def get_user_preferences(self, user_id: str) -> UserSkillPreference:
        """
        Get user's per-skill preferences.

        Args:
            user_id: The user identifier

        Returns:
            UserSkillPreference (empty if not found)
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": "SKILL_PREFERENCES"}
            )
            item = response.get("Item")
            if not item:
                return UserSkillPreference(user_id=user_id)
            return UserSkillPreference.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting skill preferences for {user_id}: {e}")
            raise

    async def save_user_preferences(
        self, user_id: str, preferences: Dict[str, bool]
    ) -> UserSkillPreference:
        """
        Save user's per-skill preferences.

        Merges with existing preferences (does not replace).

        Args:
            user_id: The user identifier
            preferences: Map of skill_id -> enabled state

        Returns:
            Updated UserSkillPreference
        """
        try:
            existing = await self.get_user_preferences(user_id)

            existing.skill_preferences.update(preferences)
            existing.updated_at = datetime.now(timezone.utc)

            self._table.put_item(Item=existing.to_dynamo_item())

            logger.info(f"Saved skill preferences for user: {user_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error saving skill preferences for {user_id}: {e}")
            raise


# Global repository instance
_repository_instance: Optional[SkillCatalogRepository] = None


def get_skill_catalog_repository() -> SkillCatalogRepository:
    """Get or create the global SkillCatalogRepository instance."""
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = SkillCatalogRepository()
    return _repository_instance
