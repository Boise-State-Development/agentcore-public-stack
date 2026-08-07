# CLI Device Authorization

**Status:** in progress — domain layer landed, everything else pending
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
implications. CSRF stays dormant because `CSRFMiddleware` only enforces when a
session cookie is present.

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
- [ ] **Repository** — DynamoDB persistence. Needs lookup by `device_code_hash`
      (poll) *and* by `user_code` (browser approval); a second item keyed
      `USERCODE#<code>` pointing at the hash avoids a GSI.
- [ ] **Service** — create / approve / single-use claim, with the claim being a
      conditional update so two concurrent polls cannot both receive the value.
- [ ] **Routes** — `POST /auth/cli/authorize`, `GET /auth/cli/verify`,
      `POST /auth/cli/token`, plus the `state_data.device_code` branch in the
      existing `GET /auth/callback` (`app_api/auth/bff/routes.py:293`). Add
      `device_code` to `OIDCStateData` (`apis/shared/auth/state_store.py:14`).
- [ ] **Middleware branch** — header fallback in `SessionRefreshMiddleware`.
      Must not re-emit or clear cookies on the header path.
- [ ] **CDK** — a grants table with a `ttl` attribute. Requires a
      `platform.yml` deploy before the feature works.
- [ ] **Rate limiting** — on `/auth/cli/verify` (user-code guessing) and
      `/auth/cli/token` (poll abuse).
- [ ] **TUI** — replace the PKCE flow with device polling; delete
      `tui/src/agentcore_tui/auth/` (~600 lines, 60 tests) and the
      "Browser SSO (OIDC + PKCE)" section of `tui/README.md`. Keep
      `client/auth.py`'s `AuthProvider` seam and add a `SessionAuth`
      implementation; `BearerAuth` becomes unused.

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
