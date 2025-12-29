"""AppRole repository for DynamoDB operations."""

import os
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from .models import AppRole, EffectivePermissions

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
            now = datetime.utcnow().isoformat() + "Z"
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
            role.updated_at = datetime.utcnow().isoformat() + "Z"
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

            # Delete all mapping items
            await self._delete_mapping_items(role_id)

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

        Args:
            tool_id: The tool identifier

        Returns:
            List of role info dicts with roleId, displayName, enabled
        """
        try:
            response = self._table.query(
                IndexName="ToolRoleMappingIndex",
                KeyConditionExpression="GSI2PK = :pk",
                ExpressionAttributeValues={":pk": f"TOOL#{tool_id}"},
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
                KeyConditionExpression="GSI3PK = :pk",
                ExpressionAttributeValues={":pk": f"MODEL#{model_id}"},
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

        return items

    async def _delete_mapping_items(self, role_id: str):
        """Delete all mapping items for a role (JWT, tool, model mappings)."""
        try:
            # Query all items with this role's PK
            response = self._table.query(
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": f"ROLE#{role_id}"},
            )

            # Delete each item except the DEFINITION (which will be updated)
            with self._table.batch_writer() as batch:
                for item in response.get("Items", []):
                    if item["SK"] != "DEFINITION":
                        batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        except ClientError as e:
            logger.error(f"Error deleting mapping items for {role_id}: {e}")
            raise

    async def role_exists(self, role_id: str) -> bool:
        """Check if a role exists."""
        role = await self.get_role(role_id)
        return role is not None
