"""Restore rehydrates stripped documents as digests (offload spec §4E, PR-3).

Pins: block→upload matching (sanitized name incl. PromptBuilder's `_2`
suffix, size tiebreak, no double-claiming), the rendered digest replacing the
bytes with the `upload_id` handle inside, lazy outline-only digests for rows
without one (persisted, no model call), the placeholder fallback for
unmatched blocks, the never-raise contract, the kill switch, and the two
ledger events the session manager records.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session import document_rehydration as dr
from apis.shared.files.models import FileMetadata, FileStatus
from tests.shared.test_document_read import build_pdf

MODULE = "agents.main_agent.session.document_rehydration"


@pytest.fixture(autouse=True)
def diagnostics_enabled(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    monkeypatch.delenv("DOCUMENT_REHYDRATE_ENABLED", raising=False)


def _meta(upload_id="up-1", filename="BBR Policy.pdf", size=1000, mime="application/pdf", digest=None, user="u1"):
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size, s3_key="k", s3_bucket="b", status=FileStatus.READY, digest=digest,
    )


def _doc(name="BBR Policy_pdf", fmt="pdf", raw=b"x" * 1000):
    """PromptBuilder sanitizes the WHOLE filename, dot included: `BBR Policy.pdf` → `BBR Policy_pdf`."""
    return {"document": {"format": fmt, "name": name, "source": {"bytes": raw}}}


READY_DIGEST = {
    "version": 1, "status": "ready", "format": "pdf", "unit": "page", "count": 47,
    "sections": [{"start": 1, "title": "Declarations"}, {"start": 4, "title": "Insuring Agreements"}],
    "abstract": "A cyber liability policy form.", "tokens": 80,
}


class TestMatching:
    def test_prefers_name_and_size_then_name_then_format_and_size(self):
        a = _meta("a", "BBR Policy.pdf", 1000)
        b = _meta("b", "BBR Policy.pdf", 2000)
        c = _meta("c", "Other.pdf", 1000)
        assert dr.match_document("BBR Policy_pdf", "pdf", 2000, [a, b, c], set()) is b
        assert dr.match_document("BBR Policy_pdf", "pdf", 999, [a, b, c], set()) is a
        assert dr.match_document("Renamed", "pdf", 1000, [b, c], set()) is c
        assert dr.match_document("Renamed", "docx", 1000, [b, c], set()) is None

    def test_duplicate_suffix_and_claimed_rows(self):
        first = _meta("a", "memo.pdf", 10)
        second = _meta("b", "memo.pdf", 10)
        used: set = set()
        assert dr.match_document("memo_pdf", "pdf", 10, [first, second], used) is first
        used.add("a")
        assert dr.match_document("memo_pdf_2", "pdf", 10, [first, second], used) is second
        used.add("b")
        assert dr.match_document("memo_pdf_3", "pdf", 10, [first, second], used) is None


class TestRehydrate:
    def test_replaces_bytes_with_the_rendered_digest_and_handle(self, monkeypatch):
        meta = _meta(digest=READY_DIGEST)
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [meta])
        messages = [
            {"role": "user", "content": [{"text": "q\n\n[Attached files: BBR Policy.pdf]"}, _doc()]},
            {"role": "assistant", "content": [{"text": "a"}]},
        ]
        result = dr.rehydrate_documents(messages, session_id="s1", user_id="u1")

        block = result.messages[0]["content"][1]
        assert set(block) == {"text"}
        assert block["text"].startswith('<document-digest name="BBR Policy.pdf" upload_id="up-1" format="pdf" pages="47">')
        assert "<abstract>A cyber liability policy form.</abstract>" in block["text"]
        assert '<section page="4">Insuring Agreements</section>' in block["text"]
        assert (result.rehydrated, result.stripped, result.lazy_digests) == (1, 0, 0)
        assert result.digest_tokens > 0 and result.upload_ids == ["up-1"]
        # The originals are untouched (deep copy) and no bytes survive.
        assert "document" in messages[0]["content"][1]
        assert b"x" * 1000 not in repr(result.messages).encode()

    def test_lazy_digest_is_built_from_the_restored_bytes_and_persisted(self, monkeypatch):
        meta = _meta(digest=None, size=5)
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [meta])
        repo = MagicMock()
        monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
        raw = build_pdf(["1. Intro\nbody", "2. Terms"])
        messages = [{"role": "user", "content": [_doc(raw=raw)]}]

        result = dr.rehydrate_documents(messages, session_id="s1", user_id="u1")

        text = result.messages[0]["content"][0]["text"]
        assert 'upload_id="up-1"' in text and '<section page="2">2. Terms</section>' in text
        assert "<abstract>" not in text  # outline only — no model call on the restore path
        assert result.lazy_digests == 1
        repo.update_file_digest_sync.assert_called_once()
        user, upload, item = repo.update_file_digest_sync.call_args.args
        assert (user, upload, item["status"], item["count"]) == ("u1", "up-1", "ready", 2)
        assert item["tokens"] > 0

    def test_lazy_digest_persist_failure_still_serves_this_restore(self, monkeypatch):
        meta = _meta(digest=None)
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [meta])
        repo = MagicMock()
        repo.update_file_digest_sync.side_effect = RuntimeError("ddb")
        monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
        result = dr.rehydrate_documents([{"role": "user", "content": [_doc(raw=build_pdf(["Hello"]))]}], session_id="s1", user_id="u1")
        assert result.rehydrated == 1 and "<document-digest" in result.messages[0]["content"][0]["text"]

    def test_unmatched_blocks_keep_the_placeholder(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [_meta(filename="unrelated.pdf", size=1)])
        result = dr.rehydrate_documents([{"role": "user", "content": [_doc()]}], session_id="s1", user_id="u1")
        assert result.messages[0]["content"][0] == {"text": "[Document placeholder: name=BBR Policy_pdf, format=pdf, original_size=1000 bytes]"}
        assert (result.rehydrated, result.stripped, result.stripped_tokens) == (0, 1, 250)

    def test_lookup_failure_falls_back_for_every_block_with_one_attempt(self, monkeypatch):
        calls = []

        def _boom(*_):
            calls.append(1)
            raise RuntimeError("ddb down")

        monkeypatch.setattr(f"{MODULE}.load_session_documents", _boom)
        messages = [{"role": "user", "content": [_doc("a"), _doc("b")]}]
        result = dr.rehydrate_documents(messages, session_id="s1", user_id="u1")
        assert result.stripped == 2 and len(calls) == 1
        assert all(b["text"].startswith("[Document placeholder:") for b in result.messages[0]["content"])

    def test_no_documents_means_no_lookup(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: (_ for _ in ()).throw(AssertionError("queried")))
        result = dr.rehydrate_documents([{"role": "user", "content": [{"text": "hi"}]}], session_id="s1", user_id="u1")
        assert (result.rehydrated, result.stripped) == (0, 0)

    def test_s3location_blocks_are_left_alone(self, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [])
        block = {"document": {"format": "pdf", "name": "n", "source": {"s3Location": {"uri": "s3://b/k"}}}}
        result = dr.rehydrate_documents([{"role": "user", "content": [block]}], session_id="s1", user_id="u1")
        assert result.messages[0]["content"][0] == block

    def test_kill_switch_restores_the_placeholder_path(self, monkeypatch):
        monkeypatch.setenv("DOCUMENT_REHYDRATE_ENABLED", "false")
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: [_meta(digest=READY_DIGEST)])
        result = dr.rehydrate_documents([{"role": "user", "content": [_doc()]}], session_id="s1", user_id="u1")
        assert result.stripped == 1 and "[Document placeholder:" in result.messages[0]["content"][0]["text"]

    def test_load_filters_to_this_users_readable_documents(self, monkeypatch):
        rows = [_meta("a"), _meta("b", user="someone-else"), _meta("c", filename="d.csv", mime="text/csv")]
        repo = SimpleNamespace(list_session_files_sync=lambda sid, status=None: rows)
        monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
        assert [m.upload_id for m in dr.load_session_documents("s1", "u1")] == ["a"]


class TestSessionManagerEvents:
    def test_rehydrated_and_stripped_events_are_recorded(self, make_session_manager, monkeypatch):
        rows = [_meta("up-1", "BBR Policy.pdf", 1000, digest=READY_DIGEST)]
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: rows)
        manager = make_session_manager()
        messages = [{"role": "user", "content": [{"text": "q"}, _doc(), _doc("Orphan", raw=b"y" * 400)]}]
        stripped = manager._strip_document_bytes(messages)

        texts = [b["text"] for b in stripped[0]["content"][1:]]
        assert texts[0].startswith("<document-digest") and texts[1].startswith("[Document placeholder:")
        events = manager.drain_compaction_events()
        assert [e["kind"] for e in events] == ["document_rehydrated", "document_stripped"]
        assert events[0]["documents"] == 1 and events[0]["documentTokens"] > 0
        assert events[1] == {"kind": "document_stripped", "documents": 1, "documentTokens": 100}

    def test_restore_output_is_stable_across_restores(self, make_session_manager, monkeypatch):
        rows = [_meta("up-1", "BBR Policy.pdf", 1000, digest=READY_DIGEST)]
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: rows)
        manager = make_session_manager()
        messages = [{"role": "user", "content": [_doc()]}]
        first = manager._strip_document_bytes(messages)
        second = manager._strip_document_bytes(messages)
        assert first == second
