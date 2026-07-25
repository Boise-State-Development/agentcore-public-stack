"""Role-seeded default pins for the Agent Marketplace (D9 role side — Phase 6).

An admin assigns default pinned Agents per ``AppRole``, so a role's members start with a
useful sidebar instead of an empty one. Storage mirrors the grant items on the **app-roles**
table:

    PK = ROLE#{role_id}, SK = AGENT_PIN#{agent_id}
    { order: int, locked: bool, createdAt, createdBy }

Four properties, each of which the rest of the feature leans on:

**1. ⚠️ A pin is not a permission.** These items share a partition with ``TOOL_GRANT#`` /
``MODEL_GRANT#`` / ``SKILL_GRANT#`` and nothing else. They are deliberately absent from
``AppRole``, from ``EffectivePermissions`` and from ``_compute_effective_permissions``;
they do not inherit through ``inheritsFrom``, and they are resolved by the query below
rather than merged into the permission payload the model call path reads.

**2. They live in their own module for that reason.** The obvious home was
``AppRoleRepository``, which already owns this table — and that is exactly the coupling
worth refusing. Keeping the pin read out of the repository that computes permissions makes
"a pin is not a permission" a structural fact rather than a comment.

**3. Resolution is live, never materialized (D9.1).** There is no fan-out job writing a
row per member. Removing a role pin removes it for everyone in the role who has not
independently pinned the Agent — a real capability loss, taken deliberately in exchange for
an entire asynchronous subsystem and its backfill.

**4. The write is a whole-list replace.** ``order`` is a property of the list, not of any
one row, so per-item saves would let two edits interleave into an order nobody chose.
``createdAt``/``createdBy`` survive the replace for ids that stay, so the audit trail
records when an Agent was *seeded*, not when the list was last dragged around.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Sequence

from boto3.dynamodb.conditions import Key

from .models import RoleAgentPin, RoleAgentPinInput

logger = logging.getLogger(__name__)

_SK_PREFIX = "AGENT_PIN#"

# A role that seeds 25 Agents has handed every one of its members a sidebar they did not
# choose and cannot curate down without 25 gestures. The user-side ``MAX_PINS`` is 100
# because that shelf is the user's own; this one is somebody else's, so it is stricter.
MAX_ROLE_PINS = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_APP_ROLES_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _to_pin(item: dict) -> RoleAgentPin:
    return RoleAgentPin(
        agent_id=str(item["SK"])[len(_SK_PREFIX) :],
        order=int(item.get("order", 0)),
        locked=bool(item.get("locked", False)),
        created_at=item.get("createdAt"),
        created_by=item.get("createdBy"),
    )


async def list_role_pins(role_id: str) -> List[RoleAgentPin]:
    """This role's default pins, in seed order.

    Sorted here rather than by the sort key: ``AGENT_PIN#{agent_id}`` orders by id, and the
    admin's ordering is an attribute. Ties break on agent id so two pins written in the
    same save resolve identically on every read.
    """
    response = _table().query(
        KeyConditionExpression=Key("PK").eq(f"ROLE#{role_id}") & Key("SK").begins_with(_SK_PREFIX)
    )
    pins = [_to_pin(item) for item in response.get("Items", [])]
    pins.sort(key=lambda pin: (pin.order, pin.agent_id))
    return pins


async def list_pins_for_roles(role_ids: Sequence[str]) -> Dict[str, List[RoleAgentPin]]:
    """Default pins for several roles at once — one query per role.

    A query per role rather than a scan: a user holds a handful of roles, the result is
    cached upstream behind the AppRole cache TTL, and a table scan on every pin read would
    grow with the number of roles in the institution rather than the number the caller holds.
    """
    result: Dict[str, List[RoleAgentPin]] = {}
    for role_id in role_ids:
        try:
            result[role_id] = await list_role_pins(role_id)
        except Exception:
            # One unreadable role must not empty the whole shelf: the other roles' seeds
            # and the user's own pins are still correct answers.
            logger.warning(f"Failed to read default pins for role {role_id}", exc_info=True)
            result[role_id] = []
    return result


async def put_role_pins(
    role_id: str, pins: Sequence[RoleAgentPinInput], updated_by: str
) -> List[RoleAgentPin]:
    """Replace this role's default pins with ``pins``, in the order given.

    Raises ``ValueError`` past ``MAX_ROLE_PINS`` or on a duplicated agent id — a duplicate
    would collapse to one item on write and silently renumber everything after it.
    """
    agent_ids = [pin.agent_id for pin in pins]
    if len(agent_ids) > MAX_ROLE_PINS:
        raise ValueError(f"A role can seed at most {MAX_ROLE_PINS} agents.")
    if len(set(agent_ids)) != len(agent_ids):
        raise ValueError("The same agent cannot be pinned to a role twice.")

    existing = {pin.agent_id: pin for pin in await list_role_pins(role_id)}
    now = _now()
    table = _table()

    with table.batch_writer() as batch:
        for agent_id in existing:
            if agent_id not in agent_ids:
                batch.delete_item(Key={"PK": f"ROLE#{role_id}", "SK": f"{_SK_PREFIX}{agent_id}"})
        for order, pin in enumerate(pins):
            prior = existing.get(pin.agent_id)
            batch.put_item(
                Item={
                    "PK": f"ROLE#{role_id}",
                    "SK": f"{_SK_PREFIX}{pin.agent_id}",
                    "order": order,
                    "locked": pin.locked,
                    # Preserved across a reorder: this records when the Agent was seeded,
                    # not when the list was last dragged.
                    "createdAt": (prior.created_at if prior else None) or now,
                    "createdBy": (prior.created_by if prior else None) or updated_by,
                    "updatedAt": now,
                }
            )

    logger.info(f"📌 {updated_by} set {len(agent_ids)} default pin(s) on role {role_id}")
    return await list_role_pins(role_id)


async def delete_role_pins(role_id: str) -> None:
    """Drop every default pin for a role. Called when the role itself is deleted."""
    table = _table()
    pins = await list_role_pins(role_id)
    if not pins:
        return
    with table.batch_writer() as batch:
        for pin in pins:
            batch.delete_item(Key={"PK": f"ROLE#{role_id}", "SK": f"{_SK_PREFIX}{pin.agent_id}"})
    logger.info(f"📌 Deleted {len(pins)} default pin(s) with role {role_id}")
