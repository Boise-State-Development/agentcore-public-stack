"""First-run setup: URL probe, sign-in method, device flow and API key, storage."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.app import App
from textual.widgets import Input

from aps_tui import cli
from aps_tui.app import ChatApp
from aps_tui.client.device_auth import DeviceAuthorization, DeviceSession
from aps_tui.config import Config
from aps_tui.credentials import CredentialSource
from aps_tui.errors import ConfigError
from aps_tui.screens.setup import RESTART, SetupScreen, normalise_base_url, probe_base_url

from .conftest import make_config, rendered_text


class TestNormalise:
    def test_adds_scheme_and_strips_slash(self) -> None:
        assert normalise_base_url("  ai.example.edu/ ") == "https://ai.example.edu"
        assert normalise_base_url("http://localhost:8000/") == "http://localhost:8000"
        assert normalise_base_url("") == ""


class TestProbe:
    @staticmethod
    def _client(handler: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_retries_with_api_suffix(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/api/health":
                return httpx.Response(200, json={"status": "healthy"})
            return httpx.Response(200, text="<html>spa</html>")

        assert await probe_base_url("https://h", client=self._client(handler)) == "https://h/api"
        assert seen == ["/health", "/api/health"]

    async def test_nothing_answering_is_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        assert await probe_base_url("https://h/api", client=self._client(handler)) is None


class FakeDeviceClient:
    """Stands in for DeviceAuthClient: authorises at once, approves after one poll."""

    def __init__(self, base_url: str, *, fail: Exception | None = None) -> None:
        self.base_url = base_url
        self.fail = fail

    async def __aenter__(self) -> FakeDeviceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def authorize(self) -> DeviceAuthorization:
        if self.fail:
            raise self.fail
        return DeviceAuthorization(
            device_code="d", user_code="ABCD-EFGH", verification_uri="u", verification_uri_complete="https://h/verify?c=ABCD-EFGH"
        )

    async def poll_for_session(self, authorization: DeviceAuthorization, *, on_progress: Any = None) -> DeviceSession:
        if on_progress:
            on_progress(5, 540)
        return DeviceSession(session="sealed", expires_in=7200, user_id="u1", username="jordan")


class Store:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.config: dict[str, object] = {}
        self.opened: list[str] = []

    def save_session(self, url: str, sealed: str) -> None:
        self.sessions[url] = sealed

    def save_key(self, url: str, key: str) -> None:
        self.keys[url] = key

    def write_config(self, settings: dict[str, object], path: Path | None) -> Path:
        self.config.update(settings)
        return Path("/tmp/config.toml")

    def open_browser(self, url: str) -> bool:
        self.opened.append(url)
        return True


class Host(App[str | None]):
    CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "aps_tui" / "app.tcss"


async def probe_ok(url: str) -> str | None:
    return url if url.startswith("https://good") else None


def screen_for(store: Store, *, base_url: str = "", fail: Exception | None = None) -> SetupScreen:
    return SetupScreen(
        base_url=base_url,
        probe=probe_ok,
        device_client_factory=lambda url: FakeDeviceClient(url, fail=fail),  # type: ignore[arg-type,return-value]
        save_session=store.save_session,
        save_key=store.save_key,
        write_config=store.write_config,
        open_browser=store.open_browser,
    )


async def settle(pilot: Any, predicate: Any, tries: int = 20) -> None:
    for _ in range(tries):
        await pilot.pause()
        if predicate():
            return


class TestSetupScreen:
    async def test_browser_sign_in_stores_session_and_url(self) -> None:
        store = Store()
        results: list[str | None] = []
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen_for(store), callback=results.append)
            await pilot.pause()
            assert "Where is your deployment?" in rendered_text(app)
            await pilot.press(*"good.example.edu")
            await pilot.press("enter")
            await settle(pilot, lambda: "How do you want to sign in" in rendered_text(app))
            assert re.search(r"https://good\.example\.edu\b", rendered_text(app))
            await pilot.press("enter")  # browser sign-in is highlighted first
            await settle(pilot, lambda: bool(results))
        assert results == ["https://good.example.edu"]
        assert store.sessions == {"https://good.example.edu": "sealed"}
        assert store.config == {"base_url": "https://good.example.edu"}
        assert store.opened == ["https://h/verify?c=ABCD-EFGH"]

    async def test_the_code_and_url_are_shown_while_waiting(self) -> None:
        """The browser launch fails silently on headless hosts; the code and
        URL are the only way through, so they must be on screen."""
        store = Store()
        store.open_browser = lambda url: False  # type: ignore[method-assign,assignment]

        class Slow(FakeDeviceClient):
            async def poll_for_session(self, authorization: DeviceAuthorization, *, on_progress: Any = None) -> DeviceSession:
                import asyncio

                if on_progress:
                    on_progress(5, 540)
                await asyncio.sleep(10)
                return await super().poll_for_session(authorization)

        screen = SetupScreen(
            base_url="https://good",
            probe=probe_ok,
            device_client_factory=lambda url: Slow(url),  # type: ignore[arg-type,return-value]
            save_session=store.save_session,
            save_key=store.save_key,
            write_config=store.write_config,
            open_browser=store.open_browser,
        )
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("enter")  # URL pre-filled
            await settle(pilot, lambda: "How do you want" in rendered_text(app))
            await pilot.press("enter")
            await settle(pilot, lambda: "ABCD-EFGH" in rendered_text(app))
            frame = rendered_text(app)
            assert "ABCD-EFGH" in frame and "https://h/verify?c=ABCD-EFGH" in frame and "9m 00s left" in frame
            await pilot.press("escape")  # cancels the wait, back to the method step
            await settle(pilot, lambda: "How do you want" in rendered_text(app))
        assert store.sessions == {}

    async def test_api_key_path(self) -> None:
        store = Store()
        results: list[str | None] = []
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen_for(store, base_url="https://good/api"), callback=results.append)
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot, lambda: "How do you want" in rendered_text(app))
            await pilot.press("down", "enter")
            await settle(pilot, lambda: "Paste your API key" in rendered_text(app))
            assert app.screen.query_one("#setup-input", Input).password
            await pilot.press(*"sk-secret")
            await pilot.press("enter")
            await settle(pilot, lambda: bool(results))
        assert results == ["https://good/api"]
        assert store.keys == {"https://good/api": "sk-secret"}
        assert store.config == {"base_url": "https://good/api"}

    async def test_a_bad_url_stays_on_the_first_step_with_a_reason(self) -> None:
        store = Store()
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen_for(store))
            await pilot.pause()
            await pilot.press(*"bad.example.edu")
            await pilot.press("enter")
            await settle(pilot, lambda: "Nothing answered" in rendered_text(app))
            assert "https://bad.example.edu/health" in rendered_text(app)
            assert isinstance(app.screen, SetupScreen) and app.screen._step == "url"

    async def test_a_failed_sign_in_returns_to_the_method_step(self) -> None:
        store = Store()
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen_for(store, base_url="https://good", fail=ConfigError("Sign-in was denied", hint="Choose Approve.")))
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot, lambda: "How do you want" in rendered_text(app))
            await pilot.press("enter")
            await settle(pilot, lambda: "Sign-in was denied" in rendered_text(app))
            frame = rendered_text(app)
            assert "Choose Approve." in frame and "How do you want" in frame
        assert store.sessions == {}

    async def test_escape_on_the_first_step_leaves_setup(self) -> None:
        results: list[str | None] = []
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen_for(Store()), callback=results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
        assert results == [None]


class TestAppIntegration:
    def _unconfigured(self) -> Config:
        return make_config(base_url="", api_key=None, credential_source=CredentialSource.NONE, banner=False)

    async def test_an_unconfigured_app_opens_setup_over_the_notice(self) -> None:
        app = ChatApp(self._unconfigured())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, SetupScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert "Not configured" in rendered_text(app)
            assert "walked through setup" in rendered_text(app)

    async def test_a_configured_app_does_not(self) -> None:
        app = ChatApp(make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION, banner=False))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, SetupScreen)

    async def test_finishing_setup_exits_with_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = Store()
        app = ChatApp(self._unconfigured())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SetupScreen)
            screen._probe = probe_ok
            screen._device_client_factory = lambda url: FakeDeviceClient(url)  # type: ignore[assignment,arg-type,return-value]
            screen._save_session = store.save_session
            screen._write_config = store.write_config
            screen._open_browser = store.open_browser
            await pilot.press(*"good.example.edu")
            await pilot.press("enter")
            await settle(pilot, lambda: "How do you want" in rendered_text(app))
            await pilot.press("enter")
            await settle(pilot, lambda: not app.is_running, tries=40)
        assert app.return_value == RESTART

    def test_the_cli_relaunches_after_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runs: list[Config] = []
        results = iter([RESTART, None])

        class FakeApp:
            def __init__(self, config: Config, **kw: Any) -> None:
                runs.append(config)

            def run(self) -> str | None:
                return next(results)

        import aps_tui.app as app_module

        monkeypatch.setattr(app_module, "ChatApp", FakeApp)
        monkeypatch.setattr(cli, "resolve_config", lambda **kw: make_config(force_banner=kw.get("force_banner", False)))
        assert cli.main(["--banner"]) == 0
        assert len(runs) == 2
        assert runs[0].force_banner is True and runs[1].force_banner is False
