"""Domain models for user settings."""

from pydantic import BaseModel, ConfigDict, field_validator
from typing import Annotated, List, Optional
from pydantic import Field

from apis.shared.voice_catalog import get_voice

# Personal instructions ride in the system prompt of every conversation the user has,
# so they are kept short: about a thousand tokens, paid (cached) on every turn.
MAX_PERSONAL_INSTRUCTIONS_CHARS = 4_000

# The sidebar's navigation entries are defined by the SPA, not here, so the
# backend checks the shape of a layout rather than enumerating the entries: a
# new nav item ships with a frontend change alone. The caps only bound the
# item, which is read on every page load.
MAX_SIDEBAR_ITEMS = 32
SIDEBAR_ITEM_ID_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"


class SidebarItemPreference(BaseModel):
    """One sidebar navigation entry: where it sits and whether it is shown.

    Order is the list's order. A hidden entry is still listed, so its place
    survives being hidden; the SPA offers it under "More".
    """

    id: Annotated[str, Field(pattern=SIDEBAR_ITEM_ID_PATTERN)]
    visible: bool


class UserSettings(BaseModel):
    """User settings stored in DynamoDB."""
    model_config = ConfigDict(populate_by_name=True)

    default_model_id: Annotated[Optional[str], Field(alias="defaultModelId")] = None
    personal_instructions: Annotated[Optional[str], Field(alias="personalInstructions")] = None
    # The Nova 2 Sonic voice the user hears in voice mode. `None` means the
    # platform default (`apis.shared.voice_catalog.DEFAULT_VOICE_ID`).
    voice_id: Annotated[Optional[str], Field(alias="voiceId")] = None
    # The user's order and visibility for the sidebar's navigation entries.
    # `None` means the SPA's default layout.
    sidebar_items: Annotated[
        Optional[List[SidebarItemPreference]], Field(alias="sidebarItems")
    ] = None


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
    # Null or an empty list clears it (back to the SPA's default layout).
    sidebar_items: Annotated[
        Optional[List[SidebarItemPreference]],
        Field(alias="sidebarItems", max_length=MAX_SIDEBAR_ITEMS),
    ] = None

    @field_validator("sidebar_items")
    @classmethod
    def _validate_sidebar_items(
        cls, value: Optional[List[SidebarItemPreference]]
    ) -> Optional[List[SidebarItemPreference]]:
        if value is None:
            return None
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("Sidebar item ids must be unique")
        return value

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
