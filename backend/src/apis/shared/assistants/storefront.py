"""The curated store front (Agent Marketplace D10, Phase 5).

    PK = "AGENT_STOREFRONT", SK = "CONFIG"    { featured: [agentId, ...] }

**A single item holding an ordered array**, deliberately unlike categories and publishers
(which are per-item records with an ``order`` field, following ``UserMenuLink``). Two
reasons: the list is short, and reordering has to be atomic — with per-item ``order``
fields a drag that rewrites five rows is five writes and a half-applied order is
reachable. Here the array *is* the order, and one ``put_item`` moves the whole row.

⚠️ **This is the only ranking lever that exists.** ``GSI5_SK`` is ``created_at``, so
everything below the featured row is newest-first; there is no popularity sort and v1
deliberately does not approximate one. Promotion is therefore how a good Agent gets found,
which is exactly why the ordering is admin-owned and explicit rather than derived.

Membership is *not* self-healing here: an id whose listing was later taken down stays in
the array until an admin removes it, and the read paths drop it from what they render.
Pruning on read would quietly rewrite an admin's curation from a GET, and a takedown that
is later reversed would have silently cost the Agent its slot.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List

logger = logging.getLogger(__name__)

_PARTITION = "AGENT_STOREFRONT"
_SK = "CONFIG"

# The featured row is a shelf, not a second store. Ten is the point past which the row
# stops reading as "these are the ones we stand behind".
MAX_FEATURED = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


async def get_featured_ids() -> List[str]:
    """The featured agent ids, in render order. Empty when nothing has been promoted."""
    item = _table().get_item(Key={"PK": _PARTITION, "SK": _SK}).get("Item")
    if not item:
        return []
    return [str(agent_id) for agent_id in item.get("featured", [])]


async def put_featured_ids(agent_ids: List[str], *, updated_by: str) -> List[str]:
    """Replace the featured row.

    De-duplicates while preserving first position — a double-add is an admin slip, not an
    instruction to render the same tile twice — and enforces ``MAX_FEATURED``. The caller
    is responsible for refusing ids that are not published; that check needs the listing
    records and belongs in the service layer, not here.
    """
    deduped: List[str] = []
    for agent_id in agent_ids:
        if agent_id and agent_id not in deduped:
            deduped.append(agent_id)

    if len(deduped) > MAX_FEATURED:
        raise ValueError(
            f"The store front holds at most {MAX_FEATURED} agents; {len(deduped)} were given."
        )

    _table().put_item(
        Item={
            "PK": _PARTITION,
            "SK": _SK,
            "featured": deduped,
            "updatedAt": _now(),
            "updatedBy": updated_by,
        }
    )
    logger.info(f"⭐ Store front set to {len(deduped)} agents by {updated_by}")
    return deduped
