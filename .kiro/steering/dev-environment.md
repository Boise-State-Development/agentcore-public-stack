---
inclusion: manual
---
# Dev Environment — MANDATORY Container Execution Rules

All runtime operations MUST execute inside the Docker container. The host machine is ONLY for reading and writing code files via Kiro.

## Container Details

- **Container name**: `devcontainer-dev-1`
- **Image**: `devcontainer-dev` (Ubuntu Noble base)
- **Compose file**: `~/Github/devcontainer/docker-compose.yml`
- **Service name**: `dev`
- **User**: `colin` (UID 1000, non-root with passwordless sudo)

## Volume Mounts

- `/home/colin/Github` → `/workspace` (full Github folder)
- Docker socket passthrough for Docker-in-Docker

## Exposed Ports

- `4200` → `4200` (Angular dev server)
- `8000` → `8000` (App API)
- `8001` → `8001` (Inference API)

## Installed Tools

Node.js LTS, npm, Angular CLI, AWS CLI v2, AWS CDK, .NET SDK 8.0, GitHub CLI (gh), Python 3, git, build-essential

## What MUST run inside the container

- Syntax checking and compilation (`python3 -c`, `tsc`, `ng build`, `npm run build`)
- Running tests (`pytest`, `vitest`, `npm test`)
- Installing dependencies (`uv sync`, `npm install`)
- Linting and formatting (`ruff`, `black`, `eslint`, `prettier`)
- Type checking (`mypy`, `tsc --noEmit`)
- AWS CLI commands (`aws`, `cdk synth`, `cdk deploy`, `cdk diff`)
- Running scripts (`python3 backend/scripts/...`, `bash scripts/...`)
- Package management (`uv`, `npm`, `npx`)
- Git operations (`git`, `gh`)
- Docker builds (`docker build`)
- Any command that imports project dependencies or executes project code

## What runs on the host (NO container)

- Reading and writing files (Kiro file tools)
- `docker compose` commands that manage the container itself

## Command format

```bash
docker exec -it devcontainer-dev-1 bash -c "<command>"
```

## Workspace path inside container

The host `~/Github` is mounted at `/workspace` inside the container. The repo lives at `/workspace/agentcore-public-stack/` (or wherever it sits under `~/Github`).

## NO EXCEPTIONS

Do NOT run `python3`, `uv`, `npm`, `node`, `pytest`, `ruff`, `black`, `mypy`, `aws`, `cdk`, or any build/test/lint/deploy command directly on the host. Always use `docker exec -it devcontainer-dev-1 bash -c "..."`. If a command fails inside the container due to missing dependencies, install them inside the container first.
