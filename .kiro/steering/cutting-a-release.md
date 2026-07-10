---
inclusion: auto
name: cutting-a-release
description: >-
  Cut a new release of this monorepo. Use whenever the user wants to make, cut,
  ship, or prep a release, bump the version, or update RELEASE_NOTES.md /
  CHANGELOG.md — e.g. "let's cut a release", "make a new release", "bump the
  version and update the release notes", "ship the next version". Covers the
  release-branch workflow, the SemVer bump + version sync, identifying what
  changed across the divergent main/develop histories, writing the changelog
  and release notes (sized to the release), the squash-merge PR into main, and
  the required backmerge into develop.
---

# Cutting a Release

This is the single source of truth for cutting a release. It folds together three
concerns that always move together: the **branch/PR workflow**, the **version
bump**, and the **two release documents** (`RELEASE_NOTES.md` + `CHANGELOG.md`).

Do the steps in order. The whole thing is one pass on a dedicated `release/x.y.z`
branch, cut from `develop`.

---

## 0. The shape of a release (read first)

```
develop (PR-only)                     main (PR-only, squash)
   │                                     │
   │  1. pull latest develop             │
   │  2. cut release/x.y.z ──────────────┐
   │       bump VERSION + sync           │
   │       write CHANGELOG + NOTES       │
   │       commit + push                 │
   │                                     │
   │              3. PR release/x.y.z ──▶ main   (squash-merge after review)
   │                                     │
   │  4. backmerge  ◀────────────────────┤  (branch off develop, merge main in,
   │       (PR into develop,             │   PR into develop, MERGE COMMIT)
   │        merge commit — NOT squash)   │
   ▼                                     ▼
```

Two hard rules that this repo's branch model forces:

1. **Never commit directly to `develop` or `main`.** Both are PR-only. Every
   change — including the release bump and the backmerge — lands through a branch
   and a PR.
2. **After the squash-merge into `main`, you MUST backmerge `main` into `develop`.**
   Skipping this leaves the branches divergent and every future release conflicts
   on the release-artifact files. See §6.

---

## 1. Branch workflow

### 1.1 Cut the release branch

```bash
git switch develop
git pull --ff-only origin develop     # release must contain all of develop
git switch -c release/x.y.z           # branch name == the version being shipped
```

- The release branch **corresponds to the version being pushed** (`release/1.2.0`
  ships `1.2.0`). No `v` prefix on the branch.
- It carries **all commits currently on `develop`** — that's the release payload.

### 1.2 Do the work (the rest of this doc)

Create a task list, then on the release branch: determine the bump (§3), bump
`VERSION` + sync (§2), write both release docs (§4–§5). Keep every edit on the
release branch — `develop` stays untouched.

### 1.3 Commit and push

Commit the release artifacts together with a `Release/x.y.z` subject:

```bash
git add VERSION backend/pyproject.toml backend/uv.lock \
        frontend/ai.client/package.json frontend/ai.client/package-lock.json \
        infrastructure/package.json infrastructure/package-lock.json \
        CHANGELOG.md RELEASE_NOTES.md README.md
git commit -m "Release/x.y.z" -m "<one-paragraph summary + bullet notes>"
git push -u origin release/x.y.z
```

### 1.4 PR into `main`

Open a PR from `release/x.y.z` → `main`. Once prerequisites and code review pass,
it is **squash-merged** into `main`. The version-check CI gate (see §2) passes
because `VERSION` changed.

### 1.5 Backmerge into `develop`

Non-negotiable follow-up. See §6.

---

## 2. Version bump

The monorepo uses a single `VERSION` file at the repo root as the source of truth.

- **Format:** `MAJOR.MINOR.PATCH[-PRERELEASE]` (SemVer 2.0), no `v` prefix.
  Examples: `1.0.0-beta.1`, `1.1.0`, `1.2.0`.

**How to bump:**

1. Edit `VERSION` with the new version.
2. Run `bash scripts/common/sync-version.sh`.
3. Verify with `bash scripts/common/sync-version.sh --check` (must print `[PASS]`).
4. Commit `VERSION` **and** every file the sync script touched (including lockfiles).

The sync script propagates `VERSION` into `backend/pyproject.toml`,
`frontend/ai.client/package.json`, `infrastructure/package.json`, the `README.md`
version badge and "Current release" text, and **regenerates the lockfiles**
(`backend/uv.lock`, both `package-lock.json`). All of those must be committed.

> Run the sync inside the dev container (it needs `uv` + `npm` at the project's
> pinned versions). See the dev-environment steering doc for how to exec into it.

**PR gate.** PRs to `main` fail CI if `VERSION` is unchanged vs `main`, or if the
manifests are out of sync (run the sync script to fix).

**CI handles the rest** — Docker image tags, AWS resource tags, health-endpoint
versions, frontend version display, and the `vX.Y.Z` git tag are all automatic.
Just bump `VERSION` and sync.

---

## 3. Decide the bump — SemVer + release sizing

Pick the SemVer tier from what actually shipped, then size the write-up to match.

| Bump | When | Notes depth |
|---|---|---|
| **Patch** `x.y.Z` | Bug fixes, security/dep bumps, CI/CD, docs, internal refactors — no new user-facing capability | **Brief.** 2–4 sentence Highlights + compact per-category bullets + one deployment-note paragraph. No feature spotlights, no per-layer subsections. |
| **Minor** `x.Y.0` | New features, endpoints, pages, or capabilities (even if flag-gated/preview) | **Deep.** Full treatment: Highlights + one spotlight per major feature (backend/frontend/infra/tests subsections) + per-category bullets. |
| **Major** `X.0.0` | Breaking changes, architecture shifts, migrations | **Deepest.** Everything above + a prominent migration/upgrade section and breaking-change callouts. |

Rules of thumb:

- **Don't pad a patch.** Three CI commits and a dep bump = a screen or less.
- **Don't starve a feature.** A real new capability earns a spotlight with the
  what/why/how and file/endpoint/class detail this audience expects.
- **Size to the largest single change.** One real feature among ten chores makes
  it a feature (minor) release for write-up purposes.
- Turning a previously-dark feature **on by default**, or completing a
  previously-preview capability, counts as user-facing → **minor**.

---

## 4. Identify what changed (the divergent-history trap)

This repo squash-merges `develop` → `main`, so **`main` and `develop` have
divergent histories**. A squash collapses a whole release into one commit on
`main` whose SHA matches nothing on `develop`.

> **Do NOT use `git log main..develop`.** Even after a backmerge makes `main` an
> ancestor of `develop`, main's *squashed* history means develop's granular
> commits are never reachable from main — so `main..develop` returns the entire
> project history (thousands of commits), not the release delta. This has bitten
> us every release.

**Use the previous release's cut point as the boundary instead:**

```bash
# The commit develop was at when the LAST release branch was cut,
# or the previous release tag:
git tag --list 'v*' --sort=-creatordate | head
git log <prev-release-cut-or-tag>..develop --no-merges --reverse --format='%h %s'
git log <prev-release-cut-or-tag>..develop --merges --format='%s'   # PR numbers
```

If the boundary range still pulls in commits carried by a prior **backmerge**
(e.g. the previous `Release/x.y.z` squash commit and any direct-to-main hotfixes),
exclude those — they are not new work for this release.

For every non-trivial commit, **read the diff, not just the message** (`git show
--stat <sha>`, `git show --no-patch <sha>`). Messages lie; a "fix: update models"
can hide 800 lines of feature. Bucket each change into the standard categories:

| Category | Emoji | Use for |
|---|---|---|
| New | 🚀 | New features, endpoints, pages, capabilities |
| Improved | ✨ | Enhancements to existing features |
| Fixed | 🐛 | Bug fixes |
| Changed | ⚠️ | Breaking changes, removals, deprecations, migration-required updates |
| Security | 🔒 | CVE patches, CodeQL fixes, auth hardening |
| Performance | ⚡ | Latency/throughput/cost wins users notice |
| Infrastructure | 🏗️ | CDK/IaC changes, new AWS resources, deploy-order changes |
| Dependencies | 📦 | Package upgrades (grouped in a table) |
| CI/CD | 🔧 | Workflow/pipeline/tooling changes |
| Docs | 📚 | Documentation worth calling out |

**Include** user- and operator-facing changes, bug fixes people hit, security
updates, breaking changes/migrations, and dependency upgrades. **Changelog-only:**
minor dep bumps with no behavior change, internal test additions. **Exclude from
both:** pure internal refactors, typo/comment/formatter churn.

---

## 5. Write the two documents

Both files are updated in the **same pass** from the same categorized list.
Write `CHANGELOG.md` first (the factual log), then `RELEASE_NOTES.md` (the
narrative), and cross-check that every changelog line maps to something in the
notes (allowing the changelog-only exceptions above).

- **`CHANGELOG.md`** — [Keep a Changelog] format, terse, one line per change,
  PR-linked `(#NNN)` when known, grouped by the category emojis. New version goes
  at the top; omit empty categories; never invent PR numbers.
- **`RELEASE_NOTES.md`** — narrative, benefit-first with technical depth. New
  release at the top; **do not modify previous entries.** Lead each item with the
  user/operator outcome, then the mechanism (file names, endpoints, class names).

The exact section order, the feature-spotlight template, the header format, and
the changelog per-release skeleton live in the reference files so this doc stays
scannable:

- Release-notes structure & spotlight template → #[[file:.claude/skills/cutting-a-release/references/release-notes-format.md]]
- Changelog structure & entry skeleton → #[[file:.claude/skills/cutting-a-release/references/changelog-format.md]]

**Translate technical → benefit.** "Implemented tool-catalog caching" →
"Tool admin changes now propagate to chat on the next turn (was: required a
restart). Backed by a TTL-cached DynamoDB snapshot."

**Anchor/link hygiene:** GitHub renders `{#custom-anchor}` heading suffixes as
literal text — don't use them. Prefer plain text over fragile intra-doc anchors.

**Before finalizing:** confirm `VERSION`, the README badge, and both docs all
show the new version (`sync-version.sh --check`), and that the new entries sit
above the untouched previous ones.

---

## 6. Backmerge `main` → `develop` (required)

After the release PR is squash-merged into `main`, reconcile the branches. This
is PR-based like everything else — **you cannot push the merge directly to
`develop`.**

```bash
git fetch origin main develop
git switch develop && git merge --ff-only origin/develop     # get current
git switch -c backmerge/main-into-develop-x.y.z
git merge --no-commit --no-ff origin/main                    # preview conflicts
```

**Resolve conflicts in `main`'s favor for the release-artifact files.** The only
conflicts are `VERSION`, `CHANGELOG.md`, `RELEASE_NOTES.md`, `README.md`, the
manifests, and the lockfiles — because both branches touched them. `main` holds
the canonical post-release state (and develop's copy is the pre-release state,
plus any stale `[Unreleased]`-style content that has now shipped):

```bash
git checkout --theirs -- VERSION CHANGELOG.md RELEASE_NOTES.md README.md \
  backend/pyproject.toml backend/uv.lock \
  frontend/ai.client/package.json frontend/ai.client/package-lock.json \
  infrastructure/package.json infrastructure/package-lock.json
git add -A
bash scripts/common/sync-version.sh --check     # must PASS
git commit --no-edit
```

Then verify `git merge-base --is-ancestor origin/main HEAD` prints success (main
is now an ancestor), push the branch, and open a PR into `develop`.

> **Merge the backmerge PR with a MERGE COMMIT, not a squash.** The entire point
> is to make `main` a genuine ancestor of `develop`. Squashing it recreates the
> divergence and the conflicts return next release. (Feature branches into
> `develop` squash as usual; the backmerge is the deliberate exception.)

Some backmerges are conflict-free — if `develop` never touched the release
artifacts since the last reconciliation and `main` only *added* on top, git
auto-merges. Verify the result anyway (VERSION + both docs correct, feature code
intact) before committing.

---

## 7. Common pitfalls

- **`git log main..develop` for the changelog** — returns all history, not the
  delta (see §4). Use the prior release cut point.
- **Trusting commit messages** — always read the diff for non-trivial commits.
- **Forgetting the lockfiles** — `sync-version.sh` regenerates `uv.lock` and both
  `package-lock.json`; they must be committed or CI drifts.
- **Squashing the backmerge** — recreates divergence; use a merge commit.
- **Committing straight to `develop`/`main`** — both are PR-only.
- **Padding a patch / starving a feature** — size the notes to the release (§3).
- **Inventing PR numbers or fragile `{#anchor}` links** — omit if unknown; use
  plain text.
- **Skipping the backmerge** — the #1 cause of next-release conflict pain.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
