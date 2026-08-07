# CLI Device Authorization

**Status:** backend **complete, deployed, and verified end-to-end against
`dev.boisestate.ai`** (2026-08-07). TUI **auth leg done and proven live**; the
`/chat/stream` transport and the turn wiring remain.
**Audience:** platform maintainers, auth owners
**Supersedes:** the CLI-as-OAuth-client approach reverted in #850
**Branch:** `feature/tui-client` — backend landed in `ecf58181`
("feat(auth): CLI device authorization backend"); the TUI agent-stream dialect
and widgets landed in the commit that carries this document.

---

## Handoff summary — read this first

Everything server-side is done, tested, and pushed. What remains is entirely in
`tui/`, plus one end-to-end verification that needs a deployed environment.

**Done (backend, 141 tests; full backend suite 6028 passed / 3 skipped):** domain
layer, repository, service, the three `/auth/cli/*` routes, the device branch in
`GET /auth/callback`, rate limiting, and the `SessionRefreshMiddleware` header
branch. Per-layer detail in the task list below.

**Done (TUI; 128 tests, suite 465 passed):** the agent-stream SSE dialect and the
transcript widgets that render what it carries. Neither needs auth, which is why
they were built first.

**Not started:** the `/chat/stream` transport and wiring the dialect into a
turn. The CLI's device-polling flow and the deletion of the dead PKCE package are
**done**. Detail in "What is left in the TUI" below.

**The backend is no longer just "tested" — it is proven.** All seven steps of
"Verifying after a deploy" were executed against `dev.boisestate.ai` on
2026-08-07, including the browser leg. Results are recorded inline in that
section. Nothing failed. The one-line summary: a CLI-obtained sealed session
reaches `/auth/session`, `/models`, `/sessions` and a full tool-using turn on
`POST /chat/stream`, and is indistinguishable in the table from a session a
browser minted.

**No infrastructure change is required.** Verified rather than assumed: grants
live in the existing BFF sessions table, and app-api's task role already holds an
explicit `BffSessionsAccess` statement
(`infrastructure/lib/constructs/app-api/app-api-iam-grants.ts:174`) granting
`GetItem`/`PutItem`/`UpdateItem`/`DeleteItem`/`Query`/`Scan` on it. That covers
the repository's `TransactWriteItems` as well — per
[AWS's transaction IAM docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)
there is no separate `dynamodb:TransactWriteItems` action, and permission is
governed by the underlying `PutItem`. `BFF_SESSIONS_TABLE_NAME`, already on the
task, is the only env var the feature needs. No `platform.yml` run, no new table,
no Cognito change.

**What cannot be verified without a deploy:** the browser leg. `GET
/auth/cli/verify` redirects to the real Cognito Hosted UI and `GET /auth/callback`
needs a real authorization code. Both sides are covered by tests with a mocked
exchange, but nobody has watched a human complete the flow. See "Verifying after
a deploy".

---

## What this is

How the terminal client (`tui/`) authenticates as a real user, so it can reach
the session-authenticated surface of app-api: `/sessions`, `/models`, `/tools`,
and the tool-using agent behind `POST /chat/stream`.

The CLI obtains a **real BFF session** and presents it in a header. It does not
mint its own Cognito tokens and never talks to Cognito directly.

## Why the previous approach was abandoned

The reverted design (0cbd47ec, reverted in 405d3526 / PR #850) gave the CLI its
own **public Cognito app client** and had it call the AgentCore Runtime's
`/invocations` directly with its own PKCE-minted token.

That looked cheap and was not, for one structural reason:

`POST /chat/stream` forwards `record.cognito_access_token` — the BFF session's
token — upstream to `/invocations`, and the agent forwards **that same token** on
to MCP servers, which validate it in-app (`mcp_external` + Function URL
`AuthType: NONE` + `forward_auth_token`; see
`docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md`).

So a CLI-minted token would arrive at external MCP servers that may pin
`client_id` to the BFF client. **Those servers are deployed outside this
repository**, so the remaining work could not be bounded from here. On top of
that, a second app client carried a permanent tax: a bearer branch on app-api, a
federated-IdP fan-out (with no resync mechanism — a provider created before the
second client existed never reached it), and a separate refresh path because a
public client sends no `SECRET_HASH`.

Routing the CLI through app-api makes all of it moot: the token seen downstream
is always BFF-minted, exactly as it is for the SPA today.

## The seam that makes this small

`SessionRefreshMiddleware.dispatch`
(`backend/src/apis/shared/middleware/session_refresh.py`) does this:

```python
cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
if not cookie_value:
    return await call_next(request)
record, clear_cookie = await self._resolve_session(cookie_value)
request.state.bff_session = record
```

Everything downstream consumes the resulting `SessionRecord` and **none of it
reads a cookie** — `get_current_user_from_session`
(`apis/shared/auth/dependencies.py:217`), the BFF JWT validator, RBAC, profile
enrichment, `raw_token`, the `/chat/stream` upstream forward, quotas, and the
session lease.

So a CLI holding a valid sealed session value needs **one new resolution branch
in one middleware**. No route changes. No dependency changes. No MCP
implications.

CSRF needs one small change, not none. `CSRFMiddleware` gates on
`request.state.bff_session` being set — **not** on cookie presence, despite
what an earlier draft of this document claimed. Attaching a session from a
header therefore drags every state-changing CLI request into CSRF enforcement,
where it fails for want of a double-submit token it cannot hold. The middleware
sets `request.state.bff_session_from_header` and `CSRFMiddleware` exempts it.
That is correct rather than merely expedient: CSRF depends on the browser
attaching a credential to a cross-site request by itself, which is true of
cookies and false of headers — a hostile page cannot set `Authorization`
cross-origin without the server allowing it through CORS preflight.

The sealed value is an AES-GCM envelope over the session id under a
Secrets-Manager-held derived key, deliberately portable so any Fargate task can
unseal it (`apis/shared/sessions_bff/cookie.py`). That portability is what makes
it safe to hand to a CLI.

**No Cognito configuration changes anywhere** — not the app client, callback
URLs, scopes, or identity providers. The browser leg reuses the existing BFF
login against the existing client; `state` is opaque and passed through.

## Flow

```
CLI                          app-api                        browser
 |  POST /auth/cli/authorize    |                              |
 |----------------------------->| create pending grant         |
 |<-- device_code, user_code ---|                              |
 |    verification_uri          |                              |
 |                              |                              |
 |  print URL + user code ------------------------------------->| opens
 |                              |<-- GET /auth/cli/verify ------|
 |                              |    (existing BFF login,       |
 |                              |     device_code in state)     |
 |                              |--- Cognito authorize -------->|
 |                              |<-- GET /auth/callback --------|
 |                              | exchange code (BFF secret),   |
 |                              | mint CLI session, approve     |
 |                              | grant, set NO cookie          |
 |                              |--- "return to terminal" ----->|
 |  POST /auth/cli/token        |                              |
 |----------------------------->| seal session_id, mark claimed |
 |<-- sealed session value -----|                              |
 |                              |                              |
 |  Authorization: BFF <sealed> |                              |
 |----------------------------->| middleware resolves session   |
```

No loopback ports, no PKCE, no browser-to-container networking — which is what
blocked the previous approach in a containerised dev environment.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Session ownership | CLI gets its **own** session | Its browser round trip is a separate authorization exchange, so it has its own refresh token. Two records sharing one would tumble each other on Cognito rotation — the hazard the middleware's per-session lock exists to prevent. Independently revocable. |
| Transport | Sealed value in `Authorization: BFF <sealed>` | Reuses `_resolve_session` verbatim: no new crypto, no new storage, no cookie so no CSRF plumbing. |
| Handoff | Polling (RFC 8628 shape) | No terminal round-trip and no loopback address. |
| `device_code` at rest | SHA-256 hashed | A leaked grant table must yield nothing claimable. Plain SHA-256, not a KDF: the input is 256 bits of machine entropy, so a slow hash would only add latency per poll. |
| What approval records | `session_id` only, never a sealed value | Sealing needs the codec key from Secrets Manager, so the table is not a credential store. |
| Expiry | Checked in application code | DynamoDB TTL deletion is asynchronous and documented as lagging up to 48h, so an expired row stays readable. |
| Storage | Items live in the **BFF sessions table** under `DEVICE-GRANT#` / `DEVICE-USERCODE#` prefixes | Follows `apis.shared.harness.grants`, which already keeps `HEADLESS-GRANT#` items there. The classification argument is easier here: that module pins Cognito refresh tokens, whereas a grant holds a hash and a `session_id`. No new table, no new IAM grant, and **no infrastructure deploy before the feature works**. `DEVICE_GRANTS_TABLE_NAME` moves them to a dedicated table as a config change. |
| Orphaned CLI sessions | Deleted when approval fails at the callback | On the device path nobody holds the freshly minted session — the browser deliberately gets no cookie and the CLI can no longer claim it — so leaving it would strand a live credential until its 8h TTL. |

**User-visible consequence of the session-ownership choice:** signing out in the
browser does **not** sign out the CLI, and vice versa. This matches `gh` and
`aws`. `agentcore-tui logout` deletes the CLI session server-side.

### The `user_code` is low-entropy on purpose

It is human-transcribable, so it cannot carry much entropy. It is therefore
single-use, short-lived (10 min), rate-limited, and **authorises nothing on its
own** — it only identifies which pending grant a separately authenticated
browser session is approving. The alphabet excludes every pair that is
indistinguishable in common terminal fonts (`0/O`, `1/I/L`, `2/Z`, `5/S`, `8/B`)
and all vowels, so a wrong character is a genuine typo rather than a font
problem. That is what lets normalisation safely refuse to "correct" a character
into someone else's code. `slow_down` throttling stops a tight poll loop from
becoming a guessing amplifier.

## Tasks

- [x] **Domain layer** — `apis/shared/auth/device_grants/models.py`:
      `DeviceGrant`, `GrantStatus`, code generation, normalisation, hashing,
      state machine, poll throttle. 33 tests.
- [x] **Repository** — `device_grants/repository.py`. Grant keyed
      `DEVICE-GRANT#<device_code_hash>`; a pointer item
      `DEVICE-USERCODE#<user_code>` holds the hash so the browser lookup needs
      no GSI. Both written in one `TransactWriteItems`, so a user-code
      collision fails loudly instead of retargeting another CLI's grant, and
      a partial create is impossible. Readers deliberately do **not** hide
      expired rows — RFC 8628 needs `expired_token` distinguished from an
      unknown grant. 33 tests.
- [x] **Service** — `device_grants/service.py`. `authorize` / `approve` /
      `deny` / `lookup_pending` / `poll`. 42 tests. The ordering inside `poll`
      is the load-bearing part:
      1. throttle from the *previous* poll's stamp, writing this one after, so
         a client ignoring `interval` never advances past `slow_down`;
      2. read the session and seal **before** claiming, so a revoked session
         or an unreachable Secrets Manager fails non-destructively;
      3. claim last, and return only what the claim returns — the losing poll
         has by then sealed a valid value and must discard it.
- [x] **Routes** — `POST /auth/cli/authorize`, `GET /auth/cli/verify`,
      `POST /auth/cli/token` in `app_api/auth/bff/cli_routes.py`, browser
      screens in `bff/pages.py`, plus the device branch in `GET /auth/callback`.
      24 tests. Two deviations from this spec's original wording:
      the `OIDCStateData` field is named **`device_user_code`**, because it
      carries the *user* code (the server never sees the device code in the
      clear); and `verify` reports unknown, expired, and already-answered
      codes identically, so the endpoint is not a user-code existence oracle.
- [x] **Rate limiting** — reuses `apis/shared/rate_limit.py`. `authorize` and
      `verify` are keyed per client IP; `token` is keyed on the device-code
      hash so one aggressive client cannot exhaust the budget for everyone
      behind a shared egress address. Note the limiter is **fail-open** by
      design, so it is a backstop, not a primary control.
- [x] **Middleware branch** — `sealed_session_from_header` +
      `Authorization: BFF <sealed>` fallback in `SessionRefreshMiddleware`.
      Cookie takes precedence, so no existing request's path changes. The
      header path never writes or clears cookies, but it *does* still slide
      the DDB TTL — an active CLI keeps its session alive like an active
      browser. Also required the `CSRFMiddleware` exemption described above
      (`bff_session_from_header`), which this spec originally said was
      unnecessary. 23 tests.
- [~] **TUI** — partly done. See "What is left in the TUI" below for the split
      and the two open design questions.
- ~~**CDK** — a grants table with a `ttl` attribute.~~ **Not needed.** Grants
      live in the existing BFF sessions table (see the Storage decision
      above), so there is no `platform.yml` deploy blocking this feature.

---

## What is left in the TUI

### Already built (auth-independent)

Both pieces were built ahead of auth because neither needs a session, and the
existing code was explicitly waiting for them — `turn.py`'s docstring says its
handler registry "has to grow to roughly thirty-five when the agent stream
lands".

**`tui/src/agentcore_tui/client/agent_events.py`** — the agent-stream dialect, a
*sibling* of `client/events.py` rather than an extension of it. Typed events for
the 23 the SPA handles plus `metadata_summary`, `tool_error` and `tool_progress`,
`parse_agent_event()`, and `AgentTurnAccumulator` (with `ToolCallRecord`) folding
a turn into text, tool calls, citations, usage, title, quota notices, artifacts
and interrupts. 87 tests, no network. Wire shapes were taken from the SPA's
`stream-parser-types.ts` + `session/services/models/message.model.ts`, the
backend's `apis/shared/harness/sse.py`, and the SSE table in `CLAUDE.MD`.

**`tui/src/agentcore_tui/widgets/agent_content.py`** — `ToolCall`, `Citations`,
`QuotaNotice`, `CompactionNotice`, `ArtifactCard`, `InterruptNotice`, plus a new
"agent-stream content" section in `app.tcss`. 41 tests, mounted into the real
transcript through `run_test` so the stylesheet is parsed and exercised.
`ToolCall` holds the same `ToolCallRecord` the accumulator mutates, so a caller
updates the record and calls `refresh_from_record()` — there is no second copy of
the state to keep in step.

### Validated against the real stream — and it found a bug

Both modules above were written from the SPA's types and the SSE table in
`CLAUDE.MD`. Neither is the wire. During the end-to-end verification a real turn
was captured off `POST /chat/stream` (claude-haiku-4-5, `calculator` enabled) and
replayed through `parse_agent_event()`. That capture is now
`tui/tests/fixtures/live_agent_stream.sse`, asserted by
`tui/tests/test_agent_stream_live_replay.py` (10 tests).

Three claims the dialect rests on held up on real bytes: 17 of the capture's 41
frames are Strands passthrough (`event` ×14, `message` ×3), `metadata` fired 3
times for one turn, and two assistant messages opened and closed so the answer is
the last non-empty one (`162708`, correct).

**The bug it found:** `init_event_loop` and `start_event_loop` parsed as
`UnknownEvent`. They are Strands event-loop lifecycle frames with empty payloads;
`start_event_loop` fires once per LLM call, so a tool turn emits several. Neither
source the dialect was written from names them, because the SPA drops them
through its `switch` default rather than listing them — so **no fixture could
have caught this.** Fixed as a `LIFECYCLE_EVENT_NAMES` set resolving to
`IgnoredEvent(reason="lifecycle")`, kept separate from
`PASSTHROUGH_EVENT_NAMES` because the reasons differ and the reason is the part
worth keeping: passthrough frames are dropped because they *duplicate* content
(consuming them doubles the answer), these because they carry none.

**Not a bug, but load-bearing for whoever renders usage:** the stream carries
**no `metadata_summary` at all.** `stream_coordinator.py` deliberately swallows
it (`continue`, in the `metadata_summary` branch) because Strands'
`accumulated_usage` sums each call's full context and therefore overstates
current occupancy; it sends a final per-call `metadata` instead. Consequences:

* `AgentTurnAccumulator.usage` is the **last LLM call's context size**, not the
  turn's token total. In the capture that is 3360 in / 5 out, where the turn
  actually consumed 3286 + 3360 in and 57 + 5 out.
* That is exactly right for a context-window percentage — it is what the SPA's
  context badge uses — and **wrong as a "tokens used" or cost figure.** Label it
  as context wherever it lands in the status bar.
* `MetadataSummary` and `_turn_usage` are therefore unreachable on this path.
  Keep them (a future server may send one) but do not rely on them.

### Still to do

1. **Transport** — `client/agent_stream.py`: a module pairing the `/chat/stream`
   payload shape with the dialect above, mirroring what `converse.py` does for
   `events.py`. **This is now the next thing**: the auth leg below is done, so
   there is a session to carry.
2. ~~**Auth leg**~~ — **done.** `client/device_auth.py` (`DeviceAuthClient`,
   `DeviceAuthorization`, `DeviceSession`) drives
   `POST /auth/cli/authorize` → poll `POST /auth/cli/token`; `SessionAuth` on the
   existing `AuthProvider` seam sends `Authorization: BFF <sealed>`; the sealed
   value is stored under its own keyring service (`agentcore-tui-session`).
   Proven live: authorize → 19 polls at the advertised 5s interval with no
   `slow_down` → claimed, then `GET /models` returned 200 on the header alone.
   29 tests, no sockets and no real waiting (`sleep` and `clock` are injected).
   Three things in there are load-bearing:
   * an **unknown** `error` code is terminal, not "keep waiting" — treating it as
     pending polls a dead grant for the full ten minutes and then reports a
     timeout instead of what the server said;
   * `slow_down` widens the interval **permanently**, because the server
     throttles from the *previous* poll's timestamp: a client that reverts to its
     original interval can be told to slow down forever;
   * the loop never sleeps past the deadline, so an expiry is reported when it
     happens rather than an interval later.
3. ~~**Delete the dead PKCE package**~~ — **done.** `tui/src/agentcore_tui/auth/`
   and `tests/test_auth.py` removed (1,314 lines), along with
   `cognito_domain_url`, `cli_client_id`, `callback_ports`, `sso_configured` and
   the `--provider` flag. `CredentialSource.SSO_SESSION` became `BFF_SESSION` and
   `BearerAuth` was deleted rather than left as a seam: it existed for a CLI that
   minted its own tokens, which is the design #850 reverted. `AuthProvider` is
   the seam; a provider is not.

   No migration path was kept for the retired `agentcore-tui-sso` keyring
   service, and that is deliberate. The package has never been published, the
   PKCE login existed on this branch for about one day, and the revert deleted
   the Cognito app client — so any refresh token still sitting in a developer's
   keyring is already unusable, because Cognito rejects a refresh for a client
   that no longer exists. A `LEGACY_SSO_SERVICE` constant would have implied a
   released user base that does not exist. Clear a stale entry by hand if you
   have one:
   `python3 -c "import keyring; keyring.delete_password('agentcore-tui-sso', '<base-url>')"`.
4. **Turn wiring** — see the open question below.
5. **`cancel()` must interrupt server-side** — `TurnController.cancel()` is local
   only today. Against the agent endpoint it must also
   `POST /sessions/{id}/interrupt`, or the server keeps generating and holds the
   session lease, locking the user out of their own conversation. The call
   belongs in `TurnController`, not the screen; there is already a docstring
   there saying so.
6. **Never transparently retry a stream** — a reopen double-runs the turn and
   corrupts memory. Surface the failure instead.

### The container needs its keyring switched on

`login --sso` stores a live platform session, so it needs somewhere encrypted to
put it. A container has no login session, therefore no D-Bus session bus and no
unlocked keyring, and Python's `keyring` then selects `backends.fail.Keyring` and
raises on every write — which presents as "no keyring is installed" even after
the packages are in the image.

Resolved by giving the devcontainer a real one rather than weakening where the
credential goes. Plaintext-on-disk and environment-variable fallbacks were both
considered and rejected: they would have made the container the least safe place
to sign in, and it is the place we sign in most.

* `.devcontainer/Dockerfile` installs `gnome-keyring libsecret-1-0 dbus-x11`,
  which is enough for `keyring` to select its `SecretService` backend with **no
  client-side code at all** — `config.save_session_to_keyring` and friends work
  unchanged.
* `scripts/local-dev/keyring-init.sh` starts the bus and unlocks the keyring with
  a passphrase the developer types. The passphrase is never stored; a passphrase
  in a file would sit next to the thing it encrypts.
* The Dockerfile writes the bus-address rejoin to **`/etc/profile.d/`**, not
  `~/.bashrc`. `docker exec ... bash -lc '...'` is a *non-interactive* login
  shell and Ubuntu's stock `~/.bashrc` returns on its fourth line for exactly
  that case, so anything appended there is unreachable from every scripted exec
  in this repo's docs. (Worth knowing: the Dockerfile's pre-existing `~/.bashrc`
  PATH block has the same problem.)

Verified in a clean container built from the changed image: `fail.Keyring` before
init, `SecretService.Keyring` after, a session written in one `docker exec` and
read in another, `agentcore-tui status` reporting `credential  : signed in`, the
value absent from the keyring file on disk, and the session still readable after
a `docker restart` when `~/.local/share/keyrings` is a named volume.

Three traps found while building it, each of which produced a *working* keyring
inside the script and a broken one in the next command:

1. **`--components=secrets` is mandatory.** Without it the daemon starts but
   never claims `org.freedesktop.secrets`, so there is no Secret Service to find.
2. **The daemon must `--daemonize`.** Backgrounding with `&` dies with the
   `docker exec`, and because `org.freedesktop.secrets` is D-Bus *activatable*,
   the next request spawns a replacement that never received a passphrase — so
   the collection is locked. `setsid` is not a substitute: it detaches the
   process before it reads the passphrase from the pipe.
3. **A stale bus address outlives its daemon.** The socket file in `/tmp`
   survives a container restart, so testing `[ -S "$socket" ]` reuses a dead bus.
   Liveness has to be a real `ListNames` call.



### Open question 1: how to wire the dialect into a turn

`TurnController` is currently single-dialect in two ways that both need a
decision, and the right answer depends on the transport, which is why this was
left alone rather than guessed at:

* `begin()` hardcodes `self._accumulator = TurnAccumulator()`.
* `_handlers` is typed `dict[type[ConverseEvent], EventHandler]`.

The registry itself already works for both dialects, since the event classes are
distinct types. Two viable shapes:

**(a) Generalise `TurnController`.** Inject an accumulator factory and widen the
handler mapping to a common base. One controller, one buffering/flush
implementation, one place where cancel lives. Risk: a class serving two dialects
accumulates conditionals, and the accumulators expose different properties
(`AgentTurnAccumulator.text` is the *last* message; `TurnAccumulator.text` is
everything).

**(b) A sibling `AgentTurnController`.** Mirrors the sibling-dialect decision and
keeps each controller honest about its own stream. Risk: duplicating the
buffer/flush logic, which is genuinely shared and easy to let drift.

The steering doc's phrasing ("the list that grows to ~35 entries") reads as (a),
but it was written before the accumulators diverged. Whoever picks this up should
decide with the transport in hand.

### Open question 2: `TurnSink` needs new callbacks

`TurnSink` is five methods covering text, reasoning, usage, state and notices.
The agent stream carries six more kinds of thing, each with a widget already
built and unused. Suggested additions, but the naming and granularity are
genuinely open:

| Event | Widget | Note |
|---|---|---|
| `tool_use` / `tool_result` / `tool_progress` | `ToolCall` | Mount on first sight, then `refresh_from_record()`. Do **not** re-mount per event. |
| `citation` | `Citations` | Batch them: mount once at end of turn, not one widget per citation. |
| `quota_*` | `QuotaNotice` (or `quota_notice_for`) | `QuotaExceeded` means the turn never ran — the UI should not show a pending assistant message. |
| `compaction` | `CompactionNotice` | Sum the deltas; one notice per turn, not per event. |
| `artifact` | `ArtifactCard` | Dedupe by id, highest version — the accumulator already does. |
| `oauth_required` / `tool_approval_required` | `InterruptNotice` | The turn is paused, not failed. Do not report it as an error. |
| `session_title` | — | Set `screen.sub_title` / the header. May arrive **after** `done`. |

Keep the protocol uniformly async, for the reason its docstring already gives.

---

## Verified after deploy — 2026-08-07

All seven steps below were executed against `https://dev.boisestate.ai` after a
`backend.yml` code deploy of `8a616d43`. **Every one passed.** No infrastructure
deploy was needed, exactly as predicted.

Each step keeps its original instructions so it can be re-run on another
environment; the ✅ notes record what the deployment actually did.

1. **Confirm the routes are live.**
   `curl -s https://<host>/api/openapi.json | jq '.paths | keys | map(select(startswith("/auth/cli")))'`
   should list `/auth/cli/authorize`, `/auth/cli/verify`, `/auth/cli/token`.
   → ✅ all three.
2. **Mint a grant.** `curl -XPOST https://<host>/api/auth/cli/authorize` →
   expect `device_code`, `user_code`, `verification_uri`,
   `verification_uri_complete`, `expires_in`, `interval`. Check
   `verification_uri` is the right host: it is *derived* from
   `BFF_AUTH_CALLBACK_URL` by swapping the last path segment, so a deployment
   whose routing does not put `/auth/cli/verify` beside `/auth/callback` needs
   `BFF_CLI_VERIFICATION_URL` set explicitly.
   → ✅ 200 with all six fields; `expires_in` 600, `interval` 5. The derivation
   landed on `https://dev.boisestate.ai/api/auth/cli/verify`, so **no
   `BFF_CLI_VERIFICATION_URL` override is needed on this deployment.**
3. **Poll once before approving.** `POST /auth/cli/token` with the `device_code`
   → expect HTTP **400** with `{"error": "authorization_pending"}`. Poll again
   immediately → expect `{"error": "slow_down"}`.
   → ✅ both. The throttle-from-the-*previous*-poll ordering behaves live.
4. **The browser leg (the untested part).** Open `verification_uri_complete`.
   Expect a Cognito redirect, a normal sign-in, then a "You're signed in" page.
   Confirm in devtools that this response sets **no** cookies — that is the
   invariant the whole no-cookie design rests on, and it is the thing most likely
   to have been broken by a proxy or CloudFront behaviour rewriting headers.
   → ✅ completed by a human; no cookies set. Neither CloudFront nor the ALB
   rewrites the response.
5. **Claim.** Poll again → expect HTTP 200 with `session`, `expires_in`,
   `user_id`, `username`. Poll a third time → expect 400 `invalid_grant`; the
   value is single-use.
   → ✅ 200 with all four (`expires_in` 28783), then 400 `invalid_grant`.
6. **Use it.**
   `curl -H "Authorization: BFF <session>" https://<host>/api/auth/session`
   should return the user. Then try a state-changing request (e.g.
   `POST /api/sessions`) — a **403 with a CSRF message here means the
   `bff_session_from_header` exemption is not reaching `CSRFMiddleware`**, which
   is the single most likely integration failure. Check middleware ordering.
   → ✅ `/auth/session` returned the user with `roles: ["system_admin"]`.
   `POST /memory/search` with **no** CSRF token returned 200, so the exemption
   reaches `CSRFMiddleware`. Both endpoints 401 without the header, so the
   middleware branch is doing the work rather than something else letting the
   request through. `GET /models` and `GET /sessions` also served the header.
7. **Check the table.** Grant items appear under
   `PK = DEVICE-GRANT#<sha256>` and `PK = DEVICE-USERCODE#<code>` in the BFF
   sessions table, both carrying `ttl`. The device code itself must not appear
   anywhere in plaintext.
   → ✅ both items present with `SK = META` and the same `ttl`, so DynamoDB
   reaps them together. A full-table scan confirmed **the raw device code
   appears nowhere**; only `device_code_hash` does. The grant recorded the whole
   lifecycle (`created_at` → `approved_at` → `claimed_at`, `poll_count` 4,
   `status: claimed`), and its `session_id` matched a real `SESSION#` item —
   the CLI's session is an ordinary BFF session, which was the point.

Rollback is a redeploy of the previous image: the routes vanish and nothing else
changes, because no existing code path was modified except the two middleware
branches, both of which are inert without a `BFF` header.

### Also confirmed live, beyond the checklist

* **Rate-limit keying is what the Tasks section claims.** The table showed
  `RATE#cli-authorize:<client-ip>` and `RATE#cli-verify:<client-ip>` alongside
  `RATE#cli-token:<device-code-hash>` — per-IP for the two browser-reachable
  routes, per-flow for polling. Note the `SK` bucket width differs per route
  (`WIN#<now // window>`, so `now // 300` for authorize/verify and `now // 60`
  for token); an absent row usually means the window's `ttl` (`window * 2`)
  lapsed and DynamoDB reaped it, not that the limiter is missing. Reaping is
  lazy and unordered, so expired rows can outlive newer ones.
* **`verify` is genuinely not a user-code existence oracle.** A code that
  existed and was already claimed and a code that never existed return
  **byte-identical** responses ("That code has expired", same sha256). Only
  syntactically invalid input differs ("That code doesn't look right"), which
  leaks nothing about existence — it is a typo hint.
  ⚠️ When re-testing this, build the "nonexistent" code from
  `USER_CODE_ALPHABET` (`CDFGHJKMNPQRTVWXY34679`). An obvious probe like
  `ZZZZ-ZZZZ` or `AAAA-AAAA` fails the *shape* check instead and returns the
  other page, which looks exactly like an oracle and is not one.

## Facts worth not rediscovering

### Auth and the backend

- `get_current_user_from_session` is **not** cookie-only in the way its
  docstring implies. It reads `request.state.bff_session`, so the integration
  point is the middleware, not the dependency.
- Cognito has **no** device authorization grant (RFC 8628). This flow is
  app-api's own, reusing the existing BFF login for the browser leg.
- Cognito matches `redirect_uri` byte-for-byte and does not honour RFC 8252's
  variable-loopback-port rule. This is why loopback redirects were a dead end in
  a container.
- The BFF app client is confidential (`generateSecret: true`), which is why the
  code exchange must happen in app-api and not in the CLI.
- Native Cognito users exist alongside federated `ms-entra-id_*` users. Native
  users lack `custom:provider_sub`.
- The dev environment is prefix `dev-boisestateai-v2`, API base
  `https://dev.boisestate.ai/api`.
- `CSRFMiddleware` keys off `request.state.bff_session`, **not** the cookie.
  Any future way of populating that state must decide explicitly whether it is
  CSRF-relevant. `.kiro/steering/terminal-client.md` still carries the older,
  incorrect "only enforces when a session cookie is present" wording and should
  be corrected when the TUI work lands.
- There is no `dynamodb:TransactWriteItems` IAM action; transactional writes are
  authorized by the underlying `PutItem`/`UpdateItem`/`DeleteItem` permissions.
  CDK's `grantReadWriteData` does not list a transaction action either, and does
  not need to.

### Tests and tooling

- `tests/routes/test_pbt_auth_sweep.py` sweeps **every** registered route and
  asserts 401 when unauthenticated. All three `/auth/cli/*` routes had to be
  added to its `PUBLIC_ROUTE_PATTERNS` allowlist with justification. Any new
  public route needs the same, or the full suite goes red. This is the guard that
  caught the routes work; it is easy to miss because it lives nowhere near the
  code it checks.
- FastAPI rejects `-> Union[HTMLResponse, RedirectResponse]` at import time
  ("Invalid args for response field"). A route returning either must be
  annotated `-> Response`, as `bff_callback` is.
- `pytest.ini` sets `--disable-warnings`, so warning summaries are hidden. To
  see them: `pytest -o addopts="--import-mode=importlib"`.
- Backend `mypy` is pinned to `python_version = "3.10"` in
  `backend/pyproject.toml` while the project requires 3.13, so every `StrEnum`
  degrades to `str` and produces spurious errors. `mypy --python-version 3.13`
  is clean apart from the `boto3`-untyped set the rest of the codebase already
  reports. Unrelated one-line fix, not gated in CI.
- **The backend is not `black`-clean.** Running `black` across
  `src/apis/shared/middleware/` reformatted large stretches of pre-existing
  hand-wrapped code — 15 unrelated hunks in the file that handles session auth.
  That was reverted and the edits reapplied by hand. Format only the files you
  add, or a security-critical diff becomes unreviewable. `tui/` *is*
  black-clean, so there the whole project is safe to format.

### The TUI and the agent stream

- `cd tui && uv sync` does **not** install pytest. Use `uv sync --all-extras`.
  Gates: `uv run pytest -q`, `uv run ruff check src tests`,
  `uv run black --check src tests`, `uv run mypy src`. All four are clean today
  and `mypy` in `tui/` genuinely passes, unlike the backend's.
- The stream interleaves raw Strands passthrough frames (`event`, `message`,
  `result`) with the typed events. **They restate content the typed events
  already carry, so handling them doubles every assistant response.** They parse
  to a distinct `IgnoredEvent`, not `UnknownEvent`, specifically so that a later
  "log the unknown events" change cannot start doubling output.
- `metadata` fires **per LLM call** — a turn using two tools emits three of them.
  Only `metadata_summary` is a whole-turn total and it is authoritative for cost.
- The answer is the **last non-empty assistant message**, not the concatenation
  of all of them. Each tool round trip closes a message and opens another, so
  concatenating splices the model's pre-tool narration onto the real answer.
- On `content_block_delta`, the block type is inferred from **which field is
  present** (`text` vs `input`), not from `type`. The SPA does the same. Treating
  an `input` delta as prose leaks raw JSON into the answer.
- A bare `data:`-only frame carries its event name in `type`, which collides with
  the *block* type field on `content_block_start`. Presence of `toolUse` breaks
  the tie.
- `tool_use`'s `input` is a **partial JSON prefix** while the model streams the
  arguments, and the same `tool_use_id` is re-emitted. Parse failure there is
  normal, not an error, and a partial re-emit must not wipe already-known
  arguments.
- `tool_result` arrives in three nestings: `{tool_result: {...}}` (the SPA's
  declared shape), `{message: {content: [{toolResult: {...}}]}}` (Strands), and
  flat `{toolUseId, result}` (the event formatter).
- Fields appear in camelCase *or* snake_case depending on which backend path
  emitted the frame, so every id lookup accepts both.
- `session_title` may arrive **after** `done`. Do not gate it on turn completion.
- MCP App UI (`ui_resource`, `ui_tool_input_partial`) and artifacts are HTML in
  sandboxed iframes. There is no terminal rendering; the widgets say so rather
  than pretending.
- Textual containers default to `height: 1fr`. Any widget between `#transcript`
  and its text that does not set `height: auto` makes the transcript
  unscrollable, silently clipping every answer past the first screenful — it
  presents as responses truncating mid-sentence. `TestTranscriptGrowth` in
  `tests/test_agent_widgets.py` guards it; keep new widgets covered there.

---

## Smaller decisions taken, open to reversal

None of these block progress; each was a judgement call made to keep moving, and
any of them can be changed cheaply.

| Choice | Alternative if you disagree |
|---|---|
| `verification_uri` is **derived** from `BFF_AUTH_CALLBACK_URL` by swapping the last path segment, with `BFF_CLI_VERIFICATION_URL` as an override. | Add a required env var. Rejected because a second URL can drift out of agreement with the Cognito-registered callback, and `is_ready()` already guarantees the callback is set. |
| The `OIDCStateData` field is `device_user_code`, not the spec's original `device_code`. | It holds the *user* code; the server never sees the device code in the clear. The original name would have been actively misleading at the call site. |
| `/auth/cli/verify` reports unknown, expired and already-answered codes with one identical page. | Distinguishing them would give an unauthenticated caller an existence oracle for user codes. |
| The two browser pages are server-rendered from `bff/pages.py`. | Routing them through the SPA would make a CLI login depend on the SPA's router and build for two screens that never change. They take no request input, which is the XSS argument. |
| Rate limits: authorize 10/5min per IP, verify 20/5min per IP, token 40/min **per device-code hash**. | Token is keyed on the grant rather than the IP so one aggressive client cannot throttle everyone behind a shared egress address. Note `RateLimiter` is fail-open. |
| The whole backend landed as **one commit**. | Each layer was independently green, so it can be split into four for review if preferred — say so before opening the PR. |

## Documentation follow-ups

- `.kiro/steering/terminal-client.md` says "CSRF needs no work for a header
  client: `CSRFMiddleware` only enforces when a session cookie is present." That
  is **wrong** — see the CSRF paragraph above. Fix it when the TUI work lands.
- The same doc's "Phase 2" section still describes the reverted app-client design
  as pending and lists the CDK grants table as a task. Both are stale.
- `tui/README.md` still documents "Browser SSO (OIDC + PKCE)", which is dead.


