"""Admin routes for one skill's scripts and the admin switch (skill-code-sandbox §8.10).

Governed by the delegable ``admin.skill_scripts`` scope, not ``admin.skills``:
that scope manages the admin catalog and reads user-authored skills as 404 by
design, while this page reads anyone's scripts, so it is granted on purpose.
The package boundary is the permission boundary
(``tests/architecture/test_admin_scope_coverage.py``).

- ``GET /admin/skill-scripts/{skill_id}``: the author and provenance, the
  scripts with read-only source, each reviewed version and where it was
  reviewed, and the switch.
- ``PUT /admin/skill-scripts/{skill_id}/switch`` ``{off, hashes?, reason}``:
  turns the scripts off everywhere, for every version or the listed hashes, or
  back on. Written ``by: admin``, so only an admin can turn it back on; it also
  undoes an author's switch. Audited as ``skill.scripts_switched``.

The Runtime reads the switch row on every script call and never caches it
(``script_trust.read_trust_rows``), so a switch takes effect on the next call,
with no new conversation.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from apis.shared.auth import User, require_admin_scope
from apis.shared.skills import script_switch_admin as admin_switch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skill-scripts", tags=["admin-skill-scripts"])

require_skill_scripts_admin = require_admin_scope("admin.skill_scripts")

_SKILL_ID = Path(..., min_length=1, max_length=128)


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ScriptFileOut(_Camel):
    path: str
    content_hash: str
    size: int
    source: Optional[str] = None
    truncated: bool = False
    unreadable: bool = False


class ReviewPlaceOut(_Camel):
    scope_type: str
    scope_id: str
    scope_kind: str
    scope_name: Optional[str] = None
    scope_missing: bool = False
    reviewed_by: str
    reviewer_name: Optional[str] = None
    reviewed_at: Optional[str] = None
    method: str
    state: str
    version: Optional[int] = None


class ReviewedVersionOut(_Camel):
    scripts_hash: str
    is_current: bool
    switched_off: bool
    scripts: Dict[str, str]
    reviews: List[ReviewPlaceOut]


class SwitchOut(_Camel):
    off: bool
    by: Optional[str] = None
    by_user_id: Optional[str] = None
    by_name: Optional[str] = None
    at: Optional[str] = None
    reason: Optional[str] = None
    hashes: List[str] = Field(default_factory=list)


class SkillScriptsDetail(_Camel):
    skill_id: str
    display_name: str
    status: str
    owner_id: str
    author_name: Optional[str] = None
    admin_catalog: bool
    provenance: Optional[Dict[str, Any]] = None
    scripts_hash: Optional[str] = None
    current_switched_off: bool
    scripts: List[ScriptFileOut]
    versions: List[ReviewedVersionOut]
    switch: SwitchOut


class AdminSwitchRequest(_Camel):
    off: bool
    #: Only these script-set hashes are off; empty or absent means every version.
    hashes: Optional[List[str]] = Field(None, max_length=admin_switch.MAX_SWITCH_HASHES)
    reason: str = Field(..., min_length=1, max_length=admin_switch.REASON_MAX_CHARS)


def _detail_out(detail: admin_switch.ScriptDetail) -> SkillScriptsDetail:
    return SkillScriptsDetail.model_validate(asdict(detail))


async def _require_detail(skill_id: str) -> admin_switch.ScriptDetail:
    detail = await admin_switch.load_script_detail(skill_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    return detail


@router.get("/{skill_id}", response_model=SkillScriptsDetail, response_model_by_alias=True)
async def get_skill_scripts(
    skill_id: str = _SKILL_ID,
    admin: User = Depends(require_skill_scripts_admin),
) -> SkillScriptsDetail:
    """One skill's scripts, where each version is reviewed, and the switch."""
    return _detail_out(await _require_detail(skill_id))


@router.put("/{skill_id}/switch", response_model=SkillScriptsDetail, response_model_by_alias=True)
async def set_skill_scripts_switch(
    body: AdminSwitchRequest,
    skill_id: str = _SKILL_ID,
    admin: User = Depends(require_skill_scripts_admin),
) -> SkillScriptsDetail:
    """Turn a skill's scripts off everywhere (all versions or ``hashes``), or back on."""
    await _require_detail(skill_id)
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Give a reason for the switch.")
    try:
        await admin_switch.set_admin_switch(
            skill_id, admin, off=body.off, hashes=body.hashes, reason=body.reason
        )
    except admin_switch.InvalidSwitchError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info(
        "admin skill scripts switch",
        extra={"event": "skill_scripts_switched", "skill_id": skill_id, "off": body.off},
    )
    return _detail_out(await _require_detail(skill_id))
