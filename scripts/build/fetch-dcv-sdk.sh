#!/usr/bin/env bash
#
# Fetch and verify the Amazon DCV Web Client SDK for the browser-takeover
# live view (docs/specs/authenticated-web-assessment.md D3).
#
# WHY THIS IS A BUILD STEP AND NOT A VENDORED DIRECTORY
# -----------------------------------------------------
# The SDK is a EULA-licensed AWS download, not an open-source package, and this
# repository is PUBLIC. Committing the archive would be redistribution. AWS's
# own instruction is to "place the extracted directory on your web server", so
# we fetch it at build time and deploy it to the sandbox origin. Nothing
# licensed lands in git, and the signature is checked on every build rather
# than trusted once at commit time.
#
# It is deliberately NOT on npm: the only `dcv` package on the public registry
# is an unrelated third-party Vue component library, and `dcv-ui` does not
# exist at all. `bedrock-agentcore`'s BrowserLiveView React component imports
# both as undeclared dependencies, which is why we do not use it.
#
# WHAT ACTUALLY MAKES THIS SAFE
# ------------------------------
# Not "it's from AWS" — vendors ship compromised artifacts too. Three pins do:
#
#   1. VERSION       — an exact archive, so the bytes cannot change under us.
#   2. ARCHIVE_SHA256 — pinned HERE, deliberately not read from the published
#      .sha256sum: whoever could swap the archive on the CDN could swap that
#      file too. Changing it requires a reviewed diff.
#   3. GPG_FINGERPRINT — pinned for the same reason. Importing the key from
#      the network and trusting whatever it serves *today* is trust-on-first-
#      use on every build, which is verification theater. We import into a
#      throwaway keyring and assert the fingerprint before verifying anything.
#
# ⚠️ The fingerprint below was captured from AWS's published key and pinned at
# review time; AWS does not appear to publish it out-of-band, so it is
# trust-on-first-use ONCE, caught by code review thereafter. If you can obtain
# it through a second channel, do, and note it here.
#
# Residual risk worth stating: dcv.js runs in the viewer page and forwards the
# user's keystrokes to the remote browser, so a compromised SDK could capture a
# password. That is exactly why the page is served from the sandbox origin,
# which holds no session cookie, rather than from the SPA.
#
set -euo pipefail

VERSION="1.13.2-1074"
ARCHIVE="nice-dcv-web-client-sdk-${VERSION}.zip"
BASE_URL="https://d1uj6qtbmh3dt5.cloudfront.net"
ARCHIVE_URL="${BASE_URL}/webclientsdk/${ARCHIVE}"
SIGNATURE_URL="${ARCHIVE_URL}.sign"
GPG_KEY_URL="${BASE_URL}/NICE-GPG-KEY"

ARCHIVE_SHA256="31933c6b692983cfeb7fb2ca47d3a76433e61eaee84feb710aecca17919a4dee"
GPG_FINGERPRINT="5B9EEBC864449701F6CE566A11B5C70A170C6114"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The UMD build: a plain <script> tag, no bundler. The viewer is deliberately
# framework-free, so the ESM build would buy nothing and cost a build step.
#
# Named `dcvjs` to match the SDK's OWN default `baseUrl`, which is the
# relative path it resolves its worker and decoder files from. Matching the
# default means the viewer never has to set baseUrl, which is one fewer
# assumption that can be silently wrong at runtime.
DEST="${REPO_ROOT}/infrastructure/assets/mcp-sandbox/dcvjs"

log() { printf '  %s\n' "$*"; }

# Global, not local to main(): the EXIT trap fires after main() returns, and a
# function-local would be unbound there under `set -u`.
workdir=""
cleanup() { [[ -n "$workdir" ]] && rm -rf "$workdir"; }
trap cleanup EXIT

main() {
  log "Amazon DCV Web Client SDK ${VERSION}"

  for tool in curl shasum unzip; do
    command -v "$tool" >/dev/null || { echo "ERROR: '$tool' is required" >&2; exit 1; }
  done

  workdir="$(mktemp -d)"

  log "Downloading…"
  curl -fsS "$ARCHIVE_URL" -o "${workdir}/${ARCHIVE}"
  curl -fsS "$SIGNATURE_URL" -o "${workdir}/${ARCHIVE}.sign"

  log "Verifying SHA256 against the pinned value…"
  local actual
  actual="$(shasum -a 256 "${workdir}/${ARCHIVE}" | cut -d' ' -f1)"
  if [[ "$actual" != "$ARCHIVE_SHA256" ]]; then
    cat >&2 <<EOF
ERROR: SHA256 mismatch for ${ARCHIVE}
  expected (pinned): ${ARCHIVE_SHA256}
  actual:            ${actual}

Do NOT "fix" this by updating the pin to the value you just got. Either the
upstream artifact was republished under the same version — which AWS should
not do and is worth asking about — or the download was tampered with. Confirm
the new artifact deliberately, then update VERSION and both pins together.
EOF
    exit 1
  fi

  # GPG is the stronger check but is not installed everywhere (macOS ships
  # without it). The SHA256 pin above already fails closed on a swapped
  # artifact, so a missing gpg is a warning locally and an error in CI.
  if command -v gpg >/dev/null; then
    log "Verifying the GPG signature against the pinned fingerprint…"
    export GNUPGHOME="${workdir}/gnupg"
    mkdir -p "$GNUPGHOME"
    chmod 700 "$GNUPGHOME"
    curl -fsS "$GPG_KEY_URL" -o "${workdir}/NICE-GPG-KEY"
    gpg --batch --quiet --import "${workdir}/NICE-GPG-KEY"

    local imported
    imported="$(gpg --batch --with-colons --fingerprint \
      | awk -F: '/^fpr:/ { print $10; exit }')"
    if [[ "$imported" != "$GPG_FINGERPRINT" ]]; then
      cat >&2 <<EOF
ERROR: the signing key served by AWS is not the pinned key.
  expected (pinned): ${GPG_FINGERPRINT}
  served:            ${imported}

This is the check that matters. Stop and establish out-of-band whether AWS
rotated the key before touching the pin.
EOF
      exit 1
    fi
    gpg --batch --quiet --verify "${workdir}/${ARCHIVE}.sign" "${workdir}/${ARCHIVE}"
    log "Signature OK."
  elif [[ -n "${CI:-}" ]]; then
    echo "ERROR: gpg is required in CI and was not found." >&2
    exit 1
  else
    log "WARNING: gpg not found — SHA256 verified, signature NOT checked."
  fi

  log "Extracting to ${DEST#"$REPO_ROOT"/}…"
  rm -rf "$DEST"
  mkdir -p "$DEST"
  unzip -q "${workdir}/${ARCHIVE}" -d "${workdir}/x"
  # "You must retain the folder structure when deploying" — copy the UMD tree
  # wholesale, including EULA.txt and third-party-licenses.txt, which the
  # license requires be served alongside it.
  cp -R "${workdir}/x/nice-dcv-web-client-sdk/dcvjs-umd/." "$DEST/"

  [[ -f "${DEST}/dcv.js" ]] || { echo "ERROR: dcv.js missing after extract" >&2; exit 1; }
  [[ -f "${DEST}/EULA.txt" ]] || { echo "ERROR: EULA.txt missing after extract" >&2; exit 1; }

  log "Done."
}

main "$@"
