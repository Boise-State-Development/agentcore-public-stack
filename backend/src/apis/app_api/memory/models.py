"""Memory API request/response models"""

from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, ConfigDict


class MemoryRecord(BaseModel):
    """A single memory record from AgentCore Memory"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    record_id: Optional[str] = Field(None, alias="recordId", description="Unique identifier for this memory record")
    content: str = Field(..., description="The memory content/text")
    namespace: Optional[str] = Field(None, description="Namespace where this memory is stored")
    relevance_score: Optional[float] = Field(None, alias="relevanceScore", description="Relevance score from semantic search (0-1)")
    created_at: Optional[str] = Field(None, alias="createdAt", description="ISO 8601 timestamp of when the memory was created")
    updated_at: Optional[str] = Field(None, alias="updatedAt", description="ISO 8601 timestamp of last update")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata associated with the memory")


class MemoryStrategy(BaseModel):
    """A memory strategy configured for the memory instance"""
    model_config = ConfigDict(populate_by_name=True, extra='allow')

    strategy_id: str = Field(..., alias="strategyId", description="Strategy identifier")
    strategy_type: str = Field(..., alias="strategyType", description="Type of strategy (user_preference, semantic, summary)")
    namespace: Optional[str] = Field(None, description="Namespace pattern for this strategy")
    status: Optional[str] = Field(None, description="Strategy status")
    config: Optional[Dict[str, Any]] = Field(None, description="Strategy configuration")


class MemoriesResponse(BaseModel):
    """Response containing a list of memory records"""
    model_config = ConfigDict(populate_by_name=True)

    memories: List[MemoryRecord] = Field(..., description="List of memory records")
    namespace: str = Field(..., description="Namespace that was queried")
    query: Optional[str] = Field(None, description="Search query used (if semantic search)")
    total_count: int = Field(..., alias="totalCount", description="Total number of memories returned")


class MemorySearchRequest(BaseModel):
    """Request for semantic memory search"""
    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(..., min_length=1, description="Search query for semantic matching")
    namespace: Optional[str] = Field(None, description="Specific namespace to search (defaults to user's namespace)")
    top_k: int = Field(10, alias="topK", ge=1, le=50, description="Number of results to return")


class StrategiesResponse(BaseModel):
    """Response containing memory strategies"""
    model_config = ConfigDict(populate_by_name=True)

    strategies: List[MemoryStrategy] = Field(..., description="List of configured memory strategies")
    memory_id: str = Field(..., alias="memoryId", description="Memory instance ID")


class DeleteMemoryRequest(BaseModel):
    """Request to delete specific memories"""
    model_config = ConfigDict(populate_by_name=True)

    record_ids: Optional[List[str]] = Field(None, alias="recordIds", description="Specific record IDs to delete")
    namespace: Optional[str] = Field(None, description="Delete all memories in this namespace")
    clear_all: bool = Field(False, alias="clearAll", description="Clear all user memories (preferences and facts)")


class DeleteMemoryResponse(BaseModel):
    """Response after deleting memories"""
    model_config = ConfigDict(populate_by_name=True)

    deleted_count: int = Field(..., alias="deletedCount", description="Number of memories deleted")
    message: str = Field(..., description="Status message")
