"""The ``source_provenance`` SSE frame the coordinator drains from the broker.

The tool has already persisted its verdicts, so this drain is purely the live
view — which is why a failure here must cost the mid-turn update and nothing
else.
"""

from __future__ import annotations

import json

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.files import provenance_broker as broker


def _claims():
    return [
        {
            "id": "total",
            "state": "supported",
            "method": "literal",
            "quote": "The total approved budget is $487,500",
            "locator": {"kind": "page", "value": 3, "precision": "measured"},
            "uploadId": "up-1",
            "filename": "award.pdf",
        }
    ]


def setup_function():
    broker._pending.clear()


def _parse(frame: str) -> dict:
    name, data = frame.split("\n", 1)
    assert name == "event: source_provenance"
    return json.loads(data.split("data: ", 1)[1].strip())


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("SOURCE_PROVENANCE_ENABLED", "true")


@pytest.mark.asyncio
async def test_a_queued_verdict_becomes_one_frame(flag_on):
    broker.queue_provenance("u1", "s1", {"toolUseId": "tu-7", "claims": _claims()})
    [frame] = await StreamCoordinator._drain_source_provenance_events("s1", "u1")
    payload = _parse(frame)
    assert payload["type"] == "source_provenance"
    assert payload["sessionId"] == "s1"
    assert payload["toolUseId"] == "tu-7"
    assert payload["claims"] == _claims()


@pytest.mark.asyncio
async def test_the_frame_carries_precision_so_the_spa_can_hedge(flag_on):
    claims = _claims()
    claims[0]["locator"]["precision"] = "estimated"
    broker.queue_provenance("u1", "s1", {"toolUseId": "tu-1", "claims": claims})
    [frame] = await StreamCoordinator._drain_source_provenance_events("s1", "u1")
    assert _parse(frame)["claims"][0]["locator"]["precision"] == "estimated"


@pytest.mark.asyncio
async def test_nothing_queued_emits_nothing(flag_on):
    assert await StreamCoordinator._drain_source_provenance_events("s1", "u1") == []


@pytest.mark.asyncio
async def test_the_drain_is_a_noop_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("SOURCE_PROVENANCE_ENABLED", raising=False)
    broker.queue_provenance("u1", "s1", {"toolUseId": "tu-1", "claims": _claims()})
    assert await StreamCoordinator._drain_source_provenance_events("s1", "u1") == []


@pytest.mark.asyncio
async def test_another_conversation_is_not_drained(flag_on):
    broker.queue_provenance("owner", "s1", {"toolUseId": "tu-1", "claims": _claims()})
    assert await StreamCoordinator._drain_source_provenance_events("s1", "u2") == []
    assert len(await StreamCoordinator._drain_source_provenance_events("s1", "owner")) == 1


@pytest.mark.asyncio
async def test_a_broker_failure_never_breaks_the_stream(flag_on, monkeypatch):
    def _boom(user_id, session_id):
        raise RuntimeError("broker")

    monkeypatch.setattr(broker, "take_provenance", _boom)
    assert await StreamCoordinator._drain_source_provenance_events("s1", "u1") == []
