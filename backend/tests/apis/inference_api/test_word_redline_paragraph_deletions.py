"""``redline_word_document`` deleting whole paragraphs (docs/specs/word-tracked-changes.md §3c).

The spec documents ``delete`` / ``through`` and is pinned with the rest of it in
``test_word_redline_paragraphs.py`` (``SPEC_DIGEST``). Calls here go through the
tool as the Runtime builds it, and are read back the way the model reads them.
"""

from __future__ import annotations

import pytest

from apis.inference_api.chat.routes import _build_word_document_tools
from apis.shared.files.document_read import docx_paragraphs
from tests.apis.inference_api.test_word_redline_paragraphs import (  # noqa: F401 - store is a fixture
    _call,
    _changes_doc,
    _said,
    store,
)

def _redline_tool(monkeypatch):
    monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")
    tools = _build_word_document_tools(["create_word_document"], "s1", "u1", "Pat Reviewer")
    return next(t for t in tools if t.tool_name == "redline_word_document")


def test_the_spec_documents_deletions_after_insertions(monkeypatch):
    doc = _changes_doc(_redline_tool(monkeypatch))
    assert '- delete: "paragraph"' in doc and "- through:" in doc
    assert doc.index('- insert: "after" or "before"') < doc.index('- delete: "paragraph"')


class TestCall:
    @pytest.mark.asyncio
    async def test_on_a_change_deletes_a_whole_paragraph(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        change = {"find": "has given written notice", "delete": "paragraph", "comment": "Not acceptable."}
        said = _said(await _call(tool, document_name="services-agreement", changes=[change]))
        assert "Tracked changes by Pat Reviewer: 1. deleted the paragraph" in said
        # What the model reads back: the paragraph is one deletion, and the
        # comment sits on the paragraph that stays.
        paragraphs = docx_paragraphs(store["data"])
        at = paragraphs.index("[deleted: Contractor has given written notice of the overdue amount; and]")
        assert paragraphs[at + 1] == (
            "University has not cured within thirty (30) days after that notice. "
            "[comment by Pat Reviewer: Not acceptable.]"
        )

    @pytest.mark.asyncio
    async def test_a_section_through_a_later_paragraph(self, monkeypatch, store):
        tool = _redline_tool(monkeypatch)
        change = {"find": "Payment", "delete": "Paragraphs", "through": "University has not cured"}
        said = _said(await _call(tool, document_name="services-agreement", changes=[change]))
        assert '1. deleted 6 paragraphs, "Payment" through' in said

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change,problem",
        [
            ({"find": "Kickoff", "delete": True}, '`delete` must be "paragraph"'),
            ({"find": "Kickoff", "delete": "paragraph", "through": 4}, "`through` must be text"),
            ({"find": "Kickoff", "delete": "paragraph", "insert": "after"}, "give `insert` or `delete`, not both"),
            ({"find": "Kickoff", "delete": "row"}, '`delete` must be "paragraph"'),
            ({"find": "Payment", "through": "Invoices"}, '`through` needs `delete: "paragraph"`'),
        ],
    )
    async def test_malformed_deletions_change_nothing(self, monkeypatch, store, change, problem):
        tool = _redline_tool(monkeypatch)
        result = await _call(tool, document_name="services-agreement", changes=[change])
        assert problem in _said(result)
        assert store == {}
