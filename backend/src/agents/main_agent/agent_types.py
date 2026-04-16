"""
Agent type registry and factory function.

Provides a single entry point for creating agents by type string.
New agent types register here as they're implemented.
"""

import logging
from typing import Optional, List

from agents.main_agent.base_agent import BaseAgent

logger = logging.getLogger(__name__)


# Registry of agent type string → class
# New agent types add themselves here as they're implemented
_AGENT_TYPES = {}


def register_agent_type(type_name: str, agent_class: type) -> None:
    """Register an agent class for a given type string."""
    _AGENT_TYPES[type_name] = agent_class


def get_available_types() -> List[str]:
    """Return list of registered agent type names."""
    return list(_AGENT_TYPES.keys())


def create_agent(agent_type: str = "chat", **kwargs) -> BaseAgent:
    """
    Create an agent by type string.

    Args:
        agent_type: Agent type ("chat", "skill", "voice"). Default: "chat".
        **kwargs: Passed directly to the agent constructor (session_id, user_id, etc.)

    Returns:
        BaseAgent subclass instance

    Raises:
        ValueError: If agent_type is not registered
    """
    agent_class = _AGENT_TYPES.get(agent_type)
    if agent_class is None:
        available = ", ".join(sorted(_AGENT_TYPES.keys()))
        raise ValueError(f"Unknown agent_type '{agent_type}'. Available: {available}")

    logger.info(f"Creating {agent_type} agent ({agent_class.__name__})")
    return agent_class(**kwargs)


# Register built-in agent types
def _register_defaults():
    from agents.main_agent.chat_agent import ChatAgent
    from agents.main_agent.skill_agent import SkillAgent
    register_agent_type("chat", ChatAgent)
    register_agent_type("skill", SkillAgent)

    # Voice agent is optional (requires strands-agents[bidi])
    try:
        from agents.main_agent.voice_agent import VoiceAgent
        register_agent_type("voice", VoiceAgent)
    except Exception:
        pass


_register_defaults()
