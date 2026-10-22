"""DOCUMENT_READ_ENABLED must reach every path that promises the tool.

The gap this guards: the flag was read only at the tool-injection gate
(``_document_tools_gate``). Neither the restore path nor the live offload
consulted it, so pulling the one kill switch an operator would actually reach
for left restore emitting ``<document-digest upload_id=…>`` handles for a tool
that would not be injected — and left the live path still evicting bytes, which
is strictly worse than the pre-offload world, where live bytes never left.

Rules pinned here:

* restore still renders a digest (it has to drop the bytes either way, and a
  digest beats the placeholder) but **without** the ``upload_id`` handle;
* the live offload **stops entirely** — bytes stay inline;
* with the flag on, everything is byte-identical to before.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agents.main_agent.session import document_offload as do
from agents.main_agent.session import document_rehydration as rh
from apis.shared.files.document_digest import DigestSection, DocumentDigest, render_digest
from apis.shared.files.models import FileMetadata, FileStatus

RAW = b"P" * 40_000


def _meta() -> FileMetadata:
    digest = DocumentDigest(
        status="ready", format="pdf", unit="page", count=10,
        abstract="An abstract.", sections=[DigestSection(start=1, title="Intro")],
    )
    return FileMetadata(
        upload_id="u1", user_id="user-1", filename="Policy.pdf", mime_type="application/pdf",
        size_bytes=len(RAW), status=FileStatus.READY, s3_key="k/u1", s3_bucket="b",
        session_id="s1", digest=digest.to_item(),
    )


class _Repo:
    def list_session_files_sync(self, session_id, status=None):
        return [_meta()]

    def update_file_digest_sync(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def repo(monkeypatch):
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: _Repo())


def _doc_block() -> Dict[str, Any]:
    return {"document": {"format": "pdf", "name": "Policy_pdf", "source": {"bytes": RAW}}}


def _conversation() -> list:
    """Attach on turn 1, three turns elapsed — unpinned and offloadable."""
    return [
        {"role": "user", "content": [{"text": "here"}, _doc_block()]},
        {"role": "assistant", "content": [{"text": "a"}]},
        {"role": "user", "content": [{"text": "q2"}]},
        {"role": "assistant", "content": [{"text": "b"}]},
        {"role": "user", "content": [{"text": "q3"}]},
        {"role": "assistant", "content": [{"text": "c"}]},
    ]


class TestRenderHandle:
    def test_default_is_unchanged(self):
        d = DocumentDigest.from_item(_meta().digest)
        assert 'upload_id="u1"' in render_digest(d, filename="Policy.pdf", upload_id="u1")

    def test_handle_can_be_omitted_without_losing_content(self):
        d = DocumentDigest.from_item(_meta().digest)
        text = render_digest(d, filename="Policy.pdf", upload_id="u1", include_handle=False)
        assert "upload_id" not in text
        # ...but the content the model actually reasons from survives.
        assert "An abstract." in text and "Intro" in text and 'pages="10"' in text


class TestRestorePath:
    def test_tool_on_renders_the_handle(self, monkeypatch):
        monkeypatch.delenv("DOCUMENT_READ_ENABLED", raising=False)
        res = rh.rehydrate_documents([{"role": "user", "content": [_doc_block()]}],
                                     session_id="s1", user_id="user-1")
        assert 'upload_id="u1"' in res.messages[0]["content"][0]["text"]

    def test_tool_off_keeps_the_digest_but_drops_the_handle(self, monkeypatch):
        monkeypatch.setenv("DOCUMENT_READ_ENABLED", "false")
        res = rh.rehydrate_documents([{"role": "user", "content": [_doc_block()]}],
                                     session_id="s1", user_id="user-1")
        text = res.messages[0]["content"][0]["text"]
        assert res.rehydrated == 1, "a digest still beats the placeholder"
        assert "upload_id" not in text, "must not advertise a tool that is not injected"
        assert "An abstract." in text


class TestLiveOffloadPath:
    def test_tool_on_offloads(self, monkeypatch):
        monkeypatch.delenv("DOCUMENT_READ_ENABLED", raising=False)
        assert do.offload_enabled_for("s1") is True
        msgs = _conversation()
        cands = do.candidate_documents(msgs, do.pinned_document_names(msgs, None))
        assert do.offload_documents(msgs, cands, session_id="s1", user_id="user-1").offloaded == 1

    def test_tool_off_is_disabled_for_every_session(self, monkeypatch):
        monkeypatch.setenv("DOCUMENT_READ_ENABLED", "false")
        assert do.offload_enabled_for("s1") is False
        # ...even for a session the rollout bucket would otherwise treat.
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", "100")
        assert all(do.offload_enabled_for(f"session-{i}") is False for i in range(50))

    def test_tool_off_leaves_the_bytes_inline(self, monkeypatch):
        """The regression in one assertion: the live path must never evict a
        document whose only recovery path is switched off."""
        monkeypatch.setenv("DOCUMENT_READ_ENABLED", "false")
        msgs = _conversation()
        if do.offload_enabled_for("s1"):  # pragma: no cover - the gate above
            cands = do.candidate_documents(msgs, do.pinned_document_names(msgs, None))
            do.offload_documents(msgs, cands, session_id="s1", user_id="user-1")
        assert msgs[0]["content"][1]["document"]["source"]["bytes"] == RAW
