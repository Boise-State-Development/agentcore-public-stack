"""Tests for recognizing an MCP App form left open for the user.

GitHub's write tools return ``isError: true`` on purpose while their App's
form waits for a Submit click, and on MCP 2026-07-28 mark it with
``structuredContent.status == "awaiting_user_submission"``. The marker is the
only signal: the text match on GitHub's stub sentence that covered MCP 1.x
(PR #1662) is retired, so a bare stub now reads as a failure.
"""

from __future__ import annotations

# Module level on purpose: the real-wire test's tool annotation is a string
# (future annotations) that the MCP server resolves against these globals.
import mcp.types as mcp_types

from apis.shared.mcp_apps.awaiting_submission import (
    AWAITING_USER_SUBMISSION,
    has_awaiting_marker,
)

# The create stub as dev received it from api.githubcopilot.com on
# 2026-10-18 (session 80079483…), under MCP 1.x: no structuredContent at all.
_GITHUB_CREATE_STUB = {
    "status": "error",
    "toolUseId": "tooluse_1",
    "content": [
        {
            "text": (
                "An interactive form has been shown to the user for creating a new "
                "issue in philmerrell/mcp-app-scratch. STOP — do not call any other "
                "tools, do not respond as if the issue was created, and do not claim "
                "the operation succeeded. The issue has NOT been created yet; only "
                "the form was rendered."
            )
        }
    ],
    "isError": True,
}


def _stub(**overrides):
    result = {**_GITHUB_CREATE_STUB, "content": list(_GITHUB_CREATE_STUB["content"])}
    result.update(overrides)
    return result


class TestHasAwaitingMarker:
    def test_the_marker_counts_whatever_the_text(self) -> None:
        result = {
            "status": "error",
            "content": [{"text": "anything"}],
            "structuredContent": {"status": AWAITING_USER_SUBMISSION, "reason": "r"},
        }
        assert has_awaiting_marker(result)

    def test_githubs_stub_with_the_marker_counts(self) -> None:
        assert has_awaiting_marker(
            _stub(structuredContent={"status": AWAITING_USER_SUBMISSION})
        )

    def test_githubs_bare_stub_without_the_marker_is_not_awaiting(self) -> None:
        """The #1662 text fallback is retired: a pre-2026-07-28 server's stub
        reads as the error it says it is."""
        assert not has_awaiting_marker(_stub())

    def test_an_ordinary_app_failure_is_still_a_failure(self) -> None:
        result = _stub(content=[{"text": "Connection to the MCP server was closed"}])
        assert not has_awaiting_marker(result)

    def test_another_structured_status_is_not_awaiting(self) -> None:
        assert not has_awaiting_marker(_stub(structuredContent={"status": "done"}))

    def test_malformed_results_are_not_awaiting(self) -> None:
        for result in (
            None,
            "text",
            {},
            {"status": "error", "content": []},
            {"content": "x"},
            {"structuredContent": "awaiting_user_submission"},
            {"structuredContent": None},
        ):
            assert not has_awaiting_marker(result)


def test_a_2026_07_28_server_sends_the_marker_itself():
    """Real wire: on 2026-07-28 GitHub attaches the marker to its stub
    (`TestTypedIssueWriteAwaitingForm`), and it reaches the Strands result
    whole, so nothing on our side has to write it."""
    from strands.tools.mcp import MCPClient

    from agents.main_agent.integrations.mcp_transport import streamable_http_transport
    from tests.fixtures.flaky_mcp_server import FlakyMCPServer

    marker = {"status": AWAITING_USER_SUBMISSION, "tool": "issue_write"}

    def setup(server):
        @server.tool()
        def form_write(title: str) -> mcp_types.CallToolResult:
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=_GITHUB_CREATE_STUB["content"][0]["text"])],
                isError=True,
                structuredContent=marker,
            )

    with FlakyMCPServer(fail_calls=False, setup=setup) as server:
        client = MCPClient(lambda: streamable_http_transport(server.url))
        client.start()
        try:
            result = client.call_tool_sync("t-1", "form_write", {"title": "x"})
        finally:
            client.stop(None, None, None)

    assert result["status"] == "error"
    assert result["structuredContent"] == marker
    assert has_awaiting_marker(result)
