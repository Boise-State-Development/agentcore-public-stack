"""DynamoDB repository for admin-managed agent templates.

PK ``TEMPLATE#<template_id>``, SK ``METADATA``. Listing is a full Scan — the
catalog is small (tens of items at most) so a GSI is unnecessary, matching the
``system_prompts`` repository this is modelled on.
"""

import asyncio
import logging
import os
import re
import uuid
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from apis.shared.caching import config_cache
from apis.shared.timestamps import utc_now_iso

from .models import (
    AgentTemplate,
    AgentTemplateCreate,
    AgentTemplateUpdate,
    TemplateBinding,
    TemplateModelConfig,
)

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Derive a URL/DynamoDB-safe template id from a name.

    Lowercased, non-alphanumeric runs collapsed to a single hyphen, trimmed.
    Falls back to a uuid when the name has no usable characters.
    """
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or f"template-{uuid.uuid4().hex[:8]}"


class AgentTemplatesRepository:
    """CRUD for agent templates in DynamoDB.

    Full Scan is used for listing — the catalog is expected to be small, so
    Scan is appropriate and avoids a GSI.
    """

    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        self._table_name = table_name or os.getenv("DYNAMODB_AGENT_TEMPLATES_TABLE_NAME")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._table_name)

        if not self._enabled:
            logger.warning(
                "DYNAMODB_AGENT_TEMPLATES_TABLE_NAME not set. "
                "Agent templates repository is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._dynamodb = session.resource("dynamodb", region_name=self._region)
        else:
            self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self._table_name)
        logger.info(f"Initialized agent templates repository: table={self._table_name}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def list_templates(self, enabled_only: bool = False) -> List[AgentTemplate]:
        """Return all templates, sorted by (sort_order, name).

        When ``enabled_only`` is set, disabled templates are filtered out — this
        is what the public picker endpoint uses.
        """
        if not self._enabled:
            return []

        try:
            items = await config_cache.get_or_load(
                config_cache.AGENT_TEMPLATES,
                lambda: asyncio.to_thread(self._scan_template_items),
            )
        except ClientError:
            logger.error("Error listing agent templates", exc_info=True)
            raise

        templates = [AgentTemplate.from_dynamo_item(item) for item in items]
        if enabled_only:
            templates = [t for t in templates if t.status == "enabled"]
        templates.sort(key=lambda t: (t.sort_order, t.name.lower()))
        return templates

    def _scan_template_items(self) -> List[dict]:
        """Scan the raw TEMPLATE# items. Blocking; call via ``asyncio.to_thread``."""
        response = self._table.scan(
            FilterExpression="SK = :sk",
            ExpressionAttributeValues={":sk": "METADATA"},
        )
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = self._table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "METADATA"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))
        return items

    async def get_template(self, template_id: str) -> Optional[AgentTemplate]:
        """Return a single template by id, or None if not found."""
        if not self._enabled:
            return None
        try:
            response = self._table.get_item(
                Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"}
            )
            item = response.get("Item")
            if not item:
                return None
            return AgentTemplate.from_dynamo_item(item)
        except ClientError:
            logger.error("Error getting agent template", exc_info=True)
            raise

    async def create_template(
        self, data: AgentTemplateCreate, created_by: Optional[str] = None
    ) -> AgentTemplate:
        """Create a template and return it.

        Raises ``ValueError`` if the resolved id already exists (surfaced as a
        409 by the route).
        """
        if not self._enabled:
            raise RuntimeError("Agent templates repository is not enabled")

        template_id = (data.template_id or slugify(data.name)).strip()
        now = utc_now_iso()
        template = AgentTemplate(
            template_id=template_id,
            name=data.name,
            description=data.description,
            emoji=data.emoji,
            instructions=data.instructions,
            tags=list(data.tags),
            starters=list(data.starters),
            model_config_=TemplateModelConfig(
                model_id=data.model_cfg.model_id,
                params=dict(data.model_cfg.params or {}),
            ),
            bindings=[
                TemplateBinding(kind=b.kind, ref=b.ref, config=dict(b.config or {}))
                for b in data.bindings
            ],
            pitch=data.pitch,
            status=data.status,
            sort_order=data.sort_order,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            updated_by=created_by,
        )

        try:
            self._table.put_item(
                Item=template.to_dynamo_item(),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ValueError(f"Template '{template_id}' already exists")
            logger.error("Error creating agent template", exc_info=True)
            raise

        config_cache.invalidate(config_cache.AGENT_TEMPLATES)
        logger.info(f"Created agent template: {template_id} name={template.name!r}")
        return template

    async def update_template(
        self, template_id: str, updates: AgentTemplateUpdate
    ) -> Optional[AgentTemplate]:
        """Apply a partial update. Returns None if not found.

        A conditional put guards against TOCTOU resurrection — if another admin
        deletes the row between our read and write, the put fails rather than
        recreating it.
        """
        if not self._enabled:
            return None

        existing = await self.get_template(template_id)
        if not existing:
            return None

        data = updates.model_dump(exclude_none=True, by_alias=False)
        # model config is nested — apply it onto the dataclass explicitly.
        mc = data.pop("model_cfg", None)
        if mc is not None:
            existing.model_config_ = TemplateModelConfig(
                model_id=mc.get("model_id"),
                params=dict(mc.get("params") or {}),
            )
        bindings = data.pop("bindings", None)
        if bindings is not None:
            existing.bindings = [
                TemplateBinding(
                    kind=b.get("kind", ""),
                    ref=b.get("ref", ""),
                    config=dict(b.get("config") or {}),
                )
                for b in bindings
            ]
        for field_name, value in data.items():
            setattr(existing, field_name, value)
        existing.updated_at = utc_now_iso()

        try:
            self._table.put_item(
                Item=existing.to_dynamo_item(),
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning(f"Agent template {template_id} disappeared during update")
                return None
            logger.error("Error updating agent template", exc_info=True)
            raise

        config_cache.invalidate(config_cache.AGENT_TEMPLATES)
        logger.info(f"Updated agent template: {template_id}")
        return existing

    async def delete_template(self, template_id: str) -> bool:
        """Delete a template. Returns True if deleted, False if not found."""
        if not self._enabled:
            return False
        existing = await self.get_template(template_id)
        if not existing:
            return False
        try:
            self._table.delete_item(
                Key={"PK": f"TEMPLATE#{template_id}", "SK": "METADATA"}
            )
        except ClientError:
            logger.error("Error deleting agent template", exc_info=True)
            raise
        config_cache.invalidate(config_cache.AGENT_TEMPLATES)
        logger.info(f"Deleted agent template: {template_id}")
        return True


_repository: Optional[AgentTemplatesRepository] = None


def get_agent_templates_repository() -> AgentTemplatesRepository:
    global _repository
    if _repository is None:
        _repository = AgentTemplatesRepository()
    return _repository
