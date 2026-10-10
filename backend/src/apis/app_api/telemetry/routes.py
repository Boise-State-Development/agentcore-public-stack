"""Client-measured turn timing (docs/specs/turn-path-ttft.md §6).

The server's ``FirstTokenMs`` starts at the Runtime handler, so it cannot see
the browser, the app-api hop, Runtime routing, or a cold session start, which
is where the AgentCore Runtime V2 move and session prewarm changed the user's
wait. The SPA measures send-to-first-token itself and reports it here, once
per turn, after the first token has arrived, so reporting never delays a turn.

Written as EMF into ``AgentCoreStack/TurnLatency`` beside the server stages:

- ``ClientFirstTokenMs`` on every report;
- ``ClientFirstTurnFirstTokenMs`` or ``ClientFollowUpFirstTokenMs``, because a
  conversation's first turn starts its Runtime session and a follow-up
  usually finds it running, and one percentile over both moves whenever the
  mix does;
- for first turns, ``ClientFirstTurnWarmedFirstTokenMs`` or
  ``ClientFirstTurnColdFirstTokenMs``, which tracks what session prewarm is
  worth over time.

Metric names rather than dimensions, like every EMF caller here. The model id,
the warm's age and the turn's shape ride as log properties for Logs Insights.
No message content, user id or session id is recorded.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import client_turn_timing_enabled
from apis.shared.observability.emf import emit_emf_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

# Must match the namespace the server stages and the dashboard use.
TURN_LATENCY_NAMESPACE = "AgentCoreStack/TurnLatency"

# The proxy cuts a turn at 300 s; a "first token" later than this is a bug report.
_MAX_FIRST_TOKEN_MS = 600_000
# A person sends a few turns a minute; this only stops a script flooding the log.
_MAX_REPORTS_PER_USER_PER_MINUTE = 60


class TurnTimingReport(BaseModel):
    """One turn, as the browser saw it."""

    first_token_ms: int = Field(alias="firstTokenMs", ge=0, le=_MAX_FIRST_TOKEN_MS)
    turn_kind: Literal["first", "followup"] = Field(alias="turnKind")
    model_id: Optional[str] = Field(default=None, alias="modelId", max_length=200)
    # Milliseconds between the session prewarm and the send, or None when the
    # conversation was not warmed in this tab.
    warm_age_ms: Optional[int] = Field(default=None, alias="warmAgeMs", ge=0, le=86_400_000)
    has_agent: bool = Field(default=False, alias="hasAgent")
    has_files: bool = Field(default=False, alias="hasFiles")
    # Whether the tab was visible when the first token arrived: a background
    # tab's user was not watching, and its timers may have been throttled.
    visible: bool = True


class _ReportLimiter:
    """Per-user, in-process cap. Losing a report to it costs one sample."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: Dict[str, List[float]] = {}

    def allow(self, user_id: str, now: float) -> bool:
        with self._lock:
            recent = [t for t in self._recent.get(user_id, []) if now - t < 60.0]
            if len(recent) >= _MAX_REPORTS_PER_USER_PER_MINUTE:
                self._recent[user_id] = recent
                return False
            recent.append(now)
            self._recent[user_id] = recent
            if len(self._recent) > 10_000:
                self._recent = {u: ts for u, ts in self._recent.items() if any(now - t < 60.0 for t in ts)}
            return True

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._recent.clear()


_limiter = _ReportLimiter()


def metrics_for(report: TurnTimingReport) -> Dict[str, int]:
    """The metric names one report is recorded under."""
    value = report.first_token_ms
    metrics = {"ClientFirstTokenMs": value}
    if report.turn_kind == "first":
        metrics["ClientFirstTurnFirstTokenMs"] = value
        warmed = report.warm_age_ms is not None
        metrics["ClientFirstTurnWarmedFirstTokenMs" if warmed else "ClientFirstTurnColdFirstTokenMs"] = value
    else:
        metrics["ClientFollowUpFirstTokenMs"] = value
    return metrics


@router.post("/turn-timing", status_code=status.HTTP_204_NO_CONTENT)
async def report_turn_timing(
    report: TurnTimingReport,
    current_user: User = Depends(get_current_user_from_session),
) -> Response:
    """Record one turn's client-measured time to first token. 204, always, once admitted."""
    if not client_turn_timing_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not _limiter.allow(current_user.user_id, time.monotonic()):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    metrics = metrics_for(report)
    emit_emf_metrics(
        namespace=TURN_LATENCY_NAMESPACE,
        metrics=metrics,
        properties={
            "source": "spa",
            "turnKind": report.turn_kind,
            "modelId": report.model_id,
            "warmAgeMs": report.warm_age_ms,
            "hasAgent": report.has_agent,
            "hasFiles": report.has_files,
            "visible": report.visible,
        },
        units={name: "Milliseconds" for name in metrics},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
