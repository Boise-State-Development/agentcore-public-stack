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

:func:`validate_frontmatter` checks a parsed frontmatter against the
agentskills.io rules (lenient on input, strict for what this platform writes);
:func:`validate_skill_metadata` applies the same rules to a stored skill's
``skill_metadata``.

:func:`find_unsafe_characters` reports characters that render as nothing or
reorder text (controls, zero-width and bidirectional format characters, the
tag block), for fields a person reads and the model is shown.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

MAX_FRONTMATTER_BYTES = 16 * 1024
MAX_FRONTMATTER_NODES = 2_000
# Nesting of mappings and sequences; the top-level mapping is depth 1.
MAX_FRONTMATTER_DEPTH = 8

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

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
    """Something about the file worth telling its author.

    The parser's notes (``byte_order_mark``, ``frontmatter_unclosed``) are
    warnings about the file as a whole, so ``level`` and ``field`` default to
    that. :func:`validate_frontmatter` sets both: ``level`` is ``"error"`` or
    ``"warning"`` and ``field`` names the frontmatter key.
    """

    code: str
    message: str
    level: str = "warning"
    field: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == LEVEL_ERROR


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


class _YamlSyntaxError(Exception):
    """Plain YAML syntax error: the only failure the colon repair may retry."""

    def __init__(self, nodes_composed: int) -> None:
        super().__init__("not valid YAML")
        self.nodes_composed = nodes_composed


def _load_frontmatter(block: str) -> Any:
    """Load ``block`` through the bounded loader.

    Raises:
        SkillMdError: A bound was broken (alias, anchor, depth, nodes). Never retried.
        _YamlSyntaxError: The block is not valid YAML.
    """
    loader = _BoundedSafeLoader(block)
    try:
        return loader.get_single_data()
    except SkillMdError:
        raise
    except yaml.YAMLError:
        raise _YamlSyntaxError(loader.nodes_composed) from None
    finally:
        loader.dispose()


# ``key: value`` on one line, the key a plain word (Strands' ``_fix_yaml_colons``
# matches the same shape).
_KEY_VALUE_LINE = re.compile(r"^(\s*[A-Za-z_][\w-]*):[ \t]+(\S.*)$")
# A value YAML reads as something other than a plain scalar: quoted, a flow
# collection, a block scalar, an alias, anchor or tag, or a comment.
_NOT_PLAIN_START = tuple("\"'[{|>*&!#%@`")


def quote_colon_values(block: str) -> Tuple[str, List[str]]:
    """Quote unquoted scalar values that contain ``": "``, and name the keys changed.

    The commonest authoring slip in a ``SKILL.md`` is
    ``description: Use when: the user asks``, which YAML reads as a nested
    mapping and refuses. Each such value is rewritten as a double-quoted scalar
    with backslashes and double quotes escaped. A value that starts as anything
    but a plain scalar is left alone.
    """
    lines: List[str] = []
    repaired: List[str] = []
    for line in block.split("\n"):
        match = _KEY_VALUE_LINE.match(line)
        if match:
            key, value = match.group(1), match.group(2).rstrip()
            if ": " in value and not value.startswith(_NOT_PLAIN_START):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                line = f'{key}: "{escaped}"'
                repaired.append(key.strip())
        lines.append(line)
    return "\n".join(lines), repaired


def _load_frontmatter_leniently(block: str) -> Tuple[Any, Optional[Diagnostic]]:
    """Load ``block``; on a YAML syntax error only, retry once with colon values quoted.

    The retried text goes through the same bounded loader, so every bound still
    holds. A refusal for a bound is never retried.
    """
    try:
        return _load_frontmatter(block), None
    except _YamlSyntaxError as first:
        fixed, keys = quote_colon_values(block)
        if not keys:
            raise _invalid_yaml(first.nodes_composed) from None
        try:
            loaded = _load_frontmatter(fixed)
        except _YamlSyntaxError:
            raise _invalid_yaml(first.nodes_composed) from None
    named = ", ".join(keys)
    return loaded, Diagnostic(
        "frontmatter_repaired",
        f"SKILL.md's frontmatter was not valid YAML because the value of {named} "
        "contains \": \". It was read as if quoted. To fix it at the source, put the "
        f"value in double quotes, e.g. {keys[0]}: \"…\".",
        field=keys[0] if len(keys) == 1 else "",
    )


def _invalid_yaml(nodes_composed: int) -> SkillMdError:
    return SkillMdError(
        "SKILL.md's frontmatter is not valid YAML.", nodes_composed=nodes_composed
    )


def parse_skill_md(text: str) -> ParsedSkillMd:
    """Split a ``SKILL.md`` into its YAML frontmatter and its body.

    A leading byte-order mark is dropped and CRLF line ends become LF. No
    frontmatter (or an opening ``---`` that is never closed) means an empty
    mapping and the whole text as the body. A frontmatter that is not valid
    YAML is retried once with unquoted ``": "`` values quoted
    (:func:`quote_colon_values`); a retry that succeeds adds a
    ``frontmatter_repaired`` warning.

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

    loaded: Any = {}
    if block.strip():
        loaded, repaired = _load_frontmatter_leniently(block)
        if repaired is not None:
            diagnostics.append(repaired)
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


# =============================================================================
# Validation (agentskills.io frontmatter rules)
# =============================================================================

# The six top-level keys the agentskills.io specification defines, in the
# order the projection writes them.
STANDARD_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")

NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
COMPATIBILITY_MAX_LENGTH = 500
LICENSE_MAX_LENGTH = 500
METADATA_MAX_KEYS = 50
METADATA_MAX_BYTES = 16 * 1024
ALLOWED_TOOLS_MAX_NAMES = 64

_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _error(field_name: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(code, message, level=LEVEL_ERROR, field=field_name)


def _warning(field_name: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(code, message, level=LEVEL_WARNING, field=field_name)


def _check_name(value: Any) -> List[Diagnostic]:
    if not isinstance(value, str) or not value:
        return [_error("name", "name_missing", "name is required and must be a string.")]
    if len(value) > NAME_MAX_LENGTH:
        return [
            _error(
                "name",
                "name_too_long",
                f"name is {len(value)} characters; the limit is {NAME_MAX_LENGTH}.",
            )
        ]
    if "--" in value or not _NAME_PATTERN.match(value):
        return [
            _error(
                "name",
                "name_invalid",
                "name may use only lowercase letters, digits and single hyphens, and "
                "cannot start or end with a hyphen.",
            )
        ]
    return []


def _check_description(value: Any) -> List[Diagnostic]:
    if not isinstance(value, str) or not value.strip():
        return [
            _error(
                "description",
                "description_missing",
                "description is required and cannot be blank.",
            )
        ]
    length = len(value.strip())
    if length > DESCRIPTION_MAX_LENGTH:
        return [
            _error(
                "description",
                "description_too_long",
                f"description is {length:,} characters; the limit is "
                f"{DESCRIPTION_MAX_LENGTH:,}.",
            )
        ]
    return []


def _check_compatibility(value: Any) -> List[Diagnostic]:
    if not isinstance(value, str) or not value:
        return [
            _error(
                "compatibility",
                "compatibility_invalid",
                "compatibility, when given, must be a non-empty string.",
            )
        ]
    if len(value) > COMPATIBILITY_MAX_LENGTH:
        return [
            _error(
                "compatibility",
                "compatibility_too_long",
                f"compatibility is {len(value)} characters; the limit is "
                f"{COMPATIBILITY_MAX_LENGTH}.",
            )
        ]
    return []


def _check_license(value: Any) -> List[Diagnostic]:
    if not isinstance(value, str):
        return [_error("license", "license_invalid", "license, when given, must be a string.")]
    if len(value) > LICENSE_MAX_LENGTH:
        return [
            _error(
                "license",
                "license_too_long",
                f"license is {len(value)} characters; the limit is {LICENSE_MAX_LENGTH}.",
            )
        ]
    return []


def metadata_serialized_bytes(value: Dict[Any, Any]) -> int:
    """Size of a ``metadata`` map as the validator measures it (compact JSON, UTF-8)."""
    return len(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        .encode("utf-8")
    )


def _check_metadata(value: Any) -> List[Diagnostic]:
    if not isinstance(value, dict):
        return [
            _error(
                "metadata",
                "metadata_invalid",
                "metadata, when given, must be a map of string keys to string values.",
            )
        ]
    problems: List[Diagnostic] = []
    bad = sorted(
        str(key)
        for key, item in value.items()
        if not isinstance(key, str) or not isinstance(item, str)
    )
    if bad:
        named = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        problems.append(
            _error(
                "metadata",
                "metadata_value_not_string",
                f"metadata keys and values must be strings; {named} is not.",
            )
        )
    if len(value) > METADATA_MAX_KEYS:
        problems.append(
            _error(
                "metadata",
                "metadata_too_many_keys",
                f"metadata has {len(value)} keys; the limit is {METADATA_MAX_KEYS}.",
            )
        )
    size = metadata_serialized_bytes(value)
    if size > METADATA_MAX_BYTES:
        problems.append(
            _error(
                "metadata",
                "metadata_too_large",
                f"metadata is {size:,} bytes; the limit is {METADATA_MAX_BYTES // 1024} KiB.",
            )
        )
    return problems


def allowed_tool_names(value: Any) -> Optional[List[str]]:
    """``allowed-tools`` as a list of names, or None if it is neither a string nor a list of strings.

    The specification writes it as one space-separated string; a YAML list is
    accepted on input.
    """
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [name for item in value for name in item.split()]
    return None


def _check_allowed_tools(value: Any) -> List[Diagnostic]:
    names = allowed_tool_names(value)
    if names is None:
        return [
            _error(
                "allowed-tools",
                "allowed_tools_invalid",
                "allowed-tools, when given, must be a space-separated string or a list of names.",
            )
        ]
    if len(names) > ALLOWED_TOOLS_MAX_NAMES:
        return [
            _error(
                "allowed-tools",
                "allowed_tools_too_many",
                f"allowed-tools names {len(names)} tools; the limit is {ALLOWED_TOOLS_MAX_NAMES}.",
            )
        ]
    return []


_OPTIONAL_CHECKS = {
    "license": _check_license,
    "compatibility": _check_compatibility,
    "metadata": _check_metadata,
    "allowed-tools": _check_allowed_tools,
}


def _unknown_key_diagnostics(keys: List[str], *, strict: bool) -> List[Diagnostic]:
    diagnostics: List[Diagnostic] = []
    for key in keys:
        message = (
            f"{key} is not an agentskills.io frontmatter field. "
            + (
                "A conformant SKILL.md has only name, description, license, "
                "compatibility, metadata and allowed-tools."
                if strict
                else "It is kept and written under metadata."
            )
        )
        make = _error if strict else _warning
        diagnostics.append(make(key, "unknown_field", message))
    return diagnostics


def validate_frontmatter(frontmatter: Dict[str, Any], *, strict: bool) -> List[Diagnostic]:
    """Check a ``SKILL.md`` frontmatter against the agentskills.io rules.

    Returns every problem found, in key order, each an ``error`` or a
    ``warning`` naming its field. ``strict`` decides only how a top-level key
    outside the six standard ones is reported: a warning on input (it is kept
    and folded under ``metadata``), an error for output, which must be
    conformant.
    """
    diagnostics = _check_name(frontmatter.get("name"))
    diagnostics += _check_description(frontmatter.get("description"))
    for key, check in _OPTIONAL_CHECKS.items():
        if key in frontmatter:
            diagnostics += check(frontmatter[key])
    unknown = sorted(str(key) for key in frontmatter if key not in STANDARD_KEYS)
    return diagnostics + _unknown_key_diagnostics(unknown, strict=strict)


# Keys a stored skill's ``skill_metadata`` may hold that the row itself owns:
# the projection writes them from the row's own fields, never from here.
ROW_OWNED_METADATA_KEYS = frozenset(
    {"name", "description", "allowed-tools", "instructions", "compose"}
)


# Problems the projection repairs losslessly, so input reports them as warnings
# rather than refusing: a non-string value inside ``metadata`` is written as a
# string, and a non-string ``license`` is kept under ``metadata``. Stored rows
# already hold such values (bundle import keeps YAML ints and booleans), and the
# edit forms send ``skillMetadata`` back unchanged on every save, so refusing
# them would block an edit to an unrelated field. Output stays strict.
_REPAIRED_ON_OUTPUT = {
    "metadata_value_not_string": (
        "metadata keys and values should be strings; {detail} It is kept and "
        "written as a string."
    ),
    "license_invalid": (
        "license should be a string. It is kept and written under metadata."
    ),
}


def _soften_on_input(diagnostic: Diagnostic) -> Diagnostic:
    template = _REPAIRED_ON_OUTPUT.get(diagnostic.code)
    if template is None or not diagnostic.is_error:
        return diagnostic
    detail = diagnostic.message.split("; ", 1)[-1]
    return Diagnostic(
        diagnostic.code,
        template.format(detail=detail),
        level=LEVEL_WARNING,
        field=diagnostic.field,
    )


def validate_skill_metadata(skill_metadata: Optional[Dict[str, Any]]) -> List[Diagnostic]:
    """Lenient check of a stored skill's ``skill_metadata`` (the frontmatter beyond name and description).

    ``license``, ``compatibility``, ``metadata`` and ``allowed-tools`` follow
    the specification's rules, except that a non-string value inside
    ``metadata`` and a non-string ``license`` are warnings, because the
    projection stringifies them; every other key is a warning, because the
    projection keeps it under ``metadata``. A key the row owns (``name``,
    ``description``, ``instructions``, ...) is a warning too: the row's own
    field is what gets written. Callers refuse on errors and pass warnings.
    """
    if skill_metadata is None:
        return []
    if not isinstance(skill_metadata, dict):
        return [_error("skillMetadata", "skill_metadata_invalid", "skillMetadata must be a map.")]
    diagnostics: List[Diagnostic] = []
    for key, check in _OPTIONAL_CHECKS.items():
        if key in skill_metadata:
            diagnostics += [_soften_on_input(d) for d in check(skill_metadata[key])]
    for key in sorted(str(k) for k in skill_metadata if k in ROW_OWNED_METADATA_KEYS):
        diagnostics.append(
            _warning(
                key,
                "row_owned_field",
                f"{key} in skillMetadata is ignored; the skill's own {key} is used.",
            )
        )
    unknown = sorted(
        str(key)
        for key in skill_metadata
        if key not in STANDARD_KEYS and key not in ROW_OWNED_METADATA_KEYS
    )
    return diagnostics + _unknown_key_diagnostics(unknown, strict=False)


def skill_metadata_problem(skill_metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """The first error :func:`validate_skill_metadata` finds, as a sentence, or None.

    Warnings never refuse a write, so they are not reported here.
    """
    for diagnostic in validate_skill_metadata(skill_metadata):
        if diagnostic.is_error:
            return diagnostic.message
    return None
