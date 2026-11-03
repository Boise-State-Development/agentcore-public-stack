"""Rewind on the Runtime: the restore filter and the warm in-place cut.

docs/specs/conversation-rewind-and-fork.md §4.3 and §8.1. The point of the
design is the prompt cache: a warm agent cuts the list it already sent, so the
retained messages are the same objects, and the same bytes, the earlier turns
sent. Re-restoring would run the sanitizers and lose the LTM context that was
never persisted, and the prefix would no longer match.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from apis.shared.sessions.rewind import RewindState

from .conftest import TEST_SESSION_ID, make_conversation


@pytest.fixture(autouse=True)
def _rewind_on(monkeypatch):
    monkeypatch.setenv("SESSION_REWIND_ENABLED", "true")


def _dump(messages):
    return json.dumps(messages, sort_keys=True)


def _row(state: RewindState, compaction: CompactionState = None):
    row = {"rewind": state.to_dict()}
    if compaction is not None:
        row["compaction"] = compaction.to_dict()
    return row


def _stub_row(mgr, holder):
    """Serve the session row from ``holder["row"]`` and count the reads."""
    holder.setdefault("reads", 0)

    def _read():
        holder["reads"] += 1
        return holder.get("row")

    mgr._read_session_row = _read


def _agent(messages):
    agent = MagicMock()
    agent.messages = messages
    return agent


class TestWarmCut:
    def test_cuts_the_live_list_in_place_and_keeps_every_retained_byte(self, make_session_manager):
        mgr = make_session_manager()
        live = make_conversation(4)  # 8 messages; user messages at 0, 2, 4, 6
        retained_before = _dump(live[:4])
        retained_objects = live[:4]
        holder = {"row": _row(RewindState().with_cut(4, 8))}
        _stub_row(mgr, holder)
        agent = _agent(live)

        assert mgr.apply_rewind(agent) == 4

        # Same list object (the #741 alias survives) and the same message
        # objects: nothing was re-serialized, so the cached prefix still matches.
        assert agent.messages is live
        assert len(live) == 4
        assert all(a is b for a, b in zip(live, retained_objects))
        assert _dump(live) == retained_before
        assert mgr._rewind_epoch == 1

    def test_applies_once_then_leaves_later_turns_alone(self, make_session_manager):
        mgr = make_session_manager()
        live = make_conversation(4)
        holder = {"row": _row(RewindState().with_cut(4, 8))}
        _stub_row(mgr, holder)
        agent = _agent(live)

        mgr.apply_rewind(agent)
        # The next turn appends a new exchange at logical 4..5.
        live.extend(make_conversation(1))
        assert mgr.apply_rewind(agent) is None
        assert len(live) == 6

    def test_two_rewinds_before_a_send_cut_to_the_earlier_point(self, make_session_manager):
        mgr = make_session_manager()
        live = make_conversation(4)
        holder = {"row": _row(RewindState().with_cut(6, 8).with_cut(2, 8))}
        _stub_row(mgr, holder)

        assert mgr.apply_rewind(_agent(live)) == 2
        assert len(live) == 2

    def test_rewinding_to_the_first_message_empties_the_list(self, make_session_manager):
        mgr = make_session_manager()
        live = make_conversation(2)
        _stub_row(mgr, {"row": _row(RewindState().with_cut(0, 4))})

        assert mgr.apply_rewind(_agent(live)) == 0
        assert live == []

    def test_a_sibling_sharing_the_list_does_not_cut_it_again(self, make_session_manager):
        # An @-mention built a second agent that adopted the first one's list
        # (_adopt_session_conversation), and the live-conversation holder with it.
        first = make_session_manager()
        second = make_session_manager()
        live = make_conversation(4)
        second._live_conversation = first._live_conversation
        holder = {"row": _row(RewindState().with_cut(4, 8))}
        _stub_row(first, holder)
        _stub_row(second, holder)

        first.apply_rewind(_agent(live))
        live.extend(make_conversation(1))  # first's turn appends logical 4..5

        assert second.apply_rewind(_agent(live)) is None
        assert len(live) == 6
        assert second._rewind_epoch == 1

    def test_clamps_the_truncation_anchor_and_drops_a_parked_cut(self, make_session_manager):
        cfg = CompactionConfig(enabled=True, token_threshold=1000, protected_turns=3)
        mgr = make_session_manager(compaction_config=cfg)
        mgr.compaction_state = CompactionState(checkpoint=0, truncation_anchor=6, pending_checkpoint=4)
        _stub_row(mgr, {"row": _row(RewindState().with_cut(4, 8))})

        mgr.apply_rewind(_agent(make_conversation(4)))

        assert mgr.compaction_state.truncation_anchor == 4
        assert mgr.compaction_state.pending_checkpoint is None

    def test_hands_its_row_read_to_the_compaction_re_read(self, make_session_manager):
        cfg = CompactionConfig(enabled=True, token_threshold=1000, protected_turns=3)
        mgr = make_session_manager(compaction_config=cfg)
        mgr.compaction_state = CompactionState()
        compaction = CompactionState(checkpoint=0, truncation_anchor=2)
        holder = {"row": _row(RewindState().with_cut(4, 8), compaction)}
        _stub_row(mgr, holder)

        mgr.apply_rewind(_agent(make_conversation(4)))
        loaded = mgr._load_compaction_state()

        assert holder["reads"] == 1
        assert loaded.truncation_anchor == 2

    def test_does_nothing_with_the_flag_off(self, make_session_manager, monkeypatch):
        monkeypatch.setenv("SESSION_REWIND_ENABLED", "false")
        mgr = make_session_manager()
        live = make_conversation(4)
        holder = {"row": _row(RewindState().with_cut(4, 8))}
        _stub_row(mgr, holder)

        assert mgr.apply_rewind(_agent(live)) is None
        assert len(live) == 8 and holder["reads"] == 0

    def test_never_rewound_session_is_untouched(self, make_session_manager):
        mgr = make_session_manager()
        live = make_conversation(4)
        _stub_row(mgr, {"row": {"compaction": CompactionState().to_dict()}})

        assert mgr.apply_rewind(_agent(live)) is None
        assert len(live) == 8


class TestColdRestore:
    """The restore reads messages through ``list_messages``; only that read filters."""

    def _session_messages(self, messages):
        out = []
        for m in messages:
            sm = MagicMock()
            sm.to_message.return_value = m
            out.append(sm)
        return out

    def _restore(self, mgr, stored, row):
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        del mgr.list_messages  # the fixture's stub; use the real override
        session_messages = self._session_messages(stored)
        holder = {"row": row}
        _stub_row(mgr, holder)

        def _sdk_initialize(self_inner, agent, **kw):
            # Strands' restore: keyword arguments, like RepositorySessionManager.
            loaded = self_inner.list_messages(session_id=TEST_SESSION_ID, agent_id=agent.agent_id, offset=0)
            agent.messages = [sm.to_message() for sm in loaded]

        agent = MagicMock()
        agent.agent_id = "default"
        agent.messages = []
        with patch.object(AgentCoreMemorySessionManager, "list_messages", return_value=session_messages), \
                patch.object(AgentCoreMemorySessionManager, "initialize", _sdk_initialize):
            mgr.initialize(agent)
            # After the restore, the same method answers with the physical list.
            physical = mgr.list_messages(TEST_SESSION_ID, "default")
        return agent, physical

    def test_filters_rewound_messages_and_counts_physically(self, make_session_manager):
        mgr = make_session_manager()
        stored = make_conversation(5)  # 10 stored, rewound at 4, then one new turn at 8..9
        state = RewindState().with_cut(4, 8)

        agent, physical = self._restore(mgr, stored, _row(state))

        assert agent.messages == stored[:4] + stored[8:]
        assert len(physical) == 10
        # New messages are numbered past every stored one, hidden included.
        assert mgr.message_count == 10
        assert mgr._rewind_epoch == 1
        assert mgr._live_conversation.rewind_epoch == 1

    def test_a_cold_restore_then_head_of_turn_does_not_cut_again(self, make_session_manager):
        mgr = make_session_manager()
        stored = make_conversation(5)
        agent, _ = self._restore(mgr, stored, _row(RewindState().with_cut(4, 8)))

        assert mgr.apply_rewind(agent) is None
        assert len(agent.messages) == 6
