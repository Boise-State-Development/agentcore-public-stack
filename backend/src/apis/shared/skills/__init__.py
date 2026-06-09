"""Shared skill-catalog utilities used by both app_api and the runtime.

The skills parallel of ``apis/shared/tools``: the DynamoDB-backed catalog of
admin-authored Skills (instruction bundles that bind catalog tools). Consumed
by ``app_api`` (admin authoring) and the runtime (``agents``/``inference_api``);
it never imports from either, per the import boundary.
"""

from .freshness import (
    get_all_skill_ids,
    get_freshness_hash,
    get_skill_updated_at,
    invalidate,
)
from .models import (
    SKILL_ID_PATTERN,
    SkillDefinition,
    SkillStatus,
    SkillVisibility,
)
from .repository import SkillCatalogRepository, get_skill_catalog_repository

__all__ = [
    "SKILL_ID_PATTERN",
    "SkillDefinition",
    "SkillStatus",
    "SkillVisibility",
    "SkillCatalogRepository",
    "get_skill_catalog_repository",
    "get_all_skill_ids",
    "get_freshness_hash",
    "get_skill_updated_at",
    "invalidate",
]
