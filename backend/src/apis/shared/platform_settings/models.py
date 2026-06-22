"""Models for platform-wide chat-mode settings.

Chat-mode settings control which agent mode new conversations get by
default (``skill`` routes through the SkillAgent, ``chat`` through the
plain ChatAgent) and whether users may switch between the two modes.

The defaults here must reproduce the server behavior that existed before
these settings were introduced (``DEFAULT_AGENT_TYPE = "skill"``, client
``agent_type`` overrides honored), so an environment without a stored
settings item sees no behavior change.
"""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ChatMode = Literal["skill", "chat"]

DEFAULT_CHAT_MODE: ChatMode = "skill"


class ChatModeSettings(BaseModel):
    """The stored chat-mode policy."""

    default_mode: ChatMode = DEFAULT_CHAT_MODE
    allow_mode_toggle: bool = True
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    def to_dynamo_item(self) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "defaultMode": self.default_mode,
            "allowModeToggle": self.allow_mode_toggle,
        }
        if self.updated_at is not None:
            item["updatedAt"] = self.updated_at.isoformat()
        if self.updated_by is not None:
            item["updatedBy"] = self.updated_by
        return item

    @classmethod
    def from_dynamo_item(cls, item: Dict[str, Any]) -> "ChatModeSettings":
        updated_at = item.get("updatedAt")
        return cls(
            default_mode=item.get("defaultMode", DEFAULT_CHAT_MODE),
            allow_mode_toggle=item.get("allowModeToggle", True),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
            updated_by=item.get("updatedBy"),
        )


class ChatModeSettingsUpdate(BaseModel):
    """Admin PUT body. Accepts camelCase from the SPA."""

    model_config = ConfigDict(populate_by_name=True)

    default_mode: ChatMode = Field(alias="defaultMode")
    allow_mode_toggle: bool = Field(alias="allowModeToggle")


class ChatModeSettingsResponse(BaseModel):
    """Admin-facing view, including audit fields."""

    model_config = ConfigDict(populate_by_name=True)

    default_mode: ChatMode = Field(alias="defaultMode")
    allow_mode_toggle: bool = Field(alias="allowModeToggle")
    updated_at: Optional[datetime] = Field(None, alias="updatedAt")
    updated_by: Optional[str] = Field(None, alias="updatedBy")

    @classmethod
    def from_settings(cls, settings: ChatModeSettings) -> "ChatModeSettingsResponse":
        return cls(
            default_mode=settings.default_mode,
            allow_mode_toggle=settings.allow_mode_toggle,
            updated_at=settings.updated_at,
            updated_by=settings.updated_by,
        )


class ChatSettingsPublicResponse(BaseModel):
    """User-facing view for the SPA — the policy flags plus whether the
    skills feature is enabled at all (drives the admin nav + mode toggle)."""

    model_config = ConfigDict(populate_by_name=True)

    default_mode: ChatMode = Field(alias="defaultMode")
    allow_mode_toggle: bool = Field(alias="allowModeToggle")
    skills_enabled: bool = Field(default=True, alias="skillsEnabled")

    @classmethod
    def from_settings(
        cls, settings: ChatModeSettings, *, skills_enabled: bool = True
    ) -> "ChatSettingsPublicResponse":
        return cls(
            default_mode=settings.default_mode,
            allow_mode_toggle=settings.allow_mode_toggle,
            skills_enabled=skills_enabled,
        )
