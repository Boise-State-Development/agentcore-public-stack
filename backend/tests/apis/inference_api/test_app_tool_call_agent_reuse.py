"""An App's ``tools/call`` reuses the conversation's live agent instead of building one.

``_agent_for_app_dispatch`` cannot rebuild the turn's cache key (it has no
memory context, injected tools or memory binding), so every App press used
to miss and pay a full agent build plus a memory restore. These pin the
reuse, that it never crosses users on one session id, and that a
conversation with no usable live agent still builds, with ``cache_write``
off.
"""

from types import SimpleNamespace
from typing import Any, Sequence
from unittest.mock import AsyncMock, patch

import pytest
from strands.tools.registry import ToolRegistry
from strands.tools.tool_provider import ToolProvider

from agents.main_agent.integrations import mcp_apps as mcp_apps_mod
from apis.inference_api.chat import routes
from apis.inference_api.chat import service
from apis.inference_api.chat.app_tool_dispatch import agent_carries_tool_client
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.tools.models import ToolUIMetadata

TOOL = "issue_write"


class _Provider(ToolProvider):
    """Stands in for an MCPClient: Strands registers it as a tool provider."""

    async def load_tools(self, **_: Any) -> Sequence[Any]:
        return []

    def add_consumer(self, consumer_id: Any, **_: Any) -> None:
        pass

    def remove_consumer(self, consumer_id: Any, **_: Any) -> None:
        pass


class _Catalog:
    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, _name: str) -> ToolUIMetadata:
        return ToolUIMetadata(resource_uri="ui://srv/w", visibility=["model", "app"])

    def get_client(self, _name: str) -> Any:
        return self._client


def _agent_over(*providers: Any) -> SimpleNamespace:
    """A BaseAgent-shaped object whose Strands registry holds `providers`."""
    registry = ToolRegistry()
    registry.process_tools(list(providers))
    return SimpleNamespace(agent=SimpleNamespace(tool_registry=registry, messages=[]))


def _key(session_id: str, user_id: str, variant: str) -> tuple:
    return (*service._conversation_owner_key(session_id, user_id), variant)


@pytest.fixture
def catalog_client(monkeypatch):
    client = _Provider()
    monkeypatch.setattr(mcp_apps_mod, "is_mcp_apps_host_enabled", lambda: True)
    monkeypatch.setattr(mcp_apps_mod, "get_ui_tool_catalog", lambda: _Catalog(client))
    monkeypatch.setattr(service, "_agent_cache", {})
    return client


def _request(session_id: str = "s-1") -> InvocationRequest:
    return InvocationRequest(
        session_id=session_id,
        app_tool_call={"tool_use_id": "tu-1", "tool_name": TOOL, "arguments": {}},
    )


async def _select(request: InvocationRequest, user_id: str, built: Any = None):
    """Run the route's agent choice with the build stubbed out."""
    with patch.object(routes, "get_agent", new=AsyncMock(return_value=built)) as get_agent, \
         patch.object(routes, "_agent_for_app_dispatch", new=AsyncMock(return_value=built)) as build:
        agent = await routes._agent_for_app_tool_call(
            request,
            SimpleNamespace(user_id=user_id),
            user_id,
            None,
            agent_type="chat",
            accessible_skill_ids=None,
        )
    return agent, get_agent, build


@pytest.mark.asyncio
async def test_a_press_reuses_the_conversations_live_agent(catalog_client):
    live = _agent_over(catalog_client)
    service._agent_cache[_key("s-1", "u-1", "turn")] = live

    agent, get_agent, build = await _select(_request(), "u-1")

    assert agent is live
    get_agent.assert_not_awaited()
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_newest_agent_carrying_the_client_wins(catalog_client):
    older = _agent_over(catalog_client)
    newer = _agent_over(catalog_client)
    mention = _agent_over(_Provider())  # newest, but built without this server
    service._agent_cache[_key("s-1", "u-1", "a")] = older
    service._agent_cache[_key("s-1", "u-1", "b")] = newer
    service._agent_cache[_key("s-1", "u-1", "mention")] = mention

    agent, _, build = await _select(_request(), "u-1")

    assert agent is newer
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_reuse_never_crosses_users_on_one_session_id(catalog_client):
    """The owner's agent carries the client; the other user's request must not get it."""
    owners = _agent_over(catalog_client)
    service._agent_cache[_key("s-1", "owner", "turn")] = owners
    built = object()

    agent, _, build = await _select(_request(), "intruder", built=built)

    assert agent is built
    assert agent is not owners
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_live_agent_still_builds(catalog_client):
    built = object()

    agent, _, build = await _select(_request(), "u-1", built=built)

    assert agent is built
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_live_agent_without_the_tools_client_still_builds(catalog_client):
    service._agent_cache[_key("s-1", "u-1", "turn")] = _agent_over(_Provider())
    built = object()

    agent, _, build = await _select(_request(), "u-1", built=built)

    assert agent is built
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_tool_the_catalog_does_not_know_still_builds(monkeypatch):
    """No catalog client: build, so the dispatch's 403 / visibility gate decides."""
    monkeypatch.setattr(mcp_apps_mod, "is_mcp_apps_host_enabled", lambda: True)
    monkeypatch.setattr(mcp_apps_mod, "get_ui_tool_catalog", lambda: _Catalog(None))
    monkeypatch.setattr(service, "_agent_cache", {})
    service._agent_cache[_key("s-1", "u-1", "turn")] = _agent_over(_Provider())
    built = object()

    agent, _, build = await _select(_request(), "u-1", built=built)

    assert agent is built
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_fallback_build_never_seeds_a_cache_slot(catalog_client):
    async def passthrough(tools, *_args, **_kwargs):
        return tools

    with patch.object(routes, "_resolve_fallback_model", new=AsyncMock(return_value=("m", "bedrock"))), \
         patch.object(routes, "_resolve_model_settings", new=AsyncMock(return_value=(False, {}, None, None, "bedrock", None))), \
         patch.object(routes, "_apply_attachment_tool_autoenable", new=passthrough), \
         patch.object(routes, "_apply_admin_always_on_tools", new=passthrough), \
         patch.object(routes, "_plain_turn_prompt", new=AsyncMock(return_value="prompt")), \
         patch.object(routes, "get_agent", new=AsyncMock(return_value=object())) as get_agent:
        await routes._agent_for_app_tool_call(
            _request(),
            SimpleNamespace(user_id="u-1"),
            "u-1",
            None,
            agent_type="chat",
            accessible_skill_ids=None,
        )

    get_agent.assert_awaited_once()
    assert get_agent.await_args.kwargs["cache_write"] is False


def test_an_app_only_tools_client_counts_as_carried(catalog_client):
    """An App-only tool never reaches the registry's tools; its client is still a provider."""
    agent = _agent_over(catalog_client)
    assert agent.agent.tool_registry.registry == {}
    assert agent_carries_tool_client(agent, TOOL)


def test_nothing_is_carried_while_the_host_flag_is_off(catalog_client, monkeypatch):
    monkeypatch.setattr(mcp_apps_mod, "is_mcp_apps_host_enabled", lambda: False)
    assert not agent_carries_tool_client(_agent_over(catalog_client), TOOL)


def test_live_conversation_agents_is_newest_first_and_owner_scoped(monkeypatch):
    monkeypatch.setattr(service, "_agent_cache", {})
    a, b, other = object(), object(), object()
    service._agent_cache[_key("s-1", "u-1", "a")] = a
    service._agent_cache[_key("s-1", "u-2", "x")] = other
    service._agent_cache[_key("s-1", "u-1", "b")] = b

    assert service.live_conversation_agents("s-1", "u-1") == [b, a]
    assert service.live_conversation_agents("s-1", "u-2") == [other]
    assert service.live_conversation_agents("s-2", "u-1") == []
