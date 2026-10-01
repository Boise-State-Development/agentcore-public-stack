"""Domain models for user settings."""

from pydantic import BaseModel, ConfigDict, field_validator
from typing import Annotated, Optional
from pydantic import Field

from apis.shared.voice_catalog import get_voice

# Personal instructions ride in the system prompt of every conversation the user has,
# so they are kept short: about a thousand tokens, paid (cached) on every turn.
MAX_PERSONAL_INSTRUCTIONS_CHARS = 4_000


class UserSettings(BaseModel):
    """User settings stored in DynamoDB."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    personal_instructions: Annotated[Optional[str], Field(alias="personalInstructions")] = None
    # The Nova 2 Sonic voice the user hears in voice mode. `None` means the
    # platform default (`apis.shared.voice_catalog.DEFAULT_VOICE_ID`).
    voice_id: Annotated[Optional[str], Field(alias="voiceId")] = None


class UserSettingsUpdate(BaseModel):
    """Partial update payload for user settings."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    # Blank clears them.
    personal_instructions: Annotated[
        Optional[str], Field(alias="personalInstructions", max_length=MAX_PERSONAL_INSTRUCTIONS_CHARS)
    ] = None
    # Blank clears it (back to the platform default). Anything else must name
    # a voice in the catalog — a stored id Bedrock rejects would break every
    # later voice session, so it is refused at the door with a 422.
    voice_id: Annotated[Optional[str], Field(alias="voiceId")] = None

    @field_validator("voice_id")
    @classmethod
    def _validate_voice_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return ""
        voice = get_voice(stripped)
        if voice is None:
            raise ValueError(f"Unknown voice '{stripped}'")
        return voice.voice_id
