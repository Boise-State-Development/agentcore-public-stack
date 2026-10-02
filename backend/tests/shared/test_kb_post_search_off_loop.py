"""The knowledge-base search's follow-up reads stay off the event loop.

An agent turn starts its knowledge-base search before the agent build and
awaits it after (turn-path spec §5 P3b), and a cold build holds the event loop.
After #1411 two DynamoDB reads still ran on the loop once the search returned,
so they waited for the build to let go — ~120ms of residual `rag_wait` on dev:

- the document-status filter, which drops chunks from documents that are not
  `complete` (fail-closed), and
- `resolve_context_cap`'s knowledge-base record read.

The filter now runs on the same worker thread as the backend search, right
after it; the cap is fetched on a worker alongside the search. Neither changes
what reaches the model.
"""

import asyncio
import threading
from typing import Any, List
from unittest.mock import patch

import pytest

import apis.inference_api.chat.routes as routes
from apis.shared.assistants import rag_service
from apis.shared.assistants.kb_access import granted
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk

ASSISTANT_ID = "ast-offloop-001"
ACCESS = granted(ASSISTANT_ID, "user-1", "owner")


def _chunk(doc: str) -> Chunk:
    return Chunk(text=f"text from {doc}", relevance=0.9, document_id=doc, key=f"k-{doc}", metadata={"document_id": doc})


class _Backend:
    def __init__(self, chunks: List[Chunk]) -> None:
        self.chunks = chunks
        self.thread = None

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        self.thread = threading.current_thread()
        return list(self.chunks)


@pytest.mark.asyncio
async def test_the_status_filter_runs_on_the_search_thread_not_the_loop():
    backend = _Backend([_chunk("doc-1"), _chunk("doc-gone")])
    filter_threads: List[Any] = []

    def _filter(chunks, assistant_id):
        filter_threads.append(threading.current_thread())
        return [c for c in chunks if c.metadata["document_id"] != "doc-gone"]

    with patch.object(rag_service, "load_record", return_value=None), patch.object(
        rag_service, "resolve_backend", return_value=backend
    ), patch.object(rag_service, "_filter_chunks_by_document_status", side_effect=_filter), patch.object(
        rag_service, "emit_count"
    ):
        results = await rag_service.search_assistant_knowledgebase_with_formatting(
            ASSISTANT_ID, "q", 5, access=ACCESS
        )

    assert [r["metadata"]["document_id"] for r in results] == ["doc-1"]
    assert filter_threads == [backend.thread]
    assert backend.thread is not threading.current_thread()


@pytest.mark.asyncio
async def test_no_chunks_means_no_filter_read():
    backend = _Backend([])
    with patch.object(rag_service, "load_record", return_value=None), patch.object(
        rag_service, "resolve_backend", return_value=backend
    ), patch.object(
        rag_service, "_filter_chunks_by_document_status", side_effect=AssertionError("filtered nothing")
    ), patch.object(rag_service, "emit_count"):
        assert await rag_service.search_assistant_knowledgebase_with_formatting(
            ASSISTANT_ID, "q", 5, access=ACCESS
        ) == []


CHUNKS = [{"text": "Use a four-level scale.", "distance": 0.1, "metadata": {"document_id": "d1"}, "key": "k1"}]


@pytest.mark.asyncio
async def test_the_cap_is_read_on_a_worker_while_the_search_runs():
    cap_started = threading.Event()
    cap_threads: List[Any] = []

    def _cap(assistant_id):
        cap_threads.append(threading.current_thread())
        cap_started.set()
        return 8000

    async def _search(**kwargs):
        # Cannot finish until the cap read has begun: read after the search,
        # as before, it never would.
        assert await asyncio.to_thread(cap_started.wait, 2)
        return list(CHUNKS)

    with patch.object(rag_service, "resolve_context_cap", side_effect=_cap), patch.object(
        rag_service, "search_assistant_knowledgebase_with_formatting", side_effect=_search
    ):
        chunks, augmented = await routes._search_and_augment(
            assistant_id=ASSISTANT_ID, message="How many levels?", access=ACCESS
        )

    assert chunks == CHUNKS
    assert augmented == rag_service.augment_prompt_with_context(
        user_message="How many levels?", context_chunks=CHUNKS, max_context_length=8000
    )
    assert cap_threads and cap_threads[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_a_failed_cap_read_fails_open_like_before():
    async def _search(**kwargs):
        return list(CHUNKS)

    with patch.object(rag_service, "resolve_context_cap", side_effect=RuntimeError("ddb down")), patch.object(
        rag_service, "search_assistant_knowledgebase_with_formatting", side_effect=_search
    ):
        assert await routes._search_and_augment(
            assistant_id=ASSISTANT_ID, message="m", access=ACCESS
        ) == (None, "m")
