"""Resource-ownership helpers and consistent 404 mapping.

Centralizes the "the requesting user does not own this object" check so every
call site enforces the same invariant and produces the same response shape.
The error is mapped to **HTTP 404, not 403**, deliberately: a 403 response
leaks the existence of the resource to non-owners, providing an enumeration
oracle. 404 makes a non-owner's request indistinguishable from a request for
a non-existent resource.

Helpers raise :class:`OwnershipError` on mismatch. Register
:func:`register_ownership_handler` on the FastAPI app to convert these to
404 responses with a generic detail.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Keys we know how to read ``owner_id``-style fields under, in priority order.
# Records may come from DynamoDB (camelCase) or domain models (snake_case),
# so accept both.
_OWNER_FIELDS: tuple[str, ...] = (
    "user_id",
    "userId",
    "owner_id",
    "ownerId",
    "owner",
)


class OwnershipError(Exception):
    """Raised when a request targets a resource the caller does not own."""

    def __init__(self, resource_kind: str = "resource") -> None:
        super().__init__(f"{resource_kind} not found")
        self.resource_kind = resource_kind


def _extract_owner(record: Any) -> str | None:
    """Return the owner identifier from *record*, or None if not present."""
    if record is None:
        return None
    # Pydantic / dataclass / ORM
    for attr in _OWNER_FIELDS:
        val = getattr(record, attr, None)
        if val is not None:
            return str(val)
    # Mapping (DynamoDB item, dict)
    if isinstance(record, dict):
        for key in _OWNER_FIELDS:
            if key in record and record[key] is not None:
                return str(record[key])
    return None


def _require_owner(user_id: str, record: Any, resource_kind: str) -> None:
    """Internal: raise OwnershipError unless ``record`` belongs to ``user_id``.

    Treats a missing record (None) and an owner mismatch identically — both
    surface as a 404 to the caller. The distinction is logged server-side.
    """
    if not user_id:
        raise OwnershipError(resource_kind)
    if record is None:
        raise OwnershipError(resource_kind)
    owner = _extract_owner(record)
    if owner is None:
        # Defensive: a record without an identifiable owner is treated as
        # not-found for safety. Log so this surfaces in operations.
        logger.warning(
            "Ownership check on %s record had no recognized owner field; treating as not found",
            resource_kind,
        )
        raise OwnershipError(resource_kind)
    if owner != user_id:
        raise OwnershipError(resource_kind)


def require_session_owner(user_id: str, session_record: Any) -> None:
    """Raise OwnershipError unless *session_record* belongs to *user_id*."""
    _require_owner(user_id, session_record, "session")


def require_memory_owner(user_id: str, record: Any) -> None:
    """Raise OwnershipError unless the memory *record* belongs to *user_id*.

    Memory records are namespaced (e.g. ``/preferences/{user_id}``); pass the
    record's namespace string as ``record`` and we'll match the trailing
    segment, or pass an object with an ``owner_id``/``user_id`` attribute.
    """
    if isinstance(record, str):
        # Namespace path — owner is the last path segment.
        if not record:
            raise OwnershipError("memory record")
        owner_segment = record.rstrip("/").rsplit("/", 1)[-1]
        if owner_segment != user_id:
            raise OwnershipError("memory record")
        return
    _require_owner(user_id, record, "memory record")


def require_file_owner(user_id: str, file_record: Any) -> None:
    """Raise OwnershipError unless *file_record* belongs to *user_id*."""
    _require_owner(user_id, file_record, "file")


def register_ownership_handler(app: Any) -> None:
    """Register a FastAPI exception handler that maps OwnershipError to 404.

    Imported lazily so this module remains importable without FastAPI in
    contexts that only need the helper functions.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def _handler(_request: Request, exc: OwnershipError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{exc.resource_kind.capitalize()} not found."},
        )

    app.add_exception_handler(OwnershipError, _handler)
