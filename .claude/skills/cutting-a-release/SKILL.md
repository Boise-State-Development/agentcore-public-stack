---
name: cutting-a-release
description: >-
  Cut a new release of this monorepo. Use whenever the user wants to make, cut,
  ship, or prep a release, bump the version, or update RELEASE_NOTES.md /
  CHANGELOG.md — e.g. "let's cut a release", "make a new release", "bump the
  version and update the release notes", "ship the next version". Covers the
  release-branch workflow, the pre-merge DynamoDB GSI check, the SemVer bump +
  version sync, identifying what changed across the divergent main/develop
  histories, writing the changelog and release notes sized to the release, the
  squash-merge PR into main, and the required backmerge into develop.
---

# Cutting a Release

Single source of truth for cutting a release. Three concerns move together: the
**branch/PR workflow**, the **version bump**, and the **two release documents**
(`RELEASE_NOTES.md` + `CHANGELOG.md`). Do the steps in order, all on one
`release/x.y.z` branch cut from `develop`.

Detailed format templates live in reference files — load them when you reach §6:

- Release-notes structure, section order, and spotlight template →
  [references/release-notes-format.md](references/release-notes-format.md)
- Changelog structure and per-release entry skeleton →
  [references/changelog-format.md](references/changelog-format.md)

---

## 0. Shape of a release

```
develop (PR-only)                     main (PR-only, squash)
   │  1. pull latest develop           │
   │  2. cut release/x.y.z ────────────┐
   │       bump VERSION + sync         │
   │       write CHANGELOG + NOTES     │
   │       commit + push               │
   │            3. PR release/x.y.z ──▶ main   (squash-merge after review)
   │  4. backmerge ◀────────────────────┤  (branch off develop, merge main in,
   │       (PR to develop, MERGE COMMIT)│   PR into develop, MERGE COMMIT)
   ▼                                    ▼
```

Two hard rules of this repo's branch model:

1. **Never commit directly to `develop` or `main`.** Both are PR-only — the
   release bump and the backmerge each land through a branch + PR.
2. **After the squash-merge into `main`, you MUST backmerge `main` → `develop`**
   (§7). Skipping it leaves the branches divergent and every future release
   conflicts on the release-artifact files.

---

## 1. Pre-merge prerequisite — the DynamoDB GSI limit

**Ask before opening the release PR: does any table that ALREADY EXISTS in
production gain more than one Global Secondary Index in this release?**

If yes, the release **must be split into two deploys** — ship one index, wait for
it to report `ACTIVE`, then ship the next. That split has to happen **before the
merge**, not after a rollback.

### Why one is the limit

DynamoDB's `UpdateTable` API permits **exactly one GSI creation or deletion per
call**. CloudFormation issues one `UpdateTable` per changed table, so a template
that adds two indexes to an existing table fails with:

```
Cannot perform more than one GSI creation or deletion in a single update
```

The limit applies to **`UpdateTable` only**. `CreateTable` accepts any number of
indexes, so **a brand-new table with five GSIs is fine** — which is exactly why
the question is "does an *existing* table gain more than one?" and not "are there
new indexes?".

The blast radius is the whole stack, not the table: when the update fails,
CloudFormation rolls back **every other resource in the deploy** with it.
Meanwhile `backend.yml` and `frontend-deploy.yml` are separate workflows that
succeed on their own, so the release ends up running **new application code
against old infrastructure**.

### Why an incremental environment cannot reveal this

`dev` deploys on every merge into `develop`. Two indexes that arrive in two
separate PRs therefore get **two separate platform deploys**, one index each —
both succeed, and dev looks completely healthy.

`main` only ever sees the *aggregate*. A release collapses a whole batch of
merges into a single CloudFormation update, so the two indexes that dev applied
one at a time arrive at prod **in the same `UpdateTable` call**. Only an
environment that jumps a whole release at once is exposed, which means **no
amount of dev soak time will surface it** and passing CI proves nothing here.
Reviewing the release diff is the only place it can be caught.

> **This is what took production down on 2026-08-01.** Release 1.12.0 (#814)
> added `AgentDirectoryIndex` and `AgentReportsIndex` to the existing
> `{prefix}-rag-assistants` table in one update. They had reached `develop` in
> separate merges (`02d0f2e9`, `a128831d`) and dev had deployed each one
> cleanly. The prod update failed, rolled back the rest of the deploy including a
> brand-new audit-log table, and left the agent store returning 500 against
> already-deployed new code. Recovery took two more patch releases. The 1.12.0
> release notes *did* flag GSI backfill timing — but not the per-update limit.

### How to check

CI enforces this on every PR into `main` (`.github/workflows/gsi-update-limit.yml`).
To check locally before you open the PR:

```bash
node scripts/release/check-gsi-update-limit.mjs
```

It diffs `infrastructure/gsi-inventory.json` — a committed, synth-generated
inventory of every DynamoDB table and its indexes — between `origin/main` and
your branch, and fails when a table present in **both** needs more than one index
operation. Tables that appear only on your branch are exempt (`CreateTable`), as
are tables dropped entirely (`DeleteTable`).

Note it counts **creations and deletions together**, because the API limit is one
GSI *operation* per update: renaming an index — one add plus one drop — fails
exactly like adding two does.

The inventory is regenerated and verified by the infrastructure test suite, so a
GSI added in code without the inventory updated fails `npx jest` first:

```bash
cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit
```

> The check reports **SKIPPED** when `main` has no inventory to diff against —
> true only until the first release carrying `gsi-inventory.json` lands. For that
> one release, answer the question by reading the diff yourself.

**The automation does not replace the question.** It compares committed
inventories, so it only sees what `git` sees. Read the `gsi-inventory.json` diff
in the release PR: one added line under an existing table is fine, two is a split.

### If the answer is yes — split the release

Ship the indexes in separate releases, each merged and deployed on its own:

1. Release N carries **one** of the new indexes. Merge, deploy, and confirm the
   index reports `ACTIVE` before continuing — CloudFormation reporting
   `UPDATE_COMPLETE` is **not** the same as the index being usable:

   ```bash
   aws dynamodb describe-table --table-name <prefix>-rag-assistants \
     --query 'Table.GlobalSecondaryIndexes[].{Name:IndexName,Status:IndexStatus}'
   ```

2. Release N+1 adds the second index, once the first is `ACTIVE`.

Prefer doing this **on the release branch before the merge**: drop the second
index from the release branch, ship it, then restore it in the next release.

### Recovery, if it has already failed

Each half needs its **own patch release** — there is no way to ship them as one
PR and no way to skip the version bump. `.github/workflows/version-check.yml`
fails any PR into `main` whose `VERSION` is unchanged, and it has **no label,
flag, or skip path**. So a failed two-index deploy costs two patch releases
(1.12.0 → 1.12.1 added the first index alone, 1.12.2 restored the second).

> **Do NOT stack PR 2 on PR 1.** After PR 1 squash-merges into `main`, the
> merge-base for a branch cut from PR 1 does not move. PR 1's removal and PR 2's
> restoration then **cancel out in the three-dot diff GitHub shows and merges** —
> the PR looks correct, CI passes, and the restore silently no-ops. The index
> never comes back.
>
> Open PR 2 **only after PR 1 has merged**, branched fresh from `main`, as a
> `git revert` of PR 1's squash commit:
>
> ```bash
> git fetch origin main
> git switch -c release/x.y.z origin/main
> git revert --no-commit <pr-1-squash-sha>   # restores the second index
> ```

---

## 2. Branch workflow

```bash
git switch develop
git pull --ff-only origin develop     # release must contain all of develop
git switch -c release/x.y.z           # branch name == version being shipped (no v prefix)
```

The branch name **corresponds to the version being pushed** (`release/1.2.0` ships
`1.2.0`) and carries **all commits currently on `develop`**.

Then, on the release branch: create a task list, determine the bump (§4), bump
`VERSION` + sync (§3), write both docs (§5–§6). Keep every edit on the release
branch — `develop` stays untouched. Commit and push:

```bash
git add VERSION backend/pyproject.toml backend/uv.lock \
        frontend/ai.client/package.json frontend/ai.client/package-lock.json \
        infrastructure/package.json infrastructure/package-lock.json \
        CHANGELOG.md RELEASE_NOTES.md README.md
git commit -m "Release/x.y.z" -m "<one-paragraph summary + bullet notes>"
git push -u origin release/x.y.z
```

Open a PR `release/x.y.z` → `main`. After prerequisites (§1) + code review pass, it is
**squash-merged** into `main` (the version-check gate passes because `VERSION`
changed). Then do the backmerge (§7).

---

## 3. Version bump

Single `VERSION` file at the repo root is the source of truth. Format:
`MAJOR.MINOR.PATCH[-PRERELEASE]` (SemVer 2.0), no `v` prefix.

1. Edit `VERSION`.
2. `bash scripts/common/sync-version.sh`
3. `bash scripts/common/sync-version.sh --check` → must print `[PASS]`.
4. Commit `VERSION` **and every file the sync touched, including lockfiles**.

The sync script propagates the version into `backend/pyproject.toml`,
`frontend/ai.client/package.json`, `infrastructure/package.json`, the `README.md`
badge + "Current release" line, and **regenerates** `backend/uv.lock` and both
`package-lock.json`. Run it inside the dev container (needs `uv` + `npm` at pinned
versions).

- **PR gate:** PRs to `main` fail CI if `VERSION` is unchanged vs `main` or the
  manifests are out of sync.
- **CI handles the rest:** Docker image tags, AWS resource tags, health-endpoint
  versions, frontend version display, and the `vX.Y.Z` git tag. Just bump + sync.

---

## 4. Decide the bump — SemVer + release sizing

Pick the tier from what shipped, then size the write-up to match.

| Bump | When | Notes depth |
|---|---|---|
| **Patch** `x.y.Z` | Bug fixes, security/dep bumps, CI/CD, docs, internal refactors — no new user-facing capability | **Brief.** 2–4 sentence Highlights + compact per-category bullets + one deployment note. No spotlights, no per-layer subsections. |
| **Minor** `x.Y.0` | New features, endpoints, pages, capabilities (even if flag-gated/preview) | **Deep.** Highlights + one spotlight per major feature (backend/frontend/infra/tests) + per-category bullets. |
| **Major** `X.0.0` | Breaking changes, architecture shifts, migrations | **Deepest.** Above + prominent migration/upgrade section and breaking-change callouts. |

Rules of thumb: don't pad a patch; don't starve a feature; size to the largest
single change; turning a dark feature **on by default** or completing a preview
capability counts as user-facing → **minor**.

---

## 5. Identify what changed (the divergent-history trap)

`develop` is squash-merged → `main`, so **`main` and `develop` have divergent
histories**; a squash collapses a whole release into one commit on `main` whose
SHA matches nothing on `develop`.

> **Do NOT use `git log main..develop`.** Even after a backmerge makes `main` an
> ancestor of `develop`, main's *squashed* history means develop's granular
> commits are never reachable from main — so `main..develop` returns the entire
> project history, not the release delta.

Use the previous release's cut point (or tag) as the boundary:

```bash
git tag --list 'v*' --sort=-creatordate | head
git log <prev-release-cut-or-tag>..develop --no-merges --reverse --format='%h %s'
git log <prev-release-cut-or-tag>..develop --merges --format='%s'   # PR numbers
```

Exclude commits a prior **backmerge** dragged in (the previous `Release/x.y.z`
squash commit, direct-to-main hotfixes) — they aren't new work. For every
non-trivial commit **read the diff, not the message** (`git show --stat <sha>`).
Bucket each change:

| Category | Emoji | Use for |
|---|---|---|
| New | 🚀 | New features, endpoints, pages, capabilities |
| Improved | ✨ | Enhancements to existing features |
| Fixed | 🐛 | Bug fixes |
| Changed | ⚠️ | Breaking changes, removals, deprecations, migrations |
| Security | 🔒 | CVE patches, CodeQL fixes, auth hardening |
| Performance | ⚡ | Latency/throughput/cost wins users notice |
| Infrastructure | 🏗️ | CDK/IaC changes, new AWS resources, deploy-order changes |
| Dependencies | 📦 | Package upgrades (grouped in a table) |
| CI/CD | 🔧 | Workflow/pipeline/tooling changes |
| Docs | 📚 | Documentation worth calling out |

**Include** user/operator-facing changes, bug fixes people hit, security updates,
breaking changes, dependency upgrades. **Changelog-only:** minor dep bumps with no
behavior change, internal test additions. **Exclude from both:** pure internal
refactors, typo/comment/formatter churn.

---

## 6. Write the two documents

Same pass, same categorized list. Write `CHANGELOG.md` first (factual log), then
`RELEASE_NOTES.md` (narrative). Cross-check every changelog line maps to something
in the notes (except the changelog-only items above).

- **`CHANGELOG.md`** — [Keep a Changelog], terse, one line per change, PR-linked
  `(#NNN)` when known, grouped by category emoji. New version at top; omit empty
  categories; never invent PR numbers. Full skeleton →
  [references/changelog-format.md](references/changelog-format.md).
- **`RELEASE_NOTES.md`** — narrative, benefit-first with technical depth. New
  release at top; **never modify previous entries.** Lead each item with the
  user/operator outcome, then the mechanism (files, endpoints, classes). Section
  order + spotlight template → [references/release-notes-format.md](references/release-notes-format.md).

**Translate technical → benefit.** "Implemented tool-catalog caching" → "Tool
admin changes now propagate to chat on the next turn (was: required a restart).
Backed by a TTL-cached DynamoDB snapshot."

**Link hygiene:** GitHub renders `{#custom-anchor}` heading suffixes literally —
don't use them; prefer plain text over fragile intra-doc anchors.

**Before finalizing:** `sync-version.sh --check` passes, and both docs lead with
the new version above the untouched previous entries.

---

## 7. Backmerge `main` → `develop` (required)

After the release PR squash-merges into `main`, reconcile the branches — PR-based,
you cannot push directly to `develop`.

```bash
git fetch origin main develop
git switch develop && git merge --ff-only origin/develop
git switch -c backmerge/main-into-develop-x.y.z
git merge --no-commit --no-ff origin/main               # preview conflicts
```

If there are conflicts they're only the release-artifact files (`VERSION`,
`CHANGELOG.md`, `RELEASE_NOTES.md`, `README.md`, manifests, lockfiles) — resolve
in **main's favor** (main holds the canonical post-release state; develop's copy
is pre-release plus any stale `[Unreleased]`-style content that has now shipped):

```bash
git checkout --theirs -- VERSION CHANGELOG.md RELEASE_NOTES.md README.md \
  backend/pyproject.toml backend/uv.lock \
  frontend/ai.client/package.json frontend/ai.client/package-lock.json \
  infrastructure/package.json infrastructure/package-lock.json
git add -A
bash scripts/common/sync-version.sh --check     # must PASS
git commit --no-edit
```

Verify `git merge-base --is-ancestor origin/main HEAD` succeeds, push, and open a
PR into `develop`.

> **Merge the backmerge PR with a MERGE COMMIT, not a squash** — the point is to
> make `main` a genuine ancestor of `develop`. Squashing recreates the divergence
> and the conflicts return next release. (Feature branches into `develop` squash
> as usual; the backmerge is the deliberate exception.)

A clean auto-merge happens when `develop` never touched the release artifacts
since the last reconciliation and `main` only *added* on top. Verify the result
anyway (VERSION + both docs correct, feature code intact) before committing.

---

## 8. Common pitfalls

- **Adding two GSIs to an existing table in one release** — `UpdateTable` allows
  one per call, and dev's per-merge deploys never show it (§1). Split the release.
- **Stacking the second GSI PR on the first** — the three-dot diff cancels the two
  commits and the restore silently no-ops (§1). Revert off `main` instead.
- **`git log main..develop`** returns all history, not the delta (§5) — use the
  prior release cut point.
- **Trusting commit messages** — read the diff for non-trivial commits.
- **Forgetting lockfiles** — `sync-version.sh` regenerates `uv.lock` + both
  `package-lock.json`; commit them or CI drifts.
- **Squashing the backmerge** — recreates divergence; use a merge commit.
- **Committing straight to `develop`/`main`** — both are PR-only.
- **Padding a patch / starving a feature** — size the notes to the release (§4).
- **Inventing PR numbers or fragile `{#anchor}` links** — omit / use plain text.
- **Skipping the backmerge** — the #1 cause of next-release conflict pain.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
