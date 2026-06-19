# AgentCore Public Stack — Development Container

A reproducible, security-hardened Docker image with every toolchain needed to
build, test, lint, deploy, and end-to-end-test every stack in this monorepo.

For agent execution rules and the workspace-path map, see
[`.kiro/steering/dev-environment.md`](../.kiro/steering/dev-environment.md).

## What's inside

| Tool                         | Version    | Pin type                                  |
|------------------------------|------------|-------------------------------------------|
| Ubuntu (base)                | 24.04 LTS  | Multi-arch sha256 image-index digest      |
| Python                       | 3.13       | Managed by uv (lockfile-driven)           |
| uv (Python pkg manager)      | 0.7.12     | sha256-pinned ghcr.io image               |
| Node.js                      | 22.22.3    | sha256-verified upstream tarball          |
| npm                          | 11.2.0     | Matches `frontend/ai.client/package.json` |
| AWS CLI                      | 2.34.40    | sha256 + PGP signature verified           |
| AWS CDK CLI                  | 2.1128.0   | Matches `infrastructure/package.json`     |
| Docker CLI (client only)     | 29.4.3     | sha256-verified static binary             |
| Docker buildx (CLI plugin)   | 0.30.1     | sha256-verified GitHub release            |
| Playwright chromium runtime  | n/a        | Apt deps for Playwright 1.59.x            |

> All artifacts downloaded over the network during the build are verified
> against either a pinned sha256 or a PGP signature. Apt packages installed
> from the Ubuntu repos are not individually version-pinned but are frozen
> by the base image digest, matching the convention used by
> `backend/Dockerfile.app-api` and `backend/Dockerfile.inference-api`.

## Building

From the repo root:

```bash
docker build \
    --build-arg DOCKER_GID="$(getent group docker | cut -d: -f3)" \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:latest \
    .
```

The `DOCKER_GID` build-arg ensures the in-container `docker` group matches
your host's, so `docker build` and friends work from inside the container.
See **The Docker GID Gotcha** below for why this matters.

Cross-platform (BuildKit + buildx):

```bash
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --build-arg DOCKER_GID="$(getent group docker | cut -d: -f3)" \
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

## Running

### Long-lived shell — recommended

Start once, `docker exec` into it many times. Project deps installed by
`uv sync` and `npm ci` cache between runs:

```bash
docker rm -f agentcore-dev 2>/dev/null || true
docker run -d \
    --name agentcore-dev \
    --group-add "$(getent group docker | cut -d: -f3)" \
    -v "$(pwd)":/workspace \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -p 4200:4200 -p 8000:8000 -p 8001:8001 \
    -w /workspace \
    agentcore-devcontainer:latest \
    sleep infinity

docker exec -it agentcore-dev bash
```

### One-shot

```bash
docker run --rm -it \
    --group-add "$(getent group docker | cut -d: -f3)" \
    -v "$(pwd)":/workspace \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -p 4200:4200 -p 8000:8000 -p 8001:8001 \
    agentcore-devcontainer:latest
```

## The Docker GID Gotcha

The Dockerfile bakes its internal `docker` group at GID **999** by default —
the standard Debian/Ubuntu value. Whether the in-container `dev` user can
read the bind-mounted `/var/run/docker.sock` depends on what GID the **host**
uses for its docker group:

| Host                                 | Host docker GID  | What to do                                                |
|--------------------------------------|------------------|-----------------------------------------------------------|
| Native Linux (most distros)          | 999              | Nothing                                                   |
| WSL2 with Docker Desktop             | 1001             | `--group-add 1001` at run time, **or** rebuild with `--build-arg DOCKER_GID=1001` |
| Other                                | varies           | `getent group docker \| cut -d: -f3` to find yours        |

The `$(getent group docker | cut -d: -f3)` shell expression in the build and
run commands above auto-resolves the right value on any Linux host, so you
don't have to hard-code anything.

Symptom when the GID is wrong: any command that opens the docker daemon
socket — `docker build`, `docker push`, `docker ps`, `docker run` — fails
with `permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`. Pure-CLI ops like `docker --version` still
work because they don't touch the socket.

This **only** affects workflows that drive the Docker daemon from inside the
container — building/pushing the project's own service images. Python,
Node, AWS, and CDK workflows don't care.

## Verifying everything works

Inside the container:

```bash
# All toolchains resolve and report versions
node --version && npm --version
uv --version && uv python list --only-installed
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
talks to whatever daemon is exposed via the bind-mounted
`/var/run/docker.sock`. When you run a script like
`bash scripts/stack-app-api/build.sh`, it shells out to `docker build` against
the host daemon.

The host daemon resolves build contexts using **host filesystem paths**, not
container paths. Builds initiated from inside the dev container still work
because the Docker CLI streams the build context as a tarball over the
socket — the daemon doesn't need to read the host filesystem at the
in-container path. Keep this in mind if you ever use `RUN --mount=type=bind`
in a Dockerfile, which DOES require host-side paths.

## Files in this directory

| File                        | Purpose                                                  |
|-----------------------------|----------------------------------------------------------|
| `Dockerfile`                | The dev container image definition.                      |
| `aws-cli-public-key.gpg`    | AWS CLI Team PGP public key (for installer signature).   |
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
