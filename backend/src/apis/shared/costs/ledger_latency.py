"""Latency percentiles for a release-ledger window, from CloudWatch (spec §6.1, §5.3).

DynamoDB holds sums and counts, so the ledger has means and no percentiles.
p50 / p90 of the turn's time to first token come from the ``TurnLatency``
metrics at read time, one ``GetMetricData`` call per window, cached for an hour.

**The join is by time, not by stamp.** The latency metrics are published
without dimensions (``appVersion`` and ``build`` ride the EMF record as
*properties*, which Logs Insights can group by but ``GetMetricData`` cannot),
so a version's percentiles are the percentiles over its window. Outside a
deploy day the window holds one version and that is exact; a window that
contains a deploy mixes both, and the readout says so in a caveat
(``deploy_in_window``) instead of pretending otherwise.

Each window asks for one period spanning the whole window, so CloudWatch
computes the percentile over every sample in it rather than the reader
averaging daily percentiles (which is not a percentile). A metric with fewer
than :data:`ledger_kpis.MIN_SHARE_N` samples reports ``None``: a p90 of nine
probe runs is not a number to compare.

Failure (no permission, throttling, no data) returns ``available: False`` and
never fails the readout; the readout turns that into a caveat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from apis.shared.costs.ledger_kpis import MIN_SHARE_N

logger = logging.getLogger(__name__)

NAMESPACE = os.environ.get("TURN_LATENCY_EMF_NAMESPACE", "AgentCoreStack/TurnLatency")

#: Readout name -> metric name. The server figure is the turn's whole server
#: share of the wait (handler entry to first token); the client figure is the
#: SPA's send-to-first-token; the probe is the hourly synthetic turn.
METRICS: Dict[str, str] = {
    "serverFirstTokenMs": "FirstTokenMs",
    "clientFirstTokenMs": "ClientFirstTokenMs",
    "probeColdFirstTokenMs": "TtftProbeColdFirstTokenMs",
    "probeFollowUpFirstTokenMs": "TtftProbeFollowUpFirstTokenMs",
}

_STATS = (("p50", "p50"), ("p90", "p90"), ("n", "SampleCount"))

CACHE_TTL_SECONDS = 3600

Window = Tuple[str, str]
Latency = Dict[str, Any]


def _window_bounds(window: Window) -> Tuple[datetime, datetime]:
    start = datetime.strptime(window[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(window[1], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


def _queries(period: int) -> list:
    queries = []
    for index, (name, metric) in enumerate(METRICS.items()):
        for stat_index, (_, stat) in enumerate(_STATS):
            queries.append({
                "Id": f"m{index}s{stat_index}",
                "Label": f"{name}|{stat}",
                "MetricStat": {
                    "Metric": {"Namespace": NAMESPACE, "MetricName": metric, "Dimensions": []},
                    "Period": period,
                    "Stat": stat,
                },
                "ReturnData": True,
            })
    return queries


def fetch_window_latency(window: Window, client: Any = None) -> Latency:
    """``{available, metrics: {name: {p50, p90, n}}}`` for one ``(from, to)`` window. Never raises."""
    start, end = _window_bounds(window)
    period = int((end - start).total_seconds())
    try:
        if client is None:
            import boto3

            client = boto3.client("cloudwatch")
        response = client.get_metric_data(
            MetricDataQueries=_queries(period), StartTime=start, EndTime=end, ScanBy="TimestampAscending",
        )
    except Exception as exc:  # noqa: BLE001 - latency is an optional panel
        logger.warning("Release ledger: CloudWatch latency read failed: %s", type(exc).__name__)
        return {"available": False, "metrics": {}}

    raw: Dict[str, Dict[str, Optional[float]]] = {name: {} for name in METRICS}
    for result in response.get("MetricDataResults", []):
        name, _, stat = str(result.get("Label", "")).partition("|")
        if name not in raw:
            continue
        values = result.get("Values") or []
        raw[name][stat] = float(values[0]) if values else None

    metrics: Dict[str, Dict[str, Any]] = {}
    for name, stats in raw.items():
        n = int(stats.get("SampleCount") or 0)
        enough = n >= MIN_SHARE_N
        metrics[name] = {
            "p50": round(stats["p50"], 1) if enough and stats.get("p50") is not None else None,
            "p90": round(stats["p90"], 1) if enough and stats.get("p90") is not None else None,
            "n": n,
        }
    return {"available": True, "metrics": metrics}


class _LatencyCache:
    """TTL + single flight, per window (the config-cache pattern, with its own TTL)."""

    def __init__(self) -> None:
        self._entries: Dict[Window, Tuple[float, Latency]] = {}
        self._locks: Dict[Window, asyncio.Lock] = {}

    async def get(self, window: Window, loader: Callable[[Window], Latency]) -> Latency:
        entry = self._entries.get(window)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        lock = self._locks.setdefault(window, asyncio.Lock())
        async with lock:
            entry = self._entries.get(window)
            if entry and entry[0] > time.monotonic():
                return entry[1]
            value = await asyncio.to_thread(loader, window)
            # A failed read is cached briefly, so an outage does not turn every
            # page load into a CloudWatch call, but recovers within minutes.
            ttl = CACHE_TTL_SECONDS if value.get("available") else 300
            self._entries[window] = (time.monotonic() + ttl, value)
            return value

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()


_cache = _LatencyCache()


async def window_latency(window: Window, loader: Optional[Callable[[Window], Latency]] = None) -> Latency:
    """Cached :func:`fetch_window_latency`."""
    return await _cache.get(window, loader or fetch_window_latency)


def clear_cache() -> None:
    _cache.clear()
