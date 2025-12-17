# Quota Management System - Phase 1 Implementation Specification

**Phase:** 1 (MVP - Core Infrastructure)
**Created:** 2025-12-17
**Status:** Ready for Implementation

---

## Table of Contents

1. [Overview](#overview)
2. [Phase 1 Scope](#phase-1-scope)
3. [Architecture](#architecture)
4. [Database Schema](#database-schema)
5. [Backend Implementation](#backend-implementation)
6. [CDK Infrastructure](#cdk-infrastructure)
7. [Testing Strategy](#testing-strategy)
8. [Validation Criteria](#validation-criteria)

---

## Overview

### Objectives

Implement the foundational quota management system with:
- Scalable DynamoDB schema supporting 100,000+ users
- Core quota resolution with intelligent caching
- Basic quota assignments (direct user, JWT role, default tier)
- Admin CRUD APIs for tiers and assignments
- Hard limit blocking enforcement
- CDK infrastructure for all resources

### Success Criteria

- ✅ All DynamoDB queries use targeted GSI queries (ZERO table scans)
- ✅ Quota resolution completes in <100ms with cache
- ✅ 90% cache hit rate reduces DynamoDB costs
- ✅ Admin APIs follow existing patterns in `backend/src/apis/app_api/admin/`
- ✅ CDK creates all tables with proper GSIs
- ✅ Hard limits block requests when exceeded
- ✅ System scales to 100,000+ users without performance degradation

---

## Phase 1 Scope

### ✅ Included in Phase 1

**Database:**
- DynamoDB tables: `UserQuotas`, `QuotaEvents`
- All GSIs for scalable queries
- CDK infrastructure

**Backend:**
- Core models (Pydantic)
- Repository layer (DynamoDB access)
- QuotaResolver with cache
- QuotaChecker (hard limit enforcement)
- Admin CRUD APIs for tiers and assignments

**Features:**
- Quota tier management
- Direct user assignments
- JWT role assignments
- Default tier fallback
- Hard limit blocking
- Basic event recording (blocks only)

### ❌ Deferred to Phase 2

- Quota overrides (temporary exceptions)
- Soft limit warnings (80%, 90%)
- Email domain matching
- Event viewer UI
- Quota inspector UI
- Enhanced analytics
- Frontend implementation

---

## Architecture

### System Components (Phase 1)

```
┌─────────────────────────────────────────────────────────────┐
│                     Backend API                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Admin API Routes                         │   │
│  │  /api/admin/quota/tiers/*                            │   │
│  │  /api/admin/quota/assignments/*                      │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │           Quota Resolution Service                    │   │
│  │  - QuotaResolver (with 5min cache)                   │   │
│  │  - QuotaChecker (hard limits only)                   │   │
│  │  - QuotaEventRecorder (blocks only)                  │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────┬────────────────────────────────────────────────┘
             │ boto3
             ▼
┌─────────────────────────────────────────────────────────────┐
│                      DynamoDB                                │
│  ┌──────────────┐  ┌──────────────┐                         │
│  │  UserQuotas  │  │ QuotaEvents  │                         │
│  │   Table      │  │    Table     │                         │
│  │   (3 GSIs)   │  │   (1 GSI)    │                         │
│  └──────────────┘  └──────────────┘                         │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow (Phase 1)

```
1. Admin Request → Admin API
   ├─ Create/Update/Delete Tier
   ├─ Create/Update/Delete Assignment
   └─ QuotaAdminService → Repository → DynamoDB

2. User Request → QuotaChecker.check_quota(user)
   ├─ QuotaResolver.resolve_user_quota(user)
   │   ├─ Check cache (5min TTL)
   │   ├─ If miss: Query DynamoDB
   │   │   ├─ Check direct user (GSI2: UserAssignmentIndex)
   │   │   ├─ Check JWT roles (GSI3: RoleAssignmentIndex)
   │   │   └─ Fall back to default tier
   │   └─ Cache result
   ├─ Get current usage from CostAggregator
   ├─ Check hard limit (100% → block)
   └─ Record block event if exceeded

3. Allow/Block request
```

---

## Database Schema

### Tables Overview

| Table Name | Purpose | Primary Key | GSIs | Expected Size |
|------------|---------|-------------|------|---------------|
| `UserQuotas` | Tiers & assignments | PK, SK | 3 GSIs | ~10K items |
| `QuotaEvents` | Event tracking | PK, SK | 1 GSI | ~1M items/month |

### UserQuotas Table

**Purpose:** Single-table design for quota tiers and assignments (Phase 1: no overrides)

**Primary Key:**
- **PK** (String): Entity type identifier
- **SK** (String): Metadata or sort key

**Attributes:**
- All entity fields (camelCase for consistency with API)
- GSI key attributes (GSI1PK, GSI1SK, GSI2PK, etc.)

**Capacity:**
- Billing Mode: **PAY_PER_REQUEST** (on-demand)
- Rationale: Admin operations are infrequent; read patterns favor caching

#### Entity Types

##### 1. Quota Tier

```json
{
  "PK": "QUOTA_TIER#<tier_id>",
  "SK": "METADATA",
  "tierId": "premium",
  "tierName": "Premium Tier",
  "description": "For premium users with higher usage needs",
  "monthlyCostLimit": 500.00,
  "dailyCostLimit": 20.00,
  "periodType": "monthly",
  "actionOnLimit": "block",
  "enabled": true,
  "createdAt": "2025-12-17T00:00:00Z",
  "updatedAt": "2025-12-17T00:00:00Z",
  "createdBy": "admin123"
}
```

**Query Pattern:**
- Get tier by ID: `PK = "QUOTA_TIER#<tier_id>" AND SK = "METADATA"`
- List all tiers: Query with `begins_with(PK, "QUOTA_TIER#")`

##### 2. Quota Assignment

```json
{
  "PK": "ASSIGNMENT#<assignment_id>",
  "SK": "METADATA",
  "GSI1PK": "ASSIGNMENT_TYPE#jwt_role",
  "GSI1SK": "PRIORITY#200#<assignment_id>",
  "GSI2PK": "USER#<user_id>",
  "GSI2SK": "ASSIGNMENT#<assignment_id>",
  "GSI3PK": "ROLE#Faculty",
  "GSI3SK": "PRIORITY#200",
  "assignmentId": "abc123",
  "tierId": "premium",
  "assignmentType": "jwt_role",
  "jwtRole": "Faculty",
  "priority": 200,
  "enabled": true,
  "createdAt": "2025-12-17T00:00:00Z",
  "updatedAt": "2025-12-17T00:00:00Z",
  "createdBy": "admin123"
}
```

**Assignment Types (Phase 1):**
- `direct_user` - Specific user assignment
- `jwt_role` - Role-based assignment
- `default_tier` - Default for all users

**Priority System:**
- Higher number = higher priority (evaluated first)
- Typical values:
  - Direct user: 300
  - JWT role: 200
  - Default tier: 100

**Query Patterns:**
- Get assignment by ID: `PK = "ASSIGNMENT#<id>" AND SK = "METADATA"`
- Get user's assignment: GSI2 query `GSI2PK = "USER#<user_id>"`
- Get role assignments: GSI3 query `GSI3PK = "ROLE#<role>"` (sorted by priority)
- List by type: GSI1 query `GSI1PK = "ASSIGNMENT_TYPE#<type>"`

#### Global Secondary Indexes (Phase 1)

| GSI Name | PK | SK | Projection | Use Case |
|----------|----|----|------------|----------|
| **AssignmentTypeIndex** (GSI1) | `ASSIGNMENT_TYPE#<type>` | `PRIORITY#<num>#<id>` | ALL | List assignments by type, sorted by priority |
| **UserAssignmentIndex** (GSI2) | `USER#<user_id>` | `ASSIGNMENT#<id>` | ALL | Find direct user assignment (O(1) lookup) |
| **RoleAssignmentIndex** (GSI3) | `ROLE#<jwt_role>` | `PRIORITY#<num>` | ALL | Find role assignments, sorted by priority |

**Important:** All GSIs use **PAY_PER_REQUEST** billing to match base table.

### QuotaEvents Table

**Purpose:** Track quota enforcement events (Phase 1: blocks only)

**Primary Key:**
- **PK** (String): `USER#<user_id>`
- **SK** (String): `EVENT#<timestamp>#<event_id>`

**Attributes:**
```json
{
  "PK": "USER#test123",
  "SK": "EVENT#2025-12-17T12:00:00.123Z#evt123",
  "GSI5PK": "TIER#premium",
  "GSI5SK": "TIMESTAMP#2025-12-17T12:00:00.123Z",
  "eventId": "evt123",
  "userId": "test123",
  "tierId": "premium",
  "eventType": "block",
  "currentUsage": 505.00,
  "quotaLimit": 500.00,
  "percentageUsed": 101.0,
  "timestamp": "2025-12-17T12:00:00.123Z",
  "metadata": {
    "tierName": "Premium Tier",
    "sessionId": "session_xyz",
    "assignmentId": "abc123"
  }
}
```

**Event Types (Phase 1):**
- `block` - Hard limit exceeded, request blocked

**Query Patterns:**
- Get user events: `PK = "USER#<user_id>"` (sorted by timestamp DESC)
- Get recent event: `PK = "USER#<user_id>" AND SK >= "EVENT#<cutoff_time>"`
- Get tier events: GSI5 query `GSI5PK = "TIER#<tier_id>"`

**Global Secondary Index:**

| GSI Name | PK | SK | Projection | Use Case |
|----------|----|----|------------|----------|
| **TierEventIndex** (GSI5) | `TIER#<tier_id>` | `TIMESTAMP#<iso_timestamp>` | ALL | Analytics on tier usage (Phase 2) |

**Capacity:** PAY_PER_REQUEST (high write volume, infrequent reads)

**TTL:** Consider adding TTL attribute to auto-delete events >90 days (Phase 2 optimization)

---

## Backend Implementation

### Directory Structure

```
backend/src/
├── apis/
│   └── app_api/
│       └── admin/
│           └── quota/                  # ← NEW: Quota admin API
│               ├── __init__.py
│               ├── routes.py           # FastAPI routes
│               ├── service.py          # Business logic
│               └── models.py           # Request/response models
├── agentcore/
│   └── quota/                          # ← NEW: Core quota logic
│       ├── __init__.py
│       ├── models.py                   # Pydantic domain models
│       ├── repository.py               # DynamoDB access layer
│       ├── resolver.py                 # QuotaResolver with cache
│       ├── checker.py                  # QuotaChecker (enforcement)
│       └── event_recorder.py           # QuotaEventRecorder
└── middleware/
    └── quota_middleware.py             # ← NEW: Request-level quota checking
```

### Core Models

**File:** `backend/src/agentcore/quota/models.py`

```python
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any, List
from enum import Enum
from datetime import datetime

class QuotaAssignmentType(str, Enum):
    """How a quota is assigned to users (Phase 1)"""
    DIRECT_USER = "direct_user"
    JWT_ROLE = "jwt_role"
    DEFAULT_TIER = "default_tier"

class QuotaTier(BaseModel):
    """A quota tier configuration"""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    tier_name: str = Field(..., alias="tierName")
    description: Optional[str] = None

    # Quota limits
    monthly_cost_limit: float = Field(..., alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)
    period_type: Literal["daily", "monthly"] = Field(default="monthly", alias="periodType")

    # Hard limit behavior (Phase 1: block only)
    action_on_limit: Literal["block"] = Field(
        default="block",
        alias="actionOnLimit"
    )

    # Metadata
    enabled: bool = Field(default=True)
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: str = Field(..., alias="createdBy")

class QuotaAssignment(BaseModel):
    """Assignment of a quota tier to users"""
    model_config = ConfigDict(populate_by_name=True)

    assignment_id: str = Field(..., alias="assignmentId")
    tier_id: str = Field(..., alias="tierId")
    assignment_type: QuotaAssignmentType = Field(..., alias="assignmentType")

    # Assignment criteria (one populated based on type)
    user_id: Optional[str] = Field(None, alias="userId")
    jwt_role: Optional[str] = Field(None, alias="jwtRole")

    # Priority (higher = more specific, evaluated first)
    priority: int = Field(
        default=100,
        description="Higher priority overrides lower",
        ge=0
    )

    # Metadata
    enabled: bool = Field(default=True)
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    created_by: str = Field(..., alias="createdBy")

    @field_validator('user_id', 'jwt_role')
    @classmethod
    def validate_criteria_match(cls, v, info):
        """Ensure criteria matches assignment type"""
        assignment_type = info.data.get('assignment_type')
        field_name = info.field_name

        if assignment_type == QuotaAssignmentType.DIRECT_USER and field_name == 'user_id':
            if not v:
                raise ValueError("user_id required for direct_user assignment")
        elif assignment_type == QuotaAssignmentType.JWT_ROLE and field_name == 'jwt_role':
            if not v:
                raise ValueError("jwt_role required for jwt_role assignment")

        return v

class QuotaEvent(BaseModel):
    """Track quota enforcement events (Phase 1: blocks only)"""
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(..., alias="eventId")
    user_id: str = Field(..., alias="userId")
    tier_id: str = Field(..., alias="tierId")
    event_type: Literal["block"] = Field(..., alias="eventType")  # Phase 1: blocks only

    # Context
    current_usage: float = Field(..., alias="currentUsage")
    quota_limit: float = Field(..., alias="quotaLimit")
    percentage_used: float = Field(..., alias="percentageUsed")

    timestamp: str
    metadata: Optional[Dict[str, Any]] = None

class QuotaCheckResult(BaseModel):
    """Result of quota check"""
    allowed: bool
    message: str
    tier: Optional[QuotaTier] = None
    current_usage: float = Field(default=0.0, alias="currentUsage")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    percentage_used: float = Field(default=0.0, alias="percentageUsed")
    remaining: Optional[float] = None

class ResolvedQuota(BaseModel):
    """Resolved quota information for a user"""
    user_id: str = Field(..., alias="userId")
    tier: QuotaTier
    matched_by: str = Field(
        ...,
        alias="matchedBy",
        description="How quota was resolved (e.g., 'direct_user', 'jwt_role:Faculty')"
    )
    assignment: QuotaAssignment
```

### Repository Layer (Partial - Key Methods)

**File:** `backend/src/agentcore/quota/repository.py`

```python
from typing import Optional, List
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import logging
from .models import QuotaTier, QuotaAssignment, QuotaEvent

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

    # ========== Quota Assignments ==========

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
```

**Note:** See full repository implementation in `docs/QUOTA_MANAGEMENT_PHASE1_FULL.md` for all CRUD methods.

### Quota Resolver (with Cache)

**File:** `backend/src/agentcore/quota/resolver.py`

```python
from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaAssignment, ResolvedQuota
from .repository import QuotaRepository

logger = logging.getLogger(__name__)

class QuotaResolver:
    """
    Resolves user quota tier with intelligent caching.

    Phase 1: Supports direct user, JWT role, and default tier assignments.
    Cache TTL: 5 minutes (reduces DynamoDB calls by ~90%)
    """

    def __init__(
        self,
        repository: QuotaRepository,
        cache_ttl_seconds: int = 300  # 5 minutes
    ):
        self.repository = repository
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[Optional[ResolvedQuota], datetime]] = {}

    async def resolve_user_quota(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota tier for a user using priority-based matching with caching.

        Priority order (highest to lowest):
        1. Direct user assignment (priority ~300)
        2. JWT role assignment (priority ~200)
        3. Default tier (priority ~100)
        """
        cache_key = self._get_cache_key(user)

        # Check cache
        if cache_key in self._cache:
            resolved, cached_at = self._cache[cache_key]
            if datetime.utcnow() - cached_at < timedelta(seconds=self.cache_ttl):
                logger.debug(f"Cache hit for user {user.user_id}")
                return resolved

        # Cache miss - resolve from database
        logger.debug(f"Cache miss for user {user.user_id}, resolving...")
        resolved = await self._resolve_from_db(user)

        # Cache result
        self._cache[cache_key] = (resolved, datetime.utcnow())

        return resolved

    async def _resolve_from_db(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota from database using targeted GSI queries.
        ZERO table scans.
        """

        # 1. Check for direct user assignment (GSI2: UserAssignmentIndex)
        user_assignment = await self.repository.query_user_assignment(user.user_id)
        if user_assignment and user_assignment.enabled:
            tier = await self.repository.get_tier(user_assignment.tier_id)
            if tier and tier.enabled:
                return ResolvedQuota(
                    user_id=user.user_id,
                    tier=tier,
                    matched_by="direct_user",
                    assignment=user_assignment
                )

        # 2. Check JWT role assignments (GSI3: RoleAssignmentIndex)
        if user.roles:
            role_assignments = []
            for role in user.roles:
                # Targeted query per role (O(log n) per role)
                assignments = await self.repository.query_role_assignments(role)
                role_assignments.extend(assignments)

            if role_assignments:
                # Sort by priority (descending) and take highest enabled
                role_assignments.sort(key=lambda a: a.priority, reverse=True)
                for assignment in role_assignments:
                    if assignment.enabled:
                        tier = await self.repository.get_tier(assignment.tier_id)
                        if tier and tier.enabled:
                            return ResolvedQuota(
                                user_id=user.user_id,
                                tier=tier,
                                matched_by=f"jwt_role:{assignment.jwt_role}",
                                assignment=assignment
                            )

        # 3. Fall back to default tier (GSI1: AssignmentTypeIndex)
        default_assignments = await self.repository.list_assignments_by_type(
            assignment_type="default_tier",
            enabled_only=True
        )
        if default_assignments:
            # Take highest priority default
            default_assignment = default_assignments[0]
            tier = await self.repository.get_tier(default_assignment.tier_id)
            if tier and tier.enabled:
                return ResolvedQuota(
                    user_id=user.user_id,
                    tier=tier,
                    matched_by="default_tier",
                    assignment=default_assignment
                )

        # No quota configured
        logger.warning(f"No quota configured for user {user.user_id}")
        return None

    def _get_cache_key(self, user: User) -> str:
        """
        Generate cache key from user attributes.

        Includes user_id and roles hash to auto-invalidate when these change.
        """
        roles_hash = hash(frozenset(user.roles)) if user.roles else 0
        return f"{user.user_id}:{roles_hash}"

    def invalidate_cache(self, user_id: Optional[str] = None):
        """Invalidate cache for specific user or all users"""
        if user_id:
            # Remove all cache entries for this user
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(f"{user_id}:")]
            for key in keys_to_remove:
                del self._cache[key]
            logger.info(f"Invalidated cache for user {user_id}")
        else:
            # Clear entire cache
            self._cache.clear()
            logger.info("Invalidated entire quota cache")
```

### Quota Checker (Hard Limits Only)

**File:** `backend/src/agentcore/quota/checker.py`

```python
from typing import Optional
from datetime import datetime
import logging
from apis.shared.auth.models import User
from api.costs.service import CostAggregator
from .models import QuotaTier, QuotaCheckResult, QuotaEvent
from .resolver import QuotaResolver
from .event_recorder import QuotaEventRecorder

logger = logging.getLogger(__name__)

class QuotaChecker:
    """Checks quota limits and enforces hard limits (Phase 1)"""

    def __init__(
        self,
        resolver: QuotaResolver,
        cost_aggregator: CostAggregator,
        event_recorder: QuotaEventRecorder
    ):
        self.resolver = resolver
        self.cost_aggregator = cost_aggregator
        self.event_recorder = event_recorder

    async def check_quota(self, user: User) -> QuotaCheckResult:
        """
        Check if user is within quota limits (Phase 1: hard limits only).

        Returns QuotaCheckResult with:
        - allowed: bool - whether request should proceed
        - message: str - explanation
        - tier: QuotaTier - applicable tier
        - current_usage, quota_limit, percentage_used, remaining
        """
        # Resolve user's quota tier
        resolved = await self.resolver.resolve_user_quota(user)

        if not resolved:
            # No quota configured - allow by default
            return QuotaCheckResult(
                allowed=True,
                message="No quota configured",
                current_usage=0.0,
                percentage_used=0.0
            )

        tier = resolved.tier

        # Handle unlimited tier (if configured with very high limit)
        if tier.monthly_cost_limit >= 999999:
            return QuotaCheckResult(
                allowed=True,
                message="Unlimited quota",
                tier=tier,
                current_usage=0.0,
                quota_limit=tier.monthly_cost_limit,
                percentage_used=0.0
            )

        # Get current usage for the period
        period = self._get_current_period(tier.period_type)
        summary = await self.cost_aggregator.get_user_cost_summary(
            user_id=user.user_id,
            period=period
        )

        current_usage = summary.total_cost
        limit = tier.monthly_cost_limit
        percentage_used = (current_usage / limit * 100) if limit > 0 else 0
        remaining = max(0, limit - current_usage)

        # Check hard limit (Phase 1: block only, no warnings)
        if current_usage >= limit:
            # Record block event
            await self.event_recorder.record_block(
                user=user,
                tier=tier,
                current_usage=current_usage,
                limit=limit,
                percentage_used=percentage_used
            )

            return QuotaCheckResult(
                allowed=False,
                message=f"Quota exceeded: ${current_usage:.2f} / ${limit:.2f}",
                tier=tier,
                current_usage=current_usage,
                quota_limit=limit,
                percentage_used=percentage_used,
                remaining=0.0
            )

        # Within limits
        return QuotaCheckResult(
            allowed=True,
            message="Within quota",
            tier=tier,
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            remaining=remaining
        )

    def _get_current_period(self, period_type: str) -> str:
        """Get current period string for cost aggregation"""
        now = datetime.utcnow()

        if period_type == "monthly":
            return now.strftime("%Y-%m")
        elif period_type == "daily":
            return now.strftime("%Y-%m-%d")
        else:
            return now.strftime("%Y-%m")
```

### Event Recorder (Blocks Only)

**File:** `backend/src/agentcore/quota/event_recorder.py`

```python
from typing import Optional
from datetime import datetime
import uuid
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaEvent
from .repository import QuotaRepository

logger = logging.getLogger(__name__)

class QuotaEventRecorder:
    """Records quota enforcement events (Phase 1: blocks only)"""

    def __init__(self, repository: QuotaRepository):
        self.repository = repository

    async def record_block(
        self,
        user: User,
        tier: QuotaTier,
        current_usage: float,
        limit: float,
        percentage_used: float,
        session_id: Optional[str] = None
    ):
        """Record quota block event"""
        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="block",
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            timestamp=datetime.utcnow().isoformat() + 'Z',
            metadata={
                "tier_name": tier.tier_name,
                "session_id": session_id
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded block event for user {user.user_id}")
        except Exception as e:
            logger.error(f"Failed to record block event: {e}")
```

### Admin API Routes

**File:** `backend/src/apis/app_api/admin/quota/routes.py`

```python
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
import logging
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.resolver import QuotaResolver
from agentcore.quota.models import QuotaTier, QuotaAssignment
from api.costs.service import CostAggregator
from .service import QuotaAdminService
from .models import (
    QuotaTierCreate,
    QuotaTierUpdate,
    QuotaAssignmentCreate,
    QuotaAssignmentUpdate,
    UserQuotaInfo
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quota", tags=["admin-quota"])

# ========== Dependencies ==========

def get_quota_repository() -> QuotaRepository:
    """Get quota repository instance"""
    return QuotaRepository()

def get_quota_resolver(
    repo: QuotaRepository = Depends(get_quota_repository)
) -> QuotaResolver:
    """Get quota resolver instance"""
    return QuotaResolver(repository=repo)

def get_quota_service(
    repo: QuotaRepository = Depends(get_quota_repository),
    resolver: QuotaResolver = Depends(get_quota_resolver),
    cost_aggregator: CostAggregator = Depends()
) -> QuotaAdminService:
    """Get quota admin service instance"""
    return QuotaAdminService(
        repository=repo,
        resolver=resolver,
        cost_aggregator=cost_aggregator
    )

# ========== Quota Tiers ==========

@router.post("/tiers", response_model=QuotaTier, status_code=status.HTTP_201_CREATED)
async def create_tier(
    tier_data: QuotaTierCreate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Create a new quota tier (admin only)"""
    # TODO: Add admin role check
    try:
        tier = await service.create_tier(tier_data, admin_user)
        return tier
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/tiers", response_model=List[QuotaTier])
async def list_tiers(
    enabled_only: bool = False,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """List all quota tiers (admin only)"""
    tiers = await service.list_tiers(enabled_only=enabled_only)
    return tiers

@router.get("/tiers/{tier_id}", response_model=QuotaTier)
async def get_tier(
    tier_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Get quota tier by ID (admin only)"""
    tier = await service.get_tier(tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail=f"Tier {tier_id} not found")
    return tier

@router.patch("/tiers/{tier_id}", response_model=QuotaTier)
async def update_tier(
    tier_id: str,
    updates: QuotaTierUpdate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Update quota tier (admin only)"""
    tier = await service.update_tier(tier_id, updates, admin_user)
    if not tier:
        raise HTTPException(status_code=404, detail=f"Tier {tier_id} not found")
    return tier

@router.delete("/tiers/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tier(
    tier_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Delete quota tier (admin only)"""
    try:
        success = await service.delete_tier(tier_id, admin_user)
        if not success:
            raise HTTPException(status_code=404, detail=f"Tier {tier_id} not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ========== Quota Assignments ==========

@router.post("/assignments", response_model=QuotaAssignment, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    assignment_data: QuotaAssignmentCreate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Create a new quota assignment (admin only)"""
    try:
        assignment = await service.create_assignment(assignment_data, admin_user)
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/assignments", response_model=List[QuotaAssignment])
async def list_assignments(
    assignment_type: Optional[str] = None,
    enabled_only: bool = False,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """List all quota assignments (admin only)"""
    assignments = await service.list_assignments(
        assignment_type=assignment_type,
        enabled_only=enabled_only
    )
    return assignments

@router.get("/assignments/{assignment_id}", response_model=QuotaAssignment)
async def get_assignment(
    assignment_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Get quota assignment by ID (admin only)"""
    assignment = await service.get_assignment(assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Assignment {assignment_id} not found")
    return assignment

@router.patch("/assignments/{assignment_id}", response_model=QuotaAssignment)
async def update_assignment(
    assignment_id: str,
    updates: QuotaAssignmentUpdate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Update quota assignment (admin only)"""
    try:
        assignment = await service.update_assignment(assignment_id, updates, admin_user)
        if not assignment:
            raise HTTPException(status_code=404, detail=f"Assignment {assignment_id} not found")
        return assignment
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(
    assignment_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Delete quota assignment (admin only)"""
    success = await service.delete_assignment(assignment_id, admin_user)
    if not success:
        raise HTTPException(status_code=404, detail=f"Assignment {assignment_id} not found")

# ========== User Quota Info (Inspector) ==========

@router.get("/users/{user_id}", response_model=UserQuotaInfo)
async def get_user_quota_info(
    user_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Get comprehensive quota information for a user (admin only)"""
    # TODO: Add ability to pass user email/roles for resolution
    info = await service.get_user_quota_info(user_id=user_id, email="", roles=[])
    return info
```

---

## CDK Infrastructure

### Stack Structure

```
cdk/
└── lib/
    └── stacks/
        └── quota-stack.ts           # ← NEW: Quota DynamoDB tables
```

### QuotaStack Implementation

**File:** `cdk/lib/stacks/quota-stack.ts`

```typescript
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

export interface QuotaStackProps extends cdk.StackProps {
  environment: string;
}

export class QuotaStack extends cdk.Stack {
  public readonly userQuotasTable: dynamodb.Table;
  public readonly quotaEventsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: QuotaStackProps) {
    super(scope, id, props);

    const { environment } = props;

    // ========== UserQuotas Table ==========

    this.userQuotasTable = new dynamodb.Table(this, 'UserQuotasTable', {
      tableName: `UserQuotas-${environment}`,
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
      removalPolicy: environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    // GSI1: AssignmentTypeIndex
    // Query assignments by type, sorted by priority
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'AssignmentTypeIndex',
      partitionKey: {
        name: 'GSI1PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI1SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: UserAssignmentIndex
    // Query direct user assignments (O(1) lookup)
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'UserAssignmentIndex',
      partitionKey: {
        name: 'GSI2PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI2SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI3: RoleAssignmentIndex
    // Query role-based assignments, sorted by priority
    this.userQuotasTable.addGlobalSecondaryIndex({
      indexName: 'RoleAssignmentIndex',
      partitionKey: {
        name: 'GSI3PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI3SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ========== QuotaEvents Table ==========

    this.quotaEventsTable = new dynamodb.Table(this, 'QuotaEventsTable', {
      tableName: `QuotaEvents-${environment}`,
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
      removalPolicy: environment === 'prod'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
    });

    // GSI5: TierEventIndex
    // Query events by tier for analytics (Phase 2)
    this.quotaEventsTable.addGlobalSecondaryIndex({
      indexName: 'TierEventIndex',
      partitionKey: {
        name: 'GSI5PK',
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: 'GSI5SK',
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ========== Outputs ==========

    new cdk.CfnOutput(this, 'UserQuotasTableName', {
      value: this.userQuotasTable.tableName,
      description: 'UserQuotas table name',
      exportName: `UserQuotasTable-${environment}`,
    });

    new cdk.CfnOutput(this, 'QuotaEventsTableName', {
      value: this.quotaEventsTable.tableName,
      description: 'QuotaEvents table name',
      exportName: `QuotaEventsTable-${environment}`,
    });

    // ========== Tags ==========

    cdk.Tags.of(this).add('Environment', environment);
    cdk.Tags.of(this).add('Service', 'quota-management');
    cdk.Tags.of(this).add('Phase', '1');
  }
}
```

### Integration with Main Stack

**File:** `cdk/bin/cdk.ts` (modification)

```typescript
import { QuotaStack } from '../lib/stacks/quota-stack';

const app = new cdk.App();
const environment = app.node.tryGetContext('environment') || 'dev';

// Existing stacks...

// Add QuotaStack
const quotaStack = new QuotaStack(app, `QuotaStack-${environment}`, {
  environment,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

app.synth();
```

### Deployment Commands

```bash
# Deploy quota stack to dev
cd cdk
cdk deploy QuotaStack-dev

# Deploy to production
cdk deploy QuotaStack-prod --context environment=prod

# View differences before deploy
cdk diff QuotaStack-dev
```

---

## Testing Strategy

### Unit Tests

**File:** `backend/tests/quota/test_resolver.py`

```python
import pytest
from datetime import datetime
from agentcore.quota.resolver import QuotaResolver
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.models import QuotaTier, QuotaAssignment, QuotaAssignmentType
from apis.shared.auth.models import User

@pytest.fixture
def mock_repository(mocker):
    return mocker.Mock(spec=QuotaRepository)

@pytest.fixture
def resolver(mock_repository):
    return QuotaResolver(repository=mock_repository, cache_ttl_seconds=300)

@pytest.mark.asyncio
async def test_resolve_direct_user_assignment(resolver, mock_repository):
    """Test that direct user assignment takes priority"""
    user = User(user_id="test123", email="test@example.com", roles=["Student"])

    # Mock direct user assignment
    assignment = QuotaAssignment(
        assignment_id="assign1",
        tier_id="premium",
        assignment_type=QuotaAssignmentType.DIRECT_USER,
        user_id="test123",
        priority=300,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    tier = QuotaTier(
        tier_id="premium",
        tier_name="Premium",
        monthly_cost_limit=500.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.query_user_assignment.return_value = assignment
    mock_repository.get_tier.return_value = tier

    # Resolve
    resolved = await resolver.resolve_user_quota(user)

    assert resolved is not None
    assert resolved.tier.tier_id == "premium"
    assert resolved.matched_by == "direct_user"
    assert resolved.assignment.assignment_id == "assign1"

@pytest.mark.asyncio
async def test_resolve_fallback_to_role(resolver, mock_repository):
    """Test fallback to role assignment when no direct user assignment"""
    user = User(user_id="test456", email="test@example.com", roles=["Faculty"])

    # No direct user assignment
    mock_repository.query_user_assignment.return_value = None

    # Mock role assignment
    role_assignment = QuotaAssignment(
        assignment_id="assign2",
        tier_id="faculty",
        assignment_type=QuotaAssignmentType.JWT_ROLE,
        jwt_role="Faculty",
        priority=200,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    tier = QuotaTier(
        tier_id="faculty",
        tier_name="Faculty",
        monthly_cost_limit=1000.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.query_role_assignments.return_value = [role_assignment]
    mock_repository.get_tier.return_value = tier

    # Resolve
    resolved = await resolver.resolve_user_quota(user)

    assert resolved is not None
    assert resolved.tier.tier_id == "faculty"
    assert resolved.matched_by == "jwt_role:Faculty"

@pytest.mark.asyncio
async def test_cache_hit(resolver, mock_repository):
    """Test that cache reduces DynamoDB calls"""
    user = User(user_id="test789", email="test@example.com", roles=[])

    # First call - cache miss
    mock_repository.query_user_assignment.return_value = None
    mock_repository.query_role_assignments.return_value = []

    default_assignment = QuotaAssignment(
        assignment_id="default1",
        tier_id="basic",
        assignment_type=QuotaAssignmentType.DEFAULT_TIER,
        priority=100,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    tier = QuotaTier(
        tier_id="basic",
        tier_name="Basic",
        monthly_cost_limit=100.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.list_assignments_by_type.return_value = [default_assignment]
    mock_repository.get_tier.return_value = tier

    resolved1 = await resolver.resolve_user_quota(user)

    # Second call - cache hit (no DB calls)
    resolved2 = await resolver.resolve_user_quota(user)

    assert resolved1 == resolved2
    # Verify DB was only called once
    assert mock_repository.query_user_assignment.call_count == 1
```

### Integration Tests

**File:** `backend/tests/quota/test_integration.py`

```python
import pytest
import boto3
from moto import mock_dynamodb
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.models import QuotaTier, QuotaAssignment, QuotaAssignmentType

@pytest.fixture
def dynamodb():
    with mock_dynamodb():
        yield boto3.resource('dynamodb', region_name='us-east-1')

@pytest.fixture
def create_tables(dynamodb):
    # Create UserQuotas table
    table = dynamodb.create_table(
        TableName='UserQuotas',
        KeySchema=[
            {'AttributeName': 'PK', 'KeyType': 'HASH'},
            {'AttributeName': 'SK', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'PK', 'AttributeType': 'S'},
            {'AttributeName': 'SK', 'AttributeType': 'S'},
            {'AttributeName': 'GSI2PK', 'AttributeType': 'S'},
            {'AttributeName': 'GSI2SK', 'AttributeType': 'S'},
        ],
        GlobalSecondaryIndexes=[
            {
                'IndexName': 'UserAssignmentIndex',
                'KeySchema': [
                    {'AttributeName': 'GSI2PK', 'KeyType': 'HASH'},
                    {'AttributeName': 'GSI2SK', 'KeyType': 'RANGE'},
                ],
                'Projection': {'ProjectionType': 'ALL'},
            }
        ],
        BillingMode='PAY_PER_REQUEST',
    )

    # Create QuotaEvents table
    events_table = dynamodb.create_table(
        TableName='QuotaEvents',
        KeySchema=[
            {'AttributeName': 'PK', 'KeyType': 'HASH'},
            {'AttributeName': 'SK', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'PK', 'AttributeType': 'S'},
            {'AttributeName': 'SK', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )

    return table, events_table

@pytest.mark.asyncio
async def test_create_and_retrieve_tier(dynamodb, create_tables):
    """Test creating and retrieving a tier from DynamoDB"""
    repo = QuotaRepository()

    tier = QuotaTier(
        tier_id="test-tier",
        tier_name="Test Tier",
        monthly_cost_limit=200.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="test"
    )

    # Create
    created = await repo.create_tier(tier)
    assert created.tier_id == "test-tier"

    # Retrieve
    retrieved = await repo.get_tier("test-tier")
    assert retrieved is not None
    assert retrieved.tier_name == "Test Tier"
    assert retrieved.monthly_cost_limit == 200.0
```

### API Tests

**File:** `backend/tests/quota/test_api.py`

```python
import pytest
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

@pytest.fixture
def admin_token():
    # TODO: Generate admin JWT token
    return "Bearer test_admin_token"

def test_create_tier(admin_token):
    """Test creating a tier via API"""
    response = client.post(
        "/api/admin/quota/tiers",
        json={
            "tierId": "api-test-tier",
            "tierName": "API Test Tier",
            "monthlyCostLimit": 300.0,
            "actionOnLimit": "block"
        },
        headers={"Authorization": admin_token}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["tierId"] == "api-test-tier"
    assert data["tierName"] == "API Test Tier"

def test_list_tiers(admin_token):
    """Test listing tiers via API"""
    response = client.get(
        "/api/admin/quota/tiers",
        headers={"Authorization": admin_token}
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
```

---

## Validation Criteria

### Phase 1 Completion Checklist

Use this checklist to validate Phase 1 implementation before proceeding to Phase 2:

#### ✅ Database (CDK)

- [ ] `UserQuotas` table created with correct schema
- [ ] All 3 GSIs created (AssignmentTypeIndex, UserAssignmentIndex, RoleAssignmentIndex)
- [ ] `QuotaEvents` table created with correct schema
- [ ] GSI5 (TierEventIndex) created
- [ ] Tables use PAY_PER_REQUEST billing
- [ ] Point-in-time recovery enabled
- [ ] Correct removal policy (RETAIN for prod, DESTROY for dev)

#### ✅ Backend - Core Logic

- [ ] `QuotaTier` model validates correctly
- [ ] `QuotaAssignment` model validates criteria match
- [ ] `QuotaRepository` implements all CRUD operations
- [ ] `QuotaRepository` uses ZERO table scans (all queries use GSIs or PK)
- [ ] `QuotaResolver` caches results for 5 minutes
- [ ] `QuotaResolver` correctly prioritizes: direct user > role > default
- [ ] `QuotaChecker` blocks requests when hard limit exceeded
- [ ] `QuotaEventRecorder` records block events to QuotaEvents table

#### ✅ Backend - Admin API

- [ ] Admin routes mounted at `/api/admin/quota/`
- [ ] All tier CRUD endpoints working (POST, GET, PATCH, DELETE)
- [ ] All assignment CRUD endpoints working
- [ ] `/users/{user_id}` endpoint returns comprehensive quota info
- [ ] All endpoints require authentication
- [ ] Proper error handling (400, 404, 500)

#### ✅ Testing

- [ ] Unit tests for `QuotaResolver` pass
- [ ] Unit tests for `QuotaChecker` pass
- [ ] Unit tests for `QuotaRepository` pass
- [ ] Integration tests with mocked DynamoDB pass
- [ ] API tests for all endpoints pass

#### ✅ Performance

- [ ] Quota resolution completes in <100ms with cache hit
- [ ] Cache hit rate >80% after warmup
- [ ] No DynamoDB scans in CloudWatch metrics
- [ ] All queries use targeted PK or GSI queries

#### ✅ Documentation

- [ ] All public methods have docstrings
- [ ] Type hints on all function signatures
- [ ] README updated with quota management overview
- [ ] API endpoints documented

### Manual Validation Steps

#### 1. Deploy Infrastructure

```bash
# Deploy CDK stack
cd cdk
cdk deploy QuotaStack-dev

# Verify tables created
aws dynamodb list-tables --query "TableNames[?contains(@, 'UserQuotas')]"
aws dynamodb describe-table --table-name UserQuotas-dev \
  --query "Table.GlobalSecondaryIndexes[].IndexName"
```

Expected output: `["AssignmentTypeIndex", "UserAssignmentIndex", "RoleAssignmentIndex"]`

#### 2. Test Admin API

```bash
# Create a tier
curl -X POST http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "test-tier",
    "tierName": "Test Tier",
    "monthlyCostLimit": 100.0,
    "actionOnLimit": "block"
  }'

# List tiers
curl http://localhost:8000/api/admin/quota/tiers \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Create direct user assignment
curl -X POST http://localhost:8000/api/admin/quota/assignments \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tierId": "test-tier",
    "assignmentType": "direct_user",
    "userId": "test123",
    "priority": 300
  }'
```

#### 3. Test Quota Resolution

```python
# Test quota resolution in Python console
from apis.shared.auth.models import User
from agentcore.quota.repository import QuotaRepository
from agentcore.quota.resolver import QuotaResolver

user = User(user_id="test123", email="test@example.com", roles=[])
repo = QuotaRepository()
resolver = QuotaResolver(repository=repo)

resolved = await resolver.resolve_user_quota(user)
print(f"Resolved tier: {resolved.tier.tier_name}")
print(f"Matched by: {resolved.matched_by}")
```

#### 4. Test Hard Limit Blocking

```python
from agentcore.quota.checker import QuotaChecker
from api.costs.service import CostAggregator

# Assume user has exceeded quota
result = await checker.check_quota(user)
print(f"Allowed: {result.allowed}")
print(f"Message: {result.message}")
print(f"Usage: {result.current_usage} / {result.quota_limit}")
```

#### 5. Verify No Scans in CloudWatch

```bash
# Check DynamoDB metrics for scans
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=UserQuotas-dev Name=Operation,Value=Scan \
  --start-time 2025-12-17T00:00:00Z \
  --end-time 2025-12-17T23:59:59Z \
  --period 3600 \
  --statistics Sum
```

Expected: Sum should be 0 (no scans)

---

## Next Steps

Once Phase 1 validation is complete:

1. **User Acceptance Testing**
   - Admin creates 3 tiers (Basic, Premium, Enterprise)
   - Admin creates assignments for different user types
   - Verify quota resolution works for sample users
   - Verify hard limits block requests correctly

2. **Performance Benchmarking**
   - Load test with 10,000 simulated users
   - Measure cache hit rate
   - Measure DynamoDB query latency
   - Verify no performance degradation

3. **Proceed to Phase 2**
   - See `QUOTA_MANAGEMENT_PHASE2_SPEC.md`
   - Implement quota overrides
   - Implement soft limit warnings
   - Implement email domain matching
   - Build frontend UI

---

## Appendix

### Sample Data for Testing

#### Sample Tiers

```json
[
  {
    "tierId": "basic",
    "tierName": "Basic",
    "description": "For casual users",
    "monthlyCostLimit": 50.0,
    "actionOnLimit": "block",
    "enabled": true
  },
  {
    "tierId": "premium",
    "tierName": "Premium",
    "description": "For regular users",
    "monthlyCostLimit": 200.0,
    "actionOnLimit": "block",
    "enabled": true
  },
  {
    "tierId": "enterprise",
    "tierName": "Enterprise",
    "description": "For power users",
    "monthlyCostLimit": 1000.0,
    "actionOnLimit": "block",
    "enabled": true
  }
]
```

#### Sample Assignments

```json
[
  {
    "assignmentType": "default_tier",
    "tierId": "basic",
    "priority": 100
  },
  {
    "assignmentType": "jwt_role",
    "tierId": "premium",
    "jwtRole": "Faculty",
    "priority": 200
  },
  {
    "assignmentType": "direct_user",
    "tierId": "enterprise",
    "userId": "admin123",
    "priority": 300
  }
]
```

### Expected Query Patterns

| Operation | Query Type | Index Used | O Complexity |
|-----------|------------|------------|--------------|
| Get tier by ID | GetItem | Primary Key | O(1) |
| List all tiers | Query (begins_with) | Primary Key | O(n) tiers |
| Get user assignment | Query | GSI2 (UserAssignmentIndex) | O(1) |
| Get role assignments | Query | GSI3 (RoleAssignmentIndex) | O(k) roles |
| List assignments by type | Query | GSI1 (AssignmentTypeIndex) | O(m) assignments |
| Get user events | Query | Primary Key | O(1) |
| Get tier events | Query | GSI5 (TierEventIndex) | O(p) events |

**No Scans:** All operations use targeted queries with known keys.

---

**End of Phase 1 Specification**
