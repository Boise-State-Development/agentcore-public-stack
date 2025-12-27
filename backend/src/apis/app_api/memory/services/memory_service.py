"""Memory retrieval service for AgentCore Memory

This service provides access to user memories stored in AgentCore Memory,
including preferences, facts, and semantic search capabilities.
"""

import logging
from typing import List, Optional, Dict, Any, Tuple
from functools import lru_cache

from agents.strands_agent.session.memory_config import load_memory_config

logger = logging.getLogger(__name__)

# AgentCore Memory integration (optional, only for cloud deployment)
try:
    from bedrock_agentcore.memory import MemoryClient
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False
    MemoryClient = None


def _get_memory_client() -> Optional[Any]:
    """
    Get a MemoryClient instance for AgentCore Memory operations.

    Returns:
        MemoryClient if available and configured, None otherwise
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        logger.warning("AgentCore Memory SDK not available")
        return None

    config = load_memory_config()
    if not config.is_cloud_mode:
        logger.info("Memory is in local mode, AgentCore Memory not available")
        return None

    return MemoryClient(region_name=config.region)


@lru_cache(maxsize=1)
def _get_strategy_namespaces() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Get the actual namespace patterns from configured memory strategies.

    AgentCore Memory stores memories in strategy-specific namespaces like:
    /strategies/{strategyId}/actors/{actorId}

    This function discovers the actual strategy IDs to build correct namespaces.

    Returns:
        Tuple of (semantic_strategy_id, preference_strategy_id, summary_strategy_id)
    """
    client = _get_memory_client()
    if not client:
        return None, None, None

    config = load_memory_config()

    try:
        strategies = client.get_memory_strategies(memory_id=config.memory_id)

        semantic_id = None
        preference_id = None
        summary_id = None

        for strategy in strategies:
            strategy_type = strategy.get('type') or strategy.get('memoryStrategyType')
            strategy_id = strategy.get('strategyId') or strategy.get('memoryStrategyId')

            if strategy_type == 'SEMANTIC':
                semantic_id = strategy_id
            elif strategy_type == 'USER_PREFERENCE':
                preference_id = strategy_id
            elif strategy_type == 'SUMMARIZATION':
                summary_id = strategy_id

        logger.info(f"Discovered strategies - Semantic: {semantic_id}, Preference: {preference_id}, Summary: {summary_id}")
        return semantic_id, preference_id, summary_id

    except Exception as e:
        logger.error(f"Failed to get memory strategies: {e}", exc_info=True)
        return None, None, None


def _build_namespace(strategy_id: str, user_id: str) -> str:
    """Build the full namespace path for a strategy and user."""
    return f"/strategies/{strategy_id}/actors/{user_id}"


def _extract_memory_content(memory: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract and normalize memory content from AgentCore Memory response.

    Args:
        memory: Raw memory record from AgentCore Memory

    Returns:
        Normalized memory record with consistent field names
    """
    # Extract the text content
    content = memory.get('content', {})
    if isinstance(content, dict):
        text = content.get('text', str(content))
    else:
        text = str(content)

    return {
        'recordId': memory.get('memoryRecordId'),
        'content': text,
        'namespace': memory.get('namespaces', [None])[0] if memory.get('namespaces') else None,
        'relevanceScore': memory.get('score'),
        'createdAt': str(memory.get('createdAt')) if memory.get('createdAt') else None,
        'strategyId': memory.get('memoryStrategyId'),
    }


async def get_user_preferences(
    user_id: str,
    query: Optional[str] = None,
    top_k: int = 10
) -> List[Dict[str, Any]]:
    """
    Retrieve user preferences from AgentCore Memory.

    Args:
        user_id: User identifier
        query: Optional search query for semantic matching
        top_k: Number of results to return

    Returns:
        List of preference memory records
    """
    client = _get_memory_client()
    if not client:
        return []

    config = load_memory_config()
    _, preference_strategy_id, _ = _get_strategy_namespaces()

    if not preference_strategy_id:
        logger.warning("No USER_PREFERENCE strategy found")
        return []

    namespace = _build_namespace(preference_strategy_id, user_id)
    search_query = query or "user preferences settings behavior"

    try:
        logger.info(f"Retrieving preferences for user {user_id} from namespace {namespace}")
        memories = client.retrieve_memories(
            memory_id=config.memory_id,
            namespace=namespace,
            query=search_query,
            top_k=top_k
        )
        logger.info(f"Retrieved {len(memories)} preference memories")
        return [_extract_memory_content(m) for m in memories]
    except Exception as e:
        logger.error(f"Failed to retrieve preferences: {e}", exc_info=True)
        return []


async def get_user_facts(
    user_id: str,
    query: Optional[str] = None,
    top_k: int = 10
) -> List[Dict[str, Any]]:
    """
    Retrieve user facts from AgentCore Memory.

    Args:
        user_id: User identifier
        query: Optional search query for semantic matching
        top_k: Number of results to return

    Returns:
        List of fact memory records
    """
    client = _get_memory_client()
    if not client:
        return []

    config = load_memory_config()
    semantic_strategy_id, _, _ = _get_strategy_namespaces()

    if not semantic_strategy_id:
        logger.warning("No SEMANTIC strategy found")
        return []

    namespace = _build_namespace(semantic_strategy_id, user_id)
    search_query = query or "information facts knowledge"

    try:
        logger.info(f"Retrieving facts for user {user_id} from namespace {namespace}")
        memories = client.retrieve_memories(
            memory_id=config.memory_id,
            namespace=namespace,
            query=search_query,
            top_k=top_k
        )
        logger.info(f"Retrieved {len(memories)} fact memories")
        return [_extract_memory_content(m) for m in memories]
    except Exception as e:
        logger.error(f"Failed to retrieve facts: {e}", exc_info=True)
        return []


async def get_session_summaries(
    user_id: str,
    session_id: str,
    query: Optional[str] = None,
    top_k: int = 10
) -> List[Dict[str, Any]]:
    """
    Retrieve session summaries from AgentCore Memory.

    Session summaries are condensed representations of conversation content,
    capturing key topics, decisions, and outcomes from a specific session.

    Args:
        user_id: User identifier
        session_id: Session identifier
        query: Optional search query for semantic matching
        top_k: Number of results to return

    Returns:
        List of summary memory records
    """
    client = _get_memory_client()
    if not client:
        return []

    config = load_memory_config()
    _, _, summary_strategy_id = _get_strategy_namespaces()

    if not summary_strategy_id:
        logger.warning("No SUMMARY strategy found")
        return []

    # Summary namespace includes session_id since summaries are per-session
    namespace = f"/strategies/{summary_strategy_id}/actors/{user_id}/sessions/{session_id}"
    search_query = query or "conversation summary topics decisions"

    try:
        logger.info(f"Retrieving summaries for user {user_id}, session {session_id} from namespace {namespace}")
        memories = client.retrieve_memories(
            memory_id=config.memory_id,
            namespace=namespace,
            query=search_query,
            top_k=top_k
        )
        logger.info(f"Retrieved {len(memories)} summary memories")
        return [_extract_memory_content(m) for m in memories]
    except Exception as e:
        logger.error(f"Failed to retrieve summaries: {e}", exc_info=True)
        return []


async def search_memories(
    user_id: str,
    query: str,
    namespace: Optional[str] = None,
    top_k: int = 10
) -> List[Dict[str, Any]]:
    """
    Semantic search across user memories.

    Args:
        user_id: User identifier
        query: Search query for semantic matching
        namespace: Specific namespace to search (defaults to facts namespace)
        top_k: Number of results to return

    Returns:
        List of matching memory records
    """
    client = _get_memory_client()
    if not client:
        return []

    config = load_memory_config()
    semantic_strategy_id, _, _ = _get_strategy_namespaces()

    # Default to semantic/facts namespace if not specified
    if namespace is None and semantic_strategy_id:
        namespace = _build_namespace(semantic_strategy_id, user_id)
    elif namespace and not namespace.startswith("/strategies"):
        # Legacy namespace format - try to use it directly
        pass

    if not namespace:
        logger.warning("No valid namespace for search")
        return []

    try:
        logger.info(f"Searching memories for user {user_id} in namespace {namespace}")
        memories = client.retrieve_memories(
            memory_id=config.memory_id,
            namespace=namespace,
            query=query,
            top_k=top_k
        )
        logger.info(f"Found {len(memories)} matching memories")
        return [_extract_memory_content(m) for m in memories]
    except Exception as e:
        logger.error(f"Failed to search memories: {e}", exc_info=True)
        return []


async def get_memory_strategies() -> List[Dict[str, Any]]:
    """
    Get all configured memory strategies.

    Returns:
        List of strategy configurations
    """
    client = _get_memory_client()
    if not client:
        return []

    config = load_memory_config()

    try:
        logger.info(f"Getting memory strategies for memory {config.memory_id}")
        strategies = client.get_memory_strategies(memory_id=config.memory_id)
        logger.info(f"Retrieved {len(strategies)} strategies")
        return strategies
    except Exception as e:
        logger.error(f"Failed to get memory strategies: {e}", exc_info=True)
        return []


async def get_all_user_memories(
    user_id: str,
    session_id: Optional[str] = None,
    top_k: int = 20
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get all memories for a user across all namespaces.

    Args:
        user_id: User identifier
        session_id: Optional session identifier for retrieving session summaries
        top_k: Number of results per namespace

    Returns:
        Dictionary with 'preferences', 'facts', and optionally 'summaries' keys containing memory lists
    """
    preferences = await get_user_preferences(user_id, top_k=top_k)
    facts = await get_user_facts(user_id, top_k=top_k)

    result = {
        "preferences": preferences,
        "facts": facts
    }

    # Include session summaries if session_id is provided
    if session_id:
        summaries = await get_session_summaries(user_id, session_id, top_k=top_k)
        result["summaries"] = summaries

    return result


def is_memory_available() -> bool:
    """
    Check if AgentCore Memory is available and configured.

    Returns:
        True if memory is available, False otherwise
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        return False

    try:
        config = load_memory_config()
        return config.is_cloud_mode
    except Exception:
        return False


def get_memory_config_info() -> Dict[str, Any]:
    """
    Get information about the current memory configuration.

    Returns:
        Dictionary with memory configuration details
    """
    try:
        config = load_memory_config()
        semantic_id, preference_id, summary_id = _get_strategy_namespaces()

        return {
            "available": AGENTCORE_MEMORY_AVAILABLE and config.is_cloud_mode,
            "mode": "cloud" if config.is_cloud_mode else "local",
            "memory_id": config.memory_id if config.is_cloud_mode else None,
            "region": config.region,
            "strategies": {
                "semantic": semantic_id,
                "preference": preference_id,
                "summary": summary_id
            },
            "namespaces": {
                "preferences": f"/strategies/{preference_id}/actors/{{userId}}" if preference_id else None,
                "facts": f"/strategies/{semantic_id}/actors/{{userId}}" if semantic_id else None,
                "summaries": f"/strategies/{summary_id}/actors/{{userId}}/sessions/{{sessionId}}" if summary_id else None
            }
        }
    except Exception as e:
        return {
            "available": False,
            "mode": "unknown",
            "error": str(e)
        }


async def delete_memory(
    user_id: str,
    record_id: str,
    namespace: Optional[str] = None
) -> bool:
    """
    Delete a specific memory record from AgentCore Memory.

    Uses boto3 directly since the MemoryClient SDK wrapper doesn't expose delete methods.

    Args:
        user_id: User identifier
        record_id: The memory record ID to delete
        namespace: Optional namespace (not currently used - boto3 API doesn't require it)

    Returns:
        True if deletion was successful, False otherwise
    """
    import boto3

    config = load_memory_config()
    if not config.is_cloud_mode:
        logger.warning("Cannot delete memory in local mode")
        return False

    try:
        # Use boto3 directly since MemoryClient doesn't expose delete methods
        client = boto3.client('bedrock-agentcore', region_name=config.region)

        logger.info(f"Attempting to delete memory record {record_id} from memory {config.memory_id}")

        # Use batch_delete_memory_records API
        response = client.batch_delete_memory_records(
            memoryId=config.memory_id,
            records=[{'memoryRecordId': record_id}]
        )

        # Check if deletion was successful
        successful = response.get('successfulRecords', [])
        failed = response.get('failedRecords', [])

        if successful:
            logger.info(f"Successfully deleted memory record {record_id}")
            return True
        elif failed:
            error_msg = failed[0].get('errorMessage', 'Unknown error')
            logger.warning(f"Failed to delete memory record {record_id}: {error_msg}")
            return False
        else:
            logger.info(f"Delete request processed for {record_id}")
            return True

    except Exception as e:
        logger.error(f"Failed to delete memory {record_id}: {e}", exc_info=True)
        return False
