"""A compaction cut that retires a ``skills`` activation names it in the summary.

The skill's instructions reached the model only as the activation's tool
result, so a cut that retires that turn takes them away. The summary written
at that cut gains one line naming the skill and telling the model to activate
it again; a cut with no activation in it writes exactly the summary it wrote
before. The line is computed once, at the checkpoint advance, and persisted
with the summary, so a restore prepends the same bytes every time.

Fixed policy throughout (window unknown, threshold 1000): ceiling 1000,
floor 250, hard ceiling 1500, as in ``test_compaction_tail_bound``.
"""

import copy
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agents.main_agent.session import compaction_summary
from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.skill_activations import (
    MAX_LISTED_SKILLS,
    activated_skills_in,
    cut_skills_reminder,
    skills_reminder_line,
    with_skills_reminder,
)

from .conftest import (
    make_assistant_message,
    make_tool_result_message,
    make_tool_use_message,
    make_user_message,
)

CONFIG = dict(
    enabled=True,
    token_threshold=1000,
    protected_turns=3,
    summary_token_budget=200,
    deferred_apply_enabled=False,
    summary_model_enabled=False,
)

LINE = (
    "Skills that were active before this point: {names}. "
    "Activate one again with the skills tool before relying on its instructions."
)


def _now():
    return datetime.now(timezone.utc)


def _dump(messages):
    return json.dumps(messages, sort_keys=True)


def _activation(tool_id, skill_name):
    return make_tool_use_message(tool_id, "skills", {"skill_name": skill_name})


def _turn(i, size, skill=None):
    """One turn; with ``skill``, the assistant activates it before answering."""
    messages = [make_user_message(f"q{i} " + "u" * size)]
    if skill:
        messages += [_activation(f"s{i}", skill), make_tool_result_message(f"s{i}", f"# {skill} instructions")]
    messages.append(make_assistant_message(f"a{i}"))
    return messages


def _conversation(*turns):
    messages = []
    for i, (size, skill) in enumerate(turns):
        messages += _turn(i, size, skill)
    return messages


def _manager(make_session_manager, store, summaries=("LTM summary",), **overrides):
    mgr = make_session_manager(compaction_config=CompactionConfig(**{**CONFIG, **overrides}))
    mgr._load_compaction_state = lambda: CompactionState.from_dict(store.get("compaction"))

    def _save(state, record_event=False):
        state.updated_at = _now().isoformat()
        store["compaction"] = state.to_dict()

    mgr._save_compaction_state = _save
    mgr._retrieve_session_summaries = lambda: list(summaries)
    mgr.compaction_state = CompactionState.from_dict(store.get("compaction"))
    return mgr


async def _cut(make_session_manager, live, **kwargs):
    """Run one over-the-hard-ceiling turn end; return the persisted summary."""
    store = {}
    available = kwargs.pop("available", None)
    mgr = _manager(make_session_manager, store, **kwargs)
    result = await mgr.update_after_turn(2100, current_messages=live, available_skill_names=available)
    assert result is not None
    return mgr.compaction_state.summary, store, mgr


# ---------------------------------------------------------------------------
# activated_skills_in
# ---------------------------------------------------------------------------


class TestActivatedSkillsIn:
    def test_none(self):
        assert activated_skills_in([]) == []
        assert activated_skills_in(_conversation((10, None), (10, None))) == []

    def test_one(self):
        assert activated_skills_in(_conversation((10, "pdf-workflows"))) == ["pdf-workflows"]

    def test_repeated_is_listed_once(self):
        messages = _conversation((10, "pdf-workflows"), (10, "pdf-workflows"))
        assert activated_skills_in(messages) == ["pdf-workflows"]

    def test_several_in_first_seen_order(self):
        messages = _conversation((10, "doc-drafting"), (10, "pdf-workflows"), (10, "doc-drafting"), (10, "apa"))
        assert activated_skills_in(messages) == ["doc-drafting", "pdf-workflows", "apa"]

    @pytest.mark.parametrize(
        "tool_input",
        [{}, {"skill_name": None}, {"skill_name": 7}, {"skill_name": ""}, {"skill_name": "  "}, "pdf-workflows", None],
    )
    def test_malformed_skills_call_is_ignored(self, tool_input):
        messages = [
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "x", "name": "skills", "input": tool_input}}]},
            _activation("y", "doc-drafting"),
        ]
        assert activated_skills_in(messages) == ["doc-drafting"]

    def test_other_tools_and_read_skill_file_are_not_activations(self):
        messages = [
            make_tool_use_message("a", "read_skill_file", {"skill_name": "pdf-workflows", "path": "references/x.md"}),
            make_tool_use_message("b", "search", {"skill_name": "nope"}),
            {"role": "user", "content": "not a list"},
            "not a message",
        ]
        assert activated_skills_in(messages) == []


class TestReminderLine:
    def test_exact_wording(self):
        assert skills_reminder_line(["a", "b"]) == LINE.format(names="a, b")

    def test_no_names_no_line(self):
        assert skills_reminder_line([]) is None
        assert cut_skills_reminder(_conversation((10, None))) is None

    def test_bounded_names_and_count(self):
        names = [f"skill-{i}" for i in range(MAX_LISTED_SKILLS + 3)] + []
        names[0] = "x" * 80
        line = skills_reminder_line(names)
        listed = line[len("Skills that were active before this point: ") : line.index(". Activate")].split(", ")
        assert len(listed) == MAX_LISTED_SKILLS + 1
        assert listed[0] == "x" * 64
        assert listed[-1] == "and 3 more"

    def test_only_names_still_available(self):
        messages = _conversation((10, "doc-drafting"), (10, "gone-skill"))
        assert cut_skills_reminder(messages, ["doc-drafting"]) == LINE.format(names="doc-drafting")
        assert cut_skills_reminder(messages, []) is None
        assert cut_skills_reminder(messages, None) == LINE.format(names="doc-drafting, gone-skill")

    def test_a_previous_line_being_cut_is_carried_forward(self):
        previous = with_skills_reminder("Earlier.", skills_reminder_line(["doc-drafting", "pdf-workflows"]))
        head = make_user_message(f"<conversation_summary>\n{previous}\n</conversation_summary>\n\nq")
        messages = [head, make_assistant_message("a")] + _conversation((10, "apa"), (10, "pdf-workflows"))
        assert cut_skills_reminder(messages) == LINE.format(names="doc-drafting, pdf-workflows, apa")

    def test_with_reminder_appends_one_line(self):
        assert with_skills_reminder("S", None) == "S"
        assert with_skills_reminder(None, None) is None
        assert with_skills_reminder("S", "L") == "S\n\nL"
        assert with_skills_reminder(None, "L") == "L"


# ---------------------------------------------------------------------------
# The cut: LTM (model-written) path
# ---------------------------------------------------------------------------


class TestModelWrittenSummary:
    @pytest.mark.asyncio
    async def test_activation_in_the_cut_adds_the_line(self, make_session_manager):
        live = _conversation((8000, "pdf-workflows"), (40, None))
        summary, _, mgr = await _cut(make_session_manager, live)
        assert mgr.compaction_state.checkpoint == 4  # the activation turn is retired
        assert summary == "LTM summary\n\n" + LINE.format(names="pdf-workflows")

    @pytest.mark.asyncio
    async def test_no_activation_is_byte_identical_to_before(self, make_session_manager):
        summary, _, _ = await _cut(make_session_manager, _conversation((8000, None), (40, None)))
        assert summary == "LTM summary"

    @pytest.mark.asyncio
    async def test_activation_after_the_cut_is_byte_identical_to_before(self, make_session_manager):
        live = _conversation((8000, None), (40, "pdf-workflows"))
        summary, _, mgr = await _cut(make_session_manager, live)
        assert mgr.compaction_state.checkpoint == 2  # the activation turn is kept
        assert summary == "LTM summary"

    @pytest.mark.asyncio
    async def test_unavailable_skill_is_not_named(self, make_session_manager):
        live = _conversation((8000, "pdf-workflows"), (40, None))
        summary, _, _ = await _cut(make_session_manager, live, available=("doc-drafting",))
        assert summary == "LTM summary"

    @pytest.mark.asyncio
    async def test_line_survives_model_compression(self, make_session_manager, monkeypatch):
        """Over budget, the model compresses the records; the line goes on after, verbatim."""

        async def fake_compress(records, budget_tokens, *, model_id, region, effort=None, send_temperature=True):
            return "Compressed narrative.", False

        monkeypatch.setattr(compaction_summary, "_compress", fake_compress)
        live = _conversation((8000, "pdf-workflows"), (40, None))
        summary, _, mgr = await _cut(
            make_session_manager,
            live,
            summaries=("x " * 2000,),
            summary_model_enabled=True,
            summary_extract_enabled=False,
        )
        assert mgr.compaction_state.policy["summaryOutcome"] == "model"
        assert summary == "Compressed narrative.\n\n" + LINE.format(names="pdf-workflows")

    @pytest.mark.asyncio
    async def test_deferred_cut_parks_the_same_summary(self, make_session_manager):
        live = _conversation((8000, "pdf-workflows"), (40, None))
        store = {}
        mgr = _manager(make_session_manager, store, deferred_apply_enabled=True)
        await mgr.update_after_turn(2100, current_messages=live)
        assert mgr.compaction_state.pending_summary == "LTM summary\n\n" + LINE.format(names="pdf-workflows")


# ---------------------------------------------------------------------------
# The cut: fallback path (no LTM summaries)
# ---------------------------------------------------------------------------

FALLBACK_NO_SKILL = "Previous conversation:\n- User: q0 " + "u" * 147 + "\n- Assistant: a0"


class TestFallbackSummary:
    @pytest.mark.asyncio
    async def test_activation_in_the_cut_adds_the_line(self, make_session_manager):
        live = _conversation((8000, "pdf-workflows"), (40, None))
        summary, _, _ = await _cut(make_session_manager, live, summaries=())
        assert summary == (
            FALLBACK_NO_SKILL + "\n\nTools used: skills\n\n" + LINE.format(names="pdf-workflows")
        )

    @pytest.mark.asyncio
    async def test_no_activation_is_byte_identical_to_before(self, make_session_manager):
        summary, _, _ = await _cut(make_session_manager, _conversation((8000, None), (40, None)), summaries=())
        assert summary == FALLBACK_NO_SKILL

    @pytest.mark.asyncio
    async def test_activation_after_the_cut_is_byte_identical_to_before(self, make_session_manager):
        live = _conversation((8000, None), (40, "pdf-workflows"))
        summary, _, _ = await _cut(make_session_manager, live, summaries=())
        assert summary == FALLBACK_NO_SKILL

    @pytest.mark.asyncio
    async def test_fallback_with_no_text_still_gets_the_line(self, make_session_manager):
        """A cut whose retired turns yield no fallback text keeps the line alone."""
        live = [make_user_message("<attachment/>" + "u" * 8000)] + _turn(0, 0, "pdf-workflows")[1:] + _turn(1, 40)
        live[3] = {"role": "assistant", "content": [{"text": "<xml/>"}]}
        summary, _, _ = await _cut(make_session_manager, live, summaries=())
        assert summary == LINE.format(names="pdf-workflows")


# ---------------------------------------------------------------------------
# Restore: the persisted line is never recomputed
# ---------------------------------------------------------------------------


def _agent(messages):
    agent = MagicMock()
    agent.messages = messages
    return agent


class TestRestoreIsByteStable:
    @pytest.mark.asyncio
    async def test_two_consecutive_restores_after_the_cut_are_identical(self, make_session_manager):
        live = _conversation((8000, "pdf-workflows"), (40, None))
        stored = copy.deepcopy(live)
        _, store, _ = await _cut(make_session_manager, live)

        first = _manager(make_session_manager, store, summaries=("LTM CHANGED",))
        agent1 = _agent(copy.deepcopy(stored))
        first._apply_compaction(agent1)
        second = _manager(make_session_manager, store, summaries=("LTM CHANGED AGAIN",))
        agent2 = _agent(copy.deepcopy(stored))
        second._apply_compaction(agent2)

        assert _dump(agent1.messages) == _dump(agent2.messages)
        head = agent1.messages[0]["content"][0]["text"]
        assert LINE.format(names="pdf-workflows") in head
        # The restore read the persisted text: a changed LTM record changes nothing.
        assert "CHANGED" not in head


# ---------------------------------------------------------------------------
# Where the turn's skill set comes from: the agent's configuration, read after the turn
# ---------------------------------------------------------------------------


class TestTurnSkillSet:
    def test_chat_agent_names_are_slugs_of_its_configured_ids(self):
        from types import SimpleNamespace

        from agents.main_agent.chat_agent import ChatAgent

        fget = ChatAgent.disclosed_skill_names.fget
        assert fget(SimpleNamespace(_accessible_skill_ids=["pdf_workflows", "doc_drafting", ""])) == (
            "doc-drafting",
            "pdf-workflows",
        )
        assert fget(SimpleNamespace(_accessible_skill_ids=None)) == ()

    def test_coordinator_reads_only_a_tuple_of_names(self):
        from types import SimpleNamespace

        from agents.main_agent.streaming.stream_coordinator import _disclosed_skill_names

        assert _disclosed_skill_names(SimpleNamespace(disclosed_skill_names=("a", "b"))) == ("a", "b")
        assert _disclosed_skill_names(SimpleNamespace()) is None
        assert _disclosed_skill_names(MagicMock()) is None
        assert _disclosed_skill_names(SimpleNamespace(disclosed_skill_names=("a", 1))) is None
