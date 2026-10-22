#!/usr/bin/env bash
#============================================================
# recover-evicted-deploy.sh — detect a deploy run that GitHub
# silently evicted from the shared "deploy-<ref>" concurrency
# group, and re-dispatch it.
#
# Usage:
#   recover-evicted-deploy.sh <peer-workflow-file>
#
#   from backend.yml   ->  recover-evicted-deploy.sh platform.yml
#   from platform.yml  ->  recover-evicted-deploy.sh backend.yml
#
# WHY THIS EXISTS
# ---------------
# platform.yml and backend.yml share `concurrency: deploy-<ref>`
# so a CFN deploy and the API-driven code deploys never mutate
# the same ECS service / AgentCore Runtime / Lambda at once.
# But a GitHub concurrency group is a latch with a queue depth
# of ONE, not a queue:
#
#   "By default, any existing pending job or workflow in the
#    same concurrency group will be canceled and the new queued
#    job or workflow will take its place."
#
# So when a third run enters an occupied group, the run that was
# waiting is cancelled BEFORE IT EVER STARTS A JOB. One merge is
# enough to trigger it: a commit touching both infrastructure/
# and backend/ starts two runs that compete with each other and
# with the previous merge's still-running deploy.
#
# Eviction is HARMLESS when the evicting run supersedes the
# evicted one — same workflow, same branch, later SHA — because
# both deploy "the tree at HEAD" and git is cumulative. Eviction
# is DESTRUCTIVE across workflows: a Backend Deploy does not run
# `cdk deploy`, so when it evicts a pending Platform Stack run
# the CDK change is simply never applied. Nothing surfaces it —
# the PR's checks are green and `gh run list` reports
# `conclusion=cancelled`, which reads exactly like a human
# pressing Cancel.
#
# This script closes that gap from inside the run that did the
# evicting: it looks for peer runs that ended `cancelled` having
# executed ZERO jobs (the tell for an eviction — a human cancel
# of a running deploy always leaves at least one job behind),
# ignores the ones a later peer run already covers, and
# re-dispatches the peer workflow for the ones nothing covers.
#
# The re-dispatch targets the branch, not the dropped SHA: these
# deploys are convergent, so deploying current HEAD applies every
# change that was dropped along the way.
#
# Requires: gh + jq (both preinstalled on GitHub runners), a
# GH_TOKEN carrying actions:write, and the standard
# GITHUB_REPOSITORY / GITHUB_REF_NAME environment.
#============================================================
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <peer-workflow-file>" >&2
    echo "  e.g. $0 platform.yml" >&2
    exit 1
fi

PEER_WORKFLOW="$1"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
BRANCH="${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"

# Only look at recent history. On first rollout this keeps the
# script from trying to "recover" months of already-superseded
# evictions, and in steady state an eviction is always minutes old.
LOOKBACK_SECONDS="${EVICTION_LOOKBACK_SECONDS:-21600}"   # 6 hours

# Ping-pong brake. A re-dispatched run joins the same group and can
# itself be evicted by the next merge; the next merge's guard then
# recovers it, so this converges on its own once merges stop. The
# brake bounds how much noise a long merge train can generate before
# a human is asked to look.
REDISPATCH_WINDOW_SECONDS="${EVICTION_REDISPATCH_WINDOW_SECONDS:-1800}"  # 30 min
REDISPATCH_MAX="${EVICTION_REDISPATCH_MAX:-3}"

NOW_EPOCH="$(date -u +%s)"
CUTOFF_EPOCH=$(( NOW_EPOCH - LOOKBACK_SECONDS ))
REDISPATCH_CUTOFF_EPOCH=$(( NOW_EPOCH - REDISPATCH_WINDOW_SECONDS ))

summary() {
    # $GITHUB_STEP_SUMMARY is absent when running locally.
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        echo "$1" >> "$GITHUB_STEP_SUMMARY"
    fi
}

echo "Checking ${PEER_WORKFLOW} on ${BRANCH} for concurrency-group evictions..."

# A transient API failure must not fail a deploy — warn and move on.
if ! RUNS_JSON="$(gh api -X GET \
        "repos/${REPO}/actions/workflows/${PEER_WORKFLOW}/runs" \
        -f branch="${BRANCH}" -F per_page=50 2>/dev/null)"; then
    echo "::warning::Could not list ${PEER_WORKFLOW} runs; skipping eviction check."
    exit 0
fi

# Candidates: cancelled, inside the lookback window, and not already
# covered by a LATER peer run on the same branch. A later run counts as
# cover if it is still live (it is about to do the work) or if it
# completed as anything other than `cancelled` — a later failure is a
# loud problem, not a silent drop, and re-dispatching would only add
# noise. A later run that was itself cancelled is not cover; it shows up
# as its own candidate, and re-dispatching the newest one covers them all.
CANDIDATES="$(jq -r --argjson cutoff "$CUTOFF_EPOCH" '
    [ .workflow_runs[]
      | { id, sha: .head_sha, status, conclusion, event,
          created: .created_at,
          epoch: (.created_at | fromdateiso8601) } ] as $runs
    | $runs
    | map(select(.conclusion == "cancelled" and .epoch >= $cutoff))
    | map(. as $e | select(
        $runs | any(
            .epoch > $e.epoch
            and (.status != "completed" or .conclusion != "cancelled")
        ) | not ))
    | sort_by(.epoch)
    | reverse
    | .[] | "\(.id)\t\(.sha)\t\(.created)"
' <<<"$RUNS_JSON")"

if [[ -z "$CANDIDATES" ]]; then
    echo "OK — no uncovered ${PEER_WORKFLOW} evictions on ${BRANCH}."
    exit 0
fi

# Confirm each candidate really was evicted rather than cancelled by a
# person. An evicted run never starts a job, so its job count is zero.
EVICTED=""
while IFS=$'\t' read -r run_id sha created; do
    [[ -z "$run_id" ]] && continue
    if ! job_count="$(gh api "repos/${REPO}/actions/runs/${run_id}/jobs" \
            --jq '.total_count' 2>/dev/null)"; then
        echo "::warning::Could not read jobs for run ${run_id}; treating as a human cancel."
        continue
    fi
    if [[ "$job_count" -eq 0 ]]; then
        echo "  evicted: run ${run_id} (${sha:0:8}, ${created}) ran 0 jobs"
        EVICTED+="${run_id}"$'\t'"${sha}"$'\t'"${created}"$'\n'
    else
        echo "  skipping: run ${run_id} (${sha:0:8}) ran ${job_count} job(s) — a real cancel, not an eviction"
    fi
done <<<"$CANDIDATES"

if [[ -z "$EVICTED" ]]; then
    echo "OK — every cancelled ${PEER_WORKFLOW} run on ${BRANCH} executed jobs; nothing was dropped."
    exit 0
fi

EVICTED_COUNT="$(grep -c . <<<"$EVICTED")"

# Brake: too many recent re-dispatches means the group is thrashing and a
# human should look rather than have the guard keep firing.
RECENT_DISPATCHES="$(jq -r --argjson cutoff "$REDISPATCH_CUTOFF_EPOCH" '
    [ .workflow_runs[]
      | select(.event == "workflow_dispatch"
               and (.created_at | fromdateiso8601) >= $cutoff) ]
    | length
' <<<"$RUNS_JSON")"

{
    echo "### ⚠️ ${PEER_WORKFLOW} was evicted from the deploy concurrency group"
    echo
    echo "\`${EVICTED_COUNT}\` run(s) of \`${PEER_WORKFLOW}\` on \`${BRANCH}\` were cancelled"
    echo "before executing any job — GitHub dropped them from the shared"
    echo "\`deploy-${BRANCH}\` group when this run took the pending slot."
    echo
    echo "| Dropped run | Commit | Queued at |"
    echo "|---|---|---|"
    while IFS=$'\t' read -r run_id sha created; do
        [[ -z "$run_id" ]] && continue
        echo "| [\`${run_id}\`](https://github.com/${REPO}/actions/runs/${run_id}) | \`${sha:0:8}\` | ${created} |"
    done <<<"$EVICTED"
    echo
} | { while IFS= read -r l; do summary "$l"; done; }

if [[ "$RECENT_DISPATCHES" -ge "$REDISPATCH_MAX" ]]; then
    echo "::error::${PEER_WORKFLOW} was evicted from the deploy group on ${BRANCH}, but ${RECENT_DISPATCHES} re-dispatches already fired in the last $((REDISPATCH_WINDOW_SECONDS / 60)) minutes. Not re-dispatching again. Run it manually once the branch is quiet: gh workflow run ${PEER_WORKFLOW} --ref ${BRANCH}"
    summary "**Not re-dispatched** — ${RECENT_DISPATCHES} re-dispatches already fired in the last $((REDISPATCH_WINDOW_SECONDS / 60)) minutes. Deploy manually once \`${BRANCH}\` is quiet:"
    summary '```'
    summary "gh workflow run ${PEER_WORKFLOW} --ref ${BRANCH}"
    summary '```'
    exit 1
fi

# One dispatch covers every dropped SHA: it deploys current HEAD, and
# these deploys are convergent.
echo "Re-dispatching ${PEER_WORKFLOW} on ${BRANCH}..."
if gh workflow run "${PEER_WORKFLOW}" --ref "${BRANCH}"; then
    echo "::warning::${PEER_WORKFLOW} was silently evicted from the deploy concurrency group on ${BRANCH} (${EVICTED_COUNT} run(s), 0 jobs executed). It has been re-dispatched against ${BRANCH} HEAD."
    summary "**Recovered** — \`${PEER_WORKFLOW}\` has been re-dispatched against \`${BRANCH}\` HEAD."
    summary "It will queue behind this run and deploy every change that was dropped."
else
    echo "::error::${PEER_WORKFLOW} was silently evicted from the deploy concurrency group on ${BRANCH} and could NOT be re-dispatched (the job needs actions:write). Deploy it manually: gh workflow run ${PEER_WORKFLOW} --ref ${BRANCH}"
    summary "**Re-dispatch FAILED** (the guard job needs \`actions: write\`). Deploy manually:"
    summary '```'
    summary "gh workflow run ${PEER_WORKFLOW} --ref ${BRANCH}"
    summary '```'
    exit 1
fi
