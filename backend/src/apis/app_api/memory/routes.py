"""Memory API routes

Provides endpoints for retrieving and managing user memories from AgentCore Memory.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
import logging

from .models import (
    MemoryRecord,
    MemoriesResponse,
    MemorySearchRequest,
    StrategiesResponse,
    MemoryStrategy,
    DeleteMemoryResponse,
)
from .services.memory_service import (
    get_user_preferences,
    get_user_facts,
    search_memories,
    get_memory_strategies,
    get_all_user_memories,
    is_memory_available,
    get_memory_config_info,
    delete_memory,
)
from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/status")
async def get_memory_status(
    current_user: User = Depends(get_current_user)
):
    """
    Get the current status and configuration of AgentCore Memory.

    Returns information about whether memory is available, the current mode,
    and configured namespaces.

    Requires JWT authentication.
    """
    logger.info(f"GET /memory/status - User: {current_user.user_id}")

    config_info = get_memory_config_info()

    return {
        "status": "available" if config_info.get("available") else "unavailable",
        **config_info
    }


@router.get("/preferences", response_model=MemoriesResponse, response_model_exclude_none=True)
async def get_preferences_endpoint(
    query: Optional[str] = Query(None, description="Optional search query for semantic matching"),
    top_k: int = Query(10, ge=1, le=50, alias="topK", description="Number of results to return"),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve learned user preferences from AgentCore Memory.

    Preferences are behavioral patterns learned from conversations, such as:
    - "User prefers concise responses"
    - "User likes code examples in Python"
    - "User works in US Eastern timezone"

    Requires JWT authentication. Returns only preferences for the authenticated user.

    Args:
        query: Optional search query for semantic matching
        top_k: Number of results to return (1-50)

    Returns:
        MemoriesResponse with list of preference memories
    """
    user_id = current_user.user_id

    logger.info(f"GET /memory/preferences - User: {user_id}, Query: {query}, TopK: {top_k}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        memories = await get_user_preferences(user_id, query=query, top_k=top_k)

        # Convert to MemoryRecord models
        memory_records = []
        for mem in memories:
            record = MemoryRecord(
                record_id=mem.get("record_id") or mem.get("recordId"),
                content=mem.get("content") or mem.get("text") or str(mem),
                namespace=f"/preferences/{user_id}",
                relevance_score=mem.get("relevance_score") or mem.get("score"),
                created_at=mem.get("created_at") or mem.get("createdAt"),
                updated_at=mem.get("updated_at") or mem.get("updatedAt"),
                metadata=mem.get("metadata")
            )
            memory_records.append(record)

        return MemoriesResponse(
            memories=memory_records,
            namespace=f"/preferences/{user_id}",
            query=query,
            total_count=len(memory_records)
        )

    except Exception as e:
        logger.error(f"Error retrieving preferences: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve preferences: {str(e)}"
        )


@router.get("/facts", response_model=MemoriesResponse, response_model_exclude_none=True)
async def get_facts_endpoint(
    query: Optional[str] = Query(None, description="Optional search query for semantic matching"),
    top_k: int = Query(10, ge=1, le=50, alias="topK", description="Number of results to return"),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve learned facts about the user from AgentCore Memory.

    Facts are information learned from conversations, such as:
    - "User is a software engineer at Acme Corp"
    - "User is building a React dashboard"
    - "User's project uses PostgreSQL"

    Requires JWT authentication. Returns only facts for the authenticated user.

    Args:
        query: Optional search query for semantic matching
        top_k: Number of results to return (1-50)

    Returns:
        MemoriesResponse with list of fact memories
    """
    user_id = current_user.user_id

    logger.info(f"GET /memory/facts - User: {user_id}, Query: {query}, TopK: {top_k}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        memories = await get_user_facts(user_id, query=query, top_k=top_k)

        # Convert to MemoryRecord models
        memory_records = []
        for mem in memories:
            record = MemoryRecord(
                record_id=mem.get("record_id") or mem.get("recordId"),
                content=mem.get("content") or mem.get("text") or str(mem),
                namespace=f"/facts/{user_id}",
                relevance_score=mem.get("relevance_score") or mem.get("score"),
                created_at=mem.get("created_at") or mem.get("createdAt"),
                updated_at=mem.get("updated_at") or mem.get("updatedAt"),
                metadata=mem.get("metadata")
            )
            memory_records.append(record)

        return MemoriesResponse(
            memories=memory_records,
            namespace=f"/facts/{user_id}",
            query=query,
            total_count=len(memory_records)
        )

    except Exception as e:
        logger.error(f"Error retrieving facts: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve facts: {str(e)}"
        )


@router.get("", response_model_exclude_none=True)
async def get_all_memories_endpoint(
    top_k: int = Query(20, ge=1, le=50, alias="topK", description="Number of results per category"),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve all memories for the authenticated user.

    Returns both preferences and facts in a single response.

    Requires JWT authentication.

    Args:
        top_k: Number of results per category (1-50)

    Returns:
        Object with 'preferences' and 'facts' arrays
    """
    user_id = current_user.user_id

    logger.info(f"GET /memory - User: {user_id}, TopK: {top_k}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        all_memories = await get_all_user_memories(user_id, top_k=top_k)

        # Convert to response format
        def convert_memories(memories, namespace):
            records = []
            for mem in memories:
                record = MemoryRecord(
                    record_id=mem.get("record_id") or mem.get("recordId"),
                    content=mem.get("content") or mem.get("text") or str(mem),
                    namespace=namespace,
                    relevance_score=mem.get("relevance_score") or mem.get("score"),
                    created_at=mem.get("created_at") or mem.get("createdAt"),
                    updated_at=mem.get("updated_at") or mem.get("updatedAt"),
                    metadata=mem.get("metadata")
                )
                records.append(record)
            return records

        preferences = convert_memories(
            all_memories.get("preferences", []),
            f"/preferences/{user_id}"
        )
        facts = convert_memories(
            all_memories.get("facts", []),
            f"/facts/{user_id}"
        )

        return {
            "preferences": {
                "memories": [p.model_dump(by_alias=True, exclude_none=True) for p in preferences],
                "namespace": f"/preferences/{user_id}",
                "totalCount": len(preferences)
            },
            "facts": {
                "memories": [f.model_dump(by_alias=True, exclude_none=True) for f in facts],
                "namespace": f"/facts/{user_id}",
                "totalCount": len(facts)
            }
        }

    except Exception as e:
        logger.error(f"Error retrieving all memories: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve memories: {str(e)}"
        )


@router.post("/search", response_model=MemoriesResponse, response_model_exclude_none=True)
async def search_memories_endpoint(
    request: MemorySearchRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Semantic search across user memories.

    Performs a semantic search to find memories matching the query.
    Defaults to searching the facts namespace.

    Requires JWT authentication.

    Args:
        request: Search request with query, optional namespace, and top_k

    Returns:
        MemoriesResponse with matching memories
    """
    user_id = current_user.user_id

    logger.info(f"POST /memory/search - User: {user_id}, Query: {request.query}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        memories = await search_memories(
            user_id,
            query=request.query,
            namespace=request.namespace,
            top_k=request.top_k
        )

        # Determine namespace used
        namespace = request.namespace or f"/facts/{user_id}"
        if not namespace.startswith("/"):
            namespace = f"/{namespace}/{user_id}"

        # Convert to MemoryRecord models
        memory_records = []
        for mem in memories:
            record = MemoryRecord(
                record_id=mem.get("record_id") or mem.get("recordId"),
                content=mem.get("content") or mem.get("text") or str(mem),
                namespace=namespace,
                relevance_score=mem.get("relevance_score") or mem.get("score"),
                created_at=mem.get("created_at") or mem.get("createdAt"),
                updated_at=mem.get("updated_at") or mem.get("updatedAt"),
                metadata=mem.get("metadata")
            )
            memory_records.append(record)

        return MemoriesResponse(
            memories=memory_records,
            namespace=namespace,
            query=request.query,
            total_count=len(memory_records)
        )

    except Exception as e:
        logger.error(f"Error searching memories: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to search memories: {str(e)}"
        )


@router.get("/strategies", response_model=StrategiesResponse, response_model_exclude_none=True)
async def get_strategies_endpoint(
    current_user: User = Depends(get_current_user)
):
    """
    Get configured memory strategies.

    Returns the list of memory strategies configured for the AgentCore Memory instance.
    Strategies define how memories are extracted and organized.

    Requires JWT authentication.

    Returns:
        StrategiesResponse with list of strategies
    """
    user_id = current_user.user_id

    logger.info(f"GET /memory/strategies - User: {user_id}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        strategies = await get_memory_strategies()
        config_info = get_memory_config_info()

        # Convert to MemoryStrategy models
        strategy_records = []
        for strat in strategies:
            record = MemoryStrategy(
                strategy_id=strat.get("strategy_id") or strat.get("strategyId") or "unknown",
                strategy_type=strat.get("strategy_type") or strat.get("strategyType") or strat.get("type") or "unknown",
                namespace=strat.get("namespace"),
                status=strat.get("status"),
                config=strat.get("config") or strat.get("configuration")
            )
            strategy_records.append(record)

        return StrategiesResponse(
            strategies=strategy_records,
            memory_id=config_info.get("memory_id") or "unknown"
        )

    except Exception as e:
        logger.error(f"Error retrieving strategies: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve strategies: {str(e)}"
        )


@router.delete("/{record_id}", response_model=DeleteMemoryResponse)
async def delete_memory_endpoint(
    record_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a specific memory record.

    Deletes a memory record by its ID. The memory must belong to the authenticated user.

    Requires JWT authentication.

    Args:
        record_id: The ID of the memory record to delete

    Returns:
        DeleteMemoryResponse with deletion status
    """
    user_id = current_user.user_id

    logger.info(f"DELETE /memory/{record_id} - User: {user_id}")

    if not is_memory_available():
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory is not available. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured."
        )

    try:
        success = await delete_memory(user_id, record_id)

        if success:
            return DeleteMemoryResponse(
                deleted_count=1,
                message=f"Successfully deleted memory {record_id}"
            )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Memory record {record_id} not found or could not be deleted"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting memory: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete memory: {str(e)}"
        )
