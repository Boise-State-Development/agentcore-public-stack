---
title: Testing
description: What each test suite covers, how to run it locally, and what runs on a pull request.
sidebar:
  order: 2
---

## Suites

| Suite | Where | Run locally |
|---|---|---|
| Backend (pytest, ~11k tests) | `backend/tests/` | `cd backend && uv run pytest tests/ -n logical` |
| Root supply-chain contracts | `tests/supply_chain/` | `uv run --project backend --no-sync pytest tests/supply_chain/` |
| Frontend (Vitest via Analog) | `frontend/ai.client/src/**/*.spec.ts` | `cd frontend/ai.client && npm test` |
| Infrastructure (jest) | `infrastructure/test/` | `cd infrastructure && npx jest` |
| Load-test unit tests | `tests/load/tests/` | `cd tests/load && uv run pytest tests/` |
| Operational scripts | `scripts/restore-data/test_restore.py` | `cd scripts/restore-data && uv run --frozen pytest test_restore.py` |

The backend suite is hermetic: a conftest guard fails any test that opens a socket to a non-local host, so a partially mocked test cannot quietly reach AWS. Run it with `-n logical` rather than `-n auto`; with psutil installed, `auto` counts physical cores and starts half as many workers as the machine has.

## What runs on a pull request

`.github/workflows/ci.yml` runs on every pull request into `develop` or `main`, but it does not run every suite every time. A `changes` job reads the PR's file list and maps each path to the suites that can observe it (`scripts/ci/classify-changes.sh`):

| Changed paths | Suites |
|---|---|
| `docs/`, `docs-site/`, `tui/`, root markdown, `LICENSE` | none |
| `backend/` | backend, load-test unit |
| `backend/scripts/` | backend, load-test unit, infrastructure (jest asserts the backfill scripts exist) |
| `frontend/` | frontend, frontend under `--coverage` |
| `infrastructure/`, `RELEASE_NOTES.md` | infrastructure, plus the backend supply-chain and architecture suites that read the CDK tree |
| `.claude/` | infrastructure (one jest test reads the release skill) |
| `tests/load/` | load-test unit |
| `.github/`, `scripts/`, anything unrecognised | everything |

The mapping is fail-open. A new top-level directory, an empty file list, or a `workflow_dispatch` run turns every suite on; only the explicit list in the first row turns none on. The rules are pinned by `backend/tests/supply_chain/test_ci_path_filter.py`, so extend the script and the test together.

The deploy workflows (`backend.yml`, `platform.yml`, `frontend-deploy.yml`) are unaffected: on push to `develop` and `main` they still run their full suites before deploying, so the PR gate is the first line, not the last.
