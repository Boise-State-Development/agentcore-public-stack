"""Server-rendered pages for the CLI device-authorization flow.

These are the only server-rendered HTML pages in app-api. Routing them
through the SPA would make the CLI's verification URL depend on the SPA's
router and build for screens that never change, and would mean a CLI login
could break because of a frontend deploy.

**Escape everything that is not a literal.** Headings and paragraphs are
literals defined in this module or a caller in this package. The
confirmation page is the one place a request-derived value appears (the user
code, already shape-checked against the code alphabet, plus a hex token), and
it is passed through ``html.escape`` anyway, so the safety of the page never
rests on a validator somewhere else.

Every page is sent with headers that keep it out of frames and caches and
stop it leaking its URL (which carries the user code) as a referrer. Framing
matters most for the confirmation page: a page that one click approves must
never be shown inside someone else's.

Kept in its own module so ``routes.py`` (the callback's device branch) and
``cli_routes.py`` (the verify endpoint) can both use them without importing
each other.
"""

from __future__ import annotations

import html
from typing import Literal

from fastapi import status
from fastapi.responses import HTMLResponse

_Tone = Literal["ok", "error", "ask"]

_ACCENTS: dict[_Tone, str] = {
    "ok": "#16a34a",
    "error": "#dc2626",
    "ask": "#2563eb",
}
_MARKS: dict[_Tone, str] = {
    "ok": "&check;",
    "error": "!",
    "ask": "?",
}

#: Sent with every page. `default-src 'none'` with inline styles only: the
#: pages load nothing. `form-action` is left unset on purpose, because the
#: confirmation form's answer is a redirect to the identity provider, and a
#: browser that applies `form-action` to redirects would block it.
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _page(*, title: str, heading: str, body: str, tone: _Tone) -> HTMLResponse:
    accent = _ACCENTS[tone]
    mark = _MARKS[tone]
    return HTMLResponse(
        status_code=status.HTTP_200_OK,
        headers=_PAGE_HEADERS,
        content=f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.6 system-ui, -apple-system, sans-serif;
    display: grid; place-items: center; min-height: 100dvh; margin: 0;
    padding: 2rem; background: Canvas; color: CanvasText;
  }}
  main {{ max-width: 32rem; text-align: center; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .5rem; }}
  p {{ margin: 0 0 .5rem; opacity: .85; }}
  .mark {{
    width: 3rem; height: 3rem; border-radius: 999px; margin: 0 auto 1.25rem;
    display: grid; place-items: center; color: #fff; font-size: 1.5rem;
    background: {accent};
  }}
  .code {{
    font: 600 1.75rem/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: .12em; margin: 1rem 0; opacity: 1;
  }}
  .actions {{ display: flex; gap: .75rem; justify-content: center; margin-top: 1.5rem; }}
  button {{
    font: inherit; padding: .5rem 1.25rem; border-radius: .5rem; cursor: pointer;
    border: 1px solid color-mix(in srgb, CanvasText 30%, transparent);
    background: transparent; color: inherit;
  }}
  button.primary {{ background: {accent}; border-color: {accent}; color: #fff; }}
  button:focus-visible {{ outline: 2px solid {accent}; outline-offset: 2px; }}
</style>
</head>
<body>
  <main>
    <div class="mark" aria-hidden="true">{mark}</div>
    <h1>{heading}</h1>
    {body}
  </main>
</body>
</html>""",
    )


def device_problem_page(heading: str, detail: str) -> HTMLResponse:
    """A dead end the human can act on.

    Returns 200, not 4xx: this is a rendered page for a person, and a browser
    error status would invite the CLI or a proxy to treat it as a retryable
    failure. The terminal learns the real outcome by polling, not from here.
    """
    return _page(title="Sign-in problem", heading=heading, body=f"<p>{detail}</p>", tone="error")


def device_confirm_page(*, display_code: str, user_code: str, confirm_token: str) -> HTMLResponse:
    """Ask the person to check the code before anything else happens.

    RFC 8628 §3.3.1: a code that arrived prefilled in the link must still be
    shown and confirmed, because the person who opens a link is not always
    the person whose terminal produced it. Nothing has been sent to the
    identity provider yet, and nothing is approved, until Continue is
    submitted from this page.

    The form posts to ``verify`` relative to this page's own URL, so it lands
    on the same route whatever path prefix the deployment serves app-api
    under, and the query string (with the user code in it) is not resent.
    """
    shown = html.escape(display_code, quote=True)
    code_field = html.escape(user_code, quote=True)
    token_field = html.escape(confirm_token, quote=True)
    body = (
        "<p>A terminal is asking to sign in to your account with this code:</p>"
        f'<p class="code">{shown}</p>'
        "<p>Continue only if you started this sign-in yourself and the code "
        "matches the one in your terminal. If it doesn't, choose Cancel.</p>"
        '<form method="post" action="verify">'
        f'<input type="hidden" name="user_code" value="{code_field}">'
        f'<input type="hidden" name="confirm_token" value="{token_field}">'
        '<div class="actions">'
        '<button type="submit" name="action" value="cancel">Cancel</button>'
        '<button type="submit" name="action" value="continue" class="primary">Continue</button>'
        "</div>"
        "</form>"
    )
    return _page(title="Confirm sign-in", heading="Sign in to your terminal?", body=body, tone="ask")


def device_cancelled_page() -> HTMLResponse:
    """Terminal state after Cancel — the CLI reports the refusal itself."""
    return _page(
        title="Sign-in cancelled",
        heading="Sign-in cancelled",
        body=("<p>Nothing was signed in. Your terminal will say the request was declined.</p><p>You can close this tab.</p>"),
        tone="error",
    )


def device_approved_page() -> HTMLResponse:
    """Terminal state for the browser leg — the CLI takes it from here."""
    return _page(
        title="Signed in",
        heading="You're signed in",
        body=("<p>Return to your terminal — it should continue automatically.</p><p>You can close this tab.</p>"),
        tone="ok",
    )
