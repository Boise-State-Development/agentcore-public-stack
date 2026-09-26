"""Canonical memory-file format: frontmatter, items, anchors, links, aliases.

Shared Projects §4.2. A canonical memory file is a system-rendered YAML
frontmatter block followed by an ordered list of **items**: top-level ``- ``
list lines (continuation lines indented two spaces), each ending in an anchor
comment ``<!-- e:{id} -->``. Anchors are minted server-side and give an item
a stable identity across edits, reorders and moves within the file::

    ---
    name: canvas-integration
    description: "Canvas API conventions and enrollment sync decisions"
    aliases: ["canvas", "lms sync"]
    created: 2026-09-01T14:02:00Z
    updated: 2026-09-20T09:41:00Z
    version: 14
    ---
    - Batch enrollment calls in groups of 50. <!-- e:k3m9qxz2 -->
    - Term codes are YYYYTT, see [[sis-conventions]]. <!-- e:7hd0r4tw -->

This module is pure: no I/O, no AWS, no clock except :func:`new_anchor`. It
parses and renders; the rules that need the rest of the space (collisions,
known anchors, link targets) live in ``validation.py``.

**Frontmatter** is a deliberately small YAML subset, because the system renders
it: scalars (plain, ``'single'`` or ``"double"`` quoted), flow lists
(``[a, "b c"]``) and block lists (``- a``). Strings are rendered as JSON
strings, which are valid YAML double-quoted scalars, so an exported file opens
in any YAML tool while this parser never needs a YAML dependency. Nested maps
and multi-line scalars are rejected with a clear message rather than guessed
at.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

# ---- constants ---------------------------------------------------------

INDEX_SLUG = "MEMORY.md"
"""The space index. Reserved: never an ordinary entry, on any write path."""

_RESERVED_SLUGS = frozenset({INDEX_SLUG.casefold()})

MAX_SLUG_CHARS = 128
MAX_DESCRIPTION_CHARS = 160
MAX_ALIASES = 10
MAX_ALIAS_CHARS = 64

# Canonical slugs: lowercase words joined by - _ . and grouped by / (e.g.
# ``people/jane-doe``). Freeform spaces keep accepting any non-empty slug.
_CANONICAL_SLUG_RE = re.compile(
    r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*(?:/[a-z0-9]+(?:[-_.][a-z0-9]+)*)*$"
)

# Anchors are 8 lowercase Crockford base32 characters (40 random bits). They
# only need to be unique within a file, and every item carries one into the
# prompt: measured with CountTokens on Haiku 4.5, an 8-character id costs
# ~10 tokens per item where a 26-character ULID costs ~26.
ANCHOR_LENGTH = 8
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
_ANCHOR_ID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{8}$")

# A well-formed anchor comment at the very end of an item.
_TRAILING_ANCHOR_RE = re.compile(r"[ \t]*<!--[ \t]*e:([0-9A-Za-z]{8})[ \t]*-->\s*\Z")
# Anything that looks like an anchor comment. Used to reject anchors that are
# malformed or sit anywhere other than the end of an item.
_ANY_ANCHOR_RE = re.compile(r"<!--\s*e:")

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")

FRONTMATTER_DELIMITER = "---"
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*)|[ \t]*)$")
_FM_BLOCK_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.*)$")

FrontmatterValue = Union[str, int, List[str]]


class MemoryFormatError(ValueError):
    """A memory file (or part of one) is not in the canonical format.

    ``code`` is a stable machine-readable reason (``prose_in_body``,
    ``unknown_anchor``, …) for metrics and tests; the message is written for
    the person or agent who made the save and says how to fix it.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# ---- data --------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One item: a list entry's text and its anchor (``None`` until minted)."""

    text: str
    anchor: Optional[str] = None


@dataclass(frozen=True)
class Frontmatter:
    """The system-rendered header of a canonical file.

    ``name`` equals the slug. ``scope`` is rendered only when set (spaces gain
    scopes with Shared Projects 2.4). ``version`` counts the file's saves.
    """

    name: str
    description: str = ""
    aliases: Tuple[str, ...] = ()
    created: str = ""
    updated: str = ""
    version: int = 0
    scope: Optional[str] = None


@dataclass(frozen=True)
class ParsedFile:
    """A canonical file split into its parts.

    ``frontmatter`` holds the raw parsed keys exactly as written (``None`` when
    the file has no frontmatter block), so callers can tell "not given" from
    "given empty".
    """

    frontmatter: Optional[Dict[str, FrontmatterValue]]
    items: Tuple[Item, ...] = field(default_factory=tuple)


# ---- anchors -----------------------------------------------------------


def new_anchor(*, randomness: Optional[int] = None) -> str:
    """Mint an anchor id: 40 random bits as 8 base32 characters.

    ``randomness`` exists for deterministic tests. Callers check a fresh id
    against the file's anchors, so a collision is retried, never stored.
    """
    value = (secrets.randbits(40) if randomness is None else randomness) & ((1 << 40) - 1)
    chars = []
    for _ in range(ANCHOR_LENGTH):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def is_valid_anchor(anchor: str) -> bool:
    return bool(_ANCHOR_ID_RE.match(anchor))


def anchor_comment(anchor: str) -> str:
    return f"<!-- e:{anchor} -->"


# Every well-formed anchor comment wherever it sits, with the blanks before it.
_STRIP_ANCHOR_RE = re.compile(r"[ \t]*<!--[ \t]*e:[0-9A-Za-z]{8}[ \t]*-->")


def strip_anchors(text: str) -> str:
    """``text`` without its ``<!-- e:… -->`` anchor comments.

    For memory injected into a prompt, where each anchor costs ~10 tokens per
    item on every turn and the model has no use for it. Anything that edits a
    file reads it with its anchors (``memory_read``), so edits stay anchor-stable.
    """
    return _STRIP_ANCHOR_RE.sub("", text)


# ---- slugs, descriptions, aliases --------------------------------------


def is_reserved_slug(slug: str) -> bool:
    """True for slugs no entry may take (``MEMORY.md``, any casing)."""
    return slug.strip().casefold() in _RESERVED_SLUGS


def validate_slug(slug: str, *, canonical: bool) -> str:
    """Return the slug stripped, or raise if it cannot name an entry."""
    cleaned = (slug or "").strip()
    if not cleaned:
        raise MemoryFormatError("An entry name is required.", code="slug_required")
    if is_reserved_slug(cleaned):
        raise MemoryFormatError(
            f"'{INDEX_SLUG}' is the space index, not an entry. Edit it through the index instead.",
            code="reserved_slug",
        )
    if canonical:
        if len(cleaned) > MAX_SLUG_CHARS:
            raise MemoryFormatError(
                f"Entry names are limited to {MAX_SLUG_CHARS} characters.", code="slug_too_long"
            )
        if not _CANONICAL_SLUG_RE.match(cleaned):
            raise MemoryFormatError(
                f"'{cleaned}' is not a valid file name. Use lowercase letters, digits and "
                "- _ . separators, with / to group files (for example people/jane-doe).",
                code="slug_invalid",
            )
    return cleaned


def normalize_description(description: str) -> str:
    """Collapse a description to one line; reject it past the length limit."""
    text = " ".join((description or "").split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise MemoryFormatError(
            f"The description is {len(text)} characters; the limit is {MAX_DESCRIPTION_CHARS}.",
            code="description_too_long",
        )
    return text


def normalize_aliases(aliases: Sequence[str], *, slug: str) -> Tuple[str, ...]:
    """Clean an alias list: trimmed, one-line, de-duplicated case-insensitively.

    An alias equal to the file's own name is dropped (it resolves anyway).
    Order is kept, so the rendered frontmatter matches what the editor sent.
    """
    seen = {slug.casefold()}
    cleaned: List[str] = []
    for raw in aliases or ():
        if not isinstance(raw, str):
            raise MemoryFormatError("Aliases must be text.", code="alias_invalid")
        alias = " ".join(raw.split())
        if not alias:
            continue
        if len(alias) > MAX_ALIAS_CHARS:
            raise MemoryFormatError(
                f"The alias '{alias[:20]}…' is longer than {MAX_ALIAS_CHARS} characters.",
                code="alias_too_long",
            )
        if "[" in alias or "]" in alias:
            raise MemoryFormatError(
                f"The alias '{alias}' may not contain square brackets.", code="alias_invalid"
            )
        if is_reserved_slug(alias):
            raise MemoryFormatError(f"'{alias}' is reserved and cannot be an alias.", code="alias_reserved")
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(alias)
    if len(cleaned) > MAX_ALIASES:
        raise MemoryFormatError(f"A file can have at most {MAX_ALIASES} aliases.", code="too_many_aliases")
    return tuple(cleaned)


# ---- links -------------------------------------------------------------


def extract_links(text: str) -> List[str]:
    """Return the ``[[name]]`` targets in ``text``, trimmed, first-seen order, unique."""
    seen: set[str] = set()
    out: List[str] = []
    for match in _WIKILINK_RE.findall(text or ""):
        name = match.strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


# ---- frontmatter -------------------------------------------------------


def split_frontmatter(text: str, *, strict: bool = True) -> Tuple[Optional[str], str]:
    """Split ``text`` into ``(frontmatter_text | None, body)``.

    A frontmatter block starts on the first line with ``---`` and ends at the
    next ``---`` (or ``...``) line. An opening delimiter with no close is an
    error when ``strict``; otherwise the whole text is treated as body, which
    is how freeform entries are read.
    """
    normalized = _normalize_newlines(text or "")
    lines = normalized.split("\n")
    if not lines or lines[0].rstrip() != FRONTMATTER_DELIMITER:
        return None, normalized
    for i in range(1, len(lines)):
        if lines[i].rstrip() in (FRONTMATTER_DELIMITER, "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    if strict:
        raise MemoryFormatError(
            "The frontmatter block is not closed: add a '---' line after it.",
            code="frontmatter_unterminated",
        )
    return None, normalized


def parse_frontmatter(fm_text: str) -> Dict[str, FrontmatterValue]:
    """Parse the supported YAML subset into ``{key: str | int | [str]}``.

    ``version`` is returned as an int; every other scalar as a string.
    """
    result: Dict[str, FrontmatterValue] = {}
    lines = fm_text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if raw[0] in " \t":
            raise _fm_error(f"Unexpected indented line in frontmatter: '{stripped[:40]}'.")
        match = _FM_KEY_RE.match(raw.rstrip())
        if not match:
            raise _fm_error(f"Frontmatter lines must look like 'key: value' (got '{stripped[:40]}').")
        key, value_text = match.group(1), (match.group(2) or "").strip()
        if key in result:
            raise _fm_error(f"The frontmatter field '{key}' appears twice.")
        if value_text.startswith("#"):
            value_text = ""
        if value_text:
            if value_text[0] in "|>":
                raise _fm_error(f"Multi-line values are not supported (field '{key}').")
            value: FrontmatterValue = (
                _parse_flow_list(value_text, key) if value_text.startswith("[") else _parse_scalar(value_text, key)
            )
        else:
            block: List[str] = []
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    i += 1
                    continue
                item = _FM_BLOCK_ITEM_RE.match(nxt)
                if not item:
                    if nxt[0] in " \t":
                        raise _fm_error(f"Nested values are not supported (field '{key}').")
                    break
                block.append(_parse_scalar(item.group(1).strip(), key))
                i += 1
            value = block if block else ""
        if key == "version":
            value = _parse_version(value)
        result[key] = value
    return result


def render_frontmatter(fm: Frontmatter) -> str:
    """Render the header block, delimiters included, in a fixed field order."""
    lines = [
        FRONTMATTER_DELIMITER,
        f"name: {_render_scalar(fm.name)}",
        f"description: {_json(fm.description)}",
    ]
    if fm.scope:
        lines.append(f"scope: {_render_scalar(fm.scope)}")
    lines.append("aliases: [" + ", ".join(_json(a) for a in fm.aliases) + "]")
    lines.append(f"created: {_render_scalar(fm.created)}")
    lines.append(f"updated: {_render_scalar(fm.updated)}")
    lines.append(f"version: {int(fm.version)}")
    lines.append(FRONTMATTER_DELIMITER)
    return "\n".join(lines) + "\n"


def _fm_error(message: str) -> MemoryFormatError:
    return MemoryFormatError(message, code="frontmatter_invalid")


def _json(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# Plain (unquoted) scalars are only used for values the system controls
# (slugs, timestamps, scope). Anything a YAML reader could take for another
# type (a keyword, a bare number) or misparse is quoted instead.
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_./:+-]*[A-Za-z0-9_./+-])?$")
_NUMBER_LIKE_RE = re.compile(r"^[0-9._+-]+$")
_YAML_KEYWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "~"})


def _render_scalar(value: str) -> str:
    plain = (
        bool(_PLAIN_SAFE_RE.match(value))
        and not _NUMBER_LIKE_RE.match(value)
        and value.lower() not in _YAML_KEYWORDS
    )
    return value if plain else _json(value)


def _parse_scalar(text: str, key: str) -> str:
    if text[:1] in ('"', "'"):
        double = text[0] == '"'
        end = _find_closing_double_quote(text) if double else _find_closing_single_quote(text)
        rest = "" if end is None else text[end + 1 :].strip()
        if end is None or (rest and not rest.startswith("#")):
            raise _fm_error(f"The value of '{key}' has an unclosed or malformed quote.")
        if not double:
            return text[1:end].replace("''", "'")
        try:
            return json.loads(text[: end + 1])
        except ValueError as exc:
            raise _fm_error(f"The value of '{key}' is not a valid quoted string.") from exc
    if text[:1] in "{&*!%@`":
        raise _fm_error(f"The value of '{key}' uses YAML syntax that is not supported here; quote it.")
    # Plain scalar: a " #" starts a comment.
    comment = re.search(r"[ \t]#", text)
    plain = text[: comment.start()] if comment else text
    return plain.strip()


def _find_closing_double_quote(text: str) -> Optional[int]:
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i
        i += 1
    return None


def _find_closing_single_quote(text: str) -> Optional[int]:
    i = 1
    while i < len(text):
        if text[i] == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def _parse_flow_list(text: str, key: str) -> List[str]:
    """Parse ``[a, "b, c", 'd']``; nested lists and maps are rejected."""
    comment = ""
    items: List[str] = []
    buf = ""
    i = 1
    closed = False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            end = _find_closing_double_quote(text[i:])
            if end is None:
                raise _fm_error(f"The list in '{key}' has an unclosed quote.")
            buf += text[i : i + end + 1]
            i += end + 1
            continue
        if ch == "'":
            end = _find_closing_single_quote(text[i:])
            if end is None:
                raise _fm_error(f"The list in '{key}' has an unclosed quote.")
            buf += text[i : i + end + 1]
            i += end + 1
            continue
        if ch in "[{":
            raise _fm_error(f"Nested values are not supported (field '{key}').")
        if ch == ",":
            items.append(buf)
            buf = ""
            i += 1
            continue
        if ch == "]":
            items.append(buf)
            closed = True
            comment = text[i + 1 :].strip()
            break
        buf += ch
        i += 1
    if not closed:
        raise _fm_error(f"The list in '{key}' is missing its closing ']'.")
    if comment and not comment.startswith("#"):
        raise _fm_error(f"Unexpected text after the list in '{key}'.")
    return [_parse_scalar(part.strip(), key) for part in items if part.strip()]


def _parse_version(value: FrontmatterValue) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise _fm_error("The frontmatter field 'version' must be a whole number.")


# ---- items -------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_item_text(text: str) -> str:
    """The canonical form of an item's text.

    Newlines normalized, trailing whitespace removed from every line, leading
    and trailing blank lines dropped, and the first line left-trimmed. Inner
    blank lines and continuation indentation are kept. Rendering then parsing
    any normalized text gives the same text back.
    """
    lines = [line.rstrip() for line in _normalize_newlines(text or "").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    lines[0] = lines[0].lstrip()
    return "\n".join(lines)


def check_item_text(text: str) -> str:
    """Normalize an item's text and reject the shapes an item cannot hold."""
    normalized = normalize_item_text(text)
    if not normalized:
        raise MemoryFormatError("An item cannot be empty.", code="empty_item")
    if _ANY_ANCHOR_RE.search(normalized):
        raise MemoryFormatError(
            "Anchor comments (<!-- e:… -->) are managed by the system and may only end an item.",
            code="misplaced_anchor",
        )
    return normalized


def parse_items(body: str) -> Tuple[Item, ...]:
    """Parse a canonical body into items.

    Each item starts with a ``- `` line at column 0; indented lines and blank
    lines that follow belong to it. Any other line is prose, which only
    ``MEMORY.md`` may hold. A trailing ``<!-- e:… -->`` becomes the anchor
    (lower-cased); an item without one is new and gets an anchor on save.
    """
    blocks: List[Tuple[int, List[str]]] = []
    for lineno, raw in enumerate(_normalize_newlines(body or "").split("\n"), start=1):
        if raw.startswith("- ") or raw.rstrip() == "-":
            blocks.append((lineno, [raw[2:]]))
        elif blocks and raw.startswith("  "):
            blocks[-1][1].append(raw[2:])
        elif blocks and raw.startswith("\t"):
            blocks[-1][1].append(raw[1:])
        elif not raw.strip():
            if blocks:
                blocks[-1][1].append("")
        else:
            raise MemoryFormatError(
                f"Line {lineno} is not part of a list item: '{raw.strip()[:60]}'. Memory files hold "
                "one fact per '- ' item; only MEMORY.md can hold other text.",
                code="prose_in_body",
            )

    items: List[Item] = []
    for lineno, lines in blocks:
        joined = "\n".join(lines)
        anchor: Optional[str] = None
        match = _TRAILING_ANCHOR_RE.search(joined)
        if match:
            anchor = match.group(1).lower()
            if not is_valid_anchor(anchor):
                raise MemoryFormatError(
                    f"The item on line {lineno} has a malformed anchor.", code="malformed_anchor"
                )
            joined = joined[: match.start()]
        try:
            text = check_item_text(joined)
        except MemoryFormatError as exc:
            raise MemoryFormatError(f"Item on line {lineno}: {exc}", code=exc.code) from exc
        items.append(Item(text=text, anchor=anchor))
    return tuple(items)


def render_items(items: Sequence[Item]) -> str:
    """Render anchored items as a markdown list (one trailing newline).

    Every item must already carry an anchor; minting is the save pipeline's
    job, so a render can never invent identities.
    """
    out: List[str] = []
    for item in items:
        if not item.anchor or not is_valid_anchor(item.anchor):
            raise MemoryFormatError("Every item must have an anchor before it is rendered.", code="missing_anchor")
        lines = item.text.split("\n")
        rendered = [f"- {lines[0]}"] + [f"  {line}" if line else "" for line in lines[1:]]
        rendered[-1] = f"{rendered[-1]} {anchor_comment(item.anchor)}"
        out.extend(rendered)
    return "\n".join(out) + ("\n" if out else "")


# ---- whole files -------------------------------------------------------


def parse_file(text: str) -> ParsedFile:
    """Parse a canonical file (frontmatter optional) strictly."""
    fm_text, body = split_frontmatter(text, strict=True)
    frontmatter = parse_frontmatter(fm_text) if fm_text is not None else None
    return ParsedFile(frontmatter=frontmatter, items=parse_items(body))


def render_file(fm: Frontmatter, items: Sequence[Item]) -> str:
    """Render a canonical file: frontmatter then items."""
    return render_frontmatter(fm) + render_items(items)


def frontmatter_from_parsed(values: Dict[str, FrontmatterValue], *, name: str) -> Frontmatter:
    """Build a :class:`Frontmatter` from parsed values (missing fields default).

    Used to read the header of a stored canonical file, where every field was
    rendered by the system.
    """

    def text(key: str) -> str:
        value = values.get(key, "")
        return value if isinstance(value, str) else ""

    aliases = values.get("aliases") or []
    version = values.get("version", 0)
    return Frontmatter(
        name=text("name") or name,
        description=text("description"),
        aliases=tuple(aliases) if isinstance(aliases, list) else (),
        created=text("created"),
        updated=text("updated"),
        version=version if isinstance(version, int) else 0,
        scope=text("scope") or None,
    )
