# `RELEASE_NOTES.md` — structure & spotlight template

Narrative, benefit-first release notes for product owners, operators, and the
developers who deploy this system. Detailed and technical, but always lead with
the outcome. The new release goes at the **top**; never modify previous entries.

> **Patch releases use the short form.** The full section order and feature-
> spotlight template below describe a **feature/minor or major** release. For a
> patch, keep only the **Header**, a short **Highlights** paragraph, the relevant
> **per-category bullets**, **Feature flags**, **Manual steps** and
> **Deployment notes** — omit feature spotlights, per-layer subsections, and the
> test-coverage section. The last three are never omitted; each may read "none".

## Header

```markdown
# Release Notes — vX.Y.Z

**Release Date:** <Month Day, Year>
**Previous Release:** vX.Y.(Z-1) (<date>)

---
```

## Section order (feature / minor / major)

1. **Highlights** — 3–5 sentence standalone summary. Someone reading only this
   paragraph should grasp the release theme, the 2–3 biggest features, and whether
   any action is required.
2. **Feature spotlights** — one `##` per major feature, with backend / frontend /
   infrastructure / test-coverage subsections. Narrative depth belongs here.
3. **🐛 Bug fixes** — lead with the user-visible symptom, then the root cause.
4. **🔒 Security** — CVEs, CodeQL findings, auth hardening.
5. **⚡ Performance** — measurable improvements only.
6. **⚠️ Breaking changes** — migration steps required. Omit if none.
7. **🏗️ Infrastructure** — new resources, SSM params, IAM changes operators must know.
8. **🔧 CI/CD** — workflow/pipeline changes, GitHub Actions upgrade table.
9. **📦 Dependencies** — markdown table with From/To columns, grouped by component.
10. **🧪 Test coverage** — line counts + scope for notable additions (optional).
11. **🚩 Feature flags** — every flag this release **adds**, **changes the default
    of**, or **retires**, as a table (template below). Always include; "No new
    flags, no default changes" is a valid section. CI (`new-feature-flags.yml`)
    fails the release PR when an added flag is not named here.
12. **🛠️ Manual steps** — every step a deploy does not do by itself: a backfill
    script, a bootstrap re-seed, a GitHub environment variable to set, an admin
    action. Each with the exact command, the environments, and when in the
    window to run it (template below). Always include; "None" is a valid
    section. CI (`pending-backfills.yml`) fails the release PR when an added
    `backfill_*.py` is not named here.
13. **🚀 Deployment notes** — deploy order and what operators must do
    differently. Always include, even if the answer is "no special steps."
    Point at Feature flags and Manual steps rather than repeating them.

## Feature spotlight template

```markdown
## <Feature Name>

<1–2 sentence user-facing summary: what changed and why it matters.>

### Backend
- <file / module> — <what changed>

### Frontend
- <component> — <what changed>

### Infrastructure
- <CDK stack / resource> — <what changed>

### Test Coverage
<N>+ lines of new tests covering <scope>.
```

## Feature flags template

One row per flag. Copy the default, the services and the kind from the reader's
docstring in `feature_flags.py` and from `infrastructure/lib/config.ts`; do not
guess. A two-sided flag (backend + SPA) is one row naming both.

```markdown
## 🚩 Feature flags

| Flag | Default | Set on | Kind | What on does |
|---|---|---|---|---|
| `SESSION_REWIND_ENABLED` / `CDK_SESSION_REWIND_ENABLED` / SPA `features.sessionRewind` | **OFF** (in development; only `"true"` enables) | app-api and the AgentCore Runtime (written to the Runtime only while on); set both sides the same | Feature switch | "Rewind to here" on a user message hides it and everything after it and puts its text back in the composer |

**Default changes:** <flag> went from OFF to ON (now `=false` opts out) — or "none".
**Retired:** <flag> (its off path is gone; a deployment that set it `=false` loses that opt-out) — or "none".
**Still opt-in, on in our prod** (#1798): `sessionRewind`, `sessionFork` (promote after two weeks in prod); `sessionPrewarm` (waits on the V2 readout); `attachmentKb`, `CONVERSATION_INDEX_ENABLED` (a cost decision each deployment makes) — or "none".
```

The **Still opt-in** line is required whenever any flag is on in prod but off by
default, even in a release that touched none of them: the gate fails the release
PR unless the newest entry names each SPA flag in that state. Write what
happens to each one this release — promoted (then it moves to **Default
changes**), or why it stays opt-in.

## Manual steps template

Name the script, the interpreter, the command, the environments and the
verification. "A backfill is required" is the note that sends someone digging
through `git log`.

````markdown
## 🛠️ Manual steps

### Backfill: <what it populates>

**Run after this deploy, on every environment with <the data>. Idempotent; dry-run by default.**

```bash
AWS_PROFILE=<env> backend/.venv/bin/python backend/scripts/backfill_<name>.py \
    --table <prefix>-<table> --region us-west-2            # dry-run: read it first
AWS_PROFILE=<env> backend/.venv/bin/python backend/scripts/backfill_<name>.py \
    --table <prefix>-<table> --region us-west-2 --apply
```

Verify with a live `query … --select COUNT` on the index, never `describe-table` `ItemCount` (it refreshes about every six hours).

### Re-seed: <what changed in the seed>

Run the `bootstrap-data-seeding` workflow (or `scripts/stack-bootstrap/seed.sh`) so <row> picks up <change>. Say what the upsert will and will not overwrite.

### Environment variables to set

`CDK_<X>_ENABLED=true` on the `<env>` GitHub environment, then a `platform.yml` deploy — only if this release expects a deployment to turn something on.
````

## Writing style

- Match the tone and depth of the existing entries — written for developers who
  deploy and maintain this system.
- Every feature section explains **what** changed, **why** it matters, and **how**
  it works at a technical level.
- Use specific file names, endpoint paths, and class names.
- Include line counts for large test additions (e.g. "4,200+ lines of new tests").
- Dependency upgrades use a markdown table with From/To columns.
- Lead with the user/operator outcome; follow with the mechanism.

## Translating technical → user-benefit

| Engineering commit | Release-note framing |
|---|---|
| "Implemented caching layer on tool catalog" | "Tool admin changes now propagate to chat on the next turn (previously required a restart). Backed by a TTL-cached DynamoDB snapshot." |
| "Fixed null pointer in session metadata write" | "Resolved an issue where sessions could accumulate duplicate sidebar entries." |
| "Added OAuth2 USER_FEDERATION flow" | "Users can now connect external MCP tools (Google, Microsoft, GitHub, Canvas) with one-click consent directly from chat." |
| "Refactored OAuth extractor" | *(exclude — no user impact)* |

## Pitfalls

- **Don't modify previous entries** — only prepend the new one.
- **Don't use `{#custom-anchor}` heading suffixes** — GitHub renders them as
  literal text. Prefer plain text over fragile intra-doc anchors.
- **Don't invent PR numbers** — omit if unconfirmed.
- **Don't duplicate narrative across categories** — a feature spanning
  backend+frontend+infra stays in one spotlight with subsections.
