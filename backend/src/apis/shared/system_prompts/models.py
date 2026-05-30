"""Models for admin-managed custom system prompts.

Admins create prompt catalog entries; users opt in per conversation via
``selected_prompt_id`` in ``SessionPreferences``. The prompt text is never
exposed to users — only the name and description are returned on the
user-facing list endpoint.

Storage uses a single table with PK ``PROMPT#<uuid>``, SK ``METADATA``.
No GSI needed — the catalog is small enough for a full Scan.

Wire format is snake_case (no field aliases) to match the ``user_menu_links``
convention. The frontend admin page consumes the response shape directly.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MAX_PROMPT_TEXT_LENGTH = 8_000

PromptStatus = Literal["enabled", "disabled"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


@dataclass
class SystemPrompt:
    """Admin-managed system prompt stored in DynamoDB."""

    prompt_id: str
    name: str
    description: str
    prompt_text: str
    status: PromptStatus
    created_at: str
    updated_at: str
    created_by: Optional[str] = None

    def to_dynamo_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "PK": f"PROMPT#{self.prompt_id}",
            "SK": "METADATA",
            "promptId": self.prompt_id,
            "name": self.name,
            "description": self.description,
            "promptText": self.prompt_text,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.created_by:
            item["createdBy"] = self.created_by
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "SystemPrompt":
        try:
            created_at = item["createdAt"]
            updated_at = item["updatedAt"]
        except KeyError as e:
            raise ValueError(
                f"System prompt item {item.get('PK', '?')} is missing required "
                f"timestamp field: {e.args[0]}"
            ) from e
        status = item.get("status", "enabled")
        if status not in ("enabled", "disabled"):
            # Defensive: an unknown status should never reach the agent.
            # Treat anything we don't recognize as disabled.
            status = "disabled"
        return cls(
            prompt_id=item["promptId"],
            name=item["name"],
            description=item["description"],
            prompt_text=item["promptText"],
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            created_by=item.get("createdBy"),
        )


# =============================================================================
# Pydantic request/response models
# =============================================================================


class SystemPromptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1, max_length=512)
    prompt_text: str = Field(..., min_length=1, max_length=MAX_PROMPT_TEXT_LENGTH)
    status: PromptStatus = "enabled"


class SystemPromptUpdate(BaseModel):
    """Partial update — all fields optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, min_length=1, max_length=512)
    prompt_text: Optional[str] = Field(None, min_length=1, max_length=MAX_PROMPT_TEXT_LENGTH)
    status: Optional[PromptStatus] = None


class SystemPromptAdminResponse(BaseModel):
    """Full admin response — includes prompt_text. Wire format is snake_case."""

    prompt_id: str
    name: str
    description: str
    prompt_text: str
    status: PromptStatus
    created_at: str
    updated_at: str
    created_by: Optional[str] = None

    @classmethod
    def from_prompt(cls, prompt: SystemPrompt) -> "SystemPromptAdminResponse":
        return cls(
            prompt_id=prompt.prompt_id,
            name=prompt.name,
            description=prompt.description,
            prompt_text=prompt.prompt_text,
            status=prompt.status,
            created_at=prompt.created_at,
            updated_at=prompt.updated_at,
            created_by=prompt.created_by,
        )


class SystemPromptUserResponse(BaseModel):
    """User-facing response — name and description only, no prompt_text."""

    prompt_id: str
    name: str
    description: str

    @classmethod
    def from_prompt(cls, prompt: SystemPrompt) -> "SystemPromptUserResponse":
        return cls(
            prompt_id=prompt.prompt_id,
            name=prompt.name,
            description=prompt.description,
        )


class SystemPromptAdminListResponse(BaseModel):
    prompts: List[SystemPromptAdminResponse]
    total: int


class SystemPromptUserListResponse(BaseModel):
    prompts: List[SystemPromptUserResponse]
    total: int
