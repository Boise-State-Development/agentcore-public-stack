"""Example tests for the canonical memory-file format, validation and token counting.

The round-trip, anchor-stability and link-resolution properties live in
``tests/property/test_pbt_memory_format.py``. These tests pin the rejections
(each with its stable ``code``), the frontmatter subset, the save rules that
need a manifest, and the CountTokens fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import pytest

from apis.shared.memory import tokens as memory_tokens
from apis.shared.memory.format import (
    Frontmatter,
    Item,
    MemoryFormatError,
    extract_links,
    is_reserved_slug,
    new_anchor,
    normalize_aliases,
    normalize_description,
    parse_file,
    parse_frontmatter,
    parse_items,
    render_file,
    render_frontmatter,
    render_items,
    split_frontmatter,
    strip_anchors,
    validate_slug,
)
from apis.shared.memory.validation import (
    CurrentFile,
    freeform_link_warnings,
    validate_canonical_save,
    validate_index_links,
)

A1 = new_anchor(randomness=1)
A2 = new_anchor(randomness=2)


@dataclass(frozen=True)
class F:
    slug: str
    aliases: Tuple[str, ...] = ()
    archived: bool = False


def _code(exc_info) -> str:
    return exc_info.value.code


# ---- items -------------------------------------------------------------


class TestItems:
    def test_parses_items_continuations_and_anchors(self):
        body = f"- first <!-- e:{A1} -->\n- second line\n  continued\n\n  after a blank <!-- e:{A2.upper()} -->\n- new\n"
        assert parse_items(body) == (
            Item("first", A1),
            Item("second line\ncontinued\n\nafter a blank", A2),
            Item("new", None),
        )

    def test_renders_anchor_on_the_last_line(self):
        assert render_items([Item("a\nb", A1)]) == f"- a\n  b <!-- e:{A1} -->\n"

    def test_render_requires_anchors(self):
        with pytest.raises(MemoryFormatError) as e:
            render_items([Item("no anchor")])
        assert _code(e) == "missing_anchor"

    @pytest.mark.parametrize(
        "body, code",
        [
            ("Some prose\n- item", "prose_in_body"),
            ("* star bullet", "prose_in_body"),
            ("1. numbered", "prose_in_body"),
            ("- \n- x", "empty_item"),
            ("-", "empty_item"),
            ("- a <!-- e:123 -->", "misplaced_anchor"),
            ("- a <!-- e:uuuuuuuu -->", "malformed_anchor"),
            (f"- a <!-- e:{A1} --> tail", "misplaced_anchor"),
            (f"- <!-- e:{A1} --> middle <!-- e:{A2} -->", "misplaced_anchor"),
            ("- a <!-- e:01J9Z3K4M8N2P5Q7R9S1T3V5W7 -->", "misplaced_anchor"),
        ],
    )
    def test_rejections(self, body, code):
        with pytest.raises(MemoryFormatError) as e:
            parse_items(body)
        assert _code(e) == code

    def test_prose_error_names_the_line(self):
        with pytest.raises(MemoryFormatError, match="Line 2"):
            parse_items("- ok\nnot ok")


# ---- frontmatter -------------------------------------------------------


class TestFrontmatter:
    def test_render_shape(self):
        fm = Frontmatter(
            name="canvas-integration",
            description='Canvas "API" notes',
            aliases=("canvas", "lms sync"),
            created="2026-09-01T14:02:00Z",
            updated="2026-09-20T09:41:00Z",
            version=14,
            scope="project",
        )
        assert render_frontmatter(fm) == (
            "---\n"
            "name: canvas-integration\n"
            'description: "Canvas \\"API\\" notes"\n'
            "scope: project\n"
            'aliases: ["canvas", "lms sync"]\n'
            "created: 2026-09-01T14:02:00Z\n"
            "updated: 2026-09-20T09:41:00Z\n"
            "version: 14\n"
            "---\n"
        )

    def test_keyword_and_numeric_names_are_quoted(self):
        assert 'name: "true"' in render_frontmatter(Frontmatter(name="true"))
        assert 'name: "2026"' in render_frontmatter(Frontmatter(name="2026"))

    def test_parses_the_subset(self):
        values = parse_frontmatter(
            "# a comment\n"
            "name: notes   # trailing comment\n"
            "description: 'it''s fine'\n"
            "aliases:\n"
            "  - one\n"
            '  - "two, three"\n'
            "version: 3\n"
        )
        assert values == {"name": "notes", "description": "it's fine", "aliases": ["one", "two, three"], "version": 3}

    def test_flow_list_with_quotes_and_commas(self):
        assert parse_frontmatter("aliases: [a, \"b, c\", 'd']")["aliases"] == ["a", "b, c", "d"]
        assert parse_frontmatter("aliases: []")["aliases"] == []

    @pytest.mark.parametrize(
        "text",
        [
            "nested:\n  key: value",
            "aliases: [a, [b]]",
            "aliases: [a, b",
            "description: |\n  multi",
            'description: "unclosed',
            "name: a\nname: b",
            "just text",
            "  indented: x",
            "version: two",
            "meta: {a: 1}",
        ],
    )
    def test_rejects_what_it_does_not_support(self, text):
        with pytest.raises(MemoryFormatError) as e:
            parse_frontmatter(text)
        assert _code(e) == "frontmatter_invalid"

    def test_split(self):
        assert split_frontmatter("no frontmatter") == (None, "no frontmatter")
        assert split_frontmatter("---\na: b\n---\n- x") == ("a: b", "- x")
        assert split_frontmatter("---\na: b\n...\nbody") == ("a: b", "body")
        with pytest.raises(MemoryFormatError) as e:
            split_frontmatter("---\na: b\n- x")
        assert _code(e) == "frontmatter_unterminated"
        # Freeform reads never fail on a stray delimiter.
        assert split_frontmatter("---\nnot closed", strict=False) == (None, "---\nnot closed")

    def test_parse_file_without_frontmatter(self):
        parsed = parse_file("- a\n")
        assert parsed.frontmatter is None
        assert parsed.items == (Item("a"),)


# ---- names -------------------------------------------------------------


class TestNames:
    @pytest.mark.parametrize("slug", ["MEMORY.md", "memory.md", " Memory.MD "])
    def test_index_slug_is_reserved_in_every_mode(self, slug):
        assert is_reserved_slug(slug)
        for canonical in (True, False):
            with pytest.raises(MemoryFormatError) as e:
                validate_slug(slug, canonical=canonical)
            assert _code(e) == "reserved_slug"

    def test_canonical_slug_rules(self):
        assert validate_slug(" people/jane-doe ", canonical=True) == "people/jane-doe"
        for bad in ("Jane", "a b", "../x", "a//b", "-a", "a-", "x" * 129):
            with pytest.raises(MemoryFormatError):
                validate_slug(bad, canonical=True)
        # Freeform keeps accepting any non-empty name.
        assert validate_slug("Jane Doe's notes", canonical=False) == "Jane Doe's notes"
        with pytest.raises(MemoryFormatError) as e:
            validate_slug("  ", canonical=False)
        assert _code(e) == "slug_required"

    def test_aliases_are_cleaned(self):
        assert normalize_aliases(["  LMS   sync ", "lms sync", "", "notes", "Canvas"], slug="notes") == (
            "LMS sync",
            "Canvas",
        )
        with pytest.raises(MemoryFormatError):
            normalize_aliases(["[[x]]"], slug="n")
        with pytest.raises(MemoryFormatError):
            normalize_aliases(["memory.md"], slug="n")
        with pytest.raises(MemoryFormatError):
            normalize_aliases([str(i) for i in range(11)], slug="n")

    def test_description_is_one_line_and_bounded(self):
        assert normalize_description("  two\n lines ") == "two lines"
        with pytest.raises(MemoryFormatError) as e:
            normalize_description("x" * 161)
        assert _code(e) == "description_too_long"

    def test_extract_links(self):
        assert extract_links("[[a]] [[ B ]] [[a]] [[A]] [not] [[x\ny]]") == ["a", "B"]


# ---- validation --------------------------------------------------------


def _current(items=(), **fm) -> CurrentFile:
    base = dict(name="notes", created="2026-09-01T00:00:00Z", updated="2026-09-02T00:00:00Z", version=2)
    base.update(fm)
    return CurrentFile(frontmatter=Frontmatter(**base), items=tuple(items))


class TestValidateCanonicalSave:
    def test_new_file_gets_anchors(self):
        result = validate_canonical_save(
            slug="notes", files=[], current=None, text="- one\n- two\n", description="Notes"
        )
        assert [i.text for i in result.items] == ["one", "two"]
        assert result.minted_anchors == tuple(i.anchor for i in result.items)
        assert result.description == "Notes"
        assert parse_items(result.body) == result.items

    def test_frontmatter_beats_form_fields_and_none_keeps_current(self):
        current = _current(description="old", aliases=("canvas",))
        kept = validate_canonical_save(slug="notes", files=[F("notes")], current=current, items=())
        assert (kept.description, kept.aliases) == ("old", ("canvas",))
        given = validate_canonical_save(
            slug="notes",
            files=[F("notes")],
            current=current,
            text='---\ndescription: "from fm"\naliases: [x]\n---\n',
            description="from form",
        )
        assert (given.description, given.aliases) == ("from fm", ("x",))

    def test_echoed_locked_fields_are_accepted(self):
        current = _current(items=[Item("a", A1)])
        text = render_file(current.frontmatter, current.items)
        result = validate_canonical_save(slug="notes", files=[F("notes")], current=current, text=text)
        assert result.items == current.items

    @pytest.mark.parametrize(
        "fm_line, code",
        [
            ("name: other", "locked_field"),
            ("created: 2020-01-01T00:00:00Z", "locked_field"),
            ("scope: project", "locked_field"),
            ("version: 1", "stale_version"),
            ("updated: 2026-01-01T00:00:00Z", "stale_version"),
            ("status: active", "unknown_field"),
        ],
    )
    def test_locked_and_unknown_fields(self, fm_line, code):
        with pytest.raises(MemoryFormatError) as e:
            validate_canonical_save(
                slug="notes", files=[F("notes")], current=_current(), text=f"---\n{fm_line}\n---\n"
            )
        assert _code(e) == code

    def test_new_file_may_not_set_system_fields(self):
        with pytest.raises(MemoryFormatError) as e:
            validate_canonical_save(slug="notes", files=[], current=None, text="---\nversion: 1\n---\n")
        assert _code(e) == "locked_field"
        ok = validate_canonical_save(slug="notes", files=[], current=None, text="---\nname: notes\n---\n- a\n")
        assert len(ok.items) == 1

    def test_name_and_alias_collisions_are_case_insensitive(self):
        files = [F("canvas", aliases=("LMS",)), F("notes")]
        with pytest.raises(MemoryFormatError) as e:
            validate_canonical_save(slug="lms", files=files, current=None, items=())
        assert _code(e) == "name_collision"
        with pytest.raises(MemoryFormatError) as e:
            validate_canonical_save(slug="notes", files=files, current=_current(), items=(), aliases=["Canvas"])
        assert _code(e) == "alias_collision"
        # An existing file keeps its own aliases without colliding with itself.
        own = validate_canonical_save(
            slug="notes", files=[F("notes", aliases=("n",))], current=_current(aliases=("n",)), items=()
        )
        assert own.aliases == ("n",)

    def test_anchor_rules(self):
        current = _current(items=[Item("a", A1), Item("b", A2)])
        with pytest.raises(MemoryFormatError) as e:
            validate_canonical_save(
                slug="notes", files=[F("notes")], current=current, items=(Item("a", A1), Item("copy", A1))
            )
        assert _code(e) == "duplicate_anchor"
        moved = validate_canonical_save(
            slug="notes", files=[F("notes")], current=current, items=(Item("b edited", A2), Item("c"))
        )
        assert moved.items[0] == Item("b edited", A2)
        assert moved.removed_anchors == (A1,)
        assert len(moved.minted_anchors) == 1

    def test_links_resolve_including_self_aliases_and_index(self):
        result = validate_canonical_save(
            slug="notes",
            files=[F("canvas"), F("old", archived=True)],
            current=None,
            items=(Item("see [[Canvas]], [[old]], [[me]] and [[MEMORY.md]]"),),
            aliases=["me"],
        )
        assert result.links == ("Canvas", "old", "me", "MEMORY.md")
        assert result.archived_links == ("old",)
        assert result.warnings == ()

    def test_new_dead_link_fails_and_names_the_link(self):
        with pytest.raises(MemoryFormatError, match=r"\[\[nowhere\]\]") as e:
            validate_canonical_save(slug="notes", files=[], current=None, items=(Item("see [[nowhere]]"),))
        assert _code(e) == "dead_link"

    def test_needs_exactly_one_input(self):
        with pytest.raises(ValueError):
            validate_canonical_save(slug="notes", files=[], current=None)
        with pytest.raises(ValueError):
            validate_canonical_save(slug="notes", files=[], current=None, text="", items=())


class TestIndexAndFreeformLinks:
    def test_index_new_dead_link_fails_kept_one_warns(self):
        files = [F("canvas")]
        assert validate_index_links("# Memory\n[[canvas]]", files) == ()
        with pytest.raises(MemoryFormatError) as e:
            validate_index_links("[[gone]]", files)
        assert _code(e) == "dead_link"
        warnings = validate_index_links("[[gone]] still", files, previous_text="[[gone]]")
        assert warnings and "gone" in warnings[0]

    def test_freeform_only_warns(self):
        assert freeform_link_warnings("[[a]] [[self]]", [F("a")], slug="self") == ()
        warnings = freeform_link_warnings("[[missing]]", [], slug="self")
        assert warnings and "missing" in warnings[0]


# ---- tokens ------------------------------------------------------------


class _Client:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class TestTokens:
    def test_counts_with_the_base_model_id(self, monkeypatch):
        monkeypatch.setenv(memory_tokens.TOKEN_COUNT_MODEL_ENV, "us.anthropic.claude-haiku-4-5-20251001-v1:0")
        client = _Client(result={"inputTokens": 42})
        assert memory_tokens.count_file_tokens("hello", client=client) == memory_tokens.TokenCount(42, "count")
        call = client.calls[0]
        assert call["modelId"] == "anthropic.claude-haiku-4-5-20251001-v1:0"
        assert call["input"]["converse"]["messages"][0]["content"][0]["text"] == "hello"

    def test_default_model_when_unset(self, monkeypatch):
        monkeypatch.delenv(memory_tokens.TOKEN_COUNT_MODEL_ENV, raising=False)
        assert memory_tokens.token_count_model_id() == memory_tokens.DEFAULT_TOKEN_COUNT_MODEL_ID

    def test_empty_model_turns_counting_off(self, monkeypatch):
        monkeypatch.setenv(memory_tokens.TOKEN_COUNT_MODEL_ENV, "")
        client = _Client(result={"inputTokens": 1})
        assert memory_tokens.count_file_tokens("x" * 10, client=client) == memory_tokens.TokenCount(3, "estimate")
        assert client.calls == []

    @pytest.mark.parametrize(
        "client",
        [
            _Client(error=_ClientError("ThrottlingException")),
            _Client(error=_ClientError("AccessDeniedException")),
            _Client(error=TimeoutError()),
            _Client(result={}),
        ],
    )
    def test_any_failure_falls_back_to_the_estimate(self, client):
        assert memory_tokens.count_file_tokens("x" * 9, client=client, model_id="m") == memory_tokens.TokenCount(
            3, "estimate"
        )
        assert len(client.calls) == 1

    def test_empty_text_needs_no_call(self):
        client = _Client(result={"inputTokens": 5})
        assert memory_tokens.count_file_tokens("", client=client, model_id="m").tokens == 0
        assert client.calls == []

    def test_suite_never_counts_against_real_bedrock(self):
        # tests/conftest.py turns counting off so no test reaches Bedrock.
        assert memory_tokens.token_count_model_id() == ""


# ---- anchors out of injected memory (2.4b) --------------------------------


class TestStripAnchors:
    def test_removes_every_anchor_and_the_blanks_before_it(self):
        items = (Item(anchor="abcdefgh", text="Batch in groups of 50."), Item(anchor="hjkmnpqr", text="Two\nlines"))
        text = render_items(items)
        assert "<!-- e:" in text
        assert strip_anchors(text) == "- Batch in groups of 50.\n- Two\n  lines\n"

    def test_leaves_other_comments_and_text_alone(self):
        text = "- Keep <!-- a note --> and e:abcdefgh\n"
        assert strip_anchors(text) == text

    def test_accepts_either_case_and_loose_spacing(self):
        assert strip_anchors("- x<!--e:ABCDEFGH-->\n") == "- x\n"
