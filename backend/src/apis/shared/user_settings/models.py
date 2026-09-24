"""Domain models for user settings."""

from pydantic import BaseModel, ConfigDict
from typing import Annotated, Optional
from pydantic import Field

# Personal instructions ride in the system prompt of every conversation the user has,
# so they are kept short: about a thousand tokens, paid (cached) on every turn.
MAX_PERSONAL_INSTRUCTIONS_CHARS = 4_000


class UserSettings(BaseModel):
    """User settings stored in DynamoDB."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    personal_instructions: Annotated[Optional[str], Field(alias="personalInstructions")] = None


class UserSettingsUpdate(BaseModel):
    """Partial update payload for user settings."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    # Blank clears them.
    personal_instructions: Annotated[
        Optional[str], Field(alias="personalInstructions", max_length=MAX_PERSONAL_INSTRUCTIONS_CHARS)
    ] = None
