"""
Compaction models for session context management.

These models define the state and configuration for automatic context window
compaction, which helps manage token usage in long conversations.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
import os

from agents.main_agent.config.constants import EnvVars, Defaults


@dataclass
class CompactionState:
    """
    Compaction state stored in DynamoDB session metadata.

    Stored as a nested attribute within the session record rather than
    a separate DynamoDB item. This simplifies storage and ensures atomic
    updates with session data.
    """
    checkpoint: int = 0  # Message index to load from (0 = load all)
    summary: Optional[str] = None  # Pre-computed summary for skipped messages
    last_input_tokens: int = 0  # Input tokens from last turn
    updated_at: Optional[str] = None  # ISO timestamp of last update

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "checkpoint": self.checkpoint,
            "summary": self.summary,
            "lastInputTokens": self.last_input_tokens,
            "updatedAt": self.updated_at
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CompactionState":
        """Create from DynamoDB item dictionary."""
        if not data:
            return cls()
        return cls(
            checkpoint=int(data.get("checkpoint", 0)),
            summary=data.get("summary"),
            last_input_tokens=int(data.get("lastInputTokens", 0)),
            updated_at=data.get("updatedAt")
        )


@dataclass
class CompactionConfig:
    """
    Configuration for compaction behavior.

    Can be loaded from environment variables or passed directly.
    """
    enabled: bool = True
    token_threshold: int = 100_000  # Trigger checkpoint when exceeded
    protected_turns: int = 3  # Recent turns to protect from truncation
    max_tool_content_length: int = 500  # Max chars before truncating tool output

    @classmethod
    def from_env(cls) -> "CompactionConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.environ.get(EnvVars.COMPACTION_ENABLED, str(Defaults.COMPACTION_ENABLED).lower()).lower() == "true",
            token_threshold=int(os.environ.get(EnvVars.COMPACTION_TOKEN_THRESHOLD, str(Defaults.COMPACTION_TOKEN_THRESHOLD))),
            protected_turns=int(os.environ.get(EnvVars.COMPACTION_PROTECTED_TURNS, str(Defaults.COMPACTION_PROTECTED_TURNS))),
            max_tool_content_length=int(os.environ.get(EnvVars.COMPACTION_MAX_TOOL_CONTENT_LENGTH, str(Defaults.COMPACTION_MAX_TOOL_CONTENT_LENGTH))),
        )
