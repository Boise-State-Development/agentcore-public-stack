"""Credential storage, and the application identity every path derives from.

Two secrets are stored under two *services* rather than two accounts, so
revoking one cannot disturb the other:

============================ =========================================
``aps-tui``            API keys
``aps-tui-session``    Sealed BFF sessions (CLI device auth)
============================ =========================================

Two backends sit behind the same three functions (:func:`load`,
:func:`store`, :func:`delete`):

* **The OS keyring** (macOS Keychain, Windows Credential Manager, a Secret
  Service on Linux desktops). The default, and the only one used unless
  something selects the other.
* **An encrypted file** (:mod:`aps_tui.credential_file`) for hosts with no
  keyring at all: containers, VMs, SSH boxes. ``cli`` selects it before
  anything else runs, because unlocking it needs a passphrase prompt and a
  full-screen app cannot host ``getpass``. Callers never know which is active.

Reads degrade rather than raising: ``keyring`` raises on hosts with no Secret
Service, and that must not crash startup. Writes raise, because a caller who
asked to *store* a credential needs to know it failed.

``import keyring`` is deferred into each function. It is slow to import and can
fail at import time on those same hosts, and this module is imported on every
startup path including ``--version``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from platformdirs import user_data_path

from .errors import ConfigError

if TYPE_CHECKING:
    from .credential_file import CredentialFile

#: Application identity, used for keyring services and every platformdirs path.
APP_NAME = "aps-tui"

#: Keyring service under which API keys are stored.
API_KEY_SERVICE = APP_NAME

#: Keyring service for sealed BFF sessions from the CLI device-auth flow.
#: Separate from API keys so revoking one credential cannot disturb the other.
SESSION_SERVICE = f"{APP_NAME}-session"


class StorePreference(StrEnum):
    """``credential_store`` in the config file, or ``AGENTCORE_CREDENTIAL_STORE``."""

    #: The OS keyring when one works, otherwise the encrypted file.
    AUTO = "auto"
    #: Always the OS keyring, even if that means nothing can be stored.
    KEYRING = "keyring"
    #: Always the encrypted file, even where an OS keyring exists.
    FILE = "file"


def account_for(base_url: str) -> str:
    """Keyring account name — the base URL, so several deployments coexist."""
    return base_url.rstrip("/") or "default"


def credential_file_path() -> Path:
    """Where the encrypted credential file lives (it may not exist yet)."""
    return user_data_path(APP_NAME, appauthor=False, ensure_exists=False) / "credentials.enc"


def os_keyring_available() -> bool:
    """True when ``keyring`` found a backend that can actually hold secrets.

    ``fail.Keyring`` is what it selects with no Secret Service, and ``null``
    discards everything; neither can store a credential.
    """
    try:
        import keyring
        from keyring.backends import fail, null

        return not isinstance(keyring.get_keyring(), (fail.Keyring, null.Keyring))
    except Exception:  # pragma: no cover - depends on host keyring
        return False


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class _Active:
    """Which backend is in use. Module state, because the CLI picks once."""

    use_file: bool = False
    #: The unlocked file, or None when it is locked or does not exist yet.
    file: CredentialFile | None = None
    #: Why the file cannot be read, when it exists but is locked.
    locked_reason: str | None = None


def use_os_keyring() -> None:
    _Active.use_file, _Active.file, _Active.locked_reason = False, None, None


def use_file_store(store: CredentialFile | None, *, locked_reason: str | None = None) -> None:
    """Route every operation to the encrypted file.

    ``store`` is None when there is nothing to open yet (no file) or it could
    not be unlocked (``locked_reason`` says why).
    """
    _Active.use_file, _Active.file, _Active.locked_reason = True, store, locked_reason


def using_file_store() -> bool:
    return _Active.use_file


def file_store_unlocked() -> bool:
    return _Active.file is not None


def describe_backend() -> str:
    """Where credentials go, for messages such as "Session stored in …"."""
    if _Active.use_file:
        return f"the encrypted credential file ({credential_file_path()})"
    return "the OS keyring"


# ---------------------------------------------------------------------------
# The three operations
# ---------------------------------------------------------------------------


def load(service: str, base_url: str) -> tuple[str | None, str | None]:
    """Return ``(secret, unavailable_reason)``. Never raises."""
    if _Active.use_file:
        if _Active.file is None:
            return None, _Active.locked_reason
        return _Active.file.get(service, account_for(base_url)), None
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
    if _Active.use_file:
        if _Active.file is None:
            raise ConfigError(
                "The encrypted credential file is not unlocked, so nothing can be saved.",
                hint=_Active.locked_reason or "Run aps-tui from an interactive terminal so it can ask for the passphrase.",
            )
        try:
            _Active.file.set(service, account_for(base_url), secret)
        except OSError as exc:
            raise ConfigError(f"Could not write {_Active.file.path}: {exc}") from exc
        return
    try:
        import keyring

        keyring.set_password(service, account_for(base_url), secret)
    except Exception as exc:
        raise ConfigError(f"Could not write to the OS keyring ({type(exc).__name__}).", hint=hint) from exc


def delete(service: str, base_url: str) -> bool:
    """Remove a secret. False when there was nothing to remove."""
    if _Active.use_file:
        if _Active.file is None:
            return False
        try:
            return _Active.file.delete(service, account_for(base_url))
        except OSError:
            return False
    try:
        import keyring

        keyring.delete_password(service, account_for(base_url))
        return True
    except Exception:
        return False
