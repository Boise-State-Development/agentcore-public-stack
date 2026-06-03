"""Tests for ContextAttributionHook — per-turn system/tools/messages breakdown.

The hook computes the split once at cold start (caching the stable
system/tools tokens on the agent) and derives the messages partition each turn
from the authoritative projected total. Tool overhead deliberately absorbs the
tool-use scaffolding (full - count(system + messages, no tools)).
"""

import pytest
from strands.hooks import BeforeModelCallEvent

from agents.main_agent.session.hooks.context_attribution import (
    ContextAttributionHook,
    get_context_breakdown,
)


class FakeModel:
    """Async count_tokens returning system + per-message + (tools→overhead).

    Mirrors the real behavior the hook relies on: tool overhead is only counted
    when tool_specs are supplied (the empty-messages / no-tools baselines never
    include it).
    """

    def __init__(self, system=100, per_msg=10, tool_overhead=500, raise_on_count=False):
        self.system = system
        self.per_msg = per_msg
        self.tool_overhead = tool_overhead
        self.raise_on_count = raise_on_count
        self.calls = []

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        self.calls.append({"n_messages": len(messages), "has_tools": bool(tool_specs)})
        if self.raise_on_count:
            raise RuntimeError("count failed")
        total = self.system if system_prompt else 0
        total += len(messages) * self.per_msg
        total += self.tool_overhead if tool_specs else 0
        return total


class FakeToolRegistry:
    def __init__(self, specs):
        self._specs = specs

    def get_all_tool_specs(self):
        return self._specs


class FakeAgent:
    def __init__(self, model, messages, system_prompt="SYSTEM-PROMPT", tool_specs=None):
        self.model = model
        self.messages = messages
        self.system_prompt = system_prompt
        self._system_prompt_content = None
        self.tool_registry = FakeToolRegistry(tool_specs if tool_specs is not None else [{"name": "t"}])


def _event(agent, projected):
    return BeforeModelCallEvent(agent=agent, projected_input_tokens=projected)


def _parts(breakdown):
    return {p["key"]: p["tokens"] for p in breakdown["partitions"]}


class TestColdStart:
    @pytest.mark.asyncio
    async def test_computes_partitions_that_sum_to_total(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=650))

        bd = get_context_breakdown(agent)
        parts = _parts(bd)
        # system_only=100; no_tools(1 msg)=110; toolTokens=650-110=540; messages=650-100-540=10
        assert parts == {"system": 100, "tools": 540, "messages": 10}
        assert bd["total"] == 650
        assert sum(parts.values()) == bd["total"]

    @pytest.mark.asyncio
    async def test_makes_exactly_two_count_calls_neither_with_tools(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=650))

        assert model.calls == [
            {"n_messages": 0, "has_tools": False},  # system only
            {"n_messages": 1, "has_tools": False},  # system + messages, no tools
        ]

    @pytest.mark.asyncio
    async def test_tool_partition_absorbs_scaffolding(self):
        # full (projected) deliberately exceeds the no-tools baseline by more
        # than bare schemas would — the surplus is the scaffolding, and it must
        # land in `tools`, not `messages`.
        model = FakeModel(system=100, per_msg=10)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=900))

        parts = _parts(get_context_breakdown(agent))
        assert parts["tools"] == 900 - 110  # = 790, all of it on tools
        assert parts["messages"] == 10      # the actual single message, not inflated


class TestWarmTurn:
    @pytest.mark.asyncio
    async def test_reuses_cached_split_without_recounting(self):
        model = FakeModel(system=100, per_msg=10)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        hook = ContextAttributionHook()
        await hook._on_before_model_call(_event(agent, projected=650))
        calls_after_cold = len(model.calls)

        # Conversation grows; only the projected total changes.
        agent.messages = agent.messages + [{"role": "assistant", "content": [{"text": "ok"}]}]
        await hook._on_before_model_call(_event(agent, projected=700))

        assert len(model.calls) == calls_after_cold  # no new CountTokens calls
        parts = _parts(get_context_breakdown(agent))
        assert parts["system"] == 100
        assert parts["tools"] == 540
        assert parts["messages"] == 700 - 100 - 540  # grows with the turn


class TestProjectedUnavailable:
    @pytest.mark.asyncio
    async def test_cold_start_counts_full_with_tools_when_projected_none(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=None))

        bd = get_context_breakdown(agent)
        parts = _parts(bd)
        # full counted with tools = 100 + 10 + 500 = 610; tools = 610-110 = 500
        assert bd["total"] == 610
        assert parts == {"system": 100, "tools": 500, "messages": 10}
        # 3 calls: system only, no-tools, then full WITH tools
        assert len(model.calls) == 3
        assert model.calls[2]["has_tools"] is True

    @pytest.mark.asyncio
    async def test_warm_turn_without_projected_leaves_breakdown_untouched(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        hook = ContextAttributionHook()
        await hook._on_before_model_call(_event(agent, projected=650))
        bd_before = get_context_breakdown(agent)

        await hook._on_before_model_call(_event(agent, projected=None))
        assert get_context_breakdown(agent) == bd_before


class TestRobustness:
    @pytest.mark.asyncio
    async def test_count_failure_is_swallowed_and_yields_no_breakdown(self):
        model = FakeModel(raise_on_count=True)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        # Must not raise.
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=650))
        assert get_context_breakdown(agent) is None

    def test_get_context_breakdown_is_none_when_absent(self):
        agent = FakeAgent(FakeModel(), messages=[])
        assert get_context_breakdown(agent) is None
