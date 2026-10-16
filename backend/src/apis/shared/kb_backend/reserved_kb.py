"""Lazily provision a platform-owned (reserved id) managed knowledge base.

One knowledge base per environment, created by the first writer that needs it
through the shared ``provision_managed_kb`` saga, with the reserved id as both
``assistant_id`` and ``app_kb_id`` (:mod:`~apis.shared.kb_backend.reserved`). The
conversations consumer (``apis/app_api/conversation_index/consumer.py``) wrote this
shape first; the large-attachments pipeline
(``apis/shared/files/attachment_index.py``) is the second caller, so the shape
lives here.

* Already provisioned: one record read, nothing else.
* A record without both AWS ids: another invocation is provisioning (or one
  crashed mid-way). Wait for it; if it never finishes, resume through
  ``provision_managed_kb``, which reuses the record's persisted token.
* No record: provision. Losing the record race raises ``ProvisioningInProgress``,
  and the loser waits for the winner rather than creating a second knowledge base.

Measured 47–124 s to ACTIVE, then a ~65 s first-ingest warm-up the caller does not
wait for (ingestion is accepted asynchronously). Raises when the knowledge base is
still not ready at the end, so the caller's message is retried rather than
ingesting into nothing.

Stdlib only at module scope (``tests/architecture/test_kb_backend_boundary.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: Opaque owner recorded on the KB_Record and its ``ManagedKbOwnerUserId`` tag.
#: A reserved knowledge base belongs to the platform, not to a user.
RESERVED_KB_OWNER = "system"

#: Covers the measured 124 s worst case to ACTIVE plus the data source.
PROVISIONING_WAIT_SECONDS = 360.0
PROVISIONING_POLL_SECONDS = 5.0


def is_provisioned(record: Optional[Mapping[str, Any]]) -> bool:
    return bool(record) and bool(record.get("awsKbId")) and bool(record.get("awsDataSourceId"))


async def _wait_until_provisioned(
    kb_id: str,
    sleep: Callable[[float], Awaitable[None]],
    wait_seconds: float,
    poll_seconds: float,
) -> bool:
    from apis.shared.kb_backend.records import get_kb_record

    waited = 0.0
    while True:
        record = await asyncio.to_thread(get_kb_record, kb_id, kb_id)
        if is_provisioned(record):
            return True
        if waited >= wait_seconds:
            return False
        await sleep(poll_seconds)
        waited += poll_seconds


async def ensure_reserved_kb(
    kb_id: str,
    client: Any,
    *,
    purpose_tags: Optional[Dict[str, str]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wait_seconds: float = PROVISIONING_WAIT_SECONDS,
    poll_seconds: float = PROVISIONING_POLL_SECONDS,
    provision_kwargs: Optional[Dict[str, Any]] = None,
) -> None:
    """Make sure the reserved knowledge base ``kb_id`` exists, creating it at most once."""
    from apis.shared.kb_backend.provisioning import ProvisioningInProgress, provision_managed_kb
    from apis.shared.kb_backend.records import get_kb_record

    record = await asyncio.to_thread(get_kb_record, kb_id, kb_id)
    if is_provisioned(record):
        return
    if record and await _wait_until_provisioned(kb_id, sleep, wait_seconds, poll_seconds):
        return

    try:
        provisioned = await provision_managed_kb(
            kb_id,
            kb_id,
            RESERVED_KB_OWNER,
            client=client,
            extra_tags=purpose_tags or {},
            sleep=sleep,
            **(provision_kwargs or {}),
        )
    except ProvisioningInProgress:
        logger.info(f"knowledge base {kb_id} is being provisioned by another invocation; waiting")
        if not await _wait_until_provisioned(kb_id, sleep, wait_seconds, poll_seconds):
            raise
        return
    if provisioned.created:
        logger.info(f"provisioned the {kb_id} knowledge base {provisioned.aws_kb_id}")


__all__ = [
    "PROVISIONING_POLL_SECONDS",
    "PROVISIONING_WAIT_SECONDS",
    "RESERVED_KB_OWNER",
    "ensure_reserved_kb",
    "is_provisioned",
]
