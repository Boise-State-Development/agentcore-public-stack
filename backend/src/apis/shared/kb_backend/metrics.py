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


def metric_namespace() -> str:
    """The custom namespace this feature publishes into.

    Falls back to the same default the rest of the backend uses for
    ``PROJECT_PREFIX`` so local runs do not crash; a mismatch in a deployed
    environment shows up as an access-denied warning from :func:`emit_count`
    rather than as silence.
    """
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
    try:
        import boto3

        datum = {"MetricName": metric_name, "Value": value, "Unit": "Count"}
        if dimensions:
            datum["Dimensions"] = [
                {"Name": k, "Value": v} for k, v in sorted(dimensions.items())
            ]
        boto3.client("cloudwatch").put_metric_data(
            Namespace=metric_namespace(), MetricData=[datum]
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break retrieval
        logger.warning(f"Failed to emit {metric_name} metric: {exc}")
