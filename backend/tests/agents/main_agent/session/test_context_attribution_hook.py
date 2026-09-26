"""Tests for ContextAttributionHook — per-call system/tools/messages breakdown.

The contract these pin down: nothing the hook does sits in front of a model
call. ``BeforeModelCallEvent`` only records Strands' projection and starts one
background split measurement (four native counts of one snapshot);
``AfterModelCallEvent`` places the messages partition against the prompt the
call was billed for. Tool overhead deliberately absorbs the tool-use
scaffolding (full - count(system + messages, no tools)).
"""

import asyncio
import inspect

import pytest
from strands.hooks import AfterModelCallEvent, BeforeInvocationEvent, BeforeModelCallEvent

from agents.main_agent.session.hooks.context_attribution import (
    ContextAttributionHook,
    clear_probe_baselines,
    clear_split_memo,
    get_context_breakdown,
    get_prefix_token_split,
    get_projected_input_tokens,
)


class FakeModel:
    """Async count_tokens returning system + per-message + (tools→overhead).

    Mirrors the real behavior the hook relies on: tool overhead is only counted
    when tool_specs are supplied (the no-tools baselines never include it).
    """

    # The hook only measures a split when the model's counter is
    # authoritative (real on Bedrock Converse, a heuristic on the OpenAI
    # surfaces). This fake declares itself authoritative so the cases exercise
    # the computation; TestAuthoritativeCounterGate flips it.
    token_count_is_authoritative = True

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
        total = self.system if (system_prompt or system_prompt_content) else 0
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


HI = {"role": "user", "content": [{"text": "hi"}]}


def _before(agent, projected=None):
    return BeforeModelCallEvent(agent=agent, projected_input_tokens=projected)


def _after(agent, input_tokens, cache_read=0, cache_write=0):
    message = {
        "role": "assistant",
        "content": [{"text": "ok"}],
        "metadata": {
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": 5,
                "totalTokens": input_tokens + 5,
                "cacheReadInputTokens": cache_read,
                "cacheWriteInputTokens": cache_write,
            }
        },
    }
    return AfterModelCallEvent(
        agent=agent,
        stop_response=AfterModelCallEvent.ModelStopResponse(message=message, stop_reason="end_turn"),
    )


async def _settle(hook):
    """Let the background split measurement finish."""
    if hook._split_task is not None:
        await hook._split_task


async def _call(hook, agent, billed, projected=None):
    """One model call: before (TTFT path), the background split, after."""
    hook._on_before_model_call(_before(agent, projected))
    await _settle(hook)
    hook._on_after_model_call(_after(agent, billed))


def _parts(breakdown):
    return {p["key"]: p["tokens"] for p in breakdown["partitions"]}


class TestNothingInFrontOfTheModelCall:
    """The TTFT contract (CLAUDE.md): the BeforeModelCallEvent callback
    records and schedules; every count runs after it has returned."""

    def test_the_before_callback_is_synchronous(self):
        assert not inspect.iscoroutinefunction(ContextAttributionHook._on_before_model_call)

    @pytest.mark.asyncio
    async def test_no_count_is_made_before_the_callback_returns(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()

        hook._on_before_model_call(_before(agent, 650))
        assert model.calls == [], "a count here would be added to time to first token"

        await _settle(hook)
        assert len(model.calls) == 4

    @pytest.mark.asyncio
    async def test_one_measurement_in_flight_at_a_time(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()

        hook._on_before_model_call(_before(agent))
        first = hook._split_task
        hook._on_before_model_call(_before(agent))
        assert hook._split_task is first
        await _settle(hook)
        assert len(model.calls) == 4

    @pytest.mark.asyncio
    async def test_the_measurement_counts_the_snapshot_not_the_live_list(self):
        """Strands appends to agent.messages while the counts run; the tools
        residual must come from two counts of the same conversation."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()

        hook._on_before_model_call(_before(agent))
        agent.messages.append({"role": "assistant", "content": [{"text": "streamed"}]})
        await _settle(hook)

        assert {c["n_messages"] for c in model.calls[2:]} == {1}
        assert get_prefix_token_split(agent) == {"system": 100, "tools": 500}


class TestColdStart:
    @pytest.mark.asyncio
    async def test_computes_partitions_that_sum_to_the_billed_total(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        await _call(ContextAttributionHook(), agent, billed=650)

        bd = get_context_breakdown(agent)
        # system = (probe+system 110) - (probe 10) = 100; tools = full 610 - no-tools 110 = 500;
        # messages = billed 650 - 100 - 500 = 50.
        assert _parts(bd) == {"system": 100, "tools": 500, "messages": 50}
        assert bd["total"] == 650
        assert sum(_parts(bd).values()) == bd["total"]

    @pytest.mark.asyncio
    async def test_the_billed_total_includes_cache_reads_and_writes(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        hook._on_before_model_call(_before(agent))
        await _settle(hook)
        hook._on_after_model_call(_after(agent, 50, cache_read=400, cache_write=200))

        assert get_context_breakdown(agent)["total"] == 650

    @pytest.mark.asyncio
    async def test_makes_exactly_four_counts_none_empty_only_the_full_one_with_tools(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[HI])
        await _call(ContextAttributionHook(), agent, billed=650)

        assert model.calls == [
            {"n_messages": 1, "has_tools": False},  # probe only (per-model baseline)
            {"n_messages": 1, "has_tools": False},  # probe + system
            {"n_messages": 1, "has_tools": False},  # system + messages, no tools
            {"n_messages": 1, "has_tools": True},  # system + messages + tools
        ]
        # Bedrock rejects an empty conversation; the hook must never send one.
        assert all(c["n_messages"] >= 1 for c in model.calls)

    @pytest.mark.asyncio
    async def test_tool_partition_absorbs_scaffolding(self):
        # Whatever the full count adds over the no-tools count — schemas plus
        # the scaffolding Bedrock injects — lands in `tools`, not `messages`.
        model = FakeModel(system=100, per_msg=10, tool_overhead=790)
        agent = FakeAgent(model, messages=[HI])
        await _call(ContextAttributionHook(), agent, billed=900)

        parts = _parts(get_context_breakdown(agent))
        assert parts["tools"] == 790
        assert parts["messages"] == 10

    @pytest.mark.asyncio
    async def test_the_split_is_published_even_if_the_call_answered_first(self):
        """A fast model call can finish before the counts: the breakdown
        appears when the split lands, against that call's billed prompt."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()

        hook._on_before_model_call(_before(agent))
        hook._on_after_model_call(_after(agent, 650))
        assert get_context_breakdown(agent) is None

        await _settle(hook)
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 500, "messages": 50}


class TestLaterCalls:
    @pytest.mark.asyncio
    async def test_reuses_the_split_without_recounting(self):
        model = FakeModel(system=100, per_msg=10)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        await _call(hook, agent, billed=650)
        calls_after_cold = len(model.calls)

        agent.messages = agent.messages + [{"role": "assistant", "content": [{"text": "ok"}]}]
        await _call(hook, agent, billed=700)

        assert len(model.calls) == calls_after_cold  # no new CountTokens calls
        parts = _parts(get_context_breakdown(agent))
        assert parts["system"] == 100
        assert parts["tools"] == 500
        assert parts["messages"] == 700 - 100 - 500  # grows with the conversation

    @pytest.mark.asyncio
    async def test_a_call_without_usage_leaves_the_breakdown_untouched(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        await _call(hook, agent, billed=650)
        before = get_context_breakdown(agent)

        hook._on_before_model_call(_before(agent))
        hook._on_after_model_call(AfterModelCallEvent(agent=agent, exception=RuntimeError("boom")))
        assert get_context_breakdown(agent) == before


class TestProjectedInputForInterruptedTurns:
    """An interrupted call never reports usage, so the persisted turn needs
    the best pre-call figure there is — and none rather than a bad one."""

    @pytest.mark.asyncio
    async def test_the_native_count_of_the_request_is_preferred(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        hook._on_before_model_call(_before(agent, projected=9_999))
        await _settle(hook)

        # The call was cut: no AfterModelCallEvent, messages unchanged.
        assert get_projected_input_tokens(agent) == 610

    def test_a_usage_anchored_projection_is_used(self):
        model = FakeModel()
        warm = [HI, {"role": "assistant", "content": [{"text": "ok"}], "metadata": {"usage": {"inputTokens": 1}}}, HI]
        agent = FakeAgent(model, messages=warm)
        ContextAttributionHook()._on_before_model_call(_before(agent, projected=1_234))

        assert get_projected_input_tokens(agent) == 1_234

    def test_a_cold_projection_is_all_heuristic_so_none(self):
        model = FakeModel()
        model.token_count_is_authoritative = False  # no split to take a native count from
        agent = FakeAgent(model, messages=[HI])
        ContextAttributionHook()._on_before_model_call(_before(agent, projected=1_234))

        assert get_projected_input_tokens(agent) is None

    @pytest.mark.asyncio
    async def test_the_snapshot_count_is_not_reused_once_the_conversation_moved(self):
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        await _call(hook, agent, billed=650)

        agent.messages = agent.messages + [{"role": "assistant", "content": [{"text": "ok"}]}, HI]
        hook._on_before_model_call(_before(agent, projected=720))
        assert get_projected_input_tokens(agent) is None  # no usage metadata on that fake assistant turn


class TestRobustness:
    @pytest.mark.asyncio
    async def test_count_failure_is_swallowed_and_yields_no_breakdown(self):
        model = FakeModel(raise_on_count=True)
        agent = FakeAgent(model, messages=[HI])
        # Must not raise — neither the callback nor the background task.
        await _call(ContextAttributionHook(), agent, billed=650)
        assert get_context_breakdown(agent) is None

    def test_get_context_breakdown_is_none_when_absent(self):
        agent = FakeAgent(FakeModel(), messages=[])
        assert get_context_breakdown(agent) is None

    def test_before_callback_without_a_running_loop_does_not_raise(self):
        agent = FakeAgent(FakeModel(), messages=[HI])
        ContextAttributionHook()._on_before_model_call(_before(agent))
        assert get_context_breakdown(agent) is None


class TestInlineAttachmentGuard:
    """``toolTokens`` is a residual, so any disagreement between its two sides
    about how a content block is counted lands wholly in it.

    Measured on dev 2026-09-16 (session ``61de2256``), when one side was
    Strands' projection: ``toolTokens`` of 106,756 where the session's real
    tools prefix was 12,516 — the entire document attributed to tools. Both
    sides are now native counts of one snapshot, but the split stays deferred
    while inline bytes are in context until a document turn proves it safe.
    """

    def _doc_message(self):
        return {
            "role": "user",
            "content": [
                {"text": "what does it say?"},
                {"document": {"format": "pdf", "name": "d_pdf", "source": {"bytes": b"%PDF-1.4"}}},
            ],
        }

    @pytest.mark.asyncio
    async def test_no_split_is_computed_while_a_document_is_inline(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[self._doc_message()])
        await _call(ContextAttributionHook(), agent, billed=100_000)

        assert get_context_breakdown(agent) is None, "a contaminated split must not be published"
        assert model.calls == [], "and it must not pay for CountTokens to compute one"

    @pytest.mark.asyncio
    async def test_an_image_counts_too(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[{
            "role": "user",
            "content": [{"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}}],
        }])
        await _call(ContextAttributionHook(), agent, billed=50_000)
        assert get_context_breakdown(agent) is None

    @pytest.mark.asyncio
    async def test_a_digest_is_not_an_attachment_so_the_split_is_taken(self):
        """The offload turns the document into text, which counts normally —
        so an attachment session still gets ``prefixTokens`` from turn 2."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[{
            "role": "user",
            "content": [{"text": '<document-digest name="d.pdf" upload_id="u1" pages="60">'}],
        }])
        await _call(ContextAttributionHook(), agent, billed=650)
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 500, "messages": 50}

    @pytest.mark.asyncio
    async def test_a_later_clean_call_computes_the_split(self):
        """Deferred, not abandoned: the same agent takes the split once the
        attachment has left the live context."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[self._doc_message()])
        hook = ContextAttributionHook()
        await _call(hook, agent, billed=100_000)
        assert get_context_breakdown(agent) is None

        agent.messages = [{"role": "user", "content": [{"text": "follow-up"}]}]
        await _call(hook, agent, billed=650)
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 500, "messages": 50}

    @pytest.mark.asyncio
    async def test_a_measured_split_is_still_used_when_a_document_arrives_later(self):
        """The guard only defers the *measurement*. An agent that already has a
        trustworthy split keeps reporting against it."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[HI])
        hook = ContextAttributionHook()
        await _call(hook, agent, billed=650)

        agent.messages = [HI, self._doc_message()]
        await _call(hook, agent, billed=95_000)
        parts = _parts(get_context_breakdown(agent))
        assert parts["system"] == 100 and parts["tools"] == 500
        assert parts["messages"] == 95_000 - 600, "the document lands in messages, where it belongs"


class TestSessionSplitMemo:
    """A rebuilt Agent for the same session + configuration adopts the split
    its predecessor measured instead of paying for the counts again.

    Sessions whose injected tools keep them out of the agent cache rebuild
    their Agent every turn (docs/specs/load-test-assessment-2026-09.md §1
    fix 1)."""

    def setup_method(self):
        clear_split_memo()

    def teardown_method(self):
        clear_split_memo()

    @pytest.mark.asyncio
    async def test_second_agent_for_same_session_makes_no_count_calls(self):
        first_model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        first = FakeAgent(first_model, messages=[HI])
        await _call(ContextAttributionHook(session_id="s1"), first, billed=650)
        assert len(first_model.calls) == 4

        second_model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        second = FakeAgent(second_model, messages=[HI, {"role": "assistant", "content": [{"text": "yo"}]}])
        await _call(ContextAttributionHook(session_id="s1"), second, billed=660)

        assert second_model.calls == []
        assert _parts(get_context_breakdown(second)) == {"system": 100, "tools": 500, "messages": 60}

    @pytest.mark.asyncio
    async def test_different_session_recounts(self):
        await _call(ContextAttributionHook(session_id="s1"), FakeAgent(FakeModel(), messages=[HI]), billed=650)
        other_model = FakeModel()
        await _call(ContextAttributionHook(session_id="s2"), FakeAgent(other_model, messages=[HI]), billed=650)
        assert len(other_model.calls) == 4

    @pytest.mark.asyncio
    async def test_changed_tools_or_prompt_recounts(self):
        await _call(ContextAttributionHook(session_id="s1"), FakeAgent(FakeModel(), messages=[HI]), billed=650)

        tools_changed = FakeModel()
        await _call(
            ContextAttributionHook(session_id="s1"),
            FakeAgent(tools_changed, messages=[HI], tool_specs=[{"name": "t"}, {"name": "u"}]),
            billed=700,
        )
        assert len(tools_changed.calls) == 4

        prompt_changed = FakeModel()
        await _call(
            ContextAttributionHook(session_id="s1"),
            FakeAgent(prompt_changed, messages=[HI], system_prompt="OTHER"),
            billed=650,
        )
        assert len(prompt_changed.calls) == 4

    @pytest.mark.asyncio
    async def test_no_session_id_means_instance_only(self):
        await _call(ContextAttributionHook(), FakeAgent(FakeModel(), messages=[HI]), billed=650)
        again = FakeModel()
        await _call(ContextAttributionHook(), FakeAgent(again, messages=[HI]), billed=650)
        assert len(again.calls) == 4

    @pytest.mark.asyncio
    async def test_a_deferred_split_is_not_memoised(self):
        # Inline attachment → the split is skipped, so nothing must be stored
        # for a later clean agent to adopt.
        pdf = {"role": "user", "content": [{"document": {"format": "pdf", "name": "d", "source": {"bytes": b"%PDF"}}}]}
        skipped = FakeModel()
        await _call(ContextAttributionHook(session_id="s1"), FakeAgent(skipped, messages=[pdf]), billed=650)
        assert skipped.calls == []

        clean = FakeModel()
        await _call(ContextAttributionHook(session_id="s1"), FakeAgent(clean, messages=[HI]), billed=650)
        assert len(clean.calls) == 4

    def test_memo_is_bounded(self):
        from agents.main_agent.session.hooks import context_attribution as ca

        for i in range(ca._SPLIT_MEMO_MAX + 50):
            ca._memo_put((f"s{i}", "p", "t"), {"systemTokens": 1, "toolTokens": 1})
        assert len(ca._split_memo) == ca._SPLIT_MEMO_MAX
        assert ca._memo_get(("s0", "p", "t")) is None
        assert ca._memo_get((f"s{ca._SPLIT_MEMO_MAX + 49}", "p", "t")) is not None


class TestProbeBaseline:
    """The system prompt is counted against a fixed probe user message because
    Bedrock refuses an empty conversation. The probe's own weight is a
    per-model constant, measured once per model id per process."""

    def setup_method(self):
        clear_probe_baselines()

    def teardown_method(self):
        clear_probe_baselines()

    class ConfiguredModel(FakeModel):
        def __init__(self, model_id, **kw):
            super().__init__(**kw)
            self.config = {"model_id": model_id}

    @pytest.mark.asyncio
    async def test_probe_weight_is_measured_once_per_model_id(self):
        first = self.ConfiguredModel("us.anthropic.x", system=100, per_msg=10)
        await _call(ContextAttributionHook(), FakeAgent(first, messages=[HI]), billed=650)
        assert len(first.calls) == 4

        second = self.ConfiguredModel("us.anthropic.x", system=100, per_msg=10)
        second_agent = FakeAgent(second, messages=[HI])
        await _call(ContextAttributionHook(), second_agent, billed=650)
        # Baseline reused: probe+system, no-tools and full only.
        assert len(second.calls) == 3
        assert _parts(get_context_breakdown(second_agent))["system"] == 100

        other = self.ConfiguredModel("us.anthropic.y", system=100, per_msg=10)
        await _call(ContextAttributionHook(), FakeAgent(other, messages=[HI]), billed=650)
        assert len(other.calls) == 4

    @pytest.mark.asyncio
    async def test_system_partition_is_the_difference_not_the_probe(self):
        # A probe that weighs 24 tokens on its own must not leak into system.
        model = self.ConfiguredModel("m", system=7, per_msg=24)
        agent = FakeAgent(model, messages=[HI])
        await _call(ContextAttributionHook(), agent, billed=600)
        assert _parts(get_context_breakdown(agent))["system"] == 7

    @pytest.mark.asyncio
    async def test_a_model_without_a_config_counts_the_probe_each_time(self):
        a, b = FakeModel(), FakeModel()
        await _call(ContextAttributionHook(), FakeAgent(a, messages=[HI]), billed=650)
        await _call(ContextAttributionHook(), FakeAgent(b, messages=[HI]), billed=650)
        assert len(a.calls) == len(b.calls) == 4


class TestAuthoritativeCounterGate:
    """No split at all where there is no native counter.

    On the OpenAI surfaces (``bedrock-responses``, ``mantle``) ``count_tokens``
    degrades to chars/4, and a residual built from it is tools PLUS the
    estimator disagreement — measured live on Kimi K3 as 13,967 then 7,145
    for a byte-identical tool set.

    Absent `prefixTokens` reads "not tracked"; a wrong one corrupts every share
    computed from it. Nothing else is lost — cost and the context meter come
    from provider-reported usage, not from this split.
    """

    def _heuristic_agent(self):
        model = FakeModel()
        model.token_count_is_authoritative = False
        return model, FakeAgent(model, [HI])

    @pytest.mark.asyncio
    async def test_no_breakdown_when_the_counter_is_a_heuristic(self):
        model, agent = self._heuristic_agent()
        await _call(ContextAttributionHook(session_id="s1"), agent, billed=1000)
        assert get_context_breakdown(agent) is None

    @pytest.mark.asyncio
    async def test_it_does_not_even_start_a_measurement(self):
        model, agent = self._heuristic_agent()
        hook = ContextAttributionHook(session_id="s1")
        await _call(hook, agent, billed=1000)
        assert hook._split_task is None
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_nothing_is_written_for_the_cost_row_either(self):
        """`prefixTokens` reads the same split, so it must be absent too."""
        model, agent = self._heuristic_agent()
        await _call(ContextAttributionHook(session_id="s1"), agent, billed=1000)
        assert get_prefix_token_split(agent) is None

    @pytest.mark.asyncio
    async def test_the_authoritative_path_is_unchanged(self):
        agent = FakeAgent(FakeModel(), [HI])
        await _call(ContextAttributionHook(session_id="s1"), agent, billed=1000)
        assert get_context_breakdown(agent) is not None

    def test_it_classifies_the_real_model_classes(self):
        """The gate has to hold against the actual classes, not just the fake.

        Constructed offline — neither touches AWS at build time.
        """
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
        from agents.main_agent.session.hooks.context_attribution import (
            _token_count_is_authoritative,
        )
        from apis.shared.models.bedrock_responses import build_bedrock_responses_model

        # Native counting on, as `BedrockModelConfig` always builds it.
        converse = CountTokensBedrockModel(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            region_name="us-west-2",
            use_native_token_count=True,
        )
        responses = build_bedrock_responses_model("us.moonshotai.kimi-k3", region="us-west-2")

        assert _token_count_is_authoritative(converse) is True
        assert _token_count_is_authoritative(responses) is False

    def test_an_explicit_declaration_beats_the_isinstance_check(self):
        """The extension point for a transport that later gains a real counter."""
        from agents.main_agent.session.hooks.context_attribution import (
            _token_count_is_authoritative,
        )
        from apis.shared.models.bedrock_responses import build_bedrock_responses_model

        responses = build_bedrock_responses_model("us.moonshotai.kimi-k3", region="us-west-2")
        assert _token_count_is_authoritative(responses) is False

        responses.token_count_is_authoritative = True
        assert _token_count_is_authoritative(responses) is True


class TestHeuristicCountsNeverBecomeASplit:
    """Being a ``BedrockModel`` does not make a count native. Claude Sonnet 5
    has no CountTokens at all, and a throttle or failure has no native answer
    — the heuristic charges JSON at chars/2, so prod Sonnet 5 once recorded
    ``tools = 13,606`` against a whole billed prompt of 12,909. The split must
    come from native counts or not exist."""

    @staticmethod
    def _converse(skip_list_ids=()):
        from strands.models import bedrock as strands_bedrock

        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

        strands_bedrock._SKIP_COUNT_TOKENS_MODELS.update(skip_list_ids)
        return CountTokensBedrockModel(
            model_id="global.anthropic.claude-sonnet-5",
            region_name="us-west-2",
            use_native_token_count=True,
            native_projection=False,
        )

    @pytest.fixture(autouse=True)
    def _clean(self):
        from strands.models import bedrock as strands_bedrock

        clear_split_memo()
        clear_probe_baselines()
        strands_bedrock._SKIP_COUNT_TOKENS_MODELS.clear()
        yield
        strands_bedrock._SKIP_COUNT_TOKENS_MODELS.clear()
        clear_probe_baselines()

    def test_a_model_bedrock_will_not_count_is_not_authoritative(self):
        from agents.main_agent.session.hooks.context_attribution import _token_count_is_authoritative

        assert _token_count_is_authoritative(self._converse()) is True
        # What native_count_tokens records after Bedrock answers "doesn't
        # support counting tokens" (or AccessDenied) for the id it sent — the
        # base id, with the `global.` profile prefix stripped.
        assert _token_count_is_authoritative(self._converse({"anthropic.claude-sonnet-5"})) is False

    def test_native_counting_off_is_not_authoritative(self):
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
        from agents.main_agent.session.hooks.context_attribution import _token_count_is_authoritative

        model = CountTokensBedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", region_name="us-west-2")
        assert _token_count_is_authoritative(model) is False

    @pytest.mark.asyncio
    async def test_prod_sonnet_5_shape_records_no_split_and_no_breakdown(self):
        """The real class, counting against a Bedrock that refuses the model:
        no count is native, so nothing is recorded — the cost row reads "not
        tracked" instead of a tools figure bigger than the prompt — and the
        model goes on the skip list, so later agents don't try."""
        from strands.models import bedrock as strands_bedrock

        model = self._converse()

        class Refusing:
            def count_tokens(self, **kwargs):
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "The provided model doesn't support counting tokens."}},
                    "CountTokens",
                )

        model._count_client = Refusing()
        tool_specs = [{"name": "t", "description": "d" * 4000, "inputSchema": {"json": {"type": "object"}}}]
        agent = FakeAgent(model, [HI], tool_specs=tool_specs)
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))

        await _call(hook, agent, billed=12_909)

        assert get_prefix_token_split(agent) is None
        assert get_context_breakdown(agent) is None
        assert "anthropic.claude-sonnet-5" in strands_bedrock._SKIP_COUNT_TOKENS_MODELS

    @pytest.mark.asyncio
    async def test_a_count_that_falls_back_mid_split_taints_it_once_per_turn(self):
        model = CountingFake()
        agent = FakeAgent(model, [HI])
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        model.fall_back_on_call = 2  # the system-with-probe count is throttled

        await _call(hook, agent, billed=1000)
        assert get_context_breakdown(agent) is None
        calls_after_first = len(model.calls)

        # Same turn, next call: no second attempt against a throttled counter.
        await _call(hook, agent, billed=1100)
        assert len(model.calls) == calls_after_first
        assert get_context_breakdown(agent) is None

        # Next turn: clean counts, so the split is taken.
        model.fall_back_on_call = None
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        await _call(hook, agent, billed=1200)
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 500, "messages": 600}

    @pytest.mark.asyncio
    async def test_a_heuristic_projection_does_not_matter(self):
        """Strands' projection is the heuristic by design now; the split
        never reads it, so a fallback there taints nothing."""
        model = CountingFake()
        agent = FakeAgent(model, [HI])
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        model.heuristic_count_fallbacks += 1  # the projection fell back

        await _call(hook, agent, billed=1000, projected=999_999)

        assert get_prefix_token_split(agent) == {"system": 100, "tools": 500}

    @pytest.mark.asyncio
    async def test_a_heuristic_probe_weight_is_not_memoised(self):
        from agents.main_agent.session.hooks.context_attribution import _native_counter, _probe_baseline

        model = CountingFake()
        model.config = {"model_id": "m-1"}
        counter = _native_counter(model)
        model.fall_back_on_call = 1
        assert await _probe_baseline(model, counter) is None
        model.fall_back_on_call = None
        await _probe_baseline(model, counter)
        await _probe_baseline(model, counter)
        assert len(model.calls) == 2  # the heuristic answer was not kept; the native one was


class CountingFake(FakeModel):
    """A fake that keeps the real model's fallback counter; call N (1-based)
    can be made to "fall back"."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.heuristic_count_fallbacks = 0
        self.fall_back_on_call = None

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        result = await super().count_tokens(messages, tool_specs, system_prompt, system_prompt_content)
        if self.fall_back_on_call is not None and len(self.calls) == self.fall_back_on_call:
            self.heuristic_count_fallbacks += 1
        return result
