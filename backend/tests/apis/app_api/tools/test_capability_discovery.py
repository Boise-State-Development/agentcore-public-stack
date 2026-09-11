"""Capability discovery: prompts + resources from a saved MCP server.

The stack only ever called ``tools/list``. These cover the part that is easy to
get wrong — a server that implements *some* of the three listings. Answering
"method not found" to ``prompts/list`` is normal and must not cost us the
resources listing, or the snapshot.

The MCP client is stubbed; the logic under test is the guarding, pagination and
capping around it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apis.app_api.tools.discovery import discover_capabilities_for_saved_tool
from apis.shared.tools.models import (
    MAX_CAPABILITY_ENTRIES,
    MCPServerConfig,
    ToolDefinition,
    ToolProtocol,
    ToolStatus,
)


class _StubClient:
    """Stands in for strands' MCPClient as a context manager."""

    def __init__(self, prompts=None, resources=None, templates=None):
        self._prompts = prompts
        self._resources = resources
        self._templates = templates

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self, payload, kind):
        if payload is None:
            # What a server that doesn't implement the method actually does.
            raise RuntimeError(f"Method not found: {kind}")
        return payload

    def list_prompts_sync(self, pagination_token=None):
        return self._answer(self._prompts, "prompts/list")

    def list_resources_sync(self, pagination_token=None):
        return self._answer(self._resources, "resources/list")

    def list_resource_templates_sync(self, pagination_token=None):
        return self._answer(self._templates, "resources/templates/list")


def _tool(tool_id="canvas_faculty", protocol=ToolProtocol.MCP_EXTERNAL):
    return ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id,
        description="x",
        protocol=protocol,
        status=ToolStatus.ACTIVE,
        mcp_config=MCPServerConfig(server_url="https://example.com/mcp", tools=[]),
    )


def _prompts(*names):
    return SimpleNamespace(
        prompts=[
            SimpleNamespace(
                name=n,
                title=None,
                description=f"desc for {n}",
                arguments=[SimpleNamespace(name="course_id")],
            )
            for n in names
        ],
        nextCursor=None,
    )


def _resources(*uris):
    return SimpleNamespace(
        resources=[
            SimpleNamespace(uri=u, name=None, description=None, mimeType="text/plain")
            for u in uris
        ],
        nextCursor=None,
    )


def _templates(*uris):
    return SimpleNamespace(
        resourceTemplates=[
            SimpleNamespace(
                uriTemplate=u, name=None, description=None, mimeType=None
            )
            for u in uris
        ],
        nextCursor=None,
    )


async def _run(tool, client):
    """Patch at the source module: discovery imports it inside the function."""
    with patch(
        "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
        return_value=client,
    ):
        return await discover_capabilities_for_saved_tool(tool)


@pytest.mark.asyncio
async def test_collects_prompts_and_resources():
    snapshot = await _run(
        _tool(),
        _StubClient(
            prompts=_prompts("grade_summary"),
            resources=_resources("canvas://a"),
            templates=_templates("canvas://courses/{id}/syllabus"),
        ),
    )
    assert snapshot.supports_prompts is True
    assert snapshot.supports_resources is True
    assert [p.name for p in snapshot.prompts] == ["grade_summary"]
    assert snapshot.prompts[0].arguments == ["course_id"]
    assert {r.uri for r in snapshot.resources} == {
        "canvas://a",
        "canvas://courses/{id}/syllabus",
    }
    # A template is not a readable URI — the UI has to say so.
    assert [r.uri_template for r in snapshot.resources if "{id}" in r.uri] == [True]
    assert snapshot.error is None


@pytest.mark.asyncio
async def test_a_server_without_prompts_still_yields_resources():
    # The regression this guards: one unsupported listing taking the whole
    # snapshot down with it.
    snapshot = await _run(
        _tool(),
        _StubClient(prompts=None, resources=_resources("x://1"), templates=None),
    )
    assert snapshot.supports_prompts is False
    assert snapshot.prompts == []
    assert snapshot.supports_resources is True
    assert len(snapshot.resources) == 1
    assert snapshot.error is None


@pytest.mark.asyncio
async def test_a_server_with_neither_is_not_an_error():
    snapshot = await _run(_tool(), _StubClient())
    assert snapshot.supports_prompts is False
    assert snapshot.supports_resources is False
    # "Offers nothing" and "we couldn't ask" are different facts.
    assert snapshot.error is None


@pytest.mark.asyncio
async def test_unreachable_server_records_an_error():
    class _Boom(_StubClient):
        def __enter__(self):
            raise RuntimeError("connection refused")

    snapshot = await _run(_tool(), _Boom())
    assert snapshot.error is not None
    assert "connection refused" in snapshot.error


@pytest.mark.asyncio
async def test_gateway_tools_are_not_probed():
    # A Gateway target exposes no prompt or resource surface to ask.
    snapshot = await _run(_tool(protocol=ToolProtocol.MCP_GATEWAY), _StubClient(prompts=_prompts("p")))
    assert snapshot.prompts == []
    assert snapshot.error is not None


@pytest.mark.asyncio
async def test_entries_are_capped_so_one_server_cannot_blow_the_item():
    many = _resources(*[f"x://{i}" for i in range(MAX_CAPABILITY_ENTRIES + 50)])
    snapshot = await _run(_tool(), _StubClient(resources=many))
    assert len(snapshot.resources) == MAX_CAPABILITY_ENTRIES
    assert snapshot.truncated is True


@pytest.mark.asyncio
async def test_pagination_follows_the_cursor():
    pages = [
        SimpleNamespace(
            resources=[
                SimpleNamespace(
                    uri="x://1", name=None, description=None, mimeType=None
                )
            ],
            nextCursor="c1",
        ),
        SimpleNamespace(
            resources=[
                SimpleNamespace(
                    uri="x://2", name=None, description=None, mimeType=None
                )
            ],
            nextCursor=None,
        ),
    ]

    class _Paged(_StubClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def list_resources_sync(self, pagination_token=None):
            page = pages[self.calls]
            self.calls += 1
            return page

        def list_prompts_sync(self, pagination_token=None):
            raise RuntimeError("no prompts")

        def list_resource_templates_sync(self, pagination_token=None):
            raise RuntimeError("no templates")

    snapshot = await _run(_tool(), _Paged())
    assert {r.uri for r in snapshot.resources} == {"x://1", "x://2"}


@pytest.mark.asyncio
async def test_a_broken_cursor_cannot_loop_forever():
    class _Endless(_StubClient):
        def list_resources_sync(self, pagination_token=None):
            return SimpleNamespace(
                resources=[
                    SimpleNamespace(
                        uri="x://same", name=None, description=None, mimeType=None
                    )
                ],
                nextCursor="always",
            )

        def list_prompts_sync(self, pagination_token=None):
            raise RuntimeError("no prompts")

        def list_resource_templates_sync(self, pagination_token=None):
            raise RuntimeError("no templates")

    snapshot = await _run(_tool(), _Endless())
    assert snapshot.truncated is True


@pytest.mark.asyncio
async def test_long_descriptions_are_clipped():
    long_desc = "x" * 5000
    payload = SimpleNamespace(
        prompts=[
            SimpleNamespace(
                name="p", title=None, description=long_desc, arguments=[]
            )
        ],
        nextCursor=None,
    )
    snapshot = await _run(_tool(), _StubClient(prompts=payload))
    assert len(snapshot.prompts[0].description) < 600
