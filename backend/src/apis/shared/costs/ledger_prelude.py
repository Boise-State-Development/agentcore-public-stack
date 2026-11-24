"""Harness time before the first token, per version, for the release readout.

``PreludeTotalMs`` and the top-level stage metrics (``turn_timing.py``) are the
only measure of what the harness itself adds before the model is called:
``FirstTokenMs`` is dominated by the model, and the ledger's
``meanFirstTokenMs`` is the model's own first byte. This module reads them
**per build**, not per time window.

**Why Logs Insights, not GetMetricData.** The metrics are dimension-less, so
``GetMetricData`` can only answer "what was the p50 between these two
times" (:mod:`ledger_latency`), and a window with a deploy in it mixes two
builds. Every EMF record also carries ``appVersion`` and ``build`` as
top-level properties (release-ledger D1), and the records are plain JSON
lines in the Runtime's log group, so one Logs Insights query per side filters
to the unit itself and computes the percentiles over its own turns. A deploy
day then splits cleanly instead of blurring.

Two record shapes carry the stages. ``turn_prelude``'s record holds
``PreludeTotalMs`` and the stages up to the stream (preamble, rag, tools,
agent build, stream setup); ``turn_first_token``'s holds ``FirstTokenMs`` and
the stages after it (head of turn, pre-model). One query reads both, and
``count(<field>)`` gives each stage its own n, because a turn that ends before
its first token writes the first record and not the second.

**Cost.** A Logs Insights query is billed on bytes scanned, not matched: on
dev a 15-day window scanned about 1 GB (~$0.005). Each side is one query over
its own window, cached for an hour like the CloudWatch read.

**Mix.** A stage moves with what the turns were as much as with the code:
on prod, 1.37.0's turns were mostly Agent turns with a knowledge base and
1.38.0's were not, and ``RagMs`` fell 215 → 50 ms with no change to
retrieval. So the same query counts each side's Agent turns
(``hasAssistant``) and resumes (``isResume``), the two properties the
prelude record carries that change which stages run, and the readout
raises ``prelude_mix_shift`` when either share moves.

**Content-free.** The query names metric fields and the two build stamps;
``sessionId`` is never selected.

Failure (no log group configured, no permission, a query that does not finish
in time) returns ``available: False`` with a ``reason`` and never fails the
readout; the readout turns that into a caveat.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from apis.shared.costs.ledger_kpis import MIN_SHARE_N
from apis.shared.costs.ledger_latency import LatencyCache, Latency
from apis.shared.costs.ledger_reader import Unit, build_kind

logger = logging.getLogger(__name__)

#: The Runtime's log group. app-api already has it (feedback eval sampling)
#: and is granted ``logs:StartQuery`` on it; the script resolves it from SSM.
RUNTIME_LOG_GROUP_ENV = "AGENTCORE_RUNTIME_LOG_GROUP"
TIMEOUT_ENV = "LEDGER_PRELUDE_QUERY_TIMEOUT_SECONDS"
#: Under the ALB's 60 s idle timeout, which the readout route sits behind; the
#: two sides run concurrently. ``release_readout.py`` raises it for a terminal
#: read, where a prod week (several GB scanned) can take longer.
DEFAULT_TIMEOUT_SECONDS = 45.0
POLL_SECONDS = 1.0

#: Readout name -> EMF metric name, in the order the turn runs them.
#: ``preludeTotalMs`` is handler entry to the model call, model excluded; the
#: rest are its top-level stages (the dotted sub-stages roll up into
#: ``preambleMs`` and ``agentBuildMs``). ``ModelMs`` is left out on purpose:
#: it is the model's time, the one thing this table exists to exclude.
STAGES: Dict[str, str] = {
    "preludeTotalMs": "PreludeTotalMs",
    "preambleMs": "PreambleMs",
    "ragMs": "RagMs",
    "toolsMs": "ToolsMs",
    "agentBuildMs": "AgentBuildMs",
    "streamSetupMs": "StreamSetupMs",
    "headOfTurnMs": "HeadOfTurnMs",
    "preModelMs": "PreModelMs",
}

#: A stage is listed under *Moved* when its p50 changes by more than
#: ``max(MOVED_MIN_MS, MOVED_MIN_RELATIVE × baseline p50)`` with at least
#: ``MOVED_MIN_N`` turns on each side. Tuned against dev, 2026-11-11 → 11-22:
#: on days with ≥ 200 turns ``PreludeTotalMs``'s daily p50 sat in 966–1,027 ms
#: (a ±3% band) and a real ~20 ms improvement to ``PreModelMs`` and
#: ``AgentBuildMs`` was plainly visible, while a day of 11 turns read 7.4 s
#: (cold starts). 5% of a ~1 s prelude is 50 ms, outside that band; the 30 ms
#: floor keeps a 40 ms stage from "moving" on a 3 ms wobble.
MOVED_MIN_MS = 30.0
MOVED_MIN_RELATIVE = 0.05
MOVED_MIN_N = 200

_STAMP = re.compile(r"^[A-Za-z0-9._:+\-]{1,80}$")

Prelude = Dict[str, Any]


def _bounds(window: Tuple[str, str]) -> Tuple[int, int]:
    start = datetime.strptime(window[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(window[1], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    end = min(end, datetime.now(timezone.utc))
    return int(start.timestamp()), max(int(end.timestamp()), int(start.timestamp()) + 1)


def query_for(unit: Unit) -> Tuple[str, str]:
    """``(query string, scope)``. ``scope`` is ``build`` when the query filters to one build.

    A build the ledger attributes by commit (``build_kind == "commit"``) is a
    label from the deploy map, never a stamp an EMF record carries, so it
    narrows to the version and says so (``scope: "version"``).
    """
    if not _STAMP.match(unit.app_version):
        raise ValueError(f"appVersion {unit.app_version!r} is not a stamp")
    clauses = [f'appVersion = "{unit.app_version}"']
    scope = "version"
    if unit.build and build_kind(unit.build) == "image" and _STAMP.match(unit.build):
        clauses.append(f'build = "{unit.build}"')
        scope = "build"
    stats = []
    for index, metric in enumerate(STAGES.values()):
        stats += [
            f"count({metric}) as s{index}n",
            f"pct({metric}, 50) as s{index}p50",
            f"pct({metric}, 90) as s{index}p90",
        ]
    # Counted on the prelude record only: the first-token record carries the
    # same properties, and counting both would double every turn that streamed.
    stats += ["sum(mixAssistant) as mixAssistant", "sum(mixResume) as mixResume"]
    query = (
        "filter ispresent(PreludeTotalMs) or ispresent(FirstTokenMs)\n"
        f"| filter {' and '.join(clauses)}\n"
        "| fields ispresent(PreludeTotalMs) and hasAssistant = 1 as mixAssistant,"
        " ispresent(PreludeTotalMs) and isResume = 1 as mixResume\n"
        f"| stats {', '.join(stats)}"
    )
    return query, scope


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fields(rows: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for row in rows or []:
        for cell in row or []:
            fields[str(cell.get("field"))] = cell.get("value")
        break
    return fields


def parse_mix(rows: Any) -> Dict[str, int]:
    """``{turns, assistantTurns, resumeTurns}``, counted on the prelude record."""
    fields = _fields(rows)
    return {
        "turns": int(_number(fields.get("s0n")) or 0),
        "assistantTurns": int(_number(fields.get("mixAssistant")) or 0),
        "resumeTurns": int(_number(fields.get("mixResume")) or 0),
    }


def parse_results(rows: Any) -> Dict[str, Dict[str, Any]]:
    """One ``stats`` row (no ``by``) into ``{stage: {p50, p90, n}}``, with the floor applied."""
    fields = _fields(rows)
    out: Dict[str, Dict[str, Any]] = {}
    for index, name in enumerate(STAGES):
        n = int(_number(fields.get(f"s{index}n")) or 0)
        enough = n >= MIN_SHARE_N
        p50, p90 = _number(fields.get(f"s{index}p50")), _number(fields.get(f"s{index}p90"))
        out[name] = {
            "p50": round(p50, 1) if enough and p50 is not None else None,
            "p90": round(p90, 1) if enough and p90 is not None else None,
            "n": n,
        }
    return out


def fetch_unit_prelude(
    key: Tuple[str, Optional[str], Tuple[str, str]],
    client: Any = None,
    *,
    log_group: Optional[str] = None,
    timeout: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Prelude:
    """``{available, scope, stages: {name: {p50, p90, n}}, mix}`` for one unit in one window. Never raises.

    ``key`` is ``(appVersion, build or None, (from, to))`` so the cache can hold it.
    """
    app_version, build, window = key
    unit = Unit(app_version, build)
    log_group = (log_group or os.environ.get(RUNTIME_LOG_GROUP_ENV, "")).strip()
    if not log_group:
        return {"available": False, "reason": "no_log_group", "stages": {}}
    try:
        query, scope = query_for(unit)
    except ValueError:
        return {"available": False, "reason": "unstamped", "stages": {}}
    if timeout is None:
        timeout = _number(os.environ.get(TIMEOUT_ENV)) or DEFAULT_TIMEOUT_SECONDS
    start, end = _bounds(window)
    query_id = None
    try:
        if client is None:
            import boto3

            client = boto3.client("logs")
        query_id = client.start_query(
            logGroupName=log_group, startTime=start, endTime=end, queryString=query,
        )["queryId"]
        deadline = time.monotonic() + timeout
        while True:
            response = client.get_query_results(queryId=query_id)
            status = response.get("status")
            if status == "Complete":
                results = response.get("results")
                return {"available": True, "scope": scope, "stages": parse_results(results),
                        "mix": parse_mix(results)}
            if status in ("Failed", "Cancelled", "Timeout", "Unknown"):
                logger.warning("Release ledger: prelude query ended %s", status)
                return {"available": False, "reason": "query_failed", "stages": {}}
            if time.monotonic() >= deadline:
                try:
                    client.stop_query(queryId=query_id)
                except Exception:  # noqa: BLE001 - best effort; the query ends on its own
                    pass
                return {"available": False, "reason": "timeout", "stages": {}}
            sleep(POLL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the prelude is an optional panel
        logger.warning("Release ledger: prelude query failed: %s", type(exc).__name__)
        return {"available": False, "reason": "read_failed", "stages": {}}


_cache = LatencyCache()


async def unit_prelude(
    unit: Unit,
    window: Tuple[str, str],
    loader: Optional[Callable[[Any], Latency]] = None,
) -> Prelude:
    """Cached :func:`fetch_unit_prelude` for one side."""
    return await _cache.get((unit.app_version, unit.build, tuple(window)), loader or fetch_unit_prelude)


def clear_cache() -> None:
    _cache.clear()
