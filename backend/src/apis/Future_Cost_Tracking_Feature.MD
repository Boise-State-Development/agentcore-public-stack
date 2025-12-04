Implementation Prompt: Cost Calculation and Aggregation Service
Context
We have successfully implemented the foundational infrastructure for cost tracking in our AWS Bedrock multi-agent conversational AI system. All necessary data is being captured and stored. Now we need to implement the actual cost calculation and aggregation services to enable cost dashboards and billing features.

What's Already Complete ✅
1. Data Capture (100% Complete)
All message metadata is being captured and stored with:

Token usage: inputTokens, outputTokens, cacheReadInputTokens, cacheWriteInputTokens
Model info: modelId, modelName, modelVersion
Attribution: userId, sessionId, timestamp
Latency metrics: timeToFirstToken, endToEndLatency
File locations:

Models: backend/src/apis/app_api/messages/models.py (lines 39-83)
Storage: Messages are stored with embedded metadata in sessions/session_{id}/agents/agent_default/messages/message_N.json
2. Pricing Configuration (Placeholder Complete)
Pricing data structure exists with all AWS Bedrock Claude model pricing:

File: backend/src/apis/app_api/costs/pricing_config.py
Function: get_model_pricing(model_id: str) -> Optional[Dict[str, float]]
Data: BEDROCK_PRICING dictionary with pricing for all Claude models
3. Storage Infrastructure (Complete)
Messages API: backend/src/apis/app_api/messages/service.py
Metadata service: backend/src/apis/app_api/metadata/service.py
Both local file storage and cloud (DynamoDB) storage supported
What Needs to Be Implemented
Phase 1: Cost Calculator Service ⏳
Goal: Create a service that calculates the cost of a single message given its metadata.

File to create: backend/src/apis/app_api/costs/calculator.py

Required classes:

from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from ..messages.models import MessageMetadata

class Cost(BaseModel):
    """Detailed cost breakdown for a message"""
    model_config = ConfigDict(populate_by_name=True)
    
    input_cost: float = Field(..., alias="inputCost", description="Cost of input tokens (USD)")
    output_cost: float = Field(..., alias="outputCost", description="Cost of output tokens (USD)")
    cache_read_cost: float = Field(..., alias="cacheReadCost", description="Cost of cache reads (USD)")
    cache_write_cost: float = Field(..., alias="cacheWriteCost", description="Cost of cache writes (USD)")
    total_cost: float = Field(..., alias="totalCost", description="Total cost (USD)")
    currency: str = Field(default="USD", description="Currency code")
    
    # Optional: Add cost savings from caching
    cache_savings: Optional[float] = Field(None, alias="cacheSavings", description="Savings from cache reads vs regular input (USD)")

class CostCalculator:
    """Service for calculating costs from message metadata"""
    
    def calculate_message_cost(self, metadata: MessageMetadata) -> Optional[Cost]:
        """
        Calculate cost for a single message
        
        Args:
            metadata: MessageMetadata containing token usage and model info
            
        Returns:
            Cost breakdown or None if unable to calculate
            
        Algorithm:
            1. Extract token_usage and model_info from metadata
            2. Get pricing for the model using get_model_pricing()
            3. Calculate:
               - input_cost = (input_tokens / 1_000_000) * input_price_per_mtok
               - output_cost = (output_tokens / 1_000_000) * output_price_per_mtok
               - cache_read_cost = (cache_read_tokens / 1_000_000) * cache_read_price_per_mtok
               - cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_price_per_mtok
               - cache_savings = (cache_read_tokens / 1_000_000) * (input_price - cache_read_price)
            4. Return Cost object with breakdown
        """
        pass

Key implementation notes:

Import get_model_pricing from pricing_config.py
Handle cases where model_info or token_usage is None (return None)
Handle cases where pricing is not found for a model (log warning, return None)
Calculate cache savings to show value of prompt caching
Phase 2: Cost Aggregator Service ⏳
Goal: Aggregate costs across multiple messages for dashboards and billing.

File to create: backend/src/apis/app_api/costs/aggregator.py

Required classes:

from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

class UserCostSummary(BaseModel):
    """Cost summary for a user across a date range"""
    model_config = ConfigDict(populate_by_name=True)
    
    user_id: str = Field(..., alias="userId")
    start_date: str = Field(..., alias="startDate", description="ISO 8601 start date")
    end_date: str = Field(..., alias="endDate", description="ISO 8601 end date")
    total_cost: float = Field(..., alias="totalCost", description="Total cost in USD")
    message_count: int = Field(..., alias="messageCount", description="Number of messages")
    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    cache_read_tokens: int = Field(..., alias="cacheReadTokens")
    cache_savings: float = Field(..., alias="cacheSavings", description="Total savings from caching")
    
    # Breakdown by model
    cost_by_model: Dict[str, float] = Field(..., alias="costByModel", description="Cost per model")

class SessionCostSummary(BaseModel):
    """Cost summary for a conversation session"""
    model_config = ConfigDict(populate_by_name=True)
    
    session_id: str = Field(..., alias="sessionId")
    user_id: str = Field(..., alias="userId")
    total_cost: float = Field(..., alias="totalCost")
    message_count: int = Field(..., alias="messageCount")
    total_tokens: int = Field(..., alias="totalTokens")
    primary_model: str = Field(..., alias="primaryModel", description="Most used model")
    started_at: str = Field(..., alias="startedAt", description="First message timestamp")
    last_message_at: str = Field(..., alias="lastMessageAt", description="Last message timestamp")

class CostAggregator:
    """Service for aggregating costs across messages"""
    
    def __init__(self):
        self.calculator = CostCalculator()
    
    async def get_user_costs(
        self,
        user_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> UserCostSummary:
        """
        Get aggregated costs for a user in a date range
        
        Args:
            user_id: User identifier
            start_date: ISO 8601 start date (optional, defaults to beginning of current month)
            end_date: ISO 8601 end date (optional, defaults to now)
            
        Returns:
            UserCostSummary with aggregated cost data
            
        Algorithm:
            1. Query all sessions for user_id (from local storage or DynamoDB)
            2. For each session, get all messages
            3. Filter messages by date range using metadata.attribution.timestamp
            4. For each message, calculate cost using CostCalculator
            5. Aggregate:
               - Sum total costs
               - Count messages
               - Sum token usage
               - Group costs by model
               - Calculate total cache savings
            6. Return UserCostSummary
        """
        pass
    
    async def get_session_costs(
        self,
        session_id: str
    ) -> SessionCostSummary:
        """
        Get total costs for a conversation session
        
        Args:
            session_id: Session identifier
            
        Returns:
            SessionCostSummary with aggregated cost data
            
        Algorithm:
            1. Get all messages for session using get_messages()
            2. For each message, calculate cost
            3. Aggregate costs and stats
            4. Determine primary model (most frequently used)
            5. Extract first/last message timestamps
            6. Return SessionCostSummary
        """
        pass
    
    async def get_costs_by_date(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        group_by: str = "day"  # "day" or "month"
    ) -> List[Dict[str, any]]:
        """
        Get costs grouped by date for charting
        
        Returns:
            List of {date: str, cost: float} for time-series charts
        """
        pass

Key implementation notes:

Use get_messages() from messages/service.py to retrieve messages
For local storage: Iterate through session directories in sessions/
For cloud storage: Query DynamoDB using GSI on attribution.userId + attribution.timestamp
Parse ISO 8601 timestamps using datetime.fromisoformat()
Group by model name for breakdown
Handle missing metadata gracefully (skip messages without cost data)
Phase 3: Cost API Endpoints ⏳
Goal: Create REST API endpoints for accessing cost data.

File to create: backend/src/apis/app_api/costs/routes.py

Required endpoints:

from fastapi import APIRouter, Query, HTTPException
from typing import Optional

router = APIRouter(prefix="/costs", tags=["costs"])

@router.get("/user/{user_id}")
async def get_user_costs(
    user_id: str,
    start_date: Optional[str] = Query(None, description="ISO 8601 start date"),
    end_date: Optional[str] = Query(None, description="ISO 8601 end date")
) -> UserCostSummary:
    """
    Get cost summary for a user
    
    Example: GET /api/costs/user/user_123?start_date=2025-01-01&end_date=2025-01-31
    """
    aggregator = CostAggregator()
    return await aggregator.get_user_costs(user_id, start_date, end_date)

@router.get("/session/{session_id}")
async def get_session_costs(
    session_id: str
) -> SessionCostSummary:
    """
    Get cost summary for a conversation session
    
    Example: GET /api/costs/session/abc123
    """
    aggregator = CostAggregator()
    return await aggregator.get_session_costs(session_id)

@router.get("/dashboard")
async def get_cost_dashboard(
    user_id: str = Query(...),
    period: str = Query("month", description="'day', 'week', or 'month'")
) -> Dict[str, any]:
    """
    Get cost dashboard data for charts
    
    Returns:
        {
            "summary": UserCostSummary,
            "time_series": List[{date: str, cost: float}],
            "by_model": List[{model: str, cost: float}],
            "top_sessions": List[SessionCostSummary] (top 5 most expensive)
        }
    """
    pass

Integration:

Register router in backend/src/apis/app_api/__init__.py or main FastAPI app
Add CORS configuration if needed
Add authentication/authorization middleware (check user_id matches authenticated user)
Phase 4: Update Messages API (Optional Enhancement) ⏳
Goal: Include calculated cost in the messages API response.

File to modify: backend/src/apis/app_api/messages/service.py

Enhancement:

# In _convert_message() function, add cost calculation:

def _convert_message(msg: Any, metadata: Any = None) -> Message:
    # ... existing code ...
    
    # Calculate cost if metadata exists
    if message_metadata:
        from ..costs.calculator import CostCalculator
        calculator = CostCalculator()
        cost = calculator.calculate_message_cost(message_metadata)
        if cost:
            # Add cost to metadata (extend MessageMetadata model)
            message_metadata.cost = cost
    
    return Message(...)

Requires:

Add cost: Optional[Cost] field to MessageMetadata model
Import Cost and CostCalculator in messages service
Testing Strategy
Unit Tests
File to create: backend/tests/test_costs.py

import pytest
from apis.app_api.costs.calculator import CostCalculator, Cost
from apis.app_api.messages.models import MessageMetadata, TokenUsage, ModelInfo, Attribution

def test_calculate_cost_claude_sonnet():
    """Test cost calculation for Claude Sonnet 4.5"""
    metadata = MessageMetadata(
        token_usage=TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            cache_read_input_tokens=800,
            cache_write_input_tokens=0
        ),
        model_info=ModelInfo(
            model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            model_name="Claude Sonnet 4.5",
            model_version="v1"
        ),
        attribution=Attribution(
            user_id="test_user",
            session_id="test_session",
            timestamp="2025-01-15T10:00:00Z"
        )
    )
    
    calculator = CostCalculator()
    cost = calculator.calculate_message_cost(metadata)
    
    assert cost is not None
    assert cost.input_cost == pytest.approx(0.0006, rel=1e-4)  # (200/1M) * 3.0
    assert cost.cache_read_cost == pytest.approx(0.00024, rel=1e-4)  # (800/1M) * 0.30
    assert cost.output_cost == pytest.approx(0.0075, rel=1e-4)  # (500/1M) * 15.0
    assert cost.total_cost == pytest.approx(0.00834, rel=1e-4)
    assert cost.cache_savings > 0  # Should show savings from cache reads

def test_calculate_cost_missing_model():
    """Test handling of unknown model"""
    metadata = MessageMetadata(
        token_usage=TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500),
        model_info=ModelInfo(
            model_id="unknown.model.id",
            model_name="Unknown Model"
        ),
        attribution=Attribution(
            user_id="test_user",
            session_id="test_session",
            timestamp="2025-01-15T10:00:00Z"
        )
    )
    
    calculator = CostCalculator()
    cost = calculator.calculate_message_cost(metadata)
    
    assert cost is None  # Should return None for unknown model

@pytest.mark.asyncio
async def test_aggregate_session_costs():
    """Test session cost aggregation"""
    aggregator = CostAggregator()
    summary = await aggregator.get_session_costs("test_session")
    
    assert summary.session_id == "test_session"
    assert summary.total_cost > 0
    assert summary.message_count > 0

Integration Tests
Manual testing checklist:

Send a message through the chat API
Verify metadata is stored with model info and attribution
Call /api/costs/user/{user_id} - should return cost summary
Call /api/costs/session/{session_id} - should return session costs
Verify costs match expected values based on token usage and pricing
File Summary
Files to Create:
backend/src/apis/app_api/costs/calculator.py - Cost calculation logic
backend/src/apis/app_api/costs/aggregator.py - Cost aggregation service
backend/src/apis/app_api/costs/routes.py - API endpoints
backend/tests/test_costs.py - Unit tests
Files to Modify:
backend/src/apis/app_api/costs/__init__.py - Export new classes
backend/src/apis/app_api/messages/models.py - Add cost field to MessageMetadata (optional)
backend/src/apis/app_api/messages/service.py - Add cost calculation (optional)
Main FastAPI app - Register costs router
Files Referenced (No Changes):
backend/src/apis/app_api/costs/pricing_config.py - Use get_model_pricing()
backend/src/apis/app_api/messages/service.py - Use get_messages()
backend/src/apis/app_api/storage/paths.py - Use path utilities
Implementation Order
Start with CostCalculator - Core calculation logic (2 hours)
Add unit tests - Verify calculations are correct (1 hour)
Implement CostAggregator - Aggregation across messages (3 hours)
Create API endpoints - REST API (2 hours)
Integration testing - End-to-end verification (1 hour)
Optional: Add cost to messages API - Enhanced response (1 hour)
Total estimated time: ~10 hours

Success Criteria
✅ Can calculate accurate costs for any message with metadata
✅ Can aggregate costs by user for any date range
✅ Can get total cost for any conversation session
✅ API endpoints return correct data
✅ All unit tests pass
✅ Cache savings are calculated and displayed
✅ Handles missing/invalid data gracefully

Additional Context
Current project structure:

backend/src/
├── apis/app_api/
│   ├── costs/
│   │   ├── __init__.py (exists, empty)
│   │   ├── pricing_config.py (exists, complete)
│   │   ├── calculator.py (TO CREATE)
│   │   ├── aggregator.py (TO CREATE)
│   │   └── routes.py (TO CREATE)
│   ├── messages/
│   │   ├── models.py (complete with metadata)
│   │   └── service.py (complete)
│   ├── metadata/
│   │   └── service.py (complete)
│   └── storage/
│       └── paths.py (complete)
└── agents/strands_agent/
    └── streaming/
        └── stream_coordinator.py (captures metadata)

Key dependencies:

Pydantic for models
FastAPI for API endpoints
pytest for testing
Standard library for file I/O and date handling
