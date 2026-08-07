# CLI Device Authorization

**Status:** in progress — backend complete through the routes layer; middleware
branch and TUI client pending
**Audience:** platform maintainers, auth owners
**Supersedes:** the CLI-as-OAuth-client approach reverted in #850

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
- [ ] **TUI** — replace the PKCE flow with device polling; delete
      `tui/src/agentcore_tui/auth/` (~600 lines, 60 tests) and the
      "Browser SSO (OIDC + PKCE)" section of `tui/README.md`. Keep
      `client/auth.py`'s `AuthProvider` seam and add a `SessionAuth`
      implementation; `BearerAuth` becomes unused.
- ~~**CDK** — a grants table with a `ttl` attribute.~~ **Not needed.** Grants
      live in the existing BFF sessions table (see the Storage decision
      above), so there is no `platform.yml` deploy blocking this feature.

## Facts worth not rediscovering

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
- `tests/routes/test_pbt_auth_sweep.py` sweeps **every** registered route and
  asserts 401 when unauthenticated. All three `/auth/cli/*` routes had to be
  added to its `PUBLIC_ROUTE_PATTERNS` allowlist with justification. Any new
  public route needs the same, or the full suite goes red.
- FastAPI rejects `-> Union[HTMLResponse, RedirectResponse]` at import time
  ("Invalid args for response field"). A route returning either must be
  annotated `-> Response`, as `bff_callback` is.
- `CSRFMiddleware` keys off `request.state.bff_session`, **not** the cookie.
  Any future way of populating that state must decide explicitly whether it is
  CSRF-relevant. `.kiro/steering/terminal-client.md` still carries the older,
  incorrect "only enforces when a session cookie is present" wording and should
  be corrected when the TUI work lands.
- `pytest.ini` sets `--disable-warnings`, so warning summaries are hidden. To
  see them: `pytest -o addopts="--import-mode=importlib"`.
- Backend `mypy` is pinned to `python_version = "3.10"` in
  `backend/pyproject.toml` while the project requires 3.13, so every `StrEnum`
  degrades to `str` and produces spurious errors. `mypy --python-version 3.13`
  is clean apart from the `boto3`-untyped set the rest of the codebase already
  reports. Unrelated one-line fix, not gated in CI.
