"""Inline spreadsheets and decks are stored as session files before the turn runs.

The attachment phase diverts csv/xlsx/pptx out of the prompt and tells the
model the Spreadsheet Analysis / PowerPoint tools can reach them. Those tools
only see files that have a ``FileMetadata`` row, which an upload has and an
inline base64 ``files`` entry did not. These pin the wiring:

- only the *inline* diverted files are persisted (resolved uploads already
  have rows; identity decides, not filename);
- a file the persister could not store leaves the diverted set, leaves the
  ``[Attached files: …]`` marker, and is named in the guidance with its reason;
- the kill switch restores the pre-fix behaviour exactly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, List

import pytest

import apis.inference_api.chat.routes as routes
from apis.inference_api.chat.models import FileContent, InvocationRequest
from apis.shared.files.inline_persist import PersistFailure

CSV = "text/csv"
PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@dataclass
class _ResolvedUpload:
    filename: str
    content_type: str
    bytes: str


class _FakePersister:
    def __init__(self, fail_names: set | None = None) -> None:
        self.calls: List[dict] = []
        self.fail_names = fail_names or set()

    async def persist(self, *, user_id: str, session_id: str, files, source: str = "inline"):
        self.calls.append({"user_id": user_id, "session_id": session_id, "files": list(files)})
        persisted, failures = [], []
        for f in files:
            if f.filename in self.fail_names:
                failures.append(PersistFailure(file=f, reason="it could not be stored"))
            else:
                persisted.append(f)
        return persisted, failures


class _FakeResolver:
    def __init__(self, resolved: List[_ResolvedUpload]) -> None:
        self._resolved = resolved

    async def resolve_files(self, *, user_id: str, upload_ids: List[str], max_files: Any = None):
        return self._resolved


@pytest.fixture
def phase(monkeypatch):
    """Wire the attachment phase to fakes; returns a runner + the persister."""
    persister = _FakePersister()
    state = {"persister": persister, "resolved": []}

    async def _no_pending(*args, **kwargs):
        return []

    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "pop_pending_attachments", _no_pending)
    monkeypatch.setattr(routes, "get_inline_attachment_persister", lambda: state["persister"])
    monkeypatch.setattr(routes, "get_file_resolver", lambda: _FakeResolver(state["resolved"]))

    async def run(files=None, upload_ids=None):
        req = InvocationRequest(
            session_id="sess-1",
            message="look at this",
            files=files,
            file_upload_ids=upload_ids,
        )
        return await routes._resolve_turn_attachments(req, "user-1", None, is_resume=False, is_continuation=False)

    return run, state


@pytest.mark.asyncio
async def test_inline_diverted_files_are_persisted_and_pdf_is_not(phase):
    run, state = phase
    csv = FileContent(filename="data.csv", content_type=CSV, bytes=_b64(b"a,b\n"))
    pdf = FileContent(filename="doc.pdf", content_type=PDF, bytes=_b64(b"%PDF"))
    deck = FileContent(filename="deck.pptx", content_type=PPTX, bytes=_b64(b"PK"))

    out = await run(files=[csv, pdf, deck])

    assert len(state["persister"].calls) == 1
    call = state["persister"].calls[0]
    assert call["user_id"] == "user-1" and call["session_id"] == "sess-1"
    # Both diverted classes, in attachment order; the inline-for-Bedrock PDF is untouched.
    assert [f.filename for f in call["files"]] == ["data.csv", "deck.pptx"]
    assert [f.filename for f in out.diverted_tabular] == ["data.csv"]
    assert [f.filename for f in out.diverted_presentations] == ["deck.pptx"]
    assert [f.filename for f in out.files_to_send] == ["doc.pdf"]
    assert out.unpersisted_inline == []
    assert out.marker_names == ["data.csv", "doc.pdf", "deck.pptx"]


@pytest.mark.asyncio
async def test_resolved_uploads_are_not_re_persisted(phase):
    run, state = phase
    state["resolved"] = [_ResolvedUpload("sales.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _b64(b"PK"))]

    out = await run(upload_ids=["up-1"])

    assert state["persister"].calls == []
    assert [f.filename for f in out.diverted_tabular] == ["sales.xlsx"]


@pytest.mark.asyncio
async def test_identity_not_filename_selects_the_inline_copy(phase):
    run, state = phase
    # Same name arrives both ways. Dedupe keeps the first (inline) occurrence,
    # and that object is the one persisted — never the resolved upload.
    inline = FileContent(filename="same.csv", content_type=CSV, bytes=_b64(b"inline"))
    state["resolved"] = [_ResolvedUpload("same.csv", CSV, _b64(b"uploaded"))]

    out = await run(files=[inline], upload_ids=["up-1"])

    assert len(state["persister"].calls) == 1
    assert state["persister"].calls[0]["files"] == [inline]
    assert [f.filename for f in out.diverted_tabular] == ["same.csv"]


@pytest.mark.asyncio
async def test_failure_leaves_the_diverted_set_the_marker_and_names_the_reason(phase):
    run, state = phase
    state["persister"] = _FakePersister(fail_names={"bad.csv"})
    good = FileContent(filename="good.csv", content_type=CSV, bytes=_b64(b"1"))
    bad = FileContent(filename="bad.csv", content_type=CSV, bytes=_b64(b"2"))
    pdf = FileContent(filename="doc.pdf", content_type=PDF, bytes=_b64(b"%PDF"))

    out = await run(files=[good, bad, pdf])

    assert [f.filename for f in out.diverted_tabular] == ["good.csv"]
    assert [x.file.filename for x in out.unpersisted_inline] == ["bad.csv"]
    # The card must not be promised for a file that is not in the session.
    assert out.marker_names == ["good.csv", "doc.pdf"]

    note = routes._build_attachment_guidance(
        out.diverted_tabular,
        out.diverted_presentations,
        out.oversized_inline,
        ["list_spreadsheets", "analyze_spreadsheet"],
        unpersisted=out.unpersisted_inline,
    )
    assert "`good.csv` are available through the Spreadsheet Analysis tool" in note
    assert "`bad.csv` (it could not be stored) could not be stored for this conversation" in note
    assert "Upload them through the Files panel" in note


@pytest.mark.asyncio
async def test_kill_switch_restores_the_old_behaviour(phase, monkeypatch):
    run, state = phase
    monkeypatch.setenv("INLINE_ATTACHMENT_PERSIST_ENABLED", "false")
    csv = FileContent(filename="data.csv", content_type=CSV, bytes=_b64(b"a,b\n"))

    out = await run(files=[csv])

    assert state["persister"].calls == []
    assert [f.filename for f in out.diverted_tabular] == ["data.csv"]
    assert out.unpersisted_inline == []


@pytest.mark.asyncio
async def test_no_diverted_inline_means_no_call(phase):
    run, state = phase
    pdf = FileContent(filename="doc.pdf", content_type=PDF, bytes=_b64(b"%PDF"))
    await run(files=[pdf])
    assert state["persister"].calls == []


def test_flag_default_on_only_false_disables(monkeypatch):
    from apis.shared.feature_flags import inline_attachment_persist_enabled

    monkeypatch.delenv("INLINE_ATTACHMENT_PERSIST_ENABLED", raising=False)
    assert inline_attachment_persist_enabled() is True
    monkeypatch.setenv("INLINE_ATTACHMENT_PERSIST_ENABLED", "")
    assert inline_attachment_persist_enabled() is True
    monkeypatch.setenv("INLINE_ATTACHMENT_PERSIST_ENABLED", "False")
    assert inline_attachment_persist_enabled() is False
