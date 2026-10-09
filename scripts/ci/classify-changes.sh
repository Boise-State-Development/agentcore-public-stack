#!/usr/bin/env bash
# Map a pull request's changed paths to the test suites that can observe them.
#
# stdin:  one repository-relative path per line (the PR's changed files; a
#         rename is listed under both its old and new name).
# stdout: `key=true|false` lines for $GITHUB_OUTPUT, one per tests.yml suite:
#         backend, backend_contracts, frontend, infra, ios, load, scripts.
#
# Called by the `changes` job in .github/workflows/ci.yml. Before it existed
# the PR gate ran every suite on every pull request, so a two-line docs change
# paid the full backend suite (seven minutes on the runner) for a result that
# could not have changed.
#
# Fail-open by construction:
#   * a path that matches no rule below turns EVERY suite on, so a new
#     top-level directory is never silently untested;
#   * an empty file list (no PR, or the API call failed) does the same;
#   * only an explicit allowlist of paths that no suite reads turns nothing on.
# Which suite reads what is pinned by
# backend/tests/supply_chain/test_ci_path_filter.py — extend both together.
set -euo pipefail

backend=false
backend_contracts=false
frontend=false
infra=false
ios=false
load=false
scripts=false
all=false
seen=0

while IFS= read -r path; do
  [ -z "$path" ] && continue
  seen=$((seen + 1))
  case "$path" in
    # Workflows, composite actions, dependabot and the retention doc are read
    # by the supply-chain suites. scripts/ feeds the frontend coverage job
    # (scripts/frontend/test.sh), the infra jest suite (scripts/release), the
    # backend supply-chain suite (scripts/build) and the restore-data suite.
    # Both change rarely and both reach several suites, so run everything.
    .github/*|scripts/*) all=true ;;
    # backend/scripts holds the backfills that infrastructure/test asserts
    # exist by path (pending-backfills.test.ts).
    backend/scripts/*) backend=true; load=true; infra=true ;;
    # tests/load hand-encodes the SSE event names and the chat payload, so it
    # runs with the backend: a rename in agents/main_agent/streaming should
    # fail there rather than produce a load test that never sees a turn end.
    backend/*) backend=true; load=true ;;
    frontend/*) frontend=true ;;
    # The iOS job runs on a macOS runner (about ten times the per-minute
    # cost of Ubuntu), so it runs only for changes under ios/.
    ios/*) ios=true ;;
    # The backend supply-chain suite reads the CDK tree side by side with the
    # env-var readers; pending-backfills.test.ts reads RELEASE_NOTES.md.
    infrastructure/*|RELEASE_NOTES.md) infra=true; backend_contracts=true ;;
    # gsi-update-limit.test.ts asserts against the cutting-a-release skill.
    .claude/*) infra=true ;;
    # The repo-root supply-chain suite runs inside the backend jobs.
    tests/supply_chain/*) backend_contracts=true ;;
    tests/load/*) load=true ;;
    # No suite reads these. docs-site has its own build on push to main, and
    # the TUI is not wired into CI at all.
    docs/*|docs-site/*|tui/*) ;;
    # Any other directory is unknown to this script: run everything.
    */*) all=true ;;
    # Root files no suite reads (RELEASE_NOTES.md was matched above).
    *.md|*.MD|LICENSE|.gitignore|.editorconfig) ;;
    *) all=true ;;
  esac
done

if [ "$seen" -eq 0 ] || [ "$all" = true ]; then
  backend=true
  backend_contracts=true
  frontend=true
  infra=true
  ios=true
  load=true
  scripts=true
fi

printf 'backend=%s\n' "$backend"
printf 'backend_contracts=%s\n' "$backend_contracts"
printf 'frontend=%s\n' "$frontend"
printf 'infra=%s\n' "$infra"
printf 'ios=%s\n' "$ios"
printf 'load=%s\n' "$load"
printf 'scripts=%s\n' "$scripts"
