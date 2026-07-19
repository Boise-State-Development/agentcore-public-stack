"""Backfill: collapse the two artifact catalog rows into one "Artifacts" toggle.

Artifacts shipped as two independent ``protocol: local`` catalog rows —
``create_artifact`` and ``update_artifact`` — so the tool picker listed them as
two unrelated entries. They are now a single capability following the
Word-document idiom: one catalog row (``create_artifact``, displayName
"Artifacts") whose id is the gate key, and enabling it injects *both* the
create and update tools at runtime. See ``ARTIFACT_TOOL_IDS`` /
``_build_artifact_tools`` in ``apis/inference_api/chat/routes.py``.

``seed_default_tools`` is create-only (it skips rows that already exist), so a
seed run does nothing to an environment that was seeded before this change.
This script performs the migration:

1. **Retitle** ``TOOL#create_artifact`` to the merged displayName/description.
2. **Promote** every role that grants ``update_artifact`` so it also grants
   ``create_artifact`` — a role holding *only* the retired id would otherwise
   silently lose artifact access. Both representations are updated: the
   ``TOOL_GRANT#`` mapping item (GSI2/ToolRoleMappingIndex) and the
   ``grantedTools`` / ``effectivePermissions.tools`` arrays on ``DEFINITION``.
3. **Promote** user tool preferences the same way. ``toolPreferences`` is a
   sparse override map: an explicit *enable* of the retired id carries over to
   ``create_artifact`` (create wins when both keys are set), and the retired
   key is dropped either way. An explicit *disable* does not carry over —
   someone who switched update off while leaving create at its default-on never
   asked to lose artifacts entirely.
4. **Promote** assistant tool bindings (``kind == "tool"``, ``ref ==
   "update_artifact"``) to ``create_artifact``, de-duplicating.
5. **Delete** the ``TOOL#update_artifact`` catalog row and every
   ``TOOL_GRANT#update_artifact`` mapping item.

Note the promote-before-delete ordering: nothing is removed until its
replacement is in place, so an aborted run degrades to "both ids granted",
never to "neither".

OUT OF SCOPE
------------
Schedule snapshots (``enabledTools`` on the sessions-metadata table) are left
alone. A stale ``update_artifact`` entry there is an inert no-op — the runtime
only reads ``create_artifact`` — and any snapshot taken while both rows were
seeded default-on already carries ``create_artifact`` alongside it.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to actually write.
* **Idempotent + re-runnable.** A second run finds nothing to do.
* **Scoped.** ``--assistants-table`` is optional; omit it to skip step 4.

Run against dev first, then prod::

    AWS_PROFILE=dev-ai python backend/scripts/backfill_artifact_tool_merge.py \\
        --table dev-boisestateai-v2-app-roles \\
        --assistants-table dev-boisestateai-v2-assistants          # dry-run
    AWS_PROFILE=dev-ai python backend/scripts/backfill_artifact_tool_merge.py \\
        --table dev-boisestateai-v2-app-roles \\
        --assistants-table dev-boisestateai-v2-assistants --apply
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_artifact_tool_merge")

KEEP_ID = "create_artifact"
RETIRED_ID = "update_artifact"

MERGED_DISPLAY_NAME = "Artifacts"
MERGED_DESCRIPTION = (
    "Save standalone HTML or Markdown documents as versioned artifacts the "
    "user can open and iterate on. Updates create a new immutable version."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def retitle_catalog_row(table: Any, apply: bool) -> int:
    """Point TOOL#create_artifact at the merged name/description."""
    resp = table.get_item(Key={"PK": f"TOOL#{KEEP_ID}", "SK": "METADATA"})
    item = resp.get("Item")
    if not item:
        logger.warning(f"TOOL#{KEEP_ID} not found — nothing to retitle")
        return 0
    if item.get("displayName") == MERGED_DISPLAY_NAME:
        logger.info(f"TOOL#{KEEP_ID} already retitled — skipped")
        return 0

    logger.info(
        f"retitle TOOL#{KEEP_ID}: {item.get('displayName')!r} -> {MERGED_DISPLAY_NAME!r}"
    )
    if apply:
        table.update_item(
            Key={"PK": f"TOOL#{KEEP_ID}", "SK": "METADATA"},
            UpdateExpression=(
                "SET displayName = :dn, description = :d, updatedAt = :u"
            ),
            ExpressionAttributeValues={
                ":dn": MERGED_DISPLAY_NAME,
                ":d": MERGED_DESCRIPTION,
                ":u": _now(),
            },
        )
    return 1


def _roles_granting(table: Any, tool_id: str) -> List[Dict[str, Any]]:
    """Every TOOL_GRANT mapping item for a tool, via ToolRoleMappingIndex."""
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "IndexName": "ToolRoleMappingIndex",
        "KeyConditionExpression": Key("GSI2PK").eq(f"TOOL#{tool_id}"),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def promote_role_grants(table: Any, apply: bool) -> int:
    """Ensure every role granting the retired id also grants the keeper."""
    changed = 0
    for mapping in _roles_granting(table, RETIRED_ID):
        role_pk = mapping["PK"]
        role_id = mapping.get("roleId", role_pk)

        definition = table.get_item(Key={"PK": role_pk, "SK": "DEFINITION"}).get("Item")
        if not definition:
            logger.warning(f"{role_pk}: grant exists but DEFINITION missing — skipped")
            continue

        granted: List[str] = list(definition.get("grantedTools") or [])
        effective = definition.get("effectivePermissions") or {}
        effective_tools: List[str] = list(effective.get("tools") or [])

        # A wildcard already covers the keeper; only the arrays need pruning.
        needs_keeper = KEEP_ID not in granted and "*" not in granted

        if needs_keeper:
            logger.info(f"{role_id}: grant {KEEP_ID} (held only {RETIRED_ID})")
            if apply:
                table.put_item(
                    Item={
                        "PK": role_pk,
                        "SK": f"TOOL_GRANT#{KEEP_ID}",
                        "GSI2PK": f"TOOL#{KEEP_ID}",
                        "GSI2SK": role_pk,
                        "roleId": mapping.get("roleId"),
                        "displayName": mapping.get("displayName"),
                        "enabled": mapping.get("enabled", True),
                    }
                )
            granted.append(KEEP_ID)
            if "*" not in effective_tools:
                effective_tools.append(KEEP_ID)

        new_granted = [t for t in granted if t != RETIRED_ID]
        new_effective = [t for t in effective_tools if t != RETIRED_ID]

        if new_granted != (definition.get("grantedTools") or []) or new_effective != (
            effective.get("tools") or []
        ):
            logger.info(f"{role_id}: grantedTools -> {new_granted}")
            if apply:
                effective["tools"] = new_effective
                table.update_item(
                    Key={"PK": role_pk, "SK": "DEFINITION"},
                    UpdateExpression=(
                        "SET grantedTools = :g, effectivePermissions = :e, updatedAt = :u"
                    ),
                    ExpressionAttributeValues={
                        ":g": new_granted,
                        ":e": effective,
                        ":u": _now(),
                    },
                )
            changed += 1

        logger.info(f"{role_id}: delete TOOL_GRANT#{RETIRED_ID}")
        if apply:
            table.delete_item(Key={"PK": role_pk, "SK": f"TOOL_GRANT#{RETIRED_ID}"})
    return changed


def _scan(table: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def promote_user_preferences(table: Any, apply: bool) -> int:
    """Carry an explicit opinion on the retired id over to the keeper."""
    changed = 0
    # `contains` matches strings/sets only, never Map keys — toolPreferences is
    # a Map, so this has to be a nested-path existence check.
    rows = _scan(
        table,
        FilterExpression=Attr("SK").eq("TOOL_PREFERENCES")
        & Attr(f"toolPreferences.{RETIRED_ID}").exists(),
    )
    for row in rows:
        prefs: Dict[str, Any] = dict(row.get("toolPreferences") or {})
        if RETIRED_ID not in prefs:
            continue

        retired_value = prefs.pop(RETIRED_ID)
        # Create wins when the user has an opinion on both. Otherwise only a
        # *True* carries over: someone who turned update off while leaving
        # create at its default-on never asked to lose artifacts, so dropping
        # the key (falling back to enabledByDefault) is the faithful read.
        if KEEP_ID not in prefs and retired_value:
            prefs[KEEP_ID] = True

        logger.info(f"{row['PK']}: toolPreferences -> {prefs}")
        if apply:
            table.update_item(
                Key={"PK": row["PK"], "SK": "TOOL_PREFERENCES"},
                UpdateExpression="SET toolPreferences = :p, updatedAt = :u",
                ExpressionAttributeValues={":p": prefs, ":u": _now()},
            )
        changed += 1
    return changed


def promote_assistant_bindings(table: Any, apply: bool) -> int:
    """Rewrite tool bindings on the retired id to the keeper, de-duplicated."""
    changed = 0
    rows = _scan(table, FilterExpression=Attr("SK").eq("METADATA") & Attr("bindings").exists())
    for row in rows:
        bindings: List[Dict[str, Any]] = list(row.get("bindings") or [])
        if not any(
            b.get("kind") == "tool" and b.get("ref") == RETIRED_ID for b in bindings
        ):
            continue

        has_keeper = any(
            b.get("kind") == "tool" and b.get("ref") == KEEP_ID for b in bindings
        )
        new_bindings: List[Dict[str, Any]] = []
        for b in bindings:
            if b.get("kind") == "tool" and b.get("ref") == RETIRED_ID:
                if has_keeper:
                    continue  # keeper already bound — just drop the retired one
                new_bindings.append({**b, "ref": KEEP_ID})
                has_keeper = True
                continue
            new_bindings.append(b)

        logger.info(f"{row['PK']}: bindings {RETIRED_ID} -> {KEEP_ID}")
        if apply:
            table.update_item(
                Key={"PK": row["PK"], "SK": "METADATA"},
                UpdateExpression="SET bindings = :b, updatedAt = :u",
                ExpressionAttributeValues={":b": new_bindings, ":u": _now()},
            )
        changed += 1
    return changed


def delete_retired_catalog_row(table: Any, apply: bool) -> int:
    resp = table.get_item(Key={"PK": f"TOOL#{RETIRED_ID}", "SK": "METADATA"})
    if "Item" not in resp:
        logger.info(f"TOOL#{RETIRED_ID} already absent — skipped")
        return 0
    logger.info(f"delete TOOL#{RETIRED_ID}")
    if apply:
        table.delete_item(Key={"PK": f"TOOL#{RETIRED_ID}", "SK": "METADATA"})
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="app-roles table name")
    parser.add_argument(
        "--assistants-table",
        help="assistants table name; omit to skip the binding rewrite",
    )
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--apply", action="store_true", help="actually write")
    args = parser.parse_args()

    if not args.apply:
        logger.info("DRY RUN — pass --apply to write")

    dynamodb = boto3.Session(region_name=args.region).resource("dynamodb")
    table = dynamodb.Table(args.table)

    try:
        retitled = retitle_catalog_row(table, args.apply)
        roles = promote_role_grants(table, args.apply)
        prefs = promote_user_preferences(table, args.apply)
        bindings = 0
        if args.assistants_table:
            bindings = promote_assistant_bindings(
                dynamodb.Table(args.assistants_table), args.apply
            )
        else:
            logger.info("--assistants-table not given — skipping binding rewrite")
        deleted = delete_retired_catalog_row(table, args.apply)
    except ClientError as e:
        logger.error(f"aborted: {e}")
        return 1

    logger.info(
        f"done: retitled={retitled} roles={roles} prefs={prefs} "
        f"bindings={bindings} deleted={deleted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
