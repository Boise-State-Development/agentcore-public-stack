"""The field-set parser.

``docs/specs/structured-extraction.md`` §3–§4, §14. Parsing a markdown table is
pure, so every authoring rule gets a case here rather than a dev run.

The order guarantee and the "every failure names what is wrong" rule are the
two worth reading: order is a *cost* property (a reordered field list is a
different schema and recompiles a grammar, §6.4), and a parse failure is an
authoring mistake whose message the author has to be able to act on.
"""

from __future__ import annotations

import pytest

from apis.shared.extraction.field_sets import (
    MAX_FIELDS,
    FieldSetError,
    field_set_name_for,
    parse_field_set,
)

FULL = """---
title: NSF Solicitation
version: 3
---

Everything a proposal office needs off the front of a solicitation.

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| deadline | Submission deadline | date | The full proposal submission deadline | | no |
| budget_cap | Budget cap | currency | The maximum total award amount | | no |
| cost_share | Cost sharing | enum | Whether cost sharing is required | required, not required, voluntary | no |
| page_limit | Page limit | number | Project description page limit in pages | | yes |
| allows_foreign | Foreign PIs | boolean | Whether a foreign PI may lead | | no |
| topics | Topic areas | list | The research topic areas named | | yes |

## Rules
- budget_cap must be positive
"""


def parse(text: str, name: str = "nsf-solicitation"):
    return parse_field_set(text, name=name)


class TestWholeFile:
    def test_metadata_comes_from_frontmatter(self) -> None:
        field_set = parse(FULL)
        assert (field_set.name, field_set.title, field_set.version) == (
            "nsf-solicitation",
            "NSF Solicitation",
            "3",
        )

    def test_fields_keep_the_authored_order(self) -> None:
        """Not a sort, not a dict iteration (D4).

        A reordered list is a different schema, and Bedrock charges grammar
        compilation for a schema it has not seen (§6.4).
        """
        assert [f.name for f in parse(FULL).fields] == [
            "deadline",
            "budget_cap",
            "cost_share",
            "page_limit",
            "allows_foreign",
            "topics",
        ]

    def test_types_and_enum_values_are_read(self) -> None:
        by_name = {f.name: f for f in parse(FULL).fields}
        assert by_name["budget_cap"].type == "currency"
        assert by_name["cost_share"].enum == ("required", "not required", "voluntary")
        assert by_name["page_limit"].optional is True
        assert by_name["deadline"].optional is False

    def test_find_is_carried_verbatim(self) -> None:
        """The load-bearing field: it is the field's prompt (§3)."""
        by_name = {f.name: f for f in parse(FULL).fields}
        assert by_name["page_limit"].find == "Project description page limit in pages"

    def test_rules_are_carried_unparsed_not_rejected(self) -> None:
        """G9 is out of scope, so a set authored with rules must still parse."""
        assert "budget_cap must be positive" in (parse(FULL).rules or "")

    def test_judgement_types_are_the_ones_g2_exempts(self) -> None:
        """``enum_field`` is derived here, not asked of the model (D7)."""
        by_name = {f.name: f for f in parse(FULL).fields}
        assert by_name["cost_share"].is_judgement is True
        assert by_name["allows_foreign"].is_judgement is True
        assert by_name["budget_cap"].is_judgement is False
        assert by_name["deadline"].is_judgement is False


class TestDefaultsAndTolerance:
    def test_frontmatter_is_optional(self) -> None:
        field_set = parse(
            "| name | find |\n|---|---|\n| deadline | the deadline |\n"
        )
        assert (field_set.title, field_set.version) == ("nsf-solicitation", "1")

    def test_title_defaults_to_a_readable_form_of_the_name(self) -> None:
        field_set = parse("| name | find |\n|---|---|\n| budget_cap | the cap |\n")
        assert field_set.fields[0].title == "Budget Cap"

    def test_type_defaults_to_string(self) -> None:
        field_set = parse("| name | find |\n|---|---|\n| sponsor | the sponsor |\n")
        assert field_set.fields[0].type == "string"

    def test_column_headers_are_matched_case_insensitively_with_aliases(self) -> None:
        field_set = parse(
            "| Field | Label | Type | Search Phrase |\n|---|---|---|---|\n"
            "| deadline | Deadline | date | the deadline |\n"
        )
        assert field_set.fields[0].title == "Deadline"
        assert field_set.fields[0].type == "date"

    def test_unknown_columns_are_ignored_not_rejected(self) -> None:
        """A set may carry a note column for its own authors."""
        field_set = parse(
            "| name | find | notes |\n|---|---|---|\n"
            "| deadline | the deadline | ask Jo |\n"
        )
        assert len(field_set.fields) == 1

    def test_prose_containing_a_pipe_before_the_table_is_skipped(self) -> None:
        field_set = parse(
            "Use this for NSF | DOE solicitations.\n\n"
            "| name | find |\n|---|---|\n| deadline | the deadline |\n"
        )
        assert [f.name for f in field_set.fields] == ["deadline"]

    def test_enum_values_are_split_on_commas_and_backticks_are_stripped(self) -> None:
        field_set = parse(
            "| name | type | find | enum |\n|---|---|---|---|\n"
            "| tier | enum | the tier | `high`, `low` |\n"
        )
        assert field_set.fields[0].enum == ("high", "low")

    def test_a_rules_section_with_its_own_table_is_not_mistaken_for_the_fields(
        self,
    ) -> None:
        field_set = parse(
            "| name | find |\n|---|---|\n| deadline | the deadline |\n"
            "\n## Rules\n\n| name | find |\n|---|---|\n| bogus | nope |\n"
        )
        assert [f.name for f in field_set.fields] == ["deadline"]


class TestAuthoringErrors:
    """Every one of these is a message an author reads, not a 500 (§4)."""

    def test_no_table_at_all(self) -> None:
        with pytest.raises(FieldSetError, match="no field table"):
            parse("---\ntitle: Nothing\n---\n\nJust prose.\n")

    def test_table_with_no_rows(self) -> None:
        with pytest.raises(FieldSetError, match="no rows"):
            parse("| name | find |\n|---|---|\n")

    def test_empty_file(self) -> None:
        with pytest.raises(FieldSetError, match="empty"):
            parse("   \n\n")

    def test_unclosed_frontmatter(self) -> None:
        with pytest.raises(FieldSetError, match="never closed"):
            parse("---\ntitle: X\n\n| name | find |\n|---|---|\n| a | b |\n")

    def test_unknown_type_is_named_not_coerced(self) -> None:
        with pytest.raises(FieldSetError, match="unknown type 'monetary'"):
            parse(
                "| name | type | find |\n|---|---|---|\n"
                "| cap | monetary | the cap |\n"
            )

    def test_missing_find_is_refused(self) -> None:
        with pytest.raises(FieldSetError, match="'find' is required"):
            parse("| name | find |\n|---|---|\n| deadline | |\n")

    def test_enum_without_values(self) -> None:
        with pytest.raises(FieldSetError, match="needs allowed values"):
            parse(
                "| name | type | find | enum |\n|---|---|---|---|\n"
                "| tier | enum | the tier | |\n"
            )

    def test_enum_values_on_a_non_enum_type_are_refused(self) -> None:
        """Silently ignoring them would let an author believe a constraint is
        enforced when nothing reads it."""
        with pytest.raises(FieldSetError, match="only read for type 'enum'"):
            parse(
                "| name | type | find | enum |\n|---|---|---|---|\n"
                "| tier | string | the tier | a, b |\n"
            )

    def test_duplicate_enum_values(self) -> None:
        with pytest.raises(FieldSetError, match="duplicate allowed values"):
            parse(
                "| name | type | find | enum |\n|---|---|---|---|\n"
                "| tier | enum | the tier | a, b, a |\n"
            )

    def test_duplicate_field_name_names_both_rows(self) -> None:
        with pytest.raises(FieldSetError, match="declared twice"):
            parse(
                "| name | find |\n|---|---|\n"
                "| deadline | the deadline |\n| deadline | again |\n"
            )

    def test_non_snake_case_field_name(self) -> None:
        with pytest.raises(FieldSetError, match="must be snake_case"):
            parse("| name | find |\n|---|---|\n| Budget-Cap | the cap |\n")

    def test_unparseable_optional_cell(self) -> None:
        with pytest.raises(FieldSetError, match="must be yes or no"):
            parse(
                "| name | find | optional |\n|---|---|---|\n"
                "| deadline | the deadline | sometimes |\n"
            )

    def test_too_many_fields_is_refused_rather_than_truncated(self) -> None:
        """A silently dropped column is a column a reviewer thinks was checked."""
        rows = "".join(
            f"| field_{i} | value {i} |\n" for i in range(MAX_FIELDS + 1)
        )
        with pytest.raises(FieldSetError, match=f"exceeds the limit of {MAX_FIELDS}"):
            parse(f"| name | find |\n|---|---|\n{rows}")

    def test_a_bad_set_name_is_refused(self) -> None:
        with pytest.raises(FieldSetError, match="lowercase words"):
            parse("| name | find |\n|---|---|\n| a | b |\n", name="NSF_Solicitation")


class TestFilenameConvention:
    """``field-set.<name>.md``, because a skill resource filename may not carry
    a path separator — see the note on ``FIELD_SET_PREFIX``."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("field-set.nsf-solicitation.md", "nsf-solicitation"),
            ("references/field-set.nsf-solicitation.md", "nsf-solicitation"),
            ("references/field-set.awards.md", "awards"),
            ("references/forms.md", None),
            ("field-set.md", None),
            ("field-set..md", None),
            ("field-set.NSF.md", None),
            ("field-set.nsf_solicitation.md", None),
            ("scripts/field-set.x.md", "x"),
            ("", None),
        ],
    )
    def test_name_is_read_from_the_filename(self, path: str, expected) -> None:
        assert field_set_name_for(path) == expected
