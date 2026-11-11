"""Document handles in the persisted message (unified-attachment-delivery §6.2, phase 4a).

Pins:

* the message written to Memory carries a handle (upload id, name, size, class)
  in place of an attached document's bytes, while the live message keeps them;
* only under the ``token`` profile, only for a user message's top-level
  document blocks (tool-result slices keep their bytes);
* **restore renders byte-identical history from a handle and from the bytes it
  replaced**, through the real Memory serialization, for every restore outcome:
  a ready digest, a lazy digest (read from the stored object), an unmatched
  placeholder, two copies of one file, rehydration off and ``document_read`` off;
* a message persisted with bytes before this change still restores;
* the message API returns a handle as the name and format clients read.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from strands.types.session import SessionMessage

from agents.main_agent.session import document_rehydration as dr
from apis.shared.files import attachment_handle as ah
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.sessions.messages import _ensure_document_base64
from tests.shared.test_document_read import build_pdf

from .conftest import TEST_SESSION_ID, TEST_USER_ID

MODULE = "agents.main_agent.session.document_rehydration"

READY_DIGEST = {
    "version": 1, "status": "ready", "format": "pdf", "unit": "page", "count": 3,
    "sections": [{"start": 1, "title": "Scope"}, {"start": 3, "title": "Budget"}],
    "abstract": "A grant notice.", "tokens": 40,
}

PDF = build_pdf(["1. Scope\nbody", "2. Terms", "3. Budget"])
DOCX = b"PK\x03\x04" + b"d" * 4_000


def _meta(upload_id, filename, raw, mime="application/pdf", digest=None, user=TEST_USER_ID):
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id=TEST_SESSION_ID, filename=filename, mime_type=mime,
        size_bytes=len(raw), s3_key=f"k/{upload_id}", s3_bucket="b", status=FileStatus.READY, digest=digest,
    )


def _doc(name, fmt, raw):
    return {"document": {"format": fmt, "name": name, "source": {"bytes": raw}}}


def _turn(*blocks, text="Compare these.\n\n[Attached files: notice.pdf, memo.docx]"):
    return {"role": "user", "content": [{"text": text}, *blocks]}


def _memory_round_trip(message: Dict[str, Any]) -> Dict[str, Any]:
    """What restore reads back: the SDK's own encoding, through JSON."""
    stored = json.loads(json.dumps(SessionMessage.from_message(message, 0).to_dict()))
    return SessionMessage.from_dict(stored).to_message()


def _resolver(rows):
    used: set = set()

    def _resolve(name, fmt, size):
        ref = dr.match_document(name, fmt, size, rows, used)
        if ref is None:
            return None
        used.add(ref.upload_id)
        return ref.upload_id

    return _resolve


def _restore(messages: List[Dict[str, Any]]):
    return dr.rehydrate_documents(messages, session_id=TEST_SESSION_ID, user_id=TEST_USER_ID)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCUMENT_REHYDRATE_ENABLED", raising=False)
    monkeypatch.delenv("DOCUMENT_READ_ENABLED", raising=False)
    ah.clear_session_uploads()
    yield
    ah.clear_session_uploads()


# ---------------------------------------------------------------------------
# The persisted form
# ---------------------------------------------------------------------------


class TestPersistedMessage:
    def test_bytes_become_a_handle_and_the_live_message_is_untouched(self):
        live = _turn(_doc("notice_pdf", "pdf", PDF))
        before = repr(live)
        persisted = ah.persisted_message(live, lambda *_: "up-notice")

        assert repr(live) == before
        assert persisted["content"][0] == live["content"][0]
        assert persisted["content"][1] == {"document": {"format": "pdf", "name": "notice_pdf", "source": {
            "attachmentHandle": {"version": 1, "uploadId": "up-notice", "sizeBytes": len(PDF), "class": "pdf"},
        }}}
        assert PDF not in repr(persisted).encode()

    def test_an_unresolved_block_has_no_upload_id(self):
        persisted = ah.persisted_message(_turn(_doc("memo_docx", "docx", DOCX)), lambda *_: None)
        handle = ah.handle_of(persisted["content"][1]["document"])
        assert handle == {"version": 1, "sizeBytes": len(DOCX), "class": "document"}

    def test_a_resolver_error_still_writes_a_handle(self):
        def _boom(*_):
            raise RuntimeError("x")

        persisted = ah.persisted_message(_turn(_doc("memo_docx", "docx", DOCX)), _boom)
        assert ah.handle_of(persisted["content"][1]["document"]) is not None

    def test_only_a_user_message_s_attached_documents(self):
        assistant = {"role": "assistant", "content": [_doc("x_pdf", "pdf", PDF)]}
        slice_result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": [
            {"json": {"upload_id": "up-notice"}}, _doc("notice_p1-1_ab", "pdf", PDF),
        ]}}]}
        text_only = _turn()
        for message in (assistant, slice_result, text_only):
            assert ah.persisted_message(message, lambda *_: "up") is message

    def test_a_handle_is_left_as_it_is(self):
        once = ah.persisted_message(_turn(_doc("notice_pdf", "pdf", PDF)), lambda *_: "up")
        assert ah.persisted_message(once, lambda *_: "other") is once


# ---------------------------------------------------------------------------
# Restore is byte-identical from a handle and from bytes
# ---------------------------------------------------------------------------


class TestRestoreIsTheSame:
    def _both(self, monkeypatch, rows, live, stored_objects=None):
        """Restore ``live`` persisted with bytes (today) and with handles."""
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: list(rows))
        repo = MagicMock()
        monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
        reads: List[str] = []

        def _stored(meta):
            reads.append(meta.upload_id)
            return stored_objects[meta.upload_id]

        monkeypatch.setattr(f"{MODULE}.stored_bytes", _stored)
        with_bytes = [_memory_round_trip(m) for m in live]
        with_handles = [_memory_round_trip(ah.persisted_message(m, _resolver(rows))) for m in live]
        assert all(PDF not in repr(m).encode() for m in with_handles)

        old = _restore(with_bytes)
        before_reads = list(reads)
        new = _restore(with_handles)
        return old, new, before_reads, reads[len(before_reads):]

    def _assert_same(self, old, new):
        assert json.dumps(new.messages, sort_keys=True) == json.dumps(old.messages, sort_keys=True)
        assert (new.rehydrated, new.stripped, new.lazy_digests, new.upload_ids) == (
            old.rehydrated, old.stripped, old.lazy_digests, old.upload_ids,
        )

    def test_ready_digests_need_no_read(self, monkeypatch):
        rows = [
            _meta("up-notice", "notice.pdf", PDF, digest=READY_DIGEST),
            _meta("up-memo", "memo.docx", DOCX, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  digest={**READY_DIGEST, "format": "docx", "unit": "paragraph"}),
        ]
        live = [_turn(_doc("notice_pdf", "pdf", PDF), _doc("memo_docx", "docx", DOCX)),
                {"role": "assistant", "content": [{"text": "Done."}]}]
        old, new, _, reads = self._both(monkeypatch, rows, live, {})
        self._assert_same(old, new)
        assert new.rehydrated == 2 and reads == []

    def test_a_lazy_digest_reads_the_stored_object_and_renders_the_same(self, monkeypatch):
        rows = [_meta("up-notice", "notice.pdf", PDF)]
        old, new, old_reads, reads = self._both(
            monkeypatch, rows, [_turn(_doc("notice_pdf", "pdf", PDF))], {"up-notice": PDF}
        )
        self._assert_same(old, new)
        assert new.lazy_digests == 1
        assert old_reads == [] and reads == ["up-notice"]

    def test_an_unmatched_block_gets_the_same_placeholder(self, monkeypatch):
        old, new, _, reads = self._both(monkeypatch, [], [_turn(_doc("direct_pdf", "pdf", PDF))], {})
        self._assert_same(old, new)
        assert new.stripped == 1 and reads == []
        assert new.messages[0]["content"][1]["text"] == dr.placeholder_text("direct_pdf", "pdf", len(PDF))

    def test_two_copies_of_one_file_map_to_two_rows(self, monkeypatch):
        rows = [_meta("up-b", "notice.pdf", PDF, digest=READY_DIGEST), _meta("up-a", "notice.pdf", PDF, digest=READY_DIGEST)]
        live = [_turn(_doc("notice_pdf", "pdf", PDF)), {"role": "assistant", "content": [{"text": "a"}]},
                _turn(_doc("notice_pdf_2", "pdf", PDF))]
        old, new, _, _ = self._both(monkeypatch, rows, live, {})
        self._assert_same(old, new)
        assert new.upload_ids == ["up-b", "up-a"]

    @pytest.mark.parametrize("env", ["DOCUMENT_REHYDRATE_ENABLED", "DOCUMENT_READ_ENABLED"])
    def test_with_a_kill_switch_off(self, monkeypatch, env):
        monkeypatch.setenv(env, "false")
        rows = [_meta("up-notice", "notice.pdf", PDF, digest=READY_DIGEST)]
        old, new, _, _ = self._both(monkeypatch, rows, [_turn(_doc("notice_pdf", "pdf", PDF))], {})
        self._assert_same(old, new)

    def test_a_failed_read_falls_back_to_the_placeholder(self, monkeypatch):
        rows = [_meta("up-notice", "notice.pdf", PDF)]
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: rows)

        def _fail(meta):
            raise OSError("s3 down")

        monkeypatch.setattr(f"{MODULE}.stored_bytes", _fail)
        persisted = _memory_round_trip(ah.persisted_message(_turn(_doc("notice_pdf", "pdf", PDF)), _resolver(rows)))
        result = _restore([persisted])
        assert result.messages[0]["content"][1]["text"] == dr.placeholder_text("notice_pdf", "pdf", len(PDF))

    def test_a_message_persisted_with_bytes_still_restores(self, monkeypatch):
        """§9.1 'What stays': events written before phase 4 carry bytes."""
        rows = [_meta("up-notice", "notice.pdf", PDF, digest=READY_DIGEST)]
        monkeypatch.setattr(f"{MODULE}.load_session_documents", lambda *_: rows)
        result = _restore([_memory_round_trip(_turn(_doc("notice_pdf", "pdf", PDF)))])
        assert result.rehydrated == 1
        assert result.messages[0]["content"][1]["text"].startswith('<document-digest name="notice.pdf" upload_id="up-notice"')


# ---------------------------------------------------------------------------
# The session manager's write
# ---------------------------------------------------------------------------


class TestAppendMessage:
    def _manager(self, make_session_manager, monkeypatch, enabled):
        monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "true" if enabled else "")
        mgr = make_session_manager()
        sent: List[Dict[str, Any]] = []
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

        monkeypatch.setattr(AgentCoreMemorySessionManager, "append_message", lambda self, m, a, **k: sent.append(m))
        return mgr, sent

    def test_the_token_profile_persists_a_handle(self, make_session_manager, monkeypatch):
        mgr, sent = self._manager(make_session_manager, monkeypatch, True)
        ah.remember_session_uploads(TEST_USER_ID, TEST_SESSION_ID, [_meta("up-notice", "notice.pdf", PDF)])
        live = _turn(_doc("notice_pdf", "pdf", PDF))
        agent = MagicMock()
        agent.messages = [live]

        mgr.append_message(live, agent)

        assert ah.handle_of(sent[0]["content"][1]["document"])["uploadId"] == "up-notice"
        assert agent.messages[0]["content"][1]["document"]["source"]["bytes"] == PDF, "the warm agent keeps its bytes"

    def test_another_user_s_uploads_are_not_named(self, make_session_manager, monkeypatch):
        mgr, sent = self._manager(make_session_manager, monkeypatch, True)
        ah.remember_session_uploads("someone-else", TEST_SESSION_ID, [_meta("up-x", "notice.pdf", PDF, user="someone-else")])
        mgr.append_message(_turn(_doc("notice_pdf", "pdf", PDF)), MagicMock())
        assert "uploadId" not in ah.handle_of(sent[0]["content"][1]["document"])

    def test_the_legacy_profile_persists_the_bytes(self, make_session_manager, monkeypatch):
        mgr, sent = self._manager(make_session_manager, monkeypatch, False)
        live = _turn(_doc("notice_pdf", "pdf", PDF))
        mgr.append_message(live, MagicMock())
        assert sent == [live]


def test_the_message_api_shows_a_handle_as_its_name_and_format():
    persisted = ah.persisted_message(_turn(_doc("notice_pdf", "pdf", PDF)), lambda *_: "up")
    assert _ensure_document_base64(persisted["content"][1]["document"]) == {"format": "pdf", "name": "notice_pdf"}

