"""Tests for the partial-output tag every model's stream carries.

The retry strategy may restart a failed call only if the user saw none of it.
OpenAI raises the same exception either way, so the wrapper records which case
it was on the exception itself.
"""

import pytest

from apis.shared.models.stream_output import (
    is_visible_output_chunk,
    streamed_partial_output,
    tag_partial_output,
)


class _FakeModel:
    """Yields the given chunks, then raises."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = 0

    async def stream(self, *args, **kwargs):
        self.calls += 1
        for chunk in self._chunks:
            yield chunk
        raise RuntimeError("The server had an error while processing your request.")


async def _drain(model):
    seen = []
    with pytest.raises(RuntimeError) as excinfo:
        async for chunk in model.stream([], None, "system", tool_choice=None):
            seen.append(chunk)
    return seen, excinfo.value


class TestTagPartialOutput:
    @pytest.mark.asyncio
    async def test_failure_before_any_content_is_not_tagged(self):
        model = tag_partial_output(_FakeModel([{"messageStart": {"role": "assistant"}}]))

        seen, error = await _drain(model)

        assert seen == [{"messageStart": {"role": "assistant"}}]
        assert streamed_partial_output(error) is False

    @pytest.mark.asyncio
    async def test_failure_after_text_is_tagged(self):
        chunks = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": "Here is"}}},
        ]
        model = tag_partial_output(_FakeModel(chunks))

        seen, error = await _drain(model)

        assert seen == chunks
        assert streamed_partial_output(error) is True

    @pytest.mark.asyncio
    async def test_wrapping_twice_is_a_no_op(self):
        model = _FakeModel([])
        tag_partial_output(model)
        wrapped = model.stream
        tag_partial_output(model)

        assert model.stream is wrapped
        await _drain(model)
        assert model.calls == 1

    @pytest.mark.asyncio
    async def test_successful_stream_passes_through(self):
        class _Ok:
            async def stream(self, *args, **kwargs):
                yield {"contentBlockDelta": {"delta": {"text": "hi"}}}
                yield {"messageStop": {"stopReason": "end_turn"}}

        model = tag_partial_output(_Ok())
        assert [c async for c in model.stream([])] == [
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]


class TestIsVisibleOutputChunk:
    @pytest.mark.parametrize(
        "chunk, visible",
        [
            ({"contentBlockDelta": {"delta": {"text": "x"}}}, True),
            ({"contentBlockStart": {"start": {}}}, True),
            ({"messageStart": {"role": "assistant"}}, False),
            ({"metadata": {"usage": {}}}, False),
            ("not a dict", False),
        ],
    )
    def test_classification(self, chunk, visible):
        assert is_visible_output_chunk(chunk) is visible
