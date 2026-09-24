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
import re
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
        "toolId": "ask_user_question",
        "displayName": "Clarifying Questions",
        "description": (
            "Pause and ask the user multiple-choice questions when a request "
            "is ambiguous, then continue with their answer."
        ),
        "category": "utility",
        # On by default. The picker ships, survives a refresh, and the turn
        # resumes into the same tool call; and the model only reaches for it on
        # genuinely ambiguous requests — measured 24/24 on ambiguous prompts
        # and 0/18 on clear ones, so it does not turn direct questions into
        # interrogations. Its spec is ~630 tokens in the cacheable prefix.
        "enabledByDefault": True,
        "protocol": "local",
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "browse_web",
        "displayName": "Web Browser",
        "description": (
            "Browse the web in a real Chrome browser: navigate pages, read "
            "JavaScript-rendered content, fill forms, and click through "
            "multi-step flows."
        ),
        "category": "browser",
        # Deliberately off by default. Each session bills an AgentCore Browser
        # session on top of model tokens, and a browsing transcript is a large
        # per-turn payload — this is opt-in per user, granted per role.
        "enabledByDefault": False,
        "protocol": "local",
        "isPublic": False,
        "forwardAuthToken": False,
    },
    {
        "toolId": "request_user_login",
        "displayName": "Browser Sign-In",
        "description": (
            "Hand the browser to the user so they can sign in to a site the "
            "agent cannot reach, then continue browsing the authenticated "
            "session."
        ),
        "category": "browser",
        # Deliberately off by default, and the most restrictive default in this
        # file. While a takeover is live the user has a fully interactive
        # Chromium running inside our AWS account with our egress — that is a
        # capability to grant to named staff/evaluator roles, never a default.
        # RBAC granularity is one tool_id, which is exactly why this is its own
        # entry rather than an action on browse_web.
        "enabledByDefault": False,
        "protocol": "local",
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
        # Single catalog entry / toggle that provisions the whole artifact
        # toolset. Enabling this one id injects create/update at runtime — see
        # ARTIFACT_TOOL_IDS and _build_artifact_tools in
        # apis/inference_api/chat/routes.py. Keep the toolId as
        # "create_artifact": it is the gate key.
        "toolId": "create_artifact",
        "displayName": "Artifacts",
        "description": "Save standalone HTML or Markdown documents as versioned artifacts the user can open and iterate on. Updates create a new immutable version.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        # Single catalog entry / toggle that provisions the whole Word
        # document toolset. Enabling this one id injects create/modify/list/
        # read at runtime — see WORD_DOCUMENT_TOOL_IDS and
        # _build_word_document_tools in apis/inference_api/chat/routes.py.
        # Keep the toolId as "create_word_document": it is the gate key.
        "toolId": "create_word_document",
        "displayName": "Word Documents",
        "description": "Create, edit, read, and list Word (.docx) documents using python-docx in a sandboxed environment. Generated files are saved to the chat's Files with a download link.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        # Single catalog entry / toggle that provisions the whole workspace
        # toolset. Enabling this one id injects list/read/write at runtime —
        # see WORKSPACE_TOOL_IDS and _build_workspace_tools in
        # apis/inference_api/chat/routes.py. Keep the toolId as
        # "workspace_files": it is the gate key.
        "toolId": "workspace_files",
        "displayName": "File Workspace",
        "description": "List, read, and save files in the conversation's workspace. Reads uploaded text files on demand and saves text deliverables (markdown, CSV, JSON) to the chat's Files with a download link.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        # Spreadsheet ANALYSIS (read an uploaded .xlsx/.csv and answer questions
        # about it via Code Interpreter) — distinct from the spreadsheet
        # *authoring* tool below. Both ids are context-bound: enabling them
        # injects the tools at runtime, see SPREADSHEET_TOOL_IDS and
        # _build_spreadsheet_tools in apis/inference_api/chat/routes.py.
        #
        # These two rows were missing from this file until 1.23.0 while the
        # Python TOOL_CATALOG carried them, so a freshly bootstrapped
        # deployment got no catalog row and the tools could not be granted to
        # any role — the live environments only have them because the rows
        # were created by hand. test_seed_matches_tool_catalog now pins the
        # two lists together so they cannot drift again.
        #
        # enabledByDefault mirrors the live deployments (True). Safe against
        # the injected-tool cost trap because SPREADSHEET_TOOL_IDS is in
        # KEY_DESCRIBED_INJECTED_TOOL_IDS: the factory closes over
        # (session_id, user_id, assistant_id), all cache-key elements, so a
        # default-on injected tool does NOT bypass the agent cache here.
        "toolId": "list_spreadsheets",
        "displayName": "List Spreadsheet Files",
        "description": "List spreadsheet files available for analysis from the assistant's knowledge base or conversation attachments.",
        "category": "data",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        "toolId": "analyze_spreadsheet",
        "displayName": "Analyze Spreadsheet",
        "description": "Analyze spreadsheet data with Python in a sandboxed Code Interpreter session: filter, aggregate, compute statistics and answer questions about the contents.",
        "category": "data",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        # Single catalog entry / toggle that provisions the whole Excel
        # spreadsheet toolset. Enabling this one id injects create/modify/list/
        # read at runtime — see EXCEL_SPREADSHEET_TOOL_IDS and
        # _build_excel_spreadsheet_tools in apis/inference_api/chat/routes.py.
        # Keep the toolId as "create_excel_spreadsheet": it is the gate key.
        # Distinct from the spreadsheet *analysis* tools (list_spreadsheets /
        # analyze_spreadsheet), which read uploaded tabular files.
        "toolId": "create_excel_spreadsheet",
        "displayName": "Excel Spreadsheets",
        "description": "Create, edit, read, and list Excel (.xlsx) spreadsheets using openpyxl in a sandboxed environment. Generated files are saved to the chat's Files with a download link.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    {
        # Single catalog entry / toggle that provisions the whole PowerPoint
        # presentation toolset. Enabling this one id injects create/modify/list/
        # read at runtime — see POWERPOINT_PRESENTATION_TOOL_IDS and
        # _build_powerpoint_presentation_tools in apis/inference_api/chat/routes.py.
        # Keep the toolId as "create_powerpoint_presentation": it is the gate key.
        "toolId": "create_powerpoint_presentation",
        "displayName": "PowerPoint Presentations",
        "description": "Create, edit, read, and list PowerPoint (.pptx) presentations using python-pptx in a sandboxed environment. Generated files are saved to the chat's Files with a download link.",
        "category": "document",
        "protocol": "local",
        "enabledByDefault": False,
        "isPublic": True,
        "forwardAuthToken": False,
    },
    # --- Platform self-service (Account & Usage) system tools ---
    # .kiro/specs/platform-self-service/. `system: True` force-injects the tool
    # on every granted turn (bypassing the user picker); an admin's runtime
    # off-switch is this row's status (set it to `disabled` in the Tools panel —
    # no redeploy). `hidden: True` keeps it out of the user picker list while
    # still showing it in the transcript when invoked. isPublic so every
    # authenticated user has it (they only ever read their OWN account); the
    # runtime tool objects are closure-bound (agents/local_tools/account_tools.py).
    {
        "toolId": "whoami",
        "displayName": "Account Lookup",
        "description": "Look up who the signed-in user is on this platform (name, roles, plan).",
        "category": "account",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
        "system": True,
        "hidden": True,
    },
    {
        "toolId": "get_my_quota",
        "displayName": "Usage & Quota",
        "description": "Report how much of the signed-in user's usage quota is left.",
        "category": "account",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
        "system": True,
        "hidden": False,
    },
    {
        "toolId": "get_my_settings",
        "displayName": "My Settings",
        "description": "Report the signed-in user's account settings, such as their default model.",
        "category": "account",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
        "system": True,
        "hidden": False,
    },
    {
        "toolId": "set_default_model",
        "displayName": "Change My Default Model",
        "description": "Change the signed-in user's default model, to one they are allowed to use. Confirmation-gated write.",
        "category": "account",
        "protocol": "local",
        "enabledByDefault": True,
        "isPublic": True,
        "forwardAuthToken": False,
        "system": True,
        "hidden": False,
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
            # EntityTypeIndex (GSI5) — mirrors ToolDefinition.to_dynamo_item.
            # This seeder hand-builds the item rather than going through the
            # model, so a new index key has to be added in BOTH places. Miss it
            # here and a freshly bootstrapped deployment lists zero tools once
            # the catalog read moves to the index — with no error, because a
            # sparse index answers "nothing matched", not "something is wrong".
            "GSI5PK": "ENTITY#TOOL",
            "GSI5SK": pk,
            "toolId": tool_id,
            "displayName": tool_def["displayName"],
            "description": tool_def["description"],
            "category": tool_def["category"],
            "protocol": tool_def["protocol"],
            "status": "active",
            "enabledByDefault": tool_def["enabledByDefault"],
            "isPublic": tool_def["isPublic"],
            "forwardAuthToken": tool_def["forwardAuthToken"],
            # Platform self-service tier (.kiro/specs/platform-self-service/).
            # Default false so every existing entry is unchanged; the account
            # tools set them true. `system` makes the tool force-injected on
            # granted turns; `hidden` keeps it out of the user picker list.
            "system": tool_def.get("system", False),
            "hidden": tool_def.get("hidden", False),
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
# end-to-end: SKILL.md-style instructions + a supporting reference file
# (uploaded to the skill-resources S3 bucket and referenced by the row's
# `resources` manifest). Mirrors the real `pdf`/`docx` bundle shape, where the
# instructions body names a reference file the agent reads on demand. Skills
# are pure knowledge bundles (Skills v2) — they bind no tools.
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
    "1. Use a web-fetching tool (e.g. `fetch_url_content`, if the agent has it\n"
    "   enabled) to pull the page text for each URL the user provides.\n"
    "2. Extract the relevant facts. For handling tables, paywalls, and noisy\n"
    "   pages, read the `extraction_tips.md` reference file.\n"
    "3. Summarize with inline source attributions (page title + URL).\n"
    "\n"
    "Never invent details that are not present in the fetched content.\n"
)


def _skill_slug(skill_id: str) -> str:
    """agentskills.io-valid skill name from a catalog id.

    Mirrors ``apis/shared/skills/bundle.py::slugify_skill_name``. Duplicated
    rather than imported because this script is standalone — ``seed.sh`` runs it
    straight after infra deploy, without the app package on the path.
    """
    slug = skill_id.strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64].strip("-") or "skill"


def _write_skill_md(
    skill_id: str, description: str, instructions: str, region: str
) -> None:
    """Write the skill's ``SKILL.md`` projection so the prefix is a real bundle.

    Mirrors ``apis/shared/skills/bundle.py::generate_skill_md`` for the subset
    the seed uses (no advisory tools, no frontmatter passthrough). Without this
    the seeded prefix holds only resource bytes and is not a valid
    agentskills.io bundle — it could not be handed to a managed Harness or
    exported as-is. Best-effort, like the reference upload.
    """
    bucket = os.environ.get("S3_SKILL_RESOURCES_BUCKET_NAME", "")
    if not bucket:
        return

    content = (
        "---\n"
        f"name: {_skill_slug(skill_id)}\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        f"{instructions.strip()}\n"
    )
    key = f"skills/{skill_id}/SKILL.md"
    try:
        s3 = boto3.Session(region_name=region).client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
            ServerSideEncryption="AES256",
        )
    except ClientError as e:
        logger.warning(
            "Could not write SKILL.md for skill '%s' to %s — seeding without "
            "the projection: %s",
            skill_id,
            bucket,
            e,
        )
        return

    logger.info("Wrote SKILL.md for skill '%s' to s3://%s/%s", skill_id, bucket, key)


def _upload_skill_reference(
    skill_id: str, filename: str, content: bytes, content_type: str, region: str
) -> Optional[dict[str, Any]]:
    """Upload a reference file's bytes to the skill-resources bucket.

    Stored in the standard agentskills.io bundle layout
    (``skills/{skill_id}/references/{filename}``), mirroring
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
    key = f"skills/{skill_id}/references/{filename}"
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
        "kind": "reference",
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
    """Seed one example knowledge skill (instructions + reference file)."""
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

    description = "Fetch web pages and turn them into accurate, citable notes."

    item: dict[str, Any] = {
        "PK": pk,
        "SK": sk,
        # SkillOwnerIndex (GSI4) — mirrors SkillDefinition.to_dynamo_item.
        "GSI4PK": "OWNER#system",
        "GSI4SK": f"SKILL#{skill_id}",
        "skillId": skill_id,
        "displayName": "Web Research Assistant",
        "description": description,
        "instructions": EXAMPLE_SKILL_INSTRUCTIONS,
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

    # Projection last: the row is the source of truth, so it must exist first.
    _write_skill_md(skill_id, description, EXAMPLE_SKILL_INSTRUCTIONS, region)

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
