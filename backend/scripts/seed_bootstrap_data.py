#!/usr/bin/env python3
"""
Bootstrap data seeding script for first-time platform deployment.

Seeds auth providers, quota tiers, quota assignments, Bedrock models,
and system admin JWT role mappings into DynamoDB and Secrets Manager.
Designed to be invoked by scripts/stack-bootstrap/seed.sh after
infrastructure deployment.

All operations are idempotent: re-running with identical inputs produces
the same database state.

Environment variables:
    DDB_AUTH_PROVIDERS_TABLE  - Auth providers DynamoDB table name
    DDB_USER_QUOTAS_TABLE     - User quotas DynamoDB table name
    DDB_MANAGED_MODELS_TABLE  - Managed models DynamoDB table name
    DDB_APP_ROLES_TABLE       - App roles DynamoDB table name
    SECRETS_AUTH_ARN          - Secrets Manager ARN for auth secrets
    AWS_REGION                - AWS region

    SEED_AUTH_PROVIDER_ID     - Provider slug (e.g., entra-id)
    SEED_AUTH_DISPLAY_NAME    - Login page display name
    SEED_AUTH_ISSUER_URL      - OIDC issuer URL
    SEED_AUTH_CLIENT_ID       - OAuth client ID
    SEED_AUTH_CLIENT_SECRET   - OAuth client secret
    SEED_AUTH_BUTTON_COLOR    - Hex color for login button (optional)
    SEED_ADMIN_JWT_ROLE      - JWT role that grants system admin access (e.g., Admin)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
import httpx
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


def seed_auth_provider(
    table_name: str,
    secrets_arn: str,
    region: str,
    provider_id: str,
    display_name: str,
    issuer_url: str,
    client_id: str,
    client_secret: str,
    button_color: str | None = None,
    discover: bool = True,
) -> SeedResult:
    """Seed a single OIDC auth provider into DynamoDB and Secrets Manager."""
    result = SeedResult(category="auth_provider")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)
    secrets_client = session.client("secretsmanager")

    pk = f"AUTH_PROVIDER#{provider_id}"

    # Check for existing item
    try:
        existing = table.get_item(Key={"PK": pk, "SK": pk})
        if "Item" in existing:
            msg = f"Auth provider '{provider_id}' already exists — skipped"
            logger.info(msg)
            result.skipped = 1
            result.details.append(msg)
            return result
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        msg = f"Failed to check existing auth provider '{provider_id}': {error_code}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    # OIDC discovery
    discovered: dict[str, Any] = {}
    if discover:
        discovery_url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        logger.info("Discovering OIDC endpoints from %s", discovery_url)
        try:
            resp = httpx.get(discovery_url, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            discovered = resp.json()
            logger.info("OIDC discovery successful")
        except Exception as e:
            logger.warning("OIDC discovery failed: %s — continuing without discovered endpoints", e)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item: dict[str, Any] = {
        "PK": pk,
        "SK": pk,
        "GSI1PK": "ENABLED#true",
        "GSI1SK": pk,
        "providerId": provider_id,
        "displayName": display_name,
        "providerType": "oidc",
        "enabled": True,
        "issuerUrl": issuer_url,
        "clientId": client_id,
        "scopes": "openid profile email",
        "responseType": "code",
        "pkceEnabled": True,
        "userIdClaim": "sub",
        "emailClaim": "email",
        "nameClaim": "name",
        "rolesClaim": "roles",
        "pictureClaim": "picture",
        "firstNameClaim": "given_name",
        "lastNameClaim": "family_name",
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    # Map discovered endpoints
    endpoint_mapping = {
        "authorizationEndpoint": "authorization_endpoint",
        "tokenEndpoint": "token_endpoint",
        "jwksUri": "jwks_uri",
        "userinfoEndpoint": "userinfo_endpoint",
        "endSessionEndpoint": "end_session_endpoint",
    }
    for dynamo_key, oidc_key in endpoint_mapping.items():
        value = discovered.get(oidc_key)
        if value:
            item[dynamo_key] = value

    if button_color:
        item["buttonColor"] = button_color

    # Write to DynamoDB
    try:
        table.put_item(Item=item)
        logger.info("Auth provider '%s' written to DynamoDB", provider_id)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        msg = f"Failed to write auth provider '{provider_id}' to DynamoDB: {error_code}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    # Write client secret to Secrets Manager
    try:
        try:
            response = secrets_client.get_secret_value(SecretId=secrets_arn)
            secrets = json.loads(response["SecretString"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                secrets = {}
            else:
                raise
        except (json.JSONDecodeError, KeyError):
            secrets = {}

        if provider_id not in secrets:
            secrets[provider_id] = client_secret
            secrets_client.put_secret_value(
                SecretId=secrets_arn,
                SecretString=json.dumps(secrets),
            )
            logger.info("Client secret for '%s' stored in Secrets Manager", provider_id)
        else:
            logger.info("Client secret for '%s' already in Secrets Manager — kept existing", provider_id)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        msg = f"Failed to write secret for '{provider_id}': {error_code}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    result.created = 1
    result.details.append(f"Auth provider '{provider_id}' created")
    return result


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


# Default Bedrock models to seed
DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "modelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
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
        "isReasoningModel": False,
        "supportsCaching": True,
        "isDefault": True,
    },
    {
        "modelId": "global.anthropic.claude-sonnet-4-6",
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
        "isReasoningModel": False,
        "supportsCaching": True,
        "isDefault": False,
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
            "isReasoningModel": model_def["isReasoningModel"],
            "supportsCaching": model_def["supportsCaching"],
            "isDefault": model_def["isDefault"],
            "createdAt": now,
            "updatedAt": now,
        }

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
]


def seed_system_admin_role(
    table_name: str,
    region: str,
) -> SeedResult:
    """Seed the system_admin role with DEFINITION, MODEL_GRANT#*, and TOOL_GRANT#*.

    This runs unconditionally (no JWT role required). The JWT mapping
    is handled separately by seed_system_admin_jwt_roles.
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
            msg = "system_admin role already exists — skipped"
            logger.info(msg)
            result.skipped = 1
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
        "jwtRoleMappings": [],
        "inheritsFrom": [],
        "grantedTools": ["*"],
        "grantedModels": ["*"],
        "effectivePermissions": {
            "tools": ["*"],
            "models": ["*"],
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
        result.details.append("system_admin role created with TOOL_GRANT#* and MODEL_GRANT#*")
    except ClientError as e:
        msg = f"Failed to create system_admin role: {e}"
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


def seed_system_admin_jwt_roles(
    table_name: str,
    region: str,
    jwt_role: str,
) -> SeedResult:
    """Seed JWT role mapping for the system_admin AppRole.

    Writes a JWT_MAPPING item to the app-roles table so that
    AppRoleService.resolve_user_permissions() can resolve users with the
    given JWT role to the system_admin AppRole via the JwtRoleMappingIndex GSI.

    If the system_admin role definition does not yet exist, the full role
    (DEFINITION + JWT_MAPPING + TOOL_GRANT + MODEL_GRANT items) is created.
    If the role exists and already has the correct mapping, the operation is
    skipped.  If it exists with a different mapping, the old mapping items
    are replaced.
    """
    result = SeedResult(category="system_admin_jwt")
    session = boto3.Session(region_name=region)
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)

    role_id = "system_admin"
    pk = f"ROLE#{role_id}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Check for existing role definition
    try:
        existing = table.get_item(Key={"PK": pk, "SK": "DEFINITION"})
    except ClientError as e:
        msg = f"Failed to check existing system_admin role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)
        return result

    if "Item" in existing:
        current_mappings = existing["Item"].get("jwtRoleMappings", [])
        if jwt_role in current_mappings:
            msg = f"system_admin already has JWT mapping '{jwt_role}' — skipped"
            logger.info(msg)
            result.skipped = 1
            result.details.append(msg)
            return result

        # Update: replace old JWT mappings with new one
        logger.info(
            "Updating system_admin JWT mappings: %s -> ['%s']",
            current_mappings,
            jwt_role,
        )

        # Delete old JWT_MAPPING items
        try:
            query_resp = table.query(
                KeyConditionExpression=(
                    boto3.dynamodb.conditions.Key("PK").eq(pk)
                    & boto3.dynamodb.conditions.Key("SK").begins_with("JWT_MAPPING#")
                ),
            )
            with table.batch_writer() as batch:
                for item in query_resp.get("Items", []):
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        except ClientError as e:
            msg = f"Failed to delete old JWT_MAPPING items: {e}"
            logger.error(msg)
            result.failed = 1
            result.details.append(msg)
            return result

        # Update DEFINITION item's jwtRoleMappings
        try:
            table.update_item(
                Key={"PK": pk, "SK": "DEFINITION"},
                UpdateExpression="SET jwtRoleMappings = :m, updatedAt = :u",
                ExpressionAttributeValues={
                    ":m": [jwt_role],
                    ":u": now,
                },
            )
        except ClientError as e:
            msg = f"Failed to update system_admin jwtRoleMappings: {e}"
            logger.error(msg)
            result.failed = 1
            result.details.append(msg)
            return result

        # Write new JWT_MAPPING item
        try:
            table.put_item(Item={
                "PK": pk,
                "SK": f"JWT_MAPPING#{jwt_role}",
                "GSI1PK": f"JWT_ROLE#{jwt_role}",
                "GSI1SK": pk,
                "roleId": role_id,
                "enabled": True,
            })
        except ClientError as e:
            msg = f"Failed to write JWT_MAPPING item for '{jwt_role}': {e}"
            logger.error(msg)
            result.failed = 1
            result.details.append(msg)
            return result

        result.created = 1
        result.details.append(
            f"system_admin JWT mapping updated to '{jwt_role}'"
        )
        return result

    # Role does not exist — create full system_admin role
    logger.info("system_admin role not found — creating with JWT mapping '%s'", jwt_role)

    definition_item: dict[str, Any] = {
        "PK": pk,
        "SK": "DEFINITION",
        "roleId": role_id,
        "displayName": "System Administrator",
        "description": "Full access to all system features. This role cannot be deleted.",
        "jwtRoleMappings": [jwt_role],
        "inheritsFrom": [],
        "grantedTools": ["*"],
        "grantedModels": ["*"],
        "effectivePermissions": {
            "tools": ["*"],
            "models": ["*"],
            "quotaTier": None,
        },
        "priority": 1000,
        "isSystemRole": True,
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "bootstrap-seed",
    }

    jwt_mapping_item = {
        "PK": pk,
        "SK": f"JWT_MAPPING#{jwt_role}",
        "GSI1PK": f"JWT_ROLE#{jwt_role}",
        "GSI1SK": pk,
        "roleId": role_id,
        "enabled": True,
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

    try:
        client = session.client("dynamodb")
        client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": table_name, "Item": _serialize(definition_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(jwt_mapping_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(tool_grant_item)}},
                {"Put": {"TableName": table_name, "Item": _serialize(model_grant_item)}},
            ]
        )
        result.created = 1
        result.details.append(
            f"system_admin role created with JWT mapping '{jwt_role}'"
        )
    except ClientError as e:
        msg = f"Failed to create system_admin role: {e}"
        logger.error(msg)
        result.failed = 1
        result.details.append(msg)

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
    auth_table = os.environ.get("DDB_AUTH_PROVIDERS_TABLE", "")
    quotas_table = os.environ.get("DDB_USER_QUOTAS_TABLE", "")
    models_table = os.environ.get("DDB_MANAGED_MODELS_TABLE", "")
    app_roles_table = os.environ.get("DDB_APP_ROLES_TABLE", "")
    secrets_arn = os.environ.get("SECRETS_AUTH_ARN", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    # Auth provider env vars (all optional — skip seeding if any missing)
    auth_provider_id = os.environ.get("SEED_AUTH_PROVIDER_ID", "")
    auth_display_name = os.environ.get("SEED_AUTH_DISPLAY_NAME", "")
    auth_issuer_url = os.environ.get("SEED_AUTH_ISSUER_URL", "")
    auth_client_id = os.environ.get("SEED_AUTH_CLIENT_ID", "")
    auth_client_secret = os.environ.get("SEED_AUTH_CLIENT_SECRET", "")
    auth_button_color = os.environ.get("SEED_AUTH_BUTTON_COLOR", "") or None

    results: list[SeedResult] = []

    # --- Auth provider seeding ---
    required_auth_var_names = [
        "SEED_AUTH_ISSUER_URL",
        "SEED_AUTH_CLIENT_ID",
        "SEED_AUTH_CLIENT_SECRET",
    ]
    missing_auth = [
        name for name in required_auth_var_names if not os.environ.get(name, "")
    ]

    if missing_auth:
        logger.warning(
            "Skipping auth provider seeding — missing env vars: %s",
            ", ".join(missing_auth),
        )
        result = SeedResult(category="auth_provider", skipped=1)
        result.details.append(f"Skipped — missing: {', '.join(missing_auth)}")
        results.append(result)
    else:
        results.append(
            seed_auth_provider(
                table_name=auth_table,
                secrets_arn=secrets_arn,
                region=region,
                provider_id=auth_provider_id or "default",
                display_name=auth_display_name or "Default Provider",
                issuer_url=auth_issuer_url,
                client_id=auth_client_id,
                client_secret=auth_client_secret,
                button_color=auth_button_color,
                discover=True,
            )
        )

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

    # --- Tool seeding ---
    results.append(seed_default_tools(table_name=app_roles_table, region=region))

    # --- System admin JWT role seeding ---
    admin_jwt_role = os.environ.get("SEED_ADMIN_JWT_ROLE", "")
    if admin_jwt_role:
        results.append(
            seed_system_admin_jwt_roles(
                table_name=app_roles_table,
                region=region,
                jwt_role=admin_jwt_role,
            )
        )
    else:
        logger.warning(
            "Skipping system admin JWT role seeding — SEED_ADMIN_JWT_ROLE not set"
        )
        r = SeedResult(category="system_admin_jwt", skipped=1)
        r.details.append("Skipped — SEED_ADMIN_JWT_ROLE not set")
        results.append(r)

    # --- Summary ---
    print_summary(results)

    total_failed = sum(r.failed for r in results)
    sys.exit(1 if total_failed else 0)


if __name__ == "__main__":
    main()
