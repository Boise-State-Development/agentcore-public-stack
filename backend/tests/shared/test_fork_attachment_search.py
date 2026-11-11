"""Attachment search inside a forked conversation.

A fork ("Fork from here", ``docs/specs/conversation-rewind-and-fork.md`` §7)
copies its parent's messages, document blocks included, but not the upload rows
those blocks name, nor their entries in the attachments KB: both stay under the
session the file was attached in. #2000 let a fork list and read those
documents through its parent's and root's rows; search still filtered on the
fork's own session id, so ``document_read``'s ``query`` found nothing and the
conversation-level planner never counted the documents as searchable.

Pins:

- through the tool, a fork's ``query`` finds a document indexed under its root,
  session-wide and by ``upload_id``;
- a fork of another user's conversation finds nothing of theirs, even with the
  owner's session ids as its ancestors;
- the filter keeps its single ``equals`` for a session that is not a fork and
  widens only the session clause, with ``in``, for one that is;
- the planner's conversation-files read and the re-index on open include the
  ancestors' rows, the caller's only.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from apis.shared.files import attachment_index as ai
from apis.shared.files import attachment_search as search_mod
from apis.shared.files import document_read as dr
from apis.shared.files import large_document
from apis.shared.files.attachment_search import attachment_filter
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.kb_backend.protocol import Chunk
from apis.shared.sessions.fork import ancestor_session_ids

MB = 1024 * 1024
OWNER = "user-1"
OTHER = "user-2"
ROOT = "sess-root"
PARENT = "sess-parent"
FORK = "sess-fork"
FORK_ROW = {"forkedFrom": {"sessionId": PARENT, "messageIndex": 4, "cut": 4}, "rootSessionId": ROOT}


def _row(upload_id: str, *, user: str = OWNER, session: str = ROOT, kb: Optional[str] = "indexed",
         filename: str = "manual.pdf") -> FileMetadata:
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id=session, filename=filename, mime_type="application/pdf",
        size_bytes=38 * MB, s3_key=f"user-files/{user}/{session}/{upload_id}/{filename}", s3_bucket="b",
        status=FileStatus.READY,
        preparation=large_document.preparation_record(status="ready", fmt="pdf", page_count=10, shard_pages=10),
        kb_index=ai.kb_index_record(kb) if kb else None,
    )


def _indexed(row: FileMetadata, text: str = "the notice period is 30 days") -> Chunk:
    """The chunk the KB holds for a row: id and attributes name where it was attached."""
    metadata = {"user_id": row.user_id, "session_id": row.session_id, "upload_id": row.upload_id,
                search_mod.PAGE_NUMBER_METADATA_KEY: 3.0}
    did = ai.document_id(row.user_id, row.session_id, row.upload_id)
    return Chunk(text=text, relevance=0.8, document_id=did, metadata=metadata, key=did)


def _matches(flt: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
    """The KB's own filtering, for the operators this module may send."""
    (op, operand), = flt.items()
    if op == "andAll":
        return all(_matches(f, metadata) for f in operand)
    if op == "orAll":
        return any(_matches(f, metadata) for f in operand)
    if op == "equals":
        return metadata.get(operand["key"]) == operand["value"]
    if op == "in":
        return metadata.get(operand["key"]) in operand["value"]
    raise AssertionError(f"unexpected operator {op}")


class FilteringKb:
    """A KB holding every user's chunks that, like the service, applies the filter."""

    def __init__(self, chunks: List[Chunk]) -> None:
        self.chunks = chunks
        self.filters: List[Dict[str, Any]] = []

    async def search(self, kb_ref, query, top_k, retrieval_filter):
        self.filters.append(retrieval_filter)
        return [c for c in self.chunks if _matches(retrieval_filter, c.metadata)][:top_k]


class Repo:
    def __init__(self, *rows: FileMetadata) -> None:
        self.rows = {(r.user_id, r.upload_id): r for r in rows}

    async def get_file(self, user_id, upload_id):
        return self.rows.get((user_id, upload_id))

    async def list_session_files(self, session_id, status=None):
        return [r for r in self.rows.values() if r.session_id == session_id]

    def list_session_files_sync(self, session_id, status=None):
        return [r for r in self.rows.values() if r.session_id == session_id]


@pytest.fixture
def world(monkeypatch):
    """The owner's root document and another user's, both indexed in the one KB."""
    mine = _row("up-mine")
    theirs = _row("up-theirs", user=OTHER, session="sess-theirs")
    repo = Repo(mine, theirs)
    kb = FilteringKb([_indexed(mine), _indexed(theirs, "their secret notice period")])
    for target in (
        "apis.shared.files.document_read.get_file_upload_repository",
        "apis.shared.files.workspace.get_file_upload_repository",
        "apis.shared.files.repository.get_file_upload_repository",
    ):
        monkeypatch.setattr(target, lambda: repo)
    monkeypatch.setattr("apis.shared.kb_backend.managed_backend.ManagedKbBackend", lambda: kb)
    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
    return SimpleNamespace(repo=repo, kb=kb, mine=mine, theirs=theirs)


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


def _query(session: str, user: str, ancestors, **kw) -> Dict[str, Any]:
    from agents.builtin_tools.document_read_tool import make_document_read_tool

    tool = make_document_read_tool(session, user, searchable=True, ancestor_session_ids=ancestors)
    result = asyncio.run(_call(tool, query="notice period", **kw))
    assert result["status"] == "success", result
    return result["content"][0]["json"]


# ---------------------------------------------------------------------------
# The bug, through the tool
# ---------------------------------------------------------------------------


class TestForkSearch:
    def test_a_fork_finds_its_roots_indexed_document(self, world):
        payload = _query(FORK, OWNER, ancestor_session_ids(FORK, FORK_ROW))
        assert [(r["upload_id"], r["page"]) for r in payload["results"]] == [("up-mine", 3)]
        assert "not_searchable" not in payload

    def test_a_fork_finds_it_by_upload_id(self, world):
        payload = _query(FORK, OWNER, ancestor_session_ids(FORK, FORK_ROW), upload_id="up-mine")
        assert [r["upload_id"] for r in payload["results"]] == ["up-mine"]

    def test_without_the_ancestors_the_fork_finds_nothing(self, world):
        """What a fork got before this fix: its own session holds no rows."""
        payload = _query(FORK, OWNER, ())
        assert payload["results"] == [] and payload["count"] == 0

    def test_the_root_itself_is_unchanged(self, world):
        payload = _query(ROOT, OWNER, ())
        assert [r["upload_id"] for r in payload["results"]] == ["up-mine"]
        assert world.kb.filters[-1] == attachment_filter(OWNER, ROOT)


class TestAnotherUsersFork:
    def test_a_fork_owned_by_someone_else_finds_nothing_of_the_owners(self, world):
        # Even handed the owner's session ids as ancestors, the owner clause is
        # never widened: the other user's search sees only their own rows.
        payload = _query(FORK, OTHER, ancestor_session_ids(FORK, FORK_ROW))
        assert payload["results"] == []
        assert all(r.get("upload_id") != "up-mine" for r in payload.get("not_searchable", []))
        assert all(f["andAll"][0] == {"equals": {"key": "user_id", "value": OTHER}} for f in world.kb.filters)

    def test_naming_the_owners_upload_id_is_not_found(self, world):
        from agents.builtin_tools.document_read_tool import make_document_read_tool

        tool = make_document_read_tool(FORK, OTHER, searchable=True, ancestor_session_ids=(PARENT, ROOT))
        result = asyncio.run(_call(tool, query="notice period", upload_id="up-mine"))
        assert result["status"] == "error"
        assert world.kb.filters == []

    def test_a_hit_from_outside_the_scope_is_dropped_even_if_the_kb_returns_it(self, world):
        # The filter should make this impossible; the post-check holds if it fails.
        class LeakyKb(FilteringKb):
            async def search(self, kb_ref, query, top_k, retrieval_filter):
                return list(self.chunks)

        stray = _row("up-stray", session="sess-elsewhere")
        world.repo.rows[(OWNER, "up-stray")] = stray
        hits = asyncio.run(search_mod.search_attachments(
            OWNER, FORK, "notice", ancestor_session_ids=(PARENT, ROOT),
            backend=LeakyKb([_indexed(world.mine), _indexed(stray), _indexed(world.theirs)]),
            repository=world.repo,
        ))
        assert [h.upload_id for h in hits] == ["up-mine"]


class TestFilter:
    def test_a_session_that_is_not_a_fork_keeps_its_single_equals(self):
        assert attachment_filter("u", "s", ancestor_session_ids=()) == attachment_filter("u", "s")
        assert attachment_filter("u", "s")["andAll"][1] == {"equals": {"key": "session_id", "value": "s"}}

    def test_a_fork_widens_only_the_session_clause(self):
        sent = attachment_filter("u", FORK, "up", ancestor_session_ids=(PARENT, ROOT, "", FORK))
        assert sent == {"andAll": [
            {"equals": {"key": "user_id", "value": "u"}},
            {"in": {"key": "session_id", "value": [FORK, PARENT, ROOT]}},
            {"equals": {"key": "upload_id", "value": "up"}},
        ]}

    def test_the_widened_filter_passes_the_isolation_check(self):
        from apis.shared.kb_backend.managed_backend import retrieval_configuration

        retrieval_configuration(5, attachment_filter("u", FORK, ancestor_session_ids=(PARENT, ROOT)))


# ---------------------------------------------------------------------------
# The planner's conversation set and the re-index on open
# ---------------------------------------------------------------------------


def test_the_planners_read_includes_the_ancestors_rows_for_the_caller_only(monkeypatch):
    from apis.inference_api.chat import routes

    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
    own = _row("up-own", session=FORK)
    root = _row("up-root")
    theirs = _row("up-theirs", user=OTHER)
    repo = Repo(own, root, theirs)
    threads: List[bool] = []
    sync = repo.list_session_files_sync

    def recording(session_id, status=None):
        threads.append(threading.current_thread() is threading.main_thread())
        return sync(session_id, status)

    repo.list_session_files_sync = recording
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)

    async def go():
        task = routes._start_session_files_read(
            FORK, OWNER, is_brief=False, is_resume=False, is_continuation=False,
            ancestor_session_ids=ancestor_session_ids(FORK, FORK_ROW),
        )
        return await task

    rows = asyncio.run(go())
    assert [r.upload_id for r in rows] == ["up-own", "up-root"]
    assert threads == [False, False, False]


def test_opening_a_fork_requeues_its_ancestors_evicted_documents(monkeypatch):
    from apis.app_api.files import attachment_reindex

    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
    monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "https://sqs.invalid/q")
    sent: List[tuple] = []
    monkeypatch.setattr(attachment_reindex, "_send_all", lambda url, user, ids: sent.append((user, ids)))
    repo = Repo(_row("up-root", kb="evicted"), _row("up-theirs", user=OTHER, kb="evicted"))

    async def meta(session_id, user_id):
        return SimpleNamespace(row=FORK_ROW, owned_by_other=False)

    monkeypatch.setattr("apis.shared.sessions.metadata.load_session_meta", meta)
    queued = asyncio.run(attachment_reindex.requeue_evicted_attachments(FORK, OWNER, repository=repo))
    assert queued == 1 and sent == [(OWNER, ["up-root"])]
