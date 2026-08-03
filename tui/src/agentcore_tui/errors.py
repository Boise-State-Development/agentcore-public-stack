"""Typed errors for the AgentCore terminal client.

Every error carries a ``hint`` — the actionable next step shown to the user in
the TUI's status bar. A raw HTTP status is useless in a terminal; "your API key
expired, run `agentcore-tui login`" is not.

The status mapping mirrors what ``/chat/api-converse`` actually returns (see
``backend/src/apis/app_api/chat/converse_routes.py``):

===== ==========================================================
401   Invalid or expired API key
403   RBAC denied access to the requested model
400   Bad request — malformed messages, unknown model, content policy
429   Per-key rate limit (60/min) or quota exhaustion; may set Retry-After
502   Upstream Bedrock/Mantle failure
===== ==========================================================
"""

from __future__ import annotations


class AgentCoreTuiError(Exception):
    """Base class for every error this client raises."""

    #: Actionable remediation shown to the user alongside the message.
    hint: str = ""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message} — {self.hint}"
        return self.message


class ConfigError(AgentCoreTuiError):
    """Configuration is missing or unusable (no base URL, no API key, bad TOML)."""


class ApiError(AgentCoreTuiError):
    """An HTTP-level failure from app-api."""

    def __init__(self, message: str, *, status_code: int, hint: str = "") -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code


class AuthError(ApiError):
    """401 — the API key was rejected."""

    hint = "Keys expire after 90 days. Mint a new one in Settings -> API Keys, then run `agentcore-tui login`."

    def __init__(self, message: str = "API key was rejected") -> None:
        super().__init__(message, status_code=401)


class ModelAccessDeniedError(ApiError):
    """403 — RBAC forbids this model for the key owner's roles."""

    def __init__(self, model_id: str) -> None:
        super().__init__(
            f"Your account is not permitted to use {model_id}",
            status_code=403,
            hint="Pick another model with F2, or ask an administrator to grant your role access to this model.",
        )
        self.model_id = model_id


class BadRequestError(ApiError):
    """400 — the request was malformed or rejected by content policy."""

    def __init__(self, message: str = "The request was rejected as invalid") -> None:
        super().__init__(
            message,
            status_code=400,
            hint="Check the model ID is correct and that the prompt does not violate content policy.",
        )


class RateLimitedError(ApiError):
    """429 — per-key rate limit (60 req/min) or an exhausted cost quota."""

    def __init__(self, message: str = "Rate limit or quota exceeded", *, retry_after: int | None = None) -> None:
        wait = f"Retry in {retry_after}s." if retry_after else "Wait a moment and retry."
        super().__init__(message, status_code=429, hint=f"{wait} The endpoint allows 60 requests per minute per key.")
        self.retry_after = retry_after


class UpstreamError(ApiError):
    """502 — the model provider failed behind app-api."""

    def __init__(self, message: str = "The model provider failed to respond") -> None:
        super().__init__(message, status_code=502, hint="This is a server-side failure. Retrying usually helps.")


class StreamError(AgentCoreTuiError):
    """The SSE stream delivered an ``error`` event, or ended unexpectedly."""

    hint = "The turn was interrupted. Your message history is preserved — press Enter to retry."


class ConnectionFailedError(AgentCoreTuiError):
    """The base URL was unreachable (DNS, TLS, refused, timeout)."""

    def __init__(self, base_url: str, detail: str) -> None:
        super().__init__(
            f"Could not reach {base_url}: {detail}",
            hint="Check the base URL and your network or VPN. Set it with `agentcore-tui login --base-url ...`.",
        )
        self.base_url = base_url
