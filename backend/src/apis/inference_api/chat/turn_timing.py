"""Wall-clock marks for the work that happens BEFORE the SSE stream opens.

WHY THIS EXISTS
---------------
Measured on dev (docs/specs/agent-state-feedback.md), a warm artifact turn spent
**3.75 seconds between the user's click and the first SSE byte**, against ~3.8s
for the entire streamed response. A cold turn spent 6.7s before the first
``agent_status`` frame.

That window is invisible from both ends. The client has nothing to show but a
generic "Thinking"; the server cannot narrate it, because FastAPI flushes
response headers when the handler returns its ``StreamingResponse`` and
``invocations`` awaits model resolution, the quota check, RAG retrieval, tool
building and ``get_agent`` *before* that return. There is no channel open.

Before restructuring anything to open that channel — the three options in the
spec all carry real risk — we need to know which stage owns the time. On a warm
turn the agent cache hits, so ``get_agent`` is probably NOT the bulk of it, and
narrating the wrong stage would be exactly the waste the CLAUDE.md
cost-effectiveness tenet exists to catch.

WHAT IT COSTS
-------------
Nothing against the model: this never touches the prompt, the conversation, or
the cacheable prefix. At runtime it is a ``perf_counter()`` call per mark and
one log line per turn.

HOW TO READ IT
--------------
One structured line per turn, on the inference-api runtime log group
(``<runtime id>-DEFAULT``). The guard on the prod account makes Logs Insights
unusable, so read it with ``filter-log-events``::

    aws logs filter-log-events --log-group-name <group> \\
        --filter-pattern turn_prelude --profile <profile> \\
        --query 'events[].message' --output text > /tmp/prelude.txt

``stages`` holds the delta in ms for each stage in the order it ran, so the
slowest one is the answer. ``totalMs`` is click-adjacent: it starts when the
handler is entered, so it excludes the app-api hop and any Runtime cold start —
compare it against the client-side gap to size what is left.

A stage name may carry a dotted prefix (``preamble.quota``) to record a
sub-stage. ``groups`` then sums every stage sharing a prefix, so the coarse
number a stage used to report survives its own decomposition — see
``emit``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TurnPrelude:
    """Records named marks across the pre-stream stages of one turn.

    Best-effort in every direction, like the status hook it exists to inform:
    a mark is two attribute reads and an append, and ``emit`` swallows
    everything. A measurement must never be able to break the turn it measures.
    """

    __slots__ = ("_t0", "_wall_t0", "_last", "_marks")

    def __init__(self) -> None:
        now = time.perf_counter()
        self._t0 = now
        self._last = now
        # TWO clocks, deliberately. `perf_counter` is monotonic and is what the
        # stage deltas must use — a clock step mid-turn would otherwise produce
        # a negative stage. But the stream coordinator measures with
        # `time.time()`, and a duration is only meaningful within one clock
        # domain, so the value handed to it is captured here in ITS domain.
        self._wall_t0 = time.time()
        self._marks: List[Tuple[str, float]] = []

    def mark(self, stage: str) -> None:
        """Close the stage that just finished and open the next one.

        ``stage`` may be dotted (``preamble.quota``) to record a sub-stage;
        ``emit`` sums the prefix back into ``groups``.
        """
        try:
            now = time.perf_counter()
            self._marks.append((stage, (now - self._last) * 1000.0))
            self._last = now
        except Exception:  # noqa: BLE001 - never break a turn to measure it
            logger.debug("Turn prelude mark skipped", exc_info=True)

    @property
    def started_at(self) -> float:
        """``time.time()`` for the moment the handler was entered.

        Wall clock, not ``perf_counter``, because the only consumer is the
        stream coordinator's turn duration and it subtracts this from a
        ``time.time()`` reading — mixing the two domains yields a meaningless
        number, not a slightly wrong one.

        It exists because the coordinator's own ``stream_start_time`` no longer
        marks the start of the turn: the agent build was deferred into the
        stream generator (PR-3) and runs before that generator is iterated, so
        measuring from it excludes the build. On dev, a turn the user waited
        7.8s for reported 2.1s.
        """
        return self._wall_t0

    @property
    def last_stage_ms(self) -> Optional[int]:
        """Duration of the stage most recently closed by ``mark``, in ms.

        Lets a caller report the stage it just finished without timing it a
        second time in a different clock.
        """
        if not self._marks:
            return None
        return int(self._marks[-1][1])

    @property
    def total_ms(self) -> int:
        return max(0, int((time.perf_counter() - self._t0) * 1000))

    def _groups(self) -> Dict[str, int]:
        """Sum the dotted sub-stages back into the stage they decompose.

        A stage that gets split into sub-stages would otherwise take its own
        history with it: the four-turn dev baseline in
        docs/specs/agent-state-feedback.md is stated in terms of a single
        ``preamble`` number, and nothing else in the log reproduces it. Summing
        the prefix keeps the coarse series comparable across the split while
        ``stages`` answers the new question.

        Undotted stages are deliberately absent — a group of one is noise, and
        the reader already has that number in ``stages``.
        """
        totals: Dict[str, int] = {}
        for name, ms in self._marks:
            prefix, dot, _ = name.partition(".")
            if not dot:
                continue
            totals[prefix] = totals.get(prefix, 0) + int(ms)
        return totals

    def emit(
        self,
        *,
        session_id: str,
        stream_kind: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log one structured line describing where the prelude spent its time.

        No user id and no message content: this is a latency record, and the
        content-free rule applies to it the same as to the cost rows.
        """
        try:
            payload: Dict[str, Any] = {
                "sessionId": session_id,
                "streamKind": stream_kind,
                "totalMs": self.total_ms,
                "stages": {name: int(ms) for name, ms in self._marks},
            }
            groups = self._groups()
            if groups:
                payload["groups"] = groups
            if extra:
                payload.update(extra)
            logger.info("turn_prelude %s", json.dumps(payload, default=str))
        except Exception:  # noqa: BLE001
            logger.debug("Turn prelude emit skipped", exc_info=True)
