#!/usr/bin/env bash
# Launch the TUI in the dev container. RUN THIS ON THE HOST, not inside the
# container (unlike the other scripts in this directory).
#
#   scripts/local-dev/tui.sh              # chat
#   scripts/local-dev/tui.sh status       # non-interactive checks
#
# Docker's detach key sequence collides with the app's own bindings, so detach
# is remapped to NUL (`ctrl-@`), which nothing sends in practice.
set -euo pipefail

CONTAINER="${AGENTCORE_DEV_CONTAINER:-agentcore-dev}"
# Override if you genuinely need Docker's detach sequence back.
DETACH_KEYS="${AGENTCORE_DETACH_KEYS:-ctrl-@}"

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "[ERROR] container '${CONTAINER}' is not running." >&2
    echo "        Start it with the docker run command in tui/README.md," >&2
    echo "        or set AGENTCORE_DEV_CONTAINER to the right name." >&2
    exit 1
fi

# -t only when stdout is a terminal, so `tui.sh status | tee` still works.
tty_flags=(-i)
if [ -t 1 ]; then
    tty_flags=(-i -t)
fi

exec docker exec "${tty_flags[@]}" \
    --detach-keys "${DETACH_KEYS}" \
    -e TERM="${TERM:-xterm-256color}" \
    -e COLORTERM="${COLORTERM:-truecolor}" \
    "${CONTAINER}" \
    bash -lc 'cd /workspace && exec scripts/local-dev/run-tui.sh "$@"' _ "$@"
