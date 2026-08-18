"""Abandoning a paused turn when the user types instead of consenting.

`OAuthConsentHook` pauses a turn by calling `event.interrupt(...)`, which
sets `_interrupt_state.activated` on the agent. The agent is cached across
turns, so if the user never completes consent and just sends a new message,
that flag is still armed — and Strands' `InterruptState.resume` rejects a
plain string prompt with

    TypeError: prompt_type=<class 'str'> | must resume from interrupt with
    list of interruptResponse's

which reached the user as a non-recoverable `stream_error`, on every
subsequent turn, for the life of the process.

Two things have to happen together: drop the flag, and leave `agent.messages`
in a shape Bedrock accepts. Strands appends the assistant `toolUse` message
before running tools and returns on interrupt without appending the matching
`toolResult`, so the history ends on an unanswered tool call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.main_agent.streaming.stream_coordinator import (
    _drop_abandoned_turn_tail,
    _is_interrupt_resume_prompt,
    reset_stale_interrupt_state,
)


class _InterruptState:
    """Stand-in for strands.interrupt.InterruptState."""

    def __init__(self, activated: bool, interrupts: dict | None = None):
        self.activated = activated
        self.interrupts = interrupts or {}
        self.context = {"tool_use_message": {}}

    def deactivate(self) -> None:
        self.interrupts = {}
        self.context = {}
        self.activated = False


def _paused_agent(messages: list) -> SimpleNamespace:
    return SimpleNamespace(
        _interrupt_state=_InterruptState(True, {"i-1": object()}),
        messages=messages,
    )


def _completed_turn() -> list:
    return [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi there"}]},
    ]


def _abandoned_tail() -> list:
    """A turn that paused on a tool call: user asked, model emitted a
    toolUse, the hook interrupted before any toolResult was appended."""
    return [
        {"role": "user", "content": [{"text": "list my issues"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "t-1", "name": "github_issues"}}],
        },
    ]


class TestIsInterruptResumePrompt:
    def test_resume_payload_is_recognised(self):
        prompt = [{"interruptResponse": {"interruptId": "i-1", "response": "ok"}}]
        assert _is_interrupt_resume_prompt(prompt) is True

    def test_plain_string_is_not_a_resume(self):
        assert _is_interrupt_resume_prompt("what's the weather") is False

    def test_multimodal_content_is_not_a_resume(self):
        # A fresh turn with an attachment is a list too — it must not be
        # mistaken for a resume, or the stale pause survives.
        prompt = [{"text": "describe this"}, {"image": {"format": "png"}}]
        assert _is_interrupt_resume_prompt(prompt) is False

    def test_empty_list_is_not_a_resume(self):
        # `[]` is the max_tokens "Continue" prompt. A real resume always
        # carries at least one entry (`if interrupt_responses:`).
        assert _is_interrupt_resume_prompt([]) is False

    def test_mixed_content_block_is_not_a_resume(self):
        prompt = [{"interruptResponse": {"interruptId": "i-1"}, "text": "hi"}]
        assert _is_interrupt_resume_prompt(prompt) is False


class TestDropAbandonedTurnTail:
    def test_drops_back_to_the_last_completed_assistant_turn(self):
        messages = _completed_turn() + _abandoned_tail()
        dropped = _drop_abandoned_turn_tail(messages)

        assert dropped == 2
        assert messages == _completed_turn()

    def test_drops_a_multi_cycle_abandoned_turn_whole(self):
        """The abandoned turn may have completed tool cycles before the one
        that paused — none of it survives, the turn produced no answer."""
        messages = _completed_turn() + [
            {"role": "user", "content": [{"text": "do a lot"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t-1"}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t-1"}}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t-2"}}]},
        ]
        dropped = _drop_abandoned_turn_tail(messages)

        assert dropped == 4
        assert messages == _completed_turn()

    def test_empties_history_when_the_first_turn_was_abandoned(self):
        messages = _abandoned_tail()
        assert _drop_abandoned_turn_tail(messages) == 2
        assert messages == []

    def test_mutates_in_place_preserving_the_alias(self):
        """The list is shared by reference between the cached agents serving
        one session (#741/#750) — rebinding would silently fork history."""
        messages = _completed_turn() + _abandoned_tail()
        alias = messages

        _drop_abandoned_turn_tail(messages)

        assert alias is messages
        assert alias == _completed_turn()

    def test_no_op_on_already_clean_history(self):
        messages = _completed_turn()
        assert _drop_abandoned_turn_tail(messages) == 0
        assert messages == _completed_turn()


class TestResetStaleInterruptState:
    def test_clears_the_flag_and_the_dangling_tool_use(self):
        messages = _completed_turn() + _abandoned_tail()
        agent = _paused_agent(messages)

        reset_stale_interrupt_state(agent, "a brand new question")

        assert agent._interrupt_state.activated is False
        assert messages == _completed_turn()
        # Ends on an assistant turn, so the incoming user prompt keeps roles
        # alternating, and carries no unanswered toolUse.
        assert messages[-1]["role"] == "assistant"

    def test_leaves_a_genuine_resume_untouched(self):
        messages = _completed_turn() + _abandoned_tail()
        agent = _paused_agent(messages)
        prompt = [{"interruptResponse": {"interruptId": "i-1", "response": "ok"}}]

        reset_stale_interrupt_state(agent, prompt)

        # Clearing here would destroy the very turn being resumed.
        assert agent._interrupt_state.activated is True
        assert len(messages) == 4

    def test_no_op_when_no_pause_is_armed(self):
        messages = _completed_turn()
        agent = SimpleNamespace(
            _interrupt_state=_InterruptState(False), messages=messages
        )

        reset_stale_interrupt_state(agent, "hello again")

        assert messages == _completed_turn()

    def test_continuation_clears_a_stale_pause(self):
        """`[]` is a max_tokens Continue, not a resume."""
        messages = _completed_turn() + _abandoned_tail()
        agent = _paused_agent(messages)

        reset_stale_interrupt_state(agent, [])

        assert agent._interrupt_state.activated is False

    def test_agent_without_interrupt_state_is_a_no_op(self):
        agent = SimpleNamespace(messages=_completed_turn())
        reset_stale_interrupt_state(agent, "hi")  # must not raise

    def test_deactivate_failure_leaves_history_alone(self):
        """If we can't clear the flag we must not half-apply the repair —
        the turn will fail either way, and mangled history outlives it."""

        class _Boom(_InterruptState):
            def deactivate(self):
                raise RuntimeError("boom")

        messages = _completed_turn() + _abandoned_tail()
        agent = SimpleNamespace(_interrupt_state=_Boom(True), messages=messages)

        reset_stale_interrupt_state(agent, "new question")

        assert len(messages) == 4


class TestStrandsContractAlignment:
    """Guard against the real `InterruptState` drifting from our predicate."""

    @pytest.mark.parametrize(
        "prompt",
        [
            "a string prompt",
            [{"text": "multimodal"}],
        ],
    )
    def test_prompts_we_call_fresh_are_exactly_what_strands_rejects(self, prompt):
        # Private in the SDK (`_InterruptState`) — imported by its real
        # name on purpose: this test exists to fail loudly if a strands
        # upgrade renames or reshapes it under us.
        from strands.interrupt import _InterruptState

        state = _InterruptState()
        state.activate()

        assert _is_interrupt_resume_prompt(prompt) is False
        with pytest.raises(TypeError, match="must resume from interrupt"):
            state.resume(prompt)

    def test_a_prompt_we_call_a_resume_is_accepted_by_strands(self):
        from strands.interrupt import Interrupt, _InterruptState

        state = _InterruptState()
        state.interrupts = {"i-1": Interrupt(id="i-1", name="oauth:github")}
        state.activate()
        prompt = [{"interruptResponse": {"interruptId": "i-1", "response": "ok"}}]

        assert _is_interrupt_resume_prompt(prompt) is True
        state.resume(prompt)  # must not raise
        assert state.interrupts["i-1"].response == "ok"

    def test_reset_makes_the_real_sdk_accept_the_next_plain_prompt(self):
        """End-to-end guard on the reported bug.

        Builds a genuinely paused agent around the real `_InterruptState`,
        runs the reset, then replays exactly what `stream_async` does with a
        fresh prompt. Before the fix this raised the production TypeError.
        """
        from strands.interrupt import Interrupt, _InterruptState

        state = _InterruptState()
        state.interrupts = {"i-1": Interrupt(id="i-1", name="oauth:github-oauth")}
        state.context = {"tool_use_message": {}, "tool_results": []}
        state.activate()

        messages = _completed_turn() + _abandoned_tail()
        agent = SimpleNamespace(_interrupt_state=state, messages=messages)

        # Pre-condition: this is the crash.
        with pytest.raises(TypeError, match="must resume from interrupt"):
            state.resume("a brand new question")

        reset_stale_interrupt_state(agent, "a brand new question")

        # Post-condition: the same call is now a no-op, and the history the
        # prompt is about to land on is Bedrock-valid.
        state.resume("a brand new question")
        assert state.activated is False
        assert messages[-1]["role"] == "assistant"
        assert not any(
            "toolUse" in block
            for msg in messages
            for block in (msg.get("content") or [])
            if isinstance(block, dict)
        )
