"""``serialize_field_set`` and ``validate_field_set_upload``.

``docs/specs/field-set-authoring.md`` §4 and §6. Two properties, pinned for two
different reasons (D5):

1. **Semantic round trip** — ``parse(serialize(parse(x))) == parse(x)``, for
   files written by hand in formats the serializer never produces. This is the
   cost property: opening a set and saving it unchanged must change nothing the
   generated schema can see, or the next extraction pays grammar compilation.
2. **Exact bytes** — the serializer's own output is pinned verbatim, so its
   format cannot drift between PRs and turn every save into a whole-file diff.
   A review-hygiene property, not a cost one.
"""

from __future__ import annotations

import pytest

from apis.shared.extraction.field_sets import (
    MAX_FIELDS,
    Field,
    FieldSet,
    FieldSetError,
    FieldSetLayout,
    parse_field_set,
    read_field_set_file,
    serialize_field_set,
    validate_field_set_upload,
)
from apis.shared.extraction.schema import field_set_fingerprint

CANONICAL = """---
title: NSF Solicitation
version: 3
---

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

# Hand-authored files in shapes the serializer never writes: column aliases,
# reordered and padded header columns, alignment colons in the delimiter row, a
# note column the parser ignores, prose around the table, backticked names,
# other spellings of yes/no, and a Rules section carrying its own table.
FOREIGN_FORMATS = {
    "aliases_and_reordered_columns": """---
title: "Grant Intake"
version: 2
---

Fields the office reads off every intake.

| Search phrase | Field | Type | Allowed values | Label | Optional |
|:---|:---:|---:|---|---|---|
|   The date proposals are due   | `due_date` | DATE |  |  Due  | N |
| Whether the PI must be tenure-track | pi_rule | enum | tenure-track, any | PI rule | - |

Trailing prose an author left for colleagues.
""",
    "note_column_and_no_frontmatter": """| name | find | note | optional |
|---|---|---|---|
| sponsor | The funding agency's name | ask Dana before changing | Yes |
| amount | The award amount in dollars | | no |
""",
    "rules_with_their_own_table": """---
title: Budget
---
| name | type | find |
| --- | --- | --- |
| total | currency | Total requested budget |

## Rules

| rule | severity |
|---|---|
| total > 0 | error |
""",
    "other_frontmatter_lines": """---
title: Award
owner: Research Office
# reviewed 2026-10
version: 4
---
| name | find |
|---|---|
| amount | The award amount |
""",
    "pipe_separated_enum": """---
title: Review
version: 1
---
| name | type | find | values |
|---|---|---|---|
| level | enum | Review level the funder requires | `full` |
""",
}


def _round_trip(text: str, name: str = "set") -> FieldSet:
    parsed = parse_field_set(text, name=name)
    return parse_field_set(serialize_field_set(parsed), name=name)


class TestSemanticRoundTrip:
    @pytest.mark.parametrize("key", sorted(FOREIGN_FORMATS))
    def test_hand_authored_files_read_back_field_for_field(self, key):
        parsed = parse_field_set(FOREIGN_FORMATS[key], name="set")

        assert _round_trip(FOREIGN_FORMATS[key]) == parsed

    @pytest.mark.parametrize("key", sorted(FOREIGN_FORMATS))
    def test_a_save_keeps_the_schema_fingerprint(self, key):
        """The cost property itself: the in-process model cache keys on this."""
        parsed = parse_field_set(FOREIGN_FORMATS[key], name="set")

        assert field_set_fingerprint(_round_trip(FOREIGN_FORMATS[key])) == (
            field_set_fingerprint(parsed)
        )

    def test_field_order_is_the_authored_order(self):
        parsed = parse_field_set(CANONICAL, name="nsf")

        assert [f.name for f in _round_trip(CANONICAL, "nsf").fields] == [
            f.name for f in parsed.fields
        ]

    def test_rules_survive_verbatim(self):
        parsed = parse_field_set(FOREIGN_FORMATS["rules_with_their_own_table"], name="b")

        assert _round_trip(FOREIGN_FORMATS["rules_with_their_own_table"], "b").rules == (
            parsed.rules
        )
        assert "| total > 0 | error |" in parsed.rules

    def test_serializing_is_idempotent(self):
        parsed = parse_field_set(FOREIGN_FORMATS["aliases_and_reordered_columns"], name="set")
        once = serialize_field_set(parsed)

        assert serialize_field_set(parse_field_set(once, name="set")) == once


class TestLayoutRoundTrip:
    """``read → serialize(layout) → read`` gives back the file's set **and** what
    it ignores, so a structured save never deletes an author's note column,
    their prose or their frontmatter (field-set authoring §5.4)."""

    @pytest.mark.parametrize("key", sorted(FOREIGN_FORMATS))
    def test_set_and_layout_both_survive(self, key):
        field_set, layout = read_field_set_file(FOREIGN_FORMATS[key], name="set")

        text = serialize_field_set(field_set, layout=layout)

        assert read_field_set_file(text, name="set") == (field_set, layout)

    def test_a_note_column_is_reported_and_kept(self):
        field_set, layout = read_field_set_file(
            FOREIGN_FORMATS["note_column_and_no_frontmatter"], name="set"
        )

        assert layout.unmanaged_columns == ("note",)
        assert layout.unmanaged_cells == (("ask Dana before changing",), ("",))
        assert "| ask Dana before changing |" in serialize_field_set(field_set, layout=layout)

    def test_prose_around_the_table_is_reported(self):
        _, layout = read_field_set_file(
            FOREIGN_FORMATS["aliases_and_reordered_columns"], name="set"
        )

        assert layout.preamble == "Fields the office reads off every intake."
        assert layout.postscript == "Trailing prose an author left for colleagues."
        assert layout.unmanaged_columns == ()

    def test_other_frontmatter_lines_are_reported_as_written(self):
        field_set, layout = read_field_set_file(
            FOREIGN_FORMATS["other_frontmatter_lines"], name="set"
        )

        assert layout.frontmatter == ("owner: Research Office", "# reviewed 2026-10")
        assert (field_set.title, field_set.version) == ("Award", "4")

    def test_an_overridden_alias_column_is_unmanaged(self):
        """``row[column] = cell`` keeps the last of two aliases, so the earlier
        column is one the parser does not read."""
        _, layout = read_field_set_file(
            "| field | name | find |\n|---|---|---|\n| old | amount | The award |\n",
            name="set",
        )

        assert layout.unmanaged_columns == ("field",)
        assert layout.unmanaged_cells == (("old",),)

    def test_the_layout_never_changes_the_set(self):
        """The extraction path reads :class:`FieldSet` alone; the layout must
        not be able to alter what it sees, or its schema."""
        for key, text in FOREIGN_FORMATS.items():
            field_set, layout = read_field_set_file(text, name="set")
            saved = serialize_field_set(field_set, layout=layout)

            assert field_set_fingerprint(parse_field_set(saved, name="set")) == (
                field_set_fingerprint(field_set)
            ), key


class TestExactBytes:
    def test_canonical_output_is_pinned(self):
        assert serialize_field_set(parse_field_set(CANONICAL, name="nsf")) == CANONICAL

    def test_a_foreign_file_is_rewritten_in_the_canonical_shape(self):
        text = serialize_field_set(
            parse_field_set(FOREIGN_FORMATS["note_column_and_no_frontmatter"], name="intake")
        )

        assert text == (
            "---\n"
            "title: intake\n"
            "version: 1\n"
            "---\n"
            "\n"
            "| name | title | type | find | enum | optional |\n"
            "|---|---|---|---|---|---|\n"
            "| sponsor | Sponsor | string | The funding agency's name | | yes |\n"
            "| amount | Amount | string | The award amount in dollars | | no |\n"
        )

    def test_preamble_sits_between_frontmatter_and_table(self):
        parsed = parse_field_set(CANONICAL, name="nsf")
        text = serialize_field_set(parsed, layout=FieldSetLayout(preamble="  Read me first.\n"))

        assert text.startswith("---\ntitle: NSF Solicitation\nversion: 3\n---\n\nRead me first.\n\n| name |")
        assert parse_field_set(text, name="nsf") == parsed


def _one_field_set(**field_overrides) -> FieldSet:
    values = {
        "name": "amount",
        "title": "Amount",
        "type": "currency",
        "find": "The award amount",
        "enum": (),
        "optional": False,
    }
    values.update(field_overrides)
    return FieldSet(name="s", title="S", version="1", fields=(Field(**values),))


class TestRefusals:
    """A value the file cannot carry is refused by name, never saved altered."""

    def test_a_pipe_in_find(self):
        with pytest.raises(FieldSetError, match=r"'find' cannot contain '\|'"):
            serialize_field_set(_one_field_set(find="Amount | total"))

    def test_a_line_break_in_find(self):
        with pytest.raises(FieldSetError, match="line break"):
            serialize_field_set(_one_field_set(find="The award\namount"))

    def test_a_comma_inside_one_allowed_value(self):
        fs = _one_field_set(type="enum", enum=("yes, with conditions", "no"))

        with pytest.raises(FieldSetError, match="cannot contain a comma"):
            serialize_field_set(fs)

    def test_a_title_the_frontmatter_would_strip(self):
        fs = FieldSet(name="s", title='"Quoted"', version="1", fields=_one_field_set().fields)

        with pytest.raises(FieldSetError, match="its title would read back as 'Quoted'"):
            serialize_field_set(fs)

    def test_untrimmed_find_is_named_rather_than_silently_trimmed(self):
        with pytest.raises(FieldSetError, match="field 'amount': find would read back as"):
            serialize_field_set(_one_field_set(find="  The award amount  "))

    def test_a_preamble_that_reads_as_a_field_table(self):
        parsed = parse_field_set(CANONICAL, name="nsf")

        with pytest.raises(FieldSetError, match="cannot be written as a file that reads back"):
            serialize_field_set(
                parsed, layout=FieldSetLayout(preamble="| name | find |\n|---|---|\n| x | y |")
            )

    def test_a_pipe_in_the_text_after_the_table(self):
        parsed = parse_field_set(CANONICAL, name="nsf")

        with pytest.raises(FieldSetError, match="after the table cannot contain"):
            serialize_field_set(parsed, layout=FieldSetLayout(postscript="a | b"))

    def test_an_extra_column_headed_like_a_real_one(self):
        fs = _one_field_set()
        layout = FieldSetLayout(unmanaged_columns=("Label",), unmanaged_cells=(("x",),))

        with pytest.raises(FieldSetError, match="reads as the 'title' column"):
            serialize_field_set(fs, layout=layout)

    def test_extra_cells_that_do_not_line_up(self):
        fs = _one_field_set()

        with pytest.raises(FieldSetError, match="0 rows for 1 fields"):
            serialize_field_set(fs, layout=FieldSetLayout(unmanaged_columns=("note",)))
        with pytest.raises(FieldSetError, match="2 extra cells for 1 extra columns"):
            serialize_field_set(
                fs,
                layout=FieldSetLayout(unmanaged_columns=("note",), unmanaged_cells=(("a", "b"),)),
            )

    def test_a_frontmatter_line_that_sets_the_title(self):
        with pytest.raises(FieldSetError, match="its title would read back as 'Other'"):
            serialize_field_set(_one_field_set(), layout=FieldSetLayout(frontmatter=("title: Other",)))

    def test_no_fields(self):
        with pytest.raises(FieldSetError, match="no rows"):
            serialize_field_set(FieldSet(name="s", title="S", version="1", fields=()))

    def test_more_than_the_ceiling(self):
        fields = tuple(
            Field(name=f"f{i}", title=f"F{i}", type="string", find=f"field {i}")
            for i in range(MAX_FIELDS + 1)
        )

        with pytest.raises(FieldSetError, match=f"exceeds the limit of {MAX_FIELDS}"):
            serialize_field_set(FieldSet(name="s", title="S", version="1", fields=fields))


class TestValidateUpload:
    def test_a_file_that_is_not_a_field_set_passes_untouched(self):
        assert validate_field_set_upload("forms.md", b"| not | a | set |") is None
        assert validate_field_set_upload("field-set.notes.txt", b"anything") is None

    def test_a_valid_field_set_is_parsed(self):
        parsed = validate_field_set_upload("field-set.nsf.md", CANONICAL.encode())

        assert parsed is not None and parsed.name == "nsf"
        assert len(parsed.fields) == 6

    def test_a_broken_field_set_is_refused_with_the_parsers_message(self):
        body = b"| name | type |\n|---|---|\n| amount | currency |\n"

        with pytest.raises(FieldSetError, match="'find' is required"):
            validate_field_set_upload("field-set.budget.md", body)

    def test_a_name_the_resolver_would_skip_is_refused(self):
        with pytest.raises(FieldSetError, match="lowercase words joined by hyphens"):
            validate_field_set_upload("field-set.NSF_Review.md", CANONICAL.encode())

    def test_bytes_that_are_not_utf8(self):
        with pytest.raises(FieldSetError, match="not UTF-8"):
            validate_field_set_upload("field-set.nsf.md", b"\xff\xfe\x00")
