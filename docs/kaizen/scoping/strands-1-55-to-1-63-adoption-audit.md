# Strands 1.55 → 1.63: what we ship but haven't adopted

**Status:** audit, 2026-11-14. Read-only: wheel diff plus a grep of `backend/src`. Nothing was
built, and nothing was measured live. Each Adopt item is queued in `review-queue.md` as its own
change.
**Scope:** every `strands-agents` Python release from `python/v1.55.0` (2026-09-08) through
`python/v1.63.0` (2026-10-30), our pin on `develop` before the 1.65.0 branch. That is 14 releases:
1.55.0, 1.55.1, 1.56.0, 1.57.0–1.57.2, 1.58.0, 1.58.1, 1.59.0, 1.60.0, 1.61.0, 1.61.1, 1.62.0 and
1.63.0. 1.63 → 1.65 belongs to the linked `strands-release-review` readout and is not repeated
here.
**Answers:** the 1.62 + 1.63 half of review-queue [2026-11-03] *Strategic read of strands-agents
1.62 + 1.63*, and the years-old question behind every Strands bump: the bumps were read for what
could break us (#1012, #1694, #1850), never for what we could take.

**Method.**

1. Pulled release notes for all 14 tags (`gh release view python/vX --repo
   strands-agents/harness-sdk`). Dropped site, docs, CI, CLI, harness-package, Bidi and
   TypeScript-only commits. About 150 Python items remained.
2. For each remaining item, read the shipped code by diffing the PyPI wheels `1.54.0` against
   `1.63.0` in a scratchpad. Checked each PR's file list to confirm it is Python, because five "Python
   or both" items turned out to touch only `strands-ts/`. Grepped our import surface and opt-in config
   under `backend/src`.
3. Scored every candidate on four lenses. The first three are **speed** (TTFT, then turn latency),
   **answer quality**, and **cost** (prompt-cache stability, token waste, metering accuracy). A
   change that helps one of them has to cost the other two nothing, or say what it costs. The fourth
   lens, **capability unlock**, has its own section (§5). It is ranked separately, so new products
   don't crowd out efficiency wins.
4. Before calling anything a gap, checked it against `decisions.md`, `standing-watches.md`,
   `review-queue.md` and `research/2026-09-11` → `2026-11-13`.

**The short version.** Most of this window's value already landed: either free with the bump, or
through two subtraction PRs (#1683 removed the cache-write shim, #1694 removed
`signed_reasoning.py`). Four things are left worth doing:

- **A turn-latency win we have been leaving on the floor.** Every tool batch runs sequentially, for
  a reason that no longer exists.
- **One subtraction.** A per-turn `cancel_signal` retires our write to Strands' private
  `_cancel_signal`.
- **One adoption the standing watch says to make, and that would cost us money.** Swapping in
  upstream's prompt-cache key loses the cross-session static-prefix hit.
- **One concrete blocker** that hardens the 2026-05-18 compaction decision.

---

## 1. Ranked table

Verdicts: **Adopt** (worth its own change), **Already-have** (in our pin and already working for us,
or ours is better), **Decline** (wrong fit, or a pillar pays), **Watch** (right idea, not yet).
Ranked by expected value across the three pillars.

| # | Item | Upstream | What it does | Our state | Verdict | Speed / quality / cost |
|---|---|---|---|---|---|---|
| 1 | Concurrent tool execution, serializing only the tools that can't run in parallel | `ConcurrentToolExecutor` (pre-window); **public middleware #4976 (1.62)** is the clean seam | Runs a model-requested tool batch in parallel. An `ExecuteToolStage` middleware can hold a lock for named tools only | `agent_factory.py:372-385` forces `SequentialToolExecutor()` "to prevent concurrent browser operations … Playwright errors with NovaAct". **NovaAct and Playwright are gone.** The browser is raw CDP (`browser/cdp_client.py:7`), and `browser/session_pool.py:107` already holds a per-session `asyncio.Lock` | **Adopt (spike)** | **Speed:** a batch of N independent tools drops from Σd to ≈ max d. Three 1.5 s gateway reads save ≈ 3 s, and the 18-way fan-out in prod session `aecd387d` was 18 sequential calls. **TTFT:** 0; it runs after the first token. **Cost:** 0 tokens, `toolConfig` unchanged. Tool results land in completion order, but are appended once and persisted, so history stays byte-stable. **Quality:** 0 if unsafe tools are serialized. **Risks to settle first:** harness-sdk#4713 (open: the concurrent executor swallows a `BaseException`, so check `_CooperativeStopSignal` and `CancelledError`), interrupts raised from two tools in one batch, and the order of `agent_status` / `ui_resource` frames |
| 2 | Per-turn `cancel_signal` in place of `agent.cancel()` plus a private clear | `cancel_signal=` on `stream_async` (pre-window); #4833 (1.62) docs now steer callers to it: "for cancellation driven from outside the agent … pass a `cancel_signal`" | The caller owns a `threading.Event` per invocation. Strands mirrors it onto its internal signal, MCP in-flight calls included, and never stores it on the agent | `routes.py:221-248` calls `agent.cancel()`. `stream_coordinator.py:221-248` (`reset_cancellation_state`) then has to reach into Strands' **private** `_cancel_signal` to clear a cancel that landed between turns, which is the sticky-Stop bug class (#741/#751) | **Adopt (Subtract)** | **Speed:** one watcher task per turn (µs). **Cost:** 0. **Quality:** removes a whole failure class, because a per-turn event cannot leak onto the cached agent, and it drops a private-attribute dependency that can break on any bump. `session_manager.cancelled` stays, because `StopHook` and the coordinator read it |
| 3 | Keep our prompt-cache key, and correct the standing watch that says to replace it | #4083 (1.56): auto `prompt_cache_key = strands-<session_id>` via `_openai_cache.apply_cache_config` | Session-scoped cache routing for the OpenAI family, active only when `cache_config` is passed | `bedrock_responses.py:274-294` derives the key from **system-prompt + tool-spec fingerprints**, so same-prefix requests from *different* conversations share one cache. `standing-watches.md:33` says "Adopt on the next pin bump" | **Already-have (ours is better)**, plus a docs fix | **Cost:** adopting upstream's key would make every new GPT conversation's first call write the static prefix (30k+ tokens at the write premium) instead of reading it. Also note 1.64's `cache_config` checkpoint hazard (+11.8%, the 1.65 readout). Measure with `probe_gpt56_cache_rates.py` before touching it. Speed and quality: 0 |
| 4 | Request-wide `InvocationContext` (limits + usage across the agent tree) | #5288 (1.63, Python port of #4667); `structured_output_attempts` #4926 (1.63) | `Agent(limits=…)` now bounds turns and tokens across nested agents, Graph/Swarm and aux calls. `result.invocation_context.usage` covers the whole request | Already queued, unbuilt: [2026-07-17] *Adopt Strands `Limits` on the headless lane*. The interactive lane has **no** per-turn cycle cap (no `limits=` anywhere in `backend/src`) | **Watch** (refresh the queued item) | **Cost:** caps a runaway loop's spend, which matters most unattended. The 1.63 change is that the cap now covers sub-agents, so build it once, before handoffs/consults add nested agents. 0 tokens, ~0 latency (a check at each cycle boundary) |
| 5 | Head+tail offload preview | #5602 (1.63) | `ContextOffloader` preview keeps 60% head + 40% tail with an elision marker, where it used to keep the head only | We use it through `BoundedToolResultOffloader` (`tool_result_offload.py:180`) at a 4,000-token cap and a 1,000-token preview (`constants.py:211-213`), default on | **Already-have (free)** | **Quality:** the end of a long result (a log's final error, a CSV's totals) is now visible without a retrieval call. Same 1,000-token size, so cost and speed are 0. Worth one eval case to lock it in |
| 6 | Offloader rewrite ordered after user hooks | #5601 (1.63) | `_handle_tool_result` registers at `order=70`, after user `AfterToolCall` hooks | Our hooks read the full result before it is offloaded. Covered by the 1.63 readout | **Already-have** | **Quality:** hooks such as `tool_census` see the full result. 0 cost |
| 7 | Cache-write tokens on the OpenAI paths | #4361 (1.57), #4193 (1.57.2, ours), #4943 (1.58.1) | Maps `cache_write_tokens` → `cacheWriteInputTokens` and keeps reported cache usage | Shim deleted in #1683. `usage_normalization.py:33-42` documents it, and `TestStrandsSdkContract` pins it. **`standing-watches.md:31` still lists the row as pending** | **Already-have** (docs row stale) | **Cost/metering:** GPT-5.6 write premium priced, `wastedUsd` real. Done |
| 8 | Signed-reasoning hygiene across model families | #5166 (1.61.1), #5171 drop_block retry (1.62) | Drops reasoning blocks a target model rejects, and retries with `drop_block` when a history edit invalidates replayed thinking | `signed_reasoning.py` deleted in #1694. #1890 ships the 5.5 thinking dict with `drop_block` | **Already-have** | **Quality:** a model switch no longer fails the turn. The Claude → GPT direction is still uncovered (OpenAI-family classes, #1698); not re-proposed |
| 9 | Auto caching for Claude ids missing from `model_info` | #5697 (1.63), `model_info` #5627 (1.63) | `_cache_strategy` reads `model_info.capabilities.prompt_caching` and keeps the claude/anthropic name test for unlisted ids | `bedrock_cache_points_supported()` (`model_config.py:480-520`) is documented against exactly this | **Already-have** | **Cost:** a new Claude id keeps caching on day one. Nova is still listed without `promptCaching`, so the Nova Micro coverage gap (queue [2026-09-11]) is unchanged |
| 10 | Free correctness fixes on the turn path | #4067 (1.61.1), #4625 (1.57.2), #4426 (1.57), #5434 (1.63), #4124 (1.55), #4693 (1.58), #5158 (1.61.1) | A text block was dropped beside a `toolUse`; non-dict `toolUse.input` broke replay; schema normalization mutated caller specs; blank prompts poisoned history; missing `reasoningText`; blank `guardContent`; OpenAI tool-result adjacency with injected text | All in the pin. Nothing local to retire: no defensive spec copies, no blank-prompt guard. `[]` (the max_tokens Continue prompt) still passes #5434's check, because an empty list yields no message | **Already-have** | **Quality:** silent data loss and bricked sessions closed. #4426 is prompt-cache adjacent: a mutated spec could have moved `toolConfig` bytes. Cost and speed: 0 |
| 11 | MCP hang on a dropped connection | #4499 (1.58) | No hang when the server drops during an in-flight call | In pin (memory: a hang now means a stale venv) | **Already-have** | **Speed:** no 600 s stalled turns on an MCP drop |
| 12 | `after_current_tools=True` / `message=` on `agent.cancel` | #4833 (1.62) | Stop after the current tool batch finishes, optionally with a final message | Stop cancels immediately (`routes.py:248`) | **Watch** (capability, §5) | **Quality:** a Stop pressed mid-batch keeps the tools' work and leaves no orphaned `toolUse` to repair. **Cost:** the batch's tool tokens are paid instead of discarded. It's a product choice (handoffs §8.8), not an efficiency, so not queued |
| 13 | Public `Model.model_info` (limits + capabilities) | #5627 (1.63) | Bundled `model_info.json` exposes context window, max output, `promptCaching`, `forcedToolChoice`, `nativeStructuredOutput` | `context_window.py:70` imports private `strands.models._defaults.get_context_window_limit` (still re-exported in 1.63). `CountTokensBedrockModel` memoizes `model_info` | **Watch** | Second source only: its Sonnet 4.6 max output is wrong (128K vs the card's 64K), and the model cards stay primary. The lookup is still private (`_model_info.lookup_model_info`), so swapping the import buys nothing until upstream exports one |
| 14 | Upstream `ContextManager` (stash, offload strategies, presets, session support) | #4231, #4146, #4187, #4118 (1.55); #4254, #4282 (1.56); #5075 (1.61); #5121 (1.61.1) | An experimental replacement for conversation managers, with a durable stash of offloaded content | `TurnBasedSessionManager(AgentCoreMemorySessionManager)` (`turn_based_session_manager.py:233`), and `AgentCoreMemorySessionManager` subclasses `RepositorySessionManager` | **Decline (blocked), new evidence** | **Since 1.63 (#5446) a `context_manager` raises on any `RepositorySessionManager`**, because each message is persisted once and its edits would not be saved. So beyond decisions.md 2026-05-18's four gaps, it cannot run on our session manager at all; it needs `SnapshotSessionManager`. Recorded so no one re-proposes it as a swap |
| 15 | Summarizing-manager fixes | #4402 (1.57), #5292/#5293 (1.62), #5603, #5691 (1.63) | Text-only re-roled summaries, tool calls rendered as text in summarizer input, truncation fallback | We don't use `SummarizingConversationManager`. Compaction is ours, and `build_conversation_manager` is the sliding window | **Decline (N/A)** | Our summarizer is a separate code path, so these fixes don't reach it. One idea worth borrowing: render tool calls as text in summarizer input (#5293). Check whether our summarizer already does before filing |
| 16 | `web_fetch` vended tool, agentic mode | #4001 (1.55); aux-model summarizers in harness (#4853, 1.60) | Fetches a page and has an analyst model answer a question about it, so the page never enters main context | `local_tools/url_fetcher.py:81,122` returns up to 50,000 chars (~12.5k tokens). The offloader then caps anything over 4,000 tokens at a 1,000-token preview | **Decline** | **Cost:** the offloader already bounds main-context growth, so the saving is small. The analyst call is ~12k tokens on an aux model, which would have to be metered (it isn't negligible, unlike the side-channel decision's calls). **Speed:** +1 model call per fetch. **Quality:** unmeasured; a lossy analyst can drop what the main model needed. Re-open only with an eval showing previews force retrievals |
| 17 | `mcp_router` vended tool | #4252 (1.57) | One `connect`/`list_tools`/`call_tool` tool in place of N MCP tool specs | Our MCP tools are full specs in `toolConfig` | **Decline** | **Cost:** shrinks `toolConfig`, but every `list_tools` result enters history, per session, uncached on first read. **Speed:** +1–2 model rounds before the first real call. **Quality:** untyped `call_tool` args lose schema guidance. The tool-search work is the better path |
| 18 | `aux_model` | #4658 (1.58) | A cheaper model for Strands-internal side calls (summarization, memory extraction, web_fetch, steering) | None of those Strands internals run in our stack. Our side calls (titles, tool summaries, extraction) are our own clients | **Decline (N/A)** | — |
| 19 | MCP `on_tools_changed` refresh (2.x) | #4112 (1.55) | Re-lists tools on a server's list-changed notification | Off: opt-in by registering the callback. We are on mcp 2.3.0 | **Decline** | **Cost hazard if adopted:** a mid-session refresh rewrites `toolConfig`, which is a full prefix re-write. Only acceptable deferred to the next turn's build, sorted, and counted as an explained miss |
| 20 | Tool registry removal | #5444 (1.63) | Removes a tool from a live agent | Tool changes rebuild the agent via the config-keyed cache | **Decline** | Any removal rewrites `toolConfig` anyway. No speed gain over a cache-miss rebuild that's worth the shared-state risk |
| 21 | `Agent.shutdown()` | #4519 (1.57.1) | Flushes `memory_manager` at end of life | We have no Strands `memory_manager` | **Decline (N/A)** | — |
| 22 | Structured-output attempt limits / forced retry by name | #4926 (1.63), #4263 (1.56) | Bounds failed structured-output retries | `document_extract` calls `BedrockModel.structured_output` directly, not through the agent loop | **Decline (N/A)**; see the queued [2026-11-03] `document_extract` mechanism entry | — |
| 23 | Bedrock `requestTimeout` | #4408 (1.57) | Per-request timeout | **TypeScript only** (the PR touches only `strands-ts/`). research/2026-09-25 and reviews/2026-09-25 listed it as a Python unlock "against the 600 s SSE ceiling". It isn't one. Our `read_timeout` (`model_config.py:361,668`) is the Python equivalent | **Decline (not Python)** | Correction for the record |
| 24 | OTel baggage propagation | #4840 (1.60) | Propagates baggage to outbound calls | The source of the `session.id` baggage reaching third-party MCP servers; fix in our PR 1957 | **Decline (hazard, tracked)** | — |

---

## 2. Top 5, with next steps

1. **Spike concurrent tool execution (#1).** The reason we run every batch sequentially is a
   NovaAct/Playwright constraint from code we deleted.
   - **Next:** an `ExecuteToolStage` middleware with an allowlist of parallel-safe tools: MCP
     gateway reads, `document_read`, KB/project search, `verify_sources`, web search. Anything that
     writes a shared resource (browser, Code Interpreter session, the Office writers, artifact
     writes, `my_skill_*`) takes one per-session lock. Swap `SequentialToolExecutor` for
     `ConcurrentToolExecutor` behind a rollout switch.
   - **Settle before merge:** #4713 (`BaseException` swallowed), whether two interrupts in one batch
     pause cleanly, and `agent_status` / `ui_resource` frame order. Add a hermetic test of each.
   - **Measure:** the smoke turn matrix plus a three-tool fan-out on dev. Report the turn-duration
     delta from `turnDurationMs`, and the tool-batch share of prod turns from the release ledger's
     `#TOOL#` rows.
2. **Pass a per-turn `cancel_signal` (#2).**
   - **Next:** create a `threading.Event` per turn, pass it to `agent.stream_async(…,
     cancel_signal=…)` at `stream_coordinator.py:709`, and have `_mark_session_cancelled` set it.
     Delete the `_cancel_signal` half of `reset_cancellation_state`.
   - **Keep:** `session_manager.cancelled` and its reset.
   - **Test:** the sticky-Stop regression tests, plus "a cancel armed between turns does not stop
     the next turn", which the per-turn event makes true by construction.
3. **Correct the two stale prompt-caching standing-watch rows (#3, #7).** Row 1 (`usage_normalization`
   shim) retired in #1683. Row 3 says to adopt `apply_cache_config` and retire
   `build_prompt_cache_key`. That would trade a cross-session key for a session-scoped one, and since
   1.64 it also turns on the +11.8% checkpoint. Rewrite it as "keep ours; the upstream key is
   narrower". This is a docs change that prevents a cost regression someone would otherwise ship from
   the watch's own instruction.
4. **Refresh the [2026-07-17] headless `Limits` entry with #5288 (#4).**
   - **Next:** note that limits and usage are now request-wide across nested agents. Build it on
     `InvocationContext` before handoffs and consults add nested agents, so it is built once.
   - **Separately decide** whether the interactive lane gets a generous `turns` cap. It has none
     today, so a text-form tool loop is bounded only by the 600 s stream.
5. **Record #5446 against decisions.md 2026-05-18 (#14).** Upstream's context manager now refuses
   to run on a `RepositorySessionManager`, and ours is one. That is a concrete, checkable blocker.
   Re-opening requires a session-manager migration as well as the four gaps already listed.

---

## 3. Subtract (upstream now does something we built)

| Ours | Upstream | State |
|---|---|---|
| `reset_cancellation_state`'s write to private `_cancel_signal` (`stream_coordinator.py:245-248`) | `stream_async(cancel_signal=…)`, an invocation-scoped external signal | **Adopt** (#2 above) |
| `usage_normalization` cache-write recovery | #4193 / #4361 | Done (#1683) |
| `signed_reasoning.py`, the Claude reasoning projection | #5166 | Done (#1694) |
| Deprecated `cache_tools` key | `CacheConfig.tools_ttl` #3985 | Done (`model_config.py:660-664`) |
| Tracking a message replaced in place | `MessageUpdatedEvent` (new in this window) | Adopted already (`turn_based_session_manager.py`) |

**Not a subtraction, despite the watch:** `build_prompt_cache_key` (#3 above). Neither is
`bedrock_cache_points_supported`, which was settled by measurement on 2026-09-11 and is unchanged by
#5697.

---

## 4. Already received free with the pin

#4067, #4625, #4426, #5434, #4124, #4693, #5158, #4499, #5602, #5601, #5697, #4221 (nested Bedrock
prefixes in the context-window lookup), #4694 / #4998 (5.5-family windows; our catalog still wins),
#5016 (one `reasoningContent` warning per request on `OpenAIModel`), #4186.
There is nothing to wire for any of them. The useful action is a regression test where none exists:
#4067 (text beside a `toolUse`) and #5602 (head+tail preview) are the two whose loss would be silent.

---

## 5. Capability unlock (new things we could offer)

This is the fourth lens. It is scored apart from the table in §1, in the same columns as the
`strands-release-review` skill's Unlocks table (PR #2080), so this audit and the per-release
readouts line up. An unlock is never part of a bump, and it carries the three-pillar rule: it
costs speed, quality and cost nothing, or it names the cost. Every row below is unused in
`backend/src` on `develop` (pin 1.63.0). I have not re-checked them against 1.65.

| Unlock | Upstream | What it unlocks | Where it lands | Speed / quality / cost | Next step |
|---|---|---|---|---|---|
| Public middleware | `strands.middleware` #4976 (1.62) | A clean way to wrap a tool's execution, here a lock on the tools that write shared resources | `agent_factory.py:372-385`, next to `integrations/mcp_elicitation.py` (already a middleware) | Middleware itself costs none. The concurrent-tools use saves Σd − max d per batch | **Scoping**: the queued [2026-11-14] concurrent-tools spike |
| Request-wide limits and usage across nested agents | `InvocationContext` #5288 (1.63); `structured_output_attempts` #4926 (1.63) | Per-node metering, and one turn/token budget across handoffs, consults and Agent Designer composition, without our own aggregator | patterns spec §4 "Metering" row; handoffs §8.10; the [2026-07-17] headless `Limits` entry | Speed: none (a check at each cycle boundary). Quality: none. Cost: none, and metering gets more accurate | **Watch**: adopt with the first nested agent (standing watch 2b). The queue entry is refreshed |
| Vended `subagent` / `swarm` / `graph` tools | #4841 (1.59), #4922 (1.62), #5131 (1.63) | Delegation and composition driven by the model | patterns spec §4.2 (what Strands handles vs. ours) | Cost: each child is a full prefix with its own cache, so a fan-out multiplies prefix writes. The tool spec enters `toolConfig` once. Speed: children can run in parallel | **Spec sync**: a patterns §4.2 row beside the 1.65 readout's #6093 (async child-agent builders) |
| Background tool execution | `BackgroundTasksConfig` #4115 / #4226 (1.55.x) | The model keeps working while a slow tool (browser crawl, extraction) runs | `agent_factory.py` `Agent(...)` | Cost: `agentic` mode adds a `background` property to every selectable tool's schema plus a management tool, so one `toolConfig` rewrite and per-tool tokens. `always=[…]` leaves schemas untouched. Speed: a win only for independent work | **Watch**: try `always=[one slow tool]` first |
| MCP Tasks (SEP-2663) on mcp 2.x | `MCPClient(tasks_config=…)` #4125 (1.55) | Long-running MCP tools that outlive one request | `integrations/external_mcp_client.py`, `gateway_mcp_client.py` | None. It is transport only; the result still enters history once | **Watch**: when a connector ships long-running tools |
| Client-level MCP `_meta` | `MCPClient(meta=…)` #4660 (1.61) | Request metadata on every tool call, model-driven ones included | the same two MCP clients | Speed, quality, cost: none. ⚠️ Anything sent here leaves our boundary, so it needs the review the baggage leak got (PR 1957) | **Watch**: only when a server needs it |
| Deferred cancel | `agent.cancel(after_current_tools=True, message=…)` #4833 (1.62) | "Finish what you're doing, then stop", with no orphaned `toolUse` to repair | `routes.py:221-248`; handoffs §8.8 | Quality: the batch's work is kept. Cost: the batch's tool tokens are paid instead of discarded. Speed: Stop lands later by one batch | **Watch**: a product choice; pairs with the queued per-turn `cancel_signal` |
| `DecisionModel` / `LLMDecisionModel` as a HITL risk classifier | #5125, #5588 (1.63) | Auto-approve low-risk tool calls instead of always asking | the tool-approval hook ([2026-07-10] policy-layer entry) | Speed: +1 model call per gated tool, before the tool runs. Cost: that call, metered. Quality: a wrong call approves a tool unasked | **Watch**: the 1.65 readout's #5975 `HumanInTheLoop("auto")` is the same idea with the same per-call cost |
| `handoff_to_user` vended tool | #4348 (1.57) | A reference design for an interrupt tool | `builtin_tools/ask_user_question.py` | None | **Watch**: read before building any new interrupt tool |

---

## 6. What this audit could not settle

- No live measurement. #1 (concurrency) and #3 (cache key) both carry a measurement step, and
  neither should merge on this document's estimates.
- Per-tool durations are live-only (`agent_status.durationMs`), so the prod size of #1's win needs
  the ledger's tool counts or a dev fan-out. It can't be computed from `C#` rows.
- The TS-only misreadings (#4408 here; #4863, #4569, #5065 and #4851, whose release-note lines read
  as shared or Python) suggest
  the per-release review should check each PR's file list, not the release-note line. Passed to the
  `strands-release-review` session as a method note.
