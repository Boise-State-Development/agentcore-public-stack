#!/usr/bin/env python3
"""Read a quiet watcher triage shadow from its run rows (docs/specs/quiet-watcher-triage.md §6.5).

A thin shell: it lists one user's watch schedules (optionally those whose label starts with
``--label-prefix``), reads each one's ``SCHEDRUN#`` rows since ``--since``, looks up the
schedule model's catalog rates, and hands them to
:mod:`apis.shared.scheduled_prompts.triage_readout`, which holds every counting and pricing
rule. Read-only. It prints labels, model ids, counts, durations, costs and the run ids of shadow
misses, never a user id, a schedule id or any content.

Usage::

    cd backend
    AWS_PROFILE=dev-ai \\
    DYNAMODB_SESSIONS_METADATA_TABLE_NAME=dev-boisestateai-v2-sessions-metadata \\
    DYNAMODB_MANAGED_MODELS_TABLE_NAME=dev-boisestateai-v2-managed-models \\
        .venv/bin/python scripts/watch_triage_readout.py \\
        --user-id <cognito sub> --since 7d --label-prefix "Triage readout:"

``--since`` takes ``<n>h`` or ``<n>d``, or an ISO timestamp. ``--out`` writes the markdown
to a file as well as printing it.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apis.shared.scheduled_prompts import service  # noqa: E402
from apis.shared.scheduled_prompts.runs import ScheduleRun  # noqa: E402
from apis.shared.scheduled_prompts.triage_readout import render, summarize  # noqa: E402


def since_iso(value: str, now: Optional[datetime] = None) -> str:
    """``6h`` / ``7d`` back from now, or an ISO timestamp as given, as the row keys' UTC form."""
    match = re.fullmatch(r"(\d+)([hd])", value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        start = (now or datetime.now(timezone.utc)) - delta
    else:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    return start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def runs_since(user_id: str, schedule_id: str, since: str) -> List[ScheduleRun]:
    """Every run row of one schedule finished at or after ``since``, oldest first (paged)."""
    from boto3.dynamodb.conditions import Key

    table = service._get_table()
    condition = Key("PK").eq(f"USER#{user_id}") & Key("SK").between(
        f"SCHEDRUN#{schedule_id}#{since}", f"SCHEDRUN#{schedule_id}#~"
    )
    kwargs = {"KeyConditionExpression": condition}
    rows: List[ScheduleRun] = []
    while True:
        page = table.query(**kwargs)
        rows += [ScheduleRun.model_validate(item) for item in page.get("Items", [])]
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user-id", required=True, help="the watchers' owner (never printed)")
    parser.add_argument("--since", default="7d")
    parser.add_argument("--label-prefix", default="", help="only watchers whose label starts with this")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    from apis.shared.costs.pricing_config import get_model_pricing

    since = since_iso(args.since)
    schedules = [
        s
        for s in await service.list_scheduled_prompts(args.user_id)
        if s.mode == "watch" and s.label.startswith(args.label_prefix)
    ]
    if not schedules:
        print("No watch schedules match.", file=sys.stderr)
        return 1
    readouts = []
    for schedule in sorted(schedules, key=lambda s: s.label):
        rows = await asyncio.to_thread(runs_since, args.user_id, schedule.schedule_id, since)
        pricing = await get_model_pricing(schedule.model_id) if schedule.model_id else None
        readouts.append(summarize(schedule.label, schedule.model_id, rows, pricing=pricing))
    text = render(readouts, window=f"since {since}")
    print(text)
    if args.out:
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
