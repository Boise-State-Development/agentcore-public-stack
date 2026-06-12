"""Per-tool approval gate for external MCP tools.

Pauses the agent before invoking any MCP tool that the admin flagged with
`needs_approval=True` in the tool catalog. The pause uses Strands' interrupt
protocol: the streaming layer surfaces a `tool_approval_required` SSE event
to the frontend, the user clicks Approve/Deny, and the resume request feeds
the decision back here via `event.interrupt(...)`'s return value.

Design notes:
- Approval is scoped to the parent MCP server (the same tool name on a
  different server can be unflagged); the gating set comes from
  `ExternalMCPIntegration.approval_names_for_client`.
- A declined approval becomes a `cancel_tool` so the agent sees a tool error
  it can apologize/replan against, rather than a silent no-op.
- The interrupt name is scoped by `toolUseId` so two parallel calls of the
  same tool in one turn produce distinct interrupts (and distinct SSE events
  the frontend can correlate per-prompt).
- Tools can be approval-gated through indirection too: in skills mode a
  bound external MCP tool runs behind the `skill_executor` meta-tool, so
  neither `selected_tool` nor `tool_use["name"]` identifies the flagged
  tool. The optional `tool_use_approval_lookup` resolves the folded target
  from the raw tool_use (name + input) in that case — same interrupt, same
  resume; the prompt describes the inner tool, not the executor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Set

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)


def _encode_tool_input(value: Any) -> Optional[str]:
    """JSON-encode tool input for transport + persistence.

    Returns None for empty / missing input so the frontend can skip the
    "Inspect arguments" affordance. `default=str` keeps the call total —
    a tool that snuck a non-JSON-serializable value into its args still
    produces *some* readable string instead of crashing the hook.
    """
    if value is None:
        return None
    if isinstance(value, dict) and not value:
        return None
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


# Resolves a Strands `selected_tool` to the set of MCP-server-exposed tool
# names that the admin flagged needs_approval, scoped to the parent MCP
# server. Returns an empty set when the tool isn't an external MCP tool, or
# when no per-tool flags apply. Indirected so the hook stays decoupled from
# the integration module.
ApprovalNamesLookup = Callable[[Any], Set[str]]


@dataclass(frozen=True)
class FoldedToolApproval:
    """An approval-flagged tool resolved from an indirect (folded) tool_use.

    `tool_name` / `tool_input` describe the inner tool the user is being
    asked to approve (e.g. `gmail_search` and its args), not the meta-tool
    that carries it — the frontend dialog renders them verbatim.
    """

    tool_name: str
    tool_input: Any = None


# Second-chance resolution from the raw `tool_use` dict (name + input) for
# tools that dispatch indirectly — SkillAgent's `skill_executor` meta-tool
# runs folded external MCP tools, so `selected_tool` is the executor and
# `ApprovalNamesLookup` can't gate it. Returns the folded target only when
# that target is approval-flagged; None for everything else. Consulted only
# when the direct path didn't fire.
ToolUseApprovalLookup = Callable[[dict], Optional[FoldedToolApproval]]


# Default user-facing message; admins can extend this later by wiring a
# per-tool message field through the catalog if needed.
_DEFAULT_APPROVAL_MESSAGE = (
    "This tool is configured to require user approval before it runs. "
    "Approve to proceed, or decline to cancel the call."
)


class MCPExternalApprovalHook(HookProvider):
    """Pause the agent for user approval before a flagged MCP tool runs."""

    def __init__(
        self,
        approval_names_lookup: ApprovalNamesLookup,
        tool_use_approval_lookup: Optional[ToolUseApprovalLookup] = None,
    ):
        """Initialize.

        Args:
            approval_names_lookup: See `ApprovalNamesLookup`.
            tool_use_approval_lookup: See `ToolUseApprovalLookup`. Optional.
                When omitted, only directly-selected MCP tools are gated and
                indirectly-dispatched tools (skill meta-tools) bypass the
                approval prompt.
        """
        self._approval_names_lookup = approval_names_lookup
        self._tool_use_approval_lookup = tool_use_approval_lookup

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        approval_names = self._approval_names_lookup(event.selected_tool)
        tool_name = event.tool_use.get("name", "")
        if approval_names and tool_name in approval_names:
            self._require_approval(event, tool_name, event.tool_use.get("input"))
            return

        # Second chance: the call may be a flagged tool hiding behind an
        # indirect dispatcher (skill_executor). The lookup applies the
        # approval check itself, so a non-None result means "gate this".
        folded = self._resolve_folded_target(event.tool_use)
        if folded is not None:
            self._require_approval(event, folded.tool_name, folded.tool_input)

    def _resolve_folded_target(self, tool_use: Any) -> Optional[FoldedToolApproval]:
        if self._tool_use_approval_lookup is None or not isinstance(tool_use, dict):
            return None
        return self._tool_use_approval_lookup(tool_use)

    def _require_approval(
        self, event: BeforeToolCallEvent, tool_name: str, tool_input: Any
    ) -> None:
        """Interrupt for user approval; cancel the call unless approved.

        `tool_name` / `tool_input` describe the tool the user is approving —
        for folded dispatch that's the inner tool, while the interrupt's
        `toolUseId` stays the executor's so the frontend correlates it with
        the actual streamed tool_use block.
        """
        tool_use_id = event.tool_use.get("toolUseId", "")
        encoded_input = _encode_tool_input(tool_input)

        logger.info(
            "Pausing for user approval: tool=%s tool_use_id=%s",
            tool_name,
            tool_use_id,
        )

        # Strands' BeforeToolCallEvent already folds toolUseId into the
        # interrupt id; we add it to the name too so logs and metadata
        # entries are unambiguous when multiple parallel calls are paused.
        response = event.interrupt(
            name=f"tool_approval:{tool_use_id or tool_name}",
            reason={
                "type": "tool_approval_required",
                "toolUseId": tool_use_id,
                "toolName": tool_name,
                "toolInput": encoded_input,
                "message": _DEFAULT_APPROVAL_MESSAGE,
            },
        )

        # The frontend POSTs `{"response": "approved"}` or
        # `{"response": "declined"}`; the routes layer wraps it in
        # `{"interruptResponse": ...}` so by the time the response lands
        # here it's the inner string. Anything other than "approved" is
        # treated as a decline — fail closed.
        if response == "approved":
            return

        event.cancel_tool = (
            f"User declined to approve the {tool_name!r} tool call; "
            "the agent should not invoke it."
        )
