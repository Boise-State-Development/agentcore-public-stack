"""Replay a transcript through the production compaction code, per arm.

Nothing here reimplements a compaction decision. Each arm drives a real
``TurnBasedSessionManager`` — ``update_after_turn`` (trigger, hysteresis,
floor-seeking cut, bounded summary), ``apply_pending_compaction`` (the
paid-when-free apply) and, on the ``restore`` pace, ``_apply_compaction`` (the
restore slice, including the cold truncation-anchor advance). Only the I/O is
stubbed: DynamoDB persistence, AgentCore LTM retrieval (unless a records
provider is supplied), EMF, and the clock the cache-TTL checks read.

Arms differ only by the ``CompactionConfig`` object handed to the manager —
the scoping doc's §2.1 finding: the policy reads no environment at call time,
so every arm runs in one process on the same transcript.

Paces model what happens between turns, because it decides which bytes the
model is holding when the question arrives:

- ``restore`` — every turn rebuilds the agent from stored history with a cold
  cache (the agent-cache-bypass cohort, and any session resumed after a pause).
  Old tool results below the truncation anchor are cut to
  ``max_tool_content_length``. A parked cut is meant to apply here too, but the
  anchor advance saves state (stamping ``updated_at``) before
  ``apply_pending_compaction`` reads the gap, so it waits for the hard ceiling —
  the missed free apply the 2026-09-25 prod readout found. The default pace.
- ``cold`` — a warm agent (live list kept), but every gap exceeds the cache TTL,
  so a parked cut applies at the next turn. No restore, so no anchor truncation.
- ``warm`` — a warm agent with every gap inside the TTL: a parked cut waits
  until the hard ceiling forces it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState
from agents.main_agent.session.compaction_policy import CompactionPolicy, estimate_message_tokens
from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager

from .corpus import Transcript, message_text

PREFIX_KEY = "harness-model|harness-agent"
PACE_GAP_SECONDS = {"restore": 600, "cold": 600, "warm": 60}
PACES = tuple(PACE_GAP_SECONDS)
SUMMARY_MODES = ("fallback", "none", "records")


@dataclass(frozen=True)
class Arm:
    name: str
    # ``None`` = no compaction at all: the full history (the control).
    config: Optional[CompactionConfig]
    description: str


def default_arms(*, summary_model_enabled: bool = False) -> Dict[str, Arm]:
    """The arms slice 1 compares. Configs are explicit objects, never ``from_env``."""
    base: Dict[str, Any] = {"summary_model_enabled": summary_model_enabled}
    return {
        "full": Arm("full", None, "No compaction: the whole history (control)."),
        "model_relative": Arm(
            "model_relative", CompactionConfig(**base),
            "Production defaults since 1.23.0: ceiling min(0.5w, 100k), floor 0.25x, hysteresis, deferred apply.",
        ),
        "legacy": Arm(
            "legacy", CompactionConfig(model_relative_enabled=False, **base),
            "Kill switch: fixed 100k threshold, keep the last protected_turns turns, no hysteresis, immediate checkpoint.",
        ),
        "floor_50": Arm(
            "floor_50", CompactionConfig(floor_ratio=0.5, **base),
            "Tuning arm: production policy with a shallower cut (floor = half the ceiling).",
        ),
    }


@dataclass
class ArmRun:
    variant: int
    arm: str
    pace: str
    summary_mode: str
    context_window: int
    overhead_tokens: int
    history: List[Dict[str, Any]]
    live_offset: int
    peak_input_tokens: int
    probe_input_tokens: int
    policy: Optional[Dict[str, Any]]
    summary: Optional[str]
    # Ledger events exactly as production queues them for the C# cost row.
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def cuts(self) -> int:
        return sum(1 for e in self.events if e["kind"] == "checkpoint")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant, "arm": self.arm, "pace": self.pace,
            "summaryMode": self.summary_mode, "contextWindow": self.context_window,
            "overheadTokens": self.overhead_tokens, "liveOffset": self.live_offset,
            "peakInputTokens": self.peak_input_tokens, "probeInputTokens": self.probe_input_tokens,
            "policy": self.policy, "summary": self.summary, "events": self.events,
            "history": self.history,
        }


def history_tokens(messages: Sequence[Dict[str, Any]]) -> int:
    """The production estimator (chars/4 per block), summed."""
    return sum(estimate_message_tokens(m) for m in messages)


class _Clock:
    """Real-time stamps, with the pace's gap inserted between turns.

    A save stamps *now*, exactly as ``_save_compaction_state`` does; the gap
    is applied once per turn by backdating the previous turn's stamp. Stamping
    every save in the past instead would hide any head-of-turn save that
    resets the gap before a later check reads it — which production does: the
    restore's truncation-anchor save runs before ``apply_pending_compaction``.
    """

    def __init__(self, gap_seconds: int) -> None:
        self.gap = timedelta(seconds=gap_seconds)

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def previous_turn_end(self) -> str:
        return (datetime.now(timezone.utc) - self.gap).isoformat()


def _make_manager(
    config: CompactionConfig,
    clock: _Clock,
    events: List[Dict[str, Any]],
    records_provider: Callable[[], List[str]],
    summary_mode: str,
) -> TurnBasedSessionManager:
    manager = object.__new__(TurnBasedSessionManager)
    manager.compaction_config = config
    manager.compaction_state = CompactionState()
    manager._current_prefix_key = None
    manager._valid_cutoff_indices = []
    manager._all_messages_for_summary = []
    manager._live_offset = 0
    manager._total_message_count_at_init = 0
    manager._last_turn_completed_at = None
    manager.region_name = "us-west-2"

    def save(state: CompactionState, record_event: bool = False) -> None:
        state.updated_at = clock.now()

    manager._save_compaction_state = save
    manager._load_compaction_state = lambda: manager.compaction_state
    manager._adopt_persisted_compaction_state = lambda: None
    manager._retrieve_session_summaries = records_provider
    if summary_mode == "none":
        manager._generate_fallback_summary = lambda messages: None
    manager._emit_compaction_metrics = lambda *a, **k: None
    manager._emit_emf = lambda *a, **k: None
    manager.record_compaction_event = lambda kind, **fields: events.append({"kind": kind, **fields})
    return manager


def simulate(
    transcript: Transcript,
    arm: Arm,
    *,
    pace: str = "restore",
    context_window: int = 200_000,
    overhead_tokens: int = 15_000,
    summary_mode: str = "fallback",
    records_for_turn: Optional[Callable[[int], List[str]]] = None,
) -> ArmRun:
    """Play ``transcript`` turn by turn under ``arm``; return the probe-time history.

    ``overhead_tokens`` stands in for the system prompt + tool specs, which
    count toward the ceiling but are not history. ``records_for_turn(t)``
    returns the LTM summary records that would exist after turn ``t`` (the
    ``records`` summary mode); otherwise production's fallback summary runs.
    """
    if pace not in PACE_GAP_SECONDS:
        raise ValueError(f"unknown pace {pace!r}; expected one of {PACES}")
    if summary_mode not in SUMMARY_MODES:
        raise ValueError(f"unknown summary mode {summary_mode!r}; expected one of {SUMMARY_MODES}")
    if summary_mode == "records" and records_for_turn is None:
        raise ValueError("summary_mode='records' needs records_for_turn")

    stored: List[Dict[str, Any]] = []
    peak = 0

    if arm.config is None:
        for turn in transcript.turns:
            stored.extend(copy.deepcopy(turn))
            peak = max(peak, overhead_tokens + history_tokens(stored))
        return ArmRun(
            variant=transcript.variant, arm=arm.name, pace=pace, summary_mode=summary_mode,
            context_window=context_window, overhead_tokens=overhead_tokens, history=stored,
            live_offset=0, peak_input_tokens=peak,
            probe_input_tokens=overhead_tokens + history_tokens(stored), policy=None, summary=None,
        )

    events: List[Dict[str, Any]] = []
    current_turn = {"t": 0}
    if summary_mode == "records":
        provider = lambda: list(records_for_turn(current_turn["t"]))  # noqa: E731
    else:
        provider = lambda: []  # noqa: E731
    clock = _Clock(PACE_GAP_SECONDS[pace])
    manager = _make_manager(arm.config, clock, events, provider, summary_mode)
    agent = SimpleNamespace(messages=[])

    def head_of_turn() -> None:
        # The pause: the previous turn's last save now lies ``gap`` seconds back.
        if manager.compaction_state.updated_at is not None:
            manager.compaction_state.updated_at = clock.previous_turn_end()
        if pace == "restore":
            agent.messages = copy.deepcopy(stored)
            manager._total_message_count_at_init = len(stored)
            manager._apply_compaction(agent)
        manager.apply_pending_compaction(agent, prefix_key=PREFIX_KEY)

    # The session manager logs every decision at INFO; the harness reports them.
    session_logger = logging.getLogger("agents.main_agent.session")
    previous_level = session_logger.level
    session_logger.setLevel(logging.WARNING)
    try:
        for t, turn in enumerate(transcript.turns):
            current_turn["t"] = t
            head_of_turn()
            stored.extend(copy.deepcopy(turn))
            agent.messages.extend(copy.deepcopy(turn))
            measured = history_tokens(agent.messages)
            input_tokens = overhead_tokens + measured
            peak = max(peak, input_tokens)
            asyncio.run(manager.update_after_turn(
                input_tokens,
                current_messages=agent.messages,
                context_window=context_window,
                history_tokens=measured,
            ))
        # The probe turn: the question arrives on whatever the head of the next
        # turn leaves in place.
        current_turn["t"] = len(transcript.turns)
        head_of_turn()
    finally:
        session_logger.setLevel(previous_level)

    state = manager.compaction_state
    return ArmRun(
        variant=transcript.variant, arm=arm.name, pace=pace, summary_mode=summary_mode,
        context_window=context_window, overhead_tokens=overhead_tokens,
        history=agent.messages, live_offset=manager._live_offset,
        peak_input_tokens=peak,
        probe_input_tokens=overhead_tokens + history_tokens(agent.messages),
        policy={**CompactionPolicy.resolve(arm.config, context_window).to_dict(), "lastCut": state.policy},
        summary=state.summary, events=events,
    )


def plant_availability(run: ArmRun, transcript: Transcript) -> List[Dict[str, Any]]:
    """Per plant: is the scored value still anywhere in the probe-time history?

    Free (no model), and an upper bound on what any model can answer: a value
    that is not in the context can only be recovered by a lucky guess.
    ``statementRetained`` says whether the turn that stated it survived the
    slice; ``inContext`` also sees values the summary carried, and misses
    values the slice kept but tool-result truncation cut.
    """
    from .score import contains_any

    text = "\n".join(message_text(m) for m in run.history)
    starts = transcript.turn_starts
    rows = []
    for plant in transcript.plants:
        rows.append({
            "variant": transcript.variant,
            "arm": run.arm,
            "plantId": plant.plant_id,
            "family": plant.family,
            "turn": plant.turn,
            "statementRetained": starts[plant.turn] >= run.live_offset,
            "inContext": contains_any(text, plant.expected),
            "inSummary": bool(run.summary) and contains_any(run.summary or "", plant.expected),
        })
    return rows
