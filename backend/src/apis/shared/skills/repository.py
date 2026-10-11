"""
Skill Catalog Repository

DynamoDB operations for the admin-managed Skill catalog. Mirrors
``ToolCatalogRepository`` and reuses the same table as AppRoles
(``DYNAMODB_APP_ROLES_TABLE_NAME``) with a distinct PK pattern:

  - Skill: PK=SKILL#{skill_id}, SK=METADATA
  - Version: PK=SKILL#{skill_id}, SK=VERSION#{n:08d} (``versions.SkillVersion``,
    cut when a binding pins the skill; no GSI keys)

``SkillOwnerIndex`` (GSI4: GSI4PK=OWNER#{owner_id}, GSI4SK=SKILL#{skill_id})
backs the user-authored tier's "list my skills" query
(``list_skills_by_owner``). Admin catalog lists still scan by
``begins_with(PK, "SKILL#")`` and narrow to ``owner_id == "system"``. See
``docs/specs/skills-as-agent-primitive.md`` (§4, PR-3).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .models import SkillDefinition, SkillStatus, UserSkillPreference
from .versions import VERSION_SK_PREFIX, SkillVersion, version_sk

logger = logging.getLogger(__name__)

# GSI4 — see module docstring. Provisioned in the CDK auth-tables construct.
SKILL_OWNER_INDEX = "SkillOwnerIndex"


class SkillVersionExistsError(ValueError):
    """A version number was taken by a concurrent pin."""

    def __init__(self, skill_id: str, number: Optional[int]):
        super().__init__(f"Version {number} of skill {skill_id} already exists")
        self.skill_id = skill_id
        self.number = number


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
        self, status: Optional[str] = None, owner_id: Optional[str] = None
    ) -> List[SkillDefinition]:
        """
        List all skills, optionally filtered by status and/or owner.

        Args:
            status: Optional status filter (active, draft, disabled)
            owner_id: Optional owner filter. Pass ``"system"`` to get only the
                admin catalog (excluding every user-authored skill); pass a user
                id to get that user's skills — though ``list_skills_by_owner``
                is the cheap GSI-backed path for the latter.

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

            if owner_id is not None:
                skills = [s for s in skills if s.owner_id == owner_id]

            # Sort by category then display_name
            skills.sort(key=lambda s: (s.category or "", s.display_name))

            return skills

        except ClientError as e:
            logger.error(f"Error listing skills: {e}")
            raise

    async def list_skills_by_owner(
        self, owner_id: str, status: Optional[str] = None
    ) -> List[SkillDefinition]:
        """
        List the skills authored by one owner, via the ``SkillOwnerIndex`` GSI.

        This is the "list my skills" query for the user-authored tier — a
        partition query, not a table scan, so it stays cheap as the catalog
        grows. Passing ``"system"`` returns the admin catalog.

        Args:
            owner_id: Author identity (a user id, or ``"system"``)
            status: Optional status filter (active, draft, disabled)

        Returns:
            List of SkillDefinition objects, sorted by display name
        """
        try:
            key_condition = Key("GSI4PK").eq(f"OWNER#{owner_id}")

            response = self._table.query(
                IndexName=SKILL_OWNER_INDEX,
                KeyConditionExpression=key_condition,
            )
            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = self._table.query(
                    IndexName=SKILL_OWNER_INDEX,
                    KeyConditionExpression=key_condition,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            skills = [SkillDefinition.from_dynamo_item(item) for item in items]

            if status:
                skills = [s for s in skills if s.status == status]

            skills.sort(key=lambda s: s.display_name.lower())
            return skills

        except ClientError as e:
            logger.error(f"Error listing skills for owner {owner_id}: {e}")
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
            List of SkillDefinition objects (may be shorter if some not found),
            sorted by skill_id. DynamoDB batch_get_item response order is
            arbitrary; these records feed the <available_skills> system-prompt
            block, and an order flip between turns invalidates the Bedrock
            prompt cache (exact-prefix match).
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

            return sorted(skills, key=lambda s: s.skill_id)

        except ClientError as e:
            logger.error(f"Error batch getting skills: {e}")
            raise

    # =========================================================================
    # Version snapshots (shared-projects 3.1): SK=VERSION#{n:08d}
    # =========================================================================

    async def put_skill_version(self, version: SkillVersion) -> SkillVersion:
        """Write one numbered version, refusing to overwrite an existing number.

        Raises ``SkillVersionExistsError`` when that number is taken, so the caller
        can re-pick (see ``pinning.pin_current``).
        """
        try:
            self._table.put_item(
                Item=version.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SkillVersionExistsError(version.skill_id, version.version) from e
            logger.error(f"Error writing version {version.version} of skill {version.skill_id}: {e}")
            raise
        logger.info(f"Cut version {version.version} of skill {version.skill_id}")
        return version

    async def get_latest_skill_version(self, skill_id: str) -> Optional[SkillVersion]:
        """The highest-numbered version, read as the last key of the partition."""
        response = self._table.query(
            KeyConditionExpression=Key("PK").eq(f"SKILL#{skill_id}")
            & Key("SK").begins_with(VERSION_SK_PREFIX),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return SkillVersion.from_dynamo_item(items[0]) if items else None

    async def batch_get_skill_versions(
        self, refs: List[Tuple[str, int]]
    ) -> Dict[Tuple[str, int], SkillVersion]:
        """Versions by ``(skill_id, number)``; a missing one is simply absent."""
        wanted = list(dict.fromkeys(refs))
        found: Dict[Tuple[str, int], SkillVersion] = {}
        for i in range(0, len(wanted), 100):
            keys = [
                {"PK": f"SKILL#{sid}", "SK": version_sk(n)} for sid, n in wanted[i : i + 100]
            ]
            response = self._dynamodb.meta.client.batch_get_item(
                RequestItems={self.table_name: {"Keys": keys}}
            )
            for item in response.get("Responses", {}).get(self.table_name, []):
                version = SkillVersion.from_dynamo_item(item)
                found[(version.skill_id, version.version)] = version
        return found

    async def delete_skill_versions(self, skill_id: str) -> List[SkillVersion]:
        """Delete every version of a skill; return what was deleted.

        Called when the skill itself is hard-deleted. Versions must not outlive it:
        a skill id can be reused, and a stale pin on a project would otherwise run
        the deleted skill's instructions under the new skill's live status.
        """
        deleted: List[SkillVersion] = []
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(f"SKILL#{skill_id}")
            & Key("SK").begins_with(VERSION_SK_PREFIX),
        }
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                deleted.append(SkillVersion.from_dynamo_item(item))
                self._table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        if deleted:
            logger.info(f"Deleted {len(deleted)} version(s) of skill {skill_id}")
        return deleted

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
