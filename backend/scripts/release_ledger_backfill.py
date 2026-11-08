#!/usr/bin/env python3
"""Backfill the release ledger from existing ``C#`` cost rows (docs/specs/release-ledger.md §7).

The ledger's ``LEDGER#`` rollups are bumped per call from the moment
``RELEASE_LEDGER_ENABLED`` is on. Everything before that exists only as ``C#``
rows, so this does the one scan the spec otherwise avoids: every ``C#`` row in
the range, read through ``LEDGER_ROW_PROJECTION`` (ids and numbers, never
content), mapped by the **same** ``ledger_updates`` the live writer uses, and
``ADD``ed into the same rows.

**Which version a row belongs to.** A row stamped with ``appVersion`` /
``build`` (PR-1, live on dev from 2026-11-06 ~22:04 UTC) is attributed by its
own stamp. An older row is attributed by a deploy-time map: the latest
successful ``backend.yml`` run on the branch that completed at or before the
row's timestamp, labelled with the ``VERSION`` at that run's commit and a build
of ``git-<sha12>`` (the image's content hash was never recorded for those runs,
so the label says it is a commit, not a hash). A row older than the map's first
entry is ``unknown``. Build the map with ``build-map``.

**Rows the live writer already counted are skipped.** The live writer marks
each ``C#`` row it will count with ``ledgered``, in the same ``put_item`` that
stores it, and the backfill never adds a marked row. That mark, not a time, is
what keeps a call from being counted twice: a row's ``timestamp`` is when the
call ran, earlier than when it was stored, so the first live turn's rows sit
just *before* ``firstLiveAt`` (four of them on dev, 2026-11-07). A marked row
still counts toward the per-day reconciliation, since it is in the ledger.

**Where it must stop.** The live writer also stamps ``firstLiveAt`` on each
ledger row it first touches; the earliest across ``LEDGER#VERSION`` is where
live counting began. The script refuses a range past it, and with no ``--to``
it stops exactly there (or now, if the flag has never been on). Rows written by
PR-3's build, the first live writer, carry no mark, and the ceiling keeps the
backfill clear of them. Each run records a ``LEDGER#BACKFILL`` marker for its
range and a later run refuses to overlap one, because ``ADD`` is not
idempotent. So the procedure is: backfill up to now, turn the flag on, then
run once more with no ``--to`` to fill the gap up to the first live write.

**Known gap.** Turning the flag on redeploys the Runtime, but a microVM already
serving a session keeps the old environment until that session ends, so a few
minutes of calls after ``firstLiveAt`` can be written by a process with the
flag still off. The per-day reconciliation below shows it on the cutover day.

Default is a dry run that prints totals and reconciles each full day against
``ROLLUP#DAILY`` (cost and call count). ``--yes`` writes.

Usage::

    cd backend
    .venv/bin/python scripts/release_ledger_backfill.py build-map --branch develop --out /tmp/versions.json
    AWS_PROFILE=dev-ai .venv/bin/python scripts/release_ledger_backfill.py backfill \\
        --region us-west-2 --prefix dev-boisestateai-v2 --version-map /tmp/versions.json \\
        --to 2026-11-07 [--report /tmp/report.json] [--yes]
"""

from __future__ import annotations

import argparse
import bisect
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from apis.shared.observability.content_policy import LEDGER_ROW_PROJECTION, build_projection  # noqa: E402
from apis.shared.sessions.ledger_rollups import (  # noqa: E402
    BACKFILL_PK,
    BACKFILLED_AT,
    FIRST_LIVE_AT,
    LEDGERED,
    UNKNOWN,
    LedgerUpdate,
    ledger_updates,
    merge_updates,
    row_date,
    update_item_kwargs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def parse_instant(value: str) -> datetime:
    """An ISO date or datetime as an aware UTC instant (a bare date is its 00:00Z)."""
    text = value.strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def row_instant(row: Mapping[str, Any]) -> Optional[datetime]:
    try:
        return parse_instant(str(row.get("timestamp")))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The version map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deploy:
    at: datetime
    app_version: str
    build: str


def load_version_map(path: Path) -> List[Deploy]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    deploys = [Deploy(parse_instant(e["at"]), str(e["appVersion"]), str(e["build"])) for e in entries]
    return sorted(deploys, key=lambda d: d.at)


def attribute(row: Dict[str, Any], deploys: Sequence[Deploy], keys: Sequence[datetime]) -> str:
    """Give an unstamped row the version live at its timestamp. Returns how it was attributed."""
    if row.get("appVersion") not in (None, "", UNKNOWN) and row.get("build") not in (None, "", UNKNOWN):
        return "stamped"
    instant = row_instant(row)
    index = bisect.bisect_right(keys, instant) - 1 if instant is not None else -1
    if index < 0:
        row["appVersion"], row["build"] = UNKNOWN, UNKNOWN
        return "unknown"
    row["appVersion"], row["build"] = deploys[index].app_version, deploys[index].build
    return "mapped"


def build_version_map(branch: str, limit: int) -> List[Dict[str, str]]:
    """Successful ``backend.yml`` runs on ``branch``, oldest first, with the ``VERSION`` at each commit."""
    runs = json.loads(subprocess.run(
        ["gh", "run", "list", "--workflow", "backend.yml", "--branch", branch, "--limit", str(limit),
         "--json", "headSha,updatedAt,conclusion,status"],
        check=True, capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout)
    entries: List[Dict[str, str]] = []
    versions: Dict[str, str] = {}
    for run in sorted(runs, key=lambda r: r["updatedAt"]):
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        sha = run["headSha"]
        if sha not in versions:
            shown = subprocess.run(["git", "show", f"{sha}:VERSION"], capture_output=True, text=True, cwd=REPO_ROOT)
            versions[sha] = shown.stdout.strip() if shown.returncode == 0 and shown.stdout.strip() else UNKNOWN
        entries.append({"at": run["updatedAt"], "appVersion": versions[sha], "build": f"git-{sha[:12]}", "sha": sha})
    return entries


# ---------------------------------------------------------------------------
# DynamoDB reads
# ---------------------------------------------------------------------------


def _query_all(table: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def first_live_write(rollup_table: Any) -> Optional[datetime]:
    from boto3.dynamodb.conditions import Key

    rows = _query_all(
        rollup_table,
        KeyConditionExpression=Key("PK").eq("LEDGER#VERSION"),
        ProjectionExpression="#f",
        ExpressionAttributeNames={"#f": FIRST_LIVE_AT},
    )
    stamps = [parse_instant(str(r[FIRST_LIVE_AT])) for r in rows if r.get(FIRST_LIVE_AT)]
    return min(stamps) if stamps else None


def backfill_markers(rollup_table: Any) -> List[Tuple[datetime, datetime]]:
    from boto3.dynamodb.conditions import Key

    rows = _query_all(rollup_table, KeyConditionExpression=Key("PK").eq(BACKFILL_PK))
    return [(parse_instant(str(r["from"])), parse_instant(str(r["to"]))) for r in rows]


def daily_rollups(rollup_table: Any) -> Dict[str, Dict[str, Decimal]]:
    from boto3.dynamodb.conditions import Key

    rows = _query_all(rollup_table, KeyConditionExpression=Key("PK").eq("ROLLUP#DAILY"))
    return {
        str(r["SK"]): {"cost": Decimal(str(r.get("totalCost", 0))), "calls": Decimal(str(r.get("totalRequests", 0)))}
        for r in rows
    }


def scan_cost_rows(metadata_table: Any, segments: int) -> List[Dict[str, Any]]:
    """Every ``C#`` row, through the ledger projection. Content never leaves DynamoDB."""
    from boto3.dynamodb.conditions import Attr

    projection, names = build_projection(LEDGER_ROW_PROJECTION)

    def segment(index: int) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "FilterExpression": Attr("SK").begins_with("C#"),
            "ProjectionExpression": projection,
            "ExpressionAttributeNames": names,
            "Segment": index,
            "TotalSegments": segments,
        }
        items: List[Dict[str, Any]] = []
        while True:
            page = metadata_table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    with ThreadPoolExecutor(max_workers=segments) as pool:
        return [row for part in pool.map(segment, range(segments)) for row in part]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def plan(
    rows: Iterable[Dict[str, Any]],
    deploys: Sequence[Deploy],
    start: datetime,
    end: datetime,
) -> Tuple[List[LedgerUpdate], Dict[str, Any]]:
    """The merged updates for the rows in ``[start, end)``, and a content-free summary of them."""
    keys = [d.at for d in deploys]
    attribution: Dict[str, int] = defaultdict(int)
    by_day: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: {"cost": Decimal(0), "calls": Decimal(0)})
    by_version: Dict[str, Dict[str, Decimal]] = defaultdict(
        lambda: {"calls": Decimal(0), "turns": Decimal(0), "cost": Decimal(0)}
    )
    updates: List[LedgerUpdate] = []
    in_range = 0
    for row in rows:
        instant = row_instant(row)
        if instant is None or not (start <= instant < end):
            continue
        in_range += 1
        live = bool(row.get(LEDGERED))
        attribution["live" if live else attribute(row, deploys, keys)] += 1
        row_updates = ledger_updates(row)
        if not row_updates:
            continue
        version = row_updates[0].add
        day = by_day[row_date(row)]
        day["cost"] += Decimal(version.get("cost", 0))
        day["calls"] += Decimal(version.get("calls", 0))
        if live:
            continue
        label = f"{row['appVersion']}#{row['build']}"
        for name in ("calls", "turns", "cost"):
            by_version[label][name] += Decimal(version.get(name, 0))
        updates.extend(row_updates)
    merged = merge_updates(updates)
    summary = {
        "rows": in_range,
        "attribution": dict(attribution),
        "ledgerRows": len(merged),
        "byDay": {d: by_day[d] for d in sorted(by_day)},
        "byVersion": {v: by_version[v] for v in sorted(by_version)},
    }
    return merged, summary


def reconcile(
    by_day: Mapping[str, Mapping[str, Decimal]],
    rollups: Mapping[str, Mapping[str, Decimal]],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    """Per full day in range: ledger vs ``ROLLUP#DAILY`` cost and calls."""
    days: Dict[str, Any] = {}
    for day in sorted(set(by_day) | set(rollups)):
        day_start = parse_instant(day)
        full = start <= day_start and parse_instant(day + "T23:59:59.999999") < end
        if not full:
            continue
        ledger = by_day.get(day, {"cost": Decimal(0), "calls": Decimal(0)})
        rollup = rollups.get(day, {"cost": Decimal(0), "calls": Decimal(0)})
        days[day] = {
            "ledgerCost": ledger["cost"], "rollupCost": rollup["cost"],
            "costDiff": ledger["cost"] - rollup["cost"],
            "ledgerCalls": ledger["calls"], "rollupCalls": rollup["calls"],
            "callDiff": ledger["calls"] - rollup["calls"],
        }
    ledger_cost = sum((d["ledgerCost"] for d in days.values()), Decimal(0))
    rollup_cost = sum((d["rollupCost"] for d in days.values()), Decimal(0))
    return {
        "fullDays": len(days),
        "ledgerCost": ledger_cost,
        "rollupCost": rollup_cost,
        "costDiff": ledger_cost - rollup_cost,
        "maxDayCostDiff": max((abs(d["costDiff"]) for d in days.values()), default=Decimal(0)),
        "ledgerCalls": sum((d["ledgerCalls"] for d in days.values()), Decimal(0)),
        "rollupCalls": sum((d["rollupCalls"] for d in days.values()), Decimal(0)),
        "daysWithCallDiff": {k: d["callDiff"] for k, d in days.items() if d["callDiff"]},
        "days": days,
    }


def write(rollup_table: Any, updates: Sequence[LedgerUpdate], start: datetime, end: datetime, rows: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    marker_key = {"PK": BACKFILL_PK, "SK": f"{start.isoformat()}#{end.isoformat()}"}
    # The marker goes first, so an interrupted run still blocks a second
    # overlapping one: half-applied ADDs are recovered by hand, never by a rerun.
    rollup_table.put_item(Item={
        **marker_key, "from": start.isoformat(), "to": end.isoformat(), "rows": rows,
        "ledgerRows": len(updates), "startedAt": now, "status": "started",
    })

    def apply(update: LedgerUpdate) -> None:
        stamped = LedgerUpdate(update.pk, update.sk, update.add, {**update.set, BACKFILLED_AT: now})
        rollup_table.update_item(**update_item_kwargs(stamped))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(apply, updates))
    rollup_table.update_item(
        Key=marker_key,
        UpdateExpression="SET #s = :s, completedAt = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "complete", ":c": datetime.now(timezone.utc).isoformat()},
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def run_backfill(args: argparse.Namespace) -> int:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource("dynamodb")
    metadata_table = dynamodb.Table(f"{args.prefix}-sessions-metadata")
    rollup_table = dynamodb.Table(f"{args.prefix}-system-cost-rollup")

    live = first_live_write(rollup_table)
    start = parse_instant(args.from_)
    end = parse_instant(args.to) if args.to else (live or datetime.now(timezone.utc))
    if live is not None and end > live:
        print(f"refusing: --to {end.isoformat()} reaches past the first live ledger write "
              f"({live.isoformat()}); those calls are already counted", file=sys.stderr)
        return 2
    if end <= start:
        print("refusing: empty range", file=sys.stderr)
        return 2
    for done_from, done_to in backfill_markers(rollup_table):
        if start < done_to and done_from < end:
            print(f"refusing: overlaps an earlier backfill [{done_from.isoformat()}, {done_to.isoformat()})",
                  file=sys.stderr)
            return 2

    deploys = load_version_map(Path(args.version_map))
    rows = scan_cost_rows(metadata_table, args.segments)
    updates, summary = plan(rows, deploys, start, end)
    summary["range"] = [start.isoformat(), end.isoformat()]
    summary["firstLiveWrite"] = live.isoformat() if live else None
    summary["reconciliation"] = reconcile(summary["byDay"], daily_rollups(rollup_table), start, end)

    rec = summary["reconciliation"]
    print(f"range           {start.isoformat()} → {end.isoformat()}")
    print(f"first live write {summary['firstLiveWrite']}")
    print(f"cost rows       {summary['rows']}  ({dict(summary['attribution'])})")
    print(f"ledger rows     {summary['ledgerRows']}")
    print(f"versions        {len(summary['byVersion'])}")
    print(f"full days       {rec['fullDays']}")
    print(f"cost            ledger ${rec['ledgerCost']:.6f}  ROLLUP#DAILY ${rec['rollupCost']:.6f}  "
          f"diff ${rec['costDiff']:.6f}  worst day ${rec['maxDayCostDiff']:.6f}")
    print(f"calls           ledger {rec['ledgerCalls']}  ROLLUP#DAILY {rec['rollupCalls']}  "
          f"days that differ: {len(rec['daysWithCallDiff'])}")
    for day, diff in sorted(rec["daysWithCallDiff"].items()):
        detail = rec["days"][day]
        print(f"  {day}  calls {diff:+}  cost {detail['costDiff']:+.6f}")
    if args.report:
        Path(args.report).write_text(json.dumps(summary, default=_json_default, indent=2), encoding="utf-8")
        print(f"report          {args.report}")

    if not args.yes:
        print("dry run: nothing written (pass --yes to write)")
        return 0
    write(rollup_table, updates, start, end, summary["rows"])
    print(f"wrote {len(updates)} ledger rows and the {BACKFILL_PK} marker")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-map", help="write the deploy-time version map from backend.yml runs")
    build.add_argument("--branch", default="develop")
    build.add_argument("--limit", type=int, default=5000)
    build.add_argument("--out", required=True)

    back = sub.add_parser("backfill", help="scan C# rows and add them to the ledger (dry run by default)")
    back.add_argument("--profile", default=None)
    back.add_argument("--region", default="us-west-2")
    back.add_argument("--prefix", required=True)
    back.add_argument("--version-map", required=True)
    back.add_argument("--from", dest="from_", default="2000-01-01")
    back.add_argument("--to", default=None, help="exclusive; default: the first live ledger write, else now")
    back.add_argument("--segments", type=int, default=4)
    back.add_argument("--report", default=None, help="write the content-free summary as JSON")
    back.add_argument("--yes", action="store_true", help="write the ledger rows")

    args = parser.parse_args(argv)
    if args.command == "build-map":
        entries = build_version_map(args.branch, args.limit)
        Path(args.out).write_text(json.dumps(entries, indent=2), encoding="utf-8")
        print(f"{len(entries)} deploys → {args.out}")
        return 0
    return run_backfill(args)


if __name__ == "__main__":
    raise SystemExit(main())
