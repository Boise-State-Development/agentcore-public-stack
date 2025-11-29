"""Chat feature service layer

Contains business logic for chat operations, including agent creation and management.
"""

import logging
from typing import Optional, List

from agent.agent import ChatbotAgent

logger = logging.getLogger(__name__)


def get_agent(
    session_id: str,
    user_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
    caching_enabled: Optional[bool] = None
) -> ChatbotAgent:
    """
    Create agent instance with current configuration for session

    No caching - creates new agent each time to reflect latest configuration.
    Session message history is managed by AgentCore Memory automatically.
    """
    logger.info(f"Creating agent for session {session_id}, user {user_id or 'anonymous'}")
    logger.info(f"  Model: {model_id or 'default'}, Temperature: {temperature or 0.7}")
    logger.info(f"  System prompt: {system_prompt[:50] if system_prompt else 'default'}...")
    logger.info(f"  Caching: {caching_enabled if caching_enabled is not None else True}")
    logger.info(f"  Tools: {enabled_tools or 'all'}")

    # Create agent with AgentCore Memory - messages and preferences automatically loaded/saved
    agent = ChatbotAgent(
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        model_id=model_id,
        temperature=temperature,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled
    )

    return agent

