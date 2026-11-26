#!/usr/bin/env python3
"""Read the session-prewarm prepare A/B from the logs: treated vs holdout, caveats first.

A thin shell: it resolves the two log groups, fetches the ``turn_prelude``,
``turn_first_token`` and ``turn_prepare`` Runtime lines and the client
turn-timing EMF records for a window, and hands them to
:mod:`apis.shared.costs.prepare_ab`, which holds every parsing, dedupe,
percentile, comparison and caveat rule (docs/specs/turn-path-ttft.md §5 P7,
*Running the A/B*). Read-only; content-free (session ids are joined on and
never printed, unless ``--debug``).

Usage::

    cd backend
    AWS_PROFILE=dev-ai .venv/bin/python scripts/prepare_ab_readout.py \\
        --profile dev-ai --prefix dev-boisestateai-v2 --since 7d \\
        --holdout-percent 50 --min-n 50

The Runtime log group comes from SSM ``/<prefix>/inference-api/runtime-id``
(the helper ``release_readout.py`` uses). app-api's group has an auto-generated
name; the script finds it by describing log groups, and ``--app-log-group``
names it when more than one matches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apis.shared.costs import prepare_ab  # noqa: E402
from release_readout import runtime_log_group  # noqa: E402


def find_app_log_group(logs: Any, prefix: str) -> Optional[str]:
    """The one log group whose name holds ``AppApiLogGroup`` and the prefix, else ``None``."""
    names = []
    for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePattern="AppApiLogGroup"):
        names += [g["logGroupName"] for g in page.get("logGroups", []) if prefix.lower() in g["logGroupName"].lower()]
    return names[0] if len(names) == 1 else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prefix", required=True, help="project prefix (e.g. dev-boisestateai-v2)")
    parser.add_argument("--since", required=True, help="window start: ISO date/time (UTC) or 7d / 12h")
    parser.add_argument("--until", help="window end, same forms; default now")
    parser.add_argument("--profile", help="AWS profile")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--min-n", type=int, default=50, help="floor per arm (default 50)")
    parser.add_argument("--holdout-percent", type=float, default=50.0,
                        help="the configured CDK_SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT (default 50)")
    parser.add_argument("--seed", type=int, default=20261126, help="bootstrap seed")
    parser.add_argument("--include-hidden", action="store_true", help="client side: include visible=false turns")
    parser.add_argument("--app-log-group", help="app-api log group, when it cannot be found by name")
    parser.add_argument("--runtime-log-group", help="Runtime log group, instead of resolving it from SSM")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--debug", action="store_true", help="print which log groups were read")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, boto_session: Any = None) -> int:
    args = _parser().parse_args(argv)
    now = time.time()
    start = prepare_ab.parse_when(args.since, now=now)
    end = prepare_ab.parse_when(args.until, now=now) if args.until else now
    if end <= start:
        raise SystemExit("--until must be after --since")
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    os.environ.setdefault("AWS_REGION", args.region)
    if boto_session is None:
        import boto3

        boto_session = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs = boto_session.client("logs")
    run = prepare_ab.logs_insights_runner(logs)

    runtime_group = args.runtime_log_group or runtime_log_group(args.prefix, args.region)
    if not runtime_group:
        raise SystemExit("could not resolve the Runtime log group from SSM; pass --runtime-log-group")
    runtime = prepare_ab.parse_runtime(prepare_ab.fetch_lines(run, runtime_group, start, end, prepare_ab.RUNTIME_QUERY))

    app_group = args.app_log_group or find_app_log_group(logs, args.prefix)
    client = []
    if app_group:
        client = prepare_ab.parse_client(prepare_ab.fetch_lines(run, app_group, start, end, prepare_ab.CLIENT_QUERY))
    else:
        sys.stderr.write("app-api log group not found; the client comparison is empty (pass --app-log-group)\n")
    if args.debug:
        sys.stderr.write(f"runtime log group: {runtime_group}\napp-api log group: {app_group}\n")

    report = prepare_ab.build_report(
        runtime=runtime, client=client, window=(start, end), holdout_percent=args.holdout_percent,
        min_n=args.min_n, seed=args.seed, include_hidden=args.include_hidden,
    )
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n" if args.json
                     else prepare_ab.render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
