"""Tests for configuration and credential resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcore_tui import config as config_module
from agentcore_tui import keyring_store
from agentcore_tui.config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODELS,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL_ID,
    Config,
    read_config_file,
    resolve_config,
    write_config_file,
)
from agentcore_tui.credentials import Capability, CredentialSource
from agentcore_tui.errors import ConfigError

MODEL_A = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_B = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


class TestConfigProperties:
    def test_is_complete_requires_a_url_and_a_usable_credential(self) -> None:
        """Asks the credential discriminant, not ``api_key``. Under SSO there is
        no API key and the client is still fully configured."""
        assert not Config(base_url="https://h").is_complete
        assert not Config(credential_source=CredentialSource.API_KEY).is_complete
        assert Config(base_url="https://h", credential_source=CredentialSource.API_KEY).is_complete
        assert Config(base_url="https://h", api_key=None, credential_source=CredentialSource.BFF_SESSION).is_complete

    def test_an_api_key_alone_does_not_make_it_complete(self) -> None:
        """The field is set but the discriminant was never resolved, which is
        exactly the state a directly-constructed Config is in."""
        assert not Config(base_url="https://h", api_key="k").is_complete

    def test_capabilities_follow_the_credential(self) -> None:
        api_key = Config(base_url="https://h", credential_source=CredentialSource.API_KEY)
        session = Config(base_url="https://h", credential_source=CredentialSource.BFF_SESSION)
        assert api_key.can(Capability.CHAT) is True
        assert api_key.can(Capability.SESSIONS) is False
        assert session.can(Capability.SESSIONS) is True

    def test_missing_names_each_gap(self) -> None:
        assert Config().missing() == ["base URL", "API key"]
        assert Config(base_url="https://h").missing() == ["API key"]

    def test_api_key_is_absent_from_repr(self) -> None:
        """A traceback or debug log must not leak the credential."""
        assert "super-secret" not in repr(Config(base_url="https://h", api_key="super-secret"))

    def test_with_model_returns_a_copy(self) -> None:
        original = Config(base_url="https://h", api_key="k", model_id=MODEL_A)
        updated = original.with_model(MODEL_B)
        assert updated.model_id == MODEL_B
        assert original.model_id == MODEL_A


class TestResolutionPrecedence:
    def test_explicit_arguments_beat_environment(self, config_file: Path) -> None:
        resolved = resolve_config(
            base_url="https://explicit",
            model_id=MODEL_B,
            config_file=config_file,
            env={ENV_BASE_URL: "https://from-env", ENV_MODEL_ID: MODEL_A},
            use_keyring=False,
        )
        assert resolved.base_url == "https://explicit"
        assert resolved.model_id == MODEL_B

    def test_environment_beats_config_file(self, config_file: Path) -> None:
        config_file.write_text('base_url = "https://from-file"\n', encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={ENV_BASE_URL: "https://from-env"}, use_keyring=False)
        assert resolved.base_url == "https://from-env"

    def test_config_file_is_used_when_nothing_else_set(self, config_file: Path) -> None:
        config_file.write_text('base_url = "https://from-file"\n', encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.base_url == "https://from-file"

    def test_env_api_key_is_preferred_over_keyring(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(_: str) -> tuple[str | None, str | None]:
            raise AssertionError("keyring must not be consulted when the env var is set")

        monkeypatch.setattr(config_module, "load_key_from_keyring", fail)
        resolved = resolve_config(
            config_file=config_file,
            env={ENV_BASE_URL: "https://h", ENV_API_KEY: "from-env"},
        )
        assert resolved.api_key == "from-env"

    def test_keyring_is_consulted_when_env_is_absent(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_module, "load_key_from_keyring", lambda _: ("from-keyring", None))
        resolved = resolve_config(config_file=config_file, env={ENV_BASE_URL: "https://h"})
        assert resolved.api_key == "from-keyring"
        assert resolved.api_key_from_plaintext_file is False

    def test_plaintext_file_key_is_honoured_but_flagged(self, config_file: Path) -> None:
        config_file.write_text('base_url = "https://h"\napi_key = "in-file"\n', encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.api_key == "in-file"
        assert resolved.api_key_from_plaintext_file is True

    def test_unavailable_keyring_degrades_without_raising(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Headless Linux hosts have no Secret Service; that must not be fatal."""
        monkeypatch.setattr(config_module, "load_key_from_keyring", lambda _: (None, "NoKeyringError: no backend"))
        resolved = resolve_config(config_file=config_file, env={ENV_BASE_URL: "https://h"})
        assert resolved.api_key is None
        assert resolved.keyring_unavailable_reason == "NoKeyringError: no backend"
        assert not resolved.is_complete

    def test_trailing_slash_is_normalised(self, config_file: Path) -> None:
        resolved = resolve_config(base_url="https://h/api/", config_file=config_file, env={}, use_keyring=False)
        assert resolved.base_url == "https://h/api"

    def test_defaults_apply_with_no_sources(self, config_file: Path) -> None:
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.base_url == ""
        assert resolved.model_id == DEFAULT_MODELS[0]
        assert resolved.max_tokens == DEFAULT_MAX_TOKENS


class TestModelList:
    def test_custom_model_list_replaces_defaults(self, config_file: Path) -> None:
        config_file.write_text('models = ["model-x", "model-y"]\n', encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.models == ("model-x", "model-y")
        assert resolved.model_id == "model-x"

    def test_explicit_model_outside_the_list_is_prepended(self, config_file: Path) -> None:
        """The picker must always be able to display the active selection."""
        config_file.write_text('models = ["model-x"]\n', encoding="utf-8")
        resolved = resolve_config(model_id="model-z", config_file=config_file, env={}, use_keyring=False)
        assert resolved.model_id == "model-z"
        assert resolved.models[0] == "model-z"
        assert "model-x" in resolved.models

    def test_empty_model_list_falls_back_to_defaults(self, config_file: Path) -> None:
        config_file.write_text("models = []\n", encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.models == DEFAULT_MODELS

    def test_non_string_model_list_is_rejected(self, config_file: Path) -> None:
        config_file.write_text("models = [1, 2]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="list of strings"):
            resolve_config(config_file=config_file, env={}, use_keyring=False)


class TestConfigFileIO:
    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_config_file(tmp_path / "absent.toml") == {}

    def test_malformed_toml_raises_with_the_path(self, config_file: Path) -> None:
        config_file.write_text("base_url = = =\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid TOML"):
            read_config_file(config_file)

    def test_write_then_read_round_trips(self, config_file: Path) -> None:
        write_config_file(
            {"base_url": "https://h/api", "model_id": MODEL_A, "max_tokens": 8192, "temperature": 0.5, "models": [MODEL_A, MODEL_B]},
            config_file,
        )
        loaded = read_config_file(config_file)
        assert loaded["base_url"] == "https://h/api"
        assert loaded["max_tokens"] == 8192
        assert loaded["temperature"] == 0.5
        assert loaded["models"] == [MODEL_A, MODEL_B]

    def test_write_preserves_unrelated_existing_keys(self, config_file: Path) -> None:
        write_config_file({"base_url": "https://h", "system_prompt": "be terse"}, config_file)
        write_config_file({"base_url": "https://other"}, config_file)
        loaded = read_config_file(config_file)
        assert loaded["base_url"] == "https://other"
        assert loaded["system_prompt"] == "be terse"

    def test_write_refuses_to_persist_an_api_key(self, config_file: Path) -> None:
        """Keys belong in the keyring; this is the last line of defence."""
        write_config_file({"base_url": "https://h", "api_key": "must-not-persist"}, config_file)
        assert "must-not-persist" not in config_file.read_text(encoding="utf-8")
        assert "api_key" not in read_config_file(config_file)

    def test_write_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "config.toml"
        write_config_file({"base_url": "https://h"}, nested)
        assert nested.is_file()

    def test_quotes_and_backslashes_survive_a_round_trip(self, config_file: Path) -> None:
        tricky = 'say "hi" \\ then stop'
        write_config_file({"system_prompt": tricky}, config_file)
        assert read_config_file(config_file)["system_prompt"] == tricky


class TestScalarValidation:
    @pytest.mark.parametrize("field", ["temperature", "top_p", "timeout_seconds"])
    def test_non_numeric_scalar_is_rejected(self, config_file: Path, field: str) -> None:
        config_file.write_text(f'{field} = "warm"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a number"):
            resolve_config(config_file=config_file, env={}, use_keyring=False)

    def test_non_integer_max_tokens_is_rejected(self, config_file: Path) -> None:
        config_file.write_text("max_tokens = 1.5\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must be an integer"):
            resolve_config(config_file=config_file, env={}, use_keyring=False)

    def test_integer_temperature_is_accepted_as_float(self, config_file: Path) -> None:
        config_file.write_text("temperature = 1\n", encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.temperature == 1.0


class TestBannerResolution:
    """`--banner` > AGENTCORE_BANNER > config file > on."""

    def test_defaults_to_enabled(self, config_file: Path) -> None:
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False)
        assert resolved.banner is True
        assert resolved.force_banner is False

    def test_dataclass_default_is_disabled(self) -> None:
        """The opposite of resolve_config's default, on purpose: a
        directly-constructed Config (tests, embedding) shows no animation."""
        assert Config().banner is False

    def test_config_file_can_disable_it(self, config_file: Path) -> None:
        config_file.write_text("banner = false\n", encoding="utf-8")
        assert resolve_config(config_file=config_file, env={}, use_keyring=False).banner is False

    def test_env_var_overrides_the_config_file(self, config_file: Path) -> None:
        config_file.write_text("banner = false\n", encoding="utf-8")
        env = {"AGENTCORE_BANNER": "1"}
        assert resolve_config(config_file=config_file, env=env, use_keyring=False).banner is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
    def test_env_var_falsey_spellings(self, config_file: Path, raw: str) -> None:
        env = {"AGENTCORE_BANNER": raw}
        assert resolve_config(config_file=config_file, env=env, use_keyring=False).banner is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "ON"])
    def test_env_var_truthy_spellings(self, config_file: Path, raw: str) -> None:
        env = {"AGENTCORE_BANNER": raw}
        assert resolve_config(config_file=config_file, env=env, use_keyring=False).banner is True

    def test_unparseable_env_var_is_an_error(self, config_file: Path) -> None:
        """Silently treating `maybe` as False is the kind of thing nobody debugs."""
        env = {"AGENTCORE_BANNER": "maybe"}
        with pytest.raises(ConfigError, match="must be a boolean"):
            resolve_config(config_file=config_file, env=env, use_keyring=False)

    def test_empty_env_var_falls_through_to_the_file(self, config_file: Path) -> None:
        config_file.write_text("banner = false\n", encoding="utf-8")
        env = {"AGENTCORE_BANNER": ""}
        assert resolve_config(config_file=config_file, env=env, use_keyring=False).banner is False

    def test_non_boolean_config_file_value_is_an_error(self, config_file: Path) -> None:
        config_file.write_text('banner = "sometimes"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a boolean"):
            resolve_config(config_file=config_file, env={}, use_keyring=False)

    def test_force_overrides_a_disabling_config_file(self, config_file: Path) -> None:
        """`--banner` means "show me now", so it beats `banner = false`."""
        config_file.write_text("banner = false\n", encoding="utf-8")
        resolved = resolve_config(config_file=config_file, env={}, use_keyring=False, banner=True, force_banner=True)
        assert resolved.banner is True
        assert resolved.force_banner is True

    def test_explicit_false_wins_over_the_env_var(self, config_file: Path) -> None:
        """This is `--no-banner` against AGENTCORE_BANNER=1."""
        env = {"AGENTCORE_BANNER": "1"}
        resolved = resolve_config(config_file=config_file, env=env, use_keyring=False, banner=False)
        assert resolved.banner is False


class TestSessionKeyringAccessors:
    """The sealed session is stored under its own keyring *service*.

    Separate from API keys so revoking one credential cannot disturb the other,
    and separate from the retired SSO service so an upgrade does not read a
    refresh token as if it were a session.
    """

    def test_the_three_services_are_distinct(self) -> None:
        services = {
            keyring_store.API_KEY_SERVICE,
            keyring_store.SESSION_SERVICE,
            keyring_store.LEGACY_SSO_SERVICE,
        }
        assert len(services) == 3

    def test_save_and_load_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        vault: dict[tuple[str, str], str] = {}
        monkeypatch.setattr(
            keyring_store,
            "store",
            lambda service, base_url, secret, hint: vault.__setitem__((service, base_url), secret),
        )
        monkeypatch.setattr(
            keyring_store,
            "load",
            lambda service, base_url: (vault.get((service, base_url)), None),
        )

        config_module.save_session_to_keyring("https://h", "sealed")

        assert vault == {(keyring_store.SESSION_SERVICE, "https://h"): "sealed"}
        assert config_module.load_session_from_keyring("https://h") == ("sealed", None)

    def test_saving_a_session_does_not_suggest_an_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unlike the API key.

        A sealed session is equivalent to being signed in and rotates on every
        sign-in, so telling users to export it would be advice to leave a live
        credential in their shell history and process table.
        """
        hints: list[str] = []
        monkeypatch.setattr(keyring_store, "store", lambda service, base_url, secret, hint: hints.append(hint))
        config_module.save_session_to_keyring("https://h", "sealed")
        assert "AGENTCORE_" not in hints[0]
        assert "keyring" in hints[0]

    def test_delete_targets_the_session_service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[tuple[str, str]] = []
        monkeypatch.setattr(keyring_store, "delete", lambda service, base_url: bool(asked.append((service, base_url))))
        config_module.delete_session_from_keyring("https://h")
        assert asked == [(keyring_store.SESSION_SERVICE, "https://h")]
