"""The paused-turn snapshot carries the memory binding (Shared Projects 2.1).

The binding is an agent-cache key element, so resume can only find the paused
agent if the snapshot the coordinator persists includes it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

BINDING = {"spaceId": "space-1", "spaceName": "Team notes", "access": "readwrite"}


async def _persist(construction_snapshot):
    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    wrapper = SimpleNamespace(_construction_snapshot=construction_snapshot)
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(agent, "sess-1", "user-1", wrapper)
    write.assert_awaited_once()
    return write.await_args.args[2]


@pytest.mark.asyncio
async def test_the_binding_is_persisted():
    snapshot = await _persist({"enabled_tools": [], "memory_binding": BINDING})
    assert snapshot.memory_binding == BINDING


@pytest.mark.asyncio
async def test_no_binding_persists_none():
    snapshot = await _persist({"enabled_tools": [], "memory_binding": None})
    assert snapshot.memory_binding is None


# ── Shared Projects 2.4b: a project harness's scopes ride the same field ──

SCOPES = {"projectId": "prj_1", "sharedSpaceId": "spc_shared", "personalSpaceId": None}


@pytest.mark.asyncio
async def test_a_harness_snapshot_round_trips_and_resumes_into_the_same_slot():
    from apis.inference_api.chat.service import memory_binding_digest
    from apis.shared.sessions.models import PausedTurnSnapshot

    snapshot = await _persist({"enabled_tools": [], "memory_binding": SCOPES})
    stored = snapshot.model_dump(by_alias=True, exclude_none=True)
    replayed = PausedTurnSnapshot.model_validate(stored).memory_binding
    assert memory_binding_digest(replayed) == memory_binding_digest(SCOPES)


def test_a_snapshot_written_before_2_4b_still_loads():
    """No new fields: a snapshot from before 2.4b parses and keys as it did."""
    from apis.inference_api.chat.service import memory_binding_digest
    from apis.shared.sessions.models import PausedTurnSnapshot

    old = {
        "enabledTools": ["calculator"],
        "memoryBinding": BINDING,
        "memoryContext": "<memory_space scope=\"agent\">…</memory_space>",
        "capturedAt": "2026-09-25T00:00:00+00:00",
        "expiresAt": "2026-09-25T01:00:00+00:00",
    }
    snapshot = PausedTurnSnapshot.model_validate(old)
    assert snapshot.memory_binding == BINDING
    assert memory_binding_digest(snapshot.memory_binding) == memory_binding_digest(dict(BINDING))
