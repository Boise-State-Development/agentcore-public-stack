"""The coordinator's half of the displayText fix: arm early, back stop late.

`displayText` is what the UI renders in place of a prompt the model saw but
the user never typed — RAG context, attachment guidance, an
`<interruption_note>`. It used to be written only here, at the end of a
successful turn, so a stopped or dropped turn left the augmented prompt as the
only renderable text. `DisplayTextHook` now writes it at append time instead.

What stays the coordinator's job, and is tested here:

1. **Arm the hook every turn, unconditionally — including to None.** The agent
   instance is cached across turns (#741/#751); an arm left by a previous turn
   would stamp its text onto this turn's message index. Same discipline as the
   `turn_lease` stamp next to it.
2. **Back stop only what the hook didn't do.** A wrapper with no hook (voice,
   tests) must keep the old end-of-turn write, and a hook whose write failed
   must not silence it — but the normal path must not put twice.

Driven through the real `stream_response`, like the steering-events suite.
"""

from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _FakeAgent:
    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            return
            yield  # pragma: no cover - empty stream

        return _gen()


class _SessionManager:
    def __init__(self) -> None:
        self.cancelled = False
        self.turn_lease = None

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


class _RecordingHook:
    """Stands in for DisplayTextHook — records arming, reports its result."""

    def __init__(self, wrote: bool = False) -> None:
        self.arms: List[dict] = []
        self._wrote = wrote

    def arm(self, **kwargs) -> None:
        self.arms.append(kwargs)

    @property
    def wrote_this_turn(self) -> bool:
        return self._wrote


class _Wrapper:
    def __init__(self, hook=None) -> None:
        if hook is not None:
            self.display_text_hook = hook


async def _run(wrapper=None, original_message: Optional[str] = None) -> None:
    coordinator = StreamCoordinator()
    async for _ in coordinator.stream_response(
        agent=_FakeAgent(),
        prompt="augmented prompt the model saw",
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=wrapper,
        original_message=original_message,
    ):
        pass


@pytest.fixture
def store():
    with patch(
        "apis.shared.sessions.metadata.store_user_display_text", new_callable=AsyncMock
    ) as mock:
        yield mock


class TestArming:
    @pytest.mark.asyncio
    async def test_arms_the_hook_with_this_turns_text_and_index(self, store):
        hook = _RecordingHook()

        await _run(_Wrapper(hook), original_message="what the user typed")

        assert hook.arms == [
            {
                "session_id": "sess-1",
                "user_id": "user-1",
                "message_index": 0,
                "display_text": "what the user typed",
            }
        ]

    @pytest.mark.asyncio
    async def test_arms_to_none_when_the_prompt_was_not_modified(self, store):
        """Unconditional arming is the point: a cached agent whose previous
        turn was augmented must not write that turn's text against this one."""
        hook = _RecordingHook()

        await _run(_Wrapper(hook), original_message=None)

        assert hook.arms == [
            {
                "session_id": "sess-1",
                "user_id": "user-1",
                "message_index": 0,
                "display_text": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_a_wrapper_without_the_hook_is_not_an_error(self, store):
        await _run(_Wrapper(), original_message="what the user typed")
        await _run(None, original_message="what the user typed")


class TestBackstop:
    @pytest.mark.asyncio
    async def test_skipped_once_the_hook_has_written(self, store):
        """The hook's write is the one that matters; repeating it at turn end
        would put the same record twice on every augmented turn."""
        await _run(_Wrapper(_RecordingHook(wrote=True)), original_message="typed")

        store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_when_the_hook_write_failed(self, store):
        """`wrote_this_turn` stays False on a storage failure, so a turn that
        completes still gets its record."""
        await _run(_Wrapper(_RecordingHook(wrote=False)), original_message="typed")

        store.assert_awaited_once_with(
            session_id="sess-1", user_id="user-1", message_id=0, display_text="typed"
        )

    @pytest.mark.asyncio
    async def test_runs_for_a_wrapper_that_carries_no_hook(self, store):
        """Voice and tests keep exactly the behaviour they had before."""
        await _run(_Wrapper(), original_message="typed")

        store.assert_awaited_once_with(
            session_id="sess-1", user_id="user-1", message_id=0, display_text="typed"
        )

    @pytest.mark.asyncio
    async def test_nothing_written_when_the_prompt_was_not_modified(self, store):
        await _run(_Wrapper(), original_message=None)

        store.assert_not_awaited()
