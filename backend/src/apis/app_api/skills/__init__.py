"""Skills API module (app_api-specific service).

Models and repository live in apis.shared.skills. The service lives here as it
is app_api-specific (admin authoring + RBAC sync), mirroring apis.app_api.tools.
"""

from apis.shared.skills.models import (
    AddRemoveSkillRolesRequest,
    AdminSkillListResponse,
    AdminSkillResponse,
    SetSkillRolesRequest,
    SkillCreateRequest,
    SkillDefinition,
    SkillRoleAssignment,
    SkillRolesResponse,
    SkillStatus,
    SkillUpdateRequest,
    SkillVisibility,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)

from .service import SkillCatalogService, get_skill_catalog_service

__all__ = [
    "SkillDefinition",
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
    "SkillCatalogService",
    "get_skill_catalog_service",
]
