# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open

### [2026-05-15] Bump `bedrock-agentcore` 1.6.4 → 1.9.1 (re-prioritized — lag widened 3 → 4)
- **Source**: research/2026-05-15.md ▸ Top 5 #1. Re-surfacing of the 2026-05-10 queue item — lag widened and Dependabot version-updates were disabled in #293 (May 13), so this won't get there on its own.
- **Surface**: backend (`backend/pyproject.toml`, `backend/uv.lock`)
- **Effort × Impact**: L × M-H
- **Subtracts**: no — pure dep bump (justified: 4 versions of upstream fixes, latest 2026-05-12; sets up adoption of PR #478 `async_mode` once 1.10.0 ships)
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #1 (Ship)

### [2026-05-15] Audit and fix `/ping` to emit `time_of_last_update` (AgentCore SDK issue #471)
- **Source**: research/2026-05-15.md ▸ Top 5 #2 — https://github.com/aws/bedrock-agentcore-sdk-python/issues/471
- **Surface**: backend (`backend/src/apis/inference_api/` `/ping` handler — one of the two routes the AgentCore Runtime data plane actually serves)
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive against silent microVM reaping on long generations
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #2 (Ship)

### [2026-05-15] Strands 1.39 → 1.40 bump, gated on `use_native_token_count` audit + proactive-compression double-fire check
- **Source**: research/2026-05-15.md ▸ Top 5 #3 — Strands v1.40.0 release notes + breaking PR #2284
- **Surface**: backend (`backend/pyproject.toml`, `apis/shared/` token-metric reads, `agents/main_agent/streaming/`, `TurnBasedSessionManager`)
- **Effort × Impact**: M × M-H
- **Subtracts**: **yes — library-native subtraction.** Strands' proactive context compression (PR #2239) reduces the surface area of our custom session-manager compaction logic.
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #6 (Ship)

### [2026-05-15] Defensive A2A AgentCard `capabilities={"streaming": True}` check
- **Source**: research/2026-05-15.md ▸ Top 5 #4 — aws-samples/sample-strands-agent-with-agentcore commit `50c9112`
- **Surface**: backend (A2A AgentCard construction sites)
- **Effort × Impact**: L × M
- **Subtracts**: no — defensive against silent 40-min A2A-client timeouts
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #4 (Ship)

### [2026-05-15] Wire per-tool `duration_ms` into `tool_result` SSE
- **Source**: research/2026-05-15.md ▸ Top 5 #5 — Claude Code 2.1.141 hook pattern
- **Surface**: backend (Strands `AfterToolCall` hook) + frontend (`<tool-result>` component — inline timing badge for `> 250ms`)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: partial — single hook-driven field replaces any ad-hoc per-tool timing; pre-paves the planned context-attribution prototype
- **Unlocks**:
  - Per-tool timing visibility in the UI (which slow tool is the bottleneck on this turn?)
  - Data substrate for the planned context-attribution prototype — separates tool latency from token cost
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #3 (Ship)

### [2026-05-15] Investigate inference-api deploy — new images reach ECR but Runtime isn't rolled (issue #288)
- **Source**: reviews/2026-05-15.md ▸ Proposal #10 (new from internal friction, issue #288 May 12). Pairs with the 1.6.4 → 1.9.1 bump (same SDK package owns `update_agent_runtime`).
- **Surface**: cross-cutting — `.github/workflows/deploy-inference-api.yml` + bedrock-agentcore SDK `update_agent_runtime` call shape
- **Effort × Impact**: L-M × M-H
- **Subtracts**: possibly — removes the manual-redeploy band-aid that's been the workaround
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #10 (Ship)

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

### [2026-05-10] Promote tool-result rendering to a per-tool renderer registry (PR #0 of MCP Apps host sequence)
- **Source**: research/2026-05-10.md ▸ Top 6 #3 ▸ Agentic UI/UX (AI SDK + Cursor). Locked in as PR #0 of the MCP Apps host renderer sequence (`docs/kaizen/scoping/mcp-apps-host-renderer.md`, PR #296).
- **Surface**: frontend (`<tool-result>` component + new `ToolRendererRegistry` service)
- **Effort × Impact**: M × M-H
- **Subtracts**: partial — replaces implicit switch with explicit registry; absorbs scattered tool-specific UI logic. Pre-paves MCP Apps PR #4.
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #5 (Ship)

### [2026-05-10] Audit `BedrockModel.stream` cancellation path against Strands #2266
- **Source**: research/2026-05-10.md ▸ Top 6 #4
- **Surface**: backend
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive (SSE-disconnect path is hot)
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #8 (Ship)

### [2026-05-10] Close issues #266 and #267 — features already in our Strands 1.39 pin
- **Source**: research/2026-05-10.md ▸ Top 6 #5
- **Surface**: cross-cutting
- **Effort × Impact**: L × M
- **Subtracts**: **yes — library-native subtraction; retires 2 build-from-scratch issues**
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #7 (Ship)

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

### [2026-05-10] Replace dead source URLs in `kaizen-research` skill (+ AgentCore starter-toolkit slug typo)
- **Source**: research/2026-05-10.md ▸ Retirement candidates + research/2026-05-15.md ▸ Retirement candidates (starter-toolkit slug)
- **Surface**: skills (`.claude/skills/kaizen-research/SKILL.md`)
- **Effort × Impact**: L × L
- **Subtracts**: yes — replaces 2 broken URLs (`bedrock/whats-new/` 404, `docs.claude.com/.../release-notes` 404) with working ones; drops `anthropics/courses` (quiet since Nov 2025); fixes `aws/amazon-bedrock-agentcore-starter-toolkit` → `aws/bedrock-agentcore-starter-toolkit` slug
- **Status**: open — surfaced in reviews/2026-05-15.md ▸ Proposal #9 (Ship)

### [2026-05-10] Add Reddit `.rss` or Reddit MCP to `kaizen-research`
- **Source**: research/2026-05-10.md ▸ Risks ▸ "Reddit blocked from WebFetch"
- **Surface**: skills (`.claude/skills/kaizen-research/SKILL.md`)
- **Effort × Impact**: L × L
- **Subtracts**: no — restores a half-blind source
- **Status**: open — research/2026-05-15.md confirmed Reddit is blocked at the *domain* level via WebFetch (not just the HTML path), so the proposal as scoped is infeasible. reviews/2026-05-15.md ▸ Retirement Candidates recommends **Decline**; move to Resolved + log in `docs/kaizen/decisions.md` after Phil's mark.

## Resolved

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
- **Reasoning**: Replaced by the 2026-05-15 re-prioritized entry (`1.6.4 → 1.9.1`) — lag widened from 3 → 4 versions in window, and Dependabot version-updates were disabled by #293 (May 13), so the lag is now structural rather than incidental.
- **Reviewed-in**: reviews/2026-05-15.md ▸ Proposal #1

