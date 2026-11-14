# Strands 1.63.0 → 1.65.0 readout
> Reviewed 2026-11-14 with the `strands-release-review` skill, against `develop` at `c11d935df`. Pin: `strands-agents==1.63.0` (and `[bidi]`).
> Releases in range: python/v1.64.0 (2026-11-04), python/v1.65.0 (2026-11-13).
> Wheels: both sha256-verified from PyPI. 57 Python files and `model_info.json` changed; 7 new modules or packages (`models/_model_proxy.py`, `models/_summarize.py`, `decisions/*` ×3, `storage/embeddings/`, `storage/search/s3_vector.py`), 1 removed (`_context_manager/methods/summarize.py`). Requires-Dist: only the `anthropic` extra widened (`<1.0.0` → `<3.0.0`). We don't install that extra. `mcp` range unchanged.

## Verdict

**Bump now, with three small gates.** There is nothing in either release that the 1.64.0 hold was waiting on. The GPT-5.6 message checkpoints (#5418) turn on only when `cache_config` is passed to `OpenAIResponsesModel`, and none of our OpenAI-family configs carry one (`model_config.py:705-770`). That change gets a guard test, not a measurement. The one item that changes what our main path does every call is the new `ModelProxy` wrapper. It's a no-op for us, but it sits on the TTFT path, so the bump PR owes the A/B numbers. The adoptable wins are the reasoning-block fix (#5708), stricter interrupt restore (#5654), and Nova tool-result status and overflow detection. Unlocks worth scoping: async child-agent builders (#6093), for consults, and turn-scoped `injected` notes (#5977), which stop paying for status notes on every later turn.

## Prompt-cache impact

- **Bedrock Claude (the default path): none expected; reasoned, not yet measured.** Our `CacheConfig` sets neither `ttl` nor the new `messages_ttl`, so `_messages_cache_ttl()` returns `None` exactly as `cache_config.ttl` did, and the message point stays TTL-less (`model_config.py:646-664`). `ModelProxy._prepare` returns the message list unchanged when it has no `summary`/`injected` blocks, and we write neither. The drop_block latch moved from `_format_request` into `_converse`, but it fires only after a binding 400, and we pre-set the binding for the 5.5 family. **Proof owed:** `tests/agents/main_agent/core/test_bedrock_golden_requests.py` on the 1.65.0 pin. A pass means byte-identical requests.
- **Bedrock Nova (Converse):** toolResult blocks now carry `status` (#5747, `amazon.nova` added to `_MODELS_INCLUDE_STATUS`). A Nova session with tool results in history would re-write its prefix once, but we curate no Nova chat model and `bedrock_cache_points_supported()` refuses Nova, so there is no cached prefix to lose.
- **OpenAI Responses on bedrock-runtime / Mantle:** unchanged while no `cache_config` is passed. 1.65.0's `model_info.json` now lists `promptCaching: cachePoints` for every curated GPT-5.6 and GPT-6 id. That is the trigger, and it only fires through `cache_config`.
- **OpenAI on Converse (`openai.*` via `BedrockModel`):** `BedrockModel.model_info` now reports `prompt_caching="auto"`. `_cache_strategy` resolves to `None` exactly as the old name test did. No bytes change.
- **Gemini:** tool-result images and documents are now sent as media parts (#5941). Requests with those change once. No Gemini model is curated.

## Changes

| # | Upstream | Verdict | Where it lands for us | Why |
|---|---|---|---|---|
| 1 | #4471 `resume()` raises `ValueError` on a `None` response | **Gate** | `apis/inference_api/chat/models.py:42` (`response: Any = None`); CLAUDE.md SSE rows `mcp_elicitation_required`, `user_question_required`, `browser_login_required`, `client_tool_call` (lines 119, 128–130) | A client that omits `response` today pauses forever. After the bump the turn fails with a `ValueError` from inside Strands. Reject a null `response` at the route with a streamed error and a test (research 2026-11-13 Top 5 #5), and change the "would re-raise forever" wording to "is refused". |
| 2 | #5654 restore a pending tool execution only for an activated state, and validate it | **Adopt** | resume path: `chat/routes.py:6365`, `chat/service.py:250`, `stream_coordinator.py:306` (`reset_stale_interrupt_state`) | A stale or malformed persisted execution is dropped (or raises `SessionException` naming the field) instead of replaying. That matches what `reset_stale_interrupt_state` already does by hand. Prove it with the smoke resume rows; no code change. |
| 3 | #5708 same-index signed reasoning blocks preserved | **Adopt** | every streamed turn (`event_loop/streaming.py`), and `handoff_brief.py:24` (`stream_messages`) | Two signed blocks under one index used to be joined into one block with one signature, which the next turn's thinking binding rejects. This is a pure win on the 5.5 family. It only changes how new messages are stored, so restored history is unaffected. |
| 4 | `ModelProxy` wraps every model call, `count_tokens`, `structured_output` and the sliding-window summarizer | **Gate** (benchmark) | turn path: `event_loop.py` `stream()`; `bedrock_count_tokens.py:176` (`model_info` memo still applies) | Two `any()` scans over every message's blocks per model call, then a pass-through. That's likely a few µs, but it runs before the first token on every tool round. The A/B in the bump PR settles it. `stream_messages` survives as a wrapper, so `handoff_brief.py:24` is unaffected. |
| 5 | #6061 drop_block retry adds `{"type": "adaptive"}` when no thinking config is set; latch moved to `_converse` | **Gate** (comment) | `agents/main_agent/core/model_config.py:37-40` | The comment says Strands "only retries when the request already carries a `thinking` dict". That's no longer true. Keep our up-front binding: it also sets `display: "summarized"` (the read-timeout fix) and avoids paying for a rejected request. Rewrite the comment to say why we keep it. Not a Subtract. |
| 6 | #5418 (1.64.0) GPT-5.6+ message checkpoints when `cache_config` is passed; `messages_ttl` added to `CacheConfig` | **Guard** | `model_config.py:705` `to_openai_config`, `:716` `to_mantle_config`, `:740` `to_bedrock_responses_config` | `bedrock_responses.py:160` measured an end-of-history breakpoint at **+11.8%** cost. Add a test that none of the three configs carries `cache_config`, so turning upstream caching on for the OpenAI family is a deliberate change that has to set `messages_ttl=False`. |
| 7 | `apply_cache_config` (standing watch 2a: "adopt it, retire `build_prompt_cache_key`") | **Subtract — not yet** | `apis/shared/models/bedrock_responses.py:274,348` | The swap isn't byte-identical. Upstream derives `prompt_cache_key` per session (`strands-<session_id>`), and ours is per static prefix (system prompt + tool specs fingerprint), shared across sessions. Adopting it means passing `cache_key=build_prompt_cache_key(...)` plus `messages_ttl=False`. That retires the one `setdefault` line and brings in #6's risk. Update the 2a row to say so. |
| 8 | #5840 / #5962 `Model.summarize()` base implementation, `summary` blocks, `serverSideSummary` | **Subtract** (scoping only) | `agents/main_agent/session/compaction_summary.py:403`, `apis/inference_api/chat/handoff_brief.py` | The base implementation replays the caller's own request closed by an instruction "so a caller that replays its own conversation can share its prompt cache". That's the shape of our compaction summary and handoff brief. It isn't a drop-in: `BedrockModel` turns `server_side_summary` off, and ours carry bounding, telemetry and `C#` rows. Worth one scoping item comparing request bytes. Not part of the bump. |
| 9 | #6073 Haiku 5.5 `forcedToolChoice` true → false (plus #5981 native-output enum/const) | **Guard** | `apis/shared/extraction/extractor.py:138,243` (`BedrockModel.structured_output`) | On Haiku 5.5, `structured_output` would skip the forced tool and use native output. The 14-field ceiling and D11's fallback were measured on strict forced tools. Inert today: the default `DOCUMENT_EXTRACT_MODEL_ID` is Haiku 4.5 (`infrastructure/lib/config.ts:2089`), unchanged, and neither GitHub environment overrides it. Add a test that lists the curated ids whose `model_info` has `forced_tool_choice=False`, so a bump that changes the list fails loudly and moving extraction onto one forces a re-measure. |
| 10 | #5747 toolResult `status` sent to Nova; #5746 "Input Tokens Exceeded" detected as context overflow | **Adopt** | `agent_factory.py:460` (`SlidingWindowConversationManager` reduce on overflow) | A Nova or OpenAI-style overflow now raises `ContextWindowOverflowException`, which our window manager handles, instead of surfacing as an opaque `ValidationException`. A Nova chat model, if a deployment catalogs one, now sees error status on failed tools. No cache cost (see above). |
| 11 | #5977 `ContextInjector` writes `injected` blocks; `temporaryMessages` true for the 5.5 family | **Inert** | none: we don't use `ContextInjector` (`rg ContextInjector backend/src` is empty) | `Model.model_info` reports `temporary_messages=False` unless the provider turns it on (only `AnthropicModel`), and `BedrockModel` skips injected blocks with a warning. Our per-turn notes stay plain text. |
| 12 | #5914 pre-built `client=` for the OpenAI providers | **Inert** | `apis/shared/models/mantle.py:116`, `bedrock_responses.py:376` | We mint a bearer token per request, and the docstring says a pre-built client "should not be shared across asyncio event loops". Nothing to adopt while tokens rotate. |
| 13 | #6093 async multiagent builders, `multiagent/spec.py`, `_agent_tool_streamer.py` | **Inert** | none: no `strands.multiagent` import | Record for standing watch 2b. `multiagent/` changed in 1.65.0, so the 11-13 "no change since 1.64.0" line needs updating on the next scan. |
| 14 | #5819 `Usage` docstring: providers differ on whether `inputTokens` includes cache | **Inert** | `apis/shared/models/usage_normalization.py` | Docs only. #4618 has **not** shipped: `_total_prompt_tokens` is still at `telemetry/metrics.py:184` and Bedrock usage handling is unchanged. No usage gate on this bump. |
| 15 | #4345 metrics accumulate for hook retries | **Inert** | none: our retry is a `ModelRetryStrategy`, not an `AfterModelCallEvent.retry` hook | — |
| 16 | `Agent.structured_output_async` warns on dropped system blocks; #5792 summarizer truncation; SageMaker, Mistral, Ollama, llama.cpp, Writer, LlamaAPI, sandbox, vended tools, decisions, storage | **Inert** | none of these are imported | — |
| 17 | `_openai_bedrock._OPENAI_PATH_MODEL_PREFIXES` lists `google.gemma-4-` (since 1.51.0, still on 1.65.0) | **Subtract** (carried) | `apis/shared/models/mantle.py:69` (`_ensure_gemma4_openai_v1_routing`) | Its own docstring calls it "a no-op on the pinned SDK". Deleting it retires one of our private-module imports. Small separate PR. |

Release-note items whose PR touches neither `strands-py/src/` nor `shared/` (TypeScript, CLI or site only; checked with `gh pr view --json files`), dropped: #6055 (MCP refresh keeps other tools), #5749, #5751, #5748, #5732, #5325, #5569, #6016, #5972, #5946, #3988, #6022, #5924.

## Unlocks

Scored apart from the verdicts above. Every one of these is verdict-Inert today, because nothing we run calls it yet.

| Upstream | Unlocks | Lands on | Costs speed / quality / cost | Next step |
|---|---|---|---|---|
| #6093 async agent builders; content-filtered and refused children reported as tool errors (`multiagent/spec.py`) | Child agents built through our own async turn phases (`resolve_turn_agent`, `_build_main_agent`) rather than a sync copy, and a child's refusal reaching the parent as a tool error it can act on | handoffs spec Phase 1 (consults); patterns §4.2 (what Strands handles vs. ours) | none until used; a consult's own cost is the consult's | spec-sync row for watch 2b (patterns §4.2: async builders are now upstream) |
| #5975 `HumanInTheLoop("auto")` | Approval decided per call by the SDK's risk strategy, instead of our per-tool `needsApproval` flag (`apis/shared/tool_approval/`) | the `tool_approval_required` flow | **costs speed and money:** a decision on every tool call, before the tool runs; quality unknown against our explicit flags | watch; scope only if per-tool flags become the friction, and measure the decision's latency per tool round first |
| #5977 `injected` blocks with `dynamic_trailing_blocks` | Turn-scoped notes that the model reads on their turn and that later requests drop, instead of persisting in history | per-turn notes in the user message, e.g. `<project_files_status>` (`apis/shared/assistants/kb_readiness.py:109`) | cost: saves the note's tokens on every later turn, and the cache point sits ahead of it, so it's cache-safe by design; quality: the model loses the note after its turn, which suits status notes but not facts it must keep | scoping item: which of our per-turn notes are status (inject) vs. fact (persist) |
| #5552 (1.64.0) `S3VectorSearchStrategy` for `S3Storage` | Natural-language search over offloaded tool results, in place of the default keyword scan | `ContextOffloader` storage (`agents/main_agent/core/tool_result_offload.py:233`) | **costs money and resources:** an S3 Vectors index plus an embedding per offload; speed off the first-token path | watch; scope only if offload-retrieval misses show up in evals |
| #5840 / #5962 `Model.summarize()` | One provider-neutral summarize call that shares the caller's prompt cache | compaction summary, handoff brief | none on Bedrock (no server-side summary) | already the row 8 Subtract scoping item |

## model_info.json (curated ids)

From `model_info_diff.py`. 4 of 19 curated ids have no entry (`amazon.nova-2-sonic-v1:0`, `qwen.qwen3-coder-30b-a3b-instruct`, `us.moonshotai.kimi-k3`, and `us.anthropic.claude-haiku-4-5-…`, which resolves to `claude-haiku-4-5` and is unchanged).

| Field | Ids | Read by (Python, 1.65.0) | Effect for us |
|---|---|---|---|
| `defaultEffort` added | all 15 that have an entry | **nothing**: not in `_CAPABILITY_FIELDS` | none (TypeScript reads it) |
| `forcedToolChoice` true → false | `us.anthropic.claude-haiku-5-5` | `_native_output.py:111` | row 9 |
| `serverSideSummary` → true | Haiku/Opus/Sonnet 5.5 | `ModelProxy._prepare`, `AnthropicModel.summarize` | none: `BedrockModel` forces it off |
| `temporaryMessages` → true | Haiku/Opus/Sonnet 5.5 | `ModelProxy._prepare` | none: `Model.model_info` forces it off on Bedrock |
| `promptCaching` → `cachePoints` | GPT-5.6 luna/sol/terra, GPT-6 astra/luna/sol, GPT-6.1 sol | `OpenAIResponsesModel._format_request`, `BedrockModel._cache_strategy` | none while no `cache_config` is passed (row 6); Converse overrides to `auto` |

## The bump PR must carry

- [ ] `strands-agents==1.65.0` and `strands-agents[bidi]==1.65.0` in `backend/pyproject.toml`, `uv.lock` re-resolved, `test_strands_agentcore_pairing.py` green.
- [ ] **Gate 1:** reject a null `interrupt_responses[].response` at the route (streamed error + test). Update the four CLAUDE.md SSE rows.
- [ ] **Gate 2:** rewrite the `model_config.py:37-40` comment on why we pre-set the binding.
- [ ] **Gate 3:** `format_request` / `ModelProxy` A/B on an idle machine from a synced venv (old wheel on `PYTHONPATH`), 40 tools / 60k-char system prompt / 41 messages, µs per call for both arms in the PR.
- [ ] **Guard A:** test that `to_openai_config`, `to_mantle_config` and `to_bedrock_responses_config` carry no `cache_config`.
- [ ] **Guard B:** test listing the curated ids with `forced_tool_choice=False` (today: Haiku 5.5).
- [ ] `test_bedrock_golden_requests.py` green on the new pin (or regenerated with the cost stated).
- [ ] Full suite: `uv run python -m pytest tests/ -n 3`.
- [ ] After deploy to dev: `smoke_turns.py --only ask_user_question_resume,tool_approval_resume,client_tool_call_resume,restore_after_reload,second_turn_cache`.
- [ ] `docs/kaizen/standing-watches.md` 2a: the `build_prompt_cache_key` row gets row 7's per-session vs per-prefix finding, and `CacheConfig` gained `messages_ttl`.

Follow-ups, not in the bump: row 8 (a `Model.summarize()` scoping item) and row 17 (delete the Gemma 4 bridge).

## Not checked

- **The `ModelProxy` benchmark and the golden test were not run.** This worktree has no synced venv, and installing one needs the developer's go-ahead. Both are owed by the bump PR (Gate 3 and the golden-test line).
- No live probe. Nothing in range changes a request we can't reason about from the diff, but the golden test is the measurement for Bedrock Claude.
