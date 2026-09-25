"""Tests for context itemization — the finer rows carved out of the measured
system / tools totals by character share.

The invariant that matters most: every row set sums exactly to the measured
total it came from, because ``prefixTokens`` and compaction read those totals.
"""

import pytest
from strands.hooks import BeforeModelCallEvent

from agents.main_agent.session.hooks.context_attribution import (
    ContextAttributionHook,
    clear_probe_baselines,
    clear_split_memo,
    get_context_breakdown,
)
from agents.main_agent.session.hooks.context_itemization import (
    MAX_SKILL_ROWS,
    apportion,
    itemize_system,
    itemize_tools,
)


class FakeState:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key):
        return self._values.get(key)


class FakeTool:
    def __init__(self, name, description="d", mcp_client=None):
        self.tool_name = name
        self.tool_spec = {"name": name, "description": description, "inputSchema": {"json": {}}}
        if mcp_client is not None:
            self.mcp_client = mcp_client


class FilteredMCPClient:  # name-matched, like the real Gateway client
    pass


class ExternalClient:
    def __init__(self, server_url=None, context_label=None):
        self.server_url = server_url
        if context_label:
            self.context_label = context_label


class FakeRegistry:
    def __init__(self, tools):
        self.registry = {t.tool_name: t for t in tools}
        self.dynamic_tools = {}

    def get_all_tool_specs(self):
        return [t.tool_spec for t in self.registry.values()]


class FakeAgent:
    def __init__(self, system_prompt, tools=(), state=None):
        self.system_prompt = system_prompt
        self.tool_registry = FakeRegistry(list(tools))
        self.state = FakeState(state)


SKILLS_XML = (
    "<available_skills>\n"
    "<skill><name>brand-deck</name><description>" + "x" * 300 + "</description></skill>\n"
    "<skill><name>grader</name><description>" + "y" * 100 + "</description></skill>\n"
    "</available_skills>"
)


def _prompt(*, agent="", personal="", memory="", skills=False):
    text = "PLATFORM FLOOR\n<user_instructions>\nBase prompt.\n\nCurrent date: today"
    if agent:
        text += "\n\n## Assistant-Specific Instructions\n\n" + agent
    if personal:
        text += "\n\n## Personal Instructions\n\n" + personal
    text += "\n</user_instructions>"
    if memory:
        # Since Shared Projects 2.2 the memory block is its own system text
        # block after the wrapper; Strands joins text blocks with a newline.
        text += '\n<memory_space scope="agent" name="Notes" note="...">\n\n### fact\n' + memory + "\n\n</memory_space>"
    if skills:
        text += "\n" + SKILLS_XML
    return text


def _by_key(rows):
    return {r["key"]: r for r in rows}


class TestApportion:
    def test_rows_sum_exactly_to_the_total(self):
        rows = apportion(100, [("a", "A", 1), ("b", "B", 1), ("c", "C", 1)])
        assert sum(r["tokens"] for r in rows) == 100
        assert sorted(r["tokens"] for r in rows) == [33, 33, 34]

    def test_drops_zero_weights_and_handles_nothing_to_split(self):
        assert [r["key"] for r in apportion(10, [("a", "A", 0), ("b", "B", 5)])] == ["b"]
        assert apportion(0, [("a", "A", 5)]) == []
        assert apportion(10, [("a", "A", 0)]) == []


class TestItemizeSystem:
    def test_plain_prompt_stays_one_row(self):
        rows = itemize_system(FakeAgent("Just the default prompt."), 500)
        assert rows == [{"key": "system", "label": "System instructions", "tokens": 500}]

    def test_skills_and_memory_become_top_level_rows_summing_to_the_measured_total(self):
        agent = FakeAgent(
            _prompt(agent="Be a tutor. " * 20, memory="likes tea " * 30, skills=True),
            state={"agent_skills": {"last_injected_xml": SKILLS_XML}},
        )
        rows = itemize_system(agent, 2_000)
        parts = _by_key(rows)
        assert set(parts) == {"system", "skills", "memory"}
        assert sum(r["tokens"] for r in rows) == 2_000
        assert parts["skills"]["tokens"] > 0 and parts["memory"]["tokens"] > 0

    def test_skills_found_without_plugin_state(self):
        rows = itemize_system(FakeAgent(_prompt(skills=True)), 1_000)
        assert "skills" in _by_key(rows)

    def test_system_children_follow_the_composition_headings(self):
        agent = FakeAgent(_prompt(agent="A" * 400, personal="P" * 200))
        system = _by_key(itemize_system(agent, 1_000))["system"]
        children = _by_key(system["children"])
        assert set(children) == {"platform", "agent", "personal"}
        assert children["agent"]["tokens"] > children["personal"]["tokens"]
        assert sum(c["tokens"] for c in system["children"]) == system["tokens"]

    def test_skill_children_are_named_and_sized(self):
        agent = FakeAgent(_prompt(skills=True))
        skills = _by_key(itemize_system(agent, 1_000))["skills"]
        names = [c["label"] for c in skills["children"]]
        assert names == ["brand-deck", "grader"]
        assert sum(c["tokens"] for c in skills["children"]) == skills["tokens"]

    def test_many_skills_fold_into_one_row(self):
        many = "".join(
            f"<skill><name>s{i}</name><description>{'z' * (10 + i)}</description></skill>"
            for i in range(10)
        )
        agent = FakeAgent(_prompt() + f"\n<available_skills>{many}</available_skills>")
        children = _by_key(itemize_system(agent, 1_000))["skills"]["children"]
        assert len(children) == MAX_SKILL_ROWS
        assert children[-1]["label"] == "5 more skills"


class TestItemizeTools:
    def test_groups_by_origin_and_sums_to_the_measured_total(self):
        tools = [
            FakeTool("calculator"),
            FakeTool("fetch_url_content"),
            FakeTool("wikipedia___search", "w" * 400, FilteredMCPClient()),
            FakeTool("list_courses", "c" * 800, ExternalClient(context_label="Canvas")),
            FakeTool("lookup", "l" * 100, ExternalClient(server_url="https://mcp.example.edu/mcp")),
            FakeTool("skills"),
            FakeTool("memory_read"),
        ]
        rows = itemize_tools(FakeAgent("p", tools), 5_000)
        parts = _by_key(rows)
        assert set(parts) == {
            "builtin", "gateway:wikipedia", "mcp:Canvas", "mcp:mcp.example.edu", "skills", "memory",
        }
        assert parts["gateway:wikipedia"]["label"] == "Wikipedia"
        assert parts["mcp:Canvas"]["tokens"] > parts["gateway:wikipedia"]["tokens"]
        assert sum(r["tokens"] for r in rows) == 5_000

    def test_one_origin_is_not_itemized(self):
        assert itemize_tools(FakeAgent("p", [FakeTool("a"), FakeTool("b")]), 500) is None


class TestItemizedOnRead:
    @pytest.fixture(autouse=True)
    def _clear(self):
        clear_split_memo()
        clear_probe_baselines()
        yield
        clear_split_memo()
        clear_probe_baselines()

    @pytest.mark.asyncio
    async def test_partitions_still_sum_to_total_and_keep_measured_totals(self):
        class Model:
            token_count_is_authoritative = True

            async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
                return (1_000 if system_prompt else 0) + len(messages) * 10

        agent = FakeAgent(
            _prompt(agent="Be a tutor. " * 40, skills=True),
            tools=[FakeTool("calculator"), FakeTool("x___y", "q" * 200, FilteredMCPClient())],
        )
        agent.model = Model()
        agent.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        agent._system_prompt_content = None

        await ContextAttributionHook()._on_before_model_call(
            BeforeModelCallEvent(agent=agent, projected_input_tokens=3_000)
        )
        # The hook itself stores only the three measured totals — nothing
        # extra runs before the model call.
        assert [p["key"] for p in get_context_breakdown(agent)["partitions"]] == ["system", "tools", "messages"]

        bd = get_context_breakdown(agent, itemized=True)
        parts = _by_key(bd["partitions"])
        assert sum(p["tokens"] for p in bd["partitions"]) == bd["total"] == 3_000
        # system + skills is the measured system total (1,000); tools = 3,000 - 1,010.
        assert parts["system"]["tokens"] + parts["skills"]["tokens"] == 1_000
        assert parts["tools"]["tokens"] == 1_990
        assert sum(c["tokens"] for c in parts["tools"]["children"]) == 1_990
        assert parts["messages"]["tokens"] == 10

    @pytest.mark.asyncio
    async def test_itemizing_is_memoized_per_agent(self, monkeypatch):
        from agents.main_agent.session.hooks import context_attribution as ca

        calls = []
        real = ca.itemize_tools
        monkeypatch.setattr(ca, "itemize_tools", lambda a, t: calls.append(t) or real(a, t))
        agent = FakeAgent(_prompt(), tools=[FakeTool("calculator"), FakeTool("x___y", "q", FilteredMCPClient())])
        setattr(agent, ca._BREAKDOWN_ATTR, {"total": 900, "partitions": [
            {"key": "system", "label": "System instructions", "tokens": 100},
            {"key": "tools", "label": "Tools", "tokens": 700},
            {"key": "messages", "label": "Messages", "tokens": 100},
        ]})
        first = get_context_breakdown(agent, itemized=True)
        second = get_context_breakdown(agent, itemized=True)
        assert first == second
        assert len(calls) == 1
