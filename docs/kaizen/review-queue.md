# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open

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
