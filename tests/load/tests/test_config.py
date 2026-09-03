"""Tests for configuration loading and the fail-fast guards.

Every check here exists to convert a misconfiguration into an immediate,
readable error instead of a run that generates load and fails every request —
which reads as a broken platform rather than a broken test.
"""

from __future__ import annotations

import json

import pytest

from agentcore_load.config import ConfigError, load_config, validate_host

COGNITO = "https://example.auth.us-west-2.amazoncognito.com"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGENTCORE_LOAD_COGNITO_DOMAIN",
        "AGENTCORE_LOAD_USERS_FILE",
        "AGENTCORE_LOAD_USERNAME",
        "AGENTCORE_LOAD_PASSWORD",
        "AGENTCORE_LOAD_MODEL_ID",
        "AGENTCORE_LOAD_PROVIDER",
        "AGENTCORE_LOAD_ENABLED_TOOLS",
        "AGENTCORE_LOAD_TURNS_PER_CONVERSATION",
        "AGENTCORE_LOAD_PROMPTS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_http_host_is_rejected() -> None:
    # The __Host- cookie prefix requires Secure, so over http the session
    # cookie is dropped and every chat request 401s.
    with pytest.raises(ConfigError, match="must be https"):
        validate_host("http://localhost:8000")


def test_missing_host_is_rejected() -> None:
    with pytest.raises(ConfigError, match="No --host"):
        validate_host(None)


def test_trailing_slash_stripped_from_host() -> None:
    assert validate_host("https://chat.example.edu/api/") == "https://chat.example.edu/api"


def test_cognito_domain_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", "http://example.com")
    monkeypatch.setenv("AGENTCORE_LOAD_USERNAME", "u")
    monkeypatch.setenv("AGENTCORE_LOAD_PASSWORD", "p")
    with pytest.raises(ConfigError, match="must be an https"):
        load_config()


def test_missing_cognito_domain_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="AGENTCORE_LOAD_COGNITO_DOMAIN"):
        load_config()


def test_missing_credentials_explains_both_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    with pytest.raises(ConfigError, match="AGENTCORE_LOAD_USERS_FILE"):
        load_config()


def test_single_credential_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERNAME", "loadtest01")
    monkeypatch.setenv("AGENTCORE_LOAD_PASSWORD", "secret")

    config = load_config()
    assert len(config.credentials) == 1
    assert config.credentials[0].username == "loadtest01"
    # Defaults: system default model, no tools.
    assert config.model_id is None
    assert config.enabled_tools == []


def test_manifest_file_loads_a_pool(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manifest = tmp_path / "users.json"
    manifest.write_text(
        json.dumps(
            [
                {"username": "load01", "password": "a"},
                {"username": "load02", "password": "b"},
            ]
        )
    )
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERS_FILE", str(manifest))

    config = load_config()
    assert [c.username for c in config.credentials] == ["load01", "load02"]


def test_manifest_entry_missing_password_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manifest = tmp_path / "users.json"
    manifest.write_text(json.dumps([{"username": "load01"}]))
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERS_FILE", str(manifest))

    with pytest.raises(ConfigError, match="needs both"):
        load_config()


def test_empty_manifest_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manifest = tmp_path / "users.json"
    manifest.write_text("[]")
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERS_FILE", str(manifest))

    with pytest.raises(ConfigError, match="non-empty JSON array"):
        load_config()


def test_credential_repr_hides_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # Locust prints exception context into its stats tables; a credential must
    # never be readable from a traceback.
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERNAME", "loadtest01")
    monkeypatch.setenv("AGENTCORE_LOAD_PASSWORD", "super-secret-value")

    rendered = repr(load_config().credentials[0])
    assert "super-secret-value" not in rendered
    assert "***" in rendered


def test_turns_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERNAME", "u")
    monkeypatch.setenv("AGENTCORE_LOAD_PASSWORD", "p")
    monkeypatch.setenv("AGENTCORE_LOAD_TURNS_PER_CONVERSATION", "0")

    with pytest.raises(ConfigError, match=">= 1"):
        load_config()


def test_enabled_tools_parsed_as_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_LOAD_COGNITO_DOMAIN", COGNITO)
    monkeypatch.setenv("AGENTCORE_LOAD_USERNAME", "u")
    monkeypatch.setenv("AGENTCORE_LOAD_PASSWORD", "p")
    monkeypatch.setenv("AGENTCORE_LOAD_ENABLED_TOOLS", "calculator, web_search ,")

    assert load_config().enabled_tools == ["calculator", "web_search"]
