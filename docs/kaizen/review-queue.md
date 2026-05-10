# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open

### [2026-05-10] Patch 4 high-severity `fast-uri` Dependabot alerts (#116, #117, #121, #122)
- **Source**: research/2026-05-10.md ▸ Top 7 #1 ▸ Security posture
- **Surface**: frontend (`frontend/ai.client/package.json` overrides)
- **Effort × Impact**: L × H
- **Subtracts**: no — security fix
- **Status**: open

### [2026-05-10] Bump `bedrock-agentcore` 1.6.4 → 1.9.0
- **Source**: research/2026-05-10.md ▸ Top 7 #2
- **Surface**: backend
- **Effort × Impact**: L × M
- **Subtracts**: no — pure dep bump (justified: 3 versions of upstream fixes, latest in scan window)
- **Status**: open

### [2026-05-10] Fix 3 `py/log-injection` error-severity CodeQL findings (#623, #624, #625, #631)
- **Source**: research/2026-05-10.md ▸ Top 7 #3 ▸ Security posture
- **Surface**: backend (`chat/service.py`, `agent_types.py`, `connectors/routes.py`)
- **Effort × Impact**: L-M × M-H
- **Subtracts**: no — security fix
- **Status**: open

### [2026-05-10] Audit `BedrockModel.stream` cancellation path against Strands #2266
- **Source**: research/2026-05-10.md ▸ Top 7 #4
- **Surface**: backend
- **Effort × Impact**: L × M-H
- **Subtracts**: no — defensive (SSE-disconnect path is hot)
- **Status**: open

### [2026-05-10] Close issues #266 and #267 — features already in our Strands 1.39 pin
- **Source**: research/2026-05-10.md ▸ Top 7 #5
- **Surface**: cross-cutting
- **Effort × Impact**: L × M
- **Subtracts**: **yes — library-native subtraction; retires 2 build-from-scratch issues**
- **Status**: open

### [2026-05-10] Audit `oauth_required` SSE flow against ref-repo's mid-tool-call 401/403 handling
- **Source**: research/2026-05-10.md ▸ Top 7 #6
- **Surface**: backend
- **Effort × Impact**: M × H
- **Subtracts**: no — defensive
- **Status**: open

### [2026-05-10] Triage Nightly Build & Test failure cluster (9× since May 6)
- **Source**: research/2026-05-10.md ▸ Top 7 #7
- **Surface**: cross-cutting / CI
- **Effort × Impact**: L-M × M-H
- **Subtracts**: possibly — if root is issue #220 (test isolation)
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
