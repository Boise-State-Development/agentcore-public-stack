#!/usr/bin/env python3
"""
Bootstrap data seeding script for first-time platform deployment.

Seeds quota tiers, quota assignments, Bedrock models, system admin role,
and default tools into DynamoDB. Designed to be invoked by
scripts/stack-bootstrap/seed.sh after infrastructure deployment.

Auth provider seeding has been removed — admin authentication is now
handled via the Cognito first-boot flow.

All operations are idempotent: re-running with identical inputs produces
the same database state.

Environment variables:
    DDB_USER_QUOTAS_TABLE     - User quotas DynamoDB table name
    DDB_MANAGED_MODELS_TABLE  - Managed models DynamoDB table name
    DDB_APP_ROLES_TABLE       - App roles DynamoDB table name
    AWS_REGION                - AWS region
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("seed_bootstrap_data")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# Fixed namespace for deterministic model UUIDs
MODEL_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


@dataclass
class SeedResult:
    """Result of a single seed operation."""

    category: str
    created: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


def seed_default_quota_tier(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed the default quota tier ($50 monthly, 80% soft limit, block)."""
    result = SeedResult(category="quota_tier")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    pk = "QUOTA_TIER#default"
    sk = "METADATA"

    try:
        existing = table.get_item(Key={"PK": pk, "SK": sk})
        if "Item" in existing:
            msg = "Default quota tier already exists — skipped"
            logger.info(msg)
            result.skipped = 1
            result.details.append(msg)
            return result
    except ClientError as e:
        msg = f"Failed to check existing quota tier: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item = {
        "PK": pk,
        "SK": sk,
        "tierId": "default",
        "tierName": "Default",
        "description": "Default quota tier for all users",
        "monthlyCostLimit": Decimal("5.0"),
        "periodType": "monthly",
        "softLimitPercentage": Decimal("80.0"),
        "actionOnLimit": "block",
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    try:
        table.put_item(Item=item)
        logger.info("Default quota tier created")
        result.created = 1
        result.details.append("Default quota tier created")
    except ClientError as e:
        msg = f"Failed to write default quota tier: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)

    return result


def seed_default_quota_assignment(
    table_name: str,
    region: str,
    tier_id: str = "default",
) -> SeedResult:
    """Seed the default quota assignment (default_tier type, priority 100)."""
    result = SeedResult(category="quota_assignment")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    pk = "ASSIGNMENT#default-assignment"
    sk = "METADATA"

    try:
        existing = table.get_item(Key={"PK": pk, "SK": sk})
        if "Item" in existing:
            msg = "Default quota assignment already exists — skipped"
            logger.info(msg)
            result.skipped = 1
            result.details.append(msg)
            return result
    except ClientError as e:
        msg = f"Failed to check existing quota assignment: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item = {
        "PK": pk,
        "SK": sk,
        "GSI1PK": "ASSIGNMENT_TYPE#default_tier",
        "GSI1SK": "PRIORITY#100#default-assignment",
        "assignmentId": "default-assignment",
        "tierId": tier_id,
        "assignmentType": "default_tier",
        "priority": 100,
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    try:
        table.put_item(Item=item)
        logger.info("Default quota assignment created")
        result.created = 1
        result.details.append("Default quota assignment created")
    except ClientError as e:
        msg = f"Failed to write default quota assignment: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)

    return result


# Sensible inference-param defaults for general-purpose Claude chat models.
# Temperature 0.7 mirrors the previous always-on default and gives admins a
# starting point they can tighten per-model. Bounds match Anthropic's accepted
# range. max_tokens is supported but left at no-default — the model's own cap
# applies unless an admin explicitly sets one.
CLAUDE_CHAT_SUPPORTED_PARAMS: dict[str, Any] = {
    "params": {
        "temperature": {
            "supported": True,
            "min": Decimal("0"),
            "max": Decimal("1"),
            "default": Decimal("0.7"),
            "locked": False,
        },
        "top_p": {
            "supported": True,
            "min": Decimal("0"),
            "max": Decimal("1"),
            "default": None,
            "locked": False,
        },
        "max_tokens": {
            "supported": True,
            "min": Decimal("1"),
            "max": None,
            "default": None,
            "locked": False,
        },
    }
}


# Sonnet 4.6 adds the `effort` knob (adaptive-thinking depth + overall token
# spend). The per-model `allowed` set is the whole point of the design —
# it's data, not code, so Opus 4.7 (which also gets `xhigh`/`max`) is just a
# different array on a different record. Ordered low->high so future clamping
# degrades gracefully. NOTE: Anthropic's published docs additionally list
# `max` for Sonnet 4.6; this seeds the narrower low/medium/high set — widen
# the array here if you want `max` exposed on this model.
CLAUDE_SONNET_46_SUPPORTED_PARAMS: dict[str, Any] = {
    "params": {
        **CLAUDE_CHAT_SUPPORTED_PARAMS["params"],
        "effort": {
            "supported": True,
            "allowed": ["low", "medium", "high"],
            "default": "high",
            "locked": False,
        },
    }
}


# Default Bedrock models to seed
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "modelName": "Claude Haiku 4.5",
        "provider": "bedrock",
        "providerName": "Anthropic",
        "inputModalities": ["TEXT", "IMAGE"],
        "outputModalities": ["TEXT"],
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000,
        "inputPricePerMillionTokens": Decimal("1.00"),
        "outputPricePerMillionTokens": Decimal("5.00"),
        "cacheWritePricePerMillionTokens": Decimal("1.25"),
        "cacheReadPricePerMillionTokens": Decimal("0.10"),
        "supportsCaching": True,
        "isDefault": True,
        "supportedParams": CLAUDE_CHAT_SUPPORTED_PARAMS,
    },
    {
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "modelName": "Claude Sonnet 4.6",
        "provider": "bedrock",
        "providerName": "Anthropic",
        "inputModalities": ["TEXT", "IMAGE"],
        "outputModalities": ["TEXT"],
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000,
        "inputPricePerMillionTokens": Decimal("3.00"),
        "outputPricePerMillionTokens": Decimal("15.00"),
        "cacheWritePricePerMillionTokens": Decimal("3.75"),
        "cacheReadPricePerMillionTokens": Decimal("0.30"),
        "supportsCaching": True,
        "isDefault": False,
        "supportedParams": CLAUDE_SONNET_46_SUPPORTED_PARAMS,
    },
    {
        "modelId": "amazon.nova-2-sonic-v1:0",
        "modelName": "Nova 2 Sonic",
        "provider": "bedrock",
        "providerName": "Amazon",
        "inputModalities": ["TEXT", "SPEECH"],
        "outputModalities": ["TEXT", "SPEECH"],
        "maxInputTokens": 200000,
        "maxOutputTokens": 4096,
        "inputPricePerMillionTokens": Decimal("3.00"),
        "outputPricePerMillionTokens": Decimal("12.00"),
        "cacheWritePricePerMillionTokens": Decimal("0"),
        "cacheReadPricePerMillionTokens": Decimal("0"),
        "supportsCaching": False,
        "isDefault": False,
        # Voice/bidi model: param shape differs from chat models. Leave
        # supportedParams unset so the runtime passes through to whatever
        # the BidiAgent path negotiates.
    },
]


def seed_default_models(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed default Bedrock model registrations."""
    result = SeedResult(category="model")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    for model_def in DEFAULT_MODELS:
        model_id = model_def["modelId"]
        deterministic_uuid = str(uuid.uuid5(MODEL_UUID_NAMESPACE, model_id))

        # Check existence via GSI query
        try:
            query_resp = table.query(
                IndexName="ModelIdIndex",
                KeyConditionExpression=boto3.dynamodb.conditions.Key("GSI1PK").eq(f"MODEL#{model_id}"),
                Limit=1,
            )
            if query_resp.get("Items"):
                msg = f"Model '{model_def['modelName']}' ({model_id}) already exists — skipped"
                logger.info(msg)
                result.skipped += 1
                result.details.append(msg)
                continue
        except ClientError as e:
            msg = f"Failed to check existing model '{model_id}': {e}"
            logger.error(msg)
            result.failed += 1
            result.details.append(msg)
            continue

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        pk = f"MODEL#{deterministic_uuid}"
        item: dict[str, Any] = {
            "PK": pk,
            "SK": pk,
            "GSI1PK": f"MODEL#{model_id}",
            "GSI1SK": pk,
            "id": deterministic_uuid,
            "modelId": model_id,
            "modelName": model_def["modelName"],
            "provider": model_def["provider"],
            "providerName": model_def["providerName"],
            "inputModalities": model_def["inputModalities"],
            "outputModalities": model_def["outputModalities"],
            "maxInputTokens": model_def["maxInputTokens"],
            "maxOutputTokens": model_def["maxOutputTokens"],
            "allowedAppRoles": [],
            "availableToRoles": [],
            "enabled": True,
            "inputPricePerMillionTokens": model_def["inputPricePerMillionTokens"],
            "outputPricePerMillionTokens": model_def["outputPricePerMillionTokens"],
            "cacheWritePricePerMillionTokens": model_def["cacheWritePricePerMillionTokens"],
            "cacheReadPricePerMillionTokens": model_def["cacheReadPricePerMillionTokens"],
            "supportsCaching": model_def["supportsCaching"],
            "isDefault": model_def["isDefault"],
            "createdAt": now,
            "updatedAt": now,
        }

        # Optional: per-model inference parameter capabilities. Stored as a
        # nested map; absence means "passthrough" at runtime.
        if "supportedParams" in model_def and model_def["supportedParams"] is not None:
            item["supportedParams"] = model_def["supportedParams"]

        try:
            table.put_item(Item=item)
            msg = f"Model '{model_def['modelName']}' ({model_id}) created"
            logger.info(msg)
            result.created += 1
            result.details.append(msg)
        except ClientError as e:
            msg = f"Failed to write model '{model_id}': {e}"
            logger.error(msg)
            result.failed += 1
            result.details.append(msg)

    return result


DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "toolId": "fetch_url_content",
        "displayName": "URL Fetcher",
        "description": "Fetch and extract text content from web pages, job descriptions, articles, and documentation.",
        "category": "search",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "create_visualization",
        "displayName": "Charts & Graphs",
        "description": "Create interactive bar, line, and pie charts from data.",
        "category": "data",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "calculator",
        "displayName": "Calculator",
        "description": "Perform mathematical calculations and evaluations.",
        "category": "utility",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "generate_diagram_and_validate",
        "displayName": "Code Interpreter",
        "description": "Generate diagrams, charts, and visualizations using Python code in a sandboxed environment.",
        "category": "code",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "create_artifact",
        "displayName": "Create Artifact",
        "description": "Save standalone HTML or Markdown documents as versioned artifacts the user can open and iterate on.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        "toolId": "update_artifact",
        "displayName": "Update Artifact",
        "description": "Replace an existing artifact's content, creating a new immutable version.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
    },
]


def seed_system_admin_role(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed the system_admin role with DEFINITION, MODEL_GRANT#*, and TOOL_GRANT#*.

    This runs unconditionally (no JWT role required). Admin access is now
    granted via the Cognito first-boot flow.
    """
    result = SeedResult(category="system_admin_role")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    role_id = "system_admin"
    pk = f"ROLE#{role_id}"

    try:
        existing = table.get_item(Key={"PK": pk, "SK": "DEFINITION"})
        if "Item" in existing:
            # Role exists — ensure JWT mapping is present (additive, non-destructive)
            try:
                jwt_check = table.get_item(Key={"PK": pk, "SK": "JWT_MAPPING#system_admin"})
                if "Item" in jwt_check:
                    msg = "system_admin role already exists with JWT mapping — skipped"
                    logger.info(msg)
                    result.skipped = 1
                    result.details.append(msg)
                    return result
            except ClientError:
                pass  # If check fails, try to add the mapping anyway

            # JWT mapping is missing — add it without touching anything else
            logger.info("system_admin role exists but JWT_MAPPING#system_admin is missing — adding it")
            try:
                jwt_mapping_item = {
                    "PK": pk,
                    "SK": "JWT_MAPPING#system_admin",
                    "GSI1PK": "JWT_ROLE#system_admin",
                    "GSI1SK": pk,
                    "roleId": role_id,
                    "enabled": True,
                }
                table.put_item(Item=jwt_mapping_item)

                # Also update the DEFINITION to include the mapping in jwtRoleMappings
                existing_mappings = existing["Item"].get("jwtRoleMappings", [])
                if "system_admin" not in existing_mappings:
                    existing_mappings.append("system_admin")
                    table.update_item(
                        Key={"PK": pk, "SK": "DEFINITION"},
                        UpdateExpression="SET jwtRoleMappings = :m",
                        ExpressionAttributeValues={":m": existing_mappings},
                    )

                msg = "Added missing JWT_MAPPING#system_admin to existing system_admin role"
                logger.info(msg)
                result.created = 1
                result.details.append(msg)
            except ClientError as e:
                msg = f"Failed to add JWT mapping to existing system_admin role: {e}"
                logger.error(msg)
                result.failed = 1
                result.details.append(msg)
            return result
    except ClientError as e:
        msg = f"Failed to check existing system_admin role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    definition_item: dict[str, Any] = {
        "PK": pk,
        "SK": "DEFINITION",
        "roleId": role_id,
        "displayName": "System Administrator",
        "description": "Full access to all system features. This role cannot be deleted.",
        "jwtRoleMappings": ["system_admin"],
        "inheritsFrom": [],
        "grantedTools": ["*"],
        "grantedModels": ["*"],
        "grantedSkills": ["*"],
        "effectivePermissions": {
            "tools": ["*"],
            "models": ["*"],
            "skills": ["*"],
            "quotaTier": None,
        },
        "priority": 1000,
        "isSystemRole": True,
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    tool_grant_item = {
        "PK": pk,
        "SK": "TOOL_GRANT#*",
        "GSI2PK": "TOOL#*",
        "GSI2SK": pk,
        "roleId": role_id,
        "displayName": "System Administrator",
        "enabled": True,
    }

    model_grant_item = {
        "PK": pk,
        "SK": "MODEL_GRANT#*",
        "GSI3PK": "MODEL#*",
        "GSI3SK": pk,
        "roleId": role_id,
        "displayName": "System Administrator",
        "enabled": True,
    }

    # Skill grants reuse the GSI2 keyspace with a SKILL# partition value
    # (mirror of tool grants; the TOOL#/SKILL# partitions are disjoint).
    skill_grant_item = {
        "PK": pk,
        "SK": "SKILL_GRANT#*",
        "GSI2PK": "SKILL#*",
        "GSI2SK": pk,
        "roleId": role_id,
        "displayName": "System Administrator",
        "enabled": True,
    }

    jwt_mapping_item = {
        "PK": pk,
        "SK": "JWT_MAPPING#system_admin",
        "GSI1PK": "JWT_ROLE#system_admin",
        "GSI1SK": pk,
        "roleId": role_id,
        "enabled": True,
    }

    try:
        client = session.client("dynamodb")
        client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": table_name, "Item": _serialize(definition_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(tool_grant_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(model_grant_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(skill_grant_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(jwt_mapping_item)}},
            ]
        )
        result.created = 1
        result.details.append("system_admin role created with TOOL_GRANT#*, MODEL_GRANT#*, SKILL_GRANT#*, and JWT_MAPPING#system_admin")
    except ClientError as e:
        msg = f"Failed to create system_admin role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)

    return result


def seed_default_role(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed the default role with DEFINITION, MODEL_GRANT#*, and TOOL_GRANT#*.

    The default role is the fallback for users who have no JWT role mappings.
    Without it, regular users (e.g. Cognito users with no groups) get empty
    permissions and cannot see any models or tools.
    """
    result = SeedResult(category="default_role")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    role_id = "default"
    pk = f"ROLE#{role_id}"

    try:
        existing = table.get_item(Key={"PK": pk, "SK": "DEFINITION"})
        if "Item" in existing:
            msg = "default role already exists — skipped"
            logger.info(msg)
            result.skipped = 1
            result.details.append(msg)
            return result
    except ClientError as e:
        msg = f"Failed to check existing default role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    definition_item: dict[str, Any] = {
        "PK": pk,
        "SK": "DEFINITION",
        "roleId": role_id,
        "displayName": "Default",
        "description": "Default role for all users. Grants access to all models and tools.",
        "jwtRoleMappings": [],
        "inheritsFrom": [],
        "grantedTools": ["*"],
        "grantedModels": ["*"],
        "effectivePermissions": {
            "tools": ["*"],
            "models": ["*"],
            "quotaTier": "default",
        },
        "priority": 1,
        "isSystemRole": True,
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    tool_grant_item = {
        "PK": pk,
        "SK": "TOOL_GRANT#*",
        "GSI2PK": "TOOL#*",
        "GSI2SK": pk,
        "roleId": role_id,
        "displayName": "Default",
        "enabled": True,
    }

    model_grant_item = {
        "PK": pk,
        "SK": "MODEL_GRANT#*",
        "GSI3PK": "MODEL#*",
        "GSI3SK": pk,
        "roleId": role_id,
        "displayName": "Default",
        "enabled": True,
    }

    try:
        client = session.client("dynamodb")
        client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": table_name, "Item": _serialize(definition_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(tool_grant_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(model_grant_item)}},
            ]
        )
        result.created = 1
        result.details.append("default role created with TOOL_GRANT#* and MODEL_GRANT#*")
    except ClientError as e:
        msg = f"Failed to create default role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)

    return result


def seed_default_tools(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed default tool registrations into the app-roles table."""
    result = SeedResult(category="tool")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    for tool_def in DEFAULT_TOOLS:
        tool_id = tool_def["toolId"]
        pk = f"TOOL#{tool_id}"
        sk = "METADATA"

        try:
            existing = table.get_item(Key={"PK": pk, "SK": sk})
            if "Item" in existing:
                msg = f"Tool '{tool_def['displayName']}' ({tool_id}) already exists — skipped"
                logger.info(msg)
                result.skipped += 1
                result.details.append(msg)
                continue
        except ClientError as e:
            msg = f"Failed to check existing tool '{tool_id}': {e}"
            logger.error(msg)
            result.failed += 1
            result.details.append(msg)
            continue

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "GSI1PK": f"CATEGORY#{tool_def['category']}",
            "GSI1SK": pk,
            "toolId": tool_id,
            "displayName": tool_def["displayName"],
            "description": tool_def["description"],
            "category": tool_def["category"],
            "protocol": tool_def["protocol"],
            "status": "active",
            "enabledByDefault": tool_def["enabledByDefault"],
            "isPublic": tool_def["isPublic"],
            "forwardAuthToken": tool_def["forwardAuthToken"],
            "createdAt": now,
            "updatedAt": now,
            "createdBy": "bootstrap-seed",
        }

        try:
            table.put_item(Item=item)
            msg = f"Tool '{tool_def['displayName']}' ({tool_id}) created"
            logger.info(msg)
            result.created += 1
            result.details.append(msg)
        except ClientError as e:
            msg = f"Failed to write tool '{tool_id}': {e}"
            logger.error(msg)
            result.failed += 1
            result.details.append(msg)

    return result



# =============================================================================
# Example bundled skill (PR-6b)
# =============================================================================
#
# Seeds one demonstrable admin-managed Skill so the feature can be exercised
# end-to-end: SKILL.md-style instructions + a bound LOCAL catalog tool
# (fetch_url_content, seeded above) + a supporting reference file (uploaded to
# the skill-resources S3 bucket and referenced by the row's `resources`
# manifest). Mirrors the real `pdf`/`docx` bundle shape, where the instructions
# body names a reference file the agent reads on demand via skill_dispatcher.
#
# The skill is granted to the `default` role so any user can reach it once an
# assistant opts into agent_type="skill". It is otherwise inert: the default
# agent_type stays "chat", so this changes nothing for existing chats.

EXAMPLE_SKILL_ID = "web_research"

EXAMPLE_SKILL_REFERENCE_FILENAME = "extraction_tips.md"

EXAMPLE_SKILL_REFERENCE_BODY = b"""# Extraction Tips

Guidance for turning a fetched web page into citable, well-structured notes.

## Prefer primary sources
- Quote the page's own words for any claim you will cite; paraphrase only
  after you have the exact wording recorded.
- Capture the page title and URL alongside every excerpt so a citation can be
  reconstructed later.

## Tables and lists
- Re-render HTML tables as Markdown tables; keep the original column order.
- Preserve list nesting - it usually encodes hierarchy that matters.

## Noise to drop
- Navigation chrome, cookie banners, "related articles", and ad copy are not
  content. Exclude them before summarizing.

## When a page is thin or blocked
- If the fetched text is a paywall stub or a JS placeholder, say so explicitly
  rather than summarizing the stub as if it were the article.
"""

EXAMPLE_SKILL_INSTRUCTIONS = (
    "# Web Research Assistant\n"
    "\n"
    "Help the user research a topic by fetching web pages and turning them into\n"
    "accurate, citable notes.\n"
    "\n"
    "## Workflow\n"
    "1. Use the bound `fetch_url_content` tool (via `skill_executor`) to pull\n"
    "   the page text for each URL the user provides.\n"
    "2. Extract the relevant facts. For handling tables, paywalls, and noisy\n"
    "   pages, read `extraction_tips.md` — call `skill_dispatcher` again with\n"
    "   `reference=\"extraction_tips.md\"`.\n"
    "3. Summarize with inline source attributions (page title + URL).\n"
    "\n"
    "Never invent details that are not present in the fetched content.\n"
)


def _upload_skill_reference(
    skill_id: str, filename: str, content: bytes, content_type: str, region: str
) -> Optional[dict[str, Any]]:
    """Upload a reference file's bytes to the skill-resources bucket.

    Content-addressed (``skills/{skill_id}/{sha256}``), mirroring
    ``apis/shared/skills/resource_store.py``. Best-effort: returns the manifest
    entry on success, or ``None`` (with a warning) when the bucket is not
    configured or the put fails, so the skill still seeds without the bytes.
    """
    bucket = os.environ.get("S3_SKILL_RESOURCES_BUCKET_NAME", "")
    if not bucket:
        logger.warning(
            "S3_SKILL_RESOURCES_BUCKET_NAME unset — seeding '%s' without its "
            "reference file '%s'",
            skill_id,
            filename,
        )
        return None

    digest = hashlib.sha256(content).hexdigest()
    key = f"skills/{skill_id}/{digest}"
    try:
        s3 = boto3.Session(region_name=region).client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
    except ClientError as e:
        logger.warning(
            "Could not upload reference '%s' for skill '%s' to %s — seeding "
            "without it: %s",
            filename,
            skill_id,
            bucket,
            e,
        )
        return None

    logger.info("Uploaded reference '%s' for skill '%s' to s3://%s/%s", filename, skill_id, bucket, key)
    return {
        "filename": filename,
        "contentHash": digest,
        "size": len(content),
        "contentType": content_type,
        "s3Key": key,
    }


def _grant_skill_to_default_role(table, skill_id: str) -> None:
    """Grant ``skill_id`` to the ``default`` role (idempotent).

    RBAC resolution reads the precomputed ``effectivePermissions.skills`` on the
    role DEFINITION (see ``apis/shared/rbac/service.py::_merge_permissions``), so
    granting requires patching that array — the reverse-lookup ``SKILL_GRANT#``
    item alone is not enough. Writes both. No-op if the default role is absent.
    """
    pk = "ROLE#default"
    try:
        existing = table.get_item(Key={"PK": pk, "SK": "DEFINITION"})
    except ClientError as e:
        logger.warning("Could not read default role for skill grant: %s", e)
        return

    item = existing.get("Item")
    if not item:
        logger.warning(
            "default role not found — skipping grant of example skill '%s' "
            "(system_admin's skills=['*'] still covers it)",
            skill_id,
        )
        return

    granted = list(item.get("grantedSkills", []) or [])
    eff = dict(item.get("effectivePermissions", {}) or {})
    eff_skills = list(eff.get("skills", []) or [])

    changed = False
    if skill_id not in granted:
        granted.append(skill_id)
        changed = True
    if "*" not in eff_skills and skill_id not in eff_skills:
        eff_skills.append(skill_id)
        changed = True

    if changed:
        eff["skills"] = eff_skills
        table.update_item(
            Key={"PK": pk, "SK": "DEFINITION"},
            UpdateExpression="SET grantedSkills = :g, effectivePermissions = :e",
            ExpressionAttributeValues={":g": granted, ":e": eff},
        )
        logger.info("Granted example skill '%s' to default role", skill_id)

    # Reverse-lookup grant item (so /admin/skills/{id}/roles lists 'default').
    table.put_item(
        Item={
            "PK": pk,
            "SK": f"SKILL_GRANT#{skill_id}",
            "GSI2PK": f"SKILL#{skill_id}",
            "GSI2SK": pk,
            "roleId": "default",
            "displayName": "Default",
            "enabled": True,
        }
    )


def seed_example_skills(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed one example bundled skill (instructions + bound tool + reference file)."""
    result = SeedResult(category="skill")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    skill_id = EXAMPLE_SKILL_ID
    pk = f"SKILL#{skill_id}"
    sk = "METADATA"

    try:
        existing = table.get_item(Key={"PK": pk, "SK": sk})
        if "Item" in existing:
            msg = f"Example skill '{skill_id}' already exists — skipped"
            logger.info(msg)
            result.skipped += 1
            result.details.append(msg)
            # Still (idempotently) ensure the default-role grant is present.
            _grant_skill_to_default_role(table, skill_id)
            return result
    except ClientError as e:
        msg = f"Failed to check existing skill '{skill_id}': {e}"
        logger.error(msg)
        result.failed += 1
        result.details.append(msg)
        return result

    # Upload the supporting reference file (best-effort — see helper).
    resources: list[dict[str, Any]] = []
    ref = _upload_skill_reference(
        skill_id,
        EXAMPLE_SKILL_REFERENCE_FILENAME,
        EXAMPLE_SKILL_REFERENCE_BODY,
        "text/markdown",
        region,
    )
    if ref:
        resources.append(ref)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item: dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        # SkillOwnerIndex (GSI4) — mirrors SkillDefinition.to_dynamo_item.
        "GSI4PK": "OWNER#system",
        "GSI4SK": f"SKILL#{skill_id}",
        "skillId": skill_id,
        "displayName": "Web Research Assistant",
        "description": "Fetch web pages and turn them into accurate, citable notes.",
        "instructions": EXAMPLE_SKILL_INSTRUCTIONS,
        "boundToolIds": ["fetch_url_content"],
        "compose": [],
        "resources": resources,
        "status": "active",
        "ownerId": "system",
        "visibility": "admin",
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    try:
        table.put_item(Item=item)
        msg = (
            f"Example skill '{skill_id}' created "
            f"({len(resources)} reference file(s))"
        )
        logger.info(msg)
        result.created += 1
        result.details.append(msg)
    except ClientError as e:
        msg = f"Failed to write example skill '{skill_id}': {e}"
        logger.error(msg)
        result.failed += 1
        result.details.append(msg)
        return result

    _grant_skill_to_default_role(table, skill_id)
    return result


def _serialize(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a high-level DynamoDB item dict to low-level client format."""
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {k: serializer.serialize(v) for k, v in item.items()}


def print_summary(results: list[SeedResult]) -> None:
    """Print a structured summary of all seed operations."""
    print()
    print("=" * 60)
    print("  Bootstrap Data Seeding Summary")
    print("=" * 60)
    for r in results:
        print(f"  {r.category:<20s}  created={r.created}  skipped={r.skipped}  failed={r.failed}")
        for detail in r.details:
            print(f"    - {detail}")
    print("=" * 60)

    total_failed = sum(r.failed for r in results)
    if total_failed:
        print(f"  RESULT: {total_failed} operation(s) failed")
    else:
        total_created = sum(r.created for r in results)
        total_skipped = sum(r.skipped for r in results)
        print(f"  RESULT: OK ({total_created} created, {total_skipped} skipped)")
    print()


def main() -> None:
    """Entry point: read env vars, dispatch seeders, print summary."""
    # Required env vars for DynamoDB tables and region
    quotas_table = os.environ.get("DDB_USER_QUOTAS_TABLE", "")
    models_table = os.environ.get("DDB_MANAGED_MODELS_TABLE", "")
    app_roles_table = os.environ.get("DDB_APP_ROLES_TABLE", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    results: list[SeedResult] = []

    # --- Quota tier seeding ---
    results.append(seed_default_quota_tier(table_name=quotas_table, region=region))

    # --- Quota assignment seeding ---
    results.append(
        seed_default_quota_assignment(table_name=quotas_table, region=region, tier_id="default")
    )

    # --- Model seeding ---
    results.append(seed_default_models(table_name=models_table, region=region))

    # --- System admin role seeding ---
    results.append(seed_system_admin_role(table_name=app_roles_table, region=region))

    # --- Default role seeding ---
    results.append(seed_default_role(table_name=app_roles_table, region=region))

    # --- Tool seeding ---
    results.append(seed_default_tools(table_name=app_roles_table, region=region))

    # --- Example bundled skill (PR-6b) ---
    results.append(seed_example_skills(table_name=app_roles_table, region=region))

    # --- Summary ---
    print_summary(results)

    total_failed = sum(r.failed for r in results)
    sys.exit(1 if total_failed else 0)


if __name__ == "__main__":
    main()
