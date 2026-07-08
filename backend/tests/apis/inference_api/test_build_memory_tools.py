"""Agent Designer Phase 3 — the _build_memory_tools extra_tools seam.

None binding → no tools; read access → list+read; readwrite → list+read+write.
"""

from types import SimpleNamespace

from apis.inference_api.chat.routes import _build_memory_tools


def _binding(access):
    return SimpleNamespace(space_id="spc_1", space_name="Brain", access=access, role="editor")


def test_no_binding_yields_no_tools():
    assert _build_memory_tools(None, "u1", "u1@x.edu") == []


def test_read_access_exposes_list_and_read_only():
    tools = _build_memory_tools(_binding("read"), "u1", "u1@x.edu")
    names = [t.tool_name for t in tools]
    assert names == ["memory_list", "memory_read"]


def test_readwrite_access_adds_write():
    tools = _build_memory_tools(_binding("readwrite"), "u1", "u1@x.edu")
    names = [t.tool_name for t in tools]
    assert names == ["memory_list", "memory_read", "memory_write"]
