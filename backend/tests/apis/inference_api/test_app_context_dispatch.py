"""Tests for app-pushed model context dispatch (MCP Apps PR #6).

The pending context is held per `(session, user)`, off any agent instance.
It used to live on the `agent.state` of whatever agent the dispatch got, and
the dispatch reads the agent cache with a key the real turn's rarely
matches, so the context landed on a throwaway agent and never reached the
model (dev, 2026-10-18: GitHub's issue_write App created an issue and the
next turn said it had not).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import app_context_dispatch, routes
from apis.inference_api.chat.app_context_dispatch import (
    AppContextUpdateError,
    PendingAppContextStore,
    dispatch_app_context_update,
    merge_and_clear_pending_context,
)
from apis.inference_api.chat.models import InvocationRequest


@pytest.fixture(autouse=True)
def _empty_store():
    app_context_dispatch.get_pending_app_context_store().clear()
    yield
    app_context_dispatch.get_pending_app_context_store().clear()


def _push(resource_uri="ui://srv/widget", *, session="s-1", user="u-1", **kwargs):
    kwargs.setdefault("content", None)
    kwargs.setdefault("structured_content", None)
    return dispatch_app_context_update(
        session_id=session, user_id=user, resource_uri=resource_uri, **kwargs
    )


def test_dispatch_stores_and_acks():
    ack = _push(
        content=[{"type": "text", "text": "hello"}],
        structured_content={"count": 2},
    )
    assert ack == {"resourceUri": "ui://srv/widget", "status": "stored", "pending": 1}

    block = merge_and_clear_pending_context("s-1", "u-1")
    assert 'resource="ui://srv/widget"' in block
    assert "hello" in block
    assert '"count": 2' in block


def test_last_write_wins_per_resource_uri():
    _push("ui://a", structured_content={"v": 1})
    ack = _push("ui://a", structured_content={"v": 2})
    assert ack["pending"] == 1  # same uri overwrote, not appended

    block = merge_and_clear_pending_context("s-1", "u-1")
    assert '"v": 2' in block
    assert '"v": 1' not in block


def test_requires_content_or_structured():
    with pytest.raises(AppContextUpdateError) as ei:
        _push("ui://a")
    assert ei.value.code == 400


def test_unserializable_payload_is_400():
    with pytest.raises(AppContextUpdateError) as ei:
        _push("ui://a", structured_content={"x": object()})
    assert ei.value.code == 400
    assert merge_and_clear_pending_context("s-1", "u-1") is None


def test_stored_entry_is_decoupled_from_the_callers_payload():
    payload = {"status": "open"}
    _push("ui://a", structured_content=payload)
    payload["status"] = "mutated after the push"

    assert '"status": "open"' in merge_and_clear_pending_context("s-1", "u-1")


def test_merge_drains_clears_and_dedupes_by_uri():
    _push("ui://a", structured_content={"a": 1})
    _push("ui://b", content=[{"type": "text", "text": "note-b"}])

    block = merge_and_clear_pending_context("s-1", "u-1")
    assert block.startswith("<mcp_app_context>") and block.endswith("</mcp_app_context>")
    assert 'resource="ui://a"' in block
    assert 'resource="ui://b"' in block
    assert "note-b" in block
    assert '"a": 1' in block
    # Rendered in push order, so the same pushes always produce the same bytes.
    assert block.index('resource="ui://a"') < block.index('resource="ui://b"')

    # Cleared: a second merge with no new updates yields nothing.
    assert merge_and_clear_pending_context("s-1", "u-1") is None


def test_merge_empty_returns_none():
    assert merge_and_clear_pending_context("s-1", "u-1") is None


def test_context_never_crosses_users_on_one_session_id():
    """A session id alone does not name a conversation; `(session, user)` does."""
    _push("ui://a", session="shared", user="owner", structured_content={"secret": 1})

    assert merge_and_clear_pending_context("shared", "intruder") is None
    assert '"secret": 1' in merge_and_clear_pending_context("shared", "owner")


def test_context_never_crosses_sessions_for_one_user():
    _push("ui://a", session="s-1", structured_content={"x": 1})
    assert merge_and_clear_pending_context("s-2", "u-1") is None
    assert merge_and_clear_pending_context("s-1", "u-1") is not None


def test_merge_never_raises_into_the_turn():
    with patch.object(
        app_context_dispatch._store, "drain", side_effect=RuntimeError("boom")
    ):
        assert merge_and_clear_pending_context("s-1", "u-1") is None


class TestPendingAppContextStore:
    def test_expired_updates_are_dropped(self):
        now = [0.0]
        store = PendingAppContextStore(ttl_seconds=10, clock=lambda: now[0])
        store.put(("s", "u"), "ui://a", {"v": 1})
        now[0] = 11.0
        assert store.drain(("s", "u")) is None

    def test_a_late_write_does_not_revive_expired_updates(self):
        now = [0.0]
        store = PendingAppContextStore(ttl_seconds=10, clock=lambda: now[0])
        store.put(("s", "u"), "ui://a", {"v": 1})
        now[0] = 11.0
        assert store.put(("s", "u"), "ui://b", {"v": 2}) == 1
        assert list(store.drain(("s", "u"))) == ["ui://b"]

    def test_least_recently_written_conversation_is_evicted(self):
        store = PendingAppContextStore(max_conversations=2)
        store.put(("s1", "u"), "ui://a", {})
        store.put(("s2", "u"), "ui://a", {})
        store.put(("s1", "u"), "ui://b", {})  # s1 is now the most recent
        store.put(("s3", "u"), "ui://a", {})
        assert store.drain(("s2", "u")) is None
        assert store.drain(("s1", "u")) is not None
        assert store.drain(("s3", "u")) is not None

    def test_resources_per_conversation_are_bounded(self):
        store = PendingAppContextStore(max_resources=2)
        for uri in ("ui://a", "ui://b", "ui://c"):
            pending = store.put(("s", "u"), uri, {})
        assert pending == 2
        assert list(store.drain(("s", "u"))) == ["ui://b", "ui://c"]


def test_turn_prompt_carries_pending_context_ahead_of_the_message():
    _push("ui://a", structured_content={"issue": 2})

    prompt = routes._with_pending_app_context("did it work?", "s-1", "u-1")
    assert prompt.startswith("<mcp_app_context>")
    assert prompt.endswith("\n\ndid it work?")
    assert '"issue": 2' in prompt
    # Drained: the next turn's message goes through untouched.
    assert routes._with_pending_app_context("next", "s-1", "u-1") == "next"


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, raw_token="t", email=None, roles=[])


@pytest.mark.asyncio
async def test_pushed_context_reaches_the_next_turn_without_any_agent():
    """The dev regression, end to end through the route.

    The dispatch used to rebuild the conversation agent with
    `cache_write=False`; its key missed the real turn's slot, so the context
    went onto a throwaway instance. Now the push builds no agent at all (a
    tripwire `get_agent`), and the next turn of the same conversation gets it.
    """
    snapshot = SimpleNamespace(row=None, owned_by_other=False)
    request = InvocationRequest(
        session_id="conv-1",
        enabled_tools=["github"],
        model_id="some-model",
        app_context_update={
            "resource_uri": "ui://github-mcp-server/issue-write",
            "structured_content": {"number": 2, "state": "open"},
            "content": [{"type": "text", "text": "Created issue #2"}],
        },
    )
    tripwire = AsyncMock(side_effect=AssertionError("a context push must not build an agent"))
    with patch.object(routes, "load_session_meta", new=AsyncMock(return_value=snapshot)), \
         patch.object(routes, "resolve_effective_model", new=AsyncMock(return_value=None)), \
         patch.object(routes, "get_agent", new=tripwire):
        response = await routes.invocations(request, current_user=_user("u-1"))

    assert response.status_code == 200
    tripwire.assert_not_called()

    prompt = routes._with_pending_app_context("was the issue created?", "conv-1", "u-1")
    assert 'resource="ui://github-mcp-server/issue-write"' in prompt
    assert "Created issue #2" in prompt
    assert prompt.endswith("was the issue created?")
