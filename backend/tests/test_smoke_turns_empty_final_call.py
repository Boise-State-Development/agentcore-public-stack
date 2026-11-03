"""``smoke_turns.py``'s ``empty_final_call`` row, run against a fake transport.

The frames below are the shape dev streamed on 2026-10-30 for a calculator
turn whose second model call was empty: Bedrock sent that call's
``messageStop`` + ``metadata`` with no ``messageStart``, and the coordinator
closed the turn with a ``metadata`` frame repeating the last call's usage.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "smoke_turns.py"
_spec = importlib.util.spec_from_file_location("smoke_turns", _SCRIPT)
smoke_turns = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Registered before exec: the script's dataclasses resolve their module via sys.modules.
sys.modules[_spec.name] = smoke_turns
_spec.loader.exec_module(smoke_turns)

Frame = smoke_turns.Frame

FIRST = {"inputTokens": 3, "outputTokens": 65}
EMPTY = {"inputTokens": 6, "outputTokens": 2}


def _frames(*, empty_call: bool = True) -> List[Any]:
    names: List[Tuple[str, Dict[str, Any]]] = [
        ("message_start", {"role": "assistant"}),
        ("content_block_delta", {"type": "text", "text": "About 7 million."}),
        ("message_stop", {"stopReason": "tool_use"}),
        ("metadata", {"usage": dict(FIRST)}),
        ("tool_use", {"tool_use": {"name": "calculator"}}),
        ("tool_result", {}),
    ]
    if empty_call:
        names += [("message_stop", {"stopReason": "end_turn"}), ("metadata", {"usage": dict(EMPTY)})]
    else:
        names += [
            ("message_start", {"role": "assistant"}),
            ("content_block_delta", {"type": "text", "text": "7006652"}),
            ("message_stop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": dict(EMPTY)}),
        ]
    names += [("metadata", {"usage": dict(EMPTY)}), ("done", {})]
    return [Frame(name=n, data=d, t_ms=float(i)) for i, (n, d) in enumerate(names)]


class _FakeTransport:
    via_app_api = False
    label = "fake"

    def __init__(self, turns: List[List[Any]], rows: List[Dict[str, Any]]) -> None:
        self._turns = list(turns)
        self._rows = rows
        self.sent: List[Dict[str, Any]] = []

    async def stream_turn(self, payload: Dict[str, Any], **_: Any) -> Any:
        self.sent.append(payload)
        return smoke_turns.TurnResult(status_code=200, frames=self._turns.pop(0))

    async def get_calls(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        return 200, {"calls": self._rows}


def _run(transport: _FakeTransport) -> Any:
    args = argparse.Namespace(tool="calculator", model_id=None)
    ctx = smoke_turns.Context(transport=transport, args=args)
    # The row polls while rows land; the fake's rows never change.
    with patch.object(smoke_turns.asyncio, "sleep", AsyncMock()):
        return asyncio.run(smoke_turns.row_empty_final_call(ctx))


def _row(message_id: int, usage: Dict[str, int]) -> Dict[str, Any]:
    return {"messageId": message_id, **usage}


def test_calls_are_counted_by_stop_and_skip_the_turn_level_metadata() -> None:
    turn = smoke_turns.TurnResult(status_code=200, frames=_frames())
    calls = smoke_turns._calls_from_frames(turn)
    assert [c["usage"] for c in calls] == [FIRST, EMPTY]


def test_one_row_per_call_with_its_own_usage_passes() -> None:
    result = _run(_FakeTransport([_frames()], [_row(1, FIRST), _row(3, EMPTY)]))
    assert result.status == "PASS"
    assert result.data["messageIds"] == [1, 3]


def test_the_merged_row_of_the_old_bug_fails() -> None:
    """Pre-#1785: the empty call's usage overwrote call 1's row."""
    try:
        _run(_FakeTransport([_frames()], [_row(1, EMPTY)]))
    except smoke_turns.RowFailure as failure:
        assert "2 model calls streamed" in str(failure)
    else:
        raise AssertionError("a merged row must fail the row")


def test_a_row_holding_another_calls_usage_fails() -> None:
    try:
        _run(_FakeTransport([_frames()], [_row(1, EMPTY), _row(3, EMPTY)]))
    except smoke_turns.RowFailure as failure:
        assert "call 0" in str(failure)
    else:
        raise AssertionError("a row with the wrong call's usage must fail the row")


def test_a_model_that_always_writes_text_is_observed_not_failed() -> None:
    transport = _FakeTransport([_frames(empty_call=False), _frames(empty_call=False)], [])
    result = _run(transport)
    assert result.status == "OBSERVED"
    assert len(transport.sent) == len(smoke_turns.EMPTY_FINAL_PROMPTS)
