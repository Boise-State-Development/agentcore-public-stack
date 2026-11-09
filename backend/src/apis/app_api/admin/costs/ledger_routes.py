"""Release-ledger admin API (docs/specs/release-ledger.md §6.1).

    GET /admin/costs/ledger/versions   the version/build timeline, totals and KPIs
    GET /admin/costs/ledger/series     daily points per version/model/tool/agent/project
    GET /admin/costs/ledger/tools      the tools table
    GET /admin/costs/ledger/agents     the agents table
    GET /admin/costs/ledger/projects   the projects table
    GET /admin/costs/ledger/quality    per-version quality panel
    GET /admin/costs/ledger/readout    candidate vs baseline (§6.3)
    GET /admin/costs/ledger/evals      LEDGER#EVAL rows for the Evaluations page (eval-harness §16)
    GET /admin/costs/ledger/export     NDJSON of the raw ledger rows

Scope ``admin.costs``, read-only. **404 while ``RELEASE_LEDGER_ENABLED`` is
off, with auth checked first**: the guard chains through the cookie-session
scope dependency, so an unauthenticated caller gets 401 and an admin without
``admin.costs`` gets 403 whichever way the flag is set.

Reads only ``LEDGER#`` rows (plus ``ROLLUP#DAILY`` for active users, the
feedback fleet partitions for the thumbs arm, and ``SRCV#`` rows for
provenance), never scans, and computes KPIs only through
``apis.shared.costs.ledger_kpis``. Nothing here is on the turn path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from apis.shared.auth import User, require_admin_scope
from apis.shared.costs import ledger_kpis, ledger_latency, ledger_provenance, ledger_readout
from apis.shared.costs.ledger_reader import (
    FAMILY_LABELS,
    TOOL_FIELDS_NOT_MEASURED,
    LedgerStore,
    Unit,
    export_row,
    group_rows,
    group_table,
    resolve_range,
    series as build_series,
    today_utc,
    tool_table,
    unit_of,
    version_bands,
    version_timeline,
)
from apis.shared.feature_flags import release_ledger_enabled
from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNASSESSED,
    STATE_UNVERIFIED,
)
from apis.shared.storage.dynamodb_storage import DynamoDBStorage

from .ledger_models import (
    LedgerEvalsResponse,
    LedgerGroupsResponse,
    LedgerQualityResponse,
    LedgerReadout,
    LedgerSeriesResponse,
    LedgerToolsResponse,
    LedgerVersionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs/ledger", tags=["admin-costs-ledger"])

require_ledger_costs_admin = require_admin_scope("admin.costs")

#: How far back the readout looks for a candidate and its baseline.
READOUT_LOOKBACK_DAYS = 180
#: Caps on the thumbs join, as on `/admin/feedback/fleet`.
MAX_THUMBS = 2000
MAX_THUMB_SESSIONS = 300


async def require_ledger_admin(user: User = Depends(require_ledger_costs_admin)) -> User:
    """``admin.costs``, then the flag: auth fails first, so the 404 never hides a 401/403."""
    if not release_ledger_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


def get_ledger_store() -> LedgerStore:
    return LedgerStore()


def get_storage() -> DynamoDBStorage:
    return DynamoDBStorage()


def _range(from_date: Optional[str], to_date: Optional[str]) -> Tuple[str, str]:
    try:
        return resolve_range(from_date, to_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _unit(text: Optional[str]) -> Optional[Unit]:
    if not text:
        return None
    try:
        return Unit.parse(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _read(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        logger.error("Release ledger read failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read the release ledger")


_DATE = r"^\d{4}-\d{2}-\d{2}$"
FromQ = Query(None, alias="from", pattern=_DATE, description="First date, YYYY-MM-DD (UTC)")
ToQ = Query(None, alias="to", pattern=_DATE, description="Last date, inclusive")
UnitQ = Query("version", description="version: every build of an appVersion together; build: each build apart")


# ---------------------------------------------------------------------------
# Joins shared by quality and readout
# ---------------------------------------------------------------------------


async def _thumbs(storage: DynamoDBStorage, from_date: str, to_date: str) -> Dict[str, Any]:
    """Thumbs keyed by the build and date of the cost row each landed on."""
    from apis.app_api.admin.feedback.fleet import load_session_records, thumbs_by_build_day

    try:
        thumbs = await storage.get_feedback_in_window(
            start=f"{from_date}T00:00:00", end=f"{to_date}T￿", limit=MAX_THUMBS,
        )
    except Exception:
        logger.warning("Release ledger: feedback window unreadable", exc_info=True)
        return {"byKey": {}, "coverage": {"available": False}}
    session_ids: List[str] = []
    for row in thumbs:
        sid = str(row.get("sessionId") or "")
        if sid and sid not in session_ids:
            session_ids.append(sid)
    omitted = max(0, len(session_ids) - MAX_THUMB_SESSIONS)
    records = await load_session_records(storage, session_ids[:MAX_THUMB_SESSIONS])
    by_key = thumbs_by_build_day(thumbs, records)
    joined = sum(c["up"] + c["down"] for c in by_key.values())
    return {
        "byKey": by_key,
        "coverage": {
            "available": True,
            "thumbs": len(thumbs),
            "joined": joined,
            "truncated": len(thumbs) >= MAX_THUMBS,
            "sessionsJoined": len(records),
            "sessionsOmitted": omitted,
        },
    }


async def _provenance(storage: DynamoDBStorage, from_date: str, to_date: str,
                      tool_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    from apis.app_api.admin.feedback.fleet import build_day_key

    return await ledger_provenance.provenance_by_unit(
        storage, from_date=from_date, to_date=to_date, tool_rows=tool_rows, key_of=build_day_key,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/versions", response_model=LedgerVersionsResponse, response_model_by_alias=True)
async def get_ledger_versions(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """Every version and build with rows in the range, oldest first, with deploy markers and KPIs."""
    start, end = _range(from_date, to_date)
    rows = await _read(store.version_rows, start, end)
    markers = await _read(store.deploy_markers)
    backfills = await _read(store.backfill_markers)
    versions, builds = version_timeline(rows, markers)
    return LedgerVersionsResponse.model_validate({
        "from": start, "to": end, "versions": versions, "builds": builds,
        "backfills": [export_row(m) for m in backfills],
    })


def _family_rows(store: LedgerStore, by: str, start: str, end: str) -> List[Dict[str, Any]]:
    return store.version_rows(start, end) if by == "version" else store.family_rows(by.upper(), start, end)


@router.get("/series", response_model=LedgerSeriesResponse, response_model_by_alias=True)
async def get_ledger_series(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    by: Literal["version", "model", "tool", "agent", "project"] = Query("version"),
    unit: Literal["version", "build"] = UnitQ,
    limit: int = Query(12, ge=1, le=50, description="Series returned, largest first"),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """Daily points per key for a chart, and the version bands to shade behind them."""
    start, end = _range(from_date, to_date)
    by_build = unit == "build"
    version_rows = await _read(store.version_rows, start, end)
    rows = version_rows if by == "version" else await _read(_family_rows, store, by, start, end)
    points, omitted = build_series(rows, by, by_build=by_build, limit=limit)
    return LedgerSeriesResponse.model_validate({
        "from": start, "to": end, "by": by, "unit": unit, "series": points,
        "bands": version_bands(version_rows, by_build), "omittedKeys": omitted,
    })


def _filter(rows: Sequence[Dict[str, Any]], unit: Optional[Unit]) -> List[Dict[str, Any]]:
    return [r for r in rows if unit is None or unit.matches(r)]


@router.get("/tools", response_model=LedgerToolsResponse, response_model_by_alias=True)
async def get_ledger_tools(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    version: Optional[str] = Query(None, description="<appVersion> or <appVersion>#<build>"),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """Per tool: calls, error rate, payload, estimated input tokens and dollars, latency, spec tokens."""
    start, end = _range(from_date, to_date)
    unit = _unit(version)
    rows = _filter(await _read(store.family_rows, "TOOL", start, end), unit)
    return LedgerToolsResponse.model_validate({
        "from": start, "to": end, "version": unit.key if unit else None,
        "tools": tool_table(rows), "notMeasured": list(TOOL_FIELDS_NOT_MEASURED),
    })


async def _groups(kind: str, from_date, to_date, version, limit, store: LedgerStore) -> LedgerGroupsResponse:
    start, end = _range(from_date, to_date)
    unit = _unit(version)
    family = kind.upper()
    rows = _filter(await _read(store.family_rows, family, start, end), unit)
    table, omitted = group_table(rows, FAMILY_LABELS[family], limit=limit)
    return LedgerGroupsResponse.model_validate({
        "from": start, "to": end, "version": unit.key if unit else None,
        "kind": f"{kind}s", "rows": table, "omitted": omitted,
    })


@router.get("/agents", response_model=LedgerGroupsResponse, response_model_by_alias=True)
async def get_ledger_agents(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    version: Optional[str] = Query(None, description="<appVersion> or <appVersion>#<build>"),
    limit: int = Query(20, ge=1, le=200),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """Agents by cost: calls, turns, cost and waste per turn, tool error rate."""
    return await _groups("agent", from_date, to_date, version, limit, store)


@router.get("/projects", response_model=LedgerGroupsResponse, response_model_by_alias=True)
async def get_ledger_projects(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    version: Optional[str] = Query(None, description="<appVersion> or <appVersion>#<build>"),
    limit: int = Query(20, ge=1, le=200),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """Projects by cost, in the agents table's shape."""
    return await _groups("project", from_date, to_date, version, limit, store)


_QUALITY_KPIS = (
    "emptyAnswerShare", "interruptedShare", "pausedShare", "forcedCompactionRate", "toolErrorRate",
)
_OUTCOMES = (
    ("completed", "completedTurns"), ("paused", "pausedTurns"), ("stopped", "stoppedTurns"),
    ("error", "errorTurns"), ("interrupted", "interruptedTurns"), ("emptyAnswer", "emptyAnswerTurns"),
    ("outcomeTurns", "outcomeTurns"),
)


@router.get("/quality", response_model=LedgerQualityResponse, response_model_by_alias=True)
async def get_ledger_quality(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    unit: Literal["version", "build"] = UnitQ,
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
    storage: DynamoDBStorage = Depends(get_storage),
):
    """Per version (or build): eval and smoke runs, the thumbs arm, outcomes, empty answers, provenance."""
    start, end = _range(from_date, to_date)
    by_build = unit == "build"
    version_rows = await _read(store.version_rows, start, end)
    eval_rows = await _read(store.eval_rows, start, end)
    smoke_rows = await _read(store.smoke_rows, start, end)
    tool_rows = await _read(store.family_rows, "TOOL", start, end)
    thumbs, provenance = await asyncio.gather(
        _thumbs(storage, start, end), _provenance(storage, start, end, tool_rows),
    )

    sole = ledger_readout.sole_units_by_date(version_rows, by_build)
    units = []
    for unit_key, rows in group_rows(version_rows, lambda r: unit_of(r, by_build)).items():
        dates = sorted({str(r.get("date") or "") for r in rows})
        side = {"unit": unit_key, "dates": set(dates)}
        summed = ledger_kpis.sum_rows(rows)
        kpis = ledger_kpis.version_kpis(summed)
        thumb_counts = ledger_readout.counts_in(thumbs["byKey"], side, sole)
        up, down = thumb_counts.get("up", 0), thumb_counts.get("down", 0)
        rate = ledger_kpis.share({"down": down, "n": up + down}, "down", "n")
        prov = ledger_readout.counts_in(provenance["byKey"], side, sole)
        units.append({
            "key": unit_key.key, "appVersion": unit_key.app_version, "build": unit_key.build,
            "firstDate": dates[0], "lastDate": dates[-1],
            "kpis": {name: kpis[name] for name in _QUALITY_KPIS},
            "outcomes": {name: int(summed.get(counter, 0)) for name, counter in _OUTCOMES},
            "eval": [export_row(r) for r in eval_rows if unit_key.matches(r)],
            "smoke": [export_row(r) for r in smoke_rows if unit_key.matches(r)],
            "thumbs": {"up": up, "down": down, "n": up + down, "downRate": rate["value"],
                       "belowFloor": rate["value"] is None, "retries": thumb_counts.get("retries", 0)},
            "provenance": {
                "verdicts": prov.get("verdicts", 0), "claims": prov.get("claims", 0),
                "supported": prov.get(STATE_SUPPORTED, 0), "quoteUnsupported": prov.get(STATE_QUOTE_UNSUPPORTED, 0),
                "unassessed": prov.get(STATE_UNASSESSED, 0), "unverified": prov.get(STATE_UNVERIFIED, 0),
                "quoteUnsupportedShare": ledger_kpis.share(prov, STATE_QUOTE_UNSUPPORTED, "claims"),
            },
        })
    units.sort(key=lambda u: (u["firstDate"], u["key"]))
    return LedgerQualityResponse.model_validate({
        "from": start, "to": end, "unit": unit, "minimumN": ledger_kpis.MIN_SHARE_N, "units": units,
        "thumbsCoverage": thumbs["coverage"], "provenanceCoverage": provenance["coverage"],
    })


async def _platform(month: str) -> Optional[Dict[str, Any]]:
    try:
        from .service import AdminCostService

        summary = await AdminCostService().get_platform_cost_summary(period=month)
    except Exception:  # noqa: BLE001 - the platform line is context, not the readout
        logger.info("Release ledger: platform cost unavailable for the readout", exc_info=True)
        return None
    return {
        "month": month, "available": summary.available, "inferenceCost": summary.inference_cost,
        "platformCost": summary.platform_cost, "totalCost": summary.total_cost,
    }


@router.get("/readout", response_model=LedgerReadout, response_model_by_alias=True)
async def get_ledger_readout(
    version: Optional[str] = Query(None, description="Candidate: <appVersion> or <appVersion>#<build>; default the newest with a full day"),
    baseline: Optional[str] = Query(None, description="Baseline, same shape; default the unit before the candidate"),
    days: int = Query(ledger_readout.DEFAULT_DAYS, ge=1, le=ledger_readout.MAX_DAYS),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
    storage: DynamoDBStorage = Depends(get_storage),
):
    """The §6.3 comparison bundle: windows, mix shift, KPIs, tools, agents, projects,
    latency, quality, platform and the generated caveats. Every pair is
    ``[baseline, candidate]``; ``null`` is below a floor, never zero."""
    today = today_utc()
    lookback = ledger_readout.lookback_start(today, READOUT_LOOKBACK_DAYS)
    version_rows = await _read(store.version_rows, lookback, today.isoformat())
    markers = await _read(store.deploy_markers)
    try:
        selection = ledger_readout.select_windows(
            version_rows, candidate=_unit(version), baseline=_unit(baseline), days=days, today=today, markers=markers,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    start, end = ledger_readout.window_span(selection)
    model_rows, tool_rows, agent_rows, project_rows, daily, eval_rows, smoke_rows = await asyncio.gather(
        _read(store.family_rows, "MODEL", start, end),
        _read(store.family_rows, "TOOL", start, end),
        _read(store.family_rows, "AGENT", start, end),
        _read(store.family_rows, "PROJECT", start, end),
        _read(store.daily_rollups, start, end),
        _read(store.eval_rows, lookback, today.isoformat()),
        _read(store.smoke_rows, lookback, today.isoformat()),
    )
    cand, base = selection["candidate"], selection["baseline"]
    latency_c, latency_b, thumbs, provenance, platform = await asyncio.gather(
        ledger_latency.window_latency(tuple(cand["window"])),
        ledger_latency.window_latency(tuple(base["window"])) if base else _none(),
        _thumbs(storage, start, end),
        _provenance(storage, start, end, tool_rows),
        _platform(cand["window"][1][:7]),
    )
    bundle = ledger_readout.build_readout(
        selection,
        version_rows=version_rows, model_rows=model_rows, tool_rows=tool_rows,
        agent_rows=agent_rows, project_rows=project_rows, daily_rollups=daily,
        latency={"candidate": latency_c, "baseline": latency_b},
        eval_rows=eval_rows, smoke_rows=smoke_rows, thumbs=thumbs, provenance=provenance, platform=platform,
    )
    return LedgerReadout.model_validate(bundle)


async def _none() -> None:
    return None


#: The ``track`` a row published before EH-9 lacks (eval-harness spec §16
#: D20). Its own choice on the page, because it is neither a nightly nor a
#: release measurement as far as anyone can now tell.
UNTRACKED = "untracked"
_ID = r"^[A-Za-z0-9_.:#@/+\-]{1,160}$"


def _track_of(row: Dict[str, Any]) -> str:
    return str(row.get("track") or UNTRACKED)


@router.get("/evals", response_model=LedgerEvalsResponse, response_model_by_alias=True)
async def get_ledger_evals(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    model_id: Optional[str] = Query(None, alias="modelId", pattern=_ID, description="Only this model's runs"),
    track: Optional[Literal["nightly", "release", "manual", "untracked"]] = Query(
        None, description="Only this track's runs; untracked is a row published before --track existed",
    ),
    unit: Literal["version", "build"] = Query("build", description="What the version bands shade"),
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """``LEDGER#EVAL`` rows in range for the Evaluations page, newest first (eval-harness spec §16 D21).

    Content-free as the rows are: per-case entries are synthetic case ids,
    counts and closed failure codes, and no answer exists in any row. The page
    draws one model at a time (D22), so the counts of models and tracks come
    back with the filtered runs, and the version bands are the ones the
    Releases tab shades, from ``LEDGER#VERSION``.
    """
    start, end = _range(from_date, to_date)
    rows = await _read(store.eval_rows, start, end)
    version_rows = await _read(store.version_rows, start, end)

    model_counts: Dict[str, int] = {}
    track_counts: Dict[str, int] = {}
    for row in rows:
        model = str(row.get("modelId") or "")
        if model:
            model_counts[model] = model_counts.get(model, 0) + 1
        track_counts[_track_of(row)] = track_counts.get(_track_of(row), 0) + 1

    runs = [
        r for r in rows
        if (model_id is None or str(r.get("modelId") or "") == model_id)
        and (track is None or _track_of(r) == track)
    ]
    runs.sort(key=lambda r: str(r.get("startedAt") or r.get("SK") or ""), reverse=True)
    return LedgerEvalsResponse.model_validate({
        "from": start, "to": end, "modelId": model_id, "track": track, "unit": unit,
        "runs": [export_row(r) for r in runs],
        "models": [{"modelId": m, "runs": n} for m, n in sorted(model_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        "tracks": dict(sorted(track_counts.items())),
        "bands": version_bands(version_rows, unit == "build"),
    })


def _ndjson(rows: Sequence[Dict[str, Any]]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(export_row(row), sort_keys=True, separators=(",", ":")) + "\n"


@router.get("/export")
async def export_ledger(
    from_date: Optional[str] = FromQ,
    to_date: Optional[str] = ToQ,
    _: User = Depends(require_ledger_admin),
    store: LedgerStore = Depends(get_ledger_store),
):
    """NDJSON of every ledger row in the range, as stored (ids and numbers only), for offline work.

    Order: ``LEDGER#VERSION``, then each month family, then the eval, smoke,
    deploy and backfill markers. Summing ``cost`` over the ``LEDGER#VERSION``
    lines reproduces the ``versions`` route's totals for the same range.
    """
    start, end = _range(from_date, to_date)

    def collect() -> List[Dict[str, Any]]:
        rows = list(store.version_rows(start, end))
        for family in FAMILY_LABELS:
            rows.extend(store.family_rows(family, start, end))
        rows.extend(store.eval_rows(start, end))
        rows.extend(store.smoke_rows(start, end))
        rows.extend(store.deploy_markers())
        rows.extend(store.backfill_markers())
        return rows

    rows = await _read(collect)
    return StreamingResponse(
        _ndjson(rows),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="release-ledger-{start}-{end}.ndjson"'},
    )
