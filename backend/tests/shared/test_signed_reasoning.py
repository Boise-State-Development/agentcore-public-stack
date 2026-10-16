"""Claude sees history without the reasoning blocks it cannot verify.

A turn on an OpenAI-compatible model stores its reasoning as an unsigned
``reasoningContent`` block. After a mid-conversation switch to Claude that
block would be replayed, and Claude checks the signature on every replayed
reasoning block. These tests pin that the projection drops only unsigned
reasoning, never touches the shared list, and is byte-stable.
"""

import copy
import json

from apis.shared.models.signed_reasoning import (
    REASONING_OMITTED_TEXT,
    project_signed_reasoning_messages,
    signed_reasoning_input,
)

UNSIGNED = {"reasoningContent": {"reasoningText": {"text": "GPT thinking"}}}
SIGNED = {"reasoningContent": {"reasoningText": {"text": "Claude thinking", "signature": "sig"}}}
REDACTED = {"reasoningContent": {"redactedContent": b"opaque"}}
TOOL_USE = {"toolUse": {"toolUseId": "t1", "name": "search", "input": {}}}


def _history():
    """Claude turn, then a GPT turn with a tool call, then the turn now on Claude."""
    return [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [copy.deepcopy(SIGNED), copy.deepcopy(REDACTED), {"text": "hello"}]},
        {"role": "user", "content": [{"text": "search something"}]},
        {"role": "assistant", "content": [copy.deepcopy(UNSIGNED), copy.deepcopy(TOOL_USE)]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "found"}]}}]},
        {"role": "assistant", "content": [copy.deepcopy(UNSIGNED)]},
        {"role": "user", "content": [{"text": "and now?"}]},
    ]


class TestProjection:
    def test_drops_unsigned_reasoning_and_keeps_the_rest(self):
        projected = project_signed_reasoning_messages(_history())

        assert projected[3]["content"] == [TOOL_USE]
        assert projected[1]["content"] == [SIGNED, REDACTED, {"text": "hello"}]

    def test_message_left_empty_gets_a_placeholder(self):
        """Bedrock rejects a message with no content blocks."""
        assert project_signed_reasoning_messages(_history())[5]["content"] == [{"text": REASONING_OMITTED_TEXT}]

    def test_top_level_signature_counts_as_signed(self):
        block = {"reasoningContent": {"reasoningText": {"text": "x"}, "signature": "sig"}}
        messages = [{"role": "assistant", "content": [block, {"text": "ok"}]}]
        assert project_signed_reasoning_messages(messages) is messages

    def test_empty_signature_is_unsigned(self):
        block = {"reasoningContent": {"reasoningText": {"text": "x", "signature": ""}}}
        messages = [{"role": "assistant", "content": [block, {"text": "ok"}]}]
        assert project_signed_reasoning_messages(messages)[0]["content"] == [{"text": "ok"}]

    def test_never_mutates_the_shared_history(self):
        """The list is aliased across every agent serving the session (#741)."""
        history = _history()
        before = copy.deepcopy(history)

        project_signed_reasoning_messages(history)

        assert history == before

    def test_untouched_messages_are_shared_not_copied(self):
        history = _history()
        projected = project_signed_reasoning_messages(history)
        for i in (0, 1, 2, 4, 6):
            assert projected[i] is history[i]

    def test_claude_only_history_is_returned_as_is(self):
        history = _history()[:3]
        assert project_signed_reasoning_messages(history) is history

    def test_byte_stable_across_calls(self):
        first = json.dumps(project_signed_reasoning_messages(_history()), sort_keys=True, default=repr)
        second = json.dumps(project_signed_reasoning_messages(_history()), sort_keys=True, default=repr)
        assert first == second

    def test_tolerates_odd_shapes(self):
        messages = [{"role": "user", "content": "plain"}, {"role": "user", "content": ["x", {"reasoningContent": "?"}]}]
        assert project_signed_reasoning_messages(messages) is messages


class _RecordingModel:
    def __init__(self):
        self.streamed = None

    def stream(self, messages, *args, **kwargs):
        self.streamed = messages
        return iter(())

    async def count_tokens(self, messages, *args, **kwargs):
        return 0


def test_wrapper_streams_the_projection():
    model = signed_reasoning_input(_RecordingModel())
    model.stream(_history(), None, "system")
    assert model.streamed[3]["content"] == [TOOL_USE]
