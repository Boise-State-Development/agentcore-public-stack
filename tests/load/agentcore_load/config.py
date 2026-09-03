"""Environment-driven configuration for the load suite.

Everything is read from env vars so the same locustfile runs from a laptop,
the devcontainer, or a container in the load-generator harness without code
changes. Nothing here reads AWS credentials or SSM — resolving the Cognito
domain and provisioning users is the provisioning scripts' job, and this
process deliberately has no AWS permissions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    """Raised when the environment is not usable for a run.

    Always raised before any load is generated. A misconfigured run that
    starts and then fails every request looks like a broken *system* rather
    than a broken *test*, so these checks are deliberately fail-fast.
    """


@dataclass(frozen=True)
class Credential:
    """One Cognito user the test can log in as.

    Multiple simulated users may share a credential — each Locust user gets
    its own cookie jar, so they establish independent BFF sessions. See the
    README for why that is useful and what it distorts (quota concentration).
    """

    username: str
    password: str

    def __repr__(self) -> str:
        # Keep the password out of tracebacks and Locust's error tables.
        return f"Credential(username={self.username!r}, password=***)"


@dataclass(frozen=True)
class LoadConfig:
    """Resolved settings for a run."""

    cognito_domain_url: str
    credentials: list[Credential] = field(default_factory=list)

    # Chat shape. `model_id`/`provider` of None means "system default", which
    # is what the SPA sends when the user has not picked a model.
    model_id: str | None = None
    provider: str | None = None
    # Empty tool list keeps turns fast, cheap and low-variance. Tool calls add
    # multi-second, high-variance latency that swamps the signal you are
    # usually looking for. Opt in explicitly when testing the tool path.
    enabled_tools: list[str] = field(default_factory=list)

    turns_per_conversation: int = 3
    prompts: list[str] = field(default_factory=list)

    # Ceiling on a single turn. app-api's proxy gives up at 300s
    # (_PROXY_TIMEOUT_SECONDS), so going past that measures nothing new.
    turn_timeout_seconds: float = 300.0

    @property
    def cognito_authorize_host(self) -> str:
        return urlparse(self.cognito_domain_url).netloc


_DEFAULT_PROMPTS = [
    "In two sentences, what is a load test?",
    "Name three benefits of caching. Be brief.",
    "Summarize the difference between latency and throughput.",
    "What does 'p99 latency' mean? One short paragraph.",
    "List four common causes of slow API responses.",
]


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is required. See tests/load/README.md for the full list "
            f"of environment variables."
        )
    return value


def _load_credentials() -> list[Credential]:
    """Load the credential pool from a manifest file or a single env pair.

    The manifest is the output of the provisioning script — a JSON array of
    ``{"username": ..., "password": ...}``. Passwords are read from disk and
    never logged.
    """
    manifest_path = os.environ.get("AGENTCORE_LOAD_USERS_FILE", "").strip()
    if manifest_path:
        path = Path(manifest_path)
        if not path.is_file():
            raise ConfigError(f"AGENTCORE_LOAD_USERS_FILE does not exist: {path}")
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise ConfigError(f"{path} must contain a non-empty JSON array of users.")

        credentials = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ConfigError(f"{path}[{index}] is not an object.")
            username = entry.get("username")
            password = entry.get("password")
            if not username or not password:
                raise ConfigError(f"{path}[{index}] needs both 'username' and 'password'.")
            credentials.append(Credential(username=str(username), password=str(password)))
        return credentials

    username = os.environ.get("AGENTCORE_LOAD_USERNAME", "").strip()
    password = os.environ.get("AGENTCORE_LOAD_PASSWORD", "")
    if username and password:
        return [Credential(username=username, password=password)]

    raise ConfigError(
        "No credentials configured. Set AGENTCORE_LOAD_USERS_FILE to a JSON "
        "manifest, or AGENTCORE_LOAD_USERNAME + AGENTCORE_LOAD_PASSWORD for a "
        "single user."
    )


def _split_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} must be >= 1, got {value}")
    return value


def load_config() -> LoadConfig:
    """Build the config, failing fast on anything unusable."""
    cognito_domain_url = _require("AGENTCORE_LOAD_COGNITO_DOMAIN").rstrip("/")
    if not cognito_domain_url.startswith("https://"):
        raise ConfigError(
            f"AGENTCORE_LOAD_COGNITO_DOMAIN must be an https:// URL (got {cognito_domain_url!r})."
        )

    prompt_file = os.environ.get("AGENTCORE_LOAD_PROMPTS_FILE", "").strip()
    if prompt_file:
        path = Path(prompt_file)
        if not path.is_file():
            raise ConfigError(f"AGENTCORE_LOAD_PROMPTS_FILE does not exist: {path}")
        prompts = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        if not prompts:
            raise ConfigError(f"{path} contains no non-empty lines.")
    else:
        prompts = list(_DEFAULT_PROMPTS)

    return LoadConfig(
        cognito_domain_url=cognito_domain_url,
        credentials=_load_credentials(),
        model_id=os.environ.get("AGENTCORE_LOAD_MODEL_ID", "").strip() or None,
        provider=os.environ.get("AGENTCORE_LOAD_PROVIDER", "").strip() or None,
        enabled_tools=_split_list("AGENTCORE_LOAD_ENABLED_TOOLS"),
        turns_per_conversation=_positive_int("AGENTCORE_LOAD_TURNS_PER_CONVERSATION", 3),
        prompts=prompts,
    )


def validate_host(host: str | None) -> str:
    """Validate Locust's ``--host`` (the app-api origin).

    The session and CSRF cookies are ``__Host-`` prefixed, which requires
    ``Secure``. `requests` will not store or resend a Secure cookie over
    plain http, so an http host produces a login that appears to succeed and
    then 401s on every chat request — a confusing failure worth catching here.
    """
    if not host:
        raise ConfigError(
            "No --host set. Pass the app-api origin, e.g. "
            "`locust --host https://chat.example.edu/api`."
        )
    if not host.startswith("https://"):
        raise ConfigError(
            f"--host must be https:// (got {host!r}). The BFF session cookie is "
            "'__Host-' prefixed and therefore Secure-only; over http it is "
            "silently dropped and every chat request would 401."
        )
    return host.rstrip("/")
