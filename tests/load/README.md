# Load tests (Locust)

Simulates signed-in browser users against the real chat path: Cognito Hosted UI
login → `POST /chat/stream` → SSE, with client-side time-to-first-token.

> **This spends real money.** Every chat turn is a live Bedrock invocation
> billed to your account and counted against the acting user's quota. A 50-user
> run at one turn per 10s is ~300 turns/minute. Start small, watch the cost
> dashboard, and never point this at production without agreeing a budget
> first.

## What it tests, and why this path

`POST /chat/stream` is the only path real users take. It is cookie-only —
Bearer callers were retired in the BFF migration (`apis/shared/auth/dependencies.py`)
— so the test performs a full OAuth authorization-code login rather than
minting a token. Each turn traverses CloudFront → ALB → app-api on Fargate →
inference-api on the AgentCore Runtime, with app-api holding a connection open
relaying SSE for the whole turn.

That last detail is the point. Concurrency is bounded by held-open connections,
not by request rate, so **RPS is the wrong number to watch**. Watch concurrent
users, time-to-first-token, and error rate.

The API-key path (`/chat/api-converse`) is deliberately *not* exercised here.
It is a minor feature, and it is rate-limited to 60 requests per 60 seconds per
key (`apis/shared/rate_limit.py`), so it cannot represent platform load.

## Setup

Runs in the devcontainer. From `tests/load/`:

```bash
uv sync
```

Deliberately a separate project from `backend/` — Locust is a testing tool, not
an application dependency, and putting it in `backend/pyproject.toml` would
land it in `backend/uv.lock` and in the app-api image's dependency resolution.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `--host` (CLI flag) | yes | app-api origin, e.g. `https://chat.example.edu/api`. **Must be https** — see below. |
| `AGENTCORE_LOAD_COGNITO_DOMAIN` | yes | Hosted UI domain, e.g. `https://your-prefix.auth.us-west-2.amazoncognito.com` |
| `AGENTCORE_LOAD_USERS_FILE` | one of | JSON array of `{"username", "password"}` — the provisioning script's output |
| `AGENTCORE_LOAD_USERNAME` / `_PASSWORD` | one of | Single user, for a smoke run |
| `AGENTCORE_LOAD_MODEL_ID` | no | Omit for the system default |
| `AGENTCORE_LOAD_PROVIDER` | no | `bedrock`, `openai`, `gemini` |
| `AGENTCORE_LOAD_ENABLED_TOOLS` | no | Comma-separated. Empty (default) = no tools |
| `AGENTCORE_LOAD_TURNS_PER_CONVERSATION` | no | Default 3 |
| `AGENTCORE_LOAD_PROMPTS_FILE` | no | One prompt per line; `#` lines are comments. Overrides the built-ins |
| `AGENTCORE_LOAD_ALLOW_CREDENTIAL_REUSE` | no | Let simulated users share Cognito identities. Off by default — see below |

**`--host` must be https.** The session and CSRF cookies are `__Host-`
prefixed, hence `Secure`-only. Over plain http `requests` silently drops them
and every chat request 401s — which reads as a broken backend. `validate_host`
fails fast instead.

**Leave tools disabled** unless you are specifically testing them. Tool calls
add multi-second, high-variance latency that swamps the signal.

## Running

```bash
export AGENTCORE_LOAD_COGNITO_DOMAIN="https://your-prefix.auth.us-west-2.amazoncognito.com"
export AGENTCORE_LOAD_USERS_FILE="$HOME/.config/agentcore-load/users.json"

# Web UI at :8089
locust -f locustfile.py --host https://chat.example.edu/api

# Headless, 10 users, 5 minutes
locust -f locustfile.py --host https://chat.example.edu/api \
    --headless --users 10 --spawn-rate 1 --run-time 5m

# Free control run — no Bedrock spend
locust -f locustfile_readonly.py --host https://chat.example.edu/api \
    --headless --users 50 --spawn-rate 5 --run-time 5m
```

One worker per core with `--processes -1`. You will not need it soon: the
generator is never the bottleneck on a path where each user holds a stream open
for seconds. In the devcontainer, mind the memory cap and keep `--processes`
at or below 4.

## One Cognito identity per simulated user

Each simulated user is assigned its **own** credential from the manifest, and a
run that asks for more users than the pool holds **refuses to start**:

```
Refusing to start: 50 simulated users requested but only 3 credential(s)
available. Each user needs its own Cognito identity or the run measures
DynamoDB partition contention that real users would not create.
```

This is deliberately a hard failure rather than a warning. Simulated users
sharing an identity also share its `user_id`, which means one DynamoDB partition
for session and cost writes, one quota counter, and one memory namespace. The
resulting latency partly measures the test's own key collisions — a
plausible-looking wrong answer, which is worse than an obviously broken run.

Provision to match your target concurrency:

```bash
scripts/load-test/provision.sh --users 300 --quota-days 1
```

`AGENTCORE_LOAD_ALLOW_CREDENTIAL_REUSE=1` restores the old round-robin for cases
where sharing genuinely does not matter — a smoke run, or measuring the login
path alone.

Under `--processes`, each worker takes a stride of the pool (`pool[i::n]`) so two
workers never deal the same account. That needs at least one credential per
worker.

## Campus-scale runs

The default profile is cheap on purpose, and that makes it **useless for
capacity planning**. Measured against the real production deployment:

| Per turn | production | default profile |
|---|---|---|
| input tokens | 384 | 1,781 |
| cache-read tokens | 24,926 | 0 |
| output tokens | 1,372 | 139 |
| **total counted against quota** | **~26,700** | **~1,920** |

Cache-read tokens count against the Bedrock TPM quota. So the default profile
understates consumption ~14× per turn, and a run that passes comfortably can
correspond to a production workload that throttles.

Use the representative profile for anything you intend to draw conclusions from:

```bash
source tests/load/profiles/campus-representative.env
```

It enables a realistic tool set (tool schemas are what create the cached
prefix), uses long-form prompts, and deepens conversations. Note the prompts are
answerable *without* tool calls on purpose — the tools are there to reproduce
prompt size, not to fire Canvas, Salesforce, and Brave Search at 300× concurrency.

### The classroom burst

The shape that actually threatens this platform is not 1300 users arriving
gradually; it is one instructor saying "ask the assistant about X" and 300
students submitting inside thirty seconds.

```bash
source tests/load/profiles/campus-representative.env
export AGENTCORE_LOAD_TURNS_PER_CONVERSATION=2
locust -f locustfile_classroom.py --host https://your-domain/api --headless
```

`--users`, `--spawn-rate` and `--run-time` are ignored; `ClassroomBurstShape`
drives the run and ends it. It fires two bursts — the first against cold
infrastructure (2 Fargate tasks, empty prompt cache), the second against warm —
because "survives a class following another class" and "survives the 9am class"
are different answers. Tunable via `AGENTCORE_LOAD_BURST_*`; see `shapes.py`.

### Watch the quota while it runs

Throttling reaches Locust as opaque failed turns. Run this alongside any chat
scenario to see the approach to the limit instead:

```bash
scripts/load-test/watch-tpm.sh --model-id global.anthropic.claude-sonnet-5
```

It also prints implied **tokens/turn**, which is how you confirm the run is
representative rather than trusting that it is. If that column reads ~2,000
instead of ~26,700, the profile is not loaded and the TPM result will not
generalise.

## Reading the results

Four row types appear, and they measure different things:

| Row | What it is |
|---|---|
| `POST /chat/stream` | **Time to response headers only** — the request is streamed, so this is not turn duration. `response_length` is 0 for streamed requests; ignore the byte columns. |
| `SSE chat: time to first token` | Login-independent responsiveness. The number that tracks user experience. |
| `SSE chat: full turn` | Whole turn, first byte to `done`. `response_length` is the character count. |
| `GET /auth/login`, `POST cognito /login`, … | Login hops. Login degrading under load is a real finding, so they are measured, not hidden. |

Expect `full turn` percentiles in the tens of seconds and do not read that as
failure. Measured over 14 days in dev, a healthy turn averages 3.0–4.5s with
daily maxima of 16.7s, 16.9s and 24.4s. A sudden *drop* can mean turns are
failing early.

A turn that returns HTTP 200 can still fail inside the stream — quota blocks
and model errors arrive as `message_stop` with `stopReason: "error"`. Those are
recorded as failures on `SSE chat: full turn`, not on the HTTP row.

## Provisioning users

Not done here. This process has no AWS credentials by design. It consumes a
credential manifest produced separately, because creating users, granting quota
overrides and tearing them down mutates live shared state and needs to be
gated and audited on its own terms.

Two things to know when provisioning:

- **Users must have permanent passwords.** `FORCE_CHANGE_PASSWORD` blocks
  scripted login; the run fails at `POST cognito /login` with a clear message.
- **Quota overrides are usually required.** Sustained turns will trip per-user
  cost limits, after which you are measuring the quota-enforcement path rather
  than the chat path. Grant time-limited `unlimited` overrides and **remove them
  afterwards** — a forgotten override is a disabled cost control.

Fewer credentials than simulated users is fine: each Locust user keeps its own
cookie jar, so several can log in as one Cognito account and get independent
BFF sessions. Credentials are handed out round-robin. The distortion is that
quota, memory and session history concentrate on those accounts, so keep the
pool wide enough that per-user state is not the bottleneck you accidentally
find.

## Limitations

- **Cognito Hosted UI only.** Login submits the server-rendered form. If a
  deployment uses managed login (branding v2) with a client-rendered form, or
  an IdP with MFA or conditional access, scripted login will fail — with a
  diagnostic naming the forms it parsed. Use a dedicated Cognito provider for
  load-test users.
- **`requests`-based, not `FastHttpUser`.** SSE needs `iter_lines`, which
  `FastHttpUser` does not offer. Lower per-worker ceiling, irrelevant on this
  path.
- **`-f -` will not work** for distributed runs, because this is a package
  rather than a single file. Ship the directory to workers (volume mount or
  image layer) instead.
- **No file uploads, voice, or tool-heavy turns.** Text turns only.
