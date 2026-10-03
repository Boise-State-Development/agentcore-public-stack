"""Side-channel ``converse`` calls send ``temperature`` without ``topP``.

Claude 4.5+ rejects the pair ("`temperature` and `top_p` cannot both be
specified for this model"). Every side channel swallows exceptions and falls
back quietly, so pointing one at a Claude model would degrade it with nothing
but a log line to show for it. The compaction summary hit exactly that; its
own test lives beside it in ``test_compaction_summary.py``. These pin the
other side channels to the same shape.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import apis.inference_api.chat.service as chat_service
from apis.shared import aws_clients
from apis.shared.files import document_digest as dd
from apis.shared.tool_summaries.summarizer import summarize_tool_batch


def _client(text: str, stop_reason: str = "end_turn") -> MagicMock:
    def converse(**kwargs):
        config = kwargs.get("inferenceConfig", {})
        if "temperature" in config and "topP" in config:
            raise RuntimeError("ValidationException: `temperature` and `top_p` cannot both be specified")
        return {"stopReason": stop_reason, "output": {"message": {"content": [{"text": text}]}}}

    client = MagicMock()
    client.converse.side_effect = converse
    return client


@pytest.fixture(autouse=True)
def _fresh_title_client(monkeypatch):
    """The title client is cached per process; each test builds its own."""
    monkeypatch.setattr(chat_service, "_title_bedrock_client", None)


async def _drain_title_writes() -> None:
    await asyncio.gather(*list(chat_service._pending_title_writes))


def _patch_boto3_module(monkeypatch, client: MagicMock) -> None:
    module = MagicMock()
    module.client.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "boto3", module)
    # The side channels share a process-cached client; drop the previous fake.
    aws_clients.reset_cached_clients()


@pytest.fixture(autouse=True)
def _fresh_bedrock_client():
    """The Bedrock client is cached per process; each test builds its own."""
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


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
    await _drain_title_writes()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["guardrail_intervened", "content_filtered", None])
async def test_conversation_title_refusal_keeps_the_placeholder(monkeypatch, stop_reason):
    """A guardrail stop's text is the refusal; it must never become the title."""
    client = _client("Sorry, the model cannot answer this.", stop_reason=stop_reason)
    monkeypatch.setattr(chat_service.boto3, "client", MagicMock(return_value=client))
    update = AsyncMock()
    monkeypatch.setattr(chat_service, "update_session_title", update)
    title = await chat_service.generate_conversation_title(session_id="s", user_id="u", user_input="hi")
    assert title == "New Conversation"
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_title_at_the_token_ceiling_is_still_clipped(monkeypatch):
    client = _client("A" * 80, stop_reason="max_tokens")
    monkeypatch.setattr(chat_service.boto3, "client", MagicMock(return_value=client))
    monkeypatch.setattr(chat_service, "update_session_title", AsyncMock())
    title = await chat_service.generate_conversation_title(session_id="s", user_id="u", user_input="hi")
    assert title == "A" * 47 + "..."
    await _drain_title_writes()


@pytest.mark.asyncio
async def test_conversation_title_returns_before_its_write_lands(monkeypatch):
    """The stream pushes `session_title` when this task finishes, so the
    DynamoDB write must not sit between Nova answering and the user seeing it."""
    client = _client("Planning a biology syllabus")
    monkeypatch.setattr(chat_service.boto3, "client", MagicMock(return_value=client))
    write_may_finish = asyncio.Event()
    written: list = []

    async def slow_write(session_id, user_id, title):
        await write_may_finish.wait()
        written.append(title)

    monkeypatch.setattr(chat_service, "update_session_title", slow_write)

    title = await chat_service.generate_conversation_title(session_id="s", user_id="u", user_input="hi")

    assert title == "Planning a biology syllabus"
    assert written == []
    write_may_finish.set()
    await _drain_title_writes()
    assert written == ["Planning a biology syllabus"]


@pytest.mark.asyncio
async def test_conversation_title_reuses_one_bedrock_client(monkeypatch):
    """A client per title paid botocore's model load and a fresh TLS handshake."""
    client = _client("Planning a biology syllabus")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(chat_service.boto3, "client", factory)
    monkeypatch.setattr(chat_service, "update_session_title", AsyncMock())

    for _ in range(3):
        await chat_service.generate_conversation_title(session_id="s", user_id="u", user_input="hi")
    await _drain_title_writes()

    assert factory.call_count == 1
    assert client.converse.call_count == 3
