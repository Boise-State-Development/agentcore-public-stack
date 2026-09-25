"""Tests for ContextAttributionHook — per-turn system/tools/messages breakdown.

The hook computes the split once at cold start (caching the stable
system/tools tokens on the agent) and derives the messages partition each turn
from the authoritative projected total. Tool overhead deliberately absorbs the
tool-use scaffolding (full - count(system + messages, no tools)).
"""

import pytest
from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent

from agents.main_agent.session.hooks.context_attribution import (
    ContextAttributionHook,
    clear_probe_baselines,
    clear_split_memo,
    get_context_breakdown,
)


class FakeModel:
    """Async count_tokens returning system + per-message + (tools→overhead).

    Mirrors the real behavior the hook relies on: tool overhead is only counted
    when tool_specs are supplied (the empty-messages / no-tools baselines never
    include it).
    """

    # The hook only computes a split when the model's counter is authoritative
    # (real on Bedrock Converse, a heuristic on the OpenAI surfaces). This fake
    # declares itself authoritative so the existing cases still exercise the
    # computation; TestAuthoritativeCounterGate flips it.
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
    async def test_makes_exactly_three_count_calls_none_with_tools_and_none_empty(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=650))

        assert model.calls == [
            {"n_messages": 1, "has_tools": False},  # probe only (per-model baseline)
            {"n_messages": 1, "has_tools": False},  # probe + system
            {"n_messages": 1, "has_tools": False},  # system + messages, no tools
        ]
        # Bedrock rejects an empty conversation; the hook must never send one.
        assert all(c["n_messages"] >= 1 for c in model.calls)

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
        # 4 calls: probe baseline, probe+system, no-tools, then full WITH tools
        assert len(model.calls) == 4
        assert model.calls[3]["has_tools"] is True

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


class TestInlineAttachmentGuard:
    """``toolTokens`` is a residual between two independently sourced counts
    (``full`` from Strands' projection, ``no_tools`` from our CountTokens call),
    so any disagreement about how a content block is counted lands wholly in it.

    Measured on dev 2026-09-16 (session ``61de2256``): a call reported
    ``toolTokens`` of 106,756 where the session's real tools prefix was 12,516 —
    a 94,240 difference against a document measured at ~94,485, i.e. the entire
    document attributed to tools. The split is therefore not computed while
    inline bytes are in context.
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
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=100_000))

        assert get_context_breakdown(agent) is None, "a contaminated split must not be published"
        assert model.calls == [], "and it must not pay for CountTokens to compute one"

    @pytest.mark.asyncio
    async def test_an_image_counts_too(self):
        model = FakeModel()
        agent = FakeAgent(model, messages=[{
            "role": "user",
            "content": [{"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}}],
        }])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=50_000))
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
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=650))
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 540, "messages": 10}

    @pytest.mark.asyncio
    async def test_a_later_clean_turn_computes_the_split(self):
        """Deferred, not abandoned: the same agent takes the split once the
        attachment has left the live context."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[self._doc_message()])
        hook = ContextAttributionHook()
        await hook._on_before_model_call(_event(agent, projected=100_000))
        assert get_context_breakdown(agent) is None

        agent.messages = [{"role": "user", "content": [{"text": "follow-up"}]}]
        await hook._on_before_model_call(_event(agent, projected=650))
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 540, "messages": 10}

    @pytest.mark.asyncio
    async def test_a_cached_split_is_still_used_when_a_document_arrives_later(self):
        """The guard only defers the *computation*. An agent that already has a
        trustworthy split keeps reporting against it."""
        model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        hook = ContextAttributionHook()
        await hook._on_before_model_call(_event(agent, projected=650))

        agent.messages = [{"role": "user", "content": [{"text": "hi"}]}, self._doc_message()]
        await hook._on_before_model_call(_event(agent, projected=95_000))
        parts = _parts(get_context_breakdown(agent))
        assert parts["system"] == 100 and parts["tools"] == 540
        assert parts["messages"] == 95_000 - 640, "the document lands in messages, where it belongs"


class TestSessionSplitMemo:
    """A rebuilt Agent for the same session + configuration adopts the split
    its predecessor measured instead of paying two CountTokens calls again.

    Sessions whose injected tools keep them out of the agent cache rebuild
    their Agent every turn; without the memo that was 2 extra counts per turn
    (docs/specs/load-test-assessment-2026-09.md §1 fix 1)."""

    def setup_method(self):
        clear_split_memo()

    def teardown_method(self):
        clear_split_memo()

    @pytest.mark.asyncio
    async def test_second_agent_for_same_session_makes_no_count_calls(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        first_model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        first = FakeAgent(first_model, messages=list(msgs))
        await ContextAttributionHook(session_id="s1")._on_before_model_call(_event(first, projected=650))
        assert len(first_model.calls) == 3

        second_model = FakeModel(system=100, per_msg=10, tool_overhead=500)
        second = FakeAgent(second_model, messages=list(msgs) + [{"role": "assistant", "content": [{"text": "yo"}]}])
        await ContextAttributionHook(session_id="s1")._on_before_model_call(_event(second, projected=660))

        assert second_model.calls == []
        assert _parts(get_context_breakdown(second)) == {"system": 100, "tools": 540, "messages": 20}

    @pytest.mark.asyncio
    async def test_different_session_recounts(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(FakeModel(), messages=list(msgs)), projected=650)
        )
        other_model = FakeModel()
        await ContextAttributionHook(session_id="s2")._on_before_model_call(
            _event(FakeAgent(other_model, messages=list(msgs)), projected=650)
        )
        assert len(other_model.calls) == 3

    @pytest.mark.asyncio
    async def test_changed_tools_or_prompt_recounts(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(FakeModel(), messages=list(msgs)), projected=650)
        )

        tools_changed = FakeModel()
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(tools_changed, messages=list(msgs), tool_specs=[{"name": "t"}, {"name": "u"}]), projected=700)
        )
        assert len(tools_changed.calls) == 3

        prompt_changed = FakeModel()
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(prompt_changed, messages=list(msgs), system_prompt="OTHER"), projected=650)
        )
        assert len(prompt_changed.calls) == 3

    @pytest.mark.asyncio
    async def test_no_session_id_means_instance_only(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        await ContextAttributionHook()._on_before_model_call(
            _event(FakeAgent(FakeModel(), messages=list(msgs)), projected=650)
        )
        again = FakeModel()
        await ContextAttributionHook()._on_before_model_call(
            _event(FakeAgent(again, messages=list(msgs)), projected=650)
        )
        assert len(again.calls) == 3

    @pytest.mark.asyncio
    async def test_a_deferred_split_is_not_memoised(self):
        # Inline attachment → the split is skipped, so nothing must be stored
        # for a later clean agent to adopt.
        pdf = {"role": "user", "content": [{"document": {"format": "pdf", "name": "d", "source": {"bytes": b"%PDF"}}}]}
        skipped = FakeModel()
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(skipped, messages=[pdf]), projected=650)
        )
        assert skipped.calls == []

        clean = FakeModel()
        await ContextAttributionHook(session_id="s1")._on_before_model_call(
            _event(FakeAgent(clean, messages=[{"role": "user", "content": [{"text": "hi"}]}]), projected=650)
        )
        assert len(clean.calls) == 3

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
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        first = self.ConfiguredModel("us.anthropic.x", system=100, per_msg=10)
        await ContextAttributionHook()._on_before_model_call(_event(FakeAgent(first, messages=list(msgs)), projected=650))
        assert len(first.calls) == 3

        second = self.ConfiguredModel("us.anthropic.x", system=100, per_msg=10)
        second_agent = FakeAgent(second, messages=list(msgs))
        await ContextAttributionHook()._on_before_model_call(_event(second_agent, projected=650))
        # Baseline reused: probe+system and no-tools only.
        assert len(second.calls) == 2
        assert _parts(get_context_breakdown(second_agent))["system"] == 100

        other = self.ConfiguredModel("us.anthropic.y", system=100, per_msg=10)
        await ContextAttributionHook()._on_before_model_call(_event(FakeAgent(other, messages=list(msgs)), projected=650))
        assert len(other.calls) == 3

    @pytest.mark.asyncio
    async def test_system_partition_is_the_difference_not_the_probe(self):
        # A probe that weighs 24 tokens on its own must not leak into system.
        model = self.ConfiguredModel("m", system=7, per_msg=24)
        agent = FakeAgent(model, messages=[{"role": "user", "content": [{"text": "hi"}]}])
        await ContextAttributionHook()._on_before_model_call(_event(agent, projected=600))
        assert _parts(get_context_breakdown(agent))["system"] == 7

    @pytest.mark.asyncio
    async def test_a_model_without_a_config_counts_the_probe_each_time(self):
        msgs = [{"role": "user", "content": [{"text": "hi"}]}]
        a, b = FakeModel(), FakeModel()
        await ContextAttributionHook()._on_before_model_call(_event(FakeAgent(a, messages=list(msgs)), projected=650))
        await ContextAttributionHook()._on_before_model_call(_event(FakeAgent(b, messages=list(msgs)), projected=650))
        assert len(a.calls) == len(b.calls) == 3


class TestAuthoritativeCounterGate:
    """No split at all where ``count_tokens`` is a heuristic.

    ``toolTokens`` is ``projected_input_tokens - count_tokens(no tools)``. Those
    are the same estimator only on Bedrock Converse. On the OpenAI surfaces
    (``bedrock-responses``, ``mantle``) ``count_tokens`` degrades to chars/4
    while the projection is anchored on real usage, so the residual is tools
    PLUS the estimator disagreement — measured live on Kimi K3 as 13,967 then
    7,145 for a byte-identical tool set.

    Absent `prefixTokens` reads "not tracked"; a wrong one corrupts every share
    computed from it. Nothing else is lost — cost and the context meter come
    from provider-reported usage, not from this split.
    """

    @pytest.mark.asyncio
    async def test_no_breakdown_when_the_counter_is_a_heuristic(self):
        model = FakeModel()
        model.token_count_is_authoritative = False
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])

        await ContextAttributionHook(session_id="s1")._compute(_event(agent, 1000))

        assert get_context_breakdown(agent) is None

    @pytest.mark.asyncio
    async def test_it_does_not_even_spend_the_count_calls(self):
        """The guard is before the two CountTokens calls, not after — a wrong
        number we then discard would still have cost two round trips."""
        model = FakeModel()
        model.token_count_is_authoritative = False
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])

        await ContextAttributionHook(session_id="s1")._compute(_event(agent, 1000))

        assert model.calls == []

    @pytest.mark.asyncio
    async def test_nothing_is_written_for_the_cost_row_either(self):
        """`prefixTokens` reads the same split, so it must be absent too."""
        from agents.main_agent.session.hooks.context_attribution import (
            get_prefix_token_split,
        )

        model = FakeModel()
        model.token_count_is_authoritative = False
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])

        await ContextAttributionHook(session_id="s1")._compute(_event(agent, 1000))

        assert get_prefix_token_split(agent) is None

    @pytest.mark.asyncio
    async def test_the_authoritative_path_is_unchanged(self):
        model = FakeModel()
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])

        await ContextAttributionHook(session_id="s1")._compute(_event(agent, 1000))

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
    has no CountTokens at all, and a throttle or failure falls back per call,
    to a heuristic that charges JSON at chars/2 — prod Sonnet 5 recorded
    ``tools = 13,606`` against a whole billed prompt of 12,909. The split must
    come from native counts or not exist."""

    @staticmethod
    def _converse(skip_list_ids=()):
        from strands.models import bedrock as strands_bedrock

        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

        strands_bedrock._SKIP_COUNT_TOKENS_MODELS.update(skip_list_ids)
        return CountTokensBedrockModel(
            model_id="global.anthropic.claude-sonnet-5", region_name="us-west-2", use_native_token_count=True
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
        # What count_tokens records after Bedrock answers "doesn't support
        # counting tokens" (or AccessDenied) for the id it sent.
        assert _token_count_is_authoritative(self._converse({"global.anthropic.claude-sonnet-5"})) is False

    def test_native_counting_off_is_not_authoritative(self):
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
        from agents.main_agent.session.hooks.context_attribution import _token_count_is_authoritative

        model = CountTokensBedrockModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", region_name="us-west-2")
        assert _token_count_is_authoritative(model) is False

    @pytest.mark.asyncio
    async def test_prod_sonnet_5_shape_records_no_split_and_no_breakdown(self):
        """The real class, counting against a Bedrock that refuses the model:
        every count is the heuristic, so nothing is recorded — the cost row
        reads "not tracked" instead of a tools figure bigger than the prompt."""
        from agents.main_agent.session.hooks.context_attribution import get_prefix_token_split

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
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}], tool_specs=tool_specs)
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        # Strands' projection runs first and is the call that fails.
        projected = await model.count_tokens(agent.messages, tool_specs=tool_specs, system_prompt="SYSTEM-PROMPT")

        await hook._on_before_model_call(_event(agent, projected))

        assert get_prefix_token_split(agent) is None
        assert get_context_breakdown(agent) is None

    @pytest.mark.asyncio
    async def test_a_count_that_falls_back_mid_split_taints_it_once_per_turn(self):
        model = CountingFake()
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        model.fall_back_on_call = 2  # the system-with-probe count is throttled

        await hook._on_before_model_call(_event(agent, 1000))
        assert get_context_breakdown(agent) is None
        calls_after_first = len(model.calls)

        # Same turn, next call: no second attempt against a throttled counter.
        await hook._on_before_model_call(_event(agent, 1100))
        assert len(model.calls) == calls_after_first
        assert get_context_breakdown(agent) is None

        # Next turn: clean counts, so the split is taken.
        model.fall_back_on_call = None
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        await hook._on_before_model_call(_event(agent, 1200))
        assert _parts(get_context_breakdown(agent)) == {"system": 100, "tools": 1200 - 110, "messages": 10}

    @pytest.mark.asyncio
    async def test_a_heuristic_projection_is_not_split(self):
        """Strands counts the projection after the previous call's
        attribution ended; a fallback there means `full` is an estimate."""
        model = CountingFake()
        agent = FakeAgent(model, [{"role": "user", "content": [{"text": "hi"}]}])
        hook = ContextAttributionHook(session_id="s1")
        hook._on_turn_start(BeforeInvocationEvent(agent=agent))
        model.heuristic_count_fallbacks += 1  # the projection fell back

        await hook._on_before_model_call(_event(agent, 999_999))

        assert getattr(agent, "_context_attribution_split", None) is None
        assert get_context_breakdown(agent) is None
        # Deferred, not re-counted in front of this model call.
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_heuristic_probe_weight_is_not_memoised(self):
        from agents.main_agent.session.hooks.context_attribution import _probe_baseline

        model = CountingFake()
        model.config = {"model_id": "m-1"}
        model.fall_back_on_call = 1
        await _probe_baseline(model)
        model.fall_back_on_call = None
        await _probe_baseline(model)
        await _probe_baseline(model)
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
