"""Shared models module for managed model operations.

This module provides model management functionality shared between
app API and inference API deployments.
"""

from .models import (
    ManagedModel,
    ManagedModelCreate,
    ManagedModelUpdate,
)
from .managed_models import (
    create_managed_model,
    get_managed_model,
    list_managed_models,
    list_all_managed_models,
    get_default_managed_model,
    update_managed_model,
    delete_managed_model,
)

__all__ = [
    # Models
    "ManagedModel",
    "ManagedModelCreate",
    "ManagedModelUpdate",
    # Service functions
    "create_managed_model",
    "get_managed_model",
    "list_managed_models",
    "list_all_managed_models",
    "get_default_managed_model",
    "update_managed_model",
    "delete_managed_model",
]
