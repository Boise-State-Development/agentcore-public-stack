"""Best-effort custom metrics for the managed knowledge base seam.

Metric publishing is *observability*, never control flow. Every function here
swallows its own failures: a knowledge base search must not fail because
CloudWatch was briefly unavailable, and a missing metric is a monitoring gap, not
a user-facing error. This matches the convention in
``apis/app_api/kb_sync/dispatcher.py``.

Namespace
---------
The namespace must match what the IAM grant allows, or every publish is silently
denied. ``ManagedKbRoleConstruct`` conditions ``cloudwatch:PutMetricData`` on
``cloudwatch:namespace`` equal to ``{projectPrefix}/ManagedKb``, and derives it
from the same ``projectPrefix`` that CDK injects here as ``PROJECT_PREFIX``. The
two must therefore agree; :func:`metric_namespace` is the single reader of that
variable so there is one place to look when they do not.

Not an ``AWS/...`` namespace, deliberately: CloudWatch reserves every namespace
beginning with ``AWS`` for its own services and rejects writes to them, so such a
grant would authorize nothing while looking correct. Bedrock's own
``AWS/Bedrock/KnowledgeBases`` metrics are a read source, never a write target.

Import weight
-------------
``boto3`` is imported inside :func:`emit_count`, so importing this module costs
nothing. The seam is imported by a size-constrained Lambda image.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

#: Emitted when a query was longer than the hard cap and had to be truncated
#: (Requirement 22.3). A non-zero value means users are sending queries that the
#: managed backend would reject outright, which is worth knowing before the
#: engine is switched under them.
METRIC_QUERY_CLAMPED = "KbQueryClamped"

#: Emitted when the document-status filter could not confirm status and therefore
#: dropped chunks (Requirement 22.4). Distinct from an ordinary empty result: this
#: one means retrieval degraded, not that the corpus had no match.
METRIC_STATUS_FILTER_FAIL_CLOSED = "KbStatusFilterFailClosed"

#: Emitted when retrieval was refused because the invoking user's access could not
#: be established (Requirement 25.1). Counts both honest denials and
#: check-failed-so-denied, dimensioned by ``reason`` to keep them apart: the first
#: is the system working, the second is a degradation worth alarming on.
METRIC_ACCESS_DENIED = "KbAccessDenied"

#: Dual-read pilot observations (Requirement 18.3). Values, not counts: the
#: question each answers is "how much do the two backends agree, and at what
#: cost", and a count cannot answer either.
METRIC_DUAL_READ_OVERLAP = "KbDualReadOverlap"
METRIC_DUAL_READ_RANK_CORRELATION = "KbDualReadRankCorrelation"
METRIC_DUAL_READ_LATENCY = "KbDualReadLatency"

#: Emitted when the observational managed read failed. Never a user-facing
#: failure — the turn was served from legacy before the comparison ran — but a
#: sustained non-zero value is the pilot telling us the engine is not ready.
METRIC_DUAL_READ_FAILED = "KbDualReadFailed"

#: Fleet gauges (Requirement 22.1), emitted once per reconciler pass rather than
#: per event, because each is a statement about the whole account.
METRIC_KB_COUNT = "KbCount"
METRIC_KB_STORAGE_GB = "KbStorageGB"
METRIC_KB_IDLE_GB = "KbIdleGB"

#: Days without a sign of life before a knowledge base's bytes count toward
#: :data:`METRIC_KB_IDLE_GB`. A reporting threshold only: nothing reclaims in this
#: phase, and the number the follow-up spec eventually evicts on should be chosen
#: from the distribution this metric records, not inherited from this guess.
IDLE_THRESHOLD_DAYS = 30

#: Bytes per gigabyte, decimal — matching how AWS bills storage ($5.00/GB-month),
#: so a dashboard number and an invoice line can be compared without a conversion
#: nobody remembers to apply.
BYTES_PER_GB = 1_000_000_000


def emit_fleet_gauges(
    kb_count: int,
    stored_bytes: int,
    idle_bytes: int,
    *,
    unmeasured: int = 0,
    idle_threshold_days: Optional[int] = None,
) -> None:
    """Publish the account-wide knowledge base gauges. Never raises.

    Requirement 22.1. Emitted through EMF rather than ``PutMetricData`` because the
    caller is a Lambda whose stdout already reaches CloudWatch Logs, so this needs
    no client, no batching and no IAM — and because these are gauges published once
    per pass, which is exactly the shape EMF is good at. The namespace is the same
    :func:`metric_namespace` the ``PutMetricData`` grant is conditioned on, so both
    mechanisms land in one place and a dashboard does not have to know which code
    path produced a number.

    ``unmeasured`` rides along as a log property, not a metric: it is the count of
    knowledge bases with no recorded activity at all, which is context for reading
    ``KbIdleGB`` rather than something to alarm on. Emitting it as a metric would
    invite an alarm on a number that is legitimately large the day this ships and
    legitimately near zero a month later.
    """
    try:
        from apis.shared.observability.emf import emit_emf_metrics

        emit_emf_metrics(
            metric_namespace(),
            {
                METRIC_KB_COUNT: int(kb_count),
                METRIC_KB_STORAGE_GB: round(stored_bytes / BYTES_PER_GB, 6),
                METRIC_KB_IDLE_GB: round(idle_bytes / BYTES_PER_GB, 6),
            },
            properties={
                "unmeasuredKnowledgeBases": int(unmeasured),
                "idleThresholdDays": int(
                    IDLE_THRESHOLD_DAYS if idle_threshold_days is None else idle_threshold_days
                ),
            },
            units={
                METRIC_KB_COUNT: "Count",
                METRIC_KB_STORAGE_GB: "Gigabytes",
                METRIC_KB_IDLE_GB: "Gigabytes",
            },
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break a sweep
        logger.warning(f"Failed to emit knowledge base fleet gauges: {exc}")


def metric_namespace() -> str:
    """The custom namespace this feature publishes into.

    Prefers ``MANAGED_KB_METRIC_NAMESPACE``, which the CDK construct sets from the
    *same* helper that builds the IAM condition — so where that variable is present
    the grant and the publish cannot disagree. Falls back to deriving from
    ``PROJECT_PREFIX`` for services that do not receive it and for local runs.
    """
    explicit = os.environ.get("MANAGED_KB_METRIC_NAMESPACE")
    if explicit:
        return explicit
    prefix = os.environ.get("PROJECT_PREFIX", "agentcore")
    return f"{prefix}/ManagedKb"


def emit_count(
    metric_name: str,
    value: int = 1,
    dimensions: Optional[Mapping[str, str]] = None,
) -> None:
    """Publish a single count metric. Never raises.

    Swallowing the failure is the point: the caller is on a request path, and a
    metric that cannot be published is strictly less important than the answer the
    user is waiting for.
    """
    _publish(metric_name, value, "Count", dimensions)


def emit_value(
    metric_name: str,
    value: float,
    unit: str = "None",
    dimensions: Optional[Mapping[str, str]] = None,
) -> None:
    """Publish a measurement rather than an occurrence. Never raises.

    Separate from :func:`emit_count` so the unit is a decision at the call site.
    A latency published as ``Count`` is not merely mislabelled — CloudWatch will
    graph and alarm on it as a rate, and the mistake is invisible until somebody
    tries to read the dashboard.
    """
    _publish(metric_name, value, unit, dimensions)


def _publish(
    metric_name: str,
    value: float,
    unit: str,
    dimensions: Optional[Mapping[str, str]],
) -> None:
    try:
        import boto3

        datum: dict = {"MetricName": metric_name, "Value": value, "Unit": unit}
        if dimensions:
            datum["Dimensions"] = [
                {"Name": k, "Value": v} for k, v in sorted(dimensions.items())
            ]
        boto3.client("cloudwatch").put_metric_data(
            Namespace=metric_namespace(), MetricData=[datum]
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break retrieval
        logger.warning(f"Failed to emit {metric_name} metric: {exc}")
