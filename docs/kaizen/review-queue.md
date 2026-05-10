# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open

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
