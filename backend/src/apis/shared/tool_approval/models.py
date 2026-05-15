"""Models for the per-tool approval interrupt flow.

Mirrors `apis.shared.oauth.models.OAuthRequiredEvent`: a Strands hook raises
an interrupt before invoking a flagged tool, the streaming layer surfaces
the SSE event to the frontend, the user clicks Approve/Deny, and the
resume request feeds the decision back via `interrupt_responses`.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolApprovalRequiredEvent(BaseModel):
    """SSE event signalling that an MCP tool needs user approval before it runs.

    Emitted mid-turn when `MCPExternalApprovalHook` raises a Strands interrupt:
    the in-flight tool call is held in `_interrupt_state`, the frontend
    receives this event and renders an inline approve/deny prompt, and the
    user's decision POSTs back to `/invocations` with an
    `interrupt_responses` entry whose `response` is `"approved"` or
    `"declined"`.

    `tool_input` is a JSON-encoded string rather than a structured object —
    the same shape is persisted as a `PendingInterrupt` breadcrumb (where
    DynamoDB would otherwise coerce floats to Decimal), so we use one shape
    end-to-end and let the frontend render the string verbatim.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = "tool_approval_required"
    interrupt_id: str = Field(..., alias="interruptId")
    tool_use_id: str = Field(..., alias="toolUseId")
    tool_name: str = Field(..., alias="toolName")
    tool_input: Optional[str] = Field(default=None, alias="toolInput")
    message: str

    def to_sse_format(self) -> str:
        import json

        return (
            f"event: tool_approval_required\n"
            f"data: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"
        )
