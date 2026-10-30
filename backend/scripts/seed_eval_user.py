"""Seed the eval harness's own user on a deployed stack (docs/specs/eval-harness.md D7).

The harness runs as a dedicated user so that what it does never lands in a
person's conversation list or long-term memory, and so that "the eval user's
grants" is a fixed thing a baseline can describe. This writes, idempotently:

* the ``eval_harness`` app role, mapped to the Cognito group ``eval-harness``
  and granting exactly the tools and skills the golden set names
  (``required_grants`` over ``backend/evals/cases``) plus every model. It goes
  through ``AppRoleRepository`` like the admin console, so the grant rows and
  ``effective_permissions`` land together;
* a Cognito-native user ``eval-harness`` in that group, with a permanent
  password. The password is generated here, stored only as an SSM
  SecureString (``/<prefix>/eval-harness/credentials``), and never printed.
  A re-run reuses the stored password, so the user and the parameter never
  drift apart;
* the user's settings with long-term memory off. Retention cases plant facts
  ("the gate code is 7319"), and a user whose memory kept them would let a
  later run answer from memory instead of the conversation — a pass that
  proves nothing. With memory off, conversation history still persists within
  a session, which is what the retention family measures.

The harness signs in as this user with the scripted Hosted UI login
(``eval_harness.py run --auth cognito``), which needs the pool's classic
Hosted UI and the COGNITO provider on the BFF client (both are the defaults).

Usage (from ``backend/``, with credentials for the stack's account):

    uv run python scripts/seed_eval_user.py --prefix dev-boisestateai-v2
    uv run python scripts/seed_eval_user.py --prefix <prefix> --dry-run   # print the role, write nothing
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import secrets
import string
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_eval_user")

ROLE_ID = "eval_harness"
GROUP = "eval-harness"
USERNAME = "eval-harness"
#: ``.invalid`` is reserved (RFC 2606): nothing is ever delivered there, and
#: the pool's required ``email`` attribute is satisfied without a real address.
EMAIL = "eval-harness@example.invalid"
DEFAULT_MODELS = ["*"]
CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "cases"


def credentials_parameter(prefix: str) -> str:
    return f"/{prefix}/eval-harness/credentials"


def grants_from_golden_set(cases_dir: Path = CASES_DIR) -> Dict[str, List[str]]:
    """The tools and skills every case in the golden set may offer the model."""
    from apis.shared.evals.cases import load_cases, required_grants

    return required_grants(load_cases(cases_dir))


def build_role(*, tools: Sequence[str], skills: Sequence[str], models: Sequence[str]):
    """The role as the repository will write it. Pure, so the test can pin it."""
    from apis.shared.rbac.models import AppRole, EffectivePermissions

    tools = sorted(set(tools))
    skills = sorted(set(skills))
    models = sorted(set(models))
    return AppRole(
        role_id=ROLE_ID,
        display_name="Eval Harness",
        description=(
            "The evaluation harness's own account (docs/specs/eval-harness.md). Grants exactly the "
            "tools and skills the golden set offers, plus every model; seeded by "
            "backend/scripts/seed_eval_user.py."
        ),
        jwt_role_mappings=[GROUP],
        inherits_from=[],
        granted_tools=tools,
        granted_models=models,
        granted_skills=skills,
        effective_permissions=EffectivePermissions(tools=tools, models=models, skills=skills, quota_tier=None),
        priority=10,
        is_system_role=False,
        enabled=True,
        created_by="eval-harness-seed",
    )


def generate_password(length: int = 32) -> str:
    """A password that satisfies any Cognito policy: every character class, no ambiguity in a shell."""
    symbols = "!#%*+-=?@^_"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in symbols for c in candidate)
        ):
            return candidate


def read_credentials(ssm, prefix: str) -> Optional[Dict[str, str]]:
    try:
        value = ssm.get_parameter(Name=credentials_parameter(prefix), WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    return json.loads(value)


def ensure_credentials(ssm, prefix: str) -> Dict[str, str]:
    """The stored credentials, created on first run. Never logged."""
    stored = read_credentials(ssm, prefix)
    if stored and stored.get("username") == USERNAME and stored.get("password"):
        return stored
    creds = {"username": USERNAME, "password": generate_password()}
    ssm.put_parameter(
        Name=credentials_parameter(prefix),
        Value=json.dumps(creds),
        Type="SecureString",
        Overwrite=True,
        Description="Eval harness user credentials (backend/scripts/seed_eval_user.py)",
    )
    return creds


def ensure_cognito_user(idp, pool_id: str, password: str) -> str:
    """Create or confirm the user with a permanent password, in the group. Returns its ``sub``."""
    try:
        idp.get_group(UserPoolId=pool_id, GroupName=GROUP)
    except idp.exceptions.ResourceNotFoundException:
        idp.create_group(
            UserPoolId=pool_id,
            GroupName=GROUP,
            Description="Eval harness account (maps to the eval_harness app role)",
        )
    try:
        idp.admin_get_user(UserPoolId=pool_id, Username=USERNAME)
    except idp.exceptions.UserNotFoundException:
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=USERNAME,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": EMAIL},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
    # Permanent, so the scripted login never meets a forced password change;
    # re-applying the stored password keeps the user and the parameter in step.
    idp.admin_set_user_password(UserPoolId=pool_id, Username=USERNAME, Password=password, Permanent=True)
    idp.admin_add_user_to_group(UserPoolId=pool_id, Username=USERNAME, GroupName=GROUP)
    attrs = idp.admin_get_user(UserPoolId=pool_id, Username=USERNAME)["UserAttributes"]
    return next(a["Value"] for a in attrs if a["Name"] == "sub")


async def upsert_role(role, repository) -> str:
    existing = await repository.get_role(role.role_id)
    if existing is None:
        await repository.create_role(role)
        return "created"
    await repository.update_role(role)
    return "updated"


async def turn_memory_off(user_id: str, repository) -> None:
    await repository.update_settings(user_id, {"memoryMode": "off"})


def _ssm_value(ssm, name: str, default: Optional[str] = None) -> str:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 — fall back to the naming convention
        if default is None:
            raise
        return default


async def main_async(args: argparse.Namespace) -> int:
    grants = grants_from_golden_set()
    role = build_role(tools=grants["tools"], skills=grants["skills"], models=args.models.split(","))
    if args.dry_run:
        print(json.dumps(dataclasses.asdict(role), indent=2, default=str))
        return 0

    import boto3

    ssm = boto3.client("ssm", region_name=args.region)
    idp = boto3.client("cognito-idp", region_name=args.region)
    prefix = args.prefix

    roles_table = _ssm_value(ssm, f"/{prefix}/rbac/app-roles-table-name")
    settings_table = _ssm_value(ssm, f"/{prefix}/settings/user-settings-table-name", f"{prefix}-user-settings")
    pool_id = _ssm_value(ssm, f"/{prefix}/auth/cognito/user-pool-id")
    os.environ["DYNAMODB_APP_ROLES_TABLE_NAME"] = roles_table
    os.environ["DYNAMODB_USER_SETTINGS_TABLE_NAME"] = settings_table
    os.environ.setdefault("AWS_REGION", args.region)

    from apis.shared.rbac.repository import AppRoleRepository
    from apis.shared.user_settings.repository import UserSettingsRepository

    outcome = await upsert_role(role, AppRoleRepository(table_name=roles_table))
    creds = ensure_credentials(ssm, prefix)
    user_id = ensure_cognito_user(idp, pool_id, creds["password"])
    await turn_memory_off(user_id, UserSettingsRepository(table_name=settings_table))

    logger.info("%s role %s: group=%s", outcome, ROLE_ID, GROUP)
    logger.info("  tools:  %s", ", ".join(role.granted_tools))
    logger.info("  skills: %s", ", ".join(role.granted_skills))
    logger.info("  models: %s", ", ".join(role.granted_models))
    logger.info("user %s (user id %s) is in group %s, long-term memory off", USERNAME, user_id, GROUP)
    logger.info("credentials: SSM SecureString %s", credentials_parameter(prefix))
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prefix", help="CDK project prefix, e.g. dev-boisestateai-v2")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated model ids ('*' = all)")
    p.add_argument("--dry-run", action="store_true", help="print the role, write nothing")
    args = p.parse_args(argv)
    if not args.dry_run and not args.prefix:
        p.error("--prefix is required unless --dry-run")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
