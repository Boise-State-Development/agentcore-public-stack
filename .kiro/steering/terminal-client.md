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

**Phase 2 — direction changed. See #[[file:docs/specs/CLI_DEVICE_AUTH_SPEC.md]].**

The original plan gave the CLI its own public Cognito app client and had it call
the AgentCore Runtime directly with a PKCE-minted token. That was **reverted**
(PR #850) after deploying it revealed the cost: `/chat/stream` forwards the BFF
session's token upstream, and the agent forwards *that* to MCP servers which
validate it in-app. A CLI-minted token would therefore reach external MCP
servers that may pin `client_id` to the BFF client — servers deployed outside
this repository, so the work could not be bounded. A second app client also
carried a permanent tax (bearer branch, IdP fan-out with no resync, a separate
refresh path with no `SECRET_HASH`).

**The replacement:** the CLI obtains a **real BFF session** via an app-api device
authorization flow and presents it as `Authorization: BFF <sealed>`. It never
talks to Cognito. The token seen downstream is always BFF-minted, so MCP is
unaffected and **no Cognito configuration changes at all**.

Why this is small: `SessionRefreshMiddleware` resolves the cookie and attaches a
`SessionRecord` to `request.state.bff_session`; every consumer reads that record
and none reads a cookie. So the integration point is one branch in one
middleware — not `get_current_user_from_session`, despite its docstring.

**Landed so far:** the **entire backend**, deployed and verified end-to-end
against `dev.boisestate.ai` on 2026-08-07 — domain layer, repository, service,
the three `/auth/cli/*` routes, the device branch in `GET /auth/callback`, rate
limiting, and the `SessionRefreshMiddleware` header branch (141 tests). On the
TUI side, the agent-stream SSE dialect (`client/agent_events.py`) and the
transcript widgets that render it (`widgets/agent_content.py`) are done, since
neither needs auth.

All seven verification steps passed, including the browser leg and the
no-cookie invariant. Results are recorded step-by-step in the spec's "Verified
after deploy" section. **No infrastructure deploy was required** — grants live in
the existing BFF sessions table, so there is no CDK grants table and
`platform.yml` never ran.

**Still pending:** nothing in the original plan. The device-polling flow, the
`/chat/stream` transport and the turn wiring are all **done and proven live**
against `dev.boisestate.ai` — a signed-in terminal runs tool-using agent turns,
and a second turn answered from server-side memory with only one message sent.
Optional follow-ups are listed at the end of the spec.

**Two dialects, one machinery.** `BaseTurnController` owns the delta buffers and
`flush()`, the busy flag, `begin`/`finish`, and the `stream()` skeleton that must
never raise. `TurnController` (api-converse) and `AgentTurnController`
(`/chat/stream`) subclass it and own only what differs: which accumulator, which
handlers, how the stream opens, what a finished turn means, and whether cancel
has anything to tell the server. `screens/chat.py` picks one from
`config.credential_source`, so signing in switches the terminal to the agent.

Do not collapse these into one class with conditionals. The accumulators disagree
about what `.text` means — the agent's is the **last** assistant message, since
each tool round trip closes one and opens another — so a shared `complete()`
would need a branch for the difference that matters most.

**`TurnSink` gained exactly two methods**, `on_tool(record)` and `on_title`.
`on_tool` takes the mutable `ToolCallRecord` the accumulator folds into, not the
event, so the widget and the fold are the same object; callers key on
`tool_use_id` and call `refresh_from_record()` rather than re-mounting. Everything
else the agent carries (citations, artifacts, quota, compaction, interrupts) goes
through `on_notice`, because all of it is end-of-turn fact rather than live state.

**Dead code removed:** `tui/src/agentcore_tui/auth/` and `tests/test_auth.py`
(1,314 lines), plus `cognito_domain_url`, `cli_client_id`, `callback_ports`,
`sso_configured` and `--provider`. `BearerAuth` went too — it existed for a CLI
that minted its own tokens, which is the design #850 reverted. `AuthProvider` is
the seam; a provider is not.

**Container keyring:** the image now ships one. A container has no login session,
so `keyring` used to select `backends.fail.Keyring` and `login --sso` completed
the flow only to fail at storage. `.devcontainer/Dockerfile` installs
`gnome-keyring libsecret-1-0 dbus-x11` and
`scripts/local-dev/keyring-init.sh` unlocks it with a passphrase you type once
per container start — run it with `-it`, it prompts. No TUI code changed; the
`SecretService` backend is selected automatically. Plaintext-file and env-var
fallbacks were rejected: they would make the container the least safe place to
sign in, and it is where we sign in most.

Three traps, each of which yields a working keyring in one command and a broken
one in the next — details in the spec:

- `gnome-keyring-daemon` needs **`--components=secrets`** or it never claims
  `org.freedesktop.secrets`.
- It needs **`--daemonize`**; `&` dies with the `docker exec`, and since the
  service is D-Bus-activatable the next request spawns a *locked* replacement.
  `setsid` is not a substitute — it detaches before the passphrase is read.
- A stale bus socket **file** survives a container restart, so liveness must be a
  real `ListNames` call, not `[ -S "$socket" ]`.

The bus-address rejoin lives in **`/etc/profile.d/`**, not `~/.bashrc`:
`bash -lc` is a non-interactive login shell and Ubuntu's stock `~/.bashrc`
returns early for exactly that. The Dockerfile's pre-existing `~/.bashrc` PATH
block has the same latent problem.

## The agent is told it is in a terminal

`client_surface: "terminal"` on every `/chat/stream` turn
(`client/agent_stream.py`, `CLIENT_SURFACE`). The backend composes
`SHARED_SYSTEM_PROMPT` + a per-surface interface block, so the agent names F2/F3/F4
instead of a gear icon and is explicitly forbidden from emitting KaTeX and
Mermaid — a terminal renders both as literal noise, and the model's priors push
hard toward them unless told no.

**If you move a keybinding, update `TERMINAL_SURFACE_GUIDANCE`** in
`backend/src/agents/main_agent/core/system_prompt_builder.py`. It hardcodes F2,
F3 and F4; a stale block means the agent confidently tells users the wrong key,
which is the exact failure the split was built to end. A test asserts the keys
are present but cannot know whether they are still correct.

The surface is a dimension of the **agent cache key**, and a paused turn records
it in its snapshot. Both are deliberate — details in
#[[file:docs/specs/TUI_WEB_PARITY_SPEC.md]].

## Test fixtures written from types will not save you

The agent-stream dialect was built from the SPA's `stream-parser-types.ts` and
the SSE table in `CLAUDE.MD`, with 87 tests. It still shipped a bug: Strands'
`init_event_loop` / `start_event_loop` frames parsed as `UnknownEvent`, because
the SPA drops them through its `switch` default instead of naming them, so
neither source mentions them and no fixture could contain them.

It was found in minutes by replaying one real captured turn. That capture is now
`tui/tests/fixtures/live_agent_stream.sse` with
`tui/tests/test_agent_stream_live_replay.py` asserting on it. **When you touch a
wire dialect, capture a real response and replay it** — the SPA's handled-event
set is not the server's sent-event set, and only the wire knows the difference.

Two things that capture also pinned down, both worth not rediscovering:

* **`metadata_summary` never reaches the client.** `stream_coordinator.py`
  swallows it on purpose (Strands' `accumulated_usage` sums each call's full
  context and overstates occupancy) and sends a final per-call `metadata`
  instead. So `AgentTurnAccumulator.usage` is the **last call's context size**,
  not the turn's token total — correct for a context-% badge, wrong as a
  "tokens used" figure. Label it accordingly.
* **`metadata` fires per LLM call.** A one-tool turn emitted three.

## The auth situation (read before touching auth)

`get_current_user_from_session` (`apis/shared/auth/dependencies.py:217`) does
**not** read the request. It consumes `request.state.bff_session`, which
`SessionRefreshMiddleware` populates. Its docstring says "External Bearer callers
were retired in the BFF migration" — true, but the useful consequence is that
adding a *session* resolution path costs one middleware branch and changes no
route.

The only API-key endpoint in all of app-api is `POST /chat/api-converse`, which
is a bare Bedrock Converse wrapper — **no tools, no memory, no session
persistence**. Everything richer is session-authenticated.

`/invocations` accepts `Authorization: Bearer` via `get_current_user_trusted`,
which decodes with `verify_signature: False` because AgentCore's JWT authorizer
validates upstream. **Do not reuse `get_current_user_trusted` on app-api** —
there is no upstream check there.

Facts established by research, worth not rediscovering:
- Cognito matches `redirect_uri` **byte-for-byte** and does **not** honour RFC
  8252's "treat the loopback port as variable" rule. Loopback redirects are a
  dead end from a container, which is one reason the device flow polls instead.
- Cognito does **not** support the device authorization grant (RFC 8628). The
  flow in the spec is app-api's own, reusing the existing BFF login.
- The BFF app client is confidential (`generateSecret: true`), so the code
  exchange must happen in app-api, never in the CLI.
- CSRF needs no work for a header client: `CSRFMiddleware` only enforces when a
  session cookie is present.
- The sealed session value is an AES-GCM envelope under a Secrets-Manager key,
  portable across tasks by design — which is what makes handing it to a CLI safe.

## Layout

```
tui/src/agentcore_tui/
├── cli.py            argparse entry: chat, login [--sso], logout, status
├── config.py         CLI flags > env > TOML file > keyring > defaults
├── credentials.py    which credential is held (API key / BFF session) + capabilities
├── keyring_store.py  OS keyring access; owns APP_NAME
├── state.py          local bookkeeping the client writes (banner version)
├── conversation.py   domain: Message + ConversationStore
├── turn.py           BaseTurnController + TurnController/AgentTurnController,
│                     behind the TurnSink protocol
├── usage.py          token counts, shared by wire/domain/view
├── errors.py         typed errors, each carrying an actionable `hint`
├── logging_setup.py  rotating file log; content redacted unless opted in
├── app.py            bindings, screen stack, palette, startup (~145 lines)
├── app.tcss          stylesheet
├── client/           endpoints.py (URLs), auth.py (AuthProvider seam +
│                     ApiKeyAuth/SessionAuth), device_auth.py (CLI sign-in),
│                     converse.py + events.py (api-converse),
│                     agent_stream.py + agent_events.py (the agent)
├── screens/          chat.py, model_picker.py, splash.py
└── widgets/          transcript messages, composer, status bar
```

637 tests, no network required: `httpx.MockTransport` for HTTP, Textual's
`run_test()` pilot for the UI, a `RecordingSink` for the turn lifecycle with no
app at all, real loopback round-trips for the OAuth receiver, and one replay of a
real captured agent stream.

**Architecture rules that exist for Phase 2 and should not be relaxed:**

- Conversation state lives in `ConversationStore` (App-owned, passed to screens),
  never on the App. A second screen reaching `app._history` is what this replaced.
- A new feature area is a new `Screen` sharing the store, not more widgets on the
  App. `app.py` stays at wiring size.
- A second endpoint is a module in `client/` pairing payload shape with event
  dialect. `events.py` is the api-converse dialect (11 events); the agent stream
  (~35) is a **sibling** dialect module, not an extension of it.
- Ask `Config.credential_source` / `Config.can(Capability.X)`, never `api_key`.
  When signed in there is no API key and the client is still fully configured.
  `resolve_source` now prefers a **session** over an API key, because a session
  is strictly more capable and a transport for it exists. An API-key-only user
  still gets the Converse path.
- `is_complete` says a credential exists, not that this screen can chat. Those
  came apart once already: the screen reported "Ready" and then failed the first
  message telling the user to run a login they had just run. If you add a
  transport, add its check to `ChatScreen.on_mount`.
- `AgentTurnController._interrupt_server()` is where
  `POST /sessions/{id}/interrupt` lives, and it must stay on the cancel path.
  Cancelling only the local stream leaves the server generating and holding the
  session lease. It returns a bool and never raises, because a failure there
  would replace a clean "Stopped" with a traceback.
- Gotcha: `ConversationStore` defines `__len__`, so an empty store is **falsy**.
  Use `store if store is not None else ...`, never `store or ...` — that bug
  silently handed each screen a private copy of the conversation.

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
- **app-api slips `: keepalive` comment frames into idle streams** (every 20s,
  `proxy_routes.py`) because two hops in front of the response cut an idle
  connection. `httpx_sse` skips SSE comments correctly — verified, not assumed,
  by `TestKeepalives` — so they are invisible to the dialect. Anything that
  hand-parses this stream must skip them too, or a slow tool call turns into a
  stream of unknown events.
- **409 from `/chat/stream` is not a failure.** It is the per-session
  single-flight guard saying a turn is already running, relayed deliberately by
  app-api to undo the AgentCore Runtime's rewrite of it to 424. It maps to
  `SessionBusyError`, whose hint says wait or press Esc — a 409 used to fall
  through to `UpstreamError` and tell the user that retrying usually helps,
  which is the one thing that cannot work. Reachable because the TUI can resume a
  conversation someone has open in a browser tab.

## When Phase 2 resumes

Task list and rationale live in
#[[file:docs/specs/CLI_DEVICE_AUTH_SPEC.md]]. Order:

1. Repository + service for device grants (DynamoDB; lookup by
   `device_code_hash` for polls and by `user_code` for browser approval).
   The claim must be a conditional update so two concurrent polls cannot both
   receive the session value.
2. Routes — `/auth/cli/authorize`, `/auth/cli/verify`, `/auth/cli/token`, plus a
   `state_data.device_code` branch in the existing `GET /auth/callback`.
3. The middleware header branch. Must not re-emit or clear cookies on that path.
4. CDK grants table, then a `platform.yml` deploy.
5. TUI client: device polling, and delete the `auth/` package.

**Do not `cdk deploy` locally** — `cdk.context.json` has
`projectPrefix: "agentcore"` with `domainName`/`awsAccount`/`certificateArn`
blank; the real values come from GitHub Variables via `load-env.sh`, and
deploying with blanks risks replacing live CloudFront/ALB/Route53 resources.

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
