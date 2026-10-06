"""
Session manager factory for creating AgentCore Memory session managers
"""
import contextvars
import os
import logging
from typing import Optional, Any, Dict, Tuple
from functools import lru_cache

from agents.main_agent.config.constants import EnvVars, Defaults
from agents.main_agent.session.memory_config import load_memory_config
from agents.main_agent.session.compaction_models import CompactionConfig
from agents.main_agent.session.preview_session_manager import (
    PreviewSessionManager,
    is_preview_session,
)

logger = logging.getLogger(__name__)

# Kill switch for async session persistence. Default ON; only the literal
# string "false" disables it — an empty or unset value stays enabled (workflow
# env vars can materialize as ""). See ``session_async_persistence_enabled``.
SESSION_ASYNC_PERSISTENCE_ENABLED_ENV = "AGENTCORE_SESSION_ASYNC_PERSISTENCE_ENABLED"


MEMORY_SUMMARY_RETRIEVAL_ENV = "MEMORY_SUMMARY_NAMESPACE_RETRIEVAL_ENABLED"


def summary_namespace_retrieval_enabled() -> bool:
    """Whether the current session's own summary is retrieved on every user
    message and prepended to it. Off unless ``=true``: it re-injects a
    conversation the model already holds, at a lookup per message. The
    compaction path still reads summaries where they matter
    (`TurnBasedSessionManager._retrieve_session_summaries`)."""
    return os.environ.get(MEMORY_SUMMARY_RETRIEVAL_ENV, "").strip().lower() == "true"


def session_async_persistence_enabled() -> bool:
    """Whether AgentCore Memory writes are offloaded off the event loop.

    ``batch_size`` is 1, so every message appended during a turn — the user
    message, each assistant message, each tool result — fires a synchronous
    boto3 ``create_event`` plus a ``sync_agent`` from inside the SSE stream
    generator, blocking the asyncio event loop for the whole container.

    ``async_mode`` (bedrock-agentcore 1.21.0) wraps exactly those calls in
    ``asyncio.to_thread``. It is the same discipline the chat path already
    applies to every other boto3 caller (artifacts, spreadsheet tools, the
    interrupted-turn persistence in ``stream_coordinator``); session
    persistence was the last blocking one on the hot path.

    Two constraints come with it, both satisfied here:

    - The agent MUST be invoked via the async path. Sync ``agent(...)`` raises
      RuntimeError from Strands' hook registry, which refuses to dispatch
      coroutine callbacks synchronously. Every invocation in this repo goes
      through ``stream_async``.
    - ``AgentInitializedEvent`` cannot be async (Strands disallows it), so
      ``initialize()`` — session restore plus our compaction — still blocks the
      calling thread. This flag buys per-turn writes, not cold start.

    Read per call (no module-level caching) so tests and live config changes
    behave predictably; the env read is negligible next to the boto3 work.
    """
    return os.environ.get(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "").lower() != "false"


# AgentCore Memory integration (optional, only for cloud deployment)
try:
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory import MemoryClient
    from bedrock_agentcore.memory.integrations.strands import session_manager as _sdk_session_manager
    AGENTCORE_MEMORY_AVAILABLE = True
except ImportError:
    AGENTCORE_MEMORY_AVAILABLE = False


# ---------------------------------------------------------------------------
# One set of AgentCore Memory clients per process
# ---------------------------------------------------------------------------
#
# The SDK's session manager builds its clients from scratch on every
# construction, twice (see ``agent_build_shared_session_enabled``). Everything
# below exists to hand it the process-wide session from
# ``apis.shared.aws_clients`` instead, which warm-up builds at container
# start (``apis/inference_api/warmup.py``).

# Set by the factory for the duration of one session manager's construction.
# The SDK builds its ``MemoryClient`` with no session and no session id, so this
# is how the per-session decision reaches ``_SharedSessionMemoryClient``.
_use_shared_memory_clients: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "use_shared_memory_clients", default=False
)


if AGENTCORE_MEMORY_AVAILABLE:
    from apis.shared.aws_clients import shared_boto_session

    class _SharedSessionMemoryClient(MemoryClient):
        """``MemoryClient`` built from the shared session when the flag is on.

        The SDK session manager constructs ``MemoryClient(region_name=...)``
        with no session, so it cannot be handed one; its clients are then
        replaced a few lines later, so they were pure cost. Rebinding the name
        in the SDK module is the only seam the pinned bedrock-agentcore offers.
        Pinned by ``test_session_factory_shared_clients.py``, which fails if an
        SDK upgrade stops going through it.
        """

        def __init__(
            self,
            region_name: Optional[str] = None,
            integration_source: Optional[str] = None,
            boto3_session: Any = None,
        ) -> None:
            if boto3_session is None and _use_shared_memory_clients.get():
                boto3_session = shared_boto_session()
            super().__init__(
                region_name=region_name,
                integration_source=integration_source,
                boto3_session=boto3_session,
            )

    _sdk_session_manager.MemoryClient = _SharedSessionMemoryClient


@lru_cache(maxsize=2)
def _discover_strategy_ids(
    memory_id: str, region: str, *, shared_session: bool = False
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Discover the actual strategy IDs from the configured memory strategies.

    AgentCore Memory stores memories in strategy-specific namespaces:
    /strategies/{strategyId}/actors/{actorId}

    This function queries the memory to find the actual strategy IDs. It is
    a control-plane read of static configuration, cached for the life of the
    process.

    Args:
        memory_id: AgentCore Memory ID
        region: AWS region
        shared_session: Build the ``MemoryClient`` on the process-wide session
            (``agent_build_shared_session_enabled``) instead of a fresh one.
            Part of the cache key on purpose: warm-up primes the shared entry
            at container start, and a process with the kill switch set must
            still do exactly what it did before.

    Returns:
        Tuple of (semantic_strategy_id, preference_strategy_id, summary_strategy_id)
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        return None, None, None

    try:
        client = MemoryClient(
            region_name=region,
            boto3_session=shared_boto_session() if shared_session else None,
        )
        strategies = client.get_memory_strategies(memory_id=memory_id)

        semantic_id = None
        preference_id = None
        summary_id = None

        for strategy in strategies:
            strategy_type = strategy.get('type') or strategy.get('memoryStrategyType')
            strategy_id = strategy.get('strategyId') or strategy.get('memoryStrategyId')

            if strategy_type == 'SEMANTIC':
                semantic_id = strategy_id
                logger.info(f"  📚 Found SEMANTIC strategy: {semantic_id}")
            elif strategy_type == 'USER_PREFERENCE':
                preference_id = strategy_id
                logger.info(f"  ⚙️ Found USER_PREFERENCE strategy: {preference_id}")
            elif strategy_type == 'SUMMARIZATION':
                summary_id = strategy_id
                logger.info(f"  📝 Found SUMMARIZATION strategy: {summary_id}")

        return semantic_id, preference_id, summary_id

    except Exception as e:
        logger.error(f"Failed to discover memory strategies: {e}", exc_info=True)
        return None, None, None


def warm_strategy_ids() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Discover the memory's strategy ids on the shared session, once, at container start.

    Called from ``apis/inference_api/warmup.py`` on the startup daemon thread
    so the first turn finds the ids cached and its clients built. Raises when no memory is configured (``load_memory_config``); the
    warm-up step logs that and moves on.
    """
    if not AGENTCORE_MEMORY_AVAILABLE:
        return None, None, None
    config = load_memory_config()
    return _discover_strategy_ids(config.memory_id, config.region, shared_session=True)


class SessionFactory:
    """Factory for creating appropriate session manager based on environment"""

    @staticmethod
    def create_session_manager(
        session_id: str,
        user_id: str,
        caching_enabled: bool = True,
        compaction_enabled: Optional[bool] = None,
        compaction_threshold: Optional[int] = None,
    ) -> Any:
        """
        Create appropriate session manager based on environment configuration

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences
            caching_enabled: Whether to enable prompt caching
            compaction_enabled: Override AGENTCORE_MEMORY_COMPACTION_ENABLED env var
            compaction_threshold: Override AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD env var

        Returns:
            Session manager instance (TurnBasedSessionManager or PreviewSessionManager)
        """
        # Check for preview session first - these use in-memory storage only
        if is_preview_session(session_id):
            logger.info("🔍 Preview session detected")
            return PreviewSessionManager(session_id=session_id, user_id=user_id)

        if not AGENTCORE_MEMORY_AVAILABLE:
            raise RuntimeError(
                "bedrock_agentcore package is required. "
                "Install with: uv sync --extra agentcore"
            )

        # Load memory configuration from environment (raises if AGENTCORE_MEMORY_ID not set)
        config = load_memory_config()

        return SessionFactory._create_cloud_session_manager(
            memory_id=config.memory_id,
            session_id=session_id,
            user_id=user_id,
            aws_region=config.region,
            caching_enabled=caching_enabled,
            compaction_enabled=compaction_enabled,
            compaction_threshold=compaction_threshold,
        )

    @staticmethod
    def _create_cloud_session_manager(
        memory_id: str,
        session_id: str,
        user_id: str,
        aws_region: str,
        caching_enabled: bool,
        compaction_enabled: Optional[bool] = None,
        compaction_threshold: Optional[int] = None,
    ) -> Any:
        """
        Create AgentCore Memory session manager with built-in compaction

        Args:
            memory_id: AgentCore Memory ID (AWS Bedrock service)
            session_id: Session identifier
            user_id: User identifier
            aws_region: AWS region
            caching_enabled: Whether to enable caching
            compaction_enabled: Override AGENTCORE_MEMORY_COMPACTION_ENABLED env var
            compaction_threshold: Override AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD env var

        Returns:
            TurnBasedSessionManager with compaction support

        Note:
            AgentCore Memory uses AWS-managed DynamoDB tables. The table is automatically
            created and managed by AWS Bedrock - you only need to provide the memory_id.
        """
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager

        logger.info(f"🚀 Cloud mode: Using AWS Bedrock AgentCore Memory")
        logger.info(f"   • Memory ID: {memory_id}")
        logger.info(f"   • Region: {aws_region}")

        # Discover actual strategy IDs from the memory configuration. With the
        # shared session on this is a cache hit: warm-up discovered them at
        # container start (`warm_strategy_ids`).
        from apis.shared.feature_flags import agent_build_shared_session_enabled

        shared_clients = agent_build_shared_session_enabled()
        semantic_id, preference_id, summary_id = _discover_strategy_ids(
            memory_id, aws_region, shared_session=shared_clients
        )

        # Load retrieval thresholds from environment (configurable per deployment)
        relevance_score = float(os.environ.get(EnvVars.MEMORY_RELEVANCE_SCORE, str(Defaults.MEMORY_RELEVANCE_SCORE)))
        top_k = int(os.environ.get(EnvVars.MEMORY_TOP_K, str(Defaults.MEMORY_TOP_K)))

        # Build retrieval config using the correct namespace patterns
        # AgentCore stores memories in: /strategies/{strategyId}/actors/{actorId}
        retrieval_config: Dict[str, RetrievalConfig] = {}

        if preference_id:
            # User preferences (e.g., coding style, response length preferences)
            preference_namespace = f"/strategies/{preference_id}/actors/{{actorId}}"
            retrieval_config[preference_namespace] = RetrievalConfig(
                top_k=top_k,
                relevance_score=relevance_score
            )
            logger.info(f"   • Preferences namespace: {preference_namespace}")

        if semantic_id:
            # Semantic facts (e.g., user's name, project details, learned information)
            facts_namespace = f"/strategies/{semantic_id}/actors/{{actorId}}"
            retrieval_config[facts_namespace] = RetrievalConfig(
                top_k=top_k,
                relevance_score=relevance_score
            )
            logger.info(f"   • Facts namespace: {facts_namespace}")

        if summary_id and summary_namespace_retrieval_enabled():
            # Session summaries (condensed conversation context for the current session)
            # Note: Summary namespace includes sessionId since summaries are per-session.
            #
            # Off by default. This namespace holds AgentCore's summary of THIS
            # session, and the SDK retrieves it on every user message and
            # prepends it to that message — a second copy of a conversation
            # the model already has in context, paid as input tokens on every
            # turn, plus one RetrieveMemoryRecords call per message against a
            # 30/s account quota. The summary is genuinely useful once the
            # conversation has been compacted, and that path reads it directly
            # (`_retrieve_session_summaries` at checkpoint advance), so nothing
            # is lost by not retrieving it per message. See
            # docs/specs/load-test-assessment-2026-09.md P2-F.
            summary_namespace = f"/strategies/{summary_id}/actors/{{actorId}}/sessions/{{sessionId}}"
            retrieval_config[summary_namespace] = RetrievalConfig(
                top_k=top_k,
                relevance_score=relevance_score
            )
            logger.info(f"   • Summary namespace: {summary_namespace}")

        logger.info(f"   • Retrieval: top_k={top_k}, relevance_score={relevance_score}")

        if not retrieval_config:
            logger.warning("⚠️ No memory strategies found - long-term memory retrieval disabled")

        # Configure AgentCore Memory with dynamically discovered namespaces
        async_persistence = session_async_persistence_enabled()

        agentcore_memory_config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=user_id,
            enable_prompt_caching=caching_enabled,
            retrieval_config=retrieval_config,
            async_mode=async_persistence,
        )

        # Build compaction config
        compaction_config = CompactionConfig.from_env()

        # Apply overrides
        if compaction_enabled is not None:
            compaction_config.enabled = compaction_enabled
        if compaction_threshold is not None:
            compaction_config.token_threshold = compaction_threshold

        # Create session manager with compaction built-in
        token = _use_shared_memory_clients.set(shared_clients)
        try:
            session_manager = TurnBasedSessionManager(
                agentcore_memory_config=agentcore_memory_config,
                region_name=aws_region,
                compaction_config=compaction_config if compaction_config.enabled else None,
                user_id=user_id,
                summarization_strategy_id=summary_id,
                boto_session=shared_boto_session() if shared_clients else None,
            )
        finally:
            _use_shared_memory_clients.reset(token)

        logger.info("✅ AgentCore Memory initialized")
        logger.info("   • Storage: AWS-managed DynamoDB")
        logger.info("   • Short-term memory: Conversation history (retention: CDK_CONVERSATION_RETENTION_DAYS, default 365 days)")
        logger.info("   • Long-term memory: %s (%d namespaces)", "Enabled" if retrieval_config else "Disabled", len(retrieval_config))
        if compaction_config.enabled:
            logger.info("   • Compaction: Enabled (threshold=%s)", f"{compaction_config.token_threshold:,}")
        else:
            logger.info("   • Compaction: Disabled")
        logger.info("   • Persistence: %s", "Async (off the event loop)" if async_persistence else "Sync (blocking)")
        logger.info("   • Clients: %s", "Shared (process-wide)" if shared_clients else "Per session manager")

        return session_manager

    @staticmethod
    def is_cloud_mode() -> bool:
        """
        Check if running in cloud mode (AgentCore Memory available and configured).

        Returns:
            bool: True if AgentCore Memory package is available and AGENTCORE_MEMORY_ID is set
        """
        if not AGENTCORE_MEMORY_AVAILABLE:
            return False
        try:
            load_memory_config()
            return True
        except Exception:
            return False
