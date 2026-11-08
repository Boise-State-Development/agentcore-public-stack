"""``compare_word_documents``: built beside ``redline_word_document``.

It joins the Word family under the same condition as the redline tool,
``WORD_TRACKED_CHANGES_ENABLED`` with the Word grant, and adds one constant spec
(pinned below) without moving the redline tool's (pinned in
``test_word_redline_paragraphs.py`` and asserted here too). A call writes the
revised document's differences into a copy of the original as tracked changes
(docs/specs/word-tracked-changes.md §3b).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apis.inference_api.chat.routes import _build_word_document_tools
from apis.shared.kb_backend.docx_revisions import annotated_text
from tests.apis.inference_api.test_word_redline_paragraphs import SPEC_DIGEST as REDLINE_DIGEST
from tests.shared.docx_fixtures import build_docx, deleted, inserted, paragraph, run

FIXTURES = Path(__file__).parents[2] / "agents" / "builtin_tools" / "fixtures"
ORIGINAL = (FIXTURES / "contract-numbered.docx").read_bytes()
REVISED = (FIXTURES / "contract-numbered-revised.docx").read_bytes()

#: The tool's spec (419 Bedrock CountTokens, 378 cl100k). Shipping it is one toolConfig rewrite
#: per deployment for users with the Word tools. A change here is a deliberate
#: spec change.
COMPARE_DIGEST = "dc565b69cf2f354d26d7ee89f82f576568d0b268fd13b7a88e11be96c5f626b7"

WORD_TOOLS = [
    "create_word_document", "modify_word_document", "list_word_documents", "read_word_document",
    "redline_word_document", "compare_word_documents",
]


def _digest(tool) -> str:
    return hashlib.sha256(json.dumps(tool.tool_spec, sort_keys=True).encode()).hexdigest()


def _tools(monkeypatch, tracked: str = "true"):
    monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", tracked)
    return {t.tool_name: t for t in _build_word_document_tools(["create_word_document"], "s1", "u1", "Pat Reviewer")}


def _said(result) -> str:
    if isinstance(result, dict):
        return "\n".join(block.get("text", "") for block in result.get("content", []))
    return json.loads(result)["summary"]


class TestSpec:
    def test_it_joins_last_and_the_redline_spec_does_not_move(self, monkeypatch):
        tools = _tools(monkeypatch)
        assert list(tools) == WORD_TOOLS
        assert _digest(tools["redline_word_document"]) == REDLINE_DIGEST
        assert _digest(tools["compare_word_documents"]) == COMPARE_DIGEST

    def test_it_needs_tracked_changes_and_the_word_grant(self, monkeypatch):
        assert "compare_word_documents" not in _tools(monkeypatch, "false")
        assert _build_word_document_tools(["web_search"], "s1", "u1", "Pat") == []

    def test_the_spec_does_not_depend_on_identity(self):
        from agents.builtin_tools.word_document_tool import make_compare_word_documents_tool

        a = make_compare_word_documents_tool("s1", "u1")
        b = make_compare_word_documents_tool("s2", "u2")
        assert _digest(a) == _digest(b) == COMPARE_DIGEST


@pytest.fixture()
def store(monkeypatch):
    """Two session documents by name, and whatever the tool saves."""
    import agents.builtin_tools.word_document_tool as word

    files = {
        "services-agreement": SimpleNamespace(filename="services-agreement.docx", s3_bucket="b", s3_key="k1"),
        "services-agreement-returned": SimpleNamespace(
            filename="services-agreement-returned.docx", s3_bucket="b", s3_key="k2"
        ),
    }
    contents = {"k1": ORIGINAL, "k2": REVISED}
    saved = {}

    async def find(user_id, session_id, name):
        return files.get(name.removesuffix(".docx"))

    async def save(user_id, session_id, filename, data, mime):
        saved.update(filename=filename, data=data)
        return "up-1", "7.0 KB"

    monkeypatch.setattr(word, "_storage_configured", lambda: True)
    monkeypatch.setattr(word, "_find_word_document", find)
    monkeypatch.setattr(word, "_download_s3_bytes", lambda bucket, key: contents[key])
    monkeypatch.setattr(word, "_store_document", save)
    return SimpleNamespace(saved=saved, contents=contents, files=files)


async def _call(monkeypatch, **kwargs):
    tool = _tools(monkeypatch)["compare_word_documents"]
    return await tool._tool_func(**kwargs)


class TestCall:
    @pytest.mark.asyncio
    async def test_a_clean_return_comes_back_as_a_redline_by_the_counterparty(self, monkeypatch, store):
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement-returned"
        )
        card = json.loads(result)
        assert card["ui_type"] == "file_download"
        assert card["payload"]["filename"] == "services-agreement-compared.docx"
        said = card["summary"]
        assert (
            "Tracked changes by Counterparty against 'services-agreement.docx': 3 reworded, 2 added and "
            "1 removed paragraph(s). Changes: Reworded " in said
        )
        assert '"thirty (30)" → "forty-five (45)"' in said
        assert 'In a table: reworded "$10,000": "$10,000" → "$15,000"' in said
        assert store.saved["filename"] == "services-agreement-compared.docx"
        assert "[deleted: thirty (30)][inserted: forty-five (45)]" in annotated_text(store.saved["data"])

    @pytest.mark.asyncio
    async def test_the_author_and_output_name_can_be_given(self, monkeypatch, store):
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement-returned",
            author="Sponsor Counsel", output_name="returned-vs-sent.docx",
        )
        assert "Tracked changes by Sponsor Counsel" in _said(result)
        assert store.saved["filename"] == "returned-vs-sent.docx"

    @pytest.mark.asyncio
    async def test_the_same_wording_saves_nothing_and_says_so(self, monkeypatch, store):
        store.contents["k2"] = ORIGINAL
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement-returned"
        )
        assert result["status"] == "success"
        assert "have the same wording, so there is nothing to mark" in _said(result)
        assert store.saved == {}

    @pytest.mark.asyncio
    async def test_a_document_with_tracked_changes_is_refused(self, monkeypatch, store):
        store.contents["k2"] = build_docx(paragraph(run("State of "), deleted("Idaho"), inserted("Utah"), run(".")))
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement-returned"
        )
        assert result["status"] == "error"
        assert "- The revised version already contains tracked changes." in _said(result)
        assert store.saved == {}

    @pytest.mark.asyncio
    async def test_a_missing_document_is_named(self, monkeypatch, store):
        result = await _call(monkeypatch, original_name="services-agreement", revised_name="nda")
        assert "No Word document named 'nda' was found" in _said(result)

    @pytest.mark.asyncio
    async def test_the_same_document_twice_is_refused(self, monkeypatch, store):
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement.docx"
        )
        assert "are the same document" in _said(result)

    @pytest.mark.asyncio
    async def test_a_bad_output_name_is_refused(self, monkeypatch, store):
        result = await _call(
            monkeypatch, original_name="services-agreement", revised_name="services-agreement-returned",
            output_name="../x",
        )
        assert "Invalid output name" in _said(result)
        assert store.saved == {}


class TestDetails:
    def test_the_change_list_is_bounded(self):
        from agents.builtin_tools.word_document_tool import _MAX_COMPARE_CHARS, _compare_details

        result = SimpleNamespace(
            changes=[f'Reworded "clause {n}": "{"x" * 40}" → "{"y" * 40}"' for n in range(200)],
            unmarked=["Not marked: a table is added in the revised version; a comment quotes it."],
            reworded=200, added=0, removed=0,
        )
        details = _compare_details("a.docx", "Counterparty", result)
        assert len(details) < _MAX_COMPARE_CHARS + 200
        kept = details.count("Reworded ")
        assert 0 < kept <= 40
        assert f"…and {201 - kept} more; the redline has every change." in details
        assert details.endswith("Differences that could not be marked are comments in the document.")
