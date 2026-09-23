#!/usr/bin/env bash
# Remove everything a load run leaves behind: the Cognito users and quota
# overrides created by provision.sh, and the app-side data the application
# writes once those users sign in and chat.
#
#   scripts/load-test/teardown.sh --manifest ~/.config/agentcore-load/users-<run>.json
#   scripts/load-test/teardown.sh --orphans            # dry run: runs whose manifest is gone
#   scripts/load-test/teardown.sh --orphans --apply
#
# NOT RUN BY CI. Deletes users from a live pool, removes quota-override rows,
# and deletes per-user rows across the application's tables.
#
# Run this. A forgotten 'unlimited' override is a cost control that is silently
# switched off for a real user id, leftover load-test users keep working
# credentials against your platform, and leftover app rows are counted as real
# users by the admin dashboards.
#
# Safety rails (the README has the full list):
#  * Every username in the manifest must start with the load-test prefix, and
#    every user_id must be a well-formed Cognito sub. Both are checked across
#    the whole file before anything is deleted.
#  * Before any app data is touched, each sub's `<prefix>-users` row must carry
#    the load-test email — `<username>@<domain>` in manifest mode,
#    `loadtest-…@<domain>` in orphan mode. One mismatch in a manifest refuses the
#    whole manifest, so a swapped sub cannot reach a real user's data.
#  * Overrides are deleted before users; the users row is deleted last.
#
# Required environment:
#   CDK_PROJECT_PREFIX   resolves SSM parameters
#   CDK_AWS_REGION       (or AWS_REGION)
set -euo pipefail

# `wait -n` (the job pool) and mapfile need bash 4.3+. macOS still ships 3.2,
# where the failure would otherwise surface mid-run as a cryptic syntax error.
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3) )); then
    echo "teardown.sh needs bash 4.3+ (this is ${BASH_VERSION}). Run inside the devcontainer." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"
# shellcheck source=./app-data.sh
source "${SCRIPT_DIR}/app-data.sh"

MANIFEST=""
ORPHANS=false
APPLY=false
LIMIT=""
DRY_RUN=false
ASSUME_YES=false
KEEP_MANIFEST=false

usage() {
    cat <<'EOF'
Usage: teardown.sh --manifest PATH [options]
       teardown.sh --orphans [--apply] [options]

  --manifest PATH    Manifest written by provision.sh
  --orphans          Find load-test users whose manifest is gone and remove
                     their app data. Dry run unless --apply is also given.
  --apply            With --orphans: actually delete
  --limit N          With --orphans: handle at most N users this pass
  --email-domain D   Load-test email domain (default load.invalid; must match
                     what provision.sh was given)
  --jobs N           Users processed concurrently (default 8)
  --keep-manifest    Do not delete the manifest afterwards
  --dry-run          Print the plan; make no changes
  --yes              Skip the confirmation prompt
  -h, --help         This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest)      MANIFEST="$2"; shift 2 ;;
        --orphans)       ORPHANS=true; shift ;;
        --apply)         APPLY=true; shift ;;
        --limit)         LIMIT="$2"; shift 2 ;;
        --email-domain)  LOAD_TEST_EMAIL_DOMAIN="$2"; shift 2 ;;
        --jobs)          TEARDOWN_JOBS="$2"; shift 2 ;;
        --keep-manifest) KEEP_MANIFEST=true; shift ;;
        --dry-run)       DRY_RUN=true; shift ;;
        --yes)           ASSUME_YES=true; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ "${ORPHANS}" = true ] && [ -n "${MANIFEST}" ]; then
    log_error "--orphans and --manifest are separate modes; give one."
    exit 1
fi
if [ "${ORPHANS}" = false ] && [ -z "${MANIFEST}" ]; then
    log_error "--manifest (or --orphans) is required."
    usage
    exit 1
fi
if [ "${ORPHANS}" = false ] && { [ "${APPLY}" = true ] || [ -n "${LIMIT}" ]; }; then
    log_error "--apply and --limit only apply to --orphans."
    exit 1
fi
if [ -n "${LIMIT}" ] && { ! [[ "${LIMIT}" =~ ^[0-9]+$ ]] || [ "${LIMIT}" -lt 1 ]; }; then
    log_error "--limit must be a positive integer (got '${LIMIT}')"
    exit 1
fi
if ! [[ "${TEARDOWN_JOBS}" =~ ^[0-9]+$ ]] || [ "${TEARDOWN_JOBS}" -lt 1 ] || [ "${TEARDOWN_JOBS}" -gt 32 ]; then
    log_error "--jobs must be between 1 and 32 (got '${TEARDOWN_JOBS}')"
    exit 1
fi
if ! [[ "${LOAD_TEST_EMAIL_DOMAIN}" =~ ^[A-Za-z0-9.-]+$ ]]; then
    log_error "--email-domain must be a bare domain (got '${LOAD_TEST_EMAIL_DOMAIN}')"
    exit 1
fi
# --orphans is a dry run unless --apply says otherwise.
if [ "${ORPHANS}" = true ] && [ "${APPLY}" = false ]; then
    DRY_RUN=true
fi

# ---------------------------------------------------------------------------
# Manifest checks — before credentials, before any AWS call.
#
# The prefix and sub checks run across the whole manifest and refuse the entire
# file on any violation, so a bad manifest cannot delete a subset of real users
# before failing.
# ---------------------------------------------------------------------------
if [ "${ORPHANS}" = false ]; then
    if [ ! -f "${MANIFEST}" ]; then
        log_error "Manifest not found: ${MANIFEST}"
        exit 1
    fi
    if ! jq -e 'type == "array" and length > 0' "${MANIFEST}" >/dev/null 2>&1; then
        log_error "Manifest is not a non-empty JSON array: ${MANIFEST}"
        log_error "Nothing was deleted."
        exit 1
    fi

    OFFENDING="$(jq -r --arg prefix "${USERNAME_PREFIX}" '
        [.[] | (.username // "<missing username>")
              | select(startswith($prefix) | not)]
        | join(", ")
    ' "${MANIFEST}")"

    if [ -n "${OFFENDING}" ]; then
        log_error "Refusing to act on this manifest. These entries do not start with '${USERNAME_PREFIX}':"
        log_error "  ${OFFENDING}"
        log_error "Only files produced by provision.sh can be torn down. Nothing was deleted."
        exit 1
    fi

    # user_id reaches DynamoDB keys and S3 prefixes. An empty one would make
    # "user-files/<sub>/" a prefix over every user, so a malformed sub anywhere
    # refuses the file, exactly like a bad username.
    BAD_SUBS="$(jq -r '
        [.[] | select((.user_id // "") | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$") | not)
             | .username]
        | join(", ")
    ' "${MANIFEST}")"

    if [ -n "${BAD_SUBS}" ]; then
        log_error "Refusing to act on this manifest. These entries have a missing or malformed user_id:"
        log_error "  ${BAD_SUBS}"
        log_error "Nothing was deleted."
        exit 1
    fi
fi

require_env

# Concurrent jobs share the account's DynamoDB, Cognito and AgentCore request
# rates. Give the CLI's own retry on throttling more headroom than its default
# of 3 attempts, unless the operator has chosen a value.
export AWS_MAX_ATTEMPTS="${AWS_MAX_ATTEMPTS:-10}"

USER_POOL_ID="$(resolve_user_pool_id)"
QUOTA_TABLE="$(resolve_quota_table)"
resolve_app_targets

WORK="$(_tmpdir)/work"
mkdir -p "${WORK}/users"

# ---------------------------------------------------------------------------
# Select the users to tear down. Each row of TARGETS is four fields joined by
# the ASCII unit separator (\x1f):
#   sub, username (empty in orphan mode), override id (manifest only), email
# Not tabs: bash treats tab as IFS whitespace and collapses consecutive ones,
# which would silently shift every field after an empty one.
# ---------------------------------------------------------------------------
SEP=$'\x1f'
TARGETS=()

# Per-user phases run as jobs (see parallel_each). Each takes the sub and its
# work directory, reads the target record from "$dir/target", and leaves its
# results as files there for the parent to read once every job has finished.

# Orphan mode only. A sub that still has a Cognito account is not an orphan:
# either its run is in progress or its manifest still exists. It is left for
# `--manifest`, which also removes the account and its override.
_job_cognito() {
    local sub="$1" dir="$2" count
    count="$(aws cognito-idp list-users \
        --user-pool-id "${USER_POOL_ID}" \
        --filter "sub = \"${sub}\"" \
        --query "length(Users)" \
        --output text \
        --region "${CDK_AWS_REGION}")" || return 1
    if [ "${count}" = "0" ]; then
        echo gone > "${dir}/cognito"
    else
        echo live > "${dir}/cognito"
    fi
}

_job_users_row() {
    local sub="$1" dir="$2"
    user_rows "${sub}" > "${dir}/users.items" || return 1
    jq -c '{PK, SK}' "${dir}/users.items" > "${dir}/users.keys"
}

_job_inventory() {
    local sub="$1" dir="$2"
    inventory_user "${sub}" "${dir}" "${WORK}" > "${dir}/summary"
}

# The whole per-user sequence, in the order the rails require: override, then
# account, then app data with the users row last.
_job_delete() {
    local sub="$1" dir="$2"
    local username override_id email failures=0 stuck=0
    IFS="${SEP}" read -r _ username override_id email < "${dir}/target"

    {
        log_info "${username:-${email}}"

        if [ -n "${override_id}" ]; then
            if delete_override "${QUOTA_TABLE}" "${override_id}"; then
                log_success "  override ${override_id} removed"
            else
                log_error "  FAILED to remove override ${override_id} — cost limit still bypassed"
                failures=$((failures + 1))
            fi
        fi

        if [ -n "${username}" ]; then
            if aws cognito-idp admin-delete-user \
                    --user-pool-id "${USER_POOL_ID}" \
                    --username "${username}" \
                    --region "${CDK_AWS_REGION}" \
                    --no-cli-pager >/dev/null 2>&1; then
                log_success "  user deleted"
            else
                # Already gone is the common case on a re-run; report it
                # without failing the whole teardown.
                log_warn "  user not deleted (already absent?)"
            fi
        fi

        if [ -f "${dir}/summary" ]; then
            log_info "  app data"
            delete_user_app_data "${sub}" "${dir}" || stuck=$?
            failures=$((failures + stuck))
        fi
    } > "${dir}/delete.log" 2>&1
    echo "${failures}" > "${dir}/delete.failures"
}

_print_delete_log() {
    cat "${WORK}/users/$1/delete.log"
}

# Every sub whose STEP job did not finish cleanly, one per line.
_failed_step() {
    local step="$1" sub
    shift
    for sub in "$@"; do
        [ "$(cat "${WORK}/users/${sub}/${step}.status" 2>/dev/null)" = "0" ] || echo "${sub}"
    done
}

if [ "${ORPHANS}" = false ]; then
    mapfile -t TARGETS < <(jq -r '.[] | [.user_id, .username, (.override_id // ""), ""] | join("\u001f")' "${MANIFEST}")
    MODE_LABEL="manifest ${MANIFEST}"
else
    log_info "Looking up ${LOAD_TEST_EMAIL_DOMAIN} users in ${USERS_TABLE} (EmailDomainIndex)…"
    domain_values="$(_tmpdir)/domain-values.json"
    jq -n --arg d "DOMAIN#$(printf '%s' "${LOAD_TEST_EMAIL_DOMAIN}" | tr '[:upper:]' '[:lower:]')" \
        '{":d": {S: $d}}' > "${domain_values}"

    if ! _ddb_paged query "${USERS_TABLE}" -- \
            --index-name EmailDomainIndex \
            --key-condition-expression "GSI2PK = :d" \
            --expression-attribute-values "file://${domain_values}" \
            > "${WORK}/candidates.jsonl"; then
        log_error "Could not query ${USERS_TABLE}. Nothing was deleted."
        exit 1
    fi

    skipped_shape=0
    CANDIDATES=()
    while IFS="${SEP}" read -r sub email pk; do
        if ! is_valid_sub "${sub}" || [ "${pk}" != "USER#${sub}" ] || ! owner_email_ok "${email}"; then
            log_warn "  skip ${email:-<no email>} (${pk}): not a load-test row shape"
            skipped_shape=$((skipped_shape + 1))
            continue
        fi
        mkdir -p "${WORK}/users/${sub}"
        printf '%s\n' "${sub}${SEP}${SEP}${SEP}${email}" > "${WORK}/users/${sub}/target"
        CANDIDATES+=("${sub}")
    done < <(jq -r '[.userId.S // "", .email.S // "", .PK.S // ""] | join("\u001f")' "${WORK}/candidates.jsonl" | sort -t"${SEP}" -k2)

    if [ ${#CANDIDATES[@]} -gt 0 ]; then
        log_info "Checking ${#CANDIDATES[@]} sub(s) against Cognito…"
        parallel_each cognito _job_cognito "${CANDIDATES[@]}"
        failed="$(_failed_step cognito "${CANDIDATES[@]}")"
        if [ -n "${failed}" ]; then
            log_error "Could not query Cognito for: $(echo "${failed}" | tr '\n' ' ')"
            log_error "Nothing was deleted."
            exit 1
        fi
    fi

    skipped_live=0
    for sub in "${CANDIDATES[@]}"; do
        if [ "$(cat "${WORK}/users/${sub}/cognito")" = "live" ]; then
            skipped_live=$((skipped_live + 1))
            continue
        fi
        if [ -z "${LIMIT}" ] || [ "${#TARGETS[@]}" -lt "${LIMIT}" ]; then
            TARGETS+=("$(cat "${WORK}/users/${sub}/target")")
        fi
    done

    orphaned=$(( ${#CANDIDATES[@]} - skipped_live ))
    log_info "$(wc -l < "${WORK}/candidates.jsonl" | tr -d ' ') ${LOAD_TEST_EMAIL_DOMAIN} row(s): ${orphaned} orphaned, ${skipped_live} still in Cognito (use --manifest), ${skipped_shape} not load-test shaped"
    if [ -n "${LIMIT}" ] && [ "${orphaned}" -gt "${#TARGETS[@]}" ]; then
        log_info "--limit ${LIMIT}: handling ${#TARGETS[@]} this pass; re-run for the rest."
    fi
    MODE_LABEL="orphans (users whose Cognito account is gone)"
    if [ ${#TARGETS[@]} -eq 0 ]; then
        log_success "No orphaned load-test users. Nothing to do."
        exit 0
    fi
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    log_error "Manifest yielded no usable entries: ${MANIFEST}"
    exit 1
fi

SUBS=()
for target in "${TARGETS[@]}"; do
    sub="${target%%"${SEP}"*}"
    mkdir -p "${WORK}/users/${sub}"
    printf '%s\n' "${target}" > "${WORK}/users/${sub}/target"
    SUBS+=("${sub}")
done

# ---------------------------------------------------------------------------
# Owner check, across every target, before anything is deleted.
#
# The users row is the evidence: a sub whose row carries anything but the
# load-test email refuses the whole run (in a manifest it means the file was
# edited or swapped). A sub with no row at all never signed in — or was
# already torn down — and owns no app data, so there is nothing to verify and
# nothing to delete for it.
# ---------------------------------------------------------------------------
log_info "Verifying ${#SUBS[@]} users row(s) in ${USERS_TABLE}…"
parallel_each users-row _job_users_row "${SUBS[@]}"
failed="$(_failed_step users-row "${SUBS[@]}")"
if [ -n "${failed}" ]; then
    log_error "Could not read ${USERS_TABLE} for: $(echo "${failed}" | tr '\n' ' ')"
    log_error "Nothing was deleted."
    exit 1
fi

VERIFIED=()
MISMATCHED=()
for sub in "${SUBS[@]}"; do
    dir="${WORK}/users/${sub}"
    IFS="${SEP}" read -r _ username _ _ < "${dir}/target"
    [ -s "${dir}/users.items" ] || continue

    emails="$(jq -r '.email.S // empty' "${dir}/users.items")"
    ok=true
    [ -z "${emails}" ] && ok=false
    while IFS= read -r email; do
        [ -z "${email}" ] && continue
        owner_email_ok "${email}" "${username}" || ok=false
    done <<< "${emails}"

    if [ "${ok}" = true ]; then
        VERIFIED+=("${sub}")
    else
        MISMATCHED+=("${username:-${sub}} -> ${emails:-<no email>}")
    fi
done

if [ ${#MISMATCHED[@]} -gt 0 ]; then
    log_error "Refusing to act: these users rows do not carry the expected load-test email:"
    for line in "${MISMATCHED[@]}"; do
        log_error "  ${line}"
    done
    log_error "A swapped or hand-edited sub would otherwise delete a real user's data. Nothing was deleted."
    exit 1
fi

# ---------------------------------------------------------------------------
# Inventory the verified users. Reads only.
# ---------------------------------------------------------------------------
if [ ${#VERIFIED[@]} -gt 0 ]; then
    log_info "Indexing S3 user prefixes and ${ROLLUP_TABLE} markers…"
    if ! index_s3_prefixes "${WORK}" || ! index_rollup_markers "${WORK}"; then
        log_error "Could not index S3 prefixes or rollup markers. Nothing was deleted."
        exit 1
    fi

    log_info "Inventorying ${#VERIFIED[@]} user(s)…"
    parallel_each inventory _job_inventory "${VERIFIED[@]}"
    failed="$(_failed_step inventory "${VERIFIED[@]}")"
    if [ -n "${failed}" ]; then
        log_error "Could not inventory: $(echo "${failed}" | tr '\n' ' ')"
        log_error "Nothing was deleted."
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
cat <<EOF

$(log_plan "Load-test teardown plan")
  Project prefix : ${CDK_PROJECT_PREFIX}
  AWS account    : ${LOAD_TEST_AWS_ACCOUNT}
  Region         : ${CDK_AWS_REGION}
  Mode           : ${MODE_LABEL}
  User pool      : ${USER_POOL_ID}
  Quota table    : ${QUOTA_TABLE}
$(print_app_targets)
  Email domain   : ${LOAD_TEST_EMAIL_DOMAIN}
  Entries        : ${#TARGETS[@]}  (${#VERIFIED[@]} with app data)

EOF

for sub in "${SUBS[@]}"; do
    dir="${WORK}/users/${sub}"
    IFS="${SEP}" read -r _ username override_id email < "${dir}/target"
    if [ "${ORPHANS}" = false ]; then
        printf '    delete user %-34s override %s\n' "${username}" "${override_id:-<none>}"
    else
        printf '    orphan %s  %s\n' "${sub}" "${email}"
    fi
    if [ -f "${dir}/summary" ]; then
        printf '        app data: %s, users row\n' "$(cat "${dir}/summary")"
    else
        printf '        app data: none (no users row: never signed in, or already torn down)\n'
    fi
done
echo
print_spend_kept_in_rollups "${WORK}/users"
echo

if [ "${DRY_RUN}" = true ]; then
    if [ "${ORPHANS}" = true ]; then
        log_info "Dry run (the default for --orphans): no changes made. Re-run with --apply to delete."
    else
        log_info "--dry-run: no changes made."
    fi
    exit 0
fi

if [ "${ORPHANS}" = true ]; then
    confirm_or_exit "${ASSUME_YES}" "Delete app data for ${#VERIFIED[@]} orphaned load-test user(s)?"
else
    confirm_or_exit "${ASSUME_YES}" "Delete ${#TARGETS[@]} user(s), their quota overrides and their app data?"
fi

# ---------------------------------------------------------------------------
# Delete. Per user: override first — if the run is interrupted, the worse
# leftover to have is a live user with a disabled cost limit, so remove the
# limit bypass before the account that could use it — then the account, then
# app data with the users row last. Users run concurrently; each one's steps
# stay in that order, and its log prints as a block, in plan order.
# ---------------------------------------------------------------------------
PARALLEL_ON_DONE=_print_delete_log parallel_each delete _job_delete "${SUBS[@]}"

failures=0
for sub in "${SUBS[@]}"; do
    n="$(cat "${WORK}/users/${sub}/delete.failures" 2>/dev/null || echo 1)"
    [ "$(cat "${WORK}/users/${sub}/delete.status" 2>/dev/null)" = "0" ] || n=$((n + 1))
    failures=$((failures + n))
done

if [ "${failures}" -gt 0 ]; then
    if [ "${ORPHANS}" = true ]; then
        log_error "${failures} step(s) failed. Re-run teardown.sh --orphans --apply; users rows were kept for anything unfinished."
    else
        log_error "${failures} step(s) failed. Manifest kept: ${MANIFEST}"
        log_error "Re-run teardown; overrides can also be removed from the admin Quota Overrides page."
    fi
    exit 1
fi

if [ "${ORPHANS}" = false ]; then
    if [ "${KEEP_MANIFEST}" = true ]; then
        log_warn "Manifest kept at ${MANIFEST} — it still contains plaintext passwords."
    else
        rm -f "${MANIFEST}"
        log_info "Manifest deleted."
    fi
fi

log_success "Teardown complete."
