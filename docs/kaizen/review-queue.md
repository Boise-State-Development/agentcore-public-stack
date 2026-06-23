# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open
<!-- Newest at top. -->

### [2026-06-19] Wire configurable Bedrock Guardrails (issue #480)
- **Source**: research/2026-06-19.md ▸ Top 5 #1 — internal issue #480 (June 15) + AWS Summit NYC Guardrails cluster (`InvokeGuardrailChecks` API + AgentCore policy Guardrails GA, June 16). Strands `BedrockModel` already supports `guardrail_id`/`version`/`stream_processing_mode`/`trace`.
- **Surface**: backend (`inference_api` `BedrockModel` construction) + infrastructure (optional `CDK_GUARDRAIL_ID` / `CDK_GUARDRAIL_VERSION` env vars threaded to inference-api runtime env)
- **Effort × Impact**: L-M × H
- **Subtracts**: addition only — config wiring of a capability Strands already exposes; zero-cost when unset; mirrors `CDK_ARTIFACTS_ENABLED`/`CDK_MCP_SANDBOX_ENABLED` optional-feature pattern
- **Unlocks**: deployers attach content-safety filtering + staff-alerting monitoring to all model invocations without modifying inference-api source (FERPA duty-of-care for higher-ed: proactive self-harm/crisis-language monitoring Claude's reactive layer doesn't surface)
- **Status**: open — strongest fit of the week (filed issue + library-native path + AWS feature cluster aligned). Verify the guardrail *resource* region availability; confirm guardrail streaming mode is compatible with the SSE relay.

### [2026-06-19] Fix Nightly Build & Test (`exit 127` at install — ~14 consecutive failures)
- **Source**: research/2026-06-19.md ▸ Internal Audit + Top 5 #2 — `gh run view 27820449858 --log-failed` shows `exit code 127` on every install/setup step (June 19); failing daily since June 5. Carries the [2026-06-12] nightly item forward with a sharper diagnosis (was "root cause unknown").
- **Surface**: CI — `.github/workflows/` nightly workflow install/setup steps (`setup-uv` / `setup-node` / cache action)
- **Effort × Impact**: L × H
- **Subtracts**: no — hygiene; prerequisite for trusting the Strands 1.44 + bedrock-agentcore 1.15 bumps
- **Status**: open — time-sensitive; CI environment/runner regression (a binary expected by install returns 127, likely `uv`/`node` PATH after an action or runner-image change), NOT a test regression. Do not land dep bumps on a suite that can't install. Supersedes the [2026-06-12] nightly item.

### [2026-06-19] Bump Strands 1.40 → 1.44 — supersedes the [2026-06-12] 1.43 keystone
- **Source**: research/2026-06-19.md ▸ Top 5 #3 — strands-agents v1.44.0 (June 16). **Supersedes** [2026-06-12] "Strands 1.40 → 1.43" — target advances one more minor; no new Python breaking changes 1.41–1.44.
- **Surface**: backend (`pyproject.toml`/`uv.lock` — `strands-agents` 1.40→1.44, `strands-agents-tools` 0.5.2→0.8.1; agent invocation in `inference_api`; `BedrockModel`/`CacheConfig`; SSE `limit_*` stop-reason; tooling that assumed the old `vX.Y.Z` tag scheme — now `python/vX.Y.Z` in the `harness-sdk` monorepo)
- **Effort × Impact**: M × H
- **Subtracts**: yes — library-native `Limits` retires the hand-rolled runaway guardrail; `cache_tools_ttl` retires hand-rolled TTL; collapses the superseded 1.42/1.43 keystone entries + the #2635 guard into one PR
- **Unlocks**: native per-turn cost ceiling; 1h prompt caching; accurate context attribution on tool-heavy turns
- **Status**: open — prerequisite: green Nightly (see above). **Do NOT** adopt the new native memory-manager / agentic-context-management ports on this bump (they overlap `TurnBasedSessionManager`; decisions.md bars a bare swap) — scope a separate compat review. #2636 (non-ASCII) still live in 1.44.0 — add a known-limitation comment; a second bump follows once #2661/#2653 merge.

### [2026-06-19] Bump `bedrock-agentcore` 1.9.1 → 1.15.0 + adopt `async_mode`
- **Source**: research/2026-06-19.md ▸ Top 5 #4 — bedrock-agentcore v1.15.0 (June 17); 6 minors behind. **Supersedes** the [2026-06-12] "1.9.1 → 1.14.1" item and consolidates the [2026-05-22] bump + async_mode items. The #482 SSE-disconnect-deadlock fix lives in the 1.14.x line.
- **Surface**: `backend/pyproject.toml` + `uv.lock`; `AgentCoreMemoryConfig` construction (`async_mode`); inference-api `/invocations` SSE worker (the #482 deadlock path)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: yes — folds the deferred [2026-05-22] "#482 SSE-disconnect deadlock guard" into a dep bump (upstream fix instead of a hand-rolled guard); `async_mode` retires the latent #452 event-loop-blocking mode; consolidates 2–3 queue items
- **Unlocks**: interactive-shell API access; bearer-token integration; A2A cap prerequisite for the first A2A server PR
- **Status**: open — bundle as the "dep hygiene" PR after the Nightly is green; verify the #482 fix exercises our SSE-disconnect path.

### [2026-06-19] Ship the interactive context-breakdown badge (Cursor + LibreChat convergence)
- **Source**: research/2026-06-19.md ▸ Top 5 #5 — LibreChat v0.8.7-rc1 real-time context gauge + Cursor Context Usage Report (2026-06-05) + internal PR #433. **Reinforces** the [2026-06-05] "make the context-breakdown badge interactive" item with a second independent product datapoint.
- **Surface**: frontend (context-breakdown badge component in `frontend/ai.client/src/app/session/`)
- **Effort × Impact**: M × M
- **Subtracts**: no — addition; lands on a surface we shipped and reuses `contextBreakdown` already on the final `metadata` event
- **Unlocks**: user-facing context-cost transparency + an actionable "what's eating context / how to trim it" follow-up
- **Status**: open — presentation-layer only (no backend change). Now validated by two products. Consider folding into / superseding the [2026-06-05] item at review.

### [2026-06-12] Add Claude Fable 5 to model settings + audit model-ID string matching — ⚠️ WITHDRAW (Fable 5 revoked on Bedrock)
- **Source**: research/2026-06-19.md ▸ Retirement candidates — Fable 5 + Mythos 5 were **revoked on Bedrock for all users (US gov directive, June 12–13)**, three days after their June 9 GA. The "add Fable 5 / consider as default" core of the original [2026-06-12] item is dead until/unless the directive lifts.
- **Surface**: n/a (withdrawal)
- **Effort × Impact**: — × —
- **Subtracts**: yes — removes a queued addition that external availability killed
- **Status**: open — **recommend review-prep mark the [2026-06-12] Fable 5 item RESOLVED (Decline/superseded)**. Minor residual merit: the `claude-opus-4` capability-gate string-match audit is still worth doing for future-proofing, but decoupled from Fable 5. Opus 4.8 stays the default/floor.

### [2026-06-12] Bump Strands 1.40 → 1.43 — supersedes [2026-06-05] keystone; closes #2635 + context_manager="auto" + A2A isolation fix
- **Source**: research/2026-06-12.md ▸ Top 5 #1 — Strands v1.43.0 released June 12, 2026. **Supersedes** [2026-06-05] "Strands 1.40 → 1.42 bump" — target advances one more minor; no additional blast radius. Also closes the [2026-06-05] "#2635 guard" queue item.
- **Surface**: backend (`pyproject.toml`/`uv.lock` — `strands-agents==1.40.0` → `==1.43.0`; agent invocation in `inference_api`; `BedrockModel`/`CacheConfig`; SSE `limit_*` stop-reason)
- **Effort × Impact**: M × H
- **Subtracts**: yes — library-native `Limits` retires the hand-rolled runaway guardrail; `cache_tools_ttl` retires hand-rolled TTL; #2635 defensive guard resolves as part of the bump; three queue items collapse into one PR
- **Unlocks**: native per-turn cost ceiling; 1h prompt caching; accurate context attribution on tool-heavy turns
- **Status**: open — prerequisite: confirm Nightly CI is green (see [2026-06-12] nightly investigation item) before landing. #2636 (non-ASCII) still live in 1.43.0 — add a known-limitation comment, a second bump will follow once PR #2661 merges.

### [2026-06-12] Add Claude Fable 5 to model settings + audit model-ID string matching
- **Source**: research/2026-06-12.md ▸ Top 5 #2 — Claude Fable 5 GA June 9 (https://aws.amazon.com/about-aws/whats-new/2026/06/claude-fable-5-aws/). Naming convention shift (`-fable-`/`-mythos-` suffixes vs. `claude-opus-4.N`) is a live breakage risk.
- **Surface**: frontend (`model-settings.html`, `model-settings.ts` — add `claude-fable-5` to dropdown) + backend (grep `claude-opus-4` in capability gates: prompt-caching beta header, fine-grained tool-streaming beta header; admin model catalog)
- **Effort × Impact**: L-M × H
- **Subtracts**: partial — Fable 5 at $10/$50/M may replace Opus 4.8 as default once benchmarked; no hard retirement yet
- **Unlocks**: top-of-range Anthropic model on Bedrock at materially lower cost; model-list parity for end users
- **Status**: open — use the `claude-api` skill to confirm exact Bedrock IDs before committing; verify context window + caching API support on Bedrock model card before flipping to default

### [2026-06-12] Investigate + triage Nightly Build & Test (7 consecutive failures June 5–12)
- **Source**: research/2026-06-12.md ▸ Internal Audit — CI failures. Same pattern resolved via PR #290 in May; root cause unknown this time.
- **Surface**: CI — `.github/workflows/` nightly workflow + backend test suite
- **Effort × Impact**: L × H
- **Subtracts**: no — hygiene; prerequisite for trusting the Strands 1.43 keystone bump and any other dep changes
- **Status**: open — time-sensitive; run `gh run view <latest-nightly-id> --log-failed`; classify flaky-vs-regression; quarantine or file. Do not land dep bumps on an untrusted suite.

### [2026-06-12] Bump `bedrock-agentcore` 1.9.1 → 1.14.1 + adopt `async_mode` + note A2A cap prerequisite
- **Source**: research/2026-06-12.md ▸ Top 5 #4 — bedrock-agentcore v1.14.1 (June 11). **Consolidates** [2026-05-22] "Bump bedrock-agentcore 1.9.1 → 1.11.0" and [2026-05-22] "Re-bump 1.9.1 → 1.11.0 + async_mode" open items (which were already 4+ minors behind; now 5).
- **Surface**: `backend/pyproject.toml` + `uv.lock`; `AgentCoreMemoryConfig` construction (`async_mode` adoption)
- **Effort × Impact**: L × M
- **Subtracts**: `async_mode` adoption retires the latent #452 event-loop-blocking failure mode; two queue items consolidate into one
- **Unlocks**: interactive shell API access; A2A cap fix is a hard prerequisite for the first A2A server PR
- **Status**: open — can bundle with the starlette CVE bump (#5 below) as a single "dep hygiene" PR

### [2026-06-12] Bump `starlette` 1.0.0 → 1.0.1 to close CVE-2026-48710
- **Source**: research/2026-06-12.md ▸ Top 5 #5 — FastMCP v3.4.1 (June 5) surfaced CVE-2026-48710 affecting starlette < 1.0.1. Our `pyproject.toml` pins `starlette==1.0.0`.
- **Surface**: `backend/pyproject.toml` — 1-line pin bump
- **Effort × Impact**: L × M
- **Subtracts**: no — 1-line security fix; the existing comment says the pin was already security-motivated
- **Status**: open — bundle with bedrock-agentcore bump (#4 above) as a single dep-hygiene PR; also flag to MCP server repos to bump FastMCP to ≥3.4.1

### [2026-06-05] Strands 1.40 → 1.42 bump — unblocks `Limits` (cost caps) + `cache_tools_ttl` (#269 caching)
- **Source**: research/2026-06-05.md ▸ Top 5 #1 — Strands v1.42.0 (June 1). **Consolidates** the queued 2026-05-22 "Strands 1.40→1.41 + caching #269" item AND the 2026-05-29 #2 "Adopt Strands `Limits`" item — both were gated on 1.42, which is now out. Treat as one keystone bump, not two.
- **Surface**: backend (`pyproject.toml`/`uv.lock`, agent invocation in `inference_api`, `BedrockModel`/`CacheConfig`, SSE `limit_*` stop-reason) + infrastructure (CloudWatch Bedrock-spend alarm)
- **Effort × Impact**: M × H
- **Subtracts**: yes — adopts library-native `Limits` (retires the hand-rolled runaway guardrail) + `cache_tools_ttl` (retires the hand-rolled TTL); two queued items collapse into one bump
- **Unlocks**: native per-turn cost ceiling (`limit_*` stop_reason) + end-to-end 1h prompt caching → lower input-token cost, surfaced in the admin "Cache Savings" card
- **Status**: open — 1.42 is released (no longer gated). Blast radius to audit first: `strands-agents-tools` 0.5→0.8 (possible breaking tool-interface changes) + `starlette` 1.2.1 (FastAPI 0.136.x transitive compat)

### [2026-06-05] Guard the context-attribution path against Strands `count_tokens` toolResult=0 bug (#2635)
- **Source**: research/2026-06-05.md ▸ Top 5 #2 — Strands issue #2635 (open) + internal PR #428–433 (context-attribution feature, shipped this window)
- **Surface**: backend (`CountTokensBedrockModel`, the `contextBreakdown` hook/coordinator channel, the compaction trigger)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: no — defensive; protects the freshly-shipped context-breakdown badge
- **Status**: open — time-sensitive; confirm native Bedrock CountTokens is used for all turns (incl. JSON toolResults) and the heuristic path #2635 affects is never hit; add a regression test asserting a non-zero count for a turn with a JSON toolResult

### [2026-06-05] Make the context-breakdown badge interactive (Cursor Context Usage Report pattern)
- **Source**: research/2026-06-05.md ▸ Top 5 #3 — Cursor "Context Usage Report" (https://cursor.com/changelog/canvas-improvements) + internal PR #433
- **Surface**: frontend (context-breakdown badge component + Artifacts docked panel)
- **Effort × Impact**: M × M
- **Subtracts**: no — addition; justified because it lands on a surface we shipped last week and reuses `contextBreakdown` data already on the final metadata event
- **Unlocks**: user-facing context-cost transparency + an actionable "what's eating context / how to trim it" follow-up
- **Status**: open — presentation-layer work; no new backend (data already on the wire)

### [2026-06-05] Bump `docling` past the 2.81.0 content-sniffing defect → close #405 (`.txt` uploads fail)
- **Source**: research/2026-06-05.md ▸ Top 5 #4 — docling 2.97.0 (June 3) + internal issue #405
- **Surface**: backend (document-ingestion docling dep pin)
- **Effort × Impact**: L × M
- **Subtracts**: yes — library-native bump closes an open user-facing bug; no custom workaround needed
- **Status**: open — cleanest subtraction of the week; bump off 2.81.x, verify `.txt` upload, close #405

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

### [2026-05-29] Align MCP Apps capability advertisement to spec-canonical `io.modelcontextprotocol/ui`
- **Source**: research/2026-05-29.md ▸ Top 5 #3 — SEP-1865 folded into the 2026-07-28 draft spec, PR #2791 (May 27)
- **Surface**: backend (inference-api `initialize` capability advertisement — currently `experimental.ui`; the `ui_resource` SSE path)
- **Effort × Impact**: L-M × M
- **Subtracts**: yes — retires our pre-standard `experimental.ui` identifier in favor of the conformant name
- **Unlocks**: RC-conformant negotiation with future MCP hosts/servers once the spec stabilizes (~2026-07-28)
- **Status**: open — on our timeline before the RC stabilizes; diff the merged draft for any change to the declare-templates-ahead-of-time / tool-list prefetch shape

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

### [2026-05-22] Defensive guard against SDK #482 SSE-disconnect runtime deadlock
- **Source**: research/2026-05-22.md ▸ Top 5 #2 — AgentCore SDK issue #482
- **Surface**: backend (`inference-api` streaming worker — the `/invocations` SSE handler)
- **Effort × Impact**: M × H
- **Subtracts**: no — defensive; silent 78s+ microVM stall on mid-stream client disconnect
- **Status**: open

### [2026-05-22] Bump `bedrock-agentcore` 1.9.1 → 1.11.0
- **Source**: research/2026-05-22.md ▸ Top 5 #3 — SDK v1.10.0/v1.11.0 releases
- **Surface**: backend (`pyproject.toml`, `uv.lock`)
- **Effort × Impact**: L × M
- **Subtracts**: possibly — v1.10.0 header-forwarding may retire a custom `X-Amzn-Custom-` header workaround (audit during bump)
- **Status**: open

### [2026-05-22] Opus 4.7 `temperature`-omission guard
- **Source**: research/2026-05-22.md ▸ Top 5 #4 — ref-repo commit `9385454`
- **Surface**: backend (provider-translation chokepoint — same site as `_shape_thinking_value` / #329 / #331)
- **Effort × Impact**: L × M
- **Subtracts**: no — defensive; Opus 4.7 rejects `temperature` on extended-thinking turns
- **Status**: open

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

### [2026-05-22] Re-bump `bedrock-agentcore` 1.9.1 → 1.11.0 + adopt `async_mode`
- **Source**: reviews/2026-05-22.md ▸ Proposal #2 — re-evaluation of the `async_mode`/#452 risk the 2026-05-15 review explicitly deferred "to the 2026-05-22 review".
- **Surface**: backend (`backend/pyproject.toml`, `backend/uv.lock`, `AgentCoreMemoryConfig` construction)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: no — dep bump; adopting `async_mode` retires the latent #452 event-loop-blocking failure mode
- **Status**: open — surfaced in reviews/2026-05-22.md ▸ Proposal #2 (Ship); no decision logged yet. Lag re-opened to 2 releases the week after #337 closed it.

### [2026-05-22] Fast PR-gate for the deterministic `supply_chain` + `architecture` test subset
- **Source**: reviews/2026-05-22.md ▸ Proposal #6 — root-cause of the Proposal #1 friction (policy violation merged clean because PR-merge CI runs no pytest).
- **Surface**: CI — new lightweight job in the PR workflow
- **Effort × Impact**: L × M
- **Subtracts**: no — addition; converts a recurring post-merge friction class into a pre-merge block. Scoped to two deterministic dirs to avoid reopening the "no full pytest in PR CI" decision.
- **Status**: open — surfaced in reviews/2026-05-22.md ▸ Proposal #6 (Ship scoped, or Defer 2 weeks); no decision logged yet.

## Resolved

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
