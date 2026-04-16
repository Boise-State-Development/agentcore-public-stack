"""
Skill Agent — ChatAgent with progressive skill disclosure.

Replaces individual skill tools with skill_dispatcher + skill_executor,
injecting a lightweight skill catalog into the system prompt instead of
loading all tool schemas upfront.
"""

import logging
from typing import List, Optional

from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.core import AgentFactory
from agents.main_agent.skills import (
    SkillRegistry,
    skill_dispatcher,
    skill_executor,
    set_dispatcher_registry,
)

logger = logging.getLogger(__name__)


class SkillAgent(ChatAgent):
    """
    Chat agent with progressive skill disclosure.

    Overrides _create_agent() to:
    1. Discover skills from SKILL.md definitions
    2. Bind tools to skills via _skill_name metadata
    3. Replace skill tools with skill_dispatcher + skill_executor
    4. Inject skill catalog into system prompt

    The LLM sees a lightweight catalog and two meta-tools instead of
    all individual tool schemas, dramatically reducing token usage.
    """

    def __init__(self, skills_dir: Optional[str] = None, **kwargs):
        """
        Initialize skill agent.

        Args:
            skills_dir: Optional path to skills definitions directory.
                        Defaults to skills/definitions/ relative to the skills module.
            **kwargs: All BaseAgent constructor args (session_id, user_id, etc.)
        """
        self._skills_dir = skills_dir
        self._registry: Optional[SkillRegistry] = None
        super().__init__(**kwargs)

    def _create_agent(self) -> None:
        """Create Strands Agent with skill disclosure instead of raw tool schemas."""
        try:
            # Step 1: Get all filtered tools (local + gateway + external MCP)
            all_tools = self._build_filtered_tools()

            # Step 2: Initialize skill registry
            self._registry = SkillRegistry(self._skills_dir)
            discovered = self._registry.discover_skills()

            if discovered == 0:
                logger.warning("No skills discovered — falling back to standard ChatAgent behavior")
                # Fall back to standard ChatAgent behavior
                hooks = self._create_hooks()
                self.agent = AgentFactory.create_agent(
                    model_config=self.model_config,
                    system_prompt=self.system_prompt,
                    tools=all_tools,
                    session_manager=self.session_manager,
                    hooks=hooks,
                )
                return

            # Step 3: Bind tools to skills
            self._registry.bind_tools(all_tools)

            # Step 4: Separate skill tools from non-skill tools
            skill_tool_set = set()
            for skill_name in self._registry.get_skill_names():
                for tool_obj in self._registry.get_tools(skill_name):
                    skill_tool_set.add(id(tool_obj))

            non_skill_tools = [t for t in all_tools if id(t) not in skill_tool_set]

            # Step 5: Wire up the dispatcher registry
            set_dispatcher_registry(self._registry)

            # Step 6: Build final tool list: non-skill tools + dispatcher + executor
            final_tools = non_skill_tools + [skill_dispatcher, skill_executor]

            # Step 7: Inject skill catalog into system prompt
            catalog = self._registry.get_catalog()
            if catalog:
                if isinstance(self.system_prompt, str):
                    self.system_prompt = self.system_prompt + "\n\n" + catalog
                elif isinstance(self.system_prompt, list):
                    # System prompt is a list of content blocks
                    self.system_prompt.append({"text": "\n\n" + catalog})

            logger.info(
                f"SkillAgent created: {discovered} skills, "
                f"{len(non_skill_tools)} non-skill tools, "
                f"2 meta-tools (dispatcher + executor)"
            )

            # Step 8: Create agent
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
