"""Device-authorization grants, so the terminal client can obtain a real session.

See :mod:`.models` for why this exists and what the two codes are for.
"""

from __future__ import annotations

from .models import (
    DeviceAuthorizationResponse,
    DeviceGrant,
    DevicePendingResponse,
    DeviceTokenRequest,
    DeviceTokenResponse,
    GrantStatus,
    generate_device_code,
    generate_user_code,
    hash_device_code,
    normalise_user_code,
)
from .repository import DeviceGrantRepository, get_device_grant_repository
from .service import (
    ApprovalOutcome,
    DeviceGrantService,
    derive_verification_uri,
    get_device_grant_service,
)

__all__ = [
    "ApprovalOutcome",
    "DeviceAuthorizationResponse",
    "DeviceGrant",
    "DeviceGrantRepository",
    "DeviceGrantService",
    "DevicePendingResponse",
    "DeviceTokenRequest",
    "DeviceTokenResponse",
    "GrantStatus",
    "derive_verification_uri",
    "generate_device_code",
    "generate_user_code",
    "get_device_grant_repository",
    "get_device_grant_service",
    "hash_device_code",
    "normalise_user_code",
]
