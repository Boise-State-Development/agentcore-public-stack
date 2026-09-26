"""Side-channel ``converse`` calls send ``temperature`` without ``topP``.

Claude 4.5+ rejects the pair ("`temperature` and `top_p` cannot both be
specified for this model"). Every side channel swallows exceptions and falls
back quietly, so pointing one at a Claude model would degrade it with nothing
but a log line to show for it. The compaction summary hit exactly that; its
own test lives beside it in ``test_compaction_summary.py``. These pin the
other side channels to the same shape.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import apis.inference_api.chat.service as chat_service
from apis.shared.files import document_digest as dd
from apis.shared.tool_summaries.summarizer import summarize_tool_batch


def _client(text: str) -> MagicMock:
    def converse(**kwargs):
        config = kwargs.get("inferenceConfig", {})
        if "temperature" in config and "topP" in config:
            raise RuntimeError("ValidationException: `temperature` and `top_p` cannot both be specified")
        return {"stopReason": "end_turn", "output": {"message": {"content": [{"text": text}]}}}

    client = MagicMock()
    client.converse.side_effect = converse
    return client


def _patch_boto3_module(monkeypatch, client: MagicMock) -> None:
    module = MagicMock()
    module.client.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "boto3", module)


@pytest.mark.asyncio
async def test_tool_batch_summary(monkeypatch):
    monkeypatch.setenv("TOOL_SUMMARIES_ENABLED", "true")
    client = _client("Found 3 active courses")
    _patch_boto3_module(monkeypatch, client)
    calls = [{"toolUseId": "t0", "toolName": "list_courses", "input": "{}", "result": "[]", "ok": True, "durationMs": 1}]
    assert await summarize_tool_batch(calls) == "Found 3 active courses"
    assert "topP" not in client.converse.call_args.kwargs["inferenceConfig"]


@pytest.mark.asyncio
async def test_document_abstract(monkeypatch):
    client = _client("A crisp abstract.")
    _patch_boto3_module(monkeypatch, client)
    result = await dd.generate_abstract(
        dd.DocumentDigest(), "text", model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert result == "A crisp abstract."
    assert "topP" not in client.converse.call_args.kwargs["inferenceConfig"]


@pytest.mark.asyncio
async def test_conversation_title(monkeypatch):
    client = _client("Planning a biology syllabus")
    monkeypatch.setattr(chat_service.boto3, "client", MagicMock(return_value=client))
    monkeypatch.setattr(chat_service, "update_session_title", AsyncMock())
    title = await chat_service.generate_conversation_title(session_id="s", user_id="u", user_input="hi")
    assert title == "Planning a biology syllabus"
    assert "topP" not in client.converse.call_args.kwargs["inferenceConfig"]
