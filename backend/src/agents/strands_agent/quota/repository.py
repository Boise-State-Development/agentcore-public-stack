"""DynamoDB repository for quota management (Phase 1)."""

from typing import Optional, List
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import logging
import uuid
from .models import QuotaTier, QuotaAssignment, QuotaEvent, QuotaAssignmentType

logger = logging.getLogger(__name__)


class QuotaRepository:
    """DynamoDB repository for quota management (Phase 1)"""

    def __init__(
        self,
        table_name: str = "UserQuotas",
        events_table_name: str = "QuotaEvents"
    ):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(table_name)
        self.events_table = self.dynamodb.Table(events_table_name)

    # ========== Quota Tiers ==========

    async def get_tier(self, tier_id: str) -> Optional[QuotaTier]:
        """Get quota tier by ID (targeted query)"""
        try:
            response = self.table.get_item(
                Key={
                    "PK": f"QUOTA_TIER#{tier_id}",
                    "SK": "METADATA"
                }
            )

            if 'Item' not in response:
                return None

            item = response['Item']
            # Remove DynamoDB keys
            item.pop('PK', None)
            item.pop('SK', None)

            return QuotaTier(**item)
        except ClientError as e:
            logger.error(f"Error getting tier {tier_id}: {e}")
            return None

    async def list_tiers(self, enabled_only: bool = False) -> List[QuotaTier]:
        """List all quota tiers (query with begins_with)"""
        try:
            # Use Query on PK prefix instead of Scan
            response = self.table.query(
                KeyConditionExpression="begins_with(PK, :prefix)",
                ExpressionAttributeValues={
                    ":prefix": "QUOTA_TIER#"
                }
            )

            tiers = []
            for item in response.get('Items', []):
                item.pop('PK', None)
                item.pop('SK', None)
                tier = QuotaTier(**item)

                if enabled_only and not tier.enabled:
                    continue

                tiers.append(tier)

            return tiers
        except ClientError as e:
            logger.error(f"Error listing tiers: {e}")
            return []

    async def create_tier(self, tier: QuotaTier) -> QuotaTier:
        """Create a new quota tier"""
        item = {
            "PK": f"QUOTA_TIER#{tier.tier_id}",
            "SK": "METADATA",
            **tier.model_dump(by_alias=True, exclude_none=True)
        }

        try:
            self.table.put_item(Item=item)
            return tier
        except ClientError as e:
            logger.error(f"Error creating tier: {e}")
            raise

    async def update_tier(self, tier_id: str, updates: dict) -> Optional[QuotaTier]:
        """Update quota tier (partial update)"""
        try:
            # Build update expression
            update_parts = []
            expr_attr_names = {}
            expr_attr_values = {}

            for key, value in updates.items():
                update_parts.append(f"#{key} = :{key}")
                expr_attr_names[f"#{key}"] = key
                expr_attr_values[f":{key}"] = value

            # Add updatedAt timestamp
            now = datetime.utcnow().isoformat() + 'Z'
            update_parts.append("#updatedAt = :updatedAt")
            expr_attr_names["#updatedAt"] = "updatedAt"
            expr_attr_values[":updatedAt"] = now

            response = self.table.update_item(
                Key={
                    "PK": f"QUOTA_TIER#{tier_id}",
                    "SK": "METADATA"
                },
                UpdateExpression="SET " + ", ".join(update_parts),
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_values,
                ReturnValues="ALL_NEW"
            )

            item = response['Attributes']
            item.pop('PK', None)
            item.pop('SK', None)

            return QuotaTier(**item)
        except ClientError as e:
            logger.error(f"Error updating tier {tier_id}: {e}")
            return None

    async def delete_tier(self, tier_id: str) -> bool:
        """Delete quota tier"""
        try:
            self.table.delete_item(
                Key={
                    "PK": f"QUOTA_TIER#{tier_id}",
                    "SK": "METADATA"
                }
            )
            return True
        except ClientError as e:
            logger.error(f"Error deleting tier {tier_id}: {e}")
            return False

    # ========== Quota Assignments ==========

    async def get_assignment(self, assignment_id: str) -> Optional[QuotaAssignment]:
        """Get assignment by ID"""
        try:
            response = self.table.get_item(
                Key={
                    "PK": f"ASSIGNMENT#{assignment_id}",
                    "SK": "METADATA"
                }
            )

            if 'Item' not in response:
                return None

            item = response['Item']
            # Clean all GSI keys
            for key in ['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK', 'GSI3PK', 'GSI3SK']:
                item.pop(key, None)

            return QuotaAssignment(**item)
        except ClientError as e:
            logger.error(f"Error getting assignment {assignment_id}: {e}")
            return None

    async def query_user_assignment(self, user_id: str) -> Optional[QuotaAssignment]:
        """
        Query direct user assignment using GSI2 (UserAssignmentIndex).
        O(1) lookup - no scan.
        """
        try:
            response = self.table.query(
                IndexName="UserAssignmentIndex",
                KeyConditionExpression="GSI2PK = :pk",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_id}"
                },
                Limit=1
            )

            items = response.get('Items', [])
            if not items:
                return None

            item = items[0]
            # Clean GSI keys
            for key in ['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK', 'GSI3PK', 'GSI3SK']:
                item.pop(key, None)

            return QuotaAssignment(**item)
        except ClientError as e:
            logger.error(f"Error querying user assignment for {user_id}: {e}")
            return None

    async def query_role_assignments(self, role: str) -> List[QuotaAssignment]:
        """
        Query role-based assignments using GSI3 (RoleAssignmentIndex).
        Returns assignments sorted by priority (descending).
        O(log n) lookup - no scan.
        """
        try:
            response = self.table.query(
                IndexName="RoleAssignmentIndex",
                KeyConditionExpression="GSI3PK = :pk",
                ExpressionAttributeValues={
                    ":pk": f"ROLE#{role}"
                },
                ScanIndexForward=False  # Descending order (highest priority first)
            )

            assignments = []
            for item in response.get('Items', []):
                for key in ['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK', 'GSI3PK', 'GSI3SK']:
                    item.pop(key, None)
                assignments.append(QuotaAssignment(**item))

            return assignments
        except ClientError as e:
            logger.error(f"Error querying role assignments for {role}: {e}")
            return []

    async def list_assignments_by_type(
        self,
        assignment_type: str,
        enabled_only: bool = False
    ) -> List[QuotaAssignment]:
        """
        List assignments by type using GSI1 (AssignmentTypeIndex).
        Sorted by priority (descending). O(log n) - no scan.
        """
        try:
            response = self.table.query(
                IndexName="AssignmentTypeIndex",
                KeyConditionExpression="GSI1PK = :pk",
                ExpressionAttributeValues={
                    ":pk": f"ASSIGNMENT_TYPE#{assignment_type}"
                },
                ScanIndexForward=False  # Highest priority first
            )

            assignments = []
            for item in response.get('Items', []):
                for key in ['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK', 'GSI3PK', 'GSI3SK']:
                    item.pop(key, None)

                assignment = QuotaAssignment(**item)

                if enabled_only and not assignment.enabled:
                    continue

                assignments.append(assignment)

            return assignments
        except ClientError as e:
            logger.error(f"Error listing assignments for type {assignment_type}: {e}")
            return []

    async def list_all_assignments(self, enabled_only: bool = False) -> List[QuotaAssignment]:
        """List all assignments (for admin UI)"""
        try:
            # Query all assignment types
            all_assignments = []
            for assignment_type in QuotaAssignmentType:
                assignments = await self.list_assignments_by_type(
                    assignment_type.value,
                    enabled_only=enabled_only
                )
                all_assignments.extend(assignments)

            return all_assignments
        except Exception as e:
            logger.error(f"Error listing all assignments: {e}")
            return []

    async def create_assignment(self, assignment: QuotaAssignment) -> QuotaAssignment:
        """Create a new quota assignment with GSI keys"""
        # Build GSI keys based on assignment type
        gsi_keys = self._build_gsi_keys(assignment)

        item = {
            "PK": f"ASSIGNMENT#{assignment.assignment_id}",
            "SK": "METADATA",
            **gsi_keys,
            **assignment.model_dump(by_alias=True, exclude_none=True)
        }

        try:
            self.table.put_item(Item=item)
            return assignment
        except ClientError as e:
            logger.error(f"Error creating assignment: {e}")
            raise

    async def update_assignment(self, assignment_id: str, updates: dict) -> Optional[QuotaAssignment]:
        """Update quota assignment (partial update)"""
        try:
            # Get current assignment to rebuild GSI keys if needed
            current = await self.get_assignment(assignment_id)
            if not current:
                return None

            # Build update expression
            update_parts = []
            expr_attr_names = {}
            expr_attr_values = {}

            # Apply updates to current assignment
            for key, value in updates.items():
                setattr(current, key, value)

            # Rebuild GSI keys with updated values
            gsi_keys = self._build_gsi_keys(current)
            for key, value in gsi_keys.items():
                updates[key] = value

            # Build update expression
            for key, value in updates.items():
                update_parts.append(f"#{key} = :{key}")
                expr_attr_names[f"#{key}"] = key
                expr_attr_values[f":{key}"] = value

            # Add updatedAt timestamp
            now = datetime.utcnow().isoformat() + 'Z'
            update_parts.append("#updatedAt = :updatedAt")
            expr_attr_names["#updatedAt"] = "updatedAt"
            expr_attr_values[":updatedAt"] = now

            response = self.table.update_item(
                Key={
                    "PK": f"ASSIGNMENT#{assignment_id}",
                    "SK": "METADATA"
                },
                UpdateExpression="SET " + ", ".join(update_parts),
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_values,
                ReturnValues="ALL_NEW"
            )

            item = response['Attributes']
            for key in ['PK', 'SK', 'GSI1PK', 'GSI1SK', 'GSI2PK', 'GSI2SK', 'GSI3PK', 'GSI3SK']:
                item.pop(key, None)

            return QuotaAssignment(**item)
        except ClientError as e:
            logger.error(f"Error updating assignment {assignment_id}: {e}")
            return None

    async def delete_assignment(self, assignment_id: str) -> bool:
        """Delete quota assignment"""
        try:
            self.table.delete_item(
                Key={
                    "PK": f"ASSIGNMENT#{assignment_id}",
                    "SK": "METADATA"
                }
            )
            return True
        except ClientError as e:
            logger.error(f"Error deleting assignment {assignment_id}: {e}")
            return False

    def _build_gsi_keys(self, assignment: QuotaAssignment) -> dict:
        """Build GSI key attributes based on assignment type"""
        gsi_keys = {
            "GSI1PK": f"ASSIGNMENT_TYPE#{assignment.assignment_type.value}",
            "GSI1SK": f"PRIORITY#{assignment.priority}#{assignment.assignment_id}"
        }

        # GSI2: User-specific index
        if assignment.assignment_type == QuotaAssignmentType.DIRECT_USER and assignment.user_id:
            gsi_keys["GSI2PK"] = f"USER#{assignment.user_id}"
            gsi_keys["GSI2SK"] = f"ASSIGNMENT#{assignment.assignment_id}"

        # GSI3: Role-specific index
        if assignment.assignment_type == QuotaAssignmentType.JWT_ROLE and assignment.jwt_role:
            gsi_keys["GSI3PK"] = f"ROLE#{assignment.jwt_role}"
            gsi_keys["GSI3SK"] = f"PRIORITY#{assignment.priority}"

        return gsi_keys

    # ========== Quota Events ==========

    async def record_event(self, event: QuotaEvent) -> QuotaEvent:
        """Record a quota event (Phase 1: blocks only)"""
        item = {
            "PK": f"USER#{event.user_id}",
            "SK": f"EVENT#{event.timestamp}#{event.event_id}",
            "GSI5PK": f"TIER#{event.tier_id}",
            "GSI5SK": f"TIMESTAMP#{event.timestamp}",
            **event.model_dump(by_alias=True, exclude_none=True)
        }

        try:
            self.events_table.put_item(Item=item)
            return event
        except ClientError as e:
            logger.error(f"Error recording event: {e}")
            raise

    async def get_user_events(
        self,
        user_id: str,
        limit: int = 50,
        start_time: Optional[str] = None
    ) -> List[QuotaEvent]:
        """Get quota events for a user (targeted query by PK)"""
        try:
            key_condition = "PK = :pk"
            expr_values = {":pk": f"USER#{user_id}"}

            if start_time:
                key_condition += " AND SK >= :start"
                expr_values[":start"] = f"EVENT#{start_time}"

            response = self.events_table.query(
                KeyConditionExpression=key_condition,
                ExpressionAttributeValues=expr_values,
                ScanIndexForward=False,  # Latest first
                Limit=limit
            )

            events = []
            for item in response.get('Items', []):
                for key in ['PK', 'SK', 'GSI5PK', 'GSI5SK']:
                    item.pop(key, None)
                events.append(QuotaEvent(**item))

            return events
        except ClientError as e:
            logger.error(f"Error getting events for user {user_id}: {e}")
            return []

    async def get_tier_events(
        self,
        tier_id: str,
        limit: int = 100,
        start_time: Optional[str] = None
    ) -> List[QuotaEvent]:
        """Get quota events for a tier (Phase 2 analytics)"""
        try:
            key_condition = "GSI5PK = :pk"
            expr_values = {":pk": f"TIER#{tier_id}"}

            if start_time:
                key_condition += " AND GSI5SK >= :start"
                expr_values[":start"] = f"TIMESTAMP#{start_time}"

            response = self.events_table.query(
                IndexName="TierEventIndex",
                KeyConditionExpression=key_condition,
                ExpressionAttributeValues=expr_values,
                ScanIndexForward=False,  # Latest first
                Limit=limit
            )

            events = []
            for item in response.get('Items', []):
                for key in ['PK', 'SK', 'GSI5PK', 'GSI5SK']:
                    item.pop(key, None)
                events.append(QuotaEvent(**item))

            return events
        except ClientError as e:
            logger.error(f"Error getting events for tier {tier_id}: {e}")
            return []
