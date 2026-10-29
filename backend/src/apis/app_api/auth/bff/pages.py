"""Server-rendered pages for the CLI device-authorization flow.

These are the only server-rendered HTML pages in app-api. Routing them
through the SPA would make the CLI's verification URL depend on the SPA's
router and build for two screens that never change, and would mean a CLI
login could break because of a frontend deploy.

**No request input reaches this markup.** Every interpolated value is a
literal defined in this module or a caller in this package, which is what
keeps these pages XSS-free without an escaping layer. If you ever need to
echo a user-supplied value here, escape it explicitly with
``html.escape`` — do not rely on the current shape.

Kept in its own module so ``routes.py`` (the callback's device branch) and
``cli_routes.py`` (the verify endpoint) can both use them without importing
each other.
"""

from __future__ import annotations

from fastapi import status
from fastapi.responses import HTMLResponse

_OK_ACCENT = "#16a34a"
_ERROR_ACCENT = "#dc2626"


def _page(*, title: str, heading: str, body: str, ok: bool) -> HTMLResponse:
    accent = _OK_ACCENT if ok else _ERROR_ACCENT
    mark = "&check;" if ok else "!"
    return HTMLResponse(
        status_code=status.HTTP_200_OK,
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
    return _page(title="Sign-in problem", heading=heading, body=f"<p>{detail}</p>", ok=False)


def device_approved_page() -> HTMLResponse:
    """Terminal state for the browser leg — the CLI takes it from here."""
    return _page(
        title="Signed in",
        heading="You're signed in",
        body=("<p>Return to your terminal — it should continue automatically.</p>" "<p>You can close this tab.</p>"),
        ok=True,
    )
