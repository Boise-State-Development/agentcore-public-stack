---
inclusion: manual
---
# Dev Environment — Container Execution Rules

This repo ships its own purpose-built dev container. **All build, test, lint,
synth, and deploy operations execute inside that container.** The host (or
the systemd-nspawn jail you may be running in) is for editing files and
orchestrating the dev container, nothing else.

## The Image

- **Image tag**: `agentcore-devcontainer:latest`
- **Built from**: `.devcontainer/Dockerfile`
- **Workspace path inside**: `/workspace`
- **User inside**: `dev` (UID 1000, GID 1000, passwordless sudo)
- **Toolchains**: Python 3.13 (via uv 0.7.12), Node.js 22 LTS, npm 11.2.0,
  AWS CLI 2.34.40, AWS CDK 2.1128.0, Docker CLI 29.4.3 with buildx 0.30.1
  plugin, plus the Playwright chromium runtime libs and fonts. See
  `.devcontainer/README.md` for the full pinned-version table.

If the image is missing, build it:

```bash
docker build \
    --build-arg DOCKER_GID="$(getent group docker | cut -d: -f3)" \
    -f .devcontainer/Dockerfile \
    -t agentcore-devcontainer:latest \
    .
```

## The Docker GID Gotcha (READ THIS)

The Dockerfile creates an internal `docker` group at **GID 999** (Debian/
Ubuntu default), and that's what the `dev` user is a member of. Whether the
`dev` user can use `/var/run/docker.sock` from inside the container depends
on the **host's** docker group GID:

| Host | Host docker GID | Action needed |
|---|---|---|
| Native Linux (Debian/Ubuntu, most distros) | 999 | None |
| WSL2 with Docker Desktop | **1001** | Pass `--group-add 1001` (or rebuild with `--build-arg DOCKER_GID=1001`) |
| Other | varies | `getent group docker \| cut -d: -f3` to find it |

Symptoms when the GID is wrong: any `docker` command that needs the daemon
(`docker build`, `docker push`, `docker ps`, `docker run`, `docker info`)
fails with `permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`. Pure-CLI ops like `docker --version` still
work because they don't open the socket.

This **only** affects workflows that drive `docker build`/`docker push` from
inside the dev container — that is, `scripts/build/build-one.sh`,
`scripts/build/build-all-images.sh`, and any deploy script that invokes them.
Everything else (`pytest`, `ng build`, `cdk synth`, `ruff`, `mypy`,
`playwright test`) doesn't touch the socket and works regardless of the GID.

## Resource Caps (READ THIS)

The dev container **must** be launched with explicit memory/CPU/PID caps.
Without them, a single jest run, ng build, or cdk synth can demand all the
RAM and CPU the WSL2 VM has, swap-thrash the kernel, and freeze the
host machine and Kiro along with it. This has happened. The caps below are
what every launcher in this doc uses; do not omit them.

### Topology on this machine (WSL2 + Docker Desktop)

```
Windows host
└─ WSL2 utility VM            ← capped via C:\Users\<you>\.wslconfig
   ├─ user distro (where this nspawn jail and Kiro live)
   └─ docker-desktop distro   ← runs dockerd
      └─ agentcore-dev container  ← capped via `docker run` flags below
```

Two layers of caps:

1. **Outer (Windows protection) — `.wslconfig`.** Caps the entire WSL2 VM
   so Docker, the user distro, and the nspawn jail combined cannot starve
   Windows. Required values for this machine:

   ```ini
   # C:\Users\<you>\.wslconfig
   [wsl2]
   memory=8GB
   processors=8
   swap=4GB
   autoMemoryReclaim=gradual
   ```

   After editing, `wsl --shutdown` from PowerShell and re-launch.

2. **Inner (Kiro protection) — `docker run` flags.** Inside the WSL2 VM the
   Docker Desktop distro and the nspawn jail compete for the same pool with
   no built-in wall between them, so we add caps on the dev container
   itself. Required flags every time `agentcore-dev` is started:

   | Flag | Value | Why |
   |---|---|---|
   | `--memory=4g` | hard memory cap | half of the WSL2 VM's 8 GB |
   | `--memory-swap=4g` | equal to `--memory` ⇒ no swap | a runaway process OOM-kills itself fast instead of swap-thrashing |
   | `--cpus=4` | half of the WSL2 VM's 8 logical CPUs | leaves ≥4 for Kiro / nspawn / dockerd |
   | `--pids-limit=4096` | fork-bomb safety | cheap insurance |

These numbers assume an 8 GB / 8 CPU `.wslconfig`. If the `.wslconfig`
budget changes, scale both layers proportionally — the dev container should
take roughly half of the WSL2 VM, never more.

## Starting the Dev Container

Long-lived shell — start once, exec into it many times:

```bash
docker rm -f agentcore-dev 2>/dev/null || true
docker run -d \
    --name agentcore-dev \
    --memory=4g \
    --memory-swap=4g \
    --cpus=4 \
    --pids-limit=4096 \
    --group-add "$(getent group docker | cut -d: -f3)" \
    -v /home/colin/agent-workspace/colinmxs/agentcore-public-stack:/workspace \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -p 4200:4200 -p 8000:8000 -p 8001:8001 \
    -w /workspace \
    agentcore-devcontainer:latest \
    sleep infinity
```

> **Important — host vs. nspawn paths.** The `-v` source path must be the
> path as `dockerd` sees it (the WSL host filesystem), NOT the path inside
> the systemd-nspawn jail. From the nspawn the repo looks like
> `/mnt/workspace/colinmxs/agentcore-public-stack`, but `dockerd` lives on
> the WSL host and only sees
> `/home/colin/agent-workspace/colinmxs/agentcore-public-stack`. Always
> pass the host path.

One-shot (rare; prefer the long-lived form above so deps cache between runs):

```bash
docker run --rm \
    --memory=4g \
    --memory-swap=4g \
    --cpus=4 \
    --pids-limit=4096 \
    --group-add "$(getent group docker | cut -d: -f3)" \
    -v /home/colin/agent-workspace/colinmxs/agentcore-public-stack:/workspace \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -w /workspace \
    agentcore-devcontainer:latest \
    bash -c "<command>"
```

## Command Format

Once `agentcore-dev` is running, every operation goes through `docker exec`:

```bash
docker exec agentcore-dev bash -lc "<command>"
```

`-l` so PATH and the project's bashrc adjustments (project-local
`node_modules/.bin`, etc.) are picked up. `-i`/`-t` only when you actually
need a TTY (interactive shells); leave them off for scripted runs.

## What MUST run inside `agentcore-dev`

- Python work — `uv sync`, `uv run pytest`, `ruff`, `black`, `mypy`
- Node work — `npm ci`, `ng build`, `ng test`, `npx playwright test`
- AWS work — `aws ...`, `cdk synth`, `cdk diff`, `cdk deploy`
- Docker work for the project's own images — `docker build -f backend/Dockerfile.app-api ...`,
  `docker push`, etc.
- Anything that imports project dependencies or runs project code
- Repo build/test/deploy wrapper scripts (`scripts/build/`, `scripts/platform/`, `scripts/frontend/`)

## What runs OUTSIDE `agentcore-dev`

- File reads/writes (use Kiro's file tools — they go to `/mnt/workspace/...`
  in the nspawn, which the WSL host bind-mounts)
- `git` operations (commits, branches, pushes — do these from the nspawn,
  not the dev container, so commits land with the host's git config)
- Building, starting, and stopping `agentcore-dev` itself

## Workspace Path Mapping

| Where | Path |
|---|---|
| WSL host (what `dockerd` sees) | `/home/colin/agent-workspace/colinmxs/agentcore-public-stack` |
| systemd-nspawn (where Kiro runs) | `/mnt/workspace/colinmxs/agentcore-public-stack` |
| Inside `agentcore-dev` | `/workspace` |

All three are the same files. Use the path appropriate to whoever's reading
them at that moment.

## NO EXCEPTIONS

Do NOT run `python3`, `uv`, `npm`, `node`, `pytest`, `ruff`, `black`, `mypy`,
`aws`, `cdk`, or any build/test/lint/deploy command directly in the
nspawn — the toolchains aren't there and even if they were, they wouldn't
match the project's pinned versions. Always go through
`docker exec agentcore-dev bash -lc "..."`.

If a command fails inside `agentcore-dev` due to a missing tool that
genuinely belongs in the dev environment, the right fix is to add it to
`.devcontainer/Dockerfile` (with a pinned version and a sha256, per the
project's reproducibility posture) and rebuild — not to install it ad-hoc
inside the running container.
