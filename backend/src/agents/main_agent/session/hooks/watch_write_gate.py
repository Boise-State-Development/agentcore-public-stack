"""A watch run asks before it writes (docs/specs/proactive-trust.md §7.5, §12 Q7).

A quiet watcher runs unattended every few minutes, so a ``write``-class call (positive
evidence it changes something: MCP ``destructiveHint`` or the catalog's ``writes``
marker) pauses for the owner's approval unless the schedule lists the tool in
``actWithoutAsking``. The pause is the ordinary approval pause, so slice 2's path is
unchanged downstream: a ``tool_approval_required`` frame, a ``needs_user`` history row,
an ``agent_needs_approval`` bell row and the watcher's hold.

* **Built only for a watch run** (``run_mode: "watch"`` on a headless session, the
  request's or the paused run's snapshot), whose agent is never cached, so the hook
  dies with the run. A standard schedule is audit only, with no new pauses.
* **``unclassified`` calls are not gated**, and calls the approval gate already pauses
  (``needs_approval``, the skill-authoring writes) are skipped: Strands refuses two
  interrupts with one name and runs every ``Before`` callback either way.
* **An exemption names one tool on one server** (``{catalogId}/{toolName}``), never a
  whole server.

No I/O: it reads the agent's tool facts and the list it was built with.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, FrozenSet, Iterable, Optional

from strands.hooks import BeforeToolCallEvent

from agents.main_agent.core.action_audit import WRITE, ToolFacts, classify_call
from agents.main_agent.session.hooks.tool_approval import ApprovalDetails, MCPExternalApprovalHook

logger = logging.getLogger(__name__)

#: Evidence another gate already pauses on, so this one leaves the call alone.
_GATED_ELSEWHERE = frozenset({"needs_approval", "skill_write"})

WATCH_WRITE_MESSAGE = (
    "This watcher wants to run a tool that changes something. Approve to let it run this once, "
    "or decline to skip it. To let it run without asking, add the tool to the schedule's "
    "Act without asking list."
)


def exemption_key(tool: Any, tool_name: str, facts: ToolFacts) -> Optional[str]:
    """``{catalogId}/{toolName}`` for a call, the form ``actWithoutAsking`` lists.

    ``toolName`` is the server's own name for the tool: the MCP tool's name, or for a
    Gateway tool (``<target>___<tool>``) the part after the target.
    """
    try:
        catalog_id = facts.tool_id(tool)
    except Exception:  # noqa: BLE001
        catalog_id = None
    if not catalog_id:
        return None
    raw = getattr(getattr(tool, "mcp_tool", None), "name", None) or tool_name
    if "___" in raw:
        raw = raw.split("___", 1)[1]
    return f"{catalog_id}/{raw}"


class WatchWriteGate:
    """Pauses a watch run's ``write``-class calls for approval, unless exempted."""

    def __init__(self, facts: Callable[[], ToolFacts], act_without_asking: Iterable[str] = ()) -> None:
        self._facts = facts
        self._exempt: FrozenSet[str] = frozenset(str(entry) for entry in act_without_asking or ())
        self._approval = MCPExternalApprovalHook(
            approval_names_lookup=self._names_to_gate,
            approval_details_lookup=self._details,
        )

    def register(self, strands_agent: Any) -> None:
        """Add the gate to ``strands_agent`` (the watch run's own, never a cached one)."""
        strands_agent.add_hook(self._approval._gate, BeforeToolCallEvent)

    def should_gate(self, tool: Any, tool_name: str) -> bool:
        facts = self._facts()
        verdict = classify_call(tool, tool_name, facts)
        if verdict is None or verdict.call_class != WRITE or verdict.why in _GATED_ELSEWHERE:
            return False
        key = exemption_key(tool, tool_name, facts)
        if key is not None and key in self._exempt:
            logger.info("Watch run: %s runs without asking (actWithoutAsking)", key)
            return False
        return True

    def _names_to_gate(self, selected_tool: Any) -> set[str]:
        name = str(getattr(selected_tool, "tool_name", "") or "")
        return {name} if name and self.should_gate(selected_tool, name) else set()

    async def _details(self, tool_name: str, tool_input: Any) -> ApprovalDetails:
        return ApprovalDetails(tool_input=tool_input, message=WATCH_WRITE_MESSAGE)
