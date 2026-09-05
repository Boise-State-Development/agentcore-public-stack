# Feature announcements

**Status:** PROPOSED — no code. Written 2026-09-04 from the "how do we tell
users about new features, and let them acknowledge it once" conversation.

**Refs:** `apis/shared/user_menu_links/` (the closest existing precedent — an
admin-authored, markdown-bodied, modal-rendered surface), `apis/shared/rbac/
admin_scopes.py` (the delegation registry this adds a scope to),
`components/quota-warning-banner/` (the banner surface pattern),
`docs/specs/granular-admin-permissions.md`, `docs/specs/mid-turn-steering.md`
(#934, the `isLoading()` caveat in §D8)

---

## 1. Problem

There is no way for an admin to tell users that something changed.

The platform ships features continuously — Skills, Agents, the Marketplace,
Memory Spaces, MCP Apps, mid-turn steering — and every one of them landed
silently. Discovery is left to the user noticing a new nav item. The only
admin-authored user-facing text in the product today is a **user-menu link**
(`apis/shared/user_menu_links/`), which is a static, always-present entry:
it has no notion of "new", no notion of "this user has already seen it", and
no way to draw attention to itself.

So the two halves of the ask are:

1. **Reach** — put an admin-authored message in front of users, with a
   choice of how loudly.
2. **Acknowledgement** — record, per user and durably, that they have seen
   it, so it stops being shown. Without this half the feature is worse than
   nothing: a banner that reappears on every page load trains users to
   ignore banners.

The second half is the part that is easy to get wrong, and where the
interesting decisions are.

---

## 2. What already exists

Everything below is a real precedent in the codebase, not an analogy. The
design leans on all four rather than inventing parallel machinery.

| Precedent | What it gives us |
|---|---|
| `apis/shared/user_menu_links/{models,repository,service}.py` | Admin-authored content, fixed-partition single-table storage (`PK: USER_MENU_LINKS`, `SK: LINK#<uuid>`), markdown body, `enabled`/`order` fields, admin CRUD at `/admin/user-menu-links` + a public read at `/user-menu-links`. The announcement repository is this file with more fields. |
| `apis/shared/user_settings/repository.py` | Per-user server-side state at `PK: USER#<user_id>`, `SK: SETTINGS`. Establishes the per-user key shape acknowledgements reuse. |
| `components/quota-warning-banner/quota-warning-banner.component.ts` | The banner surface: compact, `role="status"`, `aria-live="polite"`, dismiss button, light/dark. Its dismissal is *client-side*, which is correct for a recurring live signal and wrong for a one-shot announcement — see §D3. |
| `components/topnav/components/user-menu-link-modal/` | Markdown-in-a-dialog, via `ngx-markdown`'s `<markdown [data]>`, `@angular/cdk/dialog`, `appDialogDismiss`, focus/escape handling. The announcement modal is this component with an ack button. |
| `apis/shared/rbac/admin_scopes.py` + `admin/admin-scope.model.ts` | The closed, delegable admin-scope registry. `admin.user_menu_links` already exists in the `Customization` group; `admin.announcements` slots in beside it. |
| `apis/shared/users/repository.py` (`UserProfile.created_at`) | The signup timestamp that makes new-user backfill suppression (§D6) possible. |

---

## 3. Decision summary

| # | Decision |
|---|---|
| D1 | Three surfaces — **panel** (durable), **banner** (ambient), **modal** (interruptive) — selected per announcement. The panel is always implied. |
| D2 | Three acknowledgement actions — `seen`, `dismissed`, `acknowledged` — monotonic, never downgraded. |
| D3 | Ack state is **server-side per user**, not `localStorage`. |
| D4 | Acks are keyed by `<announcementId>#R<revision>`; editing an announcement does not re-show it unless the admin bumps `revision`. |
| D5 | The server computes visibility; the client renders what it is handed. |
| D6 | Users who joined after `publishAt` do not see the announcement, unless the admin opts in with `showToNewUsers`. |
| D7 | At most **one** modal and **one** banner per response, no matter how many are eligible. |
| D8 | The modal never opens over an active or paused turn. |
| D9 | `targetRoles` is a **display filter, not an RBAC grant** — it must never be written into a role's `granted*` lists. |
| D10 | Markdown is sanitized, and the admin scope is delegable — those two facts are connected. |
| D11 | Gated by `ANNOUNCEMENTS_ENABLED`, default ON with a kill switch. |
| D12 | Nothing about this feature touches the model call path. |

---

## 4. Design

### D1 — Three surfaces, selected per announcement

An announcement carries `surfaces: list[Literal["panel", "banner", "modal"]]`.

- **`panel`** — a "What's New" entry in the user dropdown, next to the
  existing admin-managed links, with an unread dot on the avatar. Pull-based,
  never interrupts, browsable forever. **Implied on every announcement**: the
  service adds `"panel"` if the admin omits it, so dismissing a loud surface
  can never destroy the information.
- **`banner`** — a strip below the top nav, rendered by a sibling of
  `quota-warning-banner`. One line plus an optional CTA and a ✕.
- **`modal`** — a dialog on next load. This is the only surface that can
  demand a real acknowledgement (`requiresAck`).

The pattern that matters: **one durable surface plus at most one
attention-grabber.** The admin picks how loud; the record persists either way.

Two surfaces are deliberately excluded.

**Toasts.** `components/toast` exists, but a toast is ephemeral by
construction — if the user is not looking at that corner of the screen for
four seconds, the announcement never happened. There is no honest way to
record `seen` for it.

**Injected chat messages.** Tempting — the user is already looking at the
thread — and wrong on this platform specifically. A message in the session
lands inside the cacheable prefix; CLAUDE.md's prompt-cache contract is
explicit that restored history must be byte-stable between turns, and an
announcement injected into history re-writes a 30k–150k-token prefix at the
cache-write premium for every user who receives it. It also becomes context
the model reads and may respond to. The reach is not worth a fleet-wide cache
bust. **Announcements never touch the model call path** (D12).

### D2 — Three acknowledgement actions, monotonic

| Action | Written when | Effect |
|---|---|---|
| `seen` | The announcement renders on any surface | Clears the unread dot. Does not suppress anything. |
| `dismissed` | User clicks ✕ or "Got it" | Suppresses banner and modal. Entry stays in the panel. |
| `acknowledged` | User clicks the confirm button on a `requiresAck` modal | As `dismissed`, plus it is a durable record an admin can report on. |

They are ranked (`seen=1 < dismissed=2 < acknowledged=3`) and the stored rank
**only ever increases**. The write is a conditional `UpdateExpression`:

```
SET actionRank = :rank, #action = :action, actionAt = :now, ...
ConditionExpression: attribute_not_exists(actionRank) OR actionRank < :rank
```

A `ConditionalCheckFailedException` here is success, not an error — swallow it.

This matters more than it looks. `seen` is written automatically on render, so
it races the user's click on ✕; without the guard, a late-arriving `seen`
write would clobber `dismissed` and the banner would come back. CLAUDE.md
already records two production bugs (#741, #751) whose shape was
per-user/per-session state moving backwards. Same class, so use the same
discipline: **make the write monotonic at the database, not in application
ordering.**

### D3 — Server-side ack state, not `localStorage`

`quota_warning` dismissal is client-side today and that is right for what it
is: a recurring signal that recomputes every turn, where "dismiss" means "not
now" and re-showing tomorrow is the intended behaviour.

An announcement is the opposite — a one-shot statement where "dismiss" means
"never again". Client-side state gives:

- the banner back on every other device,
- the banner back in a private window,
- the banner back after a cache clear,
- no way to answer "did people read the policy change?",
- and for `requiresAck`, an acknowledgement record that any user can erase
  from devtools, which is not a record at all.

So acks are DynamoDB items under the user's partition. The cost is two small
queries on SPA bootstrap (§5), which is cheap and bounded.

`localStorage` still has one legitimate job: the **fail-open** case in §D7 —
if the ack POST fails, hide the item locally for the tab session so the user
is not stuck under an undismissable banner. It is a fallback, never the
source of truth.

### D4 — Revision-keyed acks

The ack sort key is `ACK#<announcementId>#R<revision>`, not
`ACK#<announcementId>`.

Without the revision, an admin fixing a typo in a published announcement has
two equally bad options: the edit is invisible to everyone who already
dismissed it, or every edit un-dismisses it for the entire user base and a
typo fix re-fires a modal at ten thousand people.

With it, `revision` is an explicit admin lever. `PATCH` to body/title leaves
`revision` alone by default; a **"Show this again"** action in the admin UI
increments it, and everyone's suppression lapses at once. Because the ack SKs
share the `ACK#<id>#` prefix, a `begins_with` query still tells us the user
saw revision 1 — which lets the panel mark the entry **Updated** rather than
plain unread.

### D5 — The server computes visibility

`GET /announcements` returns only what this user should actually see, already
filtered and capped. The client renders it; it does not evaluate targeting,
dates, or ack state.

The alternative — ship all announcements plus the ack list and filter in the
SPA — means the visibility rules exist in two languages, drift, and the
`showToNewUsers` and one-modal caps get re-implemented (differently) in
TypeScript. It also leaks announcements to users who were never targeted.

Filter chain, in order:

1. `state == "published"` (draft / scheduled / archived are excluded)
2. `publishAt <= now` and (`expiresAt` is null or `expiresAt > now`)
3. `targetRoles` intersects `user.roles`, or contains `"*"`
4. `showToNewUsers` is true, or `publishAt > user_profile.created_at`
5. no ack item at the current revision with `actionRank >= 2`
6. cap: all panel items, at most one banner, at most one modal (§D7)

### D6 — New-user backfill suppression

Rule: a user whose `created_at` is **after** an announcement's `publishAt`
does not see it, unless the announcement sets `showToNewUsers: true`.

This is the single most common failure mode of announcement systems. Without
it, a user signing up eighteen months from now logs in for the first time and
is met with a queue of modals about features that have always existed from
their point of view. It is also self-inflicted in a way users read as
brokenness rather than as history.

`showToNewUsers: true` exists for the real exception — a standing policy
notice ("AI output must be reviewed before use") that genuinely does apply to
everyone who ever joins. Those should be rare, and the admin form should say
so next to the checkbox.

Fallback: if the user profile has no usable `created_at` (the repository
already heals malformed ISO strings on read), treat the user as **existing**
and show the announcement. Failing toward showing a message is the recoverable
direction; failing toward silence is not.

### D7 — One modal, one banner, per response

Even with §D6, an admin can publish three things in a week and a returning
user is eligible for all of them. Three stacked banners is a broken page;
three sequential modals is an ordeal.

So the response caps the loud surfaces:

- **banner**: at most one — highest `severity`, then oldest `publishAt`.
- **modal**: at most one — same ordering, with `requiresAck` items sorted
  first so a blocking notice is never queued behind an informational one.

Oldest-first drains the queue in the order things happened, and the rest stay
eligible for the next page load. The panel is uncapped — it is a list, and a
list of five is fine.

**Fail-open dismissal.** If `POST /announcements/{id}/ack` fails, the client
hides the item for the tab session anyway and retries opportunistically. A
user trapped under a banner they cannot dismiss because of a transient 500 is
a worse outcome than an announcement that reappears tomorrow.

### D8 — Never interrupt a turn

The modal opens on route settle, and only when all of these hold:

- no active stream — `messageMapService.isLoadingSession() === null`
- no pending tool-approval or OAuth-consent prompt
- the composer is not focused with a non-empty draft

The second condition is not belt-and-braces. Per `docs/specs/
mid-turn-steering.md` (#934), **`isLoading()` is `false` while a turn is
paused on an interrupt** — so a stream-only check would happily throw a modal
over an OAuth consent dialog and steal its focus. Check the
`tool-approval` and `oauth-consent` services directly.

If the gate fails, do not queue the modal for later in the session — leave it
eligible and let it open on the next clean load. Deferred modals that fire
minutes later, mid-thought, are the worst version of this feature.

### D9 — `targetRoles` is a filter, not a grant

`targetRoles: list[str]` is matched against `User.roles`, with `"*"` meaning
everyone. It is stored on the announcement.

CLAUDE.md's RBAC rule says a `granted*` list on the **AppRole record** is the
only thing access checks read, and that `allowedAppRoles` on a resource is a
derived, display-only projection. That rule exists because a role list
persisted on a *tool*, *model*, or *skill* silently grants nothing.

**Announcements are outside that rule, and must stay outside it.** Visibility
of a notice is not access control: there is no capability being granted, no
`can_access_*` predicate, and nothing to inherit. Writing `targetRoles`
through to `AppRole.granted*` would put display metadata into the
access-decision path — strictly worse than the bug the rule prevents.

Concretely: the announcement admin form's role picker writes **only** to the
announcement item, and `apis/shared/rbac/` is not modified by this feature.
Worth a comment on the field so a future reader does not "fix" it into the
role service.

### D10 — Sanitized markdown, delegable scope

Body content is markdown, rendered with `ngx-markdown` exactly as
`user-menu-link-modal.component.ts` does, so heading/list/link styling matches
assistant messages.

But note what changes. `admin.user_menu_links` is held by a small number of
people; announcements are the kind of thing you *want* to delegate to
comms/enablement staff, and a broadcast surface authored by a wider group is a
stored-XSS target aimed at every user of the platform.

Therefore:

- `provideMarkdown` must run with sanitization on for this content — do
  **not** enable `[disableSanitizer]` on the announcement renderer.
- The server validates `ctaUrl` with the same `http(s)`-only check
  `user_menu_links` already applies (`_validate_http_url`), for the same
  documented reason: Angular's `DomSanitizer` strips `javascript:` from
  `[href]`, but anyone hitting the API with curl bypasses the SPA form.
- Body length is capped (16 KB) at the model layer.
- Writes are audit-logged through the existing `admin.audit` trail.

### D11 — Feature flag

`ANNOUNCEMENTS_ENABLED` in `apis/shared/feature_flags.py`, **default ON with a
kill switch** per house style — unset or empty resolves to enabled, only the
literal `"false"` disables:

```python
def announcements_enabled() -> bool:
    return os.environ.get("ANNOUNCEMENTS_ENABLED", "").strip().lower() != "false"
```

CDK threads `config.announcements.enabled` with the same empty-string-safe
ternary the other flags use, so an unset GitHub Actions variable cannot
silently disable the feature. While off, both routers 404 and the SPA renders
no surfaces.

### D12 — No model-path impact

Stated as a decision so it survives review: this feature adds nothing to the
system prompt, `toolConfig`, or conversation history; makes no model calls;
and is not read on the inference path. The read is two DynamoDB queries at SPA
bootstrap. Nothing here can move the cache-hit rate.

---

## 5. Data model

One new table, `<prefix>-announcements`, added to `AdminTablesConstruct`
(`infrastructure/lib/constructs/data/admin-tables-construct.ts`) beside
`UserMenuLinksTable`. Generic `PK`/`SK` strings, `PAY_PER_REQUEST`, PITR on,
AWS-managed encryption — identical to its siblings — plus **TTL on `ttl`**,
which is why acks live here rather than in the user-settings table.

### Announcement item

```
PK  ANNOUNCEMENTS
SK  ANNOUNCEMENT#<uuid>
```

| Field | Type | Notes |
|---|---|---|
| `announcementId` | str | uuid4 |
| `title` | str | ≤ 140 chars; the banner line and panel heading |
| `bodyMarkdown` | str | ≤ 16 KB; panel and modal only |
| `summary` | str? | one line for the banner when `title` is too long |
| `surfaces` | list | subset of `panel` / `banner` / `modal`; `panel` forced on |
| `severity` | enum | `info` \| `success` \| `warning`; drives banner colour + ordering |
| `state` | enum | `draft` \| `scheduled` \| `published` \| `archived` |
| `publishAt` | iso8601 | when it becomes visible |
| `expiresAt` | iso8601? | **required** if `surfaces` includes banner or modal |
| `targetRoles` | list[str] | `["*"]` default. Display filter (§D9) |
| `showToNewUsers` | bool | default `false` (§D6) |
| `requiresAck` | bool | modal only; disables backdrop dismiss |
| `ctaLabel` / `ctaUrl` | str? | http(s) only, validated server-side |
| `revision` | int | starts at 1; bumping re-shows (§D4) |
| `createdAt` / `updatedAt` / `createdBy` | | mirrors `UserMenuLink` |

Single fixed partition, queried by `PK = ANNOUNCEMENTS AND begins_with(SK,
"ANNOUNCEMENT#")`, exactly as `UserMenuLinksRepository.list_links` does. The
same "when per-org scoping is needed, the PK becomes `ANNOUNCEMENTS#<org_id>`"
note applies. Volume is tens of items; no GSI, and an in-process TTL cache
(60s) on the published list is a reasonable later optimization, not a
requirement.

### Acknowledgement item

```
PK  USER#<user_id>
SK  ACK#<announcement_id>#R<revision>
```

| Field | Type | Notes |
|---|---|---|
| `announcementId`, `revision` | | denormalized for reporting |
| `action` | enum | `seen` \| `dismissed` \| `acknowledged` |
| `actionRank` | int | 1/2/3, monotonic guard (§D2) |
| `actionAt` | iso8601 | |
| `surface` | str | which surface it was acted on — tells you whether the modal or the panel is doing the work |
| `ttl` | int | `expiresAt` + 90 days, or `publishAt` + 2 years for open-ended items |

Read: one `query` on `PK = USER#<id> AND begins_with(SK, "ACK#")`. Bounded by
the number of announcements a user has ever interacted with — tens.

TTL keeps that bounded forever without a sweeper. **Do not TTL an
`acknowledged` item for a `requiresAck` announcement** while the record has
compliance value; set `ttl` to null for those and let the archive path handle
them deliberately.

---

## 6. API surface

All routes are `app_api` — this is user-facing CRUD and has no business on
`inference-api` (CLAUDE.md's inference-api boundary: custom paths there are
unreachable through the AgentCore Runtime data plane).

### User-facing — `apis/app_api/announcements/routes.py`

`Depends(get_current_user_from_session)` on every route (cookie session, not
Bearer — CLAUDE.md's auth rule).

```
GET  /announcements
     → { panel: [...], banner: Announcement|null, modal: Announcement|null,
         unreadCount: int }
     Already filtered and capped per §D5/§D7.

POST /announcements/{id}/ack
     body: { action: "seen"|"dismissed"|"acknowledged", surface: str }
     → 204. Idempotent; monotonic (§D2). 404 if the id is not visible to
       this user — do not let an ack confirm the existence of an
       announcement targeted at another role.
```

### Admin — `apis/app_api/admin/announcements/routes.py`

Guarded by `require_admin_scope("admin.announcements")`, package-wide, per the
convention in `admin/routes.py` ("the permission boundary is the package
boundary", enforced by `tests/architecture/test_admin_scope_coverage.py`).

```
GET    /admin/announcements               list all states
POST   /admin/announcements               create (defaults to draft)
GET    /admin/announcements/{id}
PATCH  /admin/announcements/{id}          body edits; revision unchanged
POST   /admin/announcements/{id}/publish  draft|scheduled → published
POST   /admin/announcements/{id}/archive  → archived (stops showing, keeps acks)
POST   /admin/announcements/{id}/revise   revision += 1 — "show this again"
DELETE /admin/announcements/{id}
GET    /admin/announcements/{id}/stats    { seen, dismissed, acknowledged, targeted }
```

`/stats` needs a count of acks across users, which the key shape does not
support directly. Options, cheapest first: (a) atomic counters on the
announcement item incremented on first ack per rank — approximate, O(1),
adequate for "did anyone read this"; (b) a GSI on `announcementId`; (c)
a scan behind a cache. **Start with (a).** Percentages need a denominator too;
`targeted` is an estimate from the user table filtered by `targetRoles`, and
should be labelled as an estimate in the UI.

### New admin scope

Add to `ADMIN_SCOPES` in `apis/shared/rbac/admin_scopes.py`:

```python
AdminScope(
    id="admin.announcements",
    label="Announcements",
    group=GROUP_CUSTOMIZATION,
    description="Author and publish feature announcements shown to all users.",
    delegable=True,
)
```

and to `ADMIN_SCOPE_IDS` in `frontend/.../admin/admin-scope.model.ts` — the
literal union is duplicated deliberately and `admin-scope.model.spec.ts` is
the reminder to keep both ends in step.

Delegable, unlike `admin.roles` / `admin.auth_providers`: authoring a notice
does not confer admin power. It does confer a broadcast channel, which is
what §D10 is about.

---

## 7. Frontend

New service `services/announcements/announcements.service.ts` — signal-based
per house convention. Fetches once on bootstrap (after the session resolves,
so `roles` are known), exposes `panelItems()`, `bannerItem()`, `modalItem()`,
`unreadCount()`, and `ack(id, action, surface)` with the fail-open behaviour
from §D7.

| Component | Location | Notes |
|---|---|---|
| Whats-new panel | `components/topnav/components/whats-new-panel/` | Dialog listing panel items newest-first, relative dates, **New** / **Updated** pills, markdown body. Opens from the user dropdown; unread dot on the avatar and the menu row. Mirrors `user-menu-link-modal`. |
| Announcement banner | `components/announcement-banner/` | Sibling of `quota-warning-banner`. `role="status"`, `aria-live="polite"`, severity colours from the `state-*` token scale that banner already uses, ✕ + optional CTA. |
| Announcement modal | `components/announcement-modal/` | `user-menu-link-modal` plus a primary ack button. When `requiresAck`, `appDialogDismiss` and the escape handler are disabled so the only exit is the button. |
| Admin list | `admin/manage-announcements/manage-announcements.page.ts` | Mirrors `manage-user-menu-links`. State chips, surface icons, ack counts, "Show again" action. |
| Admin form | `admin/manage-announcements/announcement-form.page.ts` | Title, markdown body with live preview, surface checkboxes, severity, schedule, role picker, `showToNewUsers` (with the §D6 warning text), `requiresAck`, CTA. |

Admin nav entry under **Customization**, `data: { scope: 'admin.announcements' }`,
next to the existing user-menu-links entry.

Accessibility, non-negotiable: unread dot needs a text alternative (`aria-label`
carrying the count, not colour alone); banner is `aria-live="polite"` and never
`assertive`; modal keeps the focus trap and returns focus on close; the panel
list is keyboard-navigable.

---

## 8. Infrastructure

1. `AnnouncementsTable` in `AdminTablesConstruct` — same shape as
   `UserMenuLinksTable`, plus `timeToLiveAttribute: 'ttl'`.
2. SSM publication at `/${prefix}/admin/announcements-table-name`, matching
   the sibling parameters.
3. `announcementsTable: dynamodb.ITable` threaded through
   `PlatformComputeRefs` → `platform-stack.ts` → `app-api-environment.ts`
   as `DYNAMODB_ANNOUNCEMENTS_TABLE_NAME`, and a read/write grant in
   `app-api-iam-grants.ts` alongside the user-menu-links grant.
4. `ANNOUNCEMENTS_ENABLED` env var from `config.announcements.enabled`.
5. Add the table to the backup/restore list at `platform-stack.ts:1002`.

No new GSI, so none of the GSI deploy-ordering hazards apply.

---

## 9. PR breakdown

Sequenced so each PR is independently shippable and the risky surfaces come
last. PR-1 through PR-3 deliver a complete, useful, zero-interruption feature.

| PR | Scope | Notes |
|---|---|---|
| **PR-1** | Data model + repository + service + admin CRUD + admin scope + CDK table | No user-facing surface. Ships dark; admins can author drafts. |
| **PR-2** | `GET /announcements` + `POST .../ack` + `announcements.service.ts` + What's-new panel + unread dot | The durable surface. End-to-end value, nothing interrupts anyone. |
| **PR-3** | Admin list + form pages | Removes the "author by curl" step. Could merge into PR-1 if the form is small. |
| **PR-4** | Banner surface + severity ordering + one-banner cap | First interruptive-ish surface. |
| **PR-5** | Modal + `requiresAck` + the §D8 turn-safety gate | Highest-risk PR — everything that can annoy a user lives here. |
| **PR-6** | `/stats` + ack counters + admin reporting | Tells you whether any of this works. |

Coach marks / spotlight tooltips anchored to specific UI elements are
explicitly **not** in this sequence — see §12.

---

## 10. Testing

Backend:

- Visibility filter table-driven across state / dates / roles / new-user /
  ack — this is where the logic is, so this is where the tests are.
- **Monotonic ack**: `seen` after `dismissed` leaves `dismissed` intact. Write
  this one first; it is the §D2 regression.
- **Revision**: ack at R1, bump to R2, item becomes visible again; ack history
  at R1 still readable.
- **New-user suppression**: user `created_at` after `publishAt` sees nothing;
  `showToNewUsers` flips it; malformed `created_at` fails toward showing.
- **Caps**: five eligible announcements → 5 panel, 1 banner, 1 modal, with
  `requiresAck` first.
- Ack on an id not visible to the caller → 404, not 204.
- Admin scope coverage picks up the new package automatically
  (`test_admin_scope_coverage.py`); confirm it does rather than assuming.
- `ctaUrl` rejects `javascript:` at the API, not only in the form.
- Flag off → both routers 404.

Frontend (`ng test`, never bare `npx vitest run`):

- Service caps and fail-open dismissal (ack rejects → item still hides).
- Modal gate: does not open while `isLoadingSession()` is non-null, **and**
  does not open while a tool-approval or OAuth-consent prompt is pending
  (the #934 case).
- `requiresAck` modal ignores backdrop click and escape.
- Unread count clears on panel open.
- Use DI-token overrides rather than `vi.mock`, per house convention.

---

## 11. Risks and open questions

**Announcement fatigue is the real failure mode.** Every mechanism here is
sound and the feature still fails if admins publish a modal a week. Partial
mitigations: the one-modal cap, an `expiresAt` requirement on loud surfaces,
and `/stats` making low ack rates visible. The rest is a norm, not a control —
worth writing into the admin page's help text: *panel by default, banner when
it matters, modal when it is a policy change.*

**`/stats` denominators are estimates.** "1,200 of ~4,000 targeted users
acknowledged" — the denominator moves as people join and roles change. Label
it as approximate; do not build compliance reporting on it without a real
targeted-user snapshot at publish time.

**Open — should announcements be dismissible in bulk?** "Mark all as read" in
the panel is trivial to add and slightly undermines the ack signal. Suggest
shipping without it and adding it if users ask.

**Open — per-org scoping.** The fixed `ANNOUNCEMENTS` partition assumes
single-tenant, as `user_menu_links` does. The PK shape leaves room; nothing
else in the design does. Fine to defer, worth not forgetting.

**Open — email/out-of-band delivery.** Deliberately excluded (§12), but
`state == "published"` is the natural hook if it is ever wanted.

---

## 12. Out of scope

- **Coach marks / spotlight tooltips** anchored to specific controls. The
  highest-value discovery mechanism and the highest-maintenance one: it needs
  an anchor registry in the SPA, and every anchor is a latent breakage the
  next time that component is refactored. Revisit once the basic system is
  proven.
- **Email or out-of-band notification.** Different delivery guarantees,
  different consent story, different infrastructure.
- **Per-user scheduling / drip campaigns.** This is an announcement system,
  not a marketing automation platform.
- **Localization.** No i18n infrastructure exists in the SPA today; adding it
  for this feature alone is the wrong entry point.
- **Rich media in bodies.** Markdown text and links only. Images mean an
  upload path, storage, and a CSP conversation.
