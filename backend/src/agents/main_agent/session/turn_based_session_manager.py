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
- Stage 2: Checkpoint + Summary — triggered when the turn's context exceeds
  the model-relative ceiling (compaction_policy.py; the cut lands retained
  history at the policy floor, and a cut disarms the trigger until the
  context drops back under the ceiling — spec:
  docs/specs/compaction-model-relative-thresholds.md)

Byte-stability contract: between compaction-state changes, restoring the same
stored history must produce byte-identical ``agent.messages``. Bedrock prompt
caching requires an exact prefix match, so any per-restore mutation of older
turns (e.g. a sliding truncation window) breaks the cached prefix and forces a
full re-write of a 35k–150k prefix nearly every turn, at the cache-write
premium — 1.25x the model's own base input rate, not a flat per-MTok figure;
see the prompt-cache contract in CLAUDE.md — far more expensive than the read
tokens truncation saves.

Based on: https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

from agents.main_agent.config.constants import Defaults, EnvVars

from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

from .compaction_models import CompactionState, CompactionConfig, CompactionResult
from .compaction_policy import CompactionPolicy, choose_checkpoint
from .compaction_summary import bound_summary

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

# Bound on a single RetrieveMemoryRecords attempt from the per-message
# long-term memory retrieval hook (see `_get_retrieval_client`). Seconds.
MEMORY_RETRIEVAL_TIMEOUT_ENV = "MEMORY_RETRIEVAL_TIMEOUT_SECONDS"
MEMORY_RETRIEVAL_TIMEOUT_DEFAULT = 2.0
# Attempts per retrieval, including the first. 1 = never retry a throttle.
MEMORY_RETRIEVAL_MAX_ATTEMPTS_ENV = "MEMORY_RETRIEVAL_MAX_ATTEMPTS"
MEMORY_RETRIEVAL_MAX_ATTEMPTS_DEFAULT = 1
# RetrieveMemoryRecords rejects a searchQuery longer than this
# (ValidationException), which dropped retrieval for long pasted messages.
MEMORY_RETRIEVAL_QUERY_MAX_CHARS = 10_000
_MEMORY_RETRIEVAL_THROTTLE_CODES = ("ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException")
# Serializes replacing the retrieval client: boto3 client creation on the
# default session is not thread-safe, and the namespaces retrieve in parallel.
_RETRIEVAL_CLIENT_LOCK = threading.Lock()


def memory_retrieval_timeout_seconds() -> float:
    """Per-attempt timeout for long-term memory retrieval, from the environment."""
    raw = os.environ.get(MEMORY_RETRIEVAL_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else MEMORY_RETRIEVAL_TIMEOUT_DEFAULT
    except ValueError:
        value = MEMORY_RETRIEVAL_TIMEOUT_DEFAULT
    return value if value > 0 else MEMORY_RETRIEVAL_TIMEOUT_DEFAULT


def memory_retrieval_max_attempts() -> int:
    """Attempts per long-term memory retrieval (including the first)."""
    raw = os.environ.get(MEMORY_RETRIEVAL_MAX_ATTEMPTS_ENV, "")
    try:
        value = int(raw) if raw else MEMORY_RETRIEVAL_MAX_ATTEMPTS_DEFAULT
    except ValueError:
        value = MEMORY_RETRIEVAL_MAX_ATTEMPTS_DEFAULT
    return value if value >= 1 else MEMORY_RETRIEVAL_MAX_ATTEMPTS_DEFAULT


def memory_retrieval_query(text: str) -> str:
    """The user's text, cut to what ``RetrieveMemoryRecords`` accepts.

    Counts UTF-16 code units, not code points: that is never more than the
    service's own count, so an emoji-heavy message cannot slip past the cap.
    """
    encoded = text.encode("utf-16-le")
    limit = MEMORY_RETRIEVAL_QUERY_MAX_CHARS * 2
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-16-le", errors="ignore")

#: Compaction decisions the per-call ledger will record. Reserved kinds exist
#: so a scheduling policy can report them without a schema change.
#: Compaction-ledger event kinds. The ``document_*`` kinds are the document
#: lifecycle (docs/specs/document-context-offload.md §6.1): ``document_stripped``
#: — restore replaced inline documents with contentless placeholders (the
#: defect PR-3 fixes; recorded from PR-1 so its reach is measured before the
#: fix lands); ``document_rehydrated`` — restore replaced them with a digest
#: plus a live ``document_read`` handle (PR-3); ``document_offload`` — the
#: post-turn trigger swapped an inline document for its digest, with the
#: cache gap at the moment it fired (PR-4).
#: ``truncation_anchor`` — the restore-time anchor slid forward because the
#: prompt cache had already expired (offload spec PR-5): carries the gap, so
#: "truncation applied while the cache was live" is a row query, not a scan.
COMPACTION_EVENT_KINDS = frozenset({
    "applied", "checkpoint", "forced", "floor_unreachable",
    "document_stripped", "document_rehydrated", "document_offload",
    "truncation_anchor",
})
_MAX_PENDING_COMPACTION_EVENTS = 8


def _approx_tokens(text: Any) -> int:
    """~4 chars/token; the same estimate the admin profile uses for summaries."""
    if not text:
        return 0
    try:
        return len(text) // 4
    except TypeError:
        return 0


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
        # Compaction decisions taken since the last model call, drained by
        # ``ContextLedgerHook`` onto that call's cost row. Bounded; content-free.
        self._pending_compaction_events: List[Dict[str, Any]] = []
        self._total_message_count_at_init: int = 0
        # Absolute index (into the stored history) of ``agent.messages[0]``.
        # The restore slice sets it to the applied checkpoint; the persisted
        # checkpoint is always ``_live_offset + <index into the live list>``,
        # so cuts computed over the live list and the slice applied at restore
        # share one coordinate system (spec §3.4).
        self._live_offset: int = 0
        # model|agent key stamped at the head of each turn by the coordinator;
        # persisted at turn end so the next head-of-turn can tell whether the
        # cached prefix is already invalid (spec §3.5).
        self._current_prefix_key: Optional[str] = None
        # When the previous turn ended, as read BEFORE anything at this turn's
        # head saves compaction state. Every save stamps ``updated_at``, so a
        # head-of-turn decision that re-reads it after an earlier save (the
        # restore's truncation-anchor advance, or a parked cut's own apply)
        # sees a gap of ~0s and waits on a cache that is already cold.
        # ``_restore_turn_stamp`` is captured by the restore slice;
        # ``_turn_start_stamp`` is the one every head-of-turn decision reads.
        self._restore_turn_stamp: Optional[str] = None
        self._turn_start_stamp: Optional[str] = None

        # Session control
        self.cancelled = False

        # This turn's single-flight lease, which doubles as the mid-turn
        # steering inbox (docs/specs/mid-turn-steering.md). Per-turn state, so
        # the stream coordinator stamps it at the head of every turn — never
        # loaded once and held, per the CLAUDE.md rule about state on a cached
        # agent. None whenever the guard is inactive (preview sessions, local
        # dev without DynamoDB), which makes steering inert.
        self.turn_lease: Optional[Any] = None

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

    # ------------------------------------------------------------------
    # Long-term memory retrieval, bounded
    # ------------------------------------------------------------------

    def _get_retrieval_client(self) -> Any:
        """A dedicated ``bedrock-agentcore`` data-plane client for
        ``RetrieveMemoryRecords``, built on first use.

        The SDK's ``MemoryClient`` serves reads and writes from one boto client
        with default retries. Writes (``CreateEvent``) should keep retrying —
        a dropped message is a corrupted conversation. Reads should not: the
        retrieval hook runs on ``MessageAddedEvent`` and is awaited before the
        model call, so under load a throttled ``RetrieveMemoryRecords`` (30/s
        per account by default) put boto's retry backoff on the reply's
        first-token latency, exactly the CountTokens mechanism from
        docs/specs/load-test-assessment-2026-09.md §1 fix 3. One attempt and
        a short timeout: a throttle costs one failed request and the turn
        simply runs without long-term context, which is what a miss means.

        One attempt also means a dead pooled connection is never retried. A
        cached agent keeps this client between turns, and after ~6 minutes
        idle its connections are silently dropped on the network path; the
        next request fails within milliseconds. That is handled separately,
        by :meth:`_replace_retrieval_client`, not by boto retries.
        """
        if getattr(self, "_retrieval_client", None) is None:
            import boto3
            from botocore.config import Config as BotocoreConfig

            timeout = memory_retrieval_timeout_seconds()
            self._retrieval_client = boto3.client(
                "bedrock-agentcore",
                region_name=self.region_name,
                config=BotocoreConfig(
                    retries={"total_max_attempts": memory_retrieval_max_attempts(), "mode": "standard"},
                    read_timeout=timeout,
                    connect_timeout=timeout,
                ),
            )
        return self._retrieval_client

    def _replace_retrieval_client(self, stale: Any) -> Any:
        """Swap out a client whose pooled connection went dead, and return a
        fresh one.

        A new client, not a boto retry on the old one: the old pool can hold
        further idle connections that are just as dead, and a retry could
        draw one. Namespaces fail in parallel on the same client, so only
        the first caller replaces it; the rest get that replacement.
        """
        with _RETRIEVAL_CLIENT_LOCK:
            if getattr(self, "_retrieval_client", None) is stale:
                self._retrieval_client = None
            return self._get_retrieval_client()

    def retrieve_customer_context(self, event: Any) -> None:
        """Retrieve long-term memory for the last user message and prepend it.

        Same contract as the SDK method this overrides (namespaces resolved
        from ``retrieval_config``, relevance filter, results wrapped in
        ``<context_tag>`` and inserted as the first content block of the last
        user message so the user's own words stay last) — only the client
        differs, see :meth:`_get_retrieval_client`. Registered by the SDK's
        ``register_hooks`` in both sync and async mode via
        ``self.retrieve_customer_context``, so the override is picked up.
        """
        messages = event.agent.messages
        if not messages or messages[-1].get("role") != "user":
            return None
        content = messages[-1].get("content")
        if not content or "text" not in content[0]:
            return None
        retrieval_config = getattr(self.config, "retrieval_config", None)
        if not retrieval_config:
            return None

        user_query = memory_retrieval_query(messages[-1]["content"][0]["text"])
        client = self._get_retrieval_client()

        def retrieve_for_namespace(namespace: str, cfg: Any) -> List[str]:
            from botocore.exceptions import ConnectionClosedError, SSLError

            resolved = namespace.format(
                actorId=self.config.actor_id,
                sessionId=self.config.session_id,
                memoryStrategyId=getattr(cfg, "strategy_id", None) or "",
            )
            request = {
                "memoryId": self.config.memory_id,
                "namespacePath": resolved,
                "searchCriteria": {"searchQuery": user_query, "topK": cfg.top_k},
            }
            try:
                response = client.retrieve_memory_records(**request)
            except (ConnectionClosedError, SSLError) as e:
                # Dead pooled connection: retry once, at once, on a fresh
                # client. Throttles are ClientErrors and still get no retry.
                started = time.monotonic()
                response = self._replace_retrieval_client(client).retrieve_memory_records(**request)
                logger.info(
                    "memory retrieval reconnected namespace=%s after %s in %dms",
                    namespace, type(e).__name__, (time.monotonic() - started) * 1000,
                )
            records = response.get("memoryRecordSummaries", [])
            if getattr(cfg, "relevance_score", None):
                records = [r for r in records if r.get("score", 0.0) >= cfg.relevance_score]
            items: List[str] = []
            for record in records:
                text = (record.get("content") or {}).get("text", "") if isinstance(record, dict) else ""
                text = text.strip() if isinstance(text, str) else ""
                if text:
                    items.append(text)
            return items

        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            all_context: List[str] = []
            with ThreadPoolExecutor() as executor:
                futures = {
                    executor.submit(retrieve_for_namespace, ns, cfg): ns
                    for ns, cfg in retrieval_config.items()
                }
                for future in as_completed(futures):
                    try:
                        all_context.extend(future.result())
                    except Exception as e:  # noqa: BLE001 - one namespace failing must not sink the rest
                        # botocore's ConnectionClosedError and ReadTimeoutError
                        # carry `response = None`, so guard the lookup; raising
                        # here would discard every namespace's results.
                        error = getattr(e, "response", None)
                        code = error.get("Error", {}).get("Code") if isinstance(error, dict) else None
                        if code in _MEMORY_RETRIEVAL_THROTTLE_CODES:
                            logger.info(
                                "memory retrieval throttled namespace=%s; turn proceeds without long-term context",
                                futures[future],
                            )
                        else:
                            logger.warning(
                                "memory retrieval failed namespace=%s: %s: %s", futures[future], type(e).__name__, e
                            )

            if all_context:
                tag = getattr(self.config, "context_tag", "user_context")
                event.agent.messages[-1]["content"].insert(
                    0, {"text": f"<{tag}>{chr(10).join(all_context)}</{tag}>"}
                )
                logger.info("Retrieved %s customer context items", len(all_context))
        except Exception as e:  # noqa: BLE001 - retrieval must never break a turn
            logger.error("Failed to retrieve customer context: %s", e)
        return None

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

        # Age document_read page slices on restore exactly as the live path
        # does (offload PR-4), so a cold restore and a warm agent agree on
        # which slices are still inline. Pure function of the history's turn
        # structure, so the output is stable across restores.
        try:
            from agents.main_agent.session.document_offload import age_document_slices, offload_enabled_for

            if offload_enabled_for(getattr(self.config, "session_id", None)):
                aged, aged_tokens = age_document_slices(agent.messages)
                if aged:
                    logger.debug("Aged %d document_read slice(s) (~%d tokens) in restored history", aged, aged_tokens)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Document slice ageing failed, continuing: {e}", exc_info=True)

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
            self._live_offset = 0

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
        # The previous turn's stamp, before the anchor advance below re-stamps
        # it; ``apply_pending_compaction`` reads the gap from this.
        self._restore_turn_stamp = self.compaction_state.updated_at

        # Cache valid cutoff indices (user text messages, not tool results)
        self._valid_cutoff_indices = self._find_valid_cutoff_indices(all_messages)

        # Store messages for summary generation (shallow — only deep-copied
        # if update_after_turn actually advances the checkpoint)
        self._all_messages_for_summary = all_messages[:]

        # The gap this restore ran at, measured before anything below stamps
        # updated_at, so the ledger can say whether a restore-time truncation
        # (or slice) landed inside the prompt-cache TTL.
        restore_gap_seconds = self._seconds_since(self.compaction_state.updated_at)

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

        # The live list now starts at this absolute index.
        self._live_offset = offset

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

        if checkpoint > 0 and offset > 0:
            self.record_compaction_event(
                "applied",
                checkpoint=checkpoint,
                summaryTokens=_approx_tokens(self.compaction_state.summary),
                retainedMessages=len(agent.messages),
                truncatedToolResults=truncation_count,
                # The gap at restore time (offload spec PR-5): a restore-time
                # slice or truncation inside the TTL is a rebuild while the
                # cache was live — a rebuild's cost, and now countable.
                cacheGapSeconds=restore_gap_seconds if restore_gap_seconds is not None else -1,
            )

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

        # Measured BEFORE the save below stamps updated_at, so the row shows
        # the gap the decision was made on. This is the offload spec's PR-5:
        # the July 2026 audit found 72% of truncations firing inside the TTL
        # and could only say so by correlating timestamps by hand; the guard
        # is now the ``_cache_window_expired`` check above, and this event is
        # what proves it holds — a ``truncation_anchor`` row with
        # ``cacheGapSeconds`` under the TTL is a regression.
        gap_seconds = self._seconds_since(state.updated_at)
        previous_anchor = state.truncation_anchor
        logger.info(
            "Truncation anchor advance (cache expired, gap=%ss): %d -> %d",
            gap_seconds, previous_anchor, sliding_anchor,
        )
        state.truncation_anchor = sliding_anchor
        self._save_compaction_state(state)
        self.record_compaction_event(
            "truncation_anchor",
            anchorFrom=previous_anchor,
            anchorTo=sliding_anchor,
            cacheGapSeconds=gap_seconds if gap_seconds is not None else -1,
        )
        self._emit_emf(
            {
                "TruncationAnchorAdvanced": 1,
                "TruncationAnchorCacheGapSeconds": int(gap_seconds or 0),
            },
            {"anchorFrom": previous_anchor, "anchorTo": sliding_anchor},
            {"TruncationAnchorCacheGapSeconds": "Seconds"},
        )

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

    def _record_ledger_event(self, kind: str, **fields: Any) -> None:
        """Hand a compaction decision to the per-call compaction ledger, if present.

        The cost-diagnostics ledger (``record_compaction_event`` /
        ``drain_compaction_events`` + ``ContextLedgerHook``) lands whatever is
        recorded here on a ``C#`` cost row as ``compactionEvents``, next to
        ``windowRemovedMessages`` and the prefix token split — the evidence the
        summary cap and the scheduling rule are judged on. Head-of-turn
        decisions land on the next model call; post-turn ones (from
        ``update_after_turn``) on the turn's last call, via
        ``ContextLedgerHook.record_post_turn_events``. Resolved by attribute
        so this is a no-op on a build without the ledger; fields are ints.
        Never raises.
        """
        recorder = getattr(self, "record_compaction_event", None)
        if not callable(recorder):
            return
        try:
            recorder(kind, **{k: int(v) for k, v in fields.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
        except Exception as e:  # noqa: BLE001
            logger.debug("compaction ledger event skipped: %s", e)

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
        over a 35k–150k-token prefix at 1.25x the model's base input rate, on a
        turn where nothing about the conversation appeared to change.

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

    # ------------------------------------------------------------------
    # Compaction event ledger (content-free; persisted per model call)
    # ------------------------------------------------------------------

    def record_compaction_event(self, kind: str, **fields: Any) -> None:
        """Queue a compaction decision for a model call's cost row.

        Drained onto the next model call by ``ContextLedgerHook``, or — for a
        decision ``update_after_turn`` takes after the turn's last call — onto
        that last call by ``ContextLedgerHook.record_post_turn_events``.

        ``kind`` is one of the ``COMPACTION_EVENT_KINDS`` — ``applied`` (the
        restore-time slice ran), ``checkpoint`` (a new checkpoint was cut
        post-turn), ``forced`` and ``floor_unreachable`` (reserved for the
        scheduling policy: a cut taken at the hard ceiling because the previous
        one did not take, and a cut that could not reach its floor because the
        protected tail alone exceeds it). ``fields`` are numbers only —
        ``summaryTokens`` in particular is what proves a summary cap works
        without another table scan. Anything else is dropped here so the
        ledger can never carry content.

        No-op when ``COST_DIAGNOSTICS_ENABLED`` is off, so a row without the
        field reads "not tracked", never "0". Bounded so a runaway caller
        cannot grow a cost row.
        """
        from apis.shared.feature_flags import cost_diagnostics_enabled

        if not cost_diagnostics_enabled():
            return
        if kind not in COMPACTION_EVENT_KINDS:
            logger.debug("Ignoring unknown compaction event kind %r", kind)
            return
        if len(self._pending_compaction_events) >= _MAX_PENDING_COMPACTION_EVENTS:
            return
        event: Dict[str, Any] = {"kind": kind}
        for key, value in fields.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            event[key] = int(value)
        self._pending_compaction_events.append(event)

    def drain_compaction_events(self) -> List[Dict[str, Any]]:
        """Return and clear the queued events (called by ``ContextLedgerHook``)."""
        events, self._pending_compaction_events = self._pending_compaction_events, []
        return events

    def _save_compaction_state(self, state: CompactionState, record_event: bool = False) -> None:
        """Save compaction state to DynamoDB session metadata.

        ``record_event=True`` means this save *is* a compaction (a new
        checkpoint was cut), and bumps the session's ``compactionCount`` in
        the same update. That counter is what the admin profile reads for
        "how many times did compaction fire" — the persisted ``compaction``
        map is last-write-wins and cannot answer it. A top-level ``ADD`` is
        monotonic and race-free: two Agents serving one session can each
        increment, and neither can move it backwards.
        """
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
            update_expression = "SET compaction = :state"
            values: Dict[str, Any] = {":state": state.to_dict()}
            if record_event:
                from apis.shared.feature_flags import cost_diagnostics_enabled

                if cost_diagnostics_enabled():
                    update_expression += " ADD compactionCount :one"
                    values[":one"] = 1
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=update_expression,
                ExpressionAttributeValues=values,
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
        context_window: Optional[int] = None,
        history_tokens: Optional[int] = None,
    ) -> Optional[CompactionResult]:
        """
        Update compaction state after a turn completes.

        Called by StreamCoordinator with the turn's cache-inclusive input token
        count. Resolves the model-relative policy (ceiling / floor / hard
        ceiling — spec §3.1), applies the hysteresis rule (§3.3) and, when a
        cut is due, chooses a floor-seeking checkpoint (§3.2) in absolute
        coordinates (§3.4). Persists the new checkpoint + summary; the slice
        itself is applied at the next restore by ``_apply_compaction``.

        Returns a ``CompactionResult`` when the checkpoint advances on this
        turn so the caller can emit a ``compaction`` SSE event; otherwise
        returns ``None``.

        Args:
            input_tokens: cache-inclusive input tokens of the turn's last call.
            current_messages: the agent's live message list. When provided,
                the cutoff cache is re-derived from it so compaction works even
                when AgentCoreMemory loads messages via hooks (skipping the
                initialize-time prime path).
            context_window: the model's ``maxInputTokens`` from the catalog,
                or ``None`` when unknown (falls back to the fixed threshold).
            history_tokens: measured size of the conversation portion of the
                prompt (the ``messages`` partition of the context breakdown);
                calibrates the per-message estimates. ``None`` → use
                ``input_tokens``, which biases the cut slightly deeper.
        """
        # In-process "when did the last turn end" for the document-offload
        # cache-gap decision when compaction (whose state stamps updated_at
        # every turn) is off. Stamped before the early return on purpose.
        self._last_turn_completed_at = datetime.now(timezone.utc).isoformat()
        # The head-of-turn stamps belong to the turn that just ended.
        self._restore_turn_stamp = None
        self._turn_start_stamp = None

        if not self.compaction_config or not self.compaction_config.enabled:
            return None

        # Re-read persisted state before touching it — on EVERY turn, not just
        # the first. See ``_adopt_persisted_compaction_state`` (#751).
        self._adopt_persisted_compaction_state()
        state = self.compaction_state
        state.last_input_tokens = input_tokens
        if self._current_prefix_key:
            state.last_prefix_key = self._current_prefix_key

        policy = CompactionPolicy.resolve(self.compaction_config, context_window)

        if input_tokens <= policy.ceiling:
            if policy.hysteresis_enabled and not state.armed:
                logger.info(
                    "compaction_rearmed: input=%d <= ceiling=%d (source=%s)",
                    input_tokens, policy.ceiling, policy.source,
                )
                state.armed = True
            self._save_compaction_state(state)
            return None

        at_hard_ceiling = policy.hard_ceiling is not None and input_tokens >= policy.hard_ceiling
        # "Forced" is the spiral signal: a cut that ran while DISARMED because
        # the hard ceiling was reached. An armed cut above the hard ceiling is
        # just a large ordinary cut.
        forced = policy.hysteresis_enabled and not state.armed and at_hard_ceiling
        if state.pending_checkpoint is not None and policy.hysteresis_enabled:
            # A cut is already parked. Cutting deeper now would be the spiral;
            # the head of the next turn applies it (hard ceiling included).
            logger.info(
                "compaction_pending_waiting: input=%d > ceiling=%d, pending_checkpoint=%d, hard=%s",
                input_tokens, policy.ceiling, state.pending_checkpoint, policy.hard_ceiling,
            )
            self._save_compaction_state(state)
            return None
        if policy.hysteresis_enabled and not state.armed and not at_hard_ceiling:
            # The previous cut has not been observed to take effect yet (the
            # slice lands at the next restore) — cutting again now is exactly
            # the spiral. Wait for the context to drop under the ceiling, or
            # for the hard ceiling.
            logger.info(
                "compaction_disarmed_noop: input=%d > ceiling=%d, hard=%d (source=%s)",
                input_tokens, policy.ceiling, policy.hard_ceiling, policy.source,
            )
            self._save_compaction_state(state)
            return None
        if forced:
            logger.warning(
                "compaction_forced: input=%d >= hard_ceiling=%d while disarmed — "
                "the previous cut did not bring the context under the ceiling "
                "(summary too large, or slice not yet applied on this agent)",
                input_tokens, policy.hard_ceiling,
            )

        logger.info(
            f"Threshold exceeded: {input_tokens:,} > "
            f"{policy.ceiling:,} (floor={policy.floor}, hard={policy.hard_ceiling}, "
            f"window={policy.context_window}, source={policy.source})"
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
            self._save_compaction_state(state)
            return None

        cutoffs = self._valid_cutoff_indices
        total_turns = len(cutoffs)
        protected_turns = self.compaction_config.protected_turns

        if total_turns <= protected_turns:
            logger.debug(
                f"Only {total_turns} turns available (need > {protected_turns}), "
                f"keeping all messages"
            )
            self._save_compaction_state(state)
            return None

        # Choose the cut in LIVE-LIST coordinates, then translate to absolute.
        messages = self._all_messages_for_summary
        retained_estimate: Optional[int] = None
        if policy.floor is None:
            # Legacy (kill switch): keep the last N turns, whatever their size.
            relative_cut = cutoffs[-protected_turns]
        else:
            relative_cut, retained_estimate = choose_checkpoint(
                messages,
                cutoffs,
                protected_turns,
                policy.floor,
                history_tokens if history_tokens is not None else input_tokens,
            )

        if relative_cut <= 0:
            # Everything already fits under the floor — the excess is system
            # prompt / tools, which a history cut cannot fix.
            logger.info(
                "compaction_nothing_to_cut: retained≈%s <= floor=%s with input=%d",
                retained_estimate, policy.floor, input_tokens,
            )
            self._save_compaction_state(state)
            return None

        new_checkpoint = self._live_offset + relative_cut
        current_checkpoint = state.checkpoint

        if new_checkpoint <= current_checkpoint:
            self._save_compaction_state(state)
            return None

        logger.info(
            "compaction_cut: checkpoint %d -> %d (live_offset=%d, relative_cut=%d, "
            "retained≈%s, floor=%s, ceiling=%d, hard=%s, window=%s, forced=%s)",
            current_checkpoint, new_checkpoint, self._live_offset, relative_cut,
            retained_estimate, policy.floor, policy.ceiling, policy.hard_ceiling,
            policy.context_window, forced,
        )
        # Per-call compaction ledger: the two decisions the spec asks to see
        # on the anatomy — a cut that ran while disarmed, and a cut that could
        # not reach the floor because the protected tail alone exceeds it.
        if forced:
            self._record_ledger_event("forced", checkpoint=new_checkpoint, inputTokens=input_tokens)
        if policy.floor is not None and retained_estimate is not None and retained_estimate > policy.floor:
            self._record_ledger_event(
                "floor_unreachable",
                checkpoint=new_checkpoint, inputTokens=input_tokens, retainedTokens=retained_estimate,
            )

        # Count turns rolled into the summary on THIS event (delta, not
        # cumulative) — each inline divider stands on its own. In absolute
        # coordinates: turn starts at or after the previous checkpoint (they
        # were retained by the last slice) and before the new one.
        summarized_turns = sum(
            1 for idx in cutoffs
            if current_checkpoint <= self._live_offset + idx < new_checkpoint
        )

        # Retrieve or generate the summary for the retired messages, then
        # hold it at the budget (spec §3.6 / spiral spec PR-2). Compression
        # runs here — once, at checkpoint advance, the turn that already pays
        # a prefix re-write — and the result is persisted verbatim so every
        # restore prepends identical bytes.
        summaries = self._retrieve_session_summaries()
        if summaries:
            records = list(summaries)
            summary_source = "ltm"
        else:
            fallback = self._generate_fallback_summary(messages[:relative_cut])
            records = [fallback] if fallback else []
            summary_source = "fallback"
        bounded = await bound_summary(
            records,
            self.compaction_config.summary_token_budget,
            model_enabled=self.compaction_config.summary_model_enabled,
            model_id=self.compaction_config.summary_model_id,
            region=self.region_name,
            extract_enabled=self.compaction_config.summary_extract_enabled,
        )
        summary = bounded.text

        deferred = bool(self.compaction_config.deferred_apply_enabled and policy.hysteresis_enabled)
        if deferred:
            # Park the cut. The bytes the model sees do not change until
            # ``apply_pending_compaction`` decides the re-write is free or
            # unavoidable (spec §3.5).
            state.pending_checkpoint = new_checkpoint
            state.pending_summary = summary
            state.pending_hard_ceiling = policy.hard_ceiling
            state.pending_since = datetime.now(timezone.utc).isoformat()
        else:
            state.checkpoint = new_checkpoint
            # The anchor rides the checkpoint: everything the slice retains stays
            # byte-identical until the next compaction-state change, so the single
            # mutation (slice + summary) is paid with exactly one cache re-write.
            state.truncation_anchor = max(state.truncation_anchor, new_checkpoint)
            state.summary = summary
        # Running total persisted alongside the rest of the compaction state
        # so a refresh can rehydrate the end-of-conversation summary indicator.
        state.total_summarized_turns += summarized_turns
        if policy.hysteresis_enabled:
            state.armed = False
        state.policy = {
            **policy.to_dict(),
            "forced": forced,
            "inputTokens": input_tokens,
            "retainedTokensEstimate": retained_estimate,
            # Summary provenance — what the admin profile and the cost
            # anatomy read to explain a cut without reading the conversation.
            "summarySource": summary_source,
            "summaryOutcome": bounded.outcome,
            "summaryTokensBefore": bounded.tokens_before,
            "summaryTokensAfter": bounded.tokens_after,
            "summaryTokenBudget": self.compaction_config.summary_token_budget,
            "deferred": deferred,
        }
        # This save is the compaction event itself — count it.
        self._save_compaction_state(state, record_event=True)
        self._emit_compaction_metrics(state, policy, forced, retained_estimate, bounded, deferred)
        # Routed through the defensive seam rather than calling the recorder
        # directly: same no-op-without-a-ledger contract as every other cut
        # decision. Queued when the cut is *decided* and attached to this
        # turn's last call (the one whose input triggered it) — when
        # ``deferred`` the bytes do not move until ``apply_pending_compaction``
        # runs, which records its own ``applied``.
        self._record_ledger_event(
            "checkpoint",
            checkpoint=new_checkpoint,
            summaryTokens=_approx_tokens(summary),
            summarizedTurns=summarized_turns,
            inputTokens=input_tokens,
        )

        logger.info(
            f"Compaction checkpoint set: {new_checkpoint}, "
            f"summary_length={len(summary) if summary else 0}, "
            f"summarized_turns={summarized_turns}, "
            f"total_summarized_turns={state.total_summarized_turns}"
        )

        return CompactionResult(
            previous_checkpoint=current_checkpoint,
            new_checkpoint=new_checkpoint,
            summarized_turns=summarized_turns,
            input_tokens=input_tokens,
            context_window=policy.context_window,
            ceiling=policy.ceiling,
            floor=policy.floor,
            hard_ceiling=policy.hard_ceiling,
            forced=forced,
            retained_tokens_estimate=retained_estimate,
            deferred=deferred,
        )

    # =========================================================================
    # Paid-when-free application (spec §3.5)
    # =========================================================================

    def apply_pending_compaction(self, agent: "Agent", *, prefix_key: Optional[str] = None) -> Optional[str]:
        """Head-of-turn: apply a parked cut to the live list if the re-write is free.

        Called by the stream coordinator before the first model call of every
        turn, on cached and freshly restored agents alike. Promotes
        ``pending_checkpoint`` to ``checkpoint`` and slices ``agent.messages``
        **in place** (slice assignment, never rebinding — the #741 alias) when
        one of these holds, in this order:

        - ``cache_expired`` — more than ``cache_ttl_seconds`` since the last
          turn: the Bedrock entry is gone and the next call re-writes the
          prefix anyway, so the cut rides for free;
        - ``prefix_changed`` — ``prefix_key`` (model|agent) differs from the
          last turn's: the cached prefix is already invalid;
        - ``hard_ceiling`` — the last turn's input reached the hard ceiling
          the cut was computed under: waiting is no longer affordable.

        Otherwise the cut keeps waiting (``compaction_pending_waiting``). Returns
        the reason applied, or ``None``. Never raises.

        The gap is measured from the previous turn's stamp as it stood before
        this turn's head saved anything (``_turn_start_stamp``), and that
        stamp is left for ``apply_document_offload``, which runs next.
        """
        # Consumed every turn, so a stamp from a turn that never reached here
        # cannot make a later, genuinely warm turn look cold.
        restore_stamp = getattr(self, "_restore_turn_stamp", None)
        self._restore_turn_stamp = None
        self._turn_start_stamp = None
        if not self.compaction_config or not self.compaction_config.enabled:
            return None
        try:
            self._adopt_persisted_compaction_state()
            state = self.compaction_state
            if state is None:
                return None

            turn_stamp = restore_stamp or state.updated_at
            self._turn_start_stamp = turn_stamp
            previous_key = state.last_prefix_key
            self._current_prefix_key = prefix_key
            if state.pending_checkpoint is None:
                return None

            config = self.compaction_config
            gap_seconds = self._seconds_since(turn_stamp)
            reason: Optional[str] = None
            if self._cache_window_expired(turn_stamp, config.cache_ttl_seconds):
                reason = "cache_expired"
            elif prefix_key and previous_key and prefix_key != previous_key:
                reason = "prefix_changed"
            elif state.pending_hard_ceiling is not None and state.last_input_tokens >= state.pending_hard_ceiling:
                reason = "hard_ceiling"

            if reason is None:
                logger.info(
                    "compaction_pending_waiting: pending_checkpoint=%d, gap=%ss, last_input=%d, hard=%s",
                    state.pending_checkpoint, gap_seconds, state.last_input_tokens, state.pending_hard_ceiling,
                )
                return None

            messages = getattr(agent, "messages", None)
            if not isinstance(messages, list):
                return None
            applied = self._apply_pending_in_place(messages, state)
            if not applied:
                # Unusable pending (would drop the whole live list): clear it
                # rather than leave a cut that can never land.
                state.pending_checkpoint = None
                state.pending_summary = None
                state.pending_hard_ceiling = None
                state.pending_since = None
                self._save_compaction_state(state)
                return None

            promoted = state.pending_checkpoint
            state.checkpoint = promoted
            state.truncation_anchor = max(state.truncation_anchor, promoted)
            state.summary = state.pending_summary
            state.pending_checkpoint = None
            state.pending_summary = None
            state.pending_hard_ceiling = None
            pending_since = state.pending_since
            state.pending_since = None
            state.policy = {
                **(state.policy or {}),
                "applied": reason,
                "appliedAt": datetime.now(timezone.utc).isoformat(),
                "cacheGapSeconds": gap_seconds,
                "pendingSince": pending_since,
            }
            self._save_compaction_state(state)
            # Per-call compaction ledger: the apply is the moment the bytes
            # the model sees change, so it is the event the anatomy marks.
            self._record_ledger_event(
                "applied",
                checkpoint=promoted,
                summaryTokens=len(state.summary or "") // 4,
                retainedMessages=len(messages),
                cacheGapSeconds=gap_seconds or 0,
                # Distinguishes the head-of-turn promotion from the restore
                # slice's own "applied" event (both are real byte changes).
                promoted=1,
            )
            logger.info(
                "compaction_applied: reason=%s checkpoint=%d gap=%ss live_len=%d (%s)",
                reason, promoted, gap_seconds, len(messages),
                "rewrite_forced" if reason == "hard_ceiling" else "rewrite_scheduled",
            )
            self._emit_emf(
                {
                    "CompactionApplied": 1,
                    "CompactionAppliedForced": 1 if reason == "hard_ceiling" else 0,
                    "CompactionCacheGapSeconds": int(gap_seconds or 0),
                },
                {"applyReason": reason, "checkpoint": promoted},
                {"CompactionCacheGapSeconds": "Seconds"},
            )
            return reason
        except Exception as e:  # noqa: BLE001 - never break a turn
            logger.warning(f"apply_pending_compaction skipped: {e}", exc_info=True)
            return None

    # =========================================================================
    # Document offload (offload spec §4C / §4D, PR-4)
    # =========================================================================

    def apply_document_offload(self, agent: "Agent", *, prompt: Any = None) -> Optional[str]:
        """Head-of-turn: swap unpinned, large inline documents for their digests
        — and stub aged ``document_read`` page slices — when the prefix
        re-write is free or unavoidable.

        Runs right after ``apply_pending_compaction`` on every turn. The
        eligibility rules (pinning, minimum size) live in
        ``document_offload.py``; this method owns the cache-gap decision, the
        same one the parked cut uses:

        - ``cache_expired`` — more than ``cache_ttl_seconds`` since the last
          turn (the Bedrock entry is gone; the next call re-writes anyway);
        - ``prefix_changed`` — this turn's model|agent key differs from the
          last turn's (the cached prefix is already invalid);
        - ``over_ceiling`` — the last turn's input exceeded the compaction
          ceiling (waiting is no longer affordable; the eviction is what
          brings the prefix down).

        Otherwise nothing moves (``document_offload_waiting``). The replacement
        is the restore path's own transformation, so the live block equals
        what a cold restore would produce. Records ``document_offload`` on the
        ledger with the cache gap. Never raises; returns the reason applied.
        """
        from agents.main_agent.session.document_offload import (
            DOCUMENT_OFFLOAD_MIN_TOKENS,
            DOCUMENT_OFFLOAD_PIN_TURNS,
            DOCUMENT_SLICE_MAX_TURNS,
            age_document_slices,
            candidate_documents,
            offload_documents,
            offload_enabled_for,
            pinned_document_names,
        )

        session_id = getattr(self.config, "session_id", None)
        if not offload_enabled_for(session_id):
            return None
        try:
            messages = getattr(agent, "messages", None)
            if not isinstance(messages, list) or not messages:
                return None
            pinned = pinned_document_names(messages, prompt, pin_turns=DOCUMENT_OFFLOAD_PIN_TURNS)
            candidates = candidate_documents(messages, pinned, min_tokens=DOCUMENT_OFFLOAD_MIN_TOKENS)
            has_slices = any(
                isinstance(b, dict) and isinstance(b.get("toolResult"), dict)
                for m in messages if isinstance(m, dict) and isinstance(m.get("content"), list)
                for b in m["content"]
            )
            if not candidates and not has_slices:
                return None

            reason, gap_seconds = self._document_offload_reason()
            if reason is None:
                if candidates:
                    logger.info(
                        "document_offload_waiting: candidates=%d gap=%ss (cache live, prefix unchanged, under ceiling)",
                        len(candidates), gap_seconds,
                    )
                return None

            result = offload_documents(messages, candidates, session_id=session_id, user_id=self.user_id)
            result.slices_aged, result.slice_tokens = age_document_slices(messages, max_turns=DOCUMENT_SLICE_MAX_TURNS)
            if not result.offloaded and not result.slices_aged:
                return None

            logger.info(
                "document_offload: reason=%s documents=%d evicted≈%d tok digests≈%d tok slices=%d gap=%ss unmatched=%d",
                reason, result.offloaded, result.evicted_tokens, result.digest_tokens,
                result.slices_aged, gap_seconds, result.skipped_unmatched,
            )
            self._record_ledger_event(
                "document_offload",
                documents=result.offloaded,
                documentTokens=result.evicted_tokens,
                digestTokens=result.digest_tokens,
                slices=result.slices_aged,
                sliceTokens=result.slice_tokens,
                cacheGapSeconds=gap_seconds if gap_seconds is not None else -1,
            )
            self._emit_emf(
                {
                    "DocumentOffloaded": result.offloaded,
                    "DocumentOffloadedTokens": int(result.evicted_tokens),
                    "DocumentSlicesAged": result.slices_aged,
                    "DocumentOffloadCacheGapSeconds": int(gap_seconds or 0),
                },
                {"offloadReason": reason},
                {"DocumentOffloadedTokens": "Count", "DocumentOffloadCacheGapSeconds": "Seconds"},
            )
            return reason
        except Exception as e:  # noqa: BLE001 - never break a turn
            logger.warning(f"apply_document_offload skipped: {e}", exc_info=True)
            return None

    def _document_offload_reason(self) -> Tuple[Optional[str], Optional[int]]:
        """``(reason, cache gap seconds)`` — why a prefix re-write is free or
        unavoidable right now, or ``(None, gap)`` while the cache is live.

        Reads the compaction state when compaction is on (``updated_at`` is
        stamped every turn, ``last_prefix_key`` / ``last_input_tokens`` too)
        and the in-process last-turn timestamp otherwise, so the gate works
        with compaction disabled as well. Conservative on any doubt.
        """
        config = self.compaction_config
        state = self.compaction_state if (config and config.enabled) else None
        ttl = config.cache_ttl_seconds if config else Defaults.COMPACTION_CACHE_TTL_SECONDS
        # This turn's start stamp first: ``apply_pending_compaction`` runs
        # just before this and, when it applies a cut, re-stamps
        # ``updated_at`` — reading that would call a cold cache warm.
        last_turn_at = (
            getattr(self, "_turn_start_stamp", None)
            or (state.updated_at if state else None)
            or getattr(self, "_last_turn_completed_at", None)
        )
        gap = self._seconds_since(last_turn_at)
        if self._cache_window_expired(last_turn_at, ttl):
            return "cache_expired", gap
        if state is not None:
            current_key = getattr(self, "_current_prefix_key", None)
            if current_key and state.last_prefix_key and current_key != state.last_prefix_key:
                return "prefix_changed", gap
            ceiling = (state.policy or {}).get("ceiling") if isinstance(state.policy, dict) else None
            if ceiling is None:
                try:
                    ceiling = CompactionPolicy.resolve(config, None).ceiling
                except Exception:  # noqa: BLE001
                    ceiling = None
            if ceiling and state.last_input_tokens and state.last_input_tokens > int(ceiling):
                return "over_ceiling", gap
        return None, gap

    def _apply_pending_in_place(self, messages: List[Dict], state: CompactionState) -> bool:
        """Slice the live list at the pending checkpoint, in place.

        ``messages[0]`` sits at absolute index ``_live_offset``; the pending
        checkpoint is absolute. Produces the same bytes ``_apply_compaction``
        would derive from stored history with the promoted state (slice, then
        the summary prepended to the new first user message), so a later cold
        restore matches the live prefix.
        """
        pending = state.pending_checkpoint
        if pending is None:
            return False
        k = pending - self._live_offset
        if k <= 0:
            # Live list already starts at/after the cut (e.g. a restore that
            # sliced there). Nothing to remove; promotion still records it.
            return True
        if k >= len(messages):
            logger.warning(
                "compaction pending checkpoint %d is beyond the live list (offset=%d, len=%d); dropping it",
                pending, self._live_offset, len(messages),
            )
            return False
        head = messages[k]
        if state.pending_summary:
            head = self._prepend_summary_to_first_message([head], state.pending_summary)[0]
        messages[:] = [head] + messages[k + 1:]
        self._live_offset = pending
        return True

    @staticmethod
    def _seconds_since(updated_at: Optional[str]) -> Optional[int]:
        if not updated_at:
            return None
        try:
            last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - last).total_seconds())

    def _emit_emf(self, metrics: Dict[str, Any], properties: Dict[str, Any], units: Optional[Dict[str, str]] = None) -> None:
        """One content-free EMF record in ``AgentCoreStack/Compaction``. Never raises.

        Every record carries the conversation's ``sessionId`` as a log
        property, the way the prompt-cache records do — never a dimension,
        which would mint a metric stream per conversation. Without it, the
        2026-09-25 prod readout had to join cuts to sessions by matching input
        tokens within ±30 min. Numbers and identifiers only; no conversation
        content reaches these records.

        ``PROMPT_CACHE_OBSERVABILITY_ENABLED=false`` silences it with the rest
        of the cost observability layer.
        """
        try:
            from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled
            from apis.shared.observability.emf import emit_emf_metrics

            if not prompt_cache_observability_enabled():
                return
            session_id = getattr(getattr(self, "config", None), "session_id", None)
            emit_emf_metrics(
                "AgentCoreStack/Compaction",
                metrics=metrics,
                properties={**properties, "sessionId": session_id},
                units=units or {},
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Compaction EMF skipped: %s", e)

    def _emit_compaction_metrics(self, state, policy, forced, retained_estimate, bounded, deferred=False) -> None:
        """One record per cut: how often cuts fire, how often they are forced
        (the spiral detector), how big the summary is against its budget, how
        deep cuts land, and whether the cut was parked for a free turn."""
        self._emit_emf(
            {
                "CompactionCut": 1,
                "CompactionForced": 1 if forced else 0,
                "CompactionDeferred": 1 if deferred else 0,
                "CompactionInputTokens": int(state.last_input_tokens or 0),
                "CompactionRetainedTokens": int(retained_estimate or 0),
                "CompactionSummaryTokens": int(bounded.tokens_after or 0),
                "CompactionSummaryOverBudget": 1 if bounded.tokens_before > bounded.tokens_after else 0,
                # The protected tail alone exceeded the floor: the residual
                # case after intake offload (attachments, sub-gate results).
                "CompactionFloorUnreachable": (
                    1 if (policy.floor is not None and retained_estimate is not None and retained_estimate > policy.floor) else 0
                ),
            },
            {
                "policySource": policy.source,
                "contextWindow": policy.context_window,
                "ceiling": policy.ceiling,
                "floor": policy.floor,
                "summaryOutcome": bounded.outcome,
                "summaryTokensBefore": bounded.tokens_before,
            },
            {
                "CompactionInputTokens": "Count",
                "CompactionRetainedTokens": "Count",
                "CompactionSummaryTokens": "Count",
            },
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
        """Replace document content blocks' inline bytes with their digest — or,
        when the block cannot be matched to an upload, a text placeholder.

        Called unconditionally on every session restore — independent of whether
        compaction is enabled. Document blocks with ``source.bytes`` must never
        survive in restored history because Bedrock rejects any request where two
        document blocks share the same sanitized name across the conversation
        (ValidationException: "Messages can't contain duplicate document names").

        Since PR-3 of the offload spec the replacement is a ``<document-digest …>``
        block carrying the document's outline, abstract and ``upload_id``, so the
        model still knows what the document says and can pull any page back with
        ``document_read`` (see ``document_rehydration.py``). Blocks with no
        upload row keep the contentless placeholder, exactly as before.

        Images are handled the same way inside ``_truncate_tool_contents``, but
        that method is gated on compaction being enabled. This one is not.

        Two content-free ledger events record what happened on the next cost
        row: ``document_rehydrated`` (documents and their digest tokens) and
        ``document_stripped`` (documents that fell back to the placeholder and
        the tokens they were). The second going to zero is PR-3's gate.
        """
        from agents.main_agent.session.document_rehydration import rehydrate_documents

        result = rehydrate_documents(
            messages,
            session_id=getattr(self.config, "session_id", None),
            user_id=self.user_id,
        )
        if result.rehydrated:
            logger.info(
                "Rehydrated %d document block(s) as digests (%d built lazily) in restored history",
                result.rehydrated, result.lazy_digests,
            )
            self.record_compaction_event(
                "document_rehydrated",
                documents=result.rehydrated,
                documentTokens=result.digest_tokens,
            )
        if result.stripped:
            logger.debug(f"Stripped inline bytes from {result.stripped} unmatched document block(s) in history")
            self.record_compaction_event(
                "document_stripped",
                documents=result.stripped,
                documentTokens=result.stripped_tokens,
            )
        return result.messages

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
        # Result turns whose non-toolResult content was already carried onto a
        # rebuilt turn below. Populated one iteration ahead of its use (i adds
        # i+1), so the standalone branch does not re-emit the same block and
        # duplicate a mid-turn steering injection.
        carried_residual_indices: set = set()
        for i, msg in enumerate(messages):
            has_use, has_result = cls._block_keys(msg)

            if has_result and not has_use:
                # Standalone tool-result turn: keep only non-toolResult content
                # (rare mixed text), drop the results themselves.
                if i in carried_residual_indices:
                    continue
                residual = [b for b in msg.get("content", []) if not (isinstance(b, dict) and "toolResult" in b)]
                if residual:
                    rebuilt.append({"role": msg.get("role", "user"), "content": residual})
                continue

            if has_use and i < last_index:
                rebuilt.append(msg)
                use_ids = cls._tool_use_ids(msg)
                # The result turn is rebuilt from the id map rather than
                # copied, so any NON-toolResult block on the original result
                # turn would be silently dropped. That block is not always
                # incidental: mid-turn steering appends the user's own words
                # to the tool-result message (docs/specs/mid-turn-steering.md),
                # and a repair that discards them deletes something the user
                # said and the model already read. Carry them across.
                content = [result_by_id.get(t, missing_result(t)) for t in use_ids]
                residual = cls._non_tool_result_blocks(messages, i + 1)
                if residual:
                    content.extend(residual)
                    carried_residual_indices.add(i + 1)
                rebuilt.append({"role": "user", "content": content})
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

    @staticmethod
    def _non_tool_result_blocks(messages: List[Dict], index: int) -> List[Dict]:
        """Non-toolResult content on the user turn at ``index``, if it is one.

        Used by the repair rebuild to carry a mid-turn steering injection (a
        ``{"text": ...}`` block riding the tool-result message) onto the
        rebuilt result turn. Returns ``[]`` for anything that is not a
        result-bearing user turn, so ordinary histories are unaffected.
        """
        if index >= len(messages):
            return []
        candidate = messages[index]
        if candidate.get("role") != "user":
            return []
        content = candidate.get("content") or []
        if not any(isinstance(b, dict) and "toolResult" in b for b in content):
            return []
        return [b for b in content if not (isinstance(b, dict) and "toolResult" in b)]

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
