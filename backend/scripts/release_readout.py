#!/usr/bin/env python3
"""Write a release readout: candidate vs baseline, caveats first (docs/specs/release-ledger.md §6.5).

The readout is ``GET /admin/costs/ledger/readout``'s bundle (§6.3), rendered
as a short markdown file a release PR can include. **This script computes no
KPI and no delta.** Every number comes from the readout bundle, which
:mod:`apis.shared.costs.ledger_readout` builds from
:mod:`apis.shared.costs.ledger_kpis` (D10); the script only picks a source,
formats the bundle and lists which KPIs the bundle says moved.

Four sources, one bundle:

``--profile <p> --prefix <prefix>`` (the default way)
    Runs the readout route's own handler, in process, against the
    deployment's tables and CloudWatch with an AWS profile and no sign-in. It is
    the same code the deployed route runs, so the numbers and caveats are the
    ones the Releases tab shows. Never reads a ``C#`` row itself.

``--base-url <origin>``
    Fetches the deployed route. Needs an admin session: put the ``Cookie``
    header value of a signed-in browser in ``RELEASE_READOUT_COOKIE``.

``--readout-json <file>``
    A readout response saved earlier (from the Releases tab's export, or
    fetched in a signed-in browser page).

``--export <file.ndjson>``
    Offline, from ``GET /admin/costs/ledger/export``'s NDJSON. Runs the same
    ``select_windows`` and ``build_readout`` over the exported rows. An export
    holds ledger rows only, so CloudWatch latency, the thumbs and provenance
    joins, active users and the platform line are absent, and the markdown says
    so under *Source*.

Writes ``readout.json`` (the bundle as returned) and ``readout.md`` to
``--out`` and prints the markdown.

**Content-free.** The bundle is ids and numbers (D7). The markdown also leaves
out agent and project ids, which are real identifiers of things users made:
those rows are labelled by rank (``agent 1``) unless ``--show-ids`` is given,
which is for a local read, never a PR body.

Usage::

    cd backend
    AWS_PROFILE=dev-ai .venv/bin/python scripts/release_readout.py \\
        --profile dev-ai --prefix dev-boisestateai-v2 \\
        --version 1.34.0 --baseline 1.33.0 --out /tmp/readout-1.34.0

    .venv/bin/python scripts/release_readout.py --export release-ledger.ndjson \\
        --version 1.34.0 --baseline 1.33.0 --out /tmp/readout-offline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

COOKIE_ENV = "RELEASE_READOUT_COOKIE"

#: How far a KPI must move (relative, from the bundle's own ``delta``) to be
#: listed under *Moved*. Listing is not judging: the skill reads the list with
#: the caveats beside it.
MOVED_THRESHOLD = 0.10

#: Tools and groups shown in the markdown; the JSON keeps the bundle's own caps.
TOP_TOOLS_MD = 10
TOP_GROUPS_MD = 5

#: Which way is worse for each KPI, so *Moved* can say "worse" or "better".
#: ``None`` means the KPI has no good direction on its own (a mix measure).
WORSE_WHEN_HIGHER: Dict[str, Optional[bool]] = {
    "costPerTurn": True,
    "costPerCall": True,
    "cacheEfficiency": False,
    "unexplainedWasteShare": True,
    "partialMissShare": True,
    "toolErrorRate": True,
    "toolPayloadPerCall": True,
    "documentTurnShare": None,
    "meanFirstTokenMs": True,
    "meanTurnDurationMs": True,
    "emptyAnswerShare": True,
    "interruptedShare": True,
    "pausedShare": None,
    "forcedCompactionRate": True,
}

#: How each KPI prints. Anything not named prints as a plain number.
_FORMATS: Dict[str, str] = {
    "costPerTurn": "usd",
    "costPerCall": "usd",
    "cacheEfficiency": "pct",
    "unexplainedWasteShare": "pct",
    "partialMissShare": "pct",
    "toolErrorRate": "pct",
    "toolPayloadPerCall": "chars",
    "documentTurnShare": "pct",
    "meanFirstTokenMs": "ms",
    "meanTurnDurationMs": "ms",
    "emptyAnswerShare": "pct",
    "interruptedShare": "pct",
    "pausedShare": "pct",
    "forcedCompactionRate": "pct",
}

#: What an export cannot carry, named in the markdown's *Source* line.
EXPORT_GAPS = ("CloudWatch latency", "thumbs", "provenance", "active users", "platform cost")

Bundle = Dict[str, Any]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


DASH = "—"


def fmt(value: Any, kind: str = "num") -> str:
    """One value as the markdown prints it. ``None`` is a dash: below its floor, never zero."""
    if value is None:
        return DASH
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, (int, float)):
        return str(value)
    if kind == "usd":
        return f"${value:,.4f}" if abs(value) < 100 else f"${value:,.2f}"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "ms":
        return f"{value:,.0f} ms"
    if kind == "chars":
        return f"{value:,.0f} chars"
    if kind == "int":
        return f"{int(value):,}"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4g}"


def fmt_delta(delta: Optional[float]) -> str:
    if delta is None:
        return DASH
    return f"{delta * 100:+.1f}%"


def pair(values: Optional[Sequence[Any]], kind: str = "num") -> str:
    """``[baseline, candidate]`` as ``b → c``."""
    if not values:
        return DASH
    base, cand = (list(values) + [None, None])[:2]
    return f"{fmt(base, kind)} → {fmt(cand, kind)}"


def _side_line(label: str, side: Optional[Mapping[str, Any]]) -> str:
    if not side:
        return f"- **{label}:** none"
    window = side.get("window") or [None, None]
    days = int(side.get("days") or 0)
    builds = int(side.get("builds") or 0)
    return (
        f"- **{label}:** `{side.get('key')}` ({builds} build{'s' if builds != 1 else ''}), "
        f"{window[0]} → {window[1]}, {days} day{'s' if days != 1 else ''} "
        f"({int(side.get('fullDays') or 0)} full)"
    )


def moved(bundle: Mapping[str, Any], threshold: float = MOVED_THRESHOLD) -> List[Dict[str, Any]]:
    """KPIs whose bundle ``delta`` is at least ``threshold``, with both sides over their floor.

    Reads the bundle's own ``delta``; computes none. A KPI with a ``None``
    side has no delta, so a value under its floor can never be listed.
    """
    out = []
    for name, kpi in (bundle.get("kpis") or {}).items():
        delta = kpi.get("delta")
        if delta is None or abs(delta) < threshold:
            continue
        worse_high = WORSE_WHEN_HIGHER.get(name)
        if worse_high is None:
            direction = "shifted"
        else:
            direction = "worse" if (delta > 0) == worse_high else "better"
        out.append({"kpi": name, "delta": delta, "direction": direction, "n": kpi.get("n")})
    out.sort(key=lambda m: (m["direction"] != "worse", -abs(m["delta"])))
    return out


def _eval_lines(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    lines = []
    for row in rows:
        families = row.get("families") or {}
        gates = sorted(f"{name} {f.get('gate')}" for name, f in families.items() if isinstance(f, Mapping))
        lines.append(
            f"{row.get('SK', '')[:10]} {row.get('modelId') or ''} "
            f"gate {'FAIL' if row.get('gateFailed') else 'pass'} ({', '.join(gates) or 'no families'})"
        )
    return lines


def _smoke_lines(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    return [
        f"{row.get('SK', '')[:10]} pass {fmt(row.get('pass'), 'int')} / fail {fmt(row.get('fail'), 'int')}"
        f" / skip {fmt(row.get('skip'), 'int')}"
        for row in rows
    ]


def render_markdown(
    bundle: Mapping[str, Any],
    *,
    source: str,
    gaps: Sequence[str] = (),
    show_ids: bool = False,
    generated_at: Optional[str] = None,
) -> str:
    """The readout as markdown: caveats, then mix shift, then numbers. Content-free."""
    cand, base = bundle.get("candidate") or {}, bundle.get("baseline")
    caveats = list(bundle.get("caveats") or [])
    title = f"{cand.get('key', '?')} vs {base.get('key') if base else 'no baseline'}"
    out: List[str] = [f"### Release readout: {title}", ""]
    out.append(_side_line("Candidate", cand))
    out.append(_side_line("Baseline", base))
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    source_line = f"- **Source:** {source}, unit `{bundle.get('unit')}`, read {stamp}"
    if gaps:
        source_line += f". Not in this source: {', '.join(gaps)}"
    out += [source_line, ""]

    # -- caveats first -----------------------------------------------------------
    out.append(f"**Caveats ({len(caveats)})**" if caveats else "**Caveats:** none")
    out.append("")
    for item in caveats:
        out.append(f"- `{item.get('code')}` {item.get('message')}")
    if caveats:
        out.append("")

    # -- mix shift -----------------------------------------------------------------
    mix = bundle.get("mixShift") or {}
    models = mix.get("models") or {}
    shown = {m: v for m, v in models.items() if max((x or 0.0) for x in v) >= 0.05}
    rest = len(models) - len(shown)
    out.append(f"**Mix shift:** {'⚠ yes' if mix.get('warn') else 'no'}. "
               f"Mean daily active users {pair(mix.get('activeUsers'))}.")
    if shown:
        out += ["", "| Model (share of calls) | Baseline | Candidate |", "|---|---:|---:|"]
        for model_id, values in shown.items():
            out.append(f"| `{model_id}` | {fmt(values[0], 'pct')} | {fmt(values[1], 'pct')} |")
        if rest:
            out.append(f"| {rest} more under 5% in both | | |")
    out.append("")

    # -- KPIs ------------------------------------------------------------------------
    kpis = bundle.get("kpis") or {}
    out += ["**KPIs** (`—` is under its floor, never zero)", "",
            "| KPI | Baseline | Candidate | Δ | n (b / c) |", "|---|---:|---:|---:|---:|"]
    for name, kpi in kpis.items():
        kind = _FORMATS.get(name, "num")
        n = kpi.get("n") or [0, 0]
        out.append(f"| {name} | {fmt(kpi.get('baseline'), kind)} | {fmt(kpi.get('candidate'), kind)} "
                   f"| {fmt_delta(kpi.get('delta'))} | {fmt(n[0], 'int')} / {fmt(n[1], 'int')} |")
    by_model = (kpis.get("costPerTurn") or {}).get("byModel") or {}
    with_turns = {m: k for m, k in by_model.items() if max(k.get("n") or [0, 0]) > 0}
    if with_turns:
        out += ["", "Cost per turn by model:", "",
                "| Model | Baseline | Candidate | Δ | turns (b / c) |", "|---|---:|---:|---:|---:|"]
        for model_id, kpi in sorted(with_turns.items(), key=lambda kv: -((kv[1].get("n") or [0, 0])[1])):
            n = kpi.get("n") or [0, 0]
            out.append(f"| `{model_id}` | {fmt(kpi.get('baseline'), 'usd')} | {fmt(kpi.get('candidate'), 'usd')} "
                       f"| {fmt_delta(kpi.get('delta'))} | {fmt(n[0], 'int')} / {fmt(n[1], 'int')} |")
    out.append("")

    movers = moved(bundle)
    if movers:
        out.append(f"**Moved ≥{MOVED_THRESHOLD:.0%}, both sides over floor:** " + "; ".join(
            f"{m['kpi']} {fmt_delta(m['delta'])} ({m['direction']})" for m in movers))
    else:
        out.append(f"**Moved ≥{MOVED_THRESHOLD:.0%}, both sides over floor:** none")
    out.append("")

    # -- latency ----------------------------------------------------------------------
    latency = bundle.get("latency") or {}
    if latency.get("available"):
        out += ["**Latency** (CloudWatch, by time window)", "",
                "| Metric | p50 | p90 | n |", "|---|---:|---:|---:|"]
        for metric, values in latency.items():
            if metric == "available" or not isinstance(values, Mapping):
                continue
            out.append(f"| {metric} | {pair(values.get('p50'), 'ms')} | {pair(values.get('p90'), 'ms')} "
                       f"| {pair(values.get('n'), 'int')} |")
    else:
        out.append("**Latency:** not available for this readout")
    out.append("")

    # -- tools -----------------------------------------------------------------------
    tools = list(bundle.get("tools") or [])
    if tools:
        out += [f"**Tools** (top {min(TOP_TOOLS_MD, len(tools))} of {len(tools)} by candidate est. input $; "
                "payload `—` where not measured)", "",
                "| Tool | Calls | Error rate | Result chars/call | Est. input $ |", "|---|---:|---:|---:|---:|"]
        for tool in tools[:TOP_TOOLS_MD]:
            measured = tool.get("payloadMeasured") or [None, None]
            chars = [v if measured[i] is not False else None
                     for i, v in enumerate(tool.get("resultCharsPerCall") or [None, None])]
            out.append(f"| `{tool.get('toolId')}` | {pair(tool.get('calls'), 'int')} | "
                       f"{pair(tool.get('errorRate'), 'pct')} | {pair(chars, 'chars')} | "
                       f"{pair(tool.get('estInputUsd'), 'usd')} |")
        out.append("")

    # -- agents and projects ------------------------------------------------------------
    for kind in ("agents", "projects"):
        rows = list(bundle.get(kind) or [])
        if not rows:
            continue
        singular = kind[:-1]
        out += [f"**{kind.capitalize()}** ({len(rows)} in either window; top {min(TOP_GROUPS_MD, len(rows))} "
                "by candidate cost)", "",
                f"| {singular.capitalize()} | Turns | Cost | Cost per turn | Δ |", "|---|---:|---:|---:|---:|"]
        for rank, row in enumerate(rows[:TOP_GROUPS_MD], start=1):
            label = f"`{row.get('id')}`" if show_ids else f"{singular} {rank}"
            cpt = row.get("costPerTurn") or {}
            out.append(f"| {label} | {pair(row.get('turns'), 'int')} | {pair(row.get('cost'), 'usd')} | "
                       f"{fmt(cpt.get('baseline'), 'usd')} → {fmt(cpt.get('candidate'), 'usd')} | "
                       f"{fmt_delta(cpt.get('delta'))} |")
        out.append("")

    # -- quality -----------------------------------------------------------------------
    quality = bundle.get("quality") or {}
    out += ["**Quality**", "", "| Signal | Baseline → Candidate |", "|---|---|"]
    for name in ("emptyAnswerShare", "interruptedShare", "pausedShare", "forcedCompactionRate", "toolErrorRate"):
        out.append(f"| {name} | {pair(quality.get(name), 'pct')} |")
    outcomes = quality.get("outcomes") or {}
    if outcomes:
        out.append(f"| outcome turns | {pair(outcomes.get('outcomeTurns'), 'int')} |")
    prov = quality.get("provenance") or {}
    if any(prov.get("claims") or []):
        out.append(f"| provenance claims (quote unsupported) | {pair(prov.get('claims'), 'int')} "
                   f"({pair(prov.get('quoteUnsupported'), 'int')}, share {pair(prov.get('quoteUnsupportedShare'), 'pct')}) |")
    for side in ("baseline", "candidate"):
        for line in _eval_lines((quality.get("eval") or {}).get(side) or []):
            out.append(f"| eval, {side} | {line} |")
        for line in _smoke_lines((quality.get("smoke") or {}).get(side) or []):
            out.append(f"| smoke, {side} | {line} |")
    thumbs = quality.get("thumbs") or {}
    if thumbs:
        out.append(f"| thumbs up / down (floor n={thumbs.get('minimumN')}) | "
                   f"{pair(thumbs.get('up'), 'int')} / {pair(thumbs.get('down'), 'int')}, "
                   f"down rate {pair(thumbs.get('downRate'), 'pct')} |")
    out.append("")

    platform = bundle.get("platform")
    if platform and platform.get("available"):
        out.append(f"**Platform, {platform.get('month')}:** inference {fmt(platform.get('inferenceCost'), 'usd')}, "
                   f"platform {fmt(platform.get('platformCost'), 'usd')}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class ExportStore:
    """:class:`LedgerStore`'s read methods over the rows of an ``/export`` NDJSON file."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._by_pk: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            self._by_pk.setdefault(str(row.get("PK") or ""), []).append(dict(row))
        for items in self._by_pk.values():
            items.sort(key=lambda r: str(r.get("SK") or ""))

    def _query(self, pk: str, lo: Optional[str] = None, hi: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self._by_pk.get(pk, [])
        if lo is None or hi is None:
            return list(items)
        return [r for r in items if lo <= str(r.get("SK") or "") <= hi]

    def version_rows(self, from_date: str, to_date: str) -> List[Dict[str, Any]]:
        from apis.shared.costs.ledger_reader import VERSION_PK

        return self._query(VERSION_PK, from_date, f"{to_date}#￿")

    def family_rows(self, family: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
        from apis.shared.costs.ledger_reader import FAMILY_LABELS, months_between

        if family not in FAMILY_LABELS:
            raise ValueError(f"unknown ledger family {family!r}")
        rows: List[Dict[str, Any]] = []
        for month in months_between(from_date, to_date):
            rows.extend(self._query(f"LEDGER#{family}#{month}", from_date, f"{to_date}#￿"))
        return rows

    def deploy_markers(self) -> List[Dict[str, Any]]:
        from apis.shared.costs.ledger_reader import DEPLOY_PK

        return self._query(DEPLOY_PK)

    def eval_rows(self, from_date: str, to_date: str) -> List[Dict[str, Any]]:
        from apis.shared.costs.ledger_reader import EVAL_PK

        return self._query(EVAL_PK, from_date, f"{to_date}T￿")

    def smoke_rows(self, from_date: str, to_date: str) -> List[Dict[str, Any]]:
        from apis.shared.costs.ledger_reader import SMOKE_PK

        return self._query(SMOKE_PK, from_date, f"{to_date}T￿")


def read_export(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def readout_from_export(
    rows: Sequence[Mapping[str, Any]],
    *,
    version: Optional[str],
    baseline: Optional[str],
    days: int,
    today: date,
) -> Bundle:
    """The readout over exported rows: the route's ``select_windows`` + ``build_readout``, ledger rows only."""
    from apis.shared.costs import ledger_readout
    from apis.shared.costs.ledger_reader import Unit

    store = ExportStore(rows)
    lookback = ledger_readout.lookback_start(today, 180)
    version_rows = store.version_rows(lookback, today.isoformat())
    selection = ledger_readout.select_windows(
        version_rows,
        candidate=Unit.parse(version) if version else None,
        baseline=Unit.parse(baseline) if baseline else None,
        days=days, today=today, markers=store.deploy_markers(),
    )
    start, end = ledger_readout.window_span(selection)
    bundle = ledger_readout.build_readout(
        selection,
        version_rows=version_rows,
        model_rows=store.family_rows("MODEL", start, end),
        tool_rows=store.family_rows("TOOL", start, end),
        agent_rows=store.family_rows("AGENT", start, end),
        project_rows=store.family_rows("PROJECT", start, end),
        eval_rows=store.eval_rows(lookback, today.isoformat()),
        smoke_rows=store.smoke_rows(lookback, today.isoformat()),
    )
    return _jsonable(bundle)


def _jsonable(bundle: Mapping[str, Any]) -> Bundle:
    """The bundle through the route's own response model, so every source yields the same JSON."""
    from apis.app_api.admin.costs.ledger_models import LedgerReadout

    return LedgerReadout.model_validate(bundle).model_dump(by_alias=True, mode="json")


def readout_from_aws(
    *, profile: str, prefix: str, region: str, version: Optional[str], baseline: Optional[str], days: int,
) -> Bundle:
    """Run the deployed route's handler in process, against the deployment's tables and CloudWatch."""
    os.environ["AWS_PROFILE"] = profile
    os.environ.setdefault("AWS_REGION", region)
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    os.environ["PROJECT_PREFIX"] = prefix
    from apis.shared.config.runtime_environment import hydrate_derived_environment

    hydrate_derived_environment()
    from fastapi import HTTPException

    from apis.app_api.admin.costs import ledger_routes
    from apis.shared.costs import ledger_readout
    from apis.shared.costs.ledger_reader import LedgerStore
    from apis.shared.storage.dynamodb_storage import DynamoDBStorage

    try:
        result = asyncio.run(ledger_routes.get_ledger_readout(
            version=version, baseline=baseline, days=max(1, min(days, ledger_readout.MAX_DAYS)),
            _=None, store=LedgerStore(), storage=DynamoDBStorage(),
        ))
    except HTTPException as exc:
        raise SystemExit(f"readout refused ({exc.status_code}): {exc.detail}")
    return result.model_dump(by_alias=True, mode="json")


def readout_from_http(
    *, base_url: str, version: Optional[str], baseline: Optional[str], days: int,
) -> Bundle:
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if not cookie:
        raise SystemExit(f"--base-url needs a signed-in admin session: set {COOKIE_ENV} to the Cookie header value")
    query = {"days": str(days)}
    if version:
        query["version"] = version
    if baseline:
        query["baseline"] = baseline
    url = f"{base_url.rstrip('/')}/admin/costs/ledger/readout?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Cookie": cookie, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - an operator-given origin
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"readout refused ({exc.code}) by {base_url}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_argument_group("source (one of)")
    source.add_argument("--profile", help="AWS profile: run the readout route's code against the deployment")
    source.add_argument("--base-url", help=f"SPA origin + /api, e.g. https://dev.example.edu/api ({COOKIE_ENV})")
    source.add_argument("--readout-json", type=Path, help="a saved readout response")
    source.add_argument("--export", type=Path, help="an /admin/costs/ledger/export NDJSON file (offline)")
    parser.add_argument("--prefix", help="resource prefix, with --profile (e.g. dev-boisestateai-v2)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--version", help="candidate: <appVersion> or <appVersion>#<build>; default the newest with a full day")
    parser.add_argument("--baseline", help="baseline, same shape; default the unit before the candidate")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--today", help="with --export: the date full days are judged against (YYYY-MM-DD, default today UTC)")
    parser.add_argument("--out", type=Path, help="directory for readout.json and readout.md")
    parser.add_argument("--show-ids", action="store_true", help="print agent and project ids (local reads only, never a PR body)")
    return parser


def _source(args: argparse.Namespace) -> Tuple[Bundle, str, Tuple[str, ...]]:
    chosen = [name for name in ("profile", "base_url", "readout_json", "export") if getattr(args, name)]
    if len(chosen) != 1:
        raise SystemExit("give exactly one source: --profile, --base-url, --readout-json or --export")
    if args.profile:
        if not args.prefix:
            raise SystemExit("--profile needs --prefix")
        bundle = readout_from_aws(profile=args.profile, prefix=args.prefix, region=args.region,
                                  version=args.version, baseline=args.baseline, days=args.days)
        return bundle, f"`{args.prefix}` ledger via the readout route's code", ()
    if args.base_url:
        host = urllib.parse.urlparse(args.base_url).netloc
        bundle = readout_from_http(base_url=args.base_url, version=args.version, baseline=args.baseline, days=args.days)
        return bundle, f"`{host}` readout route", ()
    if args.readout_json:
        return json.loads(args.readout_json.read_text()), "a saved readout response", ()
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    try:
        bundle = readout_from_export(read_export(args.export), version=args.version, baseline=args.baseline,
                                     days=args.days, today=today)
    except (LookupError, ValueError) as exc:
        raise SystemExit(f"readout refused: {exc}")
    return bundle, "a ledger export (offline)", EXPORT_GAPS


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    bundle, source, gaps = _source(args)
    markdown = render_markdown(bundle, source=source, gaps=gaps, show_ids=args.show_ids)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "readout.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
        (args.out / "readout.md").write_text(markdown)
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
