"""Service layer for admin-managed system prompts.

Routes call into this service rather than the repository directly so that
business rules (enabled-only validation for sessions, future caching, etc.)
have a single home. Mirrors the layout of ``user_menu_links.service``.
"""

from typing import List, Optional

from .models import SystemPrompt, SystemPromptCreate, SystemPromptUpdate
from .repository import SystemPromptsRepository, get_system_prompts_repository


class SystemPromptsService:
    def __init__(self, repository: SystemPromptsRepository):
        self._repo = repository

    async def list_prompts(self, enabled_only: bool = False) -> List[SystemPrompt]:
        return await self._repo.list_prompts(enabled_only=enabled_only)

    async def get_prompt(self, prompt_id: str) -> Optional[SystemPrompt]:
        return await self._repo.get_prompt(prompt_id)

    async def get_enabled_prompt(self, prompt_id: str) -> Optional[SystemPrompt]:
        """Return the prompt only if it exists AND is enabled. Used by the
        session-update validation and by the inference path before appending
        a custom prompt to the system prompt."""
        prompt = await self._repo.get_prompt(prompt_id)
        if not prompt or prompt.status != "enabled":
            return None
        return prompt

    async def create_prompt(
        self, data: SystemPromptCreate, created_by: Optional[str] = None
    ) -> SystemPrompt:
        return await self._repo.create_prompt(data, created_by=created_by)

    async def update_prompt(
        self, prompt_id: str, updates: SystemPromptUpdate
    ) -> Optional[SystemPrompt]:
        return await self._repo.update_prompt(prompt_id, updates)

    async def delete_prompt(self, prompt_id: str) -> bool:
        return await self._repo.delete_prompt(prompt_id)


_service: Optional[SystemPromptsService] = None


def get_system_prompts_service() -> SystemPromptsService:
    global _service
    if _service is None:
        _service = SystemPromptsService(get_system_prompts_repository())
    return _service
