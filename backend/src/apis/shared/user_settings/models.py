"""Domain models for user settings."""

from pydantic import BaseModel, ConfigDict
from typing import Annotated, Literal, Optional
from pydantic import Field


class UserSettings(BaseModel):
    """User settings stored in DynamoDB."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    # User-level default for the skills/tools mode toggle. New conversations
    # start in this mode (when the admin policy allows toggling); per-session
    # choices live in SessionPreferences.agent_type.
    preferred_agent_mode: Annotated[
        Optional[Literal["skill", "chat"]], Field(alias="preferredAgentMode")
    ] = None


class UserSettingsUpdate(BaseModel):
    """Partial update payload for user settings."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    preferred_agent_mode: Annotated[
        Optional[Literal["skill", "chat"]], Field(alias="preferredAgentMode")
    ] = None
