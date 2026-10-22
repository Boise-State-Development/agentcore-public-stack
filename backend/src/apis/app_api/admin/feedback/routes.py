"""Admin feedback routes — the eval-sampling queue (spec §11 PR-4).

    GET  /admin/feedback/evaluations         recent down-thumbs + any verdict
    POST /admin/feedback/evaluations/run     judge up to `limit` of them, offline

Scope: ``admin.costs`` — the judge spends tokens and the verdicts sit beside
the cost rows. The run is a background task (the SDK waits on span
ingestion; minutes, not milliseconds) and 404s while
``FEEDBACK_EVAL_SAMPLING_ENABLED`` is off, per the flag's docstring.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from apis.shared.auth import User, require_admin_scope
from apis.shared.feature_flags import feedback_eval_sampling_enabled
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.storage.dynamodb_storage import DynamoDBStorage

from .fleet import build_fleet_report

from .models import (
    DownThumbQueueItem,
    DownThumbQueueResponse,
    FleetFeedbackResponse,
    SamplingRunResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["admin-feedback"])
require_feedback_admin = require_admin_scope("admin.costs")


def get_storage() -> DynamoDBStorage:
    return DynamoDBStorage()


def get_judge():
    """The AgentCore Evaluations adapter. A dependency so tests inject a fake."""
    from apis.shared.feedback_eval.sampler import AgentCoreJudge

    return AgentCoreJudge()


#: Hard caps on one fleet query. The window is a GSI range read, but the
#: join is one query per session, so an unbounded month would fan out
#: without limit. Exceeding either is reported as `coverage.truncated` /
#: `sessionsOmitted` rather than silently under-counted.
MAX_THUMBS_PER_WINDOW = 2000
MAX_SESSIONS_JOINED = 300
#: Concurrent per-session cost-row reads.
SESSION_FETCH_CONCURRENCY = 16


@router.get("/fleet", response_model=FleetFeedbackResponse, response_model_by_alias=True)
async def get_fleet_feedback(
    days: int = Query(30, ge=1, le=90, description="Trailing window in days"),
    current_user: User = Depends(require_feedback_admin),
    storage: DynamoDBStorage = Depends(get_storage),
):
    """Down-thumb rate by config arm across the fleet — model, agent switch,
    distance from a compaction cut, document turn class.

    Content-free end to end: the thumbs arrive through the projected reader
    (no user id), the cost rows through the anatomy's own projection, and
    nothing but counts leaves. Per spec §9 every arm carries its ``n``, arms
    under the floor report no rate at all, and there is no fleet-wide
    "quality score" field.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    window = {"start": start.isoformat(), "end": end.isoformat(), "days": days}

    try:
        thumbs = await storage.get_feedback_in_window(
            start=window["start"], end=window["end"], limit=MAX_THUMBS_PER_WINDOW,
        )
    except Exception:
        logger.error("Error reading the feedback window", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read feedback window")

    truncated = len(thumbs) >= MAX_THUMBS_PER_WINDOW
    session_ids: List[str] = []
    for row in thumbs:
        sid = str(row.get("sessionId") or "")
        if sid and sid not in session_ids:
            session_ids.append(sid)
    omitted = max(0, len(session_ids) - MAX_SESSIONS_JOINED)
    session_ids = session_ids[:MAX_SESSIONS_JOINED]

    records_by_session = await _load_session_records(storage, session_ids)
    report = build_fleet_report(
        thumbs,
        records_by_session,
        window=window,
        truncated=truncated,
        sessions_omitted=omitted,
    )
    logger.info(
        "Fleet feedback: %d thumbs over %sd, %d sessions joined (%d omitted)",
        report["totals"]["thumbs"], scrub_log(days), len(records_by_session), omitted,
    )
    return FleetFeedbackResponse(**report)


async def _load_session_records(
    storage: DynamoDBStorage, session_ids: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Cost rows for each session, in bounded-concurrency batches. A session
    that fails to read is dropped, not fatal: its thumbs then count toward
    `coverage.unjoined`, which is the honest result."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for offset in range(0, len(session_ids), SESSION_FETCH_CONCURRENCY):
        batch = session_ids[offset:offset + SESSION_FETCH_CONCURRENCY]
        results = await asyncio.gather(
            *(storage.get_session_cost_records(sid) for sid in batch),
            return_exceptions=True,
        )
        for sid, result in zip(batch, results):
            if isinstance(result, Exception):
                logger.debug("Fleet join: session rows unavailable")
                continue
            out[sid] = list(result or [])
    return out


@router.get("/evaluations", response_model=DownThumbQueueResponse, response_model_by_alias=True)
async def list_down_thumb_queue(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_feedback_admin),
    storage: DynamoDBStorage = Depends(get_storage),
):
    """Recent down-thumbs across the fleet, newest first, with the verdict
    where one exists. Content-free by projection."""
    try:
        rows = await storage.get_recent_down_thumbs(limit=limit)
    except Exception:
        logger.error("Error listing the down-thumb queue", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list feedback queue")
    items = []
    for row in rows:
        try:
            items.append(DownThumbQueueItem(**row))
        except Exception:  # noqa: BLE001 - a malformed row is skipped, not fatal
            continue
    return DownThumbQueueResponse(
        items=items,
        pending=sum(1 for i in items if not i.evaluated_at),
        sampling_enabled=feedback_eval_sampling_enabled(),
    )


@router.post("/evaluations/run", response_model=SamplingRunResponse, status_code=202, response_model_by_alias=True)
async def run_eval_sampling(
    background: BackgroundTasks,
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(require_feedback_admin),
    storage: DynamoDBStorage = Depends(get_storage),
    judge=Depends(get_judge),
):
    """Judge up to ``limit`` recent, not-yet-judged down-thumbs in the
    background. 202 immediately; results appear on the queue list and the
    session profiles as they land."""
    if not feedback_eval_sampling_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    def cost_row_lookup(session_id: str, message_id: int) -> Optional[Dict[str, Any]]:
        # Sync lookup for tool-failure corroboration: the call's C# row.
        try:
            from boto3.dynamodb.conditions import Key

            response = storage.sessions_metadata_table.query(
                IndexName="SessionLookupIndex",
                KeyConditionExpression=Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with("C#"),
            )
            for item in response.get("Items", []):
                try:
                    if int(item.get("messageId")) == message_id:
                        return storage._convert_decimal_to_float(item)
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001 - corroboration is best-effort
            return None
        return None

    async def task() -> None:
        from apis.shared.feedback_eval.sampler import run_sampling_batch

        try:
            await run_sampling_batch(
                storage.sessions_metadata_table, judge, limit=limit, cost_row_lookup=cost_row_lookup,
            )
        except Exception:  # noqa: BLE001 - background; nothing to return to
            logger.error("eval sampling batch failed", exc_info=True)

    background.add_task(task)
    logger.info("Admin queued an eval sampling batch (limit=%s)", scrub_log(limit))
    return SamplingRunResponse(
        accepted=True,
        limit=limit,
        note="Judging runs in the background; the SDK waits for span ingestion, so allow a few minutes.",
    )
