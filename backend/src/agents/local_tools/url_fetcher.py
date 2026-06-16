"""
Simple URL Fetcher Tool - Strands Native
Fetches and extracts text content from web pages
"""

import logging

from apis.shared.security import UrlValidationError, validate_external_url
from strands import tool

logger = logging.getLogger(__name__)


# Cap on redirects we'll follow manually (each one re-validated). Three
# is generous for legitimate sites (canonical-host shuffles, http→https
# upgrades, www → bare-domain) and tight enough to avoid loops.
_MAX_REDIRECTS = 3


def extract_text_from_html(html: str, max_length: int = 50000) -> str:
    """Extract clean text from HTML content"""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()

        # Get text
        text = soup.get_text()

        # Clean up whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)

        # Limit length
        if len(text) > max_length:
            text = text[:max_length] + "\n\n[Content truncated...]"

        return text

    except ImportError:
        # If BeautifulSoup not available, return raw text with basic cleanup
        import re

        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", html)
        # Clean up whitespace
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[Content truncated...]"

        return text


@tool
async def fetch_url_content(url: str, include_html: bool = False, max_length: int = 50000) -> dict:
    """
    Fetch and extract text content from a web page URL.
    Useful for retrieving job descriptions, articles, documentation, or any web content.

    Args:
        url: The URL to fetch (must start with http:// or https://)
        include_html: If True, includes raw HTML in response (default: False)
        max_length: Maximum character length of extracted text (default: 50000)

    Returns:
        Tool result with extracted text content, title, and metadata

    Examples:
        # Fetch job posting
        fetch_url_content("https://jobs.example.com/senior-engineer")

        # Fetch article
        fetch_url_content("https://blog.example.com/tech-trends-2025")

        # Fetch with HTML
        fetch_url_content("https://example.com", include_html=True)
    """
    try:
        import httpx

        # Pre-flight URL validation. Rejects loopback / link-local /
        # private / multicast / reserved targets and resolves every DNS
        # answer (defeats DNS rebinding). The error message is generic on
        # purpose — the validator logs structural detail server-side.
        try:
            validate_external_url(url)
        except UrlValidationError:
            return {
                "content": [
                    {
                        "json": {
                            "success": False,
                            "error": "URL is not permitted.",
                            "url": url,
                        }
                    }
                ],
                "status": "error",
            }

        # Redirect handling is manual: each hop is re-validated against the
        # same policy as the initial URL. follow_redirects=True would let
        # an attacker bounce a public-facing URL to an internal target on
        # a 302 without going through the validator.
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; StrandsAgent/1.0; +https://strands.ai)"}

            current_url = url
            redirects_followed = 0
            while True:
                response = await client.get(current_url, headers=headers)

                # Manual redirect chase. httpx exposes 3xx responses
                # directly when follow_redirects=False, so check the
                # status code rather than relying on a redirect history.
                if 300 <= response.status_code < 400 and response.status_code != 304:
                    redirects_followed += 1
                    if redirects_followed > _MAX_REDIRECTS:
                        logger.warning(
                            "Refusing to follow more than %d redirects from %s",
                            _MAX_REDIRECTS,
                            url,
                        )
                        return {
                            "content": [
                                {
                                    "json": {
                                        "success": False,
                                        "error": "Too many redirects.",
                                        "url": url,
                                    }
                                }
                            ],
                            "status": "error",
                        }
                    location = response.headers.get("location")
                    if not location:
                        # 3xx without Location: nothing useful to do; fall
                        # through and surface the response as-is.
                        break
                    # Resolve relative URLs against the current URL.
                    next_url = str(httpx.URL(current_url).join(location))
                    try:
                        validate_external_url(next_url)
                    except UrlValidationError:
                        return {
                            "content": [
                                {
                                    "json": {
                                        "success": False,
                                        "error": "Redirect target is not permitted.",
                                        "url": url,
                                    }
                                }
                            ],
                            "status": "error",
                        }
                    current_url = next_url
                    continue

                response.raise_for_status()
                break

            # Get content
            html_content = response.text
            content_type = response.headers.get("content-type", "")

            # Extract title
            title = "No title"
            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html_content, "html.parser")
                title_tag = soup.find("title")
                if title_tag:
                    title = title_tag.get_text().strip()
            except Exception:
                # Title extraction is best-effort; fall back to default
                pass

            # Extract text
            text_content = extract_text_from_html(html_content, max_length)

            # Build response
            result = {
                "success": True,
                "url": url,
                "title": title,
                "content_type": content_type,
                "text_content": text_content,
                "text_length": len(text_content),
                "status_code": response.status_code,
            }

            if include_html:
                result["html_content"] = html_content[:max_length]

            logger.info(f"Successfully fetched content from: {url} ({len(text_content)} chars)")

            return {"content": [{"json": result}], "status": "success"}

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP error {e.response.status_code}: {e.response.reason_phrase}"
        logger.error(f"HTTP error fetching {url}: {error_msg}")
        return {"content": [{"json": {"success": False, "error": error_msg, "url": url, "status_code": e.response.status_code}}], "status": "error"}

    except httpx.TimeoutException:
        error_msg = "Request timed out (30 seconds)"
        logger.error(f"Timeout fetching {url}")
        return {"content": [{"json": {"success": False, "error": error_msg, "url": url}}], "status": "error"}

    except Exception as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return {"content": [{"json": {"success": False, "error": str(e), "url": url}}], "status": "error"}
