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


def test_create_cache_key_includes_skills_hash():
    """Two skill sets must not collide in the agent cache (skills_hash)."""
    base = dict(
        session_id="s",
        user_id="u",
        enabled_tools=["t"],
        model_id="m",
        inference_params={},
        system_prompt=None,
        caching_enabled=False,
        provider="bedrock",
        freshness_hash="f",
        agent_type="skill",
    )
    k1 = service._create_cache_key(**base, skills_hash="aaa")
    k2 = service._create_cache_key(**base, skills_hash="bbb")
    assert k1 != k2
    assert k1[-1] == "aaa"
    # Default (chat) callers omit it → stable empty trailing element.
    assert service._create_cache_key(**base)[-1] == ""


@pytest.mark.asyncio
async def test_skills_hash_separates_skill_agent_cache_slots(
    mock_create_agent, mock_freshness_hash
):
    """Same session+user but different accessible skills → different agents;
    identical skills → cache hit. Verifies skills_hash is threaded into the key.
    """
    with patch(
        "apis.shared.skills.freshness.get_freshness_hash",
        new=AsyncMock(side_effect=lambda ids: "|".join(sorted(ids))),
    ):
        a1 = await service.get_agent(
            session_id="s", user_id="u", agent_type="skill",
            accessible_skill_ids=["pdf"],
        )
        a1_again = await service.get_agent(
            session_id="s", user_id="u", agent_type="skill",
            accessible_skill_ids=["pdf"],
        )
        a2 = await service.get_agent(
            session_id="s", user_id="u", agent_type="skill",
            accessible_skill_ids=["pdf", "docx"],
        )

    assert a1 is a1_again            # same skills → cache hit
    assert a1 is not a2             # different skills → distinct slot
    assert mock_create_agent.call_count == 2
    # The skill path forwards the resolved ids to the factory.
    forwarded = [c.kwargs.get("accessible_skill_ids") for c in mock_create_agent.call_args_list]
    assert ["pdf"] in forwarded and ["pdf", "docx"] in forwarded


@pytest.mark.asyncio
async def test_chat_path_unaffected_by_skills_hash(mock_create_agent, mock_freshness_hash):
    """The default chat path passes no accessible skills → skills_hash empty and
    cache behaves exactly as before. Skills v2: ChatAgent (the target of both
    "chat" and "skill" types) accepts accessible_skill_ids, so it is forwarded —
    as None on the chat path, which adds no AgentSkills plugin.
    """
    a = await service.get_agent(session_id="s", user_id="u")
    a_again = await service.get_agent(session_id="s", user_id="u")
    assert a is a_again
    assert mock_create_agent.call_count == 1
    assert mock_create_agent.call_args.kwargs.get("accessible_skill_ids") is None


# ---------------------------------------------------------------------------
# Issue #741 — conversation continuity across cache keys
# ---------------------------------------------------------------------------
#
# The cache key intentionally varies with the agent's *configuration* (system
# prompt, tools, model, skills). The conversation is not configuration: one
# session is one conversation, whichever agent ran a given turn.
#
# An `@`-mention (Marketplace D11) is the first path that changes agent identity
# mid-thread, so it is the first to violate that: the mention turn misses the
# cache and builds a second Agent, then the next plain turn reverts the key and
# cache-HITs the original instance, whose in-memory `agent.messages` still ends
# before the mention. `initialize()` never re-runs on a hit, so the stale list
# wins silently and the model answers "NOT IN HISTORY" about a turn the user can
# see on screen. Measured on dev session e5e8b259-1780-4179-8ebe-38c57d3709a5.


def _fake_agent_with_messages(*, system_prompt=None, restored=None) -> MagicMock:
    """Fake whose inner Strands agent carries a `messages` list, like the real one.

    ``restored`` models what ``TurnBasedSessionManager.initialize()`` loads from
    AgentCore Memory when a *fresh* instance is built — a copy, because each real
    instance owns its own list. That detail is the whole bug: a cache **hit**
    skips this entirely and keeps whatever the instance last saw.
    """
    inner = SimpleNamespace(
        _interrupt_state=SimpleNamespace(activated=False),
        messages=list(restored or []),
    )
    wrapper = MagicMock(spec=["agent", "_construction_snapshot"])
    wrapper.agent = inner
    wrapper._construction_snapshot = {"system_prompt": system_prompt}
    return wrapper


@pytest.mark.asyncio
async def test_second_cache_key_for_a_session_shares_the_conversation(mock_freshness_hash):
    """Turn 3 runs under a different config; turn 4 reverts and must still see turn 3.

    Models the `@`-mention round trip: plain → plain → mention → plain, where the
    mention swaps the system prompt (any cache-key component would do). ``store``
    stands in for AgentCore Memory: every turn flushes into it, and every *freshly
    built* agent restores from it. That is why the mention turn sees the earlier
    history in production — it is a cache miss, so it restores. The plain turn
    after it is a cache **hit**, restores nothing, and is therefore stale.

    The assertion is deliberately about the *conversation*, not instance identity:
    a fix may reuse one instance, hand the message list between instances, or
    re-restore on a stale hit, and this test should pass either way.
    """
    store: list[dict] = []

    def _turn(agent, *messages):
        """Run a turn: the agent appends, and the session flushes to Memory."""
        agent.agent.messages.extend(messages)
        store.extend(messages)

    with patch.object(service, "create_agent") as mock:
        mock.side_effect = lambda **kwargs: _fake_agent_with_messages(
            system_prompt=kwargs.get("system_prompt"), restored=store
        )

        plain = await service.get_agent(session_id="s", user_id="u")
        _turn(plain, {"role": "user", "t": 1}, {"role": "assistant", "t": 2})

        # The mention turn: same session, different configuration → cache miss,
        # so it restores and legitimately sees the first exchange.
        mentioned = await service.get_agent(
            session_id="s", user_id="u", system_prompt="You are the mentioned Agent."
        )
        assert len(mentioned.agent.messages) == 2, "mention turn lost the prior history"
        _turn(mentioned, {"role": "user", "t": 3}, {"role": "assistant", "t": 4})

        # The next plain turn reverts the key → cache hit on the original
        # instance, which never saw turns 3-4.
        plain_again = await service.get_agent(session_id="s", user_id="u")

    assert len(plain_again.agent.messages) == 4, (
        "the plain turn cache-hit a stale instance and cannot see the mention exchange"
    )


@pytest.mark.asyncio
async def test_adoption_keeps_the_longer_history_when_the_live_instance_trails(
    mock_freshness_hash,
):
    """A live instance behind what Memory restored must not drag the new agent back.

    Separate runtime replicas share no cache, so this is not the mention case —
    it is the guard against *losing* turns if a cached instance ever trails the
    persisted conversation. Note the comparison only fires in this direction:
    compaction legitimately makes a restored list shorter than the live one, so
    "restored is shorter" is normal and must still adopt.
    """
    stale_live = [{"role": "user", "t": 1}]
    restored = [{"role": "user", "t": 1}, {"role": "assistant", "t": 2}]

    with patch.object(service, "create_agent") as mock:
        mock.side_effect = lambda **kwargs: _fake_agent_with_messages(
            system_prompt=kwargs.get("system_prompt"),
            restored=stale_live if kwargs.get("system_prompt") is None else restored,
        )
        await service.get_agent(session_id="s", user_id="u")
        ahead = await service.get_agent(
            session_id="s", user_id="u", system_prompt="different config"
        )

    assert len(ahead.agent.messages) == 2, "adoption clobbered a longer restored history"


@pytest.mark.asyncio
async def test_adoption_does_not_reach_across_sessions(mock_freshness_hash):
    """Sharing is scoped to one session id — never between two conversations."""
    with patch.object(service, "create_agent") as mock:
        mock.side_effect = lambda **kwargs: _fake_agent_with_messages(
            system_prompt=kwargs.get("system_prompt")
        )
        a = await service.get_agent(session_id="s1", user_id="u")
        a.agent.messages.extend([{"role": "user"}, {"role": "assistant"}])
        b = await service.get_agent(session_id="s2", user_id="u")

    assert b.agent.messages == [], "a different session adopted someone else's conversation"
    assert a.agent.messages is not b.agent.messages


# ============================================================
# #834 — narrowing the `extra_tools` agent-cache bypass
# ============================================================


class TestInjectedToolCacheEligibility:
    """`get_agent` used to refuse the cache for *any* per-request tool.

    That predicate stands for "this agent captured something the key doesn't
    describe" — true for spreadsheets (`assistant_id`) and Memory Spaces (the
    resolved binding), and false for the rest, which close over only session and
    user. Both are already key elements, so the cached closures are equivalent
    and the bypass was pure cost: 76% of sessions paid a full `initialize()` +
    AgentCore Memory restore every turn
    (docs/specs/agent-cache-extra-tools-bypass.md §1–§2).

    These pin the narrowed predicate, one builder at a time per that spec's §6.
    """

    @pytest.mark.asyncio
    async def test_an_artifact_turn_now_caches_and_the_next_turn_hits(
        self, mock_create_agent, mock_freshness_hash
    ):
        first = await service.get_agent(
            session_id="s", user_id="u",
            enabled_tools=["create_artifact"],
            extra_tools=[object()],
            extra_tools_key_described=True,
        )
        second = await service.get_agent(
            session_id="s", user_id="u",
            enabled_tools=["create_artifact"],
            extra_tools=[object()],
            extra_tools_key_described=True,
        )

        assert second is first, "the artifact turn rebuilt its agent"
        assert mock_create_agent.call_count == 1, "initialize() ran twice for one session"

    @pytest.mark.asyncio
    async def test_a_turn_that_captured_something_unkeyed_still_bypasses(
        self, mock_create_agent, mock_freshness_hash
    ):
        """Spreadsheet tools close over `assistant_id`, which is not in the key —
        a cached agent could answer against the wrong assistant's corpus."""
        for _ in range(2):
            await service.get_agent(
                session_id="s", user_id="u",
                enabled_tools=["analyze_spreadsheet"],
                extra_tools=[object()],
                extra_tools_key_described=False,
            )

        assert mock_create_agent.call_count == 2, "cached an agent with unkeyed captures"

    @pytest.mark.asyncio
    async def test_callers_that_do_not_declare_get_the_old_safe_behavior(
        self, mock_create_agent, mock_freshness_hash
    ):
        """The parameter defaults to False: a caller that has not reasoned about
        its closures must not be opted in by omission."""
        for _ in range(2):
            await service.get_agent(
                session_id="s", user_id="u", extra_tools=[object()]
            )

        assert mock_create_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_the_kill_switch_restores_the_blanket_bypass(
        self, mock_create_agent, mock_freshness_hash, monkeypatch
    ):
        monkeypatch.setenv("AGENT_CACHE_INJECTED_TOOLS_ENABLED", "false")
        for _ in range(2):
            await service.get_agent(
                session_id="s", user_id="u",
                enabled_tools=["create_artifact"],
                extra_tools=[object()],
                extra_tools_key_described=True,
            )

        assert mock_create_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_an_empty_flag_value_stays_enabled(
        self, mock_create_agent, mock_freshness_hash, monkeypatch
    ):
        # Workflow env vars can materialize as "" — that must not read as off.
        monkeypatch.setenv("AGENT_CACHE_INJECTED_TOOLS_ENABLED", "")
        for _ in range(2):
            await service.get_agent(
                session_id="s", user_id="u",
                enabled_tools=["create_artifact"],
                extra_tools=[object()],
                extra_tools_key_described=True,
            )

        assert mock_create_agent.call_count == 1

    @pytest.mark.asyncio
    async def test_a_partial_toolset_caller_may_read_the_slot_but_never_seed_it(
        self, mock_create_agent, mock_freshness_hash
    ):
        """The regression this narrowing would otherwise introduce.

        The MCP App dispatch paths call `get_agent` with NO injected tools but
        the same cache key as the session's real turns. Before the narrowing they
        could not collide, because artifact turns never cached. Now they share a
        slot — so if an App call seeded it first, the next real turn would
        cache-hit an agent with no `create_artifact` and silently lose the tool
        for the rest of the session.
        """
        app_call = await service.get_agent(
            session_id="s", user_id="u",
            enabled_tools=["create_artifact"],
            cache_write=False,
        )
        real_turn = await service.get_agent(
            session_id="s", user_id="u",
            enabled_tools=["create_artifact"],
            extra_tools=[object()],
            extra_tools_key_described=True,
        )

        assert real_turn is not app_call, "a real turn inherited the App path's toolless agent"

        # …and once a real turn owns the slot, the App path reuses it rather
        # than building a throwaway.
        assert await service.get_agent(
            session_id="s", user_id="u",
            enabled_tools=["create_artifact"],
            cache_write=False,
        ) is real_turn

    @pytest.mark.asyncio
    async def test_a_newly_cacheable_agent_still_shares_the_session_conversation(
        self, mock_freshness_hash
    ):
        """#741's invariant, under more concurrent siblings.

        Caching agents that never cached before means more instances alive per
        session, so the aliasing guard gets *more* load, not less — §5 hazard 2.
        """
        store: list[dict] = []

        with patch.object(service, "create_agent") as mock:
            mock.side_effect = lambda **kwargs: _fake_agent_with_messages(
                system_prompt=kwargs.get("system_prompt"), restored=store
            )

            artifact_turn = await service.get_agent(
                session_id="s", user_id="u",
                enabled_tools=["create_artifact"],
                extra_tools=[object()],
                extra_tools_key_described=True,
            )
            artifact_turn.agent.messages.append({"role": "user", "t": 1})
            store.append({"role": "user", "t": 1})

            # An `@`-mention: different system prompt → different slot, second
            # live Agent for the same session.
            mentioned = await service.get_agent(
                session_id="s", user_id="u",
                enabled_tools=["create_artifact"],
                system_prompt="You are the mentioned Agent.",
                extra_tools=[object()],
                extra_tools_key_described=True,
            )

        assert mentioned.agent.messages, "the second live Agent forked the conversation"

def test_create_cache_key_separates_client_surfaces():
    """The same conversation opened from the web app and from the terminal must
    not share a cached agent.

    The surface changes the *built* system prompt while leaving the raw
    `system_prompt` argument untouched — and that argument is `None` on the
    default path, so `prompt_hash` cannot tell the two apart. The key begins with
    `session_id`, and the TUI lists and resumes web conversations, so without this
    dimension the second client is served an agent carrying the first client's
    interface guidance: terminal users back to being told about a gear icon.
    """
    base = dict(
        session_id="s",
        user_id="u",
        enabled_tools=["t"],
        model_id="m",
        inference_params={},
        system_prompt=None,
        caching_enabled=False,
        provider="bedrock",
        freshness_hash="f",
        agent_type="chat",
    )
    web = service._create_cache_key(**base, client_surface="web")
    terminal = service._create_cache_key(**base, client_surface="terminal")

    assert web != terminal
    # An omitted surface is the browser, so pre-existing keys are unchanged.
    assert service._create_cache_key(**base) == web
