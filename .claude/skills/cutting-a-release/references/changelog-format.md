# `CHANGELOG.md` — structure & entry skeleton

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with the category
emojis from the change-identification step. Terse, factual, PR-linked. No
narrative — that lives in `RELEASE_NOTES.md`. New version goes at the **top**.

## Full file header (first time only)

```markdown
# Changelog

All notable changes to this project are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For narrative release notes written for operators and product owners, see [RELEASE_NOTES.md](RELEASE_NOTES.md).
```

## Per-release entry

A short one-paragraph lead is enough, then categorized one-line bullets. Omit
categories with no entries.

```markdown
## [X.Y.Z] - YYYY-MM-DD

<One-paragraph summary of the release theme.>

### 🚀 Added
- Voice mode with Nova Sonic bidirectional audio streaming (`/voice/stream` WebSocket endpoint) (#234)
- `create_agent()` factory supporting `chat`, `skill`, and `voice` agent types (#235)

### ✨ Improved
- Tool admin changes now propagate on the next chat turn via TTL-cached DynamoDB snapshot (#240)

### ⚠️ Changed
- **Breaking:** Removed `/oauth/*` routes and the in-house token vault. External MCP tools now use AgentCore Identity (#241)

### 🐛 Fixed
- Duplicate sidebar entries caused by `ensure_session_metadata_exists` conditional collision (#248)

### 🔒 Security
- Markdown-rendered links now carry `rel="noopener noreferrer"` to prevent reverse-tabnabbing (#252)

### 📦 Dependencies
- Backend: `fastapi` 0.135.3 → 0.136.1, `strands-agents` 1.34.1 → 1.37.0
- CI: `github/codeql-action` 4.35.1 → 4.35.2

### 🏗️ Infrastructure
- New `CfnWorkloadIdentity` (`<projectPrefix>-platform-workload`) shared between app-api and inference-api (#241)

### 🔧 CI/CD
- E2E pipeline added with dynamic CloudFront URL discovery and Cognito user provisioning (#255)
```

## Style rules

- **One line per change.** If it needs more, it belongs as a spotlight in
  `RELEASE_NOTES.md` with only a pointer here.
- **Reference PRs with `(#NNN)`** when known. If the PR number isn't available,
  omit it — don't invent.
- **Breaking changes stay prominent:** prefix with `**Breaking:**` and include
  migration steps inline or link to the release-notes section.
- **Dependency sections** can collapse minor bumps into one line per component;
  the full From/To table lives in `RELEASE_NOTES.md`.
- **Omit empty categories** — don't render headings with no entries.

## Keeping the two documents in sync

1. Build one master bullet list of every change; categorize and filter it.
2. Write `CHANGELOG.md` first (the factual log).
3. Write `RELEASE_NOTES.md` next, promoting the largest items into narrative
   spotlights and leaving the rest as per-category bullets.
4. Cross-check: every `CHANGELOG.md` line maps to something in `RELEASE_NOTES.md`
   (spotlight, bullet, or table row) — except the changelog-only exceptions
   (minor dep bumps, internal test additions).
