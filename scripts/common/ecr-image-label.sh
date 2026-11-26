#!/usr/bin/env bash
#============================================================
# ecr-image-label.sh — print one label of an image in ECR, without pulling it.
#
# Usage: ecr-image-label.sh <image-uri> <label-key>
#   <image-uri>  REGISTRY/REPO:TAG or REGISTRY/REPO@sha256:...
#
# Prints the label's value (an empty line when the image has no such label)
# and exits 0. Exits 1, with the reason on stderr, when the image cannot be
# read: not an ECR URI, no ecr:BatchGetImage / ecr:GetDownloadUrlForLayer on
# the repository, or the image is gone. Callers decide what an unreadable
# image means; "no label" and "could not look" are different answers.
#
# Reads the image config blob (a few KB), resolving a multi-arch index to
# arm64 first, which is what the AgentCore Runtime runs.
#============================================================
set -euo pipefail

IMAGE_URI="${1:?usage: ecr-image-label.sh <image-uri> <label-key>}"
LABEL_KEY="${2:?usage: ecr-image-label.sh <image-uri> <label-key>}"

ECR_URI_REGEX='^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/(.+)$'
if [[ ! "$IMAGE_URI" =~ $ECR_URI_REGEX ]]; then
    echo "ecr-image-label: '${IMAGE_URI}' is not an ECR image URI" >&2
    exit 1
fi
REGISTRY_ID="${BASH_REMATCH[1]}"
REGION="${BASH_REMATCH[2]}"
REPO_PATH="${BASH_REMATCH[3]}"
if [[ "$REPO_PATH" == *@* ]]; then
    REPO="${REPO_PATH%@*}"; IMAGE_ID="imageDigest=${REPO_PATH#*@}"
elif [[ "$REPO_PATH" == *:* ]]; then
    REPO="${REPO_PATH%:*}"; IMAGE_ID="imageTag=${REPO_PATH##*:}"
else
    echo "ecr-image-label: '${IMAGE_URI}' names no tag or digest" >&2
    exit 1
fi

ACCEPT="application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json application/vnd.oci.image.manifest.v1+json application/vnd.docker.distribution.manifest.v2+json"
get_manifest() {
    # shellcheck disable=SC2086
    aws ecr batch-get-image --region "$REGION" --registry-id "$REGISTRY_ID" --repository-name "$REPO" \
        --image-ids "$1" --accepted-media-types $ACCEPT --query 'images[0].imageManifest' --output text
}

MANIFEST="$(get_manifest "$IMAGE_ID")" || { echo "ecr-image-label: could not read the manifest of ${IMAGE_URI}" >&2; exit 1; }
if [[ -z "$MANIFEST" || "$MANIFEST" == "None" ]]; then
    echo "ecr-image-label: ${IMAGE_URI} is not in ECR" >&2
    exit 1
fi
CHILD="$(python3 -c '
import json, sys
m = json.loads(sys.stdin.read())
if "manifests" in m:
    arm = [d for d in m["manifests"] if d.get("platform", {}).get("architecture") == "arm64"]
    print((arm or m["manifests"])[0]["digest"])
' <<<"$MANIFEST")" || { echo "ecr-image-label: unreadable manifest for ${IMAGE_URI}" >&2; exit 1; }
if [[ -n "$CHILD" ]]; then
    MANIFEST="$(get_manifest "imageDigest=${CHILD}")" || { echo "ecr-image-label: could not read the arm64 manifest of ${IMAGE_URI}" >&2; exit 1; }
fi
CONFIG_DIGEST="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["config"]["digest"])' <<<"$MANIFEST")" \
    || { echo "ecr-image-label: no config digest in the manifest of ${IMAGE_URI}" >&2; exit 1; }
URL="$(aws ecr get-download-url-for-layer --region "$REGION" --registry-id "$REGISTRY_ID" --repository-name "$REPO" \
    --layer-digest "$CONFIG_DIGEST" --query downloadUrl --output text)" \
    || { echo "ecr-image-label: could not fetch the image config of ${IMAGE_URI}" >&2; exit 1; }
CONFIG="$(curl -fsSL "$URL")" || { echo "ecr-image-label: could not download the image config of ${IMAGE_URI}" >&2; exit 1; }
LABEL_KEY="$LABEL_KEY" python3 -c '
import json, os, sys
labels = (json.loads(sys.stdin.read()).get("config") or {}).get("Labels") or {}
print(labels.get(os.environ["LABEL_KEY"], ""))
' <<<"$CONFIG"
