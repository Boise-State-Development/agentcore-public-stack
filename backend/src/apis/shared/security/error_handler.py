"""Centralized exception handlers for upstream-service and unhandled errors.

These handlers ensure the response body never reflects internal details:

* AWS API ``ClientError`` messages frequently contain regex patterns,
  enum value sets, internal parameter names, and the user-supplied input
  itself. Surfacing them to clients leaks implementation detail and provides
  an XSS reflection sink. The handler maps validation-shaped errors to a
  generic 400 and other client errors to a generic 502.
* Unhandled exceptions are mapped to a generic 500 ``{"detail": "Internal
  server error"}`` with the full traceback logged server-side.

Both handlers use the module logger at WARN/ERROR for server-side visibility.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Botocore error codes that represent client-input problems and should
# surface as HTTP 400 (with a generic body).
_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ValidationException",
        "InvalidParameterValue",
        "InvalidParameterCombination",
        "InvalidArgumentException",
        "InvalidRequestException",
        "MalformedQueryString",
    }
)

_GENERIC_BAD_REQUEST = "Invalid request parameters."
_GENERIC_UPSTREAM_ERROR = "Upstream service error."
_GENERIC_INTERNAL_ERROR = "Internal server error."
_GENERIC_VALIDATION_ERROR = "Invalid request parameters."


def register_validation_error_handler(app: Any) -> None:
    """Install a FastAPI handler for ``RequestValidationError`` that
    returns a generic body.

    FastAPI's default 422 response includes the offending input value,
    the field path, and (for regex/length validators) the pattern or
    bound the value violated. Echoing user input back creates an XSS
    reflection sink and leaks server-side validation rules. This
    handler replaces the body with a generic
    ``{"detail": "Invalid request parameters."}`` while preserving the
    422 status; the offending input and structural reason are still
    logged server-side at WARN for operator visibility.
    """
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    async def _handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning(
            "Request validation error on %s %s: %s",
            request.method,
            request.url.path,
            # exc.errors() carries the structural detail; safe to log,
            # never echoed in the response.
            exc.errors(),
        )
        return JSONResponse(status_code=422, content={"detail": _GENERIC_VALIDATION_ERROR})

    app.add_exception_handler(RequestValidationError, _handler)


def register_aws_client_error_handler(app: Any) -> None:
    """Install a FastAPI handler for ``botocore.exceptions.ClientError``.

    * ``ValidationException`` (and similar input-shape errors) → HTTP 400 with
      ``{"detail": "Invalid request parameters."}``.
    * Any other ``ClientError`` → HTTP 502 with
      ``{"detail": "Upstream service error."}``.

    The full AWS error message is logged at WARN; it never appears in the
    response body.
    """
    from botocore.exceptions import ClientError
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def _handler(request: Request, exc: ClientError) -> JSONResponse:
        error_response = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
        code = error_response.get("Code", "")
        # Log the full message server-side for operators. Path is included so
        # the entry is correlatable; the message itself never leaves the log.
        logger.warning(
            "AWS ClientError on %s %s: code=%s message=%r",
            request.method,
            request.url.path,
            code,
            error_response.get("Message", str(exc)),
        )
        if code in _CLIENT_ERROR_CODES:
            return JSONResponse(status_code=400, content={"detail": _GENERIC_BAD_REQUEST})
        return JSONResponse(status_code=502, content={"detail": _GENERIC_UPSTREAM_ERROR})

    app.add_exception_handler(ClientError, _handler)


def register_safe_500_handler(app: Any) -> None:
    """Install a FastAPI handler that maps unhandled exceptions to a generic 500.

    The handler is intentionally conservative: it only catches
    :class:`Exception` (not :class:`BaseException`), so KeyboardInterrupt and
    SystemExit propagate normally. The full traceback is logged at ERROR.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(status_code=500, content={"detail": _GENERIC_INTERNAL_ERROR})

    app.add_exception_handler(Exception, _handler)


def register_role_mutation_forbidden_handler(app: Any) -> None:
    """Install a FastAPI handler mapping ``RoleMutationForbidden`` to 403.

    Raised when a delegated admin tries to mutate a role that carries
    administrative power (a protected role, or one with ``grantedAdminScopes``)
    from a resource surface such as the tool/model/skill role pickers.

    It needs its own handler because those routes catch ``ValueError`` and map
    it to 400 — a denied privilege escalation reported as "bad request" is both
    misleading to the caller and invisible in a 4xx-by-status dashboard. The
    exception's message is safe to return: it names only the role id the caller
    just submitted, and the escalation is denied either way.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from apis.shared.rbac.role_constraints import RoleMutationForbidden

    async def _handler(request: Request, exc: RoleMutationForbidden) -> JSONResponse:
        logger.warning(
            "Role mutation forbidden on %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.add_exception_handler(RoleMutationForbidden, _handler)
