"""Pin how the invocation route calls ``get_agent`` for memory-bound agents.

The route handler is too large to drive end to end in a unit test, and the two
properties below fail silently (an orphaned paused agent; a tool-less agent in
a live cache slot), so they are pinned at the call sites. Shared Projects 2.1.
"""

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[3] / "src" / "apis" / "inference_api" / "chat" / "routes.py"


def _get_agent_calls():
    tree = ast.parse(ROUTES.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_agent":
            yield {kw.arg: kw.value for kw in node.keywords if kw.arg}


def _is_true(value):
    return isinstance(value, ast.Constant) and value.value is True


def _is_false(value):
    return isinstance(value, ast.Constant) and value.value is False


def test_resume_replays_the_memory_binding_and_never_writes_the_cache():
    resumes = [kw for kw in _get_agent_calls() if _is_true(kw.get("is_resume"))]
    assert len(resumes) == 1
    kw = resumes[0]
    assert ast.unparse(kw["memory_binding"]) == "snapshot.memory_binding"
    assert _is_false(kw.get("cache_write")), "a resume miss would seed a tool-less agent"


def test_the_main_turn_passes_the_binding_it_built_tools_from():
    mains = [
        kw for kw in _get_agent_calls()
        if _is_false(kw.get("is_resume")) and "extra_tools_key_described" in kw
    ]
    assert len(mains) == 1
    assert ast.unparse(mains[0]["memory_binding"]) == "memory_binding_key"
