# Quota Management System - Phase 2 Implementation Specification

**Phase:** 2 (Enhanced Features + Frontend)
**Created:** 2025-12-17
**Status:** Ready for Implementation (after Phase 1 validation)

---

## Table of Contents

1. [Overview](#overview)
2. [Phase 2 Scope](#phase-2-scope)
3. [Backend Enhancements](#backend-enhancements)
4. [Frontend Implementation](#frontend-implementation)
5. [Testing Strategy](#testing-strategy)
6. [Deployment Plan](#deployment-plan)
7. [Validation Criteria](#validation-criteria)

---

## Overview

### Objectives

Build upon Phase 1 foundation to deliver:
- Temporary quota overrides for exceptional cases
- Soft limit warnings (80%, 90%) before hard blocks
- Email domain-based quota matching
- Admin UI for comprehensive quota management
- Event viewer and analytics
- Quota inspector for troubleshooting

### Prerequisites

**Phase 1 must be complete and validated:**
- ✅ DynamoDB tables deployed
- ✅ Core quota resolution working
- ✅ Hard limit blocking functional
- ✅ Admin CRUD APIs operational
- ✅ Cache achieving >80% hit rate
- ✅ Zero table scans in production

---

## Phase 2 Scope

### ✅ Included in Phase 2

**Backend Enhancements:**
- Quota override management (temporary exceptions)
- Soft limit warning system (80%, 90% thresholds)
- Email domain matching (exact, wildcard, regex)
- Enhanced event recording (warnings, overrides)
- Deduplication logic for warning events

**Frontend Implementation:**
- Admin dashboard layout
- Tier management UI (CRUD)
- Assignment management UI (CRUD)
- Override management UI (create, list, disable)
- User quota inspector (search by user ID)
- Event viewer (filter by user, tier, type)
- Real-time usage display

**Analytics & Monitoring:**
- Tier usage statistics
- Event frequency charts
- Top quota consumers
- Warning/block trends

### ❌ Out of Scope

- Automated quota adjustment (ML-based)
- Usage forecasting
- Multi-tenant quota isolation
- Quota pooling (shared quotas across users)

---

## Backend Enhancements

### 1. Quota Override Support

**Already designed in Phase 1 schema** - now implement the logic.

#### Update Models

**File:** `backend/src/agentcore/quota/models.py` (additions)

```python
class QuotaOverride(BaseModel):
    """Temporary quota override for a user"""
    model_config = ConfigDict(populate_by_name=True)

    override_id: str = Field(..., alias="overrideId")
    user_id: str = Field(..., alias="userId")

    override_type: Literal["custom_limit", "unlimited"] = Field(
        ...,
        alias="overrideType"
    )

    # Custom limits (required if override_type == "custom_limit")
    monthly_cost_limit: Optional[float] = Field(None, alias="monthlyCostLimit", gt=0)
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", gt=0)

    # Temporal bounds
    valid_from: str = Field(..., alias="validFrom")
    valid_until: str = Field(..., alias="validUntil")

    # Metadata
    reason: str = Field(..., description="Justification for override")
    created_by: str = Field(..., alias="createdBy")
    created_at: str = Field(..., alias="createdAt")
    enabled: bool = Field(default=True)

    @field_validator('monthly_cost_limit')
    @classmethod
    def validate_custom_limit(cls, v, info):
        """Ensure custom_limit type has a limit specified"""
        if info.data.get('override_type') == 'custom_limit' and v is None:
            raise ValueError("monthly_cost_limit required for custom_limit type")
        return v
```

#### Update Repository

**File:** `backend/src/agentcore/quota/repository.py` (additions)

```python
# ========== Quota Overrides ==========

async def create_override(self, override: QuotaOverride) -> QuotaOverride:
    """Create a new quota override"""
    item = {
        "PK": f"OVERRIDE#{override.override_id}",
        "SK": "METADATA",
        "GSI4PK": f"USER#{override.user_id}",
        "GSI4SK": f"VALID_UNTIL#{override.valid_until}",
        **override.model_dump(by_alias=True, exclude_none=True)
    }

    try:
        self.table.put_item(Item=item)
        return override
    except ClientError as e:
        logger.error(f"Error creating override: {e}")
        raise

async def get_active_override(self, user_id: str) -> Optional[QuotaOverride]:
    """Get active override for user (valid and enabled)"""
    now = datetime.utcnow().isoformat() + 'Z'

    try:
        response = self.table.query(
            IndexName="UserOverrideIndex",
            KeyConditionExpression="GSI4PK = :pk AND GSI4SK >= :now",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_id}",
                ":now": f"VALID_UNTIL#{now}"
            },
            ScanIndexForward=False,  # Latest first
            Limit=1
        )

        items = response.get('Items', [])
        if not items:
            return None

        item = items[0]
        for key in ['PK', 'SK', 'GSI4PK', 'GSI4SK']:
            item.pop(key, None)

        override = QuotaOverride(**item)

        # Check if override is currently valid
        if override.enabled and override.valid_from <= now <= override.valid_until:
            return override

        return None
    except ClientError as e:
        logger.error(f"Error getting active override for {user_id}: {e}")
        return None
```

#### Update Resolver

**File:** `backend/src/agentcore/quota/resolver.py` (modify `_resolve_from_db`)

```python
async def _resolve_from_db(self, user: User) -> Optional[ResolvedQuota]:
    """
    Resolve quota from database using targeted GSI queries.

    Priority order (Phase 2):
    1. Active override (highest priority) ← NEW
    2. Direct user assignment
    3. JWT role assignment
    4. Email domain assignment ← NEW
    5. Default tier
    """

    # 1. Check for active override (highest priority)
    override = await self.repository.get_active_override(user.user_id)
    if override:
        tier = self._override_to_tier(override)
        return ResolvedQuota(
            user_id=user.user_id,
            tier=tier,
            matched_by="override",
            assignment=None,  # Overrides don't have assignments
            override=override
        )

    # 2. Check for direct user assignment
    # ... (same as Phase 1)

    # 3. Check JWT role assignments
    # ... (same as Phase 1)

    # 4. Check email domain assignments (NEW)
    if user.email and '@' in user.email:
        domain_assignments = await self._get_cached_domain_assignments()
        user_domain = user.email.split('@')[1]

        # Sort by priority and find matching domain
        for assignment in sorted(domain_assignments, key=lambda a: a.priority, reverse=True):
            if assignment.enabled and self._matches_email_domain(user_domain, assignment.email_domain):
                tier = await self.repository.get_tier(assignment.tier_id)
                if tier and tier.enabled:
                    return ResolvedQuota(
                        user_id=user.user_id,
                        tier=tier,
                        matched_by=f"email_domain:{assignment.email_domain}",
                        assignment=assignment
                    )

    # 5. Fall back to default tier
    # ... (same as Phase 1)

def _override_to_tier(self, override: QuotaOverride) -> QuotaTier:
    """Convert override to a tier for use in quota checking"""
    if override.override_type == "unlimited":
        return QuotaTier(
            tier_id=f"override_{override.override_id}",
            tier_name="Unlimited Override",
            monthly_cost_limit=float('inf'),
            action_on_limit="warn",
            created_at=override.created_at,
            updated_at=override.created_at,
            created_by=override.created_by
        )
    else:  # custom_limit
        return QuotaTier(
            tier_id=f"override_{override.override_id}",
            tier_name="Custom Override",
            monthly_cost_limit=override.monthly_cost_limit or 0,
            daily_cost_limit=override.daily_cost_limit,
            action_on_limit="block",
            soft_limit_percentage=80.0,  # Default
            created_at=override.created_at,
            updated_at=override.created_at,
            created_by=override.created_by
        )

async def _get_cached_domain_assignments(self) -> list:
    """Get domain assignments with separate cache (expensive query)"""
    if self._domain_assignments_cache:
        assignments, cached_at = self._domain_assignments_cache
        if datetime.utcnow() - cached_at < timedelta(seconds=self.cache_ttl):
            return assignments

    # Cache miss - query domain assignments
    assignments = await self.repository.list_assignments_by_type(
        assignment_type="email_domain",
        enabled_only=True
    )
    self._domain_assignments_cache = (assignments, datetime.utcnow())
    return assignments

def _matches_email_domain(self, user_domain: str, pattern: str) -> bool:
    """
    Enhanced email domain matching.

    Supported patterns:
    - Exact: "university.edu"
    - Wildcard subdomain: "*.university.edu"
    - Regex: "regex:^(cs|eng)\\.university\\.edu$"
    - Multiple: "university.edu,college.edu"
    """
    if not pattern:
        return False

    # Exact match
    if pattern == user_domain:
        return True

    # Wildcard subdomain (*.example.com)
    if pattern.startswith('*.'):
        base_domain = pattern[2:]
        return user_domain == base_domain or user_domain.endswith('.' + base_domain)

    # Regex pattern (prefix with "regex:")
    if pattern.startswith('regex:'):
        import re
        regex_pattern = pattern[6:]
        try:
            return bool(re.match(regex_pattern, user_domain))
        except re.error:
            logger.error(f"Invalid regex pattern: {regex_pattern}")
            return False

    # Multiple domains (comma-separated)
    if ',' in pattern:
        domains = [d.strip() for d in pattern.split(',')]
        return any(self._matches_email_domain(user_domain, d) for d in domains)

    return False
```

### 2. Soft Limit Warnings

#### Update Models

**File:** `backend/src/agentcore/quota/models.py` (modifications)

```python
class QuotaTier(BaseModel):
    """A quota tier configuration"""
    # ... existing fields ...

    # Soft limit configuration (Phase 2)
    soft_limit_percentage: float = Field(
        default=80.0,
        alias="softLimitPercentage",
        ge=0,
        le=100,
        description="Percentage at which warnings start"
    )

    # Hard limit behavior (Phase 2: warn or block)
    action_on_limit: Literal["block", "warn"] = Field(
        default="block",
        alias="actionOnLimit"
    )

class QuotaEvent(BaseModel):
    """Track quota enforcement events (Phase 2: all event types)"""
    # ... existing fields ...

    event_type: Literal["warning", "block", "reset", "override_applied"] = Field(
        ...,
        alias="eventType"
    )

class QuotaCheckResult(BaseModel):
    """Result of quota check"""
    # ... existing fields ...

    warning_level: Optional[Literal["none", "80%", "90%"]] = Field(
        None,
        alias="warningLevel"
    )
```

#### Update Checker

**File:** `backend/src/agentcore/quota/checker.py` (replace Phase 1 version)

```python
async def check_quota(self, user: User) -> QuotaCheckResult:
    """
    Check if user is within quota limits (Phase 2: soft + hard limits).

    Returns QuotaCheckResult with:
    - allowed: bool - whether request should proceed
    - message: str - explanation
    - tier: QuotaTier - applicable tier
    - current_usage, quota_limit, percentage_used, remaining
    - warning_level: "none", "80%", "90%"
    """
    # Resolve user's quota tier
    resolved = await self.resolver.resolve_user_quota(user)

    if not resolved:
        # No quota configured - allow by default
        return QuotaCheckResult(
            allowed=True,
            message="No quota configured",
            current_usage=0.0,
            percentage_used=0.0,
            warning_level="none"
        )

    tier = resolved.tier

    # Handle unlimited tier
    if tier.monthly_cost_limit == float('inf'):
        return QuotaCheckResult(
            allowed=True,
            message="Unlimited quota",
            tier=tier,
            current_usage=0.0,
            quota_limit=float('inf'),
            percentage_used=0.0,
            warning_level="none"
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

    # Determine warning level (Phase 2)
    warning_level = "none"
    soft_limit_percentage = tier.soft_limit_percentage

    if percentage_used >= 90:
        warning_level = "90%"
    elif percentage_used >= soft_limit_percentage:
        warning_level = f"{int(soft_limit_percentage)}%"

    # Record warning events if thresholds crossed (Phase 2)
    if warning_level != "none":
        await self.event_recorder.record_warning_if_needed(
            user=user,
            tier=tier,
            current_usage=current_usage,
            limit=limit,
            percentage_used=percentage_used,
            threshold=warning_level
        )

    # Check hard limit
    if current_usage >= limit:
        if tier.action_on_limit == "block":
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
                remaining=0.0,
                warning_level=warning_level
            )
        else:  # warn only (Phase 2)
            return QuotaCheckResult(
                allowed=True,
                message=f"Warning: Quota limit reached (${current_usage:.2f} / ${limit:.2f})",
                tier=tier,
                current_usage=current_usage,
                quota_limit=limit,
                percentage_used=percentage_used,
                remaining=0.0,
                warning_level=warning_level
            )

    # Within limits
    message = "Within quota"
    if warning_level != "none":
        message = f"Warning: {warning_level} quota used (${current_usage:.2f} / ${limit:.2f})"

    return QuotaCheckResult(
        allowed=True,
        message=message,
        tier=tier,
        current_usage=current_usage,
        quota_limit=limit,
        percentage_used=percentage_used,
        remaining=remaining,
        warning_level=warning_level
    )
```

#### Update Event Recorder

**File:** `backend/src/agentcore/quota/event_recorder.py` (add methods)

```python
async def record_warning_if_needed(
    self,
    user: User,
    tier: QuotaTier,
    current_usage: float,
    limit: float,
    percentage_used: float,
    threshold: str
):
    """
    Record warning event if user hasn't been warned recently.
    Prevents duplicate warnings within 60 minutes.
    """
    # Check for recent warning of this type
    recent_warning = await self.repository.get_recent_event(
        user_id=user.user_id,
        event_type="warning",
        within_minutes=60
    )

    if recent_warning and recent_warning.metadata:
        # Don't record if we've already warned about this threshold
        if recent_warning.metadata.get("threshold") == threshold:
            logger.debug(f"Skipping duplicate warning for user {user.user_id} at {threshold}")
            return

    # Record new warning
    event = QuotaEvent(
        event_id=str(uuid.uuid4()),
        user_id=user.user_id,
        tier_id=tier.tier_id,
        event_type="warning",
        current_usage=current_usage,
        quota_limit=limit,
        percentage_used=percentage_used,
        timestamp=datetime.utcnow().isoformat() + 'Z',
        metadata={
            "threshold": threshold,
            "tier_name": tier.tier_name
        }
    )

    try:
        await self.repository.record_event(event)
        logger.info(f"Recorded warning event for user {user.user_id} at {threshold}")
    except Exception as e:
        logger.error(f"Failed to record warning event: {e}")

async def record_override_applied(
    self,
    user: User,
    override_id: str,
    tier: QuotaTier
):
    """Record when an override is applied"""
    event = QuotaEvent(
        event_id=str(uuid.uuid4()),
        user_id=user.user_id,
        tier_id=tier.tier_id,
        event_type="override_applied",
        current_usage=0.0,
        quota_limit=tier.monthly_cost_limit,
        percentage_used=0.0,
        timestamp=datetime.utcnow().isoformat() + 'Z',
        metadata={
            "override_id": override_id,
            "tier_name": tier.tier_name
        }
    )

    try:
        await self.repository.record_event(event)
        logger.info(f"Recorded override applied for user {user.user_id}")
    except Exception as e:
        logger.error(f"Failed to record override event: {e}")
```

### 3. Admin API - Override Routes

**File:** `backend/src/apis/app_api/admin/quota/routes.py` (additions)

```python
# ========== Quota Overrides ==========

@router.post("/overrides", response_model=QuotaOverride, status_code=status.HTTP_201_CREATED)
async def create_override(
    override_data: QuotaOverrideCreate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Create a new quota override (admin only)"""
    try:
        override = await service.create_override(override_data, admin_user)
        return override
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/overrides", response_model=List[QuotaOverride])
async def list_overrides(
    user_id: Optional[str] = None,
    active_only: bool = False,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """List quota overrides (admin only)"""
    overrides = await service.list_overrides(
        user_id=user_id,
        active_only=active_only
    )
    return overrides

@router.get("/overrides/{override_id}", response_model=QuotaOverride)
async def get_override(
    override_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Get quota override by ID (admin only)"""
    override = await service.get_override(override_id)
    if not override:
        raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
    return override

@router.patch("/overrides/{override_id}", response_model=QuotaOverride)
async def update_override(
    override_id: str,
    updates: QuotaOverrideUpdate,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Update quota override (admin only)"""
    override = await service.update_override(override_id, updates, admin_user)
    if not override:
        raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
    return override

@router.delete("/overrides/{override_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(
    override_id: str,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Delete quota override (admin only)"""
    success = await service.delete_override(override_id, admin_user)
    if not success:
        raise HTTPException(status_code=404, detail=f"Override {override_id} not found")

# ========== Quota Events ==========

@router.get("/events", response_model=List[QuotaEvent])
async def get_events(
    user_id: Optional[str] = None,
    tier_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
    admin_user: User = Depends(get_current_user),
    service: QuotaAdminService = Depends(get_quota_service)
):
    """Get quota events with filters (admin only)"""
    events = await service.get_events(
        user_id=user_id,
        tier_id=tier_id,
        event_type=event_type,
        limit=limit
    )
    return events
```

### 4. CDK Infrastructure Update

**File:** `cdk/lib/stacks/quota-stack.ts` (add GSI4 for overrides)

```typescript
// GSI4: UserOverrideIndex (Phase 2)
// Query active overrides for a user
this.userQuotasTable.addGlobalSecondaryIndex({
  indexName: 'UserOverrideIndex',
  partitionKey: {
    name: 'GSI4PK',
    type: dynamodb.AttributeType.STRING,
  },
  sortKey: {
    name: 'GSI4SK',
    type: dynamodb.AttributeType.STRING,
  },
  projectionType: dynamodb.ProjectionType.ALL,
});
```

**Note:** This GSI was planned in Phase 1 schema but not created. Add it now.

---

## Frontend Implementation

### Directory Structure

```
frontend/ai.client/src/app/
├── admin/                          # ← NEW: Admin module
│   ├── quota/                      # ← NEW: Quota management
│   │   ├── pages/
│   │   │   ├── quota-dashboard.page.ts
│   │   │   ├── tier-list.page.ts
│   │   │   ├── tier-editor.page.ts
│   │   │   ├── assignment-list.page.ts
│   │   │   ├── assignment-editor.page.ts
│   │   │   ├── override-list.page.ts
│   │   │   ├── override-editor.page.ts
│   │   │   ├── quota-inspector.page.ts
│   │   │   └── event-viewer.page.ts
│   │   ├── components/
│   │   │   ├── tier-card.component.ts
│   │   │   ├── assignment-card.component.ts
│   │   │   ├── override-card.component.ts
│   │   │   ├── usage-meter.component.ts
│   │   │   ├── event-timeline.component.ts
│   │   │   └── quota-form.component.ts
│   │   ├── services/
│   │   │   ├── quota-http.service.ts
│   │   │   └── quota-state.service.ts
│   │   └── models/
│   │       └── quota.models.ts
│   └── admin-routing.module.ts
└── app-routing.module.ts
```

### Models

**File:** `frontend/ai.client/src/app/admin/quota/models/quota.models.ts`

```typescript
export type QuotaAssignmentType = 'direct_user' | 'jwt_role' | 'email_domain' | 'default_tier';
export type ActionOnLimit = 'block' | 'warn';
export type OverrideType = 'custom_limit' | 'unlimited';
export type EventType = 'warning' | 'block' | 'reset' | 'override_applied';
export type WarningLevel = 'none' | '80%' | '90%';

export interface QuotaTier {
  tierId: string;
  tierName: string;
  description?: string;
  monthlyCostLimit: number;
  dailyCostLimit?: number;
  periodType: 'daily' | 'monthly';
  softLimitPercentage: number;
  actionOnLimit: ActionOnLimit;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
}

export interface QuotaAssignment {
  assignmentId: string;
  tierId: string;
  assignmentType: QuotaAssignmentType;
  userId?: string;
  jwtRole?: string;
  emailDomain?: string;
  priority: number;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
}

export interface QuotaOverride {
  overrideId: string;
  userId: string;
  overrideType: OverrideType;
  monthlyCostLimit?: number;
  dailyCostLimit?: number;
  validFrom: string;
  validUntil: string;
  reason: string;
  createdBy: string;
  createdAt: string;
  enabled: boolean;
}

export interface QuotaEvent {
  eventId: string;
  userId: string;
  tierId: string;
  eventType: EventType;
  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;
  timestamp: string;
  metadata?: Record<string, any>;
}

export interface UserQuotaInfo {
  userId: string;
  tier?: QuotaTier;
  matchedBy: string;
  assignment?: QuotaAssignment;
  override?: QuotaOverride;
  currentUsage: number;
  quotaLimit?: number;
  percentageUsed: number;
  remaining?: number;
  recentEvents: QuotaEvent[];
}

// Request models
export interface QuotaTierCreate {
  tierId: string;
  tierName: string;
  description?: string;
  monthlyCostLimit: number;
  dailyCostLimit?: number;
  periodType?: 'daily' | 'monthly';
  softLimitPercentage?: number;
  actionOnLimit?: ActionOnLimit;
}

export interface QuotaAssignmentCreate {
  tierId: string;
  assignmentType: QuotaAssignmentType;
  userId?: string;
  jwtRole?: string;
  emailDomain?: string;
  priority?: number;
}

export interface QuotaOverrideCreate {
  userId: string;
  overrideType: OverrideType;
  monthlyCostLimit?: number;
  dailyCostLimit?: number;
  validFrom: string;
  validUntil: string;
  reason: string;
}
```

### HTTP Service

**File:** `frontend/ai.client/src/app/admin/quota/services/quota-http.service.ts`

```typescript
import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../../environments/environment';
import {
  QuotaTier,
  QuotaAssignment,
  QuotaOverride,
  QuotaEvent,
  UserQuotaInfo,
  QuotaTierCreate,
  QuotaAssignmentCreate,
  QuotaOverrideCreate,
} from '../models/quota.models';

@Injectable({
  providedIn: 'root',
})
export class QuotaHttpService {
  private http = inject(HttpClient);
  private baseUrl = `${environment.apiUrl}/api/admin/quota`;

  // ========== Tiers ==========

  listTiers(enabledOnly: boolean = false): Observable<QuotaTier[]> {
    const params = new HttpParams().set('enabled_only', enabledOnly);
    return this.http.get<QuotaTier[]>(`${this.baseUrl}/tiers`, { params });
  }

  getTier(tierId: string): Observable<QuotaTier> {
    return this.http.get<QuotaTier>(`${this.baseUrl}/tiers/${tierId}`);
  }

  createTier(tierData: QuotaTierCreate): Observable<QuotaTier> {
    return this.http.post<QuotaTier>(`${this.baseUrl}/tiers`, tierData);
  }

  updateTier(tierId: string, updates: Partial<QuotaTier>): Observable<QuotaTier> {
    return this.http.patch<QuotaTier>(`${this.baseUrl}/tiers/${tierId}`, updates);
  }

  deleteTier(tierId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/tiers/${tierId}`);
  }

  // ========== Assignments ==========

  listAssignments(
    assignmentType?: string,
    enabledOnly: boolean = false
  ): Observable<QuotaAssignment[]> {
    let params = new HttpParams().set('enabled_only', enabledOnly);
    if (assignmentType) {
      params = params.set('assignment_type', assignmentType);
    }
    return this.http.get<QuotaAssignment[]>(`${this.baseUrl}/assignments`, { params });
  }

  getAssignment(assignmentId: string): Observable<QuotaAssignment> {
    return this.http.get<QuotaAssignment>(`${this.baseUrl}/assignments/${assignmentId}`);
  }

  createAssignment(assignmentData: QuotaAssignmentCreate): Observable<QuotaAssignment> {
    return this.http.post<QuotaAssignment>(`${this.baseUrl}/assignments`, assignmentData);
  }

  updateAssignment(
    assignmentId: string,
    updates: Partial<QuotaAssignment>
  ): Observable<QuotaAssignment> {
    return this.http.patch<QuotaAssignment>(
      `${this.baseUrl}/assignments/${assignmentId}`,
      updates
    );
  }

  deleteAssignment(assignmentId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/assignments/${assignmentId}`);
  }

  // ========== Overrides ==========

  listOverrides(userId?: string, activeOnly: boolean = false): Observable<QuotaOverride[]> {
    let params = new HttpParams().set('active_only', activeOnly);
    if (userId) {
      params = params.set('user_id', userId);
    }
    return this.http.get<QuotaOverride[]>(`${this.baseUrl}/overrides`, { params });
  }

  createOverride(overrideData: QuotaOverrideCreate): Observable<QuotaOverride> {
    return this.http.post<QuotaOverride>(`${this.baseUrl}/overrides`, overrideData);
  }

  updateOverride(
    overrideId: string,
    updates: Partial<QuotaOverride>
  ): Observable<QuotaOverride> {
    return this.http.patch<QuotaOverride>(`${this.baseUrl}/overrides/${overrideId}`, updates);
  }

  deleteOverride(overrideId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/overrides/${overrideId}`);
  }

  // ========== User Info ==========

  getUserQuotaInfo(userId: string): Observable<UserQuotaInfo> {
    return this.http.get<UserQuotaInfo>(`${this.baseUrl}/users/${userId}`);
  }

  // ========== Events ==========

  getEvents(
    userId?: string,
    tierId?: string,
    eventType?: string,
    limit: number = 50
  ): Observable<QuotaEvent[]> {
    let params = new HttpParams().set('limit', limit);
    if (userId) params = params.set('user_id', userId);
    if (tierId) params = params.set('tier_id', tierId);
    if (eventType) params = params.set('event_type', eventType);

    return this.http.get<QuotaEvent[]>(`${this.baseUrl}/events`, { params });
  }
}
```

### State Service

**File:** `frontend/ai.client/src/app/admin/quota/services/quota-state.service.ts`

```typescript
import { Injectable, inject, signal, computed } from '@angular/core';
import { QuotaHttpService } from './quota-http.service';
import { QuotaTier, QuotaAssignment, QuotaOverride } from '../models/quota.models';

@Injectable({
  providedIn: 'root',
})
export class QuotaStateService {
  private http = inject(QuotaHttpService);

  // State
  tiers = signal<QuotaTier[]>([]);
  assignments = signal<QuotaAssignment[]>([]);
  overrides = signal<QuotaOverride[]>([]);
  loading = signal(false);

  // Computed
  enabledTiers = computed(() => this.tiers().filter((t) => t.enabled));
  tierCount = computed(() => this.tiers().length);
  assignmentCount = computed(() => this.assignments().length);

  // ========== Tiers ==========

  loadTiers(enabledOnly: boolean = false): void {
    this.loading.set(true);
    this.http.listTiers(enabledOnly).subscribe({
      next: (tiers) => {
        this.tiers.set(tiers);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  addTier(tier: QuotaTier): void {
    this.tiers.update((tiers) => [...tiers, tier]);
  }

  updateTier(tierId: string, updates: Partial<QuotaTier>): void {
    this.tiers.update((tiers) =>
      tiers.map((t) => (t.tierId === tierId ? { ...t, ...updates } : t))
    );
  }

  removeTier(tierId: string): void {
    this.tiers.update((tiers) => tiers.filter((t) => t.tierId !== tierId));
  }

  // ========== Assignments ==========

  loadAssignments(assignmentType?: string, enabledOnly: boolean = false): void {
    this.loading.set(true);
    this.http.listAssignments(assignmentType, enabledOnly).subscribe({
      next: (assignments) => {
        this.assignments.set(assignments);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  addAssignment(assignment: QuotaAssignment): void {
    this.assignments.update((assignments) => [...assignments, assignment]);
  }

  updateAssignment(assignmentId: string, updates: Partial<QuotaAssignment>): void {
    this.assignments.update((assignments) =>
      assignments.map((a) => (a.assignmentId === assignmentId ? { ...a, ...updates } : a))
    );
  }

  removeAssignment(assignmentId: string): void {
    this.assignments.update((assignments) =>
      assignments.filter((a) => a.assignmentId !== assignmentId)
    );
  }

  // ========== Overrides ==========

  loadOverrides(userId?: string, activeOnly: boolean = false): void {
    this.loading.set(true);
    this.http.listOverrides(userId, activeOnly).subscribe({
      next: (overrides) => {
        this.overrides.set(overrides);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  addOverride(override: QuotaOverride): void {
    this.overrides.update((overrides) => [...overrides, override]);
  }

  removeOverride(overrideId: string): void {
    this.overrides.update((overrides) =>
      overrides.filter((o) => o.overrideId !== overrideId)
    );
  }
}
```

### Sample Page Component

**File:** `frontend/ai.client/src/app/admin/quota/pages/tier-list.page.ts`

```typescript
import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPlusCircle, heroPencil, heroTrash } from '@ng-icons/heroicons/outline';
import { QuotaStateService } from '../services/quota-state.service';
import { QuotaHttpService } from '../services/quota-http.service';
import { QuotaTier } from '../models/quota.models';

@Component({
  selector: 'app-tier-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroPlusCircle, heroPencil, heroTrash })],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="flex justify-between items-center mb-6">
      <h1 class="text-2xl/9 font-bold">Quota Tiers</h1>
      <button
        (click)="createTier()"
        class="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-sm hover:bg-blue-700"
      >
        <ng-icon name="heroPlusCircle" class="size-5" />
        <span>Create Tier</span>
      </button>
    </div>

    @if (state.loading()) {
      <div class="text-center py-8">Loading tiers...</div>
    }

    <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      @for (tier of state.tiers(); track tier.tierId) {
        <div class="border border-gray-200 rounded-sm p-4 dark:border-gray-700">
          <div class="flex justify-between items-start mb-4">
            <div>
              <h3 class="text-lg/7 font-semibold">{{ tier.tierName }}</h3>
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">{{ tier.tierId }}</p>
            </div>
            @if (!tier.enabled) {
              <span class="px-2 py-1 text-xs/5 bg-red-100 text-red-800 rounded-xs dark:bg-red-900 dark:text-red-200">
                Disabled
              </span>
            }
          </div>

          @if (tier.description) {
            <p class="text-sm/6 text-gray-600 mb-4 dark:text-gray-400">{{ tier.description }}</p>
          }

          <div class="space-y-2 mb-4">
            <div class="flex justify-between text-sm/6">
              <span class="text-gray-600 dark:text-gray-400">Monthly Limit:</span>
              <span class="font-medium">\${{ tier.monthlyCostLimit.toFixed(2) }}</span>
            </div>
            @if (tier.dailyCostLimit) {
              <div class="flex justify-between text-sm/6">
                <span class="text-gray-600 dark:text-gray-400">Daily Limit:</span>
                <span class="font-medium">\${{ tier.dailyCostLimit.toFixed(2) }}</span>
              </div>
            }
            <div class="flex justify-between text-sm/6">
              <span class="text-gray-600 dark:text-gray-400">Warning at:</span>
              <span class="font-medium">{{ tier.softLimitPercentage }}%</span>
            </div>
            <div class="flex justify-between text-sm/6">
              <span class="text-gray-600 dark:text-gray-400">Action:</span>
              <span
                [class]="tier.actionOnLimit === 'block' ? 'text-red-600' : 'text-yellow-600'"
              >
                {{ tier.actionOnLimit }}
              </span>
            </div>
          </div>

          <div class="flex gap-2">
            <button
              (click)="editTier(tier)"
              class="flex-1 flex items-center justify-center gap-2 px-3 py-2 border border-gray-300 rounded-sm hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
            >
              <ng-icon name="heroPencil" class="size-4" />
              <span class="text-sm/6">Edit</span>
            </button>
            <button
              (click)="deleteTier(tier)"
              class="flex-1 flex items-center justify-center gap-2 px-3 py-2 border border-red-300 text-red-600 rounded-sm hover:bg-red-50 dark:border-red-600 dark:hover:bg-red-900"
            >
              <ng-icon name="heroTrash" class="size-4" />
              <span class="text-sm/6">Delete</span>
            </button>
          </div>
        </div>
      }
    </div>

    @if (state.tiers().length === 0 && !state.loading()) {
      <div class="text-center py-12 text-gray-500">
        <p class="text-lg/7">No tiers configured</p>
        <p class="text-sm/6">Create your first tier to get started</p>
      </div>
    }
  `,
})
export class TierListPage implements OnInit {
  state = inject(QuotaStateService);
  private http = inject(QuotaHttpService);
  private router = inject(Router);

  ngOnInit(): void {
    this.state.loadTiers();
  }

  createTier(): void {
    this.router.navigate(['/admin/quota/tiers/new']);
  }

  editTier(tier: QuotaTier): void {
    this.router.navigate(['/admin/quota/tiers', tier.tierId, 'edit']);
  }

  deleteTier(tier: QuotaTier): void {
    if (confirm(`Delete tier "${tier.tierName}"?`)) {
      this.http.deleteTier(tier.tierId).subscribe({
        next: () => this.state.removeTier(tier.tierId),
        error: (err) => alert(`Failed to delete tier: ${err.error?.detail || err.message}`),
      });
    }
  }
}
```

---

## Testing Strategy

### Backend Tests

**File:** `backend/tests/quota/test_soft_limits.py`

```python
import pytest
from agents.strands_agent.quota.checker import QuotaChecker
from agents.strands_agent.quota.models import QuotaTier, QuotaCheckResult

@pytest.mark.asyncio
async def test_soft_limit_warning_80_percent(checker, mock_resolver, mock_cost_aggregator):
    """Test that 80% usage triggers warning"""
    user = User(user_id="test", email="test@example.com", roles=[])

    # Mock tier with 80% soft limit
    tier = QuotaTier(
        tier_id="test",
        tier_name="Test",
        monthly_cost_limit=100.0,
        soft_limit_percentage=80.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="test"
    )

    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test",
        tier=tier,
        matched_by="default",
        assignment=mock_assignment
    )

    # Mock 85% usage
    mock_cost_aggregator.get_user_cost_summary.return_value = CostSummary(total_cost=85.0)

    result = await checker.check_quota(user)

    assert result.allowed is True
    assert result.warning_level == "80%"
    assert "Warning" in result.message
    assert result.percentage_used == 85.0
```

### Frontend Tests

**File:** `frontend/ai.client/src/app/admin/quota/services/quota-http.service.spec.ts`

```typescript
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { QuotaHttpService } from './quota-http.service';

describe('QuotaHttpService', () => {
  let service: QuotaHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [QuotaHttpService],
    });

    service = TestBed.inject(QuotaHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should list tiers', () => {
    const mockTiers = [
      { tierId: 'basic', tierName: 'Basic', monthlyCostLimit: 100 },
    ];

    service.listTiers().subscribe((tiers) => {
      expect(tiers.length).toBe(1);
      expect(tiers[0].tierId).toBe('basic');
    });

    const req = httpMock.expectOne((req) => req.url.includes('/api/admin/quota/tiers'));
    expect(req.request.method).toBe('GET');
    req.flush(mockTiers);
  });
});
```

---

## Deployment Plan

### Phase 2 Deployment Steps

#### 1. Backend Deployment

```bash
# 1. Deploy CDK infrastructure (add GSI4)
cd cdk
cdk deploy QuotaStack-dev

# 2. Deploy backend code
cd backend
docker build -t quota-backend:phase2 .
docker push quota-backend:phase2

# 3. Run migrations (if any)
# No DB migrations needed - using DynamoDB

# 4. Verify APIs
curl http://localhost:8000/api/admin/quota/overrides
curl http://localhost:8000/api/admin/quota/events
```

#### 2. Frontend Deployment

```bash
# 1. Build frontend
cd frontend/ai.client
npm run build -- --configuration=production

# 2. Deploy to hosting (S3, CloudFront, etc.)
aws s3 sync dist/ai-client s3://your-bucket/

# 3. Invalidate CDN cache
aws cloudfront create-invalidation --distribution-id XXXXX --paths "/*"
```

#### 3. Verification

```bash
# Test override creation
curl -X POST http://localhost:8000/api/admin/quota/overrides \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "userId": "test123",
    "overrideType": "custom_limit",
    "monthlyCostLimit": 1000.0,
    "validFrom": "2025-12-17T00:00:00Z",
    "validUntil": "2025-12-31T23:59:59Z",
    "reason": "Testing"
  }'

# Test soft limit warning
# (requires user with 85% usage)
```

---

## Validation Criteria

### Phase 2 Completion Checklist

#### ✅ Backend - Overrides

- [ ] Override creation stores to DynamoDB correctly
- [ ] GSI4 (UserOverrideIndex) allows O(1) active override lookup
- [ ] Overrides take priority over all other assignments
- [ ] Expired overrides are ignored
- [ ] Unlimited overrides allow infinite usage

#### ✅ Backend - Soft Limits

- [ ] 80% usage triggers warning event
- [ ] 90% usage triggers warning event
- [ ] Warning events deduplicated within 60 minutes
- [ ] Warnings don't block requests
- [ ] Tier `actionOnLimit=warn` allows over-limit usage with warning

#### ✅ Backend - Email Domains

- [ ] Exact domain match works (e.g., "university.edu")
- [ ] Wildcard subdomain match works (e.g., "*.university.edu")
- [ ] Regex pattern match works (e.g., "regex:^(cs|eng)\\.edu$")
- [ ] Multiple domain match works (e.g., "uni1.edu,uni2.edu")
- [ ] Domain assignments cached separately from user assignments

#### ✅ Frontend - UI

- [ ] Tier list displays all tiers
- [ ] Tier editor allows create/update/delete
- [ ] Assignment list displays all assignments
- [ ] Assignment editor supports all types (user, role, domain)
- [ ] Override list displays active and expired overrides
- [ ] Override editor validates date ranges
- [ ] Quota inspector resolves user quota correctly
- [ ] Event viewer displays recent events with filters

#### ✅ Integration

- [ ] End-to-end test: Create tier → Create assignment → User gets quota
- [ ] End-to-end test: Create override → User quota changes
- [ ] End-to-end test: User hits 85% → Warning event recorded
- [ ] End-to-end test: User hits 100% → Block event recorded

---

**End of Phase 2 Specification**

**Next Steps After Phase 2:**
- Monitor production metrics
- Gather admin feedback on UI
- Optimize cache hit rates
- Consider Phase 3 features (analytics, forecasting, automation)
