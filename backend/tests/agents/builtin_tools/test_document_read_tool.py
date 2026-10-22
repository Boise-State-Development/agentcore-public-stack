"""``document_read`` tool factory, its session-state gate, and the two places
its presence has to be accounted for: the agent cache key and the tool-result
offloader's exemption list.

The gate is the load-bearing decision of docs/specs/document-context-offload.md
§4B: the tool exists for any session with a readable attachment, whatever the
user's RBAC grants or picker state, and its id never enters
``INJECTED_TOOL_IDS``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.builtin_tools.document_read_tool import (
    DOCUMENT_READ_TOOL_NAME,
    make_document_read_tool,
    record_document_read,
)
from apis.shared.files.document_read import DocumentReadResult
from apis.shared.files.workspace import WorkspaceFileNotFoundError, WorkspaceStorageNotConfiguredError
from apis.shared.tools.injected import DOCUMENT_TOOL_IDS, INJECTED_TOOL_IDS

TOOL_MODULE = "agents.builtin_tools.document_read_tool"
ROUTES = "apis.inference_api.chat.routes"


async def _call(tool, *args, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


class TestTool:
    def test_identity_is_mandatory_and_the_name_is_stable(self):
        tool = make_document_read_tool("s1", "u1")
        assert tool.tool_name == DOCUMENT_READ_TOOL_NAME == "document_read"
        with pytest.raises(ValueError):
            make_document_read_tool("", "u1")
        with pytest.raises(ValueError):
            make_document_read_tool("s1", "")

    def test_id_is_recorded_but_never_an_injected_catalog_id(self):
        assert DOCUMENT_TOOL_IDS == {"document_read"}
        assert not (DOCUMENT_TOOL_IDS & INJECTED_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_no_upload_id_lists_the_sessions_documents(self, monkeypatch):
        listing = AsyncMock(return_value={"documents": [{"upload_id": "up-1"}], "count": 1})
        monkeypatch.setattr(f"{TOOL_MODULE}.list_session_documents", listing)
        result = await _call(make_document_read_tool("s1", "u1"))
        listing.assert_awaited_once_with("u1", "s1")
        assert result["status"] == "success"
        payload = result["content"][0]["json"]
        assert payload["count"] == 1 and "hint" in payload

    @pytest.mark.asyncio
    async def test_page_read_appends_the_native_block_after_the_metadata(self, monkeypatch):
        block = {"document": {"format": "pdf", "name": "policy p4-7 abc123", "source": {"bytes": b"%PDF"}}}
        read = AsyncMock(return_value=DocumentReadResult(
            mode="pages", payload={"pages_returned": 4}, document_block=block, pages_returned=4, bytes_returned=4, format="pdf",
        ))
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", read)
        result = await _call(make_document_read_tool("s1", "u1"), upload_id="up-1", page_range="4-7", max_pages=6)
        read.assert_awaited_once_with(
            "u1", "s1", "up-1", page_range="4-7", pattern=None, max_pages=6, offset=0, text_only=False
        )
        assert result["content"] == [{"json": {"pages_returned": 4}}, block]

    @pytest.mark.asyncio
    async def test_text_only_tool_reads_text_with_an_identical_spec(self, monkeypatch):
        # A TEXT-only model gets page text, never a document block — and the
        # same tool spec, so the flag cannot change toolConfig on its own.
        read = AsyncMock(return_value=DocumentReadResult(mode="pages_text", payload={"page_texts": []}))
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", read)
        text_only = make_document_read_tool("s1", "u1", text_only=True)
        result = await _call(text_only, upload_id="up-1", page_range="4-7")
        assert read.await_args.kwargs["text_only"] is True
        assert result["content"] == [{"json": {"page_texts": []}}]
        assert text_only.tool_spec == make_document_read_tool("s1", "u1").tool_spec

    @pytest.mark.asyncio
    async def test_empty_strings_mean_absent_arguments(self, monkeypatch):
        read = AsyncMock(return_value=DocumentReadResult(mode="index", payload={}))
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", read)
        await _call(make_document_read_tool("s1", "u1"), upload_id="up-1", page_range="", pattern="")
        assert read.await_args.kwargs["page_range"] is None and read.await_args.kwargs["pattern"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc,fragment",
        [
            (WorkspaceStorageNotConfiguredError("no bucket"), "not configured"),
            (WorkspaceFileNotFoundError("No file with id 'up-9'"), "up-9"),
            (RuntimeError("s3 down"), "s3 down"),
        ],
    )
    async def test_failures_surface_as_error_results_not_exceptions(self, monkeypatch, exc, fragment):
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", AsyncMock(side_effect=exc))
        result = await _call(make_document_read_tool("s1", "u1"), upload_id="up-9")
        assert result["status"] == "error"
        assert fragment in result["content"][0]["text"]

    def test_metric_is_content_free_and_in_the_compaction_namespace(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "apis.shared.observability.emf.emit_emf_metrics",
            lambda ns, metrics, properties=None, units=None: calls.append((ns, metrics, properties)),
        )
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", lambda: True)
        record_document_read(DocumentReadResult(
            mode="pages", payload={"filename": "secret.pdf"}, pages_returned=4, bytes_returned=900, format="pdf",
        ))
        assert calls == [(
            "AgentCoreStack/Compaction",
            {"DocumentRead": 1, "DocumentReadPages": 4, "DocumentReadBytes": 900},
            {"mode": "pages", "format": "pdf"},
        )]
        assert "secret" not in repr(calls)


# ---------------------------------------------------------------------------
# Routes gate
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_memo():
    from apis.inference_api.chat import routes

    routes._DOCUMENT_SESSIONS.clear()
    yield
    routes._DOCUMENT_SESSIONS.clear()


class TestRoutesGate:
    @pytest.mark.asyncio
    async def test_this_turns_document_builds_the_tool_without_a_query(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        lookup = AsyncMock(return_value=False)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        tools = await _build_document_tools("s1", "u1", turn_has_document=True)
        assert [t.tool_name for t in tools] == ["document_read"]
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_session_with_a_document_gets_the_tool_regardless_of_enabled_tools(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        lookup = AsyncMock(return_value=True)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        assert len(await _build_document_tools("s1", "u1")) == 1
        lookup.assert_awaited_once_with("u1", "s1")
        # Memoized: the second turn does not query again.
        assert len(await _build_document_tools("s1", "u1")) == 1
        assert lookup.await_count == 1

    @pytest.mark.asyncio
    async def test_no_document_means_no_tool_and_no_memo(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _DOCUMENT_SESSIONS, _build_document_tools

        lookup = AsyncMock(return_value=False)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        assert await _build_document_tools("s1", "u1") == []
        assert await _build_document_tools("s1", "u1") == []
        assert lookup.await_count == 2 and "s1" not in _DOCUMENT_SESSIONS

    @pytest.mark.asyncio
    async def test_lookup_failure_fails_closed(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", AsyncMock(side_effect=RuntimeError("ddb")))
        assert await _build_document_tools("s1", "u1") == []

    @pytest.mark.asyncio
    async def test_kill_switch_and_missing_identity(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools, _document_tools_gate

        monkeypatch.setenv("DOCUMENT_READ_ENABLED", "false")
        assert await _build_document_tools("s1", "u1", turn_has_document=True) == []
        assert await _document_tools_gate("s1", "u1", turn_has_document=True) is False
        monkeypatch.setenv("DOCUMENT_READ_ENABLED", "")
        assert await _document_tools_gate("s1", "u1", turn_has_document=True) is True
        assert await _document_tools_gate("", "u1", turn_has_document=True) is False

    @pytest.mark.parametrize(
        "mime,filename,expected",
        [
            # The exact attachment mix prod carried on 2026-09-20/21.
            ("application/pdf", "hw3.pdf", True),
            ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", True),
            ("text/markdown", "notes.md", True),
            ("text/plain", "notes.txt", True),
            ("image/png", "screenshot.png", False),
            ("image/jpeg", "photo.jpg", False),
            ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", False),
            ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "deck.pptx", False),
        ],
    )
    def test_turn_classification_matches_what_the_tool_can_read(self, mime, filename, expected):
        from apis.inference_api.chat.routes import _resolved_files_include_a_document

        rf = SimpleNamespace(content_type=mime, filename=filename, bytes="")
        assert _resolved_files_include_a_document([rf]) is expected
        # One readable document among unreadable ones is still a document turn.
        image = SimpleNamespace(content_type="image/png", filename="a.png", bytes="")
        assert _resolved_files_include_a_document([image, rf]) is expected

    def test_no_uploads_is_not_a_document_turn(self):
        from apis.inference_api.chat.routes import _resolved_files_include_a_document

        assert _resolved_files_include_a_document(None) is False
        assert _resolved_files_include_a_document([]) is False

    @pytest.mark.asyncio
    async def test_a_non_document_attachment_does_not_inject_or_poison_the_memo(
        self, monkeypatch, clear_memo
    ):
        """An image / spreadsheet / deck is an upload id, not a document.

        The gate used to short-circuit on "this turn attached something",
        which injected a tool whose listing is empty by construction AND
        memoized the session, so every later turn in that process carried it.
        Measured in prod 2026-09-20/21: 13 of 29 attachment sessions had no
        readable document and accounted for 100% of the window's avoidable
        ``toolConfigHash`` rotations.
        """
        from apis.inference_api.chat.routes import _DOCUMENT_SESSIONS, _build_document_tools

        lookup = AsyncMock(return_value=False)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        assert await _build_document_tools("s1", "u1", turn_has_document=False) == []
        # It falls through to the authoritative query rather than guessing,
        # and leaves no memo behind for the next turn to trip over.
        lookup.assert_awaited_once_with("u1", "s1")
        assert "s1" not in _DOCUMENT_SESSIONS

    @pytest.mark.asyncio
    async def test_the_gate_does_not_flap_when_the_process_memo_is_lost(
        self, monkeypatch, clear_memo
    ):
        """Same session, two containers, one answer.

        The memo is per-process and the DynamoDB query is not, so the two have
        to agree or the tool — and with it ``toolConfig`` — flips between
        turns. Prod showed A→B→A→B on exactly the sessions where they
        disagreed; each flip re-writes the whole cacheable prefix at 1.25x
        input.
        """
        from apis.inference_api.chat import routes

        # Turn 1 on container A: a real document arrives with the turn.
        lookup = AsyncMock(return_value=True)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        assert len(await routes._build_document_tools("s1", "u1", turn_has_document=True)) == 1

        # Container recycles: the memo is gone, the upload rows are not.
        routes._DOCUMENT_SESSIONS.clear()
        assert len(await routes._build_document_tools("s1", "u1")) == 1

        # And the negative case stays negative across the same boundary.
        routes._DOCUMENT_SESSIONS.clear()
        lookup.return_value = False
        assert await routes._build_document_tools("s2", "u1", turn_has_document=False) == []
        routes._DOCUMENT_SESSIONS.clear()
        assert await routes._build_document_tools("s2", "u1") == []

    @pytest.mark.asyncio
    async def test_resume_reproduces_the_live_turns_answer(self, monkeypatch, clear_memo):
        """The resume path passes no turn signal and must still land on the
        same cache-key bit the paused turn used, or the paused agent is
        orphaned. That only holds when the short-circuit is classified."""
        from apis.inference_api.chat.routes import _document_tools_gate

        monkeypatch.setattr(
            "apis.shared.files.document_read.session_has_documents", AsyncMock(return_value=False)
        )
        live = await _document_tools_gate("s1", "u1", turn_has_document=False)
        resumed = await _document_tools_gate("s1", "u1")
        assert live is resumed is False

    def test_memo_is_bounded(self, monkeypatch, clear_memo):
        from apis.inference_api.chat import routes

        monkeypatch.setattr(routes, "_DOCUMENT_SESSIONS_MAX", 3)
        for i in range(5):
            routes._remember_document_session(f"s{i}")
        assert list(routes._DOCUMENT_SESSIONS) == ["s2", "s3", "s4"]


# ---------------------------------------------------------------------------
# Agent cache key
# ---------------------------------------------------------------------------


def test_cache_key_carries_the_document_tool_bit_without_moving_the_skills_hash():
    from apis.inference_api.chat import service

    base = dict(
        session_id="s", user_id="u", enabled_tools=["t"], model_id="m", inference_params={},
        system_prompt=None, caching_enabled=False, provider="bedrock", freshness_hash="f", agent_type="chat",
    )
    without = service._create_cache_key(**base, skills_hash="k")
    with_docs = service._create_cache_key(**base, skills_hash="k", document_tools=True)
    assert without != with_docs
    assert without[-1] == with_docs[-1] == "k"
    # Trailing layout: (..., document_tools, assistant_id, skills_hash).
    assert without[-2] == with_docs[-2] == ""
    assert without[-3] is False and with_docs[-3] is True


# ---------------------------------------------------------------------------
# Offloader exemption
# ---------------------------------------------------------------------------


class TestOffloaderExemption:
    def test_document_read_results_are_never_offloaded(self):
        from agents.main_agent.core.tool_result_offload import OFFLOAD_EXEMPT_TOOLS, _exempt_tool, _should_offload

        assert "document_read" in OFFLOAD_EXEMPT_TOOLS
        assert _exempt_tool(SimpleNamespace(tool_use={"name": "document_read"})) is True
        assert _exempt_tool(SimpleNamespace(tool_use={"name": "gmail_search"})) is False
        assert _exempt_tool(SimpleNamespace(tool_use=None)) is False
        assert _should_offload("document_read", 90_000) is False
        assert _should_offload("gmail_search", 90_000) is True

    @pytest.mark.asyncio
    async def test_mixin_returns_before_counting_tokens_for_an_exempt_tool(self):
        from agents.main_agent.core.tool_result_offload import _OffloaderMixin

        class _Base:
            async def _handle_tool_result(self, event):  # pragma: no cover - must not run
                raise AssertionError("base offloader reached for an exempt tool")

        class _Sut(_OffloaderMixin, _Base):
            _max_result_tokens = 10

        big = "x" * 4_000
        result = {"toolUseId": "t1", "status": "success", "content": [{"text": big}]}
        event = SimpleNamespace(result=result, tool_use={"toolUseId": "t1", "name": "document_read"})
        await _Sut()._handle_tool_result(event)
        assert event.result is result
