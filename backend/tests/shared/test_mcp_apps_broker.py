"""Tests for the per-conversation app-tool event broker (MCP Apps PR #5)."""

import asyncio

import pytest

from apis.shared.mcp_apps.broker import (
    AppToolEventBroker,
    get_app_tool_event_broker,
)


def _ev(tag: str) -> dict:
    return {"type": "tool_use", "data": {"tag": tag}}


def test_singleton_accessor():
    assert get_app_tool_event_broker() is get_app_tool_event_broker()


def test_publish_to_active_subscriber_is_live():
    b = AppToolEventBroker()
    q = b.add_subscriber("s1")
    b.publish("s1", _ev("a"))
    b.publish("s1", _ev("b"))
    assert [e["data"]["tag"] for e in b.drain(q)] == ["a", "b"]
    assert b.drain(q) == []
    b.remove_subscriber("s1", q)


def test_publish_with_no_subscriber_buffers_then_flushes_on_subscribe():
    b = AppToolEventBroker()
    # No active stream — buffered.
    b.publish("s1", _ev("early"))
    q = b.add_subscriber("s1")
    # The next stream to open drains what it missed.
    assert [e["data"]["tag"] for e in b.drain(q)] == ["early"]
    b.remove_subscriber("s1", q)


def test_pending_ring_is_bounded():
    b = AppToolEventBroker()
    for i in range(150):
        b.publish("s1", _ev(str(i)))
    q = b.add_subscriber("s1")
    drained = b.drain(q)
    # Capped at 100, oldest dropped → tail retained.
    assert len(drained) == 100
    assert drained[0]["data"]["tag"] == "50"
    assert drained[-1]["data"]["tag"] == "149"


def test_sessions_are_isolated():
    b = AppToolEventBroker()
    qa = b.add_subscriber("a")
    qb = b.add_subscriber("b")
    b.publish("a", _ev("for-a"))
    assert [e["data"]["tag"] for e in b.drain(qa)] == ["for-a"]
    assert b.drain(qb) == []
    b.remove_subscriber("a", qa)
    b.remove_subscriber("b", qb)


def test_fan_out_to_multiple_active_subscribers():
    b = AppToolEventBroker()
    q1 = b.add_subscriber("s1")
    q2 = b.add_subscriber("s1")
    b.publish("s1", _ev("x"))
    assert b.drain(q1)[0]["data"]["tag"] == "x"
    assert b.drain(q2)[0]["data"]["tag"] == "x"
    b.remove_subscriber("s1", q1)
    b.remove_subscriber("s1", q2)


def test_remove_subscriber_prunes_session_then_buffers_again():
    b = AppToolEventBroker()
    q = b.add_subscriber("s1")
    b.remove_subscriber("s1", q)
    # With the subscriber gone the session falls back to buffering.
    b.publish("s1", _ev("after"))
    q2 = b.add_subscriber("s1")
    assert [e["data"]["tag"] for e in b.drain(q2)] == ["after"]
    b.remove_subscriber("s1", q2)


def test_publish_empty_session_is_noop():
    b = AppToolEventBroker()
    b.publish("", _ev("x"))  # must not raise


@pytest.mark.asyncio
async def test_subscribe_context_manager_pairs_add_remove():
    b = AppToolEventBroker()
    b.publish("s1", _ev("buffered"))
    async with b.subscribe("s1") as q:
        assert [e["data"]["tag"] for e in b.drain(q)] == ["buffered"]
        b.publish("s1", _ev("live"))
        assert [e["data"]["tag"] for e in b.drain(q)] == ["live"]
    # Context exit unsubscribed → back to buffering.
    b.publish("s1", _ev("after"))
    async with b.subscribe("s1") as q2:
        assert [e["data"]["tag"] for e in b.drain(q2)] == ["after"]
