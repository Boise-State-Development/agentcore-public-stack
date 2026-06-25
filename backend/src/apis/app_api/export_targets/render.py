"""Render a conversation transcript into an exportable document.

Pure formatting: takes already-fetched messages and produces bytes plus a
MIME type for an export-target adapter to upload. Paging a session into this
list (calling `get_messages` until the cursor is exhausted) is the caller's
job — keeping this module free of I/O so it is trivially unit-testable.

Markdown is the intermediate representation: the `MARKDOWN` format emits it
directly, and the `GOOGLE_DOC` format converts it to HTML (which Drive's
import turns into a native Doc, so Markdown structure maps to real styling).
"""

from __future__ import annotations

import base64
import html as html_lib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from markdown_it import MarkdownIt

from apis.shared.sessions.models import Citation, MessageContent, MessageResponse

from apis.app_api.export_targets.models import ExportFormat, ExportInclude

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System"}

_DEFAULT_TITLE = "Conversation"


@dataclass
class RenderedDocument:
    """Bytes to upload plus how to upload them.

    `mime_type` is the *source* MIME of `content` (text/markdown or text/html);
    the destination decides what to convert it into. `suggested_name` is the
    document title (Google Doc) or filename stem (Markdown file).
    """

    content: bytes
    mime_type: str
    suggested_name: str


def render_transcript(
    title: str,
    messages: Sequence[MessageResponse],
    fmt: ExportFormat,
    include: Optional[ExportInclude] = None,
) -> RenderedDocument:
    """Render `messages` into a document of the requested format.

    Raises `ValueError` for a format this renderer cannot produce (e.g. PDF,
    which needs a renderer dependency we don't ship yet — Drive's export
    adapter deliberately doesn't advertise it).
    """
    include = include or ExportInclude()
    clean_title = (title or "").strip() or _DEFAULT_TITLE
    markdown = _to_markdown(clean_title, messages, include)

    if fmt == ExportFormat.MARKDOWN:
        return RenderedDocument(
            content=markdown.encode("utf-8"),
            mime_type="text/markdown",
            suggested_name=f"{clean_title}.md",
        )
    if fmt == ExportFormat.GOOGLE_DOC:
        return RenderedDocument(
            content=_markdown_to_html(clean_title, markdown).encode("utf-8"),
            mime_type="text/html",
            suggested_name=clean_title,
        )
    raise ValueError(f"Unsupported export format for transcript render: {fmt}")


# ── markdown assembly ──────────────────────────────────────────────────────


def _to_markdown(
    title: str, messages: Sequence[MessageResponse], include: ExportInclude
) -> str:
    lines: List[str] = [f"# {title}", ""]
    for msg in messages:
        if msg.role == "user" and not include.user_messages:
            continue
        if msg.role == "assistant" and not include.assistant_messages:
            continue

        heading = _ROLE_LABELS.get(msg.role, msg.role.title())
        if include.timestamps and msg.created_at:
            heading = f"{heading} · {msg.created_at}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.extend(_render_blocks(msg.content, include))
        if include.citations and msg.citations:
            lines.extend(_render_citations(msg.citations))

    # Collapse to a single trailing newline.
    return "\n".join(lines).rstrip() + "\n"


def _render_blocks(
    content: Sequence[MessageContent], include: ExportInclude
) -> List[str]:
    out: List[str] = []
    for block in content:
        btype = block.type
        if btype == "text" and block.text:
            out.append(block.text.strip())
            out.append("")
        elif btype == "reasoningContent" and include.reasoning:
            out.extend(_render_reasoning(block.reasoning_content))
        elif btype == "toolUse" and include.tool_calls:
            out.extend(_render_tool_use(block.tool_use))
        elif btype == "toolResult" and include.tool_calls:
            out.extend(_render_tool_result(block.tool_result))
        elif btype == "image" and include.images:
            out.extend(_render_image(block.image))
        elif btype == "document":
            # Raw document blobs are never inlined (size + fidelity); always
            # a placeholder so the reader knows something was attached.
            name = (block.document or {}).get("name") if block.document else None
            out.append(f"_[attached document: {name or 'document'}]_")
            out.append("")
    return out


def _render_tool_use(tool_use: Optional[Dict[str, Any]]) -> List[str]:
    if not tool_use:
        return []
    name = tool_use.get("name", "tool")
    out = [f"**🛠 Tool call: `{name}`**", ""]
    tool_input = tool_use.get("input")
    if tool_input not in (None, {}, ""):
        out += ["```json", _safe_json(tool_input), "```", ""]
    return out


def _render_tool_result(tool_result: Optional[Dict[str, Any]]) -> List[str]:
    if not tool_result:
        return []
    status = tool_result.get("status")
    out = [f"**Tool result**{f' ({status})' if status else ''}", ""]
    text = _tool_result_text(tool_result.get("content"))
    if text:
        out += ["```", text, "```", ""]
    return out


def _tool_result_text(content: Any) -> str:
    if not content:
        return ""
    if not isinstance(content, list):
        return str(content)
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("text") is not None:
            parts.append(str(item["text"]))
        elif "json" in item:
            parts.append(_safe_json(item["json"]))
        else:
            parts.append(_safe_json(item))
    return "\n".join(p for p in parts if p)


def _render_image(image: Optional[Dict[str, Any]]) -> List[str]:
    if not image:
        return []
    fmt = image.get("format", "png")
    data = (image.get("source") or {}).get("bytes")
    if isinstance(data, (bytes, bytearray)):
        data = base64.b64encode(bytes(data)).decode("ascii")
    if not data:
        return ["_[image]_", ""]
    return [f"![image](data:image/{fmt};base64,{data})", ""]


def _render_reasoning(reasoning: Optional[Dict[str, Any]]) -> List[str]:
    text = _reasoning_text(reasoning)
    if not text:
        return []
    out = ["> **Reasoning**", ">"]
    out += [f"> {line}" for line in text.splitlines()]
    out.append("")
    return out


def _reasoning_text(reasoning: Optional[Dict[str, Any]]) -> str:
    if not reasoning:
        return ""
    reasoning_text = reasoning.get("reasoningText")
    if isinstance(reasoning_text, dict):
        return str(reasoning_text.get("text") or "")
    if isinstance(reasoning_text, str):
        return reasoning_text
    return str(reasoning.get("text") or "")


def _render_citations(citations: Sequence[Citation]) -> List[str]:
    out = ["**Sources:**", ""]
    out += [f"- {c.file_name}" for c in citations]
    out.append("")
    return out


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# ── markdown -> html ───────────────────────────────────────────────────────


def _markdown_to_html(title: str, markdown: str) -> str:
    """Convert Markdown to a standalone HTML document.

    Uses the CommonMark preset with `html` forced off — the preset otherwise
    allows raw HTML passthrough (per the CommonMark spec), so we override it so
    HTML in message text is escaped, not injected into the uploaded doc — plus
    the table rule so Markdown tables become real Docs tables on import.
    """
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    body = parser.render(markdown)
    safe_title = html_lib.escape(title)
    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8">'
        f"<title>{safe_title}</title></head>\n"
        f"<body>\n{body}</body></html>\n"
    )
