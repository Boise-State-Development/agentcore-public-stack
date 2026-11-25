# Native app authentication (iOS companion app)

**Status:** PR 1 (backend) built 2026-11-24; PR 2 (iOS sign-in) merged against a stub. Revised 2026-11-24. Follows the iOS scaffold (PR #1485, `ios/`). The first draft (2026-10-07) was written before the terminal client could sign in with a session; §"What changed since the first draft" lists the differences.
**Driver:** the iOS companion app needs to sign in and reach the same routes the web app reaches.
**Refs:** `backend/src/apis/app_api/auth/bff/routes.py`, `backend/src/apis/app_api/auth/bff/cli_routes.py`, `backend/src/apis/shared/middleware/session_refresh.py`, `backend/src/apis/shared/middleware/csrf.py`, `backend/src/apis/shared/sessions_bff/`, `docs/specs/CLI_DEVICE_AUTH_SPEC.md`, `docs/specs/TUI_WEB_PARITY_SPEC.md`, `ios/README.md`.

## What changed since the first draft

The first draft treated "a session carried in a header" as the missing piece. App-api has since gained that for the terminal client, so most of the first draft's backend PR is already on develop. What the phone still needs is narrower: a way to *obtain* a session that suits a phone, and a lifetime that suits a phone.

| Topic | First draft | Now |
|---|---|---|
| Transport | Build an `Authorization: Bearer <sealed blob>` branch in `SessionRefreshMiddleware` | **Already built** as `Authorization: BFF <sealed>`. The phone presents its session the same way. No work |
| CSRF | Add a pass-through for header requests | **Already built** (`request.state.bff_session_from_header`) |
| Transport tag in the seal | `extras.t = "device"`, each transport rejecting the other's values | **Dropped** (D5) |
| Login | App-bound handoff code with app-side PKCE | **Unchanged.** Still to build |
| Naming | `client=device`, `/auth/device/*`, "device session" | **`native`** throughout. In the code "device" now means the terminal's device-authorization grant (`device_grants`, `device_user_code`), and two meanings of one word in one callback is a bug waiting to happen |
| Provider list | New `GET /auth/providers` | **Already exists**, public, used by the SPA's login page |
| Feature flag | `NATIVE_AUTH_ENABLED` | **No boolean.** The redirect allowlist is the switch (D7) |
| Sign out | New `POST /auth/device/logout` | **Extend `POST /auth/logout`** to a header-presented session, which fixes the terminal too |
| Device metadata | Query parameters on the login URL | **In the exchange body** (a device name is often a person's name, and URLs are logged) |
| Fix found on the way | — | The Runtime-refusal retry got no fresher token on the header path. Fixed with this revision (§D1) |

## Problem

App-api accepts three credentials:

- **The BFF session cookie** (`__Host-bff_session`), the browser's. Set by a 302 at the end of a redirect chain and paired with a double-submit CSRF cookie. A native app signing in through `ASWebAuthenticationSession` never sees it: the auth session's cookie jar is separate from `URLSession`.
- **The same session in a header**, `Authorization: BFF <sealed>`, the terminal client's. It reaches every route the cookie reaches.
- **The API key** (`X-API-Key`), which reaches one tools-less route (`POST /chat/api-converse`).

So the phone already has a way to *present* a session. It has no good way to *get* one. The terminal gets its session through a device-authorization grant (`/auth/cli/*`): it shows a code, the user approves it in any browser, and the terminal polls for the result. That design exists because a terminal cannot receive a redirect. A phone can, and should not inherit the trade the grant makes (D8).

## What already exists

- **Browser login** (`bff_login`, `bff_callback` in `auth/bff/routes.py`): `GET /auth/login` mints `state`, an S256 PKCE verifier, a `nonce` and a browser-binding secret, stores them in the OIDC state store (`apis/shared/auth/state_store.py`, 600 s TTL) and redirects to Cognito, forwarding an optional `provider` as `identity_provider`. `GET /auth/callback` checks the binding cookie, exchanges the code with the confidential BFF app client, writes a `SessionRecord`, syncs the Users row, seals the cookie and 302s to the SPA.
- **Server-side session** (`sessions_bff/models.py`, `repository.py`): a DynamoDB row `PK=SESSION#<id>, SK=META` holding the Cognito access, refresh and ID tokens, `csrf_secret`, `created_at`, `last_seen_at` and `ttl`. A client only ever holds an AES-GCM-sealed `CookiePayload{session_id, version, extras}` (`CookieCodec.seal`). Tokens never leave the server.
- **Header transport** (`SessionRefreshMiddleware`, `sealed_session_from_header`): when a request carries no session cookie, the middleware reads `Authorization: BFF <sealed>`, unseals it with the same codec and runs the same `_resolve_session` path. It sets `request.state.bff_session`, so `get_current_user_from_session` and every route behind it work unchanged. It never writes or clears cookies on that path. The scheme is deliberately not `Bearer`, which upstream means a Cognito JWT.
- **CSRF** (`middleware/csrf.py`): a session resolved from the header is exempt, because a browser never attaches `Authorization` to a cross-site request on its own.
- **Refresh and lifetime**: the access token is refreshed in place when it is within 60 s of expiry. The row's `ttl` slides on an **8 hour idle window** (`BFF_SESSION_TTL_SECONDS`, written at most every 5 minutes), capped at a **30 day absolute lifetime** from `created_at`. Both apply to header sessions exactly as to cookies.
- **The terminal's sign-in** (`auth/bff/cli_routes.py`, `apis/shared/auth/device_grants/`): `POST /auth/cli/authorize`, `GET /auth/cli/verify`, `POST /auth/cli/token`. The callback's device branch gives the terminal a session row of its own and sets no cookies. A rate limiter (`apis.shared.rate_limit.RateLimiter`, counters in the sessions table) fronts all three.
- **Provider list**: `GET /auth/providers` (`auth/routes.py`), unauthenticated, returns each enabled IdP's `provider_id` and `display_name`.
- **Below app-api**: `POST /chat/stream` forwards the session's stored Cognito access token to inference-api. A signed-in terminal already runs tool-using turns this way, which is the evidence that a header session needs nothing new from the Runtime authorizer or the Gateway.

## What is missing

1. **A login exit for a native app.** Today `/auth/callback` ends in a cookie plus a redirect to the SPA, or in the terminal's "approved" page. Neither hands a session to an app.
2. **A lifetime policy for a phone.** An 8 hour idle window is right for a browser tab and wrong for an app opened twice a week.
3. **Sign out for a header session.** `POST /auth/logout` reads only the cookie. The terminal's `logout` deletes its local copy and leaves the row to lapse.
4. **Lifecycle UI.** The user needs to see and revoke signed-in devices, and an admin needs to revoke a user's.

## Decision summary

| # | Decision |
|---|---|
| D1 | The phone presents its session as `Authorization: BFF <sealed>`, the transport that already exists. Cognito tokens stay on the server. No new scheme, no middleware branch. |
| D2 | Login runs the **existing BFF authorization-code + PKCE flow** inside `ASWebAuthenticationSession`, with the existing confidential app client. No new Cognito app client in this phase. |
| D2b | The login passes `provider` through, so the user lands on the organisation's IdP (Entra ID) and never sees the hosted UI. The auth session is **non-ephemeral**, so an existing Safari sign-in, Microsoft's Enterprise SSO plug-in and tenant passkeys all work. No MSAL, no second trust root. |
| D3 | The callback hands the session to the app through a **one-time handoff code** on a custom-scheme redirect, exchanged over HTTPS with an **app-side PKCE verifier**. The sealed value never appears in a URL. |
| D4 | A native session has **no idle slide**. It lives until the 30 day absolute lifetime or until revoked. The row carries `client = "native"` so the middleware knows. |
| D5 | CSRF: nothing to do. The seal carries **no transport tag**. |
| D6 | **inference-api, the Runtime authorizer and the Gateway are untouched.** |
| D7 | **No boolean flag.** The native login exit works only for redirect URIs in `NATIVE_AUTH_REDIRECT_URIS`, which is empty by default. |
| D8 | **Alternatives rejected:** the terminal's device grant as the phone's login; a public Cognito client with tokens on the device; API keys; MSAL. See §Alternatives. |
| D9 | Devices list and revoke (`GET`/`DELETE /auth/devices`), a "Signed-in devices" card in SPA settings, and admin revocation are **PR 3**. |

## Design

### D1 — Present the session the way the terminal does

Nothing to build. The app stores the sealed value it receives from the exchange (D3) and sends `Authorization: BFF <value>` on every request. Because `get_current_user_from_session` only reads `request.state.bff_session`, every user-facing route works for the phone as it stands: `POST /chat/stream`, sessions, attachments, projects, the agent catalog.

A value the server can no longer resolve (expired, revoked, or sealed under a rotated key) is not rejected by the middleware. The request passes through with no session and the route's dependency answers `401`. The app treats any `401` as "signed out" and returns to the sign-in screen without guessing why.

**Fixed alongside this revision.** `SessionRefreshMiddleware.dispatch` built the handle the chat proxy uses to ask for a fresher token after the Runtime refuses one (`request.state.bff_refresh_session`) from `cookie_value`, which is `None` on the header path. The refresh raised inside `_token_for_retry`, which logged a warning and retried with the same token. The handle is now built from the value the request presented, pinned by `test_refresher_handle_works_for_a_header_session`.

### D2 — Reuse the BFF login flow and app client

The app opens `GET /auth/login?client=native&redirect_uri=<app scheme url>&code_challenge=<S256>` in `ASWebAuthenticationSession`, with `provider` when the deployment has a federated IdP (D2b). The route behaves as it does for the browser (state, Cognito PKCE, nonce, binding cookie, 302 to Cognito) with one difference: the `OIDCStateData` stored for this `state` also records the app's `redirect_uri` and `code_challenge`. The binding cookie works because the whole login → Cognito → callback chain runs inside the auth session's own cookie jar.

`redirect_uri` must be in `NATIVE_AUTH_REDIRECT_URIS`, compared as an exact string, or the route answers `404`. Other organisations build their own app with their own bundle id and scheme, so the allowlist is deployment configuration, not code. The app uses its bundle id as the scheme (`edu.example.agentcore://auth/callback`), which keeps one deployment's scheme from colliding with another's on a shared device.

The confidential BFF app client is reused because the code exchange happens on app-api, not on the phone, so there is no secret to protect on the device and no change to any `allowedClients` list. The cost is that a native session inherits the client's refresh-token validity (D4).

### D2b — Straight to Entra, with platform single sign-on

The app reads `GET /auth/providers` and sends the chosen `provider_id` as `provider`, exactly as the SPA's login buttons do. The first page the user sees is then the IdP's. A deployment with no federated IdP gets an empty list, the app omits the parameter, and the user sees the hosted UI. No IdP name is compiled into the app.

The `ASWebAuthenticationSession` is opened with `prefersEphemeralWebBrowserSession = false`. That shares Safari's cookie jar with the auth session, which buys three things without any code on our side:

- **An existing Safari sign-in carries over.** A user signed in to Entra in Safari is signed in to the app with no password prompt.
- **The Microsoft Enterprise SSO plug-in for Apple devices** (pushed by Intune or Jamf on managed devices) completes the Entra login silently. It intercepts Entra traffic from Safari and from any `ASWebAuthenticationSession`.
- **Passkeys.** Where the tenant enables them, the Entra page offers Face ID sign-in inside the same flow.

The price is iOS's standard consent sheet ("AgentCore wants to use *your-host* to sign in"), and the fact that signing out of the app does not sign the user out of Entra in Safari. The latter is correct: the app revokes its own session, and the IdP session belongs to the user and the browser.

### D3 — Handoff code with app-side PKCE

At `GET /auth/callback`, after the existing validation, token exchange, `SessionRecord` write and Users sync, a state that carries a native `redirect_uri` takes its own exit, beside the terminal's:

1. Mark the row `client = "native"` and give it a **provisional** `ttl` of the handoff's 120 s plus 60 s. The exchange gives it the native `ttl` (D4). *(Built this way rather than writing the native `ttl` here: a sign-in the app never finishes would otherwise leave a row holding live Cognito tokens for 30 days that nobody can use or revoke.)*
2. Mint a **handoff code**: 32 random bytes, stored in the OIDC state store under `HANDOFF#<code>` with `{session_id, code_challenge}` and a **120 s** TTL.
3. `302` to `<redirect_uri>?handoff=<code>`. **No session cookie is set**, for the reason the terminal's branch gives: the browser would otherwise be logged in as a side effect and would share one refresh token with the app.

`ASWebAuthenticationSession` returns that URL to the app, which calls `POST /auth/native/exchange` with `{"handoff", "code_verifier", "device_model"?, "app_version"?}` (no `device_name`: the app does not send one, and a device name is often a person's name). The route atomically gets-and-deletes the handoff row (one conditional `DeleteItem` returning the old item), checks `S256(code_verifier) == code_challenge`, writes the native `ttl` and the metadata onto the session row (64 and 32 characters, cut rather than refused), seals `CookiePayload(session_id)` and returns `{"session": "<sealed>", "expires_at": <absolute lifetime>, "user": {…the /auth/session shape…}}`. `session` is the field name the terminal's token response uses. A handoff that is missing, expired, already consumed, or whose verifier does not match answers `400 invalid_grant` and deletes the orphaned session row.

Two rules for the failure paths, both because a non-ephemeral auth session shares Safari's cookies:

- Once the callback has read a native state, a failure redirects to `<redirect_uri>?error=<reason>` and **does not clear session cookies**. The browser path's `_redirect_with_cookies_cleared` would sign the user out of the web app in Safari and load the SPA inside the sign-in sheet.
- Failures before the state is read (missing parameters, no binding cookie, bad state) cannot know they are native and keep today's behaviour. See Risks.

Why not put the sealed value straight in the redirect: custom-scheme redirects are delivered to whichever app registered the scheme, and URLs end up in logs. The handoff is worthless without the verifier, which never left the app's memory, and it is dead after 120 s or one use.

### D4 — Native lifetime policy

A native session does **not** slide. Its `ttl` is set once to `created_at + BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS` (30 days today). The user signs in once, stays signed in for 30 days of any use, then gets a clean "Sign in again". The access token inside the row keeps being refreshed as it nears expiry, as it is for the browser.

`SessionRecord` gains `client: Optional[str]` (absent on browser and terminal rows) and the optional device metadata. Three places must honour it, and missing any one silently returns the session to the 8 hour window:

- `_maybe_slide` still updates `last_seen_at` on the 5 minute throttle, so the devices list can show "last used", but writes the unchanged `ttl`.
- The refresh path computes `new_ttl = min(now + session_ttl_seconds, absolute_cap)`. For a native row it is `absolute_cap`.
- The refresh path rebuilds the record field by field (`updated = SessionRecord(…)`). The new fields must be copied there, or the cached record loses `client` after the first token refresh.

The 30 day ceiling is the one the browser already has, chosen to match the Cognito refresh-token default (the comment on `_DEFAULT_ABSOLUTE_LIFETIME_SECONDS`), so there is no window where the row is alive but cannot be refreshed. **Confirmed in PR 1:** `cognito-construct.ts` sets no `refreshTokenValidity` on the BFF client, so it gets Cognito's 30 day default, and the dev client reads `RefreshTokenValidity: 30` (days) with token revocation enabled. `identity-detailed.test.ts` pins both, so a change to either has to revisit this lifetime.

The refresh path now copies the record with `dataclasses.replace` instead of rebuilding it field by field, so the next field added to `SessionRecord` cannot be dropped the same way.

The terminal keeps the browser's 8 hour window. Giving it this policy is a one-line follow-up once the field exists, and is out of scope here.

### D5 — CSRF and the transport tag

CSRF needs nothing: the header path is already exempt.

The first draft sealed `extras.t = "device"` and had each transport reject the other's values. The terminal shipped without that, and its sealed value is the same `CookiePayload(session_id)` a cookie carries. This revision drops the tag rather than introduce a second population of header values. What it would buy is small: someone holding a browser's httpOnly session cookie already holds that session, and a header value placed in a cookie gains nothing and becomes subject to CSRF. If a tag is ever wanted it should cover the terminal too, in its own change.

### D6 — Nothing changes below app-api

`POST /chat/stream` forwards the stored Cognito access token as it does for the SPA and the terminal. inference-api gets no new routes and no knowledge of how the session was presented (CLAUDE.md "Inference API boundary"). The `OAuth2CallbackUrl` header the SPA sends for MCP consent is absent from the phone's requests; see Open questions.

### D7 — The allowlist is the switch

`NATIVE_AUTH_REDIRECT_URIS` is a comma-separated env var on app-api, from CDK `CDK_NATIVE_AUTH_REDIRECT_URIS` forwarded in `platform.yml`. It is empty by default. While empty, `/auth/login` answers `404` to `client=native` and `/auth/native/exchange` answers `404`.

A separate `NATIVE_AUTH_ENABLED` boolean would protect nothing. The header transport is already on for every deployment, and the login exit cannot work until a deployment supplies its own app's scheme, so "on with an empty list" is not a state worth having. One value answers both "do you want a mobile client?" and "which one?". It is deployment configuration, documented in the docs-site's `configuration/environment-variables.md`, and it does not retire.

### Alternatives considered

| Option | Why not |
|---|---|
| **The terminal's device grant (`/auth/cli/*`) as the phone's login** | It works with no backend change: open `verification_uri_complete` in the auth session and poll. But a device grant is not bound to the device that started it. Whoever holds the device code receives the session once *any* browser completes the verification link (RFC 8628 §5.4). A terminal accepts that because it has no browser. A phone has one, and the handoff binds the session to the app that began the login. It is also a worse experience: up to one poll interval (5 s) before the sheet can close, on a page that says to return to your terminal. |
| **Public Cognito app client + PKCE in the app; the phone holds Cognito tokens** | Puts refresh tokens on the device, which the BFF migration deliberately ended. Needs a second client id in the validator, the Runtime authorizer and the Gateway. Moves refresh, clock skew and rotation into the app. Logout cannot be enforced server-side without a revocation table. |
| **API key** | Reaches one tools-less route. One key per user, 90 day static secret, no device lifecycle. |
| **MSAL for iOS with the Microsoft Authenticator broker** | The most native Entra experience, but the app would hold Entra tokens and bypass Cognito. App-api would need a second trust root, and the Runtime authorizer, which trusts one client of one pool, could not validate the forwarded token. Revisit only if the tenant mandates a Conditional Access policy requiring device compliance or app protection for this app. |
| **Entra native authentication APIs** | Available only to Entra External ID customer tenants, not workforce tenants. |
| **Universal link instead of custom scheme for the redirect** | Stronger (only the associated app can claim the URL) but needs an `apple-app-site-association` file carrying the deployment's team id and new CloudFront wiring. Handoff + PKCE makes scheme capture harmless, so this is a hardening follow-up. |

### iOS side (summary; detail lives with PR 2)

- `AgentCoreKit`: a `NativeSession` actor (sign-in state machine, sealed value in Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, no iCloud sync), an `AuthenticatedClient` that attaches the header and maps `401` to a signed-out state, PKCE helpers, `HandoffExchange`.
- App target: the server-URL entry screen (already backed by `ServerConfiguration`), the `ASWebAuthenticationSession` presenter (non-ephemeral, with the provider from `GET /auth/providers`), `CFBundleURLTypes` for the bundle-id scheme, and a "signed in as" screen that calls `GET /auth/session`.
- The server is discovered from the entered base URL: `{base}/auth/login` and `{base}/auth/native/exchange`. No host is compiled into the app.

**`AuthenticatedClient` does not have to wait for PR 1.** A sealed value from the terminal's sign-in is the same kind of value, so the API client and the first screens can be developed against dev with one, within its 8 hour idle window. That is a development convenience only (D8).

## Cost analysis

Nothing on the model path changes: no prompt content, no `toolConfig`, no history, no cache fingerprint. The TTFT budget is untouched because inference-api is untouched and the proxy forwards the same token the same way.

Per-request cost on app-api is what the terminal's header path costs today: one AES-GCM unseal, one cached single-flight `get_item`, and one Cognito refresh each time the access token nears expiry. Storage is one `SESSION#` row per signed-in phone, a 120 s handoff row per login, and a 5-minute-throttled `last_seen_at` write.

## Security

- **Cognito tokens never reach the phone.** It holds an opaque sealed id, like the browser. Ending the session (`SessionRepository.revoke`) deletes the row, revokes the refresh token and publishes a revocation that every app-api task reads within 5 s. Without that feed, the dev readout measured a signed-out session still answering for about 21 s on tasks that had it cached (the cache TTL is 60 s).
- **Device theft:** the Keychain item is unavailable before first unlock and does not sync. The user or an admin revokes from the devices list (PR 3). Until then the backstop is the 30 day ceiling and the app's own sign out.
- **Scheme hijack:** an app that registers the same scheme receives a handoff code it cannot exchange (no verifier), which expires in 120 s and is single-use.
- **A login link sent to someone else:** a crafted `/auth/login?client=native…` link ends in a redirect to the app scheme on the *victim's* device. The sender never sees the handoff code, and the victim's app has no matching sign-in in progress and ignores it.
- **Replay of the exchange:** atomic get-and-delete on the handoff row, mirroring `get_and_delete_state`.
- **Logging:** the handoff code is the only secret that transits a URL and it is dead within two minutes. The sealed value travels only in a POST response body and an `Authorization` header.
- **Open redirect:** `redirect_uri` is exact-matched against the allowlist. There is no pattern matching and no default entry.
- **Rate limiting:** `/auth/native/exchange` sits behind the same `RateLimiter` as `/auth/cli/*`, keyed per client IP. The handoff is 256 bits and single-use, so this is defence in depth. A wrong verifier is a `400` with no detail about which check failed.
- **Sealing-key rotation** signs every phone out at once, as it does every browser and terminal.

## PR breakdown

**PR 1 — backend native login** (`backend/`, `infrastructure/`, `.github/workflows/platform.yml`, docs-site environment-variables row, CLAUDE.md auth section)
- `NATIVE_AUTH_REDIRECT_URIS`: env, CDK config, workflow variable.
- `client` and device metadata on `SessionRecord` and the repository; the no-slide policy in the three places D4 names.
- `client=native` branch in `/auth/login` and `/auth/callback`; handoff rows; the native failure redirect.
- `POST /auth/native/exchange`, rate-limited.
- `POST /auth/logout` deletes the row for a header-presented session (no cookie writes on that path) and calls Cognito `RevokeToken` on the stored refresh token. The browser path gets the same revoke call.
- Tests: handoff happy path; expired, consumed and wrong-verifier; allowlist empty → `404`; no session cookie set on the native exit; native failures leave session cookies alone; no-slide across a slide and across a token refresh; and header logout. The new routes live in `app_api`, which `tests/architecture/test_import_boundaries.py` already keeps inference-api from importing.
- Dev readout: a native session driving `POST /chat/stream` end to end with `curl`, before any Swift.

This is smaller than the first draft's PR 1 by the middleware branch, the CSRF change, the seal tag, the providers route and the flag wiring.

**PR 2 — iOS sign-in** (`ios/`)
- `NativeSession`, Keychain store, `AuthenticatedClient`, PKCE, handoff exchange, with unit tests against a stub server in `AgentCoreKitTests`.
- Server entry → sign in → "signed in as" flow in the app target; URL scheme; sign out.
- `PrivacyInfo.xcprivacy` gains the UserDefaults reason if the base URL is stored there.

**PR 3 — devices lifecycle** (`backend/`, `frontend/`)
- `GET /auth/devices` (own native sessions: name, model, app version, created, last seen) and `DELETE /auth/devices/{session_id}`; admin `DELETE /admin/users/{id}/devices`.
- The sessions table is keyed by session id and the repository has no per-user read, so listing needs a lookup path. Proposed: a pointer item per native session under the user's id in the same table, written at exchange and deleted with the row. No new table or index.
- "Signed-in devices" card in SPA settings behind `features.nativeDevices`; the app shows the same list.

**What the app builds next** is outside this spec: a read-only list of the user's conversations and their messages (plain JSON, no SSE), then sending a turn with streaming.

**Follow-ups, not scheduled:** a dedicated confidential app client with longer refresh validity; the native lifetime policy for the terminal; universal-link redirect; MDM managed configuration for the server URL; native MCP OAuth consent.

## Testing

- Backend unit tests as listed under PR 1, in the ordinary `backend` suite.
- Local stack: no headless-browser harness. PR 1 allowlists a loopback `redirect_uri` (`http://127.0.0.1/callback`) in the local `.env`; the dev readout completes Cognito in the in-app browser, copies the handoff from the final URL, and exchanges it with `curl`. Cognito only returns to a registered callback, and dev's BFF client registers `http://localhost:8000/auth/callback`, so the local readout needs the app-api that holds `:8000` to be the one with this code. Otherwise run it on dev after deploy with `CDK_NATIVE_AUTH_REDIRECT_URIS` set in the `development` environment.
- `smoke_turns.py --auth` gains a mode that presents a sealed session in the header, so the existing turn matrix (frame order, interrupt/resume, Stop, restore) runs over the transport the phone uses.
- iOS: `AgentCoreKitTests` for the state machine and PKCE; one UI-less integration test against a local stub for the exchange; the simulator smoke in `test-ios`.

## Risks and open questions

- **30 days may be too short for a phone.** A dedicated app client with a longer refresh validity is the lever, and it needs entries in the Runtime authorizer's and the Gateway's `allowedClients`. Decide after PR 2 has real use.
- **Conditional Access can block unmanaged phones.** If the tenant requires a compliant device for all cloud apps, the web flow fails on any phone without the Enterprise SSO plug-in, which is every student device. Confirm with the tenant owner before PR 2.
- **A failed sign-in may sign Safari out of the web app.** Callback failures before the state is read still clear the session cookies, and a non-ephemeral auth session shares Safari's. Check on a device in PR 2; if it happens, those early failures should stop clearing session cookies for every client, since a callback that never proved it belongs to this browser has no business clearing them.
- **MCP OAuth consent on native.** The terminal already faces this and resolves it by showing the authorization URL, letting the user finish in any browser, then resuming the turn (`TUI_WEB_PARITY_SPEC.md`, "OAuth consent needs redesigning, not porting"). The phone can open the same URL in an auth session. Needs its own short design before the app exposes OAuth-gated tools.
- **Request logging.** Verify that no app-api log line or OTel span attribute captures the `Authorization` header. The terminal already depends on this.
- **Sign-out wording after key rotation.** The app's `401` handling must make it a calm "please sign in again", not an error.
- **Cognito self-signup** was open on the production pool when the first draft was written; not re-checked for this revision. A native sign-in surface makes it more visible, and it should be closed before the app is distributed.

## Out of scope

- App Store distribution, TestFlight, signing (`ios/README.md` already excludes these).
- Push notifications and any server-side device registry beyond the session row.
- Biometric re-authentication inside the app. The Keychain accessibility class is the protection; Face ID gating can be added client-side later without a backend change.
- Changing how the terminal signs in.
