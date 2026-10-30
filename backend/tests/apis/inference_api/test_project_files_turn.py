"""``project_files`` on the turn path: a project harness's turn, and only that, gets it.

The tool itself is covered by ``tests/agents/builtin_tools/test_project_files_tool.py``.
Here: where it is built, in what order (after the memory family, so those specs
keep their place in the cached ``toolConfig``), that an ordinary Agent's turn
never sees it, that it is memoized per member and project, and that the harness
guidance names it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.project_memory import ProjectMemoryTurn, build_project_files_tools
from apis.shared.auth.models import User

MEMBER = User(user_id="u-member", email="member@example.edu", name="Mem", roles=["default"])
OTHER = User(user_id="u-other", email="other@example.edu", name="Oth", roles=["default"])


def _captured_users(fn) -> list:
    """Every ``User`` a function (or a function it closes over) captured."""
    found, seen = [], set()

    def walk(f):
        if id(f) in seen or not getattr(f, "__closure__", None):
            return
        seen.add(id(f))
        for cell in f.__closure__:
            value = cell.cell_contents
            if isinstance(value, User):
                found.append(value)
            elif callable(value):
                walk(value)

    walk(fn)
    return found


def _names(monkeypatch, *, project_memory, turn_project_id, assistant_id="ast_harness"):
    monkeypatch.setattr(routes, "_build_document_tools", AsyncMock(return_value=[]))
    tools = asyncio.run(routes._build_turn_tools(
        InvocationRequest(session_id="s1", rag_assistant_id=assistant_id), MEMBER, MEMBER.user_id, [],
        project_memory=project_memory, turn_has_document=False, turn_project_id=turn_project_id,
    ))
    return [t.tool_spec["name"] for t in tools.extra_tools]


class TestTheTurn:
    def test_a_harness_turn_gets_project_files_after_the_memory_family(self, monkeypatch):
        turn = ProjectMemoryTurn(project_id="prj_1", shared_space_id="spc_1", personal_space_id=None)
        assert _names(monkeypatch, project_memory=turn, turn_project_id="prj_1") == [
            "memory_list", "memory_read", "memory_query", "memory_save", "memory_propose",
            "shared_conversations_list", "shared_conversation_read",
            "project_files",
        ]

    def test_a_harness_turn_without_memory_spaces_still_gets_it(self, monkeypatch):
        """A project's Files exist whether or not the deployment runs memory spaces."""
        assert _names(monkeypatch, project_memory=None, turn_project_id="prj_1") == ["project_files"]

    def test_an_ordinary_agents_turn_never_gets_it(self, monkeypatch):
        assert "project_files" not in _names(monkeypatch, project_memory=None, turn_project_id=None)

    def test_a_turn_with_no_assistant_cannot_build_it(self, monkeypatch):
        assert "project_files" not in _names(monkeypatch, project_memory=None, turn_project_id="prj_1", assistant_id=None)

    def test_the_default_leaves_every_existing_caller_unchanged(self, monkeypatch):
        monkeypatch.setattr(routes, "_build_document_tools", AsyncMock(return_value=[]))
        tools = asyncio.run(routes._build_turn_tools(
            InvocationRequest(session_id="s1", rag_assistant_id="ast_1"), MEMBER, MEMBER.user_id, [],
            project_memory=None, turn_has_document=False,
        ))
        assert tools.extra_tools == []


class TestBuilder:
    def test_built_once_per_member_project_and_harness(self):
        first = build_project_files_tools("prj_memo", "ast_memo", MEMBER)
        assert build_project_files_tools("prj_memo", "ast_memo", MEMBER) == first
        assert build_project_files_tools("prj_memo", "ast_memo", OTHER) != first
        assert build_project_files_tools("prj_memo", "ast_other", MEMBER) != first

    def test_the_tool_carries_no_bearer_token(self):
        from dataclasses import replace

        (tool,) = build_project_files_tools("prj_tok", "ast_tok", replace(MEMBER, raw_token="secret-token"))
        captured = _captured_users(tool._tool_func)
        assert captured and all(u.raw_token is None for u in captured)


class TestGuidance:
    def test_the_harness_is_told_files_are_searched_by_passage_and_read_by_name(self):
        """2026-10-29: a faculty project with 41 submissions was told the assistant had no way to
        read Files. The guidance names the tool and forbids that answer."""
        guidance = routes.PROJECT_WORKSPACE_GUIDANCE
        assert "that search sees passages, not files" in guidance
        assert "call `project_files`" in guidance
        # The report's own wording: "list_word_documents and list_spreadsheets return empty" was read as
        # proof the Files could not be reached. Those tools are scoped to the conversation's attachments.
        assert "see only files attached to this conversation" in guidance
        assert "an empty listing from them says nothing about the project's Files" in guidance
        assert "Never say you cannot read the project's files" in guidance
        assert "project_files" not in routes.compose_agent_system_prompt("BASE", "Do X.", project_harness=False)

    def test_the_guidance_takes_nothing_from_the_turn(self):
        """Constant text: two members of one project render one cached prefix."""
        assert isinstance(routes.PROJECT_WORKSPACE_GUIDANCE, str)
        assert "{" not in routes.PROJECT_WORKSPACE_GUIDANCE
