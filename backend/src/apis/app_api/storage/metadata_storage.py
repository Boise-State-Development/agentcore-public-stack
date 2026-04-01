"""Abstract interface for message metadata storage

This module provides the storage abstraction layer for DynamoDB-backed metadata storage.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


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
        """
        Store message metadata

        Args:
            user_id: User identifier
            session_id: Session identifier
            message_id: Message identifier (0-indexed)
            metadata: Metadata dictionary containing:
                - latency: LatencyMetrics (timeToFirstToken, endToEndLatency)
                - tokenUsage: TokenUsage (inputTokens, outputTokens, etc.)
                - modelInfo: ModelInfo (modelId, modelName, pricingSnapshot)
                - attribution: Attribution (userId, sessionId, timestamp)
                - cost: Calculated cost in USD

        Raises:
            Exception: If storage operation fails
        """
        pass

    @abstractmethod
    async def get_message_metadata(
        self,
        user_id: str,
        session_id: str,
        message_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific message

        Args:
            user_id: User identifier
            session_id: Session identifier
            message_id: Message identifier

        Returns:
            Metadata dictionary or None if not found
        """
        pass

    @abstractmethod
    async def get_session_metadata(
        self,
        user_id: str,
        session_id: str
    ) -> List[Dict[str, Any]]:
        """
        Get all message metadata for a session

        Args:
            user_id: User identifier
            session_id: Session identifier

        Returns:
            List of metadata dictionaries, one per message
        """
        pass

    @abstractmethod
    async def get_user_cost_summary(
        self,
        user_id: str,
        period: str  # e.g., "2025-01" for monthly
    ) -> Optional[Dict[str, Any]]:
        """
        Get pre-aggregated cost summary for a user

        This is used for fast quota checks (<10ms).

        Args:
            user_id: User identifier
            period: Period identifier (YYYY-MM for monthly)

        Returns:
            Cost summary dictionary or None if not found
        """
        pass

    @abstractmethod
    async def update_user_cost_summary(
        self,
        user_id: str,
        period: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        timestamp: str,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        cache_savings_delta: float = 0.0,
        provider: Optional[str] = None
    ) -> None:
        """
        Update pre-aggregated cost summary (atomic increment)

        This is called after each request to update the running totals.

        Args:
            user_id: User identifier
            period: Period identifier (YYYY-MM)
            cost_delta: Cost to add to total
            usage_delta: Token counts to add (inputTokens, outputTokens, etc.)
            timestamp: ISO timestamp of the update
            model_id: Model identifier for per-model breakdown (optional)
            model_name: Human-readable model name (optional)
            cache_savings_delta: Cache savings to add to total (optional)
            provider: LLM provider (bedrock, openai, gemini) (optional)
        """
        pass

    @abstractmethod
    async def get_user_messages_in_range(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Get all message metadata for a user in a date range

        This is used for detailed cost reports and custom date ranges.

        Args:
            user_id: User identifier
            start_date: Start of period (inclusive)
            end_date: End of period (inclusive)

        Returns:
            List of metadata dictionaries matching the date range
        """
        pass
