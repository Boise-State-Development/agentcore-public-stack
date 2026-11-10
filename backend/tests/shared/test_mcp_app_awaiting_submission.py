"""Tests for recognizing an MCP App form left open for the user.

GitHub's write tools return ``isError: true`` on purpose while their App's
form waits for a Submit click. The marker that tells a host so
(``structuredContent.status == "awaiting_user_submission"``) is only sent on
MCP 2026-07-28; on our protocol the stub is a bare error with a fixed opening
sentence. These pin both shapes and the guard that keeps the text match off
everything except App tools.
"""

from __future__ import annotations

import mcp.types as mcp_types

from apis.shared.mcp_apps.awaiting_submission import (
    AWAITING_USER_SUBMISSION,
    awaiting_user_submission,
    has_awaiting_marker,
    mark_awaiting_user_submission,
)

# The create stub as dev received it from api.githubcopilot.com on
# 2026-10-18 (session 80079483…): no structuredContent at all.
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


class TestAwaitingUserSubmission:
    def test_the_marker_counts_whatever_the_tool(self) -> None:
        result = {
            "status": "error",
            "content": [{"text": "anything"}],
            "structuredContent": {"status": AWAITING_USER_SUBMISSION, "reason": "r"},
        }
        assert awaiting_user_submission(result, is_app_tool=False)
        assert awaiting_user_submission(result, is_app_tool=True)

    def test_githubs_stub_without_the_marker_counts_for_an_app_tool(self) -> None:
        assert awaiting_user_submission(_stub(), is_app_tool=True)

    def test_the_stub_text_never_counts_for_a_tool_without_an_app(self) -> None:
        assert not awaiting_user_submission(_stub(), is_app_tool=False)

    def test_the_stub_text_on_a_successful_result_is_not_awaiting(self) -> None:
        result = _stub(status="success")
        result.pop("isError")
        assert not awaiting_user_submission(result, is_app_tool=True)

    def test_an_ordinary_app_failure_is_still_a_failure(self) -> None:
        result = _stub(content=[{"text": "Connection to the MCP server was closed"}])
        assert not awaiting_user_submission(result, is_app_tool=True)

    def test_the_sentence_must_open_the_result(self) -> None:
        result = _stub(
            content=[{"text": "Error: An interactive form has been shown to the user"}]
        )
        assert not awaiting_user_submission(result, is_app_tool=True)

    def test_malformed_results_are_not_awaiting(self) -> None:
        for result in (None, "text", {}, {"status": "error", "content": []}, {"content": "x"}):
            assert not awaiting_user_submission(result, is_app_tool=True)
        assert not has_awaiting_marker({"structuredContent": "awaiting_user_submission"})


class TestMarkAwaitingUserSubmission:
    def test_marks_the_stub_in_place_and_leaves_the_error_for_the_model(self) -> None:
        result = _stub()

        assert mark_awaiting_user_submission(result, is_app_tool=True)

        assert result["structuredContent"] == {"status": AWAITING_USER_SUBMISSION}
        # The model must still read the call as not done.
        assert result["status"] == "error"
        assert result["isError"] is True
        assert result["content"] == _GITHUB_CREATE_STUB["content"]

    def test_leaves_a_servers_own_marker_untouched(self) -> None:
        structured = {"status": AWAITING_USER_SUBMISSION, "reason": "from the server"}
        result = _stub(structuredContent=structured)

        assert mark_awaiting_user_submission(result, is_app_tool=True)
        assert result["structuredContent"] is structured

    def test_does_not_touch_a_failure(self) -> None:
        result = _stub(content=[{"text": "503 Service Unavailable"}])

        assert not mark_awaiting_user_submission(result, is_app_tool=True)
        assert "structuredContent" not in result

    def test_does_not_touch_a_tool_without_an_app(self) -> None:
        result = _stub()

        assert not mark_awaiting_user_submission(result, is_app_tool=False)
        assert "structuredContent" not in result


def test_a_2026_07_28_server_sends_the_marker_itself():
    """Real wire: on 2026-07-28 GitHub attaches the marker to its stub
    (`TestTypedIssueWriteAwaitingForm`), and it reaches the Strands result
    whole, so no text match is needed and nothing overwrites it."""
    import mcp.types as mcp_types
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
    assert awaiting_user_submission(result, is_app_tool=False)
    assert mark_awaiting_user_submission(result, is_app_tool=True)
    assert result["structuredContent"] == marker
