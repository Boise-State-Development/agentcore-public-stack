"""
Main Agent - Backward-compatible alias for ChatAgent

All existing code that imports MainAgent continues to work unchanged.
New code should use create_agent() factory or import ChatAgent directly.
"""

from agents.main_agent.chat_agent import ChatAgent


class MainAgent(ChatAgent):
    """
    Backward-compatible alias for ChatAgent.

    MainAgent IS ChatAgent — same constructor, same methods, same behavior.
    Existing callers (service.py, routes.py, tests) need no changes.

    For new agent types, use the agent factory:
        from agents.main_agent.agent_types import create_agent
        agent = create_agent("chat", session_id=..., ...)
        agent = create_agent("skill", session_id=..., ...)
        agent = create_agent("voice", session_id=..., ...)
    """
    pass
