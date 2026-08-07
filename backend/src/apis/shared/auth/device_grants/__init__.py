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

__all__ = [
    "DeviceAuthorizationResponse",
    "DeviceGrant",
    "DevicePendingResponse",
    "DeviceTokenRequest",
    "DeviceTokenResponse",
    "GrantStatus",
    "generate_device_code",
    "generate_user_code",
    "hash_device_code",
    "normalise_user_code",
]
