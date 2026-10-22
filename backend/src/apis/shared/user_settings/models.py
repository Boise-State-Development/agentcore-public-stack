"""Domain models for user settings."""

from pydantic import BaseModel, ConfigDict, field_validator
from typing import Annotated, Any, List, Literal, Mapping, Optional
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


# Long-term memory mode: "on" retrieves and learns, "read_only" retrieves but
# learns nothing new, "off" does neither.
MemoryMode = Literal["on", "read_only", "off"]
_MEMORY_MODE_RANK = {"off": 0, "read_only": 1, "on": 2}


def normalize_memory_mode(value: Any) -> MemoryMode:
    """A known mode as is; anything else (missing, garbage) fails open to on."""
    return value if value in _MEMORY_MODE_RANK else "on"


def memory_mode_from(settings: Mapping[str, Any]) -> MemoryMode:
    """The memory mode of a settings dict (missing means on)."""
    return normalize_memory_mode(settings.get("memoryMode"))


def cap_memory_mode(mode: Any, cap: Any) -> MemoryMode:
    """The lower of two modes: a conversation cap can only reduce memory."""
    a, b = normalize_memory_mode(mode), normalize_memory_mode(cap)
    return a if _MEMORY_MODE_RANK[a] <= _MEMORY_MODE_RANK[b] else b


def memory_capabilities(mode: Any) -> tuple[bool, bool]:
    """(retrieval, extraction) for a mode."""
    mode = normalize_memory_mode(mode)
    return mode != "off", mode == "on"

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
    # Long-term memory mode. A missing value means on, and the setting can only
    # reduce memory (an Agent conversation is always off).
    memory_mode: Annotated[Optional[MemoryMode], Field(alias="memoryMode")] = "on"

    @field_validator("memory_mode", mode="before")
    @classmethod
    def _default_memory_mode(cls, value: Any) -> Any:
        return normalize_memory_mode(value)

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
    # "on" | "read_only" | "off"; null removes the stored value (back to on).
    memory_mode: Annotated[Optional[MemoryMode], Field(alias="memoryMode")] = None

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
