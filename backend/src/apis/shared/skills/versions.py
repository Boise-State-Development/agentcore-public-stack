"""Skill version snapshots: what a pin freezes, and how it is applied (shared-projects 3.1).

The skills parallel of ``apis/shared/assistants/versions.py``. A version is a
write-once ``SKILL#{id}`` / ``VERSION#{n:08d}`` row in the skill catalog's table,
holding the fields that decide what a skill *does*: its name, description,
instructions, composition, advisory tools, frontmatter and reference files.

**Apply is an overlay, never a replacement** (as for Agent versions). A version
carries no ``status``, ``owner_id`` or ``visibility``. Those come from the live row
every time, so a pinned skill an admin disables stops running, and one its author
deletes is gone. A pin answers "which instructions", never "may this run".

**Versions are cut when something pins, not on every skill save.** A version exists
only where a binding runs it. Cutting on save would put a write (and a copy of every
reference file) on each edit an author makes, for snapshots nothing reads. A pin
reuses the latest version when the live skill still has the same content
(``content_hash``), so pinning an unchanged skill twice is one version, not two.

**Reference files are frozen.** The live bundle keys
(``skills/{id}/references/{filename}``) are overwritten in place on re-upload, so a
version that pointed at them would change under its pin. A version's manifest points
at content-addressed copies instead (``resource_store.version_blob_key``), written when
the version is cut.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from .models import SkillDefinition, SkillResourceRef

VERSION_SK_PREFIX = "VERSION#"
VERSION_NUMBER_WIDTH = 8

# The fields a version freezes. Attribute names are the Python ones.
SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "display_name",
    "description",
    "instructions",
    "compose",
    "allowed_tools",
    "skill_metadata",
    "resources",
)


def version_sk(number: int) -> str:
    """Zero-padded so the last key in the partition is the highest version."""
    return f"{VERSION_SK_PREFIX}{number:0{VERSION_NUMBER_WIDTH}d}"


def content_hash(skill: SkillDefinition) -> str:
    """A digest of what a pin would freeze, used to reuse an unchanged version.

    Resources contribute their filename, kind and content hash, not their S3 key: a
    version's key is the frozen copy and the live row's is the bundle path, and the
    two must hash the same when the bytes are the same.
    """
    payload = {
        "display_name": skill.display_name,
        "description": skill.description,
        "instructions": skill.instructions,
        "compose": list(skill.compose),
        "allowed_tools": list(skill.allowed_tools),
        "skill_metadata": skill.skill_metadata,
        "resources": [[r.filename, r.kind, r.content_hash] for r in skill.resources],
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SkillVersion(BaseModel):
    """An immutable snapshot of a skill's content, cut when a binding pins it."""

    model_config = ConfigDict(populate_by_name=True)

    skill_id: str = Field(..., alias="skillId")
    version: Optional[int] = Field(
        None, description="1-based, allocated by the repository's conditional write"
    )
    content_hash: str = Field(..., alias="contentHash")
    created_at: Optional[str] = Field(None, alias="createdAt")
    created_by: Optional[str] = Field(
        None, alias="createdBy", description="User id of whoever pinned it. Attribution only."
    )
    source_updated_at: Optional[str] = Field(
        None, alias="sourceUpdatedAt", description="The live row's updatedAt when this was cut"
    )

    display_name: str = Field(..., alias="displayName")
    description: str = ""
    instructions: str = ""
    compose: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict[str, Any] = Field(default_factory=dict, alias="skillMetadata")
    resources: List[SkillResourceRef] = Field(default_factory=list)

    def to_dynamo_item(self) -> Dict[str, Any]:
        """The row. Deliberately no ``GSI4PK``: the owner index lists skills, not versions."""
        if self.version is None:
            raise ValueError("A version must be numbered before it can be written.")
        return {
            "PK": f"SKILL#{self.skill_id}",
            "SK": version_sk(self.version),
            "skillId": self.skill_id,
            "version": self.version,
            "contentHash": self.content_hash,
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "sourceUpdatedAt": self.source_updated_at,
            "displayName": self.display_name,
            "description": self.description,
            "instructions": self.instructions,
            "compose": list(self.compose),
            "allowedTools": list(self.allowed_tools),
            "skillMetadata": dict(self.skill_metadata),
            "resources": [r.model_dump(by_alias=True) for r in self.resources],
        }

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "SkillVersion":
        return cls(
            skill_id=item.get("skillId", ""),
            version=int(item["version"]) if item.get("version") is not None else None,
            content_hash=item.get("contentHash", ""),
            created_at=item.get("createdAt"),
            created_by=item.get("createdBy"),
            source_updated_at=item.get("sourceUpdatedAt"),
            display_name=item.get("displayName", ""),
            description=item.get("description", ""),
            instructions=item.get("instructions", ""),
            compose=list(item.get("compose") or []),
            allowed_tools=list(item.get("allowedTools") or []),
            skill_metadata=dict(item.get("skillMetadata") or {}),
            resources=[
                SkillResourceRef(
                    filename=r.get("filename", ""),
                    content_hash=r.get("contentHash", ""),
                    size=int(r.get("size", 0)),
                    content_type=r.get("contentType", ""),
                    s3_key=r.get("s3Key", ""),
                    kind=r.get("kind", "reference"),
                )
                for r in (item.get("resources") or [])
            ],
        )


def snapshot_of(
    skill: SkillDefinition,
    *,
    frozen_resources: List[SkillResourceRef],
    created_at: str,
    created_by: Optional[str],
    source_updated_at: Optional[str],
) -> SkillVersion:
    """An unnumbered version of ``skill`` whose resources point at their frozen copies."""
    return SkillVersion(
        skill_id=skill.skill_id,
        content_hash=content_hash(skill),
        created_at=created_at,
        created_by=created_by,
        source_updated_at=source_updated_at,
        display_name=skill.display_name,
        description=skill.description,
        instructions=skill.instructions,
        compose=list(skill.compose),
        allowed_tools=list(skill.allowed_tools),
        skill_metadata=dict(skill.skill_metadata),
        resources=list(frozen_resources),
    )


def apply_version(live: SkillDefinition, version: SkillVersion) -> SkillDefinition:
    """The live record with the version's content overlaid. Status and ownership stay live."""
    return live.model_copy(
        update={field: getattr(version, field) for field in SNAPSHOT_FIELDS}
    )
