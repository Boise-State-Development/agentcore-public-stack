"""Tests for the MCP discovery error-translation helpers.

The `/admin/tools/discover` route connects to an arbitrary MCP server and lists
its tools. When the target rejects the request, Strands buries the real HTTP
status inside an `MCPClientInitializationError` → `ExceptionGroup` chain; these
helpers recover it and map it to an actionable response (instead of a blanket
502). See `apis/app_api/admin/tools/routes.py`.
"""

import httpx
import pytest

from apis.app_api.admin.tools.routes import (
    _discovery_failure_detail,
    _find_upstream_http_error,
    _response_status_for_upstream,
)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://x.lambda-url.us-west-2.on.aws/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_find_upstream_http_error_unwraps_exception_group():
    """The httpx error is recovered from a nested ExceptionGroup + cause chain,
    mirroring how Strands' MCPClient wraps a target's HTTP rejection."""
    http_err = _http_status_error(403)
    group = ExceptionGroup("unhandled errors in a TaskGroup", [http_err])

    class MCPClientInitializationError(Exception):
        pass

    try:
        raise MCPClientInitializationError("the client initialization failed") from group
    except MCPClientInitializationError as exc:
        found = _find_upstream_http_error(exc)

    assert found is http_err
    assert found.response.status_code == 403


def test_find_upstream_http_error_returns_none_without_http_error():
    """A non-HTTP failure (e.g. a connection error) yields None so the caller
    falls back to a generic 502."""
    try:
        raise TimeoutError("connect timed out")
    except TimeoutError as exc:
        assert _find_upstream_http_error(exc) is None


@pytest.mark.parametrize(
    "upstream,expected",
    [
        (403, 403),  # echoed — actionable config error
        (404, 404),  # echoed — wrong path
        (401, 400),  # remapped — a bare 401 would trip the SPA's logout/redirect
        (500, 502),  # upstream server error — we are a failing gateway
        (503, 502),
    ],
)
def test_response_status_for_upstream(upstream, expected):
    assert _response_status_for_upstream(upstream) == expected


def test_discovery_failure_detail_mentions_credential_for_403():
    detail = _discovery_failure_detail(403)
    assert "403" in detail
    assert "Gateway IAM Role" in detail


def test_discovery_failure_detail_mentions_oauth_for_401():
    detail = _discovery_failure_detail(401)
    assert "401" in detail
    assert "OAuth" in detail
