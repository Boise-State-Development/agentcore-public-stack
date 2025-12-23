"""Shared error models and utilities for consistent error handling across APIs"""

from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel


class ErrorCode(str, Enum):
    """Standard error codes for API responses"""

    # Client errors (4xx)
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    QUOTA_EXCEEDED = "quota_exceeded"

    # Server errors (5xx)
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"

    # Agent-specific errors
    AGENT_ERROR = "agent_error"
    TOOL_ERROR = "tool_error"
    MODEL_ERROR = "model_error"
    STREAM_ERROR = "stream_error"


class ErrorDetail(BaseModel):
    """Structured error detail for API responses"""

    code: ErrorCode
    message: str
    detail: Optional[str] = None
    field: Optional[str] = None  # For validation errors
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        use_enum_values = True


class StreamErrorEvent(BaseModel):
    """Error event format for SSE streams"""

    error: str  # User-friendly error message
    code: ErrorCode
    detail: Optional[str] = None
    recoverable: bool = False  # Whether client should retry
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        use_enum_values = True

    def to_sse_format(self) -> str:
        """Convert to SSE event format"""
        import json
        return f"event: error\ndata: {json.dumps(self.model_dump(exclude_none=True))}\n\n"


def create_error_response(
    code: ErrorCode,
    message: str,
    detail: Optional[str] = None,
    status_code: int = 500,
    metadata: Optional[Dict[str, Any]] = None
) -> dict:
    """
    Create a standardized error response dictionary.

    Args:
        code: Error code from ErrorCode enum
        message: User-friendly error message
        detail: Optional technical detail for debugging
        status_code: HTTP status code
        metadata: Optional additional error context

    Returns:
        Dictionary suitable for HTTPException detail
    """
    error = ErrorDetail(
        code=code,
        message=message,
        detail=detail,
        metadata=metadata
    )

    return {
        "error": error.model_dump(exclude_none=True),
        "status_code": status_code
    }


def http_status_to_error_code(status_code: int) -> ErrorCode:
    """Map HTTP status codes to ErrorCode enum values"""

    mapping = {
        400: ErrorCode.BAD_REQUEST,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        422: ErrorCode.VALIDATION_ERROR,
        429: ErrorCode.RATE_LIMIT_EXCEEDED,
        500: ErrorCode.INTERNAL_ERROR,
        503: ErrorCode.SERVICE_UNAVAILABLE,
        504: ErrorCode.TIMEOUT,
    }

    return mapping.get(status_code, ErrorCode.INTERNAL_ERROR)
