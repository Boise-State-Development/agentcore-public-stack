"""
Skill tools — LLM-callable tools for progressive skill disclosure.

Two tools exposed to the agent:
- skill_dispatcher: Load a skill's instructions and tool schemas (L1 → L2)
- skill_executor: Execute a skill's tool with given input (L2 → L3)

These tools are registered with the Strands Agent in place of the individual
skill tools, dramatically reducing the upfront token cost.

Two ways to obtain the tools:

- ``make_skill_tools(registry)`` returns a fresh ``(dispatcher, executor)``
  pair bound to one specific registry via closure. This is what ``SkillAgent``
  uses, so each agent's meta-tools resolve against **its own** registry. That
  matters once skills are per-user (admin/DB-backed, RBAC-filtered): a process
  serves many users concurrently, and a shared module-level registry would let
  one user's invocation read another user's skills.
- The module-level ``skill_dispatcher`` / ``skill_executor`` + a process-global
  registry set by ``set_dispatcher_registry`` remain for the file-based dev
  path and existing callers. Safe there because every file registry is
  identical; do NOT use this path for per-user skills.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from strands import tool

logger = logging.getLogger(__name__)

# Agent-facing name of the executor meta-tool (both the module-level tool and
# the per-registry pairs from make_skill_tools use this function name). The
# OAuth consent gate matches tool_use dicts against it to see through the
# fold (see skills/mcp_binding.make_folded_tool_provider_lookup).
SKILL_EXECUTOR_TOOL_NAME = "skill_executor"

# Module-level registry reference, set by set_dispatcher_registry(). Used only
# by the module-level skill_dispatcher/skill_executor (file/dev path).
_registry = None


def set_dispatcher_registry(registry: Any) -> None:
    """
    Wire up the SkillRegistry for the module-level dispatcher and executor.

    Must be called before invoking the module-level skill_dispatcher or
    skill_executor. NOTE: this is process-global; for per-user (admin/DB)
    skills use ``make_skill_tools(registry)`` instead, which binds a registry
    per agent and avoids cross-user bleed under concurrency.

    Args:
        registry: SkillRegistry instance
    """
    global _registry
    _registry = registry


def _dispatch(registry: Any, skill_name: str, reference: str = "", source: str = "") -> str:
    """Core skill_dispatcher logic against an explicit registry."""
    if not registry.has_skill(skill_name):
        available = ", ".join(registry.get_skill_names())
        return json.dumps({
            "error": f"Unknown skill '{skill_name}'",
            "available_skills": available,
        })

    # Deep progressive disclosure: when a reference filename is given, serve
    # that supporting file's bytes from S3 instead of the instructions block.
    if reference:
        ref_result = registry.read_resource(skill_name, reference)
        if ref_result is None or "error" in (ref_result or {}):
            return json.dumps({
                "error": (ref_result or {}).get("error")
                or f"Reference file '{reference}' not found in skill '{skill_name}'",
                "available_references": registry.get_resource_names(skill_name),
            })
        return json.dumps(ref_result, default=str)

    result = {}

    # Load Level 2 instructions
    instructions = registry.load_instructions(skill_name)
    if instructions:
        result["instructions"] = instructions

    # Load tool schemas so the LLM knows what parameters to pass
    schemas = registry.get_tool_schemas(skill_name)
    if schemas:
        result["tool_schemas"] = schemas

    # Surface the skill's supporting reference files so the model knows what it
    # can read on demand (its instructions typically name them, e.g. "see
    # forms.md"). Read one by calling skill_dispatcher again with `reference=`.
    references = registry.get_resource_names(skill_name)
    if references:
        result["available_references"] = references
        result["reference_hint"] = (
            "Call skill_dispatcher again with reference=<filename> to read one "
            "of these supporting files."
        )

    if not result:
        result["error"] = f"No instructions or tools found for skill '{skill_name}'"

    return json.dumps(result, default=str)


def _execute(registry: Any, skill_name: str, tool_name: str, tool_input: Any = None) -> Any:
    """Core skill_executor logic against an explicit registry."""
    if not registry.has_skill(skill_name):
        return json.dumps({"error": f"Unknown skill '{skill_name}'"})

    tools = registry.get_tools(skill_name)
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

    # Execute the tool. Folded gateway/external MCP tools (PR-6b) are not plain
    # callables — they run through the MCP client via their own invoke().
    try:
        if getattr(target_tool, "is_mcp_folded", False):
            return target_tool.invoke(tool_input)
        return _execute_tool(target_tool, tool_input)
    except Exception as e:
        logger.error(f"Error executing {skill_name}/{tool_name}: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


def make_skill_tools(registry: Any):
    """Return a ``(skill_dispatcher, skill_executor)`` pair bound to ``registry``.

    Each pair closes over its own registry, so concurrent agents (different
    users / different accessible skills) never share skill state. This is the
    per-agent replacement for the module-global ``set_dispatcher_registry``.
    """

    @tool
    def skill_dispatcher(skill_name: str, reference: str = "", source: str = "") -> str:
        """
        Load a skill's instructions and tool schemas.

        Call this to activate a skill from the Available Skills catalog and
        learn how to use it. The response includes the skill's detailed
        instructions and the parameter schemas for its tools.

        Args:
            skill_name: Name of the skill to activate (from the catalog)
            reference: Optional — name of a reference file to read
            source: Optional — name of a tool function to view source for

        Returns:
            JSON string with the skill's instructions and tool schemas
        """
        return _dispatch(registry, skill_name, reference, source)

    @tool
    def skill_executor(skill_name: str, tool_name: str, tool_input: Any = None) -> Any:
        """
        Execute a tool within an activated skill.

        Call this after skill_dispatcher to run one of the skill's tools.

        Args:
            skill_name: Name of the skill containing the tool
            tool_name: Name of the specific tool to execute
            tool_input: Input parameters for the tool (dict or JSON string)

        Returns:
            The tool's execution result
        """
        return _execute(registry, skill_name, tool_name, tool_input)

    return skill_dispatcher, skill_executor


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
    return _dispatch(_registry, skill_name, reference, source)


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
    return _execute(_registry, skill_name, tool_name, tool_input)


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
