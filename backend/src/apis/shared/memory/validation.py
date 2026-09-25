"""Save-time validation for memory files (Shared Projects §4.3, steps 1–4).

Pure functions over the space's manifest and the file's current version: no
I/O, so every write path (UI, tools, and later proposals and maintenance) runs
the same checks and tests can drive them directly. The service does the
steps that need the outside world: token counting (step 5), the write, the
``FILEVER`` row and the manifest swap (step 7).

A failure raises :class:`~apis.shared.memory.format.MemoryFormatError` with
one actionable message, before anything is written.

Two rules go beyond the spec's sketch, both so that ordinary edits never fail
because of someone else's change:

- **Only new dead links fail.** A ``[[link]]`` the file already had when its
  target was deleted is reported as a warning, not an error, so deleting one
  file never blocks edits to the files that pointed at it.
- **Locked frontmatter may be echoed.** A caller that read a file and writes it
  back with its frontmatter intact is accepted; only a *changed* locked field
  fails. A changed ``version`` means the file moved since it was read, and the
  message says to re-read it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .format import (
    INDEX_SLUG,
    Frontmatter,
    FrontmatterValue,
    Item,
    MemoryFormatError,
    ParsedFile,
    check_item_text,
    extract_links,
    is_valid_anchor,
    new_anchor,
    normalize_aliases,
    normalize_description,
    parse_file,
    render_items,
    validate_slug,
)

EDITABLE_FIELDS = frozenset({"description", "aliases"})
LOCKED_FIELDS = frozenset({"name", "scope", "created", "updated", "version"})

LINK_FILE = "file"
LINK_ARCHIVED = "archived"
LINK_INDEX = "index"


class FileTarget(Protocol):
    """What resolution needs from a manifest entry (``MemoryEntryRef`` fits)."""

    slug: str


@dataclass(frozen=True)
class CurrentFile:
    """The version of a file the save replaces."""

    frontmatter: Frontmatter
    items: Tuple[Item, ...] = ()


@dataclass(frozen=True)
class CanonicalSave:
    """A validated save, ready to render and write.

    ``items`` all carry anchors. ``body`` is the rendered item list (the caller
    renders the frontmatter, which needs the clock and the version counter).
    """

    slug: str
    description: str
    aliases: Tuple[str, ...]
    items: Tuple[Item, ...]
    body: str
    minted_anchors: Tuple[str, ...] = ()
    removed_anchors: Tuple[str, ...] = ()
    links: Tuple[str, ...] = ()
    archived_links: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkReport:
    """Where each ``[[link]]`` in a text points."""

    resolved: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # name -> (slug, status)
    dead: Tuple[str, ...] = ()

    @property
    def archived(self) -> Tuple[str, ...]:
        return tuple(name for name, (_, status) in self.resolved.items() if status == LINK_ARCHIVED)


def _aliases_of(target: FileTarget) -> Tuple[str, ...]:
    return tuple(getattr(target, "aliases", ()) or ())


def _archived(target: FileTarget) -> bool:
    return bool(getattr(target, "archived", False))


# ---- names and links ---------------------------------------------------


def build_name_table(
    files: Iterable[FileTarget],
    *,
    exclude_slug: Optional[str] = None,
) -> Dict[str, FileTarget]:
    """Map every name and alias (case-folded) to its file.

    Slugs are claimed before aliases, and files are taken in slug order, so an
    old freeform space that already has a clash still resolves the same way on
    every save.
    """
    ordered = sorted(
        (f for f in files if f.slug != exclude_slug),
        key=lambda f: f.slug,
    )
    table: Dict[str, FileTarget] = {}
    for f in ordered:
        table.setdefault(f.slug.casefold(), f)
    for f in ordered:
        for alias in _aliases_of(f):
            table.setdefault(alias.casefold(), f)
    return table


def resolve_links(text: str, table: Dict[str, FileTarget]) -> LinkReport:
    """Resolve the links in ``text`` against a :func:`build_name_table` table.

    ``[[MEMORY.md]]`` resolves to the index. A file flagged ``archived``
    resolves with status ``archived``; it is not dead.
    """
    resolved: Dict[str, Tuple[str, str]] = {}
    dead: List[str] = []
    for name in extract_links(text):
        key = name.casefold()
        if key == INDEX_SLUG.casefold():
            resolved[name] = (INDEX_SLUG, LINK_INDEX)
            continue
        target = table.get(key)
        if target is None:
            dead.append(name)
        else:
            resolved[name] = (target.slug, LINK_ARCHIVED if _archived(target) else LINK_FILE)
    return LinkReport(resolved=resolved, dead=tuple(dead))


def check_name_collisions(
    slug: str,
    aliases: Sequence[str],
    files: Iterable[FileTarget],
    *,
    is_new: bool,
) -> None:
    """Reject a name or alias another file already answers to (case-insensitive).

    The slug is checked only when the file is new: an existing file keeps its
    name. Run it again against the manifest being committed, since another
    save may have claimed the name in between.
    """
    others = build_name_table(files, exclude_slug=slug)
    if is_new:
        owner = others.get(slug.casefold())
        if owner is not None:
            raise MemoryFormatError(
                f"'{slug}' is already used by the file '{owner.slug}' (as its name or an alias).",
                code="name_collision",
            )
    for alias in aliases:
        owner = others.get(alias.casefold())
        if owner is not None:
            raise MemoryFormatError(
                f"The alias '{alias}' is already used by the file '{owner.slug}'.",
                code="alias_collision",
            )


def _dead_link_error(names: Sequence[str]) -> MemoryFormatError:
    listed = ", ".join(f"[[{n}]]" for n in names)
    return MemoryFormatError(
        f"{listed} {'does' if len(names) == 1 else 'do'} not match any file name or alias in this "
        "space. Link to an existing file, or create that file first.",
        code="dead_link",
    )


def _kept_dead_warning(names: Sequence[str]) -> str:
    listed = ", ".join(f"[[{n}]]" for n in names)
    return f"{listed} no longer {'points' if len(names) == 1 else 'point'} to a file in this space."


def _dead_warning(names: Sequence[str]) -> str:
    listed = ", ".join(f"[[{n}]]" for n in names)
    return f"{listed} {'does' if len(names) == 1 else 'do'} not match any entry in this space."


# ---- frontmatter from the caller ----------------------------------------


def _check_caller_frontmatter(
    values: Dict[str, FrontmatterValue],
    *,
    slug: str,
    current: Optional[CurrentFile],
) -> None:
    """Allow the editable fields; allow locked ones only as unchanged echoes."""
    for key in sorted(values):
        if key in EDITABLE_FIELDS:
            continue
        if key not in LOCKED_FIELDS:
            raise MemoryFormatError(
                f"'{key}' is not a memory file field. Only description and aliases can be set.",
                code="unknown_field",
            )
        given = values[key]
        if key == "name":
            if given != slug:
                raise MemoryFormatError(
                    f"The file name is locked: it is '{slug}', not '{given}'. Save under a new "
                    "name to create a new file.",
                    code="locked_field",
                )
            continue
        if current is None:
            if given not in ("", None):
                raise MemoryFormatError(
                    f"'{key}' is set by the system. Leave it out when creating a file.",
                    code="locked_field",
                )
            continue
        fm = current.frontmatter
        expected: FrontmatterValue = {
            "scope": fm.scope or "",
            "created": fm.created,
            "updated": fm.updated,
            "version": fm.version,
        }[key]
        if given == expected:
            continue
        if key in ("version", "updated"):
            raise MemoryFormatError(
                f"This file changed since it was read (it is now version {fm.version}). "
                "Read it again and re-apply the edit.",
                code="stale_version",
            )
        raise MemoryFormatError(f"'{key}' is set by the system and cannot be changed.", code="locked_field")


# ---- the canonical save -------------------------------------------------


def validate_canonical_save(
    *,
    slug: str,
    files: Sequence[FileTarget],
    current: Optional[CurrentFile],
    text: Optional[str] = None,
    items: Optional[Sequence[Item]] = None,
    description: Optional[str] = None,
    aliases: Optional[Sequence[str]] = None,
    mint: Callable[[], str] = new_anchor,
) -> CanonicalSave:
    """Validate one save of a canonical file (§4.3 steps 1–4).

    Give either ``text`` (a whole file, frontmatter optional, as a tool or a
    raw editor sends it) or ``items`` (the structured form). ``description``
    and ``aliases`` are the form fields; frontmatter in ``text`` wins over
    them, and either wins over the current values. ``None`` keeps the current
    value.
    """
    if (text is None) == (items is None):
        raise ValueError("give exactly one of text or items")
    slug = validate_slug(slug, canonical=True)

    # Step 1: parse, and hold the caller to the editable fields.
    if text is not None:
        parsed: ParsedFile = parse_file(text)
        given_items: Sequence[Item] = parsed.items
        if parsed.frontmatter is not None:
            _check_caller_frontmatter(parsed.frontmatter, slug=slug, current=current)
            if "description" in parsed.frontmatter:
                fm_desc = parsed.frontmatter["description"]
                description = fm_desc if isinstance(fm_desc, str) else ""
            if "aliases" in parsed.frontmatter:
                fm_aliases = parsed.frontmatter["aliases"]
                if isinstance(fm_aliases, str):
                    fm_aliases = [fm_aliases] if fm_aliases else []
                if not isinstance(fm_aliases, list):
                    raise MemoryFormatError("'aliases' must be a list.", code="alias_invalid")
                aliases = fm_aliases
    else:
        given_items = items or ()

    if description is None:
        description = current.frontmatter.description if current else ""
    if aliases is None:
        aliases = current.frontmatter.aliases if current else ()
    description = normalize_description(description)
    clean_aliases = normalize_aliases(aliases, slug=slug)

    # Step 2: names and aliases are unique across the space.
    check_name_collisions(slug, clean_aliases, files, is_new=current is None)

    # Step 3: anchors are known, unique, or minted now.
    known = [i.anchor for i in (current.items if current else ()) if i.anchor]
    known_set = set(known)
    seen: set[str] = set()
    anchored: List[Item] = []
    minted: List[str] = []
    for position, item in enumerate(given_items, start=1):
        try:
            item_text = check_item_text(item.text)
        except MemoryFormatError as exc:
            raise MemoryFormatError(f"Item {position}: {exc}", code=exc.code) from exc
        anchor = item.anchor.lower() if item.anchor else None
        if anchor is not None:
            if not is_valid_anchor(anchor):
                raise MemoryFormatError(f"Item {position} has a malformed anchor.", code="malformed_anchor")
            if anchor not in known_set:
                raise MemoryFormatError(
                    f"Item {position} carries an anchor this file does not have. Anchors are "
                    "assigned by the system: remove the <!-- e:… --> comment to add it as a new item.",
                    code="unknown_anchor",
                )
            if anchor in seen:
                raise MemoryFormatError(
                    f"Item {position} repeats the anchor of an earlier item. Each item keeps its own "
                    "anchor: remove the comment from the copy.",
                    code="duplicate_anchor",
                )
        anchored.append(Item(text=item_text, anchor=anchor))
        if anchor is not None:
            seen.add(anchor)
    final_items: List[Item] = []
    for item in anchored:
        if item.anchor is None:
            fresh = mint()
            while fresh in known_set or fresh in seen:
                fresh = mint()
            seen.add(fresh)
            minted.append(fresh)
            item = Item(text=item.text, anchor=fresh)
        final_items.append(item)
    removed = tuple(a for a in known if a not in seen)

    # Step 4: links resolve (against this file's new aliases, too).
    body = render_items(final_items)
    table = build_name_table(files, exclude_slug=slug)
    self_target = _SelfTarget(slug=slug, aliases=clean_aliases)
    table[slug.casefold()] = self_target
    for alias in clean_aliases:
        table.setdefault(alias.casefold(), self_target)
    report = resolve_links(body, table)
    previous = {n.casefold() for n in extract_links("\n".join(i.text for i in current.items))} if current else set()
    new_dead = [n for n in report.dead if n.casefold() not in previous]
    if new_dead:
        raise _dead_link_error(new_dead)
    warnings = (_kept_dead_warning(report.dead),) if report.dead else ()

    return CanonicalSave(
        slug=slug,
        description=description,
        aliases=clean_aliases,
        items=tuple(final_items),
        body=body,
        minted_anchors=tuple(minted),
        removed_anchors=removed,
        links=tuple(extract_links(body)),
        archived_links=report.archived,
        warnings=warnings,
    )


@dataclass(frozen=True)
class _SelfTarget:
    slug: str
    aliases: Tuple[str, ...] = ()
    archived: bool = False


# ---- the index and freeform entries -------------------------------------


def validate_index_links(
    text: str,
    files: Sequence[FileTarget],
    *,
    previous_text: str = "",
) -> Tuple[str, ...]:
    """Check ``MEMORY.md`` links in a canonical space; return warnings.

    The index may hold prose. Its links follow the file rule: new dead links
    fail, dead links it already had are warnings.
    """
    report = resolve_links(text, build_name_table(files))
    previous = {n.casefold() for n in extract_links(previous_text)}
    new_dead = [n for n in report.dead if n.casefold() not in previous]
    if new_dead:
        raise _dead_link_error(new_dead)
    return (_kept_dead_warning(report.dead),) if report.dead else ()


def freeform_link_warnings(text: str, files: Sequence[FileTarget], *, slug: str) -> Tuple[str, ...]:
    """Dead links in a freeform entry, as warnings only (freeform never fails on links)."""
    table = build_name_table(files)
    table.setdefault(slug.casefold(), _SelfTarget(slug=slug))
    report = resolve_links(text, table)
    return (_dead_warning(report.dead),) if report.dead else ()
