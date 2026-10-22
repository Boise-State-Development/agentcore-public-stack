"""Property-based tests for the canonical memory-file format.

Feature: shared-projects (Phase 2.3, docs/specs/shared-projects.md §4.2–§4.3)

The format is only useful if three things hold for *every* input, not just the
examples a person thinks of:

1. **Round trips.** Rendering then parsing gives back exactly what was
   rendered (items, anchors, frontmatter), and rendering is idempotent. A
   file that drifts by one space per save rewrites its bytes on every save.
2. **Anchor stability.** However an editor reorders, rewrites, adds and
   deletes items, every anchor it presents stays attached to that item, every
   anchor it omits is reported removed, and new items get fresh anchors.
3. **Link resolution.** A link resolves to the file whose name or alias it
   matches, in any casing; an archived target resolves as archived, not dead;
   a link to nothing is dead, and a save that *adds* a dead link fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from hypothesis import assume, given, settings, strategies as st

from apis.shared.memory.format import (
    Frontmatter,
    Item,
    MemoryFormatError,
    check_item_text,
    frontmatter_from_parsed,
    is_valid_anchor,
    new_anchor,
    normalize_aliases,
    normalize_description,
    normalize_item_text,
    parse_file,
    parse_frontmatter,
    parse_items,
    render_file,
    render_frontmatter,
    render_items,
    split_frontmatter,
)
from apis.shared.memory.validation import (
    LINK_ARCHIVED,
    LINK_FILE,
    CurrentFile,
    build_name_table,
    resolve_links,
    validate_canonical_save,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Any Unicode except lone surrogates (they cannot be encoded to UTF-8, so they
# can never reach storage).
_chars = st.characters(blacklist_categories=("Cs",))
_line = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r"), max_size=60)


def _is_item_text(text: str) -> bool:
    try:
        return check_item_text(text) == text
    except MemoryFormatError:
        return False


#: A normalized item text: one to four lines, as an editor might type them.
st_item_text = (
    st.lists(_line, min_size=1, max_size=4)
    .map("\n".join)
    .map(normalize_item_text)
    .filter(_is_item_text)
)

#: The same without square brackets, for saves that must not add a link.
st_plain_item_text = st_item_text.filter(lambda t: "[" not in t)

st_anchor = st.builds(new_anchor, randomness=st.integers(min_value=0, max_value=(1 << 40) - 1))


@st.composite
def st_items(draw, min_size: int = 0, max_size: int = 8) -> Tuple[Item, ...]:
    texts = draw(st.lists(st_item_text, min_size=min_size, max_size=max_size))
    anchors = draw(st.lists(st_anchor, min_size=len(texts), max_size=len(texts), unique=True))
    return tuple(Item(text=t, anchor=a) for t, a in zip(texts, anchors))


_slug_word = st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True)
st_slug = st.lists(_slug_word, min_size=1, max_size=3).map("-".join)

st_timestamp = st.datetimes().map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _clean_aliases(values, slug):
    try:
        return normalize_aliases(values, slug=slug)
    except MemoryFormatError:
        return None


@st.composite
def st_frontmatter(draw) -> Frontmatter:
    slug = draw(st_slug)
    description = normalize_description(draw(st.text(alphabet=_chars, max_size=160)))
    raw_aliases = draw(st.lists(st.text(alphabet=_chars, max_size=30), max_size=5))
    aliases = _clean_aliases(raw_aliases, slug)
    assume(aliases is not None)
    return Frontmatter(
        name=slug,
        description=description,
        aliases=aliases,
        created=draw(st_timestamp),
        updated=draw(st_timestamp),
        version=draw(st.integers(min_value=0, max_value=10**6)),
        scope=draw(st.sampled_from([None, "project", "personal_in_project", "personal"])),
    )


# ---------------------------------------------------------------------------
# 1. Round trips
# ---------------------------------------------------------------------------


@given(items=st_items())
def test_items_round_trip(items):
    assert parse_items(render_items(items)) == items


@given(items=st_items())
def test_item_render_is_idempotent(items):
    once = render_items(items)
    assert render_items(parse_items(once)) == once


@given(text=st.text(alphabet=_chars, max_size=200))
def test_normalize_item_text_is_idempotent(text):
    once = normalize_item_text(text)
    assert normalize_item_text(once) == once


@given(fm=st_frontmatter())
def test_frontmatter_round_trip(fm):
    fm_text, body = split_frontmatter(render_frontmatter(fm))
    assert body == ""
    assert frontmatter_from_parsed(parse_frontmatter(fm_text), name=fm.name) == fm


@given(fm=st_frontmatter(), items=st_items())
def test_file_round_trip(fm, items):
    rendered = render_file(fm, items)
    parsed = parse_file(rendered)
    assert parsed.items == items
    assert frontmatter_from_parsed(parsed.frontmatter, name=fm.name) == fm
    assert render_file(frontmatter_from_parsed(parsed.frontmatter, name=fm.name), parsed.items) == rendered


@given(fm=st_frontmatter(), items=st_items())
def test_echoing_a_file_back_changes_nothing(fm, items):
    """A caller that reads a file and writes it back unedited is a no-op save."""
    current = CurrentFile(frontmatter=fm, items=items)
    result = validate_canonical_save(
        slug=fm.name,
        files=[_File(fm.name, fm.aliases)],
        current=current,
        text=render_file(fm, items),
    )
    assert result.items == items
    assert result.minted_anchors == ()
    assert result.removed_anchors == ()
    assert result.description == fm.description
    assert result.aliases == fm.aliases
    assert result.body == render_items(items)


_bits = st.integers(min_value=0, max_value=(1 << 40) - 1)


@given(a=_bits, b=_bits)
def test_anchor_ids_are_valid_and_distinct_per_randomness(a, b):
    first, second = new_anchor(randomness=a), new_anchor(randomness=b)
    assert is_valid_anchor(first) and len(first) == 8
    assert (first == second) == (a == b)


# ---------------------------------------------------------------------------
# 2. Anchor stability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _File:
    slug: str
    aliases: Tuple[str, ...] = ()
    archived: bool = False


@st.composite
def st_edit(draw):
    """A current file plus an arbitrary edit of it.

    The edit keeps a random subset of the items (in a random order, some with
    rewritten text) and inserts new unanchored items anywhere.
    """
    current_items = draw(st_items(min_size=0, max_size=8))
    kept = draw(st.lists(st.sampled_from(range(len(current_items))), unique=True)) if current_items else []
    kept = draw(st.permutations(kept))
    edited = []
    for index in kept:
        original = current_items[index]
        text = draw(st.one_of(st.just(original.text), st_plain_item_text))
        edited.append(Item(text=text, anchor=original.anchor))
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        position = draw(st.integers(min_value=0, max_value=len(edited)))
        edited.insert(position, Item(text=draw(st_plain_item_text)))
    return current_items, tuple(edited)


@settings(max_examples=150)
@given(edit=st_edit(), use_text=st.booleans())
def test_anchors_are_stable_under_any_edit(edit, use_text):
    current_items, edited = edit
    fm = Frontmatter(name="notes", created="2026-09-01T00:00:00Z", updated="2026-09-02T00:00:00Z", version=3)
    current = CurrentFile(frontmatter=fm, items=current_items)
    kwargs = {"text": _render_unanchored(edited)} if use_text else {"items": edited}

    result = validate_canonical_save(slug="notes", files=[_File("notes")], current=current, **kwargs)

    assert len(result.items) == len(edited)
    for given_item, saved in zip(edited, result.items):
        assert saved.text == given_item.text
        if given_item.anchor is not None:
            assert saved.anchor == given_item.anchor
    presented = {i.anchor for i in edited if i.anchor}
    assert result.removed_anchors == tuple(i.anchor for i in current_items if i.anchor not in presented)
    old = {i.anchor for i in current_items}
    assert len(result.minted_anchors) == sum(1 for i in edited if i.anchor is None)
    for anchor in result.minted_anchors:
        assert is_valid_anchor(anchor)
        assert anchor not in old
    assert len({i.anchor for i in result.items}) == len(result.items)
    # The saved body parses back to exactly the saved items.
    assert parse_items(result.body) == result.items


def _render_unanchored(items) -> str:
    """Render items as a caller would type them: anchors only where known."""
    lines = []
    for item in items:
        if item.anchor:
            lines.append(render_items([item]).rstrip("\n"))
        else:
            parts = item.text.split("\n")
            lines.append("\n".join([f"- {parts[0]}"] + [f"  {p}" if p else "" for p in parts[1:]]))
    return "\n".join(lines) + "\n"


@given(edit=st_edit(), stranger=st_anchor)
def test_an_unknown_anchor_is_always_rejected(edit, stranger):
    current_items, edited = edit
    assume(stranger not in {i.anchor for i in current_items})
    current = CurrentFile(frontmatter=Frontmatter(name="notes"), items=current_items)
    try:
        validate_canonical_save(
            slug="notes",
            files=[_File("notes")],
            current=current,
            items=tuple(edited) + (Item(text="smuggled", anchor=stranger),),
        )
    except MemoryFormatError as exc:
        assert exc.code == "unknown_anchor"
    else:  # pragma: no cover - the property failing
        raise AssertionError("an anchor the file never had was accepted")


# ---------------------------------------------------------------------------
# 3. Link resolution
# ---------------------------------------------------------------------------


@st.composite
def st_space(draw):
    """A manifest of files with unique names and aliases, some archived."""
    slugs = draw(st.lists(st_slug, min_size=1, max_size=6, unique=True))
    taken = {s.casefold() for s in slugs}
    files = []
    for slug in slugs:
        aliases = []
        for alias in draw(st.lists(st.from_regex(r"[A-Za-z][A-Za-z0-9 ]{0,10}", fullmatch=True), max_size=3)):
            alias = " ".join(alias.split())
            if alias and alias.casefold() not in taken:
                taken.add(alias.casefold())
                aliases.append(alias)
        files.append(_File(slug=slug, aliases=tuple(aliases), archived=draw(st.booleans())))
    return files


def _random_case(draw, text: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(c.upper() if f else c.lower() for c, f in zip(text, flips))


@given(data=st.data(), files=st_space())
def test_links_resolve_to_names_and_aliases_in_any_case(data, files):
    table = build_name_table(files)
    for f in files:
        for name in (f.slug,) + f.aliases:
            link = _random_case(data.draw, name)
            report = resolve_links(f"see [[{link}]]", table)
            assert report.dead == ()
            slug, status = report.resolved[link]
            assert slug == f.slug
            assert status == (LINK_ARCHIVED if f.archived else LINK_FILE)


@given(files=st_space(), missing=st_slug)
def test_a_link_to_nothing_is_dead_and_a_new_one_fails_the_save(files, missing):
    assume(missing.casefold() not in build_name_table(files))
    assume(missing != "notes")
    assert resolve_links(f"[[{missing}]]", build_name_table(files)).dead == (missing,)

    current = CurrentFile(frontmatter=Frontmatter(name="notes"), items=())
    try:
        validate_canonical_save(
            slug="notes", files=files, current=current, items=(Item(text=f"see [[{missing}]]"),)
        )
    except MemoryFormatError as exc:
        assert exc.code == "dead_link"
    else:  # pragma: no cover - the property failing
        raise AssertionError("a save that adds a dead link was accepted")


@given(files=st_space(), missing=st_slug)
def test_a_dead_link_the_file_already_had_is_only_a_warning(files, missing):
    assume(missing.casefold() not in build_name_table(files))
    assume(missing != "notes")
    anchor = new_anchor()
    old = Item(text=f"see [[{missing}]]", anchor=anchor)
    current = CurrentFile(frontmatter=Frontmatter(name="notes"), items=(old,))
    result = validate_canonical_save(slug="notes", files=files, current=current, items=(old,))
    assert result.warnings and missing in result.warnings[0]


@given(files=st_space())
def test_links_to_archived_files_are_not_dead(files):
    archived = [f for f in files if f.archived]
    assume(archived and all(f.slug != "zz-notes" for f in files))
    target = archived[0]
    current = CurrentFile(frontmatter=Frontmatter(name="zz-notes"), items=())
    result = validate_canonical_save(
        slug="zz-notes", files=files, current=current, items=(Item(text=f"see [[{target.slug}]]"),)
    )
    assert result.archived_links == (target.slug,)
    assert result.warnings == ()
