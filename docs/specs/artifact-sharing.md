# Artifact sharing

**Status:** proposed
**Feasibility:** high — no new AWS resources, no new IAM, no CSP change
**Builds on:** artifacts feature (#306–#311), conversation sharing
(`backend/src/apis/app_api/shares/`), render-token contract
(`backend/src/lambdas/artifact_render/handler.py`)

## Summary

Users can create artifacts (`create_artifact` / `update_artifact`) but cannot
show one to anyone. The only egress today is **download** — mint a token, save
the file, email the file. That loses the live render, loses versioning, and
loses revocation.

This spec adds a share record on an **artifact version**, mirroring the
conversation-share model already in production: an owner-created, revocable,
access-controlled pointer that any authenticated recipient can open at
`/shared-artifact/{shareId}`, viewing the exact bytes in the same sandboxed
iframe the owner sees.

The feasibility conclusion is that this is mostly **plumbing an ACL check in
front of an existing minting call**. Every hard part — the isolated render
origin, the strict CSP, the signed short-lived token, immutable versions, the
sandboxed viewer component, the download path — already exists and is already
deployed.

## Why it is feasible

### 1. The render token already separates *authorization* from *identity*

`RenderTokenService.mint` (`app_api/artifacts/service.py`) does exactly two
things: assert the `(user, artifact, version)` row exists, then sign an HS256
JWT. The render Lambda then uses the token's `sub` claim purely as the
**DynamoDB partition key** to locate the artifact:

```
PK = USER#{sub}
SK = ARTIFACT#{aid}#V#{ver:05d}
```

The Lambda performs **no ownership comparison of its own** — it never sees the
viewer. The token *is* the capability. So a share flow only has to answer "may
this viewer be handed a token for the owner's row?" in app-api, and mint with
`sub` = the owner's id. **The render Lambda needs no change at all** for the
core feature.

That is the single most important finding: the expensive half of a sharing
feature (a viewer-safe, authenticated, sandboxed rendering path for
attacker-authored HTML) is already built and is identity-agnostic.

### 2. Versions are immutable, so snapshot semantics come for free

`update_artifact_record` appends `V#{n+1}` and re-points `#HEAD`; there is no
`DeleteObject` grant in inference-api. A share pinned to `(artifactId, version)`
can therefore **never change under the recipient** — no snapshot copy, no S3
duplication, no schema-versioned body like `shares/snapshot_store.py` needed for
conversations. The conversation-share feature had to offload a JSON body to S3
to get point-in-time semantics; artifact sharing gets them from the storage model.

### 3. The CSP already permits exactly the topology we want

`ArtifactsDistributionConstruct` sets `frame-ancestors https://{domainName}`
(plus `config.artifacts.extraFrameAncestors`) and `connect-src 'none'`. A
recipient viewing a shared artifact **inside the SPA** is the SPA origin
framing the artifact origin — already allowed. Artifact JS still cannot reach
app-api, cannot phone home, cannot exfiltrate. Nothing in the CSP, the
distribution, or the response-headers policy changes.

### 4. app-api already holds every permission required

`app-api-iam-grants.ts` grants the task role `GetItem/PutItem/UpdateItem/
DeleteItem/Query` on the artifacts table (and `index/*`), `GetObject/PutObject/
PutObjectTagging/ListBucket` on the content bucket, and `GetSecretValue` on the
render-token secret. Share rows can live on the **existing artifacts table**
under a new key prefix. **Zero new CDK resources, zero new IAM statements.**

### 5. The whole UX pattern is already in the codebase

| Need | Existing thing to model on |
|---|---|
| Share dialog (public / specific-emails, copy link) | `session/components/share-modal/` |
| Share HTTP client | `session/services/share/share.service.ts` |
| Recipient page behind `authGuard` | `shared/shared-view.page.ts`, route `shared/:shareId` |
| Manage/revoke existing shares | `manage-sessions/manage-shares-dialog/` |
| Sandboxed artifact viewer | `artifact-panel.component.ts` |
| Download of a shared version | `artifact-download.service.ts` + `?download=1` |

### Feasibility risk register

| Risk | Severity | Handling |
|---|---|---|
| Minting a token whose `sub` is another user makes the render log attribute the view to the owner | medium | Add `vwr` (viewer id) + `shr` (share id) claims at mint time. `_verify_token` validates `alg`/`iss`/`aud`/`exp`/`iat`/`sub`/`aid`/`ver` and then returns the claim dict — it has **no extras rejection**, verified by reading the handler — so extra claims are forward-compatible with the currently-deployed Lambda and a later Lambda deploy starts logging them. No deploy sequencing required. |
| Recipient could re-share by copying the iframe URL | low | Already true of the owner's own render URL, and tokens live ~120s. The share record, not the token, is the revocable control. |
| Artifacts survive session deletion (no cascade today; the `lifecycle-class=deleted` S3 rule has no writer) | medium | Cascade artifact-share revocation on session delete, mirroring `delete_shares_for_session`. See §7. |
| Shared artifacts inside a **shared conversation** silently vanish | medium | Pre-existing gap, documented in §8 as explicitly out of scope for PR-1. |
| A recipient views an artifact whose owner has since revoked | low | Every open re-checks the share row before minting; tokens are ~120s, so the revocation window is bounded by token TTL, not by session length. |

## Non-goals

- **Anonymous/unauthenticated public links.** Conversation sharing's `"public"`
  already means "any authenticated tenant user" — the `/shared/:shareId` route
  sits behind `authGuard` and `get_shared_conversation` depends on
  `get_current_user_from_session`. Artifact sharing matches that exactly.
  Governance here is Entra JWT identity, not content inspection.
- **Collaborative editing.** Shares are read-only. A recipient who wants to
  iterate forks (§6, deferred).
- **Sharing `#HEAD` (a moving pointer).** A share pins one version. Sharing a
  pointer that moves under the recipient is a different feature with different
  consent semantics, and a moving pointer is the exact trap that bit agent
  version snapshots.
- **Changing the render Lambda's verification contract.**

## Decisions

| Question | Decision |
|---|---|
| Share target | One **immutable `(artifactId, version)`** pair, never `#HEAD` |
| Access levels | `public` (any authenticated user) \| `specific` (email allowlist) — same literals as conversation shares |
| Storage | New key prefix on the **existing** `user-artifacts` table |
| Snapshot | **None** — version immutability is the snapshot |
| Render path | app-api ACL check → mint token with `sub`=owner, `vwr`=viewer, `shr`=share |
| Render Lambda change | **None required** for PR-1 |
| Recipient surface | SPA route `/shared-artifact/:shareId` behind `authGuard` |
| Revocation | Delete the share row; effective within one token TTL (~120s) |
| Feature enablement | Same signal as artifacts: presence of `ARTIFACTS_RENDER_TOKEN_SECRET_ARN` |
| Audit | Structured logs only (conversation sharing sets this precedent; `AuditAction` is a closed set scoped to admin mutations) |

### Why a new key prefix instead of the shared-conversations table

The share row must be read on **every** render-token mint for a shared
artifact, and the mint path already touches the artifacts table to validate the
version. Co-locating keeps that to one table and no second client. The
conversation-share table is keyed `share_id` with a `SessionShareIndex`; it has
no artifact concept and adding one would leave two unrelated record shapes
under one key.

## 1. Data model

Added to the existing `{prefix}-user-artifacts` table. No new GSI **for PR-1**
(see the lookup note below).

**Share record** — the owner-scoped row, so an owner can list their own shares
with one `Query` on their existing partition:

```
PK  = USER#{owner_id}
SK  = SHARE#{artifact_id}#V#{version:05d}#{share_id}
attrs:
  share_id, artifact_id, version, owner_id, owner_email,
  access_level: "public" | "specific",
  allowed_emails: [str]        # present only when access_level == "specific"
  title                        # denormalized for the recipient header
  content_type                 # denormalized so the viewer can pick its chrome
  session_id                   # provenance / cascade-revoke
  created_at, updated_at
```

**Share lookup record** — the recipient path resolves `share_id` alone, with no
idea who the owner is:

```
PK  = SHARE#{share_id}
SK  = META
attrs: owner_id, artifact_id, version, access_level, allowed_emails,
       title, content_type, created_at
```

Two rows, written in one `transact_write_items`, is deliberately chosen over a
GSI. Per the GSI deploy-ordering trap, adding an index means a `platform.yml`
deploy that must land before the backend code that queries it, one
`UpdateTable` at a time, with CFN green ≠ index `ACTIVE`. Two items in a
transaction needs no infra deploy at all and makes the recipient read a single
`GetItem`. The cost is denormalized duplication on update — bounded, because
the only mutable fields are `access_level` and `allowed_emails`.

No IAM change is needed for the transaction: DynamoDB authorizes
`TransactWriteItems` against the **underlying** item actions, and the artifacts
grant already carries `PutItem`/`UpdateItem`/`DeleteItem`. `TransactWriteItems`
appears in no grant in `app-api-iam-grants.ts`, yet
`apis/shared/rbac/repository.py` calls `transact_write_items` against the
app-roles table in production — the pattern is already proven here. (Keep the
transaction to plain writes; a `ConditionCheck` item *would* need
`dynamodb:ConditionCheckItem` added.)

## 2. API

New router, `backend/src/apis/app_api/artifacts/shares.py`, mounted under the
same `ARTIFACTS_RENDER_TOKEN_SECRET_ARN` guard that already gates
`artifacts_router` in `main.py`.

All owner endpoints use `get_current_user_from_session` (SPA-facing — Bearer-only
would cause 401 redirect loops).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/artifacts/{artifact_id}/shares` | Create a share for `{version, accessLevel, allowedEmails?}`. 201. |
| `GET` | `/artifacts/{artifact_id}/shares` | List the caller's shares for this artifact. |
| `PATCH` | `/artifacts/shares/{share_id}` | Change `accessLevel` / `allowedEmails`. Owner only. |
| `DELETE` | `/artifacts/shares/{share_id}` | Revoke. 204. Owner only. |
| `GET` | `/shared-artifacts/{share_id}` | Recipient metadata: `{shareId, title, contentType, version, createdAt, ownerEmail, canDownload}`. Access-controlled. **Never returns content.** |
| `POST` | `/shared-artifacts/{share_id}/render-token` | Access-checked mint. Returns the same `{url, expires_at}` shape as the owner endpoint. |

Error mapping follows the existing artifacts routes: 404 unknown share/version,
403 access denied, 413 too large (content view), 500 `RenderTokenConfigError`,
503 `ArtifactQueryError`.

### The mint, in full

```python
def mint_for_share(self, *, share_id: str, viewer: User) -> tuple[str, int]:
    origin = _origin()                       # fail closed before any DDB call
    share = _get_share_lookup(share_id)      # PK=SHARE#{id}, SK=META
    if not share:
        raise ArtifactNotFoundError(...)
    _check_share_access(share, viewer)       # owner | public | email allowlist
    _assert_version_exists(                  # unchanged helper
        share["owner_id"], share["artifact_id"], int(share["version"])
    )
    now = int(time.time())
    claims = {
        "iss": _ISS, "aud": _AUD,
        "sub": share["owner_id"],            # DDB partition — NOT the viewer
        "aid": share["artifact_id"],
        "ver": int(share["version"]),
        "sid": "",
        "vwr": viewer.user_id,               # who actually looked (audit)
        "shr": share_id,                     # under which grant
        "iat": now, "exp": now + _TTL_SECONDS,
    }
    ...
```

`_check_share_access` is a direct port of `ShareService._check_access`: owner
always passes, `public` passes, `specific` compares `viewer.email.lower()`
against the lowercased allowlist, else `AccessDeniedError`.

**`sub` is the owner and that is load-bearing** — it is the DynamoDB partition
key the render Lambda builds, not an identity assertion. `vwr`/`shr` carry the
real viewer. This must be commented at the mint site, or a future reader will
"fix" it into a privilege bug.

## 3. Frontend

**Owner side.** A `Share` button beside `Download` on both the artifact card
(`artifact-card.component.ts`, action row near line 102) and the panel header
(`artifact-panel.component.ts`). Icon-only variants need `[appTooltip]` per the
frontend accessibility rule; the card's existing pattern keeps a visible label,
so match it. Opens `artifact-share-modal.component.ts`, adapted from
`share-modal.component.ts` (same access-level radio, same email chips, same
clipboard copy, same `@angular/cdk/dialog` shell).

**Recipient side.** Route `shared-artifact/:shareId` behind `authGuard`. New
`shared-artifact-view.page.ts`, modelled on `shared-view.page.ts`: the same
sticky "Shared read-only snapshot" banner, the same 403/404/500 branches, and
the artifact rendered full-width in the same sandboxed iframe
(`sandbox="allow-scripts"`, **no** `allow-same-origin`) the panel uses. Reuse
the panel's render/code toggle by extracting its iframe + `ArtifactSourceComponent`
body into a presentational child both pages import — the panel keeps its
docking, resize, and version-menu chrome.

**Services.** `ArtifactShareService` alongside `artifact-http.service.ts`
(owner CRUD), and a `mintSharedRenderToken(shareId)` path. `ArtifactDownloadService`
takes an optional share id so the hidden-iframe `?download=1` trick works
unchanged for recipients — the recipient mint is a different endpoint, same
returned URL shape.

**Content/code view for recipients.** `GET /artifacts/{id}/content` builds its
key from the authenticated user and must stay that way. Recipients get a
parallel `GET /shared-artifacts/{share_id}/content` that resolves the owner from
the share row after the same ACL check, reusing `ArtifactContentService` with an
explicit `owner_id` argument.

## 4. Delivery plan

| PR | Scope |
|---|---|
| **PR-1** | Data model + owner CRUD API + share-scoped mint. Backend only, fully tested. |
| **PR-2** | Owner UI: Share button on card + panel, share modal, manage/revoke list. |
| **PR-3** | Recipient UI: route, page, extracted viewer child component, shared content + download. |
| **PR-4** | Cascade revoke on session delete (§7) + docs-site page update. |

PR-1 through PR-3 are independently mergeable behind the artifacts enablement
signal; the Share button in PR-2 is the first user-visible change.

## 5. Testing

Backend (`backend/tests/apis/app_api/artifacts/`):

- `test_artifact_shares.py` — create writes both rows transactionally; owner
  list is partition-scoped; PATCH/DELETE reject a non-owner with 403; revoked
  share 404s.
- `test_shared_render_token.py` — **the security core.** Assert the minted
  claims: `sub` == owner, `vwr` == viewer, `shr` == share id. Assert `public`
  admits an arbitrary authenticated user; `specific` admits only allowlisted
  emails (case-insensitively) and 403s everyone else; a revoked share mints
  nothing; a share whose underlying version row is gone 404s rather than
  minting a token that renders the Lambda's error page.
- A test that the currently-deployed verifier accepts a token carrying the new
  `vwr`/`shr` claims — import `_verify_token` from the Lambda handler directly
  (it takes no AWS calls before the signature check) and assert it returns the
  claims rather than raising. This is the assertion that makes "no Lambda
  change required" a fact rather than a reading.

Frontend (`ng test` — never bare `npx vitest run`): share-modal interaction,
recipient page 403/404 branches, and the extracted viewer child rendering from
a stubbed token. Use DI token overrides, not `vi.mock`.

## 6. Deferred

- **Fork-to-own-artifact**, mirroring `POST /shares/{id}/export`: copy the
  version's S3 object into the recipient's own `{user_id}/{new_aid}/v1/` prefix
  and write fresh v1 rows, so a recipient can iterate on someone else's
  artifact in their own chat. This is the artifact analogue of "Continue this
  conversation" and is the most likely next ask.
- **Artifacts inside shared conversations** (§8).
- Share expiry (`ttl` on the share rows — the table already has a TTL attribute
  configured, so this is a field, not an infra change).
- View counts / "who opened this".

## 7. Session-delete cascade

`sessions/routes.py` already schedules `share_service.delete_shares_for_session`
as a background task on conversation delete. Artifacts are **not** cleaned up
there today, and the S3 `lifecycle-class=deleted` rule has no backend writer at
all — so artifacts (and their shares) would outlive the conversation that
produced them.

PR-4 adds `delete_artifact_shares_for_session(session_id)`, called from the same
background task. It queries `SessionIndex` for the session's artifact ids, then
deletes matching `SHARE#` rows and their lookup rows. Best-effort and
never-raising, exactly like the conversation version: the failure mode is an
orphan row, never a blocked delete.

Deliberately **not** in scope: deleting artifact content on session delete.
That is a retention decision about the artifacts feature as a whole, not about
sharing, and it deserves its own spec.

## 8. Artifacts in shared conversations — CLOSED

**Status: implemented.** Sharing a conversation now shares the artifacts it
produced. The rest of this section is the original gap analysis, kept because
the reasoning still explains the shape of the fix.

The mechanism differs from the sketch below in one important way: **no artifact
share records are created.** The conversation share record *is* the grant.

- `create_share` pins the session's artifacts, at the version each stood at, into
  the snapshot body alongside the messages. That preserves the point-in-time
  promise (a recipient reading a frozen conversation sees the artifact the
  transcript describes) and makes the snapshot the **allowlist**.
- `resolve_shared_artifact` is the whole access boundary: it checks the
  conversation share's ACL *and* that the artifact is one the snapshot pinned,
  then hands an owner id and pinned version to
  `RenderTokenService.mint_for_conversation_share`, which checks nothing itself.
- Access, updates and revocation are therefore free. Narrow the conversation's
  allowlist and the artifacts follow in the same write; revoke it and they go
  with it.

Auto-provisioning parallel artifact shares (the original sketch) was rejected on
two grounds. Each would need cascading on update, revoke, artifact delete and
session delete — and a missed cascade leaves an artifact readable after its
conversation was locked down, which is a security bug rather than a display one.
It would also put N rows in the recipient's "Shared with you" inbox for one
conversation share, when the conversation is the thing that was shared.

The snapshot's artifact list is optional on read. Conversation sharing is in
production, so bodies written before this exist and have no `artifacts` key;
they read as an empty list, and there is no migration.

### Original gap analysis



`shared-view.page.ts` renders `MessageListComponent` with `embeddedMode` and has
no artifact wiring. Artifact hydration goes through
`GET /artifacts?session_id=…`, which filters HEAD rows by
`item.user_id == requester`. A recipient viewing a shared conversation that
produced artifacts therefore sees **nothing** where the owner sees artifact
cards — silently, with no placeholder.

Closing it properly means auto-provisioning artifact shares for every artifact
in a conversation at conversation-share time (a consent decision: sharing a
conversation would then also share its artifacts), plus surfacing them in the
shared view. That is a separate spec, and it depends on this one's share record
existing first. Recording it here so it is a known gap rather than a discovered
bug.

## Effort estimate

| Area | Assessment |
|---|---|
| Infrastructure | **None.** No new table, bucket, distribution, secret, cert, DNS, IAM, or CSP change. |
| Render Lambda | **None** for PR-1. Optional later change to log `vwr`/`shr`. |
| Backend | Moderate — one service + one router, both close ports of existing code. |
| Frontend | Moderate — one new modal, one new page, one extracted viewer child. |
| Highest-risk surface | The share-scoped mint. It hands a viewer a credential for another user's DynamoDB partition; the ACL check is the only thing standing between "sharing" and "read any artifact by id". Test it as the security boundary it is. |
