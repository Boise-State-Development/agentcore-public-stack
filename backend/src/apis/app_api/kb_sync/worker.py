"""KB sync worker — executes a single policy's sync run.

PR-2 stub: acknowledges the dispatch, records the run as "skipped" (which
also clears the in-flight syncRunStartedAt stamp so the dispatcher never
sees a permanently-fresh stamp from stub runs), and exits. PR-3 replaces
the body with the Drive-file path (metadata-first change detection →
conditional download → stage to S3); PR-4 adds web re-crawl.

Payload contract with the dispatcher:
    {"policyId", "assistantId", "sourceType", "sourceRef"}
"""

import asyncio
import logging
from typing import Any, Dict

from apis.shared.sync_policies.service import record_sync_result

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def run_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = payload["policyId"]
    assistant_id = payload["assistantId"]
    logger.info(
        f"KB sync worker stub: policy {policy_id} ({payload.get('sourceType')}/{payload.get('sourceRef')}) "
        f"on assistant {assistant_id} — recording skipped (sync execution lands in PR-3/PR-4)"
    )
    await record_sync_result(assistant_id, policy_id, "skipped")
    return {"policyId": policy_id, "result": "skipped"}


def lambda_handler(event, context):
    """Async-invoke entry point (InvocationType=Event from the dispatcher)."""
    return asyncio.run(run_sync(event))
