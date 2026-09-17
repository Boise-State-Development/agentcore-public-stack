"""
Compaction models for session context management.

These models define the state and configuration for automatic context window
compaction, which helps manage token usage in long conversations.

Thresholds are model-relative — see ``compaction_policy.py`` and
docs/specs/compaction-model-relative-thresholds.md.
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
    checkpoint: int = 0  # Absolute message index to load from (0 = load all)
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
    # Hysteresis (spec §3.3). A cut disarms the trigger; a turn at or below
    # the ceiling re-arms it. While disarmed, only the hard ceiling can force
    # another cut. Legacy rows predate the field and default to armed.
    armed: bool = True
    # Snapshot of the policy the last cut was made under (window, ceiling,
    # floor, hard ceiling, whether it was forced) — what the admin session
    # profile reads to explain a compaction after the fact.
    policy: Optional[Dict[str, Any]] = None
    # Paid-when-free scheduling (spec §3.5). A cut is computed post-turn and
    # parked here; ``apply_pending_compaction`` promotes it to ``checkpoint``
    # (and slices the live list in place) pre-call, only when the prefix
    # re-write is free or unavoidable. ``checkpoint`` above is always the
    # APPLIED one — what the restore slices at.
    pending_checkpoint: Optional[int] = None
    pending_summary: Optional[str] = None
    pending_hard_ceiling: Optional[int] = None
    pending_since: Optional[str] = None
    # model id + agent id of the last turn; a change means the cached prefix
    # is already invalid, so a pending cut can ride the same re-write.
    last_prefix_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for DynamoDB storage."""
        return {
            "checkpoint": self.checkpoint,
            "summary": self.summary,
            "lastInputTokens": self.last_input_tokens,
            "updatedAt": self.updated_at,
            "totalSummarizedTurns": self.total_summarized_turns,
            "truncationAnchor": self.truncation_anchor,
            "armed": self.armed,
            "policy": self.policy,
            "pendingCheckpoint": self.pending_checkpoint,
            "pendingSummary": self.pending_summary,
            "pendingHardCeiling": self.pending_hard_ceiling,
            "pendingSince": self.pending_since,
            "lastPrefixKey": self.last_prefix_key,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CompactionState":
        """Create from DynamoDB item dictionary."""
        if not data:
            return cls()
        checkpoint = int(data.get("checkpoint", 0))
        armed = data.get("armed", True)
        policy = data.get("policy")
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
            armed=bool(armed) if armed is not None else True,
            policy=dict(policy) if isinstance(policy, dict) else None,
            pending_checkpoint=(
                int(data["pendingCheckpoint"]) if data.get("pendingCheckpoint") is not None else None
            ),
            pending_summary=data.get("pendingSummary"),
            pending_hard_ceiling=(
                int(data["pendingHardCeiling"]) if data.get("pendingHardCeiling") is not None else None
            ),
            pending_since=data.get("pendingSince"),
            last_prefix_key=data.get("lastPrefixKey"),
        )


@dataclass
class CompactionResult:
    """
    Returned by ``TurnBasedSessionManager.update_after_turn`` when a turn
    crosses the ceiling and the checkpoint advances. Carries the
    information the frontend needs to render an inline "earlier messages
    summarized" divider in the conversation, plus the policy the cut was
    made under (additive fields on the ``compaction`` SSE payload).

    ``summarized_turns`` is the *delta* count of turns rolled into the
    summary at this compaction event (not the cumulative total across
    prior compactions), so each divider stands on its own.
    """
    previous_checkpoint: int
    new_checkpoint: int
    summarized_turns: int
    input_tokens: int
    context_window: Optional[int] = None
    ceiling: Optional[int] = None
    floor: Optional[int] = None
    hard_ceiling: Optional[int] = None
    # True when the cut ran while disarmed because input reached the hard
    # ceiling — the signal that the previous cut did not take.
    forced: bool = False
    retained_tokens_estimate: Optional[int] = None
    # True when the cut was parked as pending (applied pre-call later under
    # the paid-when-free rule) rather than promoted to the checkpoint now.
    deferred: bool = False


def _env_flag_default_on(name: str) -> bool:
    """House-style kill switch: unset/empty → on; only the literal "false" is off."""
    return os.environ.get(name, "").strip().lower() != "false"


@dataclass
class CompactionConfig:
    """
    Configuration for compaction behavior.

    Can be loaded from environment variables or passed directly.
    """
    enabled: bool = True
    # Ceiling used when the model's window is unknown (and the fixed
    # threshold when model-relative policy is switched off).
    token_threshold: int = 100_000
    protected_turns: int = 3  # Recent turns to protect from truncation
    max_tool_content_length: int = 500  # Max chars before truncating tool output
    # Bedrock prompt-cache TTL. When more than this many seconds have passed
    # since the previous turn, the cache entry has already expired, so pending
    # truncations can be applied without forcing an otherwise-avoidable
    # prefix re-write.
    cache_ttl_seconds: int = 300
    # Model-relative policy (spec §3.1). See CompactionPolicy.resolve.
    model_relative_enabled: bool = Defaults.COMPACTION_MODEL_RELATIVE_ENABLED
    ceiling_ratio: float = Defaults.COMPACTION_CEILING_RATIO
    ceiling_cap_tokens: int = Defaults.COMPACTION_CEILING_CAP_TOKENS
    floor_ratio: float = Defaults.COMPACTION_FLOOR_RATIO
    hard_ceiling_ratio: float = Defaults.COMPACTION_HARD_CEILING_RATIO
    hard_ceiling_multiplier: float = Defaults.COMPACTION_HARD_CEILING_MULTIPLIER
    # Bounded summary (spec §3.6 / spiral spec PR-2). The persisted summary is
    # held at or under this many tokens (chars/4), compressed once at cut time
    # by the cheap model, with newest-first truncation as the fallback.
    summary_token_budget: int = Defaults.COMPACTION_SUMMARY_TOKEN_BUDGET
    summary_model_enabled: bool = Defaults.COMPACTION_SUMMARY_MODEL_ENABLED
    summary_model_id: str = Defaults.COMPACTION_SUMMARY_MODEL_ID
    # Paid-when-free scheduling (spec §3.5). Only meaningful with the
    # model-relative policy on; legacy mode always applies immediately.
    deferred_apply_enabled: bool = Defaults.COMPACTION_DEFERRED_APPLY_ENABLED

    @classmethod
    def from_env(cls) -> "CompactionConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.environ.get(EnvVars.COMPACTION_ENABLED, str(Defaults.COMPACTION_ENABLED).lower()).lower() == "true",
            token_threshold=int(os.environ.get(EnvVars.COMPACTION_TOKEN_THRESHOLD, str(Defaults.COMPACTION_TOKEN_THRESHOLD))),
            protected_turns=int(os.environ.get(EnvVars.COMPACTION_PROTECTED_TURNS, str(Defaults.COMPACTION_PROTECTED_TURNS))),
            max_tool_content_length=int(os.environ.get(EnvVars.COMPACTION_MAX_TOOL_CONTENT_LENGTH, str(Defaults.COMPACTION_MAX_TOOL_CONTENT_LENGTH))),
            cache_ttl_seconds=int(os.environ.get(EnvVars.COMPACTION_CACHE_TTL_SECONDS, str(Defaults.COMPACTION_CACHE_TTL_SECONDS))),
            model_relative_enabled=_env_flag_default_on(EnvVars.COMPACTION_MODEL_RELATIVE_ENABLED),
            ceiling_ratio=float(os.environ.get(EnvVars.COMPACTION_CEILING_RATIO, str(Defaults.COMPACTION_CEILING_RATIO))),
            ceiling_cap_tokens=int(os.environ.get(EnvVars.COMPACTION_CEILING_CAP_TOKENS, str(Defaults.COMPACTION_CEILING_CAP_TOKENS))),
            floor_ratio=float(os.environ.get(EnvVars.COMPACTION_FLOOR_RATIO, str(Defaults.COMPACTION_FLOOR_RATIO))),
            hard_ceiling_ratio=float(os.environ.get(EnvVars.COMPACTION_HARD_CEILING_RATIO, str(Defaults.COMPACTION_HARD_CEILING_RATIO))),
            hard_ceiling_multiplier=float(os.environ.get(EnvVars.COMPACTION_HARD_CEILING_MULTIPLIER, str(Defaults.COMPACTION_HARD_CEILING_MULTIPLIER))),
            summary_token_budget=int(os.environ.get(EnvVars.COMPACTION_SUMMARY_TOKEN_BUDGET, str(Defaults.COMPACTION_SUMMARY_TOKEN_BUDGET))),
            summary_model_enabled=_env_flag_default_on(EnvVars.COMPACTION_SUMMARY_MODEL_ENABLED),
            summary_model_id=os.environ.get(EnvVars.COMPACTION_SUMMARY_MODEL_ID, "").strip() or Defaults.COMPACTION_SUMMARY_MODEL_ID,
            deferred_apply_enabled=_env_flag_default_on(EnvVars.COMPACTION_DEFERRED_APPLY_ENABLED),
        )
