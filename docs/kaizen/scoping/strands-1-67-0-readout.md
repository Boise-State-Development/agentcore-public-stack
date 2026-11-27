# Strands 1.65.0 → 1.67.0 readout
> Reviewed 2026-11-27 with the `strands-release-review` skill, inside the `kaizen-research` run. Pin: 1.65.0 (`strands-agents==1.65.0`, `strands-agents[bidi]==1.65.0`). Releases in range: python/v1.66.0 (2026-11-23), python/v1.67.0 (2026-11-25).
> Wheels: sha256 verified from PyPI for 1.65.0, 1.66.0 and 1.67.0. Python files changed 1.65.0 → 1.67.0: 88, including new modules `models/_cache_points.py`, `models/_provider_name.py`, `types/offload.py`, `vended_plugins/cost_estimator/`, `vended_plugins/todos/`, `decisions/sagemaker_decision_model.py`, the public `context_manager/` package, and `_context_manager/archive.py` (which replaces `stash.py`). Requires-Dist: only optional extras moved (`genai-prices` for the new `cost-estimator` / `all` extras; `beautifulsoup4`, `markdownify` and `ollama` floors; dev `mypy`). The core `mcp`, `boto3` and `anthropic` ranges are unchanged, so `tests/supply_chain/test_strands_agentcore_pairing.py` should not need a change.

## Verdict

**Bump with two checks and no blocking gate.** Nothing on our Bedrock Claude path changes bytes by reading of the diff: the Converse cache-point rewrite (#6082/#6149) is gated on Nova ids (`_limits_cache_points`), and the shared `cache_point()` helper builds the same `{"type": "default"[, "ttl"]}` dict. The golden-request test could not be run here, because the main checkout's venv is stale (it imports mcp 1.x while the lock pins 2.3.0), so that proof is owed by the bump PR. The private names we import (`models._defaults.get_context_window_limit`, `types._events.ToolResultEvent`, `bedrock._SKIP_COUNT_TOKENS_MODELS`, `mcp_client.ClientSession`, `Agent._interrupt_state`) all still exist. Standing watch 2a's #4618 tell is unchanged: `_total_prompt_tokens` is still at `telemetry/metrics.py:184`, so there is no usage-convention gate. There is one Adopt (#6218, an empty `tool_use` stop now retries instead of ending the turn) and one Subtract candidate (#5933 `sort_tools`). The release also ships one real unlock: dynamic tool loading (#5699), the cache-stable tool search that last week's queue item modelled on Pydantic AI.

## Prompt-cache impact

- **Bedrock Claude (Converse, `CountTokensBedrockModel`)**: no change, reasoned rather than measured. The new cache-point code paths take effect only where `_converse_limits_cache_points(model_id)` is true (Nova Pro/Lite/Micro/2 Lite). `_supported_ttl` returns the TTL unchanged for every other id. `cache_point()` and `step_back_over_documents()` are extractions of the old inline code. **Owed:** `tests/agents/main_agent/core/test_bedrock_golden_requests.py` on the new pin.
- **Bedrock Nova (Converse)**: changes if a deployment catalogs a Nova chat model. `auto` now resolves to cache points for Nova, which means up to 3 message points with no TTL and no tools point, plus Strands' own system point through `_should_cache_system`. That point fills the gap left because `cache_points.py:19` refuses Nova. No curated row is a Nova chat model (only `amazon.nova-2-sonic-v1:0`, voice), so this is inert for a fresh install. Reasoned, not measured.
- **Bedrock xAI / OpenAI GPT on Converse**: unchanged. Both resolve to implicit `auto` caching, which places no points; OpenAI already did before.
- **OpenAI Responses on bedrock-runtime (`BedrockRuntimeResponsesModel`)**: unchanged for every curated GPT id. #6396 moves the system prompt into a leading `developer` message only where `lookup_system_prompt_placement(model) == "message"`, which the shared info sets for GPT-6.2 and for no id we curate (the `model_info_diff` run shows no placement flip for our rows). It changes the request the day GPT-6.2 is curated (see Unlocks).
- **Mantle (`OpenAIModel`, Chat Completions)**: changes on a request with tools to GPT-5.6/6 and no `reasoning_effort`, because #6291 now sends `reasoning_effort='none'` when the host is `bedrock-mantle.*`. Reasoned. Check whether any catalog row routes a GPT-5.6/6 model to Mantle Chat Completions before the bump. If one does, its first request after deploy re-writes once, and it answers with reasoning off.
- **Gemini**: not touched by the diff beyond `model_info.json` context-window rows.

## Changes

| # | Upstream | Verdict | Where it lands for us | Why |
|---|---|---|---|---|
| 1 | #6218 retry a `tool_use` stop with no `toolUse` block (twice), then raise `ValueError` | **Adopt** | `event_loop/event_loop.py` under our turn; surfaced by `agents/main_agent/streaming/stream_processor.py` / `stream_coordinator.py` | Before, such a reply ended as a completed turn with nothing in it. Now the model is asked again, and after two misses the turn fails as an error we already stream. The bump PR checks that the retried calls' usage reaches a `C#` row (they go through `update_usage`, but our per-call rows come from the stream) and that the `ValueError` reads as a conversational `stream_error` |
| 2 | #6082/#6149 Bedrock `auto` caching for Nova, OpenAI GPT and xAI | **Inert** (watch 2a) | `apis/shared/models/cache_points.py:19`, `core/model_config.py:688` | No curated Nova chat row. If a deployment adds one, Strands now gives it message and system points, which closes most of 2a's 09-11 coverage gap upstream. Probe once with `scripts/probe_bedrock_cache_point_support.py --model-id us.amazon.nova-micro-v1:0` before deciding whether to widen `cache_points.py` |
| 3 | #5933 `Agent(sort_tools=…)`, default `False` | **Guard** now, **Subtract** later | `tests/apis/inference_api/test_tool_config_order.py` | Default off, so nothing changes at bump. The PR says sorting is meant to become the default (#5700 closed 11-18), and a flip would rewrite every live prefix once. Keep the order test as the guard. Separately, `sort_tools=True` could replace our own source-ordering of tools, at one deliberate prefix rewrite (queue item) |
| 4 | #6220 hook callbacks: awaitables awaited; a sync-only event whose callback returns a coroutine now raises `RuntimeError` | **Inert**, full suite proves it | `agents/main_agent/session/hooks/` | Every hook we register on a sync-only event is a plain function, by reading. The full suite is the proof |
| 5 | #6255 skip tool start when the agent is cancelled during `BeforeToolCall` hooks | **Inert** | `stream_coordinator.py:252` | It is observed only through `Agent.cancel()`. We cancel through the per-turn `cancel_signal`, which this path doesn't read |
| 6 | #6284 + `model_info` `temporaryMessages` true → false on Claude Haiku 5.5 | **Inert** | (none) | Applies only to Strands `InjectedBlock`s (`ContextInjector`). We inject steering and notes as plain text blocks; a grep finds no `"injected"` blocks in `backend/src` |
| 7 | #6396 GPT-6.2 system prompt as a `developer` message over Responses | **Inert** until curated | `apis/shared/models/bedrock_responses.py:410` (subclass of `OpenAIResponsesModel`) | No curated GPT-6.2 id. When it is curated, our subclass inherits the placement, so the 2a note "probe it before curating" turns into a check that our explicit-cache code (`build_prompt_cache_key`, `apply_explicit_prompt_cache`) reads the system text from `input[0]` rather than `instructions` |
| 8 | #6291 Chat Completions tools on GPT-5.6/6 send `reasoning_effort='none'` (api.openai.com and bedrock-mantle hosts only) | **Inert** unless a Mantle row exists | `apis/shared/models/mantle.py` | See Prompt-cache impact. One catalog read before the bump settles it |
| 9 | #5297 `MCPClient(auto_reconnect=…)`, default `False` | **Inert** (unlock) | `agents/main_agent/integrations/external_mcp_client.py` | Default off. See Unlocks |
| 10 | #6399 per-model usage (`usage_by_model`) and the `cost_estimator` plugin (genai-prices) | **Inert** | `apis/shared/costs/` | Not a swap: our `CostCalculator` prices from the catalog row, with long-context multipliers and cache TTL buckets that genai-prices doesn't know about |
| 11 | #6509 / #6412 / #6502 / #6030 context manager graduates out of experimental, stash renamed to archive, summarizes with the agent's model | **Inert** | `core/tool_result_offload.py:118` imports `vended_plugins.context_offloader`, which is byte-identical between the wheels | We don't use the Strands context manager. Our compaction is `TurnBasedSessionManager`'s |
| 12 | #6413 / #6046 / #6178 sandbox cancel, `cancel_background_tasks`, background-task tool name | **Inert** | (none) | No Strands sandbox or background tasks in `backend/src` |
| 13 | #4223 guardrail id and version must be set together | **Inert** | (none) | `grep guardrail_id backend/src` returns nothing |
| 14 | #6254 file session `O_NOFOLLOW`, #6261 redaction by tracking id | **Inert** | (none) | We use AgentCore Memory through `TurnBasedSessionManager`, not the file or repository managers |
| 15 | #6144 union branches kept in structured-output tool specs | **Inert** | `apis/shared/extraction/schema.py` | Our extraction schemas are built by us and sent with `strict_tools`. Strands' structured-output tool is not used |
| 16 | `ModelProxy(model, agent)` records each call's tool specs; `_without_offload_marks` runs on every stream | **Inert** for bytes; per-call cost unknown | every model call | Adds a weakref write and a message walk per call. Not benchmarked here (see below) |

Dropped as TypeScript-, CLI-, site- or harness-only (no `strands-py/src` or `shared/` files, or the harness package): #6536 (MCP tool order; its PR touches only `strands-ts/`, although the 2b subagent counted it for Python), #6070, #5575, #4683, #6088 (the harness, not the SDK), #6084 (todos plugin, vended but unused), all `decisions` items (#6085, #6395, #6304, #6307, #6191), #6225, #6148, #5259.

## Unlocks

| Upstream | Unlocks | Lands on | Costs speed / quality / cost | Next step |
|---|---|---|---|---|
| #5699 dynamic tool loading (`Offload.defer('toolSpecs').when({toolCount, schemaTokens})`) | A turn with dozens of MCP tools sends each deferred tool as its name, description and an open schema, and the model loads a full schema with `load_tools` when it needs it. The `toolConfig` prefix stays the same on every call. On `BedrockModel` this is the **stub** mode; native `defer_loading` needs `AnthropicModel`, which we don't use. `strictTools` models get every spec in full | Last week's queue item "cache-stable tool search, modelled on Pydantic AI's `ToolBridge`"; `agents/main_agent/core/agent_factory.py` | Speed: one extra model round when a deferred tool is first used. Quality: unknown, since stub schemas mean a first call may be refused and retried. Cost: a smaller prefix for users with many MCP tools, but a first-load round. All three are unknown until measured | Fold into the existing ToolBridge design note as the upstream option to measure first. It is part of the experimental-then-graduated context manager, so check that it works without adopting the rest of the context manager |
| #5297 `MCPClient(auto_reconnect=True)` | A tool call that finds its MCP session dropped rebuilds it and re-sends the call once, rather than failing the tool. Useful for long turns and quiet-watcher runs against servers that idle out | `external_mcp_client.py` | Speed: a reconnect only on failure. Quality: gain. Cost: none. Risk: a re-sent call that was **not** idempotent | Watch. Scope only if the efficiency review's tool failures show dropped-session errors. `browse_web` and the Canvas tool are `result_error`, not transport |
| `model_info` `deferredToolLoading: native` on every curated Claude | Records that Claude supports native deferred tools. The Python SDK doesn't read it | — | — | Watch: it becomes reachable if Bedrock Converse ever passes `defer_loading` |
| #6396 GPT-6.2 cached system prompt over Responses | GPT-6.2 can be curated with its system prompt in the cached prefix (opencode measured about 14k → 3 uncached tokens a step) | catalog + `bedrock_responses.py` | Cost: gain. Probe it with `probe_gpt56_cache_rates.py`'s method before curating (watch 2a rule) | Into the GPT-6.2 curation, when someone picks it up |

## model_info.json (curated ids)

```
global.anthropic.claude-sonnet-5 … us.anthropic.claude-sonnet-5-5 (all curated Claude): capabilities.deferredToolLoading: - -> native   (not read by the Python SDK)
us.anthropic.claude-haiku-5-5: capabilities.temporaryMessages: True -> False   (read by ModelProxy for InjectedBlocks only; we send none)
us.openai.gpt-5.6-luna / -sol / -terra, gpt-6-luna, gpt-6-sol: capabilities.chatCompletionsTools: - -> reasoningOff   (read by openai.py on api.openai.com / bedrock-mantle hosts)
us.openai.gpt-6-astra, us.openai.gpt-6.1-sol: capabilities.chatCompletionsTools: - -> unsupported   (same reader)
all others: unchanged (Nova Sonic, Kimi K3/K3.1 and Qwen3 Coder have no entry)
```

## The bump PR must carry

- [ ] `strands-agents==1.67.0` and `strands-agents[bidi]==1.67.0` in `backend/pyproject.toml`, and `uv.lock`, in a checkout whose venv is synced (`uv sync --extra agentcore --extra dev`, with the developer's go-ahead).
- [ ] `tests/agents/main_agent/core/test_bedrock_golden_requests.py` passes unchanged (expected), or is regenerated with the rewrite priced.
- [ ] `tests/apis/inference_api/test_tool_config_order.py` passes, which proves `sort_tools` stayed off.
- [ ] Full suite: `uv run python -m pytest tests/ -n 3`. Covers #6220's hook-type check and the graduated context manager imports.
- [ ] #6218: a unit test that a `tool_use` stop with no `toolUse` block yields a `stream_error` assistant message, not a blank completed turn, and that the retried model call's usage is metered.
- [ ] One catalog read: does any row route a GPT-5.6/6 model to Mantle Chat Completions (#6291)? If so, state the one-time rewrite and the reasoning-off behaviour in the PR.
- [ ] `format_request` / `ModelProxy` A/B benchmark on an idle machine (40 tools, a 60k-character system prompt, 41 messages), old wheel via `PYTHONPATH`. `ModelProxy` gained a per-call tool-spec record and an offload-mark walk.
- [ ] Smoke rows on dev after deploy: `ask_user_question_resume,tool_approval_resume,client_tool_call_resume,restore_after_reload,second_turn_cache`.
- [ ] Stale text: `docs/kaizen/standing-watches.md` 2a row for `bedrock_cache_points_supported()` ("upstream main (#6149/#6292, not yet released)"), now released in 1.66.0. The 2b paragraph's "#5933 adds `Agent(sort_tools=…)`" is now shipped. Both are updated in this research PR.

## Not checked

- **Golden-request test and full suite on the new pin.** The main checkout's `.venv` predates the mcp 2.x pin (`ImportError: cannot import name 'MCPError'`) on **both** wheels, and syncing it would install packages, which this unattended run doesn't do. The bump PR runs both.
- **`ModelProxy` benchmark.** Same reason.
- **Nova cache probe** (`probe_bedrock_cache_point_support.py`). It needs dev credentials and is only worth running if someone means to catalog a Nova chat model.
- **Strands docs MCP** (`strands-docs`): not used. The wheel diff and PR bodies settled every verdict.
