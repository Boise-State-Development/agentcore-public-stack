"""The encrypted credential file, and how the CLI chooses and unlocks it."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from aps_tui import cli, keyring_store
from aps_tui.config import ENV_CREDENTIAL_STORE, resolve_store_preference
from aps_tui.credential_file import CredentialFile, WrongPassphraseError
from aps_tui.errors import ConfigError
from aps_tui.keyring_store import API_KEY_SERVICE, SESSION_SERVICE, StorePreference

#: Captured at import, before conftest replaces it with a no-op for every test.
REAL_PREPARE = cli._prepare_credential_store

BASE_URL = "https://example.test/api"


class TestCredentialFile:
    def test_round_trip_across_unlocks(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        store = CredentialFile.create(path, "correct horse")
        store.set(SESSION_SERVICE, BASE_URL, "sealed-session")
        store.set(API_KEY_SERVICE, BASE_URL, "sk-key")

        reopened = CredentialFile.unlock(path, "correct horse")
        assert reopened.get(SESSION_SERVICE, BASE_URL) == "sealed-session"
        assert reopened.get(API_KEY_SERVICE, BASE_URL) == "sk-key"
        assert reopened.get(SESSION_SERVICE, "https://other") is None

    def test_nothing_is_written_until_the_first_secret(self, tmp_path: Path) -> None:
        """Choosing a passphrase then abandoning sign-in must leave nothing behind."""
        path = tmp_path / "credentials.enc"
        CredentialFile.create(path, "pw")
        assert not path.exists()

    def test_secrets_are_not_on_disk_in_the_clear(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        CredentialFile.create(path, "pw").set(SESSION_SERVICE, BASE_URL, "sealed-session-value")
        raw = path.read_text(encoding="utf-8")
        assert "sealed-session-value" not in raw
        assert BASE_URL not in raw
        assert json.loads(raw)["kdf"] == "scrypt"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        CredentialFile.create(path, "pw").set(SESSION_SERVICE, BASE_URL, "x")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_wrong_passphrase(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        CredentialFile.create(path, "right").set(SESSION_SERVICE, BASE_URL, "x")
        with pytest.raises(WrongPassphraseError):
            CredentialFile.unlock(path, "wrong")

    def test_tampering_is_detected(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        CredentialFile.create(path, "pw").set(SESSION_SERVICE, BASE_URL, "x")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["ciphertext"] = envelope["ciphertext"][:-4] + ("AAAA" if not envelope["ciphertext"].endswith("AAAA") else "BBBB")
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(WrongPassphraseError):
            CredentialFile.unlock(path, "pw")

    def test_garbage_file_names_its_path_in_the_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            CredentialFile.unlock(path, "pw")
        assert str(path) in caught.value.hint

    def test_empty_passphrase_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            CredentialFile.create(tmp_path / "c.enc", "")

    def test_delete(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.enc"
        store = CredentialFile.create(path, "pw")
        store.set(SESSION_SERVICE, BASE_URL, "x")
        first = store.delete(SESSION_SERVICE, BASE_URL)
        second = store.delete(SESSION_SERVICE, BASE_URL)
        assert first is True
        assert second is False
        assert CredentialFile.unlock(path, "pw").get(SESSION_SERVICE, BASE_URL) is None

    def test_repr_hides_secrets(self, tmp_path: Path) -> None:
        store = CredentialFile.create(tmp_path / "c.enc", "pw")
        store.set(SESSION_SERVICE, BASE_URL, "sealed-session-value")
        assert "sealed-session-value" not in repr(store)


class TestDispatch:
    """keyring_store routes to the file once selected; callers cannot tell."""

    def test_load_store_delete_go_to_the_file(self, tmp_path: Path) -> None:
        store = CredentialFile.create(tmp_path / "c.enc", "pw")
        keyring_store.use_file_store(store)
        keyring_store.store(SESSION_SERVICE, BASE_URL + "/", "sealed", hint="")
        assert keyring_store.load(SESSION_SERVICE, BASE_URL) == ("sealed", None)
        deleted = keyring_store.delete(SESSION_SERVICE, BASE_URL)
        assert deleted is True
        assert keyring_store.load(SESSION_SERVICE, BASE_URL) == (None, None)

    def test_a_locked_file_degrades_on_read_and_raises_on_write(self) -> None:
        keyring_store.use_file_store(None, locked_reason="locked: no terminal")
        assert keyring_store.load(SESSION_SERVICE, BASE_URL) == (None, "locked: no terminal")
        deleted = keyring_store.delete(SESSION_SERVICE, BASE_URL)
        assert deleted is False
        with pytest.raises(ConfigError) as caught:
            keyring_store.store(SESSION_SERVICE, BASE_URL, "x", hint="")
        assert "locked: no terminal" in caught.value.hint

    def test_describe_backend(self, tmp_path: Path) -> None:
        assert keyring_store.describe_backend() == "the OS keyring"
        keyring_store.use_file_store(None)
        assert "encrypted credential file" in keyring_store.describe_backend()


class TestPreference:
    def test_defaults_to_auto(self, tmp_path: Path) -> None:
        assert resolve_store_preference(tmp_path / "none.toml", env={}) is StorePreference.AUTO

    def test_config_file_then_env(self, tmp_path: Path) -> None:
        config = tmp_path / "c.toml"
        config.write_text('credential_store = "file"\n', encoding="utf-8")
        assert resolve_store_preference(config, env={}) is StorePreference.FILE
        assert resolve_store_preference(config, env={ENV_CREDENTIAL_STORE: "Keyring"}) is StorePreference.KEYRING

    def test_a_typo_is_an_error_not_a_silent_default(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            resolve_store_preference(tmp_path / "none.toml", env={ENV_CREDENTIAL_STORE: "keychain"})


# ---------------------------------------------------------------------------
# The CLI's choice and prompts
# ---------------------------------------------------------------------------


@pytest.fixture
def no_os_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host like a container: no OS keyring, a private data directory."""
    path = tmp_path / "data" / "credentials.enc"
    monkeypatch.setattr(keyring_store, "os_keyring_available", lambda: False)
    monkeypatch.setattr(keyring_store, "credential_file_path", lambda: path)
    monkeypatch.delenv(ENV_CREDENTIAL_STORE, raising=False)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    return path


def answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> list[str]:
    """Feed hidden prompts in order; returns the prompts that were shown."""
    queue: Iterator[str] = iter(replies)
    shown: list[str] = []

    def fake(prompt: str = "") -> str:
        shown.append(prompt)
        return next(queue)

    monkeypatch.setattr(cli.getpass, "getpass", fake)
    return shown


def args_for(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(config_file=tmp_path / "config.toml")


class TestPrepare:
    def test_an_os_keyring_is_used_when_there_is_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keyring_store, "os_keyring_available", lambda: True)
        monkeypatch.delenv(ENV_CREDENTIAL_STORE, raising=False)
        shown = answers(monkeypatch)
        REAL_PREPARE(args_for(tmp_path), create=True)
        assert not keyring_store.using_file_store()
        assert shown == []

    def test_no_keyring_creates_the_file_store_automatically(
        self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shown = answers(monkeypatch, "pw", "pw")
        REAL_PREPARE(args_for(tmp_path), create=True)
        assert keyring_store.using_file_store() and keyring_store.file_store_unlocked()
        assert shown == ["New passphrase: ", "Repeat it: "]
        assert "No OS keyring was found" in capsys.readouterr().out

        keyring_store.store(SESSION_SERVICE, BASE_URL, "sealed", hint="")
        assert CredentialFile.unlock(no_os_keyring, "pw").get(SESSION_SERVICE, BASE_URL) == "sealed"

    def test_mismatch_and_empty_are_retried(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        answers(monkeypatch, "", "a", "b", "pw", "pw")
        REAL_PREPARE(args_for(tmp_path), create=True)
        assert keyring_store.file_store_unlocked()

    def test_gives_up_after_three_attempts(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        answers(monkeypatch, "a", "b", "a", "b", "a", "b")
        with pytest.raises(ConfigError):
            REAL_PREPARE(args_for(tmp_path), create=True)

    def test_a_read_only_command_does_not_ask_for_a_new_passphrase(
        self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown = answers(monkeypatch)
        REAL_PREPARE(args_for(tmp_path), create=False)
        assert keyring_store.using_file_store() and not keyring_store.file_store_unlocked()
        assert shown == []
        assert keyring_store.load(SESSION_SERVICE, BASE_URL) == (None, None)

    def test_an_existing_file_is_unlocked(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        CredentialFile.create(no_os_keyring, "pw").set(SESSION_SERVICE, BASE_URL, "sealed")
        shown = answers(monkeypatch, "wrong", "pw")
        REAL_PREPARE(args_for(tmp_path), create=False)
        assert keyring_store.load(SESSION_SERVICE, BASE_URL) == ("sealed", None)
        assert len(shown) == 2 and str(no_os_keyring) in shown[0]

    def test_three_wrong_passphrases_say_how_to_start_over(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        CredentialFile.create(no_os_keyring, "pw").set(SESSION_SERVICE, BASE_URL, "sealed")
        answers(monkeypatch, "x", "y", "z")
        with pytest.raises(ConfigError) as caught:
            REAL_PREPARE(args_for(tmp_path), create=False)
        assert str(no_os_keyring) in caught.value.hint

    def test_without_a_terminal_an_existing_file_stays_locked(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        CredentialFile.create(no_os_keyring, "pw").set(SESSION_SERVICE, BASE_URL, "sealed")
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        shown = answers(monkeypatch)
        REAL_PREPARE(args_for(tmp_path), create=False)
        secret, reason = keyring_store.load(SESSION_SERVICE, BASE_URL)
        assert secret is None and reason and "locked" in reason
        assert shown == []

    def test_without_a_terminal_creating_is_refused_with_a_way_out(
        self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        with pytest.raises(ConfigError) as caught:
            REAL_PREPARE(args_for(tmp_path), create=True)
        assert "AGENTCORE_API_KEY" in caught.value.hint
        assert not no_os_keyring.exists()

    def test_file_preference_wins_over_a_working_keyring(
        self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(keyring_store, "os_keyring_available", lambda: True)
        monkeypatch.setenv(ENV_CREDENTIAL_STORE, "file")
        answers(monkeypatch, "pw", "pw")
        REAL_PREPARE(args_for(tmp_path), create=True)
        assert keyring_store.file_store_unlocked()
        # Chosen, not discovered, so the "no keyring found" explanation is not shown.
        assert "No OS keyring was found" not in capsys.readouterr().out

    def test_keyring_preference_never_falls_back(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CREDENTIAL_STORE, "keyring")
        REAL_PREPARE(args_for(tmp_path), create=True)
        assert not keyring_store.using_file_store()


class TestCommands:
    """End to end through ``cli.main`` on a host with no OS keyring."""

    def test_api_key_login_then_status_then_logout(
        self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_prepare_credential_store", REAL_PREPARE)
        monkeypatch.delenv("AGENTCORE_API_KEY", raising=False)
        monkeypatch.delenv("AGENTCORE_BASE_URL", raising=False)
        config = str(tmp_path / "config.toml")

        answers(monkeypatch, "pw", "pw")
        assert cli.main(["login", "--base-url", BASE_URL, "--api-key", "sk-test", "--config", config]) == 0
        assert "encrypted credential file" in capsys.readouterr().out
        assert CredentialFile.unlock(no_os_keyring, "pw").get(API_KEY_SERVICE, BASE_URL) == "sk-test"

        keyring_store.use_os_keyring()  # a new process starts on the default
        monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(cli.httpx.ConnectError("offline")))
        answers(monkeypatch, "pw")
        cli.main(["status", "--config", config])
        out = capsys.readouterr().out
        assert "credential  : API key" in out
        assert f"stored in   : encrypted file ({no_os_keyring})" in out
        assert "sk-test" not in out

        keyring_store.use_os_keyring()
        answers(monkeypatch, "pw")
        assert cli.main(["logout", "--config", config]) == 0
        assert CredentialFile.unlock(no_os_keyring, "pw").get(API_KEY_SERVICE, BASE_URL) is None

    def test_sso_login_asks_for_the_passphrase_before_the_browser(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Approving a sign-in and only then finding nowhere to keep it would waste it."""
        from aps_tui.client import device_auth

        events: list[str] = []
        monkeypatch.setattr(cli, "_prepare_credential_store", lambda args, *, create: events.append(f"prepare create={create}"))

        class Client:
            def __init__(self, base_url: str) -> None:
                pass

            async def __aenter__(self) -> Client:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def authorize(self) -> object:
                events.append("authorize")
                raise ConfigError("stop here")

        monkeypatch.setattr(device_auth, "DeviceAuthClient", Client)
        cli.main(["login", "--sso", "--base-url", BASE_URL, "--config", str(tmp_path / "c.toml")])
        assert events == ["prepare create=True", "authorize"]

    def test_first_run_chat_asks_for_a_passphrase_before_setup(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setup runs inside Textual, where getpass cannot; the passphrase comes first."""
        import aps_tui.app as app_module

        from .conftest import make_config
        from aps_tui.credentials import CredentialSource

        monkeypatch.setattr(cli, "_prepare_credential_store", REAL_PREPARE)
        unlocked_when_app_ran: list[bool] = []

        class FakeApp:
            def __init__(self, config: object, **kw: object) -> None:
                pass

            def run(self) -> None:
                unlocked_when_app_ran.append(keyring_store.file_store_unlocked())

        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(cli, "resolve_config", lambda **kw: make_config(base_url="", credential_source=CredentialSource.NONE))
        answers(monkeypatch, "pw", "pw")
        assert cli.main(["--config", str(tmp_path / "c.toml")]) == 0
        assert unlocked_when_app_ran == [True]

    def test_configured_chat_with_no_file_does_not_prompt(self, tmp_path: Path, no_os_keyring: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An API key from the environment needs no store at all."""
        import aps_tui.app as app_module

        from .conftest import make_config

        monkeypatch.setattr(cli, "_prepare_credential_store", REAL_PREPARE)

        class FakeApp:
            def __init__(self, config: object, **kw: object) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(cli, "resolve_config", lambda **kw: make_config())
        shown = answers(monkeypatch)
        assert cli.main(["--config", str(tmp_path / "c.toml")]) == 0
        assert shown == []
