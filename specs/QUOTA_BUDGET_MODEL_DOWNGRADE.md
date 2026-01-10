# Quota Budget Model Downgrade Feature Specification

## Overview

This specification describes a new quota enforcement action that automatically downgrades users to a cost-effective "budget model" when they approach their quota limit, with a hard stop at 100%.

**Feature Name:** Budget Model Downgrade
**Status:** Draft
**Created:** 2026-01-05
**Author:** AgentCore Team

---

## Problem Statement

Currently, quota enforcement offers two options:
1. **Block**: Hard stop at 100% - users cannot continue working
2. **Warn**: No enforcement - users can exceed quota indefinitely

Neither option provides a middle ground that:
- Allows users to continue working when approaching limits
- Reduces cost accumulation as users near their quota
- Provides a graceful degradation experience

---

## Proposed Solution

Add a third `action_on_limit` option: **`downgrade`**

When enabled:
- At **90%** (configurable threshold): Automatically switch to a cheaper "budget model"
- At **100%**: Hard stop (same as `block` action)

This allows users to continue working with reduced capabilities while preventing quota overruns.

---

## User Stories

### Admin Stories

1. **As an admin**, I want to configure a quota tier that automatically switches users to a cheaper model when they reach 90% of their quota, so that users can continue working while controlling costs.

2. **As an admin**, I want to specify which budget model to use for downgraded sessions, so that I can balance cost savings with acceptable user experience.

3. **As an admin**, I want to set a custom threshold (e.g., 85%, 90%, 95%) for when the downgrade kicks in, so that I can tune the experience per tier.

### User Stories

1. **As a user**, I want to be notified when I've been downgraded to a budget model, so that I understand why responses may differ.

2. **As a user**, I want to continue chatting even when approaching my quota limit, so that I can complete urgent tasks.

3. **As a user**, I want to see my current quota status and whether I'm in "budget mode", so that I can manage my usage.

---

## Technical Design

### 1. Data Model Changes

#### 1.1 QuotaTier Model (Backend)

**File:** `backend/src/agents/main_agent/quota/models.py`

```python
class QuotaTier(BaseModel):
    # ... existing fields ...

    # Expand action_on_limit to include "downgrade"
    action_on_limit: Literal["block", "warn", "downgrade"] = Field(
        default="block",
        alias="actionOnLimit"
    )

    # New fields for downgrade action
    budget_model_id: Optional[str] = Field(
        None,
        alias="budgetModelId",
        description="Model ID to use when downgrade action triggers. Required if action_on_limit is 'downgrade'."
    )

    downgrade_threshold: Decimal = Field(
        default=Decimal("90.0"),
        alias="downgradeThreshold",
        ge=0,
        lt=100,
        description="Percentage at which to switch to budget model. Must be less than 100."
    )

    @model_validator(mode='after')
    def validate_downgrade_config(self):
        """Ensure budget_model_id is set when action is downgrade"""
        if self.action_on_limit == "downgrade" and not self.budget_model_id:
            raise ValueError("budget_model_id is required when action_on_limit is 'downgrade'")
        return self
```

#### 1.2 QuotaCheckResult Model (Backend)

**File:** `backend/src/agents/main_agent/quota/models.py`

```python
class QuotaCheckResult(BaseModel):
    # ... existing fields ...

    # New fields for downgrade status
    is_downgraded: bool = Field(
        default=False,
        alias="isDowngraded",
        description="True if user has been downgraded to budget model"
    )

    downgrade_model_id: Optional[str] = Field(
        None,
        alias="downgradeModelId",
        description="Budget model ID to use if is_downgraded is True"
    )

    original_model_id: Optional[str] = Field(
        None,
        alias="originalModelId",
        description="The model that would have been used without downgrade"
    )
```

#### 1.3 Frontend TypeScript Models

**File:** `frontend/ai.client/src/app/admin/quota-tiers/models/quota.models.ts`

```typescript
export type ActionOnLimit = 'block' | 'warn' | 'downgrade';

export interface QuotaTier {
  tierId: string;
  tierName: string;
  description?: string;

  // Limits
  monthlyCostLimit: number;
  dailyCostLimit?: number;
  periodType: PeriodType;

  // Soft limits
  softLimitPercentage: number;
  actionOnLimit: ActionOnLimit;

  // Downgrade configuration (new)
  budgetModelId?: string;
  downgradeThreshold?: number;

  // Metadata
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
}

export interface QuotaTierCreate {
  // ... existing fields ...
  budgetModelId?: string;
  downgradeThreshold?: number;
}

export interface QuotaTierUpdate {
  // ... existing fields ...
  budgetModelId?: string;
  downgradeThreshold?: number;
}
```

### 2. DynamoDB Schema Changes

**Table:** `user-quotas` (quota tiers partition)

Add new attributes to tier items:
| Attribute | Type | Description |
|-----------|------|-------------|
| `budgetModelId` | String | Model ID for budget mode (e.g., `us.amazon.nova-micro-v1:0`) |
| `downgradeThreshold` | Number | Percentage threshold (0-99) |

No new GSIs required - existing queries remain unchanged.

### 3. QuotaChecker Logic

**File:** `backend/src/agents/main_agent/quota/checker.py`

```python
async def check_quota(self, user: User, session_id: Optional[str] = None) -> QuotaCheckResult:
    # ... existing resolution and usage lookup ...

    # Handle downgrade action
    if tier.action_on_limit == "downgrade":
        downgrade_threshold = float(tier.downgrade_threshold)

        # At or above 100% → hard block
        if percentage_used >= 100:
            await self.event_recorder.record_block(...)
            return QuotaCheckResult(
                allowed=False,
                message=f"Quota exceeded: ${current_usage:.2f} / ${limit:.2f}",
                tier=tier,
                current_usage=current_usage,
                quota_limit=limit,
                percentage_used=percentage_used,
                remaining=0.0,
                warning_level="100%",
                is_downgraded=False  # Blocked, not downgraded
            )

        # At or above downgrade threshold → use budget model
        if percentage_used >= downgrade_threshold:
            await self.event_recorder.record_downgrade(
                user=user,
                tier=tier,
                current_usage=current_usage,
                limit=limit,
                percentage_used=percentage_used,
                budget_model=tier.budget_model_id,
                session_id=session_id
            )

            return QuotaCheckResult(
                allowed=True,
                message=f"Using budget model (${current_usage:.2f} / ${limit:.2f})",
                tier=tier,
                current_usage=current_usage,
                quota_limit=limit,
                percentage_used=percentage_used,
                remaining=remaining,
                warning_level=f"{int(downgrade_threshold)}%",
                is_downgraded=True,
                downgrade_model_id=tier.budget_model_id
            )

    # ... existing block/warn logic ...
```

### 4. Chat Routes Integration

**File:** `backend/src/apis/app_api/chat/routes.py`

```python
@router.post("/stream")
async def chat_stream(request: ChatRequest, current_user: User = Depends(get_current_user)):
    # ... existing tool filtering ...

    # Check quota
    quota_warning_event = None
    quota_exceeded_event = None
    quota_downgrade_event = None
    model_override = None

    if is_quota_enforcement_enabled():
        try:
            quota_checker = get_quota_checker()
            quota_result = await quota_checker.check_quota(
                user=current_user,
                session_id=request.session_id
            )

            if not quota_result.allowed:
                # Quota exceeded - stream as SSE
                quota_exceeded_event = build_quota_exceeded_event(quota_result)
            elif quota_result.is_downgraded:
                # Downgraded to budget model
                model_override = quota_result.downgrade_model_id
                quota_downgrade_event = build_quota_downgrade_event(quota_result)
                logger.info(
                    f"User {user_id} downgraded to budget model: {model_override} "
                    f"({quota_result.percentage_used:.1f}% quota used)"
                )
            else:
                # Check for warning
                quota_warning_event = build_quota_warning_event(quota_result)
        except Exception as e:
            logger.error(f"Error checking quota: {e}", exc_info=True)

    # ... handle quota_exceeded_event (existing) ...

    # Create agent with potential model override
    agent = get_agent(
        session_id=request.session_id,
        user_id=user_id,
        enabled_tools=authorized_tools,
        model_id=model_override  # None uses default, otherwise uses budget model
    )

    async def stream_with_cleanup():
        # Emit downgrade event first if applicable
        if quota_downgrade_event:
            yield quota_downgrade_event.to_sse_format()

        # Emit warning event if applicable (and not downgraded)
        if quota_warning_event and not quota_downgrade_event:
            yield quota_warning_event.to_sse_format()

        # ... rest of streaming logic ...
```

### 5. SSE Event Types

**File:** `backend/src/apis/shared/quota.py`

```python
class QuotaDowngradeEvent(BaseModel):
    """SSE event for quota-based model downgrade notification"""
    model_config = ConfigDict(populate_by_name=True)

    type: str = "quota_downgrade"
    budget_model_id: str = Field(..., alias="budgetModelId")
    original_model_id: Optional[str] = Field(None, alias="originalModelId")
    current_usage: float = Field(..., alias="currentUsage")
    quota_limit: float = Field(..., alias="quotaLimit")
    percentage_used: float = Field(..., alias="percentageUsed")
    threshold: float = Field(..., description="Downgrade threshold percentage")
    message: str = Field(..., description="User-friendly notification message")

    def to_sse_format(self) -> str:
        """Convert to SSE event format"""
        import json
        return f"event: quota_downgrade\ndata: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"


def build_quota_downgrade_event(result: QuotaCheckResult) -> QuotaDowngradeEvent:
    """Build a quota downgrade SSE event from QuotaCheckResult"""
    percentage = int(result.percentage_used)
    threshold = int(result.tier.downgrade_threshold) if result.tier else 90

    # User-friendly model name mapping
    model_names = {
        "us.amazon.nova-micro-v1:0": "Nova Micro",
        "us.amazon.nova-lite-v1:0": "Nova Lite",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0": "Claude Haiku",
    }

    budget_name = model_names.get(result.downgrade_model_id, result.downgrade_model_id)

    message = (
        f"You've used {percentage}% of your quota. "
        f"Switching to {budget_name} to help conserve your remaining balance."
    )

    return QuotaDowngradeEvent(
        budgetModelId=result.downgrade_model_id,
        originalModelId=result.original_model_id,
        currentUsage=float(result.current_usage),
        quotaLimit=float(result.quota_limit) if result.quota_limit else 0.0,
        percentageUsed=float(result.percentage_used),
        threshold=float(threshold),
        message=message
    )
```

### 6. Event Recording

**File:** `backend/src/agents/main_agent/quota/event_recorder.py`

Add new event type and recording method:

```python
async def record_downgrade(
    self,
    user: User,
    tier: QuotaTier,
    current_usage: float,
    limit: float,
    percentage_used: float,
    budget_model: str,
    session_id: Optional[str] = None,
    assignment_id: Optional[str] = None
) -> None:
    """Record a quota downgrade event"""
    event = QuotaEvent(
        event_id=str(uuid.uuid4()),
        user_id=user.user_id,
        tier_id=tier.tier_id,
        event_type="downgrade",  # New event type
        current_usage=Decimal(str(current_usage)),
        quota_limit=Decimal(str(limit)),
        percentage_used=Decimal(str(percentage_used)),
        timestamp=datetime.utcnow().isoformat(),
        metadata={
            "budget_model": budget_model,
            "threshold": float(tier.downgrade_threshold),
            "session_id": session_id,
            "assignment_id": assignment_id
        }
    )

    await self.repository.create_quota_event(event)
    logger.info(f"Recorded downgrade event for user {user.user_id}: switched to {budget_model}")
```

Update `QuotaEvent.event_type`:
```python
event_type: Literal["warning", "block", "reset", "override_applied", "downgrade"]
```

### 7. Frontend Admin UI

#### 7.1 Tier Detail Form Component

**File:** `frontend/ai.client/src/app/admin/quota-tiers/pages/tier-detail/tier-detail.component.ts`

```typescript
interface TierFormGroup {
  // ... existing controls ...
  actionOnLimit: FormControl<ActionOnLimit>;
  budgetModelId: FormControl<string | null>;
  downgradeThreshold: FormControl<number>;
}

// In component class
readonly tierForm: FormGroup<TierFormGroup> = this.fb.group({
  // ... existing controls ...
  actionOnLimit: this.fb.control<ActionOnLimit>('block', { nonNullable: true }),
  budgetModelId: this.fb.control<string | null>(null),
  downgradeThreshold: this.fb.control(90, {
    nonNullable: true,
    validators: [Validators.min(1), Validators.max(99)]
  }),
});

// Computed signal for showing downgrade options
readonly showDowngradeOptions = computed(() =>
  this.tierForm.controls.actionOnLimit.value === 'downgrade'
);

// Available budget models (could be loaded from API)
readonly budgetModels = signal([
  { id: 'us.amazon.nova-micro-v1:0', name: 'Nova Micro (Cheapest)' },
  { id: 'us.amazon.nova-lite-v1:0', name: 'Nova Lite' },
  { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', name: 'Claude Haiku' },
]);
```

#### 7.2 Tier Detail Template

**File:** `frontend/ai.client/src/app/admin/quota-tiers/pages/tier-detail/tier-detail.component.html`

```html
<!-- Soft Limit & Action Section -->
<div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
  <h2 class="mb-6 text-lg/7 font-semibold text-gray-900 dark:text-white">Soft Limit & Action</h2>

  <div class="space-y-6">
    <!-- Soft Limit Percentage (existing) -->
    <!-- ... -->

    <!-- Action on Limit -->
    <div>
      <label class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
        Action on Limit <span class="text-red-500">*</span>
      </label>
      <div class="mt-3 space-y-3">
        <!-- Block option (existing) -->
        <label class="flex items-start">
          <input
            type="radio"
            formControlName="actionOnLimit"
            value="block"
            class="mt-0.5 size-4 border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50"
          />
          <span class="ml-3">
            <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">Block Requests</span>
            <span class="block text-sm/6 text-gray-600 dark:text-gray-400">
              Prevent users from making requests when they reach 100% of their quota
            </span>
          </span>
        </label>

        <!-- Warn option (existing) -->
        <label class="flex items-start">
          <input
            type="radio"
            formControlName="actionOnLimit"
            value="warn"
            class="mt-0.5 size-4 border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50"
          />
          <span class="ml-3">
            <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">Warn Only</span>
            <span class="block text-sm/6 text-gray-600 dark:text-gray-400">
              Allow requests even at 100%, but record warning events
            </span>
          </span>
        </label>

        <!-- NEW: Downgrade option -->
        <label class="flex items-start">
          <input
            type="radio"
            formControlName="actionOnLimit"
            value="downgrade"
            class="mt-0.5 size-4 border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50"
          />
          <span class="ml-3">
            <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">Downgrade to Budget Model</span>
            <span class="block text-sm/6 text-gray-600 dark:text-gray-400">
              Switch to a cheaper model at threshold, hard stop at 100%
            </span>
          </span>
        </label>
      </div>
    </div>

    <!-- Downgrade Configuration (conditional) -->
    @if (showDowngradeOptions()) {
      <div class="ml-7 space-y-4 border-l-2 border-blue-200 pl-4 dark:border-blue-800">
        <!-- Downgrade Threshold -->
        <div>
          <label for="downgradeThreshold" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Downgrade Threshold (%) <span class="text-red-500">*</span>
          </label>
          <input
            type="number"
            id="downgradeThreshold"
            formControlName="downgradeThreshold"
            min="1"
            max="99"
            class="mt-1 block w-32 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6"
          />
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            Switch to budget model when usage reaches this percentage (1-99)
          </p>
        </div>

        <!-- Budget Model Selection -->
        <div>
          <label for="budgetModelId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Budget Model <span class="text-red-500">*</span>
          </label>
          <select
            id="budgetModelId"
            formControlName="budgetModelId"
            class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6"
          >
            <option [ngValue]="null" disabled>Select a budget model...</option>
            @for (model of budgetModels(); track model.id) {
              <option [value]="model.id">{{ model.name }}</option>
            }
          </select>
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            The model to use when user exceeds the downgrade threshold
          </p>
        </div>

        <!-- Visual explanation -->
        <div class="rounded-sm bg-blue-50 p-3 dark:bg-blue-900/20">
          <p class="text-sm/6 text-blue-800 dark:text-blue-300">
            <strong>How it works:</strong> Users will automatically switch to
            the budget model at {{ tierForm.controls.downgradeThreshold.value }}% usage.
            At 100%, requests will be blocked entirely.
          </p>
        </div>
      </div>
    }
  </div>
</div>
```

### 8. Frontend User Notification

#### 8.1 Stream Parser Updates

**File:** `frontend/ai.client/src/app/session/services/chat/stream-parser.service.ts`

```typescript
// Add signal for downgrade state
readonly quotaDowngrade = signal<QuotaDowngradeEvent | null>(null);

// In parseEvent method
case 'quota_downgrade':
  this.quotaDowngrade.set(data as QuotaDowngradeEvent);
  break;
```

#### 8.2 Downgrade Banner Component

**File:** `frontend/ai.client/src/app/components/quota-downgrade-banner/quota-downgrade-banner.component.ts`

```typescript
@Component({
  selector: 'app-quota-downgrade-banner',
  standalone: true,
  imports: [NgIconComponent],
  template: `
    @if (downgrade()) {
      <div class="flex items-center gap-3 rounded-sm border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-800 dark:bg-amber-900/20">
        <ng-icon name="heroExclamationTriangle" class="size-5 text-amber-600 dark:text-amber-400" />
        <div class="flex-1">
          <p class="text-sm/6 font-medium text-amber-800 dark:text-amber-300">
            {{ downgrade()?.message }}
          </p>
          <p class="text-sm/6 text-amber-700 dark:text-amber-400">
            {{ downgrade()?.percentageUsed | number:'1.0-0' }}% of quota used
          </p>
        </div>
        <button
          (click)="dismiss()"
          class="text-amber-600 hover:text-amber-800 dark:text-amber-400"
          [appTooltip]="'Dismiss'"
        >
          <ng-icon name="heroXMark" class="size-5" />
        </button>
      </div>
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QuotaDowngradeBannerComponent {
  downgrade = input<QuotaDowngradeEvent | null>(null);
  dismissed = output<void>();

  dismiss() {
    this.dismissed.emit();
  }
}
```

---

## API Changes Summary

### New/Modified Endpoints

| Endpoint | Change |
|----------|--------|
| `POST /admin/quota/tiers` | Accept `budgetModelId`, `downgradeThreshold` |
| `PUT /admin/quota/tiers/{id}` | Accept `budgetModelId`, `downgradeThreshold` |
| `GET /admin/quota/tiers` | Return new fields |
| `GET /admin/quota/tiers/{id}` | Return new fields |

### New SSE Event

| Event | When Emitted |
|-------|--------------|
| `quota_downgrade` | User is downgraded to budget model (≥threshold, <100%) |

---

## Migration Plan

### Database Migration

No schema migration required - DynamoDB is schemaless. New attributes will be added to items as tiers are created/updated.

### Backward Compatibility

- Existing tiers with `action_on_limit: "block"` or `"warn"` continue to work unchanged
- New fields (`budgetModelId`, `downgradeThreshold`) are optional for non-downgrade actions
- Frontend gracefully handles missing downgrade fields

### Rollout Strategy

1. **Phase 1**: Deploy backend changes (new fields, QuotaChecker logic)
2. **Phase 2**: Deploy frontend admin UI changes
3. **Phase 3**: Deploy frontend user notification components
4. **Phase 4**: Admin documentation and training

---

## Testing Strategy

### Unit Tests

1. **QuotaTier validation**: Ensure `budgetModelId` required when `action_on_limit == "downgrade"`
2. **QuotaChecker**: Test all branches (below threshold, at threshold, at 100%)
3. **Event recording**: Verify downgrade events are recorded correctly

### Integration Tests

1. **End-to-end downgrade flow**:
   - Create tier with downgrade action
   - Assign to test user
   - Simulate usage at threshold
   - Verify budget model is used
   - Verify SSE event emitted

2. **Admin UI**:
   - Create tier with downgrade config
   - Edit existing tier to add downgrade
   - Validation errors for missing budget model

### Manual Testing Checklist

- [ ] Admin can create tier with downgrade action
- [ ] Admin cannot save downgrade tier without budget model
- [ ] User sees downgrade banner when threshold reached
- [ ] Chat uses budget model after downgrade
- [ ] User is blocked at 100% (not just warned)
- [ ] Quota events table shows downgrade events
- [ ] Quota inspector shows downgrade status

---

## Security Considerations

1. **Model access control**: Ensure budget models are accessible to all users (no additional permissions needed)
2. **Rate limiting**: Downgrade events should be rate-limited to prevent spam
3. **Audit logging**: All downgrade events are recorded for compliance

---

## Performance Considerations

1. **Agent cache**: Downgraded sessions create new cache entries (different model_id)
2. **Event recording**: Async, non-blocking
3. **SSE overhead**: Single additional event, minimal impact

---

## Open Questions

1. **User opt-out?** Should users be able to prefer blocking over downgrade?
2. **Tool restrictions?** Should certain tools be disabled with budget models?
3. **Notification frequency?** Show banner once per session or persistently?
4. **Admin presets?** Provide "suggested" budget models per tier type?

---

## Appendix: Model Cost Reference

| Model ID | Relative Cost | Recommended For |
|----------|---------------|-----------------|
| `us.amazon.nova-micro-v1:0` | $$ (cheapest) | Simple Q&A, summaries |
| `us.amazon.nova-lite-v1:0` | $$$ | General tasks |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | $$$$ | Coding, analysis |
| `us.anthropic.claude-sonnet-4-20250514-v1:0` | $$$$$ | Complex reasoning |

Budget model selection should balance cost savings with acceptable user experience for the tier's intended use case.
