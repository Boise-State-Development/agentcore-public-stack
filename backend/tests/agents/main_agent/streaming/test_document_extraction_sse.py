"""The ``document_extraction`` SSE frame the coordinator drains from the broker.

``docs/specs/structured-extraction.md`` §9. The tool has already persisted its
rows, so this drain is purely the live view — which is why a failure here must
cost the mid-turn table and nothing else.
"""

from __future__ import annotations

import json

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.extraction import extraction_broker as broker


def _fields():
    return [
        {
            "name": "budget_cap",
            "title": "Budget cap",
            "type": "currency",
            "value": "$500,000",
            "state": "supported",
            "method": "literal",
            "quote": "Awards will be made for up to $500,000",
            "locator": {"kind": "page", "value": 2, "precision": "measured"},
        }
    ]


def _payload(**over):
    base = {
        "toolUseId": "tu-7",
        "fieldSet": "nsf-solicitation",
        "fieldSetVersion": "4",
        "uploadId": "up-1",
        "filename": "nsf.pdf",
        "modelId": "us.anthropic.claude-haiku-4-5",
        "schemaEnforced": True,
        "artifactId": "art-1",
        "downloadUploadId": "dl-1",
        "fields": _fields(),
    }
    base.update(over)
    return base


def setup_function():
    broker._pending.clear()


def _parse(frame: str) -> dict:
    name, data = frame.split("\n", 1)
    assert name == "event: document_extraction"
    return json.loads(data.split("data: ", 1)[1].strip())


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", "true")


@pytest.mark.asyncio
async def test_a_queued_run_becomes_one_frame(flag_on):
    broker.queue_extraction("u1", "s1", _payload())
    [frame] = await StreamCoordinator._drain_document_extractions("s1", "u1")
    event = _parse(frame)
    assert event["type"] == "document_extraction"
    assert event["sessionId"] == "s1"
    assert event["toolUseId"] == "tu-7"
    assert event["fieldSet"] == "nsf-solicitation"
    assert event["fields"][0]["state"] == "supported"


@pytest.mark.asyncio
async def test_the_frame_carries_what_the_model_was_not_told(flag_on):
    """§9, D8. The quote, the locator and the method travel here precisely
    because they must not reach the content blocks."""
    broker.queue_extraction("u1", "s1", _payload())
    [frame] = await StreamCoordinator._drain_document_extractions("s1", "u1")
    field = _parse(frame)["fields"][0]
    assert field["quote"].startswith("Awards will be made")
    assert field["locator"] == {"kind": "page", "value": 2, "precision": "measured"}
    assert field["method"] == "literal"


@pytest.mark.asyncio
async def test_the_extraction_model_id_is_disclosed(flag_on):
    """§6.6. Extraction may run on a different (usually cheaper) model than the
    one the user picked for the conversation, which is a thing to disclose."""
    broker.queue_extraction("u1", "s1", _payload())
    [frame] = await StreamCoordinator._drain_document_extractions("s1", "u1")
    assert _parse(frame)["modelId"] == "us.anthropic.claude-haiku-4-5"


@pytest.mark.asyncio
async def test_an_unenforced_run_says_so_on_the_frame(flag_on):
    broker.queue_extraction("u1", "s1", _payload(schemaEnforced=False))
    [frame] = await StreamCoordinator._drain_document_extractions("s1", "u1")
    assert _parse(frame)["schemaEnforced"] is False


@pytest.mark.asyncio
async def test_several_runs_each_get_their_own_frame(flag_on):
    broker.queue_extraction("u1", "s1", _payload(toolUseId="a"))
    broker.queue_extraction("u1", "s1", _payload(toolUseId="b"))
    frames = await StreamCoordinator._drain_document_extractions("s1", "u1")
    assert [_parse(f)["toolUseId"] for f in frames] == ["a", "b"]


@pytest.mark.asyncio
async def test_a_drained_run_is_not_emitted_twice(flag_on):
    broker.queue_extraction("u1", "s1", _payload())
    assert len(await StreamCoordinator._drain_document_extractions("s1", "u1")) == 1
    assert await StreamCoordinator._drain_document_extractions("s1", "u1") == []


@pytest.mark.asyncio
async def test_nothing_queued_yields_nothing(flag_on):
    assert await StreamCoordinator._drain_document_extractions("s1", "u1") == []


@pytest.mark.asyncio
async def test_the_flag_off_drains_nothing(monkeypatch):
    monkeypatch.delenv("DOCUMENT_EXTRACT_ENABLED", raising=False)
    broker.queue_extraction("u1", "s1", _payload())
    assert await StreamCoordinator._drain_document_extractions("s1", "u1") == []


@pytest.mark.asyncio
async def test_another_user_on_the_same_session_id_drains_nothing(flag_on):
    """A session id alone does not name a conversation (CLAUDE.md). Keying the
    broker on it would let two users drain each other's document values."""
    broker.queue_extraction("owner", "shared", _payload())
    assert await StreamCoordinator._drain_document_extractions("shared", "intruder") == []
    assert len(await StreamCoordinator._drain_document_extractions("shared", "owner")) == 1


@pytest.mark.asyncio
async def test_a_broker_failure_costs_the_live_view_and_nothing_else(
    flag_on, monkeypatch
):
    """The rows are already persisted, so breaking the stream over this would
    trade a mid-turn table for the whole answer."""
    import apis.shared.extraction.extraction_broker as module

    def boom(user_id, session_id):
        raise RuntimeError("broker gone")

    monkeypatch.setattr(module, "take_extractions", boom)
    assert await StreamCoordinator._drain_document_extractions("s1", "u1") == []


@pytest.mark.asyncio
async def test_the_frame_is_valid_sse(flag_on):
    broker.queue_extraction("u1", "s1", _payload())
    [frame] = await StreamCoordinator._drain_document_extractions("s1", "u1")
    assert frame.startswith("event: document_extraction\ndata: ")
    assert frame.endswith("\n\n")
    assert "\n" not in frame[len("event: document_extraction\ndata: ") : -2]
