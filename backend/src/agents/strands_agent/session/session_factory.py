"""
Session manager factory for selecting appropriate session storage
"""
import logging
import os
from pathlib import Path
from typing import Optional, Any, Dict, Tuple
from functools import lru_cache

from agents.strands_agent.session.memory_config import load_memory_config

logger = logging.getLogger(__name__)

# AgentCore Memory integration (optional, only for cloud deployment)
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    from bedrock_agentcore.memory import MemoryClient
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False


@lru_cache(maxsize=1)
def _discover_strategy_ids(memory_id: str, region: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Discover the actual strategy IDs from the configured memory strategies.

    AgentCore Memory stores memories in strategy-specific namespaces:
    /strategies/{strategyId}/actors/{actorId}

    This function queries the memory to find the actual strategy IDs.

    Args:
        memory_id: AgentCore Memory ID
        region: AWS region

    Returns:
        Tuple of (semantic_strategy_id, preference_strategy_id)
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        return None, None

    try:
        client = MemoryClient(region_name=region)
        strategies = client.get_memory_strategies(memory_id=memory_id)

        semantic_id = None
        preference_id = None

        for strategy in strategies:
            strategy_type = strategy.get('type') or strategy.get('memoryStrategyType')
            strategy_id = strategy.get('strategyId') or strategy.get('memoryStrategyId')

            if strategy_type == 'SEMANTIC':
                semantic_id = strategy_id
                logger.info(f"  📚 Found SEMANTIC strategy: {semantic_id}")
            elif strategy_type == 'USER_PREFERENCE':
                preference_id = strategy_id
                logger.info(f"  ⚙️ Found USER_PREFERENCE strategy: {preference_id}")

        return semantic_id, preference_id

    except Exception as e:
        logger.error(f"Failed to discover memory strategies: {e}", exc_info=True)
        return None, None


class SessionFactory:
    """Factory for creating appropriate session manager based on environment"""

    @staticmethod
    def create_session_manager(
        session_id: str,
        user_id: str,
        caching_enabled: bool = True
    ) -> Any:
        """
        Create appropriate session manager based on environment configuration

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences
            caching_enabled: Whether to enable prompt caching

        Returns:
            Session manager instance (TurnBasedSessionManager or LocalSessionBuffer)
        """
        # Load memory configuration from environment
        config = load_memory_config()

        if config.is_cloud_mode and AGENTCORE_MEMORY_AVAILABLE:
            # Cloud deployment: Use AgentCore Memory (AWS-managed DynamoDB)
            return SessionFactory._create_cloud_session_manager(
                memory_id=config.memory_id,
                session_id=session_id,
                user_id=user_id,
                aws_region=config.region,
                caching_enabled=caching_enabled
            )
        else:
            # Local development: Use file-based session manager with buffering
            return SessionFactory._create_local_session_manager(session_id)

    @staticmethod
    def _create_cloud_session_manager(
        memory_id: str,
        session_id: str,
        user_id: str,
        aws_region: str,
        caching_enabled: bool
    ) -> Any:
        """
        Create AgentCore Memory session manager with turn-based buffering

        Args:
            memory_id: AgentCore Memory ID (AWS Bedrock service)
            session_id: Session identifier
            user_id: User identifier
            aws_region: AWS region
            caching_enabled: Whether to enable caching

        Returns:
            TurnBasedSessionManager: Session manager with AgentCore Memory

        Note:
            AgentCore Memory uses AWS-managed DynamoDB tables. The table is automatically
            created and managed by AWS Bedrock - you only need to provide the memory_id.
        """
        from agents.strands_agent.session.turn_based_session_manager import TurnBasedSessionManager

        logger.info(f"🚀 Cloud mode: Using AWS Bedrock AgentCore Memory")
        logger.info(f"   • Memory ID: {memory_id}")
        logger.info(f"   • Region: {aws_region}")

        # Discover actual strategy IDs from the memory configuration
        semantic_id, preference_id = _discover_strategy_ids(memory_id, aws_region)

        # Build retrieval config using the correct namespace patterns
        # AgentCore stores memories in: /strategies/{strategyId}/actors/{actorId}
        retrieval_config: Dict[str, RetrievalConfig] = {}

        if preference_id:
            # User preferences (e.g., coding style, response length preferences)
            preference_namespace = f"/strategies/{preference_id}/actors/{{actorId}}"
            retrieval_config[preference_namespace] = RetrievalConfig(
                top_k=5,
                relevance_score=0.5
            )
            logger.info(f"   • Preferences namespace: {preference_namespace}")

        if semantic_id:
            # Semantic facts (e.g., user's name, project details, learned information)
            facts_namespace = f"/strategies/{semantic_id}/actors/{{actorId}}"
            retrieval_config[facts_namespace] = RetrievalConfig(
                top_k=10,
                relevance_score=0.3
            )
            logger.info(f"   • Facts namespace: {facts_namespace}")

        if not retrieval_config:
            logger.warning("⚠️ No memory strategies found - long-term memory retrieval disabled")

        # Configure AgentCore Memory with dynamically discovered namespaces
        agentcore_memory_config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=user_id,
            enable_prompt_caching=caching_enabled,
            retrieval_config=retrieval_config
        )

        # Create Turn-based Session Manager (reduces API calls by 75%)
        session_manager = TurnBasedSessionManager(
            agentcore_memory_config=agentcore_memory_config,
            region_name=aws_region
        )

        logger.info(f"✅ AgentCore Memory initialized: user_id={user_id}")
        logger.info(f"   • Session: {session_id}, User: {user_id}")
        logger.info(f"   • Storage: AWS-managed DynamoDB")
        logger.info(f"   • Short-term memory: Conversation history (90 days retention)")
        logger.info(f"   • Long-term memory: {'Enabled' if retrieval_config else 'Disabled'} ({len(retrieval_config)} namespaces)")

        return session_manager

    @staticmethod
    def _create_local_session_manager(session_id: str) -> Any:
        """
        Create file-based session manager with buffering

        Args:
            session_id: Session identifier

        Returns:
            LocalSessionBuffer: File-based session manager with buffering wrapper
        """
        from strands.session.file_session_manager import FileSessionManager
        from agents.strands_agent.session.local_session_buffer import LocalSessionBuffer

        logger.info(f"💻 Local mode: Using FileSessionManager with buffering")

        # Determine sessions directory
        sessions_dir = Path(__file__).parent.parent.parent.parent / "sessions"
        sessions_dir.mkdir(exist_ok=True)

        # Create base file manager
        base_file_manager = FileSessionManager(
            session_id=session_id,
            storage_dir=str(sessions_dir)
        )

        # Wrap with local buffering manager for stop functionality
        session_manager = LocalSessionBuffer(
            base_manager=base_file_manager,
            session_id=session_id
        )

        logger.info(f"✅ FileSessionManager with buffering initialized: {sessions_dir}")
        logger.info(f"   • Session: {session_id}")
        logger.info(f"   • File-based persistence: {sessions_dir}")

        return session_manager

    @staticmethod
    def is_cloud_mode() -> bool:
        """
        Check if running in cloud mode

        Returns:
            bool: True if AgentCore Memory is available and configured for DynamoDB
        """
        try:
            config = load_memory_config()
            return config.is_cloud_mode and AGENTCORE_MEMORY_AVAILABLE
        except Exception:
            return False
