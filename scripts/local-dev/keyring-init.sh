#!/usr/bin/env bash
#
# Bring up the container's OS keyring, so `agentcore-tui login --sso` has an
# encrypted place to keep a live platform session.
#
# Why this exists at all: a container has no login session, so it has no D-Bus
# session bus and no unlocked keyring. Python's `keyring` package then selects
# `backends.fail.Keyring` and every write raises — which reads as "no keyring
# installed" even though the packages are in the image. This script supplies the
# two missing pieces:
#
#   1. a session bus, whose address is written to ~/.cache/dbus-session so that
#      every later `docker exec bash -lc` can rejoin it (the image's .bashrc
#      sources that file);
#   2. an unlocked `login` keyring, encrypted with a passphrase you type.
#
# The passphrase is deliberately NOT stored anywhere. That is the whole point:
# a passphrase kept in a file or an environment variable would sit next to the
# thing it encrypts, which is a 0600 file with extra steps. Type it once per
# container start. If you want a non-interactive credential, use an API key —
# `scripts/local-dev/mint-api-key.sh` needs no keyring.
#
# Idempotent: safe to run repeatedly, and cheap when the keyring is already up.
#
# Run INSIDE the container:
#   docker exec -it agentcore-dev bash -lc 'scripts/local-dev/keyring-init.sh'
#
# The -it matters — this prompts.

set -euo pipefail

BUS_FILE="${HOME}/.cache/dbus-session"
KEYRING_DIR="${HOME}/.local/share/keyrings"

log() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Refuse to run outside the container.
# ---------------------------------------------------------------------------
# On the host this would start a stray bus and unlock a keyring nobody reads,
# and on a desktop it could interfere with the real session keyring.
if [ ! -d /workspace ] || [ "$(id -un)" != "dev" ]; then
    log "error: run this inside the devcontainer, as the 'dev' user."
    log "       docker exec -it agentcore-dev bash -lc 'scripts/local-dev/keyring-init.sh'"
    exit 2
fi

if ! command -v gnome-keyring-daemon >/dev/null 2>&1; then
    log "error: gnome-keyring is not installed in this image."
    log "       Rebuild the devcontainer: it is added in .devcontainer/Dockerfile."
    exit 1
fi

mkdir -p "$(dirname "${BUS_FILE}")" "${KEYRING_DIR}"

# ---------------------------------------------------------------------------
# 1. Session bus
# ---------------------------------------------------------------------------
# Reuse the recorded bus only if it actually answers. A stale address is worse
# than none: `keyring` then talks to a socket with nothing behind it and reports
# a missing backend, which reads as "the keyring is not installed".
#
# Statting the socket is NOT sufficient, and this is the bug that made a
# container restart look like a broken image: the socket *file* in /tmp survives
# the restart while the daemon that owned it does not, so `[ -S "$socket" ]`
# passes on a bus that is dead. Ask the bus a question instead.
bus_is_live() {
    [ -r "${BUS_FILE}" ] || return 1
    # shellcheck disable=SC1090
    . "${BUS_FILE}"
    [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] || return 1
    export DBUS_SESSION_BUS_ADDRESS
    dbus-send --session --dest=org.freedesktop.DBus --print-reply \
        --reply-timeout=2000 / org.freedesktop.DBus.ListNames >/dev/null 2>&1
}

if bus_is_live; then
    log "D-Bus session already running."
else
    # Drop the stale record first so a failure here cannot leave a dead address
    # behind for the next run to trust.
    rm -f "${BUS_FILE}"
    eval "$(dbus-launch --sh-syntax)"
    printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' "${DBUS_SESSION_BUS_ADDRESS}" > "${BUS_FILE}"
    chmod 600 "${BUS_FILE}"
    log "Started a D-Bus session."
fi

# shellcheck disable=SC1090
. "${BUS_FILE}"
export DBUS_SESSION_BUS_ADDRESS

# ---------------------------------------------------------------------------
# 2. Unlocked keyring
# ---------------------------------------------------------------------------
# `python -c 'import keyring'` is the honest probe: it asks the same question
# the TUI asks, rather than inferring from a running process.
keyring_works() {
    (cd /workspace/tui && uv run python -c "
import sys
import keyring
from keyring.backends import fail
if isinstance(keyring.get_keyring(), fail.Keyring):
    sys.exit(1)
keyring.set_password('agentcore-keyring-probe', 'probe', 'x')
keyring.delete_password('agentcore-keyring-probe', 'probe')
" >/dev/null 2>&1)
}

if keyring_works; then
    log "Keyring already unlocked and writable. Nothing to do."
    exit 0
fi

if [ ! -t 0 ]; then
    log "error: the keyring needs a passphrase, and stdin is not a terminal."
    log "       Re-run with a TTY:  docker exec -it agentcore-dev bash -lc '$0'"
    exit 3
fi

if [ -f "${KEYRING_DIR}/login.keyring" ]; then
    log ""
    log "Unlocking the existing keyring."
else
    log ""
    log "Creating a keyring. Choose a passphrase — it encrypts the stored"
    log "session, and is not saved anywhere, so you will type it once per"
    log "container start."
fi

printf 'Keyring passphrase: ' >&2
IFS= read -rs passphrase
printf '\n' >&2

if [ -z "${passphrase}" ]; then
    log "error: empty passphrase; refusing to create an unencrypted keyring."
    exit 2
fi

# Two flags here are load-bearing, and each fails in a way that looks like
# success from inside this script:
#
# `--components=secrets` — with the default component set the daemon starts but
#   never claims `org.freedesktop.secrets` on the bus, so `keyring` finds no
#   Secret Service at all and selects its failing backend.
#
# `--daemonize` — the daemon must outlive this shell. Backgrounding with `&`
#   is NOT enough: the process dies with the `docker exec` that started it, and
#   because `org.freedesktop.secrets` is a D-Bus *activatable* service, the next
#   client request makes dbus-daemon spawn a replacement — one that was never
#   given a passphrase, so its collection is locked. The symptom is this script
#   reporting "Keyring is up" and the very next `docker exec` failing with
#   `KeyringLocked`. Do not swap this for `setsid`, which detaches the process
#   before it has read the passphrase from the pipe and leaves it locked.
#
# `--replace` takes the bus name over from any such locked squatter.
printf '%s' "${passphrase}" | gnome-keyring-daemon --unlock --replace --components=secrets --daemonize >/dev/null 2>&1
unset passphrase
sleep 1

if keyring_works; then
    log ""
    log "Keyring is up. Now sign in:"
    log "  docker exec -it agentcore-dev bash -lc 'cd /workspace/tui && uv run agentcore-tui login --sso --base-url <your-host>/api'"
else
    log ""
    log "error: the keyring did not come up."
    log "       A wrong passphrase for an existing keyring is the usual cause."
    log "       To start over (this destroys stored credentials):"
    log "         rm -rf ${KEYRING_DIR} && rm -f ${BUS_FILE}"
    exit 1
fi
