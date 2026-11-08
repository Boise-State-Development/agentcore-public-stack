"""Reading the release ledger (docs/specs/release-ledger.md §6.1).

Two halves, kept apart so the admin API and ``scripts/release_readout.py``
(PR-7) read the same rows the same way:

* :class:`LedgerStore` issues the queries. Every read is a ``Query`` on a known
  partition of the ``system-cost-rollup`` table: ``LEDGER#VERSION`` is one
  range query on its date-prefixed sort key, the month-partitioned families are
  one query per month in range, and the marker partitions are small. **Nothing
  here reads a ``C#`` row** and nothing scans.
* The pure functions below group rows by a :class:`Unit` and hand the summed
  counters to :mod:`apis.shared.costs.ledger_kpis`, the only place a KPI is
  computed.

Content-free by construction (D7): a ledger row carries ids (version, build,
model, tool, agent, project) and numbers. :func:`export_row` is the defensive
second line for the raw NDJSON export: it drops any string that does not look
like an id, so a future writer that put prose on a ledger row could not leak it
through the export.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apis.shared.costs import ledger_kpis

logger = logging.getLogger(__name__)

ROLLUP_TABLE_ENV = "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME"

VERSION_PK = "LEDGER#VERSION"
DEPLOY_PK = "LEDGER#DEPLOY"
EVAL_PK = "LEDGER#EVAL"
SMOKE_PK = "LEDGER#SMOKE"
BACKFILL_PK = "LEDGER#BACKFILL"

#: The month-partitioned families and the label each row ``SET``s for its key.
FAMILY_LABELS: Dict[str, str] = {
    "MODEL": "modelId",
    "TOOL": "toolId",
    "AGENT": "agentId",
    "PROJECT": "projectId",
}

#: A ``build`` the backfill attributed from the deploy-time map is a commit,
#: not an image content hash (spec §7).
COMMIT_BUILD_PREFIX = "git-"

#: The widest range one request may read. ``LEDGER#VERSION`` holds a few rows a
#: day; a year of them is still a few thousand small items.
MAX_RANGE_DAYS = 400

#: Tool table columns the census does not record yet (spec §5.2 corrections):
#: reported as not measured, never as zero.
TOOL_FIELDS_NOT_MEASURED = ("awaitingUser", "callsWithUi")

_ID_TEXT = re.compile(r"^[A-Za-z0-9_.:#@/+\-]{0,160}$")

Row = Dict[str, Any]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def resolve_range(from_date: Optional[str], to_date: Optional[str], default_days: int = 30) -> Tuple[str, str]:
    """``(from, to)`` as ``YYYY-MM-DD``, inclusive. Raises ``ValueError`` on a bad range."""
    end = parse_date(to_date) if to_date else today_utc()
    start = parse_date(from_date) if from_date else end - timedelta(days=default_days - 1)
    if start > end:
        raise ValueError("from must not be after to")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range may not exceed {MAX_RANGE_DAYS} days")
    return start.isoformat(), end.isoformat()


def months_between(from_date: str, to_date: str) -> List[str]:
    """Every ``YYYY-MM`` from ``from_date``'s month to ``to_date``'s, inclusive."""
    start = parse_date(from_date).replace(day=1)
    end = parse_date(to_date).replace(day=1)
    months: List[str] = []
    cursor = start
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def date_span(from_date: str, to_date: str) -> List[str]:
    start, end = parse_date(from_date), parse_date(to_date)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


# ---------------------------------------------------------------------------
# Units: what a comparison compares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """A version (``build is None``: every build of that ``appVersion``) or one build.

    On prod a version and its build move together; on dev the version is
    constant between releases and every merge is a new build (D1), so a
    comparison there is usually between builds.
    """

    app_version: str
    build: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.app_version}#{self.build}" if self.build else self.app_version

    @property
    def is_build(self) -> bool:
        return self.build is not None

    def matches(self, row: Mapping[str, Any]) -> bool:
        if str(row.get("appVersion") or "") != self.app_version:
            return False
        return self.build is None or str(row.get("build") or "") == self.build

    @classmethod
    def parse(cls, text: str) -> "Unit":
        """``1.33.0`` or ``1.33.0#<build>``."""
        text = (text or "").strip()
        if not text or not _ID_TEXT.match(text):
            raise ValueError("version must be '<appVersion>' or '<appVersion>#<build>'")
        version, _, build = text.partition("#")
        if not version:
            raise ValueError("version must be '<appVersion>' or '<appVersion>#<build>'")
        return cls(version, build or None)


def unit_of(row: Mapping[str, Any], by_build: bool) -> Unit:
    version = str(row.get("appVersion") or "unknown")
    return Unit(version, str(row.get("build") or "unknown")) if by_build else Unit(version)


def build_kind(build: str) -> str:
    """``commit`` for a backfill label from the deploy map, ``image`` for a stamped content hash."""
    if build.startswith(COMMIT_BUILD_PREFIX):
        return "commit"
    return "unknown" if build == "unknown" else "image"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class LedgerStore:
    """Queries over the ``LEDGER#`` partitions. Synchronous; callers use ``asyncio.to_thread``."""

    def __init__(self, table: Any = None) -> None:
        self._table = table

    @property
    def table(self) -> Any:
        if self._table is None:
            table_name = os.environ.get(ROLLUP_TABLE_ENV)
            if not table_name:
                raise RuntimeError(f"{ROLLUP_TABLE_ENV} is not set")
            from apis.shared.aws_clients import get_dynamodb_table

            self._table = get_dynamodb_table(table_name)
        return self._table

    def _query(self, pk: str, lo: Optional[str] = None, hi: Optional[str] = None) -> List[Row]:
        names = {"#pk": "PK"}
        values: Dict[str, Any] = {":pk": pk}
        condition = "#pk = :pk"
        if lo is not None and hi is not None:
            names["#sk"] = "SK"
            values.update({":lo": lo, ":hi": hi})
            condition += " AND #sk BETWEEN :lo AND :hi"
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ScanIndexForward": True,
        }
        items: List[Row] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def version_rows(self, from_date: str, to_date: str) -> List[Row]:
        """``LEDGER#VERSION`` rows dated ``from_date`` through ``to_date`` (inclusive)."""
        return self._query(VERSION_PK, from_date, f"{to_date}#￿")

    def family_rows(self, family: str, from_date: str, to_date: str) -> List[Row]:
        """One month-partitioned family's rows in range: one query per month."""
        if family not in FAMILY_LABELS:
            raise ValueError(f"unknown ledger family {family!r}")
        rows: List[Row] = []
        for month in months_between(from_date, to_date):
            rows.extend(self._query(f"LEDGER#{family}#{month}", from_date, f"{to_date}#￿"))
        return rows

    def deploy_markers(self) -> List[Row]:
        """Every ``LEDGER#DEPLOY`` marker: one per service and build, so a small partition."""
        return self._query(DEPLOY_PK)

    def eval_rows(self, from_date: str, to_date: str) -> List[Row]:
        return self._query(EVAL_PK, from_date, f"{to_date}T￿")

    def smoke_rows(self, from_date: str, to_date: str) -> List[Row]:
        return self._query(SMOKE_PK, from_date, f"{to_date}T￿")

    def backfill_markers(self) -> List[Row]:
        return self._query(BACKFILL_PK)

    def daily_rollups(self, from_date: str, to_date: str) -> List[Row]:
        """``ROLLUP#DAILY`` rows, for ``activeUsers`` (distinct users cannot be ``ADD``ed, §5.3)."""
        return self._query("ROLLUP#DAILY", from_date, to_date)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_rows(rows: Iterable[Row], key: Callable[[Row], Any]) -> Dict[Any, List[Row]]:
    grouped: Dict[Any, List[Row]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return grouped


def totals(rows: Iterable[Row]) -> Dict[str, float]:
    """Summed counters, rounded so a float sum of ``Decimal`` dollars prints cleanly."""
    return {name: round(value, 10) for name, value in sorted(ledger_kpis.sum_rows(rows).items())}


def first_live_at(rows: Iterable[Row]) -> Optional[str]:
    stamps = [str(r["firstLiveAt"]) for r in rows if r.get("firstLiveAt")]
    return min(stamps) if stamps else None


def version_timeline(
    rows: Sequence[Row], markers: Sequence[Row]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(versions, builds)`` for the versions route, oldest first.

    A build appears once per ``(appVersion, build)``; a version rolls up its
    builds. Deploy markers are matched by build, so a Runtime build (whose
    marker is its first ledger row, §5.2) and an app-api build each show the
    services that announced them.
    """
    markers_by_build = group_rows(markers, lambda m: str(m.get("build") or ""))
    builds: List[Dict[str, Any]] = []
    for unit, unit_rows in group_rows(rows, lambda r: unit_of(r, True)).items():
        dates = sorted({str(r.get("date") or "") for r in unit_rows})
        summed = totals(unit_rows)
        builds.append({
            "appVersion": unit.app_version,
            "build": unit.build,
            "buildKind": build_kind(unit.build or ""),
            "firstDate": dates[0],
            "lastDate": dates[-1],
            "daysObserved": len(dates),
            "firstLiveAt": first_live_at(unit_rows),
            "backfilled": any(r.get("backfilledAt") for r in unit_rows),
            "deployMarkers": [
                {"service": str(m.get("service") or ""), "firstSeenAt": str(m.get("firstSeenAt") or "")}
                for m in sorted(markers_by_build.get(unit.build or "", []), key=lambda m: str(m.get("firstSeenAt")))
            ],
            "totals": summed,
            "kpis": ledger_kpis.version_kpis(summed),
        })
    builds.sort(key=lambda b: (b["firstDate"], b["firstLiveAt"] or "", b["build"]))

    versions: List[Dict[str, Any]] = []
    for unit, unit_rows in group_rows(rows, lambda r: unit_of(r, False)).items():
        dates = sorted({str(r.get("date") or "") for r in unit_rows})
        summed = totals(unit_rows)
        versions.append({
            "appVersion": unit.app_version,
            "firstDate": dates[0],
            "lastDate": dates[-1],
            "daysObserved": len(dates),
            "builds": len({str(r.get("build") or "") for r in unit_rows}),
            "firstLiveAt": first_live_at(unit_rows),
            "totals": summed,
            "kpis": ledger_kpis.version_kpis(summed),
        })
    versions.sort(key=lambda v: (v["firstDate"], v["appVersion"]))
    return versions, builds


def version_bands(rows: Sequence[Row], by_build: bool) -> List[Dict[str, Any]]:
    """The shaded bands a trend chart draws: each unit's first and last date."""
    bands = []
    for unit, unit_rows in group_rows(rows, lambda r: unit_of(r, by_build)).items():
        dates = sorted({str(r.get("date") or "") for r in unit_rows})
        bands.append({"key": unit.key, "appVersion": unit.app_version, "build": unit.build,
                      "from": dates[0], "to": dates[-1]})
    bands.sort(key=lambda b: (b["from"], b["key"]))
    return bands


_FAMILY_KPIS: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {
    "version": ledger_kpis.version_kpis,
    "model": ledger_kpis.model_kpis,
    "tool": ledger_kpis.tool_kpis,
    "agent": ledger_kpis.group_kpis,
    "project": ledger_kpis.group_kpis,
}

#: What a series is ranked by when only the top keys are returned.
_SERIES_RANK = {"version": "cost", "model": "cost", "tool": "calls", "agent": "cost", "project": "cost"}


def series(
    rows: Sequence[Row], by: str, *, by_build: bool = False, limit: int = 12
) -> Tuple[List[Dict[str, Any]], int]:
    """Daily points per key, the top ``limit`` keys by cost (calls for tools). ``(series, omitted)``."""
    if by == "version":
        key_of: Callable[[Row], str] = lambda r: unit_of(r, by_build).key
    else:
        label = FAMILY_LABELS[by.upper()]
        key_of = lambda r: str(r.get(label) or "unknown")
    kpi = _FAMILY_KPIS[by]
    rank = _SERIES_RANK[by]
    grouped = group_rows(rows, key_of)
    ranked = sorted(grouped, key=lambda k: (-ledger_kpis.sum_rows(grouped[k]).get(rank, 0.0), k))
    out = []
    for key in ranked[:limit]:
        points = []
        for day, day_rows in sorted(group_rows(grouped[key], lambda r: str(r.get("date") or "")).items()):
            summed = totals(day_rows)
            points.append({"date": day, "totals": summed, "kpis": kpi(summed)})
        out.append({"key": key, "points": points})
    return out, max(0, len(ranked) - limit)


# ---------------------------------------------------------------------------
# Tools, agents, projects
# ---------------------------------------------------------------------------


def _spec_tokens_path() -> Path:
    override = os.environ.get("TOOL_SPEC_TOKENS_PATH", "").strip()
    if override:
        return Path(override)
    # backend/src/apis/shared/costs/ -> backend/evals/ (and /app/src/... -> /app/evals/ in the image)
    return Path(__file__).resolve().parents[4] / "evals" / "tool_spec_tokens.json"


_SPEC_TOKENS: Optional[Dict[str, int]] = None


def spec_tokens() -> Dict[str, int]:
    """Per-tool spec tokens from the committed CountTokens measurement, by census tool name.

    Read once. The file is keyed by seeded tool id with each family member
    measured alone under the name the census uses; a member's own figure wins
    over its family's. Missing or unreadable reads as empty, and the table
    shows no spec column for that tool rather than a zero.
    """
    global _SPEC_TOKENS
    if _SPEC_TOKENS is not None:
        return _SPEC_TOKENS
    found: Dict[str, int] = {}
    try:
        data = json.loads(_spec_tokens_path().read_text())
        for tool_id, entry in (data.get("tools") or {}).items():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("tokens"), int):
                found.setdefault(str(tool_id), int(entry["tokens"]))
            for name, tokens in (entry.get("tools") or {}).items():
                if isinstance(tokens, int):
                    found[str(name)] = int(tokens)
    except (OSError, ValueError, AttributeError):
        logger.info("Release ledger: no tool spec-token measurement found; spec column empty")
    _SPEC_TOKENS = found
    return found


def tool_table(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    """The tools table (§6.1), sorted by estimated input dollars."""
    tokens = spec_tokens()
    table = []
    for tool_id, tool_rows in group_rows(rows, lambda r: str(r.get("toolId") or "unknown")).items():
        summed = totals(tool_rows)
        table.append({
            "toolId": tool_id,
            "totals": summed,
            "kpis": ledger_kpis.tool_kpis(summed),
            # Rows from before the census weighed results carry calls and no
            # payload (spec §7): "not measured", never "empty".
            "payloadMeasured": bool(summed.get("resultChars") or summed.get("timedCalls")),
            "specTokens": tokens.get(tool_id),
        })
    table.sort(key=lambda t: (-(t["totals"].get("estInputUsd") or 0.0), -(t["totals"].get("calls") or 0.0), t["toolId"]))
    return table


def group_table(rows: Sequence[Row], label: str, limit: int = 20) -> Tuple[List[Dict[str, Any]], int]:
    """Agents or projects by cost: ``(top rows, omitted)``."""
    table = []
    for key, key_rows in group_rows(rows, lambda r: str(r.get(label) or "unknown")).items():
        summed = totals(key_rows)
        table.append({"id": key, "totals": summed, "kpis": ledger_kpis.group_kpis(summed)})
    table.sort(key=lambda g: (-(g["totals"].get("cost") or 0.0), g["id"]))
    return table[:limit], max(0, len(table) - limit)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value if _ID_TEXT.match(value) else None
    if isinstance(value, Mapping):
        cleaned = {}
        for name, inner in value.items():
            if not _ID_TEXT.match(str(name)):
                continue
            inner = _clean(inner)
            if inner is not None:
                cleaned[str(name)] = inner
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [v for v in (_clean(v) for v in value) if v is not None]
    return None


def export_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """One ledger row as JSON-safe data: numbers as numbers, id-like strings only.

    Ledger rows are content-free as written (D7); this is the second line, so
    a free-text string on a future row is dropped rather than exported.
    """
    return _clean(row) or {}
