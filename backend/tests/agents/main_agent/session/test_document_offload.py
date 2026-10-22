"""Document offload (offload spec §4C / §4D, PR-4).

Pins the three rules — pinning, minimum size, and free-or-unavoidable — the
in-place mutation (never rebinding ``agent.messages``), byte-equality with
the restore transformation, slice ageing on both paths, the rollout bucket,
and the ledger event with its cache gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session import document_offload as do
from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from apis.shared.files.models import FileMetadata, FileStatus

REHYDRATION = "agents.main_agent.session.document_rehydration"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("COST_DIAGNOSTICS_ENABLED", "DOCUMENT_OFFLOAD_ENABLED", "DOCUMENT_OFFLOAD_ROLLOUT_PERCENT"):
        monkeypatch.delenv(var, raising=False)


BIG = 40_000  # bytes → 10k estimated tokens, over the 5k floor
SMALL = 4_000  # 1k tokens, under it

READY_DIGEST = {
    "version": 1, "status": "ready", "format": "pdf", "unit": "page", "count": 12,
    "sections": [{"start": 1, "title": "Intro"}], "abstract": "A policy.", "tokens": 40,
}


def _doc(name="policy_pdf", size=BIG, fmt="pdf"):
    return {"document": {"format": fmt, "name": name, "source": {"bytes": b"x" * size}}}


def _slice(name="policy p4-7 abc123", size=2000):
    return {"toolResult": {"toolUseId": "t", "status": "success", "content": [
        {"json": {"upload_id": "up-1", "filename": "policy.pdf", "pages_returned": 4}},
        _doc(name, size),
    ]}}


def _prompt(text, *blocks):
    return {"role": "user", "content": [{"text": text}, *blocks]}


def _reply(text="ok"):
    return {"role": "assistant", "content": [{"text": text}]}


def _meta(upload_id="up-1", filename="policy.pdf", size=BIG, digest=READY_DIGEST):
    return FileMetadata(
        upload_id=upload_id, user_id="u1", session_id="s1", filename=filename, mime_type="application/pdf",
        size_bytes=size, s3_key="k", s3_bucket="b", status=FileStatus.READY, digest=digest,
    )


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


class TestPinning:
    def test_attach_turn_and_the_next_are_pinned_then_released(self):
        conv = [_prompt("read this", _doc()), _reply()]
        assert do.pinned_document_names(conv, "next question") == {"policy_pdf"}      # attach turn (1 of 2)
        conv += [_prompt("next question"), _reply()]
        assert do.pinned_document_names(conv, "and another") == {"policy_pdf"}        # turn after (2 of 2)
        conv += [_prompt("and another"), _reply()]
        assert do.pinned_document_names(conv, "unrelated") == set()                   # released

    def test_naming_the_document_in_the_prompt_pins_it(self):
        conv = [_prompt("q", _doc("BBR Policy_pdf")), _reply(), _prompt("a"), _reply(), _prompt("b"), _reply()]
        assert do.pinned_document_names(conv, "what does the bbr policy say about limits?") == {"BBR Policy_pdf"}
        assert do.pinned_document_names(conv, "unrelated") == set()
        # The previous prompt counts too (the model is still answering it).
        conv[-2] = _prompt("compare against the BBR policy")
        assert do.pinned_document_names(conv, "go on") == {"BBR Policy_pdf"}

    def test_a_recent_document_read_result_pins_its_document(self):
        conv = [
            _prompt("q", _doc()), _reply(), _prompt("a"), _reply(), _prompt("b"), _reply(),
            _prompt("show page 4"), {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "document_read", "input": {}}}]},
            {"role": "user", "content": [_slice()]}, _reply(),
        ]
        assert do.pinned_document_names(conv, "thanks") == {"policy_pdf"}

    def test_short_stems_never_match_prompt_text(self):
        conv = [_prompt("q", _doc("a_pdf")), _reply(), _prompt("x"), _reply(), _prompt("y"), _reply()]
        assert do.pinned_document_names(conv, "a question about a thing") == set()

    def test_name_stem(self):
        assert do.name_stem("BBR Policy_pdf_2") == "BBR Policy"
        assert do.name_stem("notes_md") == "notes"
        assert do.name_stem("plain") == "plain"


class TestCandidates:
    def test_unpinned_and_large_only(self):
        conv = [_prompt("q", _doc("big_pdf", BIG), _doc("small_pdf", SMALL), _doc("pinned_pdf", BIG)), _reply()]
        cands = do.candidate_documents(conv, {"pinned_pdf"})
        assert [(c.message_index, c.block_index, c.name, c.tokens) for c in cands] == [(0, 1, "big_pdf", BIG // 4)]

    def test_digest_and_s3_blocks_are_not_candidates(self):
        conv = [_prompt("q", {"text": "<document-digest upload_id='u'/>"},
                        {"document": {"format": "pdf", "name": "n", "source": {"s3Location": {"uri": "s3://b/k"}}}})]
        assert do.candidate_documents(conv, set()) == []


class TestRollout:
    def test_bucket_is_stable_and_flag_and_percent_gate(self, monkeypatch):
        assert do.session_bucket("s1") == do.session_bucket("s1") and 0 <= do.session_bucket("s1") < 100
        assert do.offload_enabled_for("s1") is True and do.offload_enabled_for("") is False
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", "0")
        assert do.offload_enabled_for("s1") is False
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", str(do.session_bucket("s1") + 1))
        assert do.offload_enabled_for("s1") is True
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", "garbage")
        assert do.rollout_percent() == 100
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ENABLED", "false")
        assert do.offload_enabled_for("s1") is False


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


class TestOffloadDocuments:
    def test_replaces_in_place_with_the_restore_transformation(self, monkeypatch):
        monkeypatch.setattr(f"{REHYDRATION}.load_session_documents", lambda *_: [_meta()])
        conv = [_prompt("q", _doc()), _reply(), _prompt("a"), _reply(), _prompt("b"), _reply()]
        content_list = conv[0]["content"]
        cands = do.candidate_documents(conv, set())
        result = do.offload_documents(conv, cands, session_id="s1", user_id="u1")

        assert result.offloaded == 1 and result.evicted_tokens == BIG // 4 and result.upload_ids == ["up-1"]
        assert conv[0]["content"] is content_list                     # in place, never rebound
        assert conv[0]["content"][1]["text"].startswith('<document-digest name="policy.pdf" upload_id="up-1"')
        # Byte-identical to what a cold restore renders for the same block.
        from agents.main_agent.session.document_rehydration import rehydrate_documents

        restored = rehydrate_documents([_prompt("q", _doc())], session_id="s1", user_id="u1")
        assert restored.messages[0]["content"][1] == conv[0]["content"][1]

    def test_unmatched_documents_stay_inline(self, monkeypatch):
        monkeypatch.setattr(f"{REHYDRATION}.load_session_documents", lambda *_: [_meta(filename="other.pdf", size=1)])
        conv = [_prompt("q", _doc()), _reply()]
        result = do.offload_documents(conv, do.candidate_documents(conv, set()), session_id="s1", user_id="u1")
        assert result.offloaded == 0 and result.skipped_unmatched == 1
        assert "document" in conv[0]["content"][1]

    def test_lookup_failure_leaves_everything_inline(self, monkeypatch):
        monkeypatch.setattr(f"{REHYDRATION}.load_session_documents", lambda *_: (_ for _ in ()).throw(RuntimeError("ddb")))
        conv = [_prompt("q", _doc()), _reply()]
        result = do.offload_documents(conv, do.candidate_documents(conv, set()), session_id="s1", user_id="u1")
        assert result.offloaded == 0 and "document" in conv[0]["content"][1]


class TestSliceAgeing:
    def test_old_slices_are_stubbed_recent_ones_kept(self):
        conv = [
            _prompt("p1"), _reply(), {"role": "user", "content": [_slice("old p1-2 aaa111")]}, _reply(),
            _prompt("p2"), _reply(), {"role": "user", "content": [_slice("new p3-4 bbb222")]}, _reply(),
            _prompt("p3"), _reply(),
        ]
        count, tokens = do.age_document_slices(conv, max_turns=2)
        assert (count, tokens) == (1, 2000 // 4)
        assert conv[2]["content"][0]["toolResult"]["content"][1] == {"text": do.slice_stub("old p1-2 aaa111")}
        assert "document" in conv[6]["content"][0]["toolResult"]["content"][1]
        # Idempotent and deterministic: a second pass changes nothing.
        assert do.age_document_slices(conv, max_turns=2) == (0, 0)

    def test_too_few_turns_means_nothing_ages(self):
        conv = [_prompt("p1"), {"role": "user", "content": [_slice()]}]
        assert do.age_document_slices(conv, max_turns=2) == (0, 0)


# ---------------------------------------------------------------------------
# Session manager: the cache-gap decision and the ledger
# ---------------------------------------------------------------------------


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _manager(make_session_manager, *, updated_seconds_ago=None, last_input=0, last_key=None, current_key=None, policy=None, enabled=True):
    mgr = make_session_manager(compaction_config=CompactionConfig(enabled=enabled, cache_ttl_seconds=300))
    mgr.compaction_state = CompactionState(
        updated_at=_iso(updated_seconds_ago) if updated_seconds_ago is not None else None,
        last_input_tokens=last_input, last_prefix_key=last_key, policy=policy,
    )
    mgr._current_prefix_key = current_key
    mgr._adopt_persisted_compaction_state = lambda: None
    mgr._save_compaction_state = lambda *a, **k: None
    return mgr


def _agent(messages):
    agent = MagicMock()
    agent.messages = messages
    return agent


def _cold_conversation():
    return [_prompt("q", _doc()), _reply(), _prompt("a"), _reply(), _prompt("b"), _reply()]


class TestApplyDocumentOffload:
    @pytest.fixture(autouse=True)
    def rows(self, monkeypatch):
        monkeypatch.setattr(f"{REHYDRATION}.load_session_documents", lambda *_: [_meta()])

    def test_waits_while_the_cache_is_live(self, make_session_manager):
        mgr = _manager(make_session_manager, updated_seconds_ago=30, last_input=1000, policy={"ceiling": 100_000})
        agent = _agent(_cold_conversation())
        assert mgr.apply_document_offload(agent, prompt="x") is None
        assert "document" in agent.messages[0]["content"][1]
        assert mgr.drain_compaction_events() == []

    def test_cache_expired_offloads_and_records_the_gap(self, make_session_manager):
        mgr = _manager(make_session_manager, updated_seconds_ago=900, last_input=1000, policy={"ceiling": 100_000})
        messages = _cold_conversation()
        agent = _agent(messages)
        assert mgr.apply_document_offload(agent, prompt="x") == "cache_expired"
        assert agent.messages is messages                                # never rebound
        assert messages[0]["content"][1]["text"].startswith("<document-digest")
        events = mgr.drain_compaction_events()
        assert len(events) == 1 and events[0]["kind"] == "document_offload"
        assert events[0]["documents"] == 1 and events[0]["documentTokens"] == BIG // 4
        assert events[0]["cacheGapSeconds"] >= 899 and events[0]["digestTokens"] > 0

    def test_prefix_change_and_over_ceiling_are_the_other_reasons(self, make_session_manager):
        mgr = _manager(make_session_manager, updated_seconds_ago=10, last_input=1000, last_key="m|a", current_key="m|b", policy={"ceiling": 100_000})
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") == "prefix_changed"
        mgr = _manager(make_session_manager, updated_seconds_ago=10, last_input=150_000, last_key="m|a", current_key="m|a", policy={"ceiling": 100_000})
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") == "over_ceiling"

    def test_pinned_documents_never_move_even_when_free(self, make_session_manager):
        mgr = _manager(make_session_manager, updated_seconds_ago=900)
        agent = _agent([_prompt("q", _doc()), _reply()])                 # attach turn
        assert mgr.apply_document_offload(agent, prompt="next") is None
        assert "document" in agent.messages[0]["content"][1]
        agent = _agent(_cold_conversation())
        assert mgr.apply_document_offload(agent, prompt="about the policy please") is None

    def test_compaction_off_uses_the_in_process_turn_stamp(self, make_session_manager):
        mgr = _manager(make_session_manager, enabled=False)
        mgr._last_turn_completed_at = _iso(900)
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") == "cache_expired"
        mgr._last_turn_completed_at = _iso(5)
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") is None

    def test_kill_switch_and_bucket(self, make_session_manager, monkeypatch):
        mgr = _manager(make_session_manager, updated_seconds_ago=900)
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ENABLED", "false")
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") is None
        monkeypatch.delenv("DOCUMENT_OFFLOAD_ENABLED")
        monkeypatch.setenv("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", "0")
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") is None

    def test_slices_age_on_the_live_path_under_the_same_gate(self, make_session_manager):
        mgr = _manager(make_session_manager, updated_seconds_ago=900)
        conv = [_prompt("p1"), _reply(), {"role": "user", "content": [_slice()]}, _reply(), _prompt("p2"), _reply(), _prompt("p3"), _reply()]
        assert mgr.apply_document_offload(_agent(conv), prompt="x") == "cache_expired"
        assert conv[2]["content"][0]["toolResult"]["content"][1]["text"].startswith("[Retrieved pages placeholder:")
        assert mgr.drain_compaction_events()[0]["slices"] == 1

    def test_never_raises(self, make_session_manager, monkeypatch):
        mgr = _manager(make_session_manager, updated_seconds_ago=900)
        monkeypatch.setattr("agents.main_agent.session.document_offload.candidate_documents", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert mgr.apply_document_offload(_agent(_cold_conversation()), prompt="x") is None


class TestRestoreAgesSlices:
    def test_initialize_stubs_old_slices_like_the_live_path(self, make_session_manager, monkeypatch):
        monkeypatch.setattr(f"{REHYDRATION}.load_session_documents", lambda *_: [])
        mgr = make_session_manager()
        tool_use = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "document_read", "input": {}}}]}
        conv = [_prompt("p1"), tool_use, {"role": "user", "content": [_slice()]}, _reply(), _prompt("p2"), _reply(), _prompt("p3"), _reply()]
        agent = MagicMock()
        agent.agent_id = "default"
        agent.messages = conv
        mgr.read_agent = MagicMock(return_value=object())
        mgr.list_messages = MagicMock(return_value=[MagicMock(to_message=lambda m=m: m) for m in conv])
        mgr.initialize(agent)
        stub = agent.messages[2]["content"][0]["toolResult"]["content"][1]
        assert stub == {"text": do.slice_stub("policy p4-7 abc123")}
