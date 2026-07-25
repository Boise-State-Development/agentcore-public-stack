"""Admin-managed store categories (Agent Marketplace D10, Phase 2).

Phase 1 validated ``listing.category`` against a constant. This replaces the source with
per-item records — the ``UserMenuLink`` precedent: a fixed partition, an explicit
``order``, sorted on read — because a category set that requires a deploy to change will
not be maintained.

    PK = "AGENT_CATEGORIES", SK = "CAT#{id}"   { id, label, order, enabled }

⚠️ **A category id is immutable, because it is half of the directory partition key.**
``GSI5_PK = LISTED#{category}`` is written from ``listing.category``, so renaming an id
would strand every published listing in a partition the browse query no longer asks for.
Renaming is therefore a ``label`` change only; the id a listing stores never moves. This
is why the ids seeded below are the exact strings Phase 1 wrote — changing them now would
require rewriting GSI5 keys on live listings for no user-visible gain.

Disabling a category is the soft alternative to deleting one: it drops out of the pickers
and the browse header while its listings keep their partition intact.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .listing import DEFAULT_CATEGORIES
from .models import AgentCategory

logger = logging.getLogger(__name__)

_PARTITION = "AGENT_CATEGORIES"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _strip_keys(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}


async def list_categories(enabled_only: bool = False) -> List[AgentCategory]:
    """All categories, ordered by ``(order, label)``."""
    from boto3.dynamodb.conditions import Key

    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_PARTITION) & Key("SK").begins_with("CAT#")
    }
    table = _table()
    response = table.query(**kwargs)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    categories = [AgentCategory.model_validate(_strip_keys(i)) for i in items]
    if enabled_only:
        categories = [c for c in categories if c.enabled]
    categories.sort(key=lambda c: (c.order, c.label.lower()))
    return categories


async def get_category(category_id: str) -> Optional[AgentCategory]:
    response = _table().get_item(Key={"PK": _PARTITION, "SK": f"CAT#{category_id}"})
    item = response.get("Item")
    return AgentCategory.model_validate(_strip_keys(item)) if item else None


async def put_category(category: AgentCategory) -> AgentCategory:
    item = category.model_dump(by_alias=True, exclude_none=True)
    item["PK"] = _PARTITION
    item["SK"] = f"CAT#{category.id}"
    _table().put_item(Item=item)
    return category


async def delete_category(category_id: str) -> None:
    """Hard-delete a category record.

    The caller is responsible for refusing this while listings still reference it —
    see ``category_in_use``. Deleting a referenced category would leave those listings
    pointing at a partition with no label to render.
    """
    _table().delete_item(Key={"PK": _PARTITION, "SK": f"CAT#{category_id}"})


async def ensure_seeded() -> List[AgentCategory]:
    """Write the default category set once, if no categories exist yet.

    Idempotent bootstrap rather than a migration: the store must never be
    category-less (an author cannot submit without one), and the alternative — a
    deploy-time seeding step per environment — is one more thing to forget. Runs on
    the admin list and store-front reads, both of which are already round trips.

    Seeds the exact strings Phase 1 validated against, so any listing submitted before
    this shipped still resolves and its GSI5 partition is unchanged.
    """
    existing = await list_categories()
    if existing:
        return existing

    now = _now()
    seeded = [
        AgentCategory(id=label, label=label, order=index * 10, enabled=True, created_at=now)
        for index, label in enumerate(DEFAULT_CATEGORIES)
    ]
    for category in seeded:
        await put_category(category)
    logger.info(f"🗂️ Seeded {len(seeded)} default agent categories")
    return seeded


async def category_in_use(category_id: str) -> bool:
    """Whether any agent's listing still references this category.

    Guards delete. A scan is right here for the same reason it is on the admin listings
    table: the caller is a human clicking a button, and the alternative is a stranded
    reference the browse header cannot label.
    """
    from .listing_repository import list_by_state

    return any(
        (item.get("listing") or {}).get("category") == category_id
        for item in await list_by_state()
    )
