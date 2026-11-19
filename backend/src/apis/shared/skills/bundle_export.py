"""Export a skill as an agentskills.io bundle in a zip archive.

The S3 prefix a skill's files live under is storage, not a conformant bundle:
its directory is the ``skill_id``, and the specification wants the directory
named after ``name`` (see :mod:`apis.shared.skills.bundle`). An export is the
conformant artifact:

    {slug}/SKILL.md
    {slug}/references/{file}
    {slug}/scripts/{file}
    {slug}/assets/{file}

where ``slug = slugify_skill_name(skill_id)``, the same string ``generate_skill_md``
writes as ``name``, so the frontmatter's ``name`` always equals its directory.

``SKILL.md`` is rendered from the row by :func:`generate_skill_md`, never read
back from the S3 projection, which can lag the row or predate the current
projection rules. Files are exported at the flat names the manifest stores.

The archive is deterministic: entries in sorted order, one fixed timestamp,
fixed permissions and fixed compression, so an unchanged skill exports to the
same bytes every time. A file the manifest names that cannot be read fails the
whole export (:class:`SkillExportFileError`); there is never a partial zip.

:func:`build_skill_zip` is pure apart from the ``read_bytes`` callable it is
given, so app-api runs it in a worker thread and tests drive it without AWS.
Nothing here decides access: callers resolve an owned skill or a catalog skill
first. Nothing here reaches a prompt.
"""

from __future__ import annotations

import io
import zipfile
from typing import Callable, Dict, Optional

from apis.shared.files.content_disposition import build_content_disposition
from apis.shared.skills.bundle import KIND_DIRS, generate_skill_md, slugify_skill_name
from apis.shared.skills.models import SkillDefinition
from apis.shared.skills.repository import SkillCatalogRepository
from apis.shared.skills.resource_types import RESOURCE_SECURITY_HEADERS
from apis.shared.skills.versions import apply_version

# The earliest time a zip header can hold. Any fixed value works; this one says
# plainly that it is not a real modification time.
_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
# Unix, so the permission bits below are what an unzip tool applies.
_CREATE_SYSTEM_UNIX = 3
_FILE_MODE = 0o644
_SCRIPT_MODE = 0o755
_REGULAR_FILE = 0o100000
_COMPRESS_LEVEL = 6

ReadBytes = Callable[[str], bytes]


class SkillExportFileError(RuntimeError):
    """A file the manifest names could not be read; the export is refused (409)."""

    def __init__(self, filename: str):
        self.filename = filename
        super().__init__(
            f"The skill's file '{filename}' could not be read, so the skill was not "
            "exported. Upload the file again or remove it from the skill, then retry."
        )


class SkillExportVersionNotFoundError(LookupError):
    """The requested version of the skill does not exist (404)."""


def export_slug(skill_id: str) -> str:
    """The bundle's directory and ``name``, and the archive's file stem."""
    return slugify_skill_name(skill_id)


def export_filename(skill_id: str) -> str:
    return f"{export_slug(skill_id)}.zip"


def export_response_headers(skill_id: str) -> Dict[str, str]:
    """``attachment; filename="{slug}.zip"`` plus the skill-resource hardening (``nosniff``)."""
    return {
        "Content-Disposition": build_content_disposition("attachment", export_filename(skill_id)),
        **RESOURCE_SECURITY_HEADERS,
    }


def _entry(path: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_FIXED_DATE_TIME)
    info.create_system = _CREATE_SYSTEM_UNIX
    mode = _SCRIPT_MODE if executable else _FILE_MODE
    info.external_attr = (_REGULAR_FILE | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_skill_zip(record: SkillDefinition, read_bytes: ReadBytes) -> bytes:
    """The skill as a zip: ``{slug}/SKILL.md`` plus every file its manifest names.

    ``record`` is the row to export, with a version already overlaid when one was
    asked for (:func:`load_export_record`). ``read_bytes`` maps a manifest
    entry's ``s3_key`` to its bytes; it is the only I/O.

    Raises:
        SkillExportFileError: ``read_bytes`` failed for one of the files. The
            first failing file, in archive order, is named.
    """
    slug = export_slug(record.skill_id)
    entries: list[tuple[str, bytes, bool]] = [
        (
            f"{slug}/SKILL.md",
            generate_skill_md(
                skill_id=record.skill_id,
                description=record.description,
                instructions=record.instructions or "",
                allowed_tools=list(record.allowed_tools),
                skill_metadata=dict(record.skill_metadata),
            ).encode("utf-8"),
            False,
        )
    ]
    for ref in sorted(
        record.resources, key=lambda r: (KIND_DIRS.get(r.kind, "references"), r.filename)
    ):
        subdir = KIND_DIRS.get(ref.kind, "references")
        try:
            content = read_bytes(ref.s3_key)
        except Exception as exc:  # noqa: BLE001 - any failed read refuses the export
            raise SkillExportFileError(ref.filename) from exc
        entries.append((f"{slug}/{subdir}/{ref.filename}", content, ref.kind == "script"))

    entries.sort(key=lambda e: e[0])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content, executable in entries:
            archive.writestr(
                _entry(path, executable=executable),
                content,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=_COMPRESS_LEVEL,
            )
    return buffer.getvalue()


async def load_export_record(
    live: SkillDefinition,
    version: Optional[int],
    repository: SkillCatalogRepository,
) -> SkillDefinition:
    """The record an export of ``live`` builds from: the live row, or a frozen version.

    A version is overlaid on the live row (:func:`apply_version`), so its
    instructions, metadata and manifest are the frozen ones and its files are
    read from their content-addressed copies.

    Raises:
        SkillExportVersionNotFoundError: ``version`` names no version of the skill.
    """
    if version is None:
        return live
    found = await repository.batch_get_skill_versions([(live.skill_id, version)])
    frozen = found.get((live.skill_id, version))
    if frozen is None:
        raise SkillExportVersionNotFoundError(
            f"Version {version} of skill '{live.skill_id}' not found"
        )
    return apply_version(live, frozen)
