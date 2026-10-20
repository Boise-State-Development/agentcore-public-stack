"""The schema a field set becomes, against Bedrock's strict-mode subset.

``docs/specs/structured-extraction.md`` §6.3, §6.4, §6.5, D4–D6. Three classes
of property, and the second is the one that costs money if it regresses:

* **The subset is respected.** Strict tool use accepts a narrower JSON Schema
  than JSON Schema. A schema using an unsupported feature fails at request
  time with a ``ValidationException``, which is a failure a unit test can
  prevent entirely.
* **The schema is stable.** Bedrock compiles a grammar per schema and caches it
  24 hours; two builds of one set must be byte-identical or every run pays
  compilation again — minutes, not cents.
* **Nothing leaves ``required``.** An absent key and an explicit null are
  different facts (D5), and only the null is the model answering.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator

from pydantic import ValidationError
import pytest

from apis.shared.extraction.field_sets import parse_field_set
from apis.shared.extraction.schema import (
    build_model,
    field_set_fingerprint,
    model_for_field_set,
    reset_model_cache,
)

SET = """---
title: NSF Solicitation
version: 3
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| deadline | Submission deadline | date | the full proposal deadline | | no |
| budget_cap | Budget cap | currency | the maximum total award | | no |
| indirect_rate | Indirect rate | number | the allowable indirect rate | | yes |
| cost_share | Cost sharing | enum | whether cost sharing is required | required, not required | no |
| allows_foreign | Foreign PIs | boolean | whether a foreign PI may lead | | no |
| topics | Topic areas | list | the research topic areas | | yes |
| sponsor | Sponsor | string | the funding agency | | no |
"""


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    reset_model_cache()
    yield
    reset_model_cache()


@pytest.fixture
def field_set():
    return parse_field_set(SET, name="nsf-solicitation")


@pytest.fixture
def schema(field_set) -> Dict[str, Any]:
    return build_model(field_set).model_json_schema()


def _walk(node: Any) -> Iterator[Dict[str, Any]]:
    """Every object node in a schema, so a rule can be asserted everywhere."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _value_schema(schema: Dict[str, Any], field_name: str) -> Dict[str, Any]:
    """The ``value`` sub-schema for one field, through its ``$ref``."""
    ref = schema["properties"][field_name]["$ref"].rsplit("/", 1)[-1]
    return schema["$defs"][ref]["properties"]["value"]


def sent_schema(field_set) -> Dict[str, Any]:
    """The schema Bedrock actually receives.

    Pydantic's ``model_json_schema()`` is not it. Strands converts the model to
    a tool spec (inlining ``$defs`` and collapsing each ``anyOf`` into a type
    array) and ``strict_tools`` then runs ``ensure_strict_json_schema`` over
    the result. Asserting on the Pydantic output alone missed two schemas that
    were wrong by the time they left the process, so the subset rules below are
    checked here as well.
    """
    from strands.models._strict_schema import ensure_strict_json_schema
    from strands.tools.structured_output import convert_pydantic_to_tool_spec

    spec = convert_pydantic_to_tool_spec(model_for_field_set(field_set))
    return ensure_strict_json_schema(spec["inputSchema"]["json"])


def sent_value(field_set, field_name: str) -> Dict[str, Any]:
    return sent_schema(field_set)["properties"][field_name]["properties"]["value"]


class TestTheSupportedSubset:
    def test_every_object_forbids_additional_properties(self, schema) -> None:
        """``additionalProperties`` may only ever be ``false`` (§6.3)."""
        objects = [
            node
            for node in _walk(schema)
            if node.get("type") == "object" or "properties" in node
        ]
        assert objects
        for node in objects:
            assert node.get("additionalProperties") is False

    @pytest.mark.parametrize(
        "keyword",
        ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
         "multipleOf", "minLength", "maxLength", "pattern", "oneOf"],
    )
    def test_no_unsupported_keyword_appears_anywhere(self, schema, keyword) -> None:
        """Each of these fails the request outright, or is unsupported under
        ``strict``. A numeric range belongs in Python (§6.3, G9).

        Checked as a schema *key*, not as a substring: a ``find`` phrase saying
        "the maximum total award" is prose in a description and constrains
        nothing.
        """
        offenders = [node for node in _walk(schema) if keyword in node]
        assert not offenders, f"{keyword} appears as a schema keyword: {offenders}"

    def test_no_recursion_and_no_external_refs(self, schema) -> None:
        for node in _walk(schema):
            ref = node.get("$ref")
            if ref is not None:
                assert ref.startswith("#/$defs/"), ref

    def test_enum_is_an_enum_including_null(self, schema) -> None:
        """Native enforcement, and the reason declaring types is worth it.

        ``null`` is a member rather than a nullable wrapper — see
        ``TestTheSchemaBedrockActuallyReceives`` for why that distinction is
        the difference between reporting an absent value and inventing one.
        """
        value = _value_schema(schema, "cost_share")
        assert value["enum"] == ["required", "not required", None]

    def test_currency_is_a_string_not_a_number(self, schema) -> None:
        """D6: the document says "$500,000"; a float discards the symbol and
        invents precision."""
        types = {branch.get("type") for branch in _value_schema(schema, "budget_cap")["anyOf"]}
        assert types == {"string", "null"}

    def test_date_carries_its_format_inside_the_string_branch(self, schema) -> None:
        """A ``format`` sibling of ``anyOf`` is read by nothing. Inside the
        branch, a validator sees it."""
        branches = _value_schema(schema, "deadline")["anyOf"]
        string_branch = next(b for b in branches if b.get("type") == "string")
        assert string_branch["format"] == "date"

    def test_number_is_a_number(self, schema) -> None:
        types = {b.get("type") for b in _value_schema(schema, "indirect_rate")["anyOf"]}
        assert types == {"number", "null"}

    def test_boolean_is_a_boolean(self, schema) -> None:
        types = {b.get("type") for b in _value_schema(schema, "allows_foreign")["anyOf"]}
        assert types == {"boolean", "null"}

    def test_list_is_a_nullable_array_of_non_null_strings(self, schema) -> None:
        value = _value_schema(schema, "topics")
        assert value["items"] == {"type": "string"}
        assert set(value["type"]) == {"array", "null"}


class TestRequiredAndNullability:
    def test_every_field_is_required_at_the_top_level(self, schema, field_set) -> None:
        """D5, and the §6.5 payoff: zero optional parameters declared, so a
        40-field set stays inside strict mode's undocumented cap."""
        assert schema["required"] == [f.name for f in field_set.fields]

    def test_value_and_quote_are_both_required_on_every_field(
        self, schema, field_set
    ) -> None:
        for field in field_set.fields:
            ref = schema["properties"][field.name]["$ref"].rsplit("/", 1)[-1]
            assert set(schema["$defs"][ref]["required"]) == {"value", "quote"}

    def test_a_declared_required_field_is_still_nullable(self, schema) -> None:
        """Where this departs from D5's letter, and why.

        D5 reads as "only ``optional: true`` becomes nullable". That cannot
        hold with §14, which requires a field the document does not answer to
        come back ``null`` rather than invented — a non-nullable value leaves
        the model no way to say "not here" except to write something. So every
        value is nullable and ``optional`` records the author's expectation
        instead. D5's load-bearing half is intact: nothing leaves ``required``.
        """
        types = {b.get("type") for b in _value_schema(schema, "sponsor")["anyOf"]}
        assert "null" in types

    def test_the_quote_is_a_sibling_of_the_value(self, schema, field_set) -> None:
        """§7: a value cannot arrive without the slot for its evidence."""
        for field in field_set.fields:
            ref = schema["properties"][field.name]["$ref"].rsplit("/", 1)[-1]
            assert set(schema["$defs"][ref]["properties"]) == {"value", "quote"}


class TestStability:
    def test_two_builds_of_one_set_emit_an_identical_schema(self, field_set) -> None:
        """The property §6.4 needs. Schema drift costs minutes of grammar
        compilation, not cents."""
        first = build_model(field_set).model_json_schema()
        second = build_model(parse_field_set(SET, name="nsf-solicitation")).model_json_schema()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_property_and_required_order_follow_the_authored_rows(
        self, schema, field_set
    ) -> None:
        authored = [f.name for f in field_set.fields]
        assert list(schema["properties"].keys()) == authored
        assert schema["required"] == authored

    def test_reordering_the_rows_changes_the_schema(self) -> None:
        """Stated as a property rather than assumed: this is *why* order is
        declared and never derived (D4)."""
        lines = SET.strip().splitlines()
        first_row = next(
            i for i, line in enumerate(lines) if line.startswith("| deadline ")
        )
        swapped = "\n".join(
            lines[:first_row]
            + [lines[first_row + 1], lines[first_row]]
            + lines[first_row + 2 :]
        )
        original = build_model(parse_field_set(SET, name="s")).model_json_schema()
        other = build_model(parse_field_set(swapped, name="s")).model_json_schema()
        assert list(original["properties"])[:2] == ["deadline", "budget_cap"]
        assert list(other["properties"])[:2] == ["budget_cap", "deadline"]
        assert json.dumps(original, sort_keys=True) != json.dumps(
            other, sort_keys=True
        )

    def test_the_cache_returns_one_class_per_field_set_version(self, field_set) -> None:
        """One class is what makes two runs the same schema, and it is also
        what Strands' own ``_TOOL_SPEC_CACHE`` keys on (D4)."""
        again = parse_field_set(SET, name="nsf-solicitation")
        assert model_for_field_set(field_set) is model_for_field_set(again)

    def test_an_edit_without_a_version_bump_still_gets_a_fresh_model(self) -> None:
        """Where the cache key improves on the spec.

        Keying on ``(set, version)`` alone would serve the previous schema for
        the life of the process to an author who edited a ``find`` phrase
        without bumping ``version`` — and the symptom would look like a model
        regression. Including the content digest makes a cache hit mean exactly
        "the same schema", which is the property §6.4 needs.
        """
        edited = SET.replace("the funding agency", "the sponsoring agency")
        first = model_for_field_set(parse_field_set(SET, name="nsf-solicitation"))
        second = model_for_field_set(parse_field_set(edited, name="nsf-solicitation"))
        assert first is not second
        assert "sponsoring agency" in json.dumps(second.model_json_schema())

    def test_the_fingerprint_ignores_nothing_that_shapes_the_schema(self) -> None:
        base = parse_field_set(SET, name="nsf-solicitation")
        for edit in (
            SET.replace("| sponsor |", "| funder |"),
            SET.replace("the funding agency", "the agency"),
            SET.replace("| Sponsor |", "| Funder |"),
            SET.replace("required, not required", "required, voluntary"),
            SET.replace("| the allowable indirect rate | | yes |", "| the allowable indirect rate | | no |"),
        ):
            assert field_set_fingerprint(
                parse_field_set(edit, name="nsf-solicitation")
            ) != field_set_fingerprint(base)


class TestValidationOnReturn:
    """``structured_output`` validates with the generated model, so a type the
    grammar somehow let through still fails here with a field path (§6.1)."""

    def test_a_well_formed_row_validates(self, field_set) -> None:
        model = model_for_field_set(field_set)
        instance = model.model_validate(
            {
                "deadline": {"value": "2026-03-03", "quote": "Proposals are due March 3, 2026."},
                "budget_cap": {"value": "$500,000", "quote": "awards up to $500,000"},
                "indirect_rate": {"value": 54.5, "quote": "an indirect rate of 54.5%"},
                "cost_share": {"value": "not required", "quote": "No cost sharing is required."},
                "allows_foreign": {"value": False, "quote": "PIs must be at US institutions."},
                "topics": {"value": ["climate", "ocean"], "quote": "climate and ocean science"},
                "sponsor": {"value": "NSF", "quote": "National Science Foundation (NSF)"},
            }
        )
        assert instance.deadline.value == "2026-03-03"

    def test_an_enum_value_outside_the_set_is_refused(self, field_set) -> None:
        model = model_for_field_set(field_set)
        with pytest.raises(ValidationError):
            model.model_validate(
                {
                    "deadline": {"value": None, "quote": None},
                    "budget_cap": {"value": None, "quote": None},
                    "indirect_rate": {"value": None, "quote": None},
                    "cost_share": {"value": "maybe", "quote": None},
                    "allows_foreign": {"value": None, "quote": None},
                    "topics": {"value": None, "quote": None},
                    "sponsor": {"value": None, "quote": None},
                }
            )

    def test_a_missing_field_is_refused_rather_than_defaulted(self, field_set) -> None:
        """The whole point of keeping every property in ``required``."""
        model = model_for_field_set(field_set)
        with pytest.raises(ValidationError):
            model.model_validate({"deadline": {"value": None, "quote": None}})

    def test_an_all_null_row_is_valid(self, field_set) -> None:
        """A document that answers nothing must be expressible. Otherwise the
        model's only way to comply is to invent (§14)."""
        model = model_for_field_set(field_set)
        instance = model.model_validate(
            {f.name: {"value": None, "quote": None} for f in field_set.fields}
        )
        assert instance.sponsor.value is None


class TestTheSchemaBedrockActuallyReceives:
    """Strands rewrites the schema on the way out, and the rewrite is where
    two real bugs lived. Pydantic's own output being correct is not enough.
    """

    def test_an_enum_field_can_still_report_an_absent_value(self, field_set) -> None:
        """``Optional[Literal[...]]`` collapses to ``{"enum": [...], "type":
        ["string", "null"]}`` — ``type`` permits null and ``enum`` refuses it,
        and ``enum`` wins. The model would then have no legal way to say "the
        document does not state this", which is precisely the invention §14
        exists to catch. ``None`` has to be a member of the enum.
        """
        value = sent_value(field_set, "cost_share")
        assert None in value["enum"]
        assert set(value["enum"]) == {"required", "not required", None}

    def test_a_list_is_nullable_but_its_entries_are_not(self, field_set) -> None:
        """The converter pushes ``null`` down into every branch of a collapsed
        union, ``items`` included — which would make ``["climate", null]`` a
        schema-valid answer. The list may be absent; its entries may not be.
        """
        value = sent_value(field_set, "topics")
        assert value["items"] == {"type": "string"}
        assert set(value["type"]) == {"array", "null"}

    def test_the_date_format_survives_the_conversion(self, field_set) -> None:
        value = sent_value(field_set, "deadline")
        assert value["format"] == "date"
        assert set(value["type"]) == {"string", "null"}

    def test_every_scalar_can_be_null_in_the_sent_schema(self, field_set) -> None:
        """The §14 property, asserted on the wire format rather than inferred
        from the Python types."""
        for field in field_set.fields:
            value = sent_value(field_set, field.name)
            declared = value.get("type")
            if "enum" in value:
                assert None in value["enum"], field.name
            else:
                assert "null" in declared, f"{field.name} cannot report absence"

    def test_the_sent_schema_forbids_additional_properties_everywhere(
        self, field_set
    ) -> None:
        objects = [
            node
            for node in _walk(sent_schema(field_set))
            if "properties" in node
        ]
        assert objects
        for node in objects:
            assert node.get("additionalProperties") is False

    @pytest.mark.parametrize(
        "keyword",
        ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
         "multipleOf", "minLength", "maxLength", "pattern", "oneOf", "$ref"],
    )
    def test_the_sent_schema_uses_no_unsupported_keyword(
        self, field_set, keyword
    ) -> None:
        """``$ref`` is in this list because the converter inlines every
        definition, so one appearing again would be a regression in that
        inlining rather than something we emitted."""
        offenders = [node for node in _walk(sent_schema(field_set)) if keyword in node]
        assert not offenders, f"{keyword} reached the request: {offenders}"

    def test_two_conversions_of_one_set_are_identical(self, field_set) -> None:
        """Stability has to hold on the sent form, since that is what Bedrock
        hashes to find a compiled grammar (§6.4)."""
        again = parse_field_set(SET, name="nsf-solicitation")
        assert json.dumps(sent_schema(field_set), sort_keys=True) == json.dumps(
            sent_schema(again), sort_keys=True
        )


class TestAwkwardFieldNames:
    def test_a_field_named_like_a_basemodel_attribute_keeps_its_json_name(self) -> None:
        """A grants office may reasonably name a column ``json`` or ``copy``.

        The attribute is renamed to stay off ``BaseModel``'s; the schema
        property keeps the authored name, because that name is the claim id
        handed to G2 and the workbook's column header.
        """
        field_set = parse_field_set(
            "| name | find |\n|---|---|\n"
            "| json | the json block |\n"
            "| copy | the copy deadline |\n"
            "| schema | the schema version |\n",
            name="awkward",
        )
        schema = build_model(field_set).model_json_schema()
        assert list(schema["properties"].keys()) == ["json", "copy", "schema"]
        assert schema["required"] == ["json", "copy", "schema"]
