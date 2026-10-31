"""Render stored messages into a transcript container.

One renderer for every way a stored conversation reaches the screen: opening
your own from ``/conversations`` and reading a teammate's shared snapshot.
Tool calls use the same widget a live turn does; a stored ``toolUse`` opens a
record and the matching ``toolResult`` (which Bedrock stores on the *next*
user message) closes it. Attachments are named. Block kinds nobody here can
draw are counted, not silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from textual.widget import Widget

from ..client.agent_events import ToolCallRecord
from ..client.catalog import HistoryMessage
from .agent_content import ToolCall
from .messages import AssistantMessage, Notice, UserMessage


@dataclass
class RenderedHistory:
    """What :func:`render_history` mounted, for callers that keep handles."""

    tool_widgets: dict[str, ToolCall] = field(default_factory=dict)
    records: dict[str, ToolCallRecord] = field(default_factory=dict)
    unrendered: int = 0


async def render_history(container: Widget, messages: Iterable[HistoryMessage], *, model_label: str) -> RenderedHistory:
    """Mount ``messages`` into ``container`` in order. Does not clear it first."""
    out = RenderedHistory()
    for message in messages:
        for result in message.tool_results:
            record = out.records.get(result.tool_use_id)
            if record is not None:
                record.result = result.text
                record.is_error = result.is_error
                widget = out.tool_widgets.get(record.tool_use_id)
                if widget is not None:
                    widget.refresh_from_record()
        if message.attachments:
            names = ", ".join(a.name or a.kind for a in message.attachments)
            await container.mount(Notice(f"Attached: {names}"))
        if message.role == "user" and message.text.strip():
            await container.mount(UserMessage(message.text))
        for use in message.tool_uses:
            record = ToolCallRecord(tool_use_id=use.tool_use_id, name=use.name, arguments=use.arguments)
            out.records[use.tool_use_id] = record
            widget = ToolCall(record)
            out.tool_widgets[use.tool_use_id] = widget
            await container.mount(widget)
        if message.role == "assistant" and message.text.strip():
            assistant = AssistantMessage(model_label)
            await container.mount(assistant)
            if message.reasoning:
                await assistant.append_reasoning(message.reasoning)
            await assistant.append_text(message.text)
        out.unrendered += len(message.other_blocks)
    if out.unrendered:
        await container.mount(Notice(f"{out.unrendered} stored item(s) of a kind this terminal cannot show were skipped."))
    return out
