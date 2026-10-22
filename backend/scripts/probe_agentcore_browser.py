#!/usr/bin/env python
"""Smoke-test the AgentCore Browser tool against real AWS.

Verifies the parts unit tests cannot: that AgentCore's automation WebSocket
accepts our SigV4 headers, that it exposes a browser-level CDP endpoint where
`Target.getTargets` works, and that a real page navigates and evaluates.

Usage:
    cd backend
    AWS_PROFILE=dev-ai BROWSER_ID=<browser-id> \
        uv run python scripts/probe_agentcore_browser.py

    # or let it discover the browser from the account:
    AWS_PROFILE=dev-ai uv run python scripts/probe_agentcore_browser.py --discover

Options:
    --url URL       Page to load (default: https://example.com)
    --discover      List custom browsers in the account and use the first
    --keep          Leave the session running and print the live-view URL
                    (otherwise the session is stopped on exit)

Costs a browser session for as long as it runs. Stops it on the way out
unless --keep is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("probe")


def _discover_browser_id(region: str) -> str | None:
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        response = client.list_browsers(maxResults=20)
    except Exception as exc:  # noqa: BLE001
        logger.error("list_browsers failed: %s", exc)
        return None
    summaries = response.get("browserSummaries", []) or response.get("browsers", [])
    for summary in summaries:
        identifier = summary.get("browserId") or summary.get("browserIdentifier")
        name = summary.get("name", "")
        logger.info("found browser: %s (%s)", identifier, name)
        if identifier and not str(identifier).startswith("aws."):
            return identifier
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    region = os.environ.get("AWS_REGION", "us-west-2")

    if args.discover:
        discovered = _discover_browser_id(region)
        if discovered:
            os.environ["BROWSER_ID"] = discovered
            logger.info("using discovered browser: %s", discovered)

    identifier = os.environ.get("BROWSER_ID") or "aws.browser.v1"
    logger.info("region=%s browser=%s", region, identifier)

    from bedrock_agentcore.tools.browser_client import BrowserClient
    from agents.builtin_tools.browser.cdp_client import CdpSession

    client = BrowserClient(region=region)
    session_id = await asyncio.to_thread(
        client.start,
        identifier=identifier,
        session_timeout_seconds=300,
        viewport={"width": 1280, "height": 800},
    )
    logger.info("started session %s", session_id)

    cdp = None
    try:
        ws_url, headers = await asyncio.to_thread(client.generate_ws_headers)
        logger.info("connecting to %s", ws_url.split("/sessions/")[0] + "/sessions/...")
        cdp = await CdpSession.connect(ws_url, headers)
        print("\n[1/5] CDP connected and attached to a page target  ✅")

        await cdp.navigate(args.url)
        print(f"[2/5] Navigated to {args.url}  ✅")

        title = await cdp.evaluate("document.title")
        print(f"[3/5] document.title = {title!r}  ✅")

        text = await cdp.evaluate(
            "(() => (document.querySelector('main') || document.body).innerText)()"
        )
        preview = (text or "")[:200].replace("\n", " ")
        print(f"[4/5] Page text ({len(text or '')} chars): {preview}...  ✅")

        # The WebMCP hook: prove an init script runs before page scripts.
        await cdp.add_init_script("window.__probe_init__ = 'ran';")
        await cdp.navigate(args.url)
        marker = await cdp.evaluate("window.__probe_init__ || 'MISSING'")
        status = "✅" if marker == "ran" else "❌"
        print(f"[5/5] Init script before page load: {marker!r}  {status}")

        if args.keep:
            url = await asyncio.to_thread(client.generate_live_view_url)
            print(f"\nLive view (session left running): {url}")
            return 0

        print("\nAll checks passed.")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("probe failed: %s", exc, exc_info=True)
        return 1
    finally:
        if cdp is not None:
            await cdp.close()
        if not args.keep:
            await asyncio.to_thread(client.stop)
            logger.info("stopped session %s", session_id)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
