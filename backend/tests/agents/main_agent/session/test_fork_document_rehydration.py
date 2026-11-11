"""A fork's documents restore from its parent's and root's upload rows.

A fork copies its parent's stored messages verbatim (``fork_service``), so a
document block, bytes or handle, arrives in the fork unchanged, but its upload
row stays on the session the file was attached in. Matching a fork's blocks to
the fork's own rows alone restored every one of them as a placeholder.

Pins: which sessions a fork reads (parent then root, from the row the restore
already read), the fixed order that keeps restore byte-stable, user scoping
(another user's rows in an ancestor partition are never matched), the session
manager handing the ancestors to the restore, and the ``document_read`` gate
and listing seeing the same sessions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.main_agent.session import document_rehydration as dr
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.sessions.fork import ancestor_session_ids

MODULE = "agents.main_agent.session.document_rehydration"

READY_DIGEST = {
    "version": 1, "status": "ready", "format": "pdf", "unit": "page", "count": 3,
    "sections": [{"start": 1, "title": "Terms"}], "abstract": "A short policy.", "tokens": 40,
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.delenv("DOCUMENT_REHYDRATE_ENABLED", raising=False)


def _meta(upload_id, session_id, filename="policy.pdf", size=1000, user="u1", digest=READY_DIGEST,
          mime="application/pdf"):
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id=session_id, filename=filename, mime_type=mime,
        size_bytes=size, s3_key="k", s3_bucket="b", status=FileStatus.READY, digest=digest,
    )


def _bytes_block(name="policy_pdf", size=1000):
    return {"document": {"format": "pdf", "name": name, "source": {"bytes": b"x" * size}}}


def _handle_block(upload_id="up-root", name="policy_pdf", size=1000):
    return {"document": {"format": "pdf", "name": name, "source": {"attachmentHandle": {
        "version": 1, "uploadId": upload_id, "sizeBytes": size, "class": "pdf"}}}}


def _fork_row(parent="parent", root="root"):
    return {"forkedFrom": {"sessionId": parent, "messageIndex": 2, "cut": 2}, "rootSessionId": root}


class _Repo:
    """``list_session_files_sync`` over a fixed set of partitions, counting reads."""

    def __init__(self, partitions):
        self.partitions = partitions
        self.reads = []

    def list_session_files_sync(self, session_id, status=None):
        self.reads.append(session_id)
        return list(self.partitions.get(session_id, []))


@pytest.fixture
def repo(monkeypatch):
    holder = {}

    def _install(partitions):
        holder["repo"] = _Repo(partitions)
        monkeypatch.setattr(
            "apis.shared.files.repository.get_file_upload_repository", lambda: holder["repo"]
        )
        return holder["repo"]

    return _install


class TestAncestorSessionIds:
    def test_not_a_fork_has_none(self):
        assert ancestor_session_ids("s1", None) == ()
        assert ancestor_session_ids("s1", {"title": "x"}) == ()
        # A stray root without an origin is not a fork either.
        assert ancestor_session_ids("s1", {"rootSessionId": "r"}) == ()

    def test_parent_then_root(self):
        assert ancestor_session_ids("fork", _fork_row("parent", "root")) == ("parent", "root")

    def test_first_generation_fork_names_the_root_once(self):
        assert ancestor_session_ids("fork", _fork_row("root", "root")) == ("root",)

    def test_never_names_itself(self):
        assert ancestor_session_ids("fork", _fork_row("fork", "fork")) == ()


class TestLoadSessionDocuments:
    def test_own_rows_first_then_each_ancestor_in_order(self, repo):
        r = repo({
            "fork": [_meta("up-own", "fork", "own.pdf")],
            "parent": [_meta("up-parent", "parent", "mid.pdf")],
            "root": [_meta("up-root", "root")],
        })
        rows = dr.load_session_documents("fork", "u1", ("parent", "root"))
        assert [m.upload_id for m in rows] == ["up-own", "up-parent", "up-root"]
        assert r.reads == ["fork", "parent", "root"]

    def test_another_users_rows_in_an_ancestor_are_never_returned(self, repo):
        repo({"root": [_meta("up-theirs", "root", user="u2"), _meta("up-mine", "root")]})
        assert [m.upload_id for m in dr.load_session_documents("fork", "u1", ("root",))] == ["up-mine"]

    def test_not_a_fork_reads_one_partition(self, repo):
        r = repo({"s1": [_meta("a", "s1")]})
        dr.load_session_documents("s1", "u1")
        assert r.reads == ["s1"]


class TestForkRestore:
    def _messages(self, block):
        return [
            {"role": "user", "content": [{"text": "q\n\n[Attached files: policy.pdf]"}, block]},
            {"role": "assistant", "content": [{"text": "a"}]},
        ]

    @pytest.mark.parametrize("block", [_bytes_block(), _handle_block()], ids=["bytes", "handle"])
    def test_a_forks_document_matches_its_roots_row(self, repo, block):
        repo({"root": [_meta("up-root", "root")]})
        result = dr.rehydrate_documents(
            self._messages(block), session_id="fork", user_id="u1", ancestor_session_ids=("root",)
        )
        text = result.messages[0]["content"][1]["text"]
        assert text.startswith("<document-digest") and "up-root" in text
        assert result.rehydrated == 1 and result.stripped == 0

    def test_without_ancestors_it_is_the_old_placeholder(self, repo):
        repo({"root": [_meta("up-root", "root")]})
        result = dr.rehydrate_documents(self._messages(_bytes_block()), session_id="fork", user_id="u1")
        assert result.messages[0]["content"][1]["text"].startswith("[Document placeholder:")

    def test_another_users_row_in_the_root_is_never_matched(self, repo):
        # Same name, same size: only the user filter keeps it out.
        repo({"root": [_meta("up-theirs", "root", user="u2")]})
        result = dr.rehydrate_documents(
            self._messages(_bytes_block()), session_id="fork", user_id="u1", ancestor_session_ids=("root",)
        )
        text = result.messages[0]["content"][1]["text"]
        assert text.startswith("[Document placeholder:") and "up-theirs" not in text

    def test_restore_is_byte_stable(self, repo):
        repo({
            "fork": [_meta("up-own", "fork")],
            "root": [_meta("up-root", "root")],
        })
        messages = [
            {"role": "user", "content": [_bytes_block(), _bytes_block("policy_pdf_2")]},
        ]
        runs = [
            dr.rehydrate_documents(messages, session_id="fork", user_id="u1", ancestor_session_ids=("root",))
            for _ in range(3)
        ]
        assert runs[0].messages == runs[1].messages == runs[2].messages
        # Two copies of one name claim two rows: the fork's own first.
        assert runs[0].upload_ids == ["up-own", "up-root"]


class TestSessionManagerPassesAncestors:
    def test_restore_reads_the_ancestors_off_the_restore_row(self, make_session_manager, monkeypatch):
        seen = []

        def _load(session_id, user_id, ancestor_session_ids=()):
            seen.append((session_id, tuple(ancestor_session_ids)))
            return [_meta("up-root", "root", user=user_id)]

        monkeypatch.setattr(f"{MODULE}.load_session_documents", _load)
        manager = make_session_manager()
        manager._restore_row = _fork_row("parent", "root")
        out = manager._strip_document_bytes([{"role": "user", "content": [_bytes_block()]}])

        assert seen == [(manager.config.session_id, ("parent", "root"))]
        assert "up-root" in out[0]["content"][0]["text"]

    def test_not_a_fork_passes_none(self, make_session_manager):
        manager = make_session_manager()
        assert manager.document_ancestor_session_ids() == ()
        manager._restore_row = {"title": "plain"}
        assert manager.document_ancestor_session_ids() == ()


@pytest.fixture
def clear_memo():
    from apis.inference_api.chat import routes

    routes._DOCUMENT_SESSIONS.clear()
    yield
    routes._DOCUMENT_SESSIONS.clear()


class TestDocumentReadSeesTheRoot:
    @pytest.mark.asyncio
    async def test_gate_asks_the_ancestors_only_when_the_fork_has_none(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        lookup = AsyncMock(side_effect=lambda user, sid: sid == "root")
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        tools = await _build_document_tools("fork", "u1", ancestor_session_ids=("parent", "root"))
        assert [t.tool_name for t in tools] == ["document_read"]
        assert [c.args for c in lookup.await_args_list] == [("u1", "fork"), ("u1", "parent"), ("u1", "root")]

    @pytest.mark.asyncio
    async def test_gate_stops_at_the_forks_own_document(self, monkeypatch, clear_memo):
        from apis.inference_api.chat.routes import _build_document_tools

        lookup = AsyncMock(return_value=True)
        monkeypatch.setattr("apis.shared.files.document_read.session_has_documents", lookup)
        await _build_document_tools("fork", "u1", ancestor_session_ids=("root",))
        lookup.assert_awaited_once_with("u1", "fork")

    @pytest.mark.asyncio
    async def test_listing_includes_this_users_ancestor_documents_only(self, monkeypatch):
        from apis.shared.files import document_read

        partitions = {
            "fork": [_meta("up-own", "fork", "own.pdf")],
            "root": [_meta("up-root", "root"), _meta("up-theirs", "root", "x.pdf", user="u2")],
        }
        repo_mock = MagicMock()
        repo_mock.list_session_files = AsyncMock(side_effect=lambda sid, status=None: partitions.get(sid, []))
        monkeypatch.setattr(document_read, "get_file_upload_repository", lambda: repo_mock)

        listing = await document_read.list_session_documents("u1", "fork", ancestor_session_ids=("root",))
        assert [d["upload_id"] for d in listing["documents"]] == ["up-own", "up-root"]
