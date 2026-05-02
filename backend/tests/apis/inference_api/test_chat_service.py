"""Tests for ``apis.inference_api.chat.service.get_agent`` cache behavior.

Covers the OAuth-resume cache fix (#207):

1. Cache key alignment — when ``system_prompt`` is ``None`` on the original
   turn and the persisted snapshot also stores ``None``, the resume call
   hashes to the same cache slot and reuses the paused agent.
2. Defense-in-depth eviction — a non-resume request that lands on a cached
   agent whose ``_interrupt_state.activated`` is True must drop the cached
   instance and build a fresh one.
3. Resume requests must NOT trigger the eviction path; the whole point of
   resuming is to reuse the paused agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.inference_api.chat import service


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty agent cache."""
    service.clear_agent_cache()
    yield
    service.clear_agent_cache()


def _fake_agent(*, system_prompt=None, activated: bool = False) -> MagicMock:
    """Build a stand-in for BaseAgent that exposes the attrs ``get_agent`` reads.

    Mirrors the real shape: ``BaseAgent.agent`` is the wrapped Strands agent
    and Strands stores interrupt state on ``agent._interrupt_state``. The
    construction snapshot mirrors ``BaseAgent.__init__`` post-fix — it stores
    the *unbuilt* ``system_prompt`` so resume hashes back to the same cache
    slot.
    """
    inner = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=activated))
    wrapper = MagicMock(spec=["agent", "_construction_snapshot"])
    wrapper.agent = inner
    wrapper._construction_snapshot = {"system_prompt": system_prompt}
    return wrapper


@pytest.fixture
def mock_create_agent():
    """Patch out the agent factory so ``get_agent`` returns a fresh fake each call.

    The fake's snapshot mirrors the real ``BaseAgent`` post-fix: it stores
    the unbuilt ``system_prompt`` parameter (not a rendered output).
    """
    with patch.object(service, "create_agent") as mock:
        mock.side_effect = lambda **kwargs: _fake_agent(
            system_prompt=kwargs.get("system_prompt")
        )
        yield mock


@pytest.fixture
def mock_freshness_hash():
    """Stable freshness hash so cache keys depend only on the inputs we care about."""
    with patch(
        "apis.shared.tools.freshness.get_freshness_hash",
        new=AsyncMock(return_value="fresh"),
    ) as mock:
        yield mock


@pytest.mark.asyncio
async def test_resume_replay_from_snapshot_hits_same_cache_slot(
    mock_create_agent, mock_freshness_hash
):
    """The regression fixed in #207. Original turn with ``system_prompt=None``
    pauses on OAuth consent; ``stream_coordinator`` writes the construction
    snapshot to DynamoDB; the resume request reads ``snapshot.system_prompt``
    and feeds it back into ``get_agent``. With the fix, the snapshot stores
    the unbuilt prompt (``None``), which hashes to the same cache key as the
    original turn — so resume reuses the paused agent. With the bug
    (snapshot stored the rendered base+date string), the cache key would
    diverge and resume would rebuild, orphaning the paused agent.
    """
    # Original turn: system_prompt=None
    first = await service.get_agent(
        session_id="s1",
        user_id="u1",
        system_prompt=None,
        is_resume=False,
    )
    first.agent._interrupt_state.activated = True

    # Production replay: stream_coordinator persists _construction_snapshot
    # and the resume request feeds snapshot.system_prompt back into get_agent.
    snapshot_system_prompt = first._construction_snapshot["system_prompt"]
    assert snapshot_system_prompt is None, (
        "post-fix snapshot must store the unbuilt prompt (None), not a "
        "rendered string — otherwise resume hashes to a different cache slot"
    )

    second = await service.get_agent(
        session_id="s1",
        user_id="u1",
        system_prompt=snapshot_system_prompt,
        is_resume=True,
    )

    assert second is first, "resume should return the same cached (paused) agent"
    assert mock_create_agent.call_count == 1


@pytest.mark.asyncio
async def test_non_resume_evicts_paused_cached_agent(
    mock_create_agent, mock_freshness_hash
):
    """If a paused agent ever ends up cached on a non-resume cache lookup
    (the bug we're hardening against), evict it and build fresh. Strands would
    otherwise reject the next plain user message with ``must resume from
    interrupt with list of interruptResponse's``.
    """
    paused = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=False,
    )
    paused.agent._interrupt_state.activated = True

    rebuilt = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=False,
    )

    assert rebuilt is not paused, "non-resume must not be served the paused agent"
    assert mock_create_agent.call_count == 2


@pytest.mark.asyncio
async def test_resume_does_not_evict_paused_cached_agent(
    mock_create_agent, mock_freshness_hash
):
    """The eviction path is gated on ``is_resume=False``. A genuine resume
    request must reuse the paused agent so Strands' ``_interrupt_state.resume``
    receives the original interrupt entry list — otherwise we'd rebuild the
    agent and the resume would have nothing to resume against.
    """
    paused = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=False,
    )
    paused.agent._interrupt_state.activated = True

    resumed = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=True,
    )

    assert resumed is paused
    assert mock_create_agent.call_count == 1


@pytest.mark.asyncio
async def test_non_resume_keeps_non_paused_cached_agent(
    mock_create_agent, mock_freshness_hash
):
    """Sanity check: the eviction path only fires when ``activated`` is True.
    A normal cache hit on a healthy agent stays a cache hit.
    """
    first = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=False,
    )
    second = await service.get_agent(
        session_id="s1",
        user_id="u1",
        is_resume=False,
    )

    assert second is first
    assert mock_create_agent.call_count == 1
