"""AppRole repository for DynamoDB operations."""

import os
import logging
from typing import List, Optional, Dict, Any

import boto3
from botocore.exceptions import ClientError

from .models import AppRole, UserGrant
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


class AppRoleRepository:
    """
    Repository for AppRole CRUD operations in DynamoDB.

    Handles the single-table design with multiple GSIs for efficient access patterns.
    """

    def __init__(self, table_name: Optional[str] = None):
        """Initialize repository with DynamoDB table."""
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # =========================================================================
    # Core CRUD Operations
    # =========================================================================

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """
        Get a role by ID.

        Args:
            role_id: The role identifier

        Returns:
            AppRole if found, None otherwise
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"ROLE#{role_id}", "SK": "DEFINITION"}
            )
            item = response.get("Item")
            if not item:
                return None
            return AppRole.from_dict(item)
        except ClientError as e:
            logger.error(f"Error getting role {role_id}: {e}")
            raise

    async def list_roles(self, enabled_only: bool = False) -> List[AppRole]:
        """
        List all roles.

        Args:
            enabled_only: If True, only return enabled roles

        Returns:
            List of AppRole objects
        """
        try:
            # Scan for all role definitions
            response = self._table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "DEFINITION"},
            )
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    FilterExpression="SK = :sk",
                    ExpressionAttributeValues={":sk": "DEFINITION"},
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            roles = [AppRole.from_dict(item) for item in items]

            if enabled_only:
                roles = [r for r in roles if r.enabled]

            # Sort by priority (descending) then by role_id
            roles.sort(key=lambda r: (-r.priority, r.role_id))

            return roles

        except ClientError as e:
            logger.error(f"Error listing roles: {e}")
            raise

    async def create_role(self, role: AppRole) -> AppRole:
        """
        Create a new role with all related mapping items.

        Args:
            role: The AppRole to create

        Returns:
            The created AppRole

        Raises:
            ValueError: If role already exists
        """
        try:
            # Check if role already exists
            existing = await self.get_role(role.role_id)
            if existing:
                raise ValueError(f"Role '{role.role_id}' already exists")

            # Set timestamps
            now = utc_now_iso()
            role.created_at = now
            role.updated_at = now

            # Create all items in a transaction
            transact_items = self._build_role_items(role)

            self._dynamodb.meta.client.transact_write_items(
                TransactItems=transact_items
            )

            logger.info(f"Created role: {role.role_id}")
            return role

        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                raise ValueError(f"Role '{role.role_id}' already exists or transaction failed")
            logger.error(f"Error creating role {role.role_id}: {e}")
            raise

    async def update_role(self, role: AppRole) -> AppRole:
        """
        Update an existing role and its mapping items.

        Args:
            role: The AppRole with updated values

        Returns:
            The updated AppRole
        """
        try:
            # Get existing role to compare mappings
            existing = await self.get_role(role.role_id)
            if not existing:
                raise ValueError(f"Role '{role.role_id}' not found")

            # Update timestamp
            role.updated_at = utc_now_iso()
            role.created_at = existing.created_at  # Preserve original

            # Delete old mapping items and create new ones
            await self._delete_mapping_items(role.role_id)

            # Create all items in a transaction
            transact_items = self._build_role_items(role)

            self._dynamodb.meta.client.transact_write_items(
                TransactItems=transact_items
            )

            logger.info(f"Updated role: {role.role_id}")
            return role

        except ClientError as e:
            logger.error(f"Error updating role {role.role_id}: {e}")
            raise

    async def delete_role(self, role_id: str) -> bool:
        """
        Delete a role and all its mapping items.

        Args:
            role_id: The role identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            existing = await self.get_role(role_id)
            if not existing:
                return False

            if existing.is_system_role:
                raise ValueError(f"Cannot delete system role '{role_id}'")

            # Delete all mapping items. A deleted role takes its default pins (D9) with
            # it — unlike an update, where they must survive.
            await self._delete_mapping_items(role_id, include_agent_pins=True)

            # Delete the role definition
            self._table.delete_item(
                Key={"PK": f"ROLE#{role_id}", "SK": "DEFINITION"}
            )

            logger.info(f"Deleted role: {role_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting role {role_id}: {e}")
            raise

    # =========================================================================
    # GSI Query Operations
    # =========================================================================

    async def get_roles_for_jwt_role(self, jwt_role: str) -> List[str]:
        """
        Get AppRole IDs that are granted by a JWT role.

        Uses GSI1 (JwtRoleMappingIndex) for efficient lookup.

        Args:
            jwt_role: The JWT role from identity provider

        Returns:
            List of AppRole IDs
        """
        try:
            response = self._table.query(
                IndexName="JwtRoleMappingIndex",
                KeyConditionExpression="GSI1PK = :pk",
                ExpressionAttributeValues={":pk": f"JWT_ROLE#{jwt_role}"},
            )

            role_ids = []
            for item in response.get("Items", []):
                if item.get("enabled", True):
                    role_ids.append(item.get("roleId"))

            return role_ids

        except ClientError as e:
            logger.error(f"Error querying JWT role mappings for {jwt_role}: {e}")
            raise

    async def get_roles_for_tool(self, tool_id: str) -> List[Dict[str, Any]]:
        """
        Get AppRoles that grant access to a tool.

        Uses GSI2 (ToolRoleMappingIndex) for efficient lookup.

        Narrowed to ``ROLE#`` sort values: the same index partition also holds
        the per-user direct grants (``GSI2SK = USER#...``, see
        ``_build_user_grant_items``), which are not roles and must not be
        reported as one. ``get_user_ids_for_tool`` is the other half.

        Args:
            tool_id: The tool identifier

        Returns:
            List of role info dicts with roleId, displayName, enabled
        """
        try:
            response = self._table.query(
                IndexName="ToolRoleMappingIndex",
                KeyConditionExpression="GSI2PK = :pk AND begins_with(GSI2SK, :role)",
                ExpressionAttributeValues={":pk": f"TOOL#{tool_id}", ":role": "ROLE#"},
            )

            return [
                {
                    "roleId": item.get("roleId"),
                    "displayName": item.get("displayName"),
                    "enabled": item.get("enabled", True),
                }
                for item in response.get("Items", [])
            ]

        except ClientError as e:
            logger.error(f"Error querying tool role mappings for {tool_id}: {e}")
            raise

    async def get_roles_for_model(self, model_id: str) -> List[Dict[str, Any]]:
        """
        Get AppRoles that grant access to a model.

        Uses GSI3 (ModelRoleMappingIndex) for efficient lookup.

        Args:
            model_id: The model identifier

        Returns:
            List of role info dicts with roleId, displayName, enabled
        """
        try:
            response = self._table.query(
                IndexName="ModelRoleMappingIndex",
                KeyConditionExpression="GSI3PK = :pk AND begins_with(GSI3SK, :role)",
                ExpressionAttributeValues={":pk": f"MODEL#{model_id}", ":role": "ROLE#"},
            )

            return [
                {
                    "roleId": item.get("roleId"),
                    "displayName": item.get("displayName"),
                    "enabled": item.get("enabled", True),
                }
                for item in response.get("Items", [])
            ]

        except ClientError as e:
            logger.error(f"Error querying model role mappings for {model_id}: {e}")
            raise

    async def get_roles_for_skill(self, skill_id: str) -> List[Dict[str, Any]]:
        """
        Get AppRoles that grant access to a skill.

        Reuses GSI2 (ToolRoleMappingIndex) with a `SKILL#` partition value, so
        skill grants share the tool reverse-lookup index without a new GSI
        (the `TOOL#` and `SKILL#` partitions are disjoint). See spec §5.

        Args:
            skill_id: The skill identifier

        Returns:
            List of role info dicts with roleId, displayName, enabled
        """
        try:
            response = self._table.query(
                IndexName="ToolRoleMappingIndex",
                KeyConditionExpression="GSI2PK = :pk AND begins_with(GSI2SK, :role)",
                ExpressionAttributeValues={":pk": f"SKILL#{skill_id}", ":role": "ROLE#"},
            )

            return [
                {
                    "roleId": item.get("roleId"),
                    "displayName": item.get("displayName"),
                    "enabled": item.get("enabled", True),
                }
                for item in response.get("Items", [])
            ]

        except ClientError as e:
            logger.error(f"Error querying skill role mappings for {skill_id}: {e}")
            raise

    # =========================================================================
    # Direct user grants (``UserGrant``)
    # =========================================================================
    #
    # Stored in this same table under the user's own partition, ``USER#<id>``,
    # which already holds that user's ``TOOL_PREFERENCES`` and
    # ``SKILL_PREFERENCES`` rows (``apis/shared/tools/repository.py``,
    # ``apis/shared/skills/repository.py``). Every sort key here therefore
    # starts with ``GRANT``, and the mapping sweep deletes by that prefix only —
    # a sweep that cleared "everything but the definition" would take the
    # user's picker preferences with it.
    #
    #   PK=USER#<id>  SK=GRANTS              the record (+ GSI5 for "list all")
    #   PK=USER#<id>  SK=GRANT_TOOL#<tool>   GSI2PK=TOOL#<tool>   GSI2SK=USER#<id>
    #   PK=USER#<id>  SK=GRANT_MODEL#<m>     GSI3PK=MODEL#<m>     GSI3SK=USER#<id>
    #   PK=USER#<id>  SK=GRANT_SKILL#<s>     GSI2PK=SKILL#<s>     GSI2SK=USER#<id>
    #
    # The mapping rows reuse the role reverse-lookup indexes with a ``USER#``
    # sort value, so "who is directly granted X?" costs no new GSI (one GSI per
    # UpdateTable, and the stack is near its resource budget). The role queries
    # above narrow to ``ROLE#`` so the two populations never mix.

    USER_GRANT_SK = "GRANTS"
    USER_GRANT_MAPPING_PREFIX = "GRANT_"
    USER_GRANT_ENTITY_TYPE = "ENTITY#USER_GRANT"

    async def get_user_grant(self, user_id: str) -> Optional[UserGrant]:
        """The user's direct grant record, or ``None`` when they have none."""
        try:
            response = self._table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": self.USER_GRANT_SK}
            )
            item = response.get("Item")
            if not item:
                return None
            return UserGrant.from_dict(item)
        except ClientError as e:
            logger.error(f"Error getting user grant for {user_id}: {e}")
            raise

    async def put_user_grant(self, grant: UserGrant) -> UserGrant:
        """Create or replace a user's direct grant and its reverse-lookup rows.

        The record is the source of truth and is written first; the mapping
        rows are a projection rebuilt from it. ``created_at`` survives a
        replace.
        """
        try:
            existing = await self.get_user_grant(grant.user_id)
            now = utc_now_iso()
            grant.updated_at = now
            grant.created_at = existing.created_at if existing and existing.created_at else now

            self._table.put_item(Item=self._build_user_grant_record(grant))

            await self._delete_user_grant_mapping_items(grant.user_id)
            with self._table.batch_writer() as batch:
                for item in self._build_user_grant_mapping_items(grant):
                    batch.put_item(Item=item)

            logger.info(f"Wrote direct grant for user {grant.user_id}")
            return grant
        except ClientError as e:
            logger.error(f"Error writing user grant for {grant.user_id}: {e}")
            raise

    async def delete_user_grant(self, user_id: str) -> bool:
        """Remove a user's direct grant. Returns False when there was none."""
        try:
            existing = await self.get_user_grant(user_id)
            if not existing:
                return False
            await self._delete_user_grant_mapping_items(user_id)
            self._table.delete_item(
                Key={"PK": f"USER#{user_id}", "SK": self.USER_GRANT_SK}
            )
            logger.info(f"Deleted direct grant for user {user_id}")
            return True
        except ClientError as e:
            logger.error(f"Error deleting user grant for {user_id}: {e}")
            raise

    async def list_user_grants(self) -> List[UserGrant]:
        """Every direct grant record, via the EntityTypeIndex (no table scan)."""
        try:
            kwargs: Dict[str, Any] = {
                "IndexName": "EntityTypeIndex",
                "KeyConditionExpression": "GSI5PK = :pk",
                "ExpressionAttributeValues": {":pk": self.USER_GRANT_ENTITY_TYPE},
            }
            grants: List[UserGrant] = []
            while True:
                response = self._table.query(**kwargs)
                grants.extend(UserGrant.from_dict(i) for i in response.get("Items", []))
                last = response.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
            return grants
        except ClientError as e:
            logger.error(f"Error listing user grants: {e}")
            raise

    async def get_user_ids_for_tool(self, tool_id: str) -> List[str]:
        """Users directly granted ``tool_id`` (reverse lookup on GSI2)."""
        return await self._user_ids_on_index(
            "ToolRoleMappingIndex", "GSI2PK", "GSI2SK", f"TOOL#{tool_id}"
        )

    async def get_user_ids_for_model(self, model_id: str) -> List[str]:
        """Users directly granted ``model_id`` (reverse lookup on GSI3)."""
        return await self._user_ids_on_index(
            "ModelRoleMappingIndex", "GSI3PK", "GSI3SK", f"MODEL#{model_id}"
        )

    async def get_user_ids_for_skill(self, skill_id: str) -> List[str]:
        """Users directly granted ``skill_id`` (reverse lookup on GSI2)."""
        return await self._user_ids_on_index(
            "ToolRoleMappingIndex", "GSI2PK", "GSI2SK", f"SKILL#{skill_id}"
        )

    async def _user_ids_on_index(
        self, index_name: str, pk_attr: str, sk_attr: str, pk_value: str
    ) -> List[str]:
        try:
            response = self._table.query(
                IndexName=index_name,
                KeyConditionExpression=f"{pk_attr} = :pk AND begins_with({sk_attr}, :user)",
                ExpressionAttributeValues={":pk": pk_value, ":user": "USER#"},
            )
            return sorted(
                str(item[sk_attr])[len("USER#"):]
                for item in response.get("Items", [])
                if sk_attr in item
            )
        except ClientError as e:
            logger.error(f"Error querying direct grants for {pk_value}: {e}")
            raise

    def _build_user_grant_record(self, grant: UserGrant) -> Dict[str, Any]:
        return {
            "PK": f"USER#{grant.user_id}",
            "SK": self.USER_GRANT_SK,
            # Sparse EntityTypeIndex stamp: "list every user with a direct grant".
            "GSI5PK": self.USER_GRANT_ENTITY_TYPE,
            "GSI5SK": f"USER#{grant.user_id}",
            **grant.to_dict(),
        }

    def _build_user_grant_mapping_items(self, grant: UserGrant) -> List[Dict[str, Any]]:
        """Reverse-lookup rows for a grant, one per granted id."""
        pk = f"USER#{grant.user_id}"
        common = {"userId": grant.user_id, "expiresAt": grant.expires_at, "enabled": True}
        items: List[Dict[str, Any]] = []
        for tool_id in grant.granted_tools:
            items.append({
                "PK": pk, "SK": f"GRANT_TOOL#{tool_id}",
                "GSI2PK": f"TOOL#{tool_id}", "GSI2SK": pk, **common,
            })
        for model_id in grant.granted_models:
            items.append({
                "PK": pk, "SK": f"GRANT_MODEL#{model_id}",
                "GSI3PK": f"MODEL#{model_id}", "GSI3SK": pk, **common,
            })
        for skill_id in grant.granted_skills:
            items.append({
                "PK": pk, "SK": f"GRANT_SKILL#{skill_id}",
                "GSI2PK": f"SKILL#{skill_id}", "GSI2SK": pk, **common,
            })
        return items

    async def _delete_user_grant_mapping_items(self, user_id: str) -> None:
        """Delete a user's ``GRANT_*`` mapping rows — and nothing else under ``USER#``."""
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_id}",
                    ":prefix": self.USER_GRANT_MAPPING_PREFIX,
                },
            )
            items = response.get("Items", [])
            if not items:
                return
            with self._table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        except ClientError as e:
            logger.error(f"Error deleting user grant mappings for {user_id}: {e}")
            raise

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _build_role_items(self, role: AppRole) -> List[Dict]:
        """
        Build all DynamoDB items for a role (definition + mappings).

        Returns list of TransactWriteItem dicts.
        """
        items = []

        # 1. Role definition item
        definition_item = {
            "PK": f"ROLE#{role.role_id}",
            "SK": "DEFINITION",
            **role.to_dict(),
        }
        items.append(
            {"Put": {"TableName": self.table_name, "Item": definition_item}}
        )

        # 2. JWT role mapping items (for GSI1)
        for jwt_role in role.jwt_role_mappings:
            mapping_item = {
                "PK": f"ROLE#{role.role_id}",
                "SK": f"JWT_MAPPING#{jwt_role}",
                "GSI1PK": f"JWT_ROLE#{jwt_role}",
                "GSI1SK": f"ROLE#{role.role_id}",
                "roleId": role.role_id,
                "enabled": role.enabled,
            }
            items.append(
                {"Put": {"TableName": self.table_name, "Item": mapping_item}}
            )

        # 3. Tool permission mapping items (for GSI2)
        for tool_id in role.granted_tools:
            mapping_item = {
                "PK": f"ROLE#{role.role_id}",
                "SK": f"TOOL_GRANT#{tool_id}",
                "GSI2PK": f"TOOL#{tool_id}",
                "GSI2SK": f"ROLE#{role.role_id}",
                "roleId": role.role_id,
                "displayName": role.display_name,
                "enabled": role.enabled,
            }
            items.append(
                {"Put": {"TableName": self.table_name, "Item": mapping_item}}
            )

        # 4. Model permission mapping items (for GSI3)
        for model_id in role.granted_models:
            mapping_item = {
                "PK": f"ROLE#{role.role_id}",
                "SK": f"MODEL_GRANT#{model_id}",
                "GSI3PK": f"MODEL#{model_id}",
                "GSI3SK": f"ROLE#{role.role_id}",
                "roleId": role.role_id,
                "displayName": role.display_name,
                "enabled": role.enabled,
            }
            items.append(
                {"Put": {"TableName": self.table_name, "Item": mapping_item}}
            )

        # 5. Skill permission mapping items — reuse the GSI2 keyspace
        # (ToolRoleMappingIndex) with a `SKILL#` partition value (spec §5).
        for skill_id in role.granted_skills:
            mapping_item = {
                "PK": f"ROLE#{role.role_id}",
                "SK": f"SKILL_GRANT#{skill_id}",
                "GSI2PK": f"SKILL#{skill_id}",
                "GSI2SK": f"ROLE#{role.role_id}",
                "roleId": role.role_id,
                "displayName": role.display_name,
                "enabled": role.enabled,
            }
            items.append(
                {"Put": {"TableName": self.table_name, "Item": mapping_item}}
            )

        return items

    async def _delete_mapping_items(self, role_id: str, include_agent_pins: bool = False):
        """Delete a role's mapping items (JWT, tool, model, skill grants).

        ⚠️ **Deletes by prefix, not "everything that is not DEFINITION".** ``update_role``
        rebuilds the mapping items from the ``AppRole`` record, so anything under this
        partition that the record does not carry would be wiped by every role edit and
        never rewritten. Default agent pins (``AGENT_PIN#``, D9) are exactly that: they
        share the partition with the grants but are deliberately not part of ``AppRole``,
        because a pin is not a permission. Any future per-role item that is not
        reconstructible from ``_build_role_items`` needs the same protection.

        ``include_agent_pins`` is set only by ``delete_role``, where the role — and so
        everything hanging off it — is going away.
        """
        prefixes = ("JWT_MAPPING#", "TOOL_GRANT#", "MODEL_GRANT#", "SKILL_GRANT#")
        if include_agent_pins:
            prefixes = prefixes + ("AGENT_PIN#",)

        try:
            # Query all items with this role's PK
            response = self._table.query(
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": f"ROLE#{role_id}"},
            )

            with self._table.batch_writer() as batch:
                for item in response.get("Items", []):
                    if str(item["SK"]).startswith(prefixes):
                        batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        except ClientError as e:
            logger.error(f"Error deleting mapping items for {role_id}: {e}")
            raise

    async def role_exists(self, role_id: str) -> bool:
        """Check if a role exists."""
        role = await self.get_role(role_id)
        return role is not None
