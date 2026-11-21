"""``run_code``: skill scripts and model-written Python in the isolated sandbox.

See ``run_code_tool.py`` and ``docs/specs/skill-code-sandbox.md``.
"""

from agents.builtin_tools.code_sandbox.run_code_tool import TOOL_NAME, make_run_code_tool

__all__ = ["TOOL_NAME", "make_run_code_tool"]
