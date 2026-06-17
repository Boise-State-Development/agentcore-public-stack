"""Shared security utilities used across API services.

This package contains cross-cutting helpers for input validation, ownership
enforcement, and consistent error handling. Importable from ``app_api``,
``inference_api``, and agent code.
"""

from apis.shared.security.url_validator import (
    UrlValidationError,
    validate_external_url,
)
from apis.shared.security.ownership import (
    OwnershipError,
    require_session_owner,
    require_memory_owner,
    require_file_owner,
    register_ownership_handler,
)
from apis.shared.security.error_handler import (
    register_aws_client_error_handler,
    register_safe_500_handler,
    register_validation_error_handler,
)
from apis.shared.security.python_ast_policy import (
    PolicyError,
    validate_diagram_code,
)

__all__ = [
    "UrlValidationError",
    "validate_external_url",
    "OwnershipError",
    "require_session_owner",
    "require_memory_owner",
    "require_file_owner",
    "register_ownership_handler",
    "register_aws_client_error_handler",
    "register_safe_500_handler",
    "register_validation_error_handler",
    "PolicyError",
    "validate_diagram_code",
]
