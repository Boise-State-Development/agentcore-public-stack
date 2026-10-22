"""Establish a real BFF session by driving the Cognito Hosted UI.

Why this is necessary: ``POST /chat/stream`` is cookie-only. Bearer callers
were retired in the BFF migration (see the notes in
``apis/shared/auth/dependencies.py``), so there is no token you can mint with
``initiate-auth`` and present directly — the session cookie is only issued by
``GET /auth/callback`` in exchange for an authorization code. Getting that code
means completing the Hosted UI form the way a browser would.

The flow, mirroring ``apis/app_api/auth/bff/routes.py``:

  1. ``GET  {app_api}/auth/login``            -> 302 to Cognito /oauth2/authorize
                                                 (sets __Host-bff_oauth_state,
                                                 PKCE verifier, OIDC nonce)
  2. ``GET  {cognito}/oauth2/authorize?...``  -> Hosted UI login page (HTML form)
  3. ``POST {cognito}{form.action}``          -> 302 to the callback with ?code=&state=
  4. ``GET  {app_api}/auth/callback?...``     -> sets __Host-bff_session + __Host-bff_csrf
  5. ``GET  {app_api}/auth/session``          -> {user, csrf_token}

Step 3 is the brittle step: it depends on Cognito's login markup. The parser
below is deliberately generic (find the form with a password field, resubmit
every hidden input) rather than hardcoding field names, and raises a
descriptive error naming what it actually found so a markup change is
diagnosable instead of mysterious.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import urljoin

from .config import Credential, LoadConfig

logger = logging.getLogger(__name__)

# Cognito's own CSRF field on the Hosted UI form. Captured generically as a
# hidden input; named here only for the error message when it is absent.
_COGNITO_CSRF_FIELD = "_csrf"


class LoginError(RuntimeError):
    """A login attempt failed in a way that is not the system under test's fault."""


class _HtmlForm:
    def __init__(self, action: str | None, method: str) -> None:
        self.action = action
        self.method = method.lower()
        self.fields: dict[str, str] = {}
        self.password_field: str | None = None
        self.text_fields: list[str] = []

    def __repr__(self) -> str:
        return (
            f"_HtmlForm(action={self.action!r}, method={self.method!r}, "
            f"fields={sorted(self.fields)}, password_field={self.password_field!r})"
        )


class _FormParser(HTMLParser):
    """Collect every ``<form>`` with its inputs.

    Values of password inputs are ignored (they are never pre-filled, and we
    do not want a credential echoed into a parsed structure by accident).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_HtmlForm] = []
        self._current: _HtmlForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): (value or "") for key, value in attrs}

        if tag == "form":
            self._current = _HtmlForm(action=attr.get("action"), method=attr.get("method", "get"))
            self.forms.append(self._current)
            return

        if tag != "input" or self._current is None:
            return

        name = attr.get("name")
        if not name:
            return

        input_type = attr.get("type", "text").lower()
        if input_type == "password":
            self._current.password_field = name
            self._current.fields[name] = ""
        elif input_type in {"hidden", "text", "email", "tel"}:
            self._current.fields[name] = attr.get("value", "")
            if input_type != "hidden":
                self._current.text_fields.append(name)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _find_login_form(html: str) -> _HtmlForm:
    parser = _FormParser()
    parser.feed(html)

    candidates = [form for form in parser.forms if form.password_field]
    if not candidates:
        raise LoginError(
            "No form with a password input found on the Cognito login page. "
            f"Parsed {len(parser.forms)} form(s): {parser.forms!r}. If this "
            "deployment uses Cognito's managed login (branding v2) with a "
            "client-rendered form, scripted login is not supported — see "
            "tests/load/README.md."
        )
    return candidates[0]


def _pick_username_field(form: _HtmlForm) -> str:
    """Choose which field carries the username.

    Prefer an explicitly named field; otherwise fall back to the only
    non-hidden text input, which is what the Hosted UI renders.
    """
    for candidate in ("username", "email", "signInFormUsername"):
        if candidate in form.fields:
            return candidate
    if len(form.text_fields) == 1:
        return form.text_fields[0]
    raise LoginError(
        "Could not determine the username field on the Cognito login form. "
        f"Non-hidden text inputs: {form.text_fields!r}."
    )


def establish_bff_session(client, config: LoadConfig, credential: Credential) -> str:
    """Log in and return the CSRF token for the new session.

    ``client`` is a Locust ``HttpSession``: a ``requests.Session`` that also
    reports timings, so each hop below shows up as its own entry in the stats
    table. That is deliberate — login cost is part of what you want to see, and
    a login that degrades under load is a real finding.

    On return, ``client``'s cookie jar holds ``__Host-bff_session`` and the
    caller should send the returned token in ``X-CSRF-Token`` on every unsafe
    request.
    """
    authorize_url = _begin_login(client)
    login_page = _fetch_login_page(client, authorize_url)
    callback_url = _submit_credentials(client, authorize_url, login_page, credential)
    _complete_callback(client, callback_url)
    return _read_csrf_token(client)


def _begin_login(client) -> str:
    """GET /auth/login and return the Cognito authorize URL it redirects to."""
    with client.get(
        "/auth/login",
        allow_redirects=False,
        catch_response=True,
        name="GET /auth/login",
    ) as response:
        if response.status_code != 302:
            response.failure(f"expected 302, got {response.status_code}")
            raise LoginError(
                f"GET /auth/login returned {response.status_code}, expected a 302 to "
                "the Cognito Hosted UI."
            )
        location = response.headers.get("Location", "")
        if "/oauth2/authorize" not in location:
            response.failure("302 Location is not an /oauth2/authorize URL")
            raise LoginError(f"Unexpected login redirect target: {location!r}")
        response.success()
        return location


def _fetch_login_page(client, authorize_url: str) -> str:
    """Follow the authorize URL to the Hosted UI and return its HTML.

    Redirects are followed here: if the Cognito session is already warm this
    can bounce straight through to the callback, which is a legitimate
    outcome that the caller handles by finding no login form.
    """
    with client.get(
        authorize_url,
        catch_response=True,
        name="GET cognito /oauth2/authorize",
    ) as response:
        if response.status_code != 200:
            response.failure(f"expected 200, got {response.status_code}")
            raise LoginError(
                f"Cognito /oauth2/authorize returned {response.status_code}. Check that "
                "AGENTCORE_LOAD_COGNITO_DOMAIN matches the deployment's Hosted UI domain."
            )
        response.success()
        return response.text


def _submit_credentials(
    client,
    authorize_url: str,
    login_page: str,
    credential: Credential,
) -> str:
    """POST the login form and return the callback URL Cognito redirects to."""
    form = _find_login_form(login_page)
    if _COGNITO_CSRF_FIELD not in form.fields:
        logger.warning(
            "Cognito login form has no %r hidden field; submitting anyway. Fields present: %s",
            _COGNITO_CSRF_FIELD,
            sorted(form.fields),
        )

    username_field = _pick_username_field(form)
    payload = dict(form.fields)
    payload[username_field] = credential.username
    payload[form.password_field] = credential.password

    action_url = urljoin(authorize_url, form.action or "")

    with client.post(
        action_url,
        data=payload,
        allow_redirects=False,
        catch_response=True,
        name="POST cognito /login",
    ) as response:
        # A 200 means Cognito re-rendered the form: bad credentials, an
        # unconfirmed user, or a forced password change. All are provisioning
        # problems, not load findings, so fail loudly rather than retrying.
        if response.status_code == 200:
            response.failure("Cognito re-rendered the login form (credentials rejected)")
            raise LoginError(
                f"Cognito rejected the login for {credential.username!r}. The user may "
                "need a permanent password (FORCE_CHANGE_PASSWORD blocks scripted login)."
            )
        if response.status_code != 302:
            response.failure(f"expected 302, got {response.status_code}")
            raise LoginError(f"Cognito login POST returned {response.status_code}, expected a 302.")

        location = response.headers.get("Location", "")
        if "code=" not in location:
            response.failure("302 Location carries no authorization code")
            raise LoginError(f"Cognito login redirect has no ?code=: {location!r}")
        response.success()
        return location


def _complete_callback(client, callback_url: str) -> None:
    """GET /auth/callback to trade the code for session cookies."""
    with client.get(
        callback_url,
        allow_redirects=False,
        catch_response=True,
        name="GET /auth/callback",
    ) as response:
        # The callback answers 302 to the SPA on success. Following it would
        # fetch the Angular bundle from CloudFront on every simulated login,
        # which is load the real app only generates once per page visit.
        if response.status_code not in (302, 303):
            response.failure(f"expected 302, got {response.status_code}")
            raise LoginError(
                f"GET /auth/callback returned {response.status_code}. The state cookie "
                "binding may have failed — check that the run is over https so the "
                "__Host- cookies are stored."
            )
        response.success()


def _read_csrf_token(client) -> str:
    """GET /auth/session and return the CSRF token for this session."""
    with client.get("/auth/session", catch_response=True, name="GET /auth/session") as response:
        if response.status_code != 200:
            response.failure(f"expected 200, got {response.status_code}")
            raise LoginError(
                f"GET /auth/session returned {response.status_code} directly after a "
                "successful callback — the session cookie was not stored."
            )
        try:
            token = response.json().get("csrf_token")
        except ValueError as exc:
            response.failure("response was not JSON")
            raise LoginError("GET /auth/session did not return JSON.") from exc

        if not token:
            response.failure("no csrf_token in response")
            raise LoginError("GET /auth/session returned no csrf_token.")
        response.success()
        return str(token)
