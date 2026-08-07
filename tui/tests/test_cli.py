"""Tests for the command-line surface."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agentcore_tui import cli
from agentcore_tui.config import ENV_API_KEY, ENV_BASE_URL, read_config_file
from agentcore_tui.errors import ConfigError

BASE_URL = "https://example.test/api"


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real env and keyring out of these tests."""
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv(ENV_API_KEY, raising=False)


class TestArgumentParsing:
    @staticmethod
    def parse(argv: list[str]) -> object:
        return cli._normalise(cli._build_parser().parse_args(argv))

    def test_shared_flags_accepted_before_the_subcommand(self) -> None:
        args = self.parse(["--config", "/tmp/a.toml", "status"])
        assert args.command == "status"
        assert args.config_file == Path("/tmp/a.toml")

    def test_shared_flags_accepted_after_the_subcommand(self) -> None:
        args = self.parse(["status", "--config", "/tmp/b.toml"])
        assert args.command == "status"
        assert args.config_file == Path("/tmp/b.toml")

    def test_subcommand_does_not_clobber_an_earlier_flag(self) -> None:
        """`set_defaults` on shared parent actions caused exactly this bug once."""
        args = self.parse(["--base-url", BASE_URL, "status"])
        assert args.base_url == BASE_URL

    def test_flag_after_subcommand_wins_when_given_twice(self) -> None:
        args = self.parse(["--base-url", "https://first", "status", "--base-url", "https://second"])
        assert args.base_url == "https://second"

    def test_no_subcommand_means_chat(self) -> None:
        args = self.parse([])
        assert args.command is None
        assert args.base_url is None
        assert args.config_file is None

    def test_login_accepts_an_inline_key(self) -> None:
        args = self.parse(["login", "--base-url", BASE_URL, "--api-key", "abc"])
        assert args.command == "login"
        assert args.api_key == "abc"

    def test_absent_api_key_normalises_to_none(self) -> None:
        assert self.parse(["status"]).api_key is None

    def test_banner_flag_is_tri_state(self) -> None:
        """None means "decide from state", so absent must not become False."""
        assert self.parse([]).banner is None
        assert self.parse(["--banner"]).banner is True
        assert self.parse(["--no-banner"]).banner is False

    def test_banner_and_no_banner_together_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            self.parse(["--banner", "--no-banner"])


class TestLogin:
    def test_stores_key_in_keyring_and_url_in_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stored: dict[str, str] = {}
        monkeypatch.setattr(cli, "save_key_to_keyring", lambda url, key: stored.update({url: key}))

        config_file = tmp_path / "config.toml"
        exit_code = cli.main(["login", "--base-url", BASE_URL, "--api-key", "secret-key", "--config", str(config_file)])

        assert exit_code == 0
        assert stored == {BASE_URL: "secret-key"}
        # The key must never land in the file; only the base URL does.
        assert read_config_file(config_file) == {"base_url": BASE_URL}
        assert "secret-key" not in config_file.read_text(encoding="utf-8")
        assert "secret-key" not in capsys.readouterr().out

    def test_requires_a_base_url(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli.main(["login", "--api-key", "k", "--config", str(tmp_path / "c.toml")])
        assert exit_code == 2
        assert "no base URL" in capsys.readouterr().err

    def test_rejects_an_empty_key(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli.main(["login", "--base-url", BASE_URL, "--api-key", "   ", "--config", str(tmp_path / "c.toml")])
        assert exit_code == 2
        assert "empty API key" in capsys.readouterr().err

    def test_prompts_without_echo_when_key_is_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """getpass, not input, so the key never reaches shell history or the screen."""
        monkeypatch.setattr(cli.getpass, "getpass", lambda _: "typed-key")
        stored: dict[str, str] = {}
        monkeypatch.setattr(cli, "save_key_to_keyring", lambda url, key: stored.update({url: key}))

        exit_code = cli.main(["login", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 0
        assert stored[BASE_URL] == "typed-key"

    def test_keyring_failure_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def explode(url: str, key: str) -> None:
            raise ConfigError("Could not write to the OS keyring (NoKeyringError).", hint=f"Set {ENV_API_KEY} instead.")

        monkeypatch.setattr(cli, "save_key_to_keyring", explode)
        exit_code = cli.main(["login", "--base-url", BASE_URL, "--api-key", "k", "--config", str(tmp_path / "c.toml")])

        assert exit_code == 1
        captured = capsys.readouterr().err
        assert "keyring" in captured
        assert ENV_API_KEY in captured


class TestLogout:
    def test_reports_removal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(cli, "delete_key_from_keyring", lambda _: True)
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 0
        assert "Removed" in capsys.readouterr().out

    def test_reports_when_there_was_nothing_to_remove(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "delete_key_from_keyring", lambda _: False)
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 1

    def test_requires_a_base_url(self, tmp_path: Path) -> None:
        assert cli.main(["logout", "--config", str(tmp_path / "c.toml")]) == 2


class TestStatus:
    def test_never_prints_the_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
        monkeypatch.setenv(ENV_API_KEY, "super-secret-value")
        monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: httpx.Response(200))

        cli.main(["status", "--config", str(tmp_path / "c.toml")])
        out = capsys.readouterr().out

        assert "super-secret-value" not in out
        assert "present" in out
        assert BASE_URL in out

    def test_reports_missing_configuration(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli.main(["status", "--config", str(tmp_path / "c.toml")])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "missing" in out

    def test_reports_unreachable_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
        monkeypatch.setenv(ENV_API_KEY, "k")

        def explode(*args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(cli.httpx, "get", explode)
        exit_code = cli.main(["status", "--config", str(tmp_path / "c.toml")])

        assert exit_code == 1
        assert "unreachable" in capsys.readouterr().out

    def test_checks_the_health_endpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        called: dict[str, str] = {}

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            called["url"] = url
            return httpx.Response(200)

        monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
        monkeypatch.setenv(ENV_API_KEY, "k")
        monkeypatch.setattr(cli.httpx, "get", fake_get)

        exit_code = cli.main(["status", "--config", str(tmp_path / "c.toml")])

        assert exit_code == 0
        assert called["url"] == f"{BASE_URL}/health"
        assert "HTTP 200" in capsys.readouterr().out
