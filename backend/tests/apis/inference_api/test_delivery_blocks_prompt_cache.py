"""Prompt cache: a delivery, a run result included, reaches only the new user message.

Workflow-runs spec §7 and §11 (PR-W.2), handoffs spec §4.8 "Cache". A delivery's blocks land in the
user message this turn appends, past the history cachePoint; nothing of one may reach the system
prompt or ``toolConfig``, which sit ahead of the whole restored history. The route is too large to
drive end to end in a unit test (``test_get_agent_call_sites``), so the property is pinned at the
call sites: every use of the turn's ``DeliveryTurn`` is listed, its blocks go into
``join_delivery_blocks(…, final_message)`` and nowhere else, and the only thing of it any
prompt- or tool-building call sees is the file-kind hint, which is ``False`` for a result that
carries no files (``test_a_run_result_without_files_flips_no_tool_gate``).
"""

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[3] / "src" / "apis" / "inference_api" / "chat" / "routes.py"

#: Calls that build what sits ahead of the history: the system prompt, the tools, the agent.
_PREFIX_BUILDERS = {
    "compose_turn_system_prompt",
    "apply_turn_prompt_extras",
    "_build_turn_tools",
    "_resolve_effective_tools",
    "resolve_turn_agent",
    "get_agent",
    "_build_main_agent",
}


def _tree() -> ast.AST:
    return ast.parse(ROUTES.read_text())


def _name(func: ast.AST) -> str:
    return getattr(func, "id", None) or getattr(func, "attr", "") or ""


def test_the_turns_deliveries_are_used_only_to_prefetch_hint_start_and_render():
    used = {
        node.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "delivery_turn"
    }

    assert used == {"prefetch", "kind_hint", "start", "blocks"}


def test_the_blocks_go_into_the_user_message_and_nowhere_else():
    joins = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and _name(node.func) == "blocks":
            joins.append(node)
    assert len(joins) == 1

    assigns = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _name(node.value.func) == "join_delivery_blocks"
    ]
    assert len(assigns) == 1
    assign = assigns[0]
    assert [ast.unparse(t) for t in assign.targets] == ["final_message"]
    assert ast.unparse(assign.value) == "join_delivery_blocks(await delivery_turn.blocks(), final_message)"


def _reads_deliveries(node: ast.AST) -> bool:
    """Whether ``node`` reads ``delivery_turn`` other than through ``_turn_file_hint(…)``."""
    if isinstance(node, ast.Call) and _name(node.func) == "_turn_file_hint":
        return False
    if isinstance(node, ast.Name) and node.id == "delivery_turn":
        return True
    return any(_reads_deliveries(child) for child in ast.iter_child_nodes(node))


def test_no_prefix_builder_sees_a_delivery_except_through_the_file_hint():
    hinted = 0
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and _name(node.func) in _PREFIX_BUILDERS):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            assert not _reads_deliveries(arg), f"{_name(node.func)} reads the turn's deliveries: {ast.unparse(arg)}"
            hinted += "_turn_file_hint" in ast.unparse(arg)
    assert hinted > 0  # the hint is how a carried file reaches its gate on the turn it arrives (§4.9)
