"""Vector deletion runs off the event loop, and the full-index scan is opt-out.

Why: an agent deletion with 41 managed-KB documents once ran
``delete_vectors_for_document`` for each on the event loop. Every probe came
back empty (the vectors were never in S3 Vectors), every call fell through to
``list_vectors`` over the whole index (~5 s each), and app-api answered no
``/health`` for three and a half minutes — so ECS killed the task, three
times in eleven days on production.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

import apis.shared.embeddings.bedrock_embeddings as be

ENV = {
    "S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME": "b",
    "S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME": "i",
}


def _client(found_keys=(), listed_keys=()):
    client = MagicMock()
    client.get_vectors.return_value = {"vectors": [{"key": k} for k in found_keys]}
    client.list_vectors.return_value = {"vectors": [{"key": k} for k in listed_keys]}
    client.threads: set[int] = set()

    def record(*_a, **_k):
        client.threads.add(threading.get_ident())
        return {"vectors": []}

    client.get_vectors.side_effect = lambda *a, **k: (client.threads.add(threading.get_ident()),
                                                    {"vectors": [{"key": k2} for k2 in found_keys]})[1]
    client.list_vectors.side_effect = lambda *a, **k: (client.threads.add(threading.get_ident()),
                                                     {"vectors": [{"key": k2} for k2 in listed_keys]})[1]
    client.delete_vectors.side_effect = lambda *a, **k: (client.threads.add(threading.get_ident()), {})[1]
    return client


@pytest.fixture()
def patched():
    with patch.dict("os.environ", ENV), patch.object(be, "_get_vector_store_bucket", return_value="b"), patch.object(
        be, "_get_vector_store_index", return_value="i"
    ):
        yield


@pytest.mark.asyncio
async def test_every_boto3_call_happens_off_the_loop_thread(patched):
    loop_thread = threading.get_ident()
    client = _client(found_keys=["DOC-1#0"])
    with patch.object(be.boto3, "client", return_value=client):
        await be.delete_vectors_for_document("DOC-1")
        await be.delete_vectors_for_document_deterministic("DOC-1", 3)
        await be.delete_vector_tail("DOC-1", 1, 3)
        await be.delete_vectors_for_assistant("ast-1")
    assert client.threads, "the fake client was never called"
    assert loop_thread not in client.threads


@pytest.mark.asyncio
async def test_empty_probe_scans_the_index_by_default(patched):
    client = _client(found_keys=[], listed_keys=["DOC-1#0", "DOC-2#0"])
    with patch.object(be.boto3, "client", return_value=client):
        deleted = await be.delete_vectors_for_document("DOC-1")
    client.list_vectors.assert_called_once()
    assert deleted == 1
    client.delete_vectors.assert_called_once_with(vectorBucketName="b", indexName="i", keys=["DOC-1#0"])


@pytest.mark.asyncio
async def test_scan_index_false_never_lists_the_index(patched):
    client = _client(found_keys=[], listed_keys=["DOC-1#0"])
    with patch.object(be.boto3, "client", return_value=client):
        deleted = await be.delete_vectors_for_document("DOC-1", scan_index=False)
    client.get_vectors.assert_called_once()
    client.list_vectors.assert_not_called()
    client.delete_vectors.assert_not_called()
    assert deleted == 0


@pytest.mark.asyncio
async def test_scan_index_false_still_deletes_what_the_probe_finds(patched):
    """A knowledge base promoted to managed may still carry the S3 Vectors copy
    of a document ingested before the promotion; the probe keeps finding it."""
    client = _client(found_keys=["DOC-1#0", "DOC-1#1"])
    # The second probe batch must come back empty so the loop stops.
    client.get_vectors.side_effect = [{"vectors": [{"key": "DOC-1#0"}, {"key": "DOC-1#1"}]}, {"vectors": []}]
    with patch.object(be.boto3, "client", return_value=client):
        deleted = await be.delete_vectors_for_document("DOC-1", scan_index=False)
    assert deleted == 2
    client.list_vectors.assert_not_called()


@pytest.mark.asyncio
async def test_the_loop_stays_responsive_during_a_slow_scan(patched):
    """The point of the change: a scan that takes a while must not starve a
    concurrent coroutine (in production, ``/health``)."""
    barrier = threading.Event()

    def slow_list(**_kwargs):
        barrier.wait(timeout=5)
        return {"vectors": []}

    client = _client()
    client.list_vectors.side_effect = slow_list
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        barrier.set()

    with patch.object(be.boto3, "client", return_value=client):
        await asyncio.gather(be.delete_vectors_for_document("DOC-1"), heartbeat())
    assert ticks == 5
