"""
Skill Agent — ChatAgent with progressive skill disclosure.

Replaces individual skill tools with skill_dispatcher + skill_executor,
injecting a lightweight skill catalog into the system prompt instead of
loading all tool schemas upfront.

Two skill sources:

- **DB / admin-managed** (PR-6): when ``accessible_skill_ids`` is provided
  (the caller resolved them from the user's RBAC roles), the registry loads
  those ACTIVE skills from the catalog repository. A granted skill's bound
  catalog tools are added to the agent's tool universe so they materialize
  (skill-as-grant). Local tools fold behind the two meta-tools by object
  identity; gateway / external MCP bound tools fold via the MCP client (their
  schemas are dropped from the model tool list and they execute through
  ``call_tool_sync`` — see ``skills/mcp_binding.py``, PR-6b).
- **File / dev** (legacy): when ``accessible_skill_ids`` is None, the registry
  scans ``definitions/*/SKILL.md`` and binds local ``@skill``-decorated tools
  by their ``_skill_name`` stamp, exactly as before — unchanged behavior.

When zero skills are available the agent degrades to plain ``ChatAgent``.
"""

import logging
from typing import Any, List, Optional

from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.core import AgentFactory
from agents.main_agent.skills import SkillRegistry, make_skill_tools

logger = logging.getLogger(__name__)


def _is_active_status(status: Any) -> bool:
    """True if a skill record's status is ACTIVE (handles enum or str)."""
    return str(status).split(".")[-1].lower() == "active"


def _fetch_skill_records(skill_ids: List[str]) -> List[Any]:
    """Fetch ACTIVE skill records for the given ids from the catalog repo.

    Bridges the async repository call into this sync agent-build path the same
    way ``_register_external_mcp_tools`` / ``_expand_gateway_tool_ids`` do.
    Returns an empty list on any failure (the agent then degrades to chat).
    """
    if not skill_ids:
        return []

    import asyncio

    from apis.shared.skills.repository import get_skill_catalog_repository

    repo = get_skill_catalog_repository()

    async def _go() -> List[Any]:
        records = await repo.batch_get_skills(list(skill_ids))
        return [r for r in records if _is_active_status(getattr(r, "status", "active"))]

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return executor.submit(asyncio.run, _go()).result()
            return loop.run_until_complete(_go())
        except RuntimeError:
            return asyncio.run(_go())
    except Exception as e:  # noqa: BLE001 - degrade to chat on any error
        logger.warning("Could not load skill records: %s", e)
        return []


class SkillAgent(ChatAgent):
    """
    Chat agent with progressive skill disclosure.

    Overrides _create_agent() to:
    1. Discover skills (DB-backed when RBAC ids are supplied, else file scan)
    2. Bind tools to skills (by catalog id for DB skills, by _skill_name for file)
    3. Replace skill-bound tools with skill_dispatcher + skill_executor
    4. Inject the skill catalog into the system prompt

    The LLM sees a lightweight catalog and two meta-tools instead of all
    individual tool schemas, reducing upfront token usage.
    """

    def __init__(
        self,
        skills_dir: Optional[str] = None,
        accessible_skill_ids: Optional[List[str]] = None,
        **kwargs,
    ):
        """
        Initialize skill agent.

        Args:
            skills_dir: Optional path to file-based skill definitions (dev path).
            accessible_skill_ids: When provided, load these admin/DB skills
                (already RBAC-resolved for the user). When None, fall back to
                the file scan.
            **kwargs: All BaseAgent constructor args (session_id, user_id, ...).
        """
        self._skills_dir = skills_dir
        self._accessible_skill_ids = accessible_skill_ids
        self._db_mode = accessible_skill_ids is not None
        self._registry: SkillRegistry = SkillRegistry(skills_dir)

        # Discover skills BEFORE super().__init__ so we can augment the enabled
        # tool set below — the materialization pipeline runs inside
        # super().__init__ (which calls _create_agent at its tail).
        if self._db_mode:
            records = _fetch_skill_records(accessible_skill_ids or [])
            self._registry.load_records(records)
        else:
            self._registry.discover_skills()

        # Skill-as-grant: a granted skill's bound catalog tools become available
        # whenever this agent runs, independent of the user's per-tool
        # enabled_tools. Fold them into the enabled set so they materialize.
        original_enabled_tools = kwargs.get("enabled_tools")
        bound_ids = self._registry.all_bound_tool_ids()
        if bound_ids:
            existing = list(original_enabled_tools or [])
            kwargs["enabled_tools"] = list(dict.fromkeys(existing + bound_ids))

        super().__init__(**kwargs)

        # Resume hashes the construction snapshot's enabled_tools into the
        # cache key. The augmentation above is recomputed deterministically by
        # this same constructor, so the snapshot must store the ORIGINAL
        # (route-supplied) enabled_tools — not the augmented set — or a resume
        # would land on a different cache slot and orphan the paused agent
        # (same hazard the system_prompt snapshot avoids). The cache key on the
        # live turn already uses the original enabled_tools + skills_hash.
        self._construction_snapshot["enabled_tools"] = original_enabled_tools

    def _create_agent(self) -> None:
        """Create the Strands Agent with skill disclosure instead of raw tool schemas."""
        try:
            # Step 1: Materialize the (possibly augmented) tool universe.
            all_tools = self._build_filtered_tools()

            # Step 2: Degrade to plain ChatAgent when there are no skills.
            if self._registry.get_skill_count() == 0:
                logger.info(
                    "No skills available — falling back to standard ChatAgent behavior"
                )
                hooks = self._create_hooks()
                self.agent = AgentFactory.create_agent(
                    model_config=self.model_config,
                    system_prompt=self.system_prompt,
                    tools=all_tools,
                    session_manager=self.session_manager,
                    hooks=hooks,
                )
                return

            # Step 3: Bind tools to skills.
            if self._db_mode:
                self._bind_catalog_tools()  # local tools (by catalog id)
                self._bind_mcp_tools()      # gateway / external MCP tools (folded)
            else:
                self._registry.bind_tools(all_tools)

            # Step 4: Fold skill-bound tools out of the top-level list (matched
            # by object identity — the bound objects are the same instances the
            # tool filter materialized).
            skill_tool_ids = set()
            for skill_name in self._registry.get_skill_names():
                for tool_obj in self._registry.get_tools(skill_name):
                    skill_tool_ids.add(id(tool_obj))
            non_skill_tools = [t for t in all_tools if id(t) not in skill_tool_ids]

            # Step 5: Build per-agent meta-tools bound to THIS registry (no
            # process-global — safe for concurrent per-user skills).
            dispatcher, executor = make_skill_tools(self._registry)
            final_tools = non_skill_tools + [dispatcher, executor]

            # Step 6: Inject the skill catalog into the system prompt.
            catalog = self._registry.get_catalog()
            if catalog:
                if isinstance(self.system_prompt, str):
                    self.system_prompt = self.system_prompt + "\n\n" + catalog
                elif isinstance(self.system_prompt, list):
                    self.system_prompt.append({"text": "\n\n" + catalog})

            logger.info(
                "SkillAgent created: %d skills (%s), %d non-skill tools, "
                "2 meta-tools (dispatcher + executor)",
                self._registry.get_skill_count(),
                "db" if self._db_mode else "file",
                len(non_skill_tools),
            )

            # Step 7: Create the agent.
            hooks = self._create_hooks()
            self.agent = AgentFactory.create_agent(
                model_config=self.model_config,
                system_prompt=self.system_prompt,
                tools=final_tools,
                session_manager=self.session_manager,
                hooks=hooks,
            )

        except Exception as e:
            logger.error(f"Error creating skill agent: {e}")
            raise

    def _bind_catalog_tools(self) -> None:
        """Resolve each DB skill's bound LOCAL catalog tool ids to live objects.

        Local tools resolve via the agent's tool registry (the same instances
        the tool filter materialized), so they fold cleanly behind the meta-
        tools by object identity. Gateway / external MCP bound ids don't resolve
        to individual objects here (they're live client objects) and are handled
        by :meth:`_bind_mcp_tools`.
        """
        catalog_map: dict = {}
        for tid in self._registry.all_bound_tool_ids():
            if self.tool_registry.has_tool(tid):
                catalog_map[tid] = self.tool_registry.get_tool(tid)

        self._registry.bind_catalog_tools(catalog_map)

    def _bind_mcp_tools(self) -> None:
        """Fold a granted skill's gateway / external MCP bound tools (PR-6b).

        These materialize as *client objects* (one ``MCPClient`` per server,
        exposing many tools), not individual callables — which is why PR-6a left
        them visible. Here each bound non-local id is classified (gateway vs
        external), resolved to its concrete MCP tool(s) + owning client, wrapped
        as a ``FoldedMCPTool`` bound into the registry (so the meta-tools can
        show its schema and run it), and its agent-facing name is folded off the
        client's model tool list. The client object stays in the agent's tool
        list, so Strands keeps its session alive for ``call_tool_sync``.
        """
        non_local = [
            tid
            for tid in self._registry.all_bound_tool_ids()
            if not self.tool_registry.has_tool(tid)
        ]
        if not non_local:
            return

        # Classify the non-local bound ids with the same filter that
        # materialized the clients (its external set was populated from the
        # augmented enabled_tools in __init__).
        classified = self.tool_filter.filter_tools_extended(non_local)
        gateway_ids = classified.gateway_tool_ids
        external_ids = classified.external_mcp_tool_ids
        if not gateway_ids and not external_ids:
            return

        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from agents.main_agent.integrations.mcp_tool_folding import (
            reset_folded_tool_names,
            set_folded_tool_names,
        )
        from agents.main_agent.skills.mcp_binding import resolve_mcp_bindings

        external_integration = get_external_mcp_integration()

        # Clients are process-global and reused across agent builds; a prior
        # build's fold persists on them (set_folded_tool_names only adds).
        # resolve_mcp_bindings enumerates an external server through that same
        # fold-filtered list_tools_sync, so a stale fold makes this re-bind see
        # zero tools (the bound tool "works once, then disappears"). Reset each
        # client this build will resolve so enumeration sees the full server;
        # the fold is recomputed and re-applied from the bindings just below.
        gateway_client = self.gateway_integration.client
        if gateway_client is not None:
            reset_folded_tool_names(gateway_client)
        seen_clients: set = set()
        for tid in external_ids:
            client = external_integration.get_client(tid, self.user_id)
            if client is not None and id(client) not in seen_clients:
                seen_clients.add(id(client))
                reset_folded_tool_names(client)

        bindings = resolve_mcp_bindings(
            gateway_ids=gateway_ids,
            external_ids=external_ids,
            gateway_client=gateway_client,
            expand_gateway=self._expand_gateway_tool_ids,
            external_client_lookup=lambda tid: external_integration.get_client(
                tid, self.user_id
            ),
        )

        if bindings.catalog_map:
            self._registry.bind_catalog_tools(bindings.catalog_map)
        for client, names in bindings.fold_by_client.items():
            set_folded_tool_names(client, names)

        folded_count = sum(len(v) for v in bindings.catalog_map.values())
        logger.info(
            "SkillAgent folded %d gateway/external MCP tool(s) behind the meta-"
            "tools (%d unresolved)",
            folded_count,
            len(bindings.unresolved),
        )

    def _build_tool_use_provider_lookup(self):
        """See through the skill fold for the OAuth consent gate.

        Skill-bound external MCP tools execute via ``skill_executor``, so the
        consent hook's ``provider_lookup`` (keyed on ``MCPAgentTool``) can't
        map them. This resolver reads the executor's tool_use input and maps
        the folded tool's owning client back to its OAuth provider, so an
        unauthorized call pauses the turn with ``oauth_required`` exactly
        like a directly-enabled tool would.
        """
        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from agents.main_agent.skills.mcp_binding import (
            make_folded_tool_provider_lookup,
        )

        integration = get_external_mcp_integration()
        return make_folded_tool_provider_lookup(
            self._registry, integration.provider_for_client
        )

    def _build_tool_use_approval_lookup(self):
        """See through the skill fold for the per-tool approval gate.

        Skill-bound external MCP tools execute via ``skill_executor``, so the
        approval hook's ``approval_names_lookup`` (keyed on ``MCPAgentTool``)
        can't gate them — an admin's ``needs_approval`` flag was silently
        bypassed in skills mode. This resolver reads the executor's tool_use
        input and checks the folded tool against its owning client's flagged
        set, so the user sees the same approval prompt (describing the inner
        tool, not the executor) as a directly-enabled tool would raise.
        """
        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from agents.main_agent.skills.mcp_binding import (
            make_folded_tool_approval_lookup,
        )

        integration = get_external_mcp_integration()
        return make_folded_tool_approval_lookup(
            self._registry, integration.approval_names_for_client
        )

    @property
    def registry(self) -> Optional[SkillRegistry]:
        """Access the skill registry for inspection."""
        return self._registry
