"""``outputChars`` on the cost row: what a call's output was made of.

Output tokens are one number. Whether they were answer text or thinking decides
what a lever can save, so each call's slot counts the characters it streamed as
text, as reasoning, and as reasoning signature (the signature tracks the billed
reasoning under summarized thinking). Lengths only, never the text; absent when
the call streamed neither or the cost diagnostics are off.
"""

from __future__ import annotations

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


@pytest.fixture(autouse=True)
def diagnostics_on(monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)


def _slot():
    return StreamCoordinator._new_call_slot(0)


def test_a_new_slot_reports_nothing():
    assert StreamCoordinator._output_chars(_slot()) is None


def test_reasoning_text_and_signature_are_counted_separately():
    slot = _slot()
    StreamCoordinator._count_reasoning(slot, {"reasoningText": "abcde"})
    StreamCoordinator._count_reasoning(slot, {"reasoningText": "fgh", "reasoning_signature": "s" * 40})
    slot["text_chars"] = 12

    assert StreamCoordinator._output_chars(slot) == {"text": 12, "reasoning": 8, "reasoningSignature": 40}


def test_only_what_was_streamed_is_reported():
    slot = _slot()
    slot["text_chars"] = 7
    assert StreamCoordinator._output_chars(slot) == {"text": 7}


def test_lengths_only_never_the_text():
    slot = _slot()
    StreamCoordinator._count_reasoning(slot, {"reasoningText": "the secret plan", "reasoning_signature": "sig-value"})
    assert "secret" not in repr(slot) and "sig-value" not in repr(slot)


def test_a_flag_event_or_malformed_data_counts_nothing():
    slot = _slot()
    StreamCoordinator._count_reasoning(slot, {})
    StreamCoordinator._count_reasoning(slot, None)
    StreamCoordinator._count_reasoning(slot, {"reasoningText": 5, "redactedContent": b"x"})
    assert StreamCoordinator._output_chars(slot) is None


def test_a_slot_written_before_the_counters_existed_reports_nothing():
    assert StreamCoordinator._output_chars({"usage": {}, "metrics": {}}) is None
    assert StreamCoordinator._output_chars(None) is None


def test_the_kill_switch_removes_the_field(monkeypatch):
    slot = _slot()
    slot["text_chars"] = 7
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    assert StreamCoordinator._output_chars(slot) is None
