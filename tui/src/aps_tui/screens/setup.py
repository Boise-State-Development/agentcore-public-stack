"""First-run setup: where is your deployment, and how do you sign in.

Shown by the app when the configuration is incomplete (no base URL, or a base
URL with no credential), instead of a notice telling the user to leave and run
``aps-tui login``. It collects the same things ``login`` does, in the same
order, and stores them in the same places (the OS keyring and the config
file), so the two paths cannot disagree about what "configured" means.

Three steps, each a small form in one modal:

1. **URL.** Pre-filled with whatever is already known. The host is probed with
   ``GET /health`` before moving on, and a URL typed without ``/api`` is
   retried with it, since every deployment of this platform serves app-api
   under that prefix behind CloudFront.
2. **Method.** Browser sign-in (a real session: agent, tools, memory) or an API
   key (single-turn chat only).
3. **Sign in.** The device flow with its code and URL and a countdown, or a
   hidden input for the key.

Dismisses with the base URL once stored, or None if the user backed out. The
app then restarts itself so every screen is built from the new configuration;
nothing here reaches into the chat screen.

All side effects (probe, device client, keyring, config file) are injectable
so tests never touch the network or a real keyring.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from ..client.device_auth import DeviceAuthClient, DeviceAuthorization, DeviceSession
from ..config import save_key_to_keyring, save_session_to_keyring, write_config_file
from ..errors import AgentCoreTuiError

logger = logging.getLogger(__name__)

#: Returned by the app's ``run()`` when setup stored a configuration and the
#: client should start again with it.
RESTART = "restart"

#: Seconds allowed for the health probe. Generous for a cold Fargate task
#: behind CloudFront, short enough that a typo does not feel like a hang.
PROBE_TIMEOUT = 8.0

Prober = Callable[[str], Awaitable[str | None]]
DeviceClientFactory = Callable[[str], DeviceAuthClient]


def normalise_base_url(raw: str) -> str:
    """``my-host`` -> ``https://my-host``; strips whitespace and a trailing slash."""
    url = raw.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


async def probe_base_url(url: str, *, client: httpx.AsyncClient | None = None) -> str | None:
    """The base URL that answered ``GET /health``, or None.

    Tries the URL as given, then with ``/api`` appended if it lacks it, so a
    user who types the address they see in their browser lands on the API.
    """
    candidates = [url] if url.endswith("/api") else [url, url + "/api"]
    async with client or httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        for candidate in candidates:
            try:
                response = await client.get(f"{candidate}/health")
            except httpx.HTTPError as exc:
                logger.info("probe %s failed: %s", candidate, exc)
                continue
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and "status" in body:
                    return candidate
    return None


class SetupScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "back", "Back")]

    def __init__(
        self,
        *,
        base_url: str = "",
        config_file: Path | None = None,
        probe: Prober = probe_base_url,
        device_client_factory: DeviceClientFactory = DeviceAuthClient,
        save_session: Callable[[str, str], None] = save_session_to_keyring,
        save_key: Callable[[str, str], None] = save_key_to_keyring,
        write_config: Callable[[dict[str, object], Path | None], Path] = write_config_file,
        open_browser: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        super().__init__()
        self._initial_url = base_url
        self._base_url = ""
        self._config_file = config_file
        self._probe = probe
        self._device_client_factory = device_client_factory
        self._save_session = save_session
        self._save_key = save_key
        self._write_config = write_config
        self._open_browser = open_browser
        self._step = "url"
        self._sign_in_worker: Worker[Any] | None = None

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-frame") as frame:
            frame.border_title = "Set up aps-tui"
            yield Label("", id="setup-title")
            yield Static("", id="setup-body", markup=False)
            yield Input(id="setup-input")
            yield OptionList(id="setup-choices")
            yield Static("", id="setup-error", markup=False)
            yield Label("", id="setup-help")

    def on_mount(self) -> None:
        self._show_url_step()

    # -- step: URL -----------------------------------------------------------

    def _show_url_step(self) -> None:
        self._step = "url"
        self._set(
            title="Where is your deployment?",
            body=(
                "The address of the web app you use, for example https://ai.example.edu\n"
                "The client only works against a deployment of this platform; nothing is built in."
            ),
            help_text="Enter continue · Esc quit",
        )
        field = self.query_one("#setup-input", Input)
        field.password = False
        field.placeholder = "https://your-host"
        field.value = self._initial_url
        field.display = True
        self.query_one("#setup-choices", OptionList).display = False
        field.focus()

    @work(exclusive=True, group="probe")
    async def _check_url(self, raw: str) -> None:
        url = normalise_base_url(raw)
        if not url:
            self._error("Type the address of your deployment.")
            return
        self._error("")
        self._help("Checking…")
        self.query_one("#setup-input", Input).disabled = True
        try:
            found = await self._probe(url)
        finally:
            self.query_one("#setup-input", Input).disabled = False
        if found is None:
            self._error(f"Nothing answered at {url}/health. Check the address, your network or VPN, then try again.")
            self._help("Enter continue · Esc quit")
            self.query_one("#setup-input", Input).focus()
            return
        self._base_url = found
        self._show_method_step()

    # -- step: method --------------------------------------------------------

    def _show_method_step(self) -> None:
        self._step = "method"
        self._set(
            title=f"How do you want to sign in to {self._base_url}?",
            body="",
            help_text="↑↓ move · Enter choose · Esc back",
        )
        self.query_one("#setup-input", Input).display = False
        choices = self.query_one("#setup-choices", OptionList)
        choices.clear_options()
        choices.add_options(
            [
                Option("Sign in with your browser   recommended: agent, tools, memory, saved conversations", id="sso"),
                Option("Use an API key             single-turn chat only", id="key"),
            ]
        )
        choices.display = True
        choices.highlighted = 0
        choices.focus()

    @on(OptionList.OptionSelected, "#setup-choices")
    def _method_chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "sso":
            self._show_sso_step()
        else:
            self._show_key_step()

    # -- step: browser sign-in -----------------------------------------------

    def _show_sso_step(self) -> None:
        self._step = "sso"
        self._set(title="Sign in with your browser", body="Starting…", help_text="Esc cancel")
        self.query_one("#setup-choices", OptionList).display = False
        self._sign_in_worker = self._run_device_flow()

    @work(exclusive=True, group="sign-in")
    async def _run_device_flow(self) -> None:
        body = self.query_one("#setup-body", Static)
        try:
            async with self._device_client_factory(self._base_url) as client:
                authorization: DeviceAuthorization = await client.authorize()
                opened = self._try_open_browser(authorization.verification_uri_complete)
                lead = "Opened your browser. " if opened else ""
                body.update(
                    f"{lead}Approve the sign-in there, then come back here.\n\n"
                    f"  Your code:  {authorization.user_code}\n"
                    f"  Open:       {authorization.verification_uri_complete}"
                )

                def progress(_wait: int, remaining: int) -> None:
                    self._help(f"{remaining // 60}m {remaining % 60:02d}s left to approve · Esc cancel")

                session: DeviceSession = await client.poll_for_session(authorization, on_progress=progress)
        except asyncio.CancelledError:
            raise
        except AgentCoreTuiError as exc:
            self._fail(exc.message, exc.hint)
            return
        except Exception as exc:  # a cosmetic screen must never crash the app
            logger.exception("device flow failed")
            self._fail(f"Sign-in failed: {exc}", "")
            return
        try:
            self._save_session(self._base_url, session.session)
            written = self._write_config({"base_url": self._base_url}, self._config_file)
        except AgentCoreTuiError as exc:
            self._fail(exc.message, exc.hint)
            return
        who = session.username or session.user_id or "your account"
        logger.info("setup: signed in as %s; wrote %s", who, written)
        self.dismiss(self._base_url)

    def _try_open_browser(self, url: str) -> bool:
        try:
            return bool(self._open_browser(url))
        except Exception:  # pragma: no cover - platform dependent
            return False

    # -- step: API key -------------------------------------------------------

    def _show_key_step(self) -> None:
        self._step = "key"
        self._set(
            title="Paste your API key",
            body="Create one in the web app under Settings → API Keys. It is shown there only once, and typing here is hidden.",
            help_text="Enter save · Esc back",
        )
        self.query_one("#setup-choices", OptionList).display = False
        field = self.query_one("#setup-input", Input)
        field.password = True
        field.placeholder = "API key"
        field.value = ""
        field.display = True
        field.focus()

    def _store_key(self, key: str) -> None:
        key = key.strip()
        if not key:
            self._error("The key is empty.")
            return
        try:
            self._save_key(self._base_url, key)
            self._write_config({"base_url": self._base_url}, self._config_file)
        except AgentCoreTuiError as exc:
            self._fail(exc.message, exc.hint)
            return
        self.dismiss(self._base_url)

    # -- events --------------------------------------------------------------

    @on(Input.Submitted, "#setup-input")
    def _submitted(self, event: Input.Submitted) -> None:
        if self._step == "url":
            self._check_url(event.value)
        elif self._step == "key":
            self._store_key(event.value)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self._sign_in_worker and event.state in (WorkerState.CANCELLED, WorkerState.ERROR):
            self._sign_in_worker = None

    def action_back(self) -> None:
        """Esc: one step back, or out of setup from the first step."""
        if self._step == "url":
            self.dismiss(None)
        elif self._step == "sso":
            if self._sign_in_worker is not None:
                self._sign_in_worker.cancel()
                self._sign_in_worker = None
            self._show_method_step()
        else:
            self._show_method_step() if self._step == "key" else self._show_url_step()

    # -- helpers -------------------------------------------------------------

    def _set(self, *, title: str, body: str, help_text: str) -> None:
        self.query_one("#setup-title", Label).update(title)
        body_widget = self.query_one("#setup-body", Static)
        body_widget.update(body)
        body_widget.display = bool(body)
        self._error("")
        self._help(help_text)

    def _error(self, text: str) -> None:
        widget = self.query_one("#setup-error", Static)
        widget.update(text)
        widget.display = bool(text)

    def _help(self, text: str) -> None:
        self.query_one("#setup-help", Label).update(text)

    def _fail(self, message: str, hint: str) -> None:
        """A failed sign-in returns to the method step with the reason shown."""
        self._show_method_step()
        self._error(f"{message}\n{hint}".strip())
