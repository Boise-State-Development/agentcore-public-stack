"""Semantic search over large attachments (large-document spec §4F, PR-4).

Pins:

- isolation: the filter is built from the caller's identity alone, and a hit is
  dropped unless its id, its attributes and its file row all name the caller and
  the conversation;
- results: PDF page from ``_excerpt_page_number`` (a float), text capped;
- ``search_status``: one answer per row, read by the tool and the SPA chip;
- ``document_read``'s query mode: not-yet-searchable documents come back as a
  status and a pattern suggestion, never an error, and a failing knowledge base
  degrades the same way;
- the tool spec: unchanged without the KB, ``query`` added with it;
- ``GET /files/{id}/search-status``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from apis.shared.files import attachment_index as ai
from apis.shared.files import attachment_search as search_mod
from apis.shared.files import document_read as dr
from apis.shared.files import large_document
from apis.shared.files.attachment_search import attachment_filter, search_attachments
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.kb_backend.protocol import Chunk
from apis.shared.files.workspace import WorkspaceFileNotFoundError, WorkspaceValidationError

MB = 1024 * 1024
USER = "user-1"
SESSION = "sess-1"


def _row(upload_id: str = "up-1", *, size: int = 38 * MB, prep: Optional[str] = "ready", kb: Optional[str] = "indexed",
         user: str = USER, session: str = SESSION, mime: str = "application/pdf", filename: str = "manual.pdf",
         status: FileStatus = FileStatus.READY) -> FileMetadata:
    if prep == "ready":
        preparation = large_document.preparation_record(status="ready", fmt="pdf", page_count=10, shard_pages=10)
    elif prep is None:
        preparation = None
    else:
        preparation = large_document.preparation_record(status=prep)
    return FileMetadata(
        upload_id=upload_id, user_id=user, session_id=session, filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/{user}/{session}/{upload_id}/{filename}", s3_bucket="b",
        status=status, preparation=preparation, kb_index=ai.kb_index_record(kb) if kb else None,
    )


def _chunk(upload_id: str = "up-1", *, user: str = USER, session: str = SESSION, text: str = "the answer",
           page: Any = 4.0, attrs: Optional[Dict[str, Any]] = None, doc_id: Optional[str] = None) -> Chunk:
    metadata = {"user_id": user, "session_id": session, "upload_id": upload_id}
    if page is not None:
        metadata[search_mod.PAGE_NUMBER_METADATA_KEY] = page
    metadata.update(attrs or {})
    did = doc_id if doc_id is not None else ai.document_id(user, session, upload_id)
    return Chunk(text=text, relevance=0.8, document_id=did, metadata=metadata, key=did)


class FakeBackend:
    def __init__(self, chunks: List[Chunk]) -> None:
        self.chunks = chunks
        self.calls: List[tuple] = []

    async def search(self, kb_ref, query, top_k, retrieval_filter):
        self.calls.append((kb_ref, query, top_k, retrieval_filter))
        return list(self.chunks)


class FakeRepo:
    def __init__(self, *rows: FileMetadata) -> None:
        self.rows = {(r.user_id, r.upload_id): r for r in rows}
        self.gets: List[tuple] = []

    async def get_file(self, user_id, upload_id):
        self.gets.append((user_id, upload_id))
        return self.rows.get((user_id, upload_id))

    async def list_session_files(self, session_id, status=None):
        return [r for r in self.rows.values() if r.session_id == session_id]


def _search(chunks, repo=None, **kw):
    backend = FakeBackend(chunks)
    hits = asyncio.run(search_attachments(
        kw.pop("user", USER), kw.pop("session", SESSION), kw.pop("query", "what is the answer"),
        backend=backend, repository=repo or FakeRepo(_row()), **kw,
    ))
    return hits, backend


# ---------------------------------------------------------------------------
# attachment_search: isolation
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_the_filter_names_the_caller_and_the_conversation_exactly(self):
        assert attachment_filter("u", "s") == {"andAll": [
            {"equals": {"key": "user_id", "value": "u"}},
            {"equals": {"key": "session_id", "value": "s"}},
        ]}
        assert attachment_filter("u", "s", "up")["andAll"][2] == {"equals": {"key": "upload_id", "value": "up"}}

    def test_the_filter_sent_is_built_here_and_targets_the_attachments_kb(self):
        _, backend = _search([], upload_id="up-1")
        ((kb_ref, query, top_k, sent),) = backend.calls
        assert kb_ref == "attachments" and query == "what is the answer" and top_k == 5
        assert sent == attachment_filter(USER, SESSION, "up-1")

    @pytest.mark.parametrize("user,session", [("", SESSION), (USER, ""), ("  ", SESSION), (None, SESSION)])
    def test_a_missing_identity_raises_before_anything_is_sent(self, user, session):
        backend = FakeBackend([_chunk()])
        with pytest.raises(ValueError):
            asyncio.run(search_attachments(user, session, "q", backend=backend, repository=FakeRepo()))
        assert backend.calls == []

    def test_an_empty_query_sends_nothing(self):
        hits, backend = _search([_chunk()], query="   ")
        assert hits == [] and backend.calls == []

    @pytest.mark.parametrize("chunk", [
        _chunk(user="user-2"),                                      # id and attrs name someone else
        _chunk(session="sess-2"),                                   # another conversation
        _chunk(attrs={"user_id": "user-2"}),                        # attribute disagrees with the id
        _chunk(attrs={"session_id": "sess-2"}),
        _chunk(attrs={"upload_id": "up-9"}),
        _chunk(attrs={"user_id": None}),                            # no owner attribute
        _chunk(doc_id=f"att#user-2#{SESSION}#up-1"),                # id disagrees with the attributes
        _chunk(doc_id="conv#user-1#sess-1#3"),                      # not an attachment id at all
        _chunk(doc_id=""),
    ])
    def test_a_hit_that_does_not_name_the_caller_and_conversation_is_dropped(self, chunk):
        hits, _ = _search([chunk])
        assert hits == []

    def test_a_hit_for_another_document_is_dropped_when_one_is_named(self):
        repo = FakeRepo(_row("up-1"), _row("up-2"))
        hits, _ = _search([_chunk("up-2")], repo, upload_id="up-1")
        assert hits == []

    @pytest.mark.parametrize("row", [
        None,                                                       # deleted; delete event not landed yet
        _row(session="sess-2"),                                     # row moved/belongs elsewhere
        _row(status=FileStatus.FAILED),
    ])
    def test_a_hit_without_a_ready_row_in_this_conversation_is_dropped(self, row):
        repo = FakeRepo(*([row] if row else []))
        hits, _ = _search([_chunk()], repo)
        assert hits == []

    def test_rows_are_read_under_the_caller_once_per_document(self):
        repo = FakeRepo(_row("up-1"), _row("up-2", filename="b.pdf"))
        hits, _ = _search([_chunk("up-1"), _chunk("up-2"), _chunk("up-1", text="again")], repo)
        assert [h.upload_id for h in hits] == ["up-1", "up-2", "up-1"]
        assert sorted(repo.gets) == [(USER, "up-1"), (USER, "up-2")]


class TestResults:
    def test_page_is_the_excerpt_page_as_an_integer(self):
        (hit,), _ = _search([_chunk(page=4.0)])
        assert hit.page == 4 and hit.filename == "manual.pdf" and hit.score == 0.8

    @pytest.mark.parametrize("page", [None, "x", 0, -1.0])
    def test_no_usable_page_is_none(self, page):
        (hit,), _ = _search([_chunk(page=page)])
        assert hit.page is None

    def test_text_is_capped(self):
        (hit,), _ = _search([_chunk(text="x" * 2500)])
        assert len(hit.text) == search_mod.MAX_CHUNK_CHARS and hit.truncated


# ---------------------------------------------------------------------------
# search_status
# ---------------------------------------------------------------------------


class TestSearchStatus:
    @pytest.mark.parametrize("row,expected", [
        (_row(kb="indexed"), "searchable"),
        (_row(kb="indexing"), "indexing"),
        (_row(kb="queued"), "indexing"),
        (_row(kb=None), "indexing"),                     # prepared; the same invocation ingests next
        (_row(prep="pending", kb=None), "preparing"),
        (_row(prep="failed", kb=None), "unavailable"),
        (_row(prep=None, kb=None), "unavailable"),       # never queued (flag was off at upload)
        (_row(kb="evicted"), "evicted"),
        (_row(kb="failed"), "unavailable"),
        (_row(kb="skipped_cap"), "unavailable"),
        (_row(kb="removed"), "unavailable"),
        (_row(size=1 * MB, kb=None), "off"),             # inline-class: never indexed
        (_row(mime="text/csv", filename="d.csv"), "off"),
    ])
    def test_status_by_row(self, row, expected):
        assert ai.search_status(row, kb_enabled=True) == expected

    def test_off_without_the_knowledge_base(self):
        assert ai.search_status(_row(), kb_enabled=False) == "off"
        assert ai.search_status(None, kb_enabled=True) == "off"


# ---------------------------------------------------------------------------
# document_read query mode
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(monkeypatch):
    holder = SimpleNamespace(repo=FakeRepo())
    monkeypatch.setattr(dr, "get_file_upload_repository", lambda: holder.repo)
    monkeypatch.setattr("apis.shared.files.workspace.get_file_upload_repository", lambda: holder.repo)
    return holder


def _query(**kw) -> Dict[str, Any]:
    return asyncio.run(dr.search_documents(USER, SESSION, kw.pop("query", "notice period"), **kw))


class TestQueryMode:
    def test_session_wide_search_returns_excerpts_with_pages_and_a_page_range_hint(self, repo, monkeypatch):
        repo.repo = FakeRepo(_row("up-1"))
        search = AsyncMock(return_value=[search_mod.AttachmentHit("up-1", "manual.pdf", "30 days", 12, 0.9, False)])
        monkeypatch.setattr(search_mod, "search_attachments", search)
        result = _query()
        search.assert_awaited_once_with(USER, SESSION, "notice period", upload_id=None)
        assert result.mode == "query" and result.hits == 1 and result.bytes_returned == len("30 days")
        assert result.payload["results"] == [{"upload_id": "up-1", "filename": "manual.pdf", "page": 12, "text": "30 days"}]
        assert "page_range" in result.payload["hint"]

    def test_documents_not_searchable_are_listed_with_their_status_alongside_hits(self, repo, monkeypatch):
        repo.repo = FakeRepo(_row("up-1"), _row("up-2", kb="indexing", filename="b.pdf"))
        monkeypatch.setattr(search_mod, "search_attachments", AsyncMock(return_value=[]))
        payload = _query().payload
        assert [(e["upload_id"], e["search_status"]) for e in payload["not_searchable"]] == [("up-2", "indexing")]
        assert payload["count"] == 0 and "pattern" in payload["hint"]

    @pytest.mark.parametrize("kb,status", [("indexing", "indexing"), ("evicted", "evicted"), ("skipped_cap", "unavailable")])
    def test_a_named_document_not_yet_searchable_is_a_status_not_an_error(self, repo, monkeypatch, kb, status):
        repo.repo = FakeRepo(_row("up-1", kb=kb))
        search = AsyncMock()
        monkeypatch.setattr(search_mod, "search_attachments", search)
        payload = _query(upload_id="up-1").payload
        search.assert_not_awaited()
        assert payload["not_searchable"][0]["search_status"] == status
        assert payload["results"] == [] and "pattern" in payload["hint"] and "page_range" in payload["hint"]

    def test_without_the_knowledge_base_nothing_is_searched(self, repo, monkeypatch):
        repo.repo = FakeRepo(_row("up-1"))
        search = AsyncMock()
        monkeypatch.setattr(search_mod, "search_attachments", search)
        payload = asyncio.run(dr.search_documents(USER, SESSION, "q", kb_enabled=False)).payload
        search.assert_not_awaited()
        assert payload["not_searchable"][0]["search_status"] == "off"

    def test_a_failing_knowledge_base_degrades_to_pattern(self, repo, monkeypatch):
        repo.repo = FakeRepo(_row("up-1"))
        monkeypatch.setattr(search_mod, "search_attachments", AsyncMock(side_effect=RuntimeError("throttled")))
        payload = _query().payload
        assert payload["search_error"] and payload["results"] == [] and "pattern" in payload["hint"]
        assert "throttled" not in json.dumps(payload)

    def test_another_users_or_conversations_document_is_not_found(self, repo):
        repo.repo = FakeRepo(_row("up-1", session="sess-2"), _row("up-2", user="user-2"))
        with pytest.raises(WorkspaceFileNotFoundError):
            _query(upload_id="up-1")
        with pytest.raises(WorkspaceFileNotFoundError):
            _query(upload_id="up-2")

    def test_an_empty_query_is_a_validation_error(self, repo):
        with pytest.raises(WorkspaceValidationError):
            _query(query="  ")

    def test_a_conversation_with_no_documents_says_so(self, repo):
        payload = _query().payload
        assert payload["count"] == 0 and "no documents" in payload["hint"]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

TOOL_MODULE = "agents.builtin_tools.document_read_tool"

#: sha256 of the sorted-keys JSON of the spec before the search mode existed.
#: A deployment without the KB must keep exactly this spec, or every live
#: session re-writes its cached prefix (CLAUDE.md, prompt-cache contract).
SPEC_WITHOUT_SEARCH_SHA256 = "3d0786d1e3d2eee17adea77a97f0e49f0bc924ceaa54bfdc8c9eb59659a9114a"


def _spec_hash(tool) -> str:
    return hashlib.sha256(json.dumps(tool.tool_spec, sort_keys=True).encode()).hexdigest()


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


class TestTool:
    def test_without_the_knowledge_base_the_spec_is_unchanged(self, monkeypatch):
        from agents.builtin_tools.document_read_tool import make_document_read_tool

        monkeypatch.delenv("ATTACHMENT_KB_ENABLED", raising=False)
        tool = make_document_read_tool("s1", "u1")
        assert "query" not in tool.tool_spec["inputSchema"]["json"]["properties"]
        assert _spec_hash(tool) == SPEC_WITHOUT_SEARCH_SHA256

    def test_with_the_knowledge_base_query_is_added_and_the_spec_is_session_independent(self, monkeypatch):
        from agents.builtin_tools.document_read_tool import make_document_read_tool

        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
        a, b = make_document_read_tool("s1", "u1"), make_document_read_tool("s2", "u2", text_only=True)
        assert "query" in a.tool_spec["inputSchema"]["json"]["properties"]
        assert a.tool_spec == b.tool_spec
        assert a.tool_name == "document_read"

    def test_query_routes_to_search_with_the_closures_identity(self, monkeypatch):
        from agents.builtin_tools.document_read_tool import make_document_read_tool

        search = AsyncMock(return_value=dr.DocumentReadResult(mode="query", payload={"results": []}))
        read = AsyncMock()
        monkeypatch.setattr(f"{TOOL_MODULE}.search_documents", search)
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", read)
        tool = make_document_read_tool("s1", "u1", searchable=True)
        result = asyncio.run(_call(tool, query="notice period", upload_id="up-1", pattern="x"))
        search.assert_awaited_once_with("u1", "s1", "notice period", upload_id="up-1", kb_enabled=True)
        read.assert_not_awaited()
        assert result["status"] == "success"

    def test_without_query_the_searchable_tool_reads_as_before(self, monkeypatch):
        from agents.builtin_tools.document_read_tool import make_document_read_tool

        read = AsyncMock(return_value=dr.DocumentReadResult(mode="index", payload={}))
        monkeypatch.setattr(f"{TOOL_MODULE}.read_document", read)
        asyncio.run(_call(make_document_read_tool("s1", "u1", searchable=True), upload_id="up-1", query=" "))
        read.assert_awaited_once()

    def test_query_metric_carries_the_hit_count(self, monkeypatch):
        from agents.builtin_tools.document_read_tool import record_document_read

        calls = []
        monkeypatch.setattr(
            "apis.shared.observability.emf.emit_emf_metrics",
            lambda ns, metrics, properties=None, units=None: calls.append(properties),
        )
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", lambda: True)
        record_document_read(dr.DocumentReadResult(mode="query", payload={"query": "secret"}, hits=3, bytes_returned=40))
        assert calls == [{"mode": "query", "format": None, "hit_count": 3}]


# ---------------------------------------------------------------------------
# GET /files/{id}/search-status
# ---------------------------------------------------------------------------


class TestSearchStatusRoute:
    def _call(self, row):
        from apis.app_api.files import routes

        service = SimpleNamespace(get_file=AsyncMock(return_value=row))
        return asyncio.run(routes.get_search_status("up-1", user=SimpleNamespace(user_id=USER), service=service)), service

    def test_reads_the_callers_row_and_reports_its_status(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
        response, service = self._call(_row(kb="indexing"))
        service.get_file.assert_awaited_once_with(USER, "up-1")
        assert response.model_dump(by_alias=True) == {"uploadId": "up-1", "status": "indexing"}

    def test_off_without_the_knowledge_base(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "")
        response, _ = self._call(_row())
        assert response.status == "off"

    def test_not_found_is_404(self):
        with pytest.raises(HTTPException) as exc:
            self._call(None)
        assert exc.value.status_code == 404
