"""Spreadsheet Analysis and PowerPoint auto-enable on attachment (routes.py,
P2-E of docs/specs/load-test-assessment-2026-09.md; decks since EH-4 of
docs/specs/eval-harness.md).

CSV/XLSX never go inline and the analysis tools are opt-in in the picker, so
on the default tool set an attached spreadsheet was a dead end. A session
that holds one now gets the tools injected for the turn — gated on the
caller's RBAC grant (enables, never grants) and sticky across the session so
the agent-cache key does not flip between the attach turn and the follow-up.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import routes
from apis.shared.feature_flags import attachment_tool_autoenable_enabled
from apis.shared.files.document_read import (
    session_has_presentation_files as _real_session_has_presentation_files,
    session_has_tabular_files as _real_session_has_tabular_files,
)
from apis.shared.tools.injected import POWERPOINT_PRESENTATION_TOOL_IDS, SPREADSHEET_TOOL_IDS

USER = SimpleNamespace(user_id="u1")


@pytest.fixture(autouse=True)
def _clean_memo():
    routes._TABULAR_SESSIONS.clear()
    routes._PRESENTATION_SESSIONS.clear()
    yield
    routes._TABULAR_SESSIONS.clear()
    routes._PRESENTATION_SESSIONS.clear()


@pytest.fixture(autouse=True)
def _no_spreadsheet_in_session():
    """Likewise for the spreadsheet gate; tests that need one patch over it."""
    with patch(
        "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=False)
    ) as lookup:
        yield lookup


@pytest.fixture(autouse=True)
def _no_deck_in_session():
    """The deck gate runs beside the spreadsheet gate on every call; without a
    deck in the session its lookup answers False and never leaves the box."""
    with patch(
        "apis.shared.files.document_read.session_has_presentation_files", new=AsyncMock(return_value=False)
    ) as lookup:
        yield lookup


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
        # The deck gate checks its own id first (see TestPresentationAutoEnable);
        # no spreadsheet id is ever looked up.
        checked = {call.args[1] for call in svc.can_access_tool.await_args_list}
        assert not checked & SPREADSHEET_TOOL_IDS

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
            assert await _real_session_has_tabular_files("u1", "s1")
            rows.pop()  # only the other user's CSV and a PDF remain
            assert not await _real_session_has_tabular_files("u1", "s1")


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


class TestSessionHasPresentation:
    """The deck gate, same contract as the spreadsheet gate above."""

    @pytest.mark.asyncio
    async def test_this_turns_attachment_answers_without_a_query_and_is_remembered(self, _no_deck_in_session):
        assert await routes._session_has_presentation("s1", "u1", turn_has_presentation=True)
        assert await routes._session_has_presentation("s1", "u1")  # follow-up turn: sticky
        _no_deck_in_session.assert_not_awaited()
        assert routes._PRESENTATION_SESSIONS.get("s1") is True

    @pytest.mark.asyncio
    async def test_queries_once_and_memoizes_a_positive_answer(self, _no_deck_in_session):
        _no_deck_in_session.return_value = True
        assert await routes._session_has_presentation("s1", "u1")
        assert await routes._session_has_presentation("s1", "u1")
        assert _no_deck_in_session.await_count == 1

    @pytest.mark.asyncio
    async def test_negative_answers_are_not_memoized(self, _no_deck_in_session):
        assert not await routes._session_has_presentation("s1", "u1")
        assert not await routes._session_has_presentation("s1", "u1")
        assert _no_deck_in_session.await_count == 2
        assert "s1" not in routes._PRESENTATION_SESSIONS

    @pytest.mark.asyncio
    async def test_lookup_failure_is_fail_closed(self, _no_deck_in_session):
        _no_deck_in_session.side_effect = RuntimeError("ddb")
        assert not await routes._session_has_presentation("s1", "u1")
        assert "s1" not in routes._PRESENTATION_SESSIONS

    @pytest.mark.asyncio
    async def test_missing_identity_is_false_without_a_query(self, _no_deck_in_session):
        assert not await routes._session_has_presentation("", "u1")
        assert not await routes._session_has_presentation("s1", "")
        _no_deck_in_session.assert_not_awaited()

    def test_memo_is_bounded(self, monkeypatch):
        monkeypatch.setattr(routes, "_PRESENTATION_SESSIONS_MAX", 3)
        for sid in ("s1", "s2", "s3", "s4"):
            routes._remember_presentation_session(sid)
        assert list(routes._PRESENTATION_SESSIONS) == ["s2", "s3", "s4"]


class TestPresentationAutoEnable:
    """A deck never goes inline, and the PowerPoint read tools exist only when
    `create_powerpoint_presentation` is on — which it ships off. A session that
    holds a deck now gets it, gated on the caller's grant."""

    @pytest.mark.asyncio
    async def test_a_deck_on_this_turn_enables_the_tool(self, _no_deck_in_session):
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            ids = await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_presentation=True)
        assert ids == sorted(POWERPOINT_PRESENTATION_TOOL_IDS)
        _no_deck_in_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_deck_uploaded_earlier_in_the_session_enables_the_tool(self, _no_deck_in_session):
        _no_deck_in_session.return_value = True
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"create_powerpoint_presentation"})):
            ids = await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1")
        assert ids == ["create_powerpoint_presentation"]

    @pytest.mark.asyncio
    async def test_enables_never_grants_and_skips_the_query(self, _no_deck_in_session):
        _no_deck_in_session.return_value = True
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"analyze_spreadsheet"})):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_presentation=True) == []
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1") == []
        # A role without the tool never pays the session query.
        _no_deck_in_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_on_needs_no_rbac_call_and_no_query(self, _no_deck_in_session):
        svc = _role_service({"*"})
        with patch.object(routes, "get_app_role_service", return_value=svc):
            ids = await routes._auto_enabled_attachment_tool_ids(
                USER, "s1", "u1", turn_has_presentation=True,
                enabled_tools=["create_powerpoint_presentation"],
            )
        assert ids == []
        svc.can_access_tool.assert_not_awaited()
        _no_deck_in_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_switch(self, monkeypatch, _no_deck_in_session):
        monkeypatch.setenv("ATTACHMENT_TOOL_AUTOENABLE_ENABLED", "false")
        svc = _role_service({"*"})
        with patch.object(routes, "get_app_role_service", return_value=svc):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_presentation=True) == []
        svc.can_access_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rbac_failure_does_not_fail_the_turn(self):
        svc = SimpleNamespace(can_access_tool=AsyncMock(side_effect=RuntimeError("rbac down")))
        with patch.object(routes, "get_app_role_service", return_value=svc):
            assert await routes._auto_enabled_attachment_tool_ids(USER, "s1", "u1", turn_has_presentation=True) == []

    @pytest.mark.asyncio
    async def test_spreadsheet_and_deck_together_in_a_fixed_order(self):
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            ids = await routes._auto_enabled_attachment_tool_ids(
                USER, "s1", "u1", turn_has_tabular=True, turn_has_presentation=True
            )
        assert ids == sorted(SPREADSHEET_TOOL_IDS) + sorted(POWERPOINT_PRESENTATION_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_attach_turn_then_follow_up_produce_the_same_effective_list(self):
        """The cache key hashes enabled_tools: the deck's attach turn and the
        next turn (no upload on it) must agree or the slot flips."""
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            attach_turn = await routes._apply_attachment_tool_autoenable(
                ["calculator"], USER, "s1", "u1", turn_has_presentation=True
            )
            follow_up = await routes._apply_attachment_tool_autoenable(["calculator"], USER, "s1", "u1")
        assert attach_turn == follow_up == ["calculator", "create_powerpoint_presentation"]

    @pytest.mark.asyncio
    async def test_the_tools_are_built_once_the_id_is_enabled(self):
        """The point of the id: it is the gate `_build_powerpoint_presentation_tools` reads."""
        with patch.object(routes, "get_app_role_service", return_value=_role_service({"*"})):
            effective = await routes._apply_attachment_tool_autoenable(
                None, USER, "s1", "u1", turn_has_presentation=True
            )
        assert routes._build_powerpoint_presentation_tools(None, "s1", "u1") == []
        names = {t.tool_name for t in routes._build_powerpoint_presentation_tools(effective, "s1", "u1")}
        assert {"list_powerpoint_presentations", "read_powerpoint_presentation"} <= names


class TestPresentationSharedLookup:
    @pytest.mark.asyncio
    async def test_session_has_presentation_files_matches_pptx_for_the_owner_only(self):
        from apis.shared.files import document_read as dr

        pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        rows = [
            SimpleNamespace(user_id="u1", filename="notes.pdf", mime_type="application/pdf"),
            SimpleNamespace(user_id="u2", filename="theirs.pptx", mime_type=pptx),
            SimpleNamespace(user_id="u1", filename="mine.pptx", mime_type=""),
        ]
        repo = SimpleNamespace(list_session_files=AsyncMock(return_value=rows))
        # The autouse fixture stubs the module attribute; this is the real function.
        with patch.object(dr, "get_file_upload_repository", return_value=repo):
            assert await _real_session_has_presentation_files("u1", "s1")
            rows.pop()  # only the other user's deck and a PDF remain
            assert not await _real_session_has_presentation_files("u1", "s1")


async def _ready(value):
    return value


class TestSharedFileHints:
    """A spreadsheet or deck a handoff or delivery carries in (handoffs spec §4.9).

    Its reference row is written during the stream, after these gates ran, so the session query
    can't see it on that first turn. Without the hint the gate answered no on turn 1 and yes on
    turn 2: the tools changed between the two turns, which re-wrote the whole cached prefix once
    (measured on dev, 2026-11-13), and the model could not analyze the file on the turn it
    arrived.
    """

    @pytest.mark.asyncio
    async def test_a_shared_spreadsheet_enables_the_tools_on_its_first_turn(self, _no_spreadsheet_in_session):
        hint = asyncio.ensure_future(_ready(True))
        assert await routes._session_has_tabular("s1", "u1", tabular_hint=hint)
        _no_spreadsheet_in_session.assert_awaited_once()  # the query still runs, beside the hint
        assert routes._TABULAR_SESSIONS.get("s1") is True

    @pytest.mark.asyncio
    async def test_a_hint_that_says_no_leaves_the_answer_to_the_query(self):
        with patch("apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=True)):
            assert await routes._session_has_tabular("s1", "u1", tabular_hint=asyncio.ensure_future(_ready(False)))
        routes._TABULAR_SESSIONS.clear()
        assert not await routes._session_has_tabular("s2", "u1", tabular_hint=asyncio.ensure_future(_ready(False)))

    @pytest.mark.asyncio
    async def test_a_failed_hint_is_no_and_never_fails_the_turn(self):
        async def boom():
            raise RuntimeError("ddb")

        assert not await routes._session_has_tabular("s1", "u1", tabular_hint=asyncio.ensure_future(boom()))

    @pytest.mark.asyncio
    async def test_an_unbound_agent_reads_the_hint_beside_its_knowledge_base(self):
        with patch(
            "apis.shared.assistants.kb_tabular.assistant_kb_has_tabular_documents", new=AsyncMock(return_value=False)
        ):
            assert await routes._session_has_tabular(
                "s1", "u1", kb_assistant_id="a1", tabular_hint=asyncio.ensure_future(_ready(True))
            )

    @pytest.mark.asyncio
    async def test_a_shared_deck_enables_the_powerpoint_tools_on_its_first_turn(self, _no_deck_in_session):
        assert await routes._session_has_presentation(
            "s1", "u1", presentation_hint=asyncio.ensure_future(_ready(True))
        )
        _no_deck_in_session.assert_awaited_once()
        assert routes._PRESENTATION_SESSIONS.get("s1") is True

    @pytest.mark.asyncio
    async def test_turn_one_with_the_hint_carries_the_ids_turn_two_finds_by_query(self):
        """The property that keeps the prefix: the same tool ids before and after the rows exist."""
        allowed = set(SPREADSHEET_TOOL_IDS) | set(POWERPOINT_PRESENTATION_TOOL_IDS)
        with patch.object(routes, "get_app_role_service", return_value=_role_service(allowed)):
            first = await routes._apply_attachment_tool_autoenable(
                [], USER, "s1", "u1",
                tabular_hint=asyncio.ensure_future(_ready(True)),
                presentation_hint=asyncio.ensure_future(_ready(True)),
            )
            routes._TABULAR_SESSIONS.clear()
            routes._PRESENTATION_SESSIONS.clear()  # a fresh process: only the rows answer now
            with patch(
                "apis.shared.files.document_read.session_has_tabular_files", new=AsyncMock(return_value=True)
            ), patch(
                "apis.shared.files.document_read.session_has_presentation_files", new=AsyncMock(return_value=True)
            ):
                second = await routes._apply_attachment_tool_autoenable([], USER, "s1", "u1")
        assert first == second
        assert set(first) == allowed
