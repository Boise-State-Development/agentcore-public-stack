"""Tests for app-pushed model context dispatch (MCP Apps PR #6).

Uses a fake that faithfully mimics strands 1.40 `AgentState`: `.get()`
returns a deep copy (so the read-modify-write path is genuinely
exercised) and `.set()` enforces JSON-serializability. No live agent.
"""

import copy
import json

import pytest

from apis.inference_api.chat.app_context_dispatch import (
    STATE_KEY,
    AppContextUpdateError,
    dispatch_app_context_update,
    merge_and_clear_pending_context,
)


class _FakeState:
    """Mimics strands.agent.state.AgentState get/set semantics."""

    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key=None):
        if key is None:
            return copy.deepcopy(self._data)
        return copy.deepcopy(self._data.get(key))

    def set(self, key: str, value) -> None:
        json.dumps(value)  # raises TypeError/ValueError if not serializable
        self._data[key] = copy.deepcopy(value)


class _FakeStrands:
    def __init__(self) -> None:
        self.state = _FakeState()


class _FakeAgent:
    """BaseAgent wrapper — inner Strands agent is `.agent`."""

    def __init__(self) -> None:
        self.agent = _FakeStrands()


def test_dispatch_writes_under_resource_uri_and_acks():
    agent = _FakeAgent()
    ack = dispatch_app_context_update(
        agent,
        resource_uri="ui://srv/widget",
        content=[{"type": "text", "text": "hello"}],
        structured_content={"count": 2},
    )
    assert ack == {
        "resourceUri": "ui://srv/widget",
        "status": "stored",
        "pending": 1,
    }
    bag = agent.agent.state.get(STATE_KEY)
    entry = bag["context"]["ui://srv/widget"]
    assert entry["content"] == [{"type": "text", "text": "hello"}]
    assert entry["structuredContent"] == {"count": 2}
    assert "updatedAt" in entry


def test_last_write_wins_per_resource_uri():
    agent = _FakeAgent()
    dispatch_app_context_update(
        agent, resource_uri="ui://a", content=None, structured_content={"v": 1}
    )
    ack = dispatch_app_context_update(
        agent, resource_uri="ui://a", content=None, structured_content={"v": 2}
    )
    assert ack["pending"] == 1  # same uri overwrote, not appended
    bag = agent.agent.state.get(STATE_KEY)
    assert bag["context"]["ui://a"]["structuredContent"] == {"v": 2}


def test_requires_content_or_structured():
    with pytest.raises(AppContextUpdateError) as ei:
        dispatch_app_context_update(
            _FakeAgent(), resource_uri="ui://a", content=None, structured_content=None
        )
    assert ei.value.code == 400


def test_missing_agent_state_is_409():
    class _NoState:
        agent = None

    with pytest.raises(AppContextUpdateError) as ei:
        dispatch_app_context_update(
            _NoState(), resource_uri="ui://a", content=None,
            structured_content={"x": 1},
        )
    assert ei.value.code == 409


def test_merge_drains_clears_and_dedupes_by_uri():
    agent = _FakeAgent()
    dispatch_app_context_update(
        agent, resource_uri="ui://a", content=None,
        structured_content={"a": 1},
    )
    dispatch_app_context_update(
        agent,
        resource_uri="ui://b",
        content=[{"type": "text", "text": "note-b"}],
        structured_content=None,
    )

    block = merge_and_clear_pending_context(agent)
    assert block is not None
    assert "<mcp_app_context>" in block and "</mcp_app_context>" in block
    assert 'resource="ui://a"' in block
    assert 'resource="ui://b"' in block
    assert "note-b" in block
    assert '"a": 1' in block

    # Cleared: a second merge with no new updates yields nothing.
    assert merge_and_clear_pending_context(agent) is None


def test_merge_empty_returns_none():
    assert merge_and_clear_pending_context(_FakeAgent()) is None


def test_merge_never_raises_on_bad_agent():
    class _Broken:
        agent = None

    # _strands_agent would raise AppContextUpdateError(409); merge swallows
    # it (context is best-effort and must never break a turn).
    assert merge_and_clear_pending_context(_Broken()) is None
