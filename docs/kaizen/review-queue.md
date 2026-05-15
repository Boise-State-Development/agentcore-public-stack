# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open

### [2026-05-15] Bump `bedrock-agentcore` 1.6.4 → 1.9.1 (re-prioritized — lag widened 3 → 4)
- **Source**: research/2026-05-15.md ▸ Top 5 #1. Re-surfacing of the 2026-05-10 queue item — lag widened and Dependabot version-updates were disabled in #293 (May 13), so this won't get there on its own.
- **Surface**: backend (`backend/pyproject.toml`, `backend/uv.lock`)
- **Effort × Impact**: L × M-H
- **Subtracts**: no — pure dep bump (justified: 4 versions of upstream fixes, latest 2026-05-12; sets up adoption of PR #478 `async_mode` once 1.10.0 ships)
- **Status**: open (supersedes the 2026-05-10 queue entry — that one can be closed during review)

### [2026-05-15] Audit and fix `/ping` to emit `time_of_last_update` (AgentCore SDK issue #471)
- **Source**: research/2026-05-15.md ▸ Top 5 #2 — https://github.com/aws/bedrock-agentcore-sdk-python/issues/471
- **Surface**: backend (`backend/src/apis/inference_api/` `/ping` handler — one of the two routes the AgentCore Runtime data plane actually serves)
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive against silent microVM reaping on long generations
- **Status**: open

### [2026-05-15] Strands 1.39 → 1.40 bump, gated on `use_native_token_count` audit + proactive-compression double-fire check
- **Source**: research/2026-05-15.md ▸ Top 5 #3 — Strands v1.40.0 release notes + breaking PR #2284
- **Surface**: backend (`backend/pyproject.toml`, `apis/shared/` token-metric reads, `agents/main_agent/streaming/`, `TurnBasedSessionManager`)
- **Effort × Impact**: M × M-H
- **Subtracts**: **yes — library-native subtraction.** Strands' proactive context compression (PR #2239) reduces the surface area of our custom session-manager compaction logic.
- **Status**: open

### [2026-05-15] Defensive A2A AgentCard `capabilities={"streaming": True}` check
- **Source**: research/2026-05-15.md ▸ Top 5 #4 — aws-samples/sample-strands-agent-with-agentcore commit `50c9112`
- **Surface**: backend (A2A AgentCard construction sites)
- **Effort × Impact**: L × M
- **Subtracts**: no — defensive against silent 40-min A2A-client timeouts
- **Status**: open

### [2026-05-15] Wire per-tool `duration_ms` into `tool_result` SSE
- **Source**: research/2026-05-15.md ▸ Top 5 #5 — Claude Code 2.1.141 hook pattern
- **Surface**: backend (Strands `AfterToolCall` hook) + frontend (`<tool-result>` component — inline timing badge for `> 250ms`)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: partial — single hook-driven field replaces any ad-hoc per-tool timing; pre-paves the planned context-attribution prototype
- **Unlocks**:
  - Per-tool timing visibility in the UI (which slow tool is the bottleneck on this turn?)
  - Data substrate for the planned context-attribution prototype — separates tool latency from token cost

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
- **Status**: open

### [2026-05-10] Scope an MCP Apps host renderer in our chat (multi-PR initiative)
- **Source**: research/2026-05-10.md ▸ Top 6 #1 ▸ Agentic UI/UX
- **Surface**: frontend + backend (new SSE event `ui_resource`; `<mcp-app-frame>` Angular component; consent UX)
- **Effort × Impact**: H × H
- **Subtracts**: no — pure addition. Justified: every major host (Claude Desktop, ChatGPT, VS Code Copilot, Goose, Postman) ships this; without it, third-party MCP servers we connect can only deliver text+JSON
- **Status**: open

### [2026-05-10] Bump `bedrock-agentcore` 1.6.4 → 1.9.0
- **Source**: research/2026-05-10.md ▸ Top 6 #2
- **Surface**: backend
- **Effort × Impact**: L × M
- **Subtracts**: no — pure dep bump (justified: 3 versions of upstream fixes, latest in scan window)
- **Status**: open

### [2026-05-10] Promote tool-result rendering to a per-tool renderer registry (signal-backed)
- **Source**: research/2026-05-10.md ▸ Top 6 #3 ▸ Agentic UI/UX (AI SDK + Cursor)
- **Surface**: frontend (`<tool-result>` component + new `ToolRendererRegistry` service)
- **Effort × Impact**: M × M-H
- **Subtracts**: partial — replaces implicit switch with explicit registry; absorbs scattered tool-specific UI logic. Pre-paves MCP Apps proposal #1.
- **Status**: open

### [2026-05-10] Audit `BedrockModel.stream` cancellation path against Strands #2266
- **Source**: research/2026-05-10.md ▸ Top 6 #4
- **Surface**: backend
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive (SSE-disconnect path is hot)
- **Status**: open

### [2026-05-10] Close issues #266 and #267 — features already in our Strands 1.39 pin
- **Source**: research/2026-05-10.md ▸ Top 6 #5
- **Surface**: cross-cutting
- **Effort × Impact**: L × M
- **Subtracts**: **yes — library-native subtraction; retires 2 build-from-scratch issues**
- **Status**: open

### [2026-05-10] Triage Nightly Build & Test failure cluster (9× since May 6)
- **Source**: research/2026-05-10.md ▸ Top 6 #6
- **Surface**: cross-cutting / CI
- **Effort × Impact**: L-M × M-H
- **Subtracts**: possibly — if root is issue #220 (test isolation)
- **Status**: open

### [2026-05-10] Audit `oauth_required` SSE flow against ref-repo's mid-tool-call 401/403 handling
- **Source**: research/2026-05-10.md ▸ Risks
- **Surface**: backend
- **Effort × Impact**: M × H
- **Subtracts**: no — defensive
- **Status**: open (deferred 2 weeks per prior review — surface again on 2026-05-24)

### [2026-05-10] Named A2A agent participants in the chat UI
- **Source**: research/2026-05-10.md ▸ Agentic UI/UX ▸ Linear Agent pattern
- **Surface**: frontend (extend message model with `agent_identity`, distinct avatar/name/styling)
- **Effort × Impact**: L-M × M
- **Subtracts**: no — additive but pattern-validated across Linear/ChatGPT/Cursor
- **Status**: open

### [2026-05-10] Replace dead source URLs in `kaizen-research` skill
- **Source**: research/2026-05-10.md ▸ Retirement candidates
- **Surface**: skills (`.claude/skills/kaizen-research/SKILL.md`)
- **Effort × Impact**: L × L
- **Subtracts**: yes — replaces 2 broken URLs (`bedrock/whats-new/` 404, `docs.claude.com/.../release-notes` 404) with working ones; drops `anthropics/courses` (quiet since Nov 2025)
- **Status**: open

### [2026-05-10] Add Reddit `.rss` or Reddit MCP to `kaizen-research`
- **Source**: research/2026-05-10.md ▸ Risks ▸ "Reddit blocked from WebFetch"
- **Surface**: skills (`.claude/skills/kaizen-research/SKILL.md`)
- **Effort × Impact**: L × L
- **Subtracts**: no — restores a half-blind source
- **Status**: open

## Resolved
<!-- kaizen-review-prep moves entries here after a review. Bootstrap run — empty. -->
