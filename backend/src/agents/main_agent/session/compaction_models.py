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
    # Cumulative count of turns rolled into a summary across every
    # compaction event in this session. Surfaced on session-metadata GET so
    # the frontend's end-of-conversation indicator survives a refresh.
    total_summarized_turns: int = 0
    # Absolute message index below which tool contents are truncated on
    # restore. Bedrock prompt caching requires an exact prefix match, so
    # truncation must be a pure function of persisted state — this anchor
    # only moves when the checkpoint advances (the slice already forces a
    # cache re-write) or when the prompt cache has already expired between
    # turns (the re-write is free then). It must never be derived from a
    # per-restore sliding window.
    truncation_anchor: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "checkpoint": self.checkpoint,
            "summary": self.summary,
            "lastInputTokens": self.last_input_tokens,
            "updatedAt": self.updated_at,
            "totalSummarizedTurns": self.total_summarized_turns,
            "truncationAnchor": self.truncation_anchor,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CompactionState":
        """Create from DynamoDB item dictionary."""
        if not data:
            return cls()
        checkpoint = int(data.get("checkpoint", 0))
        return cls(
            checkpoint=checkpoint,
            summary=data.get("summary"),
            last_input_tokens=int(data.get("lastInputTokens", 0)),
            updated_at=data.get("updatedAt"),
            total_summarized_turns=int(data.get("totalSummarizedTurns", 0)),
            # Legacy records predate the anchor: default it to the checkpoint
            # so nothing retained by the slice is truncated (byte-stable from
            # the first restore under the anchor design).
            truncation_anchor=int(data.get("truncationAnchor", checkpoint)),
        )


@dataclass
class CompactionResult:
    """
    Returned by ``TurnBasedSessionManager.update_after_turn`` when a turn
    crosses the token threshold and the checkpoint advances. Carries the
    information the frontend needs to render an inline "earlier messages
    summarized" divider in the conversation.

    ``summarized_turns`` is the *delta* count of turns rolled into the
    summary at this compaction event (not the cumulative total across
    prior compactions), so each divider stands on its own.
    """
    previous_checkpoint: int
    new_checkpoint: int
    summarized_turns: int
    input_tokens: int


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
    # Bedrock prompt-cache TTL. When more than this many seconds have passed
    # since the previous turn, the cache entry has already expired, so pending
    # truncations can be applied without forcing an otherwise-avoidable
    # prefix re-write.
    cache_ttl_seconds: int = 300

    @classmethod
    def from_env(cls) -> "CompactionConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.environ.get(EnvVars.COMPACTION_ENABLED, str(Defaults.COMPACTION_ENABLED).lower()).lower() == "true",
            token_threshold=int(os.environ.get(EnvVars.COMPACTION_TOKEN_THRESHOLD, str(Defaults.COMPACTION_TOKEN_THRESHOLD))),
            protected_turns=int(os.environ.get(EnvVars.COMPACTION_PROTECTED_TURNS, str(Defaults.COMPACTION_PROTECTED_TURNS))),
            max_tool_content_length=int(os.environ.get(EnvVars.COMPACTION_MAX_TOOL_CONTENT_LENGTH, str(Defaults.COMPACTION_MAX_TOOL_CONTENT_LENGTH))),
            cache_ttl_seconds=int(os.environ.get(EnvVars.COMPACTION_CACHE_TTL_SECONDS, str(Defaults.COMPACTION_CACHE_TTL_SECONDS))),
        )
