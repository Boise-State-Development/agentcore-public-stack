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
