"""
Tool Catalog Repository

DynamoDB operations for tool catalog and user preferences.
Uses the same table as AppRoles with different PK patterns.
"""

import asyncio
import os
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import boto3
from botocore.exceptions import ClientError

from apis.shared.caching import config_cache
from apis.shared.dynamo_errors import is_missing_index_error
from .models import (
    ENTITY_TYPE_TOOL,
    ToolCapabilitySnapshot,
    ToolDefinition,
    UserToolPreference,
    ToolStatus,
)

logger = logging.getLogger(__name__)

# The GSI that makes "list every tool" a Query instead of a Scan of a table
# shared with roles, skills and a preferences row per user.
ENTITY_TYPE_INDEX = "EntityTypeIndex"


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

    # =========================================================================
    # MCP capability snapshots (PK=TOOL#{id}, SK=CAPABILITIES)
    # =========================================================================

    async def get_capabilities(
        self, tool_id: str
    ) -> Optional["ToolCapabilitySnapshot"]:
        """The stored prompts/resources snapshot for a tool, or None if never discovered."""
        try:
            response = self._table.get_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "CAPABILITIES"}
            )
            item = response.get("Item")
            if not item:
                return None
            return ToolCapabilitySnapshot.from_dynamo_item(item)
        except ClientError as e:
            logger.error(f"Error getting capabilities for {tool_id}: {e}")
            raise

    async def put_capabilities(
        self, snapshot: "ToolCapabilitySnapshot"
    ) -> "ToolCapabilitySnapshot":
        """Write a capability snapshot, replacing any previous one.

        Replace rather than merge: the snapshot is a point-in-time answer from
        the server, and merging would keep prompts the server has since removed.
        """
        try:
            self._table.put_item(Item=snapshot.to_dynamo_item())
            return snapshot
        except ClientError as e:
            logger.error(
                f"Error writing capabilities for {snapshot.tool_id}: {e}"
            )
            raise

    async def delete_capabilities(self, tool_id: str) -> None:
        """Drop a tool's snapshot — used when the tool itself is deleted."""
        try:
            self._table.delete_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "CAPABILITIES"}
            )
        except ClientError as e:
            logger.error(f"Error deleting capabilities for {tool_id}: {e}")
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
                items = await asyncio.to_thread(self._query_tool_items_by_category, category)
            else:
                # The whole-catalog read is the one on the SPA's first-load path
                # (GET /tools/), so it is cached per process; parsing below is
                # not, because callers mutate what they get back — see
                # `list_tools_with_roles`, which writes `allowed_app_roles` onto
                # each ToolDefinition in place.
                items = await config_cache.get_or_load(
                    config_cache.TOOL_CATALOG,
                    self._load_all_tool_items,
                )

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

    async def _load_all_tool_items(self) -> List[dict]:
        """Every tool row, by Query on EntityTypeIndex, with Scan as the safety net.

        The Query is the point of the index: this table is shared with roles,
        skills, role grants and one tool-preferences row PER USER, so the Scan
        reads the whole table to return the tool rows and its cost grows with
        enrollment rather than with the catalog. Measured on dev, 95 items read
        to return 24.

        Two ways the index can fail to answer, and neither may take the catalog
        down with it — an empty tool list is not a degraded experience here, it
        is a broken one:

        **The index is not there.** `platform.yml` and `backend.yml` are ordered
        by nothing, a GSI is still CREATING after CloudFormation reports success,
        and a rolled-back stack ships its images anyway. Unlike the surfaces
        `dynamo_errors` was written for, we have a *correct* answer available, so
        we fall back to it rather than degrading to empty.

        **The index is there but unpopulated.** The keys are sparse, so a catalog
        whose backfill has not run indexes nothing and the Query succeeds with
        zero rows — no error to catch. A zero result is therefore treated as
        suspect and re-read via Scan: if the table really is empty (a fresh
        install before seeding) both agree and it costs one extra read per cache
        fill; if it is not, we serve the truth and say loudly why.

        A partial backfill is NOT covered — detecting it would mean scanning
        every time, which is the cost being removed. That is what the release
        gate and the backfill's own `skipped=0 failed=0` report are for.
        """
        try:
            items = await asyncio.to_thread(self._query_tool_items)
        except ClientError as exc:
            if not is_missing_index_error(exc):
                raise
            logger.warning(
                "⚠️ DynamoDB index '%s' does not exist — falling back to Scan for "
                "the tool catalog. Expected transiently while the GSI is CREATING "
                "or a deploy is incomplete; if it persists, every catalog read is "
                "paying a full table scan.",
                ENTITY_TYPE_INDEX,
            )
            return await asyncio.to_thread(self._scan_tool_items)

        if items:
            return items

        # Zero rows from a sparse index is indistinguishable from "no tools", so
        # confirm against the base table before believing it.
        scanned = await asyncio.to_thread(self._scan_tool_items)
        if scanned:
            logger.error(
                "⚠️ DynamoDB index '%s' returned 0 tools but the table holds %d — "
                "the %s backfill has not been run in this environment. Serving the "
                "Scan result so the catalog is correct; run the backfill.",
                ENTITY_TYPE_INDEX,
                len(scanned),
                "backfill_tool_catalog_index.py",
            )
        return scanned

    def _query_tool_items(self) -> List[dict]:
        """Query the raw tool rows off EntityTypeIndex. Blocking; via ``to_thread``."""
        kwargs = {
            "IndexName": ENTITY_TYPE_INDEX,
            "KeyConditionExpression": "GSI5PK = :pk",
            "ExpressionAttributeValues": {":pk": ENTITY_TYPE_TOOL},
        }
        response = self._table.query(**kwargs)
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = self._table.query(
                **kwargs, ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            items.extend(response.get("Items", []))

        return items

    def _scan_tool_items(self) -> List[dict]:
        """Scan the raw TOOL#/METADATA items. Blocking; call via ``to_thread``."""
        filter_expr = "begins_with(PK, :pk_prefix) AND SK = :sk"
        expr_values = {":pk_prefix": "TOOL#", ":sk": "METADATA"}

        response = self._table.scan(
            FilterExpression=filter_expr,
            ExpressionAttributeValues=expr_values,
        )
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                FilterExpression=filter_expr,
                ExpressionAttributeValues=expr_values,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        return items

    def _query_tool_items_by_category(self, category: str) -> List[dict]:
        """Query raw items for one category. Blocking; call via ``to_thread``.

        Not cached: it is a bounded GSI query off the first-load path, and
        caching per category would multiply the invalidation surface for no
        measured gain.
        """
        response = self._table.query(
            IndexName="JwtRoleMappingIndex",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"CATEGORY#{category}"},
        )
        items = response.get("Items", [])

        while "LastEvaluatedKey" in response:
            response = self._table.query(
                IndexName="JwtRoleMappingIndex",
                KeyConditionExpression="GSI1PK = :pk",
                ExpressionAttributeValues={":pk": f"CATEGORY#{category}"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        return items

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

            # Invalidate in the repository, not the admin route: every write
            # to the catalog lands here, so a new caller cannot forget to.
            config_cache.invalidate(config_cache.TOOL_CATALOG)
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

            config_cache.invalidate(config_cache.TOOL_CATALOG)
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

            config_cache.invalidate(config_cache.TOOL_CATALOG)
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

            config_cache.invalidate(config_cache.TOOL_CATALOG)
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
