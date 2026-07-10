---
name: cutting-a-release
description: >-
  Cut a new release of this monorepo. Use whenever the user wants to make, cut,
  ship, or prep a release, bump the version, or update RELEASE_NOTES.md /
  CHANGELOG.md — e.g. "let's cut a release", "make a new release", "bump the
  version and update the release notes", "ship the next version". Covers the
  release-branch workflow, the SemVer bump + version sync, identifying what
  changed across the divergent main/develop histories, writing the changelog and
  release notes sized to the release, the squash-merge PR into main, and the
  required backmerge into develop.
---

# Cutting a Release

Single source of truth for cutting a release. Three concerns move together: the
**branch/PR workflow**, the **version bump**, and the **two release documents**
(`RELEASE_NOTES.md` + `CHANGELOG.md`). Do the steps in order, all on one
`release/x.y.z` branch cut from `develop`.

Detailed format templates live in reference files — load them when you reach §5:

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
   (§6). Skipping it leaves the branches divergent and every future release
   conflicts on the release-artifact files.

---

## 1. Branch workflow

```bash
git switch develop
git pull --ff-only origin develop     # release must contain all of develop
git switch -c release/x.y.z           # branch name == version being shipped (no v prefix)
```

The branch name **corresponds to the version being pushed** (`release/1.2.0` ships
`1.2.0`) and carries **all commits currently on `develop`**.

Then, on the release branch: create a task list, determine the bump (§3), bump
`VERSION` + sync (§2), write both docs (§4–§5). Keep every edit on the release
branch — `develop` stays untouched. Commit and push:

```bash
git add VERSION backend/pyproject.toml backend/uv.lock \
        frontend/ai.client/package.json frontend/ai.client/package-lock.json \
        infrastructure/package.json infrastructure/package-lock.json \
        CHANGELOG.md RELEASE_NOTES.md README.md
git commit -m "Release/x.y.z" -m "<one-paragraph summary + bullet notes>"
git push -u origin release/x.y.z
```

Open a PR `release/x.y.z` → `main`. After prerequisites + code review pass, it is
**squash-merged** into `main` (the version-check gate passes because `VERSION`
changed). Then do the backmerge (§6).

---

## 2. Version bump

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

## 3. Decide the bump — SemVer + release sizing

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

## 4. Identify what changed (the divergent-history trap)

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

## 5. Write the two documents

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

## 6. Backmerge `main` → `develop` (required)

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

## 7. Common pitfalls

- **`git log main..develop`** returns all history, not the delta (§4) — use the
  prior release cut point.
- **Trusting commit messages** — read the diff for non-trivial commits.
- **Forgetting lockfiles** — `sync-version.sh` regenerates `uv.lock` + both
  `package-lock.json`; commit them or CI drifts.
- **Squashing the backmerge** — recreates divergence; use a merge commit.
- **Committing straight to `develop`/`main`** — both are PR-only.
- **Padding a patch / starving a feature** — size the notes to the release (§3).
- **Inventing PR numbers or fragile `{#anchor}` links** — omit / use plain text.
- **Skipping the backmerge** — the #1 cause of next-release conflict pain.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
