# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open
<!-- Newest at top. -->

> ✅ **Queue hygiene completed 2026-08-14** (at Phil's request, ahead of `kaizen-review-prep`). **Nine** stale entries were resolved: four `bedrock-agentcore` bump entries and two Strands bump entries (all **shipped** in #857 — `bedrock-agentcore` 1.9.1 → **1.21.0** at zero lag, `strands-agents` → **1.51.0**; #482 and #571 closed upstream), two nightly-CI entries (**green 12 consecutive days**), and two MCP Apps spec-prep entries (superseded now the 2026-07-28 spec is final). Genuine residue was carried forward, not dropped: **#564** (still open upstream) and the **un-adopted Strands capabilities** are now their own entries below. See `## Resolved` for the evidence trail.

### [2026-08-14] Probe Anthropic's mid-conversation tool-mutation beta on Bedrock
- **Source**: research/2026-08-14.md ▸ Top 5 #1 — https://www.anthropic.com/news/claude-opus-5 (2026-07-24): "change which tools Claude can use mid-conversation without invalidating the prompt cache" (beta). Convergent: pydantic-ai `ToolReturn.tools`/`load_capability` (v2.26.0), MCP SEP-2549 cacheable list results, Claude Code 2.1.232 fork-inherits-cache.
- **Surface**: backend (`agents/main_agent/core/model_config.py` Bedrock request construction; `_build_filtered_tools`; the `toolConfigHash` fingerprint on `C#` rows; `GET /admin/costs/sessions/{id}/calls`)
- **Effort × Impact**: L (probe) × H
- **Subtracts**: conditionally, and enormously — could narrow the prompt-cache contract's most expensive clause ("any list reaching `toolConfig` must be deterministically ordered at its source") and unblock three separately-queued items that share the same wall
- **Unlocks**: per-turn tool filtering (the real fix for MCP tool-list bloat); `@`-mention switches that don't structurally re-write a 30k–150k-token prefix; the Vercel "app-visible-only tools" and pydantic-ai tool-search patterns
- **Status**: open — **recommended #1.** A probe, not a build: (a) does Bedrock Converse expose the beta at all, or is it Claude-API-only? (b) what's the header/param, and does `strands` pass it through? (c) measured on a real session, does adding a tool mid-conversation leave `cacheReadInputTokens` intact? **A negative result is a valid outcome** — same shape as G1/G3, and it closes an assumption currently sitting under several queued items.

### [2026-08-14] Migrate the MCP Apps host off `initialize`/`serverInfo` to `server/discover`
- **Source**: research/2026-08-14.md ▸ Top 5 #2 — MCP **2026-07-28 is now the Current protocol version**; SEP-2575 removed the `initialize`/`initialized` handshake + `Mcp-Session-Id` — https://modelcontextprotocol.io/specification/versioning · https://blog.modelcontextprotocol.io/posts/2026-07-28/
- **Surface**: backend (`agents/main_agent/integrations/mcp_apps.py:673` — the `getattr(result, "serverInfo", None)` capture; `_mcp_apps_server_info` consumers ~L454–461 / L628–635; `streaming/stream_coordinator.py:1680` `ui_resource` header emission; the `ClientCapabilities(experimental=...)` subclassing at `mcp_apps.py:26`)
- **Effort × Impact**: M × M–H
- **Subtracts**: yes — retires the `initialize`-response dependency, and collapses the "fresh MCP session per call" concern behind the MCP Apps proxy-call 504 work (there is no protocol-level session left to preserve)
- **Unlocks**: conformance with a published host matrix (Claude, VS Code Copilot, M365 Copilot, Goose, Postman); readiness for **MRTR (SEP-2322)** — the sanctioned interrupt/resume shape, which would let OAuth consent and tool approvals resume *without* holding an SSE stream open against the 600s timeout; readiness for SEP-2243 header-based Gateway routing
- **Status**: open — **SUPERSEDES the [2026-07-24] "prep the MCP Apps host for the 2026-07-28 spec" item** (written when the spec was an RC; it is now final). **Two cheap verifications required before any code**: (a) confirm the UI capability identifier against the apps **spec source** — this scan could NOT confirm `io.modelcontextprotocol/ui` vs `experimental.ui`; (b) confirm `ui/notifications/tool-input-partial` still exists in the spec (absent from the SDK overview page — not evidence of removal, but our `ui_tool_input_partial` relay depends on it). Keep the handshake path as a compatibility branch: `server/discover` is **mandatory for servers, optional for clients**.

### [2026-08-14] Attack the W5 memory bill — self-managed LTM strategies + model Runtime Instances
- **Source**: research/2026-08-14.md ▸ Top 5 #3 — https://aws.amazon.com/bedrock/agentcore/pricing/ (built-in long-term strategies **$0.75/1,000 records/month** vs override/self-managed **$0.25/1,000**) + Runtime **Instances** GA (EC2 + 12% fee, **Savings Plans / ODCR eligible**, 14-day sessions) — https://aws.amazon.com/about-aws/whats-new/2026/08/aws-bedrock-agentcore-runtime-instances-generally-available/. **Verified internally**: `infrastructure/lib/constructs/agentcore/memory-construct.ts:77` configures all three built-in strategies.
- **Surface**: infrastructure / backend (`memory-construct.ts` `memoryStrategies` array + `eventExpiryDuration: 90`; `inference-agentcore-construct.ts` compute type + memory allocation; `apis/app_api/memory/routes.py` `/facts/` `/preferences/` `/summaries/` readers)
- **Effort × Impact**: M × H
- **Subtracts**: yes (track 1) — a **3× per-record price cut** on a line item we're paying the premium tier for by default, with no deliberate decision behind it
- **Unlocks**: Savings-Plans/ODCR-covered agent compute — the first commitment-discount lever AgentCore has ever offered — and 14-day persistent sessions, which changes the idle-reaper calculus (#827)
- **Status**: open — **the named W5 gap, and the first week the ecosystem handed us a real lever on it.** Split into two tracks and ship track 1's *measurement* first: (1) determine per-strategy record volume and whether each is *read* often enough to justify 3× the self-managed rate; (2) model Instances against real dev/prod session-concurrency — Instances favour many concurrent short sessions sharing an agent, penalize spiky low-utilization. ⚠️ **AWS's own Instances HN post drew 1 point / 0 comments — zero independent validation.** Model it against our numbers; do not adopt on the pitch.

### [2026-08-14] Run the Anthropic `cost_optimization` cookbook as an audit against our own contract
- **Source**: research/2026-08-14.md ▸ Top 5 #4 — https://github.com/anthropics/claude-cookbooks/blob/main/cost_optimization/cost_optimization.ipynb (2026-08-12), seven measured strategies + a `usage_cost()` helper. Reinforced by opencode v1.18.17 (turn-aligned compaction) and v1.18.14 (retry cap with jitter).
- **Surface**: backend (`core/model_config.py` cache-point placement + `CacheConfig(strategy="auto")` at L389; `session/turn_based_session_manager.py` truncation anchor + repeated-compaction path; `session/compaction_models.py` `cache_ttl_seconds`; system-prompt assembly)
- **Effort × Impact**: L × M–H
- **Subtracts**: yes — every item is "find waste and delete it"; no new abstraction, no new dependency
- **Status**: open — **highest confidence-per-hour item in the scan.** Four checks: (1) **byte-stable prefix** — grep system-prompt assembly for time/random/env-derived values (the cookbook measured a **44% swing from one `datetime.now()`**; this also tests whether we're *reading* `systemPromptHash`); (2) **layered mixed-TTL breakpoints** — 54% cheaper upstream, but ⚠️ **blocked by Strands #3758** (per-section TTLs emit a checkpoint order Bedrock rejects with `ValidationException` on every request — we set no per-section TTL today, so evaluate + document, don't adopt); (3) **repeated-compaction audit** — does pass 2 preserve tool pairing and avoid re-writing the prefix? (our death-spiral incident says this is where the money went); (4) **retry cap with jitter** — an uncapped retry on a 424/throttle re-writes the whole prefix per attempt.

### [2026-08-14] Resolve the stale review queue before `kaizen-review-prep` consumes it
- **Source**: research/2026-08-14.md ▸ Top 5 #5 — internal: this file vs. `backend/pyproject.toml` (`bedrock-agentcore==1.21.0`, `strands-agents==1.51.0`) and `gh run list` (Nightly green Aug 3–14)
- **Surface**: docs/process (`docs/kaizen/review-queue.md` only — no code)
- **Effort × Impact**: L × M–H
- **Subtracts**: yes — retired 9 superseded entries (more than the 4 first estimated); 6 chained through two bump subjects and would otherwise have been re-ranked as live work
- **Status**: ✅ **DONE 2026-08-14** — executed at Phil's request in the same PR, ahead of `kaizen-review-prep`. Resolved as *superseded by events*: (a) **four** `bedrock-agentcore` bump entries ([2026-07-24]/[2026-07-17]/[2026-07-10]/[2026-07-03]) — shipped in #857, zero lag, #482 + #571 closed upstream; **#564 residue carried forward** as its own entry; (b) **two** Strands bump entries ([2026-07-10]/[2026-07-03]) — pin is 1.51.0; un-adopted capabilities split into a new [2026-08-14] entry; (c) **two** nightly-CI entries ([2026-07-24] `DELETE_FAILED` + [2026-06-19] `exit 127`) — 12 consecutive green nights; (d) **two** MCP Apps spec-prep entries ([2026-07-24]/[2026-05-29]) — superseded by the new [2026-08-14] migration entry, which also fixes the [2026-05-29] entry's **unverified** `io.modelcontextprotocol/ui` premise; (e) the [2026-07-17] curl-pin item **down-ranked, not closed**. Note: the research skill normally does not edit `## Resolved` — that is review-prep's job — so this was done under explicit instruction and each resolved entry records that.

### [2026-08-14] Guard against `bedrock-agentcore` #564 — the one failure class the 1.21.0 bump did NOT close
- **Source**: research/2026-08-14.md ▸ Community + GitHub issues; the carried-forward residue of the four now-resolved `bedrock-agentcore` bump entries — https://github.com/aws/bedrock-agentcore-sdk-python/issues/564 (**still open**; #482 and #571 closed, #564 did not)
- **Surface**: backend (`agents/main_agent/session/turn_based_session_manager.py` restore path; `AgentCoreMemorySessionManager` `read_agent`/`read_session` marker-event lookup)
- **Effort × Impact**: M × M–H
- **Subtracts**: no — a local guard against an upstream gap; delete it if/when #564 is fixed upstream
- **Status**: open — **the bump's value was real but incomplete; this is what's left.** Metadata-filtered `ListEvents` transiently misses marker events, so the manager treats the turn as a **new session** and skips history restoration *even though the data exists in the unfiltered view*. Cost impact precedes correctness impact: a false "new session" both loses context **and** re-writes the entire cacheable prefix at the cache-write premium. Related upstream risks worth checking in the same pass: **#621** (`filter_restored_tool_context` incompatible with extended thinking — same restore path) and **#629** (TracerProvider never flushed before microVM freeze, so end-of-invocation spans are dropped — which means our own cost telemetry under-reports the final turn of every session).

### [2026-07-24] Add GPT-5.6 Terra + Luna to the model catalog via the existing Mantle Responses path
- **Source**: research/2026-07-24.md ▸ Top 5 #2 — GPT-5.6 Sol/Terra/Luna GA on Bedrock via Mantle (confirms last week's flagged-for-verification item) — https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-sol-terra/
- **Surface**: backend / cross-cutting (inference-api model config + admin catalog; `_create_mantle_model` / `ModelProvider.MANTLE`; per-model region routing — Terra/Luna→us-west-2, **Sol→us-east only, gate or omit**; `CountTokensBedrockModel` de-prefix; frontend model picker)
- **Effort × Impact**: L–M × M–H
- **Subtracts**: addition only — justified: rides the built gpt-5.4 `apiMode=responses` wiring (exercises the "dark scaffolding" non-Claude Mantle lane the [2026-07-06] watchlist flagged) + adds a cheaper non-Claude tier
- **Unlocks**: Terra (½ GPT-5.5 cost) as a cheaper agent/default tier + Luna (fast/cheap); explicit-breakpoint Mantle prompt caching at a 90% cached-input discount
- **Status**: open — **new capability story of the week; low-effort because the path exists.** Pair with the caching-audit item below so the discount is captured. Gate Sol on region availability; confirm reasoning/temperature-controls handling on the OpenAI path (don't apply Claude's controls).

### [2026-07-24] Multi-provider prompt-caching audit — now doubly-motivated by GPT-5.6 explicit-breakpoint caching
- **Source**: research/2026-07-24.md ▸ Top 5 #3 — internal issue #642; Strands #3144 (`strategy="auto"` never caches system prompt); **new**: GPT-5.6 on Mantle uses explicit cache breakpoints at a 90% discount
- **Surface**: backend (`to_bedrock_config` cache-point injection; Mantle Responses builder `build_mantle_model`/`_create_mantle_model` — now the load-bearing case; the PR #697 `cacheStatus`+fingerprint observability that now *measures* this)
- **Effort × Impact**: L–M × M–H
- **Subtracts**: yes — consolidates per-provider cache logic; kills a silent full-input-token cost regression if a Mantle/OpenAI path caches nothing
- **Unlocks**: captures GPT-5.6's 90% cached-input discount instead of leaving it on the table
- **Status**: open — SUPERSEDES the [2026-07-17] caching-audit item (adds the GPT-5.6 explicit-breakpoint motivation). Instrument `cache_read`/`cache_write` per provider (1.9.0 observability surfaces them); wire explicit breakpoints on the Mantle leg; confirm the Bedrock manual cache-point still engages post-1.48; do NOT switch to `strategy="auto"`. Coupled to the GPT-5.6 item above.

### [2026-07-19] Track harness-sdk#3348 (rolling pair of message cachePoints) — local workaround gated on dashboard evidence
- **Source**: Phil-initiated (PR #697 follow-up) — https://github.com/strands-agents/harness-sdk/issues/3348 (filed by philmerrell, open, no maintainer response yet); prod session aecd387d (18-way parallel tool fan-out → cacheRead=0 / cacheWrite=134k mid-turn, the ~20-block Anthropic lookback miss mode documented in `model_config.py:366`)
- **Surface**: backend (`agents/main_agent/core/model_config.py` 3-cachePoint budget). If built locally: strands 1.48's `_inject_cache_point` **strips any pre-existing message-level cachePoints**, so a rolling pair requires dropping `CacheConfig(strategy="auto")` and hand-placing both points via a hook — the 4th Bedrock cachePoint slot is free. Position tests in `tests/agents/main_agent/core/test_bedrock_cache_points.py` are the safety net.
- **Effort × Impact**: (track) free × M; (local workaround) M × M — but high regression risk in the highest cost-of-regression area
- **Subtracts**: no — upstream-native fix preferred; a local workaround would later be deleted in favor of it
- **Status**: open — check #3348 each scan. Do NOT build the local workaround until the `AvoidableMiss`/`WastedUsd` dashboard (PR #697 follow-up, in flight) shows the fan-out miss mode fires often enough to earn the risk.

### [2026-07-19] Spike: ContextOffloader adoption (ingestion-time tool-result offload → S3)
- **Source**: Phil-initiated (PR #697 follow-up) — strands 1.48.0 ships `ContextOffloader` as a vended plugin (`strands/vended_plugins/context_offloader/`): hooks `AfterToolCallEvent`, token-gates results (default 2,500 via `model.count_tokens`), stores oversized blocks, rewrites to preview + reference, registers `retrieve_offloaded_content`. Relates to long-open issue #266 (large tool-result offload).
- **Surface**: backend (`agent_factory.py` — `plugins=` already plumbed; a custom S3 `Storage` backend is the real build — the plugin ships InMemory/File only, and references must survive AgentCore Runtime restores across turns)
- **Effort × Impact**: M–H × M–H (payoff directly measurable by the PR #697 cache/cost metrics)
- **Subtracts**: partial — bounds MCP/tool payload growth at the source instead of relying solely on reactive below-anchor truncation in `TurnBasedSessionManager` (which stays for legacy history)
- **Status**: open — spike before commitment; four known gotchas: (1) `evict_after_cycles=20` runs on `BeforeModelCallEvent` and touches *prior* messages — potential byte-stability cache-buster, verify semantics or set `None` and expire via S3 lifecycle; (2) `model.count_tokens` per tool result adds latency, and Bedrock CountTokens rejects `us.*` inference-profile ids (de-prefix precedent in context attribution); (3) adoption flips `toolConfigHash` once (expected; the new tool must land in the deterministically-ordered tool list); (4) check SPA tool-result rendering against placeholder content.

### [2026-07-17] Audit multi-provider prompt caching (issue #642 + Strands #3144)
- **Source**: research/2026-07-17.md ▸ Top 5 #2 — internal issue #642 (Jul 11); Strands #3144 (`CacheConfig(strategy="auto")` never caches system prompt, fix PR #3145 open) — https://github.com/strands-agents/sdk-python/issues/3144
- **Surface**: backend (`to_bedrock_config` cache-point injection; Mantle Responses builder `build_mantle_model`/`_create_mantle_model`; OpenAI-compatible path; `CountTokensBedrockModel` cache-token read)
- **Effort × Impact**: L–M × M–H
- **Subtracts**: yes — consolidates per-provider cache logic; kills a silent full-input-token cost regression if a Mantle/OpenAI path caches nothing
- **Status**: open — a filed internal tech-debt issue with a library-confirmed failure mode. Instrument `cache_read`/`cache_write` ratios per provider (Strands 1.46 surfaces them); confirm the Bedrock manual cache-point still engages post-1.47 and do NOT switch to `strategy="auto"` expecting system-prompt caching. Strongest internal-signal fit after the bump.

### [2026-07-17] Adopt Strands `Limits` on the unattended Scheduled Runs / headless lane
- **Source**: research/2026-07-17.md ▸ Top 5 #3 — convergent harness rail (Claude Code 2.1.212 spawn cap + opencode 1.18.2 `subagent_depth`); Strands `Limits` now available (we're on 1.47).
- **Surface**: backend (headless/scheduled path `apis/shared/harness/run_agent_headless` per [2026-07-06] managed-Harness spike + agent-loop per-invocation config)
- **Effort × Impact**: L–M × M
- **Subtracts**: yes — retires any hand-rolled `max_turns` guard on the headless lane (library-native)
- **Unlocks**: first-class per-invocation cost/turn cap on the highest-blast-radius lane (a runaway loop there burns quota unattended) — the capability the [2026-07-03] Strands bump listed but that still needs wiring now the bump landed
- **Status**: open — scope to the scheduled/headless lane first; dovetails with the queued quota-cooldown work.

### [2026-07-17] Harden the SPA SSE parser (unterminated-frame + line-ending handling)
- **Source**: research/2026-07-17.md ▸ Top 5 #4 — assistant-stream@0.3.26 (Jul 16, spec-complete SSE decoder) — https://github.com/Yonom/assistant-ui/releases — reinforced by the 1.6.0 SSE/restore bug cluster (#653 tab-switch duplicate invocation, tool-pairing repair, single-flight lease)
- **Surface**: frontend (SPA SSE parser service — the one allowlisting `session_title` past Completed-state gating; keyed per-session)
- **Effort × Impact**: L–M × M
- **Subtracts**: yes — replaces ad-hoc frame handling with a spec-complete decode pass
- **Status**: open — pattern-only (implement in Angular signals, do NOT add the React dep): discard unterminated/partial frames + tighten line-endings on the interrupt-resume + tab-switch paths just stabilized. Defensive, lands on exactly the surface 1.6.0 hardened.

### [2026-07-17] Durable curl fix — stop pinning curl to an upstream version in the Dockerfiles
- **Source**: research/2026-07-17.md ▸ Top 5 #5 — internal friction (Nightly + Backend Deploy broke Jul 11–13 on `curl=8.14.1-2+deb13u3` "version not found"); partially fixed by floating to `deb13u*` (commit `74cd7b0a`, 1.5.0)
- **Surface**: infra/CI (`backend/Dockerfile.app-api` + `Dockerfile.inference-api` line 42 `apt-get install curl=...`; check `scheduled-runs`/`kb-sync`)
- **Effort × Impact**: L × M–H
- **Subtracts**: yes — removes a recurring deploy-breaker class; the `deb13u*` wildcard only defers the next break to a Debian series/base-image bump
- **Status**: open — **DOWN-RANKED 2026-08-14: correctness debt, not active breakage.** The `deb13u*` wildcard has held all window (Backend Deploy + Nightly green Aug 3–14), so the urgency claim in the original entry no longer applies. The fix is unchanged and still right: replace the version-pinned curl with unpinned (latest security patch) or the base image's `curl-minimal` (as the Lambda images already do). The pin was never a supply-chain control — it's a HEALTHCHECK runtime probe. The wildcard only defers the next break to a Debian series / base-image bump.

### [2026-08-14] Adopt the Strands capabilities the 1.51 bump made available but did not wire
- **Source**: research/2026-08-14.md ▸ queue-hygiene cleanup — the split-out residue of the [2026-07-10] "Strands 1.40 → 1.47" entry, whose *bump* half shipped (now pinned **1.51.0** via #857). Capability refs: `continue_on_error` MCP resilience (#3101), optional hook ordering (#2559), `cache_tools_ttl`, `context_manager="auto"` — https://github.com/strands-agents/sdk-python/releases
- **Surface**: backend (`agents/main_agent/` hooks + `to_bedrock_config` + compaction; `FilteredMCPClient`/gateway targets; `CountTokensBedrockModel`)
- **Effort × Impact**: M × M–H
- **Subtracts**: candidate — hand-rolled MCP-abort handling (`continue_on_error`) and custom cache-point plumbing (`cache_tools_ttl`) are both library-native replacements
- **Unlocks**: a flaky external/Gateway MCP server no longer aborts the turn (load-bearing — Scheduled Runs use external tools unattended); deterministic hook ordering, the enabler for the tool-approval fix
- **Status**: open — **the bump is DONE; this is only the un-adopted capability list.** `Limits` on the headless lane is tracked separately at [2026-07-17]. ⚠️ `context_manager="auto"` is **barred as a bare swap** by decisions.md 2026-05-18 — our compaction additionally does tool-content truncation, LTM summary retrieval, and DynamoDB checkpoint persistence, and drives the `compaction` SSE event; only a migration design covering all four is in scope. Also re-check `cache_tools_ttl` against Strands **#3758** (per-section TTLs can emit a checkpoint order Bedrock rejects on every request) before wiring it.

### [2026-07-10] Audit whether prompt caching actually engages in `to_bedrock_config` (Strands #3144)
- **Source**: research/2026-07-10.md ▸ Top 5 #3 — **NEW** Strands open issue #3144 (`CacheConfig(strategy="auto")` never caches the system prompt); Strands 1.46 now surfaces `cache_read`/`cache_write` tokens in the metadata chunk (#2302). https://github.com/strands-agents/sdk-python/issues
- **Surface**: backend (`agents/main_agent/` cache-point config in `to_bedrock_config`; the metadata/usage path feeding `CountTokensBedrockModel` + the context-attribution badge)
- **Effort × Impact**: L–M × M–H
- **Subtracts**: no — a cost-correctness audit (may confirm we're fine, or expose a silent full-input-token regression)
- **Unlocks**: potentially large per-turn cost cut if caching is silently off; a verifiable caching invariant
- **Status**: open — **subsumed by / merge with the [2026-07-17] "multi-provider prompt caching" item above** (same Bedrock cache-point surface + Strands #3144, extended to the Mantle/OpenAI provider shapes). Assert cache points are written *and* read via `cache_read`/`cache_write` counts; measurement is nearly free now that 1.46 surfaces the tokens.

### [2026-07-10] Tool-approval policy layer + signed approvals (Vercel AI SDK) — evolve the queued approval item
- **Source**: research/2026-07-10.md ▸ Top 5 #4 — Vercel AI SDK tool-approvals (https://ai-sdk.dev/docs/agents/tool-approvals). Builds on the [2026-07-03] tool-approval item.
- **Surface**: frontend + backend (tool-approval `BeforeToolCall` hook in `apis/shared`, `tool_use`/`tool_result` SSE contract, frontend tool-call card + signal store; reuses `beginContinuationStreaming`)
- **Effort × Impact**: M × M
- **Subtracts**: yes — replaces ad-hoc synthetic-error approval handling with explicit approve/deny/user-approval lifecycle states
- **Unlocks**: server-side **policy layer** deciding auto-approve vs prompt via per-tool input-inspecting functions (gate on the tool's args, not just identity); cryptographically-signed, tamper-proof approval history; closes the "approval hook can't see through the tool-fold" hole (pairs with the Strands hook-ordering bump)
- **Status**: open — **fold into the queued [2026-07-03] tool-approval item** rather than run a separate track; sequence after the Strands hook-ordering bump lands. The new-this-week piece is the policy layer + integrity check, beyond last week's basic human-in-the-loop.

### [2026-07-10] Wire a CloudWatch `ActiveSessionCount` alarm on the inference-api runtime
- **Source**: research/2026-07-10.md ▸ Top 5 #5 — **NEW** AgentCore Runtime `ActiveSessionCount` metric (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/release-notes.html)
- **Surface**: infrastructure (CloudWatch alarm on the inference-api runtime's `AWS/Bedrock-AgentCore` `ActiveSessionCount` gauge; PlatformStack observability)
- **Effort × Impact**: L × M
- **Subtracts**: no — ops addition; justified as cheap early-warning for the exact failure class the agentcore bump fixes (defense-in-depth while the bump is pending)
- **Unlocks**: proactive detection of session-leak/exhaustion and the #482 hang (a hung container manifests as session pileup) before a 429
- **Status**: open — low-effort ops win that pairs with the agentcore bump. Alarm when concurrent sessions approach the raised quota (5,000 us-west-2).

### [2026-07-06] Spike: managed AgentCore Harness as the headless/scheduled run engine — ✅ SPIKE + Q2 LIVE PROBE COMPLETE, recommend Ship (headless-only, GO-with-boundary)
- **Source**: `scoping/2026-07-06-managed-harness-build-vs-adopt.md` (brief) + `scoping/2026-07-06-managed-harness-spike-findings.md` (**findings — 3 gating questions answered**). Surfaced while dogfooding scheduled runs (Phil asked whether we use the AWS Harness feature; we use the lower-level Runtime). AWS **managed Harness** is now GA.
- **Surface**: backend (`apis/shared/harness/run_agent_headless` — swap the Runtime `/invocations` target for an `InvokeHarness` endpoint on the headless lane only; swap `sse.py` accumulator for a Converse-stream one → same `RunResult`) + infra (a managed-Harness resource + OAuth-inbound JWT authorizer — a 1:1 port of our existing Runtime `customJwtAuthorizer`, `inference-agentcore-construct.ts:275`). Interactive `inference-api` untouched.
- **Effort × Impact**: M (spike) × H
- **Subtracts**: potentially large — managed memory (fixes AgentCore-Memory-write-only-in-cloud → F5), immutable versions + named endpoints (retires the ECR-tag/`update-function-code` fragility that bit the scheduled-runs deploy), auto observability, `InvokeHarness` Step Functions composition, execution limits/truncation — all as config on the proactive lane.
- **Unlocks**: managed long-term memory + ops maturity for proactive/scheduled agents without touching interactive chat; `export harness` keeps lock-in low.
- **Status**: open — **spike complete, all three gating questions CLEAR for the headless lane; recommend Ship a narrowly-scoped `InvokeHarness` probe.** Full replace remains a non-starter (managed Harness has `Hooks ❌`, `Choice of framework ❌`, no MCP Apps UI, not our SSE contract — interactive differentiation lives there); scope is headless-only. Answers: **(Q1) RBAC→`allowedTools` = qualified YES** — `allowedTools` is per-invoke-settable + globs cover our id shapes, and we already snapshot the RBAC-narrowed set statically at the app-api boundary (`schedules/routes.py:75`), so id→glob is mechanical; Cedar-on-Gateway covers arg-level gating. Non-membership gates relocate (quota/cost → dispatcher pre-gate + post rollup; tool-approval → exclude on headless; OAuth-consent → Identity outbound). **(Q2) per-user tokens = YES on mechanism** (OAuth-inbound `customJWTAuthorizer` = our authorizer; owner-minted token threads identity; Gateway `outboundAuth.oauth` = our `get_token_for_user` `USER_FEDERATION` exchange; **SigV4 canNOT do per-user identity** so must be OAuth-inbound) — **one residual needing a live probe: the `customParameters` vault-key gotcha**, since Gateway config performs the exchange and we lose call-site control of `customParameters`. **(Q3) lose MCP Apps + SSE = YES** — both are interactive affordances a scheduled run has no live consumer for; SPA loads the delivered session, not the harness stream. **Next action (Ship):** a scoped `InvokeHarness` prototype whose only job is to close the Q2 `customParameters` residual (one Gateway OAuth tool, one `customParameters`-sensitive provider, owner-minted Bearer, confirm the vaulted token resolves + a *missing* token fails legibly for `paused_reauth`). If it clears, adopt on the proactive lane behind a flag. Newly GA → still verify pricing/quotas in the probe. **→ Q2 PROBE RUN LIVE (dev-ai, 2026-07-06) = GO-with-boundary** (details in findings doc "Q2 probe result"): confirmed live that (a) `CreateHarness` accepts our exact `customJWTAuthorizer` (1:1 port, harness READY); (b) `outboundAuth.oauth.customParameters` is honored — persisted verbatim on `GetHarness` (2-key & 3-key maps) → **we can pin the same params the consent used**; (c) OAuth-inbound works (owner-minted Cognito Bearer → HTTP 200, ran as owner); (d) the exchange calls the **same `GetResourceOauth2Token`** our `get_token_for_user` uses; (e) a failed exchange surfaces **legibly** as a typed `runtimeClientError` stream event → maps to `paused_reauth`. **Boundary (new, blocking a clean positive):** the managed **Gateway** 3LO exchange fails with `ValidationException: You must provide a ResourceOauth2ReturnUrl` and does **not** source that URL from `defaultReturnUrl` (per-invoke or create-time), the `OAuth2CallbackUrl` header, **or** `UpdateWorkloadIdentity` `AllowedResourceOauth2ReturnUrl` — so a clean positive resolution couldn't be observed; cross-workload token visibility (platform-workload vs harness-workload) also unreached. **Decision:** Ship headless adoption, but keep `customParameters`-sensitive / all 3LO connectors on our own `get_token_for_user` (F1-proven) rather than the Harness-managed Gateway exchange until AWS's return-URL wiring is resolved (support ticket / re-probe). Pricing: no separate harness charge; **managed memory is on by default** (extra Memory cost per harness). Probe harness torn down; no new IAM/gateway-target footprint.

### [2026-07-06] Watchlist: Bedrock Mantle endpoint — Claude-on-Mantle Strands provider gap + capability parity
- **Source**: Phil-initiated kaizen focus — scope a migration from the `bedrock-runtime` (Converse) endpoint to the new `bedrock-mantle` endpoint ([AWS endpoints doc](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html); [Opus 4.8 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-8.html)). Strategic driver: align with where Bedrock is heading (new capability lands Mantle-first), not near-term need.
- **Surface**: backend — `agents/main_agent/core/agent_factory.py` (`_create_mantle_model` today is a Strands `OpenAIModel` → Chat Completions; a Claude-on-Mantle path needs an Anthropic-Messages provider, which Opus 4.8's Mantle surface is limited to), `core/model_config.py` (`ModelProvider.MANTLE` already defined), `core/bedrock_count_tokens.py` (`CountTokensBedrockModel` — bedrock-runtime-only), plus the 3 direct-Converse bypasses (`inference_api/chat/converse_routes.py`, Nova title gen `chat/service.py:355`, Titan embeddings `shared/embeddings/bedrock_embeddings.py` via `invoke_model`).
- **Effort × Impact**: (Claude-on-Mantle) M–H × L-now — high cost, low near-term value. (Non-Claude Mantle lane) L–M × M — finish the already-scaffolded OpenAI-compatible path.
- **Subtracts**: no (watchlist). If/when acted on, the non-Claude lane makes `ModelProvider.MANTLE`/`_create_mantle_model()` a first-class peer instead of dark scaffolding.
- **Unlocks**: Mantle-first capability gradient (Responses API, server-side tool use, async/long-running, Projects/Workspaces) once Claude parity lands; a uniform OpenAI-compatible lane for non-Claude models inside Bedrock without a second vendor SDK.
- **Status**: open — **strategic/future-proofing, not urgent; recommend Defer (watchlist) + a small non-Claude-lane spike.** Corrected findings: (1) `bedrock-runtime` is "fully supported," no EOL signal — Mantle recommendation is greenfield-onboarding language, but the capability gradient toward Mantle is real. (2) **The "persisted Converse wire shape = multi-model lock-in" concern does NOT hold** — verified `strands/types/content.py:78` `ContentBlock` (`toolUse`/`toolResult`/`reasoningContent`) IS Strands' provider-neutral canonical shape; every Strands provider round-trips it to/from Anthropic Messages / OpenAI Chat Completions. AgentCore Memory abstracts *persistence*; Strands abstracts *multi-model shape*. Switching a provider's endpoint changes Strands `format_request` internals, **not** our persistence schema or `_convert_content_block`. No schema-decoupling PR needed. (3) The real bedrock-runtime ties are narrow: the 3 direct-Converse bypasses, `CountTokens` (no Mantle equal — powers context-attribution + compaction), and cross-region profiles (`us.*`/`global.*`, Mantle-absent). (4) **Do NOT migrate the Claude chat path to Mantle yet**: Opus 4.8 on Mantle is Messages-API-only (Chat Completions/Responses = No), so Mantle's headline built-ins don't apply to our primary model; Mantle also lacks cross-region inference, native `CountTokens`, structured outputs, and **Guardrails** (runtime-only — see [2026-06-19] Guardrails item, which this reinforces). Pricing identical; Mantle default TPM not a win (`20M in/4M out` vs runtime `30M`). **Reopen trigger:** a Strands Anthropic-Messages-on-Mantle provider ships **AND** cross-region + native token counting reach Claude-on-Mantle. Interim, low-risk value = finishing the non-Claude OpenAI-compatible lane already scaffolded in `_create_mantle_model()`.

### [2026-07-03] Model-settings refresh: reinstate Fable 5 + add Sonnet 5 with temperature-suppression guard
- **Source**: research/2026-07-03.md ▸ Top 5 #3 — Fable 5 reinstated (https://aws.amazon.com/blogs/aws/anthropic-claude-fable-5-on-aws-mythos-class-capabilities-with-built-in-safeguards-now-available/); Sonnet 5 GA + promo pricing (https://aws.amazon.com/bedrock/pricing/); ref-repo `NO_TEMPERATURE_MODELS` (commit 35bc3a9).
- **Surface**: cross-cutting (inference-api model config + model-settings admin, `to_bedrock_config`, `CountTokensBedrockModel` de-prefix, frontend model picker)
- **Effort × Impact**: M × M–H
- **Subtracts**: addition only — justified: **un-withdraws the [2026-06-12] Fable 5 item** (revoked mid-June, reinstated July 1) and adds a materially cheaper capable tier (Sonnet 5 $2/$10 promo through Aug 31 vs. Opus 4.8)
- **Unlocks**: Fable 5 harness tier above Opus 4.8; Sonnet 5 as a cheaper default/agent tier
- **Status**: open — **the temperature-suppression guard is a prerequisite, not optional**: Sonnet 5 rejects `temperature` on ConverseStream. Fable 5 is US inference profile only (Global unstable).

### [2026-07-03] Tool-approval as first-class SSE/part state + tool_result source provenance
- **Source**: research/2026-07-03.md ▸ Top 5 #4 — assistant-ui `eve@0.0.2` (https://github.com/Yonom/assistant-ui/releases); Vercel AI SDK human-in-the-loop (https://ai-sdk.dev/cookbook/next/human-in-the-loop); NN/g State of UX 2026 (https://www.nngroup.com/articles/state-of-ux-2026/).
- **Surface**: frontend + backend (tool-approval BeforeToolCall hook, `tool_use`/`tool_result` SSE contract, frontend tool-call card + signal store; reuses `beginContinuationStreaming`)
- **Effort × Impact**: M × M
- **Subtracts**: yes — replaces ad-hoc synthetic-error approval handling with explicit approve/deny/denied states on the tool-use SSE pair
- **Unlocks**: closes the known "approval hook can't see through the tool-fold" hole (pairs with the Strands hook-ordering bump); "data sources used" provenance on `tool_result` cards (we already carry `serverName`/`icon` on `ui_resource`) — a top NN/g trust driver
- **Status**: open — auto-resume once all approvals in a turn resolve (the multi-tool piece worth stealing from the AI SDK).

### [2026-07-03] Evaluate gateway-level Guardrails (AgentCore Policy) vs. in-agent #480
- **Source**: research/2026-07-03.md ▸ Top 5 #5 — https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-guardrails.html; relates to queued issue #480.
- **Surface**: infrastructure + backend (PlatformStack Gateway construct + AgentCore Policy; `apis/shared` tool routing)
- **Effort × Impact**: M × M
- **Subtracts**: potential — one gateway-level policy vs. per-tool control complexity
- **Unlocks**: model-independent FERPA/injection enforcement across all Gateway MCP targets (the agent can't reason around it)
- **Status**: open — **fold into the #480 decision** rather than run a separate track: assess whether one gateway policy is preferable to or complements the in-agent `guardrail_id` approach.

### [2026-06-19] Wire configurable Bedrock Guardrails (issue #480)
- **Source**: research/2026-06-19.md ▸ Top 5 #1 — internal issue #480 (June 15) + AWS Summit NYC Guardrails cluster (`InvokeGuardrailChecks` API + AgentCore policy Guardrails GA, June 16). Strands `BedrockModel` already supports `guardrail_id`/`version`/`stream_processing_mode`/`trace`.
- **Surface**: backend (`inference_api` `BedrockModel` construction) + infrastructure (optional `CDK_GUARDRAIL_ID` / `CDK_GUARDRAIL_VERSION` env vars threaded to inference-api runtime env)
- **Effort × Impact**: L-M × H
- **Subtracts**: addition only — config wiring of a capability Strands already exposes; zero-cost when unset; mirrors `CDK_ARTIFACTS_ENABLED`/`CDK_MCP_SANDBOX_ENABLED` optional-feature pattern
- **Unlocks**: deployers attach content-safety filtering + staff-alerting monitoring to all model invocations without modifying inference-api source (FERPA duty-of-care for higher-ed: proactive self-harm/crisis-language monitoring Claude's reactive layer doesn't surface)
- **Status**: open — strongest fit (filed issue + library-native path). **Decide in-agent vs. gateway-level in one pass** — the [2026-07-03] "gateway-level Guardrails (AgentCore Policy)" item folds into this #480 decision (one gateway policy blankets every MCP target, model-independent). Verify guardrail *resource* region availability + SSE streaming-mode compatibility. Reviewed reviews/2026-07-03.md ▸ Proposal #4.

### [2026-06-19] Ship the interactive context-breakdown badge (Cursor + LibreChat convergence)
- **Source**: research/2026-06-19.md ▸ Top 5 #5 — LibreChat v0.8.7-rc1 real-time context gauge + Cursor Context Usage Report (2026-06-05) + internal PR #433. **Reinforces** the [2026-06-05] "make the context-breakdown badge interactive" item with a second independent product datapoint.
- **Surface**: frontend (context-breakdown badge component in `frontend/ai.client/src/app/session/`)
- **Effort × Impact**: M × M
- **Subtracts**: no — addition; lands on a surface we shipped and reuses `contextBreakdown` already on the final `metadata` event
- **Unlocks**: user-facing context-cost transparency + an actionable "what's eating context / how to trim it" follow-up
- **Status**: open — presentation-layer only (no backend change). Consolidated the superseded [2026-06-05] Cursor-only entry into this item. Lower priority than the [2026-07-03] reliability/model cluster. Reviewed reviews/2026-07-03.md ▸ below-cap (defer 1 week).

### [2026-06-05] Bump `docling` past the 2.81.0 content-sniffing defect → close #405 (`.txt` uploads fail)
- **Source**: research/2026-06-05.md ▸ Top 5 #4 — docling 2.97.0 (June 3) + internal issue #405
- **Surface**: backend (document-ingestion docling dep pin)
- **Effort × Impact**: L × M
- **Subtracts**: yes — library-native bump closes an open user-facing bug; no custom workaround needed
- **Status**: open — **#405 still open ~5 weeks; `requirements.lock` still pins `docling==2.81.0` (latest 2.109.0).** Cleanest subtraction; bump off 2.81.x, verify `.txt` upload, close #405. Reviewed reviews/2026-07-03.md ▸ Proposal #6.

### [2026-06-05] De-risk #419 (admin-managed Gateway target registration) against the new AWS auth-code-flow + BYO-secrets references
- **Source**: research/2026-06-05.md ▸ Top 5 #5 — AWS "secure OAuth auth-code flow with Gateway + MCP clients" + AgentCore Identity BYO Secrets Manager (both June 1) + internal issue #419
- **Surface**: infrastructure (Gateway target CRUD / `gateway_target_*`) + backend (`apis/shared/oauth/agentcore_identity.py` OAuth provider wiring + token-vault customParameters) + frontend (admin registration UI)
- **Effort × Impact**: H × H
- **Subtracts**: partial — BYO Secrets Manager lets us own/govern OAuth client secrets (CMK, tagging) instead of service-managed storage
- **Unlocks**: admins register external MCP servers (protocol=mcp) without code changes — net-new admin surface, now blueprinted by AWS
- **Status**: open — strategic; the AWS references materially de-risk an already-filed feature

### [2026-05-29] Migrate inference-api model config Opus 4.7 → 4.8
- **Source**: research/2026-05-29.md ▸ Top 5 #1 — Claude Opus 4.8 on Bedrock (May 28)
- **Surface**: backend (model config in `inference_api`) + admin model catalog + the `_shape_thinking_value` / `temperature` provider-translation path
- **Effort × Impact**: M × H
- **Subtracts**: partial — Opus 4.8's system-in-`messages` caching allowance simplifies the #269 caching wiring (system no longer must sit strictly outside `messages` to preserve cache)
- **Unlocks**: fewer-step tool turns (lower per-turn cost), best-in-class computer-use, ~4× fewer code-flaw pass-throughs, the `effort` compute-depth knob
- **Status**: open — verify Bedrock region availability (us-east-1 ✓) and the 4.8 context window on the model card before flipping the pin; confirm the beta.27 Opus-4.7 thinking/`temperature` handling still applies

### [2026-05-29] Compaction summary prompt: preserve standing/sensitive user instructions
- **Source**: research/2026-05-29.md ▸ Top 5 #4 — Claude Code v2.1.152 compaction-prompt change (~May 26)
- **Surface**: backend (`TurnBasedSessionManager` summarization prompt)
- **Effort × Impact**: L × M
- **Subtracts**: no — defensive/quality
- **Status**: open — cheap; dovetails with the `compaction` SSE event

### [2026-05-29] Sync-in-async defensive sweep (anchored by web-crawler DoS #399)
- **Source**: research/2026-05-29.md ▸ Top 5 #5 — internal issue #399 (web-crawler DoS, May 28); same class as AgentCore SDK #482
- **Surface**: backend (web-sources crawler immediate fix, then a sweep of sync-in-async call sites across `inference-api` / `app-api`)
- **Effort × Impact**: M × M-H
- **Subtracts**: no — defensive; protects the shared event loop from being wedged by one user's request
- **Status**: open — #399 already filed; kaizen value is the broader class-of-bug sweep (pairs with the queued SDK #482 guard)

### [2026-05-22] Opus 4.7 `temperature`-omission guard
- **Source**: research/2026-05-22.md ▸ Top 5 #4 — ref-repo commit `9385454`
- **Surface**: backend (provider-translation chokepoint — same site as `_shape_thinking_value` / #329 / #331)
- **Effort × Impact**: L × M
- **Subtracts**: no — defensive; Opus 4.7 rejects `temperature` on extended-thinking turns
- **Status**: open — **subsumed by the [2026-07-03] model-settings per-model temperature-suppression guard** (same `to_bedrock_config` chokepoint; Sonnet 5 has the same rejection). Ship as one guard covering both. Reviewed reviews/2026-07-03.md ▸ Proposal #3.

### [2026-05-15] Wire per-tool `duration_ms` into `tool_result` SSE
- **Source**: research/2026-05-15.md ▸ Top 5 #5 — Claude Code 2.1.141 hook pattern
- **Surface**: backend (Strands `AfterToolCall` hook) + frontend (`<tool-result>` component — inline timing badge for `> 250ms`)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: partial — single hook-driven field replaces any ad-hoc per-tool timing; pre-paves the planned context-attribution prototype
- **Unlocks**:
  - Per-tool timing visibility in the UI (which slow tool is the bottleneck on this turn?)
  - Data substrate for the planned context-attribution prototype — separates tool latency from token cost
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #3 (Ship); no decision logged yet

### [2026-05-15] Investigate inference-api deploy — new images reach ECR but Runtime isn't rolled (issue #288)
- **Source**: reviews/2026-05-15.md ▸ Proposal #10 (new from internal friction, issue #288 May 12). Pairs with the 1.6.4 → 1.9.1 bump (same SDK package owns `update_agent_runtime`).
- **Surface**: cross-cutting — `.github/workflows/deploy-inference-api.yml` + bedrock-agentcore SDK `update_agent_runtime` call shape
- **Effort × Impact**: L-M × M-H
- **Subtracts**: possibly — removes the manual-redeploy band-aid that's been the workaround
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #10 (Ship — recommended ship-first); no decision logged yet. **Friction intensifying**: 6+ "Deploy Inference API" failures May 15–17; a new "Deploy App API" failure cluster (8× May 16–17) may share a root cause.

### [2026-05-10] Scope AgentCore Runtime BYO filesystem (S3 Files / EFS) for persistent agent workspaces
- **Source**: research/2026-05-10.md ▸ AWS Bedrock / AgentCore (re-evaluated 2026-05-10 via strategic-lens follow-up — original framing under-weighted the capability-unlock angle)
- **Surface**: backend (`inference-api` invocation handler reads/writes mount) + infrastructure (VPC config, IAM mount permissions, S3 Files or EFS access points, per-user prefix/access-point layout for RBAC); ADR-worthy
- **Effort × Impact**: H × H
- **Subtracts**: no — pure capability addition
- **Unlocks**:
  - Code-interpreter / persistent agent workspace (artifacts survive turn and session boundaries)
  - Cross-session file uploads — PDFs/spreadsheets persist between conversations instead of re-staging per session
  - Shared skill/template/prompt hot-swap without redeploying the runtime container
  - A2A multi-agent intermediate-result handoff via shared mount
  - Persistent vector indexes / embedding caches — avoids cold-start rebuild
- **Open questions**: GA vs preview status (March 2026 managed session storage was preview; May 2026 BYO needs verification); VPC requirement is a new architectural surface for the runtime; multi-tenancy isolation strategy (per-user S3 prefix vs per-user EFS access point); RBAC mount-path layout; runtime data plane still only proxies `/invocations` + `/ping` so this doesn't unlock new HTTP routes
- **Status**: open — deferred 4 weeks in reviews/2026-05-15.md (revisit 2026-06-12). MCP Apps host renderer is the dominant strategic initiative this cycle; layering another ADR-worthy bet on top would double the open architectural surface.

### [2026-05-10] Audit `BedrockModel.stream` cancellation path against Strands #2266
- **Source**: research/2026-05-10.md ▸ Top 6 #4
- **Surface**: backend
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive (SSE-disconnect path is hot)
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #8 (Ship); no decision logged yet

### [2026-05-10] Audit `oauth_required` SSE flow against ref-repo's mid-tool-call 401/403 handling
- **Source**: research/2026-05-10.md ▸ Risks
- **Surface**: backend
- **Effort × Impact**: M × H
- **Subtracts**: no — defensive
- **Status**: open — deferred 2026-05-10 until 2026-05-24. BFF parade declared done via #297 (May 14), so deferral conditions have cleared a week early; reviews/2026-05-15.md holds to original revisit date to give one stable week.

### [2026-05-10] Named A2A agent participants in the chat UI
- **Source**: research/2026-05-10.md ▸ Agentic UI/UX ▸ Linear Agent pattern. Reinforced by research/2026-05-15.md Linear Code Intelligence 5× usage-growth datapoint.
- **Surface**: frontend (extend message model with `agent_identity`, distinct avatar/name/styling)
- **Effort × Impact**: L-M × M
- **Subtracts**: no — additive but pattern-validated across Linear/ChatGPT/Cursor
- **Status**: open — deferred 4 weeks in reviews/2026-05-15.md (revisit 2026-06-12). Earns its keep when an A2A construct lands.

## Resolved

### [2026-07-24] + [2026-07-17] + [2026-07-10] + [2026-07-03] Bump `bedrock-agentcore` off 1.9.1 (→1.17.0 / →1.18.0 / →1.18.1) → RESOLVED — SHIPPED
- **Source**: research/2026-07-24.md · research/2026-07-17.md · research/2026-07-10.md · research/2026-07-03.md ▸ each Top 5 #1
- **Decision**: Ship — **done**, no further action.
- **Reasoning**: PR #857 bumped `bedrock-agentcore` **1.9.1 → 1.21.0** (with `strands-agents` 1.48→1.51, `strands-agents-tools` 0.5.2→0.8.6, `aws-opentelemetry-distro` 0.17→0.19, `boto3` 1.43.9→1.43.68 across 4 files), released in 1.14.1 on 2026-08-13. Verified 2026-08-14: latest upstream is 1.21.0 — **zero version lag**. #482 (SSE deadlock, PR #563) and #571 (cross-process Memory event reorder, PR #572) are both **closed upstream**. All four entries chained through the same subject and repeated the now-false claim *"we're on 1.9.1, exposed today"*.
- **Residue carried forward**: **#564 is still open** — see the [2026-08-14] guard item under `## Open`.
- **Reviewed in**: resolved directly at Phil's request 2026-08-14 (ahead of reviews/2026-08-14.md); evidence in research/2026-08-14.md ▸ Version-pin lag + Retirement candidates.

### [2026-07-10] + [2026-07-03] Bump Strands (1.40 → 1.45 / → 1.47) → RESOLVED — SHIPPED, capabilities split out
- **Source**: research/2026-07-10.md ▸ Top 5 #2 · research/2026-07-03.md ▸ Top 5 #2
- **Decision**: Ship — **bump done**; un-adopted capabilities re-queued as their own entry.
- **Reasoning**: the pin is now **1.51.0** (#857), well past both entries' targets; latest upstream is 1.52.0, so lag is 1 minor / 5 days. The [2026-07-10] entry's own status line already read "the bump itself SHIPPED". The genuinely unfinished half — `continue_on_error`, optional hook ordering (#2559), `cache_tools_ttl`, `context_manager="auto"` — is now the **[2026-08-14] "Adopt the Strands capabilities the 1.51 bump made available but did not wire"** entry under `## Open`. `Limits` remains separately queued at [2026-07-17].
- **Reviewed in**: resolved directly at Phil's request 2026-08-14; evidence in research/2026-08-14.md ▸ Version-pin lag.

### [2026-07-24] Fix the nightly `DELETE_FAILED` stuck ephemeral stack + [2026-06-19] Nightly `exit 127` → RESOLVED — GREEN
- **Source**: research/2026-07-24.md ▸ Top 5 #5 · research/2026-06-19.md ▸ Top 5 #2
- **Decision**: Ship — **resolved by events**, no action needed.
- **Reasoning**: Nightly Build & Test has been **green 12 consecutive runs, Aug 3 → Aug 14**, and there have been **zero CI failures of any workflow in the last 12 days**. Both entries' premises — a wedged `DELETE_FAILED` ephemeral stack, and a ~14-failure `exit 127` install cluster — no longer reproduce. The dep-bump safety gate these entries existed to restore is functioning, and it vouched for #857.
- **Note**: neither entry records *which* fix closed it, so this is "resolved by observation" rather than "resolved by an identified commit". If Nightly regresses, re-open with a fresh diagnosis rather than reviving these.
- **Reviewed in**: resolved directly at Phil's request 2026-08-14; evidence in research/2026-08-14.md ▸ Internal Audit ▸ Activity.

### [2026-07-24] Prep the MCP Apps host for the 2026-07-28 spec + [2026-05-29] Align MCP Apps capability advertisement → RESOLVED — superseded (spec is now final)
- **Source**: research/2026-07-24.md ▸ Top 5 #4 · research/2026-05-29.md ▸ Top 5 #3
- **Decision**: Defer into the successor entry — superseded, **not** declined; the work is still wanted.
- **Reasoning**: both were written against a *moving RC* ("spec-final in 4 days", "before the RC stabilizes"). MCP **2026-07-28 is now the Current protocol version**, so the successor can be written against settled facts: the `initialize` handshake is gone, `server/discover` is the replacement (mandatory for servers, **optional for clients**), and MRTR/SEP-2322 + SEP-2243 are concrete follow-ons. ⚠️ Critically, the [2026-05-29] entry asserted `io.modelcontextprotocol/ui` is "spec-canonical" — the 2026-08-14 scan **could not confirm that identifier** against the spec source and saw a changelog reference to preserving `experimental` settings. Leaving it open would have propagated an unverified premise into an implementation.
- **Superseded by**: **[2026-08-14] "Migrate the MCP Apps host off `initialize`/`serverInfo` to `server/discover`"** under `## Open`, which carries the capability-identifier verification as an explicit precondition.
- **Reviewed in**: resolved directly at Phil's request 2026-08-14; evidence in research/2026-08-14.md ▸ MCP ecosystem ▸ Spec status.

### [2026-06-19] Bump Strands 1.40 → 1.44 + [2026-06-12] 1.43 + [2026-06-05] 1.42 + [2026-06-05] #2635 guard → RESOLVED — superseded by the [2026-07-03] 1.45 keystone
- **Decision**: Superseded — all four consolidated into the open [2026-07-03] "Strands 1.40 → 1.45 + hook ordering" item. The #2635 count-tokens guard folds into the bump.
- **Reviewed-in**: reviews/2026-07-03.md ▸ Proposal #2.

### [2026-06-19] Bump `bedrock-agentcore` 1.9.1 → 1.15.0 + [2026-06-12] 1.14.1 + [2026-05-22] 1.11.0 (×2) + [2026-05-22] #482 hand-written guard → RESOLVED — superseded by the [2026-07-03] 1.17.0 bump
- **Decision**: Superseded — all consolidated into the [2026-07-03] "bedrock-agentcore 1.9.1 → 1.17.0" item. Per research/2026-07-03.md the #482 fix is now **upstream in 1.17.0** (PR #563), so the queued hand-written guard converts to "bump the pin" — a library-native subtraction.
- **Reviewed-in**: reviews/2026-07-03.md ▸ Proposal #1.
- **Trail update 2026-08-14**: the [2026-07-03] item this pointed at is itself now resolved — the whole chain **shipped** in #857 (1.9.1 → 1.21.0). See the [2026-07-24]+[2026-07-17]+[2026-07-10]+[2026-07-03] consolidated resolution at the top of this section.

### [2026-06-12] Add Claude Fable 5 to model settings (+ the [2026-06-19] WITHDRAW) → RESOLVED — un-withdrawn, folded into the [2026-07-03] model-settings refresh
- **Decision**: Superseded — **NOT declined.** Fable 5 was revoked on Bedrock mid-June (forcing the withdrawal) and **reinstated Jul 1**. The reinstatement + Sonnet 5 GA are consolidated into the open [2026-07-03] "Model-settings refresh: reinstate Fable 5 + add Sonnet 5" item (US inference profile only; Global unstable).
- **Reviewed-in**: reviews/2026-07-03.md ▸ Proposal #3.

### [2026-06-12] Investigate + triage Nightly Build & Test (7 failures) → RESOLVED — consolidated
- **Decision**: Superseded — consolidated into the open [2026-06-19] nightly item (still open; #518 was incomplete, see that item).
- **Reviewed-in**: reviews/2026-07-03.md ▸ Proposal #9.

### [2026-06-12] Bump `starlette` 1.0.0 → 1.0.1 (CVE-2026-48710) → RESOLVED — shipped
- **Decision**: Resolved — `backend/pyproject.toml` now pins `starlette==1.3.1` (past the CVE floor) via PR #487 (June 18, "remediate 22 HIGH Dependabot findings"). Confirmed in research/2026-06-19.md's version-pin table.
- **Reviewed-in**: reviews/2026-07-03.md ▸ What Shipped.

### [2026-06-05] Make the context-breakdown badge interactive (Cursor) → RESOLVED — consolidated
- **Decision**: Superseded — consolidated into the open [2026-06-19] "interactive context-breakdown badge (Cursor + LibreChat convergence)" item.
- **Reviewed-in**: reviews/2026-07-03.md ▸ below-cap.

### [2026-05-22] Fast PR-gate for the deterministic test subset → RESOLVED — shipped (broader than proposed)
- **Decision**: Resolved — satisfied by **PR #490** (June 18, "ci: add pull_request test gate").
- **Reasoning**: #490 added `.github/workflows/ci.yml` on `pull_request → [develop, main]` with three parallel jobs — `test-backend` (`uv run pytest tests/`), `test-frontend` (vitest), `test-infra` (jest), SHA-pinned, `ubuntu-24.04`. This is **broader** than the proposed `supply_chain`+`architecture` subset: it runs the full backend pytest suite on PRs. **Premise change**: the "backend pytest isn't in CI" line (still repeated in research/2026-07-03.md) is now stale — backend pytest *is* a PR gate as of #490.
- **Reviewed-in**: reviews/2026-07-03.md ▸ Friction + What Shipped.

### [2026-05-29] Adopt Strands `Limits` for per-invocation cost/turn caps → RESOLVED — superseded (folded into the 2026-06-05 Strands 1.42 keystone)
- **Decision**: Superseded — consolidated into the [2026-06-05] "Strands 1.40 → 1.42 keystone bump" Open item.
- **Reasoning**: This item was gated on Strands 1.42, which released June 1. The 2026-06-05 research declared the consolidation: the keystone bump adopts `Limits` (cost cap) and `cache_tools_ttl` (#269) together. Tracking it as a separate item duplicates the keystone. The CloudWatch Bedrock-spend alarm half is carried in the keystone's surface area.
- **Reviewed-in**: reviews/2026-06-05.md ▸ Proposal #1 + Retirement Candidates (queue consolidation).

### [2026-05-22] Strands 1.40 → 1.41 bump + enable Bedrock prompt caching (#269) → RESOLVED — superseded (folded into the 2026-06-05 Strands 1.42 keystone)
- **Decision**: Superseded — consolidated into the [2026-06-05] "Strands 1.40 → 1.42 keystone bump" Open item.
- **Reasoning**: `cache_tools_ttl` (the #269 unblock this item targeted at 1.41) now ships in 1.42 alongside `Limits`. A single 1.40 → 1.42 bump covers both; the `starlette` 1.x transitive-conflict audit this item owed is folded into the keystone's blast-radius audit (`strands-agents-tools` 0.5→0.8 + `starlette` 1.2.1). #269 stays open as the work-tracking issue.
- **Reviewed-in**: reviews/2026-06-05.md ▸ Proposal #1 + Retirement Candidates (queue consolidation).

### [2026-05-22] Runaway-session cost guardrail — `max_turns` + CloudWatch Bedrock-spend alarm → RESOLVED — superseded (folded into the 2026-06-05 Strands 1.42 keystone)
- **Decision**: Superseded — consolidated into the [2026-06-05] "Strands 1.40 → 1.42 keystone bump" Open item.
- **Reasoning**: Strands `Limits` (1.42) is the library-native replacement for the hand-rolled `max_turns` guardrail this item proposed; the keystone adopts it and retires the hand-rolled equivalent. The CloudWatch Bedrock-spend alarm half is carried in the keystone's infrastructure surface area (the half the SDK can't provide).
- **Reviewed-in**: reviews/2026-06-05.md ▸ Proposal #1 + Retirement Candidates (queue consolidation).

### [2026-05-22] Pin `backup-data.yml` runner + actions to restore the CI gate → RESOLVED — pinned, CI green
- **Decision**: Resolved (not a logged kaizen decision — landed incidentally via the beta.27 release merge #365, May 21).
- **Reasoning**: `.github/workflows/backup-data.yml` is now correctly pinned — `runs-on: ubuntu-24.04`, `actions/checkout@de0fac2…# v6.0.2`, `astral-sh/setup-uv@d0cc045…# v6.8.0`. Deploy App API / Deploy Inference API failures stopped after May 20; Nightly Build & Test is green (May 24, 25, 28, 29; one isolated May 27 failure). The supply-chain pinning gate is restored. Flagged in reviews/2026-05-29.md ▸ What Shipped: the fix came through the release branch, not a deliberate kaizen action.
- **Reviewed-in**: reviews/2026-05-22.md ▸ Proposal #1 (verified resolved in reviews/2026-05-29.md)

### [2026-05-10] MCP Apps host renderer — multi-PR build (PRs #1–#7) → RESOLVED — shipped, host enabled
- **Decision**: Ship — build-out of the multi-PR initiative scoped in reviews/2026-05-10.md ▸ Proposal #1
- **Reasoning**: Build sequence complete and merged to `develop` 2026-05-18 → 2026-05-20 (PR #0, the renderer registry #339, is resolved separately below). PRs: #342 (PR #1/#2 — advertise MCP Apps UI extension on `initialize` + filter app-only tools), #343 (infra — sandbox-proxy origin CDK stack), #344 (PR #3 — emit `ui_resource` SSE via `resources/read` fetch path), #345 (`sandboxOrigin` field + `_meta.ui.permissions` object-shape fix), #346 (PR #4 — `<mcp-app-frame>` + postMessage bridge), #347 (PR #5 — app-initiated `tools/call` proxying + event broker), #348 (PR #6 — `ui/message`, `ui/update-model-context`, frontend consent + reload persistence), #349 (PR #7 — dogfood + flip `AGENTCORE_MCP_APPS_HOST_ENABLED` on, conditional CDK sandbox-origin SSM→env wiring). A 2026-05-19 → 05-20 dogfood pass surfaced host-renderer bugs absent from the scoping doc — fixed in a follow-up cluster: #352 (blob iframe + NG0910 dynamic-`allow` + Angular 21 fixes), #355 (dynamic per-resource CSP for the sandbox proxy), #356/#357 (shorten CFN/RHP Comment to the 128-char AWS cap), #358 (decode URL-encoded `?csp=`), #359 (remove `x-csp-debug` diagnostic), #360 (inner App iframe `allow-same-origin` to match the basic-host reference). Initiative behaviorally live; host enabled by default.
- **Reviewed-in**: reviews/2026-05-10.md ▸ Proposal #1 (scope only); build per `docs/kaizen/scoping/mcp-apps-host-renderer.md`

### [2026-05-15] Strands 1.39 → 1.40 bump (token-count audit + compaction double-fire check) → RESOLVED — shipped
- **Decision**: Ship — reviews/2026-05-15.md ▸ Proposal #6
- **Reasoning**: Shipped in PR #340 (`chore(deps): bump strands-agents 1.39.0 → 1.40.0`, merged 2026-05-18). Audit outcome: **accept the new `use_native_token_count=False` default** — the flag gates only `BedrockModel.count_tokens()`, which nothing in our cost / context-% paths reads (those read native Bedrock Converse `usage`); pinning `True` would add a redundant CountTokens API call per invocation. Compaction double-fire **confirmed absent** — Strands proactive compression is opt-in (`proactive_compression=None` default), operates on `ConversationManager` not our `TurnBasedSessionManager`; the `compaction` SSE event still emits exactly once (PR #243 invariant preserved; new regression test `test_compaction_sse_emit_once.py`). Full local backend suite: 2887 passed / 3 skipped on 1.40.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #6

### [2026-05-10] Promote tool-result rendering to a per-tool renderer registry (MCP Apps PR #0) → RESOLVED — shipped
- **Decision**: Ship — reviews/2026-05-15.md ▸ Proposal #5
- **Reasoning**: Shipped in PR #339 (`refactor(chat): tool-result renderer registry (MCP Apps PR #0)`, merged 2026-05-18). Pure refactor — implicit text/JSON/image switch lifted into a signal-backed `ToolRendererRegistryService` keyed by tool name; `DefaultToolResultComponent` reproduces prior markup verbatim (zero user-visible change); `calculator` / `fetch_url_content` / `create_visualization` migrated as proof points. 1014/1014 frontend tests green (14 new, DI-token overrides not `vi.mock`). Unblocks MCP Apps PR #1; the PR #4 MCP App renderer now plugs in as just-another-registered-renderer.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #5

### [2026-05-15] Bump `bedrock-agentcore` 1.6.4 → 1.9.1 → RESOLVED — shipped
- **Decision**: Ship — reviews/2026-05-15.md ▸ Proposal #1
- **Reasoning**: Shipped in PR #337 (`chore(deps): bump bedrock-agentcore 1.6.4 → 1.9.1 (+ coupled boto3 1.43.9)`, merged 2026-05-18). Closes the structural version-pin lag now that Dependabot version-updates are disabled (#293); first proof the kaizen loop catches lag without Dependabot.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #1

### [2026-05-15] Audit and fix `/ping` to emit `time_of_last_update` (#471) → RESOLVED — shipped
- **Decision**: Ship — reviews/2026-05-15.md ▸ Proposal #2
- **Reasoning**: Shipped in PR #338 (kaizen bundle, merged 2026-05-18). `/ping` now emits an integer `time_of_last_update` + corrected `Healthy` casing. Accepted trade-off documented in the PR: a fresh per-ping timestamp disables ping-based idle reaping for this runtime — we can't report `HealthyBusy` without async-task busy tracking (deferred `async_mode` work).
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #2

### [2026-05-15] Defensive A2A AgentCard `capabilities={"streaming": True}` check → RESOLVED — guard documented
- **Decision**: Ship (docs-only) — reviews/2026-05-15.md ▸ Proposal #4
- **Reasoning**: Resolved in PR #338 (merged 2026-05-18). A2A is client-only today (no server `AgentCard` exists), so there is no code site to patch. Added a forward-looking guard to `CLAUDE.md`: the first A2A server construct MUST advertise `capabilities` with `streaming=True`, else A2A clients hang ~40 min (ref-repo `50c9112`).
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #4

### [2026-05-10] Close issues #266 and #267 — features already in our Strands 1.39 pin → RESOLVED — decided (NOT closed; premise corrected)
- **Decision**: Decided, premise corrected — reviews/2026-05-15.md ▸ Proposal #7 (via PR #338)
- **Reasoning**: The review's "phantom tech debt — close them" framing was **wrong**. #266 (large tool-result offload) and #267 (context-window lookup fallback) are live, well-specified Strands adoption/wiring tasks whose 1.39 precondition is now met. Decision (PR #338, GitHub-only): posted "unblocked, keep open" comments on both — NOT closed. Logged in decisions.md so future research does not re-propose closing them.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #7

### [2026-05-10] Replace dead source URLs in `kaizen-research` skill (+ starter-toolkit slug) → RESOLVED — shipped
- **Decision**: Ship — reviews/2026-05-15.md ▸ Proposal #9
- **Reasoning**: Shipped in PR #338 (merged 2026-05-18). Replaced/dropped dead source URLs in `kaizen-research/SKILL.md`; fixed `aws/amazon-bedrock-agentcore-*` → `aws/bedrock-agentcore-*` slug — the review flagged the starter-toolkit; the sdk-python line had the same typo and was also fixed.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #9

### [2026-05-10] Add Reddit `.rss` or Reddit MCP to `kaizen-research` → RESOLVED — declined
- **Decision**: Decline — reviews/2026-05-15.md ▸ Retirement Candidates
- **Reasoning**: research/2026-05-15.md confirmed Reddit is blocked at the *domain* level via WebFetch (not just the HTML path), so the proposal as scoped is infeasible. Logged in decisions.md; revisit only if a Reddit MCP or `curl`-via-Bash-with-UA-header path becomes available.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Retirement Candidates

### [2026-05-10] Scope an MCP Apps host renderer in our chat (multi-PR initiative) → RESOLVED — scoping landed
- **Decision**: Ship (scope only) — reviews/2026-05-10.md ▸ Proposal #1
- **Reasoning**: Scoping doc `docs/kaizen/scoping/mcp-apps-host-renderer.md` landed in PR #296 (May 14, 2026). Four open architectural questions locked: sandbox-proxy origin, app-initiated `tools/call` plumbing, `ui/update-model-context` storage in Strands `agent.state`, full v1 method scope. PR #0 → PR #6 sequence defined; build work is now tracked via the renderer-registry queue item (PR #0 of that sequence).
- **Reviewed-in**: reviews/2026-05-10.md ▸ Proposal #1

### [2026-05-10] Triage Nightly Build & Test failure cluster (9× since May 6) → RESOLVED — fixed
- **Decision**: Ship — reviews/2026-05-10.md ▸ Proposal #6
- **Reasoning**: PR #290 (`Fix e2e testing in nightly`, May 12) landed. The Nightly Build & Test workflow has been silent since — research/2026-05-15.md confirms 0 failures in the May 10–15 window. Loop caught and resolved CI hygiene.
- **Reviewed-in**: reviews/2026-05-10.md ▸ Proposal #6

### [2026-05-10] Bump `bedrock-agentcore` 1.6.4 → 1.9.0 → RESOLVED — superseded
- **Decision**: Superseded
- **Reasoning**: Replaced by the 2026-05-15 re-prioritized entry (`1.6.4 → 1.9.1`) — lag widened from 3 → 4 versions in window, and Dependabot version-updates were disabled by #293 (May 13), so the lag is now structural rather than incidental. The re-prioritized entry shipped in PR #337.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #1
