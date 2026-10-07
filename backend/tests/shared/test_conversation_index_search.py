"""Isolation at the one function that queries the shared conversations KB.

``search_conversation_index`` builds the ``user_id equals`` filter itself and
takes none from its caller (spec §7, PR-4 isolation requirement 1), refuses an
empty user id before anything is sent (an empty filter value is a Bedrock
ValidationException), and drops any hit that does not name the caller.
"""

from typing import Any, Dict, List

import pytest

from apis.shared.conversation_search.index_search import conversation_filter, search_conversation_index
from apis.shared.kb_backend.managed_backend import validate_isolation_filter
from apis.shared.kb_backend.protocol import Chunk
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID


class RecordingBackend:
    def __init__(self, chunks: List[Chunk]):
        self.chunks = chunks
        self.calls: List[Dict[str, Any]] = []

    async def search(self, kb_ref, query, top_k=5, retrieval_filter=None, **_):
        self.calls.append({"kb_ref": kb_ref, "query": query, "top_k": top_k, "filter": retrieval_filter})
        return list(self.chunks)


def _chunk(doc_id: str, user_id: str, text: str = "t", score: float = 0.5, **extra) -> Chunk:
    """``doc_id`` is ``session#index``; the id is built as the index writes it,
    under ``user_id`` unless ``id_user`` says otherwise."""
    id_user = extra.pop("id_user", user_id)
    full_id = f"conv#{id_user}#{doc_id}"
    metadata = {"user_id": user_id, "created_at": "2026-10-07T17:09:45.716Z", **extra}
    return Chunk(text=text, relevance=score, document_id=full_id, metadata=metadata, key=full_id)


@pytest.mark.asyncio
async def test_every_query_carries_the_callers_user_filter():
    backend = RecordingBackend([])
    await search_conversation_index("user-a", "window function", backend=backend)
    assert backend.calls == [
        {
            "kb_ref": CONVERSATIONS_KB_ID,
            "query": "window function",
            "top_k": 20,
            "filter": {"equals": {"key": "user_id", "value": "user-a"}},
        }
    ]


@pytest.mark.asyncio
async def test_project_scope_is_and_all_with_the_user():
    backend = RecordingBackend([])
    await search_conversation_index("user-a", "q", project_id="proj-1", backend=backend)
    assert backend.calls[0]["filter"] == {
        "andAll": [
            {"equals": {"key": "user_id", "value": "user-a"}},
            {"equals": {"key": "project_id", "value": "proj-1"}},
        ]
    }


@pytest.mark.parametrize("project_id", [None, "proj-1"])
def test_filter_uses_only_exact_match_operators(project_id):
    validate_isolation_filter(conversation_filter("user-a", project_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", "   ", 42])
async def test_missing_or_empty_user_id_is_refused_before_any_call(user_id):
    backend = RecordingBackend([])
    with pytest.raises(ValueError):
        await search_conversation_index(user_id, "q", backend=backend)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_blank_query_sends_nothing():
    backend = RecordingBackend([])
    assert await search_conversation_index("user-a", "   ", backend=backend) == []
    assert backend.calls == []


def test_the_function_accepts_no_filter_argument():
    import inspect

    params = inspect.signature(search_conversation_index).parameters
    assert "retrieval_filter" not in params and "filter" not in params
    assert list(params)[0] == "user_id"


@pytest.mark.asyncio
async def test_hits_naming_another_user_or_no_user_are_dropped():
    backend = RecordingBackend(
        [
            _chunk("s1#2", "user-a", text="mine", score=0.9),
            _chunk("s2#0", "user-b", text="theirs", score=0.8),
            Chunk(text="ownerless", relevance=0.7, document_id="conv#user-a#s3#0", metadata={}, key="x"),
            Chunk(text="junk", relevance=0.6, document_id="not-a-conversation-doc", metadata={"user_id": "user-a"}, key="y"),
        ]
    )
    hits = await search_conversation_index("user-a", "q", backend=backend)
    assert [(h.session_id, h.message_index, h.text, h.score) for h in hits] == [("s1", 2, "mine", 0.9)]
    assert hits[0].created_at == "2026-10-07T17:09:45.716Z"


@pytest.mark.asyncio
async def test_project_scope_drops_hits_from_another_project():
    backend = RecordingBackend(
        [
            _chunk("s1#0", "user-a", project_id="proj-1"),
            _chunk("s2#0", "user-a", project_id="proj-2"),
            _chunk("s3#0", "user-a"),
        ]
    )
    hits = await search_conversation_index("user-a", "q", project_id="proj-1", backend=backend)
    assert [h.session_id for h in hits] == ["s1"]


@pytest.mark.asyncio
async def test_an_id_naming_another_user_is_dropped_even_with_the_callers_attribute():
    """The id and the attribute must both name the caller."""
    backend = RecordingBackend([_chunk("s9#0", "user-a", id_user="user-b", text="forged")])
    assert await search_conversation_index("user-a", "q", backend=backend) == []


@pytest.mark.asyncio
async def test_two_users_sharing_a_session_id_each_get_only_their_own_turn():
    # Session ids are not unique across users (found on dev, 2026-10-07).
    backend = RecordingBackend([_chunk("shared#0", "user-a", text="a's turn"), _chunk("shared#0", "user-b", text="b's turn")])
    hits = await search_conversation_index("user-a", "q", backend=backend)
    assert [(h.session_id, h.text) for h in hits] == [("shared", "a's turn")]


@pytest.mark.asyncio
async def test_a_legacy_two_part_id_is_never_a_hit():
    backend = RecordingBackend(
        [Chunk(text="old", relevance=0.9, document_id="conv#s1#2", metadata={"user_id": "user-a"}, key="conv#s1#2")]
    )
    assert await search_conversation_index("user-a", "q", backend=backend) == []
