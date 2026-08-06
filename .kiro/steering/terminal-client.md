---
inclusion: fileMatch
fileMatchPattern: ["tui/*", "scripts/local-dev/*"]
---

# Terminal Client (TUI)

A cross-platform terminal client for the platform, living in `tui/` as its own
uv project so it can ship independently (`uvx agentcore-tui`). Built on
**Textual 8.2.8**, Python 3.11+.

Branch: `feature/tui-client`. Nothing is deployed and nothing is published.

## Where it stands

**Phase 1 — done, working against a live sandbox.** Streaming chat over the
API-key authenticated `POST /chat/api-converse`. Verified end to end against a
local app-api pointed at the `dev-boisestateai-v2` tables, on both Bedrock and
Mantle (OpenAI-wire) models.

**Phase 2 — auth groundwork written, not deployed.**
- CDK: a public Cognito app client for the CLI, its id added to the AgentCore
  Runtime's `allowedClients`, and `COGNITO_CLI_APP_CLIENT_ID` exposed to app-api.
- Backend: `CognitoIdentityProviderService` now fans federated IdPs across every
  app client, not just one.
- TUI: full OIDC + PKCE login flow (`login --sso`).

**Blocked on:** deploying `platform.yml` so the client exists. Until then no real
token can be obtained, so the SSO path is unit-tested but never yet run for real.

**Not started:** the app-api Bearer branch (see Auth below), and everything that
depends on it — sessions/history, model discovery, the tool-using agent.

## The auth situation (read before touching auth)

`get_current_user_from_session` (`apis/shared/auth/dependencies.py:217`) is
**cookie-only**. It never reads the `Authorization` header, and its docstring
says so deliberately: "External Bearer callers were retired in the BFF
migration." The only API-key endpoint in all of app-api is
`POST /chat/api-converse`, which is a bare Bedrock Converse wrapper — **no
tools, no memory, no session persistence**.

The real agent is `POST /chat/stream` → inference-api `/invocations`.
`/invocations` *does* accept `Authorization: Bearer` via
`get_current_user_trusted`, but that decodes with `verify_signature: False`
because AgentCore's JWT authorizer validates upstream. **Do not reuse
`get_current_user_trusted` on app-api** — there is no upstream check there, so a
Bearer branch needs real `CognitoJWTValidator` verification, taught to accept the
CLI client id alongside `COGNITO_BFF_APP_CLIENT_ID`.

Facts established by research, worth not rediscovering:
- Cognito matches `redirect_uri` **byte-for-byte** and does **not** honour RFC
  8252's "treat the loopback port as variable" rule. Ports must be pre-registered
  on the app client; ephemeral ports are impossible. Hence the fixed
  `8976/8977/8978`, registered for both `localhost` and `127.0.0.1`.
- Cognito does **not** support the device authorization grant (RFC 8628). AWS's
  own guidance is to build it with Lambda + API Gateway + DynamoDB. Don't.
- The BFF app client is confidential (`generateSecret: true`), so PKCE against it
  would still need the secret. That is why the CLI has its own public client.
- Federated IdPs are created **at runtime by admins**, not in CDK, and there is
  no resync mechanism. That is why the fan-out exists.
- CSRF needs no work for a Bearer client: `CSRFMiddleware` only enforces when a
  session cookie is present.

## Layout

```
tui/src/agentcore_tui/
├── cli.py            argparse entry: chat, login [--sso], logout, status
├── config.py         CLI flags > env > TOML file > keyring > defaults
├── errors.py         typed errors, each carrying an actionable `hint`
├── logging_setup.py  rotating file log; content redacted unless opted in
├── app.py            the Textual App and turn lifecycle
├── app.tcss          stylesheet
├── client/           api-converse transport (converse.py) + SSE events
├── auth/             OIDC + PKCE: pkce, tokens, oidc, loopback, flow
├── screens/          model picker
└── widgets/          transcript messages, composer, status bar
```

189 tests, no network required: `httpx.MockTransport` for HTTP, Textual's
`run_test()` pilot for the UI, and real loopback round-trips for the OAuth
receiver.

## Environment (this will waste your time otherwise)

Everything runs **inside the devcontainer**. See
`.kiro/steering/colins-dev-machine.md`, but the two that matter most:

- The container needs `--memory=8g`. At 4g the CDK jest suite is OOM-killed.
- **Always pass `--runInBand` to jest.** Without it, parallel workers are
  OOM-killed and respawned in a loop that never terminates — it looks like a
  hang, and it has burned multi-hour runs.
- There is no bare `python` in the image. Everything goes through `uv run`.
- **Stale mount:** if a container command says `/workspace/...: No such file or
  directory`, check `docker exec agentcore-dev ls /workspace`. Zero entries means
  the container holds a detached mount — recreate the container. A fresh
  `docker run` seeing the mount proves nothing about a long-running one.

## Local dev loop

Requires `backend/src/.env` (gitignored) pointing at a deployed environment's
tables, with `SKIP_AUTH=true` and localhost-only `CORS_ORIGINS`. Generate the
table names from the deployed task definition rather than by hand:

```bash
aws ecs describe-task-definition --task-definition <prefix>-app-api-task \
  --query "taskDefinition.containerDefinitions[0].environment[].[name,value]" --output text
```

Then:

```bash
docker exec -it agentcore-dev aws sso login --profile <profile>
docker exec -d agentcore-dev bash -lc 'cd /workspace && scripts/local-dev/start-app-api.sh'
docker exec agentcore-dev bash -lc 'cd /workspace && scripts/local-dev/sync-models.sh'
docker exec agentcore-dev bash -lc 'cd /workspace && scripts/local-dev/mint-api-key.sh'
scripts/local-dev/tui.sh          # HOST-side wrapper; run-tui.sh is container-side
```

`tui.sh` passes `--detach-keys ctrl-@` — without it, Docker's detach sequence
eats keystrokes (see Gotchas).

`mint-api-key.sh` works because `POST /auth/api-keys` is a *session* route, so
`SKIP_AUTH` satisfies it and the local backend can mint a real key. Point
`SKIP_AUTH_USER_ID` at an **existing** user who has model grants (a role with
`MODEL_GRANT#*`). Minting **revokes that user's existing key**, so don't target a
colleague.

`start-app-api.sh` binds `127.0.0.1` and refuses a non-loopback host while the
bypass is on. `main.py` hardcodes `0.0.0.0`, so prefer the script and don't
publish port 8000.

## Verification

```bash
# TUI (container)
cd /workspace/tui && uv run pytest -q && uv run ruff check src tests \
  && uv run black --check src tests && uv run mypy src

# Infrastructure (container, in band or it hangs)
cd /workspace/infrastructure && npx tsc --noEmit && npx jest --runInBand --silent

# Backend slice
cd /workspace/backend && uv run pytest tests/ -q -k "cognito or auth_provider"

# Version sync — covers the three tui manifests
bash scripts/common/sync-version.sh --check
```

## Gotchas that have already cost real time

- **Textual containers default to `height: 1fr`.** Message widgets filled the
  viewport, so the transcript's virtual size never exceeded one screen,
  `max_scroll_y` stayed 0, and long answers were clipped and unreachable — it
  looked exactly like the model stopped mid-sentence. Every widget between
  `#transcript` and the text needs `height: auto`. Regression-tested in
  `TestTranscriptGrowth`; the app also logs `CLIPPED` if it recurs.
- **`Ctrl+P` is Docker's detach key.** `docker exec -it` withholds it pending the
  next keystroke, so the palette appeared dead and then opened with your first
  typed character in its search box. The palette is bound to **F1** and `Ctrl+P`
  is left unbound. `--detach-keys ctrl-@` also protects `Ctrl+Q`.
- **Textual only auto-registers `COMMAND_PALETTE_BINDING` when no binding targets
  the `command_palette` action.** Adding F1 alone silently killed `Ctrl+P`.
- **Optional fields hide plumbing bugs.** `cliAppClient` is optional on
  `PlatformComputeRefs`, so omitting it from the refs literal type-checked
  cleanly while the runtime authorizer quietly kept one entry. `tsc` and the
  tests both passed; only inspecting the synthesized template caught it. Verify
  CDK by reading the template, not just by compiling.
- **`createMockConfig` bypasses `loadConfig`,** so new config defaults must be
  added to the test mock too or tests assert a shape production never produces.
- **`skip-auth-guard.yml` greps `scripts/`** for the bypass being switched on. Do
  not write the literal assignment in a script, even inside a grep pattern or a
  comment; build the pattern with a character class instead. Don't add a scan
  exclusion — real deploy scripts live in that directory.
- **SVG frame assertions:** `export_screenshot()` embeds a `<style>` block whose
  CSS survives tag-stripping, and each styled run is its own `<text>` element.
  Use the `rendered_text()` helper in `tests/test_app.py`, which strips
  style/defs and regroups runs by `y`.
- **`metadata` SSE events fire per LLM call.** Only the last one (the one
  carrying `contextWindow`) is a valid whole-turn summary.

## When Phase 2 resumes

1. Deploy `platform.yml`. **Do not `cdk deploy` locally** — `cdk.context.json`
   has `projectPrefix: "agentcore"` with `domainName`/`awsAccount`/
   `certificateArn` blank; the real values come from GitHub Variables via
   `load-env.sh`, and deploying with blanks risks replacing live CloudFront/ALB/
   Route53 resources.
2. Set `cognito_domain_url` and `cli_client_id` in the TUI config, then run
   `agentcore-tui login --sso` for the first real token.
3. Prove agent chat straight to `/invocations` with that Bearer token — this
   needs no backend change and is the cheapest validation of the whole approach.
4. Add the app-api Bearer branch, which unlocks `/sessions`, history, `/models`,
   and `/tools`.

For the agent stream itself, **reuse `apis/shared/harness/sse.py`** —
`iter_sse_events()` is a correct parser for that stream and
`InvocationStreamAccumulator` already folds events into text, tool trace, usage,
and title. `harness/runner.py:run_agent_headless` is a complete working
non-browser client. The agent emits ~35 event types (api-converse had 11); the
SPA handles 23 and ignores 11, and `event`/`message`/`result` **must** be ignored
or output doubles.

Two behaviours to design for: `Esc`/`Ctrl+C` must `POST /sessions/{id}/interrupt`
or the turn keeps burning tokens and holds the session lease; and never
transparently retry a stream, because a reopen double-runs the turn and corrupts
memory.
