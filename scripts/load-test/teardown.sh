#!/usr/bin/env bash
# Remove the Cognito users and quota overrides created by provision.sh.
#
#   scripts/load-test/teardown.sh --manifest ~/.config/agentcore-load/users-<run>.json
#
# NOT RUN BY CI. Deletes users from a live pool and removes quota-override rows.
#
# Run this. A forgotten 'unlimited' override is a cost control that is silently
# switched off for a real user id, and leftover load-test users keep working
# credentials against your platform.
#
# Safety rail: every username in the manifest must start with the load-test
# prefix, and the check happens before anything is deleted. A hand-edited or
# swapped manifest therefore cannot be used to delete real accounts.
#
# Required environment:
#   CDK_PROJECT_PREFIX   resolves SSM parameters
#   CDK_AWS_REGION       (or AWS_REGION)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=./lib.sh
source "${SCRIPT_DIR}/lib.sh"

MANIFEST=""
DRY_RUN=false
ASSUME_YES=false
KEEP_MANIFEST=false

usage() {
    cat <<'EOF'
Usage: teardown.sh --manifest PATH [options]

  --manifest PATH    Manifest written by provision.sh (required)
  --keep-manifest    Do not delete the manifest afterwards
  --dry-run          Print the plan; make no changes
  --yes              Skip the confirmation prompt
  -h, --help         This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest)      MANIFEST="$2"; shift 2 ;;
        --keep-manifest) KEEP_MANIFEST=true; shift ;;
        --dry-run)       DRY_RUN=true; shift ;;
        --yes)           ASSUME_YES=true; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [ -z "${MANIFEST}" ]; then
    log_error "--manifest is required."
    usage
    exit 1
fi
if [ ! -f "${MANIFEST}" ]; then
    log_error "Manifest not found: ${MANIFEST}"
    exit 1
fi

require_env

# ---------------------------------------------------------------------------
# Parse and validate the manifest before touching anything.
#
# The prefix check is the important part: it runs across the whole manifest and
# refuses the entire file on any violation, so a bad manifest cannot delete a
# subset of real users before failing.
# ---------------------------------------------------------------------------
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

# Tab-separated: usernames and override ids are constrained to [A-Za-z0-9-]
# plus the prefix, so neither field can contain a tab.
mapfile -t ROWS < <(jq -r '.[] | [.username, (.override_id // "")] | @tsv' "${MANIFEST}")

if [ ${#ROWS[@]} -eq 0 ]; then
    log_error "Manifest yielded no usable entries: ${MANIFEST}"
    exit 1
fi

USER_POOL_ID="$(resolve_user_pool_id)"
QUOTA_TABLE="$(resolve_quota_table)"

cat <<EOF

$(log_plan "Load-test teardown plan")
  Project prefix : ${CDK_PROJECT_PREFIX}
  Region         : ${CDK_AWS_REGION}
  User pool      : ${USER_POOL_ID}
  Quota table    : ${QUOTA_TABLE}
  Manifest       : ${MANIFEST}
  Entries        : ${#ROWS[@]}

EOF

for row in "${ROWS[@]}"; do
    username="${row%%$'\t'*}"
    override_id="${row##*$'\t'}"
    printf '    delete user %-34s override %s\n' "${username}" "${override_id:-<none>}"
done
echo

if [ "${DRY_RUN}" = true ]; then
    log_info "--dry-run: no changes made."
    exit 0
fi

confirm_or_exit "${ASSUME_YES}" "Delete ${#ROWS[@]} user(s) and their quota overrides?"

# ---------------------------------------------------------------------------
# Delete. Overrides first: if the run is interrupted, the worse leftover to
# have is a live user with a disabled cost limit, so remove the limit bypass
# before the account that could use it.
# ---------------------------------------------------------------------------
failures=0

for row in "${ROWS[@]}"; do
    username="${row%%$'\t'*}"
    override_id="${row##*$'\t'}"

    log_info "${username}"

    if [ -n "${override_id}" ]; then
        if delete_override "${QUOTA_TABLE}" "${override_id}"; then
            log_success "  override ${override_id} removed"
        else
            log_error "  FAILED to remove override ${override_id} — cost limit still bypassed"
            failures=$((failures + 1))
        fi
    fi

    if aws cognito-idp admin-delete-user \
            --user-pool-id "${USER_POOL_ID}" \
            --username "${username}" \
            --region "${CDK_AWS_REGION}" \
            --no-cli-pager >/dev/null 2>&1; then
        log_success "  user deleted"
    else
        # Already gone is the common case on a re-run; report it without
        # failing the whole teardown.
        log_warn "  user not deleted (already absent?)"
    fi
done

if [ "${failures}" -gt 0 ]; then
    log_error "${failures} override(s) could not be removed. Manifest kept: ${MANIFEST}"
    log_error "Re-run teardown, or remove them from the admin Quota Overrides page."
    exit 1
fi

if [ "${KEEP_MANIFEST}" = true ]; then
    log_warn "Manifest kept at ${MANIFEST} — it still contains plaintext passwords."
else
    rm -f "${MANIFEST}"
    log_info "Manifest deleted."
fi

log_success "Teardown complete."
