#!/bin/bash

#============================================================
# Teardown - Delete runtime-created Managed Knowledge Bases
#
# Requirements 20.8, 13.4, 13.5 of .kiro/specs/managed-kb-migration.
#
# Managed knowledge bases are created at RUNTIME by the provisioning
# saga, not by CloudFormation. They are therefore not children of any
# stack and `delete-stack` does not touch them: left alone they survive
# the teardown, keep billing at $5.00/GB-month, and are invisible to
# anyone reading the CloudFormation console.
#
# Ordering is not cosmetic. This runs BEFORE the stacks come down,
# because the Bedrock service role lives in PlatformStack and Bedrock
# needs it to delete a knowledge base. Deleting the role first is a
# plausible route into DELETE_UNSUCCESSFUL, which is a real terminal
# state — the dev account has held a knowledge base stuck in it since
# 2025-11-24 — and it cannot be cleared by retrying.
#
# Scope is by TAG, never by name pattern. Two environments share an
# account, and a name prefix is a convention while a tag is what the
# reconciler and this script both read (Requirement 20.11).
#
# Exits non-zero if any matched knowledge base is not confirmed absent,
# so the caller does not proceed to delete the role out from under it
# (Requirement 13.5).
#
# Usage: bash scripts/teardown/managed-kb.sh
#============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Sourced only when not already loaded, so this script works both
# standalone and when called from destroy.sh (which has already sourced it).
if ! declare -f log_info >/dev/null 2>&1; then
    source "${PROJECT_ROOT}/scripts/common/load-env.sh"
fi

# Deletion was measured at 2-6 minutes and is asynchronous. 480s is the
# same tolerance the Python delete saga uses
# (tombstones.KB_DELETE_POLL_TIMEOUT_SECONDS), so both paths wait the
# same amount before calling a knowledge base stuck.
KB_DELETE_POLL_TIMEOUT_SECONDS="${KB_DELETE_POLL_TIMEOUT_SECONDS:-480}"
KB_DELETE_POLL_INTERVAL_SECONDS="${KB_DELETE_POLL_INTERVAL_SECONDS:-10}"

# ⚠️ Both are clamped, and the interval's floor of 1 is load-bearing.
#
# The wait loop advances a counter by the interval and stops when it reaches the
# timeout. At an interval of 0 the counter never advances, so the timeout is never
# reached and the loop spins forever — issuing an `aws` call and a `python3` parse
# on every iteration, at full CPU. That is not a hypothetical: a test set the
# interval to 0 to run fast and ran for sixteen hours instead.
#
# A non-numeric value falls back to the default rather than erroring, because
# `[ abc -lt 1 ]` fails under `set -e` and would abort a teardown over a typo in
# a tunable.
for _var in KB_DELETE_POLL_TIMEOUT_SECONDS KB_DELETE_POLL_INTERVAL_SECONDS; do
    case "${!_var}" in
        ''|*[!0-9]*)
            if [ "${_var}" = "KB_DELETE_POLL_TIMEOUT_SECONDS" ]; then
                KB_DELETE_POLL_TIMEOUT_SECONDS=480
            else
                KB_DELETE_POLL_INTERVAL_SECONDS=10
            fi
            ;;
    esac
done
[ "${KB_DELETE_POLL_INTERVAL_SECONDS}" -ge 1 ] || KB_DELETE_POLL_INTERVAL_SECONDS=1
[ "${KB_DELETE_POLL_TIMEOUT_SECONDS}" -ge 1 ] || KB_DELETE_POLL_TIMEOUT_SECONDS=1

# Bounds one run's destructive work. A tag filter that suddenly matched
# more than it should costs at most this many knowledge bases before a
# human sees the refusal.
KB_TEARDOWN_MAX="${KB_TEARDOWN_MAX:-500}"

AWS_REGION_ARG="${CDK_AWS_REGION}"
ACCOUNT_ID="${CDK_AWS_ACCOUNT}"
PREFIX="${CDK_PROJECT_PREFIX}"
ENVIRONMENT="${CDK_ENVIRONMENT:-${ENVIRONMENT:-dev}}"

kb_arn() {
    echo "arn:aws:bedrock:${AWS_REGION_ARG}:${ACCOUNT_ID}:knowledge-base/$1"
}

# All knowledge bases in the account, as "<id>\t<name>\t<status>" lines.
# ListKnowledgeBases summaries carry no ARN, so callers build it.
#
# ⚠️ Every step carries an explicit `|| return 1`. `set -e` is suspended for the
# whole dynamic extent of a function called in a condition context — which this
# one always is — so an unchecked failure here does not abort anything: the loop
# simply continues, the parse of an empty response fails silently, and the
# function returns success with no output. A caller then sees an empty account and
# reports a clean teardown. That is the shape this script must never have.
list_knowledge_bases() {
    local token="" response
    while :; do
        if [ -z "${token}" ]; then
            response=$(aws bedrock-agent list-knowledge-bases \
                --region "${AWS_REGION_ARG}" --max-results 100 --output json) || return 1
        else
            response=$(aws bedrock-agent list-knowledge-bases \
                --region "${AWS_REGION_ARG}" --max-results 100 \
                --next-token "${token}" --output json) || return 1
        fi

        printf '%s' "${response}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for summary in data.get("knowledgeBaseSummaries") or []:
    print("\t".join([
        summary.get("knowledgeBaseId", ""),
        summary.get("name", ""),
        summary.get("status", ""),
    ]))
' || return 1

        token=$(printf '%s' "${response}" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("nextToken") or "")
') || return 1
        [ -n "${token}" ] || break
    done
    return 0
}

# 0 if this knowledge base carries BOTH our prefix and our environment tag.
# Requires both: one prefix tag with the wrong env is another environment's
# knowledge base living in the same account.
is_ours() {
    local kb_id="$1"
    local tags
    if ! tags=$(aws bedrock-agent list-tags-for-resource \
        --region "${AWS_REGION_ARG}" \
        --resource-arn "$(kb_arn "${kb_id}")" \
        --output json 2>/dev/null); then
        # Unreadable tags mean unknown ownership, and unknown ownership is
        # not ours. Refusing to delete something we cannot attribute is the
        # only safe direction for a destructive pass.
        return 1
    fi

    echo "${tags}" | PREFIX="${PREFIX}" ENVIRONMENT="${ENVIRONMENT}" python3 -c '
import json, os, sys
tags = (json.load(sys.stdin) or {}).get("tags") or {}
ok = tags.get("prefix") == os.environ["PREFIX"] and tags.get("env") == os.environ["ENVIRONMENT"]
sys.exit(0 if ok else 1)
'
}

# 0 while the knowledge base is still present in ListKnowledgeBases.
#
# ⚠️ No pipeline into `grep -q`, deliberately. `grep -q` exits on its first match
# and closes the pipe, which can SIGPIPE the upstream lister; under
# `set -o pipefail` the pipeline then reports the upstream's 141 rather than
# grep's 0, and this function would answer "absent" about a knowledge base it had
# just found. Whether that happens depends on how much output was already
# written when grep exited — so it is timing-dependent, which is the worst
# possible property for the check that decides whether it is safe to delete the
# service role.
#
# Also fails SAFE: if the account cannot be listed, the answer is "still
# present". Claiming absence on a failed read is how a teardown deletes a role
# out from under a live knowledge base.
kb_still_present() {
    local kb_id="$1"
    local ids
    if ! ids=$(list_knowledge_bases | cut -f1); then
        log_warn "  could not list knowledge bases while confirming ${kb_id}; assuming present"
        return 0
    fi
    grep -Fxq "${kb_id}" <<<"${ids}"
}

log_info ""
log_info "Phase 0: Deleting runtime-created Managed Knowledge Bases..."
log_info "  Scope: prefix=${PREFIX} env=${ENVIRONMENT} region=${AWS_REGION_ARG}"

MATCHED=()
SKIPPED=0

# Captured, not piped through a process substitution. `done < <(...)` hides the
# lister's exit status entirely, so a failed listing would read as an empty
# account and this script would report a clean teardown having deleted nothing —
# while the knowledge bases kept billing.
if ! ALL_KNOWLEDGE_BASES=$(list_knowledge_bases); then
    log_warn "Could not list knowledge bases in ${AWS_REGION_ARG}."
    log_warn "Refusing to continue: an unreadable account is indistinguishable from"
    log_warn "an empty one, and treating it as empty would leave paid resources behind."
    exit 1
fi

while IFS=$'\t' read -r KB_ID KB_NAME KB_STATUS; do
    [ -n "${KB_ID}" ] || continue
    if is_ours "${KB_ID}"; then
        MATCHED+=("${KB_ID}|${KB_NAME}|${KB_STATUS}")
    else
        SKIPPED=$((SKIPPED + 1))
    fi
done <<< "${ALL_KNOWLEDGE_BASES}"

log_info "  Matched ${#MATCHED[@]} knowledge base(s); left ${SKIPPED} untagged or other-environment one(s) alone"

if [ ${#MATCHED[@]} -eq 0 ]; then
    log_success "No Managed Knowledge Bases to delete"
    exit 0
fi

if [ ${#MATCHED[@]} -gt "${KB_TEARDOWN_MAX}" ]; then
    log_warn "Refusing to delete ${#MATCHED[@]} knowledge bases in one run (limit ${KB_TEARDOWN_MAX})."
    log_warn "This is far more than expected. Verify the tag filter before raising KB_TEARDOWN_MAX."
    exit 1
fi

DELETED=()
STUCK=()

for ENTRY in "${MATCHED[@]}"; do
    KB_ID="${ENTRY%%|*}"
    REST="${ENTRY#*|}"
    KB_NAME="${REST%%|*}"
    KB_STATUS="${REST##*|}"

    if [ "${KB_STATUS}" = "DELETE_UNSUCCESSFUL" ]; then
        # A real terminal state that retrying does not clear. Named here so
        # the operator gets the remedy rather than a generic timeout.
        log_warn "  ${KB_ID} (${KB_NAME}) is in DELETE_UNSUCCESSFUL and needs operator action."
        log_warn "    Set the data source's dataDeletionPolicy to RETAIN and retry the delete."
        STUCK+=("${KB_ID}")
        continue
    fi

    log_info "  Deleting ${KB_ID} (${KB_NAME})..."
    if ! aws bedrock-agent delete-knowledge-base \
        --region "${AWS_REGION_ARG}" \
        --knowledge-base-id "${KB_ID}" >/dev/null 2>&1; then
        log_warn "  delete-knowledge-base call failed for ${KB_ID}"
        STUCK+=("${KB_ID}")
        continue
    fi
    DELETED+=("${KB_ID}")
done

# ---------------------------------------------------------------
# Confirm absence. "Delete call accepted" is not "resource gone":
# deletion is asynchronous and took 2-6 minutes when measured
# (Requirement 13.4).
# ---------------------------------------------------------------
for KB_ID in "${DELETED[@]}"; do
    log_info "  Waiting for ${KB_ID} to disappear from ListKnowledgeBases..."

    # Bounded by a countdown of attempts, not only by elapsed time. The elapsed
    # counter is enough once the interval is clamped above, but a loop whose
    # termination depends on arithmetic is a loop that can be made infinite by a
    # later edit to that arithmetic. An attempt budget cannot.
    ATTEMPTS_LEFT=$(( KB_DELETE_POLL_TIMEOUT_SECONDS / KB_DELETE_POLL_INTERVAL_SECONDS + 1 ))
    CONFIRMED=0

    while [ "${ATTEMPTS_LEFT}" -gt 0 ]; do
        if ! kb_still_present "${KB_ID}"; then
            CONFIRMED=1
            break
        fi
        ATTEMPTS_LEFT=$(( ATTEMPTS_LEFT - 1 ))
        [ "${ATTEMPTS_LEFT}" -gt 0 ] || break
        sleep "${KB_DELETE_POLL_INTERVAL_SECONDS}"
    done

    if [ "${CONFIRMED}" -eq 1 ]; then
        log_success "  ${KB_ID} confirmed absent"
    else
        log_warn "  ${KB_ID} is still listed after ${KB_DELETE_POLL_TIMEOUT_SECONDS}s; not confirmed deleted"
        STUCK+=("${KB_ID}")
    fi
done

if [ ${#STUCK[@]} -gt 0 ]; then
    log_warn "The following knowledge bases were not confirmed absent:"
    for KB_ID in "${STUCK[@]}"; do
        log_warn "  - ${KB_ID}"
    done
    # Requirement 13.5: the service role must outlive its knowledge bases, so
    # the caller must not proceed to delete the stack that owns it.
    log_warn "NOT proceeding to stack teardown: the Bedrock service role must"
    log_warn "outlive its knowledge bases, and deleting it now is a route into"
    log_warn "DELETE_UNSUCCESSFUL, which retrying does not clear."
    exit 1
fi

log_success "All ${#DELETED[@]} tagged Managed Knowledge Base(s) confirmed deleted"
