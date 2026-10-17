#!/usr/bin/env bash
#============================================================
# ecr-public-login.sh — log Docker in to ECR Public (public.ecr.aws)
# so base-image pulls count against the AWS account, not the runner's IP.
#
# Usage:
#   ecr-public-login.sh
#
# WHY
# ---
# Every Dockerfile here pulls its base image from public.ecr.aws
# (python:3.13-slim via the Docker Official Images mirror, the Lambda
# Python base images). ECR Public caps ANONYMOUS pulls by data per
# source IP, and GitHub-hosted runners share IPs heavily — the x86 pool
# most of all. On 2026-10-17 the 1.28.2 prod Backend Deploy failed
# "Build app-api image" twice with `429 Too Many Requests —
# toomanyrequests: Data limit exceeded` on the manifest GET, while the
# same digest built fine on the arm64 runners. An authenticated pull
# is metered per account, with a far higher allowance.
#
# ECR Public's auth endpoint lives only in us-east-1, whatever region
# the stack deploys to. The caller needs `ecr-public:GetAuthorizationToken`
# and `sts:GetServiceBearerToken`.
#
# BEST-EFFORT ON PURPOSE
# ----------------------
# A failed login warns and exits 0: the pull falls back to anonymous,
# which is exactly how builds ran before this script existed. A
# deployment whose CI role lacks the two permissions above keeps
# building (and keeps the old exposure to the anonymous cap) rather
# than breaking on an upgrade.
#
# All output goes to stderr. build-and-push-if-changed.sh's stdout is
# the image tag its caller captures, so a stray "Login Succeeded" there
# would corrupt the tag.
#============================================================
set -uo pipefail

REGISTRY="public.ecr.aws"
AUTH_REGION="us-east-1"

if ! command -v docker >/dev/null 2>&1; then
    echo "[ecr-public-login] docker not found — skipping ${REGISTRY} login." >&2
    exit 0
fi

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

# stderr goes to a file, not into $password: a CLI warning on a
# successful call would otherwise become part of the token.
if ! password="$(aws ecr-public get-login-password --region "$AUTH_REGION" 2>"$err_file")"; then
    echo "[ecr-public-login] WARNING: could not get an ${REGISTRY} token ($(tr '\n' ' ' <"$err_file")). Base-image pulls stay anonymous and can hit ECR Public's per-IP data limit (429 toomanyrequests). Grant the CI role ecr-public:GetAuthorizationToken and sts:GetServiceBearerToken to fix." >&2
    exit 0
fi

if ! printf '%s' "$password" | docker login --username AWS --password-stdin "$REGISTRY" >&2; then
    echo "[ecr-public-login] WARNING: docker login to ${REGISTRY} failed. Base-image pulls stay anonymous." >&2
    exit 0
fi

echo "[ecr-public-login] Logged in to ${REGISTRY}; base-image pulls are authenticated." >&2
