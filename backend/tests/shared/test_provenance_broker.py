"""The hand-off from ``verify_sources`` to the SSE stream.

A tool cannot write to the stream, and the rich verdict must not go into the
tool result (it would land in the cacheable prefix), so it is parked here. The
isolation property is the one worth testing: these payloads carry text from
someone's document.
"""

from __future__ import annotations

from apis.shared.files import provenance_broker as broker


def _payload(tool_use_id: str = "tu1") -> dict:
    return {"toolUseId": tool_use_id, "claims": [{"id": "x", "state": "supported"}]}


def setup_function():
    broker._pending.clear()


def test_queue_then_take_round_trips_in_order():
    broker.queue_provenance("u1", "s1", _payload("tu1"))
    broker.queue_provenance("u1", "s1", _payload("tu2"))
    assert [p["toolUseId"] for p in broker.take_provenance("u1", "s1")] == ["tu1", "tu2"]


def test_take_drains_so_a_payload_is_never_emitted_twice():
    broker.queue_provenance("u1", "s1", _payload())
    assert len(broker.take_provenance("u1", "s1")) == 1
    assert broker.take_provenance("u1", "s1") == []


def test_the_queue_is_keyed_on_user_and_session_not_session_alone():
    """A session id alone does not name a conversation (CLAUDE.md). Keyed on
    the session, two users sharing an id would drain each other's quoted
    document text."""
    broker.queue_provenance("owner", "s1", _payload("owners"))
    broker.queue_provenance("other", "s1", _payload("others"))
    assert [p["toolUseId"] for p in broker.take_provenance("other", "s1")] == ["others"]
    assert [p["toolUseId"] for p in broker.take_provenance("owner", "s1")] == ["owners"]


def test_an_unknown_conversation_takes_nothing():
    assert broker.take_provenance("u1", "s1") == []


def test_missing_identity_is_a_noop_both_ways():
    broker.queue_provenance("", "s1", _payload())
    broker.queue_provenance("u1", "", _payload())
    assert broker._pending == {}
    assert broker.take_provenance("", "s1") == []
    assert broker.take_provenance("u1", "") == []


def test_an_empty_payload_is_not_queued():
    broker.queue_provenance("u1", "s1", {})
    assert broker.take_provenance("u1", "s1") == []


def test_the_queue_is_bounded_and_drops_the_oldest():
    """A model calling the tool in a loop with nothing draining must not grow
    this without limit; the newest verdicts are the ones still on screen, and
    every one of them is persisted regardless."""
    for i in range(broker._MAX_PENDING_PER_KEY + 5):
        broker.queue_provenance("u1", "s1", _payload(f"tu{i}"))
    queued = broker.take_provenance("u1", "s1")
    assert len(queued) == broker._MAX_PENDING_PER_KEY
    assert queued[-1]["toolUseId"] == f"tu{broker._MAX_PENDING_PER_KEY + 4}"


def test_discard_leaves_no_residue_for_the_next_turn():
    broker.queue_provenance("u1", "s1", _payload())
    broker.discard_provenance("u1", "s1")
    assert broker.take_provenance("u1", "s1") == []
    # Idempotent: the stream's `finally` runs on every path.
    broker.discard_provenance("u1", "s1")
