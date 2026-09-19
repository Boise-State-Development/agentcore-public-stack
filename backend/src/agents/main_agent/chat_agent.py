"""
Chat Agent - Text-based conversational agent

Extends BaseAgent with Strands Agent creation and text streaming.
This is the default agent type for standard chat interactions.
"""

import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.core import AgentFactory
from agents.main_agent.skills.strands_mapping import build_skills_runtime

from apis.shared.observability.build_stages import mark_stage

logger = logging.getLogger(__name__)


class ChatAgent(BaseAgent):
    """
    Text-based chat agent using Strands Agent.

    Handles:
    - Strands Agent creation with filtered tools and hooks
    - Text message streaming via StreamCoordinator
    - Multimodal prompt building (text + files)
    - Skills disclosure (Skills v2): when ``accessible_skill_ids`` is provided,
      the vended Strands ``AgentSkills`` plugin injects an ``<available_skills>``
      catalog and a ``skills`` activation tool. Skills are pure knowledge
      bundles — they bind no tools (the tool universe comes solely from the
      Agent's bindings + RBAC-gated ``enabled_tools``).
    """

    def __init__(
        self,
        accessible_skill_ids: Optional[List[str]] = None,
        **kwargs: Any,
    ):
        """
        Args:
            accessible_skill_ids: Effective skill ids for this turn (already
                RBAC-resolved and narrowed by the request's selection). When
                non-empty an ``AgentSkills`` plugin is added; ``None``/empty
                keeps plain chat behavior. Stored BEFORE ``super().__init__``
                because ``BaseAgent.__init__`` calls ``_create_agent`` at its tail.
            **kwargs: All BaseAgent constructor args (session_id, user_id, ...).
        """
        self._accessible_skill_ids = accessible_skill_ids
        super().__init__(**kwargs)

    def _create_agent(self) -> None:
        """Create Strands Agent with filtered tools, hooks, and skills plugin."""
        try:
            tools = self._build_filtered_tools()
            # External MCP pre-flight lives in here — the spec's standing
            # (and unverified) hypothesis for the cold build.
            mark_stage("tools")
            hooks = self._create_hooks()
            mark_stage("hooks")

            # Skills disclosure: the AgentSkills plugin injects the catalog +
            # activation tool; read_skill_file is the S3 adapter for reference
            # files (added after tool filtering — it is infrastructure, not an
            # RBAC-gated tool, and is implicitly scoped to the turn's skills).
            plugin, read_skill_file = build_skills_runtime(self._accessible_skill_ids)
            plugins = [plugin] if plugin else []
            if plugin:
                tools = list(tools) + [read_skill_file]
                logger.info(
                    "ChatAgent: skills disclosure enabled (%d accessible skill ids)",
                    len(self._accessible_skill_ids or []),
                )

            # Tool-result offload at intake (compaction PR-4): oversized tool
            # results become a bounded preview + retrieval references before
            # they enter the cacheable prefix. Fail-open: None when off or
            # unconfigured. The plugin registers retrieve_offloaded_content
            # itself — one stable spec in toolConfig, not an RBAC-gated tool,
            # like read_skill_file above.
            from agents.main_agent.core.tool_result_offload import build_tool_result_offloader

            offload_session = getattr(self, "session_id", None)
            offload_user = getattr(self, "user_id", None)
            offloader = (
                build_tool_result_offloader(
                    session_id=offload_session,
                    user_id=offload_user,
                    region=os.environ.get("AWS_REGION"),
                )
                if offload_session and offload_user
                else None
            )
            if offloader is not None:
                plugins.append(offloader)
            plugins = plugins or None

            mark_stage("plugins")
            self.agent = AgentFactory.create_agent(
                model_config=self.model_config,
                system_prompt=self._system_prompt_for(tools),
                tools=tools,
                session_manager=self.session_manager,
                hooks=hooks,
                plugins=plugins,
            )

        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

    def _system_prompt_for(self, tools: List[Any]) -> str:
        """The system prompt this turn actually sends.

        Adds ``ask_user_question``'s guidance when that tool is in the turn's
        effective tool list. Without it the model overwhelmingly answers an
        ambiguous request in prose instead of asking (measured 4/24 vs 24/24);
        see ``SYSTEM_PROMPT_GUIDANCE`` for why the text is load-bearing.

        Three deliberate choices:

        * **Gated on the tool, not shipped to everyone.** A user without the
          tool would otherwise carry an instruction to call something they do
          not have.
        * **Keyed on the POST-FILTER list**, not the request's
          ``enabled_tools``. The two diverge — a tool id the catalog knows but
          the registry does not is dropped by ``ToolFilter`` with a log line
          (``canvas_faculty`` does this today) — and keying on the request
          would advertise a tool absent from ``toolConfig``. It also picks up
          the ``ASK_USER_QUESTION_ENABLED`` kill switch for free: while off the
          tool is never registered, so it cannot appear here.
        * **Applied to the prompt handed to the agent, not to
          ``self.system_prompt``.** That field is snapshotted for resume and
          hashed into the agent cache key; this text is derived from
          ``enabled_tools``, which the cache key already covers via
          ``tools_hash``, so mutating it would change resume's cache slot for
          no benefit. ``PrefixFingerprintHook`` reads the prompt off the built
          agent, so ``systemPromptHash`` still reflects what was really sent.

        Cost: ~70 tokens, constant per configuration, inside the cacheable
        prefix — it is written once per session, never re-written per turn.
        """
        from agents.builtin_tools.ask_user_question import SYSTEM_PROMPT_GUIDANCE

        for tool in tools:
            spec = getattr(tool, "tool_spec", None)
            if isinstance(spec, dict) and spec.get("name") == "ask_user_question":
                return f"{self.system_prompt}\n\n{SYSTEM_PROMPT_GUIDANCE}"
        return self.system_prompt

    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None,
        attachment_names: Optional[List[str]] = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
        interrupt_responses: Optional[List[Dict[str, Any]]] = None,
        continue_truncated: bool = False,
        turn_agent_id: Optional[str] = None,
        turn_lease: Any = None,
        turn_started_at: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses.

        Args:
            message: User message text. Ignored when resuming via
                `interrupt_responses` — the paused turn already has the
                original prompt in `_interrupt_state`.
            session_id: Session identifier (defaults to instance session_id)
            files: Optional list of FileContent objects (with base64 bytes).
                Inline attachments only — diverted ones (spreadsheets, decks)
                must not be here or they become invalid document blocks.
            attachment_names: Every filename the user attached this turn,
                including diverted ones, for the `[Attached files: …]` marker
                the SPA replays to rebuild attachment cards on reload.
            citations: Optional list of citation dicts from RAG retrieval
            original_message: Original user message before RAG augmentation
            interrupt_responses: When set, resume a paused agent turn by
                passing this list as the prompt to Strands. Each entry is
                `{"interruptResponse": {"interruptId": str, "response": Any}}`.
            continue_truncated: When True, resume after a max_tokens
                truncation by passing an empty-list prompt to Strands.
                `_convert_prompt_to_messages([])` appends no message, so the
                event loop re-runs against restored history whose tail is the
                truncated assistant message — the model continues it
                (assistant-prefill) instead of answering a new instruction.
            turn_lease: This turn's single-flight `SessionLease`, which doubles
                as the mid-turn steering inbox. Passed per turn rather than read
                off the agent for the same reason as `turn_agent_id`: the agent
                instance is cached across turns, so per-turn state must never
                live on it (#741/#751).

        Yields:
            str: SSE formatted events
        """
        if not self.agent:
            self._create_agent()

        if interrupt_responses:
            # Strands' resume protocol: passing a list of interrupt responses
            # as the prompt re-enters the loop, populates the matching
            # interrupts' `.response`, and continues from the paused tool
            # call. multimodal_builder + files do not apply here.
            prompt: Any = interrupt_responses
        elif continue_truncated:
            # Empty list → Strands appends nothing → the loop re-runs against
            # restored history (tail = truncated assistant message). No new
            # user turn, no multimodal/files.
            prompt = []
        else:
            prompt = self.multimodal_builder.build_prompt(
                message, files, attachment_names=attachment_names
            )

        async for event in self.stream_coordinator.stream_response(
            agent=self.agent,
            prompt=prompt,
            session_manager=self.session_manager,
            session_id=session_id or self.session_id,
            user_id=self.user_id,
            main_agent_wrapper=self,
            citations=citations,
            original_message=original_message,
            turn_agent_id=turn_agent_id,
            turn_lease=turn_lease,
            turn_started_at=turn_started_at,
        ):
            yield event
