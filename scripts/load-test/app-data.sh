#!/usr/bin/env bash
# App-side data a load-test user leaves behind once it signs in and chats.
# Sourced by teardown.sh after lib.sh, not executed.
#
# The Cognito user and the quota override are what provision.sh creates. Everything
# here is what the *application* writes the first time that user logs in and
# sends a turn, keyed on the Cognito `sub`. See the README's "What gets created"
# for the full table-by-table list, including the stores deliberately not
# touched because a load run cannot write them.
#
# Two invariants every function below keeps:
#
#  * Nothing is deleted for a sub until its `<prefix>-users` row has been read
#    and its email checked (owner_email_ok). A sub that does not resolve to a
#    load-test email is refused, so a swapped or mistyped sub cannot reach a
#    real user's data.
#  * The `<prefix>-users` row is deleted LAST, and only when everything else
#    for that user succeeded. It is the evidence the email check reads, so
#    deleting it early would leave a re-run with rows it can no longer verify.

# Cognito subs are UUIDs. They are interpolated into DynamoDB keys and S3
# prefixes, so anything else — an empty string above all, which would turn
# "user-files/<sub>/" into a prefix covering every user — is refused outright.
is_valid_sub() {
    [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

# The owner check. With a username (manifest mode) the email must be exactly
# the one provision.sh set, `<username>@<domain>`; without one (orphan mode) it
# must still look like one — `loadtest-…@<domain>` — which is the orphan-mode
# equivalent of the manifest's username-prefix rail. Both imply the required
# `@<domain>` suffix. The app lowercases emails on write, so compare lowercased.
owner_email_ok() {
    local email="$1" username="${2:-}"
    local domain
    email="$(printf '%s' "${email}" | tr '[:upper:]' '[:lower:]')"
    domain="$(printf '%s' "${LOAD_TEST_EMAIL_DOMAIN}" | tr '[:upper:]' '[:lower:]')"

    if [ -n "${username}" ]; then
        username="$(printf '%s' "${username}" | tr '[:upper:]' '[:lower:]')"
        [ "${email}" = "${username}@${domain}" ]
        return
    fi
    [[ "${email}" == "${USERNAME_PREFIX}"* && "${email}" == *"@${domain}" ]] \
        && [[ "${email%@*}" =~ ^[a-z0-9-]+$ ]]
}

# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

# Every store is deployed unconditionally by PlatformStack, so every one is
# required: a missing parameter means the wrong prefix or account, not an
# optional feature, and stopping before the plan is the right answer.
resolve_app_targets() {
    USERS_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/users/users-table-name")"
    SESSIONS_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/cost-tracking/sessions-metadata-table-name")"
    COST_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/cost-tracking/user-cost-summary-table-name")"
    ROLLUP_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/cost-tracking/system-cost-rollup-table-name")"
    QUOTA_EVENTS_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/quota/quota-events-table-name")"
    UPLOADS_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/user-file-uploads/table-name")"
    UPLOADS_BUCKET="$(_ssm_value "/${CDK_PROJECT_PREFIX}/user-file-uploads/bucket-name")"
    ARTIFACTS_TABLE="$(_ssm_value "/${CDK_PROJECT_PREFIX}/artifacts/table-name")"
    ARTIFACTS_BUCKET="$(_ssm_value "/${CDK_PROJECT_PREFIX}/artifacts/bucket-name")"
    MEMORY_ID="$(_ssm_value "/${CDK_PROJECT_PREFIX}/inference-api/memory-id")"

    # Long-term records live under /strategies/<strategyId>/actors/<sub>/, and
    # the strategy ids carry a random suffix per deployment, so ask the memory
    # rather than hardcode them. An older CLI without bedrock-agentcore fails
    # here, before anything is deleted, instead of halfway through.
    local strategies
    if ! strategies="$(aws bedrock-agentcore-control get-memory \
            --memory-id "${MEMORY_ID}" \
            --query "memory.strategies[].strategyId" \
            --output text \
            --region "${CDK_AWS_REGION}")"; then
        log_error "Could not read AgentCore Memory ${MEMORY_ID} (is the AWS CLI new enough for bedrock-agentcore?)."
        exit 1
    fi
    read -r -a MEMORY_STRATEGIES <<< "${strategies}"
    [ "${MEMORY_STRATEGIES[0]:-}" = "None" ] && MEMORY_STRATEGIES=()
    return 0
}

print_app_targets() {
    cat <<EOF
  App data       : ${USERS_TABLE}  (verified, deleted last)
                   ${SESSIONS_TABLE}
                   ${COST_TABLE}
                   ${ROLLUP_TABLE}  (ACTIVE# markers + counters only)
                   ${QUOTA_EVENTS_TABLE}
                   ${UPLOADS_TABLE}  + s3://${UPLOADS_BUCKET}
                   ${ARTIFACTS_TABLE}  + s3://${ARTIFACTS_BUCKET}
                   AgentCore Memory ${MEMORY_ID} (${#MEMORY_STRATEGIES[@]} strategies, long-term records)
EOF
}

# ---------------------------------------------------------------------------
# Concurrency
#
# Almost all of a teardown's wall-clock is AWS CLI start-up: roughly fifteen
# calls per user, each a fresh process. Serially that is hours for a few hundred
# users, so the per-user phases run as a bounded pool of background jobs.
# ---------------------------------------------------------------------------

TEARDOWN_JOBS="${TEARDOWN_JOBS:-8}"

# One job: run FN SUB DIR and record its exit status in DIR/STEP.status. The
# status file is written last, so its presence means every other output of the
# job is complete. Results travel back through files, never through `wait`.
_run_step() {
    local step="$1" fn="$2" sub="$3"
    local dir="${WORK}/users/${sub}" rc=0
    "${fn}" "${sub}" "${dir}" || rc=$?
    echo "${rc}" > "${dir}/${step}.status"
}

#   parallel_each STEP FN SUB...
#
# Runs FN for every SUB with at most TEARDOWN_JOBS in flight and returns when
# all have finished. With PARALLEL_ON_DONE set to a function name, that
# function is called once per SUB, in argument order, as soon as it and every
# SUB before it are done — so per-user output streams in plan order instead of
# interleaving. Without it, a progress counter goes to stderr.
parallel_each() {
    local step="$1" fn="$2"
    shift 2
    local subs=("$@")
    local total=$# next=0 i

    _parallel_flush() {
        while [ "${next}" -lt "${total}" ] \
                && [ -f "${WORK}/users/${subs[next]}/${step}.status" ]; do
            [ -n "${PARALLEL_ON_DONE:-}" ] && "${PARALLEL_ON_DONE}" "${subs[next]}"
            next=$((next + 1))
        done
        [ -z "${PARALLEL_ON_DONE:-}" ] && printf '\r  %s %d/%d' "${step}" "${next}" "${total}" >&2
        return 0
    }

    for ((i = 0; i < total; i++)); do
        while [ "$(jobs -rp | wc -l)" -ge "${TEARDOWN_JOBS}" ]; do
            wait -n || true
            _parallel_flush
        done
        _run_step "${step}" "${fn}" "${subs[i]}" &
    done
    wait || true
    _parallel_flush
    [ -z "${PARALLEL_ON_DONE:-}" ] && echo >&2
    return 0
}

# ---------------------------------------------------------------------------
# DynamoDB primitives
# ---------------------------------------------------------------------------

# Print every item a Query or Scan returns, one compact JSON object per line.
# Pagination is followed explicitly so a large partition streams page by page
# instead of arriving as one merged document.
#
#   _ddb_paged query|scan TABLE -- <extra aws args...>
#
# Expression values go through a temp file (file://) by the callers: they carry
# user ids, and argv quoting of nested JSON is where scripts like this go wrong.
_ddb_paged() {
    local op="$1" table="$2"
    shift 3
    local dir page start
    dir="$(_tmpdir)"
    page="$(mktemp "${dir}/page.XXXXXX")"
    start="$(mktemp "${dir}/start.XXXXXX")"
    : > "${start}"

    while :; do
        local args=(--table-name "${table}" --no-paginate --output json
            --region "${CDK_AWS_REGION}" "$@")
        [ -s "${start}" ] && args+=(--exclusive-start-key "file://${start}")

        aws dynamodb "${op}" "${args[@]}" > "${page}" || { rm -f "${page}" "${start}"; return 1; }
        jq -c '.Items[]' "${page}"

        if ! jq -e '.LastEvaluatedKey' "${page}" >/dev/null; then
            rm -f "${page}" "${start}"
            return 0
        fi
        jq -c '.LastEvaluatedKey' "${page}" > "${start}"
    done
}

# Every item in the `PK = USER#<sub>` partition of TABLE, keys (plus any extra
# attributes named in PROJECTION) only.
_query_user_partition() {
    local table="$1" sub="$2" projection="${3:-PK, SK}"
    local values
    values="$(mktemp "$(_tmpdir)/values.XXXXXX")"
    jq -n --arg pk "USER#${sub}" '{":pk": {S: $pk}}' > "${values}"
    _ddb_paged query "${table}" -- \
        --key-condition-expression "PK = :pk" \
        --expression-attribute-values "file://${values}" \
        --projection-expression "${projection}"
    local status=$?
    rm -f "${values}"
    return "${status}"
}

# Delete items by primary key, 25 per BatchWriteItem, retrying whatever DynamoDB
# hands back as unprocessed. KEYS_FILE holds one key object per line in
# DynamoDB JSON ({"PK":{"S":…},"SK":{"S":…}}).
#
# Non-zero if anything is still unprocessed after the retries, so the caller
# keeps the manifest rather than reporting a clean teardown that left rows.
ddb_batch_delete() {
    local table="$1" keys_file="$2"
    local dir chunk request response pending attempt
    [ -s "${keys_file}" ] || return 0

    dir="$(mktemp -d "$(_tmpdir)/batch.XXXXXX")"
    split -l 25 "${keys_file}" "${dir}/chunk-"
    request="${dir}/request.json"
    response="${dir}/response.json"

    for chunk in "${dir}"/chunk-*; do
        jq -s --arg table "${table}" \
            '{($table): [.[] | {DeleteRequest: {Key: .}}]}' "${chunk}" > "${request}"

        for attempt in 1 2 3 4 5 6; do
            if ! aws dynamodb batch-write-item \
                    --request-items "file://${request}" \
                    --output json \
                    --region "${CDK_AWS_REGION}" > "${response}"; then
                rm -rf "${dir}"
                return 1
            fi
            pending="$(jq '[.UnprocessedItems[]?[]] | length' "${response}")"
            [ "${pending}" -eq 0 ] && break
            if [ "${attempt}" -eq 6 ]; then
                rm -rf "${dir}"
                return 1
            fi
            jq '.UnprocessedItems' "${response}" > "${request}"
            sleep "$((attempt * attempt))"
        done
    done
    rm -rf "${dir}"
}

# ---------------------------------------------------------------------------
# Inventory. Reads only: this is what --dry-run prints and what the delete
# phase then removes, key for key, so the plan shown is the plan that runs.
# ---------------------------------------------------------------------------

# Load-test users row(s) for SUB, as {PK,SK,email} lines. SK is PROFILE today;
# the whole partition is read so an older SK shape cannot be missed.
user_rows() {
    _query_user_partition "${USERS_TABLE}" "$1" "PK, SK, email"
}

# One S3 listing per top-level prefix for the whole run, rather than one per
# user: the per-user prefixes are all direct children, so a delimiter listing
# names every user that has objects under it.
#
#   s3_user_prefixes BUCKET PARENT  -> "<sub>" per line
s3_user_prefixes() {
    local bucket="$1" parent="$2"
    aws s3api list-objects-v2 \
        --bucket "${bucket}" \
        --prefix "${parent}" \
        --delimiter "/" \
        --query "CommonPrefixes[].Prefix" \
        --output json \
        --region "${CDK_AWS_REGION}" \
        | jq -r --arg parent "${parent}" \
            '.[]? | ltrimstr($parent) | rtrimstr("/")'
}

# Load every S3 prefix the app writes per user into OUT_DIR, one file per
# location, so inventory_user can test membership without another call.
#
#   user-files/<sub>/           uploads, office documents, workspace files
#   compaction-offload/<sub>/   offloaded tool results (90-day lifecycle)
#   <sub>/                      artifact versions (artifacts-content bucket)
index_s3_prefixes() {
    local out="$1"
    s3_user_prefixes "${UPLOADS_BUCKET}" "user-files/" > "${out}/s3-user-files" || return 1
    s3_user_prefixes "${UPLOADS_BUCKET}" "compaction-offload/" > "${out}/s3-offload" || return 1
    s3_user_prefixes "${ARTIFACTS_BUCKET}" "" > "${out}/s3-artifacts" || return 1
}

# The system-cost-rollup table keys its unique-user markers with the sub as the
# SORT key (PK=ACTIVE#DAILY#<date> | ACTIVE#MONTHLY#<period> |
# ACTIVE#MODEL#<period>#<model>), so no query can find one user's markers. One
# filtered scan for the run, then a lookup per user. The table has no TTL
# enabled, so these never expire on their own.
#
# Output, tab-separated: sub, marker PK, rollup PK, rollup SK, counter name.
index_rollup_markers() {
    local out="$1"
    local values
    values="$(mktemp "$(_tmpdir)/values.XXXXXX")"
    jq -n '{":a": {S: "ACTIVE#"}}' > "${values}"

    _ddb_paged scan "${ROLLUP_TABLE}" -- \
        --filter-expression "begins_with(PK, :a)" \
        --expression-attribute-values "file://${values}" \
        --projection-expression "PK, SK" \
    | jq -r '
        .PK.S as $pk | .SK.S as $sub
        | if   ($pk | startswith("ACTIVE#DAILY#"))   then [$sub, $pk, "ROLLUP#DAILY",   ($pk | ltrimstr("ACTIVE#DAILY#")),   "activeUsers"]
          elif ($pk | startswith("ACTIVE#MONTHLY#")) then [$sub, $pk, "ROLLUP#MONTHLY", ($pk | ltrimstr("ACTIVE#MONTHLY#")), "activeUsers"]
          elif ($pk | startswith("ACTIVE#MODEL#"))   then [$sub, $pk, "ROLLUP#MODEL",   ($pk | ltrimstr("ACTIVE#MODEL#")),   "uniqueUsers"]
          else empty end
        | @tsv' > "${out}/rollup-markers.tsv"
    local status="${PIPESTATUS[0]}"
    rm -f "${values}"
    return "${status}"
}

# Collect everything SUB owns into DIR. Prints one summary line of counts.
# Returns non-zero only if a read failed; an empty user is a success.
inventory_user() {
    local sub="$1" dir="$2" index_dir="$3"
    mkdir -p "${dir}"

    local values
    values="$(mktemp "$(_tmpdir)/values.XXXXXX")"
    jq -n --arg pk "USER#${sub}" '{":pk": {S: $pk}}' > "${values}"

    _query_user_partition "${SESSIONS_TABLE}" "${sub}" > "${dir}/sessions.keys" || return 1
    _query_user_partition "${QUOTA_EVENTS_TABLE}" "${sub}" > "${dir}/quota-events.keys" || return 1
    _query_user_partition "${UPLOADS_TABLE}" "${sub}" > "${dir}/uploads.keys" || return 1
    _query_user_partition "${ARTIFACTS_TABLE}" "${sub}" > "${dir}/artifacts.keys" || return 1

    # Cost rows carry totalCost so the plan can say how much load-test spend
    # stays in the system rollups after the per-user rows go.
    _query_user_partition "${COST_TABLE}" "${sub}" "PK, SK, totalCost" > "${dir}/cost.items" || return 1
    jq -c '{PK, SK}' "${dir}/cost.items" > "${dir}/cost.keys"

    # Overrides are keyed OVERRIDE#<id>, reachable by user only through
    # UserOverrideIndex. In manifest mode this re-finds the manifest's own
    # override (already deleted first — the batch delete is then a no-op) plus
    # any a re-provision left; in orphan mode it is the only way to find them.
    _ddb_paged query "${QUOTA_TABLE}" -- \
        --index-name UserOverrideIndex \
        --key-condition-expression "GSI4PK = :pk" \
        --expression-attribute-values "file://${values}" \
        --projection-expression "PK, SK" > "${dir}/overrides.keys" || return 1
    rm -f "${values}"

    : > "${dir}/memory.ids"
    local strategy
    for strategy in "${MEMORY_STRATEGIES[@]}"; do
        aws bedrock-agentcore list-memory-records \
            --memory-id "${MEMORY_ID}" \
            --namespace "/strategies/${strategy}/actors/${sub}/" \
            --query "memoryRecordSummaries[].memoryRecordId" \
            --output json \
            --region "${CDK_AWS_REGION}" \
            | jq -r '.[]?' >> "${dir}/memory.ids" || return 1
    done

    : > "${dir}/s3.prefixes"
    grep -qxF "${sub}" "${index_dir}/s3-user-files" \
        && echo "s3://${UPLOADS_BUCKET}/user-files/${sub}/" >> "${dir}/s3.prefixes"
    grep -qxF "${sub}" "${index_dir}/s3-offload" \
        && echo "s3://${UPLOADS_BUCKET}/compaction-offload/${sub}/" >> "${dir}/s3.prefixes"
    grep -qxF "${sub}" "${index_dir}/s3-artifacts" \
        && echo "s3://${ARTIFACTS_BUCKET}/${sub}/" >> "${dir}/s3.prefixes"

    awk -F'\t' -v s="${sub}" '$1 == s' "${index_dir}/rollup-markers.tsv" > "${dir}/markers.tsv"

    printf 'sessions %s, cost %s, rollup-markers %s, quota-events %s, overrides %s, uploads %s, artifacts %s, s3-prefixes %s, memory %s' \
        "$(wc -l < "${dir}/sessions.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/cost.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/markers.tsv" | tr -d ' ')" \
        "$(wc -l < "${dir}/quota-events.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/overrides.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/uploads.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/artifacts.keys" | tr -d ' ')" \
        "$(wc -l < "${dir}/s3.prefixes" | tr -d ' ')" \
        "$(wc -l < "${dir}/memory.ids" | tr -d ' ')"
}

# Per-period load-test spend across every inventoried user. This is the money
# that stays in system-cost-rollup totals after teardown — printed in the plan
# so the gap between rollups and per-user rows is stated, not silent.
print_spend_kept_in_rollups() {
    local root="$1"
    local lines
    lines="$(cat "${root}"/*/cost.items 2>/dev/null | jq -rs '
        map({period: (.SK.S | ltrimstr("PERIOD#")), cost: ((.totalCost.N // "0") | tonumber)})
        | group_by(.period)
        | map("    \(.[0].period)  $\((map(.cost) | add) * 100 | round / 100)  across \(length) user(s)")
        | .[]' || true)"
    [ -z "${lines}" ] && return 0
    echo "  Load-test spend removed from ${COST_TABLE} but KEPT in"
    echo "  ${ROLLUP_TABLE} totals (it was real Bedrock spend — see README):"
    echo "${lines}"
}

# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

# Remove one unique-user marker and take that user back out of the rollup's
# population count, atomically, so the two can never disagree. The condition on
# the marker makes it idempotent: on a re-run the marker is already gone, the
# transaction is cancelled, and the counter is not decremented twice.
#
# Cost, token and request totals on the rollup row are NOT touched — see the
# README for why.
remove_rollup_marker() {
    local sub="$1" marker_pk="$2" rollup_pk="$3" rollup_sk="$4" counter="$5"
    local request err
    request="$(mktemp "$(_tmpdir)/tx.XXXXXX")"
    err="$(mktemp "$(_tmpdir)/tx-err.XXXXXX")"

    jq -n --arg table "${ROLLUP_TABLE}" --arg mpk "${marker_pk}" --arg sub "${sub}" \
          --arg rpk "${rollup_pk}" --arg rsk "${rollup_sk}" --arg counter "${counter}" '[
        {Delete: {TableName: $table,
                  Key: {PK: {S: $mpk}, SK: {S: $sub}},
                  ConditionExpression: "attribute_exists(PK)"}},
        {Update: {TableName: $table,
                  Key: {PK: {S: $rpk}, SK: {S: $rsk}},
                  UpdateExpression: "ADD #c :minus",
                  ConditionExpression: "#c > :zero",
                  ExpressionAttributeNames: {"#c": $counter},
                  ExpressionAttributeValues: {":minus": {N: "-1"}, ":zero": {N: "0"}}}}
    ]' > "${request}"

    # Retried on TransactionConflict: parallel jobs, and live app traffic
    # recording costs, both update these same few rollup rows, and DynamoDB
    # cancels a transaction that races a concurrent write rather than queueing
    # it. The CLI's own retries do not cover a cancellation.
    local attempt reasons
    for attempt in 1 2 3 4 5 6 7 8; do
        if aws dynamodb transact-write-items \
                --transact-items "file://${request}" \
                --region "${CDK_AWS_REGION}" \
                --no-cli-pager >/dev/null 2>"${err}"; then
            rm -f "${request}" "${err}"
            return 0
        fi
        reasons="$(grep -o '\[[A-Za-z, ]*\]' "${err}" | tail -1 || true)"
        # A missing marker is final even if the counter update also conflicted.
        [[ "${reasons}" == "[ConditionalCheckFailed, "* ]] && break
        case "${reasons}" in
            *TransactionConflict*|*ThrottlingError*) ;;
            *) break ;;
        esac
        sleep "$(( (RANDOM % 3) + attempt ))"
    done
    rm -f "${request}"

    case "${reasons}" in
        "[ConditionalCheckFailed, "*)
            # Marker already gone: a previous run got here. Nothing to do.
            rm -f "${err}"
            return 0 ;;
        "[None, ConditionalCheckFailed]")
            # The rollup row is missing or its counter is already 0, so there
            # is nothing to decrement. Remove the marker alone and say so.
            rm -f "${err}"
            log_warn "    ${rollup_pk} ${rollup_sk}: ${counter} missing or 0; marker removed without decrement"
            local key
            key="$(mktemp "$(_tmpdir)/key.XXXXXX")"
            jq -nc --arg mpk "${marker_pk}" --arg sub "${sub}" '{PK: {S: $mpk}, SK: {S: $sub}}' > "${key}"
            ddb_batch_delete "${ROLLUP_TABLE}" "${key}"
            local status=$?
            rm -f "${key}"
            return "${status}" ;;
    esac

    log_error "    ${marker_pk}: $(tr '\n' ' ' < "${err}")"
    rm -f "${err}"
    return 1
}

delete_memory_records() {
    local ids_file="$1"
    [ -s "${ids_file}" ] || return 0

    local dir chunk request
    dir="$(mktemp -d "$(_tmpdir)/mem.XXXXXX")"
    split -l 100 "${ids_file}" "${dir}/chunk-"
    request="${dir}/request.json"

    for chunk in "${dir}"/chunk-*; do
        jq -R -s 'split("\n") | map(select(length > 0) | {memoryRecordId: .})' "${chunk}" > "${request}"
        local failed
        if ! failed="$(aws bedrock-agentcore batch-delete-memory-records \
                --memory-id "${MEMORY_ID}" \
                --records "file://${request}" \
                --query "length(failedRecords)" \
                --output text \
                --region "${CDK_AWS_REGION}")"; then
            rm -rf "${dir}"
            return 1
        fi
        if [ "${failed}" != "0" ]; then
            rm -rf "${dir}"
            return 1
        fi
    done
    rm -rf "${dir}"
}

# Delete everything inventory_user found for SUB, users row last. Prints one
# line per store; returns the number of stores that failed.
delete_user_app_data() {
    local sub="$1" dir="$2"
    local failed=0 prefix

    _step() {
        local label="$1"; shift
        if "$@"; then
            log_success "    ${label}"
        else
            log_error "    FAILED: ${label}"
            failed=$((failed + 1))
        fi
    }

    [ -s "${dir}/overrides.keys" ] && _step "quota overrides" \
        ddb_batch_delete "${QUOTA_TABLE}" "${dir}/overrides.keys"

    while IFS= read -r prefix; do
        _step "${prefix}" aws s3 rm "${prefix}" --recursive --only-show-errors \
            --region "${CDK_AWS_REGION}"
    done < "${dir}/s3.prefixes"

    [ -s "${dir}/uploads.keys" ] && _step "file-upload rows ($(wc -l < "${dir}/uploads.keys" | tr -d ' '))" \
        ddb_batch_delete "${UPLOADS_TABLE}" "${dir}/uploads.keys"
    [ -s "${dir}/artifacts.keys" ] && _step "artifact rows ($(wc -l < "${dir}/artifacts.keys" | tr -d ' '))" \
        ddb_batch_delete "${ARTIFACTS_TABLE}" "${dir}/artifacts.keys"
    [ -s "${dir}/memory.ids" ] && _step "memory records ($(wc -l < "${dir}/memory.ids" | tr -d ' '))" \
        delete_memory_records "${dir}/memory.ids"
    [ -s "${dir}/sessions.keys" ] && _step "session rows ($(wc -l < "${dir}/sessions.keys" | tr -d ' '))" \
        ddb_batch_delete "${SESSIONS_TABLE}" "${dir}/sessions.keys"
    [ -s "${dir}/quota-events.keys" ] && _step "quota events ($(wc -l < "${dir}/quota-events.keys" | tr -d ' '))" \
        ddb_batch_delete "${QUOTA_EVENTS_TABLE}" "${dir}/quota-events.keys"

    local marker_sub marker_pk rollup_pk rollup_sk counter
    while IFS=$'\t' read -r marker_sub marker_pk rollup_pk rollup_sk counter; do
        _step "rollup ${marker_pk} (-1 ${counter})" \
            remove_rollup_marker "${marker_sub}" "${marker_pk}" "${rollup_pk}" "${rollup_sk}" "${counter}"
    done < "${dir}/markers.tsv"

    [ -s "${dir}/cost.keys" ] && _step "cost-summary rows ($(wc -l < "${dir}/cost.keys" | tr -d ' '))" \
        ddb_batch_delete "${COST_TABLE}" "${dir}/cost.keys"

    # Last, and only on a clean pass: this row is what the owner check reads.
    if [ "${failed}" -eq 0 ]; then
        _step "users row" ddb_batch_delete "${USERS_TABLE}" "${dir}/users.keys"
    else
        log_warn "    users row kept so a re-run can verify this user again"
    fi

    unset -f _step
    return "${failed}"
}
