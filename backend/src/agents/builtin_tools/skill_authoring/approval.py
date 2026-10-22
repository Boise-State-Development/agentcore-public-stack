"""What the approval card shows for a skill-authoring write (skill-creator spec D9, D10).

The four writes pause through the existing ``tool_approval_required`` interrupt:
``base_agent`` composes :data:`APPROVAL_TOOL_NAMES` into
``MCPExternalApprovalHook``'s gating set, so the paused-turn snapshot, the resume
route, the ``PendingInterrupt`` breadcrumb and the SPA card all work unchanged,
and a decline becomes a ``cancel_tool`` the model reads.

This module only shapes the card. :func:`describe_for_approval` returns the
arguments to show and a message, adding the edit's **reach** for the three tools
that change an existing skill: which of the owner's Agents and projects run it
unpinned (and so get the new version on their next turn) and which pin it.

The reach is looked up when the user is about to be asked, which is after the
model has already decided to call the tool, so it never runs on the turn path
before the first token. The hook calls it only for these four names; every other
tool call costs one set lookup, as before.

The card renders the arguments, not the message, so the reach is the first key of
the arguments it shows. Long text fields are cut to a preview: the card is a
place to read, and the arguments are also persisted on the session row as the
``PendingInterrupt`` breadcrumb, where a 1 MiB file body would not fit in a
DynamoDB item. The full text still reaches the tool unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from agents.main_agent.session.hooks.tool_approval import ApprovalDetails
from apis.shared.skills.reach import lookup_skill_reach

CREATE = "my_skill_create"
UPDATE = "my_skill_update"
WRITE_FILE = "my_skill_write_file"
DELETE_FILE = "my_skill_delete_file"

APPROVAL_TOOL_NAMES = frozenset({CREATE, UPDATE, WRITE_FILE, DELETE_FILE})
_REACH_TOOL_NAMES = frozenset({UPDATE, WRITE_FILE, DELETE_FILE})

# Characters of a long text argument the card shows before cutting it.
PREVIEW_CHARS = 8_000

_CREATE_MESSAGE = (
    "The assistant wants to create a skill in your account. A skill is instructions "
    "that future conversations follow when you turn it on. Check the instructions "
    "before approving."
)
_EDIT_MESSAGE = (
    "The assistant wants to change one of your skills. Check what will be written "
    "before approving."
)


def _preview(value: Any) -> Any:
    if isinstance(value, str) and len(value) > PREVIEW_CHARS:
        hidden = len(value) - PREVIEW_CHARS
        return f"{value[:PREVIEW_CHARS]}\n… [{hidden:,} more characters not shown]"
    return value


def _shown_input(tool_input: Any) -> Dict[str, Any]:
    if not isinstance(tool_input, dict):
        return {"input": _preview(tool_input)}
    return {key: _preview(value) for key, value in tool_input.items()}


async def describe_for_approval(
    owner_id: Optional[str], tool_name: str, tool_input: Any
) -> Optional[ApprovalDetails]:
    """The card for one paused skill-authoring write, or None for any other tool."""
    if tool_name not in APPROVAL_TOOL_NAMES:
        return None
    shown = _shown_input(tool_input)
    if tool_name not in _REACH_TOOL_NAMES:
        return ApprovalDetails(tool_input=shown, message=_CREATE_MESSAGE)

    skill_id = str(shown.get("skill_id") or "")
    reach = await asyncio.to_thread(lookup_skill_reach, skill_id, owner_id or "")
    summary = reach.summary()
    return ApprovalDetails(
        tool_input={"reach": summary, **shown},
        message=f"{_EDIT_MESSAGE} {summary}",
    )
