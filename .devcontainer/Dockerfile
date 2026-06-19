# syntax=docker/dockerfile:1.7
# =============================================================================
# AgentCore Public Stack — Development Container
# =============================================================================
#
# A reproducible, security-hardened development image with everything required
# to build, test, lint, deploy, and end-to-end-test every stack in this repo:
#
#   • Backend  — Python 3.13 (managed by uv) + uv 0.7.12, pytest, ruff, mypy, black
#   • Frontend — Node.js 22 LTS + npm 11.2.0 + Angular CLI (project-local), Vitest
#   • E2E      — Playwright 1.59.x runtime libs for chromium (browsers installed at runtime)
#   • Infra    — AWS CDK 2.1128.0 (global) + project-local aws-cdk-lib via npm ci
#   • Cloud    — AWS CLI v2.34.40 (PGP-verified) + Docker CLI 29.4.3 (static)
#
# Reproducibility posture (matches existing backend/Dockerfile.* conventions):
#   • Base image is pinned by sha256 multi-arch index digest.
#   • Every artifact downloaded from the network is verified against a sha256
#     embedded in this file as a build ARG, OR via PGP signature (AWS CLI).
#   • All language-level dependencies are installed from lock files
#     (uv.lock, package-lock.json) elsewhere; this image only provides toolchains.
#   • System packages installed via apt are not version-pinned individually,
#     but their snapshot is fixed by the base image digest. This mirrors the
#     posture of the existing production Dockerfiles in backend/.
#
# Multi-arch:
#   • Builds for both linux/amd64 and linux/arm64 via TARGETARCH selection.
#
# Build:
#   docker build -f .devcontainer/Dockerfile -t agentcore-devcontainer:latest .
#
# Run (bind-mount this repo at /workspace):
#   docker run --rm -it \
#     -v "$(pwd)":/workspace \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -p 4200:4200 -p 8000:8000 -p 8001:8001 \
#     agentcore-devcontainer:latest
#
# See .devcontainer/README.md for full usage.
# =============================================================================

# -----------------------------------------------------------------------------
# Base — Ubuntu 24.04 LTS (Noble Numbat). Matches the ubuntu-24.04 GitHub
# Actions runner used by every workflow under .github/workflows/.
# Digest is the multi-arch OCI image index, so amd64 and arm64 both resolve.
# -----------------------------------------------------------------------------
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b

# -----------------------------------------------------------------------------
# Build arguments — every pinned version and sha is exposed for visibility
# and easy upgrades. Override at build time with --build-arg if needed.
# -----------------------------------------------------------------------------

# Node.js 22 LTS (matches actions/setup-node node-version: '22' in CI).
ARG NODE_VERSION=22.22.3
ARG NODE_SHA256_AMD64=2e5d13569282d016861fae7c8f935e741693c269101a5bebcf761a5376d1f99f
ARG NODE_SHA256_ARM64=1c4a9933a5e45bc88f54f70b5f91232c127ec49f1a5989d23fb85824c7adf9b7

# npm — pinned to match frontend/ai.client/package.json "packageManager".
ARG NPM_VERSION=11.2.0

# AWS CLI v2 — pinned exact version, sha256-verified, AND PGP-verified
# against the AWS CLI Team public key (Key ID A6310ACC4672475C).
ARG AWS_CLI_VERSION=2.34.40
ARG AWS_CLI_SHA256_AMD64=cb73df4bb78f330c9404744c14b386768de386f197612d76684124f7fe455ed2
ARG AWS_CLI_SHA256_ARM64=711f1d4ed41ad8eea13ab99548f10ee7dcc1023b4ae5d46845ce6bf2481d6643

# Docker CLI (static binary). The daemon is provided by the host via socket
# bind-mount; this image only ships the client.
ARG DOCKER_VERSION=29.4.3
ARG DOCKER_SHA256_AMD64=bc9734a89d3edd15eeca8620961f6499ba69948814c85d7ac3488e34b3e16d01
ARG DOCKER_SHA256_ARM64=fdde3dbedbd8dc9e9e54a3172e682b4e69e740296e580542ea9ceacebbbf4f4c

# docker-buildx CLI plugin — required for Buildkit features such as
# `RUN --mount=type=cache` used by backend/Dockerfile.app-api and friends.
ARG BUILDX_VERSION=0.30.1
ARG BUILDX_SHA256_AMD64=c37114fcd034025ec68e224657c8a5a850df472ded3ddcbca75ad3a7ebb9710d
ARG BUILDX_SHA256_ARM64=31d012d52d6df68aef4b55db62330967b562811f0de30cdfaa4505f314797c76

# uv — Python package and toolchain manager. Pinned to match
# backend/Dockerfile.app-api and backend/Dockerfile.inference-api.
ARG UV_VERSION=0.7.12

# Python interpreter version managed by uv. Matches actions/setup-python
# python-version: '3.13' in CI and backend/pyproject.toml's targeted runtime.
ARG PYTHON_VERSION=3.13

# AWS CDK CLI — global install. Pinned to match
# infrastructure/package.json devDependencies.aws-cdk so cdk synth/deploy
# behaves identically to a fresh `npm ci` in infrastructure/.
ARG CDK_VERSION=2.1128.0

# Non-root developer user. UID/GID 1000 matches the default first user on
# most Linux desktops, so bind-mounted files retain correct ownership.
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000

# Docker host group GID — used so the in-container user can read the bind-
# mounted /var/run/docker.sock. Override to match `getent group docker` on
# your host if it differs from the Ubuntu/Debian default.
ARG DOCKER_GID=999

# -----------------------------------------------------------------------------
# Standard environment
# -----------------------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_ENV=development \
    # uv: install Python toolchains and venvs in a system-wide location so
    # they're shared between the build user and the runtime user.
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_TOOL_DIR=/opt/uv/tools \
    UV_LINK_MODE=copy \
    # Playwright: cache browsers in a system-wide location for the same reason.
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# -----------------------------------------------------------------------------
# System packages
#
# Installed in two passes for readability — the layers cost is the same
# because they share an `&&` chain. Versions are not individually pinned;
# the snapshot is fixed by the base image digest, matching the convention
# of backend/Dockerfile.app-api and backend/Dockerfile.inference-api.
#
# Pass 1 — Core developer tooling, build chain, locale, networking utils.
# Pass 2 — Playwright 1.59.x runtime libs and fonts for chromium on
#          Ubuntu 24.04, sourced from microsoft/playwright nativeDeps.ts
#          (ubuntu24.04-x64 chromium + tools).
# -----------------------------------------------------------------------------
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg sudo bash less procps \
        git git-lfs jq unzip xz-utils zip make \
        build-essential pkg-config \
        libssl-dev libffi-dev zlib1g-dev \
        locales tzdata \
        vim-tiny nano openssh-client iputils-ping dnsutils; \
    apt-get install -y --no-install-recommends \
        libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
        libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 \
        libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 \
        libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 \
        xvfb fonts-noto-color-emoji fonts-unifont libfontconfig1 \
        libfreetype6 xfonts-cyrillic xfonts-scalable fonts-liberation \
        fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf \
        fonts-freefont-ttf; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*; \
    locale-gen en_US.UTF-8

# -----------------------------------------------------------------------------
# Node.js 22 LTS
#
# Downloaded directly from nodejs.org and verified against the upstream
# SHASUMS256.txt hashes embedded above. Avoids NodeSource and other third-
# party redistributions for a smaller trust surface.
# -----------------------------------------------------------------------------
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) node_arch=x64;   node_sha="${NODE_SHA256_AMD64}" ;; \
        arm64) node_arch=arm64; node_sha="${NODE_SHA256_ARM64}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    echo "${node_sha}  node.tar.xz" | sha256sum -c -; \
    tar -xJf node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm node.tar.xz; \
    # Pin npm to the version declared by the frontend project so npm ci
    # output is bit-identical to local developer machines.
    npm install -g --no-fund --no-audit "npm@${NPM_VERSION}"; \
    node --version; \
    npm --version

# -----------------------------------------------------------------------------
# AWS CLI v2
#
# Both sha256 (integrity) and PGP signature (authenticity) are verified.
# The PGP key is the AWS CLI Team public key (RSA 4096, fingerprint
# FB5D B77F D5C1 18B8 0511 ADA8 A631 0ACC 4672 475C) published at
# https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
# -----------------------------------------------------------------------------
COPY .devcontainer/aws-cli-public-key.gpg /tmp/aws-cli.gpg
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) aws_arch=x86_64;  aws_sha="${AWS_CLI_SHA256_AMD64}" ;; \
        arm64) aws_arch=aarch64; aws_sha="${AWS_CLI_SHA256_ARM64}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWS_CLI_VERSION}.zip"; \
    curl -fsSLo awscliv2.zip.sig "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWS_CLI_VERSION}.zip.sig"; \
    echo "${aws_sha}  awscliv2.zip" | sha256sum -c -; \
    export GNUPGHOME="$(mktemp -d)"; \
    gpg --import /tmp/aws-cli.gpg; \
    gpg --batch --verify awscliv2.zip.sig awscliv2.zip; \
    unzip -q awscliv2.zip; \
    ./aws/install -i /usr/local/aws-cli -b /usr/local/bin; \
    rm -rf "${GNUPGHOME}" awscliv2.zip awscliv2.zip.sig aws /tmp/aws-cli.gpg; \
    aws --version

# -----------------------------------------------------------------------------
# Docker CLI (static binary) + buildx plugin
#
# Only the client. The daemon must be provided at runtime via a bind-mount
# of /var/run/docker.sock — see .devcontainer/README.md.
#
# buildx is required to use BuildKit features (e.g. RUN --mount=type=cache)
# that backend/Dockerfile.app-api and backend/Dockerfile.inference-api rely
# on. It's installed as a CLI plugin under /usr/libexec/docker/cli-plugins/
# so `docker build` automatically routes through it on Docker 23+.
# -----------------------------------------------------------------------------
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) docker_arch=x86_64;  docker_sha="${DOCKER_SHA256_AMD64}"; \
               buildx_arch=amd64;   buildx_sha="${BUILDX_SHA256_AMD64}" ;; \
        arm64) docker_arch=aarch64; docker_sha="${DOCKER_SHA256_ARM64}"; \
               buildx_arch=arm64;   buildx_sha="${BUILDX_SHA256_ARM64}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo docker.tgz "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_VERSION}.tgz"; \
    echo "${docker_sha}  docker.tgz" | sha256sum -c -; \
    tar -xzf docker.tgz; \
    install -m 0755 docker/docker /usr/local/bin/docker; \
    rm -rf docker docker.tgz; \
    install -d -m 0755 /usr/libexec/docker/cli-plugins; \
    curl -fsSLo /usr/libexec/docker/cli-plugins/docker-buildx \
        "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-${buildx_arch}"; \
    echo "${buildx_sha}  /usr/libexec/docker/cli-plugins/docker-buildx" | sha256sum -c -; \
    chmod 0755 /usr/libexec/docker/cli-plugins/docker-buildx; \
    docker --version; \
    docker buildx version

# -----------------------------------------------------------------------------
# uv — Python package and toolchain manager
#
# Copied from the official ghcr.io/astral-sh/uv image, pinned by sha256.
# Identical version and source as backend/Dockerfile.app-api so dev and
# prod environments resolve uv.lock the same way.
# -----------------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:0.7.12@sha256:4faec156e35a5f345d57804d8858c6ba1cf6352ce5f4bffc11b7fdebdef46a38 \
     /uv /uvx /usr/local/bin/

# Pre-fetch the project's targeted Python toolchain so the first
# `uv sync` doesn't need to download it.
RUN set -eux; \
    mkdir -p "${UV_PYTHON_INSTALL_DIR}" "${UV_TOOL_DIR}"; \
    uv python install "${PYTHON_VERSION}"; \
    uv --version

# -----------------------------------------------------------------------------
# Global npm tools — AWS CDK CLI
#
# Pinned to match infrastructure/package.json devDependencies.aws-cdk so
# `cdk synth` from anywhere in the workspace produces the same template
# as a fresh `npm ci` inside infrastructure/.
# -----------------------------------------------------------------------------
RUN npm install -g --no-fund --no-audit \
        "aws-cdk@${CDK_VERSION}" \
 && cdk --version

# -----------------------------------------------------------------------------
# Non-root developer user
#
# • UID 1000 matches the default first user on most Linux desktops so
#   bind-mounted files retain correct ownership.
# • Passwordless sudo so `apt-get install` etc. work for ad-hoc additions.
# • Member of the `docker` group so the user can talk to a bind-mounted
#   /var/run/docker.sock from the host.
# • The Ubuntu 24.04 base image ships with a default `ubuntu` user at UID
#   1000; we delete it before creating ours to avoid UID collisions.
# -----------------------------------------------------------------------------
RUN set -eux; \
    if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu 2>/dev/null || userdel ubuntu; fi; \
    if getent group "${USER_GID}" >/dev/null; then groupdel "$(getent group "${USER_GID}" | cut -d: -f1)"; fi; \
    groupadd --gid "${USER_GID}" "${USERNAME}"; \
    useradd --uid "${USER_UID}" --gid "${USER_GID}" \
            --create-home --shell /bin/bash "${USERNAME}"; \
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${USERNAME}"; \
    chmod 0440 "/etc/sudoers.d/${USERNAME}"; \
    if ! getent group docker >/dev/null; then \
        groupadd --system --gid "${DOCKER_GID}" docker; \
    fi; \
    usermod -aG docker "${USERNAME}"; \
    # Ensure shared tool dirs are writable by the dev user.
    chown -R "${USERNAME}:${USERNAME}" /opt/uv; \
    install -d -m 0755 -o "${USERNAME}" -g "${USERNAME}" "${PLAYWRIGHT_BROWSERS_PATH}"

# -----------------------------------------------------------------------------
# User-level shell setup
# -----------------------------------------------------------------------------
USER ${USERNAME}
WORKDIR /workspace

# Sensible defaults: keep history, expose user-installed binaries on PATH,
# and source repo-local virtualenvs / node_modules ergonomically.
RUN set -eux; \
    mkdir -p "/home/${USERNAME}/.local/bin" "/home/${USERNAME}/.cache"; \
    { \
      echo ''; \
      echo '# --- agentcore devcontainer defaults ---'; \
      echo 'export PATH="$HOME/.local/bin:$PATH"'; \
      echo 'export HISTSIZE=10000 HISTFILESIZE=10000 HISTCONTROL=ignoreboth'; \
      echo 'shopt -s histappend checkwinsize 2>/dev/null || true'; \
      echo '# Use the project-local Angular CLI / Playwright when available.'; \
      echo 'if [ -d "/workspace/frontend/ai.client/node_modules/.bin" ]; then'; \
      echo '    export PATH="/workspace/frontend/ai.client/node_modules/.bin:$PATH"'; \
      echo 'fi'; \
      echo 'if [ -d "/workspace/infrastructure/node_modules/.bin" ]; then'; \
      echo '    export PATH="/workspace/infrastructure/node_modules/.bin:$PATH"'; \
      echo 'fi'; \
      echo '# Auto-activate uv-managed venv if cwd is the backend.'; \
      echo 'if [ -f "/workspace/backend/.venv/bin/activate" ]; then'; \
      echo '    alias backend-activate="source /workspace/backend/.venv/bin/activate"'; \
      echo 'fi'; \
      echo 'export PS1="\\[\\033[01;32m\\]\\u@devcontainer\\[\\033[00m\\]:\\[\\033[01;34m\\]\\w\\[\\033[00m\\]\\$ "'; \
    } >> "/home/${USERNAME}/.bashrc"

# -----------------------------------------------------------------------------
# Image labels
# -----------------------------------------------------------------------------
LABEL org.opencontainers.image.title="agentcore-devcontainer" \
      org.opencontainers.image.description="Reproducible dev environment for the agentcore-public-stack monorepo" \
      org.opencontainers.image.source="https://github.com/Boise-State-Development/agentcore-public-stack" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

# -----------------------------------------------------------------------------
# Health check — quickly verify all toolchains resolve.
# -----------------------------------------------------------------------------
HEALTHCHECK --interval=5m --timeout=15s --start-period=5s --retries=2 \
    CMD bash -c '\
        node --version && \
        npm --version && \
        uv --version && \
        aws --version && \
        cdk --version && \
        docker --version && \
        git --version' >/dev/null 2>&1 || exit 1

# Default to an interactive shell. Override with -- to run a one-off command.
CMD ["/bin/bash"]
