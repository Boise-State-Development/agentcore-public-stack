"""AgentCore Browser tool package."""

from .browse_tool import browse_web
from .takeover_tool import request_user_login

# `request_user_login` is deliberately NOT re-exported through
# `agents.builtin_tools.__all__`: `create_default_registry` registers it
# explicitly so the kill switch can withhold it, exactly as it does for
# `ask_user_question`.
__all__ = ["browse_web", "request_user_login"]
