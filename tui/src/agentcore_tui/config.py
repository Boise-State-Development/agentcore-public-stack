"""Configuration and credential resolution.

Resolution order, highest priority first:

1. Explicit CLI flags (``--base-url``, ``--model``)
2. Environment variables (``AGENTCORE_BASE_URL``, ``AGENTCORE_API_KEY``, ``AGENTCORE_MODEL_ID``)
3. The user config file (TOML)
4. The OS keyring (API key only)
5. Built-in defaults

The API key is *never* written to the config file by this client — it goes to
the OS keyring, scoped to the base URL so one machine can talk to several
deployments. A key in the config file is still honoured (some CI environments
have no keyring) but the loader flags it so the UI can warn.

Config file locations, courtesy of ``platformdirs``:

========= ==================================================================
Linux     ``~/.config/agentcore-tui/config.toml``
macOS     ``~/Library/Application Support/agentcore-tui/config.toml``
Windows   ``%APPDATA%\\agentcore-tui\\config.toml``
========= ==================================================================
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from platformdirs import user_config_path

from . import keyring_store
from .credentials import Capability, CredentialSource, SessionProbe, resolve_source
from .errors import ConfigError
from .keyring_store import APP_NAME

#: Kept as a re-export: callers imported this from here before it moved.
KEYRING_SERVICE = keyring_store.API_KEY_SERVICE

ENV_BASE_URL = "AGENTCORE_BASE_URL"
ENV_API_KEY = "AGENTCORE_API_KEY"
ENV_MODEL_ID = "AGENTCORE_MODEL_ID"
ENV_COGNITO_DOMAIN = "AGENTCORE_COGNITO_DOMAIN_URL"
ENV_CLI_CLIENT_ID = "AGENTCORE_CLI_CLIENT_ID"
ENV_BANNER = "AGENTCORE_BANNER"

#: Loopback ports registered on the CLI app client by CDK. Cognito matches
#: redirect URIs byte-for-byte and does not honour RFC 8252's variable-port
#: rule, so these must agree with `cognito.cliClient.callbackPorts` in
#: infrastructure config.
DEFAULT_CALLBACK_PORTS: tuple[int, ...] = (8976, 8977, 8978)

#: Bedrock model IDs known to this codebase. These mirror the platform's own
#: defaults (``agents/main_agent/config/constants.py``) so the picker is useful
#: out of the box. `/models` is cookie-session authenticated and therefore
#: unreachable with an API key alone, so the list cannot be discovered at
#: runtime in Phase 1 — override it in the config file for your deployment.
DEFAULT_MODELS: tuple[str, ...] = (
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-7-20260115-v1:0",
)

DEFAULT_MODEL_ID = DEFAULT_MODELS[0]
DEFAULT_MAX_TOKENS = 4096

#: Generous, because a single turn on a large model can legitimately run long.
DEFAULT_TIMEOUT_SECONDS = 300.0


def config_path() -> Path:
    """Return the platform-correct config file path (may not exist)."""
    return user_config_path(APP_NAME, appauthor=False, ensure_exists=False) / "config.toml"


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved client configuration."""

    base_url: str = ""
    # repr=False so an accidental log/traceback of the config never leaks the key.
    api_key: str | None = field(default=None, repr=False)
    model_id: str = DEFAULT_MODEL_ID
    models: tuple[str, ...] = DEFAULT_MODELS
    system_prompt: str | None = None
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    top_p: float | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: True when the key came from the config file rather than env/keyring.
    api_key_from_plaintext_file: bool = False
    #: Set when the OS keyring exists but could not be read.
    keyring_unavailable_reason: str | None = None
    # -- SSO (authorization-code + PKCE against the public CLI app client) ----
    #: Cognito hosted-UI domain, e.g. https://<prefix>.auth.<region>.amazoncognito.com
    cognito_domain_url: str | None = None
    #: Public CLI app client id; SSM /<prefix>/auth/cognito/cli-app-client-id
    cli_client_id: str | None = None
    #: Loopback ports registered on that client. Cognito matches redirect URIs
    #: exactly, so only these can be bound.
    callback_ports: tuple[int, ...] = DEFAULT_CALLBACK_PORTS
    # -- startup banner ------------------------------------------------------
    #: Master switch. Defaults to *off* here but to *on* in
    #: :func:`resolve_config`, which is the only path production takes. A
    #: directly-constructed Config — tests, or anything embedding this client —
    #: therefore gets no animation it did not ask for.
    banner: bool = False
    #: Set by ``--banner`` to replay it regardless of what state records.
    force_banner: bool = False
    # -- credentials ---------------------------------------------------------
    #: Which credential this client will present. Resolved by
    #: :func:`resolve_config`; the dataclass default is NONE so a
    #: directly-constructed Config is explicit about being unauthenticated.
    #: Never infer this from ``api_key`` being set — under SSO it will not be.
    credential_source: CredentialSource = CredentialSource.NONE

    @property
    def sso_configured(self) -> bool:
        """True when there is enough configuration to attempt an SSO login."""
        return bool(self.base_url and self.cognito_domain_url and self.cli_client_id)

    @property
    def is_complete(self) -> bool:
        """True when there is enough configuration to make a request.

        Asks the credential discriminant rather than checking ``api_key``: an
        absent API key is normal for an SSO session, and testing the field
        directly would tell a signed-in user they are not configured.
        """
        return bool(self.base_url) and self.credential_source.usable

    def can(self, capability: Capability) -> bool:
        """True when the resolved credential can reach ``capability``.

        Lets the UI disable a feature it cannot serve instead of issuing a
        request that is certain to 401.
        """
        return self.credential_source.can(capability)

    def missing(self) -> list[str]:
        """Names of the required settings that are absent."""
        gaps = []
        if not self.base_url:
            gaps.append("base URL")
        if not self.credential_source.usable:
            gaps.append("API key")
        return gaps

    def with_model(self, model_id: str) -> Config:
        """Return a copy using a different model."""
        return replace(self, model_id=model_id)


# ---------------------------------------------------------------------------
# Keyring access
# ---------------------------------------------------------------------------
#
# Thin wrappers over :mod:`agentcore_tui.keyring_store`, which owns the
# degradation behaviour shared with the SSO token store.


def load_key_from_keyring(base_url: str) -> tuple[str | None, str | None]:
    """Return ``(api_key, unavailable_reason)`` from the OS keyring."""
    return keyring_store.load(keyring_store.API_KEY_SERVICE, base_url)


def save_key_to_keyring(base_url: str, api_key: str) -> None:
    """Persist an API key to the OS keyring, or raise ConfigError."""
    keyring_store.store(
        keyring_store.API_KEY_SERVICE,
        base_url,
        api_key,
        hint=f"Set {ENV_API_KEY} in your environment instead.",
    )


def delete_key_from_keyring(base_url: str) -> bool:
    """Remove a stored API key. Returns False when there was nothing to remove."""
    return keyring_store.delete(keyring_store.API_KEY_SERVICE, base_url)


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------


def read_config_file(path: Path | None = None) -> dict[str, object]:
    """Parse the TOML config file. Returns ``{}`` when it does not exist."""
    target = path or config_path()
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target} is not valid TOML: {exc}", hint="Fix or delete the file.") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {target}: {exc}") from exc


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_config_file(settings: dict[str, object], path: Path | None = None) -> Path:
    """Write scalar settings to the config file, preserving unrelated keys.

    ``tomllib`` is read-only, so this emits TOML for the small, known-shape
    document this client owns rather than pulling in a writer dependency.
    Never call this with an ``api_key`` — that belongs in the keyring.
    """
    target = path or config_path()
    merged = {**read_config_file(target), **settings}
    merged.pop("api_key", None)

    lines = ["# agentcore-tui configuration", "# Managed by `agentcore-tui login`; safe to hand-edit.", ""]
    for key, value in sorted(merged.items()):
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, (list, tuple)):
            items = ", ".join(f'"{_toml_escape(str(item))}"' for item in value)
            lines.append(f"{key} = [{items}]")
        else:
            lines.append(f'{key} = "{_toml_escape(str(value))}"')

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Best-effort tighten permissions on POSIX; a no-op on Windows.
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return target


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _as_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw.strip() else None


def _as_float(raw: object, name: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"Config value `{name}` must be a number, got {raw!r}")
    return float(raw)


def _as_int(raw: object, name: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"Config value `{name}` must be an integer, got {raw!r}")
    return raw


def _as_bool(raw: object, name: str) -> bool | None:
    """Parse a boolean from TOML (real bool) or an env var (string).

    Env vars are strings, so the usual spellings are accepted. An unrecognised
    value is an error rather than a silent False — `AGENTCORE_BANNER=off`
    quietly meaning "on" is exactly the kind of thing nobody debugs.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        if not lowered:
            return None
    raise ConfigError(f"Config value `{name}` must be a boolean, got {raw!r}")


def _as_models(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
        raise ConfigError("Config value `models` must be a list of strings")
    entries = tuple(str(item) for item in raw if str(item).strip())
    return entries or None


def _as_ports(raw: object) -> tuple[int, ...]:
    """Parse `callback_ports`, falling back to the CDK-registered defaults.

    A port not registered on the app client cannot be used — Cognito rejects the
    redirect — so a bad value here is worth an error rather than a silent
    fallback.
    """
    if raw is None:
        return DEFAULT_CALLBACK_PORTS
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("Config value `callback_ports` must be a list of integers")
    ports: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"Config value `callback_ports` must contain integers, got {item!r}")
        if not (1024 < item < 65536):
            raise ConfigError(f"Config value `callback_ports` entry {item} is outside 1025-65535")
        ports.append(item)
    return tuple(ports) or DEFAULT_CALLBACK_PORTS


def resolve_config(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_id: str | None = None,
    config_file: Path | None = None,
    env: dict[str, str] | None = None,
    use_keyring: bool = True,
    banner: bool | None = None,
    force_banner: bool = False,
    session_probe: SessionProbe | None = None,
) -> Config:
    """Merge every configuration source into a single :class:`Config`.

    Raises :class:`ConfigError` only for *malformed* configuration. A missing
    base URL or API key is not an error here — the caller inspects
    :attr:`Config.is_complete` so the TUI can start and guide the user.
    """
    environ = os.environ if env is None else env
    file_settings = read_config_file(config_file)

    resolved_base = base_url or _as_str(environ.get(ENV_BASE_URL)) or _as_str(file_settings.get("base_url")) or ""
    resolved_base = resolved_base.rstrip("/")

    file_key = _as_str(file_settings.get("api_key"))
    resolved_key = api_key or _as_str(environ.get(ENV_API_KEY))
    keyring_reason: str | None = None

    if not resolved_key and use_keyring and resolved_base:
        resolved_key, keyring_reason = load_key_from_keyring(resolved_base)

    from_plaintext = False
    if not resolved_key and file_key:
        resolved_key, from_plaintext = file_key, True

    models = _as_models(file_settings.get("models")) or DEFAULT_MODELS
    resolved_model = model_id or _as_str(environ.get(ENV_MODEL_ID)) or _as_str(file_settings.get("model_id")) or models[0]

    # A model chosen explicitly may sit outside the configured list; surface it
    # in the picker anyway so the UI never shows a selection it cannot display.
    if resolved_model not in models:
        models = (resolved_model, *models)

    # `--banner` implies the banner is wanted, so it also overrides a config
    # file or env var that switched it off.
    resolved_banner = banner
    if resolved_banner is None:
        resolved_banner = _as_bool(environ.get(ENV_BANNER), ENV_BANNER)
    if resolved_banner is None:
        resolved_banner = _as_bool(file_settings.get("banner"), "banner")
    if resolved_banner is None:
        resolved_banner = True

    return Config(
        base_url=resolved_base,
        api_key=resolved_key,
        model_id=resolved_model,
        models=models,
        system_prompt=_as_str(file_settings.get("system_prompt")),
        temperature=_as_float(file_settings.get("temperature"), "temperature"),
        max_tokens=_as_int(file_settings.get("max_tokens"), "max_tokens", DEFAULT_MAX_TOKENS),
        top_p=_as_float(file_settings.get("top_p"), "top_p"),
        timeout_seconds=_as_float(file_settings.get("timeout_seconds"), "timeout_seconds") or DEFAULT_TIMEOUT_SECONDS,
        api_key_from_plaintext_file=from_plaintext,
        keyring_unavailable_reason=keyring_reason,
        cognito_domain_url=(_as_str(environ.get(ENV_COGNITO_DOMAIN)) or _as_str(file_settings.get("cognito_domain_url"))),
        cli_client_id=(_as_str(environ.get(ENV_CLI_CLIENT_ID)) or _as_str(file_settings.get("cli_client_id"))),
        callback_ports=_as_ports(file_settings.get("callback_ports")),
        banner=resolved_banner or force_banner,
        force_banner=force_banner,
        credential_source=resolve_source(
            base_url=resolved_base,
            api_key=resolved_key,
            session_probe=session_probe,
        ),
    )
