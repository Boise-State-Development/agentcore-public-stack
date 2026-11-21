"""Where a skill file sits in its bundle: the path rule and the manifest path.

A skill's supporting files live below one of the agentskills.io kind
directories (``references/``, ``scripts/``, ``assets/``). A manifest entry's
``path`` is the file's location *below* that directory: ``nda.md`` or
``legal/nda.md``. Every row written before nested paths existed has
``path == filename``, so the rule here is the old flat-filename rule applied
per segment, with at most :data:`MAX_PATH_SEGMENTS` segments.

A path is unique within a skill's manifest across kinds, as a filename was
before: uploading ``x.md`` as a script when it is a reference moves it.

Dependency-free on purpose, so the model, the write path, import, export and the
runtime's lookups share one rule (``apis.shared`` only; no ``app_api`` or
``agents`` import).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

from apis.shared.skills.resource_store import KIND_DIRS

# Safe filenames: no separators, no traversal, a visible first character. A
# leading '_' is allowed so a Python package keeps its ``__init__.py`` (a
# package whose marker is renamed no longer imports); a leading '.' (hidden
# file) or '-' (read as a command-line flag) is not.
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

#: The deepest a file may sit below its kind directory (``a/b/c/file.md``).
MAX_PATH_SEGMENTS = 4


def path_problem(path: Optional[str]) -> Optional[str]:
    """Why ``path`` is not an acceptable manifest path, or ``None`` when it is."""
    if not path:
        return "the path is empty"
    if "\x00" in path:
        return "the path contains a NUL character"
    if "\\" in path:
        return "use '/' between folders, not a backslash"
    if path.startswith("/"):
        return "the path must be relative (no leading '/')"
    segments = path.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        return f"a file may sit at most {MAX_PATH_SEGMENTS} levels deep"
    for segment in segments:
        if segment == "":
            return "the path has an empty folder name"
        if segment in (".", ".."):
            return "the path may not contain '.' or '..'"
        if not FILENAME_PATTERN.match(segment):
            return (
                f"'{segment}' is not allowed: use letters, digits, '.', '_' and '-' "
                "only, starting with a letter, digit or '_', 1-128 characters"
            )
    return None


def validate_resource_path(path: Optional[str]) -> str:
    """Return ``path`` when it passes the rule; raise ``ValueError`` saying why not."""
    problem = path_problem(path)
    if problem is not None:
        raise ValueError(f"Invalid file path '{path}': {problem}.")
    return path  # type: ignore[return-value]


def kind_dir(kind: Optional[str]) -> str:
    """The bundle directory for ``kind``; an unknown kind reads as ``references``."""
    return KIND_DIRS.get(kind or "reference", "references")


def ref_path(ref: Any) -> str:
    """A manifest entry's path below its kind directory (its filename for old rows)."""
    return getattr(ref, "path", None) or getattr(ref, "filename", "") or ""


def is_top_level(ref: Any) -> bool:
    """Whether the entry sits directly in its kind directory (every pre-nesting row)."""
    return "/" not in ref_path(ref)


def manifest_path(ref: Any) -> str:
    """The entry's standard bundle path, e.g. ``references/legal/nda.md``."""
    return f"{kind_dir(getattr(ref, 'kind', 'reference'))}/{ref_path(ref)}"


class PathLookup:
    """What a lookup found: the entry, or every entry a bare name was ambiguous between."""

    __slots__ = ("ref", "ambiguous")

    def __init__(self, ref: Any = None, ambiguous: Optional[List[Any]] = None) -> None:
        self.ref = ref
        self.ambiguous = ambiguous or []


def lookup_resource(
    resources: Iterable[Any],
    wanted: str,
    *,
    accept_manifest_path: bool = False,
    nested_bare_names: bool = True,
) -> PathLookup:
    """Find a manifest entry by what a caller named. A manifest lookup, never a path.

    In order, the first that names exactly one entry wins:

    1. the standard bundle path (``references/legal/nda.md``), when
       ``accept_manifest_path`` (the model's reads, which list those paths);
    2. the path below the kind directory (``legal/nda.md``; for a top-level file
       that is its filename, so every old call still resolves);
    3. a bare filename (``nda.md``) when exactly one file has it. Two or more is
       reported back as ``ambiguous`` rather than guessed.

    ``nested_bare_names=False`` is script resolution's strict form: only the full
    manifest path reaches a nested file; a short form (``sub/x.py``, ``x.py``)
    names only a file at the top of its directory.
    """
    refs = list(resources or [])
    if accept_manifest_path:
        for ref in refs:
            if manifest_path(ref) == wanted:
                return PathLookup(ref)
    if nested_bare_names or "/" not in wanted:
        for ref in refs:
            if ref_path(ref) == wanted:
                return PathLookup(ref)
    if not wanted or "/" in wanted or not nested_bare_names:
        return PathLookup()
    named = [r for r in refs if getattr(r, "filename", "") == wanted]
    if len(named) == 1:
        return PathLookup(named[0])
    return PathLookup(ambiguous=sorted(named, key=manifest_path) if named else [])
