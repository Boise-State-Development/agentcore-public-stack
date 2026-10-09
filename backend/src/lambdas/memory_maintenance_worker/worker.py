"""Memory-maintenance worker: one maintenance run over a project's shared memory.

Given ``{"spaceId", "runId"}`` from app-api (async-invoked,
``InvocationType=Event``), runs ``MaintenanceRunner`` (Shared Projects 2.6,
``apis/shared/memory/maintenance/runner.py``): snapshot, one planner call per
file, the deterministic verifier, then a compaction proposal per file for an
editor to review. It never changes memory itself.

Lambda retries a failed async invocation twice. The runner only proceeds from
``queued``, so a retry of a run that started is a no-op, and this handler
returns rather than raising, so a run that ended is never retried at all.
"""

import logging
from typing import Any, Dict

from apis.shared.memory.maintenance.runner import MaintenanceRunner, remaining_from_lambda

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    space_id = (event or {}).get("spaceId")
    run_id = (event or {}).get("runId")
    if not space_id or not run_id:
        logger.error("memory-maintenance: event without spaceId and runId: %s", sorted((event or {}).keys()))
        return {"result": "invalid_event"}
    try:
        run = MaintenanceRunner().run(space_id, run_id, remaining_seconds=remaining_from_lambda(context))
    except Exception:  # noqa: BLE001 - see the module docstring: never hand Lambda a retry
        logger.exception("memory-maintenance: run %s in space %s raised", run_id, space_id)
        return {"result": "error", "runId": run_id}
    if run is None:
        return {"result": "not_found", "runId": run_id}
    return {"result": run.state, "runId": run_id, "files": len(run.results)}
