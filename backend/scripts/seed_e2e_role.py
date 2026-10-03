"""Seed the RBAC role the nightly E2E user runs under.

The nightly stack never runs the bootstrap data seeding, so its ``default``
role is the auto-seeded one that grants nothing. That is fine for the
Playwright suite, which exercises chrome and a tool-free chat turn, but the
turn-path smoke matrix (``scripts/nightly/turn-smoke.sh``) needs two tools —
``calculator`` for the tool rows and ``ask_user_question`` for the
interrupt/resume row — and a model to run them on.

This writes one ``e2e_user`` role through ``AppRoleRepository`` (the same
writer the admin console uses, so the DEFINITION row, the ``JWT_MAPPING#``
row the resolver's GSI reads, and the ``TOOL_GRANT#`` / ``MODEL_GRANT#`` rows
all land together), mapped to the Cognito group ``scripts/nightly/seed-e2e-users.sh``
puts the regular E2E user in. Idempotent: create on first run, update after.

Usage (from ``backend/``, with credentials for the stack's account):

    uv run python scripts/seed_e2e_role.py --prefix nightly-e2e-develop
    uv run python scripts/seed_e2e_role.py --prefix <prefix> --dry-run   # print, write nothing

The table name comes from ``DYNAMODB_APP_ROLES_TABLE_NAME`` when set, else the
``/<prefix>/rbac/app-roles-table-name`` SSM parameter. Grants go through
``granted_*`` AND ``effective_permissions`` — the access checks read the
latter, and a role with only the former grants nothing (CLAUDE.md, RBAC).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from typing import List, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_e2e_role")

ROLE_ID = "e2e_user"
DEFAULT_GROUP = "e2e-users"
DEFAULT_TOOLS = ["calculator", "ask_user_question"]
DEFAULT_MODELS = ["*"]


def build_role(*, group: str, tools: List[str], models: List[str]):
    """The role as the repository will write it. Pure, so the test can pin it."""
    from apis.shared.rbac.models import AppRole, EffectivePermissions

    tools = sorted(set(tools))
    models = sorted(set(models))
    return AppRole(
        role_id=ROLE_ID,
        display_name="E2E Test User",
        description=(
            "Nightly end-to-end test account. Grants the tools the turn-path smoke "
            "matrix exercises and every model; seeded by scripts/nightly/seed-e2e-users.sh."
        ),
        jwt_role_mappings=[group],
        inherits_from=[],
        granted_tools=tools,
        granted_models=models,
        granted_skills=[],
        effective_permissions=EffectivePermissions(tools=tools, models=models, skills=[], quota_tier=None),
        priority=10,
        is_system_role=False,
        enabled=True,
        created_by="nightly-seed",
    )


def resolve_table_name(prefix: Optional[str], region: str) -> str:
    env = os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME")
    if env:
        return env
    if not prefix:
        raise SystemExit("set DYNAMODB_APP_ROLES_TABLE_NAME or pass --prefix")
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=f"/{prefix}/rbac/app-roles-table-name")["Parameter"]["Value"]


async def upsert(role, repository) -> str:
    """Create the role, or update it in place. Returns ``"created"`` or ``"updated"``."""
    existing = await repository.get_role(role.role_id)
    if existing is None:
        await repository.create_role(role)
        return "created"
    await repository.update_role(role)
    return "updated"


async def main_async(args: argparse.Namespace) -> int:
    role = build_role(group=args.group, tools=args.tools.split(","), models=args.models.split(","))
    if args.dry_run:
        print(json.dumps(dataclasses.asdict(role), indent=2, default=str))
        return 0

    table = resolve_table_name(args.prefix, args.region)
    os.environ["DYNAMODB_APP_ROLES_TABLE_NAME"] = table
    os.environ.setdefault("AWS_REGION", args.region)
    from apis.shared.rbac.repository import AppRoleRepository

    outcome = await upsert(role, AppRoleRepository(table_name=table))
    logger.info(
        "%s role %s in %s: group=%s tools=%s models=%s",
        outcome,
        role.role_id,
        table,
        args.group,
        role.granted_tools,
        role.granted_models,
    )
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prefix", help="CDK project prefix, for the SSM table-name lookup")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--group", default=DEFAULT_GROUP, help="Cognito group the role maps to")
    p.add_argument("--tools", default=",".join(DEFAULT_TOOLS), help="comma-separated tool ids")
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated model ids ('*' = all)")
    p.add_argument("--dry-run", action="store_true", help="print the role, write nothing")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
