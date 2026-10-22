"""The per-turn history count, off the critical path (turn-path spec §5 P4, F5).

Every per-message metadata row (cost, usage, displayText, the artifact anchor)
is keyed by ``count of messages before this turn + offset``, and the messages
endpoint re-derives the same index on reload. So the number must be exactly
what a serial read at the head of the turn gave — a wrong one silently puts
one turn's cost or clean text on another turn's bubble. What is under test,
most expensive first:

1. **The count excludes this turn's own messages** even when the read lands
   after they were written (the cutoff on ``created_at``).
2. **The read does not block the stream**: the agent starts while it runs.
3. **A failure falls back to the count as it was at the head of the turn**,
   not to a maintained count that has since moved.
4. ``HISTORY_COUNT_PREFETCH_ENABLED=false`` restores the inline read.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.history_count import HistoryCount, count_created_before
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

_HEAD = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _msg(created_at: Optional[str]) -> SimpleNamespace:
    return SimpleNamespace(created_at=created_at)


def _old(n: int) -> List[SimpleNamespace]:
    return [_msg((_HEAD - timedelta(minutes=10 - i)).isoformat()) for i in range(n)]


class TestCountCreatedBefore:
    def test_no_cutoff_counts_everything(self):
        assert count_created_before(_old(3) + [_msg(None)], None) == 4

    def test_messages_from_this_turn_are_excluded(self):
        this_turn = _msg((_HEAD + timedelta(milliseconds=1)).isoformat())
        assert count_created_before(_old(3) + [this_turn], _HEAD) == 3

    def test_a_message_stamped_exactly_at_the_cutoff_is_this_turns(self):
        assert count_created_before([_msg(_HEAD.isoformat())], _HEAD) == 0

    def test_z_suffix_and_naive_stamps_read_as_utc(self):
        z = _msg((_HEAD - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        naive = _msg((_HEAD + timedelta(seconds=1)).replace(tzinfo=None).isoformat())
        assert count_created_before([z, naive], _HEAD) == 1

    def test_missing_or_unparseable_stamps_count_as_history(self):
        assert count_created_before([_msg(None), _msg(""), _msg("not a date")], _HEAD) == 3

    def test_empty(self):
        assert count_created_before(None, _HEAD) == 0
        assert count_created_before([], None) == 0


class TestHistoryCount:
    @pytest.mark.asyncio
    async def test_start_returns_before_the_read_finishes(self):
        release = threading.Event()

        def _count(before: datetime) -> int:
            assert release.wait(5)
            return 9

        count = HistoryCount.start(_count, fallback=0)
        assert count.value is None
        release.set()
        assert await count.resolve() == 9

    @pytest.mark.asyncio
    async def test_the_cutoff_is_taken_at_start(self):
        seen: List[datetime] = []
        count = HistoryCount.start(lambda before: seen.append(before) or 0, fallback=0, now=_HEAD)
        await count.resolve()
        assert seen == [_HEAD]

    @pytest.mark.asyncio
    async def test_reads_once_however_many_consumers_await(self):
        calls: List[int] = []
        count = HistoryCount.start(lambda before: calls.append(1) or 4, fallback=0)
        assert await asyncio.gather(count.resolve(), count.resolve(), count.resolve()) == [4, 4, 4]
        assert await count.resolve() == 4
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_a_failed_read_resolves_to_the_fallback(self):
        def _boom(before: datetime) -> int:
            raise RuntimeError("ListEvents throttled")

        count = HistoryCount.start(_boom, fallback=6)
        assert await count.resolve() == 6

    @pytest.mark.asyncio
    async def test_resolved_needs_no_read(self):
        count = HistoryCount.resolved(3)
        assert count.value == 3
        assert await count.resolve() == 3


class _ListingManager:
    """A session manager whose ``list_messages`` is the SDK's global read."""

    def __init__(self, messages, *, message_count: int = 0, gate: Optional[threading.Event] = None):
        self.config = SimpleNamespace(session_id="sess-1")
        self.message_count = message_count
        self._messages = messages
        self._gate = gate
        self.calls: List[threading.Thread] = []
        self.gate_was_open: Optional[bool] = None
        self.cancelled = False
        self.turn_lease = None

    def list_messages(self, session_id: str, agent_id: str) -> List[Any]:
        self.calls.append(threading.current_thread())
        if self._gate is not None:
            self.gate_was_open = self._gate.wait(5)
        messages = self._messages() if callable(self._messages) else self._messages
        return list(messages)

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


class TestStartHistoryCount:
    @pytest.mark.asyncio
    async def test_counts_off_the_calling_thread_with_this_turns_messages_excluded(self, monkeypatch):
        monkeypatch.delenv("HISTORY_COUNT_PREFETCH_ENABLED", raising=False)
        # The read lands after this turn's user message was written: it comes
        # back in the listing, stamped after the head of the turn.
        manager = _ListingManager(
            lambda: _old(5) + [_msg(datetime.now(timezone.utc).isoformat())]
        )

        count = StreamCoordinator()._start_history_count(manager)

        assert await count.resolve() == 5
        assert manager.calls and manager.calls[0] is not threading.current_thread()

    @pytest.mark.asyncio
    async def test_kill_switch_reads_inline_and_counts_everything(self, monkeypatch):
        monkeypatch.setenv("HISTORY_COUNT_PREFETCH_ENABLED", "false")
        manager = _ListingManager(_old(5))

        count = StreamCoordinator()._start_history_count(manager)

        assert count.value == 5
        assert manager.calls == [threading.current_thread()]

    @pytest.mark.asyncio
    async def test_the_fallback_is_the_count_at_the_head_of_the_turn(self, monkeypatch):
        monkeypatch.delenv("HISTORY_COUNT_PREFETCH_ENABLED", raising=False)
        gate = threading.Event()

        def _boom():
            raise RuntimeError("ListEvents failed")

        manager = _ListingManager(_boom, message_count=4, gate=gate)
        count = StreamCoordinator()._start_history_count(manager)
        # This turn's user message is appended while the read is in flight.
        manager.message_count = 5
        gate.set()

        assert await count.resolve() == 4

    @pytest.mark.asyncio
    async def test_a_manager_that_cannot_list_uses_its_own_count(self, monkeypatch):
        monkeypatch.delenv("HISTORY_COUNT_PREFETCH_ENABLED", raising=False)
        manager = SimpleNamespace(message_count=3)

        count = StreamCoordinator()._start_history_count(manager)

        assert count.value == 3


class _GateOpeningAgent:
    """Opens the gate the history read waits on as soon as the stream starts."""

    def __init__(self, gate: threading.Event) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._gate = gate

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            self._gate.set()
            return
            yield  # pragma: no cover - empty stream

        return _gen()


class _RecordingHook:
    def __init__(self) -> None:
        self.arms: List[dict] = []

    def arm(self, **kwargs) -> None:
        self.arms.append(kwargs)

    @property
    def wrote_this_turn(self) -> bool:
        return True


class TestThroughStreamResponse:
    @pytest.mark.asyncio
    async def test_the_agent_starts_while_the_count_is_still_reading(self, monkeypatch):
        """The whole point: the read used to finish before the agent was
        called. Here it cannot finish until the agent has started."""
        monkeypatch.delenv("HISTORY_COUNT_PREFETCH_ENABLED", raising=False)
        gate = threading.Event()
        manager = _ListingManager(
            lambda: _old(6) + [_msg(datetime.now(timezone.utc).isoformat())], gate=gate
        )
        hook = _RecordingHook()

        with patch(
            "apis.shared.sessions.metadata.store_user_display_text", new_callable=AsyncMock
        ):
            async for _ in StreamCoordinator().stream_response(
                agent=_GateOpeningAgent(gate),
                prompt="augmented prompt",
                session_manager=manager,
                session_id="sess-1",
                user_id="user-1",
                main_agent_wrapper=SimpleNamespace(display_text_hook=hook),
                original_message="typed",
            ):
                pass

        assert manager.gate_was_open is True
        [arm] = hook.arms
        assert await arm["message_index"]() == 6
