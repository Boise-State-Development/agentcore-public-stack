"""Unit tests for persist_synthetic_messages.

The persist helper centralizes a previously-triplicated pattern whose
hasattr() guard was always-False against the current SDK shape (the SDK
exposes ``create_message`` directly on ``AgentCoreMemorySessionManager``,
not via a nested ``.base_manager``). Locking that contract here prevents
the silent-skip bug from drifting back into individual call sites.
"""

from typing import Any, Dict, List

import pytest

from agents.main_agent.session.persistence import persist_synthetic_messages


class _RecordingSessionManager:
    """Mimics the modern SDK shape: ``create_message`` directly on the
    session manager. No nested base_manager indirection."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def create_message(self, session_id: str, agent_id: str, session_message: Any) -> None:
        self.calls.append(
            {"session_id": session_id, "agent_id": agent_id, "message": session_message}
        )


class _LegacyNestedSessionManager:
    """Mimics a hypothetical older SDK shape with a nested
    ``.base_manager``. We honor this for forward-compat in case a future
    SDK reintroduces the indirection."""

    def __init__(self) -> None:
        self.base_manager = _RecordingSessionManager()


class _MissingCreateMessage:
    """Has neither a direct ``create_message`` nor a usable
    ``base_manager``. The helper must return False and log loudly rather
    than silently skip — the failure mode that caused the original bug."""

    pass


def _extract(session_message: Any) -> Dict[str, Any]:
    """Pull (role, text) out of a Strands SessionMessage."""
    msg = getattr(session_message, "message", None)
    assert msg is not None
    role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
    content = msg.get("content", []) if isinstance(msg, dict) else []
    text = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return {"role": role, "text": text}


def test_writes_single_assistant_message():
    sm = _RecordingSessionManager()
    ok = persist_synthetic_messages(sm, "sess-1", [("assistant", "hello there")])

    assert ok is True
    assert len(sm.calls) == 1
    call = sm.calls[0]
    assert call["session_id"] == "sess-1"
    assert call["agent_id"] == "default"
    extracted = _extract(call["message"])
    assert extracted == {"role": "assistant", "text": "hello there"}


def test_writes_user_then_assistant_pair():
    """Used by the quota-exceeded path where the agent never ran."""
    sm = _RecordingSessionManager()
    ok = persist_synthetic_messages(
        sm,
        "sess-2",
        [("user", "what's the weather"), ("assistant", "quota exceeded")],
    )

    assert ok is True
    assert len(sm.calls) == 2
    assert _extract(sm.calls[0]["message"]) == {"role": "user", "text": "what's the weather"}
    assert _extract(sm.calls[1]["message"]) == {"role": "assistant", "text": "quota exceeded"}


def test_honors_custom_agent_id():
    sm = _RecordingSessionManager()
    persist_synthetic_messages(sm, "sess-3", [("assistant", "hi")], agent_id="voice")
    assert sm.calls[0]["agent_id"] == "voice"


def test_returns_false_and_logs_on_missing_create_message(caplog):
    """Regression guard for the original bug: previously the hasattr()
    guard silently skipped writes when create_message wasn't found.
    Now we surface it loudly."""
    sm = _MissingCreateMessage()

    with caplog.at_level("ERROR"):
        ok = persist_synthetic_messages(sm, "sess-bad", [("assistant", "test")])

    assert ok is False
    assert any(
        "no create_message method" in rec.message and "sess-bad" in rec.message
        for rec in caplog.records
    ), f"expected loud error log, got: {[r.message for r in caplog.records]}"


def test_falls_back_to_nested_base_manager_for_forward_compat():
    """If a future SDK reintroduces a ``.base_manager`` wrapper, the
    helper should still find ``create_message`` and write to it."""
    sm = _LegacyNestedSessionManager()
    ok = persist_synthetic_messages(sm, "sess-4", [("assistant", "via legacy path")])

    assert ok is True
    assert len(sm.base_manager.calls) == 1
    assert _extract(sm.base_manager.calls[0]["message"]) == {
        "role": "assistant",
        "text": "via legacy path",
    }


def test_create_message_exception_propagates():
    """The helper does NOT swallow exceptions from ``create_message`` —
    callers wrap with their own try/except so the failure is logged at
    the call site with the right context, not hidden inside this helper."""

    class _RaisingSessionManager:
        def create_message(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("AgentCore Memory rejected the write")

    sm = _RaisingSessionManager()
    with pytest.raises(RuntimeError, match="rejected the write"):
        persist_synthetic_messages(sm, "sess-x", [("assistant", "boom")])


# ---------------------------------------------------------------------------
# Role-alternation guard (last_persisted_role).
#
# Bedrock Converse requires strict user/assistant alternation. TWO consecutive
# same-role turns anywhere in stored history permanently brick the session.
# The synthetic-error paths append an assistant turn after a failure; if the
# session tail is ALREADY an assistant turn (a dangling toolUse or a prior
# synthetic error), that append is the corruption. The guard drops it.
# ---------------------------------------------------------------------------


def test_skips_assistant_write_when_tail_is_assistant():
    """The core fix: error persisted after a dangling assistant turn must NOT
    create a second consecutive assistant message — it is dropped entirely."""
    sm = _RecordingSessionManager()

    ok = persist_synthetic_messages(
        sm,
        "sess-brick",
        [("assistant", "⚠️ Something went wrong")],
        last_persisted_role="assistant",
    )

    # No write happened, but it's a successful no-op (True), not a failure.
    assert ok is True
    assert sm.calls == []


def test_persists_assistant_write_when_tail_is_user():
    """The common, healthy case: the user turn is the tail (persisted by the
    hook at turn start), so appending the assistant error is valid."""
    sm = _RecordingSessionManager()

    ok = persist_synthetic_messages(
        sm,
        "sess-ok",
        [("assistant", "the error explanation")],
        last_persisted_role="user",
    )

    assert ok is True
    assert len(sm.calls) == 1
    assert _extract(sm.calls[0]["message"]) == {"role": "assistant", "text": "the error explanation"}


def test_none_last_role_preserves_verbatim_behavior():
    """``last_persisted_role=None`` (the default) means "caller doesn't know the
    tail" → persist verbatim, matching pre-guard behavior. This is the
    quota-exceeded path that writes a fresh user+assistant pair."""
    sm = _RecordingSessionManager()

    ok = persist_synthetic_messages(
        sm,
        "sess-quota",
        [("user", "hi"), ("assistant", "quota exceeded")],
    )

    assert ok is True
    assert len(sm.calls) == 2


def test_guard_drops_leading_user_but_keeps_alternating_tail():
    """The guard tracks the running role through a multi-message batch: a
    leading ``user`` that collides with a ``user`` tail is dropped, and the
    following ``assistant`` (now valid after the user tail) is kept."""
    sm = _RecordingSessionManager()

    ok = persist_synthetic_messages(
        sm,
        "sess-multi",
        [("user", "dup user"), ("assistant", "answer")],
        last_persisted_role="user",
    )

    assert ok is True
    assert len(sm.calls) == 1
    assert _extract(sm.calls[0]["message"]) == {"role": "assistant", "text": "answer"}


def test_guard_logs_when_skipping(caplog):
    """A dropped synthetic turn is logged loudly so the skip is observable in
    incident forensics, not silent."""
    sm = _RecordingSessionManager()

    with caplog.at_level("WARNING"):
        persist_synthetic_messages(
            sm,
            "sess-log",
            [("assistant", "dropped")],
            last_persisted_role="assistant",
        )

    assert any(
        "preserve role alternation" in rec.message and "sess-log" in rec.message
        for rec in caplog.records
    ), f"expected a skip warning, got: {[r.message for r in caplog.records]}"
