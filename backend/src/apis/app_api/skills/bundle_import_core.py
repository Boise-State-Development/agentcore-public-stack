"""The tier-neutral half of skill bundle import (docs/specs/skill-bundle-import.md).

My Skills (``bundle_import_routes``) and the admin catalog
(``admin/skills/routes``) import a bundle the same way: read a ``.zip`` or a
public GitHub path into a :class:`ParsedBundle`, diff it against the skill it
would write, and describe that as a preview. Only who may write which skill
differs, so each tier keeps its own routes and service and shares everything
here: the wire models, the source loader and the preview builder.
"""

import posixpath
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field

from apis.shared.blocking_io import run_blocking
from apis.shared.feature_flags import skill_bundle_import_enabled
from apis.shared.skills.bundle_import import (
    MAX_UPLOAD_ARCHIVE_BYTES,
    BundleImportError,
    BundleLoad,
    ParsedBundle,
    TextDiff,
    diff_fields,
    diff_files,
    load_bundle,
    text_diff,
)
from apis.shared.skills.models import SkillDefinition, SkillImportSource
from apis.shared.skills.resource_types import is_text_content_type
from apis.shared.skills.skill_md import (
    LEVEL_WARNING,
    Diagnostic,
    as_import_diagnostic,
    validate_frontmatter,
)
from apis.shared.skills.skill_md_import import DiagnosticModel, unsafe_character_diagnostics
from apis.shared.skills.user_service import bundle_original_name, skill_id_from_frontmatter_name

from .github_source import GitHubSourceError, fetch_archive, parse_github_url


class ImportFileRow(BaseModel):
    path: str
    status: str
    filename: Optional[str] = None
    kind: Optional[str] = None
    size: int = 0
    reason: Optional[str] = None


class ImportedSkillSummary(BaseModel):
    name: str
    # The activation name the skill will have: derived from its id, so it can
    # differ from ``name`` (a suffix on collision, or a name that cannot be an id).
    stored_name: str = Field(..., alias="storedName")
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions_length: int = Field(..., alias="instructionsLength")
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")

    model_config = {"populate_by_name": True}


class ImportFieldChanges(BaseModel):
    description: bool
    instructions: bool
    allowed_tools: bool = Field(..., alias="allowedTools")
    skill_metadata: bool = Field(..., alias="skillMetadata")

    model_config = {"populate_by_name": True}


class ImportTextDiff(BaseModel):
    """A unified diff of one field; ``diff`` is empty when it did not change."""

    diff: str
    truncated: bool = False

    @classmethod
    def from_diff(cls, d: TextDiff) -> "ImportTextDiff":
        return cls(diff=d.diff, truncated=d.truncated)


class ImportPreviewResponse(BaseModel):
    source: SkillImportSource
    candidates: List[str] = Field(default_factory=list)
    skill: Optional[ImportedSkillSummary] = None
    changes: Optional[ImportFieldChanges] = None
    files: List[ImportFileRow] = Field(default_factory=list)
    bundle_hash: Optional[str] = Field(None, alias="bundleHash")
    target_updated_at: Optional[str] = Field(None, alias="targetUpdatedAt")
    # Only against a target (a re-import or refresh).
    description_diff: Optional[ImportTextDiff] = Field(None, alias="descriptionDiff")
    instructions_diff: Optional[ImportTextDiff] = Field(None, alias="instructionsDiff")
    previous_commit_sha: Optional[str] = Field(None, alias="previousCommitSha")
    diagnostics: List[DiagnosticModel] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def require_import_enabled() -> None:
    """404 while the flag is off — called in each handler, after auth ran."""
    if not skill_bundle_import_enabled():
        raise HTTPException(status_code=404, detail="Not found")


async def load_source(
    file: Optional[UploadFile], url: Optional[str], path: Optional[str]
) -> Tuple[BundleLoad, SkillImportSource]:
    """Read the source into a bundle (or candidates) and describe where it came from."""
    has_url = bool((url or "").strip())
    if (file is None) == (not has_url):
        raise HTTPException(status_code=400, detail="Send a .zip file or a GitHub URL.")

    try:
        if file is not None:
            data = await file.read(MAX_UPLOAD_ARCHIVE_BYTES + 1)
            if len(data) > MAX_UPLOAD_ARCHIVE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"The .zip is larger than {MAX_UPLOAD_ARCHIVE_BYTES // (1024 * 1024)} MiB.",
                )
            bundle_path = path
            source = {"type": "zip", "filename": posixpath.basename(file.filename or "")[:200] or None}
        else:
            location = parse_github_url(url or "")
            data = await fetch_archive(location)
            # An explicit path (the SPA's candidate pick) wins over the URL's.
            bundle_path = path if path is not None else (location.path or None)
            source = {
                "type": "github",
                "url": location.display_url,
                "owner": location.owner,
                "repo": location.repo,
                "ref": location.ref,
            }
        load = await run_blocking(load_bundle, data, path=bundle_path)
    except GitHubSourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except BundleImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    bundle = load.bundle
    return load, SkillImportSource(
        **source,
        path=bundle.root if bundle else (bundle_path or ""),
        commit_sha=load.commit_sha if source["type"] == "github" else None,
        bundle_hash=bundle.bundle_hash if bundle else "",
        imported_at=datetime.now(timezone.utc).isoformat(),
    )


def file_rows(bundle: ParsedBundle, target: Optional[SkillDefinition]) -> List[ImportFileRow]:
    return [
        ImportFileRow(
            path=c.path, status=c.status, filename=c.filename, kind=c.kind, size=c.size, reason=c.reason
        )
        for c in diff_files(bundle, target)
    ]


def require_bundle(load: BundleLoad) -> ParsedBundle:
    if load.bundle is None:
        raise HTTPException(
            status_code=400,
            detail="This archive holds several skills. Preview it and choose one.",
        )
    return load.bundle


def _stored_name_reason(name: str, target: Optional[SkillDefinition]) -> str:
    if target is not None:
        return "because a refresh keeps the skill's existing name."
    if skill_id_from_frontmatter_name(name) is not None:
        return "because a skill with that name already exists."
    return "because its name cannot be used as a skill id here."


def _preview_diagnostics(
    bundle: ParsedBundle,
    name: str,
    stored_name: str,
    target: Optional[SkillDefinition],
) -> List[Diagnostic]:
    """The parser's notes, the frontmatter rules, unsafe characters, and a renamed skill."""
    diagnostics = list(bundle.parse_diagnostics)
    # An import repairs every frontmatter error it lets through, so none reads as one.
    diagnostics += [
        as_import_diagnostic(d) for d in validate_frontmatter(bundle.frontmatter, strict=False)
    ]
    sources = [("SKILL.md", bundle.instructions)] + [
        (f.path, f.content.decode("utf-8", errors="replace"))
        for f in bundle.files
        if is_text_content_type(f.content_type)
    ]
    diagnostics += unsafe_character_diagnostics(sources)
    if stored_name != name:
        diagnostics.append(
            Diagnostic(
                "stored_name_differs",
                f"This skill will be stored as {stored_name}, not "
                f"{name or 'an unnamed skill'}, {_stored_name_reason(name, target)} "
                "The agent and an export will use the stored name.",
                level=LEVEL_WARNING,
                field="name",
            )
        )
    return diagnostics


def build_preview(
    bundle: ParsedBundle,
    source: SkillImportSource,
    target: Optional[SkillDefinition],
    *,
    stored_name: str,
) -> ImportPreviewResponse:
    """What importing ``bundle`` would write, against ``target`` when re-importing.

    ``stored_name`` is the activation name the skill will have, which each tier
    plans with its own id allocator (a target keeps its own).
    """
    changes = diff_fields(bundle, target) if target else None
    name = bundle_original_name(bundle) or bundle.name
    return ImportPreviewResponse(
        source=source,
        skill=ImportedSkillSummary(
            name=bundle.name,
            stored_name=stored_name,
            display_name=target.display_name if target else bundle.display_name,
            description=bundle.description,
            instructions_length=len(bundle.instructions),
            allowed_tools=bundle.allowed_tools,
        ),
        changes=ImportFieldChanges(
            description=changes.description,
            instructions=changes.instructions,
            allowed_tools=changes.allowed_tools,
            skill_metadata=changes.skill_metadata,
        )
        if changes
        else None,
        files=file_rows(bundle, target),
        bundle_hash=bundle.bundle_hash,
        target_updated_at=target.updated_at.isoformat() if target else None,
        description_diff=ImportTextDiff.from_diff(
            text_diff(target.description, bundle.description, label="description")
        )
        if target
        else None,
        instructions_diff=ImportTextDiff.from_diff(
            text_diff((target.instructions or "").strip(), bundle.instructions, label="SKILL.md")
        )
        if target
        else None,
        previous_commit_sha=target.import_source.commit_sha
        if target and target.import_source
        else None,
        diagnostics=[
            DiagnosticModel.from_diagnostic(d)
            for d in _preview_diagnostics(bundle, name, stored_name, target)
        ],
    )
