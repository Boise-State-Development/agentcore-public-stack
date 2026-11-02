"""A named, typed, ordered list of fields to pull out of a document.

``docs/specs/structured-extraction.md`` §3–§4. A field set is one markdown
file in a skill's reference bundle: YAML-ish frontmatter for the set's own
metadata, one markdown table whose rows are the fields **in authored order**,
and an optional ``## Rules`` section this slice reads past (§15, G9).

WHY A MARKDOWN TABLE AND NOT A TABLE ROW SOMEWHERE
--------------------------------------------------
The set is reference material inside a Skill, so it inherits four access
decisions already taken and already correct — the skill's visibility, its RBAC
grant, its Agent binding and its project-harness binding (D2). Nothing here
re-asks any of them, and nothing here reads DynamoDB or S3: this module is
handed bytes and returns a :class:`FieldSet`.

ORDER IS DECLARED, NEVER DERIVED
--------------------------------
The fields come back in the order the rows were authored, top to bottom. Not a
dict iteration, not a sort applied on read. Two reasons, and the second is the
expensive one: the generated schema's property order follows this list, and
Bedrock caches a compiled grammar per *identical* schema for 24 hours (§6.4).
A field list that comes back in a different order is a different schema and
pays grammar compilation again — minutes, not cents.

PURITY
------
Standard library only, so the parser is unit-testable without a fixture and
importable from app-api, the Runtime and a Lambda alike. The Pydantic model
built from a :class:`FieldSet` lives next door in :mod:`schema`, which is where
the dependency belongs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The field types
# ---------------------------------------------------------------------------

TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_CURRENCY = "currency"
TYPE_DATE = "date"
TYPE_BOOLEAN = "boolean"
TYPE_ENUM = "enum"
TYPE_LIST = "list"

#: Every type a field may declare. A row naming anything else is an authoring
#: error reported by name, not silently coerced to ``string`` — a mistyped
#: ``type`` that parses as text would produce a schema nobody asked for and a
#: column of plausible answers.
FIELD_TYPES = frozenset(
    {
        TYPE_STRING,
        TYPE_NUMBER,
        TYPE_CURRENCY,
        TYPE_DATE,
        TYPE_BOOLEAN,
        TYPE_ENUM,
        TYPE_LIST,
    }
)

#: Types whose value is a judgement mapping the prose rather than a passage
#: quoted from it. G2's value check does not apply to these, and
#: ``Claim.enum_field`` is derived from this set rather than asked of the model
#: (D7): a schema-declared type cannot be talked out of.
JUDGEMENT_TYPES = frozenset({TYPE_ENUM, TYPE_BOOLEAN})

#: The widest field set that Bedrock will actually compile a grammar for, in
#: our schema shape. **Measured on dev against Haiku 4.5, 2026-10-20** — not
#: chosen, and not the 40 the spec assumed (§16.9).
#:
#: Strict tool use has at least four undocumented capacity limits, each with
#: its own error, and a field set runs into them well before 40:
#:
#:   * 14 fields (nested ``{nullable value, string quote}``) compiles in ~20s.
#:   * 16 fields is refused in 0.29s: "Schemas ... cause performance issues".
#:   * A separate counter caps *union-typed* parameters at 16 outright.
#:   * Flat top-level scalars are far worse: 8 of them produce a 314MB grammar
#:     against a 300MB cap. Nesting each field as its own object is what makes
#:     14 reachable at all.
#:
#: A set wider than this is refused at parse time rather than truncated,
#: because a silently dropped column is a column a reviewer thinks was
#: checked — and far better than the request-time failure the author would
#: otherwise get, which names a grammar rather than their field table.
MAX_FIELDS = 14

#: The filename convention a field set is stored under in a skill's reference
#: bundle: ``field-set.<name>.md``, read at ``references/field-set.<name>.md``.
#:
#: The spec wrote this as ``references/field-sets/<name>.md``. That path is not
#: expressible: ``SkillBundleFiles.validate_filename`` rejects path separators
#: outright, and a manifest entry's standard path is ``{kind_dir}/{filename}``
#: with no nesting. The convention below keeps every property the design rests
#: on — a flat manifest scan with no S3 GET, skill-scoped access, versioning —
#: and gives up only the directory.
FIELD_SET_PREFIX = "field-set."
FIELD_SET_SUFFIX = ".md"

_SET_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class FieldSetError(ValueError):
    """A field-set file that cannot be parsed.

    Raised with a message naming what is wrong and where. The caller turns it
    into a conversational error from the tool, never a 500 (§4, given up #3):
    a malformed table is an authoring mistake, and the author is who needs to
    read the message.
    """


@dataclass(frozen=True)
class Field:
    """One field: what to call it, what shape it is, and how to find it."""

    #: ``snake_case``. The column header, the JSON Schema property and the
    #: claim id handed to G2. Stable across runs — it is what makes two runs
    #: comparable, which is why a rename is a new field and not a relabel.
    name: str
    #: Display label, separate from ``name`` so a header can change without
    #: invalidating last month's rows.
    title: str
    type: str
    #: The phrase that finds it, and the load-bearing field. A field named
    #: ``cost_share`` tells a model almost nothing; "whether the funder
    #: requires institutional cost sharing, and at what percentage of total
    #: project cost" tells it what to look for. This is what an office tunes
    #: when a field under-performs.
    find: str
    #: Allowed values. Required for ``type: enum``, rejected on anything else —
    #: an enum list on a ``string`` field is an author believing a constraint
    #: is being enforced when nothing reads it.
    enum: Tuple[str, ...] = ()
    #: The document may legitimately not contain it. Becomes a nullable union
    #: in the schema, never an absent ``required`` entry (D5).
    optional: bool = False

    @property
    def is_judgement(self) -> bool:
        """Whether G2's value check applies to this field's answer (D7)."""
        return self.type in JUDGEMENT_TYPES


@dataclass(frozen=True)
class FieldSet:
    """A named set of fields, in authored order."""

    name: str
    title: str
    #: Authoring version. Part of the generated model's cache key, so bumping
    #: it is what forces a fresh schema — and therefore a fresh grammar (§6.4).
    version: str
    fields: Tuple[Field, ...]
    #: The body of a ``## Rules`` section, verbatim and unparsed. Carried so a
    #: set authored with rules round-trips rather than being rejected; nothing
    #: in this slice reads it (§15, G9).
    rules: Optional[str] = None

    @property
    def cache_key(self) -> Tuple[str, str]:
        """``(name, version)`` — what the generated model is cached under (D4)."""
        return (self.name, self.version)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

#: Column headers the table may carry, mapped to the attribute they set.
#: Matched case-insensitively with surrounding whitespace stripped, because a
#: markdown table is written by a human and ``Find`` and ``find`` are the same
#: intent. Unknown columns are ignored rather than rejected, so a set can
#: carry a note column for its own authors.
_COLUMN_ALIASES: Dict[str, str] = {
    "name": "name",
    "field": "name",
    "title": "title",
    "label": "title",
    "type": "type",
    "find": "find",
    "search": "find",
    "search phrase": "find",
    "enum": "enum",
    "values": "enum",
    "allowed values": "enum",
    "optional": "optional",
}

_TRUE = frozenset({"true", "yes", "y", "1"})
_FALSE = frozenset({"", "false", "no", "n", "0", "-", "—"})


def field_set_name_for(path_or_filename: str) -> Optional[str]:
    """The set name a bundle path declares, or ``None`` if it declares none.

    Accepts the bare filename or the standard manifest path, matching how
    ``read_skill_file`` resolves a resource. Returns ``None`` for anything that
    is not a field-set file, so a caller can scan a whole manifest with a
    comprehension and no exception handling.
    """
    tail = (path_or_filename or "").strip().rsplit("/", 1)[-1]
    if not tail.startswith(FIELD_SET_PREFIX) or not tail.endswith(FIELD_SET_SUFFIX):
        return None
    name = tail[len(FIELD_SET_PREFIX) : -len(FIELD_SET_SUFFIX)]
    return name if _SET_NAME_RE.match(name) else None


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """``(frontmatter, body)``. Frontmatter is optional.

    Deliberately not YAML: the frontmatter carries a title and a version, both
    scalars, and a real YAML parse would let a field-set file reach for
    structure the format does not have. ``key: value`` lines only.
    """
    lines = text.splitlines()
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first >= len(lines) or lines[first].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    for index in range(first + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            return meta, "\n".join(lines[index + 1 :])
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(":")
        if _:
            meta[key.strip().lower()] = value.strip().strip("\"'")
    raise FieldSetError("frontmatter opened with '---' but was never closed")


def _row_cells(line: str) -> List[str]:
    """Cells of one markdown table row, outer pipes dropped."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_delimiter_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(
        cell and set(cell) <= set("-: ") and "-" in cell for cell in cells
    )


def _parse_bool(raw: str, *, field_name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise FieldSetError(
        f"field '{field_name}': 'optional' must be yes or no, got {raw!r}"
    )


def _parse_enum(raw: str) -> Tuple[str, ...]:
    """Allowed values from one cell.

    Split on commas, then on pipes, so both ``a, b, c`` and ``a | b | c`` work
    — a markdown table author reaching for a pipe inside a cell has already
    escaped it, and accepting both costs nothing.
    """
    text = raw.strip()
    if not text:
        return ()
    separator = "," if "," in text else "|"
    values = [part.strip().strip("`") for part in text.split(separator)]
    return tuple(value for value in values if value)


def _split_rules(body: str) -> Tuple[str, Optional[str]]:
    """``(body_before_rules, rules_or_None)``.

    Split before the table is read so a ``## Rules`` section containing its own
    table cannot be mistaken for the field table.
    """
    match = re.search(r"^##\s+Rules\s*$", body, re.IGNORECASE | re.MULTILINE)
    if match is None:
        return body, None
    rules = body[match.end() :].strip()
    return body[: match.start()], rules or None


def parse_field_set(text: str, *, name: str) -> FieldSet:
    """Parse one field-set markdown file.

    ``name`` comes from the filename (:func:`field_set_name_for`), not from the
    file's own contents: the name is how the model asks for the set and how the
    backend resolves it, so letting the body rename itself would let two files
    claim one name.

    Raises:
        FieldSetError: with a message naming the problem. Every failure here is
            an authoring mistake, and the message is what the author reads.
    """
    if not _SET_NAME_RE.match(name or ""):
        raise FieldSetError(
            f"field-set name {name!r} must be lowercase words joined by hyphens"
        )
    if not (text or "").strip():
        raise FieldSetError(f"field set '{name}' is empty")

    meta, body = _split_frontmatter(text)
    body, rules = _split_rules(body)

    header: Optional[List[str]] = None
    fields: List[Field] = []
    seen: Dict[str, int] = {}

    for line_number, line in enumerate(body.splitlines(), start=1):
        if "|" not in line:
            # Prose between the frontmatter and the table, and after it. A
            # field set is a document an office reads, not only a payload.
            continue
        cells = _row_cells(line)
        if header is None:
            lowered = [cell.strip().lower() for cell in cells]
            mapped = [_COLUMN_ALIASES.get(cell) for cell in lowered]
            if "name" not in mapped:
                # Not the field table — a pipe in prose, or a table of
                # something else entirely. Keep looking.
                continue
            header = [column or "" for column in mapped]
            continue
        if _is_delimiter_row(cells):
            continue
        if not any(cells):
            continue

        row: Dict[str, str] = {}
        for column, cell in zip(header, cells):
            if column:
                row[column] = cell

        field_name = row.get("name", "").strip().strip("`")
        if not field_name:
            raise FieldSetError(
                f"field set '{name}', table row {line_number}: no field name"
            )
        if not _FIELD_NAME_RE.match(field_name):
            raise FieldSetError(
                f"field set '{name}': field name {field_name!r} must be "
                "snake_case, starting with a letter"
            )
        if field_name in seen:
            raise FieldSetError(
                f"field set '{name}': field {field_name!r} is declared twice "
                f"(rows {seen[field_name]} and {line_number}); two columns with "
                "one name cannot both be read"
            )
        seen[field_name] = line_number

        field_type = (row.get("type") or TYPE_STRING).strip().lower() or TYPE_STRING
        if field_type not in FIELD_TYPES:
            raise FieldSetError(
                f"field set '{name}', field {field_name!r}: unknown type "
                f"{field_type!r}. Expected one of "
                f"{', '.join(sorted(FIELD_TYPES))}"
            )

        find = (row.get("find") or "").strip()
        if not find:
            raise FieldSetError(
                f"field set '{name}', field {field_name!r}: 'find' is required — "
                "it is the phrase that locates the value, and a field without "
                "one is a column header with no question behind it"
            )

        values = _parse_enum(row.get("enum", ""))
        if field_type == TYPE_ENUM and not values:
            raise FieldSetError(
                f"field set '{name}', field {field_name!r}: type 'enum' needs "
                "allowed values"
            )
        if field_type != TYPE_ENUM and values:
            raise FieldSetError(
                f"field set '{name}', field {field_name!r}: allowed values are "
                f"only read for type 'enum', not {field_type!r} — nothing would "
                "enforce them here"
            )
        if field_type == TYPE_ENUM and len(set(values)) != len(values):
            raise FieldSetError(
                f"field set '{name}', field {field_name!r}: duplicate allowed values"
            )

        fields.append(
            Field(
                name=field_name,
                title=(row.get("title") or "").strip() or field_name.replace("_", " ").title(),
                type=field_type,
                find=find,
                enum=values,
                optional=_parse_bool(row.get("optional", ""), field_name=field_name),
            )
        )

    if header is None:
        raise FieldSetError(
            f"field set '{name}': no field table found. Expected a markdown "
            "table with at least a 'name' column"
        )
    if not fields:
        raise FieldSetError(f"field set '{name}': the field table has no rows")
    if len(fields) > MAX_FIELDS:
        raise FieldSetError(
            f"field set '{name}': {len(fields)} fields exceeds the limit of "
            f"{MAX_FIELDS}. This is Bedrock's own limit on how complex a "
            "constrained schema may be, measured rather than chosen — a wider "
            "set is refused at request time with an error about grammar size. "
            "Split it into two field sets and run them separately"
        )

    return FieldSet(
        name=name,
        title=meta.get("title", "").strip() or name,
        version=meta.get("version", "").strip() or "1",
        fields=tuple(fields),
        rules=rules,
    )


__all__ = [
    "Field",
    "FieldSet",
    "FieldSetError",
    "FIELD_TYPES",
    "JUDGEMENT_TYPES",
    "MAX_FIELDS",
    "FIELD_SET_PREFIX",
    "FIELD_SET_SUFFIX",
    "TYPE_STRING",
    "TYPE_NUMBER",
    "TYPE_CURRENCY",
    "TYPE_DATE",
    "TYPE_BOOLEAN",
    "TYPE_ENUM",
    "TYPE_LIST",
    "field_set_name_for",
    "parse_field_set",
]
