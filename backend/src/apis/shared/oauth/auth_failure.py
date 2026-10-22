"""Shared "does this tool result look like an OAuth 401?" heuristic.

Two paths need the same answer about an MCP tool result:

  * ``OAuthConsentHook._handle_auth_failure`` — the model-driven tool
    loop, which clears the token cache and retries the call.
  * ``dispatch_app_tool_call`` — the app-initiated ``tools/call`` path,
    which clears the token cache but deliberately does NOT retry (an App
    call can be a mutation, so a heuristic-driven retry could apply a side
    effect twice).

Keeping the markers in one place means the two paths can never drift into
disagreeing about what an auth failure looks like.
"""

from __future__ import annotations

import re
from typing import Any

# Markers that indicate an OAuth-style auth failure in a tool result.
# A false positive triggers an unnecessary OAuth popup — far more
# disruptive than a missed match (which surfaces the underlying error to
# the user). So we err on the side of high-confidence signals only.
#
# Tiers:
#   1. HTTP 401 with negative lookarounds for path segments / adjacent
#      digits. Bare "401" in MCP error text is almost always an HTTP
#      status code in practice.
#   2. "Unauthorized" only when paired with an HTTP/status/code keyword.
#      The bare word fires on prose like "you are not authorized to view
#      this calendar" — which is application-level, not OAuth.
#   3. Unambiguous OAuth/token signals stand alone — `invalid_token`,
#      `invalid_grant` (refresh-token revocation), Google API's
#      `UNAUTHENTICATED` and `invalid authentication credentials`.
#
# We only run this on results whose `status == "error"`
# (see `looks_like_auth_failure`), so even the broader patterns above
# are gated by an explicit failure signal from the MCP framework.
AUTH_FAILURE_PATTERN = re.compile(
    r"(?<![\w/])401(?![\w/])"
    r"|\b(?:http|status|response|code)\b[^\n]{0,20}\bunauthoriz(?:ed|e)\b"
    r"|\bunauthoriz(?:ed|e)\b[^\n]{0,20}\b(?:http|status|response|code|401)\b"
    r"|\binvalid[_\s-]?token\b"
    r"|\bexpired[_\s-]?token\b"
    r"|\btoken[_\s-]?expired\b"
    r"|\brejected the oauth token\b"
    r"|\boauth token (?:has )?expired\b"
    r"|\binvalid[_\s-]?grant\b"
    r"|\binvalid[_\s-]?authentication[_\s-]?credentials\b"
    r"|\bUNAUTHENTICATED\b",
    re.IGNORECASE,
)


def looks_like_auth_failure(tool_result: Any) -> bool:
    """Heuristic: does this tool result look like an OAuth 401?

    Inspects the result's status and content for one of the markers above.
    False positives here just trigger a wasted retry; false negatives
    leave the user stuck with a stale token, so we err on the side of
    matching.
    """
    if not isinstance(tool_result, dict):
        return False
    if tool_result.get("status") != "error":
        return False
    for block in tool_result.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        if isinstance(text, str) and AUTH_FAILURE_PATTERN.search(text):
            return True
    return False
