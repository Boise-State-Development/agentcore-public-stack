"""Unit tests for restore-time tool-pairing / role-alternation repair.

`TurnBasedSessionManager._repair_tool_pairing` is the safety net that keeps a
structurally-corrupt persisted history from bricking a session with a Bedrock
Converse ValidationException ("The number of toolResult blocks at messages.N
exceeds the number of toolUse blocks of previous turn.").

The corruption shapes exercised here are the ones observed in a real bricked
production session (parallel tool calls interrupted by a user Stop):
duplicate tool-result turns, tool-result turns reordered away from their
tool-use turn (assistant/assistant/user/user), tool-results orphaned after a
synthetic error turn, and consecutive synthetic error turns.
"""

from agents.main_agent.config.constants import EnvVars
from agents.main_agent.session.turn_based_session_manager import (
    TurnBasedSessionManager as T,
)


class _FakeConfig:
    session_id = "test-session"


def _make_manager() -> T:
    """A TurnBasedSessionManager built without the heavy AgentCore parent init.

    ``_repair_restored_history`` only reads ``self.config.session_id`` and calls
    the ``_repair_tool_pairing`` classmethod, so an instance created via
    ``__new__`` with just a fake config is sufficient — and avoids a subclass
    whose ``__init__`` skips ``super().__init__``.
    """
    manager = T.__new__(T)
    manager.config = _FakeConfig()
    return manager


class _FakeAgent:
    def __init__(self, messages):
        self.messages = messages


def _use(*ids):
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": t, "name": "search", "input": {}}} for t in ids],
    }


def _res(*ids):
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": t, "content": [{"text": "ok"}]}} for t in ids],
    }


def _txt(role, text="hi"):
    return {"role": role, "content": [{"text": text}]}


def _is_valid(messages):
    """Assert the message list satisfies Bedrock's structural constraints:
    strict role alternation, and every toolResult turn matches the toolUseIds
    of the immediately-preceding turn exactly.
    """
    for i, msg in enumerate(messages):
        if i > 0 and messages[i - 1]["role"] == msg["role"]:
            return False, f"consecutive role at {i}"
        has_use, has_result = T._block_keys(msg)
        if has_result:
            prev_use = T._tool_use_ids(messages[i - 1]) if i > 0 else []
            if set(T._tool_result_ids(msg)) != set(prev_use):
                return False, f"pairing mismatch at {i}"
    return True, "valid"


class TestHealthyHistory:
    def test_clean_history_is_noop_identity(self):
        msgs = [_txt("user"), _use("a"), _res("a"), _txt("assistant")]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed == 0
        assert repaired is msgs  # identity: no rebuild for healthy history

    def test_clean_parallel_tools_is_noop(self):
        msgs = [_txt("user"), _use("a", "b", "c"), _res("a", "b", "c"), _txt("assistant")]
        _, fixed = T._repair_tool_pairing(msgs)
        assert fixed == 0


class TestDuplicateToolResults:
    def test_duplicate_consecutive_result_turn_deduped(self):
        # assistant USE x3, then the SAME results persisted twice.
        msgs = [
            _txt("user"),
            _use("a", "b", "c"),
            _res("a", "b", "c"),
            _res("a", "b", "c"),  # duplicate
            _txt("user", "next question"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why
        # exactly one result turn survives
        result_turns = [m for m in repaired if T._block_keys(m)[1]]
        assert len(result_turns) == 1


class TestReorderedParallelTools:
    def test_assistant_assistant_user_user_reordered(self):
        # Two tool-use turns back to back, then both result turns back to back.
        msgs = [
            _txt("user"),
            _use("a", "b", "c"),
            _use("d", "e"),
            _res("a", "b", "c"),
            _res("d", "e"),
            _txt("assistant", "done"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why


class TestOrphanedToolResults:
    def test_orphan_result_after_assistant_text_dropped(self):
        # A result turn re-injected after an assistant TEXT turn (no toolUse).
        msgs = [
            _txt("user"),
            _use("a", "b"),
            _res("a", "b"),
            _txt("assistant", "here is the answer"),
            _res("a", "b"),  # orphan: previous turn has no toolUse
            _txt("user", "thanks"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why


class TestConsecutiveTextTurns:
    def test_consecutive_synthetic_assistant_errors_merged(self):
        msgs = [
            _txt("user"),
            _txt("assistant", "⚠️ Something went wrong"),
            _txt("assistant", "⚠️ Something went wrong"),
            _txt("assistant", "final answer"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why
        assert sum(1 for m in repaired if m["role"] == "assistant") == 1

    def test_text_turn_merges_into_following_tooluse_turn(self):
        # assistant text immediately followed by assistant tool-use.
        msgs = [
            _txt("user"),
            _txt("assistant", "let me look that up"),
            _use("a", "b"),
            _res("a", "b"),
            _txt("assistant", "done"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why
        # merged assistant turn keeps toolUse at the tail so its result follows
        merged = repaired[1]
        assert merged["role"] == "assistant"
        assert "text" in merged["content"][0]
        assert "toolUse" in merged["content"][-1]


class TestMissingResults:
    def test_interrupted_tooluse_gets_synthesized_error_result(self):
        # Mid-list tool-use whose results were never persisted.
        msgs = [
            _txt("user"),
            _use("a", "b"),  # no result turn follows
            _txt("assistant", "moving on"),
            _txt("user", "ok"),
        ]
        repaired, fixed = T._repair_tool_pairing(msgs)
        assert fixed > 0
        ok, why = _is_valid(repaired)
        assert ok, why
        # a synthesized error result exists for both ids
        results = [b for m in repaired for b in m["content"] if "toolResult" in b]
        assert {r["toolResult"]["toolUseId"] for r in results} == {"a", "b"}
        assert all(r["toolResult"].get("status") == "error" for r in results)

    def test_trailing_tooluse_left_untouched(self):
        # A tool-use turn as the LAST message is left for prompt-arrival
        # handling (matching the SDK's own repair), not force-answered here.
        msgs = [_txt("user"), _use("a")]
        repaired, _ = T._repair_tool_pairing(msgs)
        assert repaired[-1]["content"][-1].get("toolUse", {}).get("toolUseId") == "a"


class TestIdempotency:
    def test_repair_is_idempotent(self):
        msgs = [
            _txt("user"),
            _use("a", "b", "c"),
            _res("a", "b", "c"),
            _res("a", "b", "c"),
            _use("d", "e"),
            _use("f"),
            _res("d", "e"),
            _res("f"),
            _txt("assistant", "x"),
            _txt("assistant", "y"),
        ]
        once, n1 = T._repair_tool_pairing(msgs)
        assert n1 > 0 and _is_valid(once)[0]
        twice, n2 = T._repair_tool_pairing(once)
        assert n2 == 0
        assert _is_valid(twice)[0]


class TestRepairWrapper:
    """Covers `_repair_restored_history`, the method wired into initialize()."""

    def test_wrapper_mutates_agent_messages(self, monkeypatch):
        monkeypatch.delenv(EnvVars.HISTORY_REPAIR_ENABLED, raising=False)
        agent = _FakeAgent([_txt("user"), _use("a"), _res("a"), _res("a")])
        _make_manager()._repair_restored_history(agent)
        ok, why = _is_valid(agent.messages)
        assert ok, why

    def test_kill_switch_disables_repair(self, monkeypatch):
        monkeypatch.setenv(EnvVars.HISTORY_REPAIR_ENABLED, "false")
        corrupt = [_txt("user"), _use("a"), _res("a"), _res("a")]
        agent = _FakeAgent(corrupt)
        _make_manager()._repair_restored_history(agent)
        assert agent.messages is corrupt  # untouched

    def test_wrapper_noop_on_healthy_history(self, monkeypatch):
        monkeypatch.delenv(EnvVars.HISTORY_REPAIR_ENABLED, raising=False)
        healthy = [_txt("user"), _use("a"), _res("a"), _txt("assistant")]
        agent = _FakeAgent(healthy)
        _make_manager()._repair_restored_history(agent)
        assert agent.messages is healthy  # identity preserved, no rebuild


class TestSteeringInjectionSurvivesRepair:
    """A mid-turn steering block rides the tool-result message (see
    docs/specs/mid-turn-steering.md), so a repair pass now sees a user turn
    with mixed ``toolResult`` + ``text`` content.

    Both helpers key on tool pairing, so neither treats the mixed message as a
    violation — but the rebuild path synthesizes result turns from the id map
    rather than copying them, which would drop the user's words along with
    everything else that isn't a toolResult. These are the tests that keep
    that from regressing: losing the text deletes something the user said and
    the model already read.
    """

    @staticmethod
    def _steered_res(*ids, text="actually, check the other file"):
        msg = _res(*ids)
        msg["content"].append({"text": text})
        return msg

    def test_healthy_history_with_an_injection_is_untouched(self):
        healthy = [_txt("user"), _use("a"), self._steered_res("a"), _txt("assistant")]
        repaired, violations = T._repair_tool_pairing(healthy)
        assert violations == 0
        assert repaired is healthy  # identity: no rebuild, nothing to strip

    def test_injection_survives_a_rebuild(self):
        # The observed corruption shape: a duplicate result turn (parallel
        # tool calls interrupted by a Stop) forces the rebuild path over a
        # turn whose result message carries an injection.
        msgs = [
            _txt("user"),
            _use("a"),
            self._steered_res("a"),
            _res("a"),
            _txt("assistant"),
        ]
        repaired, violations = T._repair_tool_pairing(msgs)

        assert violations > 0
        ok, why = _is_valid(repaired)
        assert ok, why
        texts = [
            block["text"]
            for msg in repaired
            for block in msg["content"]
            if "text" in block
        ]
        # Present, and exactly once: a re-emitted residual would show the user
        # saying the same thing twice.
        assert texts.count("actually, check the other file") == 1

    def test_injection_is_ordered_after_the_tool_results(self):
        """Bedrock wants toolResults leading their user turn; the injection
        follows them, which is exactly where the hook appends it live."""
        msgs = [
            _txt("user"),
            _use("a", "b"),
            self._steered_res("a", "b"),
            _res("a", "b"),
            _txt("assistant"),
        ]
        repaired, _ = T._repair_tool_pairing(msgs)

        result_turn = next(
            m
            for m in repaired
            if m["role"] == "user"
            and any("toolResult" in b for b in m["content"])
        )
        keys = ["toolResult" if "toolResult" in b else "text" for b in result_turn["content"]]
        assert keys == ["toolResult", "toolResult", "text"]

    def test_ordinary_result_turns_gain_nothing(self):
        """The carry-across must not invent content on unsteered turns."""
        msgs = [_txt("user"), _use("a"), _res("a"), _res("a"), _txt("assistant")]
        repaired, _ = T._repair_tool_pairing(msgs)

        for msg in repaired:
            for block in msg["content"]:
                assert "text" not in block or msg["role"] != "user" or "hi" in block.get("text", "")
