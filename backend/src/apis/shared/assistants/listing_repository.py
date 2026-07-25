"""Persistence for the Agent Marketplace ``listing`` block and the sparse GSI5 keys.

This is deliberately a *separate* write path from ``service.update_assistant``, for two
reasons that both bite if ignored:

1. **The generic update cannot own the directory keys.** ``Assistant`` is ``extra="allow"``
   and reads hydrate straight from the raw DynamoDB item, so ``GSI_PK``/``GSI2_SK``/… come
   back as extra model fields and ``_update_assistant_cloud`` re-writes every attribute
   not in its ``immutable_fields`` set. GSI5 is listed there precisely so a routine author
   edit can never resurrect a directory key on a delisted agent. Only this module writes
   them.
2. **Admin edits are not owner edits.** ``update_assistant`` gates on
   ``get_assistant(id, owner_id)``, an ownership check a reviewer fails by definition
   (D13 exists so an admin can fix a tagline without the author). The authorization for
   these writes lives in the service layer, not in an ownership check here.

Every write puts the listing state and its index keys in **one** ``update_item``, so the
two can never disagree — a published listing always has keys and an unpublished one never
does, with no window in between.
"""

import base64
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from .listing import gsi5_keys
from .models import AgentListing
from .serialization import from_ddb, to_ddb_safe

logger = logging.getLogger(__name__)

# Attributes this module owns. Nothing else writes them.
_GSI5_ATTRS = ("GSI5_PK", "GSI5_SK")


def _table():
    """Bind the assistants table, or raise if the environment is not configured."""
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _key(agent_id: str) -> Dict[str, str]:
    return {"PK": f"AST#{agent_id}", "SK": "METADATA"}


async def write_listing(
    agent_id: str,
    listing: AgentListing,
    created_at: str,
    *,
    tagline: Optional[str] = None,
    icon_key: Optional[str] = None,
    name: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> None:
    """Persist a listing block and reconcile the sparse directory keys in one write.

    ``created_at`` is the Agent's own creation timestamp — it is the GSI5 sort key, which
    is why browse is newest-first. The optional presentation fields let an admin D13 edit
    ride along in the same call rather than racing a second update.

    Raises ``ValueError`` (via the conditional check) if the agent no longer exists.
    """
    from botocore.exceptions import ClientError

    keys = gsi5_keys(listing.state, listing.category, created_at)

    set_parts = ["listing = :listing"]
    values: Dict[str, Any] = {
        ":listing": to_ddb_safe(listing.model_dump(by_alias=True, exclude_none=True))
    }
    names: Dict[str, str] = {}
    remove_parts = []

    if updated_at is not None:
        set_parts.append("updatedAt = :updated_at")
        values[":updated_at"] = updated_at

    if tagline is not None:
        set_parts.append("tagline = :tagline")
        values[":tagline"] = tagline
    if icon_key is not None:
        set_parts.append("iconKey = :icon_key")
        values[":icon_key"] = icon_key
    if name is not None:
        # ``name`` is a DynamoDB reserved word.
        set_parts.append("#name = :name")
        names["#name"] = "name"
        values[":name"] = name

    if keys:
        for attr, value in keys.items():
            set_parts.append(f"{attr} = :{attr.lower()}")
            values[f":{attr.lower()}"] = value
    else:
        # Not published → the keys must not exist. REMOVE is unconditional on purpose:
        # a stale key would keep a delisted agent answerable by the store query, which is
        # the single failure the sparse index is there to make impossible.
        remove_parts.extend(_GSI5_ATTRS)

    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)

    params: Dict[str, Any] = {
        "Key": _key(agent_id),
        "UpdateExpression": expression,
        "ExpressionAttributeValues": values,
        "ConditionExpression": "attribute_exists(PK)",
        "ReturnValues": "NONE",
    }
    if names:
        params["ExpressionAttributeNames"] = names

    try:
        _table().update_item(**params)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValueError(f"Agent not found: {agent_id}") from e
        logger.error(f"Failed to write listing for {agent_id}: {e}")
        raise

    logger.info(
        f"📇 Listing for {agent_id} → {listing.state} "
        f"({'indexed ' + keys['GSI5_PK'] if keys else 'not indexed'})"
    )


async def clear_listing(agent_id: str) -> None:
    """Remove the listing block and the directory keys entirely.

    Only for an agent whose listing should return to "never submitted". Not used by the
    author's unpublish path — that moves to ``private``, which is a different thing: a
    record that *has* been through review and carries its history.
    """
    from botocore.exceptions import ClientError

    try:
        _table().update_item(
            Key=_key(agent_id),
            UpdateExpression="REMOVE listing, " + ", ".join(_GSI5_ATTRS),
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="NONE",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValueError(f"Agent not found: {agent_id}") from e
        logger.error(f"Failed to clear listing for {agent_id}: {e}")
        raise


async def query_store(
    category: str, *, limit: int = 50, cursor: Optional[str] = None
) -> Tuple[list, Optional[str]]:
    """Browse one category's shelf, newest first (Phase 2).

    The *only* user-facing read of the marketplace, and it is a pure GSI5 query — no
    scan, no filter, and no state check. It cannot return an unpublished agent because
    an unpublished agent has no key in this index; that is the whole point of keeping
    the index sparse rather than filtering on ``listing.state`` after the fact.

    ``ScanIndexForward=False`` gives newest-first, since ``GSI5_SK`` is
    ``CREATED#{created_at}``. There is no popularity sort — the store front is the
    manual ranking lever instead (see the spec's ranking caveat).
    """
    from boto3.dynamodb.conditions import Key

    params: Dict[str, Any] = {
        "IndexName": "AgentDirectoryIndex",
        "KeyConditionExpression": Key("GSI5_PK").eq(f"LISTED#{category}"),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            params["ExclusiveStartKey"] = decoded

    response = _table().query(**params)
    items = [from_ddb(item) for item in response.get("Items", [])]
    next_cursor = _encode_cursor(response.get("LastEvaluatedKey"))
    return items, next_cursor


def _encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Opaque pagination cursor over a DynamoDB LastEvaluatedKey."""
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key, default=str).encode()).decode()


def _decode_cursor(cursor: str) -> Optional[Dict[str, Any]]:
    """Decode a cursor, treating anything malformed as "start from the beginning".

    A bad cursor is a client bug or a hand-edited URL, not something worth 500ing over —
    the honest degradation is the first page.
    """
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        logger.warning("Ignoring malformed store cursor")
        return None


async def list_by_state(state: Optional[str] = None) -> list:
    """Every agent carrying a listing block, optionally filtered to one state.

    Backs the admin Review queue and Listings tables. This is a table scan with a filter
    on ``attribute_exists(listing)`` — correct for the admin surface, where the population
    is the handful of agents anyone has ever submitted and the caller is a human clicking
    a nav item. The *user-facing* browse query is the GSI5 read (Phase 2) and never scans.
    """
    from botocore.exceptions import ClientError

    filter_expr = "attribute_exists(listing) AND SK = :sk"
    values: Dict[str, Any] = {":sk": "METADATA"}
    if state:
        filter_expr += " AND listing.#st = :state"
        values[":state"] = state

    params: Dict[str, Any] = {
        "FilterExpression": filter_expr,
        "ExpressionAttributeValues": values,
    }
    if state:
        params["ExpressionAttributeNames"] = {"#st": "state"}

    items = []
    try:
        table = _table()
        response = table.scan(**params)
        items.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = table.scan(**params, ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
    except ClientError as e:
        logger.error(f"Failed to list listings: {e}")
        raise

    return [from_ddb(item) for item in items]
