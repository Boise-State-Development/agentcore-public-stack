# User Admin System - Implementation Specification

**Version:** 1.0
**Created:** 2025-12-27
**Status:** Ready for Implementation

---

## Table of Contents

1. [Overview](#overview)
2. [Scope](#scope)
3. [DynamoDB Schema](#dynamodb-schema)
4. [Backend Implementation](#backend-implementation)
5. [Frontend Implementation](#frontend-implementation)
6. [User Sync Strategy](#user-sync-strategy)
7. [Testing Strategy](#testing-strategy)
8. [Deployment Plan](#deployment-plan)
9. [Validation Criteria](#validation-criteria)

---

## Overview

### Objectives

Provide admins with a centralized user lookup view to:
- Search and browse users
- View user profile information synced from JWT
- See user cost and quota status at a glance
- Access user-specific quota events and history
- Take admin actions (create overrides, assign tiers)

### Design Principles

1. **Scan-Free Queries** - All access patterns use GSIs, no table scans
2. **Just-in-Time Sync** - User records created/updated from JWT on login
3. **Eventual Consistency** - `lastLoginAt` updated on login, not per-request
4. **Composable Queries** - User detail aggregates data from multiple tables in parallel

---

## Scope

### Included

**User Management:**
- User record storage with JWT-synced data
- Search by email (exact match)
- Browse by email domain
- Browse by status + recent login
- User detail view with aggregated data

**User Detail View:**
- Profile info (email, name, roles, picture)
- Current month cost summary
- Quota status (resolved tier, usage, remaining)
- Recent quota events
- Admin actions (create override, assign tier)

**Admin Dashboard Widgets:**
- Recently active users
- Users approaching quota (80%+)
- Users by email domain

### Not Included (Future Consideration)

- Full-text search (name/email partial match)
- User suspension/account management
- Usage analytics and trends
- Session history browsing
- Data export (GDPR)

---

## DynamoDB Schema

### Users Table

```
Table: Users
Environment Variable: DYNAMODB_USERS_TABLE_NAME (default: "Users")
═══════════════════════════════════════════════════════════════

Primary Key:
  PK: USER#<user_id>
  SK: PROFILE

Attributes:
  userId: string              # From JWT "sub" claim
  email: string               # Lowercase, from JWT
  name: string                # From JWT "name" claim
  roles: string[]             # From JWT "roles" claim (stored as List)
  picture: string?            # From JWT "picture" claim (optional)
  emailDomain: string         # Extracted from email, lowercase
  createdAt: string           # ISO timestamp, first login
  lastLoginAt: string         # ISO timestamp, updated on each login
  status: string              # "active" | "inactive" | "suspended"

═══════════════════════════════════════════════════════════════
```

### Global Secondary Indexes

| GSI | PK | SK | Projection | Use Case |
|-----|----|----|------------|----------|
| **UserIdIndex** | `userId` | - | ALL | O(1) lookup by user ID (for deep links) |
| **EmailIndex** | `email` | - | ALL | O(1) exact email lookup |
| **EmailDomainIndex** | `DOMAIN#<emailDomain>` | `lastLoginAt` | KEYS_ONLY + userId, email, name, status | Browse users by company/domain |
| **StatusLoginIndex** | `STATUS#<status>` | `lastLoginAt` | KEYS_ONLY + userId, email, name, emailDomain | Browse active users by recency |

### Access Patterns

| Pattern | Query | GSI | Notes |
|---------|-------|-----|-------|
| Get user by ID (internal) | `PK = USER#<id>` | - | Primary key lookup (requires PK prefix) |
| Get user by ID (deep link) | `userId = <id>` | UserIdIndex | Direct ID lookup for admin deep links |
| Get user by email | `email = <email>` | EmailIndex | Case-insensitive (store lowercase) |
| List users by domain | `PK = DOMAIN#<domain>`, sorted by `lastLoginAt` | EmailDomainIndex | Paginated, most recent first |
| List active users | `PK = STATUS#active`, sorted by `lastLoginAt` | StatusLoginIndex | Paginated, most recent first |
| List inactive users | `PK = STATUS#inactive`, sorted by `lastLoginAt` | StatusLoginIndex | Users with old lastLoginAt |

### Deep Link Support

The `UserIdIndex` enables admin deep links to user detail pages:

```
/admin/users/:userId
```

This is used by:
- **TopUsersTableComponent** - Click on a row to navigate to user detail
- **Cost Dashboard** - Click on user in cost breakdown
- **Quota Events** - Click on user ID to view user detail
- **External links** - Share user detail URL with other admins

#### Integration with Existing Components

**TopUsersTableComponent** (`frontend/ai.client/src/app/admin/costs/components/top-users-table.component.ts`)

Already emits `userClick` event with `userId`. Update the parent component's handler:

```typescript
// In admin-costs.page.ts
onUserClick(userId: string): void {
  this.router.navigate(['/admin/users', userId]);
}
```

**Quota Event Viewer** - Add user ID links in the event list to navigate to user detail.

### Capacity Planning (30K Users)

**Read Capacity:**
- User lookup: 1 RCU per request
- List queries: ~10 RCU per page (25 items)
- Expected: 100-500 RCU sustained

**Write Capacity:**
- User sync on login: 1 WCU per login
- 30K users × 2 logins/day = 60K writes/day = ~1 WCU sustained
- Peak: 10-50 WCU (morning login surge)

**Recommendation:** On-demand capacity mode

---

## Backend Implementation

### Directory Structure

```
backend/src/
├── apis/
│   └── app_api/
│       └── admin/
│           └── users/
│               ├── __init__.py
│               ├── routes.py       # API endpoints
│               ├── service.py      # Business logic
│               └── models.py       # Request/response models
└── users/
    ├── __init__.py
    ├── models.py                   # Domain models
    ├── repository.py               # DynamoDB operations
    └── sync.py                     # JWT sync logic
```

### Domain Models

**File:** `backend/src/users/models.py`

```python
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import datetime

class UserProfile(BaseModel):
    """User profile stored in DynamoDB"""
    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    roles: List[str] = Field(default_factory=list)
    picture: Optional[str] = None
    email_domain: str = Field(..., alias="emailDomain")
    created_at: str = Field(..., alias="createdAt")
    last_login_at: str = Field(..., alias="lastLoginAt")
    status: str = Field(default="active")

    @field_validator('email', mode='before')
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower() if v else v

    @field_validator('email_domain', mode='before')
    @classmethod
    def lowercase_domain(cls, v: str) -> str:
        return v.lower() if v else v

    class Config:
        populate_by_name = True


class UserListItem(BaseModel):
    """Minimal user info for list views"""
    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    status: str
    last_login_at: str = Field(..., alias="lastLoginAt")
    email_domain: Optional[str] = Field(None, alias="emailDomain")


class UserDetailView(BaseModel):
    """Comprehensive user view for admin detail page"""
    profile: UserProfile

    # Cost summary (from UserCostSummary table)
    current_month_cost: float = Field(0.0, alias="currentMonthCost")
    current_month_requests: int = Field(0, alias="currentMonthRequests")

    # Quota status (from quota resolver)
    quota_tier_name: Optional[str] = Field(None, alias="quotaTierName")
    quota_matched_by: Optional[str] = Field(None, alias="quotaMatchedBy")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    quota_usage_percentage: float = Field(0.0, alias="quotaUsagePercentage")
    quota_remaining: Optional[float] = Field(None, alias="quotaRemaining")
    has_active_override: bool = Field(False, alias="hasActiveOverride")

    # Recent events (from QuotaEvents)
    recent_events: List[dict] = Field(default_factory=list, alias="recentEvents")

    class Config:
        populate_by_name = True
```

### Repository

**File:** `backend/src/users/repository.py`

```python
import logging
from typing import Optional, List, Tuple
from datetime import datetime
from botocore.exceptions import ClientError

from .models import UserProfile, UserListItem

logger = logging.getLogger(__name__)


class UserRepository:
    """DynamoDB repository for user operations"""

    def __init__(self, dynamodb_client, table_name: str):
        self._client = dynamodb_client
        self._table_name = table_name

    # ========== Single User Operations ==========

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user by ID using primary key.
        Use this for internal operations where you have the full PK.
        """
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={
                    "PK": {"S": f"USER#{user_id}"},
                    "SK": {"S": "PROFILE"}
                }
            )
            item = response.get("Item")
            if not item:
                return None
            return self._item_to_profile(item)
        except ClientError as e:
            logger.error(f"Error getting user {user_id}: {e}")
            raise

    async def get_user_by_user_id(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user by userId attribute via UserIdIndex GSI.
        Use this for admin deep links where you only have the raw user ID.
        """
        try:
            response = self._client.query(
                TableName=self._table_name,
                IndexName="UserIdIndex",
                KeyConditionExpression="userId = :userId",
                ExpressionAttributeValues={
                    ":userId": {"S": user_id}
                },
                Limit=1
            )
            items = response.get("Items", [])
            if not items:
                return None
            return self._item_to_profile(items[0])
        except ClientError as e:
            logger.error(f"Error getting user by userId {user_id}: {e}")
            raise

    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """Get user by email (case-insensitive)"""
        try:
            response = self._client.query(
                TableName=self._table_name,
                IndexName="EmailIndex",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={
                    ":email": {"S": email.lower()}
                },
                Limit=1
            )
            items = response.get("Items", [])
            if not items:
                return None
            return self._item_to_profile(items[0])
        except ClientError as e:
            logger.error(f"Error getting user by email {email}: {e}")
            raise

    async def create_user(self, profile: UserProfile) -> UserProfile:
        """Create a new user"""
        item = self._profile_to_item(profile)
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)"
            )
            return profile
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"User {profile.user_id} already exists")
            logger.error(f"Error creating user: {e}")
            raise

    async def update_user(self, user_id: str, profile: UserProfile) -> UserProfile:
        """Update existing user"""
        item = self._profile_to_item(profile)
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item
            )
            return profile
        except ClientError as e:
            logger.error(f"Error updating user {user_id}: {e}")
            raise

    async def upsert_user(self, profile: UserProfile) -> Tuple[UserProfile, bool]:
        """
        Create or update user.
        Returns (profile, is_new_user)
        """
        existing = await self.get_user(profile.user_id)
        if existing:
            # Preserve createdAt from existing record
            profile.created_at = existing.created_at
            await self.update_user(profile.user_id, profile)
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
        """List users by email domain, sorted by last login (descending)"""
        try:
            kwargs = {
                "TableName": self._table_name,
                "IndexName": "EmailDomainIndex",
                "KeyConditionExpression": "GSI2PK = :pk",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"DOMAIN#{domain.lower()}"}
                },
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = self._client.query(**kwargs)
            items = [self._item_to_list_item(item) for item in response.get("Items", [])]
            next_key = response.get("LastEvaluatedKey")
            return items, next_key
        except ClientError as e:
            logger.error(f"Error listing users by domain {domain}: {e}")
            raise

    async def list_users_by_status(
        self,
        status: str = "active",
        limit: int = 25,
        last_evaluated_key: Optional[dict] = None
    ) -> Tuple[List[UserListItem], Optional[dict]]:
        """List users by status, sorted by last login (descending)"""
        try:
            kwargs = {
                "TableName": self._table_name,
                "IndexName": "StatusLoginIndex",
                "KeyConditionExpression": "GSI3PK = :pk",
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"STATUS#{status}"}
                },
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = self._client.query(**kwargs)
            items = [self._item_to_list_item(item) for item in response.get("Items", [])]
            next_key = response.get("LastEvaluatedKey")
            return items, next_key
        except ClientError as e:
            logger.error(f"Error listing users by status {status}: {e}")
            raise

    # ========== Helpers ==========

    def _profile_to_item(self, profile: UserProfile) -> dict:
        """Convert UserProfile to DynamoDB item"""
        item = {
            "PK": {"S": f"USER#{profile.user_id}"},
            "SK": {"S": "PROFILE"},
            "userId": {"S": profile.user_id},
            "email": {"S": profile.email.lower()},
            "name": {"S": profile.name},
            "roles": {"L": [{"S": r} for r in profile.roles]},
            "emailDomain": {"S": profile.email_domain.lower()},
            "createdAt": {"S": profile.created_at},
            "lastLoginAt": {"S": profile.last_login_at},
            "status": {"S": profile.status},
            # GSI keys
            "GSI2PK": {"S": f"DOMAIN#{profile.email_domain.lower()}"},
            "GSI2SK": {"S": profile.last_login_at},
            "GSI3PK": {"S": f"STATUS#{profile.status}"},
            "GSI3SK": {"S": profile.last_login_at},
        }
        if profile.picture:
            item["picture"] = {"S": profile.picture}
        return item

    def _item_to_profile(self, item: dict) -> UserProfile:
        """Convert DynamoDB item to UserProfile"""
        return UserProfile(
            user_id=item["userId"]["S"],
            email=item["email"]["S"],
            name=item["name"]["S"],
            roles=[r["S"] for r in item.get("roles", {}).get("L", [])],
            picture=item.get("picture", {}).get("S"),
            email_domain=item["emailDomain"]["S"],
            created_at=item["createdAt"]["S"],
            last_login_at=item["lastLoginAt"]["S"],
            status=item.get("status", {}).get("S", "active")
        )

    def _item_to_list_item(self, item: dict) -> UserListItem:
        """Convert DynamoDB item to UserListItem"""
        return UserListItem(
            user_id=item["userId"]["S"],
            email=item["email"]["S"],
            name=item["name"]["S"],
            status=item.get("status", {}).get("S", "active"),
            last_login_at=item["lastLoginAt"]["S"],
            email_domain=item.get("emailDomain", {}).get("S")
        )
```

### User Sync Service

**File:** `backend/src/users/sync.py`

```python
import logging
from datetime import datetime
from typing import Tuple

from .models import UserProfile
from .repository import UserRepository

logger = logging.getLogger(__name__)


class UserSyncService:
    """
    Syncs user data from JWT claims to DynamoDB.
    Called on each login/token refresh.
    """

    def __init__(self, repository: UserRepository):
        self._repository = repository

    async def sync_from_jwt(self, jwt_claims: dict) -> Tuple[UserProfile, bool]:
        """
        Create or update user from JWT claims.

        Args:
            jwt_claims: Decoded JWT payload containing user info

        Returns:
            Tuple of (UserProfile, is_new_user)
        """
        user_id = jwt_claims.get("sub")
        if not user_id:
            raise ValueError("JWT missing 'sub' claim")

        email = jwt_claims.get("email", "")
        if not email:
            raise ValueError("JWT missing 'email' claim")

        # Extract domain from email
        email_domain = email.split("@")[1] if "@" in email else ""

        now = datetime.utcnow().isoformat() + "Z"

        # Build profile from JWT claims
        profile = UserProfile(
            user_id=user_id,
            email=email.lower(),
            name=jwt_claims.get("name", ""),
            roles=jwt_claims.get("roles", []),
            picture=jwt_claims.get("picture"),
            email_domain=email_domain.lower(),
            created_at=now,  # Will be overwritten if user exists
            last_login_at=now,
            status="active"
        )

        # Upsert user
        profile, is_new = await self._repository.upsert_user(profile)

        if is_new:
            logger.info(f"Created new user: {user_id} ({email})")
        else:
            logger.debug(f"Updated user: {user_id} ({email})")

        return profile, is_new
```

### Admin API Routes

**File:** `backend/src/apis/app_api/admin/users/routes.py`

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional

from apis.shared.auth.dependencies import require_admin
from apis.shared.auth.models import User

from .service import UserAdminService
from .models import (
    UserListResponse,
    UserDetailResponse,
    UserSearchQuery
)

router = APIRouter(prefix="/users", tags=["Admin - Users"])


def get_user_service() -> UserAdminService:
    """Dependency to get UserAdminService instance"""
    # Implementation depends on your DI setup
    from apis.shared.dependencies import get_user_admin_service
    return get_user_admin_service()


@router.get("", response_model=UserListResponse)
async def list_users(
    status: str = Query("active", description="Filter by status"),
    domain: Optional[str] = Query(None, description="Filter by email domain"),
    limit: int = Query(25, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_service)
):
    """
    List users with optional filters.

    - **status**: Filter by user status (active, inactive, suspended)
    - **domain**: Filter by email domain (e.g., "example.com")
    - **limit**: Number of results per page (1-100)
    - **cursor**: Pagination cursor from previous response
    """
    return await service.list_users(
        status=status,
        domain=domain,
        limit=limit,
        cursor=cursor
    )


@router.get("/search", response_model=UserListResponse)
async def search_users(
    email: str = Query(..., description="Email to search (exact match)"),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_service)
):
    """
    Search for a user by exact email match.
    """
    user = await service.search_by_email(email)
    if not user:
        return UserListResponse(users=[], next_cursor=None)
    return UserListResponse(users=[user], next_cursor=None)


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_service)
):
    """
    Get comprehensive user detail including:
    - Profile information
    - Current month cost summary
    - Quota status
    - Recent quota events
    """
    detail = await service.get_user_detail(user_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return detail


@router.get("/domains/list", response_model=List[str])
async def list_email_domains(
    limit: int = Query(50, ge=1, le=200),
    admin_user: User = Depends(require_admin),
    service: UserAdminService = Depends(get_user_service)
):
    """
    List distinct email domains with user counts.
    Useful for domain filter dropdown.
    """
    return await service.list_domains(limit=limit)
```

### Admin API Models

**File:** `backend/src/apis/app_api/admin/users/models.py`

```python
from pydantic import BaseModel, Field
from typing import List, Optional


class UserListItem(BaseModel):
    """User item for list views"""
    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    status: str
    last_login_at: str = Field(..., alias="lastLoginAt")
    email_domain: Optional[str] = Field(None, alias="emailDomain")

    # Quick stats (optional, populated for dashboard views)
    current_month_cost: Optional[float] = Field(None, alias="currentMonthCost")
    quota_usage_percentage: Optional[float] = Field(None, alias="quotaUsagePercentage")

    class Config:
        populate_by_name = True


class UserListResponse(BaseModel):
    """Paginated user list response"""
    users: List[UserListItem]
    next_cursor: Optional[str] = Field(None, alias="nextCursor")
    total_count: Optional[int] = Field(None, alias="totalCount")

    class Config:
        populate_by_name = True


class QuotaStatus(BaseModel):
    """User's current quota status"""
    tier_id: Optional[str] = Field(None, alias="tierId")
    tier_name: Optional[str] = Field(None, alias="tierName")
    matched_by: Optional[str] = Field(None, alias="matchedBy")
    monthly_limit: Optional[float] = Field(None, alias="monthlyLimit")
    current_usage: float = Field(0.0, alias="currentUsage")
    usage_percentage: float = Field(0.0, alias="usagePercentage")
    remaining: Optional[float] = None
    has_active_override: bool = Field(False, alias="hasActiveOverride")
    override_reason: Optional[str] = Field(None, alias="overrideReason")

    class Config:
        populate_by_name = True


class CostSummary(BaseModel):
    """User's current month cost summary"""
    total_cost: float = Field(0.0, alias="totalCost")
    total_requests: int = Field(0, alias="totalRequests")
    total_input_tokens: int = Field(0, alias="totalInputTokens")
    total_output_tokens: int = Field(0, alias="totalOutputTokens")
    cache_savings: float = Field(0.0, alias="cacheSavings")
    primary_model: Optional[str] = Field(None, alias="primaryModel")

    class Config:
        populate_by_name = True


class QuotaEventSummary(BaseModel):
    """Summary of a quota event"""
    event_id: str = Field(..., alias="eventId")
    event_type: str = Field(..., alias="eventType")
    timestamp: str
    percentage_used: float = Field(..., alias="percentageUsed")

    class Config:
        populate_by_name = True


class UserProfile(BaseModel):
    """Full user profile"""
    user_id: str = Field(..., alias="userId")
    email: str
    name: str
    roles: List[str] = Field(default_factory=list)
    picture: Optional[str] = None
    email_domain: str = Field(..., alias="emailDomain")
    created_at: str = Field(..., alias="createdAt")
    last_login_at: str = Field(..., alias="lastLoginAt")
    status: str

    class Config:
        populate_by_name = True


class UserDetailResponse(BaseModel):
    """Comprehensive user detail for admin view"""
    profile: UserProfile
    cost_summary: CostSummary = Field(..., alias="costSummary")
    quota_status: QuotaStatus = Field(..., alias="quotaStatus")
    recent_events: List[QuotaEventSummary] = Field(
        default_factory=list,
        alias="recentEvents"
    )

    class Config:
        populate_by_name = True
```

### Admin Service

**File:** `backend/src/apis/app_api/admin/users/service.py`

```python
import asyncio
import logging
import base64
import json
from typing import Optional, List
from datetime import datetime

from users.repository import UserRepository
from users.models import UserProfile, UserListItem
from apis.app_api.costs.aggregator import CostAggregator
from agents.strands_agent.quota.resolver import QuotaResolver
from agents.strands_agent.quota.repository import QuotaRepository
from apis.shared.auth.models import User

from .models import (
    UserListResponse,
    UserDetailResponse,
    QuotaStatus,
    CostSummary,
    QuotaEventSummary
)

logger = logging.getLogger(__name__)


class UserAdminService:
    """Service for user admin operations"""

    def __init__(
        self,
        user_repository: UserRepository,
        cost_aggregator: CostAggregator,
        quota_resolver: QuotaResolver,
        quota_repository: QuotaRepository
    ):
        self._user_repo = user_repository
        self._cost_aggregator = cost_aggregator
        self._quota_resolver = quota_resolver
        self._quota_repo = quota_repository

    async def list_users(
        self,
        status: str = "active",
        domain: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None
    ) -> UserListResponse:
        """List users with filters and pagination"""

        # Decode cursor if provided
        last_key = None
        if cursor:
            try:
                last_key = json.loads(base64.b64decode(cursor).decode())
            except Exception:
                pass

        # Query based on filters
        if domain:
            users, next_key = await self._user_repo.list_users_by_domain(
                domain=domain,
                limit=limit,
                last_evaluated_key=last_key
            )
        else:
            users, next_key = await self._user_repo.list_users_by_status(
                status=status,
                limit=limit,
                last_evaluated_key=last_key
            )

        # Encode next cursor
        next_cursor = None
        if next_key:
            next_cursor = base64.b64encode(json.dumps(next_key).encode()).decode()

        return UserListResponse(
            users=users,
            next_cursor=next_cursor
        )

    async def search_by_email(self, email: str) -> Optional[UserListItem]:
        """Search for user by exact email"""
        profile = await self._user_repo.get_user_by_email(email)
        if not profile:
            return None

        return UserListItem(
            user_id=profile.user_id,
            email=profile.email,
            name=profile.name,
            status=profile.status,
            last_login_at=profile.last_login_at,
            email_domain=profile.email_domain
        )

    async def get_user_detail(self, user_id: str) -> Optional[UserDetailResponse]:
        """
        Get comprehensive user detail.
        Uses UserIdIndex GSI to support admin deep links by raw user ID.
        """

        # Get user profile using UserIdIndex (for deep link support)
        profile = await self._user_repo.get_user_by_user_id(user_id)
        if not profile:
            return None

        # Parallel fetch of related data
        current_period = datetime.utcnow().strftime("%Y-%m")

        # Create a mock User object for quota resolution
        user = User(
            user_id=profile.user_id,
            email=profile.email,
            name=profile.name,
            roles=profile.roles
        )

        cost_summary_task = self._cost_aggregator.get_user_cost_summary(
            user_id=user_id,
            period=current_period
        )
        quota_task = self._quota_resolver.resolve_user_quota(user)
        events_task = self._quota_repo.list_user_events(
            user_id=user_id,
            limit=5
        )

        # Await all in parallel
        cost_data, resolved_quota, recent_events = await asyncio.gather(
            cost_summary_task,
            quota_task,
            events_task,
            return_exceptions=True
        )

        # Build cost summary
        cost_summary = CostSummary(total_cost=0.0, total_requests=0)
        if cost_data and not isinstance(cost_data, Exception):
            cost_summary = CostSummary(
                total_cost=cost_data.total_cost,
                total_requests=cost_data.total_requests,
                total_input_tokens=cost_data.total_input_tokens,
                total_output_tokens=cost_data.total_output_tokens,
                cache_savings=cost_data.total_cache_savings,
                primary_model=self._get_primary_model(cost_data)
            )

        # Build quota status
        quota_status = QuotaStatus()
        if resolved_quota and not isinstance(resolved_quota, Exception):
            tier = resolved_quota.tier
            usage_pct = 0.0
            remaining = None

            if tier and tier.monthly_cost_limit and tier.monthly_cost_limit != float('inf'):
                usage_pct = (cost_summary.total_cost / tier.monthly_cost_limit) * 100
                remaining = max(0, tier.monthly_cost_limit - cost_summary.total_cost)

            quota_status = QuotaStatus(
                tier_id=tier.tier_id if tier else None,
                tier_name=tier.tier_name if tier else None,
                matched_by=resolved_quota.matched_by,
                monthly_limit=tier.monthly_cost_limit if tier else None,
                current_usage=cost_summary.total_cost,
                usage_percentage=round(usage_pct, 1),
                remaining=remaining,
                has_active_override=resolved_quota.override is not None,
                override_reason=resolved_quota.override.reason if resolved_quota.override else None
            )

        # Build event summaries
        event_summaries = []
        if recent_events and not isinstance(recent_events, Exception):
            for event in recent_events:
                event_summaries.append(QuotaEventSummary(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    timestamp=event.timestamp,
                    percentage_used=event.percentage_used
                ))

        return UserDetailResponse(
            profile=profile,
            cost_summary=cost_summary,
            quota_status=quota_status,
            recent_events=event_summaries
        )

    async def list_domains(self, limit: int = 50) -> List[str]:
        """
        List distinct email domains.
        Note: This requires a scan or maintaining a separate domain list.
        For now, return empty - implement if needed.
        """
        # TODO: Implement domain listing
        # Options:
        # 1. Maintain a separate DOMAINS item updated on user create
        # 2. Scan with projection (not recommended at scale)
        # 3. Use application-level aggregation
        return []

    def _get_primary_model(self, cost_data) -> Optional[str]:
        """Get the most-used model from cost data"""
        if not cost_data or not cost_data.models:
            return None

        # Find model with most requests
        primary = max(cost_data.models, key=lambda m: m.request_count)
        return primary.model_name if primary else None
```

---

## Frontend Implementation

### Directory Structure

```
frontend/ai.client/src/app/admin/
├── users/
│   ├── models/
│   │   └── user.models.ts
│   ├── services/
│   │   ├── user-http.service.ts
│   │   └── user-state.service.ts
│   └── pages/
│       ├── user-list/
│       │   └── user-list.page.ts
│       └── user-detail/
│           └── user-detail.page.ts
└── admin.page.ts                    # Add user lookup card
```

### TypeScript Models

**File:** `frontend/ai.client/src/app/admin/users/models/user.models.ts`

```typescript
export interface UserListItem {
  userId: string;
  email: string;
  name: string;
  status: 'active' | 'inactive' | 'suspended';
  lastLoginAt: string;
  emailDomain?: string;
  currentMonthCost?: number;
  quotaUsagePercentage?: number;
}

export interface UserListResponse {
  users: UserListItem[];
  nextCursor?: string;
  totalCount?: number;
}

export interface QuotaStatus {
  tierId?: string;
  tierName?: string;
  matchedBy?: string;
  monthlyLimit?: number;
  currentUsage: number;
  usagePercentage: number;
  remaining?: number;
  hasActiveOverride: boolean;
  overrideReason?: string;
}

export interface CostSummary {
  totalCost: number;
  totalRequests: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  cacheSavings: number;
  primaryModel?: string;
}

export interface QuotaEventSummary {
  eventId: string;
  eventType: 'warning' | 'block' | 'reset' | 'override_applied';
  timestamp: string;
  percentageUsed: number;
}

export interface UserProfile {
  userId: string;
  email: string;
  name: string;
  roles: string[];
  picture?: string;
  emailDomain: string;
  createdAt: string;
  lastLoginAt: string;
  status: 'active' | 'inactive' | 'suspended';
}

export interface UserDetailResponse {
  profile: UserProfile;
  costSummary: CostSummary;
  quotaStatus: QuotaStatus;
  recentEvents: QuotaEventSummary[];
}
```

### HTTP Service

**File:** `frontend/ai.client/src/app/admin/users/services/user-http.service.ts`

```typescript
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { UserListResponse, UserDetailResponse } from '../models/user.models';

@Injectable({
  providedIn: 'root',
})
export class UserHttpService {
  private http = inject(HttpClient);
  private baseUrl = `${environment.apiUrl}/api/admin/users`;

  listUsers(
    status: string = 'active',
    domain?: string,
    limit: number = 25,
    cursor?: string
  ): Observable<UserListResponse> {
    let params = new HttpParams()
      .set('status', status)
      .set('limit', limit.toString());

    if (domain) {
      params = params.set('domain', domain);
    }
    if (cursor) {
      params = params.set('cursor', cursor);
    }

    return this.http.get<UserListResponse>(this.baseUrl, { params });
  }

  searchByEmail(email: string): Observable<UserListResponse> {
    const params = new HttpParams().set('email', email);
    return this.http.get<UserListResponse>(`${this.baseUrl}/search`, { params });
  }

  getUserDetail(userId: string): Observable<UserDetailResponse> {
    return this.http.get<UserDetailResponse>(`${this.baseUrl}/${userId}`);
  }

  listDomains(limit: number = 50): Observable<string[]> {
    const params = new HttpParams().set('limit', limit.toString());
    return this.http.get<string[]>(`${this.baseUrl}/domains/list`, { params });
  }
}
```

### State Service

**File:** `frontend/ai.client/src/app/admin/users/services/user-state.service.ts`

```typescript
import { Injectable, inject, signal, computed } from '@angular/core';
import { UserHttpService } from './user-http.service';
import {
  UserListItem,
  UserDetailResponse,
} from '../models/user.models';

@Injectable({
  providedIn: 'root',
})
export class UserStateService {
  private http = inject(UserHttpService);

  // State
  users = signal<UserListItem[]>([]);
  selectedUser = signal<UserDetailResponse | null>(null);
  loading = signal(false);
  searchQuery = signal('');
  statusFilter = signal<'active' | 'inactive' | 'suspended'>('active');
  domainFilter = signal<string | null>(null);
  nextCursor = signal<string | null>(null);

  // Computed
  hasMore = computed(() => this.nextCursor() !== null);
  userCount = computed(() => this.users().length);

  loadUsers(reset: boolean = false): void {
    if (reset) {
      this.users.set([]);
      this.nextCursor.set(null);
    }

    this.loading.set(true);

    this.http
      .listUsers(
        this.statusFilter(),
        this.domainFilter() ?? undefined,
        25,
        reset ? undefined : this.nextCursor() ?? undefined
      )
      .subscribe({
        next: (response) => {
          if (reset) {
            this.users.set(response.users);
          } else {
            this.users.update((current) => [...current, ...response.users]);
          }
          this.nextCursor.set(response.nextCursor ?? null);
          this.loading.set(false);
        },
        error: () => this.loading.set(false),
      });
  }

  searchByEmail(email: string): void {
    this.loading.set(true);
    this.searchQuery.set(email);

    this.http.searchByEmail(email).subscribe({
      next: (response) => {
        this.users.set(response.users);
        this.nextCursor.set(null);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  loadUserDetail(userId: string): void {
    this.loading.set(true);
    this.selectedUser.set(null);

    this.http.getUserDetail(userId).subscribe({
      next: (detail) => {
        this.selectedUser.set(detail);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  clearSelection(): void {
    this.selectedUser.set(null);
  }

  setStatusFilter(status: 'active' | 'inactive' | 'suspended'): void {
    this.statusFilter.set(status);
    this.loadUsers(true);
  }

  setDomainFilter(domain: string | null): void {
    this.domainFilter.set(domain);
    this.loadUsers(true);
  }
}
```

### User List Page

**File:** `frontend/ai.client/src/app/admin/users/pages/user-list/user-list.page.ts`

```typescript
import {
  Component,
  ChangeDetectionStrategy,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroMagnifyingGlass,
  heroUser,
  heroChevronRight,
} from '@ng-icons/heroicons/outline';
import { UserStateService } from '../../services/user-state.service';
import { UserListItem } from '../../models/user.models';

@Component({
  selector: 'app-user-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({ heroMagnifyingGlass, heroUser, heroChevronRight }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="mb-6">
      <h1 class="text-2xl/9 font-bold mb-2">User Lookup</h1>
      <p class="text-gray-600 dark:text-gray-400">
        Search and browse users to view their profile, costs, and quota status.
      </p>
    </div>

    <!-- Search Bar -->
    <div class="mb-6">
      <div class="relative">
        <ng-icon
          name="heroMagnifyingGlass"
          class="absolute left-3 top-1/2 -translate-y-1/2 size-5 text-gray-400"
        />
        <input
          type="email"
          [(ngModel)]="searchEmail"
          (keyup.enter)="search()"
          placeholder="Search by email address..."
          class="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
        />
      </div>
    </div>

    <!-- Filters -->
    <div class="flex gap-4 mb-6">
      <select
        [ngModel]="state.statusFilter()"
        (ngModelChange)="state.setStatusFilter($event)"
        class="px-3 py-2 border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-600"
      >
        <option value="active">Active Users</option>
        <option value="inactive">Inactive Users</option>
        <option value="suspended">Suspended Users</option>
      </select>
    </div>

    <!-- Loading State -->
    @if (state.loading() && state.users().length === 0) {
      <div class="text-center py-8">Loading users...</div>
    }

    <!-- User List -->
    <div class="space-y-2">
      @for (user of state.users(); track user.userId) {
        <div
          (click)="viewUser(user)"
          class="flex items-center gap-4 p-4 border border-gray-200 rounded-sm cursor-pointer hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800"
        >
          <!-- Avatar -->
          <div
            class="flex items-center justify-center size-10 rounded-full bg-gray-200 dark:bg-gray-700"
          >
            <ng-icon name="heroUser" class="size-5 text-gray-500" />
          </div>

          <!-- User Info -->
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-medium truncate">{{ user.email }}</span>
              @if (user.status !== 'active') {
                <span
                  class="px-2 py-0.5 text-xs rounded-xs"
                  [class]="
                    user.status === 'suspended'
                      ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
                      : 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200'
                  "
                >
                  {{ user.status }}
                </span>
              }
            </div>
            <div class="text-sm/6 text-gray-500 dark:text-gray-400">
              {{ user.name || 'No name' }} &middot; Last login:
              {{ formatDate(user.lastLoginAt) }}
            </div>
          </div>

          <!-- Quick Stats (if available) -->
          @if (user.quotaUsagePercentage !== undefined) {
            <div class="text-right">
              <div class="text-sm/6 font-medium">
                {{ user.quotaUsagePercentage }}% quota used
              </div>
              @if (user.currentMonthCost !== undefined) {
                <div class="text-sm/6 text-gray-500">
                  \${{ user.currentMonthCost.toFixed(2) }} this month
                </div>
              }
            </div>
          }

          <ng-icon name="heroChevronRight" class="size-5 text-gray-400" />
        </div>
      }
    </div>

    <!-- Empty State -->
    @if (state.users().length === 0 && !state.loading()) {
      <div class="text-center py-12 text-gray-500">
        <ng-icon name="heroUser" class="size-12 mx-auto mb-4 opacity-50" />
        <p class="text-lg/7">No users found</p>
        <p class="text-sm/6">Try adjusting your search or filters</p>
      </div>
    }

    <!-- Load More -->
    @if (state.hasMore()) {
      <div class="mt-6 text-center">
        <button
          (click)="loadMore()"
          [disabled]="state.loading()"
          class="px-4 py-2 text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          @if (state.loading()) {
            Loading...
          } @else {
            Load More
          }
        </button>
      </div>
    }
  `,
})
export class UserListPage implements OnInit {
  state = inject(UserStateService);
  private router = inject(Router);

  searchEmail = '';

  ngOnInit(): void {
    this.state.loadUsers(true);
  }

  search(): void {
    if (this.searchEmail.trim()) {
      this.state.searchByEmail(this.searchEmail.trim());
    } else {
      this.state.loadUsers(true);
    }
  }

  viewUser(user: UserListItem): void {
    this.router.navigate(['/admin/users', user.userId]);
  }

  loadMore(): void {
    this.state.loadUsers(false);
  }

  formatDate(isoString: string): string {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return 'Today';
    } else if (diffDays === 1) {
      return 'Yesterday';
    } else if (diffDays < 7) {
      return `${diffDays} days ago`;
    } else {
      return date.toLocaleDateString();
    }
  }
}
```

### User Detail Page

**File:** `frontend/ai.client/src/app/admin/users/pages/user-detail/user-detail.page.ts`

```typescript
import {
  Component,
  ChangeDetectionStrategy,
  inject,
  OnInit,
  computed,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroUser,
  heroCurrencyDollar,
  heroChartBar,
  heroShieldCheck,
  heroExclamationTriangle,
  heroClock,
} from '@ng-icons/heroicons/outline';
import { UserStateService } from '../../services/user-state.service';

@Component({
  selector: 'app-user-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroUser,
      heroCurrencyDollar,
      heroChartBar,
      heroShieldCheck,
      heroExclamationTriangle,
      heroClock,
    }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <!-- Back Button -->
    <button
      (click)="goBack()"
      class="flex items-center gap-2 text-gray-600 hover:text-gray-900 mb-6 dark:text-gray-400 dark:hover:text-white"
    >
      <ng-icon name="heroArrowLeft" class="size-5" />
      <span>Back to Users</span>
    </button>

    @if (state.loading()) {
      <div class="text-center py-8">Loading user details...</div>
    }

    @if (user(); as detail) {
      <!-- Profile Header -->
      <div
        class="flex items-start gap-6 p-6 bg-white border border-gray-200 rounded-sm mb-6 dark:bg-gray-800 dark:border-gray-700"
      >
        <!-- Avatar -->
        @if (detail.profile.picture) {
          <img
            [src]="detail.profile.picture"
            [alt]="detail.profile.name"
            class="size-16 rounded-full"
          />
        } @else {
          <div
            class="flex items-center justify-center size-16 rounded-full bg-gray-200 dark:bg-gray-700"
          >
            <ng-icon name="heroUser" class="size-8 text-gray-500" />
          </div>
        }

        <!-- Info -->
        <div class="flex-1">
          <h1 class="text-2xl/9 font-bold">{{ detail.profile.name || 'Unknown User' }}</h1>
          <p class="text-gray-600 dark:text-gray-400">{{ detail.profile.email }}</p>
          <div class="flex items-center gap-4 mt-2 text-sm/6">
            <span class="text-gray-500">ID: {{ detail.profile.userId }}</span>
            <span class="text-gray-500">Domain: {{ detail.profile.emailDomain }}</span>
          </div>
          <div class="flex items-center gap-2 mt-2">
            @for (role of detail.profile.roles; track role) {
              <span
                class="px-2 py-0.5 text-xs bg-blue-100 text-blue-800 rounded-xs dark:bg-blue-900 dark:text-blue-200"
              >
                {{ role }}
              </span>
            }
          </div>
        </div>

        <!-- Status Badge -->
        <div>
          <span
            class="px-3 py-1 text-sm rounded-sm"
            [class]="
              detail.profile.status === 'active'
                ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                : detail.profile.status === 'suspended'
                  ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
                  : 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200'
            "
          >
            {{ detail.profile.status }}
          </span>
        </div>
      </div>

      <!-- Stats Grid -->
      <div class="grid gap-6 md:grid-cols-2 lg:grid-cols-3 mb-6">
        <!-- Cost Summary -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroCurrencyDollar" class="size-5 text-green-600" />
            <h3 class="font-semibold">Current Month Cost</h3>
          </div>
          <div class="text-3xl font-bold mb-2">
            \${{ detail.costSummary.totalCost.toFixed(2) }}
          </div>
          <div class="space-y-1 text-sm/6 text-gray-600 dark:text-gray-400">
            <div>{{ detail.costSummary.totalRequests }} requests</div>
            <div>
              {{ formatTokens(detail.costSummary.totalInputTokens) }} input /
              {{ formatTokens(detail.costSummary.totalOutputTokens) }} output tokens
            </div>
            @if (detail.costSummary.cacheSavings > 0) {
              <div class="text-green-600">
                \${{ detail.costSummary.cacheSavings.toFixed(2) }} cache savings
              </div>
            }
          </div>
        </div>

        <!-- Quota Status -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroChartBar" class="size-5 text-blue-600" />
            <h3 class="font-semibold">Quota Status</h3>
          </div>
          @if (detail.quotaStatus.tierName) {
            <div class="mb-2">
              <span class="text-lg font-medium">{{ detail.quotaStatus.tierName }}</span>
              <span class="text-sm/6 text-gray-500 ml-2">
                ({{ detail.quotaStatus.matchedBy }})
              </span>
            </div>
            <!-- Progress Bar -->
            <div class="mb-2">
              <div class="flex justify-between text-sm/6 mb-1">
                <span>\${{ detail.quotaStatus.currentUsage.toFixed(2) }}</span>
                <span>\${{ detail.quotaStatus.monthlyLimit?.toFixed(2) ?? '∞' }}</span>
              </div>
              <div class="h-2 bg-gray-200 rounded-full dark:bg-gray-700">
                <div
                  class="h-2 rounded-full"
                  [class]="
                    detail.quotaStatus.usagePercentage >= 90
                      ? 'bg-red-500'
                      : detail.quotaStatus.usagePercentage >= 80
                        ? 'bg-yellow-500'
                        : 'bg-green-500'
                  "
                  [style.width.%]="Math.min(detail.quotaStatus.usagePercentage, 100)"
                ></div>
              </div>
              <div class="text-sm/6 text-gray-500 mt-1">
                {{ detail.quotaStatus.usagePercentage.toFixed(1) }}% used
                @if (detail.quotaStatus.remaining !== undefined) {
                  &middot; \${{ detail.quotaStatus.remaining.toFixed(2) }} remaining
                }
              </div>
            </div>
            @if (detail.quotaStatus.hasActiveOverride) {
              <div
                class="flex items-center gap-2 mt-3 p-2 bg-yellow-50 border border-yellow-200 rounded-xs dark:bg-yellow-900/20 dark:border-yellow-800"
              >
                <ng-icon name="heroShieldCheck" class="size-4 text-yellow-600" />
                <span class="text-sm/6">
                  Override active: {{ detail.quotaStatus.overrideReason }}
                </span>
              </div>
            }
          } @else {
            <div class="text-gray-500">No quota assigned</div>
          }
        </div>

        <!-- Activity -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroClock" class="size-5 text-purple-600" />
            <h3 class="font-semibold">Activity</h3>
          </div>
          <div class="space-y-2 text-sm/6">
            <div class="flex justify-between">
              <span class="text-gray-500">Member since:</span>
              <span>{{ formatFullDate(detail.profile.createdAt) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-500">Last login:</span>
              <span>{{ formatFullDate(detail.profile.lastLoginAt) }}</span>
            </div>
            @if (detail.costSummary.primaryModel) {
              <div class="flex justify-between">
                <span class="text-gray-500">Primary model:</span>
                <span>{{ detail.costSummary.primaryModel }}</span>
              </div>
            }
          </div>
        </div>
      </div>

      <!-- Recent Events -->
      <div
        class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
      >
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold">Recent Quota Events</h3>
          <button class="text-sm/6 text-blue-600 hover:text-blue-800">View All</button>
        </div>
        @if (detail.recentEvents.length > 0) {
          <div class="space-y-3">
            @for (event of detail.recentEvents; track event.eventId) {
              <div class="flex items-center gap-3 p-3 bg-gray-50 rounded-xs dark:bg-gray-700">
                <ng-icon
                  [name]="
                    event.eventType === 'block'
                      ? 'heroExclamationTriangle'
                      : 'heroChartBar'
                  "
                  class="size-5"
                  [class]="
                    event.eventType === 'block'
                      ? 'text-red-500'
                      : event.eventType === 'warning'
                        ? 'text-yellow-500'
                        : 'text-blue-500'
                  "
                />
                <div class="flex-1">
                  <span class="font-medium capitalize">{{ event.eventType }}</span>
                  <span class="text-gray-500 ml-2">
                    at {{ event.percentageUsed.toFixed(0) }}% usage
                  </span>
                </div>
                <span class="text-sm/6 text-gray-500">
                  {{ formatFullDate(event.timestamp) }}
                </span>
              </div>
            }
          </div>
        } @else {
          <div class="text-center py-4 text-gray-500">No recent events</div>
        }
      </div>

      <!-- Admin Actions -->
      <div class="flex gap-4 mt-6">
        <button
          (click)="createOverride()"
          class="px-4 py-2 bg-blue-600 text-white rounded-sm hover:bg-blue-700"
        >
          Create Override
        </button>
        <button
          (click)="assignTier()"
          class="px-4 py-2 border border-gray-300 rounded-sm hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
        >
          Assign Tier
        </button>
        <button
          (click)="viewCostDetails()"
          class="px-4 py-2 border border-gray-300 rounded-sm hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
        >
          View Cost Details
        </button>
      </div>
    }
  `,
})
export class UserDetailPage implements OnInit {
  state = inject(UserStateService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  user = computed(() => this.state.selectedUser());
  Math = Math; // Expose Math for template

  ngOnInit(): void {
    const userId = this.route.snapshot.paramMap.get('userId');
    if (userId) {
      this.state.loadUserDetail(userId);
    }
  }

  goBack(): void {
    this.state.clearSelection();
    this.router.navigate(['/admin/users']);
  }

  createOverride(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      this.router.navigate(['/admin/quota/overrides/new'], {
        queryParams: { userId },
      });
    }
  }

  assignTier(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      this.router.navigate(['/admin/quota/assignments/new'], {
        queryParams: { userId, type: 'direct_user' },
      });
    }
  }

  viewCostDetails(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      // Navigate to cost dashboard with user filter (if supported)
      this.router.navigate(['/admin/costs'], {
        queryParams: { userId },
      });
    }
  }

  formatTokens(tokens: number): string {
    if (tokens >= 1_000_000) {
      return `${(tokens / 1_000_000).toFixed(1)}M`;
    } else if (tokens >= 1_000) {
      return `${(tokens / 1_000).toFixed(1)}K`;
    }
    return tokens.toString();
  }

  formatFullDate(isoString: string): string {
    return new Date(isoString).toLocaleString();
  }
}
```

---

## User Sync Strategy

### When to Sync

User sync from JWT should occur:

1. **On Login** - When user authenticates and receives new tokens
2. **On Token Refresh** - When refresh token is exchanged for new access token

### Integration Point

Modify the existing auth dependency to call sync:

**File:** `backend/src/apis/shared/auth/dependencies.py`

```python
from users.sync import UserSyncService
from users.repository import UserRepository

# Initialize once
user_repo = UserRepository(dynamodb_client, table_name)
user_sync = UserSyncService(user_repo)


async def get_current_user(
    token: str = Depends(oauth2_scheme)
) -> User:
    """Validate JWT and sync user to database"""
    # Validate JWT (existing logic)
    claims = validate_jwt(token)

    # Sync user to database (fire-and-forget for performance)
    try:
        asyncio.create_task(user_sync.sync_from_jwt(claims))
    except Exception as e:
        logger.warning(f"User sync failed: {e}")
        # Don't fail the request if sync fails

    # Return user object
    return User(
        user_id=claims["sub"],
        email=claims["email"],
        name=claims.get("name", ""),
        roles=claims.get("roles", []),
        picture=claims.get("picture")
    )
```

### First-Time User Flow

```
1. User logs in for first time
2. JWT validated
3. sync_from_jwt() called
4. No existing user found
5. New user created with:
   - createdAt = now
   - lastLoginAt = now
   - status = "active"
6. User record now in DynamoDB
```

### Returning User Flow

```
1. User logs in
2. JWT validated
3. sync_from_jwt() called
4. Existing user found
5. User updated with:
   - lastLoginAt = now
   - Other fields synced (name, roles, picture)
   - createdAt preserved
6. User record updated
```

---

## Testing Strategy

### Backend Unit Tests

**File:** `backend/tests/users/test_repository.py`

```python
import pytest
from users.repository import UserRepository
from users.models import UserProfile

@pytest.mark.asyncio
async def test_create_and_get_user(user_repo):
    """Test creating and retrieving a user"""
    profile = UserProfile(
        user_id="test-123",
        email="test@example.com",
        name="Test User",
        roles=["user"],
        email_domain="example.com",
        created_at="2025-01-01T00:00:00Z",
        last_login_at="2025-01-01T00:00:00Z",
        status="active"
    )

    await user_repo.create_user(profile)
    retrieved = await user_repo.get_user("test-123")

    assert retrieved is not None
    assert retrieved.email == "test@example.com"
    assert retrieved.status == "active"


@pytest.mark.asyncio
async def test_get_user_by_email_case_insensitive(user_repo):
    """Test email lookup is case-insensitive"""
    profile = UserProfile(
        user_id="test-456",
        email="Test.User@Example.COM",
        name="Test User",
        roles=[],
        email_domain="example.com",
        created_at="2025-01-01T00:00:00Z",
        last_login_at="2025-01-01T00:00:00Z",
        status="active"
    )

    await user_repo.create_user(profile)

    # Should find with lowercase
    retrieved = await user_repo.get_user_by_email("test.user@example.com")
    assert retrieved is not None
    assert retrieved.user_id == "test-456"


@pytest.mark.asyncio
async def test_list_users_by_domain(user_repo):
    """Test listing users by email domain"""
    # Create users in different domains
    for i, domain in enumerate(["example.com", "example.com", "other.com"]):
        profile = UserProfile(
            user_id=f"user-{i}",
            email=f"user{i}@{domain}",
            name=f"User {i}",
            roles=[],
            email_domain=domain,
            created_at="2025-01-01T00:00:00Z",
            last_login_at=f"2025-01-0{i+1}T00:00:00Z",
            status="active"
        )
        await user_repo.create_user(profile)

    users, _ = await user_repo.list_users_by_domain("example.com")
    assert len(users) == 2
```

### Frontend Tests

**File:** `frontend/ai.client/src/app/admin/users/services/user-http.service.spec.ts`

```typescript
import { TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import { UserHttpService } from './user-http.service';

describe('UserHttpService', () => {
  let service: UserHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [UserHttpService],
    });

    service = TestBed.inject(UserHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should list users with status filter', () => {
    const mockResponse = {
      users: [{ userId: '123', email: 'test@example.com', name: 'Test', status: 'active' }],
      nextCursor: null,
    };

    service.listUsers('active').subscribe((response) => {
      expect(response.users.length).toBe(1);
      expect(response.users[0].userId).toBe('123');
    });

    const req = httpMock.expectOne((r) => r.url.includes('/api/admin/users'));
    expect(req.request.params.get('status')).toBe('active');
    req.flush(mockResponse);
  });

  it('should search by email', () => {
    service.searchByEmail('test@example.com').subscribe();

    const req = httpMock.expectOne((r) => r.url.includes('/search'));
    expect(req.request.params.get('email')).toBe('test@example.com');
    req.flush({ users: [], nextCursor: null });
  });
});
```

---

## Deployment Plan

### 1. Infrastructure (DynamoDB Table)

#### Option A: AWS CLI (Manual)

```bash
aws dynamodb create-table \
  --table-name Users \
  --attribute-definitions \
    AttributeName=PK,AttributeType=S \
    AttributeName=SK,AttributeType=S \
    AttributeName=userId,AttributeType=S \
    AttributeName=email,AttributeType=S \
    AttributeName=GSI2PK,AttributeType=S \
    AttributeName=GSI2SK,AttributeType=S \
    AttributeName=GSI3PK,AttributeType=S \
    AttributeName=GSI3SK,AttributeType=S \
  --key-schema \
    AttributeName=PK,KeyType=HASH \
    AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes \
    '[
      {
        "IndexName": "UserIdIndex",
        "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"}
      },
      {
        "IndexName": "EmailIndex",
        "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
        "Projection": {"ProjectionType": "ALL"}
      },
      {
        "IndexName": "EmailDomainIndex",
        "KeySchema": [
          {"AttributeName": "GSI2PK", "KeyType": "HASH"},
          {"AttributeName": "GSI2SK", "KeyType": "RANGE"}
        ],
        "Projection": {
          "ProjectionType": "INCLUDE",
          "NonKeyAttributes": ["userId", "email", "name", "status"]
        }
      },
      {
        "IndexName": "StatusLoginIndex",
        "KeySchema": [
          {"AttributeName": "GSI3PK", "KeyType": "HASH"},
          {"AttributeName": "GSI3SK", "KeyType": "RANGE"}
        ],
        "Projection": {
          "ProjectionType": "INCLUDE",
          "NonKeyAttributes": ["userId", "email", "name", "emailDomain"]
        }
      }
    ]' \
  --billing-mode PAY_PER_REQUEST
```

#### Option B: CDK (Recommended)

**File:** `infrastructure/lib/app-api-stack.ts`

Add the Users table after the existing Managed Models table section (~line 520):

```typescript
// ============================================================
// Users Table (User Admin)
// ============================================================

// Users Table - User profiles synced from JWT for admin lookup
const usersTable = new dynamodb.Table(this, 'UsersTable', {
  tableName: getResourceName(config, 'users'),
  partitionKey: {
    name: 'PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'SK',
    type: dynamodb.AttributeType.STRING,
  },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  removalPolicy: config.environment === 'prod'
    ? cdk.RemovalPolicy.RETAIN
    : cdk.RemovalPolicy.DESTROY,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
});

// UserIdIndex - O(1) lookup by userId for admin deep links
usersTable.addGlobalSecondaryIndex({
  indexName: 'UserIdIndex',
  partitionKey: {
    name: 'userId',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.ALL,
});

// EmailIndex - O(1) lookup by email
usersTable.addGlobalSecondaryIndex({
  indexName: 'EmailIndex',
  partitionKey: {
    name: 'email',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.ALL,
});

// EmailDomainIndex - Browse users by company/domain
usersTable.addGlobalSecondaryIndex({
  indexName: 'EmailDomainIndex',
  partitionKey: {
    name: 'GSI2PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI2SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.INCLUDE,
  nonKeyAttributes: ['userId', 'email', 'name', 'status'],
});

// StatusLoginIndex - Browse users by status, sorted by last login
usersTable.addGlobalSecondaryIndex({
  indexName: 'StatusLoginIndex',
  partitionKey: {
    name: 'GSI3PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI3SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.INCLUDE,
  nonKeyAttributes: ['userId', 'email', 'name', 'emailDomain'],
});

// Store users table name in SSM
new ssm.StringParameter(this, 'UsersTableNameParameter', {
  parameterName: `/${config.projectPrefix}/users/users-table-name`,
  stringValue: usersTable.tableName,
  description: 'Users table name for admin user lookup',
  tier: ssm.ParameterTier.STANDARD,
});

new ssm.StringParameter(this, 'UsersTableArnParameter', {
  parameterName: `/${config.projectPrefix}/users/users-table-arn`,
  stringValue: usersTable.tableArn,
  description: 'Users table ARN',
  tier: ssm.ParameterTier.STANDARD,
});
```

**Add to ECS container environment variables** (~line 555-567):

```typescript
environment: {
  // ... existing environment variables ...
  DYNAMODB_USERS_TABLE_NAME: usersTable.tableName,
},
```

**Grant permissions to ECS task role** (~line 600):

```typescript
// Grant permissions for users table
usersTable.grantReadWriteData(taskDefinition.taskRole);
```

**Add CloudFormation output** (~line 730):

```typescript
new cdk.CfnOutput(this, 'UsersTableName', {
  value: usersTable.tableName,
  description: 'Users table name for admin user lookup',
  exportName: `${config.projectPrefix}-UsersTableName`,
});
```

### 2. Environment Configuration

**File:** `backend/src/.env.example`

Add after the existing quota table configuration (~line 160):

```bash
# =============================================================================
# USER ADMIN CONFIGURATION
# =============================================================================

# DynamoDB table for user profiles (OPTIONAL - User Admin)
# Purpose: Store user profiles synced from JWT for admin user lookup
# Local Development: Leave empty to disable user sync (admin user lookup disabled)
# Production: Set to your DynamoDB table name for admin user management
# Schema: PK=USER#<user_id>, SK=PROFILE
# GSIs: UserIdIndex (deep links), EmailIndex (search), EmailDomainIndex, StatusLoginIndex
# Features: JWT sync on login, admin deep links from cost dashboard
# CDK Deployment: See infrastructure/lib/app-api-stack.ts
# Example: Users-dev
DYNAMODB_USERS_TABLE_NAME=
```

### 3. Backend Deployment

```bash
# Add environment variable
export DYNAMODB_USERS_TABLE_NAME=Users

# Deploy backend
cd backend
docker build -t backend:user-admin .
docker push backend:user-admin
```

### 4. Frontend Deployment

```bash
cd frontend/ai.client

# Add routes to admin module
# Build and deploy
npm run build -- --configuration=production
aws s3 sync dist/ai-client s3://your-bucket/
```

### 5. Verification

```bash
# Test user sync
curl -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"message": "hello"}'

# Verify user was created
aws dynamodb get-item \
  --table-name Users \
  --key '{"PK": {"S": "USER#your-user-id"}, "SK": {"S": "PROFILE"}}'

# Test admin API
curl http://localhost:8000/api/admin/users \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Validation Criteria

### Backend

- [ ] Users table created with correct schema
- [ ] All 4 GSIs created and queryable (UserIdIndex, EmailIndex, EmailDomainIndex, StatusLoginIndex)
- [ ] User sync creates new users on first login
- [ ] User sync updates existing users on subsequent logins
- [ ] `lastLoginAt` updated correctly
- [ ] `createdAt` preserved on updates
- [ ] Email stored and queried as lowercase
- [ ] List by domain returns users sorted by lastLoginAt
- [ ] List by status returns users sorted by lastLoginAt
- [ ] Search by email is case-insensitive
- [ ] User detail aggregates data from multiple tables
- [ ] Admin endpoints require admin role

### Frontend

- [ ] User list displays with pagination
- [ ] Search by email works
- [ ] Status filter works
- [ ] Domain filter works (if implemented)
- [ ] User detail shows profile, cost, quota, events
- [ ] Admin actions navigate to correct pages
- [ ] Loading states display correctly
- [ ] Empty states display correctly

### Integration

- [ ] End-to-end: Login → User created → Admin can view
- [ ] End-to-end: User with cost → Detail shows correct cost
- [ ] End-to-end: User with quota → Detail shows correct quota
- [ ] End-to-end: Create override from user detail

---

## Future Enhancements

1. **Full-Text Search** - Integrate OpenSearch for name/email partial matching
2. **User Suspension** - Add suspend/unsuspend functionality
3. **Bulk Operations** - Export users, bulk tier assignment
4. **Usage Analytics** - Trends, graphs, comparisons
5. **Session History** - View user's conversation sessions
6. **Audit Logging** - Track admin actions on users

---

**End of Specification**
