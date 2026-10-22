"""Prompt resolution: composing one of a server's prompts (``prompts/get``).

The listings are a stored snapshot; this is a live call whose result depends on
arguments the user just typed. What is easy to get wrong is the flattening — an
MCP prompt message can carry an image, a resource link or an embedded blob, and
none of those survive into text a person can read. Dropping them silently would
be worse than saying so.

The MCP client is stubbed; the logic under test is the capping and flattening
around it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apis.app_api.tools.discovery import resolve_prompt_for_saved_tool
from apis.shared.tools.models import (
    MAX_RESOLVED_PROMPT_CHARS,
    MAX_RESOLVED_PROMPT_MESSAGES,
    MCPServerConfig,
    ToolDefinition,
    ToolProtocol,
    ToolStatus,
)


class _StubClient:
    """Stands in for strands' MCPClient as a context manager."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.called_with = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_prompt_sync(self, prompt_id, args):
        self.called_with = (prompt_id, args)
        if self._error:
            raise RuntimeError(self._error)
        return self._result


def _tool(protocol=ToolProtocol.MCP_EXTERNAL):
    return ToolDefinition(
        tool_id="canvas_faculty",
        display_name="Canvas Faculty",
        description="x",
        protocol=protocol,
        status=ToolStatus.ACTIVE,
        mcp_config=MCPServerConfig(server_url="https://example.com/mcp", tools=[]),
    )


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _result(*messages, description=None):
    return SimpleNamespace(description=description, messages=list(messages))


def _message(content, role="user"):
    return SimpleNamespace(role=role, content=content)


async def _resolve(client, arguments=None, tool=None):
    with patch(
        "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
        return_value=client,
    ):
        return await resolve_prompt_for_saved_tool(
            tool or _tool(), "grade_submission", arguments or {}
        )


@pytest.mark.asyncio
async def test_composes_text_messages_and_passes_arguments():
    client = _StubClient(
        _result(
            _message(_text("Grade BIO 101.")),
            _message(_text("Sure."), role="assistant"),
            description="Rubric-guided feedback",
        )
    )

    resolved = await _resolve(client, {"course": "BIO 101"})

    assert client.called_with == ("grade_submission", {"course": "BIO 101"})
    assert resolved.description == "Rubric-guided feedback"
    assert [(m.role, m.text) for m in resolved.messages] == [
        ("user", "Grade BIO 101."),
        ("assistant", "Sure."),
    ]
    assert resolved.truncated is False


@pytest.mark.asyncio
async def test_non_text_content_is_reported_by_kind_not_dropped_silently():
    """An image has no readable body, but the message must still be visible."""
    client = _StubClient(
        _result(
            _message(SimpleNamespace(type="image", data="…", mimeType="image/png")),
        )
    )

    resolved = await _resolve(client)

    assert len(resolved.messages) == 1
    assert resolved.messages[0].kind == "image"
    assert resolved.messages[0].text == ""


@pytest.mark.asyncio
async def test_embedded_text_resource_is_readable_but_a_blob_is_not():
    client = _StubClient(
        _result(
            _message(
                SimpleNamespace(
                    type="resource",
                    resource=SimpleNamespace(uri="file://a.md", text="# Rubric"),
                )
            ),
            _message(
                SimpleNamespace(
                    type="resource",
                    resource=SimpleNamespace(uri="file://b.pdf", text=None),
                )
            ),
        )
    )

    resolved = await _resolve(client)

    # A text resource flattens to readable text...
    assert (resolved.messages[0].kind, resolved.messages[0].text) == ("text", "# Rubric")
    # ...a binary one names itself and carries no payload.
    assert resolved.messages[1].kind == "resource"
    assert resolved.messages[1].text == "file://b.pdf"


@pytest.mark.asyncio
async def test_oversized_text_is_capped_and_flagged():
    client = _StubClient(_result(_message(_text("x" * (MAX_RESOLVED_PROMPT_CHARS + 500)))))

    resolved = await _resolve(client)

    assert len(resolved.messages[0].text) == MAX_RESOLVED_PROMPT_CHARS
    assert resolved.truncated is True


@pytest.mark.asyncio
async def test_too_many_messages_are_capped_and_flagged():
    client = _StubClient(
        _result(*[_message(_text("hi")) for _ in range(MAX_RESOLVED_PROMPT_MESSAGES + 5)])
    )

    resolved = await _resolve(client)

    assert len(resolved.messages) == MAX_RESOLVED_PROMPT_MESSAGES
    assert resolved.truncated is True


@pytest.mark.asyncio
async def test_server_failure_raises_for_the_route_to_turn_into_a_502():
    client = _StubClient(error="Unknown prompt: grade_submission")

    with pytest.raises(RuntimeError, match="could not compose"):
        await _resolve(client)


@pytest.mark.asyncio
async def test_a_gateway_tool_has_no_prompt_surface_to_ask():
    with pytest.raises(RuntimeError, match="not an external MCP server"):
        await resolve_prompt_for_saved_tool(_tool(ToolProtocol.MCP_GATEWAY), "p", {})
