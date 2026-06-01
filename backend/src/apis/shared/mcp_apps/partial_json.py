"""Heal a streamed, partial JSON string into a parseable object.

MCP Apps (SEP-1865) lets a host stream a tool call's arguments to an App as
they are generated, via `ui/notifications/tool-input-partial`. The spec is
explicit that the **host** is responsible for closing unterminated
strings/brackets so the partial payload is valid JSON before it is delivered:

    "Partial arguments are 'healed' JSON — the host closes unclosed
     brackets/braces to produce valid JSON."

Bedrock's Converse stream delivers `toolUse.input` as raw JSON-string
fragments (`contentBlockDelta`), which our pipeline accumulates per
`toolUseId`. The accumulated prefix is almost always invalid JSON (mid-string,
dangling comma, half-written value). `heal_partial_json` turns that prefix into
the largest valid JSON **object** it can, so the `tool-input-partial`
notification's `params.arguments` (a `record<string, unknown>` per the spec)
is always a real object.

The App is additionally robust to a truncated last element (Excalidraw's
`parsePartialElements` / `excludeIncompleteLastItem`), so we optimise for
"parses to an object with as much intact as possible" rather than byte-perfect
reconstruction. Pure, dependency-free, and never raises — returns ``None`` when
no object can be recovered (the caller then emits nothing for that delta).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

# Bound the backward trim so a pathological tail can't make healing O(n^2) on a
# large accumulated buffer. The incomplete tail of a streamed value is short in
# practice; if we can't heal within this many chars from the end, we give up
# for this delta and wait for the next one.
_MAX_TRIM = 4096


def _close_candidate(s: str) -> Optional[str]:
    """Build a single best-effort closed form of ``s``.

    Walks the string tracking string/escape state and a stack of open
    containers, terminates an open string, drops a dangling separator, and
    appends the matching closers. Returns ``None`` if ``s`` has no structural
    content to close (e.g. it isn't object/array-shaped yet).
    """
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()

    closed = s
    if in_string:
        # Terminate the open string. A trailing lone backslash would escape our
        # closing quote, so drop it first.
        if escaped:
            closed = closed[:-1]
        closed += '"'

    # Strip trailing whitespace then any dangling separator/operator. A dangling
    # ':' means a key with no value yet — strip the key too (handled here by
    # also removing the now-trailing string key + optional comma).
    closed = closed.rstrip()
    while closed and closed[-1] in ",:":
        drop_colon = closed[-1] == ":"
        closed = closed[:-1].rstrip()
        if drop_colon:
            # Remove the orphaned key string: "...,\"key\"" -> "...,"
            if closed.endswith('"'):
                # walk back to the opening quote of the key
                i = len(closed) - 2
                while i >= 0:
                    if closed[i] == '"' and closed[i - 1] != "\\":
                        break
                    i -= 1
                if i >= 0:
                    closed = closed[:i].rstrip()
            # a leftover comma before the dropped key is handled by the loop

    if not stack and not closed:
        return None
    return closed + "".join(reversed(stack))


def heal_partial_json(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the partial JSON ``raw`` healed into a dict, or ``None``.

    Fast-paths a buffer that is already valid JSON. Otherwise repeatedly trims
    one trailing char and re-closes until the result parses, bounded by
    ``_MAX_TRIM``. Only an object (``dict``) is returned — tool arguments are
    always key/value per the spec; a non-object parse yields ``None``.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    # Fast path: already-complete JSON object.
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        pass

    limit = max(0, len(s) - _MAX_TRIM)
    end = len(s)
    while end > limit:
        candidate = _close_candidate(s[:end])
        if candidate is not None:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
        end -= 1
    return None
