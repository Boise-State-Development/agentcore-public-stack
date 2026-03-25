"""Shared authentication utilities for API projects."""

from .dependencies import get_current_user, security
from .models import User
from .state_store import StateStore, InMemoryStateStore, DynamoDBStateStore, create_state_store
from .rbac import require_app_roles, require_admin

__all__ = [
    "get_current_user",
    "security",
    "User",
    "StateStore",
    "InMemoryStateStore",
    "DynamoDBStateStore",
    "create_state_store",
    "require_app_roles",
    "require_admin",
]
