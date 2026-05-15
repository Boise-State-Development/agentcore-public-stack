"""
Tool Catalog Repository

DynamoDB operations for tool catalog and user preferences.
Uses the same table as AppRoles with different PK patterns.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import boto3
from botocore.exceptions import ClientError

from .models import ToolDefinition, UserToolPreference, ToolStatus

logger = logging.getLogger(__name__)


class ToolCatalogRepository:
    """
    Repository for Tool Catalog CRUD operations in DynamoDB.

    Uses the AppRoles table with different PK patterns:
    - Tool: PK=TOOL#{tool_id}, SK=METADATA
    - User Preferences: PK=USER#{user_id}, SK=TOOL_PREFERENCES

    GSI1 (JwtRoleMappingIndex) is shared with RBAC and also used for category queries:
    - GSI1PK=CATEGORY#{category}, GSI1SK=TOOL#{tool_id}
    """

    def __init__(self, table_name: Optional[str] = None):
        """Initialize repository with DynamoDB table."""
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_APP_ROLES_TABLE_NAME", "app-roles"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # =========================================================================
    # Tool CRUD Operations
    # =========================================================================

    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """
        Get a tool by ID.

        Args:
            tool_id: The tool identifier

        Returns:
            ToolDefinition if found, None otherwise
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "METADATA"}
            )
            item = response.get("Item")
            if not item:
                return None
            return ToolDefinition.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting tool {tool_id}: {e}")
            raise

    async def list_tools(
        self, status: Optional[str] = None, category: Optional[str] = None
    ) -> List[ToolDefinition]:
        """
        List all tools, optionally filtered by status or category.

        Args:
            status: Optional status filter (active, deprecated, disabled)
            category: Optional category filter

        Returns:
            List of ToolDefinition objects
        """
        try:
            if category:
                # Use GSI1 for category queries
                response = self._table.query(
                    IndexName="JwtRoleMappingIndex",
                    KeyConditionExpression="GSI1PK = :pk",
                    ExpressionAttributeValues={":pk": f"CATEGORY#{category}"},
                )
                items = response.get("Items", [])

                # Handle pagination
                while "LastEvaluatedKey" in response:
                    response = self._table.query(
                        IndexName="JwtRoleMappingIndex",
                        KeyConditionExpression="GSI1PK = :pk",
                        ExpressionAttributeValues={":pk": f"CATEGORY#{category}"},
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))
            else:
                # Scan for all tools
                filter_expr = "begins_with(PK, :pk_prefix) AND SK = :sk"
                expr_values = {":pk_prefix": "TOOL#", ":sk": "METADATA"}

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

            tools = [ToolDefinition.from_dynamo_item(item) for item in items]

            # Apply status filter if provided
            if status:
                tools = [t for t in tools if t.status == status]

            # Sort by category then display_name
            tools.sort(key=lambda t: (t.category, t.display_name))

            return tools

        except ClientError as e:
            logger.error(f"Error listing tools: {e}")
            raise

    async def create_tool(self, tool: ToolDefinition) -> ToolDefinition:
        """
        Create a new tool catalog entry.

        Args:
            tool: The ToolDefinition to create

        Returns:
            The created ToolDefinition

        Raises:
            ValueError: If tool already exists
        """
        try:
            # Check if tool already exists
            existing = await self.get_tool(tool.tool_id)
            if existing:
                raise ValueError(f"Tool '{tool.tool_id}' already exists")

            # Set timestamps
            now = datetime.now(timezone.utc)
            tool.created_at = now
            tool.updated_at = now

            # Create item
            item = tool.to_dynamo_item()
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )

            logger.info(f"Created tool: {tool.tool_id}")
            return tool

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Tool '{tool.tool_id}' already exists")
            logger.error(f"Error creating tool {tool.tool_id}: {e}")
            raise

    async def update_tool(
        self, tool_id: str, updates: Dict[str, Any], admin_user_id: Optional[str] = None
    ) -> Optional[ToolDefinition]:
        """
        Update a tool's metadata.

        Args:
            tool_id: The tool identifier
            updates: Dictionary of fields to update
            admin_user_id: ID of admin performing the update

        Returns:
            Updated ToolDefinition or None if not found
        """
        try:
            existing = await self.get_tool(tool_id)
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

            logger.info(f"Updated tool: {tool_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error updating tool {tool_id}: {e}")
            raise

    async def delete_tool(self, tool_id: str) -> bool:
        """
        Delete a tool from the catalog.

        Args:
            tool_id: The tool identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            existing = await self.get_tool(tool_id)
            if not existing:
                return False

            self._table.delete_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "METADATA"}
            )

            logger.info(f"Deleted tool: {tool_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting tool {tool_id}: {e}")
            raise

    async def soft_delete_tool(
        self, tool_id: str, admin_user_id: Optional[str] = None
    ) -> Optional[ToolDefinition]:
        """
        Soft delete a tool by setting status to DISABLED.

        Args:
            tool_id: The tool identifier
            admin_user_id: ID of admin performing the deletion

        Returns:
            Updated ToolDefinition or None if not found
        """
        return await self.update_tool(
            tool_id,
            {"status": ToolStatus.DISABLED},
            admin_user_id=admin_user_id,
        )

    async def tool_exists(self, tool_id: str) -> bool:
        """Check if a tool exists in the catalog."""
        tool = await self.get_tool(tool_id)
        return tool is not None

    # =========================================================================
    # User Preferences Operations
    # =========================================================================

    async def get_user_preferences(self, user_id: str) -> UserToolPreference:
        """
        Get user's tool preferences.

        Args:
            user_id: The user identifier

        Returns:
            UserToolPreference (empty if not found)
        """
        try:
            response = self._table.get_item(
                Key={"PK": f"USER#{user_id}", "SK": "TOOL_PREFERENCES"}
            )
            item = response.get("Item")
            if not item:
                # Return empty preferences
                return UserToolPreference(user_id=user_id)
            return UserToolPreference.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting user preferences for {user_id}: {e}")
            raise

    async def save_user_preferences(
        self, user_id: str, preferences: Dict[str, bool]
    ) -> UserToolPreference:
        """
        Save user's tool preferences.

        Merges with existing preferences (does not replace).

        Args:
            user_id: The user identifier
            preferences: Map of tool_id -> enabled state

        Returns:
            Updated UserToolPreference
        """
        try:
            # Get existing preferences
            existing = await self.get_user_preferences(user_id)

            # Merge preferences
            existing.tool_preferences.update(preferences)
            existing.updated_at = datetime.now(timezone.utc)

            # Save
            item = existing.to_dynamo_item()
            self._table.put_item(Item=item)

            logger.info(f"Saved tool preferences for user: {user_id}")
            return existing

        except ClientError as e:
            logger.error(f"Error saving user preferences for {user_id}: {e}")
            raise

    async def replace_user_preferences(
        self, user_id: str, preferences: Dict[str, bool]
    ) -> UserToolPreference:
        """
        Replace user's tool preferences entirely.

        Args:
            user_id: The user identifier
            preferences: Map of tool_id -> enabled state

        Returns:
            New UserToolPreference
        """
        try:
            pref = UserToolPreference(
                user_id=user_id,
                tool_preferences=preferences,
                updated_at=datetime.now(timezone.utc),
            )

            item = pref.to_dynamo_item()
            self._table.put_item(Item=item)

            logger.info(f"Replaced tool preferences for user: {user_id}")
            return pref

        except ClientError as e:
            logger.error(f"Error replacing user preferences for {user_id}: {e}")
            raise

    async def delete_user_preferences(self, user_id: str) -> bool:
        """
        Delete user's tool preferences.

        Args:
            user_id: The user identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            existing = await self.get_user_preferences(user_id)
            if not existing.tool_preferences:
                return False

            self._table.delete_item(
                Key={"PK": f"USER#{user_id}", "SK": "TOOL_PREFERENCES"}
            )

            logger.info(f"Deleted tool preferences for user: {user_id}")
            return True

        except ClientError as e:
            logger.error(f"Error deleting user preferences for {user_id}: {e}")
            raise

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def batch_get_tools(self, tool_ids: List[str]) -> List[ToolDefinition]:
        """
        Get multiple tools by ID.

        Args:
            tool_ids: List of tool identifiers

        Returns:
            List of ToolDefinition objects (may be shorter if some not found)
        """
        if not tool_ids:
            return []

        try:
            # DynamoDB batch_get_item limit is 100
            tools = []
            for i in range(0, len(tool_ids), 100):
                batch_ids = tool_ids[i : i + 100]
                keys = [
                    {"PK": f"TOOL#{tid}", "SK": "METADATA"} for tid in batch_ids
                ]

                response = self._dynamodb.meta.client.batch_get_item(
                    RequestItems={self.table_name: {"Keys": keys}}
                )

                items = response.get("Responses", {}).get(self.table_name, [])
                tools.extend(
                    [ToolDefinition.from_dynamo_item(item) for item in items]
                )

            return tools

        except ClientError as e:
            logger.error(f"Error batch getting tools: {e}")
            raise

    async def batch_create_tools(
        self, tools: List[ToolDefinition]
    ) -> List[ToolDefinition]:
        """
        Create multiple tools at once.

        Args:
            tools: List of ToolDefinition objects to create

        Returns:
            List of created ToolDefinition objects
        """
        if not tools:
            return []

        try:
            now = datetime.now(timezone.utc)

            with self._table.batch_writer() as batch:
                for tool in tools:
                    tool.created_at = now
                    tool.updated_at = now
                    item = tool.to_dynamo_item()
                    batch.put_item(Item=item)

            logger.info(f"Batch created {len(tools)} tools")
            return tools

        except ClientError as e:
            logger.error(f"Error batch creating tools: {e}")
            raise


# Global repository instance
_repository_instance: Optional[ToolCatalogRepository] = None


def get_tool_catalog_repository() -> ToolCatalogRepository:
    """Get or create the global ToolCatalogRepository instance."""
    global _repository_instance
    if _repository_instance is None:
        _repository_instance = ToolCatalogRepository()
    return _repository_instance
