"""Parse a ``SKILL.md`` (YAML frontmatter plus a Markdown body) within fixed bounds.

The one parser for every path that reads a ``SKILL.md`` written outside this
platform: bundle import and the bootstrap seed. It depends on the standard
library and PyYAML only.

The frontmatter is untrusted input, so it is bounded before it is parsed and
while it is composed:

- a block over :data:`MAX_FRONTMATTER_BYTES` is refused before YAML sees it;
- anchors and aliases are refused outright (frontmatter has no use for them);
- the composer counts nodes and nesting depth and stops at
  :data:`MAX_FRONTMATTER_NODES` / :data:`MAX_FRONTMATTER_DEPTH`.

Because the counts are taken while composing, a refused document is never
expanded or constructed into Python objects.

:func:`find_unsafe_characters` reports characters that render as nothing or
reorder text (controls, zero-width and bidirectional format characters, the
tag block), for fields a person reads and the model is shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_FRONTMATTER_NODES = 2_000
# Nesting of mappings and sequences; the top-level mapping is depth 1.
MAX_FRONTMATTER_DEPTH = 8

_BOM = "﻿"
_CLOSING_DELIMITER = re.compile(r"\n---[ \t]*(\n|$)")


class SkillMdError(ValueError):
    """The ``SKILL.md`` cannot be read. The message is written for the person importing it.

    ``nodes_composed`` is how many YAML nodes the composer had seen when it
    stopped (0 when the refusal came before YAML ran).
    """

    def __init__(self, message: str, *, nodes_composed: int = 0) -> None:
        super().__init__(message)
        self.nodes_composed = nodes_composed


@dataclass(frozen=True)
class Diagnostic:
    """Something about the file worth telling its author that did not stop the parse."""

    code: str
    message: str


@dataclass
class ParsedSkillMd:
    """A parsed ``SKILL.md``: lower-cased frontmatter keys, the stripped body, and notes."""

    frontmatter: Dict[str, Any]
    body: str
    diagnostics: List[Diagnostic] = field(default_factory=list)


class _BoundedSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses anchors and aliases and bounds nodes and depth."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.nodes_composed = 0
        self._collection_depth = 0

    def compose_node(self, parent: Optional[yaml.Node], index: Any) -> Optional[yaml.Node]:
        if self.check_event(yaml.AliasEvent):
            raise SkillMdError(
                "SKILL.md's frontmatter uses a YAML alias (*name). Aliases and anchors "
                "are not supported; write each value out in full.",
                nodes_composed=self.nodes_composed,
            )
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise SkillMdError(
                "SKILL.md's frontmatter uses a YAML anchor (&name). Aliases and anchors "
                "are not supported; write each value out in full.",
                nodes_composed=self.nodes_composed,
            )
        self.nodes_composed += 1
        if self.nodes_composed > MAX_FRONTMATTER_NODES:
            raise SkillMdError(
                f"SKILL.md's frontmatter has more than {MAX_FRONTMATTER_NODES:,} values. "
                "Keep the frontmatter to the skill's name, description and a few short "
                "fields, and move the rest into the body or a reference file.",
                nodes_composed=self.nodes_composed,
            )
        is_collection = isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent))
        if not is_collection:
            return super().compose_node(parent, index)
        self._collection_depth += 1
        if self._collection_depth > MAX_FRONTMATTER_DEPTH:
            raise SkillMdError(
                f"SKILL.md's frontmatter is nested more than {MAX_FRONTMATTER_DEPTH} "
                "levels deep. Flatten it.",
                nodes_composed=self.nodes_composed,
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._collection_depth -= 1


def _load_frontmatter(block: str) -> Any:
    loader = _BoundedSafeLoader(block)
    try:
        return loader.get_single_data()
    except SkillMdError:
        raise
    except yaml.YAMLError:
        raise SkillMdError(
            "SKILL.md's frontmatter is not valid YAML.",
            nodes_composed=loader.nodes_composed,
        ) from None
    finally:
        loader.dispose()


def parse_skill_md(text: str) -> ParsedSkillMd:
    """Split a ``SKILL.md`` into its YAML frontmatter and its body.

    A leading byte-order mark is dropped and CRLF line ends become LF. No
    frontmatter (or an opening ``---`` that is never closed) means an empty
    mapping and the whole text as the body.

    Raises:
        SkillMdError: The frontmatter is too large, not valid YAML, not a
            mapping, uses an anchor or alias, or exceeds the node or depth bound.
    """
    diagnostics: List[Diagnostic] = []
    if text.startswith(_BOM):
        text = text[len(_BOM):]
        diagnostics.append(
            Diagnostic("byte_order_mark", "A byte-order mark at the start of SKILL.md was removed.")
        )
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return ParsedSkillMd({}, normalized.strip(), diagnostics)
    match = _CLOSING_DELIMITER.search(normalized[3:])
    if match is None:
        diagnostics.append(
            Diagnostic(
                "frontmatter_unclosed",
                "SKILL.md opens a frontmatter block with --- but never closes it, so the "
                "whole file was read as the body.",
            )
        )
        return ParsedSkillMd({}, normalized.strip(), diagnostics)
    block = normalized[4 : 3 + match.start()]
    body = normalized[3 + match.end() :]

    size = len(block.encode("utf-8"))
    if size > MAX_FRONTMATTER_BYTES:
        raise SkillMdError(
            f"SKILL.md's frontmatter is {size:,} bytes; the limit is "
            f"{MAX_FRONTMATTER_BYTES // 1024} KiB. Move long content into the body."
        )

    loaded = _load_frontmatter(block) if block.strip() else {}
    if not isinstance(loaded, dict):
        raise SkillMdError("SKILL.md's frontmatter must be a set of key: value lines.")
    return ParsedSkillMd({str(k).lower(): v for k, v in loaded.items()}, body.strip(), diagnostics)


# =============================================================================
# Unsafe characters
# =============================================================================


@dataclass(frozen=True)
class UnsafeCharacter:
    """One unsafe character: its index in the string and its code point."""

    index: int
    code_point: int

    @property
    def label(self) -> str:
        """``U+202E``."""
        return f"U+{self.code_point:04X}"


_FORMAT_RANGES = (
    (0x200B, 0x200F),  # zero-width space/joiners, LRM, RLM
    (0x202A, 0x202E),  # bidirectional embeddings and overrides
    (0x2060, 0x2069),  # word joiner, invisible operators, bidirectional isolates
    (0xFEFF, 0xFEFF),  # zero-width no-break space / BOM
    (0xE0000, 0xE007F),  # tag block
)


def _is_unsafe(code_point: int, allow_carriage_return: bool) -> bool:
    if code_point < 0x20:
        if code_point in (0x09, 0x0A):
            return False
        return not (allow_carriage_return and code_point == 0x0D)
    if 0x7F <= code_point <= 0x9F:  # DEL and the C1 controls
        return True
    return any(low <= code_point <= high for low, high in _FORMAT_RANGES)


def find_unsafe_characters(
    text: str, *, allow_carriage_return: bool = False
) -> List[UnsafeCharacter]:
    """Every control, invisible-format or tag character in ``text``, in order.

    Tab and line feed are allowed. ``allow_carriage_return`` allows CR too, for
    a caller that normalises line ends itself.
    """
    return [
        UnsafeCharacter(index, ord(char))
        for index, char in enumerate(text)
        if _is_unsafe(ord(char), allow_carriage_return)
    ]


def unsafe_character_problem(label: str, text: Optional[str]) -> Optional[str]:
    """A sentence naming the first unsafe character in ``text``, or None if it has none.

    ``label`` names the field (``"the description"``) and starts the sentence.
    """
    if not text:
        return None
    found = find_unsafe_characters(text)
    if not found:
        return None
    first = found[0]
    more = f" and {len(found) - 1} more" if len(found) > 1 else ""
    return (
        f"{label} contains an invisible or control character ({first.label} at "
        f"position {first.index}{more}); remove it and try again"
    )
