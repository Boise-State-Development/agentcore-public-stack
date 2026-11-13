"""Compaction prices replayed reasoning blocks as the model bills them.

On dev on 2026-11-11 (Haiku 5.5, a 32-page PDF read in slices) a cut's
``retainedTokensEstimate`` was 66,565 against a measured 49,165 (+35%). The
estimator priced each ``reasoningContent`` block at its stored text / 4, but
"summarized" thinking stores a short summary while the signature carries the
full thinking:

- a thinking-only assistant message (the turn spent its 32,000-token output cap
  thinking) was re-billed in full on the next call: +32,028 tokens against a
  5.5k estimate. Its signature was 100,476 characters; another, 86,168
  characters, billed ~30.2k;
- after the cut, no retained block was billed at all.

Calibration spread the unestimated 26k over the whole history, so the retained
estimate came out 35% high.

Direct Converse calls on dev the same day (reasoning present vs stripped,
everything else identical) give the rule per model, in ``model_config``:
Haiku 5.5 and Opus 5.5 (``drop_block`` binding) bill a block while the system
prompt and the history ahead of it are unchanged and 0 once either changes, a
compaction summary included; Sonnet 4.6 and Opus 4.7 bill every signed block
whatever changed; Haiku 4.5 (budget thinking) bills none from an earlier turn.
"""

import pytest

from agents.main_agent.core.model_config import (
    REASONING_REPLAY_BOUND,
    REASONING_REPLAY_PRESERVED,
    REASONING_REPLAY_STRIPPED,
    REASONING_REPLAY_TEXT,
    reasoning_replay_mode,
)
from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import (
    MESSAGE_OVERHEAD_TOKENS,
    REASONING_SIGNATURE_CHARS_PER_TOKEN,
    choose_checkpoint,
    estimate_message_tokens,
    reasoning_billed,
)
from agents.main_agent.session.document_estimate import DocumentIndex
from apis.shared.files.tokenizer_costs import tokenizer_factor

from .conftest import make_conversation

HAIKU45 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET46 = "us.anthropic.claude-sonnet-4-6"
OPUS47 = "us.anthropic.claude-opus-4-7"
HAIKU55 = "us.anthropic.claude-haiku-5-5"
OPUS55 = "us.anthropic.claude-opus-5-5"

#: (signature characters, billed tokens) measured on dev, 2026-11-11.
MEASURED_SIGNATURES = [
    (100_476, 32_000),  # Haiku 5.5, thinking-only, output cap: exact
    (86_168, 30_200),  # Haiku 5.5, read from the next call's delta
    (4_060, 1_338),  # Sonnet 4.6, probe
]


def _reasoning(signature_chars, summary_chars=400):
    return {"reasoningContent": {"reasoningText": {"text": "s" * summary_chars, "signature": "g" * signature_chars}}}


def _user(chars):
    return {"role": "user", "content": [{"text": "u" * chars}]}


def _assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def _text(chars):
    return {"text": "a" * chars}


class TestReplayMode:
    @pytest.mark.parametrize(
        "model_id,mode",
        [
            (HAIKU55, REASONING_REPLAY_BOUND),
            (OPUS55, REASONING_REPLAY_BOUND),
            ("us.anthropic.claude-sonnet-5-5", REASONING_REPLAY_BOUND),
            (SONNET46, REASONING_REPLAY_PRESERVED),
            (OPUS47, REASONING_REPLAY_PRESERVED),
            (HAIKU45, REASONING_REPLAY_STRIPPED),
            ("us.amazon.nova-pro-v1:0", REASONING_REPLAY_TEXT),
        ],
    )
    def test_mode_per_model(self, model_id, mode):
        assert reasoning_replay_mode(model_id) == mode

    def test_bound_billed_only_while_live_and_never_after_a_cut(self):
        assert reasoning_billed(HAIKU55, live=True, in_flight=False, after_cut=False) is True
        assert reasoning_billed(HAIKU55, live=False, in_flight=False, after_cut=False) is False
        assert reasoning_billed(HAIKU55, live=True, in_flight=True, after_cut=True) is False

    def test_preserved_always_billed(self):
        for live in (True, False):
            for after_cut in (True, False):
                assert reasoning_billed(SONNET46, live=live, in_flight=False, after_cut=after_cut) is True

    def test_stripped_only_in_flight(self):
        assert reasoning_billed(HAIKU45, live=True, in_flight=False, after_cut=False) is False
        assert reasoning_billed(HAIKU45, live=True, in_flight=True, after_cut=False) is True
        assert reasoning_billed(HAIKU45, live=True, in_flight=True, after_cut=True) is False

    def test_unknown_model_keeps_the_model_blind_estimate(self):
        assert reasoning_billed(None, live=True, in_flight=True, after_cut=False) is None


class TestBlockEstimate:
    @pytest.mark.parametrize("signature_chars,billed", MEASURED_SIGNATURES)
    def test_signature_tracks_the_bill(self, signature_chars, billed):
        message = _assistant(_reasoning(signature_chars))
        estimate = estimate_message_tokens(message, DocumentIndex(model_id=HAIKU55), True) - MESSAGE_OVERHEAD_TOKENS
        assert abs(estimate - billed) / billed < 0.09

    def test_the_cap_hit_is_within_two_percent(self):
        message = _assistant(_reasoning(100_476, summary_chars=22_000))
        estimate = estimate_message_tokens(message, DocumentIndex(model_id=HAIKU55), True) - MESSAGE_OVERHEAD_TOKENS
        assert estimate == int(100_476 / REASONING_SIGNATURE_CHARS_PER_TOKEN)
        assert abs(estimate - 32_000) / 32_000 < 0.02
        # The old estimate read the 22,000-character summary: 5.5k at chars/4.
        assert estimate_message_tokens(message) - MESSAGE_OVERHEAD_TOKENS == 5_500

    def test_a_dropped_block_is_zero(self):
        message = _assistant(_reasoning(100_476), _text(400))
        index = DocumentIndex(model_id=HAIKU55)
        assert estimate_message_tokens(message, index, False) == estimate_message_tokens(
            _assistant(_text(400)), index
        )


def _dev_history():
    """The shape of the dev session: a thinking-only turn, then answered turns.

    Live-list indices: 0 u, 1 a(think+text), 2 u, 3 a(think only, 100,476),
    4 u, 5 a(text), 6 u, 7 a(think+text, the final answer).
    """
    return [
        _user(40_000),
        _assistant(_reasoning(30_000, 2_000), _text(4_000)),
        _user(4_000),
        _assistant(_reasoning(100_476, 22_000)),
        _user(4_000),
        _assistant(_text(8_000)),
        _user(4_000),
        _assistant(_reasoning(9_300, 1_000), _text(600)),
    ]


def _text_tokens(chars, model_id):
    return int(round((chars // 4) * tokenizer_factor(model_id)))


class TestDevCase:
    """Retained estimate against what the first call after the cut bills."""

    CUTOFFS = [0, 2, 4, 6]

    def _billed(self, model_id, messages, *, after_cut, reasoning):
        """What a request bills for ``messages``: text at the model's factor,
        reasoning blocks by ``reasoning(index)``."""
        total = 0
        for i, m in enumerate(messages):
            total += MESSAGE_OVERHEAD_TOKENS
            for block in m["content"]:
                if "text" in block:
                    total += _text_tokens(len(block["text"]), model_id)
                elif reasoning(i, after_cut):
                    sig = block["reasoningContent"]["reasoningText"]["signature"]
                    total += int(len(sig) / REASONING_SIGNATURE_CHARS_PER_TOKEN)
        return total

    def test_haiku_55_retained_estimate_matches_the_post_cut_bill(self):
        messages = _dev_history()
        # The turn's last call read messages 0..6 and every block in them.
        measured = self._billed(HAIKU55, messages[:7], after_cut=False, reasoning=lambda i, c: True)
        # The first call after a cut at 4 bills messages 4.. with no reasoning.
        post_cut = self._billed(HAIKU55, messages[4:], after_cut=True, reasoning=lambda i, c: False)

        cut, retained = choose_checkpoint(
            messages, self.CUTOFFS, protected_turns=2, floor_tokens=post_cut + 10,
            history_tokens=measured, documents=DocumentIndex(model_id=HAIKU55),
        )
        assert cut == 4
        assert abs(retained - post_cut) / post_cut < 0.03

    def test_haiku_55_blind_overestimate_is_large(self):
        messages = _dev_history()
        measured = self._billed(HAIKU55, messages[:7], after_cut=False, reasoning=lambda i, c: True)
        post_cut = self._billed(HAIKU55, messages[4:], after_cut=True, reasoning=lambda i, c: False)
        # Model-blind, the 32k think is priced at its 5.5k summary and the
        # calibration spreads the rest over everything, the retained part too.
        _, blind_at_4 = choose_checkpoint(
            messages, [4, 6], protected_turns=1, floor_tokens=10**9,
            history_tokens=measured, documents=DocumentIndex(model_id=None),
        )
        assert blind_at_4 > post_cut * 1.2

    def test_stale_blocks_are_not_in_the_calibration(self):
        """After an earlier rewrite at live index 2, the 30k block at 1 bills 0."""
        messages = _dev_history()
        measured = self._billed(HAIKU55, messages[:7], after_cut=False, reasoning=lambda i, c: i >= 2)
        post_cut = self._billed(HAIKU55, messages[4:], after_cut=True, reasoning=lambda i, c: False)
        _, retained = choose_checkpoint(
            messages, [4, 6], protected_turns=1, floor_tokens=10**9,
            history_tokens=measured, documents=DocumentIndex(model_id=HAIKU55), reasoning_live_from=2,
        )
        assert abs(retained - post_cut) / post_cut < 0.03

    def test_sonnet_46_keeps_retained_reasoning(self):
        messages = _dev_history()
        measured = self._billed(SONNET46, messages[:7], after_cut=False, reasoning=lambda i, c: True)
        # Every retained block is billed after the cut, the final answer's too.
        post_cut = self._billed(SONNET46, messages[4:], after_cut=True, reasoning=lambda i, c: True)
        _, retained = choose_checkpoint(
            messages, [4, 6], protected_turns=1, floor_tokens=10**9,
            history_tokens=measured, documents=DocumentIndex(model_id=SONNET46),
        )
        assert abs(retained - post_cut) / post_cut < 0.03

    def test_haiku_45_bills_no_earlier_turn_reasoning(self):
        messages = _dev_history()
        measured = self._billed(HAIKU45, messages[:7], after_cut=False, reasoning=lambda i, c: False)
        post_cut = self._billed(HAIKU45, messages[4:], after_cut=True, reasoning=lambda i, c: False)
        _, retained = choose_checkpoint(
            messages, [4, 6], protected_turns=1, floor_tokens=10**9,
            history_tokens=measured, documents=DocumentIndex(model_id=HAIKU45),
        )
        assert abs(retained - post_cut) / post_cut < 0.03

    def test_history_without_reasoning_estimates_as_before(self):
        messages = [m for m in _dev_history() if all("text" in b for b in m["content"])]
        cutoffs = [i for i, m in enumerate(messages) if m["role"] == "user"]
        args = dict(protected_turns=1, floor_tokens=10**9, history_tokens=50_000)
        assert choose_checkpoint(messages, cutoffs, documents=DocumentIndex(model_id=HAIKU55), **args) == choose_checkpoint(
            messages, cutoffs, documents=DocumentIndex(model_id=HAIKU55), reasoning_live_from=3, **args
        )


# ---------------------------------------------------------------------------
# Where the prefix was last rewritten (CompactionState.reasoning_live_from)
# ---------------------------------------------------------------------------

CONFIG = dict(enabled=True, token_threshold=1000, protected_turns=3, summary_token_budget=200)


def _manager(make_session_manager, store, **overrides):
    from datetime import datetime, timezone

    cfg = CompactionConfig(**{**CONFIG, **overrides})
    mgr = make_session_manager(compaction_config=cfg)
    mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

    def _save(state, record_event=False):
        state.updated_at = datetime.now(timezone.utc).isoformat()
        store["compaction"] = state.to_dict()

    mgr._save_compaction_state = _save
    mgr._retrieve_session_summaries = lambda: ["LTM summary"]
    mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
    return mgr


class _Agent:
    def __init__(self, messages):
        self.messages = messages


class TestReasoningLiveFrom:
    def test_round_trips_and_defaults_to_none(self):
        assert CompactionState.from_dict({}).reasoning_live_from is None
        assert CompactionState.from_dict({"checkpoint": 0}).reasoning_live_from is None
        state = CompactionState(reasoning_live_from=12)
        assert CompactionState.from_dict(state.to_dict()).reasoning_live_from == 12

    @pytest.mark.asyncio
    async def test_applied_cut_marks_the_whole_retained_list(self, make_session_manager):
        from datetime import datetime, timedelta, timezone

        store = {}
        mgr = _manager(make_session_manager, store)
        live = make_conversation(5)
        await mgr.update_after_turn(1200, current_messages=live)
        assert mgr.compaction_state.pending_checkpoint == 4
        store["compaction"]["updatedAt"] = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        assert mgr.apply_pending_compaction(_Agent(live), prefix_key="m|default") == "cache_expired"
        # Absolute: checkpoint 4 + the 6 messages the slice kept.
        assert store["compaction"]["reasoningLiveFrom"] == 10

    @pytest.mark.asyncio
    async def test_model_switch_is_persisted_after_the_turn(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, token_threshold=10**6)
        live = make_conversation(3)
        mgr.apply_pending_compaction(_Agent(live), prefix_key="haiku|default")
        await mgr.update_after_turn(500, current_messages=live)
        assert store["compaction"].get("reasoningLiveFrom") is None
        live.extend(make_conversation(1))
        mgr.apply_pending_compaction(_Agent(live), prefix_key="sonnet|default")
        # Nothing saved at the head (TTFT); the turn's end persists it.
        assert store["compaction"].get("reasoningLiveFrom") is None
        await mgr.update_after_turn(500, current_messages=live)
        assert store["compaction"]["reasoningLiveFrom"] == 8

    @pytest.mark.asyncio
    async def test_same_key_marks_nothing(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, token_threshold=10**6)
        live = make_conversation(3)
        for _ in range(2):
            mgr.apply_pending_compaction(_Agent(live), prefix_key="haiku|default")
            await mgr.update_after_turn(500, current_messages=live)
        assert store["compaction"].get("reasoningLiveFrom") is None

    @pytest.mark.asyncio
    async def test_kill_switch_cut_marks_at_once(self, make_session_manager):
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=False)
        live = make_conversation(5)
        await mgr.update_after_turn(1600, current_messages=live)
        assert mgr.compaction_state.checkpoint == 4
        assert store["compaction"]["reasoningLiveFrom"] == 10
