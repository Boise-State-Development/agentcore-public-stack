"""Hook that computes a per-call context-token attribution breakdown.

Splits the prompt each model call was billed for into ``system`` / ``tools`` /
``messages`` partitions and stashes the result on the agent. The stream
coordinator reads it via :func:`get_context_breakdown` and attaches it to the
turn's final ``metadata`` SSE event as ``contextBreakdown`` — answering "what
is filling the context window?" without an aggregate-only guess.

Decomposition (convention validated live against Bedrock CountTokens), from
four native counts of one snapshot of the conversation:

- ``systemTokens`` = ``count(probe + system) - count(probe)``. Bedrock refuses
  an empty message list, so the system prompt is counted against a fixed
  probe message whose own weight is subtracted (see ``_probe_baseline``).
- ``toolTokens``   = ``count(full) - count(system + messages, no tools)`` — the
  tool schemas **plus** the tool-use scaffolding Bedrock injects only when
  tools and a conversation coexist (~400 tokens). Folded into Tools by design:
  it is the true marginal cost of having tools enabled.
- ``messageTokens`` = ``billed prompt - systemTokens - toolTokens`` (residual;
  grows with the conversation). Partitions sum to the billed total by
  construction.

**Nothing here sits in front of a model call.** Usage reports only a total,
so the split needs CountTokens — but a CountTokens round trip is ~70 ms at
minimum and ~150 ms for a 30k-token prompt, and awaiting it before a model
call adds that to time to first token one-for-one (measured, see #1343). So
``BeforeModelCallEvent`` only records Strands' projection and, while an agent
has no split yet, starts one background task over a snapshot of the
conversation; its counts run in worker threads, concurrently with the model
call. ``AfterModelCallEvent`` reads the call's billed prompt (input + cache
read + cache write) off the assistant message and rebuilds the breakdown —
pure arithmetic, after the model has answered. The split is stable across a
session (the tool overhead is constant as the conversation grows — verified),
so it is measured once per agent and memoised per session + configuration.
Strands' own projection no longer calls CountTokens at all
(``CountTokensBedrockModel(native_projection=False)``): nothing in this stack
reads it except the interrupted-turn fallback (:func:`get_projected_input_tokens`).

**Why the split is not computed while an attachment is in context.**
``toolTokens`` is a residual, so any disagreement between its two sides about
how a content block is counted lands wholly in it. When one side was Strands'
projection and the other our own CountTokens call, a PDF's entire weight was
attributed to tools. Measured on dev 2026-09-16 (session ``61de2256``): a call
reported ``toolTokens`` of **106,756** where the session's real tools prefix
was **12,516** — a difference of 94,240 against a document measured at
~94,485, i.e. the whole document. Both sides are now native counts of the
same snapshot, which should remove that failure, but the guard stays until a
document turn has been measured to prove it: the split is skipped on any call
whose context carries inline document or image bytes, and taken on a later
clean call instead. An absent ``prefixTokens`` reads "not tracked" (the
ledger's convention); a wrong one silently corrupts every share computed from
it.

**Why the split is not computed at all on OpenAI-surface providers.**
There is no native counter to take it from. Only Bedrock Converse serves
CountTokens (``BedrockModel``). On ``bedrock-responses`` and ``mantle`` the model is an
``OpenAIResponsesModel``, whose ``count_tokens`` consults the native endpoint
only when ``use_native_token_count`` is set — and that endpoint is **not
served** on bedrock-runtime's OpenAI surface: enabling it makes ``count_tokens``
return ``None`` (measured against ``us.moonshotai.kimi-k3``, us-west-2,
2026-09-21; Strands returns None rather than raising, so it would poison the
arithmetic silently). Left unset, it degrades to the chars/4 heuristic.

When the split subtracted a heuristic count from a usage-anchored projection,
it did not yield tool tokens; it yielded tools *plus* the estimator
disagreement, which moves with the conversation. Measured live on one Kimi K3 session: the same byte-identical
tool set (``toolConfigHash`` 8f6647f7f7 on both calls) reported **13,967** then
**7,145** — a 2x swing on the number whose entire job is saying what fills the
window. Forcing both sides through ``count_tokens`` makes the residual exactly
stable (5,882 on a short conversation and 5,882 on a long one, +0 drift), which
confirms the mechanism is the mismatch and not the tools.

So the split is skipped for those providers, on the same principle the
attachment guard states: an absent ``prefixTokens`` reads "not tracked", which
the ledger already handles, while a wrong one silently corrupts every share
computed from it. **Nothing else is lost** — every cost figure, the context
meter (``lastContextTokens`` = input + cacheRead + cacheWrite, summed from real
usage) and the window all come from provider-reported usage, not from this
split. Re-enable by deleting the guard once the native count is served here;
tracked in ``docs/kaizen/review-queue.md``.

**Why a split is never built from a heuristic count, on any transport.**
``CountTokensBedrockModel`` is a ``BedrockModel``, but that does not make every
count it returns native. A model Bedrock refuses to count (Claude Sonnet 5 —
"doesn't support counting tokens"), a throttle, or any failure all fall back to
Strands' heuristic, which charges JSON at chars/2, so tool schemas dominate the
residual. On prod Sonnet 5 the split came out as ``tools = 13,606`` against a
first call whose whole billed prompt was **12,909** tokens, and the readout of
2026-09-25 found ``system + tools`` above the call's own prompt on 321 of 352
first calls in Sonnet 5 sessions. The plausibility guard in
``apis.shared.observability.prefix_tokens`` only catches the impossible cases;
on a long conversation the same inflated split passes it. So the hook asks the
model (``token_count_is_authoritative``, which reads the SDK's skip list) before
spending any count, and counts through
``CountTokensBedrockModel.native_count_tokens``, which answers ``None`` rather
than a heuristic. A split with any ``None`` among its counts is dropped and
retried on the next turn, at most once per turn. (A model without that method
that still declares an authoritative ``count_tokens`` is read through it, with
``heuristic_count_fallbacks`` compared around each call to the same effect.)

**Itemization.** The displayed partitions are finer than the measured split:
the system total is carved into System instructions / Skills / Memory and the
tools total into per-origin children, by character share
(:mod:`.context_itemization`). Every row set sums to the measured total it came
from, so ``system + skills + memory`` is the split's ``systemTokens`` — which is
what ``prefixTokens`` persists — and ``messages`` is unchanged. It is computed
on read (``get_context_breakdown(agent, itemized=True)``), never in the hook,
so it adds nothing before a model call.

Best-effort: any failure is swallowed so context attribution can never break a
model call.
"""

import asyncio
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from strands.hooks import (
    AfterModelCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from agents.main_agent.session.hooks.context_itemization import itemize_system, itemize_tools
from apis.shared.observability.prefix_tokens import prefix_split_is_plausible

logger = logging.getLogger(__name__)

# Stashed on the per-session Strands agent instance.
_SPLIT_ATTR = "_context_attribution_split"          # cached stable {systemTokens, toolTokens}
_BREAKDOWN_ATTR = "_context_attribution_breakdown"  # latest per-turn breakdown dict
_ITEMIZED_ATTR = "_context_attribution_itemized"    # (split key, system rows, tools row) memo
_LAST_PROMPT_ATTR = "_context_attribution_last_prompt"  # billed prompt tokens of the last completed call
_PROJECTED_ATTR = "_context_attribution_projected"      # Strands' pre-call projection, as handed to the hook
_SNAPSHOT_FULL_ATTR = "_context_attribution_snapshot_full"  # native count of the request the split measured

# Process-level memo of the stable split, keyed by *session and configuration*
# rather than by ``Agent`` instance. The instance attribute above is enough
# only while one Agent serves a session for its whole life; it does not, for
# every session whose injected tools keep it out of the agent cache (the
# spreadsheet-analysis family — see `apis/shared/tools/injected.py`), for
# `@`-mention turns, and for any Memory-Space binding. Those rebuild the Agent
# every turn and, without this memo, paid the two cold-start CountTokens calls
# every turn as well — on top of the SDK's own pre-call count, which is the
# "counted three times per turn" of docs/specs/load-test-assessment-2026-09.md
# §1 fix 1. The key carries a digest of the system prompt and of the full tool
# specs, so any configuration change that would move the split misses cleanly.
_SPLIT_MEMO_MAX = 512
_split_memo: "OrderedDict[Tuple[str, str, str], Dict[str, int]]" = OrderedDict()
_split_memo_lock = threading.Lock()


def clear_split_memo() -> None:
    """Drop every memoised split (tests, and any admin reset path)."""
    with _split_memo_lock:
        _split_memo.clear()


def _digest(payload: Any) -> str:
    try:
        raw = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _memo_key(session_id: str, prompt_payload: Any, specs: Any) -> Tuple[str, str, str]:
    """``(session, prompt digest, tool-spec digest)``. The digests serialize
    the whole prompt and every tool schema, so this runs in a worker thread."""
    return (session_id, _digest(prompt_payload), _digest(specs))


def _memo_get(key: Tuple[str, str, str]) -> Optional[Dict[str, int]]:
    with _split_memo_lock:
        split = _split_memo.get(key)
        if split is not None:
            _split_memo.move_to_end(key)
        return dict(split) if split is not None else None


def _memo_put(key: Tuple[str, str, str], split: Dict[str, int]) -> None:
    with _split_memo_lock:
        _split_memo[key] = dict(split)
        _split_memo.move_to_end(key)
        while len(_split_memo) > _SPLIT_MEMO_MAX:
            _split_memo.popitem(last=False)


# The fixed user message the system prompt is counted against. Its own weight
# (message scaffolding + the two-letter text) is a per-model constant, so it is
# measured once per model id per process and subtracted. Keep it short and
# never change it casually: a different probe changes every systemTokens
# figure that follows, so the ledger's shares stop being comparable across the
# deploy.
_PROBE_MESSAGES = [{"role": "user", "content": [{"text": "hi"}]}]
_probe_baselines: Dict[str, int] = {}
_probe_lock = threading.Lock()


def clear_probe_baselines() -> None:
    """Drop the per-model probe weights (tests)."""
    with _probe_lock:
        _probe_baselines.clear()


def _probe_model_key(model: Any) -> Optional[str]:
    """Memo key for the probe baseline: the model id when the model exposes
    one, else None (count every time — a test double, not a Bedrock model)."""
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        model_id = config.get("model_id")
        if isinstance(model_id, str) and model_id:
            return model_id
    return None


NativeCounter = Callable[..., Awaitable[Optional[int]]]


async def _probe_baseline(model: Any, counter: NativeCounter) -> Optional[int]:
    """Token weight of ``_PROBE_MESSAGES`` alone on ``model``, memoised per
    model id. ``None`` when the count can't be had natively."""
    key = _probe_model_key(model)
    if key is not None:
        with _probe_lock:
            cached = _probe_baselines.get(key)
        if cached is not None:
            return cached
    baseline = await counter(list(_PROBE_MESSAGES))
    # Memoised process-wide, so only a native answer may stick: a heuristic
    # probe weight would skew every systemTokens figure that follows.
    if key is not None and baseline is not None:
        with _probe_lock:
            _probe_baselines[key] = baseline
    return baseline


def _native_counter(model: Any) -> Optional[NativeCounter]:
    """An async counter that answers natively or ``None`` — never a heuristic.

    ``CountTokensBedrockModel.native_count_tokens`` is blocking, so it runs in
    a worker thread. A model without it that still declares an authoritative
    ``count_tokens`` (a future transport, the tests' doubles) is used through
    ``count_tokens``, with ``heuristic_count_fallbacks`` read around each call
    to turn a silent fallback into ``None``."""
    if not _token_count_is_authoritative(model):
        return None
    native = getattr(model, "native_count_tokens", None)
    if callable(native):

        async def count(messages: Any, tool_specs: Any = None, system_prompt_content: Any = None) -> Optional[int]:
            return await asyncio.to_thread(native, messages, tool_specs, system_prompt_content)

        return count

    async def count_via_model(
        messages: Any, tool_specs: Any = None, system_prompt_content: Any = None
    ) -> Optional[int]:
        before = _fallback_count(model)
        value = await model.count_tokens(
            messages=messages, tool_specs=tool_specs, system_prompt_content=system_prompt_content
        )
        if _fell_back_since(model, before):
            return None
        return int(value)

    return count_via_model


def _fallback_count(model: Any) -> Optional[int]:
    """The model's running count of heuristic fallbacks, or ``None`` when it
    keeps none (a test double, another transport) — which proves nothing."""
    value = getattr(model, "heuristic_count_fallbacks", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _fell_back_since(model: Any, mark: Optional[int]) -> bool:
    """Whether any count on ``model`` fell back to the heuristic since ``mark``."""
    current = _fallback_count(model)
    return mark is not None and current is not None and current != mark


def _has_inline_attachment(messages: Any) -> bool:
    """Whether any message carries inline ``document`` / ``image`` bytes.

    The condition under which ``toolTokens`` cannot be trusted — see the module
    docstring. Cheap: a walk over content blocks, no decoding."""
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("document", "image"):
                payload = block.get(key)
                if isinstance(payload, dict):
                    source = payload.get("source")
                    if isinstance(source, dict) and isinstance(
                        source.get("bytes"), (bytes, bytearray)
                    ):
                        return True
    return False


def get_context_breakdown(agent: Any, itemized: bool = False) -> Optional[dict]:
    """Return the latest context breakdown stashed on ``agent``, or ``None``.

    By default, the three measured partitions (system / tools / messages) —
    what the numeric readers (compaction calibration, static-prefix pricing,
    interrupted-turn usage) need.

    ``itemized=True`` is the display form the final ``metadata`` SSE event and
    the persisted message row carry: Skills and Memory carved out of the system
    total, and the tools total broken down by origin (:mod:`.context_itemization`).
    Itemizing is done here, lazily, rather than in the hook, so none of it runs
    before a model call; it is memoized per agent on the measured split, so a
    turn pays for it at most once — after the model has answered.
    """
    breakdown = getattr(agent, _BREAKDOWN_ATTR, None)
    if not itemized or not isinstance(breakdown, dict):
        return breakdown
    try:
        return _itemize_breakdown(agent, breakdown)
    except Exception as e:  # noqa: BLE001 - itemizing is a display nicety
        logger.debug("Context itemization skipped: %s", e)
        return breakdown


def _itemize_breakdown(agent: Any, breakdown: dict) -> dict:
    by_key = {p.get("key"): p for p in breakdown.get("partitions") or [] if isinstance(p, dict)}
    if "system" not in by_key or "tools" not in by_key:
        return breakdown
    system_tokens = int(by_key["system"].get("tokens") or 0)
    tool_tokens = int(by_key["tools"].get("tokens") or 0)

    memo_key = (system_tokens, tool_tokens)
    cached = getattr(agent, _ITEMIZED_ATTR, None)
    if isinstance(cached, tuple) and len(cached) == 3 and cached[0] == memo_key:
        system_parts, tools_partition = cached[1], cached[2]
    else:
        system_parts = _system_partitions(agent, system_tokens)
        tools_partition = _tools_partition(agent, tool_tokens)
        setattr(agent, _ITEMIZED_ATTR, (memo_key, system_parts, tools_partition))

    rest = [p for key, p in by_key.items() if key not in ("system", "tools")]
    return {
        **breakdown,
        "partitions": [*(dict(p) for p in system_parts), dict(tools_partition), *rest],
    }


def get_prefix_token_split(
    agent: Any,
    prompt_tokens: Optional[int] = None,
) -> Optional[Dict[str, int]]:
    """The stable ``{"system": n, "tools": n}`` split for this agent, or ``None``.

    Persisted on each call's cost row (as ``prefixTokens``) so the static
    prefix a session carries — and which part of it is tool schemas — is a
    stored fact rather than a scan-and-guess. Same numbers the SSE breakdown
    reports; this just reads the cached split without re-counting.

    ``prompt_tokens`` is the turn's real prompt size from provider-reported
    usage. When given, a split whose ``system + tools`` exceeds it is dropped
    rather than persisted: the static prefix is a subset of the prompt, so that
    is arithmetically impossible and means the cached residual is stale or
    corrupt (see ``apis.shared.observability.prefix_tokens``). Dropping follows
    the ledger's convention that an absent ``prefixTokens`` reads "not tracked".

    The stale split is deliberately **not** invalidated here. Recomputing costs
    two CountTokens calls, and if the underlying estimator disagreement is
    systematic for this session it would pay them on every turn — trading a
    wrong number for a latency regression. The session simply reports "not
    tracked" from this point on.
    """
    split = getattr(agent, _SPLIT_ATTR, None)
    if not isinstance(split, dict):
        return None
    try:
        system_tokens = int(split.get("systemTokens") or 0)
        tool_tokens = int(split.get("toolTokens") or 0)
    except (TypeError, ValueError):
        return None
    if not prefix_split_is_plausible(system_tokens, tool_tokens, prompt_tokens):
        logger.warning(
            "Prefix split dropped as implausible: system=%d tools=%d exceed "
            "the turn's %s-token prompt",
            system_tokens,
            tool_tokens,
            prompt_tokens,
        )
        return None
    return {"system": system_tokens, "tools": tool_tokens}


def _token_count_is_authoritative(model: Any) -> bool:
    """Whether ``model.count_tokens`` is a real count rather than a heuristic.

    Only Bedrock Converse serves one. ``CountTokensBedrockModel`` subclasses
    ``BedrockModel``, so the isinstance check covers our Converse path and
    excludes the OpenAI surfaces (``bedrock-responses``, ``mantle``) whose
    ``count_tokens`` silently degrades — see the module docstring for the
    measurement.

    Deliberately a capability check on the model object rather than a provider
    allowlist: the thing that actually decides this is which class implements
    ``count_tokens``, and a name list would drift from it.

    A model may override the answer by declaring a boolean
    ``token_count_is_authoritative`` attribute. That is the extension point for
    a future transport that gains a real counter (and what the tests use to
    exercise both branches without pretending to be a ``BedrockModel``).
    ``CountTokensBedrockModel`` declares it as a property over the SDK's skip
    list, because being a ``BedrockModel`` is not enough: a Converse model
    Bedrock will not count (Claude Sonnet 5) only ever returns the heuristic.

    Args:
        model: The Strands model backing this agent.

    Returns:
        ``True`` when the split can be trusted; ``False`` to skip it.
    """
    declared = getattr(model, "token_count_is_authoritative", None)
    if isinstance(declared, bool):
        return declared
    try:
        from strands.models.bedrock import BedrockModel
    except Exception:  # noqa: BLE001 - never let a probe break a turn
        return False
    return isinstance(model, BedrockModel)


def _prompt_tokens(usage: Any) -> Optional[int]:
    """The whole prompt a call was billed for — input plus cache read and
    write. The same total Strands' own projection baselines on."""
    if not isinstance(usage, dict):
        return None
    try:
        total = (
            int(usage.get("inputTokens") or 0)
            + int(usage.get("cacheReadInputTokens") or 0)
            + int(usage.get("cacheWriteInputTokens") or 0)
        )
    except (TypeError, ValueError):
        return None
    return total or None


def _has_usage_baseline(messages: Any) -> bool:
    """Whether any assistant message carries provider usage — i.e. whether
    Strands' projection was anchored on a real count rather than built
    wholly from the heuristic."""
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            if (message.get("metadata") or {}).get("usage"):
                return True
    return False


def get_projected_input_tokens(agent: Any) -> Optional[int]:
    """Best available input size for the call now in flight, or ``None``.

    For an interrupted turn, whose call never reported usage. Prefers a native
    count of the exact request (taken by the split measurement on an agent's
    first call); otherwise Strands' projection, but only when it was anchored
    on a real usage baseline — a cold projection is the heuristic end to end
    (JSON at chars/2), and an absent figure is better than that one.
    """
    snapshot = getattr(agent, _SNAPSHOT_FULL_ATTR, None)
    projected = getattr(agent, _PROJECTED_ATTR, None)
    messages = getattr(agent, "messages", None)
    if isinstance(snapshot, tuple) and len(snapshot) == 2 and isinstance(messages, list):
        length, full = snapshot
        if length == len(messages):
            return full
    if isinstance(projected, int) and projected > 0 and _has_usage_baseline(messages):
        return projected
    return None


def _refresh_breakdown(agent: Any) -> None:
    """Rebuild the three measured partitions from the split and the last
    call's billed prompt. Pure arithmetic; runs after a model call answers."""
    split = getattr(agent, _SPLIT_ATTR, None)
    total = getattr(agent, _LAST_PROMPT_ATTR, None)
    if not isinstance(split, dict) or not isinstance(total, int):
        return
    message_tokens = max(0, total - split["systemTokens"] - split["toolTokens"])
    setattr(
        agent,
        _BREAKDOWN_ATTR,
        {
            "total": total,
            "partitions": [
                {"key": "system", "label": "System instructions", "tokens": split["systemTokens"]},
                {"key": "tools", "label": "Tools", "tokens": split["toolTokens"]},
                {"key": "messages", "label": "Messages", "tokens": message_tokens},
            ],
        },
    )


class ContextAttributionHook(HookProvider):
    """Measure the system / tools split off the critical path, and place each
    call's messages partition against the prompt the provider billed.

    Nothing here waits in front of a model call. ``BeforeModelCallEvent``
    records Strands' projection and, while an agent has no split yet, starts
    one background measurement over a snapshot of the conversation — its
    native counts run concurrently with the model call. ``AfterModelCallEvent``
    reads the call's billed prompt off the assistant message and rebuilds the
    breakdown. ``session_id`` enables the process-level split memo (see the
    module comment).
    """

    def __init__(self, session_id: Optional[str] = None) -> None:
        self._session_id = session_id or None
        # A split attempt this turn found no native count. Reset per turn, so
        # a throttled counter costs at most one attempt a turn.
        self._split_blocked = False
        self._split_task: Optional["asyncio.Task[None]"] = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterModelCallEvent, self._on_after_model_call)

    def _on_turn_start(self, event: BeforeInvocationEvent) -> None:
        self._split_blocked = False

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        """On the TTFT path: record, maybe schedule, return. No I/O, no
        digests, no formatting — those all run in the background task."""
        try:
            agent = event.agent
            setattr(agent, _PROJECTED_ATTR, event.projected_input_tokens)
            if getattr(agent, _SPLIT_ATTR, None) is not None or self._split_blocked:
                return
            if self._split_task is not None and not self._split_task.done():
                return
            counter = _native_counter(agent.model)
            if counter is None:
                return
            messages = list(agent.messages)
            self._split_task = asyncio.get_running_loop().create_task(
                self._measure_split(agent, counter, messages)
            )
        except Exception as e:  # noqa: BLE001 - attribution must never break a turn
            logger.debug("Context attribution skipped: %s", e)

    def _on_after_model_call(self, event: AfterModelCallEvent) -> None:
        try:
            response = event.stop_response
            if response is None:
                return
            usage = ((response.message or {}).get("metadata") or {}).get("usage")
            total = _prompt_tokens(usage)
            if total is None:
                return
            setattr(event.agent, _LAST_PROMPT_ATTR, total)
            _refresh_breakdown(event.agent)
        except Exception as e:  # noqa: BLE001 - attribution must never break a turn
            logger.debug("Context attribution skipped: %s", e)

    async def _measure_split(self, agent: Any, counter: NativeCounter, messages: List[Any]) -> None:
        try:
            await self._measure_split_inner(agent, counter, messages)
        except Exception as e:  # noqa: BLE001 - a background measurement never surfaces
            logger.debug("Context attribution split failed: %s", e)

    async def _measure_split_inner(self, agent: Any, counter: NativeCounter, messages: List[Any]) -> None:
        system_prompt = getattr(agent, "system_prompt", None)
        system_prompt_content = getattr(agent, "_system_prompt_content", None)
        if system_prompt_content is None and system_prompt:
            system_prompt_content = [{"text": system_prompt}]

        # Read on the event loop (the registry is not ours to touch from a
        # worker thread); only the serialization below leaves it.
        tool_specs = agent.tool_registry.get_all_tool_specs()
        memo_key: Optional[Tuple[str, str, str]] = None
        if self._session_id:
            memo_key = await asyncio.to_thread(
                _memo_key, self._session_id, system_prompt_content or system_prompt, tool_specs
            )
            split = _memo_get(memo_key)
            if split is not None:
                # A predecessor Agent for this session + configuration already
                # measured it; adopt without spending any counts.
                setattr(agent, _SPLIT_ATTR, split)
                _refresh_breakdown(agent)
                logger.debug("Context attribution split adopted from session memo")
                return

        if _has_inline_attachment(messages):
            # Untrustworthy residual (see module docstring) — try again on a
            # later call without inline bytes.
            logger.debug("Context attribution deferred: inline attachment in context")
            return

        # Bedrock refuses an empty message list, so the system prompt is
        # counted against a fixed probe message whose own weight is
        # subtracted. `no_tools` and `full` count the same snapshot, so the
        # tools residual is between two native counts of one conversation.
        probe_only, system_with_probe, no_tools, full = await asyncio.gather(
            _probe_baseline(agent.model, counter),
            counter(list(_PROBE_MESSAGES), None, system_prompt_content),
            counter(messages, None, system_prompt_content),
            counter(messages, tool_specs, system_prompt_content),
        )
        if None in (probe_only, system_with_probe, no_tools, full):
            logger.debug("Context attribution deferred: a count had no native answer")
            self._split_blocked = True
            return
        split = {
            "systemTokens": max(0, system_with_probe - probe_only),
            "toolTokens": max(0, full - no_tools),
        }
        setattr(agent, _SPLIT_ATTR, split)
        setattr(agent, _SNAPSHOT_FULL_ATTR, (len(messages), full))
        if memo_key is not None:
            _memo_put(memo_key, split)
        _refresh_breakdown(agent)


def _system_partitions(agent: Any, system_tokens: int) -> list:
    """The measured system total, itemized (skills, memory, instruction
    sections) — or as one partition if itemizing fails."""
    try:
        return itemize_system(agent, system_tokens)
    except Exception as e:  # noqa: BLE001 - itemizing is a display nicety
        logger.debug("System itemization skipped: %s", e)
        return [{"key": "system", "label": "System instructions", "tokens": system_tokens}]


def _tools_partition(agent: Any, tool_tokens: int) -> dict:
    """The measured tools total, with per-origin children when there is more
    than one origin."""
    partition: Dict[str, Any] = {"key": "tools", "label": "Tools", "tokens": tool_tokens}
    try:
        children = itemize_tools(agent, tool_tokens)
    except Exception as e:  # noqa: BLE001 - itemizing is a display nicety
        logger.debug("Tool itemization skipped: %s", e)
        children = None
    if children:
        partition["children"] = children
    return partition
