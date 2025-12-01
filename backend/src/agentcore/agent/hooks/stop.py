"""Hook to handle session stop requests by cancelling tool execution"""

import logging
from typing import Any
from strands.hooks import HookProvider, HookRegistry, BeforeToolCallEvent

logger = logging.getLogger(__name__)


class StopHook(HookProvider):
    """Hook to handle session stop requests by cancelling tool execution"""

    def __init__(self, session_manager):
        self.session_manager = session_manager

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check_cancelled)

    def check_cancelled(self, event: BeforeToolCallEvent) -> None:
        """Cancel tool execution if session is stopped by user"""
        if hasattr(self.session_manager, 'cancelled') and self.session_manager.cancelled:
            tool_name = event.tool_use.get("name", "unknown")
            logger.info(f"🚫 Cancelling tool execution: {tool_name} (session stopped by user)")
            event.cancel_tool = "Session stopped by user"

