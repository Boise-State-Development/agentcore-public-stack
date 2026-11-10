"""§5.2's subtraction, built for the ceiling arms and off everywhere else.

docs/specs/unified-attachment-delivery.md §5.2 / §8.4: with
``ceiling_excludes_documents`` the chat ceiling (the cut trigger and the
``over_ceiling`` offload) compares the last input minus the documents the
conversation holds, while the hard ceiling keeps counting everything. These
pin that it is off by default, that no deployment can turn it on (no CDK or
workflow names its variable, and no request field carries it), and what it
does when a local stack does.

Fixed policy (window unknown, threshold 1000): ceiling 1000, floor 250, hard
ceiling 1500.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.main_agent.config.constants import EnvVars
from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import chat_input_tokens, held_document_tokens

from .conftest import make_assistant_message, make_user_message

_REPO = Path(__file__).resolve().parents[5]
CONFIG = dict(enabled=True, token_threshold=1000, protected_turns=3, summary_token_budget=200)


def _doc_turn(doc_bytes: int, text: str = "q") -> list:
    user = make_user_message(text)
    user["content"].insert(0, {"document": {"format": "txt", "name": "d", "source": {"bytes": b"x" * doc_bytes}}})
    return [user, make_assistant_message("a")]


def _manager(make_session_manager, store, **overrides):
    mgr = make_session_manager(compaction_config=CompactionConfig(**{**CONFIG, **overrides}))
    mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

    def _save(state, record_event=False):
        state.updated_at = datetime.now(timezone.utc).isoformat()
        store["compaction"] = state.to_dict()

    mgr._save_compaction_state = _save
    mgr._retrieve_session_summaries = lambda: ["LTM summary"]
    mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
    return mgr


def test_off_by_default_and_from_an_unset_or_empty_environment(monkeypatch):
    assert CompactionConfig().ceiling_excludes_documents is False
    monkeypatch.delenv(EnvVars.COMPACTION_CEILING_EXCLUDES_DOCUMENTS, raising=False)
    assert CompactionConfig.from_env().ceiling_excludes_documents is False
    for value in ("", "false", "1", "yes"):
        monkeypatch.setenv(EnvVars.COMPACTION_CEILING_EXCLUDES_DOCUMENTS, value)
        assert CompactionConfig.from_env().ceiling_excludes_documents is False
    monkeypatch.setenv(EnvVars.COMPACTION_CEILING_EXCLUDES_DOCUMENTS, "true")
    assert CompactionConfig.from_env().ceiling_excludes_documents is True


def test_off_it_reads_nothing_and_subtracts_nothing():
    messages = _doc_turn(40_000)
    assert chat_input_tokens(CompactionConfig(), 12_345, messages) == (12_345, 0)


def test_no_deployment_or_request_can_turn_it_on():
    name = EnvVars.COMPACTION_CEILING_EXCLUDES_DOCUMENTS
    for root in ("infrastructure/lib", "infrastructure/bin", ".github"):
        for path in (_REPO / root).rglob("*"):
            if path.is_file() and path.suffix in {".ts", ".yml", ".yaml", ".json"}:
                assert name not in path.read_text(encoding="utf-8", errors="ignore"), path
                assert "CEILING_EXCLUDES_DOCUMENTS" not in path.read_text(encoding="utf-8", errors="ignore"), path
    from apis.inference_api.chat.models import InvocationRequest

    assert not [f for f in InvocationRequest.model_fields if "ceiling" in f.lower() or "exclude" in f.lower()]


def test_on_it_subtracts_only_top_level_document_blocks():
    messages = _doc_turn(4_000)
    messages.append({"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": [
        {"document": {"format": "txt", "name": "slice", "source": {"bytes": b"y" * 8_000}}}]}}]})
    assert held_document_tokens(messages) == 1_000
    on = CompactionConfig(ceiling_excludes_documents=True)
    assert chat_input_tokens(on, 1_800, messages) == (800, 1_000)
    assert chat_input_tokens(on, 500, messages) == (0, 1_000)


@pytest.mark.asyncio
async def test_a_held_document_no_longer_triggers_the_cut_but_the_hard_ceiling_still_counts_it(make_session_manager):
    live = _doc_turn(4_000) + _doc_turn(0, "q1") + _doc_turn(0, "q2") + _doc_turn(0, "q3") + _doc_turn(0, "q4")
    # 1,300 input, 1,000 of it the document: 300 against the 1,000 ceiling.
    on = _manager(make_session_manager, {}, ceiling_excludes_documents=True)
    assert await on.update_after_turn(1_300, current_messages=live) is None
    assert on.compaction_state.pending_checkpoint is None and on.compaction_state.checkpoint == 0

    off = _manager(make_session_manager, {})
    await off.update_after_turn(1_300, current_messages=live)
    assert (off.compaction_state.pending_checkpoint or off.compaction_state.checkpoint) > 0

    # At the hard ceiling (1,500) the full input counts, documents and all.
    hard = _manager(make_session_manager, {}, ceiling_excludes_documents=True)
    await hard.update_after_turn(2_600, current_messages=live)
    assert (hard.compaction_state.pending_checkpoint or hard.compaction_state.checkpoint) > 0


def test_the_over_ceiling_offload_subtracts_too(make_session_manager):
    mgr = _manager(make_session_manager, {}, ceiling_excludes_documents=True)
    state = mgr.compaction_state
    state.last_input_tokens = 1_300
    state.updated_at = datetime.now(timezone.utc).isoformat()
    mgr._turn_start_stamp = state.updated_at
    assert mgr._document_offload_reason()[0] == "over_ceiling"
    assert mgr._document_offload_reason(held_document_tokens=1_000)[0] is None
