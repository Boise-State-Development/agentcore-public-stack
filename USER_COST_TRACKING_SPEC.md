# User Cost Tracking Specification

## Executive Summary

This specification outlines a comprehensive approach to accurately track user inference costs based on model usage, including token caching considerations. The system will capture token usage and pricing data at the point of inference, store it in DynamoDB for production (local files for development), and provide high-performance aggregation capabilities for future quota implementation.

**Production Target**: Scale to 10,000+ monthly active users with sub-100ms query performance.

**Note**: This application has not yet been deployed to production, so no migration strategy is required. All cost tracking features will be implemented as part of the initial production deployment.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Current Infrastructure Analysis](#current-infrastructure-analysis)
3. [Data Models](#data-models)
4. [Cost Capture Strategy](#cost-capture-strategy)
5. [Storage Architecture](#storage-architecture)
6. [Token Caching Considerations](#token-caching-considerations)
7. [Cost Calculation](#cost-calculation)
8. [Aggregation & Querying](#aggregation--querying)
9. [Future: Quota Implementation](#future-quota-implementation)
10. [Implementation Plan](#implementation-plan)

---

## Architecture Overview

### Current Flow

```
User Request
    ↓
FastAPI Endpoint (inference_api/chat/routes.py)
    ↓
get_agent() (chat/service.py) - Creates StrandsAgent with model config
    ↓
StreamCoordinator.stream_response() (streaming/stream_coordinator.py)
    ↓
process_agent_stream() (streaming/stream_processor.py) - Extracts metadata
    ↓
_store_message_metadata() (stream_coordinator.py:146-155) - Stores metadata
    ↓
Storage Layer (DynamoDB in production, local files in development)
```

### Key Capture Points

1. **Model Configuration**: Captured at agent creation (`chat/service.py:99-109`)
2. **Token Usage**: Extracted from stream events (`stream_processor.py:844-1088`)
3. **Pricing Data**: Available from managed models (`admin/models.py:147-168`)
4. **User Attribution**: Available from JWT authentication (`auth/dependencies.py`)

---

## Current Infrastructure Analysis

### Existing Components ✅

#### 1. Token Usage Tracking (Already Implemented)
- **Location**: `backend/src/agents/strands_agent/streaming/stream_processor.py:844-1088`
- **Functionality**: Extracts token usage from model metadata events
- **Data Captured**:
  - `inputTokens` - Standard input tokens
  - `outputTokens` - Standard output tokens
  - `totalTokens` - Sum of input + output
  - `cacheReadInputTokens` - Tokens read from cache (90% discount)
  - `cacheWriteInputTokens` - Tokens written to cache (25% markup)

#### 2. Model Pricing (Partially Implemented)
- **Location**: `backend/src/apis/app_api/admin/models.py:107-168`
- **Managed Model Data**:
  - `input_price_per_million_tokens`
  - `output_price_per_million_tokens`
  - Model metadata (provider, name, id)

**Gap**: No cache pricing in managed models (exists in `costs/pricing_config.py` for Bedrock only)

#### 3. Message Metadata Storage (Already Implemented)
- **Location**: `backend/src/apis/app_api/messages/models.py:74-84`
- **Storage Path**: `sessions/session_{id}/message-metadata.json`
- **Current Structure**:
  ```python
  {
    "latency": { "timeToFirstToken": int, "endToEndLatency": int },
    "token_usage": { "inputTokens": int, "outputTokens": int, ... },
    "model_info": { "modelId": str, "modelName": str, ... },
    "attribution": { "userId": str, "sessionId": str, "timestamp": str }
  }
  ```

**Gap**: Missing `pricing_snapshot` in stored metadata

#### 4. User Authentication (Already Implemented)
- **Location**: `backend/src/apis/shared/auth/dependencies.py`
- **Provides**: `user_id`, `email`, `roles` from JWT

### Missing Components ❌

1. **Cache Pricing in Managed Models**: Need to add cache pricing fields
2. **Pricing Snapshot**: Need to capture pricing at request time
3. **Cost Calculation**: Need service to calculate cost from usage + pricing
4. **User Cost Aggregation**: Need database/service for aggregating user costs
5. **Multi-Provider Pricing**: OpenAI and Gemini pricing not yet configured

---

## Data Models

### 1. Enhanced ManagedModel (Update Required)

**File**: `backend/src/apis/app_api/admin/models.py`

```python
class ManagedModel(BaseModel):
    """Managed model with full details including cache pricing"""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str
    provider_name: str = Field(..., alias="providerName")

    # Token limits
    max_input_tokens: int = Field(..., alias="maxInputTokens")
    max_output_tokens: int = Field(..., alias="maxOutputTokens")

    # Standard pricing
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens")
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens")

    # ✨ NEW: Cache pricing (for providers that support it)
    cache_write_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheWritePricePerMillionTokens",
        description="Price per million tokens written to cache (Bedrock only, ~25% markup)"
    )
    cache_read_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMillionTokens",
        description="Price per million tokens read from cache (Bedrock only, ~90% discount)"
    )

    # Other fields...
    available_to_roles: List[str] = Field(..., alias="availableToRoles")
    enabled: bool
    is_reasoning_model: bool = Field(..., alias="isReasoningModel")
    knowledge_cutoff_date: Optional[str] = Field(None, alias="knowledgeCutoffDate")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")
```

### 2. Enhanced PricingSnapshot (Update Required)

**File**: `backend/src/apis/app_api/messages/models.py`

```python
class PricingSnapshot(BaseModel):
    """Pricing rates at time of request for historical accuracy"""
    model_config = ConfigDict(populate_by_name=True)

    # Standard pricing
    input_price_per_mtok: float = Field(..., alias="inputPricePerMtok")
    output_price_per_mtok: float = Field(..., alias="outputPricePerMtok")

    # ✨ NEW: Cache pricing
    cache_write_price_per_mtok: Optional[float] = Field(
        None,
        alias="cacheWritePricePerMtok",
        description="Cache write pricing (Bedrock only)"
    )
    cache_read_price_per_mtok: Optional[float] = Field(
        None,
        alias="cacheReadPricePerMtok",
        description="Cache read pricing (Bedrock only)"
    )

    currency: str = Field(default="USD")
    snapshot_at: str = Field(..., alias="snapshotAt", description="ISO timestamp when pricing was captured")
```

### 3. Enhanced MessageMetadata (Update Required)

**File**: `backend/src/apis/app_api/messages/models.py`

```python
class MessageMetadata(BaseModel):
    """Metadata associated with a single message"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    latency: Optional[LatencyMetrics] = Field(None)
    token_usage: Optional[TokenUsage] = Field(None, alias="tokenUsage")
    model_info: Optional[ModelInfo] = Field(None, alias="modelInfo")
    attribution: Optional[Attribution] = Field(None)

    # ✨ NEW: Calculated cost (computed from usage + pricing snapshot)
    cost: Optional[float] = Field(
        None,
        description="Total cost in USD for this message (computed from token usage and pricing)"
    )
```

### 4. NEW: UserCostSummary (Create)

**File**: `backend/src/apis/app_api/costs/models.py` (new file)

```python
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any
from datetime import datetime


class CostBreakdown(BaseModel):
    """Detailed cost breakdown by token type"""
    model_config = ConfigDict(populate_by_name=True)

    input_cost: float = Field(..., alias="inputCost", description="Cost from input tokens")
    output_cost: float = Field(..., alias="outputCost", description="Cost from output tokens")
    cache_write_cost: float = Field(0.0, alias="cacheWriteCost", description="Cost from cache writes")
    cache_read_cost: float = Field(0.0, alias="cacheReadCost", description="Cost from cache reads")
    total_cost: float = Field(..., alias="totalCost", description="Total cost (sum of all)")


class ModelCostSummary(BaseModel):
    """Cost summary for a specific model"""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str

    # Token usage
    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_read_tokens: int = Field(0, alias="totalCacheReadTokens")
    total_cache_write_tokens: int = Field(0, alias="totalCacheWriteTokens")

    # Cost
    cost_breakdown: CostBreakdown = Field(..., alias="costBreakdown")

    # Stats
    request_count: int = Field(..., alias="requestCount", description="Number of requests using this model")


class UserCostSummary(BaseModel):
    """Aggregated cost summary for a user"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")

    # Time range
    period_start: str = Field(..., alias="periodStart", description="ISO timestamp of period start")
    period_end: str = Field(..., alias="periodEnd", description="ISO timestamp of period end")

    # Aggregate costs
    total_cost: float = Field(..., alias="totalCost", description="Total cost across all models")

    # Per-model breakdown
    models: list[ModelCostSummary] = Field(
        default_factory=list,
        description="Cost breakdown by model"
    )

    # Overall token usage
    total_requests: int = Field(..., alias="totalRequests")
    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_savings: float = Field(
        0.0,
        alias="totalCacheSavings",
        description="Total cost saved from cache hits"
    )
```

---

## Cost Capture Strategy

### Point of Capture: Stream Coordinator

**Location**: `backend/src/agents/strands_agent/streaming/stream_coordinator.py`

The stream coordinator already stores message metadata after streaming completes. We enhance this to include pricing and cost calculation.

#### Current Flow (Line 134-155)

```python
# Store metadata after flush completes
if message_id is not None:
    # Always update session metadata
    await self._update_session_metadata(...)

    # Store message-level metadata only if we have usage or timing data
    if accumulated_metadata.get("usage") or first_token_time:
        await self._store_message_metadata(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            accumulated_metadata=accumulated_metadata,
            stream_start_time=stream_start_time,
            stream_end_time=stream_end_time,
            first_token_time=first_token_time,
            agent=strands_agent_wrapper
        )
```

#### Enhanced Flow (Proposed)

```python
# Store metadata after flush completes
if message_id is not None:
    # Always update session metadata
    await self._update_session_metadata(...)

    # Store message-level metadata with cost calculation
    if accumulated_metadata.get("usage") or first_token_time:
        # ✨ NEW: Get pricing snapshot at time of request
        pricing_snapshot = await self._get_pricing_snapshot(
            agent=strands_agent_wrapper
        )

        # ✨ NEW: Calculate cost from usage + pricing
        cost = self._calculate_message_cost(
            usage=accumulated_metadata.get("usage", {}),
            pricing=pricing_snapshot
        )

        await self._store_message_metadata(
            session_id=session_id,
            user_id=user_id,
            message_id=message_id,
            accumulated_metadata=accumulated_metadata,
            stream_start_time=stream_start_time,
            stream_end_time=stream_end_time,
            first_token_time=first_token_time,
            agent=strands_agent_wrapper,
            pricing_snapshot=pricing_snapshot,  # ✨ NEW
            cost=cost  # ✨ NEW
        )
```

### Why This Approach?

1. **Accuracy**: Captures pricing at exact time of inference
2. **Single Source of Truth**: Reuses existing metadata storage
3. **Historical Accuracy**: Pricing snapshot allows accurate historical cost calculation even after price changes
4. **Minimal Changes**: Builds on existing infrastructure
5. **Performance**: Cost calculated once at write time, not on every read

---

## Storage Architecture

### Overview

**Development Environment**: Local file storage (existing implementation)
**Production Environment**: DynamoDB with optimized schema for cost tracking

### Local Storage (Development Only)

**Path**: `sessions/session_{id}/message-metadata.json`

**Structure** (Enhanced with cost tracking):
```json
{
  "0": {
    "latency": { "timeToFirstToken": 250, "endToEndLatency": 1500 },
    "tokenUsage": {
      "inputTokens": 1000,
      "outputTokens": 500,
      "totalTokens": 1500,
      "cacheReadInputTokens": 200,
      "cacheWriteInputTokens": 100
    },
    "modelInfo": {
      "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
      "modelName": "Claude 3.5 Sonnet",
      "modelVersion": "v2",
      "pricingSnapshot": {
        "inputPricePerMtok": 3.0,
        "outputPricePerMtok": 15.0,
        "cacheWritePricePerMtok": 3.75,
        "cacheReadPricePerMtok": 0.30,
        "currency": "USD",
        "snapshotAt": "2025-01-15T10:30:00Z"
      }
    },
    "attribution": {
      "userId": "user_123",
      "sessionId": "abc-def-ghi",
      "timestamp": "2025-01-15T10:30:00Z"
    },
    "cost": 0.0234
  }
}
```

**Purpose**: Fast local development without AWS dependencies

---

### Production Storage (DynamoDB)

#### Architecture Overview

**AgentCore Memory** (managed by AWS) handles session and message storage:
- Sessions managed via AgentCore Memory API
- Messages stored in AgentCore Memory
- Accessed via existing endpoints: `GET /sessions`, `GET /sessions/{id}/messages`

**Our Cost Tracking Tables**:
1. **SessionsMetadata** - Message-level metadata (cost, tokens, latency)
2. **UserCostSummary** - Pre-aggregated costs for fast quota checks

**Separation of Concerns**:
- AgentCore Memory = Session/message **content** (what was said)
- SessionsMetadata = Message **metadata** (cost, performance)
- UserCostSummary = Aggregated **cost summaries** (billing, quotas)

**Environment Configuration** (`.env`):
```bash
# Message Metadata Storage (cost tracking per message)
# AgentCore Memory manages sessions/messages, we store additional metadata
DYNAMODB_SESSIONS_METADATA_TABLE_NAME=SessionsMetadata

# Cost Summary Storage (separate table for aggregation)
DYNAMODB_COST_SUMMARY_TABLE_NAME=UserCostSummary  # For quota checks and dashboards
```

---

#### Table 1: SessionsMetadata

**Purpose**: Store message-level metadata (cost, tokens, latency) for messages managed by AgentCore Memory

**Key Concept**:
- Sessions and messages are in AgentCore Memory (AWS managed)
- This table stores **metadata about those messages** (cost tracking)
- Linked via `sessionId` + `messageId` references

**Schema**:

```python
{
    # Primary Key
    "PK": "USER#alice",                    # Partition key
    "SK": "SESSION#abc123#MSG#00005",      # Sort key (session + message reference)

    # References (to AgentCore Memory)
    "userId": "alice",
    "sessionId": "abc123",                 # Links to AgentCore Memory session
    "messageId": 5,                        # Links to AgentCore Memory message
    "timestamp": "2025-01-15T10:30:45.123Z",
    "ttl": 1768118400,                     # Auto-delete after 365 days (matches AgentCore Memory retention)

    # Cost & Usage
    "cost": 0.0234,                        # Decimal
    "inputTokens": 1000,
    "outputTokens": 500,
    "cacheReadTokens": 200,
    "cacheWriteTokens": 100,
    "totalTokens": 1500,

    # Model Info
    "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "modelName": "Claude 3.5 Sonnet",
    "provider": "bedrock",

    # Pricing Snapshot (for historical accuracy)
    "pricingSnapshot": {
        "inputPricePerMtok": 3.0,
        "outputPricePerMtok": 15.0,
        "cacheReadPricePerMtok": 0.30,
        "cacheWritePricePerMtok": 3.75,
        "currency": "USD",
        "snapshotAt": "2025-01-15T10:30:45.123Z"
    },

    # Latency
    "timeToFirstToken": 250,               # milliseconds
    "endToEndLatency": 1500,               # milliseconds

    # Additional metadata
    "organizationId": "org_abc",           # Future: multi-tenant
    "tags": {                              # Future: cost allocation
        "project": "marketing-bot",
        "department": "sales"
    }
}
```

**Indexes**:

**Primary Index**:
- `PK` = `USER#<user_id>` (Partition Key)
- `SK` = `SESSION#<session_id>#MSG#<message_id>` (Sort Key)

**GSI 1: UserTimestampIndex** (for time-range queries)
- `GSI1PK` = `USER#<user_id>` (Partition Key)
- `GSI1SK` = `<timestamp>` (Sort Key)
- **Projection**: ALL
- **Use Cases**:
  - Get all message metadata in date range for cost reports
  - Generate billing period summaries
  - Analytics queries

**GSI 2: ModelUsageIndex** (for model analytics - optional)
- `GSI2PK` = `MODEL#<model_id>` (Partition Key)
- `GSI2SK` = `<timestamp>` (Sort Key)
- **Projection**: KEYS_ONLY + cost, tokens
- **Use Cases**:
  - Track which models are most used
  - Calculate total cost per model across all users
  - Pricing optimization analysis

**Access Patterns**:

```python
# 1. Get message metadata for a specific message
get_item(
    Key={
        "PK": "USER#alice",
        "SK": "SESSION#abc123#MSG#00005"
    }
)

# 2. Get all message metadata for a session
query(
    KeyConditionExpression="PK = :user AND begins_with(SK, :session_prefix)",
    ExpressionAttributeValues={
        ":user": "USER#alice",
        ":session_prefix": "SESSION#abc123#MSG#"
    }
)

# 3. Get user message metadata in date range (via GSI1)
query(
    IndexName="UserTimestampIndex",
    KeyConditionExpression="GSI1PK = :user AND GSI1SK BETWEEN :start AND :end",
    ExpressionAttributeValues={
        ":user": "USER#alice",
        ":start": "2025-01-01T00:00:00Z",
        ":end": "2025-01-31T23:59:59Z"
    }
)

# 4. Write message metadata after streaming completes
put_item(
    Item={
        "PK": "USER#alice",
        "SK": "SESSION#abc123#MSG#00005",
        "userId": "alice",
        "sessionId": "abc123",  # Reference to AgentCore Memory session
        "messageId": 5,         # Reference to AgentCore Memory message
        "cost": 0.0234,
        "inputTokens": 1000,
        "outputTokens": 500,
        # ... all metadata attributes
    }
)

# 5. Integration with existing endpoints
# Sessions are fetched via: GET /sessions (AgentCore Memory)
# Messages are fetched via: GET /sessions/{session_id}/messages (AgentCore Memory)
# Metadata is enriched from this table using sessionId + messageId as keys
```

**Integration with Existing Endpoints**:

The metadata table complements your existing session/message endpoints:

| Endpoint | Data Source | Purpose |
|----------|-------------|---------|
| `GET /sessions` | AgentCore Memory | List user sessions |
| `GET /sessions/{id}/metadata` | AgentCore Memory | Get session metadata (title, preferences) |
| `GET /sessions/{id}/messages` | AgentCore Memory | Get message content |
| `GET /costs/summary` | SessionsMetadata + UserCostSummary | Get cost data (NEW) |

**Enrichment Pattern**:
```python
# Existing: Get messages from AgentCore Memory
messages = await agentcore_memory.get_messages(session_id)

# New: Enrich with cost metadata
for message in messages:
    metadata = await dynamodb.get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": f"SESSION#{session_id}#MSG#{message.id}"
        }
    )
    message.cost = metadata.get("cost")
    message.tokenUsage = metadata.get("tokenUsage")
```

**Performance Characteristics**:
- **Write**: Single-digit millisecond latency
- **Read (single item)**: Single-digit millisecond latency
- **Query (time range)**: 10-50ms for typical user (hundreds of messages)
- **Scalability**: Unlimited (auto-scales with partition key distribution)

---

#### Table 2: UserCostSummary

**Purpose**: Pre-aggregated cost summaries for fast quota checks and dashboards

**Schema**:

```python
{
    # Primary Key
    "PK": "USER#alice",                    # Partition key
    "SK": "PERIOD#2025-01",                # Sort key (YYYY-MM for monthly)

    # Aggregate Costs
    "totalCost": 125.50,                   # Decimal
    "totalRequests": 1234,
    "totalInputTokens": 5000000,
    "totalOutputTokens": 2500000,
    "totalCacheReadTokens": 1000000,
    "totalCacheWriteTokens": 500000,

    # Cache Savings
    "cacheSavings": 15.75,                 # How much saved by caching

    # Per-Model Breakdown
    "modelBreakdown": {
        "claude-sonnet-4-5": {
            "cost": 85.30,
            "requests": 890,
            "inputTokens": 3500000,
            "outputTokens": 1800000
        },
        "claude-haiku-4-5": {
            "cost": 40.20,
            "requests": 344,
            "inputTokens": 1500000,
            "outputTokens": 700000
        }
    },

    # Period Info
    "periodStart": "2025-01-01T00:00:00Z",
    "periodEnd": "2025-01-31T23:59:59Z",
    "lastUpdated": "2025-01-15T10:30:45.123Z",

    # Quota Info (denormalized for fast checks)
    "quotaLimit": 200.00,
    "quotaRemaining": 74.50,
    "quotaPercentUsed": 62.75
}
```

**Indexes**:

**Primary Index**:
- `PK` = `USER#<user_id>` (Partition Key)
- `SK` = `PERIOD#<YYYY-MM>` (Sort Key for monthly) or `PERIOD#<YYYY-MM-DD>` (for daily)

**GSI 1: PeriodIndex** (for admin queries - optional)
- `GSI1PK` = `PERIOD#<YYYY-MM>` (Partition Key)
- `GSI1SK` = `<totalCost>` (Sort Key)
- **Use Cases**:
  - Find top spenders in a period
  - Generate org-wide cost reports

**Access Patterns**:

```python
# 1. Get current month summary (for quota check)
get_item(
    Key={
        "PK": "USER#alice",
        "SK": "PERIOD#2025-01"
    }
)
# Latency: <10ms (single-item read) ✅

# 2. Get user's historical costs
query(
    KeyConditionExpression="PK = :user AND begins_with(SK, :prefix)",
    ExpressionAttributeValues={
        ":user": "USER#alice",
        ":prefix": "PERIOD#"
    },
    ScanIndexForward=False,  # Descending (newest first)
    Limit=12                  # Last 12 months
)

# 3. Update summary (atomic increment)
update_item(
    Key={"PK": "USER#alice", "SK": "PERIOD#2025-01"},
    UpdateExpression="ADD totalCost :cost, totalRequests :one, totalInputTokens :input, totalOutputTokens :output",
    ExpressionAttributeValues={
        ":cost": Decimal("0.0234"),
        ":one": 1,
        ":input": 1000,
        ":output": 500
    }
)
```

**Update Strategy**:

After each request, update the summary table asynchronously:

```python
async def _update_cost_summary(user_id: str, cost: float, usage: dict, timestamp: str):
    """Update pre-aggregated cost summary (async, non-blocking)"""

    # Determine period key
    dt = datetime.fromisoformat(timestamp)
    period_key = f"PERIOD#{dt.strftime('%Y-%m')}"

    # Atomic increment (DynamoDB handles concurrency)
    await dynamodb.update_item(
        TableName="UserCostSummary",
        Key={
            "PK": f"USER#{user_id}",
            "SK": period_key
        },
        UpdateExpression="""
            ADD totalCost :cost,
                totalRequests :one,
                totalInputTokens :input,
                totalOutputTokens :output,
                totalCacheReadTokens :cacheRead,
                totalCacheWriteTokens :cacheWrite
            SET lastUpdated = :now
        """,
        ExpressionAttributeValues={
            ":cost": Decimal(str(cost)),
            ":one": 1,
            ":input": usage.get("inputTokens", 0),
            ":output": usage.get("outputTokens", 0),
            ":cacheRead": usage.get("cacheReadInputTokens", 0),
            ":cacheWrite": usage.get("cacheWriteInputTokens", 0),
            ":now": timestamp
        }
    )

    # Also update per-model breakdown (nested update)
    # Implementation details omitted for brevity
```

**Performance Characteristics**:
- **Quota Check**: <10ms (single `GetItem`)
- **Dashboard Load**: <20ms (query last 12 months)
- **Update**: <10ms (atomic increment, non-blocking)
- **Concurrency**: Handled automatically by DynamoDB

---

### Storage Abstraction Layer

To support both local files (dev) and DynamoDB (prod), implement a storage interface:

**File**: `backend/src/apis/app_api/storage/metadata_storage.py`

```python
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from datetime import datetime


class MetadataStorage(ABC):
    """Abstract interface for message metadata storage"""

    @abstractmethod
    async def store_message_metadata(
        self,
        user_id: str,
        session_id: str,
        message_id: int,
        metadata: Dict[str, Any]
    ) -> None:
        """Store message metadata"""
        pass

    @abstractmethod
    async def get_user_cost_summary(
        self,
        user_id: str,
        period: str  # e.g., "2025-01"
    ) -> Optional[Dict[str, Any]]:
        """Get pre-aggregated cost summary for quota checks"""
        pass

    @abstractmethod
    async def get_user_messages_in_range(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Get all user messages in date range (for detailed reports)"""
        pass


class LocalFileStorage(MetadataStorage):
    """Local file storage for development"""
    # Implementation using existing file-based approach
    pass


class DynamoDBStorage(MetadataStorage):
    """DynamoDB storage for production"""
    # Implementation using boto3 DynamoDB client
    pass


# Factory function
def get_metadata_storage() -> MetadataStorage:
    """Get appropriate storage based on environment"""
    import os

    if os.environ.get("ENVIRONMENT") == "production":
        return DynamoDBStorage()
    else:
        return LocalFileStorage()
```

**Benefits**:
- Developers work locally without AWS
- Production uses scalable DynamoDB
- Easy testing (mock the interface)
- Future-proof (can add other backends)

---

## Token Caching Considerations

### Cache Token Pricing

**Bedrock Models** (Claude via Bedrock):
- **Cache Write**: ~25% markup over input price
- **Cache Read**: ~90% discount from input price

**Example** (Claude Sonnet 4.5):
- Input: $3.00 per million tokens
- Output: $15.00 per million tokens
- Cache Write: $3.75 per million tokens (25% markup)
- Cache Read: $0.30 per million tokens (90% discount)

### Cache Token Detection

Already implemented in `stream_processor.py:881-923`:

```python
# Add cache token fields if present
cache_read = usage_obj.get("cacheReadInputTokens")
if cache_read is None:
    cache_read = usage_obj.get("cache_read_input_tokens")

cache_write = usage_obj.get("cacheWriteInputTokens")
if cache_write is None:
    cache_write = usage_obj.get("cache_write_input_tokens")

# Include cache fields if they exist (even if 0)
if cache_read is not None:
    usage_data["cacheReadInputTokens"] = cache_read
if cache_write is not None:
    usage_data["cacheWriteInputTokens"] = cache_write
```

### Cache Cost Impact

**Without caching**:
```
Cost = (1000 input tokens × $3.00/M) + (500 output tokens × $15.00/M)
     = $0.003 + $0.0075
     = $0.0105
```

**With caching** (200 cache reads, 100 cache writes):
```
Standard input: 1000 - 200 - 100 = 700 tokens
Cache reads: 200 tokens
Cache writes: 100 tokens

Cost = (700 × $3.00/M) + (200 × $0.30/M) + (100 × $3.75/M) + (500 × $15.00/M)
     = $0.0021 + $0.00006 + $0.000375 + $0.0075
     = $0.010035
```

**Savings**: ~4% in this example, but can be much higher with larger cache hits

---

## Cost Calculation

### Service Implementation

**File**: `backend/src/apis/app_api/costs/calculator.py` (new file)

```python
from typing import Dict, Optional
from .models import CostBreakdown


class CostCalculator:
    """Calculate costs from token usage and pricing"""

    @staticmethod
    def calculate_message_cost(
        usage: Dict[str, int],
        pricing: Dict[str, float]
    ) -> tuple[float, CostBreakdown]:
        """
        Calculate cost for a single message

        Args:
            usage: Token usage dict with inputTokens, outputTokens, etc.
            pricing: Pricing dict with inputPricePerMtok, etc.

        Returns:
            Tuple of (total_cost, cost_breakdown)
        """
        # Extract token counts (default to 0 if not present)
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cache_read_tokens = usage.get("cacheReadInputTokens", 0)
        cache_write_tokens = usage.get("cacheWriteInputTokens", 0)

        # Extract pricing (default to 0 if not present)
        input_price = pricing.get("inputPricePerMtok", 0.0)
        output_price = pricing.get("outputPricePerMtok", 0.0)
        cache_read_price = pricing.get("cacheReadPricePerMtok", 0.0)
        cache_write_price = pricing.get("cacheWritePricePerMtok", 0.0)

        # Calculate costs (per million tokens)
        input_cost = (input_tokens / 1_000_000) * input_price
        output_cost = (output_tokens / 1_000_000) * output_price
        cache_read_cost = (cache_read_tokens / 1_000_000) * cache_read_price
        cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_price

        total_cost = input_cost + output_cost + cache_read_cost + cache_write_cost

        breakdown = CostBreakdown(
            inputCost=input_cost,
            outputCost=output_cost,
            cacheReadCost=cache_read_cost,
            cacheWriteCost=cache_write_cost,
            totalCost=total_cost
        )

        return total_cost, breakdown

    @staticmethod
    def calculate_cache_savings(
        cache_read_tokens: int,
        input_price: float,
        cache_read_price: float
    ) -> float:
        """
        Calculate cost savings from cache hits

        Without cache, these tokens would have been charged at input_price.
        With cache, they're charged at cache_read_price.

        Args:
            cache_read_tokens: Number of tokens read from cache
            input_price: Standard input price per million tokens
            cache_read_price: Cache read price per million tokens

        Returns:
            Cost savings in USD
        """
        if cache_read_tokens == 0:
            return 0.0

        standard_cost = (cache_read_tokens / 1_000_000) * input_price
        cache_cost = (cache_read_tokens / 1_000_000) * cache_read_price

        return standard_cost - cache_cost
```

### Integration Point

**File**: `backend/src/agents/strands_agent/streaming/stream_coordinator.py`

Add new methods:

```python
async def _get_pricing_snapshot(self, agent: Any) -> Optional[Dict[str, Any]]:
    """
    Get pricing snapshot from agent's model configuration

    Args:
        agent: StrandsAgent wrapper instance

    Returns:
        Pricing snapshot dict or None if unavailable
    """
    if not agent or not hasattr(agent, 'model_config'):
        return None

    model_config = agent.model_config
    model_id = model_config.model_id

    # Get managed model pricing
    # TODO: Import managed models service
    from apis.app_api.admin.services.managed_models import get_model_by_model_id

    managed_model = await get_model_by_model_id(model_id)
    if not managed_model:
        logger.warning(f"No managed model found for {model_id}")
        return None

    # Create pricing snapshot
    from datetime import datetime, timezone

    snapshot = {
        "inputPricePerMtok": managed_model.input_price_per_million_tokens,
        "outputPricePerMtok": managed_model.output_price_per_million_tokens,
        "currency": "USD",
        "snapshotAt": datetime.now(timezone.utc).isoformat()
    }

    # Add cache pricing if available (Bedrock only)
    if managed_model.cache_write_price_per_million_tokens is not None:
        snapshot["cacheWritePricePerMtok"] = managed_model.cache_write_price_per_million_tokens
    if managed_model.cache_read_price_per_million_tokens is not None:
        snapshot["cacheReadPricePerMtok"] = managed_model.cache_read_price_per_million_tokens

    return snapshot


def _calculate_message_cost(
    self,
    usage: Dict[str, Any],
    pricing: Optional[Dict[str, Any]]
) -> Optional[float]:
    """
    Calculate message cost from usage and pricing

    Args:
        usage: Token usage dict
        pricing: Pricing snapshot dict

    Returns:
        Total cost in USD or None if pricing unavailable
    """
    if not pricing:
        return None

    from apis.app_api.costs.calculator import CostCalculator

    total_cost, _ = CostCalculator.calculate_message_cost(usage, pricing)
    return total_cost
```

---

## Aggregation & Querying

### Service Implementation

**File**: `backend/src/apis/app_api/costs/aggregator.py` (new file)

```python
from datetime import datetime, timezone
from typing import Optional
from decimal import Decimal
import boto3

from .models import UserCostSummary, ModelCostSummary, CostBreakdown
from apis.app_api.storage.metadata_storage import get_metadata_storage


class CostAggregator:
    """Aggregate costs across sessions and time periods"""

    def __init__(self):
        self.storage = get_metadata_storage()

    async def get_user_cost_summary(
        self,
        user_id: str,
        period: str  # e.g., "2025-01" for monthly
    ) -> UserCostSummary:
        """
        Get aggregated cost summary for a user (fast path using pre-aggregated data)

        This method queries the UserCostSummary table for O(1) performance.

        Args:
            user_id: User identifier
            period: Period identifier (YYYY-MM for monthly)

        Returns:
            UserCostSummary with pre-aggregated costs
        """
        # Get pre-aggregated summary from storage
        summary = await self.storage.get_user_cost_summary(user_id, period)

        if not summary:
            # No data for this period, return empty summary
            return self._create_empty_summary(user_id, period)

        # Convert to UserCostSummary model
        return UserCostSummary(
            userId=user_id,
            periodStart=summary["periodStart"],
            periodEnd=summary["periodEnd"],
            totalCost=float(summary["totalCost"]),
            models=self._build_model_summaries(summary.get("modelBreakdown", {})),
            totalRequests=summary["totalRequests"],
            totalInputTokens=summary["totalInputTokens"],
            totalOutputTokens=summary["totalOutputTokens"],
            totalCacheSavings=float(summary.get("cacheSavings", 0.0))
        )

    async def get_detailed_cost_report(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> UserCostSummary:
        """
        Get detailed cost report by querying message-level data

        This method queries the MessageMetadata table for detailed breakdowns.
        Use this for custom date ranges or when detailed per-message data is needed.

        Args:
            user_id: User identifier
            start_date: Start of period
            end_date: End of period

        Returns:
            UserCostSummary with detailed aggregations
        """
        # Query message metadata in date range
        messages = await self.storage.get_user_messages_in_range(
            user_id, start_date, end_date
        )

        # Aggregate from message-level data
        total_cost = 0.0
        total_requests = len(messages)
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_savings = 0.0

        model_stats = {}

        for message in messages:
            # Extract cost and tokens
            cost = float(message.get("cost", 0.0))
            total_cost += cost

            input_tokens = message.get("inputTokens", 0)
            output_tokens = message.get("outputTokens", 0)
            cache_read_tokens = message.get("cacheReadTokens", 0)
            cache_write_tokens = message.get("cacheWriteTokens", 0)

            total_input_tokens += input_tokens
            total_output_tokens += output_tokens

            # Calculate cache savings
            if cache_read_tokens > 0:
                pricing = message.get("pricingSnapshot", {})
                standard_cost = (cache_read_tokens / 1_000_000) * pricing.get("inputPricePerMtok", 0)
                cache_cost = (cache_read_tokens / 1_000_000) * pricing.get("cacheReadPricePerMtok", 0)
                total_cache_savings += (standard_cost - cache_cost)

            # Aggregate per-model
            model_id = message.get("modelId", "unknown")
            if model_id not in model_stats:
                model_stats[model_id] = {
                    "modelName": message.get("modelName", "Unknown"),
                    "provider": message.get("provider", "unknown"),
                    "cost": 0.0,
                    "requests": 0,
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0
                }

            stats = model_stats[model_id]
            stats["cost"] += cost
            stats["requests"] += 1
            stats["inputTokens"] += input_tokens
            stats["outputTokens"] += output_tokens
            stats["cacheReadTokens"] += cache_read_tokens
            stats["cacheWriteTokens"] += cache_write_tokens

        # Build model summaries
        models = []
        for model_id, stats in model_stats.items():
            breakdown = CostBreakdown(
                inputCost=0.0,  # TODO: Store breakdown in metadata
                outputCost=0.0,
                cacheReadCost=0.0,
                cacheWriteCost=0.0,
                totalCost=stats["cost"]
            )

            model_summary = ModelCostSummary(
                modelId=model_id,
                modelName=stats["modelName"],
                provider=stats["provider"],
                totalInputTokens=stats["inputTokens"],
                totalOutputTokens=stats["outputTokens"],
                totalCacheReadTokens=stats["cacheReadTokens"],
                totalCacheWriteTokens=stats["cacheWriteTokens"],
                costBreakdown=breakdown,
                requestCount=stats["requests"]
            )
            models.append(model_summary)

        return UserCostSummary(
            userId=user_id,
            periodStart=start_date.isoformat(),
            periodEnd=end_date.isoformat(),
            totalCost=total_cost,
            models=models,
            totalRequests=total_requests,
            totalInputTokens=total_input_tokens,
            totalOutputTokens=total_output_tokens,
            totalCacheSavings=total_cache_savings
        )

    def _build_model_summaries(self, model_breakdown: dict) -> list:
        """Build ModelCostSummary objects from breakdown dict"""
        models = []
        for model_id, stats in model_breakdown.items():
            breakdown = CostBreakdown(
                inputCost=0.0,  # Stored in summary if needed
                outputCost=0.0,
                cacheReadCost=0.0,
                cacheWriteCost=0.0,
                totalCost=float(stats["cost"])
            )

            models.append(ModelCostSummary(
                modelId=model_id,
                modelName=stats.get("modelName", "Unknown"),
                provider=stats.get("provider", "unknown"),
                totalInputTokens=stats.get("inputTokens", 0),
                totalOutputTokens=stats.get("outputTokens", 0),
                totalCacheReadTokens=stats.get("cacheReadTokens", 0),
                totalCacheWriteTokens=stats.get("cacheWriteTokens", 0),
                costBreakdown=breakdown,
                requestCount=stats.get("requests", 0)
            ))

        return models

    def _create_empty_summary(self, user_id: str, period: str) -> UserCostSummary:
        """Create empty summary for period with no data"""
        return UserCostSummary(
            userId=user_id,
            periodStart=f"{period}-01T00:00:00Z",
            periodEnd=f"{period}-31T23:59:59Z",
            totalCost=0.0,
            models=[],
            totalRequests=0,
            totalInputTokens=0,
            totalOutputTokens=0,
            totalCacheSavings=0.0
        )
```

### API Endpoints

**File**: `backend/src/apis/app_api/costs/routes.py` (new file)

```python
from fastapi import APIRouter, Depends, Query
from datetime import datetime
from typing import Optional

from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from .models import UserCostSummary
from .aggregator import CostAggregator

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("/summary", response_model=UserCostSummary)
async def get_cost_summary(
    period: Optional[str] = Query(None, description="Period (YYYY-MM), defaults to current month"),
    current_user: User = Depends(get_current_user)
):
    """
    Get cost summary for the authenticated user (fast path)

    Uses pre-aggregated UserCostSummary table for <10ms response time.

    Args:
        period: Optional period (YYYY-MM), defaults to current month
        current_user: Authenticated user from JWT

    Returns:
        UserCostSummary with pre-aggregated costs

    Example:
        GET /costs/summary?period=2025-01
    """
    # Default to current month
    if not period:
        period = datetime.utcnow().strftime("%Y-%m")

    # Get pre-aggregated summary (O(1) lookup)
    aggregator = CostAggregator()
    summary = await aggregator.get_user_cost_summary(
        user_id=current_user.user_id,
        period=period
    )

    return summary


@router.get("/detailed-report", response_model=UserCostSummary)
async def get_detailed_report(
    start_date: str = Query(..., description="ISO 8601 start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="ISO 8601 end date (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed cost report for custom date range

    Queries MessageMetadata table for detailed breakdown.
    Use this for custom date ranges or when detailed per-message data is needed.

    Args:
        start_date: Start date (ISO 8601)
        end_date: End date (ISO 8601)
        current_user: Authenticated user from JWT

    Returns:
        UserCostSummary with detailed aggregations

    Example:
        GET /costs/detailed-report?start_date=2025-01-01&end_date=2025-01-15
    """
    # Parse dates
    start = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)

    # Validate date range (max 90 days for performance)
    if (end - start).days > 90:
        raise HTTPException(
            status_code=400,
            detail="Date range cannot exceed 90 days"
        )

    # Get detailed report (queries message-level data)
    aggregator = CostAggregator()
    summary = await aggregator.get_detailed_cost_report(
        user_id=current_user.user_id,
        start_date=start,
        end_date=end
    )

    return summary
```

---

## Future: Quota Implementation

### Quota Models

**File**: `backend/src/apis/app_api/costs/quota_models.py` (future)

```python
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Literal


class UserQuota(BaseModel):
    """User quota configuration"""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")

    # Quota limits
    monthly_cost_limit: float = Field(..., alias="monthlyCostLimit", description="Monthly spend limit in USD")
    daily_cost_limit: Optional[float] = Field(None, alias="dailyCostLimit", description="Daily spend limit in USD")

    # Quota period
    period: Literal["daily", "monthly"] = Field(default="monthly")

    # Actions on limit
    action_on_limit: Literal["block", "warn", "notify"] = Field(
        default="warn",
        alias="actionOnLimit"
    )

    # Current usage
    current_period_cost: float = Field(0.0, alias="currentPeriodCost")
    period_start: str = Field(..., alias="periodStart")
    period_end: str = Field(..., alias="periodEnd")


class QuotaCheckResult(BaseModel):
    """Result of quota check"""
    model_config = ConfigDict(populate_by_name=True)

    allowed: bool = Field(..., description="Whether request is allowed")
    current_usage: float = Field(..., alias="currentUsage", description="Current period usage")
    limit: float = Field(..., description="Quota limit")
    remaining: float = Field(..., description="Remaining quota")
    percentage_used: float = Field(..., alias="percentageUsed", description="Percentage of quota used")
    message: Optional[str] = Field(None, description="Message to display to user")
```

### Pre-Request Quota Check

```python
async def check_quota_before_request(user_id: str) -> QuotaCheckResult:
    """
    Check if user has remaining quota before processing request

    This is a fast check using cached/aggregated data.
    """
    # Get user quota config
    quota = await get_user_quota(user_id)

    # Get current period usage
    aggregator = CostAggregator()
    summary = await aggregator.get_user_cost_summary(
        user_id=user_id,
        start_date=datetime.fromisoformat(quota.period_start),
        end_date=datetime.fromisoformat(quota.period_end)
    )

    current_usage = summary.total_cost
    limit = quota.monthly_cost_limit
    remaining = limit - current_usage
    percentage = (current_usage / limit) * 100 if limit > 0 else 0

    # Determine if allowed
    allowed = True
    message = None

    if quota.action_on_limit == "block" and current_usage >= limit:
        allowed = False
        message = f"Monthly quota exceeded. Limit: ${limit:.2f}, Used: ${current_usage:.2f}"
    elif quota.action_on_limit == "warn" and percentage >= 80:
        message = f"You've used {percentage:.0f}% of your monthly quota (${current_usage:.2f}/${limit:.2f})"

    return QuotaCheckResult(
        allowed=allowed,
        currentUsage=current_usage,
        limit=limit,
        remaining=remaining,
        percentageUsed=percentage,
        message=message
    )
```

---

## Environment Configuration

### Backend Configuration (.env)

Add the following environment variables to `backend/src/.env`:

```bash
# =============================================================================
# DATABASE CONFIGURATION
# =============================================================================

# DynamoDB table for session metadata (message-level cost tracking)
# AgentCore Memory manages sessions and messages in the cloud
# This table stores additional metadata like cost, tokens, latency per message
# Local development uses file storage if not set
DYNAMODB_SESSIONS_METADATA_TABLE_NAME=SessionsMetadata

# DynamoDB table for user cost summaries (separate table)
# Stores pre-aggregated costs for fast quota checks and dashboards
# Required for production cost tracking and quota enforcement
DYNAMODB_COST_SUMMARY_TABLE_NAME=UserCostSummary
```

**Usage in Code**:
```python
import os

# Get table names from environment
SESSIONS_METADATA_TABLE = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "SessionsMetadata")
COST_SUMMARY_TABLE = os.environ.get("DYNAMODB_COST_SUMMARY_TABLE_NAME", "UserCostSummary")

# Use in DynamoDB operations
# Note: Sessions and messages are in AgentCore Memory, NOT DynamoDB
dynamodb.Table(SESSIONS_METADATA_TABLE).put_item(...)  # Store metadata only
dynamodb.Table(COST_SUMMARY_TABLE).get_item(...)       # Get cost summary
```

**Local Development**:
- If `DYNAMODB_SESSIONS_METADATA_TABLE_NAME` is not set → Use local file storage for metadata
- If `DYNAMODB_COST_SUMMARY_TABLE_NAME` is not set → Cost tracking disabled (dev mode)
- Sessions/messages use local AgentCore Memory storage

**Production**:
- Both environment variables MUST be set
- AgentCore Memory handles sessions/messages (AWS managed)
- Our tables handle metadata and cost summaries
- Tables must be created via Infrastructure as Code (CloudFormation/CDK)

---

## Implementation Plan

### Phase 1: Data Model Updates & DynamoDB Setup (Week 1-2)

**Priority: HIGH**

1. **Update Data Models**
   - Add `cache_write_price_per_million_tokens` to ManagedModel
   - Add `cache_read_price_per_million_tokens` to ManagedModel
   - Update `PricingSnapshot` model with cache pricing fields
   - Add `cost` field to MessageMetadata
   - Update admin UI to accept/display cache pricing

2. **Create DynamoDB Tables** (Infrastructure)
   - Create `SessionsMetadata` table (message-level cost/token/latency data)
     - Primary key: PK (partition), SK (sort)
     - GSI 1: UserTimestampIndex (for time-range queries)
     - GSI 2: ModelUsageIndex (optional, for analytics)
     - TTL enabled on `ttl` attribute (365-day retention, matches AgentCore Memory)
     - Links to AgentCore Memory sessions via `sessionId` + `messageId`
   - Create `UserCostSummary` table (separate table for cost aggregation)
     - Primary key: PK (partition), SK (sort)
     - GSI 1: PeriodIndex (optional, for admin queries)
   - Set up IAM permissions for Lambda/ECS (read/write to tables)
   - Set up IAM permissions for AgentCore Memory (already configured)
   - Configure table capacity (on-demand recommended)
   - Add environment variables to deployment configuration

3. **Create Storage Abstraction Layer**
   - Implement `MetadataStorage` interface
   - Implement `LocalFileStorage` (development)
   - Implement `DynamoDBStorage` (production)
   - Add environment-based factory pattern

**Files to Create**:
- `backend/src/apis/app_api/storage/metadata_storage.py`
- `backend/src/apis/app_api/storage/dynamodb_storage.py`
- Infrastructure: CloudFormation/CDK for DynamoDB tables

**Files to Modify**:
- `backend/src/apis/app_api/admin/models.py`
- `backend/src/apis/app_api/messages/models.py`

**Tests**:
- Pydantic model validation tests
- Storage abstraction interface tests
- Mock DynamoDB operations

---

### Phase 2: Cost Calculation & Capture (Week 3)

**Priority: HIGH**

1. **Create Cost Calculator Service**
   - Implement `calculate_message_cost()`
   - Implement `calculate_cache_savings()`
   - Handle multi-provider pricing (Bedrock, OpenAI, Gemini)
   - Add comprehensive unit tests

2. **Create Pricing Service**
   - Implement `get_model_pricing()` with LRU cache
   - Implement `create_pricing_snapshot()`
   - Query managed models efficiently

3. **Integrate into Stream Coordinator**
   - Add `_get_pricing_snapshot()` method
   - Add `_calculate_message_cost()` method
   - Update `_store_message_metadata()` to:
     - Calculate cost from usage + pricing
     - Store to MessageMetadata table (DynamoDB/local files)
     - Update UserCostSummary table (async, atomic increment)
   - Test with real streaming requests

**Files to Create**:
- `backend/src/apis/app_api/costs/calculator.py`
- `backend/src/apis/app_api/costs/pricing_service.py`

**Files to Modify**:
- `backend/src/agents/strands_agent/streaming/stream_coordinator.py`

**Tests**:
- Cost calculation unit tests (various token combinations)
- Cache savings calculation tests
- Integration tests with mocked DynamoDB
- End-to-end streaming tests

---

### Phase 3: Aggregation & API Endpoints (Week 4)

**Priority: HIGH**

1. **Create Cost Aggregator Service**
   - Implement `get_user_cost_summary()` (fast path via UserCostSummary table)
   - Implement `get_detailed_cost_report()` (query MessageMetadata table)
   - Handle date range filtering with GSI
   - Calculate cache savings

2. **Create Cost API Endpoints**
   - `GET /costs/summary?period=YYYY-MM` - Fast pre-aggregated summary
   - `GET /costs/detailed-report?start_date&end_date` - Custom date ranges
   - Add authentication/authorization
   - Add request validation (max date range)

3. **Frontend Cost Dashboard**
   - Create cost summary component
   - Display total costs, per-model breakdown
   - Show cache savings visualization
   - Add period selector (current month, last 30 days, etc.)
   - Real-time cost updates

**Files to Create**:
- `backend/src/apis/app_api/costs/aggregator.py`
- `backend/src/apis/app_api/costs/routes.py`
- `backend/src/apis/app_api/costs/models.py`
- `frontend/ai.client/src/app/costs/` (new feature module)

**Tests**:
- Aggregation logic tests
- API endpoint integration tests
- Frontend component tests

---

### Phase 4: Multi-Provider Pricing & Frontend Forms (Week 5)

**Priority: MEDIUM**

1. **Add OpenAI Pricing**
   - Research current OpenAI pricing (GPT-4, GPT-3.5, etc.)
   - Add to managed models database
   - Update calculator to handle OpenAI-specific pricing
   - No cache pricing for OpenAI (standard input/output only)

2. **Add Gemini Pricing**
   - Research current Gemini pricing
   - Add to managed models database
   - Update calculator to handle Gemini-specific pricing

3. **Update Admin Model Form (Frontend)**
   - **Location**: `frontend/ai.client/src/app/admin/manage-models/new/`
   - **Requirements**:
     - Add cache pricing fields: `cacheReadPricePerMillionTokens`, `cacheWritePricePerMillionTokens`
     - **Show cache fields ONLY when `provider === 'bedrock'`**
     - Hide cache fields for OpenAI and Gemini providers
     - Validate cache pricing fields (must be positive numbers)
     - Update form submission to include cache pricing in API request

   **Form Structure**:
   ```typescript
   interface ModelFormData {
     modelId: string;
     modelName: string;
     provider: 'bedrock' | 'openai' | 'gemini';
     inputPricePerMillionTokens: number;
     outputPricePerMillionTokens: number;

     // Cache pricing (Bedrock only)
     cacheReadPricePerMillionTokens?: number;  // Show if provider === 'bedrock'
     cacheWritePricePerMillionTokens?: number; // Show if provider === 'bedrock'

     // Other fields...
   }
   ```

   **UI Implementation**:
   ```angular
   <!-- Provider Selection -->
   <select formControlName="provider">
     <option value="bedrock">AWS Bedrock</option>
     <option value="openai">OpenAI</option>
     <option value="gemini">Google Gemini</option>
   </select>

   <!-- Standard Pricing (always shown) -->
   <input type="number" formControlName="inputPricePerMillionTokens"
          placeholder="Input price per million tokens" />
   <input type="number" formControlName="outputPricePerMillionTokens"
          placeholder="Output price per million tokens" />

   <!-- Cache Pricing (Bedrock only) -->
   @if (form.value.provider === 'bedrock') {
     <div class="cache-pricing-section">
       <h3>Cache Pricing (Optional)</h3>
       <p class="text-sm">Bedrock supports prompt caching for reduced costs on repeated content.</p>

       <input type="number" formControlName="cacheReadPricePerMillionTokens"
              placeholder="Cache read price (typically ~90% discount from input)" />

       <input type="number" formControlName="cacheWritePricePerMillionTokens"
              placeholder="Cache write price (typically ~25% markup over input)" />
     </div>
   }
   ```

4. **Pricing Management UI**
   - Admin UI to update pricing
   - Show pricing history/changelog
   - Bulk import pricing from CSV/JSON

**Files to Create**:
- `frontend/ai.client/src/app/admin/manage-models/new/model-form.component.ts` (update)
- `frontend/ai.client/src/app/admin/manage-models/new/model-form.component.html` (update)

**Files to Modify**:
- `backend/src/apis/app_api/admin/services/managed_models.py`
- `backend/src/apis/app_api/admin/models.py` (ManagedModel with cache pricing)
- Admin UI components
- Cost calculator (multi-provider support)

**Tests**:
- Multi-provider cost calculation tests
- Admin pricing update tests
- Frontend form validation tests (cache pricing shown/hidden based on provider)

---

### Phase 5: Quota System (Week 6-7 - Optional)

**Priority: LOW (Future Enhancement)**

1. **Create Quota Infrastructure**
   - `UserQuota` model (DynamoDB table)
   - `QuotaCheckResult` model
   - Quota configuration per user/org

2. **Implement Quota Service**
   - `check_quota_before_request()` (<50ms, reads UserCostSummary)
   - `update_quota_usage()` (handled by existing summary updates)
   - Quota reset logic (monthly/daily)
   - Notification triggers (80%, 90%, 100%)

3. **Integrate Quota Checks**
   - Add quota check before streaming starts
   - Block/warn based on quota config
   - Return quota status in API responses

4. **Admin Quota Management**
   - Set user/org quotas
   - View quota usage dashboard
   - Generate quota reports
   - Override quotas for specific users

**Files to Create**:
- `backend/src/apis/app_api/costs/quota_models.py`
- `backend/src/apis/app_api/costs/quota_service.py`
- `backend/src/apis/shared/middleware/quota_middleware.py`
- DynamoDB table for UserQuota

**Tests**:
- Quota check performance tests (<50ms target)
- Middleware integration tests
- Admin UI tests

---

## Performance Characteristics

### Production (DynamoDB)

**Write Performance** (per request):
- Calculate cost: <1ms (pure math)
- Write to MessageMetadata: 5-10ms (single `PutItem`)
- Update UserCostSummary: 5-10ms (atomic `UpdateItem`, async)
- **Total overhead**: ~10-20ms (async, non-blocking for user)

**Read Performance** (quota checks, dashboards):
- Quota check (UserCostSummary `GetItem`): <10ms ✅
- Monthly summary (UserCostSummary `GetItem`): <10ms ✅
- Historical costs (12 months via `Query`): <20ms ✅
- Detailed report (custom date range via GSI): 20-100ms (depends on data volume)

**Scalability**:
- **10,000 users**: Excellent (each user = separate partition key)
- **100,000 users**: Excellent (DynamoDB auto-scales)
- **1,000,000 users**: Excellent (partition key distribution ensures no hot keys)
- **Concurrent writes**: Unlimited (DynamoDB handles automatically)

### Development (Local Files)

**Write Performance**:
- Calculate cost: <1ms
- File write: 5-50ms (depends on session size)
- **Total**: Acceptable for development

**Read Performance**:
- Monthly summary: 10-100ms (file I/O)
- Detailed report: 100-500ms (multiple file reads)
- **Total**: Acceptable for development, not production

**Scalability**:
- Good for < 100 sessions
- Degrades with large session files
- **Production deployment must use DynamoDB**

---

## Security & Privacy

### Data Access Control

- **User Data**: Users can only access their own cost data
- **Admin Data**: Admins can view all user costs (RBAC)
- **Authentication**: JWT-based authentication required

### Pricing Data

- **Visibility**: Pricing data is admin-only by default
- **Transparency**: Users can see their per-request costs
- **Historical Accuracy**: Pricing snapshots prevent retroactive cost changes

### PII Considerations

- Cost data includes `user_id` but not email/name
- Session titles may contain PII - ensure proper access control
- Cost reports should not expose message content

---

## Monitoring & Alerting

### Metrics to Track

1. **Cost Metrics**:
   - Total cost per user (daily, monthly)
   - Cost per model/provider
   - Cache hit rate and savings
   - Average cost per request

2. **Usage Metrics**:
   - Total tokens processed
   - Requests per user
   - Most expensive sessions

3. **System Metrics**:
   - Cost calculation latency
   - Aggregation query time
   - Storage size growth

### Alerts

1. **User Alerts**:
   - 80% quota threshold reached
   - Daily spend anomaly detected
   - Monthly quota exceeded

2. **Admin Alerts**:
   - Overall spend spike
   - Missing pricing for new model
   - Cost calculation failures

---

## Testing Strategy

### Unit Tests

- Cost calculation with various token combinations
- Cache savings calculations
- Pricing snapshot creation
- Aggregation logic

### Integration Tests

- End-to-end streaming with cost capture
- Cost aggregation across multiple sessions
- Multi-provider cost calculations
- Quota enforcement

### Load Tests

- Cost calculation performance (1000 messages)
- Aggregation performance (100 sessions)
- Concurrent quota checks

### Manual Testing Scenarios

1. **Single Request**: Verify cost matches manual calculation
2. **With Caching**: Verify cache tokens reduce cost
3. **Multiple Models**: Switch models mid-session, verify per-model costs
4. **Date Ranges**: Filter costs by various date ranges
5. **Quota Limits**: Test block/warn behaviors

---

## Documentation

### Developer Documentation

- Architecture overview (this spec)
- API endpoint documentation
- Cost calculation examples
- Database schema

### User Documentation

- How costs are calculated
- Understanding cache savings
- Quota system explanation
- Cost dashboard user guide

### Admin Documentation

- Setting up pricing
- Managing user quotas
- Generating cost reports
- Pricing update procedures

---

## Open Questions & Decisions

### 1. Pricing for New Models

**Question**: How do we handle new models before pricing is configured?

**Options**:
- A) Block requests until pricing is added
- B) Allow requests, store tokens, calculate cost later
- C) Use default/estimated pricing with warning

**Recommendation**: Option B - Store usage, calculate when pricing available

---

### 2. Free Tier / Credits

**Question**: Should we support free credits or promotional quotas?

**Options**:
- A) Add `credits` field to user quota
- B) Negative costs for promotional periods
- C) Separate credit tracking system

**Recommendation**: Phase 6 feature, design separately

---

### 3. Cost Rounding

**Question**: How many decimal places for cost values?

**Options**:
- A) Store full precision (float)
- B) Round to cents ($0.01)
- C) Round to 4 decimals ($0.0001)

**Recommendation**: Store full precision, display 4 decimals, round on billing

---

### 4. Aggregation Frequency

**Question**: How often to pre-aggregate costs?

**Options**:
- A) Real-time (calculate on demand)
- B) Hourly (background job)
- C) Daily (midnight UTC)

**Recommendation**: Phase 1 - real-time, Phase 5 - daily pre-aggregation

---

## Success Metrics

### Phase 1-2 (Cost Capture)

- ✅ 100% of streaming requests capture pricing snapshot
- ✅ 100% of messages have calculated cost
- ✅ Cache token costs correctly calculated
- ✅ < 50ms overhead for cost calculation

### Phase 3 (Aggregation)

- ✅ Cost summary API responds in < 1s for typical user
- ✅ Per-model breakdown matches sum of message costs
- ✅ Cache savings accurately calculated

### Phase 5 (Quotas)

- ✅ Quota checks complete in < 100ms
- ✅ Users blocked at quota limit (if configured)
- ✅ Notifications sent at 80% threshold

---

## DynamoDB Best Practices & Cost Optimization

### Table Capacity Planning

**Recommended: On-Demand Mode**
- Auto-scales with traffic
- No capacity planning required
- Pay per request
- Ideal for variable workloads

**Cost Estimate** (10,000 monthly active users):
```
Assumptions:
- 10,000 users × 100 requests/month = 1M requests/month
- Average 2 writes per request (MessageMetadata + UserCostSummary)
- Average 10 reads per user/month (dashboards, quota checks)

Writes: 2M writes × $1.25/M = $2.50/month
Reads: 100K reads × $0.25/M = $0.025/month
Storage: 10GB × $0.25/GB = $2.50/month

Total: ~$5/month for 10K users ✅
```

### Data Retention Strategy

**Recommended: TTL for MessageMetadata**
```python
# Set TTL to auto-delete after 365 days (matches AgentCore Memory retention)
"ttl": int((datetime.utcnow() + timedelta(days=365)).timestamp())
```

**Benefits**:
- Reduces storage costs
- Aligns with AgentCore Memory retention policy (365 days)
- Maintains compliance (GDPR right to deletion)
- Keeps recent data for detailed reports
- UserCostSummary persists indefinitely (small footprint)

### Partition Key Distribution

**Key Design**: `USER#<user_id>`

**Why This Works**:
- Each user = separate partition
- No hot partitions (even distribution)
- Scales linearly with users
- 10K users = 10K partitions ✅

**Avoid**:
- ❌ `PERIOD#2025-01` as PK (hot partition, all users in one key)
- ❌ `MODEL#claude` as PK (hot partition for popular models)

### GSI Optimization

**UserTimestampIndex**:
- Projection: ALL (for flexibility)
- Used infrequently (detailed reports only)
- Most queries use primary index

**Alternative** (if GSI costs become significant):
- Projection: KEYS_ONLY + cost, tokens
- Reduces GSI storage by ~70%
- Requires additional `GetItem` calls for full data

### Monitoring & Alarms

**CloudWatch Metrics**:
```
1. ConsumedReadCapacityUnits (should be low with on-demand)
2. ConsumedWriteCapacityUnits (should be low with on-demand)
3. UserErrors (should be 0)
4. SystemErrors (should be 0)
5. ConditionalCheckFailedRequests (atomic increments may retry)
```

**Alarms**:
- UserErrors > 10/minute → Investigate permissions/throttling
- Average latency > 100ms → Check GSI performance
- Storage > 100GB → Review TTL configuration

---

## Conclusion

This specification provides a production-ready, scalable approach to user cost tracking for 10,000+ users:

### Key Strengths

1. **Accurate Cost Tracking**
   - Captures pricing at inference time (historical accuracy)
   - Handles token caching with proper discount calculations
   - Multi-provider support (Bedrock, OpenAI, Gemini)

2. **High Performance**
   - <10ms quota checks (critical for user experience)
   - <20ms monthly dashboard loads
   - ~10-20ms write overhead (async, non-blocking)
   - Scales to 1M+ users without degradation

3. **Production-Ready Architecture**
   - AgentCore Memory for session/message storage (AWS managed)
   - SessionsMetadata table for cost/token/latency tracking
   - UserCostSummary table for pre-aggregated costs
   - Storage abstraction for local development
   - Atomic updates for concurrent requests
   - Environment-based configuration via `.env`

4. **Cost Efficient**
   - ~$5/month for 10K users
   - TTL-based data retention
   - On-demand capacity (no over-provisioning)
   - Minimal read/write operations

5. **Future-Proof**
   - Foundation for quota enforcement
   - Supports multi-tenant organizations
   - Cost allocation tags
   - Detailed audit trail

### Implementation Timeline

- **Week 1-2**: Data models + DynamoDB setup
- **Week 3**: Cost calculation & capture
- **Week 4**: Aggregation & API endpoints
- **Week 5**: Multi-provider pricing
- **Week 6-7**: Quota system (optional)

### Success Metrics

- ✅ 100% of requests have cost calculated
- ✅ <10ms quota check latency (p99)
- ✅ <20ms dashboard load latency (p99)
- ✅ Support 10,000+ monthly users
- ✅ <$10/month infrastructure cost per 10K users

The phased approach enables incremental delivery while maintaining production quality and scalability from day one.

---

## Architecture Summary

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **AgentCore Memory** for sessions/messages | Managed by AWS, integrated with agent framework, handles conversation storage |
| **Separate metadata table** for cost tracking | Lightweight metadata layer, doesn't duplicate AgentCore Memory data |
| **Separate table** for cost summaries | Enables O(1) quota checks, pre-aggregated data for dashboards |
| **Environment variables** for table names | Flexible deployment, easy configuration, supports multi-environment |
| **Cache pricing** (Bedrock only) | OpenAI/Gemini don't support caching, avoid UI clutter for unsupported features |
| **Storage abstraction layer** | Developers work locally without AWS, production uses DynamoDB seamlessly |
| **Pre-aggregated summaries** | <10ms quota checks critical for user experience |
| **Pricing snapshots** | Historical accuracy even after price changes |
| **TTL on metadata** | Automatic data retention, compliance (GDPR), cost optimization |

### DynamoDB Schema Quick Reference

**SessionsMetadata Table** (metadata only):
```
PK: USER#<user_id>
SK: SESSION#<session_id>#MSG#<msg_id>  → Message metadata (cost, tokens, latency)

Note: Sessions and messages themselves are in AgentCore Memory
This table stores METADATA about those messages
```

**UserCostSummary Table** (separate):
```
PK: USER#<user_id>
SK: PERIOD#<YYYY-MM>  → Monthly cost summary
```

### Environment Variables

```bash
# Message metadata (cost tracking)
DYNAMODB_SESSIONS_METADATA_TABLE_NAME=SessionsMetadata

# Pre-aggregated costs (separate table)
DYNAMODB_COST_SUMMARY_TABLE_NAME=UserCostSummary
```

### Frontend Integration Points

1. **Admin Model Form**: Cache pricing fields (Bedrock only)
2. **Cost Dashboard**: Display user costs and cache savings
3. **Quota Warnings**: Show usage percentage and remaining quota

### Critical Performance Targets

- ✅ Quota check: <10ms (single GetItem)
- ✅ Monthly dashboard: <20ms (single GetItem)
- ✅ Write overhead: ~10-20ms (async, non-blocking)
- ✅ Scale: 10,000+ users without degradation
