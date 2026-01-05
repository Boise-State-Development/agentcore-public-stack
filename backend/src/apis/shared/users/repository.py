"""DynamoDB repository for user management."""

from typing import Optional, List, Tuple
import boto3
from botocore.exceptions import ClientError
import logging
import os

from .models import UserProfile, UserListItem, UserStatus

logger = logging.getLogger(__name__)


class UserRepository:
    """DynamoDB repository for user operations.

    Table Schema:
        PK: USER#<user_id>
        SK: PROFILE

    GSIs:
        UserIdIndex: userId (for admin deep links)
        EmailIndex: email (for exact email lookup)
        EmailDomainIndex: GSI2PK=DOMAIN#<domain>, GSI2SK=lastLoginAt
        StatusLoginIndex: GSI3PK=STATUS#<status>, GSI3SK=lastLoginAt
    """

    def __init__(self, table_name: str = None):
        """Initialize repository with table name from env or parameter."""
        if table_name is None:
            table_name = os.getenv("DYNAMODB_USERS_TABLE_NAME", "")

        self._table_name = table_name
        self._enabled = bool(table_name)

        if self._enabled:
            self.dynamodb = boto3.resource('dynamodb')
            self.table = self.dynamodb.Table(table_name)
            logger.info(f"UserRepository initialized with table: {table_name}")
        else:
            self.dynamodb = None
            self.table = None
            logger.info("UserRepository disabled - no table configured")

    @property
    def enabled(self) -> bool:
        """Check if user repository is enabled."""
        return self._enabled

    # ========== Single User Operations ==========

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user by ID using primary key.
        Use this for internal operations where you have the user_id.
        """
        if not self._enabled:
            return None

        try:
            response = self.table.get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": "PROFILE"
                }
            )

            if 'Item' not in response:
                return None

            return self._item_to_profile(response['Item'])
        except ClientError as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None

    async def get_user_by_user_id(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user by userId attribute via UserIdIndex GSI.
        Use this for admin deep links where you only have the raw user ID.
        """
        if not self._enabled:
            return None

        try:
            response = self.table.query(
                IndexName="UserIdIndex",
                KeyConditionExpression="userId = :userId",
                ExpressionAttributeValues={
                    ":userId": user_id
                },
                Limit=1
            )

            items = response.get("Items", [])
            if not items:
                return None

            return self._item_to_profile(items[0])
        except ClientError as e:
            logger.error(f"Error getting user by userId {user_id}: {e}")
            return None

    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """Get user by email (case-insensitive lookup)."""
        if not self._enabled:
            return None

        try:
            response = self.table.query(
                IndexName="EmailIndex",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={
                    ":email": email.lower()
                },
                Limit=1
            )

            items = response.get("Items", [])
            if not items:
                return None

            return self._item_to_profile(items[0])
        except ClientError as e:
            logger.error(f"Error getting user by email {email}: {e}")
            return None

    async def create_user(self, profile: UserProfile) -> UserProfile:
        """Create a new user record."""
        if not self._enabled:
            raise RuntimeError("UserRepository is not enabled - no table configured")

        item = self._profile_to_item(profile)

        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)"
            )
            logger.info(f"Created new user: {profile.user_id} ({profile.email})")
            return profile
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"User {profile.user_id} already exists")
            logger.error(f"Error creating user: {e}")
            raise

    async def update_user(self, profile: UserProfile) -> UserProfile:
        """Update existing user record (full replace)."""
        if not self._enabled:
            raise RuntimeError("UserRepository is not enabled - no table configured")

        item = self._profile_to_item(profile)

        try:
            self.table.put_item(Item=item)
            logger.debug(f"Updated user: {profile.user_id}")
            return profile
        except ClientError as e:
            logger.error(f"Error updating user {profile.user_id}: {e}")
            raise

    async def upsert_user(self, profile: UserProfile) -> Tuple[UserProfile, bool]:
        """
        Create or update user.

        Returns:
            Tuple of (profile, is_new_user)
        """
        if not self._enabled:
            # Return the profile as-is if disabled, treating as "existing"
            return profile, False

        existing = await self.get_user(profile.user_id)

        if existing:
            # Preserve createdAt from existing record
            profile.created_at = existing.created_at
            await self.update_user(profile)
            return profile, False
        else:
            await self.create_user(profile)
            return profile, True

    # ========== List Operations ==========

    async def list_users_by_domain(
        self,
        domain: str,
        limit: int = 25,
        last_evaluated_key: Optional[dict] = None
    ) -> Tuple[List[UserListItem], Optional[dict]]:
        """
        List users by email domain, sorted by last login (descending).
        Uses EmailDomainIndex GSI.
        """
        if not self._enabled:
            return [], None

        try:
            kwargs = {
                "IndexName": "EmailDomainIndex",
                "KeyConditionExpression": "GSI2PK = :pk",
                "ExpressionAttributeValues": {
                    ":pk": f"DOMAIN#{domain.lower()}"
                },
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit
            }

            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = self.table.query(**kwargs)
            items = [self._item_to_list_item(item) for item in response.get("Items", [])]
            next_key = response.get("LastEvaluatedKey")

            return items, next_key
        except ClientError as e:
            logger.error(f"Error listing users by domain {domain}: {e}")
            return [], None

    async def list_users_by_status(
        self,
        status: str = "active",
        limit: int = 25,
        last_evaluated_key: Optional[dict] = None
    ) -> Tuple[List[UserListItem], Optional[dict]]:
        """
        List users by status, sorted by last login (descending).
        Uses StatusLoginIndex GSI.
        """
        if not self._enabled:
            return [], None

        try:
            kwargs = {
                "IndexName": "StatusLoginIndex",
                "KeyConditionExpression": "GSI3PK = :pk",
                "ExpressionAttributeValues": {
                    ":pk": f"STATUS#{status}"
                },
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit
            }

            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = self.table.query(**kwargs)
            items = [self._item_to_list_item(item) for item in response.get("Items", [])]
            next_key = response.get("LastEvaluatedKey")

            return items, next_key
        except ClientError as e:
            logger.error(f"Error listing users by status {status}: {e}")
            return [], None

    # ========== Helper Methods ==========

    def _profile_to_item(self, profile: UserProfile) -> dict:
        """Convert UserProfile to DynamoDB item with all keys."""
        status_value = profile.status.value if isinstance(profile.status, UserStatus) else profile.status

        item = {
            # Primary key
            "PK": f"USER#{profile.user_id}",
            "SK": "PROFILE",
            # Attributes
            "userId": profile.user_id,
            "email": profile.email.lower(),
            "name": profile.name,
            "roles": profile.roles,
            "emailDomain": profile.email_domain.lower(),
            "createdAt": profile.created_at,
            "lastLoginAt": profile.last_login_at,
            "status": status_value,
            # GSI keys for EmailDomainIndex
            "GSI2PK": f"DOMAIN#{profile.email_domain.lower()}",
            "GSI2SK": profile.last_login_at,
            # GSI keys for StatusLoginIndex
            "GSI3PK": f"STATUS#{status_value}",
            "GSI3SK": profile.last_login_at,
        }

        if profile.picture:
            item["picture"] = profile.picture

        return item

    def _item_to_profile(self, item: dict) -> UserProfile:
        """Convert DynamoDB item to UserProfile."""
        created_at = item.get("createdAt", "")
        return UserProfile(
            user_id=item["userId"],
            email=item["email"],
            name=item.get("name", ""),
            roles=item.get("roles", []),
            picture=item.get("picture"),
            email_domain=item.get("emailDomain", ""),
            created_at=created_at,
            last_login_at=item.get("lastLoginAt", created_at),
            status=item.get("status", "active")
        )

    def _item_to_list_item(self, item: dict) -> UserListItem:
        """Convert DynamoDB item to UserListItem."""
        # GSI queries may not project lastLoginAt, but GSI2SK/GSI3SK contain the same value
        last_login = (
            item.get("lastLoginAt")
            or item.get("GSI3SK")  # StatusLoginIndex sort key
            or item.get("GSI2SK")  # EmailDomainIndex sort key
            or item.get("createdAt", "")
        )
        return UserListItem(
            user_id=item["userId"],
            email=item["email"],
            name=item.get("name", ""),
            status=item.get("status", "active"),
            last_login_at=last_login,
            email_domain=item.get("emailDomain")
        )
