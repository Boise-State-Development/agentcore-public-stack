"""History reaches a text-only model with its media blocks replaced by text.

A conversation that sent an image to a vision model and then switched to a
TEXT-only one (``zai.glm-5``) still carries the ``image`` block, and Bedrock
fails the whole request over it. ``text_only_input`` projects the history per
call; these tests pin that the projection replaces every media block a
text-only model rejects, never touches the shared list, and is byte-stable.
"""

import asyncio
import copy
import json
from unittest.mock import MagicMock

import pytest
from strands.models import BedrockModel

from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
from apis.shared.models.text_only_input import project_text_only_messages, text_only_input

IMAGE = {"image": {"format": "png", "source": {"bytes": b"\x89PNG..."}}}
DOCUMENT = {"document": {"format": "pdf", "name": "syllabus", "source": {"bytes": b"%PDF-1.7"}}}
IMAGE_TEXT = "[Image omitted: the current model reads text only]"


def _history():
    """A Claude-era conversation: an attached image, a tool that returned one,
    an attached document, then the turn now running on the text-only model."""
    return [
        {"role": "user", "content": [copy.deepcopy(IMAGE), {"text": "What is in this picture?"}]},
        {"role": "assistant", "content": [{"text": "A cat."}, {"toolUse": {"toolUseId": "t1", "name": "screenshot", "input": {}}}]},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        "content": [{"text": "captured"}, copy.deepcopy(IMAGE)],
                    }
                }
            ],
        },
        {"role": "assistant", "content": [{"text": "Done."}]},
        {"role": "user", "content": [copy.deepcopy(DOCUMENT), {"text": "Summarize it."}]},
        {"role": "assistant", "content": [{"text": "It is a syllabus."}]},
        {"role": "user", "content": [{"text": "Thanks. And now?"}]},
    ]


def _media_keys(messages):
    found = []
    for message in messages:
        for block in message["content"]:
            found += [k for k in ("image", "document", "video") if k in block]
            for item in block.get("toolResult", {}).get("content", []):
                found += [k for k in ("image", "document", "video") if k in item]
    return found


class TestProjection:
    def test_replaces_every_media_block_including_tool_results(self):
        projected = project_text_only_messages(_history())

        assert _media_keys(projected) == []
        assert projected[0]["content"][0] == {"text": IMAGE_TEXT}
        assert projected[2]["content"][0]["toolResult"]["content"] == [
            {"text": "captured"},
            {"text": IMAGE_TEXT},
        ]
        assert projected[4]["content"][0] == {
            "text": '[Document "syllabus" omitted: the current model reads text only]'
        }

    def test_keeps_tool_result_identity_and_status(self):
        result = project_text_only_messages(_history())[2]["content"][0]["toolResult"]
        assert result["toolUseId"] == "t1"
        assert result["status"] == "success"

    def test_video_and_unnamed_document(self):
        messages = [{"role": "user", "content": [{"video": {"format": "mp4"}}, {"document": {"format": "txt"}}]}]
        assert project_text_only_messages(messages)[0]["content"] == [
            {"text": "[Video omitted: the current model reads text only]"},
            {"text": "[Document omitted: the current model reads text only]"},
        ]

    def test_never_mutates_the_shared_history(self):
        """The list is aliased across every agent serving the session (#741).
        A vision agent sharing it must still see the image."""
        history = _history()
        before = copy.deepcopy(history)
        content_lists = [m["content"] for m in history]

        project_text_only_messages(history)

        assert history == before
        assert [m["content"] for m in history] == content_lists
        assert all(a is b for a, b in zip((m["content"] for m in history), content_lists))

    def test_untouched_messages_are_shared_not_copied(self):
        history = _history()
        projected = project_text_only_messages(history)
        assert projected is not history
        for i in (1, 3, 5, 6):
            assert projected[i] is history[i]

    def test_text_history_is_returned_as_is(self):
        history = [{"role": "user", "content": [{"text": "hi"}]}, {"role": "assistant", "content": [{"text": "hello"}]}]
        assert project_text_only_messages(history) is history

    def test_byte_stable_across_calls(self):
        """The prompt-cache contract: the same history projects to the same bytes."""
        first = json.dumps(project_text_only_messages(_history()), sort_keys=True, default=repr)
        second = json.dumps(project_text_only_messages(_history()), sort_keys=True, default=repr)
        assert first == second

    def test_tolerates_odd_shapes(self):
        messages = [{"role": "user", "content": "plain"}, {"role": "user", "content": ["x", {"toolResult": "?"}]}]
        assert project_text_only_messages(messages) is messages


class _RecordingModel:
    """The slice of the Strands ``Model`` interface the wrapper touches."""

    def __init__(self):
        self.streamed = None
        self.counted = None
        self.natively_counted = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.streamed = messages
        yield {"messageStart": {"role": "assistant"}}

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        self.counted = messages
        return 7

    def native_count_tokens(self, messages, tool_specs=None, system_prompt_content=None):
        self.natively_counted = messages
        return 9


class TestModelWrapper:
    @pytest.mark.asyncio
    async def test_stream_and_counts_see_the_projection(self):
        model = text_only_input(_RecordingModel())
        history = _history()

        events = [e async for e in model.stream(history, None, "system", tool_choice=None)]
        assert events == [{"messageStart": {"role": "assistant"}}]
        assert _media_keys(model.streamed) == []

        assert await model.count_tokens(history) == 7
        assert _media_keys(model.counted) == []

        assert model.native_count_tokens(history) == 9
        assert _media_keys(model.natively_counted) == []

        assert _media_keys(history) == ["image", "image", "document"], "the caller's list is untouched"

    def test_a_model_without_native_counts_gains_none(self):
        class NoNative:
            def stream(self, messages, *a, **k):
                return iter(())

            async def count_tokens(self, messages, *a, **k):
                return 0

        assert not hasattr(text_only_input(NoNative()), "native_count_tokens")

    def test_bedrock_request_carries_no_media_and_stays_a_bedrock_model(self):
        """Through the real Converse formatter: what Bedrock would receive."""
        model = text_only_input(
            CountTokensBedrockModel(native_projection=False, model_id="zai.glm-5", region_name="us-west-2")
        )
        assert isinstance(model, BedrockModel)

        captured = {}
        original_format = model.format_request

        def spy(messages, *args, **kwargs):
            captured["request"] = original_format(messages, *args, **kwargs)
            raise RuntimeError("stop before the network")

        model.format_request = spy
        model.client = MagicMock()

        async def drain():
            async for _ in model.stream(_history(), None, "system"):
                pass

        with pytest.raises(RuntimeError, match="stop before the network"):
            asyncio.run(drain())

        request_messages = captured["request"]["messages"]
        assert _media_keys(request_messages) == []
        assert {"text": IMAGE_TEXT} in request_messages[0]["content"]
