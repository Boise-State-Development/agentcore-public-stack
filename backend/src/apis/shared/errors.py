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
    """Error event format for SSE streams.

    This is the legacy error event format. For new implementations,
    use ConversationalErrorEvent which displays errors as assistant messages.
    """

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


class ConversationalErrorEvent(BaseModel):
    """SSE event for streaming errors as conversational assistant messages.

    Instead of returning HTTP errors, this event streams the error as an
    assistant message in the chat, providing better UX. The message is
    also persisted to session history.
    """

    type: str = "stream_error"
    code: ErrorCode
    message: str  # Markdown-formatted message to display as assistant response
    recoverable: bool = False
    retry_after: Optional[int] = None  # Seconds to wait before retry
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        use_enum_values = True

    def to_sse_format(self) -> str:
        """Convert to SSE event format"""
        import json
        return f"event: stream_error\ndata: {json.dumps(self.model_dump(exclude_none=True))}\n\n"


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


def build_conversational_error_event(
    code: ErrorCode,
    error: Exception,
    session_id: Optional[str] = None,
    recoverable: bool = False,
    retry_after: Optional[int] = None
) -> ConversationalErrorEvent:
    """Build a conversational error event for streaming as an assistant message.

    Creates a user-friendly markdown message based on the error type.

    Args:
        code: Error code from ErrorCode enum
        error: The exception that occurred
        session_id: Optional session ID for context
        recoverable: Whether the client should retry
        retry_after: Optional seconds to wait before retry

    Returns:
        ConversationalErrorEvent ready for SSE streaming
    """
    error_str = str(error)

    # Build conversational messages based on error content
    # Parse common error patterns to provide helpful context
    error_lower = error_str.lower()

    if code == ErrorCode.MODEL_ERROR:
        if "accessdenied" in error_lower or "access denied" in error_lower:
            message = f"""⚠️ I don't have access to complete this request.

> {error_str}

This usually means the model or feature you're trying to use isn't available. Try selecting a different model."""

        elif "throttl" in error_lower or "rate limit" in error_lower:
            message = f"""⚠️ I'm receiving too many requests right now.

> {error_str}

Please wait a moment and try again."""

        else:
            message = f"""⚠️ I ran into a problem with the AI model.

> {error_str}

Please try again, or try a different approach."""

    elif code == ErrorCode.TOOL_ERROR:
        message = f"""🔧 I had trouble using one of my tools.

> {error_str}

Try rephrasing your request or asking me to complete the task a different way."""

    elif code == ErrorCode.TIMEOUT:
        message = """⏱️ Your request took too long to process.

Try breaking it into smaller parts or simplifying your query."""

    elif code == ErrorCode.SERVICE_UNAVAILABLE:
        message = """🔌 I'm temporarily unavailable.

Please wait a moment and try again."""

    elif code == ErrorCode.STREAM_ERROR:
        if "accessdenied" in error_lower or "access denied" in error_lower:
            message = f"""⚠️ I don't have access to complete this request.

> {error_str}

This usually means the model or feature you're trying to use isn't available. Try selecting a different model."""

        elif "unsupported model" in error_lower:
            message = f"""⚠️ The selected model doesn't support this request.

> {error_str}

Try selecting a different model, or check that prompt caching is supported."""

        elif "prompt caching" in error_lower:
            message = f"""⚠️ There was a problem with prompt caching.

> {error_str}

Try disabling prompt caching or selecting a model that supports it."""

        else:
            message = f"""⚠️ Something went wrong while processing your request.

> {error_str}

Please try again."""

    else:
        # Generic error message
        message = f"""⚠️ Something went wrong.

> {error_str}

Please try again."""

    metadata = {}
    if session_id:
        metadata["session_id"] = session_id

    return ConversationalErrorEvent(
        code=code,
        message=message,
        recoverable=recoverable,
        retry_after=retry_after,
        metadata=metadata if metadata else None
    )
