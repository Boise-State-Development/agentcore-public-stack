"""Publisher profiles and per-user eligibility (Agent Marketplace D12).

Attribution motivates publication — people build better Agents when their name is on them
— but an institutional store also needs Agents that speak *as the institution*. Both are
true, so publisher is its own record rather than a projection of ``ownerName``.

Storage follows the ``UserMenuLink`` precedent on the assistants table: a fixed partition
with per-item records carrying an explicit ``order``, sorted on read.

    PK = "AGENT_PUBLISHERS", SK = "PUB#{id}"                    a PublisherProfile
    PK = "AGENT_PUBLISHERS", SK = "ELIG#{publisherId}#{userId}" who may *propose* it

⚠️ Nothing in this module is an access control. ``publisherId`` is display-only, and the
eligibility items are a proposal allowlist for the submit dialog — an admin may set any
publisher on any listing regardless of them (D12). ``ownerId`` continues to govern edit
rights and Skills v2 invoke-through resolution. This is the same trap as ``allowedAppRoles``
on a resource: a display projection that looks like a grant will eventually be read as one,
so it must never reach an access check.
"""

import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from .models import PublisherProfile
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

_PARTITION = "AGENT_PUBLISHERS"


def _now() -> str:
    return utc_now_iso()


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _individual_id(user_id: str) -> str:
    """Deterministic id for a user's own individual profile.

    Deterministic so first-submission auto-creation is idempotent: a second submission
    finds the existing profile instead of minting a duplicate under a new uuid. The user
    id is slugified because it lands in a sort key.
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", user_id).strip("-").lower()
    return f"user-{slug}" if slug else f"user-{uuid.uuid4().hex[:12]}"


# ── profiles ─────────────────────────────────────────────────────────────────────────
async def list_publishers(enabled_only: bool = False) -> List[PublisherProfile]:
    """All publisher profiles, ordered by ``(order, label)``."""
    from boto3.dynamodb.conditions import Key

    table = _table()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_PARTITION) & Key("SK").begins_with("PUB#")
    }
    response = table.query(**kwargs)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    profiles = [PublisherProfile.model_validate(_strip_keys(i)) for i in items]
    if enabled_only:
        profiles = [p for p in profiles if p.enabled]
    profiles.sort(key=lambda p: (p.order, p.label.lower()))
    return profiles


def _strip_keys(item: dict) -> dict:
    """Drop the DynamoDB key attributes before validating into the wire model."""
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


async def get_publisher(publisher_id: str) -> Optional[PublisherProfile]:
    response = _table().get_item(Key={"PK": _PARTITION, "SK": f"PUB#{publisher_id}"})
    item = response.get("Item")
    return PublisherProfile.model_validate(_strip_keys(item)) if item else None


async def put_publisher(profile: PublisherProfile) -> PublisherProfile:
    """Create or replace a publisher profile."""
    item = profile.model_dump(by_alias=True, exclude_none=True)
    item["PK"] = _PARTITION
    item["SK"] = f"PUB#{profile.id}"
    _table().put_item(Item=item)
    logger.info(f"📛 Wrote publisher profile {profile.id} ({profile.kind})")
    return profile


async def publisher_in_use(publisher_id: str) -> bool:
    """Whether any agent's listing is still attributed to this publisher.

    Guards delete, mirroring ``categories.category_in_use`` — same shape, same reason, and
    a scan is right here for the same reason it is there: a human clicking a button, and
    the alternative is a stranded reference nothing can render.

    Listings store the publisher *id*, so deleting a profile that is still referenced does
    not fail loudly — it silently unattributes every listing pointing at it, including live
    published ones, which then render as "Unattributed" with no admin surface to repair
    them. Disabling is the operation that was almost always meant.
    """
    from .listing_repository import list_by_state

    return any(
        (item.get("listing") or {}).get("publisherId") == publisher_id
        for item in await list_by_state()
    )


async def delete_publisher(publisher_id: str) -> None:
    """Delete a publisher profile and every eligibility item pointing at it.

    Callers must check ``publisher_in_use`` first; this does not re-check, so that the
    refusal reads as one HTTP concern in the route rather than an exception type here.
    """
    table = _table()
    table.delete_item(Key={"PK": _PARTITION, "SK": f"PUB#{publisher_id}"})
    for user_id in await list_eligibility(publisher_id):
        table.delete_item(Key={"PK": _PARTITION, "SK": f"ELIG#{publisher_id}#{user_id}"})


async def ensure_individual_profile(user_id: str, display_name: str) -> PublisherProfile:
    """Return the author's own individual profile, creating it on first submission (D12).

    Created ``verified: False`` — individual profiles are never verified; that mark means
    "a university team stands behind this". Comes with an eligibility item for that author
    alone, so they can propose themselves and nobody else can propose as them.
    """
    publisher_id = _individual_id(user_id)
    existing = await get_publisher(publisher_id)
    if existing:
        return existing

    now = _now()
    profile = PublisherProfile(
        id=publisher_id,
        label=display_name or user_id,
        kind="individual",
        verified=False,
        order=100,  # individuals sort below curated institution/department profiles
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    await put_publisher(profile)
    await add_eligibility(publisher_id, user_id)
    return profile


# ── eligibility (proposal allowlist only — never an access check) ────────────────────
async def list_eligibility(publisher_id: str) -> List[str]:
    """User ids who may *propose* this publisher at submission."""
    from boto3.dynamodb.conditions import Key

    prefix = f"ELIG#{publisher_id}#"
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_PARTITION) & Key("SK").begins_with(prefix)
    }
    table = _table()
    response = table.query(**kwargs)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return sorted(str(i["SK"])[len(prefix):] for i in items)


async def add_eligibility(publisher_id: str, user_id: str) -> None:
    _table().put_item(
        Item={
            "PK": _PARTITION,
            "SK": f"ELIG#{publisher_id}#{user_id}",
            "publisherId": publisher_id,
            "userId": user_id,
            "createdAt": _now(),
        }
    )


async def set_eligibility(publisher_id: str, user_ids: List[str]) -> List[str]:
    """Replace the eligibility set for a publisher; returns the resulting ids."""
    current = set(await list_eligibility(publisher_id))
    target = {u for u in user_ids if u}
    table = _table()

    for user_id in target - current:
        await add_eligibility(publisher_id, user_id)
    for user_id in current - target:
        table.delete_item(Key={"PK": _PARTITION, "SK": f"ELIG#{publisher_id}#{user_id}"})

    return sorted(target)


async def list_publishers_for_user(user_id: str) -> List[str]:
    """Publisher ids this user may propose (their own, plus any an admin granted).

    Used only to populate and validate the submit dialog's picker. An admin's assignment
    path does not consult it.
    """
    from boto3.dynamodb.conditions import Key

    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_PARTITION) & Key("SK").begins_with("ELIG#"),
        "FilterExpression": "userId = :uid",
        "ExpressionAttributeValues": {":uid": user_id},
    }
    table = _table()
    response = table.query(**kwargs)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return sorted({str(i["publisherId"]) for i in items if i.get("publisherId")})
