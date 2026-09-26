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
    MemoryValidationError,
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("slug", ["MEMORY.md", "memory.md", " MEMORY.MD "])
    async def test_memory_md_slug_reads_index(self, monkeypatch, slug):
        svc = _patch_service(monkeypatch)
        svc.read_index.return_value = "# Index\n- [[jane]]"
        tool = make_memory_read_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug=slug)
        assert result["status"] == "success"
        assert result["content"][0]["text"] == "# Index\n- [[jane]]"
        # routed to the index, never the entry manifest
        svc.read_index.assert_called_once_with("spc_1", "u1", "u1@x.edu")
        svc.read_entry.assert_not_called()


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
        # a tool write is the "direct save from a task" path in file history
        assert kwargs.kwargs["reason"] == "save"
        assert kwargs.kwargs["description"] == "person"

    @pytest.mark.asyncio
    async def test_an_omitted_description_is_not_sent_as_empty(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.write_entry.return_value = SimpleNamespace(slug="jane", entry_type="fact")
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        await _call(tool, slug="jane", body="- x")
        assert svc.write_entry.call_args.kwargs["description"] is None

    @pytest.mark.asyncio
    async def test_a_validation_failure_reaches_the_model_verbatim(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.write_entry.side_effect = MemoryValidationError(
            "Line 1 is not part of a list item", code="prose_in_body"
        )
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="jane", body="prose")
        assert result["status"] == "error"
        assert "Line 1 is not part of a list item" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_write_permission_error_is_error_result(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.write_entry.side_effect = MemorySpacePermissionError("read-only")
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="jane", body="x")
        assert result["status"] == "error" and "don't have write access" in result["content"][0]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("slug", ["MEMORY.md", "memory.md"])
    async def test_memory_md_slug_updates_index(self, monkeypatch, slug):
        svc = _patch_service(monkeypatch)
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug=slug, body="# Index\n- [[jane]]", entry_type="entity")
        assert result["status"] == "success"
        assert "Updated the MEMORY.md index" in result["content"][0]["text"]
        # routed to update_index (body only); never creates an entry
        svc.update_index.assert_called_once_with("spc_1", "u1", "u1@x.edu", "# Index\n- [[jane]]")
        svc.write_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_memory_md_write_permission_error_is_error_result(self, monkeypatch):
        svc = _patch_service(monkeypatch)
        svc.update_index.side_effect = MemorySpacePermissionError("read-only")
        tool = make_memory_write_tool("spc_1", "Brain", "u1", "u1@x.edu")
        result = await _call(tool, slug="MEMORY.md", body="x")
        assert result["status"] == "error" and "don't have write access" in result["content"][0]["text"]


class TestSpecsAreByteIdentical:
    """Shared Projects 2.4b added a scope-addressed family for project harnesses.

    Ordinary Agents' specs are part of their prompt-cached ``toolConfig``, so
    they must not move by a byte. Hashes pinned from ``origin/develop``
    before 2.4b; a deliberate change updates them and says so in its PR.
    """

    PINNED = {
        "memory_list": "555a3853395ac8b17c4f5d2affac75bd8904a4a129cfc33d2a12fd1d7f021bc2",
        "memory_read": "e07c642b6e9692e5b85470b8b7e98087b2136922943b43a872d7d904f45627e1",
        "memory_write": "c528910e334fd5fe9697c3a456594be0700c6b68e00b178de12a12dc35ed063e",
    }

    def test_spec_hashes(self):
        import hashlib
        import json

        for factory in (make_memory_list_tool, make_memory_read_tool, make_memory_write_tool):
            spec = factory("spc_1", "Brain", "u1", "u1@example.edu").tool_spec
            digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            assert digest == self.PINNED[spec["name"]], spec["name"]

    def test_specs_do_not_depend_on_the_binding(self):
        for factory in (make_memory_list_tool, make_memory_read_tool, make_memory_write_tool):
            a = factory("spc_1", "Brain", "u1", "u1@example.edu").tool_spec
            b = factory("spc_2", "Other", "u2", "u2@example.edu").tool_spec
            assert a == b


class TestProjectHarnessSpecs:
    def _specs(self):
        from agents.builtin_tools.memory_spaces.project_tools import (
            ProjectMemoryScopes,
            make_project_memory_tools,
        )
        from apis.shared.auth.models import User

        user = User(email="member@example.edu", user_id="u1", name="M", roles=[])
        tools = make_project_memory_tools(ProjectMemoryScopes.for_member("prj_1", "spc_1", None, user))
        return {t.tool_spec["name"]: t.tool_spec for t in tools}

    def test_every_tool_takes_a_scope_enum(self):
        specs = self._specs()
        assert list(specs) == ["memory_list", "memory_read", "memory_query", "memory_save"]
        for spec in specs.values():
            schema = spec["inputSchema"]["json"]
            assert "scope" in schema["required"]
            scope = schema["properties"]["scope"]
            assert scope["enum"] == ["project", "mine"]

    def test_save_teaches_the_item_format(self):
        description = self._specs()["memory_save"]["description"]
        assert 'one fact per "- " line' in description
        assert "prose and headings are rejected" in description
        assert "editor" in description

    def test_specs_are_the_same_for_every_member_and_project(self):
        from agents.builtin_tools.memory_spaces.project_tools import (
            ProjectMemoryScopes,
            make_project_memory_tools,
        )
        from apis.shared.auth.models import User

        other = User(email="viewer@example.edu", user_id="u2", name="V", roles=[])
        tools = make_project_memory_tools(ProjectMemoryScopes.for_member("prj_2", None, "spc_9", other))
        assert {t.tool_spec["name"]: t.tool_spec for t in tools} == self._specs()
