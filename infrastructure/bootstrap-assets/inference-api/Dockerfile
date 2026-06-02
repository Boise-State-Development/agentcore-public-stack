# Bootstrap container image for the AgentCore Runtime (inference-api).
#
# This is the placeholder image that PlatformStack ships into the
# AgentCore Runtime on its very first deploy. Same-cadence story as
# bootstrap-assets/rag-ingestion/Dockerfile and
# bootstrap-assets/artifact-render/handler.py — see those files
# for the full rationale.
#
# CDK ships this image via `ecr_assets.DockerImageAsset` to the
# cdk-assets ECR repo; the workflow then ships the real image from
# the project's {prefix}-inference-api repo via
# `aws bedrock-agentcore-control update-agent-runtime`.
#
# Pinning rationale:
#   python:3.13-slim digest-pinned identical to the real
#   backend/Dockerfile.inference-api so the supply-chain
#   dockerfile-pinning test passes.
#
# Architecture: linux/arm64 — the AgentCore Runtime is provisioned
# on ARM64 (cheaper); the real image is also ARM64. CDK's
# DockerImageAsset is given `platform: Platform.LINUX_ARM64` to
# match.

FROM --platform=linux/arm64 python:3.13-slim@sha256:739e7213785e88c0f702dcdc12c0973afcbd606dbf021a589cab77d6b00b579d

WORKDIR /app
COPY handler.py /app/handler.py

EXPOSE 8080

# stdlib only — no pip install needed.
CMD ["python", "/app/handler.py"]
