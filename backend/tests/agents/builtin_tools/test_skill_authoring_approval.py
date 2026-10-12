"""The approval gate for the skill-authoring writes (docs/specs/skill-creator.md D9, D10).

Three layers, from the card up to the real Strands event loop:

- ``describe_for_approval`` shapes the card: reach first for an edit, a preview
  of long text, nothing for a read or for any other tool;
- ``base_agent`` composes the four write names into ``MCPExternalApprovalHook``
  without disturbing the MCP path;
- driven through a real ``Agent`` with a real interrupt, a decline becomes a
  ``cancel_tool`` the model reads and the write never happens, and an approval
  lets it run.
"""

import json
from types import SimpleNamespace
from typing import Any, AsyncIterable
from unittest.mock import MagicMock

import pytest
from strands import Agent
from strands.models.model import Model

from agents.builtin_tools.skill_authoring import approval
from agents.builtin_tools.skill_authoring import tools as skill_tools
from agents.builtin_tools.skill_authoring.approval import (
    APPROVAL_TOOL_NAMES,
    PREVIEW_CHARS,
    describe_for_approval,
)
from apis.shared.skills.reach import SkillBinding, SkillReach

READS = {"my_skill_list", "my_skill_read", "my_skill_read_file"}
WRITES = {"my_skill_create", "my_skill_update", "my_skill_write_file", "my_skill_delete_file"}


@pytest.fixture()
def reach_calls(monkeypatch):
    calls = []

    def fake_lookup(skill_id, owner_id):
        calls.append((skill_id, owner_id))
        return SkillReach(skill_id, [SkillBinding("Advisor", False, True, None)])

    monkeypatch.setattr(approval, "lookup_skill_reach", fake_lookup)
    return calls


class TestCard:
    def test_the_gated_set_is_exactly_the_four_writes(self):
        assert APPROVAL_TOOL_NAMES == WRITES
        assert not APPROVAL_TOOL_NAMES & READS

    @pytest.mark.asyncio
    async def test_other_tools_get_the_default_card(self, reach_calls):
        assert await describe_for_approval("u1", "send_email", {"to": "x"}) is None
        assert await describe_for_approval("u1", "my_skill_read", {"skill_id": "s"}) is None
        assert reach_calls == []

    @pytest.mark.asyncio
    async def test_create_shows_what_will_be_written_and_looks_up_no_reach(self, reach_calls):
        details = await describe_for_approval(
            "u1", "my_skill_create", {"display_name": "X", "instructions": "do y"}
        )
        assert details.tool_input == {"display_name": "X", "instructions": "do y"}
        assert "create a skill" in details.message
        assert reach_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["my_skill_update", "my_skill_write_file", "my_skill_delete_file"])
    async def test_an_edit_leads_with_its_reach(self, reach_calls, tool_name):
        details = await describe_for_approval(
            "u1", tool_name, {"skill_id": "grant_review", "filename": "a.md"}
        )
        assert reach_calls == [("grant_review", "u1")]
        assert list(details.tool_input)[0] == "reach"
        assert '1 shared agent ("Advisor")' in details.tool_input["reach"]
        assert details.tool_input["skill_id"] == "grant_review"
        assert details.tool_input["reach"] in details.message

    @pytest.mark.asyncio
    async def test_long_text_is_previewed_not_dropped(self, reach_calls):
        """The card is persisted as the PendingInterrupt breadcrumb; a 1 MiB
        body would not fit in the session row."""
        body = "x" * (PREVIEW_CHARS + 500)
        details = await describe_for_approval(
            "u1", "my_skill_write_file", {"skill_id": "s", "filename": "a.md", "content": body}
        )
        shown = details.tool_input["content"]
        assert shown.startswith("x" * PREVIEW_CHARS)
        assert shown.endswith("[500 more characters not shown]")


class TestComposition:
    def _hook(self, monkeypatch, mcp_names=frozenset()):
        from agents.main_agent import base_agent

        integration = MagicMock()
        integration.approval_names_for_client.return_value = set(mcp_names)
        monkeypatch.setattr(
            "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
            lambda: integration,
        )
        return base_agent.BaseAgent._build_mcp_external_approval_hook(SimpleNamespace(user_id="u1"))

    def test_the_four_writes_are_gated_and_the_reads_are_not(self, monkeypatch):
        hook = self._hook(monkeypatch)
        for t in skill_tools.build_skill_authoring_tools("s", "u1", None):
            names = hook._approval_names_lookup(t)
            assert (t.tool_name in names) == (t.tool_name in WRITES), t.tool_name

    def test_an_ordinary_tool_is_not_gated(self, monkeypatch):
        hook = self._hook(monkeypatch)
        assert hook._approval_names_lookup(SimpleNamespace(tool_name="web_search")) == set()

    def test_mcp_tools_keep_their_own_set(self, monkeypatch):
        from strands.tools.mcp import MCPAgentTool

        hook = self._hook(monkeypatch, mcp_names={"send_email"})
        mcp_tool = MagicMock(spec=MCPAgentTool)
        mcp_tool.mcp_client = MagicMock()
        assert hook._approval_names_lookup(mcp_tool) == {"send_email"}


# ---------------------------------------------------------------------------
# End to end through the real Strands event loop
# ---------------------------------------------------------------------------


class _ScriptedModel(Model):
    """Calls my_skill_update once, then answers with text, recording what it saw."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list = []

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        self.calls += 1
        self.seen_messages = list(messages)
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": "my_skill_update", "toolUseId": "tu-1"}},
                    "contentBlockIndex": 0,
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({"skill_id": "grant_review", "instructions": "new"})}},
                    "contentBlockIndex": 0,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
        yield {"contentBlockDelta": {"delta": {"text": "ok"}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class _RecordingService:
    def __init__(self) -> None:
        self.updates: list = []

    async def update_my_skill(self, skill_id, updates, user):
        self.updates.append((skill_id, updates, user.user_id))
        return SimpleNamespace(display_name="Grant Review")


def _tool_results(messages):
    return [
        block["toolResult"]
        for m in messages
        for block in (m.get("content") or [])
        if isinstance(block, dict) and "toolResult" in block
    ]


class TestRealInterrupt:
    @pytest.fixture()
    def run(self, monkeypatch, reach_calls):
        service = _RecordingService()
        monkeypatch.setattr(skill_tools, "get_user_skill_service", lambda: service)
        integration = MagicMock()
        monkeypatch.setattr(
            "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
            lambda: integration,
        )
        from agents.main_agent import base_agent

        hook = base_agent.BaseAgent._build_mcp_external_approval_hook(SimpleNamespace(user_id="u1"))
        model = _ScriptedModel()
        agent = Agent(
            model=model,
            tools=skill_tools.build_skill_authoring_tools("sess", "u1", "u1@example.com"),
            hooks=[hook],
            callback_handler=None,
        )

        async def go(prompt):
            result = None
            async for event in agent.stream_async(prompt):
                if "result" in event:
                    result = event["result"]
            return result

        return SimpleNamespace(agent=agent, model=model, service=service, go=go)

    async def _pause(self, run):
        result = await run.go("tighten my grant review skill")
        assert result.stop_reason == "interrupt"
        [interrupt] = result.interrupts
        assert interrupt.reason["type"] == "tool_approval_required"
        assert interrupt.reason["toolName"] == "my_skill_update"
        shown = json.loads(interrupt.reason["toolInput"])
        assert list(shown)[0] == "reach" and "Advisor" in shown["reach"]
        assert run.service.updates == []  # nothing written while paused
        return interrupt

    @pytest.mark.asyncio
    async def test_a_decline_cancels_the_call_and_the_model_reads_why(self, run):
        interrupt = await self._pause(run)

        await run.go([{"interruptResponse": {"interruptId": interrupt.id, "response": "declined"}}])

        assert run.service.updates == []
        [tool_result] = _tool_results(run.model.seen_messages)
        assert tool_result["status"] == "error"
        assert "declined to approve the 'my_skill_update' tool call" in json.dumps(tool_result)

    @pytest.mark.asyncio
    async def test_an_approval_runs_the_write(self, run):
        interrupt = await self._pause(run)

        await run.go([{"interruptResponse": {"interruptId": interrupt.id, "response": "approved"}}])

        assert run.service.updates == [("grant_review", {"instructions": "new"}, "u1")]
        [tool_result] = _tool_results(run.model.seen_messages)
        assert tool_result["status"] == "success"
        assert "activate the skill again" in json.dumps(tool_result)
