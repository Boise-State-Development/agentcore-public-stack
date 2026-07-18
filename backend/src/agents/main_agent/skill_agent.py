"""
Skill Agent — ChatAgent with progressive skill disclosure.

Replaces individual skill tools with skill_dispatcher + skill_executor,
injecting a lightweight skill catalog into the system prompt instead of
loading all tool schemas upfront.

Skills are pure knowledge bundles (instructions + reference material) — they
do not bind or grant tools (Skills v2). The tool universe for a turn comes
solely from the Agent's bindings + the user's RBAC-gated enabled_tools.

Two skill sources:

- **DB / admin-managed**: when ``accessible_skill_ids`` is provided (the
  caller resolved them from the user's RBAC roles), the registry loads those
  ACTIVE skills from the catalog repository and surfaces their instructions
  through the meta-tools. No tools are bound.
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
    2. Bind local @skill tools (file/dev path only; DB skills bind none)
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

        super().__init__(**kwargs)

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

            # Step 3: Bind tools to skills. Only the file/dev path binds tools
            # (local @skill-decorated callables); DB-backed skills are pure
            # instruction bundles and carry no bound tools.
            if not self._db_mode:
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

    @property
    def registry(self) -> Optional[SkillRegistry]:
        """Access the skill registry for inspection."""
        return self._registry
