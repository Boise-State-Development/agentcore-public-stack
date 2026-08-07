"""Tests for the command-line surface."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agentcore_tui import cli, keyring_store
from agentcore_tui.client.device_auth import DeviceAuthorization, DeviceSession
from agentcore_tui.config import ENV_API_KEY, ENV_BASE_URL, read_config_file
from agentcore_tui.errors import ConfigError, DeviceAuthDeniedError

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


class FakeDeviceAuthClient:
    """Stands in for the real device-flow client.

    Constructed by ``cli`` rather than injected, so tests replace the class in
    :mod:`agentcore_tui.client.device_auth` and record what it was asked to do.
    """

    #: Set by each test: either a DeviceSession, or an exception to raise.
    outcome: object = None
    instances: list[FakeDeviceAuthClient] = []

    def __init__(self, base_url: str, **_kwargs: object) -> None:
        self.base_url = base_url
        self.polled = False
        FakeDeviceAuthClient.instances.append(self)

    async def __aenter__(self) -> FakeDeviceAuthClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def authorize(self) -> DeviceAuthorization:
        return DeviceAuthorization(
            device_code="d" * 43,
            user_code="Y4GN-WKY3",
            verification_uri=f"{BASE_URL}/auth/cli/verify",
            verification_uri_complete=f"{BASE_URL}/auth/cli/verify?user_code=Y4GN-WKY3",
            expires_in=600,
            interval=5,
        )

    async def poll_for_session(self, _authorization: DeviceAuthorization, **_kwargs: object) -> DeviceSession:
        self.polled = True
        if isinstance(FakeDeviceAuthClient.outcome, BaseException):
            raise FakeDeviceAuthClient.outcome
        assert isinstance(FakeDeviceAuthClient.outcome, DeviceSession)
        return FakeDeviceAuthClient.outcome


@pytest.fixture
def device_flow(monkeypatch: pytest.MonkeyPatch) -> type[FakeDeviceAuthClient]:
    """Replace the device-flow client, and keep the keyring out of it."""
    from agentcore_tui.client import device_auth

    FakeDeviceAuthClient.instances = []
    FakeDeviceAuthClient.outcome = DeviceSession(
        session="sealed-envelope-value",
        expires_in=28783,
        user_id="28d1d380",
        username="colin",
    )
    monkeypatch.setattr(device_auth, "DeviceAuthClient", FakeDeviceAuthClient)
    # Never launch a real browser from a test run.
    monkeypatch.setattr(cli, "_try_open_browser", lambda _url: False)
    return FakeDeviceAuthClient


class TestLoginSso:
    def test_stores_the_session_and_the_base_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        device_flow: type[FakeDeviceAuthClient],
    ) -> None:
        stored: dict[str, str] = {}
        monkeypatch.setattr(cli, "save_session_to_keyring", lambda url, value: stored.update({url: value}))
        config_file = tmp_path / "c.toml"

        exit_code = cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(config_file)])

        assert exit_code == 0
        assert stored == {BASE_URL: "sealed-envelope-value"}
        assert read_config_file(config_file)["base_url"] == BASE_URL
        out = capsys.readouterr().out
        assert "Signed in as colin" in out

    def test_shows_the_code_and_url_even_when_a_browser_opens(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        device_flow: type[FakeDeviceAuthClient],
    ) -> None:
        """Browser launch fails silently on headless hosts and over SSH.

        The URL is the only way through, so it is printed unconditionally —
        including when ``webbrowser.open`` claims success.
        """
        monkeypatch.setattr(cli, "_try_open_browser", lambda _url: True)
        monkeypatch.setattr(cli, "save_session_to_keyring", lambda *_a: None)

        cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])

        out = capsys.readouterr().out
        assert "Y4GN-WKY3" in out
        assert "user_code=Y4GN-WKY3" in out

    def test_never_prints_the_sealed_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        device_flow: type[FakeDeviceAuthClient],
    ) -> None:
        monkeypatch.setattr(cli, "save_session_to_keyring", lambda *_a: None)
        cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert "sealed-envelope-value" not in capsys.readouterr().out

    def test_a_declined_sign_in_reports_the_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        device_flow: type[FakeDeviceAuthClient],
    ) -> None:
        device_flow.outcome = DeviceAuthDeniedError()
        saved: list[object] = []
        monkeypatch.setattr(cli, "save_session_to_keyring", lambda *a: saved.append(a))

        exit_code = cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])

        assert exit_code == 1
        assert saved == []
        err = capsys.readouterr().err
        assert "declined" in err
        assert "login --sso" in err

    def test_requires_a_base_url(self, tmp_path: Path, device_flow: type[FakeDeviceAuthClient]) -> None:
        assert cli.main(["login", "--sso", "--config", str(tmp_path / "c.toml")]) == 2

    def test_does_not_need_cognito_configuration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        device_flow: type[FakeDeviceAuthClient],
    ) -> None:
        """The reverted design needed a domain URL and a client id.

        This one needs neither: the CLI never talks to Cognito. A base URL is
        the whole configuration surface.
        """
        monkeypatch.setattr(cli, "save_session_to_keyring", lambda *_a: None)
        assert cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")]) == 0


class TestLogout:
    @pytest.fixture(autouse=True)
    def no_real_keyring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Logout touches three services. Stub them all, or tests hit the host."""
        monkeypatch.setattr(cli, "delete_key_from_keyring", lambda _: False)
        monkeypatch.setattr(cli, "delete_session_from_keyring", lambda _: False)
        monkeypatch.setattr(keyring_store, "delete", lambda *_a: False)

    def test_reports_removal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(cli, "delete_key_from_keyring", lambda _: True)
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 0
        assert "Removed" in capsys.readouterr().out

    def test_removes_a_stored_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(cli, "delete_session_from_keyring", lambda _: True)
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 0
        assert "Removed the stored session" in capsys.readouterr().out

    def test_clears_a_refresh_token_left_by_the_reverted_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing writes that service any more, but an upgrading user may hold
        one, and leaving a live refresh token behind on an explicit logout would
        be wrong."""
        asked: list[str] = []

        def fake_delete(service: str, _base_url: str) -> bool:
            asked.append(service)
            return service == keyring_store.LEGACY_SSO_SERVICE

        monkeypatch.setattr(keyring_store, "delete", fake_delete)
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])

        assert exit_code == 0
        assert keyring_store.LEGACY_SSO_SERVICE in asked
        assert "older version" in capsys.readouterr().out

    def test_reports_when_there_was_nothing_to_remove(self, tmp_path: Path) -> None:
        exit_code = cli.main(["logout", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert exit_code == 1

    def test_requires_a_base_url(self, tmp_path: Path) -> None:
        assert cli.main(["logout", "--config", str(tmp_path / "c.toml")]) == 2


class TestStatus:
    @pytest.fixture(autouse=True)
    def no_real_keyring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`status` reads the session store; keep the host keyring out of it."""
        monkeypatch.setattr(cli, "load_session_from_keyring", lambda _: (None, None))

    def test_reports_a_stored_session_without_printing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
        monkeypatch.setattr(cli, "load_session_from_keyring", lambda _: ("sealed-envelope-value", None))
        monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: httpx.Response(200))

        cli.main(["status", "--config", str(tmp_path / "c.toml")])
        out = capsys.readouterr().out

        assert "sealed-envelope-value" not in out
        assert "session     : present" in out

    def test_reports_why_the_session_could_not_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A headless host with no Secret Service must say so, not say "none"."""
        monkeypatch.setenv(ENV_BASE_URL, BASE_URL)
        monkeypatch.setattr(cli, "load_session_from_keyring", lambda _: (None, "NoKeyringError: nope"))
        monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: httpx.Response(200))

        cli.main(["status", "--config", str(tmp_path / "c.toml")])
        assert "NoKeyringError" in capsys.readouterr().out

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
