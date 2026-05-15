---
inclusion: fileMatch
fileMatchPattern: 'RELEASE_NOTES.md,CHANGELOG.md'
---

# Writing Release Notes & Changelog

This repo maintains **two** release artifacts that describe the same set of changes for different audiences:

| File | Audience | Tone | Length |
|---|---|---|---|
| `RELEASE_NOTES.md` | Product owners, operators, developers shipping the system | Narrative, benefit-first with technical depth | Detailed |
| `CHANGELOG.md` | Developers integrating against the APIs, auditors, release engineers | Terse, factual, PR-linked (Keep a Changelog format) | Compact |

Both files are updated in the **same release pass** from the same source of commits.

---

## Branch Model & Why This Is Hard

This repo uses a squash-merge workflow: `develop` accumulates feature branches via merge commits, and when a release is cut, `develop` is squash-merged into `main`. This means `main` and `develop` have **divergent git histories** — you cannot do a simple `git log main..develop` to get a clean diff. Commit SHAs on `main` don't correspond to anything on `develop`.

## How to Identify What Changed

### Step 1: Find the boundary

Look at the last squash-merge commit on `main` to determine when the previous release was cut:

```bash
git log main --oneline -5
```

Then find the corresponding release tag or date. Use that date as your boundary.

### Step 2: List commits on develop since the boundary

```bash
git log develop --oneline --no-merges --since="<date-of-last-release>"
```

This gives you the raw commit list, but **do not rely solely on commit messages**. Dependabot commits are usually accurate, but human commits often have vague or incomplete messages.

### Step 3: Inspect the actual code changes

For every non-trivial commit, read the diff or at minimum the `--stat` output:

```bash
git show --stat <sha>
git show --no-patch <sha>   # full commit message
```

For feature commits, read the changed files to understand what was actually built — not just what the message claims. Look for:

- New API endpoints (routes files)
- New or modified models/schemas
- New frontend pages or components
- Infrastructure changes (CDK stacks, config)
- New test files (indicates new functionality)
- Dependency changes (`pyproject.toml`, `package.json`)

### Step 4: Categorize every change

Bucket each change into one of the standardized categories. Use the same bucket in both documents so they stay in sync.

| Category | Emoji | When to use |
|---|---|---|
| New | 🚀 | New features, endpoints, pages, or capabilities |
| Improved | ✨ | Enhancements to existing features (UX, readability, ergonomics) |
| Fixed | 🐛 | Bug fixes |
| Changed | ⚠️ | Breaking changes, removals, deprecations, migration-required updates |
| Security | 🔒 | CVE patches, CodeQL fixes, auth hardening |
| Performance | ⚡ | Latency, throughput, or cost reductions users will notice |
| Infrastructure | 🏗️ | CDK/IaC changes, new AWS resources, deploy order changes |
| Dependencies | 📦 | Package upgrades (grouped in a table) |
| CI/CD | 🔧 | Workflow, pipeline, or tooling changes |
| Docs | 📚 | Documentation additions worth calling out |

### Step 5: Decide what to include vs. exclude

**Include in both documents:**
- User-facing changes (features, UX, workflows)
- Operator-facing changes (deploy steps, env vars, infra)
- Bug fixes users or operators would have hit
- Security updates
- Breaking API changes and migrations
- Dependency upgrades (grouped table)

**Include in `CHANGELOG.md` only (not prominent in release notes):**
- Minor dependency bumps with no behavior change
- Internal test additions (unless they signal a new feature)

**Exclude from both:**
- Pure internal refactors with no user or operator impact
- Typo fixes, comment-only changes
- Formatter/linter churn

---

## Translating Technical → User-Benefit

When drafting `RELEASE_NOTES.md`, lead with the outcome, then explain the mechanism. Keep the technical detail — this repo's audience expects it — but don't bury the benefit.

| Engineering commit | Release note framing |
|---|---|
| "Implemented caching layer on tool catalog" | "Tool admin changes now propagate to chat on the next turn (previously required a restart). Backed by a TTL-cached DynamoDB snapshot." |
| "Fixed null pointer in session metadata write" | "Resolved an issue where sessions could accumulate duplicate sidebar entries." |
| "Added OAuth2 USER_FEDERATION flow" | "Users can now connect external MCP tools (Google, Microsoft, GitHub, Canvas) with one-click consent directly from the chat." |
| "Refactored OAuth extractor" | *(exclude — no user impact)* |

---

## `RELEASE_NOTES.md` Format

The new release goes at the **top** of the file. Do not modify previous release sections.

### Header

```markdown
# Release Notes — v1.0.0-beta.XX

**Release Date:** <Month Day, Year>
**Previous Release:** v1.0.0-beta.XX-1 (<date>)

---
```

### Section order

1. **Highlights** — 3-5 sentence standalone summary. Someone reading only this paragraph should understand the release's theme, the 2-3 biggest features, and whether any action is required.
2. **Feature spotlights** — one H2 per major feature. Use subsections for backend / frontend / infrastructure / test coverage. This is where narrative depth belongs.
3. **🐛 Bug fixes** — concise bullet list. Lead with the user-visible symptom, follow with the root cause.
4. **🔒 Security** — CVEs, CodeQL findings, auth hardening.
5. **⚡ Performance** — measurable improvements only.
6. **⚠️ Breaking changes** — migration steps required. Omit if none.
7. **🏗️ Infrastructure** — new resources, SSM parameters, IAM changes operators must know about.
8. **🔧 CI/CD improvements** — workflow and pipeline changes, plus a GitHub Actions upgrade table.
9. **📦 Dependency upgrades** — markdown table with From/To columns, grouped by component (backend / frontend / infra).
10. **🧪 Test coverage** — line counts and scope for notable test additions (optional).
11. **🚀 Deployment notes** — what operators must do differently. Always include, even if the answer is "no special steps."

### Feature spotlight template

```markdown
## <Feature Name>

<1-2 sentence user-facing summary: what changed and why it matters.>

### Backend

- <file / module> — <what changed>

### Frontend

- <component> — <what changed>

### Infrastructure

- <CDK stack / resource> — <what changed>

### Test Coverage

<N>+ lines of new tests covering <scope>.
```

### Writing style

- Match the tone and depth of the existing release notes in the file. They are detailed and technical — written for developers who deploy and maintain this system.
- Every feature section should explain **what** changed, **why** it matters, and **how** it works at a technical level.
- Use specific file names, endpoint paths, and class names when relevant.
- Include line counts for large test additions (e.g., "4,200+ lines of new tests").
- For dependency upgrades, use a markdown table with From/To columns.
- Lead with the user or operator outcome; follow with the mechanism.

---

## `CHANGELOG.md` Format

Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with the category emojis from Step 4. Terse. PR-linked where possible. No narrative.

### Full file header (first time only)

```markdown
# Changelog

All notable changes to this project are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For narrative release notes written for operators and product owners, see [RELEASE_NOTES.md](RELEASE_NOTES.md).
```

### Per-release entry

```markdown
## [1.0.0-beta.XX] - YYYY-MM-DD

### 🚀 Added
- Voice mode with Nova Sonic bidirectional audio streaming (`/voice/stream` WebSocket endpoint) (#234)
- `create_agent()` factory supporting `chat`, `skill`, and `voice` agent types (#235)

### ✨ Improved
- Tool admin changes now propagate on the next chat turn via TTL-cached DynamoDB snapshot (#240)

### ⚠️ Changed
- **Breaking:** Removed `/oauth/*` routes, `OAuthService`, and the in-house token vault. External MCP tools now use AgentCore Identity (#241)
- Settings/connections page removed; connector consent is inline during chat (#241)

### 🐛 Fixed
- Duplicate sidebar entries caused by `ensure_session_metadata_exists` conditional collision (#248)
- `OAuth2CallbackUrl` header stripped by middleware when `provider_id` query param was appended (#249)

### 🔒 Security
- Markdown-rendered links now carry `rel="noopener noreferrer"` to prevent reverse-tabnabbing (#252)

### 📦 Dependencies
- Backend: `fastapi` 0.135.3 → 0.136.1, `strands-agents` 1.34.1 → 1.37.0, `bedrock-agentcore` 1.6.0 → 1.6.4
- Frontend: (see RELEASE_NOTES.md for full table)
- CI: `github/codeql-action` 4.35.1 → 4.35.2

### 🏗️ Infrastructure
- New `CfnWorkloadIdentity` (`<projectPrefix>-platform-workload`) shared between app-api and inference-api (#241)
- SSM parameters added under `/<projectPrefix>/oauth/platform-workload-identity-{name,arn}`

### 🔧 CI/CD
- E2E pipeline added with dynamic CloudFront URL discovery and Cognito user provisioning (#255)
```

### Changelog style rules

- One line per change. If you need more than one line, the change belongs as a spotlight in `RELEASE_NOTES.md` with only a pointer here.
- Reference PRs with `(#NNN)` when known. If the PR number isn't available at authoring time, omit — don't invent.
- Keep breaking changes prominent: prefix with `**Breaking:**` and include migration steps inline or link to the release notes section.
- Dependency sections can collapse minor bumps into a single line per component; the full table lives in `RELEASE_NOTES.md`.
- Omit categories that have no entries for the release — don't render empty headings.

---

## Keeping the Two Documents in Sync

1. Do Steps 1-3 once and build a master bullet list of every change.
2. Categorize (Step 4) and filter (Step 5).
3. Write `CHANGELOG.md` first — it's the factual log.
4. Write `RELEASE_NOTES.md` next, promoting the largest categorized items into narrative spotlights and leaving everything else as per-category bullets.
5. Cross-check: every `CHANGELOG.md` line should map to something in `RELEASE_NOTES.md` (spotlight, bullet, or table row). Exceptions are the minor-dependency and internal-test-addition lines that legitimately live only in the changelog.

---

## Common Pitfalls

- **Don't trust commit messages blindly.** A commit titled "fix: update models" might contain a new feature with 800 lines of code. Always check the diff.
- **Don't miss Dependabot PRs.** They often bump 10+ packages in a single grouped PR. Check `pyproject.toml`, `package.json`, and workflow files for version changes.
- **Don't forget CI/CD changes.** Workflow file modifications (`.github/workflows/`) are easy to overlook but important for operators.
- **Don't duplicate narrative across categories.** If a feature spans backend + frontend + infra, keep it in one spotlight with subsections.
- **Don't let the changelog drift from the release notes.** Every entry in one should be traceable to the other (allowing for the minor-dependency exception).
- **Check the VERSION file and README badge.** These should already be updated via `sync-version.sh` before the release notes are finalized.
- **Don't invent PR numbers.** If you can't confirm a PR link, leave it out.
