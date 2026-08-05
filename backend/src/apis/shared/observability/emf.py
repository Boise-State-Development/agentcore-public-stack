"""CloudWatch Embedded Metric Format (EMF) emission for prompt-cache metrics.

EMF turns a structured log line into CloudWatch metrics with no SDK calls,
no batching agent, and no extra IAM: CloudWatch Logs extracts any log event
whose message is a JSON object carrying the ``_aws.CloudWatchMetrics``
directive. Both compute surfaces already ship stdout to CloudWatch Logs
(inference-api via its AgentCore Runtime log group, app-api via ECS awslogs),
so a raw JSON line on stdout is all that's needed.

The line must be *exactly* the JSON object — a ``[INFO] logger-name:`` prefix
from the app's standard formatter would break extraction — so this module
uses a dedicated non-propagating logger with a message-only formatter.
"""

import json
import logging
import os
import sys
import time
from typing import Optional

_EMF_NAMESPACE = os.environ.get("EMF_NAMESPACE", "AgentCoreStack/PromptCache")

# Dedicated raw-JSON stdout logger. propagate=False keeps the app-level
# formatter (and its non-JSON prefixes) away from these lines.
_emf_logger = logging.getLogger("apis.shared.observability.emf.raw")
if not _emf_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _emf_logger.addHandler(_handler)
    _emf_logger.setLevel(logging.INFO)
    _emf_logger.propagate = False

logger = logging.getLogger(__name__)


def emit_prompt_cache_metrics(
    cache_read_tokens: int,
    cache_write_tokens: int,
    avoidable_miss: bool,
    wasted_usd: float = 0.0,
    model_id: Optional[str] = None,
    session_id: Optional[str] = None,
    cache_status: Optional[str] = None,
    agent_switched: bool = False,
    partial_miss: bool = False,
) -> None:
    """Emit one EMF record for a completed model call.

    Metrics (no dimensions — fleet-wide sums are the alerting target; the
    per-model/per-session detail rides along as queryable log properties):

    - ``CacheReadTokens`` / ``CacheWriteTokens``: fleet cache traffic; their
      ratio is the cache-efficiency dashboard line.
    - ``AvoidableMiss``: count of calls classified ``miss_avoidable`` — the
      alarm target (a prefix-stability regression shows up as a step change).
    - ``PartialMiss``: count of calls classified ``partial_miss`` — a leading
      segment hit while the rest of the prefix was re-written against a live
      entry. Its own metric rather than a roll-in to ``AvoidableMiss`` so the
      existing alarm keeps its meaning and the two failure shapes (cold prefix
      vs. sliver-hit prefix) stay separable.
    - ``PartialMissUsd``: the subset of ``WastedUsd`` that ``PartialMiss``
      calls contributed.
    - ``WastedUsd``: dollars attributed to re-writes of already-cached prefix
      bytes — ``miss_avoidable`` **and** ``partial_miss``, since the dollars
      are identical and the north-star waste metric wants them together.
    - ``AgentSwitchMiss``: the subset of ``AvoidableMiss`` explained by the turn
      running on a different Agent than the one before it — an ``@``-mention
      (Marketplace D11) genuinely re-writes the prefix. Emitted as its own metric
      rather than as a dimension on ``AvoidableMiss`` so the alarm target stays a
      single fleet-wide sum: subtract it to get unexplained waste, which is the
      number that should never step-change. `turnAgentId` rides along as a
      property, not a dimension, so per-Agent detail is queryable without
      multiplying metric streams.

    Best-effort: never raises.
    """
    try:
        record = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": _EMF_NAMESPACE,
                        "Dimensions": [[]],
                        "Metrics": [
                            {"Name": "CacheReadTokens", "Unit": "Count"},
                            {"Name": "CacheWriteTokens", "Unit": "Count"},
                            {"Name": "AvoidableMiss", "Unit": "Count"},
                            {"Name": "PartialMiss", "Unit": "Count"},
                            {"Name": "AgentSwitchMiss", "Unit": "Count"},
                            {"Name": "WastedUsd", "Unit": "None"},
                            {"Name": "PartialMissUsd", "Unit": "None"},
                        ],
                    }
                ],
            },
            "CacheReadTokens": int(cache_read_tokens or 0),
            "CacheWriteTokens": int(cache_write_tokens or 0),
            "AvoidableMiss": 1 if avoidable_miss else 0,
            "PartialMiss": 1 if partial_miss else 0,
            "AgentSwitchMiss": 1 if (avoidable_miss and agent_switched) else 0,
            "WastedUsd": round(float(wasted_usd or 0.0), 6),
            "PartialMissUsd": round(float(wasted_usd or 0.0), 6) if partial_miss else 0.0,
        }
        if model_id:
            record["modelId"] = model_id
        if session_id:
            record["sessionId"] = session_id
        if cache_status:
            record["cacheStatus"] = cache_status
        _emf_logger.info(json.dumps(record, separators=(",", ":")))
    except Exception as e:  # noqa: BLE001 - metrics must never break a request
        logger.debug("EMF emission skipped: %s", e)


def emit_session_cache_rollup(
    session_id: str,
    partial_miss_usd: float,
    partial_miss_count: int = 0,
) -> None:
    """Emit one session's *running* partial-miss waste after a rollup bump.

    The per-call metrics above are fleet sums, which is the wrong shape for
    "one conversation is quietly burning a user's month": $0.43 a turn never
    steps a fleet-wide sum, and the incident that motivated this ran for five
    days without tripping anything. This metric carries the session's
    cumulative ``partialMissUsd`` so an alarm on ``Maximum`` answers "is any
    single session over the line right now", without making ``sessionId`` a
    dimension (unbounded cardinality) — it stays a queryable log property, so
    the alarm says *that* a session crossed and Logs Insights says *which*.

    The value is session-lifetime cumulative, not a trailing 24h window: an
    alarm period of 24h therefore reads as "a session at/over the threshold
    was active in the last day", which is the operational question. It
    resolves on its own once the session goes quiet.

    Callers should skip zero — a metric emitted on every call would be almost
    entirely zeros, and ``Maximum`` over them tells no one anything.

    Best-effort: never raises.
    """
    try:
        record = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": _EMF_NAMESPACE,
                        "Dimensions": [[]],
                        "Metrics": [
                            {"Name": "SessionPartialMissUsd", "Unit": "None"},
                        ],
                    }
                ],
            },
            "SessionPartialMissUsd": round(float(partial_miss_usd or 0.0), 6),
            "sessionId": session_id,
            "sessionPartialMissCount": int(partial_miss_count or 0),
        }
        _emf_logger.info(json.dumps(record, separators=(",", ":")))
    except Exception as e:  # noqa: BLE001 - metrics must never break a request
        logger.debug("Session cache rollup EMF emission skipped: %s", e)
