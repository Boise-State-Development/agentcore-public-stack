# AgentCore TUI

A terminal client for the AgentCore platform. Streaming AI chat in your shell —
same models, same RBAC, same quotas, and the same cost tracking as the web app.

Built with [Textual](https://textual.textualize.io/). Runs on Windows, macOS,
and Linux.

```
┌─ AgentCore ──────────────────────────────────── https://your-host/api ─┐
│ ▌You                                                                   │
│  explain the difference between a thread and a process                 │
│                                                                        │
│ ▌claude-haiku-4-5                                                      │
│  A **process** owns its own address space; a **thread** shares one...  │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│ ╭──────────────────────────────────────────────────────────────────╮   │
│ │ Ask anything...                                                  │   │
│ ╰──────────────────────────────────────────────────────────────────╯   │
│ Ready  |  claude-haiku-4-5  |  3 turns  |  1,284 in · 412 out          │
└────────────────────────────────────────────────────────────────────────┘
```

## What it does today

This is Phase 1. It talks to the API-key authenticated `/chat/api-converse`
endpoint on app-api, which is a direct Bedrock Converse wrapper. That means you
get:

- Streaming responses rendered as live Markdown, with syntax-highlighted code
- Multi-turn conversation with full history sent on each turn
- Extended-thinking (reasoning) output in a separate collapsible pane
- Per-turn token usage, including prompt-cache hits
- Model switching, RBAC-enforced server-side
- Server-side cost tracking and quota enforcement, identical to the web UI

**What it does not do yet.** The API-key endpoint has no access to tools,
memory, sessions, or assistants — those live behind the cookie-authenticated
agent path. So there is no tool use, no persistent conversation storage, no
file upload, and no session list. Conversations exist only in the running
process; quitting discards them. See [Roadmap](#roadmap).

## Install

You need an API key. Create one in the web app under **Settings → API Keys**.
The raw key is shown exactly once, keys expire after 90 days, and creating a new
key revokes the previous one.

### Recommended: uv (all platforms)

[uv](https://docs.astral.sh/uv/) runs the client without installing anything
permanently:

```bash
uvx --from /path/to/repo/tui agentcore-tui
```

Once this package is published to an index, that becomes `uvx agentcore-tui`.

To install it persistently:

```bash
uv tool install /path/to/repo/tui
```

### pipx

```bash
pipx install /path/to/repo/tui
```

### From source

```bash
cd tui
uv sync --extra dev
uv run agentcore-tui
```

### Terminal requirements

| OS | Recommended terminal | Notes |
|---|---|---|
| Windows | Windows Terminal, or PowerShell 7+ | The legacy `conhost.exe` console degrades colour and key handling. WSL works fine. |
| macOS | Ghostty, iTerm2, WezTerm, or Terminal.app | `shift+enter` for newline needs Ghostty/iTerm2/WezTerm/Kitty. |
| Linux | Any modern emulator | Headless hosts: see the keyring note in [Troubleshooting](#troubleshooting). |

## First run

```bash
agentcore-tui login --base-url https://your-host/api
```

You will be prompted for the key without echo, so it never lands in your shell
history. The key goes into your OS keyring; only the base URL is written to the
config file.

Then:

```bash
agentcore-tui
```

Check your setup at any time:

```bash
agentcore-tui status     # resolved config (key never printed) + health probe
agentcore-tui logout     # remove the stored key
```

The base URL is the app-api root. On a CloudFront deployment that usually ends
in `/api` — the same origin the SPA calls.

## Key bindings

| Key | Action |
|---|---|
| `Enter` | Send |
| `Alt+Enter`, `Shift+Enter`, `Ctrl+O` | Insert a newline |
| `Esc` | Stop the in-flight turn |
| `Ctrl+N` | New conversation |
| `F2` | Switch model |
| `F1` | Command palette (themes, and more) |
| `Ctrl+Q` | Quit |

Three aliases exist for "newline" because terminals disagree: `Shift+Enter`
requires the Kitty keyboard protocol, while `Alt+Enter` and `Ctrl+O` work
essentially everywhere. `Ctrl+J` is deliberately unbound — in a terminal it is
LF, which collides with Enter itself.

`Ctrl+Q` collides with Docker's detach sequence, so launch with
`scripts/local-dev/tui.sh` — it remaps detach via `--detach-keys ctrl-@` so
quitting can't detach you from the container instead. Or set it once in
`~/.docker/config.json`:

```json
{ "detachKeys": "ctrl-@" }
```

## Configuration

Settings resolve in this order, highest priority first:

1. CLI flags (`--base-url`, `--model`)
2. Environment variables — `AGENTCORE_BASE_URL`, `AGENTCORE_API_KEY`, `AGENTCORE_MODEL_ID`
3. The config file
4. The OS keyring (API key only)
5. Built-in defaults

Config file location:

| OS | Path |
|---|---|
| Linux | `~/.config/agentcore-tui/config.toml` |
| macOS | `~/Library/Application Support/agentcore-tui/config.toml` |
| Windows | `%APPDATA%\agentcore-tui\config.toml` |

```toml
base_url = "https://your-host/api"
model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# The model picker (F2) reads this list. `GET /models` is cookie-session
# authenticated, so an API-key client cannot discover the catalogue — set the
# models your deployment actually grants your role.
models = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
]

system_prompt = "Answer concisely."
temperature = 0.7
max_tokens = 4096
top_p = 0.9
timeout_seconds = 300
```

`api_key` in this file is honoured for environments with no keyring, but the
client warns on startup because it is plain text on disk. `agentcore-tui login`
never writes it there.

## Troubleshooting

**"API key was rejected" (401).** Keys expire after 90 days, and creating a new
key revokes the old one. Mint a fresh key and run `agentcore-tui login` again.

**"Your account is not permitted to use ..." (403).** RBAC is enforced
server-side against your role's model grants. Press `F2` to pick another model,
or ask an administrator to grant your role access.

**"Rate limit or quota exceeded" (429).** The endpoint allows 60 requests per
minute per key. This is also what an exhausted cost quota looks like.

**No keyring on this host.** Headless Linux boxes, containers, and CI often have
no Secret Service. The client degrades rather than crashing — `agentcore-tui
status` reports the reason. Use `AGENTCORE_API_KEY` there instead.

**A long answer looks like it stopped mid-sentence.** Check the log: if
`stream end ... text_chars=N` and `turn rendered chars=N` agree, the text all
arrived and the problem is layout, not the model. The transcript only scrolls
when every widget between it and the text has `height: auto` — Textual
containers default to `height: 1fr`, which makes messages fill the viewport and
clips anything past one screenful. The app logs `CLIPPED` when it detects this.
If instead the log shows `message_stop reason=max_tokens`, the model really did
stop early; raise `max_tokens`.

**`shift+enter` sends instead of inserting a newline.** Your terminal does not
implement the Kitty keyboard protocol. Use `Alt+Enter` or `Ctrl+O`.

## Logging

A full-screen TUI owns stdout, so all diagnostics go to a rotating file
(1 MB × 3 backups).

| OS | Default log path |
|---|---|
| Linux | `~/.local/state/agentcore-tui/log/agentcore-tui.log` |
| macOS | `~/Library/Logs/agentcore-tui/agentcore-tui.log` |
| Windows | `%LOCALAPPDATA%\agentcore-tui\Logs\agentcore-tui.log` |

`agentcore-tui status` prints the active path. Controls:

```bash
agentcore-tui --log-level DEBUG          # per-event SSE tracing
agentcore-tui --log-file /tmp/tui.log    # or AGENTCORE_LOG_FILE
AGENTCORE_LOG_LEVEL=DEBUG agentcore-tui
```

**Prompts and model output are not logged** unless you opt in with
`AGENTCORE_LOG_CONTENT=1`. Lengths and counts always are. The API key is never
logged at any level.

A healthy turn at INFO looks like this — enough to tell a server problem from a
rendering problem without reproducing anything:

```
INFO agentcore_tui.app              turn start model=...claude-haiku-4-5... history_turns=0 prompt_chars=86
INFO agentcore_tui.client.converse  stream start model=... turns=1 prompt_chars=86 max_tokens=4096 url=...
INFO agentcore_tui.client.converse  message_stop reason=end_turn
INFO agentcore_tui.client.converse  usage input=24 output=171 cache_read=None cache_write=None
INFO agentcore_tui.client.converse  stream end model=... elapsed=3.43s text_chars=359 events={'TextDelta': 25, ...}
INFO agentcore_tui.app              turn rendered chars=359 reasoning=0 viewport_h=17 content_h=29 max_scroll_y=12 scroll_y=12
```

Reading it:

- `text_chars` on `stream end` vs `chars` on `turn rendered` — if these agree, the
  server sent everything and any missing text is a *rendering* problem.
- `message_stop reason=max_tokens` means the model hit the token ceiling; raise
  `max_tokens` in the config file.
- `content_h` > `viewport_h` with `max_scroll_y=0` logs an explicit
  `CLIPPED` error. That combination means message widgets are not sizing to
  their content, so text below the fold is unreachable — see the note in
  `app.tcss` about `height: auto`.

## Running against a local app-api

This is the normal development loop: app-api on your machine, the TUI pointed at
it. Scripts live in `scripts/local-dev/` and all run **inside the dev container**.

One thing to understand first: a local app-api is *not* self-contained. There is
no local-DynamoDB or localstack support, so it reads and writes the real
DynamoDB tables of a deployed environment and calls real Bedrock. "Local" means
the process is local; the data plane is not. You therefore need AWS credentials
and a deployed environment to borrow tables from.

### One-time setup

**1. Credentials in the container.** Use a named volume so the SSO session
survives container recreation and never lands on disk in the repo:

```bash
docker volume create agentcore-aws
docker run -d --name agentcore-dev \
  --memory=4g --memory-swap=4g --cpus=4 --pids-limit=4096 \
  -v "$(pwd)":/workspace -v agentcore-aws:/home/dev/.aws \
  -w /workspace agentcore-devcontainer:latest sleep infinity

docker exec -it agentcore-dev aws configure sso     # or: aws sso login --profile <p>
```

**2. Generate `backend/src/.env`.** The authoritative source for table names is
the deployed task definition — don't hand-write them:

```bash
docker exec agentcore-dev bash -lc '
aws ecs describe-task-definition --task-definition <prefix>-app-api-task \
  --query "taskDefinition.containerDefinitions[0].environment[].[name,value]" \
  --output text | awk -F"\t" "{print \$1 \"=\" \$2}"' > backend/src/.env
```

Then add the local-dev overrides at the top (and remove the deployed
`CORS_ORIGINS`, which is not localhost):

```bash
AWS_PROFILE=<your-sso-profile>
CORS_ORIGINS=http://localhost:4200,http://127.0.0.1:4200,http://localhost:8000,http://127.0.0.1:8000
SKIP_AUTH=true
SKIP_AUTH_USER_ID=<an-existing-user-sub-with-model-grants>
SKIP_AUTH_EMAIL=<that-user-email>
SKIP_AUTH_ROLES=system_admin
```

`backend/src/.env` is gitignored (`backend/src/.gitignore`).

### The loop

```bash
# 1. app-api on 127.0.0.1:8000
docker exec -d agentcore-dev bash -lc 'scripts/local-dev/start-app-api.sh'

# 2. mirror the environment's enabled models into the TUI config
docker exec agentcore-dev bash -lc 'scripts/local-dev/sync-models.sh'

# 3. mint an API key (needs SKIP_AUTH=true; revokes that user's previous key)
docker exec agentcore-dev bash -lc 'scripts/local-dev/mint-api-key.sh'

# 4. chat  (run this ON THE HOST — it wraps docker exec for you)
scripts/local-dev/tui.sh

# non-interactive check
scripts/local-dev/tui.sh status
```

Use `scripts/local-dev/tui.sh` rather than a bare `docker exec -it`. It remaps
Docker's detach key sequence, which otherwise collides with the app's own key
bindings. See [Key bindings](#key-bindings).

### How the key is minted without a browser

`/chat/api-converse` requires a genuine API key — it validates against the
api-keys table and then loads the owner's profile from the users table, failing
closed with 401 if there is no profile row. There is no bypass on that path.

But `POST /auth/api-keys` is a *session* route, and `SKIP_AUTH=true` makes
session dependencies return a fake admin whose identity is `SKIP_AUTH_USER_ID`
(`apis/shared/auth/dependencies.py`). So the local backend mints a real key for
that user, and the TUI then authenticates exactly as it would in production —
same RBAC, same quotas, same cost attribution.

Point `SKIP_AUTH_USER_ID` at an **existing** user who has model grants (a role
carrying `MODEL_GRANT#*`, e.g. `system_admin`). That avoids inventing synthetic
user rows in a shared environment. Check first whether that user already has an
API key, because minting revokes it.

### Security properties of this setup

`SKIP_AUTH=true` turns every session route into an unauthenticated admin
surface, against a *shared* environment's real data. Two guards keep that
contained, and you should not defeat either:

- app-api refuses to boot unless every `CORS_ORIGINS` entry is a localhost URL.
- `scripts/local-dev/start-app-api.sh` binds `127.0.0.1` and refuses a
  non-loopback `APP_API_HOST` while `SKIP_AUTH=true`. Note `main.py` hardcodes
  `0.0.0.0` when run as `python main.py`, so prefer the script — and do not
  publish port 8000 from the container.

Costs and quota consumption land on whichever user `SKIP_AUTH_USER_ID` names.

## Development

All commands run inside the project dev container. Note there is no bare
`python` on that image — everything goes through `uv run`.

```bash
cd tui
uv sync --extra dev

uv run pytest -q                 # 133 tests, no network required
uv run ruff check src tests
uv run black --check src tests
uv run mypy src
```

Tests drive the client through `httpx.MockTransport` and the app through
Textual's `run_test()` pilot, so the suite never opens a socket and needs no
deployed backend. Two tests assert against an exported frame, which also proves
`app.tcss` parses.

For live debugging, Textual's devtools console is useful:

```bash
uvx textual-dev console          # in one pane
uv run textual run --dev agentcore_tui.app:ChatApp    # in another
```

### Layout

```
tui/
├── pyproject.toml               # deps, pins, console script
└── src/agentcore_tui/
    ├── cli.py                   # argparse entrypoint: chat, login, logout, status
    ├── config.py                # config file + env + keyring resolution
    ├── errors.py                # typed errors, each with an actionable hint
    ├── app.py                   # the Textual App and turn lifecycle
    ├── app.tcss                 # stylesheet
    ├── client/
    │   ├── converse.py          # HTTP/SSE transport, error mapping
    │   └── events.py            # typed SSE events + turn accumulator
    ├── screens/model_picker.py
    └── widgets/                 # transcript messages, composer, status bar
```

The version is kept in step with the monorepo `VERSION` file by
`scripts/common/sync-version.sh`.

## Roadmap

Phase 2 is where this gets interesting. Every rich surface on app-api — the
tool-using agent (`POST /chat/stream`), sessions, the tool catalogue, cost
dashboards, assistants — is authenticated with an OIDC browser session, not an
API key. Adding a loopback PKCE login (open the browser, catch the redirect on
`127.0.0.1`, keep the session in the OS keyring) is what unlocks:

- The real agent, with tools, MCP servers, and memory
- Persistent sessions shared with the web UI
- Model discovery via `GET /models` instead of a hand-maintained list
- Cost and quota dashboards in the terminal
