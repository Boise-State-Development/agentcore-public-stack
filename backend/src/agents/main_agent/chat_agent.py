"""
Chat Agent - Text-based conversational agent

Extends BaseAgent with Strands Agent creation and text streaming.
This is the default agent type for standard chat interactions.
"""

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.core import AgentFactory

logger = logging.getLogger(__name__)


class ChatAgent(BaseAgent):
    """
    Text-based chat agent using Strands Agent.

    Handles:
    - Strands Agent creation with filtered tools and hooks
    - Text message streaming via StreamCoordinator
    - Multimodal prompt building (text + files)
    """

    def _create_agent(self) -> None:
        """Create Strands Agent with filtered tools and session management."""
        try:
            tools = self._build_filtered_tools()
            hooks = self._create_hooks()

            self.agent = AgentFactory.create_agent(
                model_config=self.model_config,
                system_prompt=self.system_prompt,
                tools=tools,
                session_manager=self.session_manager,
                hooks=hooks,
            )

        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
        interrupt_responses: Optional[List[Dict[str, Any]]] = None,
        continue_truncated: bool = False,
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses.

        Args:
            message: User message text. Ignored when resuming via
                `interrupt_responses` — the paused turn already has the
                original prompt in `_interrupt_state`.
            session_id: Session identifier (defaults to instance session_id)
            files: Optional list of FileContent objects (with base64 bytes)
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
            prompt = self.multimodal_builder.build_prompt(message, files)

        async for event in self.stream_coordinator.stream_response(
            agent=self.agent,
            prompt=prompt,
            session_manager=self.session_manager,
            session_id=session_id or self.session_id,
            user_id=self.user_id,
            main_agent_wrapper=self,
            citations=citations,
            original_message=original_message,
        ):
            yield event
