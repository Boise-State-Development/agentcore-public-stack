"""The ``over_ceiling`` offload judges the model's own ceiling, before the first cut too.

``state.policy`` used to be written only when a cut happened, so until then
``_document_offload_reason`` fell back to ``CompactionPolicy.resolve(config,
None)``: the window-less fixed threshold (100k). Under a 200k cap a warm 127k
document was offloaded although the real ceiling was 200k (attachment_delivery
ceiling arms, Sonnet 4.6, 2026-11-10); on Haiku 5.5, whose 100k window gives a
50k ceiling, the offload waited until 100k.
"""

from datetime import datetime, timezone

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState

from .conftest import make_assistant_message, make_user_message


def _manager(make_session_manager, store, **overrides):
    config = dict(enabled=True, token_threshold=100_000, protected_turns=3, summary_token_budget=200)
    mgr = make_session_manager(compaction_config=CompactionConfig(**{**config, **overrides}))
    mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

    def _save(state, record_event=False):
        state.updated_at = datetime.now(timezone.utc).isoformat()
        store["compaction"] = state.to_dict()

    mgr._save_compaction_state = _save
    mgr._retrieve_session_summaries = lambda: ["LTM summary"]
    mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
    return mgr


def _live():
    return [make_user_message("q"), make_assistant_message("a")]


def _warm_reason(mgr):
    mgr._turn_start_stamp = datetime.now(timezone.utc).isoformat()
    return mgr._document_offload_reason()[0]


@pytest.mark.asyncio
async def test_a_200k_cap_holds_a_warm_document_under_its_own_ceiling(make_session_manager):
    mgr = _manager(make_session_manager, {}, ceiling_cap_tokens=200_000)
    await mgr.update_after_turn(127_000, current_messages=_live(), context_window=1_000_000)
    assert mgr.compaction_state.policy["ceiling"] == 200_000
    assert _warm_reason(mgr) is None


@pytest.mark.asyncio
async def test_a_small_window_offloads_over_its_own_ceiling(make_session_manager):
    mgr = _manager(make_session_manager, {})
    # Haiku 5.5: window pinned to 100k, so the ceiling is 50k.
    await mgr.update_after_turn(60_000, current_messages=_live(), context_window=100_000)
    assert mgr.compaction_state.policy["ceiling"] == 50_000
    assert _warm_reason(mgr) == "over_ceiling"


@pytest.mark.asyncio
async def test_a_turn_that_does_not_cut_keeps_the_last_cuts_keys(make_session_manager):
    store = {"compaction": CompactionState(policy={
        "ceiling": 100_000, "forced": True, "retainedTokensEstimate": 12_000, "tailBounded": True, "keptTurns": 2,
    }).to_dict()}
    mgr = _manager(make_session_manager, store)
    await mgr.update_after_turn(10_000, current_messages=_live(), context_window=1_000_000)
    policy = mgr.compaction_state.policy
    assert policy["forced"] is True and policy["retainedTokensEstimate"] == 12_000
    assert policy["tailBounded"] is True and policy["keptTurns"] == 2
    assert policy["ceiling"] == 100_000 and policy["contextWindow"] == 1_000_000
