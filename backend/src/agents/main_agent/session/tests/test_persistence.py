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
