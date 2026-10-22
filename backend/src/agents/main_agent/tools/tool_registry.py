"""
Tool registry for discovering and managing available tools
"""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry for managing available tools"""

    def __init__(self):
        """Initialize empty tool registry"""
        self._tools: Dict[str, Any] = {}

    def register_tool(self, tool_id: str, tool_obj: Any) -> None:
        """
        Register a tool in the registry

        Args:
            tool_id: Unique identifier for the tool
            tool_obj: Tool object (function with @tool decorator)
        """
        self._tools[tool_id] = tool_obj
        logger.info(f"Registered tool: {tool_id}")

    def register_module_tools(self, module: Any) -> None:
        """
        Register all tools from a module's __all__ export

        Args:
            module: Python module with __all__ export containing tool names
        """
        if not hasattr(module, '__all__'):
            logger.warning(f"Module {module.__name__} has no __all__ export")
            return

        for tool_name in module.__all__:
            tool_obj = getattr(module, tool_name)
            self.register_tool(tool_name, tool_obj)

    def get_tool(self, tool_id: str) -> Any:
        """
        Get tool by ID

        Args:
            tool_id: Tool identifier

        Returns:
            Tool object or None if not found
        """
        return self._tools.get(tool_id)

    def has_tool(self, tool_id: str) -> bool:
        """
        Check if tool exists in registry

        Args:
            tool_id: Tool identifier

        Returns:
            bool: True if tool exists
        """
        return tool_id in self._tools

    def get_all_tool_ids(self) -> list[str]:
        """
        Get all registered tool IDs

        Returns:
            list: List of all tool IDs
        """
        return list(self._tools.keys())

    def get_tool_count(self) -> int:
        """
        Get total number of registered tools

        Returns:
            int: Number of tools in registry
        """
        return len(self._tools)


def create_default_registry() -> ToolRegistry:
    """
    Create tool registry with default tools loaded

    Returns:
        ToolRegistry: Registry with Strands built-in, local, and builtin tools
    """
    from strands_tools.calculator import calculator
    from agents import local_tools, builtin_tools
    from agents.builtin_tools.ask_user_question import ask_user_question
    from agents.builtin_tools.browser import request_user_login
    from apis.shared.feature_flags import (
        ask_user_question_enabled,
        browser_takeover_enabled,
    )

    registry = ToolRegistry()

    # Register Strands built-in tools
    registry.register_tool("calculator", calculator)

    # Register local tools
    registry.register_module_tools(local_tools)

    # Register builtin tools (AWS SDK tools)
    registry.register_module_tools(builtin_tools)

    # Registered explicitly rather than via `builtin_tools.__all__` so the kill
    # switch can withhold it: an unregistered tool never reaches `toolConfig`,
    # so with the flag off the model cannot pause a turn behind a prompt the
    # client has no renderer for.
    if ask_user_question_enabled():
        registry.register_tool("ask_user_question", ask_user_question)

    # Same reasoning, and one more: this one hands a human an interactive
    # browser inside our AWS account, so it has both a kill switch here and its
    # own catalog entry granted per role (spec D1). Registration only makes it
    # *grantable*; a role that has not been granted it still never sees it.
    if browser_takeover_enabled():
        registry.register_tool("request_user_login", request_user_login)

    logger.info(f"Default registry created with {registry.get_tool_count()} tools")
    return registry
