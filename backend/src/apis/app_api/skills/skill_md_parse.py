"""Shared body of the two "Import SKILL.md" parse routes (user and admin).

Each route authenticates on its own terms, then calls :func:`parse_uploaded_skill_md`.
It reads at most one byte past the cap, so an oversized upload is refused without
being read in full, and parses in a worker thread. Nothing is written.
"""

from fastapi import HTTPException, UploadFile

from apis.shared.blocking_io import run_blocking
from apis.shared.skills.skill_md_import import (
    MAX_SKILL_MD_UPLOAD_BYTES,
    SkillMdParseResponse,
    SkillMdUploadError,
    read_skill_md_upload,
)


async def parse_uploaded_skill_md(file: UploadFile) -> SkillMdParseResponse:
    """Parse one uploaded ``SKILL.md`` into form fields and diagnostics (400 if unreadable)."""
    raw = await file.read(MAX_SKILL_MD_UPLOAD_BYTES + 1)
    try:
        parsed = await run_blocking(read_skill_md_upload, raw)
    except SkillMdUploadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SkillMdParseResponse.from_import(parsed)
