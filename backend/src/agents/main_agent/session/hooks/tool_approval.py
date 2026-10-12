"""Per-tool approval gate for external MCP tools and the skill-authoring writes.

Pauses the agent before invoking any MCP tool that the admin flagged with
`needs_approval=True` in the tool catalog. The pause uses Strands' interrupt
protocol: the streaming layer surfaces a `tool_approval_required` SSE event
to the frontend, the user clicks Approve/Deny, and the resume request feeds
the decision back here via `event.interrupt(...)`'s return value.

The same gate holds the four built-in skill-authoring writes
(`my_skill_create` / `_update` / `_write_file` / `_delete_file`,
skill-creator spec D10): `base_agent` composes their fixed names into the
gating set, and passes an `ApprovalDetailsLookup` that shapes their card.

Design notes:
- Approval is scoped to the parent MCP server (the same tool name on a
  different server can be unflagged); the gating set comes from
  `ExternalMCPIntegration.approval_names_for_client`.
- A declined approval becomes a `cancel_tool` so the agent sees a tool error
  it can apologize/replan against, rather than a silent no-op.
- The interrupt name is scoped by `toolUseId` so two parallel calls of the
  same tool in one turn produce distinct interrupts (and distinct SSE events
  the frontend can correlate per-prompt).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Set

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
class ApprovalDetails:
    """A tool-specific card: the arguments to show, and the message to send.

    `tool_input` replaces the raw input on the card ("View arguments", which
    is what the SPA renders) and on the persisted breadcrumb; the tool itself
    still receives its real input.
    """

    tool_input: Any
    message: str


# Resolves `(tool_name, tool_input)` for a call the gate is about to pause to
# the card it should show, or None for the default (raw input, default
# message). Awaited only for a tool that is actually being paused, so it costs
# nothing on any other call.
ApprovalDetailsLookup = Callable[[str, Any], Awaitable[Optional[ApprovalDetails]]]


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
        approval_details_lookup: Optional[ApprovalDetailsLookup] = None,
    ):
        """Initialize.

        Args:
            approval_names_lookup: See `ApprovalNamesLookup`.
            approval_details_lookup: See `ApprovalDetailsLookup`. Optional; MCP
                tools use the default card.
        """
        self._approval_names_lookup = approval_names_lookup
        self._approval_details_lookup = approval_details_lookup

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    async def _gate(self, event: BeforeToolCallEvent) -> None:
        approval_names = self._approval_names_lookup(event.selected_tool)
        tool_name = event.tool_use.get("name", "")
        if not approval_names or tool_name not in approval_names:
            return
        tool_input = event.tool_use.get("input")
        details = None
        if self._approval_details_lookup is not None:
            try:
                details = await self._approval_details_lookup(tool_name, tool_input)
            except Exception:  # noqa: BLE001 - a card detail must never skip the gate
                logger.warning("Approval details lookup failed for %s", tool_name, exc_info=True)
        self._require_approval(event, tool_name, tool_input, details)

    def _require_approval(
        self,
        event: BeforeToolCallEvent,
        tool_name: str,
        tool_input: Any,
        details: Optional[ApprovalDetails] = None,
    ) -> None:
        """Interrupt for user approval; cancel the call unless approved.

        `tool_name` / `tool_input` describe the tool the user is approving,
        while the interrupt's `toolUseId` lets the frontend correlate it with
        the actual streamed tool_use block. `details`, when given, replaces the
        input shown on the card and the message.
        """
        tool_use_id = event.tool_use.get("toolUseId", "")
        encoded_input = _encode_tool_input(details.tool_input if details else tool_input)
        message = details.message if details else _DEFAULT_APPROVAL_MESSAGE

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
                "message": message,
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
