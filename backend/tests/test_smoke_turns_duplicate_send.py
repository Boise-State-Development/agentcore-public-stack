"""``smoke_turns.py``'s ``duplicate_send_409`` + ``stop_mid_answer`` row, against a fake lease.

The fake models what dev showed on 2026-11-14: the container takes the
single-flight lease before it opens the response, the agent build and the
first token follow, and the lease is released when the turn ends *or* when the
client leaves (on the V2 Runtime a disconnect reaches the container and closes
the turn's generator). The duplicate needs network time to reach the container
before it can test the lease.

The row used to send the duplicate on ``message_start`` and leave 12 deltas
later (~70 ms on dev), while the duplicate took ~700 ms to land, so the lease
was already free and the duplicate was accepted: four FAILs in six nightly runs
with a guard that worked.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "smoke_turns.py"
_spec = importlib.util.spec_from_file_location("smoke_turns_duplicate_send", _SCRIPT)
smoke_turns = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Registered before exec: the script's dataclasses resolve their module via sys.modules.
sys.modules[_spec.name] = smoke_turns
_spec.loader.exec_module(smoke_turns)

TurnResult = smoke_turns.TurnResult
Frame = smoke_turns.Frame

BUSY = '{"detail":"A response is already streaming for this conversation."}'


def _sse(name: str, data: Dict[str, Any]) -> List[str]:
    return [f"event: {name}", f"data: {json.dumps(data)}", ""]


class _FakeResponse:
    """The long turn's SSE body: a build pause, then a fast model's deltas."""

    def __init__(self, build_s: float, deltas: int, on_close: Any) -> None:
        self._build_s = build_s
        self._deltas = deltas
        self._on_close = on_close
        self.message_start_at: Optional[float] = None

    async def aiter_lines(self) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        for line in _sse("agent_status", {"phase": "preparing"}):
            yield line
        await asyncio.sleep(self._build_s)
        self.message_start_at = loop.time()
        for line in _sse("message_start", {"role": "assistant"}):
            yield line
        for i in range(self._deltas):
            await asyncio.sleep(0.001)
            for line in _sse("content_block_delta", {"type": "text", "text": f"{i + 1}\n"}):
                yield line
        for name in ("message_stop", "metadata", "done"):
            for line in _sse(name, {}):
                yield line

    async def aclose(self) -> None:
        self._on_close()


def _short_turn(at_ms: float) -> List[Any]:
    """A whole turn, its frames timed from the send like `_drain_sse` times them."""
    names = ["message_start", "content_block_delta", "message_stop", "metadata", "done"]
    return [Frame(name=n, data={"type": "text", "text": "hi"} if n == "content_block_delta" else {}, t_ms=at_ms + i) for i, n in enumerate(names)]


class _LeaseTransport:
    via_app_api = True
    label = "fake-lease"

    def __init__(self, *, build_s: float = 0.2, deltas: int = 600, network_s: float = 0.05, guard: bool = True) -> None:
        self.build_s = build_s
        self.deltas = deltas
        self.network_s = network_s
        self.guard = guard
        self.lease_held = False
        self.long_response: Optional[_FakeResponse] = None
        self.dup_checked_at: Optional[float] = None

    def _release(self) -> None:
        self.lease_held = False

    async def stream_turn(
        self,
        payload: Dict[str, Any],
        *,
        on_frame: Any = None,
        abort_after_deltas: Optional[int] = None,
        abort_gate: Optional[asyncio.Event] = None,
        timeout_s: float = 300.0,
    ) -> Any:
        loop = asyncio.get_running_loop()
        started = loop.time()
        if payload["message"].startswith("Count from 1"):
            self.lease_held = True
            self.long_response = _FakeResponse(self.build_s, self.deltas, self._release)
            try:
                frames, aborted = await smoke_turns._drain_sse(
                    self.long_response, started, on_frame=on_frame, abort_after_deltas=abort_after_deltas, abort_gate=abort_gate
                )
            finally:
                self._release()  # a turn that ends releases its lease, as the generator's finally does
            return TurnResult(status_code=200, frames=frames, aborted=aborted)
        await asyncio.sleep(self.network_s)  # app-api, the Runtime, the container's prelude
        if payload["message"] == "Say hi.":
            self.dup_checked_at = loop.time()
        if self.guard and self.lease_held:
            return TurnResult(status_code=409, error=BUSY, elapsed_ms=(loop.time() - started) * 1000)
        return TurnResult(status_code=200, frames=_short_turn((loop.time() - started) * 1000))

    async def interrupt(self, session_id: str, reason: str) -> int:
        return 204

    async def get_messages(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        return 200, {"messages": []}


def _run(transport: _LeaseTransport) -> Dict[str, Any]:
    ctx = smoke_turns.Context(transport=transport, args=argparse.Namespace(model_id=None))
    results = asyncio.run(smoke_turns.row_duplicate_and_stop(ctx))
    return {r.row: r for r in results}


def test_the_duplicate_lands_while_the_first_turn_holds_its_lease() -> None:
    transport = _LeaseTransport()
    rows = _run(transport)
    assert rows["duplicate_send_409"].status == "PASS", rows["duplicate_send_409"].detail
    assert rows["stop_mid_answer"].status == "PASS", rows["stop_mid_answer"].detail
    # Sent on the response's first frame, so it is tested before the model's first token.
    assert transport.long_response is not None and transport.long_response.message_start_at is not None
    assert transport.dup_checked_at is not None
    assert transport.dup_checked_at < transport.long_response.message_start_at
    assert rows["duplicate_send_409"].data["dupStatus"] == 409


def test_the_client_stays_until_the_duplicate_has_answered() -> None:
    """A build shorter than the duplicate's trip: the first token arrives first.

    Leaving 12 deltas in would free the lease before the duplicate lands; the
    gate keeps the stream open until it has answered, and the client still
    leaves mid-answer.
    """
    transport = _LeaseTransport(build_s=0.0, network_s=0.1)
    rows = _run(transport)
    assert rows["duplicate_send_409"].status == "PASS", rows["duplicate_send_409"].detail
    assert rows["stop_mid_answer"].status == "PASS", rows["stop_mid_answer"].detail


def test_a_guard_that_lets_the_duplicate_through_is_named() -> None:
    rows = _run(_LeaseTransport(guard=False))
    verdict = rows["duplicate_send_409"]
    assert verdict.status == "FAIL"
    assert "while the first was still streaming" in verdict.detail
    assert "single-flight guard let it through" in verdict.detail


def test_a_first_turn_that_ended_first_is_inconclusive_not_a_guard_gap() -> None:
    rows = _run(_LeaseTransport(build_s=0.0, deltas=5, network_s=0.2))
    verdict = rows["duplicate_send_409"]
    assert verdict.status == "FAIL"
    assert verdict.detail.startswith("inconclusive: the first turn ended")
    assert rows["stop_mid_answer"].status == "FAIL"


def test_the_busy_refusal_is_recognised_in_both_shapes() -> None:
    assert smoke_turns._is_session_busy(TurnResult(status_code=409, error=BUSY))
    runtime_rewrite = '{"detail":"{\\"message\\":\\"Received error (409) from runtime. Please check your CloudWatch logs\\"}"}'
    assert smoke_turns._is_session_busy(TurnResult(status_code=424, error=runtime_rewrite))
    assert not smoke_turns._is_session_busy(TurnResult(status_code=424, error='{"detail":"Received error (500) from runtime"}'))
    assert not smoke_turns._is_session_busy(TurnResult(status_code=200))


def test_drain_holds_the_abort_until_the_gate_opens() -> None:
    async def go(gate_open: bool) -> Tuple[int, bool]:
        gate = asyncio.Event()
        if gate_open:
            gate.set()
        response = _FakeResponse(0.0, 40, lambda: None)
        frames, aborted = await smoke_turns._drain_sse(response, 0.0, on_frame=None, abort_after_deltas=12, abort_gate=gate)
        return sum(1 for f in frames if f.name == "content_block_delta"), aborted

    assert asyncio.run(go(True)) == (12, True)
    assert asyncio.run(go(False)) == (40, False)
