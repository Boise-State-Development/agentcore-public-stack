"""The Pydantic model a field set becomes, and the schema Bedrock enforces.

``docs/specs/structured-extraction.md`` §6. One :class:`FieldSet` becomes one
generated model, which is three things at once: the tool spec sent on the
side-channel call, the validator applied to what comes back, and the shape the
artifact and workbook writers iterate. One source of truth rather than a
generator plus a parallel dataclass.

WHAT IS ENFORCED, AND BY WHAT
-----------------------------
``BedrockModel(strict_tools=True)`` puts ``strict: true`` on the request's tool
spec and runs Strands' ``ensure_strict_json_schema`` over its ``inputSchema``,
so the model's arguments are constrained by a compiled grammar rather than by
hope. The JSON Schema subset that mode accepts is narrower than JSON Schema
(§6.3): no ``minimum``/``maximum``, no ``minLength``/``maxLength``, no
recursion, and ``additionalProperties`` only ever ``false``. Everything this
module emits stays inside it, and every constraint it cannot express is
checked in Python afterwards — which is the better place for a range check
anyway, because a constraint the grammar enforces is one the model cannot
report honestly.

EVERY PROPERTY STAYS REQUIRED
-----------------------------
A missing key and an explicit ``null`` are different facts, and only one of
them is the model answering the question (D5). So no property is ever dropped
from ``required``; absence is always a null the model chose to emit. Strands
does not do this for us — ``ensure_strict_json_schema``'s
``require_all_properties`` defaults to ``False`` for Bedrock — so it is our
rule to keep, and it has a second payoff: strict mode caps optional parameters
across a request by an undocumented number, and a 40-field set declares zero
of them (§6.5).

**Where this departs from the spec.** D5 reads as "``optional: true`` becomes a
nullable union", implying a declared-required field is non-nullable. That
cannot hold with §14, which requires a field the document does not answer to
come back ``null`` rather than invented — a non-nullable value forces the model
to write something. So every value here is nullable, and ``optional`` records
the author's expectation instead of the schema's: a null on a field the author
marked required is a *finding* for the reviewer, not a schema violation. D5's
load-bearing half is kept exactly — nothing leaves ``required``.

SCHEMA STABILITY IS A COST PROPERTY
-----------------------------------
Bedrock compiles a grammar on first use of a schema — "up to a few minutes" —
then caches it 24 hours per account for identical schemas (§6.4). Two runs of
one field set must therefore be the same schema, which means the same generated
class: a fresh class per call also misses Strands' own ``_TOOL_SPEC_CACHE``,
which keys on the model type. Hence :func:`model_for_field_set`'s cache.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Type

from pydantic import (
    BaseModel,
    ConfigDict,
    Field as PydanticField,
    WithJsonSchema,
    create_model,
)

from .field_sets import (
    TYPE_BOOLEAN,
    TYPE_CURRENCY,
    TYPE_DATE,
    TYPE_ENUM,
    TYPE_LIST,
    TYPE_NUMBER,
    Field,
    FieldSet,
)

#: Names a generated property may not be bound to as an attribute, because
#: ``BaseModel`` already uses them. A field set is authored by a grants office,
#: not by a Python programmer, and ``json`` or ``copy`` is a perfectly
#: reasonable column name — so the attribute is renamed and the JSON property
#: keeps the authored name through an alias.
_RESERVED = frozenset(dir(BaseModel)) | {"model_config", "model_fields"}

#: A date is a ``string`` with ``format: "date"`` — and the format has to land
#: *inside* the string branch of the nullable union, not beside it. Pydantic's
#: ``json_schema_extra`` on the field puts it beside the ``anyOf``, where every
#: validator ignores it; annotating the inner type puts it where one reads it.
_DateString = Annotated[str, WithJsonSchema({"type": "string", "format": "date"})]

#: ``Optional[List[str]]`` is not written as such, because Strands'
#: ``convert_pydantic_to_tool_spec`` collapses the nullable union by pushing
#: ``null`` down into every branch — including ``items``, which would make a
#: list of nulls schema-valid. The list itself is nullable (the document may
#: not state it); its entries are not.
_StringList = Annotated[
    Optional[List[str]],
    WithJsonSchema({"type": ["array", "null"], "items": {"type": "string"}}),
]


def _value_annotation(field: Field) -> Any:
    """The annotation for one field's ``value``, always nullable.

    ``currency`` is a string on purpose (D6): the document says "$500,000", and
    a float discards the symbol and invents precision. The numeric comparison
    that matters is G2's value check, which already handles currency symbols
    and thousands separators.
    """
    if field.type == TYPE_ENUM:
        # `None` is a member of the enum, not a nullable wrapper around it.
        # `Optional[Literal[...]]` emits `{"enum": [...], "type": ["string",
        # "null"]}` once Strands collapses the union — a schema that says null
        # is permitted by `type` and refused by `enum`, where `enum` wins. The
        # model would then have no legal way to report a value the document
        # does not state, which is exactly the invention §14 tests for. An
        # enum of nulls is in the supported subset (§6.3).
        return Literal[tuple(field.enum) + (None,)]  # type: ignore[valid-type]
    if field.type == TYPE_NUMBER:
        return Optional[float]
    if field.type == TYPE_BOOLEAN:
        return Optional[bool]
    if field.type == TYPE_LIST:
        return _StringList
    if field.type == TYPE_DATE:
        return Optional[_DateString]
    # string and currency are plain nullable strings.
    return Optional[str]


def _value_field_info(field: Field) -> Any:
    description = f"The value for '{field.title}'. {field.find}"
    if field.optional:
        description += " This field may legitimately be absent from the document."
    description += (
        " Use null if the document does not state it — do not infer, estimate "
        "or carry a value over from another field."
    )
    return PydanticField(..., description=description)


def _answer_model(field: Field, set_name: str) -> Type[BaseModel]:
    """The per-field ``{value, quote}`` object.

    The quote is a *sibling* of the value rather than a separate top-level
    property (§7), which keeps the two inseparable: a value cannot arrive
    without the slot for its evidence, and the pairing is what G2 verifies.
    """
    return create_model(
        f"{_class_stem(set_name)}_{field.name}",
        __config__=ConfigDict(extra="forbid"),
        value=(_value_annotation(field), _value_field_info(field)),
        quote=(
            Optional[str],
            PydanticField(
                ...,
                description=(
                    "The exact, verbatim passage from the document that this "
                    "value is taken from — copied character for character, not "
                    "paraphrased or reconstructed. Null if the document does "
                    "not state the value."
                ),
            ),
        ),
    )


def _class_stem(set_name: str) -> str:
    return "".join(part.capitalize() for part in set_name.split("-")) or "FieldSet"


def _attribute_name(field_name: str, used: Dict[str, str]) -> str:
    """A safe attribute name for a property, with the property name preserved."""
    candidate = field_name
    while candidate in _RESERVED or candidate in used:
        candidate = f"{candidate}_"
    return candidate


def build_model(field_set: FieldSet) -> Type[BaseModel]:
    """Build the generated model for *field_set*, uncached.

    Property order follows :attr:`FieldSet.fields`, which is the authored order
    of the markdown rows (D4). ``dict`` preserves insertion order and
    ``create_model`` preserves the kwargs' order, so the emitted schema's
    ``properties`` and ``required`` both come out in that order — which is what
    makes two runs of one set the same schema.
    """
    definitions: Dict[str, Tuple[Any, Any]] = {}
    aliases: Dict[str, str] = {}
    for field in field_set.fields:
        attribute = _attribute_name(field.name, aliases)
        aliases[attribute] = field.name
        definitions[attribute] = (
            _answer_model(field, field_set.name),
            PydanticField(
                ...,
                alias=field.name,
                description=f"{field.title}: {field.find}",
            ),
        )

    return create_model(
        f"{_class_stem(field_set.name)}Extraction",
        __config__=ConfigDict(extra="forbid", populate_by_name=True),
        __doc__=(
            f"Values for the '{field_set.title}' field set, read from one "
            "document. Every field must be present; use a null value and a "
            "null quote for anything the document does not state."
        ),
        **definitions,
    )


def field_set_fingerprint(field_set: FieldSet) -> str:
    """A stable digest of everything about *field_set* that shapes its schema.

    Part of the cache key beside ``(name, version)``. The spec keyed the cache
    on ``(set, version)`` alone, which is one edit away from being wrong: an
    author who changes a ``find`` phrase without bumping ``version`` would be
    served the previous schema for the life of the process, and the fix would
    look like a model regression. Including the content means a cache hit is
    exactly "the same schema", which is the property §6.4 actually needs — and
    two identical sets still share one class, so grammar reuse is unaffected.
    """
    digest = hashlib.sha256()
    for field in field_set.fields:
        digest.update(
            "\x1f".join(
                (
                    field.name,
                    field.title,
                    field.type,
                    field.find,
                    "\x1e".join(field.enum),
                    "1" if field.optional else "0",
                )
            ).encode("utf-8")
        )
        digest.update(b"\x1d")
    return digest.hexdigest()[:16]


_MODEL_CACHE: Dict[Tuple[str, str, str], Type[BaseModel]] = {}


def model_for_field_set(field_set: FieldSet) -> Type[BaseModel]:
    """The generated model for *field_set*, cached per ``(name, version, digest)``.

    The cache is part of the design rather than a tuning detail (D4): one class
    per field-set version is what makes two runs the same schema, and therefore
    what lets Bedrock reuse a compiled grammar instead of spending minutes
    building another identical one.
    """
    key = (field_set.name, field_set.version, field_set_fingerprint(field_set))
    cached = _MODEL_CACHE.get(key)
    if cached is None:
        cached = build_model(field_set)
        _MODEL_CACHE[key] = cached
    return cached


def reset_model_cache() -> None:
    """Drop the generated-model cache. For tests only."""
    _MODEL_CACHE.clear()


__all__ = [
    "build_model",
    "field_set_fingerprint",
    "model_for_field_set",
    "reset_model_cache",
]
