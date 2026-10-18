"""The always-on union at the invocation seam (PR-2, §7 D4).

`_apply_admin_always_on_tools` is where the pinned set meets the turn. The
load-bearing behaviour here is D4: the exemption is "the Agent binds its own
toolset", NOT "the turn ran an Agent". Getting that backwards in either
direction is a bug — one overrides an author's explicit scoping, the other
turns always-on into an opt-out button.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import routes

USER = SimpleNamespace(user_id="u1", roles=["staff"])


def _resolver(ids):
    return AsyncMock(return_value=list(ids))


@pytest.fixture(autouse=True)
def _no_system_tools_by_default():
    """`_apply_admin_always_on_tools` now also resolves the platform 'system'
    tool set. Default it to empty so the existing always-on tests stay
    hermetic and never reach real AWS; tests that care about system tools
    re-patch it locally.
    """
    with patch.object(routes, "resolve_system_tool_ids", _resolver([])):
        yield


class TestAgentBindingExemption:
    @pytest.mark.asyncio
    async def test_an_agent_that_binds_tools_is_left_alone(self):
        """The Agent owns its toolset, like modelConfig owns the model.

        Admin-pinned always-on is exempted for a bound Agent; system tools are
        NOT (see TestSystemTools), but with no system tools configured the
        result is the untouched bound list.
        """
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(["pinned"])
        ) as resolver, patch.object(
            routes, "resolve_system_tool_ids", _resolver([])
        ):
            bound = ["agent_tool_a", "agent_tool_b"]
            result = await routes._apply_admin_always_on_tools(
                bound, USER, agent_bound_tools=True
            )
        assert result is bound
        # Not merely unchanged — never even resolved. A bound turn should not
        # pay for the admin-pinned set it cannot use.
        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_agent_with_no_tool_bindings_still_gets_the_pinned_set(self):
        """§7 D4 — the anti-bypass case, and the COMMON one.

        An Agent with no tool bindings resolves to an empty toolset with
        `owns_toolset` False (so `agent_bound_tools` is False), and
        template-derived Agents ship with empty `bindings` by design.
        Exempting them would let anyone shed a pinned tool with a trivial
        unbound Agent.
        """
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(["pinned"])
        ), patch.object(routes, "resolve_system_tool_ids", _resolver([])):
            result = await routes._apply_admin_always_on_tools(
                [], USER, agent_bound_tools=False
            )
        assert result == ["pinned"]

    @pytest.mark.asyncio
    async def test_the_default_chat_turn_gets_the_pinned_set(self):
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(["pinned"])
        ), patch.object(routes, "resolve_system_tool_ids", _resolver([])):
            result = await routes._apply_admin_always_on_tools(["picker_tool"], USER)
        assert result == ["picker_tool", "pinned"]


class TestEffectiveToolsByTurnShape:
    """`_resolve_effective_tools` for each kind of turn. The viewer's picker is
    `picker_tool`; whether it survives is what separates the shapes.

    An Agent that binds no tools ignores the picker: an author who selected no
    tools must not get an Agent whose tools change with whoever is chatting.
    It still expressed no intent about tools, so the pinned floor applies
    (D4). Exempting it would make creating an Agent the one-click bypass."""

    @staticmethod
    async def _effective(override, *, enabled_tools=("picker_tool",), system=(), pinned=("pinned",)):
        input_data = SimpleNamespace(
            enabled_tools=list(enabled_tools), session_id="s1", rag_assistant_id="ast-1"
        )
        autoenable = AsyncMock(side_effect=lambda tools, *args, **kwargs: tools)
        with patch.object(
            routes, "_apply_attachment_tool_autoenable", autoenable
        ), patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(pinned)
        ) as always_on, patch.object(
            routes, "resolve_system_tool_ids", _resolver(system)
        ):
            result = await routes._resolve_effective_tools(
                input_data, USER, "u1", override, turn_has_tabular=False
            )
        return result, always_on, autoenable

    @pytest.mark.asyncio
    async def test_an_unbound_agent_ignores_the_picker_and_keeps_pinned_and_system(self):
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        result, _, _ = await self._effective(
            ResolvedTools(tool_ids=[], owns_toolset=False), system=["whoami"]
        )
        assert result == ["pinned", "whoami"]

    @pytest.mark.asyncio
    async def test_an_unbound_agent_with_nothing_pinned_runs_on_no_tools(self):
        """Empty, not None: None would mean "no tool filter" downstream."""
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        result, _, _ = await self._effective(
            ResolvedTools(tool_ids=[], owns_toolset=False), pinned=()
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_an_unbound_agent_checks_its_knowledge_base_for_spreadsheets(self):
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        _, _, autoenable = await self._effective(ResolvedTools(tool_ids=[], owns_toolset=False))
        assert autoenable.await_args.kwargs["kb_assistant_id"] == "ast-1"

    @pytest.mark.asyncio
    async def test_a_bound_agent_is_unchanged(self):
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        result, always_on, autoenable = await self._effective(
            ResolvedTools(tool_ids=["agent_tool_a"]), system=["whoami"]
        )
        assert result == ["agent_tool_a", "whoami"]
        always_on.assert_not_awaited()
        # Its author chose its toolset; the knowledge base does not add to it.
        assert autoenable.await_args.kwargs["kb_assistant_id"] is None

    @pytest.mark.asyncio
    async def test_a_plain_chat_turn_is_unchanged(self):
        result, _, autoenable = await self._effective(None, system=["whoami"])
        assert result == ["picker_tool", "pinned", "whoami"]
        assert autoenable.await_args.kwargs["kb_assistant_id"] is None

    @pytest.mark.asyncio
    async def test_a_project_harness_with_no_tools_keeps_the_pinned_set(self):
        """A harness that binds no tools resolves exactly like any unbound Agent."""
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        result, _, _ = await self._effective(ResolvedTools(tool_ids=[], owns_toolset=False))
        assert result == ["pinned"]

    @pytest.mark.asyncio
    async def test_a_project_whose_bound_tools_were_all_dropped_still_owns_its_toolset(self):
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        result, always_on, _ = await self._effective(ResolvedTools(tool_ids=[]))
        assert result == []
        always_on.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_viewers_of_an_unbound_agent_share_one_cache_slot(self):
        """`enabled_tools` is the tools element of the agent-cache key, so a picker
        reaching an unbound Agent's turn would also key its cached agent by viewer
        — and a cached agent built with one viewer's picker tools could serve a
        turn that should have none. The effective list, and the key, must not
        depend on the picker; a plain chat's still does."""
        from apis.inference_api.chat import service
        from apis.inference_api.chat.agent_binding_resolver import ResolvedTools

        def key(tools):
            return service._create_cache_key(
                session_id="s1", user_id="u1", enabled_tools=tools, model_id="m",
                inference_params={}, system_prompt=None, caching_enabled=False,
                provider="bedrock", freshness_hash="f", agent_type="chat",
                assistant_id="ast-1",
            )

        unbound = ResolvedTools(tool_ids=[], owns_toolset=False)
        canvas, _, _ = await self._effective(unbound, enabled_tools=["canvas-faculty"])
        bare, _, _ = await self._effective(unbound, enabled_tools=[])
        assert canvas == bare
        assert key(canvas) == key(bare)

        plain_canvas, _, _ = await self._effective(None, enabled_tools=["canvas-faculty"])
        plain_bare, _, _ = await self._effective(None, enabled_tools=[])
        assert key(plain_canvas) != key(plain_bare)


class TestSystemTools:
    """System (platform-shipped) tools apply on EVERY turn, unlike admin
    always-on. See docs/specs/platform-self-service/design.md."""

    @pytest.mark.asyncio
    async def test_system_tools_apply_even_on_a_bound_agent_turn(self):
        """An Agent author scopes the user-facing toolset; they do not get to
        remove the platform's own self-service capabilities."""
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(["pinned"])
        ) as always_on, patch.object(
            routes, "resolve_system_tool_ids", _resolver(["whoami"])
        ):
            result = await routes._apply_admin_always_on_tools(
                ["agent_tool_a"], USER, agent_bound_tools=True
            )
        # System tool unioned in; admin always-on still exempted (not awaited).
        assert result == ["agent_tool_a", "whoami"]
        always_on.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_and_always_on_combine_on_a_normal_turn(self):
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver(["pinned"])
        ), patch.object(routes, "resolve_system_tool_ids", _resolver(["whoami"])):
            result = await routes._apply_admin_always_on_tools(["picker_tool"], USER)
        assert result == ["picker_tool", "pinned", "whoami"]

    @pytest.mark.asyncio
    async def test_a_system_tool_the_user_already_has_is_not_duplicated(self):
        with patch.object(
            routes, "resolve_always_on_tool_ids", _resolver([])
        ), patch.object(routes, "resolve_system_tool_ids", _resolver(["whoami"])):
            result = await routes._apply_admin_always_on_tools(["whoami"], USER)
        assert result == ["whoami"]


class TestUnionSemantics:
    @pytest.mark.asyncio
    async def test_a_pinned_tool_the_user_already_enabled_is_not_duplicated(self):
        with patch.object(routes, "resolve_always_on_tool_ids", _resolver(["search_web"])):
            result = await routes._apply_admin_always_on_tools(
                ["search_web", "browse_web"], USER
            )
        assert result == ["search_web", "browse_web"]

    @pytest.mark.asyncio
    async def test_an_all_off_picker_receives_exactly_the_pinned_set(self):
        """§5 — a deliberate behaviour change, gated behind an admin flagging.

        `ToolFilter` returns NO tools for an empty list, so a user who turned
        everything off used to get a tool-less agent. They now get the pinned
        set, which is the point.
        """
        with patch.object(routes, "resolve_always_on_tool_ids", _resolver(["pinned"])):
            assert await routes._apply_admin_always_on_tools([], USER) == ["pinned"]

    @pytest.mark.asyncio
    async def test_pinned_ids_append_after_the_request_in_resolver_order(self):
        """The appended segment reaches `toolConfig`; its order must be fixed."""
        with patch.object(routes, "resolve_always_on_tool_ids", _resolver(["a", "b", "c"])):
            result = await routes._apply_admin_always_on_tools(["z"], USER)
        assert result == ["z", "a", "b", "c"]


class TestDeployIsANoOp:
    """§10 — with nothing pinned, the caller's own object comes back."""

    @pytest.mark.asyncio
    async def test_none_stays_none(self):
        with patch.object(routes, "resolve_always_on_tool_ids", _resolver([])):
            assert await routes._apply_admin_always_on_tools(None, USER) is None

    @pytest.mark.asyncio
    async def test_the_list_object_is_returned_by_identity(self):
        original = ["search_web"]
        with patch.object(routes, "resolve_always_on_tool_ids", _resolver([])):
            result = await routes._apply_admin_always_on_tools(original, USER)
        # Identity, not equality: an implementation that rebuilt an equal list
        # would pass `==` and still change the agent-cache slot downstream.
        assert result is original
