"""Workspace tool factories (agents/builtin_tools/workspace_tools.py).

Each tool is closed over the request identity; the shared workspace service
functions are patched. Verifies success payload shapes, the workspace_file
download-card contract on write, and that service failures surface as error
tool-results rather than raising.
"""

import json
from unittest.mock import AsyncMock

import pytest

from agents.builtin_tools.workspace_tools import (
    make_workspace_list_tool,
    make_workspace_read_tool,
    make_workspace_write_tool,
)
from apis.shared.files.workspace import (
    WorkspaceFileNotFoundError,
    WorkspaceStorageNotConfiguredError,
    WorkspaceValidationError,
)

MODULE = "agents.builtin_tools.workspace_tools"


class TestRoutesGating:
    """_build_workspace_tools in apis/inference_api/chat/routes.py."""

    @pytest.mark.parametrize(
        "enabled,expected",
        [(None, 0), ([], 0), (["calculator"], 0), (["workspace_files"], 3)],
    )
    def test_gate_key_provisions_full_toolset(self, enabled, expected):
        from apis.inference_api.chat.routes import _build_workspace_tools

        tools = _build_workspace_tools(enabled, "s1", "u1")
        assert len(tools) == expected

    def test_kill_switch_disables(self, monkeypatch):
        from apis.inference_api.chat.routes import _build_workspace_tools

        monkeypatch.setenv("WORKSPACE_TOOLS_ENABLED", "false")
        assert _build_workspace_tools(["workspace_files"], "s1", "u1") == []

    def test_empty_flag_value_stays_enabled(self, monkeypatch):
        from apis.inference_api.chat.routes import _build_workspace_tools

        monkeypatch.setenv("WORKSPACE_TOOLS_ENABLED", "")
        assert len(_build_workspace_tools(["workspace_files"], "s1", "u1")) == 3


async def _call(tool, *args, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(*args, **kwargs)


class TestWorkspaceList:
    @pytest.mark.asyncio
    async def test_lists_files(self, monkeypatch):
        svc = AsyncMock(return_value={"scope": "session", "files": [], "count": 0, "truncated": False})
        monkeypatch.setattr(f"{MODULE}.list_workspace_files", svc)
        tool = make_workspace_list_tool("s1", "u1")
        result = await _call(tool)
        assert result["status"] == "success"
        assert result["content"][0]["json"]["scope"] == "session"
        svc.assert_awaited_once_with("u1", "s1", scope="session")

    @pytest.mark.asyncio
    async def test_scope_passthrough(self, monkeypatch):
        svc = AsyncMock(return_value={"scope": "user", "files": [], "count": 0, "truncated": False})
        monkeypatch.setattr(f"{MODULE}.list_workspace_files", svc)
        tool = make_workspace_list_tool("s1", "u1")
        await _call(tool, scope="user")
        svc.assert_awaited_once_with("u1", "s1", scope="user")

    @pytest.mark.asyncio
    async def test_storage_not_configured_is_error_result(self, monkeypatch):
        svc = AsyncMock(side_effect=WorkspaceStorageNotConfiguredError("no bucket"))
        monkeypatch.setattr(f"{MODULE}.list_workspace_files", svc)
        tool = make_workspace_list_tool("s1", "u1")
        result = await _call(tool)
        assert result["status"] == "error"
        assert "not configured" in result["content"][0]["text"]


class TestWorkspaceRead:
    @pytest.mark.asyncio
    async def test_reads_with_offset(self, monkeypatch):
        svc = AsyncMock(return_value={"encoding": "text", "content": "hi"})
        monkeypatch.setattr(f"{MODULE}.read_workspace_file", svc)
        tool = make_workspace_read_tool("s1", "u1")
        result = await _call(tool, upload_id="f1", offset=8)
        assert result["status"] == "success"
        assert result["content"][0]["json"]["content"] == "hi"
        svc.assert_awaited_once_with("u1", "f1", offset=8)

    @pytest.mark.asyncio
    async def test_not_found_is_error_result(self, monkeypatch):
        svc = AsyncMock(side_effect=WorkspaceFileNotFoundError("No file with id 'ghost'"))
        monkeypatch.setattr(f"{MODULE}.read_workspace_file", svc)
        tool = make_workspace_read_tool("s1", "u1")
        result = await _call(tool, upload_id="ghost")
        assert result["status"] == "error"
        assert "ghost" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_error_result(self, monkeypatch):
        svc = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(f"{MODULE}.read_workspace_file", svc)
        tool = make_workspace_read_tool("s1", "u1")
        result = await _call(tool, upload_id="f1")
        assert result["status"] == "error"


class TestWorkspaceWrite:
    @pytest.mark.asyncio
    async def test_returns_workspace_file_download_card(self, monkeypatch):
        svc = AsyncMock(
            return_value={
                "upload_id": "up1",
                "filename": "report.md",
                "mime_type": "text/markdown",
                "size_bytes": 4,
                "size_kb": "0.0 KB",
                "download_url": "https://signed.example/f",
            }
        )
        monkeypatch.setattr(f"{MODULE}.write_workspace_file", svc)
        tool = make_workspace_write_tool("s1", "u1")
        result = await _call(tool, filename="report.md", content="# Hi", mime_type="text/markdown")

        card = json.loads(result)
        assert card["success"] is True
        assert card["ui_type"] == "workspace_file"
        assert card["ui_display"] == "inline"
        assert card["payload"] == {
            "filename": "report.md",
            "download_url": "https://signed.example/f",
            "size_kb": "0.0 KB",
        }
        svc.assert_awaited_once_with(
            "u1", "s1", "report.md", "# Hi", mime_type="text/markdown"
        )

    @pytest.mark.asyncio
    async def test_validation_failure_is_error_result(self, monkeypatch):
        svc = AsyncMock(side_effect=WorkspaceValidationError("bad extension"))
        monkeypatch.setattr(f"{MODULE}.write_workspace_file", svc)
        tool = make_workspace_write_tool("s1", "u1")
        result = await _call(tool, filename="x.csv", content="a", mime_type="text/markdown")
        assert result["status"] == "error"
        assert "bad extension" in result["content"][0]["text"]
