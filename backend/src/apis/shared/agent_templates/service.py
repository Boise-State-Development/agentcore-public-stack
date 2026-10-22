"""Service layer for admin-managed agent templates.

Routes call this rather than the repository directly so business rules (public
enabled-only listing, seeding) have a single home. Mirrors
``system_prompts.service``.
"""

from typing import List, Optional

from .models import AgentTemplate, AgentTemplateCreate, AgentTemplateUpdate
from .repository import AgentTemplatesRepository, get_agent_templates_repository


class AgentTemplatesService:
    def __init__(self, repository: AgentTemplatesRepository):
        self._repo = repository

    async def list_templates(self, enabled_only: bool = False) -> List[AgentTemplate]:
        return await self._repo.list_templates(enabled_only=enabled_only)

    async def get_template(self, template_id: str) -> Optional[AgentTemplate]:
        return await self._repo.get_template(template_id)

    async def create_template(
        self, data: AgentTemplateCreate, created_by: Optional[str] = None
    ) -> AgentTemplate:
        return await self._repo.create_template(data, created_by=created_by)

    async def update_template(
        self, template_id: str, updates: AgentTemplateUpdate
    ) -> Optional[AgentTemplate]:
        return await self._repo.update_template(template_id, updates)

    async def delete_template(self, template_id: str) -> bool:
        return await self._repo.delete_template(template_id)


_service: Optional[AgentTemplatesService] = None


def get_agent_templates_service() -> AgentTemplatesService:
    global _service
    if _service is None:
        _service = AgentTemplatesService(get_agent_templates_repository())
    return _service
