"""Tests for configuration and credential resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcore_tui import config as config_module
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
from agentcore_tui.errors import ConfigError

MODEL_A = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_B = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


class TestConfigProperties:
    def test_converse_url_is_joined_without_double_slash(self) -> None:
        assert Config(base_url="https://h/api/").converse_url == "https://h/api/chat/api-converse"
        assert Config(base_url="https://h/api").converse_url == "https://h/api/chat/api-converse"

    def test_is_complete_requires_both_url_and_key(self) -> None:
        assert not Config(base_url="https://h").is_complete
        assert not Config(api_key="k").is_complete
        assert Config(base_url="https://h", api_key="k").is_complete

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
