"""
Compacting Session Manager for AgentCore Memory

Extends AgentCoreMemorySessionManager (subclass, not wrapper) so the SDK's
standard hook wiring (register_hooks) handles message persistence, sync, and
LTM retrieval correctly.  We only override initialize() to layer on compaction
after the SDK finishes its standard session restore.

Compaction Strategy (two-feature approach):
- Stage 1: Tool content truncation — applied every turn, reduces verbose tool I/O
- Stage 2: Checkpoint + Summary — triggered when token threshold exceeded

Based on: https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import copy
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

from .compaction_models import CompactionState, CompactionConfig

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)


class TurnBasedSessionManager(AgentCoreMemorySessionManager):
    """
    Session manager with token-based context compaction.

    Inherits from AgentCoreMemorySessionManager so the SDK's register_hooks()
    handles all hook wiring: append_message, sync_agent, retrieve_customer_context.
    We override initialize() to apply compaction after the SDK restores the session.

    Features:
    - Checkpoint-based message loading (skip old messages, prepend summary)
    - Tool content truncation (reduce verbose tool I/O in older turns)
    - Session cancellation support (via cancelled flag)
    - Compaction state persisted in DynamoDB session metadata
    """

    # Class-level DynamoDB table reference for compaction state
    _dynamodb_table = None
    _dynamodb_table_name: Optional[str] = None

    def __init__(
        self,
        agentcore_memory_config: AgentCoreMemoryConfig,
        region_name: str = "us-west-2",
        compaction_config: Optional[CompactionConfig] = None,
        user_id: Optional[str] = None,
        summarization_strategy_id: Optional[str] = None,
        **kwargs: Any,
    ):
        """
        Initialize session manager with optional compaction.

        Args:
            agentcore_memory_config: AgentCore Memory configuration
            region_name: AWS region
            compaction_config: Compaction configuration (None = disabled)
            user_id: User ID for DynamoDB session lookup
            summarization_strategy_id: Strategy ID for LTM summary retrieval
        """
        super().__init__(
            agentcore_memory_config=agentcore_memory_config,
            region_name=region_name,
            **kwargs,
        )

        self.user_id = user_id
        self.region_name = region_name
        self.summarization_strategy_id = summarization_strategy_id

        # Compaction config (None means disabled)
        self.compaction_config = compaction_config

        # Compaction state (loaded during initialize)
        self.compaction_state: Optional[CompactionState] = None

        # Cached data for checkpoint calculation
        self._valid_cutoff_indices: List[int] = []
        self._all_messages_for_summary: List[Dict] = []
        self._total_message_count_at_init: int = 0

        # Session control
        self.cancelled = False

        # Message count tracking (for stream_coordinator compatibility)
        self.message_count: int = 0

        # Log initialization
        if compaction_config and compaction_config.enabled:
            logger.info(
                f"TurnBasedSessionManager initialized with compaction "
                f"(threshold={compaction_config.token_threshold:,}, "
                f"protected_turns={compaction_config.protected_turns})"
            )
        else:
            logger.info("TurnBasedSessionManager initialized (compaction disabled)")

    # =========================================================================
    # SDK Overrides — minimal surface area
    # =========================================================================

    def append_message(self, message: Dict, agent: "Agent", **kwargs: Any) -> None:
        """Append message with empty-content filtering and cancellation check."""
        if self.cancelled:
            logger.warning("Session cancelled, ignoring message")
            return

        # Filter out empty content blocks before saving
        filtered_message = self._filter_empty_text(message)
        content = filtered_message.get("content", [])
        if not content or (isinstance(content, list) and len(content) == 0):
            logger.debug("Skipping message with empty content")
            return

        super().append_message(filtered_message, agent, **kwargs)
        self.message_count += 1

    def initialize(self, agent: "Agent", **kwargs: Any) -> None:
        """
        Initialize agent with two-feature compaction.

        Flow:
        1. Capture whether this is a new session (before SDK resets the flag)
        2. Let the SDK restore agent state and load messages from AgentCore Memory
        3. Apply compaction (checkpoint + truncation) on the loaded messages
        """
        is_new_session = self._is_new_session

        logger.info(f"TurnBasedSessionManager.initialize() called for agent_id={agent.agent_id}")

        # Let the SDK handle all session restore logic:
        # - read/create agent in session repository
        # - restore agent state, internal state, conversation manager state
        # - load messages from AgentCore Memory
        # - fix broken tool-use histories
        super().initialize(agent, **kwargs)

        self._total_message_count_at_init = len(agent.messages)
        self.message_count = self._total_message_count_at_init

        # Initialize compaction defaults
        self.compaction_state = CompactionState()
        self._valid_cutoff_indices = []
        self._all_messages_for_summary = []

        # Apply compaction for existing sessions with compaction enabled
        if is_new_session:
            return

        if not self.compaction_config or not self.compaction_config.enabled:
            return

        try:
            self._apply_compaction(agent)
        except Exception as e:
            logger.error(f"Compaction failed, using full history: {e}", exc_info=True)
            self.compaction_state = CompactionState()
            self._valid_cutoff_indices = []
            self._all_messages_for_summary = []

    # =========================================================================
    # Compaction — applied after SDK session restore
    # =========================================================================

    def _apply_compaction(self, agent: "Agent") -> None:
        """
        Apply compaction to agent.messages after SDK session restore.

        Modifies agent.messages in-place to:
        1. Skip old messages (checkpoint-based)
        2. Prepend conversation summary
        3. Truncate verbose tool content in older turns
        """
        all_messages = agent.messages

        logger.info(
            f"Compaction decision: config={self.compaction_config}, "
            f"enabled={self.compaction_config.enabled}, "
            f"messages_loaded={len(all_messages)}"
        )

        # Load compaction state from DynamoDB
        self.compaction_state = self._load_compaction_state()

        # Cache valid cutoff indices (user text messages, not tool results)
        self._valid_cutoff_indices = self._find_valid_cutoff_indices(all_messages)

        # Store messages for summary generation (shallow — only deep-copied
        # if update_after_turn actually advances the checkpoint)
        self._all_messages_for_summary = all_messages[:]

        # Apply checkpoint: skip old messages, prepend summary
        checkpoint = self.compaction_state.checkpoint
        stage = "none"

        if checkpoint > 0 and checkpoint < len(all_messages):
            messages_to_process = all_messages[checkpoint:]

            summary = self.compaction_state.summary
            if summary and messages_to_process:
                messages_to_process = self._prepend_summary_to_first_message(
                    messages_to_process, summary
                )
            stage = "checkpoint"
        else:
            messages_to_process = all_messages

        # Apply truncation (always when compaction enabled)
        protected_indices = self._find_protected_indices(
            messages_to_process, self.compaction_config.protected_turns
        )
        truncated_messages, truncation_count, _ = self._truncate_tool_contents(
            messages_to_process, protected_indices=protected_indices
        )

        if truncation_count > 0:
            stage = "checkpoint+truncation" if stage == "checkpoint" else "truncation"

        agent.messages = truncated_messages

        logger.info(
            f"Compaction initialized: stage={stage}, "
            f"original={self._total_message_count_at_init}, "
            f"final={len(agent.messages)}, "
            f"truncations={truncation_count}"
        )

    # =========================================================================
    # Compaction State Persistence
    # =========================================================================

    def _get_dynamodb_table(self):
        """Lazy initialization of DynamoDB table for compaction state."""
        if TurnBasedSessionManager._dynamodb_table is None:
            table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
            if not table_name:
                logger.warning(
                    "DYNAMODB_SESSIONS_METADATA_TABLE_NAME not configured, "
                    "compaction state will not persist"
                )
                return None

            import boto3

            TurnBasedSessionManager._dynamodb_table_name = table_name
            dynamodb = boto3.resource("dynamodb", region_name=self.region_name)
            TurnBasedSessionManager._dynamodb_table = dynamodb.Table(table_name)
            logger.debug(f"Initialized DynamoDB table for compaction: {table_name}")
        return TurnBasedSessionManager._dynamodb_table

    def _get_session_via_gsi(self, table) -> Optional[Dict]:
        """Look up session record using GSI (SessionLookupIndex)."""
        try:
            from boto3.dynamodb.conditions import Key

            response = table.query(
                IndexName="SessionLookupIndex",
                KeyConditionExpression=(
                    Key("GSI_PK").eq(f"SESSION#{self.config.session_id}")
                    & Key("GSI_SK").eq("META")
                ),
            )

            items = response.get("Items", [])
            if not items:
                return None

            item = items[0]
            if item.get("userId") != self.user_id:
                logger.warning(f"Session {self.config.session_id} belongs to different user")
                return None

            return item

        except Exception as e:
            logger.debug(f"GSI lookup failed: {e}")
            return None

    def _load_compaction_state(self) -> CompactionState:
        """Load compaction state from DynamoDB session metadata."""
        if not self.user_id or not self.compaction_config or not self.compaction_config.enabled:
            return CompactionState()

        try:
            table = self._get_dynamodb_table()
            if not table:
                return CompactionState()

            session_item = self._get_session_via_gsi(table)
            if not session_item:
                return CompactionState()

            compaction_data = session_item.get("compaction")
            if compaction_data:
                state = CompactionState.from_dict(compaction_data)
                logger.info(
                    f"Loaded compaction state: checkpoint={state.checkpoint}, "
                    f"summary_len={len(state.summary) if state.summary else 0}, "
                    f"last_tokens={state.last_input_tokens}"
                )
                return state

            return CompactionState()

        except Exception as e:
            logger.warning(f"Error loading compaction state: {e}")
            return CompactionState()

    def _save_compaction_state(self, state: CompactionState) -> None:
        """Save compaction state to DynamoDB session metadata."""
        if not self.user_id or not self.compaction_config or not self.compaction_config.enabled:
            return

        try:
            table = self._get_dynamodb_table()
            if not table:
                return

            session_item = self._get_session_via_gsi(table)
            if not session_item:
                logger.warning("Session record not found, cannot save compaction state")
                return

            pk = session_item.get("PK")
            sk = session_item.get("SK")
            if not pk or not sk:
                return

            state.updated_at = datetime.now(timezone.utc).isoformat()
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression="SET compaction = :state",
                ExpressionAttributeValues={":state": state.to_dict()},
            )
            logger.debug(f"Saved compaction state: checkpoint={state.checkpoint}")
        except Exception as e:
            logger.error(f"Error saving compaction state: {e}")

    # =========================================================================
    # LTM Summary Retrieval
    # =========================================================================

    def _get_summarization_strategy_id(self) -> Optional[str]:
        """Get the SUMMARIZATION strategy ID from configuration or discovery."""
        if self.summarization_strategy_id:
            return self.summarization_strategy_id

        try:
            response = self.memory_client.gmcp_client.get_memory(
                memoryId=self.config.memory_id
            )
            strategies = response.get("memory", {}).get("strategies", [])
            for strategy in strategies:
                if strategy.get("type") == "SUMMARIZATION":
                    strategy_id = strategy.get("strategyId", "")
                    self.summarization_strategy_id = strategy_id
                    logger.debug(f"Discovered SUMMARIZATION strategy: {strategy_id}")
                    return strategy_id
            return None
        except Exception as e:
            logger.warning(f"Failed to get SUMMARIZATION strategy ID: {e}")
            return None

    def _retrieve_session_summaries(self) -> List[str]:
        """Retrieve session summaries from AgentCore LTM."""
        strategy_id = self._get_summarization_strategy_id()
        if not strategy_id:
            return []

        try:
            import boto3

            namespace = (
                f"/strategies/{strategy_id}"
                f"/actors/{self.config.actor_id}"
                f"/sessions/{self.config.session_id}"
            )

            client = boto3.client("bedrock-agentcore", region_name=self.region_name)
            response = client.list_memory_records(
                memoryId=self.config.memory_id,
                namespace=namespace,
                maxResults=100,
            )

            records = response.get("memoryRecordSummaries", [])
            summaries = []
            for record in records:
                content = record.get("content", {})
                if isinstance(content, dict):
                    text = content.get("text", "").strip()
                    if text:
                        summaries.append(text)

            if summaries:
                logger.info(f"Retrieved {len(summaries)} summaries from LTM")
            return summaries

        except Exception as e:
            logger.warning(f"Failed to retrieve summaries: {e}")
            return []

    def _generate_fallback_summary(self, messages: List[Dict]) -> Optional[str]:
        """Generate a fallback summary when LTM summaries are unavailable."""
        if not messages:
            return None

        try:
            key_points = []
            tools_used = set()
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                if role == "user":
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            text = block["text"]
                            first_line = text.split("\n")[0][:150]
                            if first_line and not first_line.startswith("<"):
                                key_points.append(f"- User: {first_line}")
                            break
                elif role == "assistant":
                    for block in content:
                        if isinstance(block, dict):
                            if "toolUse" in block:
                                tool_name = block["toolUse"].get("name", "")
                                if tool_name:
                                    tools_used.add(tool_name)
                            elif "text" in block:
                                text = block["text"]
                                first_line = text.split("\n")[0][:150]
                                if first_line and not first_line.startswith("<"):
                                    key_points.append(f"- Assistant: {first_line}")
                                break

            if key_points:
                parts = ["Previous conversation:"]
                parts.append("\n".join(key_points[-15:]))
                if tools_used:
                    parts.append(f"\nTools used: {', '.join(sorted(tools_used))}")
                return "\n".join(parts)

        except Exception as e:
            logger.warning(f"Failed to generate fallback summary: {e}")

        return None

    # =========================================================================
    # Post-Turn Compaction (Stage 2)
    # =========================================================================

    async def update_after_turn(self, input_tokens: int) -> None:
        """
        Update compaction state after a turn completes.

        Called by StreamCoordinator with input token count from model response.
        Triggers checkpoint creation when token threshold exceeded.
        """
        if not self.compaction_config or not self.compaction_config.enabled:
            return

        if self.compaction_state is None:
            self.compaction_state = CompactionState()

        self.compaction_state.last_input_tokens = input_tokens

        if input_tokens <= self.compaction_config.token_threshold:
            self._save_compaction_state(self.compaction_state)
            return

        logger.info(
            f"Threshold exceeded: {input_tokens:,} > "
            f"{self.compaction_config.token_threshold:,}"
        )

        if not self._valid_cutoff_indices:
            logger.info("No valid cutoff points cached, skipping checkpoint update")
            self._save_compaction_state(self.compaction_state)
            return

        total_turns = len(self._valid_cutoff_indices)
        protected_turns = self.compaction_config.protected_turns

        if total_turns <= protected_turns:
            logger.debug(
                f"Only {total_turns} turns available (need > {protected_turns}), "
                f"keeping all messages"
            )
            self._save_compaction_state(self.compaction_state)
            return

        new_checkpoint = self._valid_cutoff_indices[-protected_turns]
        current_checkpoint = self.compaction_state.checkpoint

        if new_checkpoint <= current_checkpoint:
            self._save_compaction_state(self.compaction_state)
            return

        logger.info(f"Checkpoint update: {current_checkpoint} -> {new_checkpoint}")

        # Retrieve or generate summary for compacted messages
        summaries = self._retrieve_session_summaries()
        if summaries:
            summary = "\n\n".join(summaries)
        else:
            messages_to_summarize = self._all_messages_for_summary[:new_checkpoint]
            summary = self._generate_fallback_summary(messages_to_summarize)

        self.compaction_state.checkpoint = new_checkpoint
        self.compaction_state.summary = summary
        self._save_compaction_state(self.compaction_state)

        logger.info(
            f"Compaction checkpoint set: {new_checkpoint}, "
            f"summary_length={len(summary) if summary else 0}"
        )

    # =========================================================================
    # Message Processing Helpers
    # =========================================================================

    @staticmethod
    def _filter_empty_text(message: dict) -> dict:
        """Filter out empty or invalid content blocks from a message."""
        if "content" not in message:
            return message
        content = message.get("content", [])
        if not isinstance(content, list):
            return message

        def is_valid_block(block):
            if not isinstance(block, dict):
                return False
            if "text" in block:
                text = block.get("text", "")
                return isinstance(text, str) and text.strip() != ""
            return any(key in block for key in ("toolUse", "toolResult", "image", "document"))

        filtered = [block for block in content if is_valid_block(block)]
        return {**message, "content": filtered}

    def _has_tool_result(self, message: Dict) -> bool:
        """Check if message contains toolResult block."""
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "toolResult" in block:
                    return True
        return False

    def _find_valid_cutoff_indices(self, messages: List[Dict]) -> List[int]:
        """Find valid cutoff points (user text message indices, not tool results)."""
        valid_indices = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and not self._has_tool_result(msg):
                valid_indices.append(i)
        return valid_indices

    def _find_protected_indices(self, messages: List[Dict], protected_turns: int) -> set:
        """Find message indices that should be protected from truncation."""
        if protected_turns <= 0:
            return set()

        turn_start_indices = self._find_valid_cutoff_indices(messages)
        if not turn_start_indices:
            return set()

        turns_to_protect = min(protected_turns, len(turn_start_indices))
        protected_start_idx = turn_start_indices[-turns_to_protect]
        return set(range(protected_start_idx, len(messages)))

    # =========================================================================
    # Truncation (Stage 1 Compaction)
    # =========================================================================

    def _truncate_text(self, text: str, max_length: int) -> str:
        """Truncate text with indicator."""
        if len(text) <= max_length:
            return text
        return text[:max_length] + f"\n... [truncated, {len(text) - max_length} chars removed]"

    def _truncate_tool_contents(
        self,
        messages: List[Dict],
        protected_indices: Optional[set] = None,
    ) -> tuple:
        """
        Stage 1 Compaction: Truncate long tool inputs/results and replace images.

        Returns:
            Tuple of (modified_messages, truncation_count, chars_saved)
        """
        if not self.compaction_config:
            return messages, 0, 0

        max_len = self.compaction_config.max_tool_content_length
        modified_messages = copy.deepcopy(messages)
        truncation_count = 0
        total_chars_saved = 0

        if protected_indices is None:
            protected_indices = set()

        for msg_idx, msg in enumerate(modified_messages):
            if msg_idx in protected_indices:
                continue

            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue

                # Replace image blocks with placeholder
                if "image" in block:
                    image_data = block["image"]
                    image_format = image_data.get("format", "unknown")
                    source = image_data.get("source", {})
                    original_bytes = source.get("bytes", b"")
                    original_size = len(original_bytes) if isinstance(original_bytes, bytes) else 0

                    content[block_idx] = {
                        "text": f"[Image placeholder: format={image_format}, original_size={original_size} bytes]"
                    }
                    truncation_count += 1
                    total_chars_saved += original_size

                # Truncate toolUse input
                elif "toolUse" in block:
                    tool_use = block["toolUse"]
                    tool_input = tool_use.get("input", {})

                    if isinstance(tool_input, dict):
                        input_str = json.dumps(tool_input, ensure_ascii=False)
                        if len(input_str) > max_len:
                            original_len = len(input_str)
                            tool_use["input"] = {"_truncated": self._truncate_text(input_str, max_len)}
                            truncation_count += 1
                            total_chars_saved += original_len - max_len
                    elif isinstance(tool_input, str) and len(tool_input) > max_len:
                        original_len = len(tool_input)
                        tool_use["input"] = self._truncate_text(tool_input, max_len)
                        truncation_count += 1
                        total_chars_saved += original_len - max_len

                # Truncate toolResult content
                elif "toolResult" in block:
                    tool_result = block["toolResult"]
                    result_content = tool_result.get("content", [])

                    if isinstance(result_content, list):
                        for result_idx, result_block in enumerate(result_content):
                            if not isinstance(result_block, dict):
                                continue

                            if "image" in result_block:
                                image_data = result_block["image"]
                                image_format = image_data.get("format", "unknown")
                                source = image_data.get("source", {})
                                original_bytes = source.get("bytes", b"")
                                original_size = (
                                    len(original_bytes) if isinstance(original_bytes, bytes) else 0
                                )

                                result_content[result_idx] = {
                                    "text": f"[Image placeholder: format={image_format}, original_size={original_size} bytes]"
                                }
                                truncation_count += 1
                                total_chars_saved += original_size

                            elif "text" in result_block:
                                text = result_block["text"]
                                if len(text) > max_len:
                                    original_len = len(text)
                                    result_block["text"] = self._truncate_text(text, max_len)
                                    truncation_count += 1
                                    total_chars_saved += original_len - max_len

                            elif "json" in result_block:
                                json_content = result_block["json"]
                                json_str = json.dumps(json_content, ensure_ascii=False)
                                if len(json_str) > max_len:
                                    original_len = len(json_str)
                                    result_block.pop("json")
                                    result_block["text"] = self._truncate_text(json_str, max_len)
                                    truncation_count += 1
                                    total_chars_saved += original_len - max_len

        if truncation_count > 0:
            logger.info(f"Truncated {truncation_count} items, saved ~{total_chars_saved:,} chars")

        return modified_messages, truncation_count, total_chars_saved

    # =========================================================================
    # Summary Injection
    # =========================================================================

    def _prepend_summary_to_first_message(
        self,
        messages: List[Dict],
        summary: str,
    ) -> List[Dict]:
        """Prepend summary to the first user message's text content."""
        if not messages or not summary:
            return messages

        modified_messages = copy.deepcopy(messages)
        first_msg = modified_messages[0]

        if first_msg.get("role") != "user":
            return messages

        summary_prefix = (
            "<conversation_summary>\n"
            "The following is a summary of our previous conversation:\n\n"
            f"{summary}\n\n"
            "Please continue the conversation with this context in mind.\n"
            "</conversation_summary>\n\n"
        )

        content = first_msg.get("content", [])
        if isinstance(content, list) and len(content) > 0:
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = summary_prefix + block["text"]
                    return modified_messages

            # No text block found, insert one
            content.insert(0, {"text": summary_prefix.rstrip()})
            first_msg["content"] = content

        return modified_messages

    # =========================================================================
    # Convenience — flush is a no-op (SDK handles persistence via hooks)
    # =========================================================================

    def flush(self) -> Optional[int]:
        """Return the last message index. SDK handles actual persistence via hooks."""
        if self.message_count > 0:
            return self.message_count - 1
        return None
