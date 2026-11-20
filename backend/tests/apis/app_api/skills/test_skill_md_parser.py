"""The bounded SKILL.md parser (``apis.shared.skills.skill_md``).

Pins the bounds a frontmatter block is held to (size before parsing; anchors,
aliases, node count and depth while composing), the line-end and BOM handling,
and the unsafe-character finder every write path uses for names and descriptions.
"""

import io
import zipfile

import pytest

from apis.shared.skills import bundle_import
from apis.shared.skills.bundle_import import BundleImportError, load_bundle
from apis.shared.skills.skill_md import (
    MAX_FRONTMATTER_BYTES,
    MAX_FRONTMATTER_DEPTH,
    MAX_FRONTMATTER_NODES,
    SkillMdError,
    find_unsafe_characters,
    parse_skill_md,
)


def _doc(frontmatter: str, body: str = "# Body") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def _nested_mappings(depth: int) -> str:
    """A frontmatter whose mappings nest ``depth`` deep (the top level is 1)."""
    inner = depth - 1
    return "a: " + "{b: " * inner + "1" + "}" * inner


def _sequence_of(items: int) -> str:
    """Top mapping + key + sequence + ``items`` scalars = ``items + 3`` nodes."""
    return "k: [" + ", ".join("1" for _ in range(items)) + "]"


def _alias_ladder(width: int, levels: int) -> str:
    lines = [f"l0: &l0 [{', '.join('x' for _ in range(width))}]"]
    for level in range(1, levels):
        refs = ", ".join(f"*l{level - 1}" for _ in range(width))
        lines.append(f"l{level}: &l{level} [{refs}]")
    return "\n".join(lines)


class TestDelimiters:
    def test_frontmatter_and_body(self):
        parsed = parse_skill_md(_doc("name: demo\nDescription: Does a thing."))
        assert parsed.frontmatter == {"name": "demo", "description": "Does a thing."}
        assert parsed.body == "# Body"
        assert parsed.diagnostics == []

    def test_no_frontmatter_is_all_body(self):
        parsed = parse_skill_md("# Just a body\n\ntext\n")
        assert parsed.frontmatter == {}
        assert parsed.body == "# Just a body\n\ntext"

    def test_unclosed_frontmatter_is_all_body_with_a_note(self):
        parsed = parse_skill_md("---\nname: demo\n# never closed\n")
        assert parsed.frontmatter == {}
        assert parsed.body.startswith("---\nname: demo")
        assert [d.code for d in parsed.diagnostics] == ["frontmatter_unclosed"]

    def test_crlf_line_ends(self):
        parsed = parse_skill_md("---\r\nname: demo\r\ndescription: d\r\n---\r\nBody\r\n")
        assert parsed.frontmatter == {"name": "demo", "description": "d"}
        assert parsed.body == "Body"

    def test_leading_bom_is_stripped(self):
        parsed = parse_skill_md("﻿" + _doc("name: demo"))
        assert parsed.frontmatter == {"name": "demo"}
        assert [d.code for d in parsed.diagnostics] == ["byte_order_mark"]

    def test_empty_frontmatter_block(self):
        assert parse_skill_md("---\n\n---\nBody").frontmatter == {}

    def test_non_mapping_is_refused(self):
        with pytest.raises(SkillMdError, match="key: value"):
            parse_skill_md(_doc("- a\n- b"))

    def test_invalid_yaml_is_refused(self):
        with pytest.raises(SkillMdError, match="not valid YAML"):
            parse_skill_md(_doc("name: [unclosed"))

    def test_bundle_import_wrapper_keeps_its_tuple_and_error(self):
        assert bundle_import.parse_skill_md(_doc("name: demo")) == ({"name": "demo"}, "# Body")
        with pytest.raises(BundleImportError, match="not valid YAML"):
            bundle_import.parse_skill_md(_doc("name: [unclosed"))


class TestAnchorsAndAliases:
    def test_one_alias_is_refused(self):
        with pytest.raises(SkillMdError, match="alias"):
            parse_skill_md(_doc("a: x\nb: *x"))

    def test_an_anchor_is_refused(self):
        with pytest.raises(SkillMdError, match="anchor"):
            parse_skill_md(_doc("a: &x hello\nb: *x"))

    def test_a_merge_key_alias_is_refused(self):
        with pytest.raises(SkillMdError):
            parse_skill_md(_doc("base: &b {k: 1}\nother:\n  <<: *b"))

    def test_an_alias_ladder_is_refused_without_being_expanded(self):
        """8 wide, 10 levels: 8**10 values if expanded; refused at the first anchor."""
        with pytest.raises(SkillMdError) as caught:
            parse_skill_md(_doc(_alias_ladder(width=8, levels=10)))
        assert "anchor" in str(caught.value) or "alias" in str(caught.value)
        assert caught.value.nodes_composed < 2_100

    def test_bundle_import_maps_the_refusal(self):
        with pytest.raises(BundleImportError, match="Aliases and anchors"):
            bundle_import.parse_skill_md(_doc(_alias_ladder(width=8, levels=10)))


class TestBounds:
    def test_depth_at_the_limit_is_accepted(self):
        assert MAX_FRONTMATTER_DEPTH == 8
        parsed = parse_skill_md(_doc(_nested_mappings(8)))
        value = parsed.frontmatter["a"]
        for _ in range(6):
            value = value["b"]
        assert value == {"b": 1}

    def test_depth_over_the_limit_is_refused(self):
        with pytest.raises(SkillMdError, match="nested more than 8"):
            parse_skill_md(_doc(_nested_mappings(9)))

    def test_sequence_depth_counts_too(self):
        nested = "a: " + "[" * 8 + "1" + "]" * 8
        with pytest.raises(SkillMdError, match="nested"):
            parse_skill_md(_doc(nested))

    def test_node_count_at_the_limit_is_accepted(self):
        assert MAX_FRONTMATTER_NODES == 2_000
        parsed = parse_skill_md(_doc(_sequence_of(1_997)))
        assert len(parsed.frontmatter["k"]) == 1_997

    def test_node_count_over_the_limit_is_refused(self):
        with pytest.raises(SkillMdError, match="more than 2,000 values") as caught:
            parse_skill_md(_doc(_sequence_of(1_998)))
        assert caught.value.nodes_composed == 2_001

    def test_frontmatter_at_the_size_limit_is_accepted(self):
        assert MAX_FRONTMATTER_BYTES == 16 * 1024
        block = "d: " + "x" * (MAX_FRONTMATTER_BYTES - 3)
        assert len(parse_skill_md(_doc(block)).frontmatter["d"]) == MAX_FRONTMATTER_BYTES - 3

    def test_frontmatter_over_the_size_limit_is_refused_before_parsing(self):
        block = "d: " + "x" * (MAX_FRONTMATTER_BYTES - 2)
        assert len(block.encode("utf-8")) == MAX_FRONTMATTER_BYTES + 1
        with pytest.raises(SkillMdError, match="16 KiB") as caught:
            parse_skill_md(_doc(block))
        assert caught.value.nodes_composed == 0

    def test_size_is_measured_in_utf8_bytes(self):
        block = "d: " + "é" * (MAX_FRONTMATTER_BYTES // 2)
        with pytest.raises(SkillMdError, match="16 KiB"):
            parse_skill_md(_doc(block))


class TestDynamoSafeBudget:
    def test_a_structure_over_the_budget_is_refused(self):
        with pytest.raises(BundleImportError, match="more than 2,000 values"):
            bundle_import._dynamo_safe({"k": list(range(MAX_FRONTMATTER_NODES))})

    def test_a_normal_structure_converts(self):
        assert bundle_import._dynamo_safe({"a": [1, 2.5, True, None]}) == {
            "a": [1, "2.5", True, None]
        }


class TestUnsafeCharacters:
    def test_plain_text_tab_and_newline_are_safe(self):
        assert find_unsafe_characters("Reviews grants.\tTwo lines\nok — café") == []

    @pytest.mark.parametrize(
        "code_point",
        [0x00, 0x07, 0x1B, 0x7F, 0x85, 0x9F, 0x200B, 0x200F, 0x202A, 0x202E,
         0x2060, 0x2066, 0x2069, 0xFEFF, 0xE0000, 0xE0041, 0xE007F],
    )
    def test_each_class_is_found_with_its_index(self, code_point):
        found = find_unsafe_characters("ab" + chr(code_point) + "cd")
        assert [(c.index, c.code_point) for c in found] == [(2, code_point)]

    def test_carriage_return_only_when_allowed(self):
        assert [c.code_point for c in find_unsafe_characters("a\r\nb")] == [0x0D]
        assert find_unsafe_characters("a\r\nb", allow_carriage_return=True) == []

    def test_neighbours_of_the_ranges_are_safe(self):
        assert find_unsafe_characters(" ‐ ⁰﻾\U000e0080") == []

    def test_label(self):
        assert find_unsafe_characters("‮")[0].label == "U+202E"


def _zip(skill_md: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/SKILL.md", skill_md)
    return buffer.getvalue()


class TestImportRefusesUnsafeCharacters:
    def test_description_with_a_bidi_override_is_refused(self):
        archive = _zip(_doc("name: demo\ndescription: Reviews ‮grants"))
        with pytest.raises(BundleImportError, match="U\\+202E"):
            load_bundle(archive)

    def test_name_with_a_zero_width_space_is_refused(self):
        archive = _zip(_doc("name: de​mo\ndescription: Reviews grants"))
        with pytest.raises(BundleImportError, match="U\\+200B"):
            load_bundle(archive)

    def test_alias_in_an_archive_is_refused(self):
        archive = _zip(_doc("name: demo\ndescription: &d Reviews grants\nother: *d"))
        with pytest.raises(BundleImportError, match="anchor"):
            load_bundle(archive)
