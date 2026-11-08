"""The deploy marker: one row per build a service has run (release-ledger spec §5.2).

The release ledger attributes every cost row to the build that wrote it, but a
timeline needs to know *when* each build went live on each environment, and
the only process that reliably knows it is the service itself at startup. So
app-api writes one ``LEDGER#DEPLOY`` row the first time a build starts, with a
conditional put so the second task of the same build, and every restart after
it, writes nothing. The AgentCore Runtime does not write one: it starts a
microVM per session, and its first ``LEDGER#VERSION`` row is its marker.

Schema (``system-cost-rollup`` table, no TTL)::

    PK: LEDGER#DEPLOY
    SK: {service}#{build}
    service, appVersion, build, firstSeenAt

Content-free by construction (a service name, a version and a hash). A local
run or an image built outside ``scripts/build`` has no build and writes
nothing. Best-effort: a failure is logged and never reaches the caller, and
the whole thing runs after startup, never in front of a request.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apis.shared.build_info import APP_BUILD, APP_VERSION, UNKNOWN

logger = logging.getLogger(__name__)

DEPLOY_PK = "LEDGER#DEPLOY"
ROLLUP_TABLE_ENV = "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME"


def deploy_marker_item(service: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The row this process would write, or ``None`` when it has no build to record."""
    if not service or APP_BUILD == UNKNOWN:
        return None
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "PK": DEPLOY_PK,
        "SK": f"{service}#{APP_BUILD}",
        "service": service,
        "appVersion": APP_VERSION,
        "build": APP_BUILD,
        "firstSeenAt": stamp,
    }


def record_deploy_marker(service: str) -> bool:
    """Write the marker once per build. Returns whether this call wrote it."""
    item = deploy_marker_item(service)
    table_name = os.environ.get(ROLLUP_TABLE_ENV)
    if item is None or not table_name:
        return False
    try:
        from botocore.exceptions import ClientError

        from apis.shared.aws_clients import get_dynamodb_table

        table = get_dynamodb_table(table_name)
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        logger.info("Deploy marker written: %s %s (%s)", service, item["appVersion"], item["build"])
        return True
    except Exception as exc:  # noqa: BLE001 — a marker must never cost a startup
        logger.warning("Deploy marker not written for %s: %s", service, exc)
        return False
