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


@dataclass(frozen=True)
class FieldSetLayout:
    """What a field-set file carries besides the set itself.

    ``docs/specs/field-set-authoring.md`` §5.4. The parser deliberately ignores
    four things a hand-written file may hold, and none of them reaches the
    schema, so :class:`FieldSet` has no place for them and the extraction path
    never sees this object. An editor that rewrites the file does need them,
    or a structured save would silently delete an author's notes. Kept apart
    from :class:`FieldSet` so its equality, and everything keyed on it, is
    exactly what it was.

    Every text value is stored trimmed, and an absent one is ``None`` or
    empty, so two layouts that write the same file compare equal.
    """

    #: Prose between the frontmatter and the field table.
    preamble: Optional[str] = None
    #: Lines after the table's header that are not table rows: prose after the
    #: table, and any written between its rows. Written back after the table,
    #: so prose from between two rows moves below it — its words survive, its
    #: position does not, because a markdown table cannot hold prose.
    postscript: Optional[str] = None
    #: Frontmatter lines other than ``title`` and ``version``, as written.
    frontmatter: Tuple[str, ...] = ()
    #: Headers of the columns the parser does not read (a note column), in
    #: authored order. A header may repeat, so these are positions, not keys.
    unmanaged_columns: Tuple[str, ...] = ()
    #: One tuple per field, in field order, holding that row's cells for
    #: :attr:`unmanaged_columns`. Empty when there are no such columns.
    unmanaged_cells: Tuple[Tuple[str, ...], ...] = ()


def default_field_title(field_name: str) -> str:
    """The title a field gets when its row leaves the title cell empty."""
    return field_name.replace("_", " ").title()


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


#: The frontmatter keys the set itself reads. Every other frontmatter line is
#: layout (:attr:`FieldSetLayout.frontmatter`).
_SET_KEYS = frozenset({"title", "version"})


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], Tuple[str, ...], str]:
    """``(frontmatter, other_lines, body)``. Frontmatter is optional.

    Deliberately not YAML: the frontmatter carries a title and a version, both
    scalars, and a real YAML parse would let a field-set file reach for
    structure the format does not have. ``key: value`` lines only.
    ``other_lines`` are the non-blank lines that set neither, as written.
    """
    lines = text.splitlines()
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first >= len(lines) or lines[first].strip() != "---":
        return {}, (), text
    meta: Dict[str, str] = {}
    other: List[str] = []
    for index in range(first + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            return meta, tuple(other), "\n".join(lines[index + 1 :])
        if not stripped:
            continue
        key, colon, value = stripped.partition(":")
        if stripped.startswith("#") or not colon or key.strip().lower() not in _SET_KEYS:
            other.append(stripped)
            continue
        meta[key.strip().lower()] = value.strip().strip("\"'")
    raise FieldSetError("frontmatter opened with '---' but was never closed")


def _prose(lines: Sequence[str]) -> Optional[str]:
    """Lines joined back into one block, trimmed, or ``None`` if blank."""
    text = "\n".join(lines).strip()
    return text or None


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
    return read_field_set_file(text, name=name)[0]


def read_field_set_file(text: str, *, name: str) -> Tuple[FieldSet, FieldSetLayout]:
    """:func:`parse_field_set`, plus everything in the file the set ignores.

    One walk of the file for both, so the editor's idea of which columns and
    lines are unread can never disagree with the parser's (field-set
    authoring §5.4). Raises exactly what :func:`parse_field_set` raises.
    """
    if not _SET_NAME_RE.match(name or ""):
        raise FieldSetError(
            f"field-set name {name!r} must be lowercase words joined by hyphens"
        )
    if not (text or "").strip():
        raise FieldSetError(f"field set '{name}' is empty")

    meta, frontmatter_other, body = _split_frontmatter(text)
    body, rules = _split_rules(body)

    header: Optional[List[str]] = None
    #: Indices of header cells the parser does not read: an unknown header,
    #: or an earlier column that a later alias of the same attribute overrides
    #: (``row[column] = cell`` below keeps the last).
    unmanaged_at: List[int] = []
    unmanaged_headers: Tuple[str, ...] = ()
    unmanaged_rows: List[Tuple[str, ...]] = []
    preamble_lines: List[str] = []
    postscript_lines: List[str] = []
    fields: List[Field] = []
    seen: Dict[str, int] = {}

    for line_number, line in enumerate(body.splitlines(), start=1):
        if "|" not in line:
            # Prose between the frontmatter and the table, and after it. A
            # field set is a document an office reads, not only a payload.
            (preamble_lines if header is None else postscript_lines).append(line)
            continue
        cells = _row_cells(line)
        if header is None:
            lowered = [cell.strip().lower() for cell in cells]
            mapped = [_COLUMN_ALIASES.get(cell) for cell in lowered]
            if "name" not in mapped:
                # Not the field table — a pipe in prose, or a table of
                # something else entirely. Keep looking.
                preamble_lines.append(line)
                continue
            header = [column or "" for column in mapped]
            last = {column: index for index, column in enumerate(header) if column}
            unmanaged_at = [
                index
                for index, column in enumerate(header)
                if not column or last[column] != index
            ]
            unmanaged_headers = tuple(cells[index] for index in unmanaged_at)
            continue
        if _is_delimiter_row(cells):
            continue
        if not any(cells):
            continue

        row: Dict[str, str] = {}
        for column, cell in zip(header, cells):
            if column:
                row[column] = cell
        unmanaged_rows.append(
            tuple(cells[index] if index < len(cells) else "" for index in unmanaged_at)
        )

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
                title=(row.get("title") or "").strip() or default_field_title(field_name),
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

    field_set = FieldSet(
        name=name,
        title=meta.get("title", "").strip() or name,
        version=meta.get("version", "").strip() or "1",
        fields=tuple(fields),
        rules=rules,
    )
    layout = FieldSetLayout(
        preamble=_prose(preamble_lines),
        postscript=_prose(postscript_lines),
        frontmatter=frontmatter_other,
        unmanaged_columns=unmanaged_headers,
        unmanaged_cells=tuple(unmanaged_rows) if unmanaged_headers else (),
    )
    return field_set, layout


# ---------------------------------------------------------------------------
# Serializing
# ---------------------------------------------------------------------------

#: The columns the serializer writes, in this order. Canonical names, never the
#: aliases: an editor that saved ``field`` one week and ``name`` the next would
#: turn every save into a whole-file diff.
_SERIALIZED_COLUMNS = ("name", "title", "type", "find", "enum", "optional")


def _single_line(value: str, *, what: str, name: str) -> str:
    """``value`` with its ends trimmed, refused if it cannot sit on one line."""
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise FieldSetError(
            f"field set '{name}': {what} cannot contain a line break, because "
            "each field is one row of the table"
        )
    return text


def _cell(value: str, *, what: str, name: str) -> str:
    text = _single_line(value, what=what, name=name)
    if "|" in text:
        raise FieldSetError(
            f"field set '{name}': {what} cannot contain '|', which ends a table "
            "cell"
        )
    return text


def _row(cells: Sequence[str]) -> str:
    return "|" + "|".join(f" {cell} " if cell else " " for cell in cells) + "|"


def serialize_field_set(
    field_set: FieldSet, *, layout: Optional[FieldSetLayout] = None
) -> str:
    """The markdown file for ``field_set`` — the inverse of :func:`read_field_set_file`.

    ``docs/specs/field-set-authoring.md`` §4 (D2): the backend owns the format in
    both directions, so the editor never parses or writes it. The output is one
    fixed shape — frontmatter (``title``, ``version``, then any other lines the
    ``layout`` carries), the ``layout``'s preamble prose, the table in authored
    order with canonical column names followed by any unmanaged columns, the
    postscript prose, then ``## Rules`` verbatim — so two saves of one set are
    byte-identical (D5, the diff property).

    The guarantee that matters is the semantic one: reading the output gives
    back exactly ``field_set`` and ``layout``. Rather than trust that every value
    survives the table, the result is read back and compared before it is
    returned, so a value the format cannot carry (a ``|`` in a phrase, a comma
    inside one enum value, a title the frontmatter would strip) is refused
    here, by name, instead of being saved as a different file from the one the
    author wrote.

    Raises:
        FieldSetError: the set cannot be written as a file that reads back as
            itself, or is not a valid set (no fields, more than
            :data:`MAX_FIELDS`, and every other rule :func:`parse_field_set`
            enforces).
    """
    layout = layout or FieldSetLayout()
    name = field_set.name
    title = _single_line(field_set.title, what="the title", name=name)
    version = _single_line(field_set.version, what="the version", name=name)

    extra_columns = [
        _cell(header, what=f"column {header!r}", name=name)
        for header in layout.unmanaged_columns
    ]
    for header in extra_columns:
        reads_as = _COLUMN_ALIASES.get(header.lower())
        if reads_as:
            raise FieldSetError(
                f"field set '{name}': an extra column cannot be headed {header!r}, "
                f"which reads as the '{reads_as}' column"
            )
    if extra_columns and len(layout.unmanaged_cells) != len(field_set.fields):
        raise FieldSetError(
            f"field set '{name}': the extra columns have "
            f"{len(layout.unmanaged_cells)} rows for {len(field_set.fields)} fields"
        )

    expected = FieldSetLayout(
        preamble=_prose([layout.preamble or ""]),
        postscript=_prose([layout.postscript or ""]),
        frontmatter=tuple(
            _single_line(line, what="a frontmatter line", name=name)
            for line in layout.frontmatter
            if line.strip()
        ),
        unmanaged_columns=tuple(extra_columns),
        unmanaged_cells=(),
    )
    if expected.postscript and "|" in expected.postscript:
        raise FieldSetError(
            f"field set '{name}': the text after the table cannot contain '|', "
            "because a line with one reads as a row of the table"
        )

    lines = ["---", f"title: {title}", f"version: {version}", *expected.frontmatter, "---", ""]

    if expected.preamble:
        lines.extend([expected.preamble, ""])

    lines.append(_row([*_SERIALIZED_COLUMNS, *extra_columns]))
    lines.append("|" + "---|" * (len(_SERIALIZED_COLUMNS) + len(extra_columns)))
    extra_rows: List[Tuple[str, ...]] = []
    for index, item in enumerate(field_set.fields):
        label = f"field {item.name!r}"
        values = [
            _cell(value, what=f"{label}: allowed value {value!r}", name=name)
            for value in item.enum
        ]
        for value in values:
            if "," in value:
                raise FieldSetError(
                    f"field set '{name}', {label}: allowed value {value!r} "
                    "cannot contain a comma, which separates allowed values"
                )
        extras: Tuple[str, ...] = ()
        if extra_columns:
            row_cells = layout.unmanaged_cells[index]
            if len(row_cells) != len(extra_columns):
                raise FieldSetError(
                    f"field set '{name}', {label}: {len(row_cells)} extra cells "
                    f"for {len(extra_columns)} extra columns"
                )
            extras = tuple(
                _cell(cell, what=f"{label}: the {header!r} cell", name=name)
                for header, cell in zip(extra_columns, row_cells)
            )
            extra_rows.append(extras)
        lines.append(
            _row(
                [
                    _cell(item.name, what=f"{label}: the name", name=name),
                    _cell(item.title, what=f"{label}: the title", name=name),
                    _cell(item.type, what=f"{label}: the type", name=name),
                    _cell(item.find, what=f"{label}: 'find'", name=name),
                    ", ".join(values),
                    "yes" if item.optional else "no",
                    *extras,
                ]
            )
        )

    if expected.postscript:
        lines.extend(["", expected.postscript])

    rules = (field_set.rules or "").strip()
    if rules:
        lines.extend(["", "## Rules", "", rules])

    text = "\n".join(lines) + "\n"

    expected = FieldSetLayout(
        preamble=expected.preamble,
        postscript=expected.postscript,
        frontmatter=expected.frontmatter,
        unmanaged_columns=expected.unmanaged_columns,
        unmanaged_cells=tuple(extra_rows),
    )
    reparsed, relaid = read_field_set_file(text, name=name)
    if reparsed != field_set:
        raise FieldSetError(
            f"field set '{name}' cannot be written as a file that reads back "
            "the same: " + _first_difference(field_set, reparsed)
        )
    if relaid != expected:
        raise FieldSetError(
            f"field set '{name}' cannot be written as a file that reads back "
            "the same: " + _layout_difference(expected, relaid)
        )
    return text


def _layout_difference(expected: FieldSetLayout, actual: FieldSetLayout) -> str:
    """A sentence naming the first part of the layout that did not survive."""
    described = (
        ("preamble", "the text before the table"),
        ("postscript", "the text after the table"),
        ("frontmatter", "the other frontmatter lines"),
        ("unmanaged_columns", "the extra columns"),
        ("unmanaged_cells", "the extra columns' cells"),
    )
    for attribute, what in described:
        if getattr(expected, attribute) != getattr(actual, attribute):
            return f"{what} would read back as {getattr(actual, attribute)!r}"
    return "the layout differs"  # pragma: no cover - every attribute is checked


def _first_difference(expected: FieldSet, actual: FieldSet) -> str:
    """A sentence naming the first thing that did not survive the round trip."""
    for attribute in ("title", "version", "rules"):
        if getattr(expected, attribute) != getattr(actual, attribute):
            return f"its {attribute} would read back as {getattr(actual, attribute)!r}"
    if len(expected.fields) != len(actual.fields):
        return (
            f"{len(expected.fields)} fields would read back as "
            f"{len(actual.fields)}"
        )
    for want, got in zip(expected.fields, actual.fields):
        if want != got:
            for attribute in ("name", "title", "type", "find", "enum", "optional"):
                if getattr(want, attribute) != getattr(got, attribute):
                    return (
                        f"field {want.name!r}: {attribute} would read back as "
                        f"{getattr(got, attribute)!r}"
                    )
    return "the parsed set differs"  # pragma: no cover - every attribute is checked


def require_field_set_name(filename: str) -> str:
    """The set name in a ``field-set.<name>.md`` filename, or the naming rule.

    Raises:
        FieldSetError: ``<name>`` is not lowercase words joined by hyphens, so
            :func:`field_set_name_for` would read the file as no field set.
    """
    tail = (filename or "").strip().rsplit("/", 1)[-1]
    name = field_set_name_for(tail)
    if name is None:
        raw = tail
        if raw.startswith(FIELD_SET_PREFIX):
            raw = raw[len(FIELD_SET_PREFIX) :]
        if raw.endswith(FIELD_SET_SUFFIX):
            raw = raw[: -len(FIELD_SET_SUFFIX)]
        raise FieldSetError(
            f"field-set name {raw!r} in '{tail}' must be lowercase words joined "
            "by hyphens, e.g. 'field-set.nsf-solicitation.md'"
        )
    return name


def validate_field_set_upload(filename: str, content: bytes) -> Optional[FieldSet]:
    """Parse a bundle file that claims to be a field set, or return ``None``.

    The check every skill-file write path runs (field-set-authoring §4.2, D6),
    unconditionally: a file stored invalid would fail later, mid-turn, in front
    of someone who did not write it. Only the filename decides whether a file is
    a field set, so every other file passes through untouched.

    A file named ``field-set.<something>.md`` whose ``<something>`` is not a valid
    set name is refused too, rather than stored: :func:`field_set_name_for`
    reads it as no field set at all, so the resolver would skip it silently and
    the author would never learn why their set does not appear.

    Raises:
        FieldSetError: the file is not UTF-8, its name is not a valid set name,
            or its contents do not parse.
    """
    tail = (filename or "").strip().rsplit("/", 1)[-1]
    if not tail.startswith(FIELD_SET_PREFIX) or not tail.endswith(FIELD_SET_SUFFIX):
        return None
    name = require_field_set_name(tail)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise FieldSetError(f"field set '{name}' is not UTF-8 text") from None
    return parse_field_set(text, name=name)


__all__ = [
    "Field",
    "FieldSet",
    "FieldSetError",
    "FieldSetLayout",
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
    "default_field_title",
    "field_set_name_for",
    "parse_field_set",
    "read_field_set_file",
    "require_field_set_name",
    "serialize_field_set",
    "validate_field_set_upload",
]
