# Native app authentication (iOS companion app)

**Status:** DRAFT — design for review, no code yet. Follows the iOS scaffold (PR #1485, `ios/`).
**Driver:** the iOS companion app needs a way to sign in that is neither the SPA's httpOnly cookie nor the TUI's API key.
**Refs:** `backend/src/apis/app_api/auth/bff/routes.py`, `backend/src/apis/shared/middleware/session_refresh.py`, `backend/src/apis/shared/sessions_bff/`, `backend/src/apis/shared/harness/grants.py`, `backend/src/apis/app_api/voice/routes.py`, `ios/README.md`, `docs/specs/authenticated-web-assessment.md` (same interrupt/consent vocabulary).

## Problem

The platform has two credentials a client can present to app-api, and neither works for a phone:

- **The BFF session cookie** (`__Host-bff_session`) is the only way a user reaches the agent path (`POST /chat/stream`, sessions, attachments, RBAC-scoped tools). It is set by a 302 at the end of a browser redirect chain, carries `__Host-`/`SameSite=lax` semantics, and is paired with a double-submit CSRF cookie that JavaScript must read. A native app signing in through `ASWebAuthenticationSession` never sees that cookie: the auth session's cookie jar is separate from `URLSession`, and `__Host-` cookies cannot be handed across anyway.
- **The API key** (`X-API-Key`) reaches exactly one route, `POST /chat/api-converse`, a direct Bedrock Converse wrapper with no tools, no memory, no sessions and no agents. It is one key per user, 90-day expiry, and minting a new one revokes the TUI's.

Everything the companion app is for (the same conversations the user has on the web, the same tools, the same quotas) sits behind the cookie. So the app needs a third transport that reaches the same routes with the same `User` and the same RBAC, without re-introducing the thing the BFF migration removed: Cognito tokens held by the client.

## What already exists

The backend already has every building block; what is missing is the transport.

- **Browser login** (`auth/bff/routes.py:281-589`): `GET /auth/login` mints `state`, an S256 PKCE verifier, a `nonce` and a browser-binding secret, stores a digest in the OIDC state store (`apis/shared/auth/state_store.py`, DynamoDB, 600 s TTL) and redirects to Cognito. `GET /auth/callback` validates the binding cookie, exchanges the code with the confidential BFF app client (`token_exchange.py:106`), writes a `SessionRecord`, syncs the Users row, seals the cookie and 302s to `BFF_POST_LOGIN_REDIRECT_URL`.
- **Server-side session** (`sessions_bff/models.py:10-27`, `repository.py`): a DynamoDB row `PK=SESSION#<id>, SK=META` holding the Cognito access, refresh and ID tokens, `csrf_secret`, `created_at`, `last_seen_at` and a `ttl`. The client only ever holds an AES-GCM-sealed `{session_id, version, extras}` blob (`sessions_bff/cookie.py:167-221`). Tokens never leave the server.
- **Refresh** (`middleware/session_refresh.py:54`): per request, unseal the cookie, load the row (single-flight, cached), refresh the Cognito access token in place when it is within 60 s of expiry, slide the 8 h idle TTL, re-emit the cookie. Capped at a 30-day absolute lifetime, which matches the BFF app client's refresh-token validity.
- **Identity resolution** (`auth/dependencies.py:312-377`): `get_current_user_from_session` reads `request.state.bff_session`, re-validates the stored access token with `CognitoJWTValidator`, enriches email/name/roles from the Users table and returns `User{email, user_id, name, roles, picture, raw_token}`. Every user-facing route depends on it (CLAUDE.md "Auth dependency on app_api routes").
- **The chat proxy** (`chat/proxy_routes.py:130-148, 305-318`): `POST /chat/stream` forwards the body to inference-api `/invocations` with `Authorization: Bearer {raw_token}` and streams the SSE back. inference-api's Runtime authorizer validates that Cognito token against `allowedClients: [bffAppClient]` (`inference-agentcore-construct.ts:305-309`).
- **CSRF** (`middleware/csrf.py:56-89`): applies only when `request.state.bff_session` was set from a cookie. The comment already anticipates non-cookie callers: "Bearer-token requests, anonymous public endpoints — bypasses CSRF entirely."
- **Two precedents for a secondary credential minted from a session:** the voice ticket (`voice/routes.py:60-96`, 60 s HMAC ticket consumed once over a WebSocket) and the headless-run grant (`harness/grants.py`), which pins a session's refresh token into its own row with its own revocation lifecycle and a 30-day TTL anchored to the login.

## What is missing

1. A way for a native app to **complete** the Cognito login and come away holding *something* it can present. Today the only output of `/auth/callback` is a cookie plus a redirect to the SPA.
2. A **bearer transport** for the existing server-side session, so a native client's requests populate `request.state.bff_session` exactly as a cookie does and reach every existing route unchanged.
3. A session **lifetime policy for devices**: an 8-hour idle TTL is right for a browser tab and wrong for a phone that is opened twice a week.
4. **Lifecycle UI**: the user needs to see and revoke signed-in devices, and an admin needs to revoke a user's devices.

## Decision summary

| # | Decision |
|---|---|
| D1 | Native clients authenticate with a **device session**: the same `SessionRecord` row the BFF uses, carried as `Authorization: Bearer <sealed blob>` instead of a cookie. Cognito tokens stay on the server. |
| D2 | Login runs the **existing BFF authorization-code + PKCE flow** inside `ASWebAuthenticationSession`, using the existing confidential app client. No new Cognito app client in this phase. |
| D3 | The callback hands the session to the app through a **one-time handoff code** on a custom-scheme redirect, exchanged over HTTPS with an **app-side PKCE verifier**. The sealed session blob never appears in a URL. |
| D4 | Device sessions have **no idle slide**; they live until the 30-day absolute lifetime (the Cognito refresh-token validity) or until revoked. |
| D5 | Bearer-transport requests **skip CSRF** (no ambient credential). A blob sealed for one transport is **rejected on the other**. |
| D6 | **inference-api, the Runtime authorizer and the Gateway are untouched.** The chat proxy forwards the stored Cognito access token exactly as it does for the SPA. |
| D7 | Gated by `NATIVE_AUTH_ENABLED` (backend, default off while in development; CDK `CDK_NATIVE_AUTH_ENABLED`). Off means the three new routes 404 and the bearer branch of the middleware is inert. |
| D8 | **Alternatives rejected:** a public Cognito client with Cognito JWTs on the device; API keys; the OAuth device-authorization grant. See §Alternatives. |
| D9 | Sessions list + revoke (`GET`/`DELETE /auth/devices`), a "Signed-in devices" card in SPA settings, and admin revocation are **PR 3**, behind the same backend flag plus `features.nativeDevices` in the SPA. |

## Design

### D1 — Device session = BFF session over a bearer header

A device session is a `SessionRecord` with one extra attribute, `transport = "device"`, plus display metadata (`device_name`, `device_model`, `app_version`). It lives in the same table under the same `SESSION#` key, so every existing access path (repository, cache, refresh lock, single-flight, user enrichment) applies without change. What the device holds is the same AES-GCM-sealed `CookiePayload` the browser holds, with `extras = {"t": "device"}` so the seal itself records the transport.

`SessionRefreshMiddleware.dispatch` (`session_refresh.py:115`) gains a second entry point: when there is no session cookie and `NATIVE_AUTH_ENABLED` is on, read `Authorization: Bearer <value>`, unseal it with the same codec, and require `extras.t == "device"`. From there it is the existing `_resolve_session` path: row load, refresh-if-needed, `request.state.bff_session = record`. The difference is what happens afterwards:

- no `bff_csrf_token` is derived and no cookies are re-emitted;
- `request.state.bff_transport = "device"` is set so the CSRF middleware and the logout route can tell the two apart;
- an unrecoverable blob returns `401` with `WWW-Authenticate: Bearer error="invalid_token"` rather than a cookie-clear, so the app knows to sign in again.

Because `get_current_user_from_session` only reads `request.state.bff_session`, **every user-facing route works for the device the moment the middleware sets it**, including `POST /chat/stream`, `POST /sessions/{id}/interrupt`, attachments, projects and the agent catalog. Nothing route-level changes. That is the whole point of choosing this over a new credential type.

A cookie value with `extras.t == "device"` is rejected by the cookie branch and a bearer value without it is rejected by the bearer branch (D5). The two populations of blobs are therefore disjoint, so a stolen cookie cannot be replayed as a bearer and a leaked bearer cannot be set as a cookie.

### D2 — Reuse the BFF login flow and app client

The app opens `GET /auth/login?client=device&redirect_uri=<app scheme url>&code_challenge=<S256>&device_name=<label>` in `ASWebAuthenticationSession`. The route behaves exactly as it does for the browser (state, Cognito PKCE, nonce, binding cookie, 302 to Cognito) with two differences: the `OIDCStateData` stored for this `state` records `client=device`, the app's `redirect_uri` and its `code_challenge`; and the binding cookie still works because the whole login → Cognito → callback chain runs inside the auth session's own cookie jar.

`redirect_uri` must be in `NATIVE_AUTH_REDIRECT_URIS` (comma-separated env, CDK `CDK_NATIVE_AUTH_REDIRECT_URIS`), compared as an exact string. Other organisations build their own app with their own bundle id and scheme, so the allowlist is deployment configuration, not code. The app uses its bundle id as the scheme (`edu.example.agentcore://auth/callback`), which keeps one deployment's scheme from colliding with another's on a shared device.

The confidential BFF app client is reused because the code exchange happens on app-api, not on the device, so there is no secret to protect on the phone. This also keeps the inference-api authorizer's `allowedClients` list unchanged (D6). The cost of reuse is that device sessions inherit the client's 30-day refresh-token validity (D4). A dedicated confidential client with a longer validity is a one-line CDK change plus one entry in two `allowedClients` lists; it is listed under follow-ups rather than done now so this phase has zero Cognito and zero authorizer changes.

### D3 — Handoff code with app-side PKCE

At `GET /auth/callback`, after the existing validation and token exchange, a `client=device` state takes a different exit:

1. Create the `SessionRecord` with `transport="device"` and the device metadata, as in the browser path. Sync the Users row as in the browser path.
2. Mint a **handoff code**: 32 random bytes, stored in the OIDC state store under `HANDOFF#<code>` with `{session_id, code_challenge}` and a **120 s** TTL.
3. `302` to `<redirect_uri>?handoff=<code>&state=<state>`. No cookie is set. The response body is empty.

`ASWebAuthenticationSession` returns that URL to the app, which then calls `POST /auth/device/exchange` with `{"handoff": "<code>", "code_verifier": "<verifier>"}` over HTTPS. The route atomically gets-and-deletes the handoff row, checks `S256(code_verifier) == code_challenge`, seals `{session_id, extras: {"t": "device"}}` and returns `{"token": "<sealed>", "expires_at": <absolute lifetime>, "user": {...same shape as /auth/session...}}`. A handoff that is missing, expired, already consumed, or whose verifier does not match answers `400 invalid_grant` and deletes the orphaned session row so a failed exchange leaves nothing behind.

Why not put the sealed token straight in the redirect URL: custom-scheme redirects are delivered to whichever app registered the scheme, and URLs end up in logs. The handoff is worthless without the verifier, which never left the app's memory, and it is dead after 120 s or one use. This is OAuth's own reasoning, applied a second time at the app boundary.

### D4 — Device lifetime policy

Device sessions do **not** slide. `ttl` is set once at creation to `created_at + BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS` (30 days today) and `_maybe_slide` returns early for `transport="device"`. The row still updates `last_seen_at` at most every 5 minutes (the existing write-throttle) so the devices list (D9) can show "last used".

The user's experience: sign in once, stay signed in for 30 days of any use, then a clean "Sign in again" prompt. The access token inside the row keeps refreshing every hour exactly as for the browser; the user never sees that. Matching the ceiling to Cognito's refresh-token validity means there is no window where the row is alive but cannot be refreshed, which is the failure mode the headless grants document under `HeadlessAuthError`.

The app treats `401 invalid_token` as "session is gone" (expired, revoked, or the server rotated its sealing key) and returns to the sign-in screen without guessing why.

### D5 — CSRF and transport separation

`CSRFMiddleware` checks `request.state.bff_transport` and passes device requests through. The threat CSRF defends against (a browser attaching an ambient credential to a cross-site request) does not exist for a header the app attaches deliberately. The unsafe-method, cookie-present path is unchanged.

The seal-level transport tag (D1) is what makes this safe: the only way to be on the bearer path is to present a blob that was sealed for the bearer path.

### D6 — Nothing changes below app-api

`POST /chat/stream` forwards `user.raw_token`, the stored Cognito access token, to inference-api. That token was issued to the BFF app client, which is already the one client the Runtime authorizer and the Gateway allow. inference-api gets no new routes and no cookie or bearer awareness, which is the rule from the BFF migration and CLAUDE.md "Inference API boundary". The `OAuth2CallbackUrl` header the SPA sends for MCP consent flows is simply absent from device requests; see Open questions for what native MCP consent will need.

### D7 — Feature flag

`native_auth_enabled()` in `apis/shared/feature_flags.py`, reading `NATIVE_AUTH_ENABLED`, only `"true"` enables. CDK `infrastructure/lib/config.ts` reads `CDK_NATIVE_AUTH_ENABLED` and sets the env var on app-api only; `platform.yml` forwards the GitHub variable. Off means the `/auth/device/*` router is not mounted, `/auth/login` rejects `client=device` with `404`, and the middleware's bearer branch is not taken. It is a **feature switch** (a deployment may reasonably not want a mobile client), so it stays after promotion to default-on. The SPA gets no flag until PR 3 (`features.nativeDevices`).

### Alternatives considered

| Option | Why not |
|---|---|
| **Public Cognito app client + PKCE in the app; device holds Cognito tokens; app-api accepts `Bearer <Cognito JWT>`** | Puts refresh tokens on the device, which the BFF migration deliberately ended. Requires the validator to accept a second client id, and the Runtime authorizer and Gateway `allowedClients` to include it. Moves refresh logic, clock skew and rotation into the app. Logout cannot be enforced server-side without a token-revocation table. Every one of those is work the device-session design gets for free from the existing row. |
| **API key, like the TUI** | Reaches one tools-less route. One key per user, so the phone and the TUI would keep revoking each other. 90-day static secret with no device lifecycle. |
| **OAuth 2.0 device-authorization grant** | Cognito does not implement it, and it is for input-constrained devices; a phone has a browser. |
| **Universal link instead of custom scheme for the redirect** | Stronger (only the associated app can claim the URL) but requires hosting an `apple-app-site-association` file carrying the deployment's team id, which is per-deployment config and new CloudFront wiring. The handoff + PKCE design makes scheme capture harmless, so this is a hardening follow-up, not a prerequisite. |

### iOS side (summary; detail lives with PR 2)

- `AgentCoreKit`: `DeviceSession` actor (sign-in state machine, token in Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, no iCloud sync), `AuthenticatedClient` that attaches the header and maps `401 invalid_token` to a signed-out state, PKCE helpers, `HandoffExchange`.
- App target: the server-URL entry screen (already backed by `ServerConfiguration`), the `ASWebAuthenticationSession` presenter, `CFBundleURLTypes` for the bundle-id scheme (needs a minimal `Info.plist`; Xcode merges the `INFOPLIST_KEY_*` settings into it), and a "signed in as" screen that calls `GET /auth/session`.
- The server is discovered from the entered base URL: `{base}/auth/login` and `{base}/auth/device/exchange`. No host is compiled into the app.

## Cost analysis

Nothing on the model path changes: no prompt content, no `toolConfig`, no history, no cache fingerprint. The TTFT budget is untouched because inference-api is untouched and the app-api proxy forwards the same token the same way.

Per-request cost on app-api is identical to the cookie path: one AES-GCM unseal, one cached single-flight `get_item`, one Cognito refresh per hour per session. Storage is one `SESSION#` row per signed-in device, a 120 s state-store row per login, and a 5-minute-throttled `last_seen_at` write. At any plausible device count this rounds to zero next to the sessions-metadata table.

## Security

- **Cognito tokens never reach the device.** The device holds an opaque sealed id, like the browser. Revoking the row is instant for every transport.
- **Device theft:** the Keychain item is unavailable before first unlock and does not sync. The user or an admin revokes from the devices list (PR 3). Until PR 3 ships, the backstop is the 30-day ceiling.
- **Scheme hijack:** a malicious app registering the same scheme receives a handoff code it cannot exchange (no verifier), which expires in 120 s and is single-use. It also does not receive `state` for any session it did not start, because `/auth/login` is only reachable from the real app's auth session.
- **Replay of the exchange:** atomic get-and-delete on the handoff row, mirroring `get_and_delete_state` and the voice ticket's `jti` replay table.
- **Cross-transport replay:** rejected by the seal tag (D1/D5).
- **Logging:** the handoff code is the only secret that transits a URL and it is dead within two minutes; the sealed token travels only in a POST response body and an `Authorization` header, which the ALB and CloudFront do not log. Confirm app-api's request logging redacts `Authorization` before PR 1 merges.
- **Open redirect:** `redirect_uri` is exact-matched against a deployment allowlist; there is no pattern matching and no default entry.
- **Rate limiting:** there is no limiter on `/auth/login` or `/auth/callback` today, and `/auth/device/exchange` is a new guessable-input endpoint, so PR 1 adds a small per-IP token bucket in front of it (the handoff is 256 bits and single-use, so this is defence in depth, not the control). A wrong verifier is a 400 with no detail about which check failed.
- **Signing-key rotation:** rotating the cookie data key signs every device out at once, as it does every browser. That is acceptable and should be documented in the ops runbook.

## PR breakdown

**PR 1 — backend device sessions** (`backend/`, `infrastructure/`, `.github/workflows/platform.yml`, docs-site flag row)
- `native_auth_enabled()` flag, CDK config and env wiring, `NATIVE_AUTH_REDIRECT_URIS`.
- `transport` and device metadata on `SessionRecord` and the repository; seal `extras.t`.
- `client=device` branch in `/auth/login` and `/auth/callback`; handoff store on `OIDCStateData`.
- `apis/app_api/auth/device/routes.py`: `POST /auth/device/exchange`, `POST /auth/device/logout`. Logout deletes the row and calls Cognito `RevokeToken` on the stored refresh token. The browser logout only deletes the row today; giving it the same revoke call is a cheap follow-up, since a restored backup must not resurrect a usable credential.
- Bearer branch in `SessionRefreshMiddleware`, `bff_transport` on request state, CSRF pass-through, no-slide policy.
- Tests: handoff happy path, expired/consumed/wrong-verifier, cross-transport rejection, CSRF bypass only on device transport, no-slide, flag off → 404s and inert middleware, and an architecture test that inference-api still has no auth-transport imports.
- Dev readout: a device session driving `POST /chat/stream` end to end with `curl`, before any Swift.

**PR 2 — iOS sign-in** (`ios/`)
- `DeviceSession`, Keychain store, `AuthenticatedClient`, PKCE, handoff exchange, with unit tests against a stub server in `AgentCoreKitTests`.
- Server entry → sign in → "signed in as" flow in the app target; URL scheme; sign out.
- `PrivacyInfo.xcprivacy` gains the UserDefaults reason if the base URL is stored there (otherwise Keychain only, no entry needed).

**PR 3 — devices lifecycle** (`backend/`, `frontend/`)
- `GET /auth/devices` (own device sessions: name, model, app version, created, last seen) and `DELETE /auth/devices/{session_id}`; admin `DELETE /admin/users/{id}/devices`.
- "Signed-in devices" card in SPA settings behind `features.nativeDevices`.
- The app shows the same list and can sign out other devices.

**Follow-ups, not scheduled:** dedicated confidential app client with longer refresh validity (if 30-day re-auth proves too short); universal-link redirect; MDM managed configuration for the server URL; native MCP OAuth consent.

## Testing

- Backend unit tests as listed under PR 1; they run in the ordinary `backend` suite.
- Local stack: no headless-browser harness. PR 1 allowlists a loopback `redirect_uri` (`http://127.0.0.1/callback`) in the local `.env`; the dev readout completes Cognito in the in-app browser, copies the handoff from the final URL, and exchanges it with `curl`. A five-minute manual check, recorded as an L1 row in `docs/testing/smoke-regression.md` once PR 2 exists.
- `smoke_turns.py` gains a `--bearer <token>` option so the existing turn matrix (frame order, interrupt/resume, Stop, restore) runs over the device transport unchanged. That is the regression gate for "every route works for the device".
- iOS: `AgentCoreKitTests` for the state machine and PKCE; one UI-less integration test against a local stub for the exchange; the simulator smoke in `test-ios`.

## Risks and open questions

- **30 days may be too short for a phone.** A dedicated app client with a 90-day or 1-year refresh validity is the lever; it needs entries in the Runtime authorizer and Gateway `allowedClients`. Decide after PR 2 has real use.
- **MCP OAuth consent on native.** The SPA passes `OAuth2CallbackUrl` so the consent round-trip lands back in the web app. A device has no such page. Options: open the consent URL in `ASWebAuthenticationSession` with a device redirect, or complete consent on the web and let the device pick it up on the next turn (the vault already warms tokens across clients). Needs its own short spec before the app exposes OAuth-gated tools.
- **Request logging.** Verify that no app-api log line or OTel span attribute captures the `Authorization` header before PR 1 merges.
- **Key rotation UX.** Rotating `BFF_COOKIE_DATA_KEY_SECRET_ARN` signs every device out with no explanation; the app's 401 handling must make that a calm "please sign in again", not an error.
- **Cognito self-signup** is currently open on the production pool. A native sign-in surface makes that more visible, not more dangerous, but it should be closed independently before the app is distributed.

## Out of scope

- App Store distribution, TestFlight, signing (ios/README.md already excludes these).
- Push notifications and any server-side device registry beyond the session row.
- Biometric re-authentication inside the app. The Keychain accessibility class is the protection; Face ID gating can be added client-side later without a backend change.
- Replacing the TUI's API-key flow. The TUI could adopt device sessions with a loopback redirect (its own roadmap says so), but that is a separate PR against `tui/`.
