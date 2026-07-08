"""Agent Designer Phase 3 — memory_* tool factories.

Each tool is closed over the bound space id + invoker identity; MemorySpaceService is
patched. Verifies success payloads and that a revoked grant (permission error) surfaces
as an error tool-result rather than raising.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.builtin_tools.memory_spaces import (
    make_memory_list_tool,
    make_memory_read_tool,
    make_memory_write_tool,
)
from apis.shared.memory.service import (
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
)

MODULE = "agents.builtin_tools.memory_spaces.tools"


async def _call(tool, *args, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(*args, **kwargs)


def _patch_service(monkeypatch) -> MagicMock:
    svc = MagicMock()
    monkeypatch.setattr(f"{MODULE}.MemorySpaceService", lambda: svc)
    return svc


class TestMemoryList:
    @pytest.mark.asyncio
    async def test_lists_manifest_summary(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.list_entries.return_value = [
            SimpleNamespace(slug="jane", entry_type="entity", description="a person", updated="2026-07-07"),
        ]
        tool = make_memory_list_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool)
        assert result["status"] == "success"
        assert result["content"][0]["json"]["entries"][0]["slug"] == "jane"
        # scoped to the bound space + invoker
        assert svc.list_entries.call_args.args[:3] == ("spc_1", "u1", "u1@x.edu")

    @pytest.mark.asyncio
    async def test_revoked_grant_is_error_result(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.list_entries.side_effect = MemorySpacePermissionError("nope")
        tool = make_memory_list_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool)
        assert result["status"] == "error"
        assert "no longer have access" in result["content"][0]["text"]


class TestMemoryRead:
    @pytest.mark.asyncio
    async def test_reads_body(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.read_entry.return_value = "Jane is the CFO."
        tool = make_memory_read_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="jane")
        assert result["status"] == "success"
        assert result["content"][0]["text"] == "Jane is the CFO."

    @pytest.mark.asyncio
    async def test_missing_entry_is_error_result(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.read_entry.side_effect = MemorySpaceNotFoundError("gone")
        tool = make_memory_read_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="ghost")
        assert result["status"] == "error" and "No memory entry 'ghost'" in result["content"][0]["text"]


class TestMemoryWrite:
    @pytest.mark.asyncio
    async def test_writes_and_confirms(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.write_entry.return_value = SimpleNamespace(slug="jane", entry_type="entity")
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="jane", body="Jane is the CFO.", entry_type="entity", description="person")
        assert result["status"] == "success"
        assert 'Saved memory entry "jane"' in result["content"][0]["text"]
        # write goes to the bound space as the invoker, with the given fields
        kwargs = svc.write_entry.call_args
        assert kwargs.args[0] == "spc_1" and kwargs.args[1] == "u1"
        assert kwargs.kwargs["entry_type"] == "entity"

    @pytest.mark.asyncio
    async def test_write_permission_error_is_error_result(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.write_entry.side_effect = MemorySpacePermissionError("read-only")
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="jane", body="x")
        assert result["status"] == "error" and "don't have write access" in result["content"][0]["text"]
