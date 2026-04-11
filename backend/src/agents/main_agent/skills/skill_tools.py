"""
Skill tools — LLM-callable tools for progressive skill disclosure.

Two tools exposed to the agent:
- skill_dispatcher: Load a skill's instructions and tool schemas (L1 → L2)
- skill_executor: Execute a skill's tool with given input (L2 → L3)

These tools are registered with the Strands Agent in place of the individual
skill tools, dramatically reducing the upfront token cost.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from strands import tool

logger = logging.getLogger(__name__)

# Module-level registry reference, set by set_dispatcher_registry()
_registry = None


def set_dispatcher_registry(registry: Any) -> None:
    """
    Wire up the SkillRegistry for the dispatcher and executor.

    Must be called before the agent invokes skill_dispatcher or skill_executor.

    Args:
        registry: SkillRegistry instance
    """
    global _registry
    _registry = registry


@tool
def skill_dispatcher(skill_name: str, reference: str = "", source: str = "") -> str:
    """
    Load a skill's instructions, tool schemas, and optional reference or source code.

    Call this tool when you want to activate a skill and learn how to use it.
    The response includes the skill's detailed instructions (SKILL.md) and
    the parameter schemas for its tools.

    Args:
        skill_name: Name of the skill to activate (from the Available Skills catalog)
        reference: Optional — name of a reference file to read
        source: Optional — name of a tool function to view source code for

    Returns:
        JSON string with skill instructions, tool schemas, and optional reference/source
    """
    if _registry is None:
        return json.dumps({"error": "Skill registry not initialized"})

    if not _registry.has_skill(skill_name):
        available = ", ".join(_registry.get_skill_names())
        return json.dumps({
            "error": f"Unknown skill '{skill_name}'",
            "available_skills": available,
        })

    result = {}

    # Load Level 2 instructions
    instructions = _registry.load_instructions(skill_name)
    if instructions:
        result["instructions"] = instructions

    # Load tool schemas so the LLM knows what parameters to pass
    schemas = _registry.get_tool_schemas(skill_name)
    if schemas:
        result["tool_schemas"] = schemas

    if not result:
        result["error"] = f"No instructions or tools found for skill '{skill_name}'"

    return json.dumps(result, default=str)


@tool
def skill_executor(skill_name: str, tool_name: str, tool_input: Any = None) -> Any:
    """
    Execute a tool within an activated skill.

    Call this tool after using skill_dispatcher to learn which tools are available
    and what parameters they accept.

    Args:
        skill_name: Name of the skill containing the tool
        tool_name: Name of the specific tool to execute
        tool_input: Input parameters for the tool (dict or JSON string)

    Returns:
        The tool's execution result
    """
    if _registry is None:
        return json.dumps({"error": "Skill registry not initialized"})

    if not _registry.has_skill(skill_name):
        return json.dumps({"error": f"Unknown skill '{skill_name}'"})

    tools = _registry.get_tools(skill_name)
    if not tools:
        return json.dumps({"error": f"No tools found for skill '{skill_name}'"})

    # Find the matching tool
    target_tool = None
    for t in tools:
        name = getattr(t, "tool_name", getattr(t, "__name__", None))
        if name == tool_name:
            target_tool = t
            break

    if target_tool is None:
        available = [getattr(t, "tool_name", getattr(t, "__name__", "?")) for t in tools]
        return json.dumps({
            "error": f"Tool '{tool_name}' not found in skill '{skill_name}'",
            "available_tools": available,
        })

    # Parse tool_input if it's a JSON string
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            pass

    if tool_input is None:
        tool_input = {}

    # Execute the tool
    try:
        result = _execute_tool(target_tool, tool_input)
        return result
    except Exception as e:
        logger.error(f"Error executing {skill_name}/{tool_name}: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


def _execute_tool(tool_obj: Any, tool_input: dict) -> Any:
    """Execute a tool function, handling sync and async cases."""
    if isinstance(tool_input, dict):
        result = tool_obj(**tool_input)
    else:
        result = tool_obj(tool_input)

    # Handle async results
    if asyncio.iscoroutine(result):
        result = _run_async(result)

    return result


def _run_async(coro):
    """Run an async coroutine, handling cases where an event loop may already exist."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                return executor.submit(asyncio.run, coro).result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
