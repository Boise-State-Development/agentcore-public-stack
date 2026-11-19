"""Read an agentskills.io bundle out of a zip archive (docs/specs/skill-bundle-import.md).

One parser for both sources: a ``.zip`` a user uploads and the archive GitHub
serves for a repository. It is pure (bytes in, a :class:`ParsedBundle` out) and
does no I/O beyond reading the archive in memory, so app-api runs it in a worker
thread and the tests drive it without AWS.

Security bounds refuse the whole archive (:class:`BundleImportError`); content
rules skip one file and say why (spec D6). Nothing here decides access: the
caller has already resolved the owned skill, and every write still goes through
``SkillBundleFiles`` (its filename rule, type allowlist and field-set check are
reused here so the preview reports exactly what a write would refuse).
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.extraction.field_sets import validate_field_set_upload
from apis.shared.skills.bundle import KIND_DIRS
from apis.shared.skills.bundle_files import (
    MAX_RESOURCE_BYTES,
    MAX_RESOURCES_PER_SKILL,
    SkillBundleFiles,
)
from apis.shared.skills.models import (
    SKILL_DESCRIPTION_MAX_LENGTH,
    SKILL_INSTRUCTIONS_MAX_BYTES,
    SkillDefinition,
)
from apis.shared.skills.skill_md import (
    MAX_FRONTMATTER_NODES,
    SkillMdError,
    unsafe_character_problem,
)
from apis.shared.skills.skill_md import parse_skill_md as _parse_skill_md
from apis.shared.skills.resource_store import compute_content_hash
from apis.shared.skills.resource_types import (
    SkillResourceTypeError,
    is_text_content_type,
    resolve_upload_content_type,
)

# Bounds (spec §5). The archive caps are the caller's (an upload and a GitHub
# download differ); these apply to what is inside.
MAX_UPLOAD_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_GITHUB_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5_000
# SKILL.md becomes the row's ``instructions``, which shares a 400 KB DynamoDB
# item with the manifest: the same cap as every other write of instructions.
MAX_SKILL_MD_BYTES = SKILL_INSTRUCTIONS_MAX_BYTES
MAX_IMPORT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_DISPLAY_NAME_LENGTH = 100

SKILL_MD = "SKILL.md"

# First directory → kind. ``reference/`` (singular) is common in the wild
# (anthropics/skills' mcp-builder uses it).
_KIND_BY_DIR = {
    "references": "reference",
    "reference": "reference",
    "scripts": "script",
    "assets": "asset",
}

# Frontmatter keys the projection owns (mirrors ``bundle._RESERVED_METADATA_KEYS``).
_RESERVED_KEYS = frozenset({"name", "description", "allowed-tools", "instructions"})

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_ZIP_ENCRYPTED_FLAG = 0x1
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FILE_STATUSES = ("added", "changed", "unchanged", "removed", "skipped")


class BundleImportError(ValueError):
    """The archive cannot be imported at all (400). The message is shown as is."""


@dataclass(frozen=True)
class BundleFile:
    """One file the import would write."""

    path: str  # relative to the bundle root, as the author wrote it
    filename: str  # the manifest name (§4)
    kind: str
    content: bytes
    content_type: str
    content_hash: str

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class SkippedFile:
    """One file the import leaves out, and why."""

    path: str
    reason: str
    size: int = 0


@dataclass
class ParsedBundle:
    """A bundle read out of an archive, ready to diff or write."""

    root: str
    name: str
    display_name: str
    description: str
    instructions: str
    allowed_tools: List[str]
    skill_metadata: Dict[str, Any]
    files: List[BundleFile]
    skipped: List[SkippedFile]
    commit_sha: Optional[str] = None
    bundle_hash: str = ""

    def __post_init__(self) -> None:
        if not self.bundle_hash:
            self.bundle_hash = _bundle_hash(self)


@dataclass
class BundleLoad:
    """What reading an archive found: one bundle, or the skills to choose from."""

    bundle: Optional[ParsedBundle] = None
    candidates: List[str] = field(default_factory=list)
    commit_sha: Optional[str] = None


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str
    filename: Optional[str] = None
    kind: Optional[str] = None
    size: int = 0
    reason: Optional[str] = None


@dataclass(frozen=True)
class FieldChanges:
    description: bool
    instructions: bool
    allowed_tools: bool
    skill_metadata: bool


# =============================================================================
# Reading the archive
# =============================================================================


def load_bundle(data: bytes, *, path: Optional[str] = None) -> BundleLoad:
    """Read ``data`` (a zip) and return the bundle at ``path``.

    ``path`` is the bundle's directory inside the archive, after the single
    top-level folder is stripped (for GitHub, a path in the repository). Without
    one, an archive holding exactly one ``SKILL.md`` imports it; one holding
    several returns their directories as ``candidates`` and no bundle.

    Raises:
        BundleImportError: The archive is not a zip, breaks a security bound, has
            no ``SKILL.md`` where asked, or the bundle breaks a size bound.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise BundleImportError("That file is not a valid .zip archive.") from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise BundleImportError(
                f"The archive has {len(infos):,} entries; the limit is "
                f"{MAX_ARCHIVE_ENTRIES:,}. Import a smaller archive or a path "
                "inside the repository."
            )
        for info in infos:
            _check_entry(info)

        comment = archive.comment.decode("ascii", errors="ignore").strip()
        commit_sha = comment if _SHA_RE.match(comment) else None

        entries = _strip_top_folder(
            [(info.filename, info) for info in infos if not info.is_dir()]
        )
        entries = [(name, info) for name, info in entries if not _is_ignored(name)]

        skill_dirs = sorted(
            {posixpath.dirname(name) for name, _ in entries if posixpath.basename(name) == SKILL_MD}
        )
        root = _choose_root(skill_dirs, path)
        if root is None:
            return BundleLoad(candidates=skill_dirs, commit_sha=commit_sha)

        bundle = _read_bundle(archive, entries, root)
        bundle.commit_sha = commit_sha
        return BundleLoad(bundle=bundle, commit_sha=commit_sha)


def normalize_bundle_path(path: Optional[str]) -> str:
    """``/skills/pdf/SKILL.md`` → ``skills/pdf``; refuses traversal."""
    cleaned = (path or "").strip().strip("/")
    if cleaned == SKILL_MD or cleaned.endswith("/" + SKILL_MD):
        cleaned = posixpath.dirname(cleaned)
    if not cleaned:
        return ""
    if "\\" in cleaned or "\x00" in cleaned or any(
        seg in ("", ".", "..") for seg in cleaned.split("/")
    ):
        raise BundleImportError(f"'{path}' is not a valid path inside the archive.")
    return cleaned


def _check_entry(info: zipfile.ZipInfo) -> None:
    """Refuse an entry no well-formed bundle has (spec D6)."""
    name = info.filename
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or _DRIVE_LETTER.match(name)
        or ".." in name.split("/")
    ):
        raise BundleImportError(
            "The archive contains an unsafe path and was not imported."
        )
    if info.flag_bits & _ZIP_ENCRYPTED_FLAG:
        raise BundleImportError(
            "The archive contains encrypted files, which cannot be imported."
        )


def _strip_top_folder(
    entries: List[Tuple[str, zipfile.ZipInfo]],
) -> List[Tuple[str, zipfile.ZipInfo]]:
    """Drop one folder that wraps everything (GitHub's ``repo-ref/``, zip tools')."""
    if not entries:
        return entries
    firsts = {name.split("/", 1)[0] for name, _ in entries}
    if len(firsts) == 1 and all("/" in name for name, _ in entries):
        prefix = next(iter(firsts)) + "/"
        return [(name[len(prefix):], info) for name, info in entries]
    return entries


def _is_ignored(name: str) -> bool:
    return any(seg.startswith(".") or seg == "__MACOSX" for seg in name.split("/"))


def _choose_root(skill_dirs: List[str], path: Optional[str]) -> Optional[str]:
    """The bundle root: ``path`` when given (``""`` is the top level), else the only one."""
    if path is not None:
        wanted = normalize_bundle_path(path)
        if wanted in skill_dirs:
            return wanted
        hint = ""
        if skill_dirs:
            shown = ", ".join(d or "(top level)" for d in skill_dirs[:10])
            hint = f" Skills found: {shown}."
        raise BundleImportError(f"No SKILL.md at '{wanted or '/'}'.{hint}")
    if not skill_dirs:
        raise BundleImportError(
            "No SKILL.md found. A skill bundle needs a SKILL.md at its top level."
        )
    if len(skill_dirs) == 1:
        return skill_dirs[0]
    return None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Optional[bytes]:
    """The entry's bytes, or ``None`` past ``limit`` — whatever its header claims."""
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    return None if len(data) > limit else data


def _read_bundle(
    archive: zipfile.ZipFile,
    entries: List[Tuple[str, zipfile.ZipInfo]],
    root: str,
) -> ParsedBundle:
    prefix = f"{root}/" if root else ""
    members = [(name[len(prefix):], info) for name, info in entries if name.startswith(prefix)]

    skill_md_info = next(info for rel, info in members if rel == SKILL_MD)
    raw = _read_bounded(archive, skill_md_info, MAX_SKILL_MD_BYTES)
    if raw is None:
        raise BundleImportError(
            f"SKILL.md is larger than {MAX_SKILL_MD_BYTES // 1024} KiB."
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BundleImportError("SKILL.md is not UTF-8 text.") from None
    frontmatter, instructions = parse_skill_md(text)

    skipped: List[SkippedFile] = []
    planned: List[Tuple[str, str, List[str], str, zipfile.ZipInfo]] = []
    for rel, info in sorted(members, key=lambda m: m[0]):
        if rel == SKILL_MD:
            continue
        if posixpath.basename(rel) == SKILL_MD:
            skipped.append(SkippedFile(rel, "another skill's SKILL.md", info.file_size))
            continue
        if _is_symlink(info):
            skipped.append(SkippedFile(rel, "symbolic link", 0))
            continue
        try:
            content_type = resolve_upload_content_type(posixpath.basename(rel))
        except SkillResourceTypeError:
            ext = posixpath.splitext(rel)[1] or "no extension"
            skipped.append(SkippedFile(rel, f"file type not allowed ({ext})", info.file_size))
            continue
        segments = rel.split("/")
        kind = _KIND_BY_DIR.get(segments[0]) if len(segments) > 1 else None
        sub = segments[1:] if kind else segments
        if kind is None:
            kind = "reference" if is_text_content_type(content_type) else "asset"
        planned.append((rel, kind, sub, content_type, info))

    files = _name_and_read(archive, planned, skipped)

    if len(files) > MAX_RESOURCES_PER_SKILL:
        raise BundleImportError(
            f"This skill has {len(files)} files to import; a skill holds at most "
            f"{MAX_RESOURCES_PER_SKILL}."
        )
    total = sum(f.size for f in files)
    if total > MAX_IMPORT_TOTAL_BYTES:
        raise BundleImportError(
            f"This skill's files add up to {total // (1024 * 1024)} MiB; the limit "
            f"is {MAX_IMPORT_TOTAL_BYTES // (1024 * 1024)} MiB."
        )

    name = str(frontmatter.get("name") or posixpath.basename(root) or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        raise BundleImportError(
            "SKILL.md has no description in its frontmatter. The description is "
            "the line the agent reads when deciding whether to use the skill."
        )
    if len(description) > SKILL_DESCRIPTION_MAX_LENGTH:
        raise BundleImportError(
            f"SKILL.md's description is {len(description)} characters; the limit "
            f"is {SKILL_DESCRIPTION_MAX_LENGTH}."
        )
    display_name = humanize_skill_name(name)
    for label, value in (("SKILL.md's description", description), ("SKILL.md's name", display_name)):
        unsafe = unsafe_character_problem(label, value)
        if unsafe:
            raise BundleImportError(unsafe[0].upper() + unsafe[1:] + ".")

    return ParsedBundle(
        root=root,
        name=name,
        display_name=display_name,
        description=description,
        instructions=instructions,
        allowed_tools=_allowed_tools(frontmatter.get("allowed-tools")),
        skill_metadata=_dynamo_safe(
            {k: v for k, v in frontmatter.items() if k not in _RESERVED_KEYS}
        ),
        files=sorted(files, key=lambda f: f.filename),
        skipped=skipped,
    )


def _name_and_read(
    archive: zipfile.ZipFile,
    planned: List[Tuple[str, str, List[str], str, zipfile.ZipInfo]],
    skipped: List[SkippedFile],
) -> List[BundleFile]:
    """Pick each file's flat manifest name (§4), then read it within bounds."""
    basenames: Dict[str, int] = {}
    for _, _, sub, _, _ in planned:
        basenames[sub[-1]] = basenames.get(sub[-1], 0) + 1

    files: List[BundleFile] = []
    taken: set = set()
    for rel, kind, sub, content_type, info in planned:
        candidate = sub[-1] if basenames[sub[-1]] == 1 else "-".join(sub)
        filename = _flat_name(candidate)
        if filename is None or filename in taken:
            skipped.append(SkippedFile(rel, "name clashes with another file or is not allowed", info.file_size))
            continue
        if info.file_size > MAX_RESOURCE_BYTES:
            skipped.append(SkippedFile(rel, "larger than 1 MiB", info.file_size))
            continue
        content = _read_bounded(archive, info, MAX_RESOURCE_BYTES)
        if content is None:
            skipped.append(SkippedFile(rel, "larger than 1 MiB", info.file_size))
            continue
        if not content:
            skipped.append(SkippedFile(rel, "empty file", 0))
            continue
        if kind == "reference":
            try:
                validate_field_set_upload(filename, content)
            except ValueError as e:
                skipped.append(SkippedFile(rel, f"invalid field set: {e}", len(content)))
                continue
        taken.add(filename)
        files.append(
            BundleFile(
                path=rel,
                filename=filename,
                kind=kind,
                content=content,
                content_type=content_type,
                content_hash=compute_content_hash(content),
            )
        )
    return files


def _flat_name(candidate: str) -> Optional[str]:
    """``candidate`` if it passes the manifest's filename rule, else a cleaned form."""
    for name in (candidate, _UNSAFE_NAME_CHARS.sub("_", candidate).lstrip("._-")):
        try:
            SkillBundleFiles.validate_filename(name)
        except ValueError:
            continue
        return name
    return None


# =============================================================================
# SKILL.md
# =============================================================================


def parse_skill_md(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a SKILL.md into its YAML frontmatter and its body.

    A thin wrapper over :func:`apis.shared.skills.skill_md.parse_skill_md`. No
    frontmatter means an empty mapping and the whole text as the body.

    Raises:
        BundleImportError: The frontmatter cannot be read (not valid YAML, not a
            mapping, over a size, node or depth bound, or uses an alias).
    """
    try:
        parsed = _parse_skill_md(text)
    except SkillMdError as exc:
        raise BundleImportError(str(exc)) from None
    return parsed.frontmatter, parsed.body


def humanize_skill_name(name: str) -> str:
    """``mcp-builder`` → ``Mcp builder``, capped at the display-name limit."""
    words = re.sub(r"[-_\s]+", " ", name or "").strip()
    if not words:
        return "Imported skill"
    return (words[0].upper() + words[1:])[:MAX_DISPLAY_NAME_LENGTH].strip()


def _allowed_tools(value: Any) -> List[str]:
    """``Read, Write`` / ``Read Write`` / ``[Read, Write]`` → names (advisory, D4)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value)
    parts = text.split(",") if "," in text else text.split()
    return [p.strip() for p in parts if p.strip()]


def _dynamo_safe(value: Any) -> Any:
    """Frontmatter values as types a DynamoDB put accepts (no floats, no dates).

    Walks at most :data:`MAX_FRONTMATTER_NODES` values. The parser already
    bounds what it hands over; this keeps the walk bounded for a structure that
    came from anywhere else.
    """
    budget = [MAX_FRONTMATTER_NODES]
    return _dynamo_safe_value(value, budget)


def _dynamo_safe_value(value: Any, budget: List[int]) -> Any:
    budget[0] -= 1
    if budget[0] < 0:
        raise BundleImportError(
            f"SKILL.md's frontmatter has more than {MAX_FRONTMATTER_NODES:,} values."
        )
    if isinstance(value, dict):
        return {str(k): _dynamo_safe_value(v, budget) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_safe_value(v, budget) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


# =============================================================================
# Hash and diff
# =============================================================================


def _bundle_hash(bundle: ParsedBundle) -> str:
    """What apply compares to the preview's (spec D3): everything it would write."""
    payload = {
        "description": bundle.description,
        "instructions": bundle.instructions,
        "allowedTools": bundle.allowed_tools,
        "skillMetadata": bundle.skill_metadata,
        "files": [[f.filename, f.kind, f.content_hash] for f in bundle.files],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def diff_files(bundle: ParsedBundle, target: Optional[SkillDefinition]) -> List[FileChange]:
    """One row per file: what the import does to it against ``target`` (spec §3)."""
    existing = {r.filename: r for r in (target.resources if target else [])}
    rows: List[FileChange] = []
    for f in bundle.files:
        ref = existing.get(f.filename)
        if ref is None:
            status = "added"
        elif ref.content_hash == f.content_hash and ref.kind == f.kind:
            status = "unchanged"
        else:
            status = "changed"
        rows.append(FileChange(f.path, status, f.filename, f.kind, f.size))
    imported = {f.filename for f in bundle.files}
    for name in sorted(set(existing) - imported):
        ref = existing[name]
        subdir = KIND_DIRS.get(ref.kind, "references")
        rows.append(FileChange(f"{subdir}/{name}", "removed", name, ref.kind, ref.size))
    for s in bundle.skipped:
        rows.append(FileChange(s.path, "skipped", size=s.size, reason=s.reason))
    return rows


def diff_fields(bundle: ParsedBundle, target: SkillDefinition) -> FieldChanges:
    return FieldChanges(
        description=bundle.description != target.description,
        instructions=bundle.instructions != (target.instructions or "").strip(),
        allowed_tools=bundle.allowed_tools != list(target.allowed_tools),
        skill_metadata=bundle.skill_metadata != dict(target.skill_metadata),
    )
