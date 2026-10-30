"""``verify_sources``: the gate it rides, the input it refuses, and — the point
of the whole feature — what does and does not reach the model.

docs/specs/source-provenance-verification.md §8. The tool's result is the
Converse payload and therefore the cacheable prefix, so the assertions about
what the result does *not* contain are as load-bearing as the ones about what
it does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.builtin_tools.verify_sources_tool import (
    MAX_CLAIMS_PER_CALL,
    MAX_QUOTE_CHARS,
    VERIFY_SOURCES_TOOL_NAME,
    make_verify_sources_tool,
)
from apis.shared.files.provenance_broker import discard_provenance, take_provenance
from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNVERIFIED,
)

TOOL_MODULE = "agents.builtin_tools.verify_sources_tool"

PAGE_1 = "NOTICE OF AWARD\nThe total approved budget is $487,500 for the period of performance."
PAGE_2 = "The project period begins January 15, 2026.\nDirect costs total $316,200."


class _Meta(SimpleNamespace):
    pass


@pytest.fixture(autouse=True)
def clean_broker():
    discard_provenance("u1", "s1")
    yield
    discard_provenance("u1", "s1")


@pytest.fixture
def document(monkeypatch):
    """Stub the document side; the units themselves are tested in the pure module."""

    async def _units(user_id, session_id, upload_id):
        return _Meta(upload_id=upload_id, filename="award.pdf"), "pdf", [PAGE_1, PAGE_2]

    monkeypatch.setattr(f"{TOOL_MODULE}.document_provenance_units", _units)


@pytest.fixture(autouse=True)
def no_persistence(monkeypatch):
    """The DynamoDB row has its own tests; here it must not be required."""
    monkeypatch.setattr(f"{TOOL_MODULE}._persist", AsyncMock())


def _ctx(tool_use_id: str = "tu-1"):
    return SimpleNamespace(tool_use={"toolUseId": tool_use_id})


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


def _states(result):
    return {c["id"]: c["state"] for c in result["content"][0]["json"]["claims"]}


# ---------------------------------------------------------------------------
# Factory and spec
# ---------------------------------------------------------------------------


class TestTool:
    def test_identity_is_mandatory_and_the_name_is_stable(self):
        tool = make_verify_sources_tool("s1", "u1")
        assert tool.tool_name == VERIFY_SOURCES_TOOL_NAME == "verify_sources"
        with pytest.raises(ValueError):
            make_verify_sources_tool("", "u1")
        with pytest.raises(ValueError):
            make_verify_sources_tool("s1", "")

    def test_the_spec_takes_a_claim_list_and_an_optional_document(self):
        """An inline attachment carries no upload id the model can see, so a
        required one is an invitation to guess (dev, Haiku 4.5: `"0"`, five
        samples of five)."""
        schema = make_verify_sources_tool("s1", "u1").tool_spec["inputSchema"]["json"]
        assert set(schema["required"]) == {"claims"}
        assert "upload_id" in schema["properties"]
        # `tool_context` is plumbing, not model input: it must not appear.
        assert "tool_context" not in schema["properties"]

    def test_the_spec_tells_the_model_what_quote_unsupported_means(self):
        """The model is the first consumer able to correct it, so the
        instruction has to say so rather than leaving it to infer."""
        description = make_verify_sources_tool("s1", "u1").tool_spec["description"]
        assert "quote_unsupported" in description
        assert "verbatim" in description


# ---------------------------------------------------------------------------
# What reaches the model, and what does not
# ---------------------------------------------------------------------------


class TestWhatReachesTheModel:
    @pytest.mark.asyncio
    async def test_the_result_is_the_state_per_claim(self, document):
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool,
            upload_id="up-1",
            claims=[
                {"id": "total", "value": "$487,500", "quote": "The total approved budget is $487,500"},
                {"id": "fake", "value": "$912,000", "quote": "The total approved budget is $487,500"},
                {"id": "ghost", "value": "x", "quote": "a sentence that is not in the document"},
            ],
            tool_context=_ctx(),
        )
        assert result["status"] == "success"
        assert _states(result) == {
            "total": STATE_SUPPORTED,
            "fake": STATE_QUOTE_UNSUPPORTED,
            "ghost": STATE_UNVERIFIED,
        }

    @pytest.mark.asyncio
    async def test_the_result_carries_nothing_but_id_and_state(self, document):
        """A tool result is the Converse payload and the cacheable prefix. The
        quote, locator, precision, method and filename would be paid at model
        rates on every later turn, so none of them may be here."""
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "total", "value": "$487,500", "quote": "The total approved budget is $487,500"}],
            tool_context=_ctx(),
        )
        assert result["content"] == [
            {"json": {"claims": [{"id": "total", "state": STATE_SUPPORTED}]}}
        ]
        payload = str(result)
        for leaked in ("locator", "precision", "method", "award.pdf", "page"):
            assert leaked not in payload


# ---------------------------------------------------------------------------
# What reaches the stream instead
# ---------------------------------------------------------------------------


class TestWhatReachesTheStream:
    @pytest.mark.asyncio
    async def test_the_rich_verdict_is_queued_for_the_sse_event(self, document):
        tool = make_verify_sources_tool("s1", "u1")
        await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "direct", "value": "$316,200", "quote": "Direct costs total $316,200."}],
            tool_context=_ctx("tu-7"),
        )
        [payload] = take_provenance("u1", "s1")
        assert payload["toolUseId"] == "tu-7"
        [verdict] = payload["claims"]
        assert verdict["id"] == "direct"
        assert verdict["state"] == STATE_SUPPORTED
        assert verdict["quote"] == "Direct costs total $316,200."
        assert verdict["filename"] == "award.pdf"
        assert verdict["uploadId"] == "up-1"
        # Page 2, measured, and `precision` is always present.
        assert verdict["locator"] == {"kind": "page", "value": 2, "precision": "measured"}

    @pytest.mark.asyncio
    async def test_the_verdict_is_persisted_before_it_is_queued(self, document, monkeypatch):
        """The emit loop may never reach the payload — a cancelled turn, a
        dropped connection — and the row is what a reload reads."""
        calls: list = []
        monkeypatch.setattr(
            f"{TOOL_MODULE}._persist",
            AsyncMock(side_effect=lambda *a, **k: calls.append(a)),
        )
        tool = make_verify_sources_tool("s1", "u1")
        await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "d", "value": "$316,200", "quote": "Direct costs total $316,200."}],
            tool_context=_ctx("tu-9"),
        )
        assert calls and calls[0][:3] == ("u1", "s1", "tu-9")

    @pytest.mark.asyncio
    async def test_another_conversation_never_drains_these_verdicts(self, document):
        """Keyed on (user, session): a session id alone does not name a
        conversation, so a second user on one id must see nothing."""
        tool = make_verify_sources_tool("s1", "u1")
        await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "d", "value": "$316,200", "quote": "Direct costs total $316,200."}],
            tool_context=_ctx(),
        )
        assert take_provenance("u2", "s1") == []
        assert take_provenance("u1", "s2") == []
        assert len(take_provenance("u1", "s1")) == 1


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("claims", [[], None, "total", [["total"]]])
    async def test_malformed_claims_are_refused_conversationally(self, document, claims):
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(tool, upload_id="up-1", claims=claims, tool_context=_ctx())
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_too_many_claims_in_one_call_are_refused(self, document):
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": str(i), "value": i, "quote": "x"} for i in range(MAX_CLAIMS_PER_CALL + 1)],
            tool_context=_ctx(),
        )
        assert result["status"] == "error"
        assert "another call" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_an_oversized_quote_is_refused_rather_than_scanned(self, document):
        """Model-controlled input, and a passage that long is not a passage."""
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "x", "value": "1", "quote": "y" * (MAX_QUOTE_CHARS + 1)}],
            tool_context=_ctx(),
        )
        assert result["status"] == "error"
        assert "characters" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_document_error_is_surfaced_not_raised(self, monkeypatch):
        from apis.shared.files.document_read import DocumentReadError

        async def _boom(user_id, session_id, upload_id):
            raise DocumentReadError("'sheet.xlsx' is not a readable document")

        monkeypatch.setattr(f"{TOOL_MODULE}.document_provenance_units", _boom)
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool, upload_id="up-1", claims=[{"id": "x", "value": "1", "quote": "y"}], tool_context=_ctx()
        )
        assert result["status"] == "error"
        assert "not a readable document" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_scanned_pdf_says_nothing_is_traced(self, monkeypatch):
        """No text layer, so there is nothing to search. Every claim reads
        unverified rather than the turn failing or a document being invented."""

        async def _empty(user_id, session_id, upload_id):
            return _Meta(upload_id=upload_id, filename="scan.pdf"), "pdf", ["", "", ""]

        monkeypatch.setattr(f"{TOOL_MODULE}.document_provenance_units", _empty)
        tool = make_verify_sources_tool("s1", "u1")
        result = await _call(
            tool,
            upload_id="up-1",
            claims=[{"id": "x", "value": "$1", "quote": "anything"}],
            tool_context=_ctx(),
        )
        assert _states(result) == {"x": STATE_UNVERIFIED}
        [payload] = take_provenance("u1", "s1")
        assert payload["claims"][0]["method"] == "document_unreadable"


# ---------------------------------------------------------------------------
# Which document: an id, a filename, or the only one there is
# ---------------------------------------------------------------------------


def _doc(upload_id: str, filename: str) -> dict:
    return {"upload_id": upload_id, "filename": filename, "format": "md", "size_bytes": 1, "modes": []}


@pytest.fixture
def session_documents(monkeypatch):
    """Owned rows by id, plus the session listing the fallback reads.

    Returns the listing so a test can set what the session holds, and the list
    of listing calls so a test can prove the happy path never pays for one.
    """
    from apis.shared.files.workspace import WorkspaceFileNotFoundError

    documents: list = []
    listing_calls: list = []

    async def _units(user_id, session_id, upload_id):
        match = [d for d in documents if d["upload_id"] == upload_id]
        if not match:
            raise WorkspaceFileNotFoundError(f"No file with id '{upload_id}' found in your workspace")
        return _Meta(upload_id=upload_id, filename=match[0]["filename"]), "pdf", [PAGE_1, PAGE_2]

    async def _listing(user_id, session_id):
        listing_calls.append((user_id, session_id))
        return {"documents": list(documents), "count": len(documents)}

    monkeypatch.setattr(f"{TOOL_MODULE}.document_provenance_units", _units)
    monkeypatch.setattr(f"{TOOL_MODULE}.list_session_documents", _listing)
    return SimpleNamespace(documents=documents, listing_calls=listing_calls)


_CLAIM = {"id": "total", "value": "$487,500", "quote": "The total approved budget is $487,500"}


class TestWhichDocument:
    @pytest.mark.asyncio
    async def test_a_real_upload_id_never_pays_for_the_listing(self, session_documents):
        session_documents.documents.extend([_doc("up-1", "award.pdf"), _doc("up-2", "budget.pdf")])
        result = await _call(
            make_verify_sources_tool("s1", "u1"), upload_id="up-2", claims=[_CLAIM], tool_context=_ctx()
        )
        assert _states(result) == {"total": STATE_SUPPORTED}
        assert take_provenance("u1", "s1")[0]["claims"][0]["uploadId"] == "up-2"
        assert session_documents.listing_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("upload_id", ["", "0", "facilities_policy.md"])
    async def test_one_document_is_the_document(self, session_documents, upload_id):
        """The eval-harness failure: a guessed id against a session holding one
        document must verify against it, not cost two recovery rounds."""
        session_documents.documents.append(_doc("up-1", "award.pdf"))
        kwargs = {"upload_id": upload_id} if upload_id else {}
        result = await _call(
            make_verify_sources_tool("s1", "u1"), claims=[_CLAIM], tool_context=_ctx(), **kwargs
        )
        assert result["status"] == "success"
        assert take_provenance("u1", "s1")[0]["claims"][0]["uploadId"] == "up-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["budget.pdf", "budget_pdf", "BUDGET", "Budget.PDF"]
    )
    async def test_a_filename_picks_among_several(self, session_documents, name):
        """The inline block is named `budget_pdf`, the attachment marker says
        `budget.pdf`; either, in any case, with or without the extension."""
        session_documents.documents.extend([_doc("up-1", "award.pdf"), _doc("up-2", "budget.pdf")])
        result = await _call(
            make_verify_sources_tool("s1", "u1"), upload_id=name, claims=[_CLAIM], tool_context=_ctx()
        )
        assert result["status"] == "success"
        assert take_provenance("u1", "s1")[0]["claims"][0]["uploadId"] == "up-2"

    @pytest.mark.asyncio
    async def test_an_ambiguous_name_lists_the_choices_instead_of_picking(self, session_documents):
        session_documents.documents.extend([_doc("up-1", "award.pdf"), _doc("up-2", "budget.pdf")])
        result = await _call(
            make_verify_sources_tool("s1", "u1"), upload_id="0", claims=[_CLAIM], tool_context=_ctx()
        )
        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "'up-1' (award.pdf)" in text and "'up-2' (budget.pdf)" in text
        assert take_provenance("u1", "s1") == []

    @pytest.mark.asyncio
    async def test_a_long_listing_is_bounded(self, session_documents):
        from agents.builtin_tools.verify_sources_tool import MAX_LISTED_DOCUMENTS

        session_documents.documents.extend(
            _doc(f"up-{i}", f"doc{i}.pdf") for i in range(MAX_LISTED_DOCUMENTS + 5)
        )
        result = await _call(make_verify_sources_tool("s1", "u1"), claims=[_CLAIM], tool_context=_ctx())
        text = result["content"][0]["text"]
        assert f"up-{MAX_LISTED_DOCUMENTS - 1}'" in text
        assert f"up-{MAX_LISTED_DOCUMENTS}'" not in text
        assert "5 more" in text

    @pytest.mark.asyncio
    async def test_a_session_with_no_document_says_so(self, session_documents):
        result = await _call(make_verify_sources_tool("s1", "u1"), claims=[_CLAIM], tool_context=_ctx())
        assert result["status"] == "error"
        assert "no readable document" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The routes gate
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_memo():
    from apis.inference_api.chat import routes

    routes._DOCUMENT_SESSIONS.clear()
    yield
    routes._DOCUMENT_SESSIONS.clear()


class TestRoutesGate:
    @pytest.mark.asyncio
    async def test_off_by_default_the_session_gets_document_read_alone(
        self, monkeypatch, clear_memo
    ):
        from apis.inference_api.chat.routes import _build_document_tools

        monkeypatch.delenv("SOURCE_PROVENANCE_ENABLED", raising=False)
        monkeypatch.setattr(
            "apis.shared.files.document_read.session_has_documents", AsyncMock(return_value=True)
        )
        tools = await _build_document_tools("s1", "u1", turn_has_document=True)
        assert [t.tool_name for t in tools] == ["document_read"]

    @pytest.mark.asyncio
    async def test_on_it_rides_the_same_gate_as_document_read(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        monkeypatch.setenv("SOURCE_PROVENANCE_ENABLED", "true")
        monkeypatch.setattr(
            "apis.shared.files.document_read.session_has_documents", AsyncMock(return_value=True)
        )
        tools = await _build_document_tools("s1", "u1", turn_has_document=True)
        assert [t.tool_name for t in tools] == ["document_read", "verify_sources"]

    @pytest.mark.asyncio
    async def test_a_session_with_no_document_gets_neither(self, monkeypatch, clear_memo):
        """There is nothing to verify a quote against without an attachment, so
        the flag alone never puts the tool in toolConfig."""
        from apis.inference_api.chat.routes import _build_document_tools

        monkeypatch.setenv("SOURCE_PROVENANCE_ENABLED", "true")
        monkeypatch.setattr(
            "apis.shared.files.document_read.session_has_documents", AsyncMock(return_value=False)
        )
        assert await _build_document_tools("s1", "u1") == []

    @pytest.mark.asyncio
    async def test_in_development_only_the_literal_true_enables(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        monkeypatch.setattr(
            "apis.shared.files.document_read.session_has_documents", AsyncMock(return_value=True)
        )
        for value in ("", "yes", "1", "on", "false", "True "):
            monkeypatch.setenv("SOURCE_PROVENANCE_ENABLED", value)
            tools = await _build_document_tools("s1", "u1", turn_has_document=True)
            expected = ["document_read", "verify_sources"] if value.strip().lower() == "true" else ["document_read"]
            assert [t.tool_name for t in tools] == expected, value

    def test_its_id_stays_out_of_the_injected_tool_lists(self):
        """Same reasoning document_read records: the governing capability is the
        user's own attachment, not an RBAC grant."""
        from apis.shared.tools.injected import INJECTED_TOOL_IDS

        assert VERIFY_SOURCES_TOOL_NAME not in INJECTED_TOOL_IDS
