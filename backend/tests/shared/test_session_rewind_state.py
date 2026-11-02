"""The rewind state on a session row (docs/specs/conversation-rewind-and-fork.md §4.1).

Physical indices are message ids and never move; logical indices are what the
agent and compaction see. These pin the arithmetic between the two, because
every reader (GET /messages, the restore filter, the warm cut) trusts it.
"""

from apis.shared.sessions.rewind import (
    RewindState,
    is_rewind_target,
    parse_message_index,
)


def _user(text="hi"):
    return {"role": "user", "content": [{"text": text}]}


class TestWithCut:
    def test_first_rewind_hides_from_the_cut_to_the_end(self):
        state = RewindState().with_cut(4, 10)

        assert state.epoch == 1
        assert state.spans == ((4, 10),)
        assert [i for i in range(10) if not state.is_hidden(i)] == [0, 1, 2, 3]
        assert state.cuts[-1].cut == 4
        assert state.cuts[-1].visible_before == 10

    def test_a_later_rewind_after_new_turns_records_logical_positions(self):
        # Rewind at 4 of 10, then two new turns land physically at 10..13.
        first = RewindState().with_cut(4, 10)
        # The new user message at physical 12 is logical 6.
        second = first.with_cut(12, 14)

        assert second.spans == ((4, 10), (12, 14))
        assert second.logical_index(12) == 6
        assert second.cuts[-1].cut == 6
        assert second.cuts[-1].visible_before == 8
        assert [i for i in range(14) if not second.is_hidden(i)] == [0, 1, 2, 3, 10, 11]

    def test_rewinding_further_back_merges_into_one_span(self):
        state = RewindState().with_cut(6, 10).with_cut(2, 10)

        assert state.spans == ((2, 10),)
        assert state.epoch == 2
        assert [c.cut for c in state.cuts] == [6, 2]

    def test_logical_indices_below_the_cut_never_move(self):
        before = RewindState().with_cut(8, 12)
        after = before.with_cut(14, 18)

        for physical in range(8):
            assert before.logical_index(physical) == after.logical_index(physical) == physical

    def test_a_cut_outside_the_stored_range_is_refused(self):
        try:
            RewindState().with_cut(5, 5)
        except ValueError:
            return
        raise AssertionError("expected ValueError")


class TestCutsSince:
    def test_returns_only_the_newer_rewinds(self):
        state = RewindState().with_cut(6, 10).with_cut(2, 10)

        assert [c.epoch for c in state.cuts_since(1)] == [2]
        assert state.cuts_since(2) == []
        assert state.covers_epochs_since(0)

    def test_knows_when_the_bounded_history_lost_some(self):
        state = RewindState()
        for _ in range(25):
            state = state.with_cut(0, 2)

        assert len(state.cuts) == 20
        assert not state.covers_epochs_since(0)
        assert state.covers_epochs_since(5)


class TestSerialization:
    def test_round_trips(self):
        state = RewindState().with_cut(4, 10, updated_at="2026-10-29T00:00:00Z").with_cut(12, 14)

        assert RewindState.from_dict(state.to_dict()) == state

    def test_dynamo_decimals_and_floats_parse(self):
        from decimal import Decimal

        state = RewindState.from_dict(
            {"epoch": Decimal("2"), "spans": [[Decimal("3"), 7.0]], "cuts": [{"epoch": 2, "cut": 3.0, "visibleBefore": 7}]}
        )

        assert state.epoch == 2 and state.spans == ((3, 7),) and state.cuts[0].cut == 3

    def test_malformed_parts_read_as_never_rewound(self):
        state = RewindState.from_dict({"epoch": "x", "spans": [[5], ["a", 2], [4, 2]], "cuts": ["nope"]})

        assert state.epoch == 0 and state.spans == () and state.cuts == ()
        assert RewindState.from_row(None) == RewindState()
        assert not RewindState.from_row({"rewind": "garbage"}).active


class TestTargets:
    def test_a_user_text_message_is_a_target(self):
        assert is_rewind_target(_user("edit me"))

    def test_tool_results_steers_and_assistant_messages_are_not(self):
        tool_result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": []}}]}
        steer = {"role": "user", "content": [{"toolResult": {"toolUseId": "t1"}}, {"text": "also this"}]}

        assert not is_rewind_target(tool_result)
        assert not is_rewind_target(steer)
        assert not is_rewind_target({"role": "assistant", "content": [{"text": "hi"}]})
        assert not is_rewind_target({"role": "user", "content": [{"text": "   "}]})

    def test_message_ids_parse_only_for_their_own_session(self):
        assert parse_message_index("abc-123", "msg-abc-123-7") == 7
        assert parse_message_index("abc-123", "msg-other-7") is None
        assert parse_message_index("abc-123", "msg-abc-123-x") is None
