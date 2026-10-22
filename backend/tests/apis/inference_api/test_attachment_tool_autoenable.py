"""Spreadsheet Analysis auto-enable on attachment (routes.py, P2-E of
docs/specs/load-test-assessment-2026-09.md).

CSV/XLSX never go inline and the analysis tools are opt-in in the picker, so
on the default tool set an attached spreadsheet was a dead end. A session
that holds one now gets the tools injected for the turn — gated on the
caller's RBAC grant (enables, never grants) and sticky across the session so
the agent-cache key does not flip between the attach turn and the follow-up.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import routes
from apis.shared.feature_flags import attachment_tool_autoenable_enabled
from apis.shared.tools.injected import SPREADSHEET_TOOL_IDS

USER = SimpleNamespace(user_id="u1")


@pytest.fixture(autouse=True)
def _clean_memo():
    routes._TABULAR_SESSIONS.clear()
    yield
    routes._TABULAR_SESSIONS.clear()


def _role_service(allowed):
    svc = SimpleNamespace()
    svc.can_access_tool = AsyncMock(side_effect=lambda user, tool_id: tool_id in allowed or "*" in allowed)
    return svc


class TestFlag:
    def test_defaults_on(self, monkeypatch):
        monkeypatch.delenv("ATTACHMENT_TOOL_AUTOENABLE_ENABLED", raising=False)
        assert attachment_tool_autoenable_enabled()

    def test_empty_is_on_and_false_is_off(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_TOOL_AUTOENABLE_ENABLED", "")
        assert attachment_tool_autoenable_enabled()
        monkeypatch.setenv("ATTACHMENT_TOOL_AUTOENABLE_ENABLED", "FALSE")
        assert not attachment_tool_autoenable_enabled()


class TestWithAutoEnabledTools:
    def test_appends_missing_ids_in_the_order_given(self):
        assert routes._with_auto_enabled_tools(["calculator"], ["analyze_spreadsheet", "list_spreadsheets"]) == [
            "calculator", "analyze_spreadsheet", "list_spreadsheets",
        ]

    def test_none_input_becomes_just_the_auto_ids(self):
        assert routes._with_auto_enabled_tools(None, ["analyze_spreadsheet"]) == ["analyze_spreadsheet"]

    def test_returns_the_same_object_when_nothing_to_add(self):
        original = ["analyze_spreadsheet", "list_spreadsheets", "calculator"]
        assert routes._with_auto_enabled_tools(original, ["list_spreadsheets"]) is original
        assert routes._with_auto_enabled_tools(None, []) is None

    def test_does_not_duplicate(self):
        assert routes._with_auto_enabled_tools(["analyze_spreadsheet"], ["analyze_spreadsheet", "list_spreadsheets"]) == [
            "analyze_spreadsheet", "list_spreadsheets",
        ]


class TestSessionHasTabular:
    @pytest.mark.asyncio
    async def test_this_turns_attachment_answers_without_a_query_and_is_remembered(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock()) as lookup:
            assert await routes._session_has_tabular("s1", "u1", turn_has_tabular=True)
            assert await routes._session_has_tabular("s1", "u1")  # follow-up turn: sticky
        lookup.assert_not_awaited()
        assert routes._TABULAR_SESSIONS.get("s1") is True

    @pytest.mark.asyncio
    async def test_queries_once_and_memoizes_a_positive_answer(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=True)) as lookup:
            assert await routes._session_has_tabular("s1", "u1")
            assert await routes._session_has_tabular("s1", "u1")
        assert lookup.await_count == 1

    @pytest.mark.asyncio
    async def test_negative_answers_are_not_memoized(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)) as lookup:
            assert not await routes._session_has_tabular("s1", "u1")
            assert not await routes._session_has_tabular("s1", "u1")
        assert lookup.await_count == 2
        assert "s1" not in routes._TABULAR_SESSIONS

    @pytest.mark.asyncio
    async def test_lookup_failure_is_fail_closed(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(side_effect=RuntimeError("ddb"))):
            assert not await routes._session_has_tabular("s1", "u1")

    @pytest.mark.asyncio
    async def test_missing_identity_is_false_without_a_query(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock()) as lookup:
            assert not await routes._session_has_tabular("", "u1")
            assert not await routes._session_has_tabular("s1", "")
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unbound_agents_knowledge_base_spreadsheet_enables_the_tools(self):
        """An Agent that binds no tools no longer reads the viewer's picker, which was
        the only way the analysis tools reached it; its knowledge base now does."""
        with patch(
            "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)
        ), patch(
            "apis.shared.assistants.kb_tabular.assistant_kb_has_tabular_documents",
            new=AsyncMock(return_value=True),
        ) as kb:
            assert await routes._session_has_tabular("s1", "u1", kb_assistant_id="ast-1")
            assert await routes._session_has_tabular("s1", "u1", kb_assistant_id="ast-1")
        # Positive answers stick, as for an attachment, so the cache key holds still.
        assert kb.await_count == 1
        assert kb.await_args.args == ("ast-1",)

    @pytest.mark.asyncio
    async def test_without_an_unbound_agent_the_knowledge_base_is_never_read(self):
        with patch(
            "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)
        ), patch(
            "apis.shared.assistants.kb_tabular.assistant_kb_has_tabular_documents", new=AsyncMock()
        ) as kb:
            assert not await routes._session_has_tabular("s1", "u1")
        kb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_knowledge_base_lookup_failure_is_fail_closed(self):
        with patch(
            "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)
        ), patch(
            "apis.shared.assistants.kb_tabular.assistant_kb_has_tabular_documents",
            new=AsyncMock(side_effect=RuntimeError("ddb")),
        ):
            assert not await routes._session_has_tabular("s1", "u1", kb_assistant_id="ast-1")
        assert "s1" not in routes._TABULAR_SESSIONS

    def test_memo_is_bounded(self, monkeypatch):
        monkeypatch.setattr(routes, "_TABULAR_SESSIONS_MAX", 3)
        for sid in ("s1", "s2", "s3", "s4"):
            routes._remember_tabular_session(sid)
        assert list(routes._TABULAR_SESSIONS) == ["s2", "s3", "s4"]


class TestAutoEnabledIds:
    @pytest.mark.asyncio
    async def test_grants_every_spreadsheet_id_the_role_admits_in_fixed_order(self):
        with patch.object(routes, "get_app_role_service", return_value=_role_service(set(SPREADSHEET_TOOL_IDS))):
            ids = await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True)
        assert ids == sorted(SPREADSHEET_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_wildcard_grant_admits_all(self):
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            ids = await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True)
        assert ids == sorted(SPREADSHEET_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_enables_never_grants(self):
        # A role that carries neither id gets nothing; one that carries one id gets that one.
        with patch.object(routes, "get_app_role_service", return_value=_role_service(set())):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True) == []
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"analyze_spreadsheet"})):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True) == [
                "analyze_spreadsheet"
            ]

    @pytest.mark.asyncio
    async def test_no_spreadsheet_in_session_means_no_rbac_call(self):
        svc = _role_service({"*"})
        with patch.object(routes, "get_app_role_service", return_value=svc), patch(
            "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)
        ):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1") == []
        svc.can_access_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_TOOL_AUTOENABLE_ENABLED", "false")
        svc = _role_service({"*"})
        with patch.object(routes, "get_app_role_service", return_value=svc):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True) == []
        svc.can_access_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rbac_failure_does_not_fail_the_turn(self):
        svc = SimpleNamespace(can_access_tool=AsyncMock(side_effect=RuntimeError("rbac down")))
        with patch.object(routes, "get_app_role_service", return_value=svc):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_tabular=True) == []


class TestApply:
    @pytest.mark.asyncio
    async def test_attach_turn_then_follow_up_produce_the_same_effective_list(self):
        """The cache key hashes enabled_tools; the attach turn and the next
        turn (no upload on it) must agree or the slot flips."""
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            attach_turn = await routes._apply_attachment_tool_autoenable(
                ["calculator"], USER, "s1", "u1", turn_has_tabular=True
            )
            follow_up = await routes._apply_attachment_tool_autoenable(["calculator"], USER, "s1", "u1")
        assert attach_turn == follow_up == ["calculator", "analyze_spreadsheet", "list_spreadsheets"]

    @pytest.mark.asyncio
    async def test_picker_already_on_is_untouched(self):
        original = ["list_spreadsheets", "analyze_spreadsheet"]
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            result = await routes._apply_attachment_tool_autoenable(original, USER, "s1", "u1", turn_has_tabular=True)
        assert result is original

    @pytest.mark.asyncio
    async def test_no_spreadsheet_means_list_passes_through_untouched(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)):
            assert await routes._apply_attachment_tool_autoenable(None, USER, "s1", "u1") is None


class TestSharedLookup:
    @pytest.mark.asyncio
    async def test_session_has_tabular_files_matches_csv_and_xlsx_for_the_owner_only(self):
        from apis.shared.files import document_read as dr

        rows = [
            SimpleNamespace(user_id="u1", filename="notes.pdf", mime_type="application/pdf"),
            SimpleNamespace(user_id="u2", filename="theirs.csv", mime_type="text/csv"),
            SimpleNamespace(user_id="u1", filename="mine.csv", mime_type="text/csv"),
        ]
        repo = SimpleNamespace(list_session_files=AsyncMock(return_value=rows))
        with patch.object(dr, "get_file_upload_repository", return_value=repo):
            assert await dr.session_has_tabular_files("u1", "s1")
            rows.pop()  # only the other user's CSV and a PDF remain
            assert not await dr.session_has_tabular_files("u1", "s1")


class TestKnowledgeBaseLookup:
    """`assistant_kb_has_tabular_documents` reads the same DOC# rows, with the same
    tests, that `list_spreadsheets` lists from."""

    @staticmethod
    def _table(*pages):
        responses = list(pages)
        table = SimpleNamespace(calls=[])

        def query(**kwargs):
            table.calls.append(kwargs)
            return responses.pop(0)

        table.query = query
        return table

    @pytest.mark.asyncio
    async def test_only_a_complete_spreadsheet_counts(self, monkeypatch):
        from apis.shared.assistants import kb_tabular

        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "assistants")
        table = self._table({"Items": [
            {"status": "complete", "filename": "syllabus.pdf", "contentType": "application/pdf"},
            {"status": "processing", "filename": "grades.csv", "contentType": "text/csv"},
        ]})
        with patch("apis.shared.aws_clients.get_dynamodb_table", return_value=table):
            assert not await kb_tabular.assistant_kb_has_tabular_documents("ast-1")

        table = self._table({"Items": [
            {"status": "complete", "filename": "roster.xlsx", "content_type": ""},
        ]})
        with patch("apis.shared.aws_clients.get_dynamodb_table", return_value=table):
            assert await kb_tabular.assistant_kb_has_tabular_documents("ast-1")

    @pytest.mark.asyncio
    async def test_pages_until_it_finds_one(self, monkeypatch):
        from apis.shared.assistants import kb_tabular

        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", "assistants")
        table = self._table(
            {"Items": [{"status": "complete", "filename": "a.pdf"}], "LastEvaluatedKey": {"SK": "DOC#1"}},
            {"Items": [{"status": "complete", "filename": "b.csv", "contentType": "text/csv"}]},
        )
        with patch("apis.shared.aws_clients.get_dynamodb_table", return_value=table):
            assert await kb_tabular.assistant_kb_has_tabular_documents("ast-1")
        assert table.calls[1]["ExclusiveStartKey"] == {"SK": "DOC#1"}

    @pytest.mark.asyncio
    async def test_no_table_or_no_assistant_is_false(self, monkeypatch):
        from apis.shared.assistants import kb_tabular

        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        assert not await kb_tabular.assistant_kb_has_tabular_documents("ast-1")
        assert not await kb_tabular.assistant_kb_has_tabular_documents("")
