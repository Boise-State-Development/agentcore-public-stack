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
    AddRemoveSkillRolesRequest,
    AdminSkillListResponse,
    AdminSkillResponse,
    SetSkillRolesRequest,
    SkillCreateRequest,
    SkillDefinition,
    SkillResourceRef,
    SkillResourcesResponse,
    SkillRoleAssignment,
    SkillRolesResponse,
    SkillStatus,
    SkillUpdateRequest,
    SkillVisibility,
)
from .access import resolve_accessible_skill_ids
from .models import UserSkillPreference
from .repository import SkillCatalogRepository, get_skill_catalog_repository
from .resource_store import (
    SkillResourceStore,
    SkillResourceStoreError,
    get_skill_resource_store,
)

__all__ = [
    "SKILL_ID_PATTERN",
    "SkillDefinition",
    "SkillResourceRef",
    "SkillResourcesResponse",
    "SkillStatus",
    "SkillVisibility",
    "SkillCreateRequest",
    "SkillUpdateRequest",
    "SkillRoleAssignment",
    "SkillRolesResponse",
    "SetSkillRolesRequest",
    "AddRemoveSkillRolesRequest",
    "AdminSkillResponse",
    "AdminSkillListResponse",
    "SkillCatalogRepository",
    "get_skill_catalog_repository",
    "SkillResourceStore",
    "SkillResourceStoreError",
    "get_skill_resource_store",
    "get_all_skill_ids",
    "get_freshness_hash",
    "get_skill_updated_at",
    "invalidate",
    "resolve_accessible_skill_ids",
    "UserSkillPreference",
]
