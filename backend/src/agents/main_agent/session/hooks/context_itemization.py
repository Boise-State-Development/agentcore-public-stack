"""Itemize the measured system / tools totals into what the user recognizes.

``ContextAttributionHook`` measures two stable totals with Bedrock CountTokens:
the system prompt and the tool schemas. Those are authoritative, but they are
not what a user thinks in — "System prompt 9,800" hides that 6,000 of it is the
skill catalog, and "Tools 14,200" hides that one MCP server is most of it.

This module carves the two measured totals into finer rows **by character
share**, at zero extra model calls:

- the system prompt splits into top-level **Skills** (the ``<available_skills>``
  catalog the Strands skills plugin appends) and **Memory** (a bound Memory
  Space block), with the rest left as **System instructions** — itself itemized
  into platform, agent / project, personal and active-mode instructions by the
  fixed headings ``apis/inference_api/chat/routes.py`` composes them under;
- the tool schemas split by origin: built-in tools, each Gateway target, each
  external MCP server, skill tools, memory tools.

Character share is an estimate — different text tokenizes at different rates —
so every itemized figure is approximate, but each set of rows sums exactly to
the measured total it came from, and those totals are what the rest of the
system (cost rows, compaction) reads. No tokenizer ships in the inference image
(tiktoken is dev-only and is an OpenAI encoding anyway), and a CountTokens call
per section would add ~80ms each to a cold start for a display nicety.

Best-effort: callers wrap this, and any failure degrades to the unitemized
partitions.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

# Headings `routes.py` composes the user-instructions section under
# (`compose_agent_system_prompt`, `compose_personal_instructions`,
# `append_active_prompt`), plus the memory block's opening tag
# (`render_memory_block`). Since Shared Projects 2.2 the memory block is its own
# system text block after `</user_instructions>`; Strands joins text blocks with
# a newline, so the tag still starts a line. Matched at a line start.
_SECTION_MARKERS: Sequence[Tuple[str, str]] = (
    ("\n## Assistant-Specific Instructions", "agent"),
    ("\n## Project Instructions", "project"),
    ("\n## Personal Instructions", "personal"),
    ("\n<memory_space", "memory"),
    ("\n## Active Mode:", "mode"),
    # The wrapper's close: whatever follows it (tool guidance) is platform text.
    ("\n</user_instructions>", "platform"),
)

_SYSTEM_CHILD_LABELS: Dict[str, str] = {
    "platform": "Platform instructions",
    "agent": "Agent instructions",
    "project": "Project instructions",
    "personal": "Personal instructions",
    "mode": "Active mode",
}

_SKILLS_STATE_KEY = "agent_skills"  # strands AgentSkills plugin state
_SKILLS_OPEN = "<available_skills>"
_SKILLS_CLOSE = "</available_skills>"
_SKILL_BLOCK = re.compile(r"<skill>.*?</skill>", re.DOTALL)
_SKILL_NAME = re.compile(r"<name>(.*?)</name>", re.DOTALL)

#: At most this many skills are listed by name; the rest fold into one row.
MAX_SKILL_ROWS = 6

_SKILL_TOOL_NAMES = frozenset({"skills", "read_skill_file"})
_MEMORY_TOOL_NAMES = frozenset({"memory_list", "memory_read", "memory_write"})


def apportion(total: int, weights: Sequence[Tuple[str, str, int]]) -> List[Dict[str, Any]]:
    """Split ``total`` across ``(key, label, weight)`` rows by weight.

    Largest-remainder rounding, so the integer rows sum to ``total`` exactly.
    Zero-weight rows are dropped.
    """
    rows = [(k, label, w) for k, label, w in weights if w > 0]
    weight_sum = sum(w for _, _, w in rows)
    if total <= 0 or weight_sum <= 0:
        return []
    raw = [total * w / weight_sum for _, _, w in rows]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    by_fraction = sorted(range(len(rows)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in by_fraction[:remainder]:
        floors[i] += 1
    return [
        {"key": k, "label": label, "tokens": tokens}
        for (k, label, _), tokens in zip(rows, floors)
    ]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _skills_xml(agent: Any, text: str) -> Optional[str]:
    """The skill catalog currently in the system prompt, if there is one."""
    xml = None
    try:
        state = getattr(agent, "state", None)
        plugin_state = state.get(_SKILLS_STATE_KEY) if state is not None else None
        if isinstance(plugin_state, dict):
            xml = plugin_state.get("last_injected_xml")
    except Exception:  # noqa: BLE001 - a missing plugin is the common case
        xml = None
    if isinstance(xml, str) and xml and xml in text:
        return xml
    start = text.find(_SKILLS_OPEN)
    end = text.find(_SKILLS_CLOSE, start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return text[start : end + len(_SKILLS_CLOSE)]
    return None


def _section_chars(text: str) -> Dict[str, int]:
    """Characters of ``text`` per section kind, by the composition headings.

    Each heading claims the text up to the next heading. Only the first
    occurrence of each counts — a later one is most likely an author's own
    heading inside their instructions, not a new section.
    """
    boundaries: List[Tuple[int, str]] = []
    for marker, kind in _SECTION_MARKERS:
        pos = text.find(marker)
        if pos >= 0:
            boundaries.append((pos, kind))
    boundaries.sort()

    chars: Dict[str, int] = {}
    cursor, kind = 0, "platform"
    for pos, next_kind in boundaries:
        chars[kind] = chars.get(kind, 0) + (pos - cursor)
        cursor, kind = pos, next_kind
    chars[kind] = chars.get(kind, 0) + (len(text) - cursor)
    return chars


def _skill_weights(xml: str) -> List[Tuple[str, str, int]]:
    rows = []
    for block in _SKILL_BLOCK.findall(xml):
        match = _SKILL_NAME.search(block)
        name = match.group(1).strip() if match else ""
        if name:
            rows.append((f"skill:{name}", name, len(block)))
    return rows


def _cap_rows(rows: List[Dict[str, Any]], limit: int, noun: str) -> List[Dict[str, Any]]:
    """Keep the ``limit`` largest rows and fold the rest into one."""
    rows = sorted(rows, key=lambda r: r["tokens"], reverse=True)
    if len(rows) <= limit:
        return rows
    head, tail = rows[: limit - 1], rows[limit - 1 :]
    head.append({
        "key": f"{noun}:other",
        "label": f"{len(tail)} more {noun}",
        "tokens": sum(r["tokens"] for r in tail),
    })
    return head


def itemize_system(agent: Any, system_tokens: int) -> List[Dict[str, Any]]:
    """Top-level partitions for the measured system-prompt total.

    Returns ``system`` (with per-section children when there is more than one
    section), then ``skills`` and ``memory`` when present. The rows sum to
    ``system_tokens``.
    """
    text = getattr(agent, "system_prompt", None)
    if not isinstance(text, str) or not text or system_tokens <= 0:
        return [{"key": "system", "label": "System instructions", "tokens": max(0, system_tokens)}]

    skills_xml = _skills_xml(agent, text)
    rest = text.replace(skills_xml, "", 1) if skills_xml else text
    sections = _section_chars(rest)
    memory_chars = sections.pop("memory", 0)
    skills_chars = len(skills_xml) if skills_xml else 0
    instruction_chars = sum(sections.values())

    top = apportion(system_tokens, [
        ("system", "System instructions", instruction_chars),
        ("skills", "Skills", skills_chars),
        ("memory", "Memory", memory_chars),
    ])
    if not top:
        return [{"key": "system", "label": "System instructions", "tokens": system_tokens}]
    if not any(p["key"] == "system" for p in top):
        top.insert(0, {"key": "system", "label": "System instructions", "tokens": 0})

    for partition in top:
        if partition["key"] == "system":
            children = apportion(partition["tokens"], [
                (kind, _SYSTEM_CHILD_LABELS[kind], sections.get(kind, 0))
                for kind in _SYSTEM_CHILD_LABELS
            ])
            if len(children) > 1:
                partition["children"] = children
        elif partition["key"] == "skills" and skills_xml:
            children = apportion(partition["tokens"], _skill_weights(skills_xml))
            if len(children) > 1:
                partition["children"] = _cap_rows(children, MAX_SKILL_ROWS, "skills")
    return top


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _humanize(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).strip()
    return words[:1].upper() + words[1:] if words else value


def _tool_origin(tool: Any, name: str) -> Tuple[str, str]:
    """``(group key, label)`` for where a registered tool came from."""
    client = getattr(tool, "mcp_client", None)
    if client is not None:
        label = getattr(client, "context_label", None)
        if isinstance(label, str) and label:
            return f"mcp:{label}", label
        if type(client).__name__ == "FilteredMCPClient":
            # Gateway tools are named `<target>___<tool>`.
            target = name.split("___", 1)[0] if "___" in name else "gateway"
            return f"gateway:{target}", _humanize(target)
        server_url = getattr(client, "server_url", None)
        host = urlparse(server_url).hostname if isinstance(server_url, str) else None
        return (f"mcp:{host}", host) if host else ("mcp", "MCP tools")
    if name in _SKILL_TOOL_NAMES:
        return "skills", "Skill tools"
    if name in _MEMORY_TOOL_NAMES:
        return "memory", "Memory tools"
    return "builtin", "Built-in tools"


def _registered_tools(agent: Any) -> List[Tuple[Any, Dict[str, Any]]]:
    """``(tool object, spec)`` for every tool the model is offered."""
    registry = getattr(agent, "tool_registry", None)
    tools: List[Any] = []
    for attr in ("registry", "dynamic_tools"):
        mapping = getattr(registry, attr, None)
        if isinstance(mapping, dict):
            tools.extend(mapping.values())
    out = []
    for tool in tools:
        spec = getattr(tool, "tool_spec", None)
        if isinstance(spec, dict):
            out.append((tool, spec))
    return out


def itemize_tools(agent: Any, tool_tokens: int) -> Optional[List[Dict[str, Any]]]:
    """Children of the measured tools total, grouped by origin, or ``None``
    when there is only one group (nothing to itemize)."""
    if tool_tokens <= 0:
        return None
    groups: Dict[str, List[Any]] = {}
    for tool, spec in _registered_tools(agent):
        name = str(spec.get("name") or getattr(tool, "tool_name", "") or "")
        key, label = _tool_origin(tool, name)
        try:
            size = len(json.dumps(spec, sort_keys=True, default=str))
        except (TypeError, ValueError):
            size = len(repr(spec))
        entry = groups.setdefault(key, [label, 0])
        entry[1] += size
    if len(groups) < 2:
        return None
    return apportion(tool_tokens, [(k, label, size) for k, (label, size) in groups.items()])
