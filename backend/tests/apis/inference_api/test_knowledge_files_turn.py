"""``knowledge_files`` on the turn path: an ordinary Agent's turn gets it while the flag is on.

A project harness keeps ``project_files`` and never gets both. The tool itself is
covered by ``tests/agents/builtin_tools/test_knowledge_files_tool.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.project_memory import ProjectMemoryTurn, build_knowledge_files_tools
from apis.shared.auth.models import User
from apis.shared.feature_flags import agent_knowledge_files_enabled

USER = User(user_id="u-user", email="user@example.edu", name="Use", roles=["default"])
OTHER = User(user_id="u-other", email="other@example.edu", name="Oth", roles=["default"])
FLAG = "AGENT_KNOWLEDGE_FILES_ENABLED"


def _tools(monkeypatch, *, assistant_id="ast_agent", turn_project_id=None, project_memory=None):
    monkeypatch.setattr(routes, "_build_document_tools", AsyncMock(return_value=[]))
    return asyncio.run(routes._build_turn_tools(
        InvocationRequest(session_id="s1", rag_assistant_id=assistant_id), USER, USER.user_id, [],
        project_memory=project_memory, turn_has_document=False, turn_project_id=turn_project_id,
    ))


def _names(monkeypatch, **kwargs):
    return [t.tool_spec["name"] for t in _tools(monkeypatch, **kwargs).extra_tools]


class TestFlag:
    @pytest.mark.parametrize("value", ["", "false", "1", "yes", "on"])
    def test_only_true_enables_it(self, monkeypatch, value):
        monkeypatch.setenv(FLAG, value)
        assert agent_knowledge_files_enabled() is False

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert agent_knowledge_files_enabled() is False

    @pytest.mark.parametrize("value", ["true", " TRUE "])
    def test_true_is_on(self, monkeypatch, value):
        monkeypatch.setenv(FLAG, value)
        assert agent_knowledge_files_enabled() is True


class TestTheTurn:
    def test_with_the_flag_on_an_agent_turn_gets_knowledge_files(self, monkeypatch):
        monkeypatch.setenv(FLAG, "true")
        tools = _tools(monkeypatch)
        assert [t.tool_spec["name"] for t in tools.extra_tools] == ["knowledge_files"]
        # Closes over (assistant, user), both cache-key elements: the agent stays cacheable.
        assert tools.extra_tools_key_described is True

    def test_with_the_flag_off_the_toolset_is_unchanged(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        assert _names(monkeypatch) == []

    def test_a_project_harness_turn_gets_project_files_and_not_knowledge_files(self, monkeypatch):
        monkeypatch.setenv(FLAG, "true")
        names = _names(monkeypatch, turn_project_id="prj_1")
        assert names == ["project_files"]

    def test_with_memory_spaces_it_still_comes_after_the_memory_family(self, monkeypatch):
        monkeypatch.setenv(FLAG, "true")
        turn = ProjectMemoryTurn(project_id="prj_1", shared_space_id="spc_1", personal_space_id=None)
        names = _names(monkeypatch, turn_project_id="prj_1", project_memory=turn)
        assert names[-1] == "project_files" and "knowledge_files" not in names

    def test_a_plain_chat_turn_with_no_agent_gets_nothing(self, monkeypatch):
        monkeypatch.setenv(FLAG, "true")
        assert _names(monkeypatch, assistant_id=None) == []

    def test_building_it_does_no_io(self, monkeypatch):
        """Nothing before the first token: the builder makes a closure, and the Query,
        the access check and the S3 read wait for the model to call it."""
        monkeypatch.setenv(FLAG, "true")

        def _io(*args, **kwargs):
            raise AssertionError("IO while building the turn's tools")

        monkeypatch.setattr("agents.builtin_tools.project_files_tool.list_kb_documents", _io)
        monkeypatch.setattr("apis.shared.assistants.kb_access.resolve_kb_access", _io)
        assert _names(monkeypatch, assistant_id="ast_no_io") == ["knowledge_files"]


class TestBuilder:
    def test_built_once_per_user_and_agent(self):
        first = build_knowledge_files_tools("ast_memo", USER)
        assert build_knowledge_files_tools("ast_memo", USER) == first
        assert build_knowledge_files_tools("ast_memo", OTHER) != first
        assert build_knowledge_files_tools("ast_other", USER) != first

    def test_the_tool_carries_no_bearer_token(self):
        from dataclasses import replace

        (tool,) = build_knowledge_files_tools("ast_tok", replace(USER, raw_token="secret-token"))
        seen, visited = [], set()

        def walk(fn):
            if id(fn) in visited or not getattr(fn, "__closure__", None):
                return
            visited.add(id(fn))
            for cell in fn.__closure__:
                value = cell.cell_contents
                if isinstance(value, User):
                    seen.append(value)
                elif callable(value):
                    walk(value)

        walk(tool._tool_func)
        assert seen and all(u.raw_token is None for u in seen)
