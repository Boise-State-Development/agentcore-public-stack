---
inclusion: manual
---

# Version Bumping

This monorepo uses a single `VERSION` file at the repo root as the source of truth.

## Format

`MAJOR.MINOR.PATCH[-PRERELEASE]` (SemVer 2.0). No `v` prefix.
Example: `1.0.0-beta.1`, `1.1.0`

## How to Bump

1. Edit the `VERSION` file with the new version
2. Run `bash scripts/common/sync-version.sh`
3. Commit both the `VERSION` file and the updated manifests

The sync script updates `backend/pyproject.toml`, `frontend/ai.client/package.json`, `infrastructure/package.json`, and regenerates `backend/uv.lock` and `package-lock.json` for both npm projects. All updated files (including lockfiles) must be committed.

## PR Gate

PRs to `main` will fail CI if:
- The `VERSION` file hasn't changed compared to `main`
- Package manifests are out of sync (run the sync script to fix)

## What CI Handles

Docker image tags, AWS resource tags, health endpoint versions, frontend version display, and git tags are all handled automatically by CI. Just bump `VERSION` and sync.
