"""The document-lifecycle analytics (docs/specs/document-context-offload.md §6.1):

- ``summarize_document_context`` — the attachment footprint of the live
  context, persisted per cost row (content-free by construction).
- the ``document_stripped`` compaction-ledger event ``_strip_document_bytes``
  records, so the restore defect is measured before PR-3 fixes it.
- the ``documentReads`` ledger entry the ``ContextLedgerHook`` tallies from
  ``document_read`` results.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.main_agent.session.compaction_policy import CHARS_PER_TOKEN, IMAGE_TOKEN_ESTIMATE
from agents.main_agent.session.document_context import summarize_document_context
from agents.main_agent.session.hooks.context_ledger import ContextLedgerHook


@pytest.fixture(autouse=True)
def diagnostics_enabled(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)


def _doc(name="a", fmt="pdf", size=4000):
    return {"document": {"format": fmt, "name": name, "source": {"bytes": b"x" * size}}}


def _image(size=100):
    return {"image": {"format": "png", "source": {"bytes": b"i" * size}}}


class TestSummary:
    def test_empty_or_malformed_is_none(self):
        assert summarize_document_context(None) is None
        assert summarize_document_context([]) is None

    def test_counts_tokens_formats_digests_and_the_last_prompts_attachments(self):
        messages = [
            {"role": "user", "content": [{"text": "q1"}, _doc("a", "pdf", 4000), _doc("b", "docx", 800), _image()]},
            {"role": "assistant", "content": [{"text": "a1"}]},
            {"role": "user", "content": [{"text": "q2"}, {"text": "[Document placeholder: name=c, format=pdf, original_size=9 bytes]"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "document_read", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "status": "success", "content": [
                {"json": {"pages_returned": 2}}, _doc("slice", "pdf", 2000)]}}]},
            {"role": "user", "content": [{"text": "  <document-digest upload_id='u'/>"}, {"text": "q3"}]},
        ]
        assert summarize_document_context(messages) == {
            "hasDocuments": True,
            "documentCount": 3,
            "documentTokens": 4000 // CHARS_PER_TOKEN + 800 // CHARS_PER_TOKEN + IMAGE_TOKEN_ESTIMATE,
            "documentDigests": 2,
            "documentsAttached": 0,   # the last prompt (q3) attached nothing
            "documentSlices": 1,
            "documentSliceTokens": 2000 // CHARS_PER_TOKEN,
            "documentMime": {"pdf": 1, "docx": 1, "image": 1},
        }

    def test_a_digest_only_context_reads_as_no_documents(self):
        messages = [
            {"role": "user", "content": [{"text": "[Document placeholder: name=a, format=pdf, original_size=1 bytes]"}, {"text": "q"}]},
            {"role": "assistant", "content": [{"text": "a"}]},
        ]
        summary = summarize_document_context(messages)
        assert summary["hasDocuments"] is False
        assert summary["documentCount"] == 0 and summary["documentDigests"] == 1

    def test_attach_turn_counts_its_own_attachments(self):
        messages = [{"role": "user", "content": [{"text": "q"}, _doc(), _doc("b")]}]
        assert summarize_document_context(messages)["documentsAttached"] == 2

    def test_never_emits_names_or_bytes(self):
        summary = summarize_document_context([{"role": "user", "content": [_doc("Secret Contract")]}])
        assert "Secret" not in repr(summary) and b"x" not in repr(summary).encode()


class TestStripEvent:
    """With no upload rows to match, restore falls back to the placeholder and
    records ``document_stripped`` — the pre-PR-3 behavior, kept as the floor.
    The rehydrated path is pinned in ``test_document_rehydration.py``."""

    @pytest.fixture(autouse=True)
    def no_upload_rows(self, monkeypatch):
        monkeypatch.setattr(
            "agents.main_agent.session.document_rehydration.load_session_documents", lambda *_: []
        )

    def test_strip_records_one_content_free_event(self, make_session_manager):
        manager = make_session_manager()
        messages = [
            {"role": "user", "content": [{"text": "q"}, _doc("a", "pdf", 8000), _doc("b", "txt", 400)]},
            {"role": "assistant", "content": [{"text": "a"}]},
        ]
        stripped = manager._strip_document_bytes(messages)
        assert all("document" not in b for b in stripped[0]["content"])
        assert manager.drain_compaction_events() == [
            {"kind": "document_stripped", "documents": 2, "documentTokens": 8400 // CHARS_PER_TOKEN}
        ]

    def test_nothing_to_strip_records_nothing(self, make_session_manager):
        manager = make_session_manager()
        manager._strip_document_bytes([{"role": "user", "content": [{"text": "q"}]}])
        assert manager.drain_compaction_events() == []

    def test_kill_switch_still_strips_but_records_nothing(self, make_session_manager, monkeypatch):
        monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
        manager = make_session_manager()
        stripped = manager._strip_document_bytes([{"role": "user", "content": [_doc()]}])
        assert "text" in stripped[0]["content"][0]
        assert manager.drain_compaction_events() == []


def _tool_event(name="document_read", status="success", pages=3, size=500):
    content = [{"json": {"pages_returned": pages}}]
    if size:
        content.append(_doc("slice", size=size))
    return MagicMock(tool_use={"toolUseId": "t", "name": name}, result={"toolUseId": "t", "status": status, "content": content})


class TestLedgerDocumentReads:
    def test_reads_are_attributed_to_the_requesting_call(self):
        hook = ContextLedgerHook()
        agent = MagicMock()
        agent.conversation_manager.removed_message_count = None
        agent._session_manager = None
        hook._on_turn_start(MagicMock())
        hook._on_before_model_call(MagicMock(agent=agent))      # call 0
        hook._on_after_tool_call(_tool_event(pages=3, size=500))
        hook._on_after_tool_call(_tool_event(pages=2, size=300))
        hook._on_before_model_call(MagicMock(agent=agent))      # call 1
        hook._on_after_tool_call(_tool_event(name="calculator"))
        hook._on_after_tool_call(_tool_event(status="error"))

        assert hook.ledger_for_call(0) == {"documentReads": {"calls": 2, "pages": 5, "bytes": 800}}
        assert hook.ledger_for_call(1) is None

    def test_kill_switch_records_nothing(self, monkeypatch):
        monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
        hook = ContextLedgerHook()
        hook._on_turn_start(MagicMock())
        hook._on_before_model_call(MagicMock())
        hook._on_after_tool_call(_tool_event())
        assert hook.ledger_for_call(0) is None
