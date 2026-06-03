# Bootstrap container image for the App API ECS Fargate task.
#
# Same-cadence story as bootstrap-assets/inference-api/Dockerfile —
# CDK ships this image at synth time via DockerImageAsset; the
# backend workflow ships the real image from the project's
# {prefix}-app-api ECR repo via `aws ecs register-task-definition`
# + `aws ecs update-service`.
#
# Pinning rationale:
#   python:3.13-slim digest-pinned identical to the real
#   backend/Dockerfile.app-api so the supply-chain
#   dockerfile-pinning test passes.
#
# Architecture: linux/amd64 — App API runs on x86_64 Fargate
# (default); the real image is also amd64.

FROM --platform=linux/amd64 python:3.13-slim@sha256:739e7213785e88c0f702dcdc12c0973afcbd606dbf021a589cab77d6b00b579d

WORKDIR /app
COPY handler.py /app/handler.py

EXPOSE 8000

# stdlib only — no pip install needed.
CMD ["python", "/app/handler.py"]
