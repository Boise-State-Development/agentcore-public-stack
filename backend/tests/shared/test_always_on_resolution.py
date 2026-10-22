"""Resolving the admin-pinned tool set (PR-2 of docs/specs/admin-always-on-tools.md).

Three properties, in descending order of how expensive they are to get wrong:

1. **Enables, never grants** — a pinned tool the caller's roles do not carry is
   dropped, and the predicate is the same one the picker and Agent bindings use.
2. **No new I/O per candidate** — the pinned set rides the catalog snapshot that
   `freshness` already reads, so resolving it costs no DynamoDB round trip (§4).
3. **Deterministic order** — the appended segment reaches the Bedrock
   `toolConfig`, so a hash-ordered set anywhere here would silently re-write the
   cacheable prefix between turns (§5).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.shared.tools import freshness
from apis.shared.tools.always_on import resolve_always_on_tool_ids, union_enabled_tools
from apis.shared.feature_flags import admin_always_on_tools_enabled

USER = SimpleNamespace(user_id="u1", roles=["staff"])


@pytest.fixture(autouse=True)
def _clean_snapshots():
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def _tool(tool_id, *, always_on=False, is_public=False, entries=()):
    """A catalog row shaped like the fields the snapshot reads."""
    cfg = SimpleNamespace(tools=[
        SimpleNamespace(name=name, always_on=flag) for name, flag in entries
    ]) if entries else None
    return SimpleNamespace(
        tool_id=tool_id,
        always_on=always_on,
        is_public=is_public,
        mcp_config=cfg,
        mcp_gateway_config=None,
    )


def _repo(tools):
    repo = SimpleNamespace()
    repo.list_tools = AsyncMock(return_value=tools)
    return repo


def _role_service(allowed):
    """Mirrors `filter_requested_tools`: narrow, never grant, scoped->base."""
    svc = SimpleNamespace()

    async def _filter(user, requested):
        if "*" in allowed:
            return list(requested)
        return [t for t in requested if t in allowed or t.split("::")[0] in allowed]

    svc.filter_requested_tools = AsyncMock(side_effect=_filter)
    return svc


class TestFlag:
    def test_defaults_on(self, monkeypatch):
        monkeypatch.delenv("ADMIN_ALWAYS_ON_TOOLS_ENABLED", raising=False)
        assert admin_always_on_tools_enabled()

    def test_empty_string_is_on(self, monkeypatch):
        monkeypatch.setenv("ADMIN_ALWAYS_ON_TOOLS_ENABLED", "")
        assert admin_always_on_tools_enabled()

    def test_only_literal_false_disables(self, monkeypatch):
        monkeypatch.setenv("ADMIN_ALWAYS_ON_TOOLS_ENABLED", "FALSE")
        assert not admin_always_on_tools_enabled()
        monkeypatch.setenv("ADMIN_ALWAYS_ON_TOOLS_ENABLED", "0")
        assert admin_always_on_tools_enabled()

    @pytest.mark.asyncio
    async def test_kill_switch_pins_nothing(self, monkeypatch):
        monkeypatch.setenv("ADMIN_ALWAYS_ON_TOOLS_ENABLED", "false")
        with patch.object(
            freshness, "get_tool_catalog_repository", create=True
        ):
            assert await resolve_always_on_tool_ids(USER) == []


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_collects_bare_and_scoped_ids(self):
        tools = [
            _tool("search_web", always_on=True),
            _tool("browse_web"),
            _tool("weather", entries=[("get_forecast", True), ("get_history", False)]),
        ]
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo(tools),
        ):
            assert await freshness.get_always_on_tool_ids() == frozenset(
                {"search_web", "weather::get_forecast"}
            )

    @pytest.mark.asyncio
    async def test_rides_the_existing_catalog_read(self):
        """§4.1 — the whole latency argument. One read fills all three slots."""
        repo = _repo([_tool("search_web", always_on=True, is_public=True)])
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            await freshness.get_all_tool_ids()
            await freshness.get_public_tool_ids()
            await freshness.get_always_on_tool_ids()

        # Not "few" reads — exactly one. A second read here would mean the
        # always-on snapshot had its own cache, which is the design this
        # rejects.
        assert repo.list_tools.await_count == 1

    @pytest.mark.asyncio
    async def test_admin_write_invalidates_it(self):
        repo = _repo([_tool("search_web", always_on=True)])
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            assert await freshness.get_always_on_tool_ids() == frozenset({"search_web"})
            freshness.invalidate("search_web")
            repo.list_tools.return_value = [_tool("search_web")]
            assert await freshness.get_always_on_tool_ids() == frozenset()

    @pytest.mark.asyncio
    async def test_a_catalog_failure_pins_nothing_rather_than_raising(self):
        repo = SimpleNamespace(list_tools=AsyncMock(side_effect=RuntimeError("boom")))
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            assert await freshness.get_always_on_tool_ids() == frozenset()


class TestEnablesNeverGrants:
    @pytest.mark.asyncio
    async def test_drops_a_pinned_tool_the_caller_cannot_access(self):
        tools = [_tool("search_web", always_on=True), _tool("payroll", always_on=True)]
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo(tools),
        ), patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=_role_service({"search_web"}),
        ):
            assert await resolve_always_on_tool_ids(USER) == ["search_web"]

    @pytest.mark.asyncio
    async def test_a_wildcard_grant_admits_everything(self):
        tools = [_tool("search_web", always_on=True), _tool("payroll", always_on=True)]
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo(tools),
        ), patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=_role_service({"*"}),
        ):
            assert await resolve_always_on_tool_ids(USER) == ["payroll", "search_web"]

    @pytest.mark.asyncio
    async def test_a_scoped_id_rides_its_base_server_grant(self):
        tools = [_tool("weather", entries=[("get_forecast", True)])]
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo(tools),
        ), patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=_role_service({"weather"}),
        ):
            # Carried VERBATIM — collapsing it to `weather` would restore the
            # whole server, which is the bug scoping exists to prevent.
            assert await resolve_always_on_tool_ids(USER) == ["weather::get_forecast"]

    @pytest.mark.asyncio
    async def test_an_rbac_failure_pins_nothing_rather_than_raising(self):
        svc = SimpleNamespace(
            filter_requested_tools=AsyncMock(side_effect=RuntimeError("boom"))
        )
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo([_tool("search_web", always_on=True)]),
        ), patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
            assert await resolve_always_on_tool_ids(USER) == []


class TestDeterministicOrder:
    @pytest.mark.asyncio
    async def test_order_is_sorted_not_set_order(self):
        """§5 — this list reaches `toolConfig`, the cacheable prefix.

        The snapshot is a frozenset, whose iteration order varies with hash
        randomisation across processes. Sorting at the source is what keeps the
        appended segment byte-stable between turns.
        """
        names = ["zeta", "alpha", "mid", "beta"]
        tools = [_tool(n, always_on=True) for n in names]
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo(tools),
        ), patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=_role_service({"*"}),
        ):
            assert await resolve_always_on_tool_ids(USER) == sorted(names)

    @pytest.mark.asyncio
    async def test_nothing_pinned_returns_an_empty_list(self):
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=_repo([_tool("search_web")]),
        ):
            assert await resolve_always_on_tool_ids(USER) == []


class TestUnionHelperIsShared:
    """`union_enabled_tools` now lives in shared so the `/invocations` path and
    the voice WebSocket cannot drift apart (spec §1: a single seam).

    The identity behaviour is the part that would drift silently, so it is
    asserted here at its new home as well as at the call sites.
    """

    def test_routes_alias_is_the_shared_implementation(self):
        from apis.inference_api.chat.routes import _with_auto_enabled_tools

        original = ["a"]
        assert _with_auto_enabled_tools(original, []) is original
        assert _with_auto_enabled_tools(original, ["b"]) == ["a", "b"]

    def test_voice_imports_the_same_helper(self):
        from apis.inference_api.chat import voice_routes

        assert voice_routes.union_enabled_tools is union_enabled_tools

    def test_none_stays_none(self):
        assert union_enabled_tools(None, []) is None

    def test_returns_the_same_object_when_nothing_to_add(self):
        original = ["a", "b"]
        assert union_enabled_tools(original, []) is original
        assert union_enabled_tools(original, ["a"]) is original

    def test_appends_only_what_is_missing_in_the_order_given(self):
        assert union_enabled_tools(["a"], ["b", "a", "c"]) == ["a", "b", "c"]

    def test_builds_from_none_when_there_is_something_to_add(self):
        assert union_enabled_tools(None, ["a"]) == ["a"]
