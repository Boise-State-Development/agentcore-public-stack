"""Wire models and conversions for the field-set authoring routes.

``docs/specs/field-set-authoring.md`` §4 (D2). The SPA never parses or writes
the field-set format: it sends and receives the JSON here, and the file is
read by :func:`read_field_set_file` and written by :func:`serialize_field_set`,
the same functions the extraction tool's resolver relies on. Nothing in this
module knows a column alias, a delimiter row or a cell rule.

Everything a hand-written file carries that the set does not — prose around
the table, extra frontmatter lines, a note column — rides through the JSON as
opaque strings (§5.4), so opening a set in the editor and saving it never
deletes what an author wrote for their colleagues.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from apis.shared.extraction.field_sets import (
    FIELD_SET_PREFIX,
    FIELD_SET_SUFFIX,
    MAX_FIELDS,
    Field as SetField,
    FieldSet,
    FieldSetError,
    FieldSetLayout,
    default_field_title,
    field_set_name_for,
    read_field_set_file,
    serialize_field_set,
)
from apis.shared.skills.bundle_files import MAX_RESOURCE_BYTES
from apis.shared.skills.models import SkillResourceRef

#: Bounds on the JSON, so a request is refused before any parsing work for its
#: size alone. Deliberately looser than the format's own rules: 15–64 fields
#: reach the parser, whose message explains the 14-field ceiling, rather than a
#: schema error that only says "too long".
_MAX_FIELDS_ON_THE_WIRE = 64
_MAX_EXTRA_COLUMNS = 16
_MAX_ENUM_VALUES = 64
_MAX_FRONTMATTER_LINES = 64
_MAX_TEXT = 4_000


def field_set_filename(name: str) -> str:
    """The bundle filename a set named ``name`` is stored under."""
    return f"{FIELD_SET_PREFIX}{name}{FIELD_SET_SUFFIX}"


class FieldSetFieldBody(BaseModel):
    """One field, as the editor shows it."""

    name: str = Field(..., max_length=128)
    #: Empty takes the parser's default, derived from ``name``.
    title: str = Field("", max_length=_MAX_TEXT)
    type: str = Field("string", max_length=32)
    #: Defaults to empty so a missing phrase reaches the parser, whose message
    #: says why it is required, instead of a bare schema error.
    find: str = Field("", max_length=_MAX_TEXT)
    enum: List[str] = Field(default_factory=list, max_length=_MAX_ENUM_VALUES)
    optional: bool = False
    #: This field's cells for the set's ``unmanagedColumns``, in that order.
    unmanaged_values: List[str] = Field(
        default_factory=list, alias="unmanagedValues", max_length=_MAX_EXTRA_COLUMNS
    )

    model_config = {"populate_by_name": True}


class FieldSetWriteRequest(BaseModel):
    """``PUT`` body: **either** the structured set **or** raw ``markdown``.

    Structured, the backend writes the file; ``title``, ``version`` and
    ``rules`` default to what the parser would read from a file that left them
    out. ``version`` is written exactly as sent and never bumped here (§6): it
    is part of the generated schema's cache key, so a save that changed it on
    its own would force a fresh grammar every time someone pressed Save.

    Raw, the bytes are stored as sent once they parse — the escape hatch for
    anything the form cannot express (§5.4, D7).
    """

    markdown: Optional[str] = Field(None, max_length=MAX_RESOURCE_BYTES)

    title: Optional[str] = Field(None, max_length=_MAX_TEXT)
    version: Optional[str] = Field(None, max_length=64)
    fields: Optional[List[FieldSetFieldBody]] = Field(
        None, max_length=_MAX_FIELDS_ON_THE_WIRE
    )
    rules: Optional[str] = Field(None, max_length=MAX_RESOURCE_BYTES)
    preamble: Optional[str] = Field(None, max_length=MAX_RESOURCE_BYTES)
    postscript: Optional[str] = Field(None, max_length=MAX_RESOURCE_BYTES)
    frontmatter: List[str] = Field(default_factory=list, max_length=_MAX_FRONTMATTER_LINES)
    unmanaged_columns: List[str] = Field(
        default_factory=list, alias="unmanagedColumns", max_length=_MAX_EXTRA_COLUMNS
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one_mode(self) -> "FieldSetWriteRequest":
        structured = self.model_fields_set - {"markdown"}
        if self.markdown is not None and structured:
            raise ValueError(
                "send either 'markdown' or the structured set, not both: "
                f"{', '.join(sorted(structured))} would be ignored"
            )
        if self.markdown is None and self.fields is None:
            raise ValueError("send either 'markdown' or 'fields'")
        return self


class FieldSetFieldResponse(BaseModel):
    name: str
    title: str
    type: str
    find: str
    enum: List[str]
    optional: bool
    unmanaged_values: List[str] = Field(..., alias="unmanagedValues")

    model_config = {"populate_by_name": True}


class FieldSetResponse(BaseModel):
    """One set, parsed, with the file it was read from.

    ``markdown`` is the stored file (or, for a validate-only structured
    ``PUT``, the file that would be stored), so markdown mode needs no second
    call. ``maxFields`` is the parser's measured ceiling, served so the editor's
    "7 of 14" can never disagree with the check behind Save.
    """

    skill_id: str = Field(..., alias="skillId")
    name: str
    filename: str
    title: str
    version: str
    fields: List[FieldSetFieldResponse]
    rules: Optional[str] = None
    preamble: Optional[str] = None
    postscript: Optional[str] = None
    frontmatter: List[str]
    unmanaged_columns: List[str] = Field(..., alias="unmanagedColumns")
    markdown: str
    max_fields: int = Field(MAX_FIELDS, alias="maxFields")

    model_config = {"populate_by_name": True}


class FieldSetSummary(BaseModel):
    """A set as listed: what the manifest knows, without reading the file."""

    name: str
    filename: str
    size: int
    content_hash: str = Field(..., alias="contentHash")

    model_config = {"populate_by_name": True}


class FieldSetListResponse(BaseModel):
    skill_id: str = Field(..., alias="skillId")
    field_sets: List[FieldSetSummary] = Field(..., alias="fieldSets")
    max_fields: int = Field(MAX_FIELDS, alias="maxFields")

    model_config = {"populate_by_name": True}


def list_field_sets(resources: List[SkillResourceRef]) -> List[FieldSetSummary]:
    """The field sets in a skill's manifest, in filename order. No S3 read.

    Only ``reference`` files count: the resolver reads field sets from
    ``references/`` alone, so a same-named asset is not one.
    """
    summaries = []
    for ref in resources:
        name = field_set_name_for(ref.filename)
        if name is None or ref.kind != "reference":
            continue
        summaries.append(
            FieldSetSummary(
                name=name,
                filename=ref.filename,
                size=ref.size,
                content_hash=ref.content_hash,
            )
        )
    return sorted(summaries, key=lambda s: s.filename)


def field_set_response(skill_id: str, name: str, markdown: str) -> FieldSetResponse:
    """Read ``markdown`` into the response shape.

    Raises:
        FieldSetError: the file does not parse.
    """
    field_set, layout = read_field_set_file(markdown, name=name)
    cells = layout.unmanaged_cells or tuple(() for _ in field_set.fields)
    return FieldSetResponse(
        skill_id=skill_id,
        name=name,
        filename=field_set_filename(name),
        title=field_set.title,
        version=field_set.version,
        fields=[
            FieldSetFieldResponse(
                name=item.name,
                title=item.title,
                type=item.type,
                find=item.find,
                enum=list(item.enum),
                optional=item.optional,
                unmanaged_values=list(row),
            )
            for item, row in zip(field_set.fields, cells)
        ],
        rules=field_set.rules,
        preamble=layout.preamble,
        postscript=layout.postscript,
        frontmatter=list(layout.frontmatter),
        unmanaged_columns=list(layout.unmanaged_columns),
        markdown=markdown,
    )


def markdown_for(name: str, body: FieldSetWriteRequest) -> str:
    """The file a ``PUT`` body describes, checked to parse.

    Raw ``markdown`` is returned as sent once it parses. A structured body is
    written by :func:`serialize_field_set`, with every string trimmed at its
    ends and nothing else touched (§6: never normalize whitespace inside
    ``find``), and each empty optional value given the default the parser
    would read, so the round trip the serializer checks holds.

    Raises:
        FieldSetError: the body does not describe a valid set, or names a value
            the file format cannot carry. The message is the parser's or the
            serializer's, and it is what the author reads.
    """
    if body.markdown is not None:
        read_field_set_file(body.markdown, name=name)
        return body.markdown

    fields_in = body.fields or []
    columns = tuple(column.strip() for column in body.unmanaged_columns)
    if not columns and any(item.unmanaged_values for item in fields_in):
        raise FieldSetError(
            f"field set '{name}': a field carries extra values, but the set "
            "names no extra columns for them"
        )

    fields: List[SetField] = []
    cells: List[Tuple[str, ...]] = []
    for item in fields_in:
        field_name = item.name.strip()
        fields.append(
            SetField(
                name=field_name,
                title=item.title.strip() or default_field_title(field_name),
                type=item.type.strip().lower() or "string",
                find=item.find.strip(),
                enum=tuple(value.strip() for value in item.enum if value.strip()),
                optional=item.optional,
            )
        )
        cells.append(tuple(value.strip() for value in item.unmanaged_values))

    field_set = FieldSet(
        name=name,
        title=(body.title or "").strip() or name,
        version=(body.version or "").strip() or "1",
        fields=tuple(fields),
        rules=(body.rules or "").strip() or None,
    )
    layout = FieldSetLayout(
        preamble=(body.preamble or "").strip() or None,
        postscript=(body.postscript or "").strip() or None,
        frontmatter=tuple(line.strip() for line in body.frontmatter if line.strip()),
        unmanaged_columns=columns,
        unmanaged_cells=tuple(cells) if columns else (),
    )
    if not fields:
        # serialize_field_set would refuse too, but only after writing a table
        # with no rows and reading it back; say it plainly.
        raise FieldSetError(f"field set '{name}': the field table has no rows")
    return serialize_field_set(field_set, layout=layout)
