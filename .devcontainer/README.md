# AgentCore Public Stack — Development Container

A reproducible, security-hardened Docker image with every toolchain needed to
build, test, lint, deploy, and end-to-end-test every stack in this monorepo.

## What's inside

| Tool                         | Version    | Pin type                              |
|------------------------------|------------|---------------------------------------|
| Ubuntu (base)                | 24.04 LTS  | Multi-arch sha256 image-index digest  |
| Python                       | 3.13       | Managed by uv (lockfile-driven)       |
| uv (Python pkg manager)      | 0.7.12     | sha256-pinned ghcr.io image           |
| Node.js                      | 22.22.3    | sha256-verified upstream tarball      |
| npm                          | 11.2.0     | Matches `frontend/ai.client/package.json` |
| AWS CLI                      | 2.34.40    | sha256 + PGP signature verified       |
| AWS CDK CLI                  | 2.1120.0   | Matches `infrastructure/package.json` |
| Docker CLI (client only)     | 29.4.3     | sha256-verified static binary         |
| Playwright chromium runtime  | n/a        | Apt deps for Playwright 1.59.x        |

> All artifacts downloaded over the network during the build are verified
> against either a pinned sha256 or a PGP signature. Apt packages installed
> from the Ubuntu repos are not individually version-pinned but are frozen
> by the base image digest, matching the convention used by
> `backend/Dockerfile.app-api` and `backend/Dockerfile.inference-api`.

## Building

From the repo root:

```bash
docker build \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:latest \
    .
```

Cross-platform (BuildKit + buildx):

```bash
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:latest \
    .
```

### Overriding pinned versions

Every pinned version and digest is exposed as a `--build-arg`. Example:

```bash
docker build \
    --build-arg NODE_VERSION=22.23.0 \
    --build-arg NODE_SHA256_AMD64=<new-sha> \
    --build-arg NODE_SHA256_ARM64=<new-sha> \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:dev \
    .
```

Always update both architecture SHAs together.

### Matching your host's docker GID

If `getent group docker` on your host reports a GID other than `999`, pass it
in so the in-container `dev` user can read `/var/run/docker.sock`:

```bash
docker build \
    --build-arg DOCKER_GID="$(getent group docker | cut -d: -f3)" \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:latest \
    .
```

## Running

### Quick start — interactive shell

```bash
docker run --rm -it \
    -v "$(pwd)":/workspace \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -p 4200:4200 -p 8000:8000 -p 8001:8001 \
    agentcore-devcontainer:latest
```

The repository is bind-mounted at `/workspace`. Files written from inside the
container are owned by UID 1000, which matches the default first user on most
Linux desktops.

### Docker Compose

A minimal `docker-compose.yml` to put alongside this Dockerfile:

```yaml
services:
  dev:
    build:
      context: ..
      dockerfile: .devcontainer/Dockerfile
    image: agentcore-devcontainer:latest
    volumes:
      - ..:/workspace
      - /var/run/docker.sock:/var/run/docker.sock
    ports:
      - "4200:4200"   # Angular dev server
      - "8000:8000"   # App API
      - "8001:8001"   # Inference API
    working_dir: /workspace
    command: sleep infinity
```

Start it with `docker compose -f .devcontainer/docker-compose.yml up -d`,
then `docker compose exec dev bash`.

### VS Code Dev Containers

The included `devcontainer.json` is recognized by the
[Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).
Open the repo and run **Reopen in Container** from the command palette.

## Verifying everything works

Inside the container:

```bash
# All toolchains resolve and report versions
node --version && npm --version
uv --version && uv python list
aws --version && cdk --version && docker --version

# Backend — Python tests
cd /workspace/backend
uv sync --frozen --extra agentcore --extra dev
uv run pytest tests/ -v --tb=short

# Backend — lint and type check
uv run ruff check src/
uv run black --check src/
uv run mypy src/

# Frontend — install, build, unit tests
cd /workspace/frontend/ai.client
npm ci
npm run build
CI=true npm run test:ci

# Frontend — Playwright (chromium only; runtime libs already present)
npx playwright install chromium    # browser binaries; system deps pre-baked
npx playwright test --project=chromium

# Infrastructure — CDK synth
cd /workspace/infrastructure
npm ci
cdk synth --all
```

Wrapper scripts under `scripts/stack-*/` work unchanged inside the container.

## Docker-in-Docker notes

The Docker daemon is **not** included in this image. The Docker CLI binary
talks to whatever daemon is exposed via `/var/run/docker.sock`. When you run
a script like `bash scripts/stack-app-api/build.sh`, it shells out to
`docker build` against the host daemon.

The host daemon resolves build contexts using **host filesystem paths**, not
container paths. If you bind-mount your repo at `/workspace` inside the
container but it lives at `/home/you/code/agentcore-public-stack` on the
host, `docker build` will look for `/workspace/...` on the host and fail.

Two ways to fix this:

1. **Mount the repo at the same path inside and outside the container.**
   For example, mount `~/code` to `/home/you/code` rather than `/workspace`.
2. **Use `docker buildx` with a remote builder** that doesn't depend on
   shared host paths.

This Dockerfile only provides the CLI; the path-alignment decision is the
caller's.

## Files in this directory

| File                        | Purpose                                                  |
|-----------------------------|----------------------------------------------------------|
| `Dockerfile`                | The dev container image definition.                      |
| `aws-cli-public-key.gpg`    | AWS CLI Team PGP public key (for installer signature).   |
| `devcontainer.json`         | VS Code Dev Containers configuration.                    |
| `.dockerignore`             | Build-context filter to keep image builds fast.          |
| `README.md`                 | This file.                                               |

## Upgrading

When bumping any pinned tool:

1. Find the new sha256 / digest from the upstream release page (Node.js
   `SHASUMS256.txt`, `download.docker.com/linux/static/stable/`,
   `awscli.amazonaws.com`, ghcr.io image registry).
2. Update the corresponding `ARG` in `Dockerfile`. Update both `_AMD64` and
   `_ARM64` SHAs together.
3. Update the version table in this README.
4. Build for both architectures (`docker buildx build --platform
   linux/amd64,linux/arm64 ...`) and run the verification commands above.
5. Commit the version-bump and SHA changes together in one commit.

The AWS CLI Team PGP key in `aws-cli-public-key.gpg` is valid until
2026-07-07. After that date, refresh it from the
[AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html#getting-started-install-instructions).
