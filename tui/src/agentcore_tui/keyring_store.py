"""OS keyring access, and the application identity every path derives from.

Both existed twice before this: ``config`` and ``auth.tokens`` each implemented
get/set/delete with identical degradation logic, and ``APP_NAME`` was defined in
``config`` and imported by ``logging_setup``, making the lowest-level module
depend on a higher-level one.

Two secrets are stored under two *services* rather than two accounts, so
revoking one cannot disturb the other:

============================ =========================================
``agentcore-tui``            API keys
``agentcore-tui-session``    Sealed BFF sessions (CLI device auth)
============================ =========================================

A third service, ``agentcore-tui-sso``, held OIDC refresh tokens for the CLI
Cognito client that was reverted in #850. It is still *deleted* on logout so a
user upgrading from that build does not leave a live refresh token in their
keyring, but nothing writes it any more. Remove ``LEGACY_SSO_SERVICE`` once no
one can plausibly be carrying one.

Every operation degrades rather than raising on read. ``keyring`` raises on
Linux hosts with no Secret Service — headless servers, containers, CI — and
that must fall back to environment variables instead of crashing. Writes are
the exception: a caller asking to *store* a credential needs to know it failed.

``import keyring`` is deferred into each function. It is slow to import and can
fail at import time on those same hosts, and this module is imported on every
startup path including ``--version``.
"""

from __future__ import annotations

from .errors import ConfigError

#: Application identity, used for keyring services and every platformdirs path.
APP_NAME = "agentcore-tui"

#: Keyring service under which API keys are stored.
API_KEY_SERVICE = APP_NAME

#: Keyring service for sealed BFF sessions from the CLI device-auth flow.
#: Separate from API keys so revoking one credential cannot disturb the other.
SESSION_SERVICE = f"{APP_NAME}-session"

#: Retired: OIDC refresh tokens for the reverted CLI Cognito client (#850).
#: Read and deleted on logout only, so an upgrading user does not silently keep
#: a live refresh token. Never written.
LEGACY_SSO_SERVICE = f"{APP_NAME}-sso"


def account_for(base_url: str) -> str:
    """Keyring account name — the base URL, so several deployments coexist."""
    return base_url.rstrip("/") or "default"


def load(service: str, base_url: str) -> tuple[str | None, str | None]:
    """Return ``(secret, unavailable_reason)``. Never raises."""
    try:
        import keyring

        return keyring.get_password(service, account_for(base_url)), None
    except Exception as exc:  # pragma: no cover - depends on host keyring
        return None, f"{type(exc).__name__}: {exc}"


def store(service: str, base_url: str, secret: str, *, hint: str) -> None:
    """Persist a secret, or raise :class:`ConfigError` carrying ``hint``.

    Raises where :func:`load` degrades: someone who asked to save a credential
    has to be told it was not saved.
    """
    try:
        import keyring

        keyring.set_password(service, account_for(base_url), secret)
    except Exception as exc:
        raise ConfigError(f"Could not write to the OS keyring ({type(exc).__name__}).", hint=hint) from exc


def delete(service: str, base_url: str) -> bool:
    """Remove a secret. False when there was nothing to remove."""
    try:
        import keyring

        keyring.delete_password(service, account_for(base_url))
        return True
    except Exception:
        return False
