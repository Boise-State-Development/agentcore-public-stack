"""A per-turn recorder for stages inside the agent build.

WHY THIS EXISTS
---------------
`docs/specs/turn-latency-preamble.md` decomposed the `preamble` stage into five
sub-stages and the measurement immediately named its owner — five reads of one
DynamoDB row — which three PRs then removed, taking the warm preamble from
455ms to 95ms.

`agent_build` is now the largest number left in the same log line: **~2950ms on
a cold turn**, against 1-47ms warm. Nothing says which part of it that is. The
spec's standing hypothesis was the serial MCP `tools/list` pre-flight, and that
hypothesis is *unverified* — dev loads exactly one external MCP server, and
`AgentFactory.create_agent` receives `tools` and `session_manager` already
built, so the expensive work is upstream of it in the agent's own constructor.

This module is the same move that worked for the preamble: measure first.

WHY A CONTEXTVAR, WHEN PR-2 ARGUED AGAINST IMPLICIT STATE
---------------------------------------------------------
PR-2 deliberately threaded its session snapshot as an explicit parameter and
rejected an implicit per-request memo. This does the opposite, and the
asymmetry is the point rather than an inconsistency:

- there, implicit state that went wrong meant **stale session data in
  production** — the bug CLAUDE.md names and this repo has shipped twice;
- here, implicit state that goes wrong means **a timing number is missing or
  attributed to the wrong stage**. Nothing the user sees, nothing persisted,
  nothing that reaches the model.

Against that, the explicit alternative costs a kwarg threaded through
`get_agent` -> a type registry -> three agent classes, two of which do not
accept the same constructor arguments (`VoiceAgent` already needs a special
case for `accessible_skill_ids`). That is a lot of production signature for a
measurement, and every one of those signatures is a place to get it wrong.

NOT propagated across threads, deliberately. `contextvars` do not cross into a
`ThreadPoolExecutor`, and the MCP load path explicitly crosses one. Every mark
here is taken on the calling thread, so the boundary is invisible — but a
future caller that tries to mark from inside that executor will silently record
nothing, which is why this note exists.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_recorder: contextvars.ContextVar[Optional[Callable[[str], None]]] = contextvars.ContextVar(
    "agent_build_stage_recorder", default=None
)


def set_stage_recorder(recorder: Optional[Callable[[str], None]]) -> contextvars.Token:
    """Install the recorder for the current context. Returns a reset token."""
    return _recorder.set(recorder)


def reset_stage_recorder(token: contextvars.Token) -> None:
    """Restore whatever recorder was installed before ``set_stage_recorder``."""
    try:
        _recorder.reset(token)
    except Exception:  # noqa: BLE001 - a measurement must not break a turn
        logger.debug("Stage recorder reset skipped", exc_info=True)


def mark_stage(stage: str) -> None:
    """Close the build stage that just finished.

    A no-op when nothing is installed, which is every caller outside the
    inference-api turn path — tests, the scheduled-runs Lambda, app-api. Never
    raises: the whole module is a measurement, and a measurement that can break
    the thing it measures is worse than no measurement.
    """
    try:
        recorder = _recorder.get()
        if recorder is not None:
            recorder(stage)
    except Exception:  # noqa: BLE001
        logger.debug("Build stage mark skipped", exc_info=True)
