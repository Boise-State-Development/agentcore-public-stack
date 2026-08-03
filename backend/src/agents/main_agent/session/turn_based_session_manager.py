"""
Compacting Session Manager for AgentCore Memory

Extends AgentCoreMemorySessionManager (subclass, not wrapper) so the SDK's
standard hook wiring (register_hooks) handles message persistence, sync, and
LTM retrieval correctly.  We only override initialize() to layer on compaction
after the SDK finishes its standard session restore.

Compaction Strategy (two-feature approach):
- Stage 1: Tool content truncation — applied only below the persisted
  truncation anchor, which moves at checkpoint advances or when the Bedrock
  prompt cache has already expired between turns
- Stage 2: Checkpoint + Summary — triggered when token threshold exceeded

Byte-stability contract: between compaction-state changes, restoring the same
stored history must produce byte-identical ``agent.messages``. Bedrock prompt
caching requires an exact prefix match, so any per-restore mutation of older
turns (e.g. a sliding truncation window) breaks the cached prefix and forces a
full re-write (~$2.5/MTok on a 35k–150k prefix) nearly every turn — far more
expensive than the read tokens truncation saves.

Based on: https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import copy
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from agents.main_agent.config.constants import EnvVars

from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

from .compaction_models import CompactionState, CompactionConfig, CompactionResult

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
    - Tool content truncation below a persisted anchor (cache-safe: history
      is byte-stable between compaction-state changes)
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
        logger.info(f"TurnBasedSessionManager.initialize() called for agent_id={agent.agent_id}")

        # Let the SDK handle all session restore logic:
        # - read/create agent in session repository
        # - restore agent state, internal state, conversation manager state
        # - load messages from AgentCore Memory
        # - fix broken tool-use histories
        super().initialize(agent, **kwargs)

        self._total_message_count_at_init = len(agent.messages)
        self.message_count = self._total_message_count_at_init

        # Slice 0 diagnostic: confirm what restored history a turn actually
        # sees at init. Decisive for the max_tokens "Continue" flow — on the
        # continuation turn the restored tail must be the truncated assistant
        # message for the model to resume rather than restart. One concise
        # line per init (init is not hot-path frequent).
        try:
            _msgs = agent.messages or []
            if _msgs:
                _last = _msgs[-1]
                _last_role = _last.get("role")
                _last_text = ""
                for _blk in _last.get("content", []) or []:
                    if isinstance(_blk, dict) and isinstance(_blk.get("text"), str):
                        _last_text += _blk["text"]
                logger.info(
                    "Restore @init: %d message(s); last role=%s, last text len=%d",
                    len(_msgs), _last_role, len(_last_text),
                )
            else:
                logger.info("Restore @init: 0 messages (new or empty-at-init session)")
        except Exception:
            logger.debug("Restore @init: diagnostic log failed", exc_info=True)

        # Initialize compaction defaults
        self.compaction_state = CompactionState()
        self._valid_cutoff_indices = []
        self._all_messages_for_summary = []

        # The cutoff cache is also re-derived per turn in `update_after_turn`,
        # so it stays correct regardless of what `super().initialize()` loaded.
        #
        # `agent.messages` is normally populated by now: the SDK takes its
        # RESTORE branch on any subsequent turn of an existing session, on a
        # cold container as readily as a warm one. That branch needs
        # `read_session()` and `read_agent()` to both return non-None, which
        # holds because `persistence_mode` defaults to FULL (turn 1 writes the
        # SESSION and AGENT metadata events these look up) and `agent_id` is
        # always Strands' default `"default"` — we never pass one. All three
        # reads are `list_events` calls against AgentCore Memory keyed on
        # memory/actor/session, so restore carries no in-process state and does
        # not depend on the agent cache. The `Restore @init` line logged just
        # above reports what actually landed.
        #
        # The empty case is still reachable — a genuinely new session, or a
        # restore that returned nothing — so the guard stays.
        if not agent.messages:
            return

        # Strip document bytes from history unconditionally — regardless of
        # whether compaction is enabled. Document content blocks with inline
        # bytes must never survive in restored history because Bedrock rejects
        # any request where two document blocks share the same sanitized name
        # across the conversation (ValidationException: "Messages can't contain
        # duplicate document names"). This can happen on what feels like a
        # "first turn" when the user returns to an existing session URL and
        # re-attaches a file with the same name as one from a prior visit.
        # The [Attached files: …] text marker already in the user message
        # preserves the reference for the model without re-sending bytes.
        # Images are handled the same way inside _truncate_tool_contents, but
        # that method is gated on compaction being enabled — this one is not.
        try:
            agent.messages = self._strip_document_bytes(agent.messages)
        except Exception as e:
            logger.warning(f"Document byte stripping failed, continuing: {e}", exc_info=True)

        # Drop empty/unrecognized content blocks from restored history. The
        # write side runs `_filter_empty_text` in `append_message`, but history
        # restored from AgentCore Memory bypasses it — a single block that comes
        # back without a recognized Bedrock discriminator (an empty {} block, or
        # a key lost on the memory serialization round-trip) makes Bedrock reject
        # EVERY subsequent turn with "messages.N.content.M.type: Field required",
        # permanently bricking the session. Runs unconditionally, mirroring
        # `_strip_document_bytes` / `_repair_restored_history`.
        try:
            agent.messages = self._sanitize_restored_content_blocks(agent.messages)
        except Exception as e:
            logger.warning(f"Content-block sanitize failed, continuing: {e}", exc_info=True)

        if not self.compaction_config or not self.compaction_config.enabled:
            self._repair_restored_history(agent)
            return

        try:
            self._apply_compaction(agent)
        except Exception as e:
            logger.error(f"Compaction failed, using full history: {e}", exc_info=True)
            # These defaults never reach DynamoDB: `update_after_turn` re-reads
            # the persisted state and only adopts what is at least as advanced,
            # so the real checkpoint/anchor survive a failed compaction.
            self.compaction_state = CompactionState()
            self._valid_cutoff_indices = []
            self._all_messages_for_summary = []

        # Repair tool-use/tool-result pairing and role alternation on the FINAL
        # restored list — after compaction slicing/truncation — so it is always
        # the exact history sent to Bedrock. Runs unconditionally (independent of
        # compaction), mirroring `_strip_document_bytes`, because the corruption
        # it fixes originates on the write side and can exist regardless of
        # whether compaction is enabled.
        self._repair_restored_history(agent)

    # =========================================================================
    # Compaction — applied after SDK session restore
    # =========================================================================

    def _apply_compaction(self, agent: "Agent") -> None:
        """
        Apply compaction to agent.messages after SDK session restore.

        Modifies agent.messages in-place to:
        1. Skip old messages (checkpoint-based)
        2. Prepend conversation summary
        3. Truncate tool content strictly below the persisted truncation anchor

        Byte-stability contract: this derivation must be a pure function of
        (stored history, persisted compaction state). Truncation is therefore
        driven ONLY by ``compaction_state.truncation_anchor`` — never by a
        window computed from the current message count, which would re-mutate
        an older turn on every restore and break Bedrock's exact-prefix cache
        match. The anchor moves at checkpoint advances (``update_after_turn``,
        where the slice already pays the one cache re-write) and
        opportunistically here when the prompt cache has already expired.
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

        # Advance the truncation anchor only while the prompt cache is
        # already cold — the prefix re-write is unavoidable then, so pending
        # truncations are free. Persisted before use so subsequent restores
        # derive the identical history.
        self._maybe_advance_truncation_anchor()

        # Apply checkpoint: skip old messages, prepend summary
        checkpoint = self.compaction_state.checkpoint
        stage = "none"
        offset = 0

        if checkpoint > 0 and checkpoint < len(all_messages):
            messages_to_process = all_messages[checkpoint:]
            offset = checkpoint

            summary = self.compaction_state.summary
            if summary and messages_to_process:
                messages_to_process = self._prepend_summary_to_first_message(
                    messages_to_process, summary
                )
            stage = "checkpoint"
        else:
            messages_to_process = all_messages

        # Truncate only messages strictly below the anchor (absolute index),
        # translated into post-slice coordinates via the checkpoint offset.
        truncation_count = 0
        anchor = min(max(self.compaction_state.truncation_anchor, offset), len(all_messages))
        if anchor > offset:
            protected_indices = set(range(anchor - offset, len(messages_to_process)))
            messages_to_process, truncation_count, _ = self._truncate_tool_contents(
                messages_to_process, protected_indices=protected_indices
            )
            if truncation_count > 0:
                stage = "checkpoint+truncation" if stage == "checkpoint" else "truncation"

        agent.messages = messages_to_process

        logger.info(
            f"Compaction initialized: stage={stage}, "
            f"original={self._total_message_count_at_init}, "
            f"final={len(agent.messages)}, "
            f"anchor={self.compaction_state.truncation_anchor}, "
            f"truncations={truncation_count}"
        )

    def _maybe_advance_truncation_anchor(self) -> None:
        """Advance the truncation anchor while the prompt cache is already cold.

        The anchor normally moves only when the checkpoint advances (that
        slice already forces one full prefix re-write, so folding truncation
        into it costs nothing extra). But when more than
        ``cache_ttl_seconds`` have passed since the previous turn, the
        Bedrock prompt-cache entry has expired anyway — the next call
        re-writes the prefix regardless — so the anchor can slide up to the
        protected-turns boundary for free. Persisted immediately so every
        subsequent restore derives the identical truncated history.
        """
        state = self.compaction_state
        config = self.compaction_config
        if not self._cache_window_expired(state.updated_at, config.cache_ttl_seconds):
            return

        cutoffs = self._valid_cutoff_indices
        if len(cutoffs) <= config.protected_turns:
            return

        sliding_anchor = cutoffs[-config.protected_turns]
        if sliding_anchor <= max(state.truncation_anchor, state.checkpoint):
            return

        logger.info(
            "Truncation anchor advance (cache expired): %d -> %d",
            state.truncation_anchor, sliding_anchor,
        )
        state.truncation_anchor = sliding_anchor
        self._save_compaction_state(state)

    @staticmethod
    def _cache_window_expired(updated_at: Optional[str], ttl_seconds: int) -> bool:
        """True when the previous turn is older than the prompt-cache TTL.

        ``updated_at`` is stamped by ``_save_compaction_state`` on every turn,
        so it is a faithful proxy for the previous model call. Unparseable or
        missing timestamps return False (conservative: assume the cache may
        still be warm and leave history untouched).
        """
        if not updated_at:
            return False
        try:
            last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() > ttl_seconds

    # =========================================================================
    # Compaction State Persistence
    # =========================================================================

    def _get_dynamodb_table(self):
        """Lazy initialization of DynamoDB table for compaction state."""
        if TurnBasedSessionManager._dynamodb_table is None:
            table_name = os.environ.get(EnvVars.DYNAMODB_SESSIONS_METADATA_TABLE)
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

    def _adopt_persisted_compaction_state(self) -> None:
        """Re-read persisted compaction state at the top of every turn (#751).

        **One session can be served by more than one session manager.** The agent
        cache keys on *configuration*, so an ``@``-mention turn (Marketplace D11)
        builds a second ``Agent`` — and each ``Agent`` builds its own
        ``TurnBasedSessionManager`` — for a session that already has one. Both
        persist to the same DynamoDB row and neither knows the other exists.

        This used to lazy-load only when ``initialize()`` had skipped the load,
        which means a cache-*hit* turn kept whatever state its instance was built
        with. ``initialize()`` never re-runs on a hit — the same property that
        forked the conversation in #741 — so the stale instance would then
        ``_save_compaction_state`` its old
        ``checkpoint`` and ``truncation_anchor`` straight over the newer ones the
        sibling had just written. Every exit path of ``update_after_turn`` saves,
        including the "nothing to do" ones, so the clobber did not need compaction
        to actually fire.

        ⚠️ **This is a cost bug before it is a correctness bug.** The truncation
        anchor is what keeps the restored prefix byte-stable (see the prompt-cache
        contract in ``CLAUDE.md``). Moving it backwards re-truncates messages that
        were previously sent whole, which rewrites the prefix — a full cache write
        at the $2.50/MTok premium over a 35k–150k-token prefix, on a turn where
        nothing about the conversation appeared to change.

        Re-reading rather than sharing the state object: the sibling may live in
        another replica, where object aliasing (#750's fix for the message list)
        reaches nothing. The read is one GSI query, and ``_save_compaction_state``
        already does an identical one immediately after, so this roughly doubles a
        cost that is already noise next to a model call. Turns are serialized per
        session by the single-flight lease, so read-modify-write is safe.

        **State never moves backwards.** A load failure is indistinguishable from
        "nothing persisted" — both return a default ``CompactionState`` — so
        blindly adopting the result would let a transient DynamoDB error zero a
        real checkpoint, which is the very clobber this exists to prevent. The
        guards below mirror ``_adopt_session_conversation``'s "keep the longer
        history" rule: adopt the persisted record only when it is at least as far
        along, and carry the anchor forward either way.
        """
        persisted = self._load_compaction_state()
        current = self.compaction_state

        if current is None:
            self.compaction_state = persisted
            return

        if persisted.checkpoint < current.checkpoint:
            # Either a sibling has not caught up, or the load failed and handed
            # back defaults. Keeping ours is correct in both cases: the checkpoint
            # is monotonic within a session, so a lower one is never news.
            logger.info(
                "Compaction state: persisted checkpoint %d is behind the live one "
                "%d; keeping the live state",
                persisted.checkpoint, current.checkpoint,
            )
            return

        if persisted.checkpoint > current.checkpoint:
            logger.info(
                "Compaction state: adopting a sibling's advance (checkpoint %d -> "
                "%d, anchor %d -> %d)",
                current.checkpoint, persisted.checkpoint,
                current.truncation_anchor, persisted.truncation_anchor,
            )

        # Clamp forward even at an equal checkpoint: the anchor also advances on
        # prompt-cache expiry (``_maybe_advance_truncation_anchor``), so this
        # instance can hold a newer anchor under the same checkpoint. Taking the
        # max keeps truncation monotonic, which is what the prefix depends on.
        persisted.truncation_anchor = max(
            persisted.truncation_anchor, current.truncation_anchor
        )
        self.compaction_state = persisted

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

    async def update_after_turn(
        self,
        input_tokens: int,
        current_messages: Optional[List[Dict]] = None,
    ) -> Optional[CompactionResult]:
        """
        Update compaction state after a turn completes.

        Called by StreamCoordinator with input token count from model response.
        Triggers checkpoint creation when token threshold exceeded.

        Returns a ``CompactionResult`` when the checkpoint advances on this
        turn so the caller can emit a ``compaction`` SSE event; otherwise
        returns ``None``.

        ``current_messages`` is the agent's live message list. When provided,
        the cutoff cache is re-derived from it so compaction works even when
        AgentCoreMemory loads messages via hooks (skipping the initialize-time
        prime path).
        """
        if not self.compaction_config or not self.compaction_config.enabled:
            return None

        # Re-read persisted state before touching it — on EVERY turn, not just
        # the first. See ``_adopt_persisted_compaction_state`` (#751).
        self._adopt_persisted_compaction_state()

        self.compaction_state.last_input_tokens = input_tokens

        if input_tokens <= self.compaction_config.token_threshold:
            self._save_compaction_state(self.compaction_state)
            return None

        logger.info(
            f"Threshold exceeded: {input_tokens:,} > "
            f"{self.compaction_config.token_threshold:,}"
        )

        # Refresh cutoff cache from the agent's current messages — at this
        # point in the turn lifecycle the user message + assistant response
        # have been added, so this is authoritative even if `initialize()`
        # ran with an empty list.
        if current_messages:
            self._valid_cutoff_indices = self._find_valid_cutoff_indices(current_messages)
            self._all_messages_for_summary = current_messages[:]
            logger.info(
                f"Refreshed cutoff cache from current messages: "
                f"{len(self._valid_cutoff_indices)} valid cutoffs across "
                f"{len(current_messages)} messages"
            )

        if not self._valid_cutoff_indices:
            logger.info("No valid cutoff points cached, skipping checkpoint update")
            self._save_compaction_state(self.compaction_state)
            return None

        total_turns = len(self._valid_cutoff_indices)
        protected_turns = self.compaction_config.protected_turns

        if total_turns <= protected_turns:
            logger.debug(
                f"Only {total_turns} turns available (need > {protected_turns}), "
                f"keeping all messages"
            )
            self._save_compaction_state(self.compaction_state)
            return None

        new_checkpoint = self._valid_cutoff_indices[-protected_turns]
        current_checkpoint = self.compaction_state.checkpoint

        if new_checkpoint <= current_checkpoint:
            self._save_compaction_state(self.compaction_state)
            return None

        logger.info(f"Checkpoint update: {current_checkpoint} -> {new_checkpoint}")

        # Count turns rolled into the summary on THIS event (delta, not
        # cumulative) — each inline divider stands on its own.
        summarized_turns = sum(
            1 for idx in self._valid_cutoff_indices
            if current_checkpoint < idx <= new_checkpoint
        )

        # Retrieve or generate summary for compacted messages
        summaries = self._retrieve_session_summaries()
        if summaries:
            summary = "\n\n".join(summaries)
        else:
            messages_to_summarize = self._all_messages_for_summary[:new_checkpoint]
            summary = self._generate_fallback_summary(messages_to_summarize)

        self.compaction_state.checkpoint = new_checkpoint
        # The anchor rides the checkpoint: everything the slice retains stays
        # byte-identical until the next compaction-state change, so the single
        # mutation (slice + summary) is paid with exactly one cache re-write.
        self.compaction_state.truncation_anchor = max(
            self.compaction_state.truncation_anchor, new_checkpoint
        )
        self.compaction_state.summary = summary
        # Running total persisted alongside the rest of the compaction state
        # so a refresh can rehydrate the end-of-conversation summary indicator.
        self.compaction_state.total_summarized_turns += summarized_turns
        self._save_compaction_state(self.compaction_state)

        logger.info(
            f"Compaction checkpoint set: {new_checkpoint}, "
            f"summary_length={len(summary) if summary else 0}, "
            f"summarized_turns={summarized_turns}, "
            f"total_summarized_turns={self.compaction_state.total_summarized_turns}"
        )

        return CompactionResult(
            previous_checkpoint=current_checkpoint,
            new_checkpoint=new_checkpoint,
            summarized_turns=summarized_turns,
            input_tokens=input_tokens,
        )

    # =========================================================================
    # Message Processing Helpers
    # =========================================================================

    # Top-level keys for content blocks Bedrock Converse recognizes.
    # Mirrors Strands BedrockModel._format_request_message_content
    # (strands/models/bedrock.py). reasoningContent is critical for
    # Anthropic extended-thinking + tool-use round-tripping: the block
    # carries a `signature` field that must be replayed verbatim while a
    # tool-use cycle is open.
    _BEDROCK_CONTENT_BLOCK_KEYS = frozenset({
        "cachePoint",
        "citationsContent",
        "document",
        "guardContent",
        "image",
        "reasoningContent",
        "text",
        "toolResult",
        "toolUse",
        "video",
    })

    @staticmethod
    def _filter_empty_text(message: dict) -> dict:
        """Drop empty text blocks; preserve every other Bedrock-recognized block.

        Empty/whitespace-only ``text`` blocks must be dropped — Bedrock Converse
        rejects them. Every other recognized content block is passed through
        unchanged. Blocks whose top-level key is not recognized are dropped and
        logged so silent stripping (e.g. when Bedrock adds a new block type)
        is observable.
        """
        if "content" not in message:
            return message
        content = message.get("content", [])
        if not isinstance(content, list):
            return message

        filtered = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                text = block.get("text", "")
                if isinstance(text, str) and text.strip() != "":
                    filtered.append(block)
                continue
            if any(key in block for key in TurnBasedSessionManager._BEDROCK_CONTENT_BLOCK_KEYS):
                filtered.append(block)
            else:
                logger.warning(
                    "Dropping unrecognized content block (keys=%s) before persistence. "
                    "If Bedrock has added a new block type, update _BEDROCK_CONTENT_BLOCK_KEYS.",
                    sorted(block.keys()),
                )
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

    # =========================================================================
    # Truncation (Stage 1 Compaction)
    # =========================================================================

    def _truncate_text(self, text: str, max_length: int) -> str:
        """Truncate text with indicator."""
        if len(text) <= max_length:
            return text
        return text[:max_length] + f"\n... [truncated, {len(text) - max_length} chars removed]"

    def _sanitize_restored_content_blocks(self, messages: List[Dict]) -> List[Dict]:
        """Drop empty/unrecognized content blocks from restored history.

        ``append_message`` runs ``_filter_empty_text`` on the write side, but
        history restored from AgentCore Memory bypasses it. A single block that
        comes back without a recognized Bedrock discriminator — an empty ``{}``
        block, an empty/whitespace ``text`` block, or a key dropped on the
        memory serialization round-trip — triggers Bedrock's
        "messages.N.content.M.type: Field required" ValidationException, which
        fails every subsequent turn on the session. This reuses the same
        recognized-key filter as the write path and additionally drops any
        message left with no content (``_repair_restored_history`` runs after
        and fixes any role-alternation gap the drop introduces).
        """
        sanitized: List[Dict] = []
        dropped_blocks = 0
        dropped_messages = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            original = msg.get("content", [])
            cleaned = self._filter_empty_text(msg)
            content = cleaned.get("content", [])
            if isinstance(original, list) and isinstance(content, list):
                dropped_blocks += max(0, len(original) - len(content))
            if isinstance(content, list) and len(content) == 0:
                dropped_messages += 1
                continue
            sanitized.append(cleaned)

        if dropped_blocks or dropped_messages:
            logger.warning(
                "Restore sanitize: dropped %d invalid content block(s) and %d "
                "empty message(s) from restored history for session %s",
                dropped_blocks, dropped_messages, self.config.session_id,
            )
        return sanitized

    def _strip_document_bytes(self, messages: List[Dict]) -> List[Dict]:
        """Replace document content blocks' inline bytes with a text placeholder.

        Called unconditionally on every session restore — independent of whether
        compaction is enabled. Document blocks with ``source.bytes`` must never
        survive in restored history because Bedrock rejects any request where two
        document blocks share the same sanitized name across the conversation
        (ValidationException: "Messages can't contain duplicate document names").

        Images are handled the same way inside ``_truncate_tool_contents``, but
        that method is gated on compaction being enabled. This one is not.

        The ``[Attached files: …]`` text marker already present in the user
        message preserves the reference for the model without re-sending bytes.
        """
        stripped_messages = copy.deepcopy(messages)
        strip_count = 0

        for msg in stripped_messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block_idx, block in enumerate(content):
                if not isinstance(block, dict) or "document" not in block:
                    continue
                doc_data = block["document"]
                source = doc_data.get("source", {})
                # Only replace blocks that carry inline bytes — s3Location
                # blocks (enhancement #401) have no bytes to strip and are
                # safe to leave as-is since they don't accumulate in history.
                if "bytes" not in source:
                    continue
                doc_name = doc_data.get("name", "unknown")
                doc_format = doc_data.get("format", "unknown")
                original_bytes = source.get("bytes", b"")
                original_size = len(original_bytes) if isinstance(original_bytes, bytes) else 0
                content[block_idx] = {
                    "text": f"[Document placeholder: name={doc_name}, format={doc_format}, original_size={original_size} bytes]"
                }
                strip_count += 1

        if strip_count > 0:
            logger.debug(f"Stripped inline bytes from {strip_count} document block(s) in history")

        return stripped_messages

    # =========================================================================
    # Tool-pairing / role-alternation repair (restore-time safety net)
    # =========================================================================

    def _repair_restored_history(self, agent: "Agent") -> None:
        """Repair tool-use/tool-result pairing on the restored history in place.

        Bedrock Converse rejects any request whose history violates its
        structural rules — most relevantly:
        "The number of toolResult blocks at messages.N.content exceeds the
        number of toolUse blocks of previous turn." A single such violation
        anywhere in stored history makes EVERY subsequent turn on that session
        fail, permanently bricking the conversation.

        The corruption originates on the write side: a turn with parallel tool
        calls in flight that is interrupted (user Stop / dropped connection) or
        retried can persist duplicated tool-result messages, tool-result
        messages reordered away from their tool-use turn (assistant, assistant,
        user, user), or tool-result messages orphaned after a synthetic error
        turn. The Strands SDK's own ``_fix_broken_tool_use`` only rebuilds the
        single message immediately after each tool-use turn, so it does not
        repair duplicates, reordering, or orphans — and it may not run at all on
        the AgentCore Memory restore path. This is the unconditional safety net.

        Runs on the FINAL ``agent.messages`` (after compaction), mirroring
        ``_strip_document_bytes``. Best-effort: any failure logs and leaves the
        history untouched rather than breaking the turn. Kill switch:
        ``AGENTCORE_MEMORY_HISTORY_REPAIR_ENABLED=false``.
        """
        if os.environ.get(EnvVars.HISTORY_REPAIR_ENABLED, "").strip().lower() == "false":
            return
        try:
            messages = agent.messages
            if not messages:
                return
            repaired, fixed = self._repair_tool_pairing(messages)
            if fixed > 0:
                logger.warning(
                    "Restore repair: fixed %d tool-pairing/alternation "
                    "violation(s) in restored history (%d -> %d messages) "
                    "for session %s",
                    fixed, len(messages), len(repaired), self.config.session_id,
                )
                agent.messages = repaired
        except Exception as e:
            logger.error(f"Restore history repair failed, using history as-is: {e}", exc_info=True)

    @staticmethod
    def _block_keys(message: Dict) -> tuple:
        """(has_tool_use, has_tool_result) for a message's content blocks."""
        has_use = has_result = False
        for block in message.get("content", []) or []:
            if isinstance(block, dict):
                if "toolUse" in block:
                    has_use = True
                elif "toolResult" in block:
                    has_result = True
        return has_use, has_result

    @staticmethod
    def _tool_use_ids(message: Dict) -> List[str]:
        return [
            b["toolUse"]["toolUseId"]
            for b in message.get("content", []) or []
            if isinstance(b, dict) and "toolUse" in b and "toolUseId" in b["toolUse"]
        ]

    @staticmethod
    def _tool_result_ids(message: Dict) -> List[str]:
        return [
            b["toolResult"]["toolUseId"]
            for b in message.get("content", []) or []
            if isinstance(b, dict) and "toolResult" in b and "toolUseId" in b["toolResult"]
        ]

    @classmethod
    def _count_pairing_violations(cls, messages: List[Dict]) -> int:
        """Count Bedrock structural violations: consecutive same-role turns,
        orphaned/mismatched toolResults, and tool-use turns not answered by the
        next turn. Zero means the history is already valid — repair can no-op.
        """
        violations = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            prev = messages[i - 1] if i > 0 else None
            if prev is not None and prev.get("role") == role:
                violations += 1
            has_use, has_result = cls._block_keys(msg)
            if has_result:
                prev_use = cls._tool_use_ids(prev) if prev is not None else []
                if not prev_use or set(cls._tool_result_ids(msg)) != set(prev_use):
                    violations += 1
            if has_use and i + 1 < len(messages):
                if set(cls._tool_use_ids(msg)) != set(cls._tool_result_ids(messages[i + 1])):
                    violations += 1
        return violations

    @classmethod
    def _repair_tool_pairing(cls, messages: List[Dict]) -> tuple:
        """Return ``(repaired_messages, violations_fixed)``.

        Rebuilds a Bedrock-valid history: every assistant tool-use turn is
        immediately followed by exactly one user turn carrying one toolResult
        per toolUseId (missing ones synthesized as errors), duplicate/orphaned
        toolResult turns are dropped, and consecutive same-role turns are
        merged. When the history is already valid the input list is returned
        unchanged (identity) with a count of 0, so healthy sessions pay only a
        scan.
        """
        violations = cls._count_pairing_violations(messages)
        if violations == 0:
            return messages, 0

        # Global toolUseId -> toolResult block (last occurrence wins: the most
        # recent result for an id is the authoritative one).
        result_by_id: Dict[str, Dict] = {}
        for msg in messages:
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and "toolResult" in block:
                    tid = block["toolResult"].get("toolUseId")
                    if tid:
                        result_by_id[tid] = block

        def missing_result(tid: str) -> Dict:
            return {
                "toolResult": {
                    "toolUseId": tid,
                    "content": [{"text": "[tool result unavailable: the turn was interrupted]"}],
                    "status": "error",
                }
            }

        # Forward pass: emit each assistant tool-use turn followed by a freshly
        # built result turn; drop standalone toolResult-only turns (their blocks
        # are re-emitted from the map at the correct slot).
        rebuilt: List[Dict] = []
        last_index = len(messages) - 1
        for i, msg in enumerate(messages):
            has_use, has_result = cls._block_keys(msg)

            if has_result and not has_use:
                # Standalone tool-result turn: keep only non-toolResult content
                # (rare mixed text), drop the results themselves.
                residual = [b for b in msg.get("content", []) if not (isinstance(b, dict) and "toolResult" in b)]
                if residual:
                    rebuilt.append({"role": msg.get("role", "user"), "content": residual})
                continue

            if has_use and i < last_index:
                rebuilt.append(msg)
                use_ids = cls._tool_use_ids(msg)
                rebuilt.append({
                    "role": "user",
                    "content": [result_by_id.get(t, missing_result(t)) for t in use_ids],
                })
                continue

            # Trailing tool-use turn (last message) or any non-tool turn: emit
            # as-is. The trailing case is deliberately left for prompt-arrival
            # handling, matching the SDK's own repair.
            rebuilt.append(msg)

        # Merge consecutive same-role turns. Guard only on the PREVIOUS turn
        # having no toolUse: a tool-use turn must stay immediately adjacent to
        # its result turn, so it can never absorb a following turn — but a
        # text turn may legitimately merge into a following tool-use turn
        # (assistant text + toolUse in one turn is valid), keeping the toolUse
        # at the tail so its result turn still follows. In a merged user turn,
        # toolResult blocks come first.
        merged: List[Dict] = []
        for msg in rebuilt:
            prev = merged[-1] if merged else None
            if (
                prev is not None
                and prev.get("role") == msg.get("role")
                and not cls._block_keys(prev)[0]  # prev has no toolUse
            ):
                combined = list(prev.get("content", [])) + list(msg.get("content", []))
                if msg.get("role") == "user":
                    combined = sorted(combined, key=lambda b: 0 if isinstance(b, dict) and "toolResult" in b else 1)
                prev["content"] = combined
            else:
                merged.append({"role": msg.get("role"), "content": list(msg.get("content", []))})

        return merged, violations

    def _truncate_tool_contents(
        self,
        messages: List[Dict],
        protected_indices: Optional[set] = None,
    ) -> tuple:
        """
        Stage 1 Compaction: Truncate long tool inputs/results and replace images.

        Note: document block byte-stripping is handled unconditionally by
        ``_strip_document_bytes`` (called from ``initialize``) and is therefore
        not repeated here.

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
