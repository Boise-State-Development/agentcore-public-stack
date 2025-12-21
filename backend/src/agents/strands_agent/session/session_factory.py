"""
Session manager factory for selecting appropriate session storage
"""
import logging
import os
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)

# AgentCore Memory integration (optional, only for cloud deployment)
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False


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
        memory_id = os.environ.get('AGENTCORE_MEMORY_ID')
        aws_region = os.environ.get('AWS_REGION', 'us-west-2')

        if memory_id and AGENTCORE_MEMORY_AVAILABLE:
            # Cloud deployment: Use AgentCore Memory with Turn-based buffering
            return SessionFactory._create_cloud_session_manager(
                memory_id=memory_id,
                session_id=session_id,
                user_id=user_id,
                aws_region=aws_region,
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
            memory_id: AgentCore Memory ID
            session_id: Session identifier
            user_id: User identifier
            aws_region: AWS region
            caching_enabled: Whether to enable caching

        Returns:
            TurnBasedSessionManager: Session manager with AgentCore Memory
        """
        from agents.strands_agent.session.turn_based_session_manager import TurnBasedSessionManager

        logger.info(f"🚀 Cloud mode: Using AgentCore Memory (memory_id={memory_id})")

        # Configure AgentCore Memory with user preferences and facts retrieval
        agentcore_memory_config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=user_id,
            enable_prompt_caching=caching_enabled,
            retrieval_config={
                # User-specific preferences (e.g., coding style, language preference)
                f"/preferences/{user_id}": RetrievalConfig(top_k=5, relevance_score=0.7),
                # User-specific facts (e.g., learned information)
                f"/facts/{user_id}": RetrievalConfig(top_k=10, relevance_score=0.3),
            }
        )

        # Create Turn-based Session Manager (reduces API calls by 75%)
        session_manager = TurnBasedSessionManager(
            agentcore_memory_config=agentcore_memory_config,
            region_name=aws_region
        )

        logger.info(f"✅ AgentCore Memory initialized: user_id={user_id}")
        logger.info(f"   • Session: {session_id}, User: {user_id}")
        logger.info(f"   • Short-term memory: Conversation history (90 days retention)")
        logger.info(f"   • Long-term memory: User preferences and facts across sessions")

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
            bool: True if AgentCore Memory is available and configured
        """
        return bool(os.environ.get('AGENTCORE_MEMORY_ID') and AGENTCORE_MEMORY_AVAILABLE)
