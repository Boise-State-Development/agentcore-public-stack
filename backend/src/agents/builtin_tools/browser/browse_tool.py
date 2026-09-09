"""`browse_web` — drive a real browser in the AgentCore Browser sandbox.

One action per call, against a browser session scoped to the conversation
(see `session_pool`). The model navigates, reads, interacts, and closes.

Cost posture. Every action's output is capped before it reaches the model,
because a browsing transcript is otherwise the classic unbounded per-turn
payload the cost tenet exists to catch: page text is truncated, evaluated
results are truncated, and a screenshot is only ever taken when the model
explicitly asks — vision tokens are the single most expensive thing this tool
can produce, so it is never automatic. Prefer `extract_text` over `screenshot`
for reading; prefer `evaluate` over both for structured extraction.

Interaction is done through JS in the page rather than synthesized input
events. That covers clicking, filling and reading for ordinary pages, keeps
the CDP surface small, and is what AWS's own guidance recommends for
extraction. Pages that require true trusted input (drag, some canvas widgets,
a few anti-bot forms) are a known limitation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from strands import tool
from strands.types.tools import ToolContext

from .cdp_client import CdpError
from . import session_pool

logger = logging.getLogger(__name__)

# Output budgets. Roughly: 8000 chars ≈ 2k tokens of page text per action.
MAX_TEXT_CHARS = int(os.environ.get("BROWSER_MAX_TEXT_CHARS", 8000))
MAX_EVAL_CHARS = int(os.environ.get("BROWSER_MAX_EVAL_CHARS", 4000))
MAX_LINKS = int(os.environ.get("BROWSER_MAX_LINKS", 50))

_ALLOWED_SCHEMES = ("http", "https")

# Page text, preferring the main content region over site chrome.
_EXTRACT_TEXT_JS = """
(() => {
  const el = document.querySelector('main') || document.querySelector('article') || document.body;
  return el ? el.innerText : '';
})()
"""

_EXTRACT_LINKS_JS = """
(() => {
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href || seen.has(href)) continue;
    if (!href.startsWith('http')) continue;
    seen.add(href);
    out.push({ text: (a.innerText || '').trim().slice(0, 120), href });
    if (out.length >= %d) break;
  }
  return out;
})()
"""


def _ok(text: str, extra: Optional[list] = None) -> Dict[str, Any]:
    content = [{"text": text}]
    if extra:
        content.extend(extra)
    return {"content": content, "status": "success"}


def _err(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _truncate(value: str, limit: int, label: str) -> str:
    if len(value) <= limit:
        return value
    return (
        value[:limit]
        + f"\n\n[{label} truncated at {limit} chars of {len(value)}. "
        "Use `evaluate` with a selector to extract only what you need.]"
    )


def _tool_enabled() -> bool:
    """Kill switch. Default on, per house style."""
    return os.environ.get("BROWSER_TOOL_ENABLED", "true").strip().lower() != "false"


def _validate_url(url: str) -> Optional[str]:
    """Return an error string if the URL is not a safe public target."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"❌ Only http/https URLs are supported (got '{parsed.scheme or 'none'}')."
    host = (parsed.hostname or "").lower()
    if not host:
        return "❌ URL has no host."
    # The browser runs in AWS with PUBLIC network mode, so it cannot reach our
    # VPC — but blocking the obvious loopback/metadata targets keeps the tool
    # honest if the network mode ever changes.
    if host in ("localhost", "127.0.0.1", "::1", "169.254.169.254", "metadata.google.internal"):
        return "❌ Refusing to browse loopback or instance-metadata addresses."
    return None


def _js_string(value: str) -> str:
    """Embed a Python string as a JS literal."""
    return json.dumps(value)


@tool(context=True)
async def browse_web(
    action: str,
    tool_context: ToolContext,
    url: Optional[str] = None,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    script: Optional[str] = None,
) -> Dict[str, Any]:
    """Browse the web in a real Chrome browser: navigate pages, read content, fill forms, click.

    Use this when a task needs a live, interactive page — content behind
    JavaScript rendering, a multi-step flow, a search you must click through.
    For a single static page, `fetch_url_content` is cheaper and faster; reach
    for this tool when that one is not enough.

    The browser session persists across calls within the conversation, so you
    can navigate then interact then read. Call `close` when finished.

    Args:
        action: One of:
            `navigate`      — go to `url` and return the page title and text.
            `extract_text`  — return the current page's visible text.
            `extract_links` — return the current page's links.
            `click`         — click the element matching `selector`.
            `type`          — set `text` into the input matching `selector`.
            `evaluate`      — run `script` (JavaScript) and return its value.
                              The best tool for structured extraction, e.g.
                              `[...document.querySelectorAll('h2')].map(e => e.innerText)`.
            `screenshot`    — capture the viewport as an image. Expensive in
                              tokens; only use it when you must SEE the layout.
            `live_view`     — get a URL where the user can watch the session.
            `close`         — end the browser session.
        url: Target URL for `navigate` (http/https).
        selector: CSS selector for `click` and `type`.
        text: Text to enter for `type`.
        script: JavaScript expression for `evaluate`.

    Returns:
        ToolResult with the action's output, truncated to a token budget.
    """
    if not _tool_enabled():
        return _err("❌ The browser tool is disabled in this environment.")

    action = (action or "").strip().lower()
    agent = getattr(tool_context, "agent", None)
    if agent is None:
        return _err("❌ Browser tool has no agent context.")

    if action == "close":
        stopped = await session_pool.release(agent)
        return _ok("✅ Browser session closed." if stopped else "No browser session was open.")

    if action == "navigate":
        if not url:
            return _err("❌ `navigate` requires `url`.")
        invalid = _validate_url(url)
        if invalid:
            return _err(invalid)

    try:
        live = await session_pool.acquire(agent)
    except Exception as exc:  # noqa: BLE001 - surface the reason, not a traceback
        logger.error("browser: could not start session: %s", exc, exc_info=True)
        return _err(f"❌ Could not start a browser session: {exc}")

    async with live.lock:
        try:
            return await _dispatch(live, action, url, selector, text, script, agent)
        except CdpError as exc:
            return _err(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("browser: action '%s' failed: %s", action, exc, exc_info=True)
            return _err(f"❌ Browser action '{action}' failed: {exc}")


async def _dispatch(
    live: Any,
    action: str,
    url: Optional[str],
    selector: Optional[str],
    text: Optional[str],
    script: Optional[str],
    agent: Any,
) -> Dict[str, Any]:
    cdp = live.cdp

    if action == "navigate":
        await cdp.navigate(url)  # type: ignore[arg-type]
        title = await cdp.evaluate("document.title")
        current = await cdp.evaluate("location.href")
        body = await cdp.evaluate(_EXTRACT_TEXT_JS) or ""
        return _ok(
            f"✅ Navigated to {current}\nTitle: {title}\n\n"
            + _truncate(str(body), MAX_TEXT_CHARS, "Page text")
        )

    if action == "extract_text":
        body = await cdp.evaluate(_EXTRACT_TEXT_JS) or ""
        return _ok(_truncate(str(body), MAX_TEXT_CHARS, "Page text"))

    if action == "extract_links":
        links = await cdp.evaluate(_EXTRACT_LINKS_JS % MAX_LINKS) or []
        if not links:
            return _ok("No links found on this page.")
        lines = [f"- {item.get('text') or '(no text)'} → {item.get('href')}" for item in links]
        return _ok(f"{len(lines)} link(s):\n" + "\n".join(lines))

    if action == "click":
        if not selector:
            return _err("❌ `click` requires `selector`.")
        clicked = await cdp.evaluate(
            f"""
            (() => {{
              const el = document.querySelector({_js_string(selector)});
              if (!el) return false;
              el.click();
              return true;
            }})()
            """
        )
        if not clicked:
            return _err(f"❌ No element matches selector `{selector}`.")
        current = await cdp.evaluate("location.href")
        return _ok(f"✅ Clicked `{selector}`. Current URL: {current}")

    if action == "type":
        if not selector or text is None:
            return _err("❌ `type` requires `selector` and `text`.")
        # Set the value and fire the events frameworks listen for, so Angular
        # / React state updates rather than silently keeping the old value.
        typed = await cdp.evaluate(
            f"""
            (() => {{
              const el = document.querySelector({_js_string(selector)});
              if (!el) return false;
              el.focus();
              el.value = {_js_string(text)};
              el.dispatchEvent(new Event('input', {{ bubbles: true }}));
              el.dispatchEvent(new Event('change', {{ bubbles: true }}));
              return true;
            }})()
            """
        )
        if not typed:
            return _err(f"❌ No element matches selector `{selector}`.")
        return _ok(f"✅ Entered text into `{selector}`.")

    if action == "evaluate":
        if not script:
            return _err("❌ `evaluate` requires `script`.")
        value = await cdp.evaluate(script)
        try:
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            rendered = str(value)
        return _ok(_truncate(rendered, MAX_EVAL_CHARS, "Result"))

    if action == "screenshot":
        encoded = await cdp.screenshot()
        if not encoded:
            return _err("❌ Screenshot returned no data.")
        return _ok(
            "✅ Screenshot of the current viewport:",
            [{"image": {"format": "png", "source": {"bytes": base64.b64decode(encoded)}}}],
        )

    if action == "live_view":
        view_url = await session_pool.live_view_url(agent)
        if not view_url:
            return _err("❌ No live view available for this session.")
        return _ok(f"Watch the browser session here (expires shortly):\n{view_url}")

    return _err(
        f"❌ Unknown action '{action}'. Valid actions: navigate, extract_text, "
        "extract_links, click, type, evaluate, screenshot, live_view, close."
    )
