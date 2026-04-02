# Code Review: Token Storage Security

**Date:** 2025-03-24
**Scope:** `frontend/ai.client/src/app/auth/auth.service.ts`, `auth.interceptor.ts`, `auth.guard.ts`
**Severity Scale:** 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low

---

## Executive Summary

The application currently stores OAuth access tokens, refresh tokens, and token expiry timestamps in `localStorage`. While the PKCE implementation and CSRF state handling are solid (using `sessionStorage` correctly for ephemeral flow data), the long-lived token storage strategy exposes the application to well-documented attack vectors. This review identifies specific vulnerabilities and proposes a phased migration path.

---

## Current Implementation

### What's Done Well

- **PKCE with S256**: The `login()` flow correctly generates a cryptographic code verifier and SHA-256 challenge. State and code verifier are stored in `sessionStorage` (ephemeral, tab-scoped) — this is correct.
- **CSRF state validation**: `handleCallback()` verifies the `state` parameter before exchanging the authorization code.
- **Token expiry buffer**: `isTokenExpired()` uses a 60-second buffer to preemptively refresh, avoiding edge-case 401s.
- **Interceptor retry logic**: The HTTP interceptor handles expired tokens gracefully with a single retry after refresh.
- **No implicit flow**: The app uses Authorization Code + PKCE exclusively, which aligns with current IETF best practice (draft-ietf-oauth-browser-based-apps-26).

### What's Stored Where

| Data | Storage | Concern |
|------|---------|---------|
| `access_token` | `localStorage` | 🔴 Accessible to any JS in the origin |
| `refresh_token` | `localStorage` | 🔴 Long-lived credential exposed to XSS |
| `token_expiry` | `localStorage` | 🟡 Leaks session timing info |
| `auth_provider_id` | `localStorage` | 🟢 Non-sensitive display data |
| `auth_state` | `sessionStorage` | ✅ Correct — ephemeral CSRF token |
| `auth_code_verifier` | `sessionStorage` | ✅ Correct — ephemeral PKCE data |
| `auth_return_url` | `sessionStorage` | ✅ Correct — ephemeral navigation state |

---

## Findings

### 🔴 F-01: Refresh Token in localStorage

**File:** `auth.service.ts:286`
```typescript
localStorage.setItem(this.refreshTokenKey, response.refresh_token);
```

**Risk:** A single XSS vulnerability anywhere in the origin gives an attacker the refresh token. Unlike access tokens (short-lived), a stolen refresh token grants the attacker the ability to mint new access tokens independently, potentially for hours or days, even after the user closes their browser.

**References:**
- OWASP HTML5 Security Cheat Sheet: *"Do not store session identifiers in local storage as the data is always accessible by JavaScript. Cookies can mitigate this risk using the httpOnly flag."*
- IETF draft-ietf-oauth-browser-based-apps-26, Section 8.5: *"localStorage persists between page reloads as well as is shared across all tabs... localStorage does not protect against unauthorized access from malicious JavaScript."*
- Auth0 Refresh Token Best Practices: Refresh tokens in SPAs should use rotation with automatic reuse detection to limit blast radius.

**Impact:** An attacker with XSS can silently exfiltrate the refresh token and use it from their own machine to generate access tokens. The user would have no indication of compromise. The attacker maintains access until the refresh token expires or is revoked.

---

### 🔴 F-02: Access Token in localStorage

**File:** `auth.service.ts:284`
```typescript
localStorage.setItem(this.tokenKey, response.access_token);
```

**Risk:** The access token is readable by any script running in the same origin. While access tokens are shorter-lived than refresh tokens, `localStorage` persists across tabs and browser restarts, meaning a token could remain available long after the user thinks they've left the application.

**References:**
- IETF draft-ietf-oauth-browser-based-apps-26, Section 8.4: In-memory storage is preferred over persistent storage for access tokens, as it *"limits the exposure of the tokens to the current execution context only."*
- OWASP JWT Cheat Sheet, Token Sidejacking section: Recommends binding tokens to a browser fingerprint via HttpOnly cookies to prevent XSS-based theft.

---

### 🟠 F-03: No Token Binding / Sidejacking Protection

**Risk:** The tokens are pure bearer tokens with no binding to the browser session. If exfiltrated, they work from any HTTP client. The OWASP JWT Cheat Sheet recommends a "user context" fingerprint — a random value sent as an HttpOnly cookie with a SHA-256 hash embedded in the token — so that a stolen token is useless without the corresponding cookie.

**Current state:** The application has no mechanism to detect or prevent token replay from a different client/device.

---

### 🟠 F-04: Token Expiry Stored as Plaintext Timestamp

**File:** `auth.service.ts:289`
```typescript
const expiryTime = Date.now() + response.expires_in * 1000;
localStorage.setItem(this.tokenExpiryKey, expiryTime.toString());
```

**Risk:** An attacker (or malicious browser extension) can trivially modify this value to extend the perceived validity of a stolen token, bypassing the client-side expiry check. The server still validates expiry, but the client will continue sending the expired token without attempting a refresh, which could cause confusing UX failures.

**Note:** This is a medium-severity issue because server-side validation is the real enforcement boundary. However, client-side expiry should not be trivially tamperable.

---

### 🟡 F-05: localStorage Persists After Logout Intent

**File:** `auth.service.ts:298-307`

The `clearTokens()` method does remove tokens from `localStorage`, but if the browser crashes, the tab is force-closed, or the user simply closes the browser without clicking "logout," the tokens remain in `localStorage` indefinitely (until they expire server-side). `sessionStorage` would at least clear on tab close.

---

### 🟡 F-06: id_token Received but Not Stored or Validated

**File:** `auth.service.ts:7`
```typescript
id_token?: string;
```

The `TokenRefreshResponse` interface includes `id_token`, but the `storeTokens()` method silently discards it. If the ID token is being used elsewhere (e.g., decoded for user profile info), it should be validated (signature, issuer, audience, expiry). If it's not needed, the interface should document why it's ignored.

---

### 🟡 F-07: Cross-Tab Token Synchronization Gap

The `storeTokens()` method dispatches a custom `token-stored` event for same-tab notification, but `localStorage` changes in other tabs are only detectable via the native `storage` event. There's no listener for the `storage` event, meaning if a user has multiple tabs open and one tab refreshes the token, other tabs may continue using the old (now potentially rotated/invalid) token until they independently detect expiry.

---

## Recommended Changes

### Option A: Backend-For-Frontend (BFF) Pattern — Recommended

This is the strongest option and is explicitly recommended by the IETF draft for *"business applications, sensitive applications, and applications that handle personal data."* AgentCore handles student data at an institution — this qualifies.

**How it works:**
1. The App API (FastAPI) handles the entire OAuth code exchange server-side
2. Tokens are stored server-side, associated with a session ID
3. The browser receives only an HttpOnly, Secure, SameSite=Strict cookie containing the session ID
4. The App API proxies requests to the Inference API / resource servers, attaching the access token server-side
5. The frontend never sees or stores any OAuth tokens

**What changes:**

| Component | Change |
|-----------|--------|
| `auth.service.ts` | Remove all `localStorage` token operations. Login redirects to App API `/auth/login` endpoint. Session state tracked via cookie presence. |
| `auth.interceptor.ts` | Remove token attachment logic. Cookies are sent automatically. Handle 401 by redirecting to login. |
| `auth.guard.ts` | Check session via a lightweight `/auth/session` API call instead of inspecting local tokens. |
| App API | New `/auth/login`, `/auth/callback`, `/auth/session`, `/auth/logout` endpoints. Server-side token storage (DynamoDB or in-memory with Redis). |
| Cookie config | `HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefix, `Path=/` |

**Mitigates:** F-01, F-02, F-03, F-04, F-05, F-07

**Trade-offs:**
- All API traffic to external resource servers must proxy through the BFF (adds latency, increases backend load)
- Requires server-side session storage infrastructure
- More complex deployment

---

### Option B: Web Worker Token Isolation — Moderate Improvement

If a full BFF is too large a change, isolating the refresh token in a Web Worker is the next best option. This is explicitly called out in the IETF draft (Section 8.3) as a practical pattern.

**How it works:**
1. A dedicated Web Worker handles all token exchange and refresh operations
2. The refresh token never leaves the Web Worker's memory
3. The Web Worker provides the access token to the main thread on request
4. Access tokens are held in-memory only (closure variable), never in `localStorage`

**What changes:**

| Component | Change |
|-----------|--------|
| New `token-worker.ts` | Web Worker that performs code exchange, stores refresh token in its own scope, handles refresh flows |
| `auth.service.ts` | Communicates with the worker via `postMessage`. No `localStorage` for tokens. Access token held in a private variable. |
| `auth.interceptor.ts` | Gets token from `AuthService` in-memory variable instead of `localStorage` |

**Mitigates:** F-01 (fully), F-02 (partially — access token still in main thread memory but not persisted), F-05

**Trade-offs:**
- Access token is still in-memory in the main thread (vulnerable to sophisticated XSS, but not to simple `localStorage` reads)
- Page refresh requires re-obtaining the access token from the worker (or re-triggering a silent auth flow)
- Does not protect against the "Acquisition and Extraction of New Tokens" attack (Section 5.1.3 of the IETF draft) — an attacker with XSS can still run their own OAuth flow

---

### Option C: Minimum Viable Hardening — Quick Wins

If neither Option A nor B is feasible short-term, these changes reduce risk with minimal refactoring:

| # | Change | Addresses |
|---|--------|-----------|
| 1 | **Move tokens from `localStorage` to `sessionStorage`**. Tokens are scoped to the tab and cleared on browser close. This is a one-line change per `getItem`/`setItem` call. | F-05 |
| 2 | **Move refresh token to in-memory only** (private class variable). Accept that page refresh requires a silent re-auth or new login. Keep access token in `sessionStorage` for tab persistence. | F-01 |
| 3 | **Add `storage` event listener** to sync token changes across tabs, or invalidate stale tabs. | F-07 |
| 4 | **Validate or discard `id_token`** explicitly. If used, validate signature/claims. If not, remove from interface or add a comment. | F-06 |
| 5 | **Derive expiry from the token itself** (decode the JWT `exp` claim) rather than storing a separate tamperable timestamp. | F-04 |
| 6 | **Configure Cognito refresh token rotation** if not already enabled. Ensures a stolen refresh token is invalidated after one use. | F-01 |
| 7 | **Shorten access token lifetime** to 5-15 minutes (Cognito default is 60 min). Reduces the window of exploitation for stolen access tokens. | F-02 |

---

## Prioritized Action Plan

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| P0 | Move refresh token to in-memory storage (Option C, item 2) | Small | Eliminates the highest-risk finding |
| P0 | Enable Cognito refresh token rotation (Option C, item 6) | Config change | Defense-in-depth for refresh token theft |
| P1 | Move access token from `localStorage` to `sessionStorage` (Option C, item 1) | Small | Reduces persistence and cross-tab exposure |
| P1 | Shorten access token lifetime to 5-15 min (Option C, item 7) | Config change | Limits blast radius of stolen access tokens |
| P2 | Derive expiry from JWT `exp` claim (Option C, item 5) | Small | Removes tamperable client-side state |
| P2 | Add cross-tab token sync via `storage` event (Option C, item 3) | Small | Prevents stale token usage |
| P3 | Evaluate and plan BFF migration (Option A) | Large | Comprehensive long-term fix |

---

## References

1. [IETF draft-ietf-oauth-browser-based-apps-26](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-browser-based-apps) — OAuth 2.0 for Browser-Based Applications (December 2025)
2. [OWASP HTML5 Security Cheat Sheet — Local Storage](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html#local-storage)
3. [OWASP JWT Cheat Sheet — Token Sidejacking](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html#token-sidejacking)
4. [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
5. [Auth0 — Refresh Tokens: What They Are and When to Use Them](https://auth0.com/blog/refresh-tokens-what-are-they-and-when-to-use-them/)
