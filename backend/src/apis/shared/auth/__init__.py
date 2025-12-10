"""Shared authentication utilities for API projects."""

from .dependencies import get_current_user, security
from .jwt_validator import EntraIDJWTValidator, get_validator
from .models import User
from .state_store import StateStore, InMemoryStateStore, DynamoDBStateStore, create_state_store
from .rbac import (
    require_roles,
    require_all_roles,
    has_any_role,
    has_all_roles,
    require_admin,
    require_faculty,
    require_staff,
    require_developer,
    require_aws_ai_access,
)

__all__ = [
    "get_current_user",
    "security",
    "EntraIDJWTValidator",
    "get_validator",
    "User",
    "StateStore",
    "InMemoryStateStore",
    "DynamoDBStateStore",
    "create_state_store",
    "require_roles",
    "require_all_roles",
    "has_any_role",
    "has_all_roles",
    "require_admin",
    "require_faculty",
    "require_staff",
    "require_developer",
    "require_aws_ai_access",
]





