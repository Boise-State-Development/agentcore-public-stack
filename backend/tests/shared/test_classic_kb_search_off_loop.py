"""The classic (S3 Vectors) knowledge-base search runs off the event loop.

An agent turn now starts its knowledge-base search before the agent build and
awaits it after (turn-path spec §5 P3b), and a cold build holds the event loop
while it runs. If either round trip of the classic search — the query
embedding, then `QueryVectors` — were sent from the loop, it would wait for the
build instead of overlapping it. So both go out back to back on one worker
thread, on the process-cached `s3vectors` client.
"""

import io
import json
import threading

import pytest

from apis.shared.embeddings import bedrock_embeddings


class _Runtime:
    def __init__(self, threads):
        self.threads = threads

    def invoke_model(self, **kwargs):
        self.threads.append(("embed", threading.current_thread()))
        self.body = json.loads(kwargs["body"])
        return {"body": io.BytesIO(json.dumps({"embedding": [0.1, 0.2]}).encode())}


class _Vectors:
    def __init__(self, threads):
        self.threads = threads

    def query_vectors(self, **kwargs):
        self.threads.append(("query", threading.current_thread()))
        self.kwargs = kwargs
        return {"vectors": []}


@pytest.mark.asyncio
async def test_both_round_trips_run_on_one_worker_thread(monkeypatch):
    threads: list = []
    runtime, vectors = _Runtime(threads), _Vectors(threads)
    clients_asked: list = []
    monkeypatch.setattr(bedrock_embeddings, "_get_bedrock_runtime_client", lambda: runtime)
    monkeypatch.setattr(bedrock_embeddings, "_VECTOR_STORE_BUCKET_NAME", "bucket")
    monkeypatch.setattr(bedrock_embeddings, "_VECTOR_STORE_INDEX_NAME", "index")

    def _get_client():
        clients_asked.append("s3vectors")
        return vectors

    monkeypatch.setattr(bedrock_embeddings, "_get_s3vectors_client", _get_client)

    response = await bedrock_embeddings.search_assistant_knowledgebase("ast-1", "how do I register?")

    assert response == {"vectors": []}
    assert [step for step, _ in threads] == ["embed", "query"]
    assert threads[0][1] is threads[1][1]
    assert threads[0][1] is not threading.current_thread()
    assert clients_asked == ["s3vectors"]
    assert runtime.body == {"inputText": "how do I register?"}
    assert vectors.kwargs["queryVector"] == {"float32": [0.1, 0.2]}
    assert vectors.kwargs["filter"] == {"assistant_id": "ast-1"}
    assert vectors.kwargs["topK"] == 5
