# Agent Marketplace — a browsable store for published Agents

**Status:** Draft / proposal (2026-07-24)
**Author:** Phil Merrell (drafted with Claude)
**Targets branch:** `develop`
**Supersedes:** [`agent-directory.md`](agent-directory.md) — carries forward D1, D2, D3, D6 and D8;
**replaces** its D4 (runnability placement) and D7 (self-service publication).
**Depends on:** [`agent-designer.md`](agent-designer.md) Phases 1–2 (the Agent record + the bindable
catalog) — shipped.
**Related:** Skills v2 invoke-through sharing ([`skills-as-agent-primitive.md`](skills-as-agent-primitive.md) §6/D7);
agentic-platform primitives F6b Registry ([`agentic-platform-primitives.md`](agentic-platform-primitives.md)).

## Summary

A **store**: a browse page of published Agents, a detail page per Agent, and the administrative
controls to curate it. Authors submit; admins approve, promote, categorize and take down. Users add
an Agent to their own set with one tap and reach it afterward by `@`-mentioning it in any
conversation. Admins can seed a role's starting set so a student's first session already has the
Canvas student agent in the sidebar.

Two things make this more than a list page: **an Agent gets an identity** (a square icon, a
publisher, a detail page worth reading) and **an Agent gets a handle** (`@Name` in the composer).
Neither exists today.

This spec adds no new primitive. It is a surface, a curation layer, and one new per-user state item
over the Agent record that already exists.

## What changed from `agent-directory.md`

| Decision | Directory spec | This spec | Why |
|---|---|---|---|
| Publication | Self-service, admin takedown as remedy (D7) | **Author submits → admin approves** (D2) | Product call. The store's credibility on day one matters more than publication throughput, and the population is small enough that review is not a bottleneck. |
| Card content | Bindings + `usageCount` + runnability dot | **Icon, name, one line** (D4) | A shelf row's job is to make you tap. Bindings and counts move to the detail page and admin reporting. |
| Runnability | Dot on every card, banner on detail | **One line on the detail page** (D6) | Follows from the card decision. Cost stated in D6. |
| Identity | `emoji` only | **Uploaded square icon + generated fallback** (D5) | An app store where a third of the tiles are a bare emoji reads as unfinished. |
| Reach | Browse page only | **`@`-mention + role-seeded pins** (D9, D11) | A nav page is visited once. The composer is visited constantly. |
| Curation | `featured` boolean | **Ordered store front, managed categories, review queue, default pins** (D10) | "Featured" implies an ordering someone owns. So does every other lever. |

Carried forward unchanged: **one noun, Agent** (D1); the **sparse directory index** (D2 of the old
spec, restated in Data model here); **`listing` separate from `visibility`** with a `listed:false`
backfill (D3); **pin is a pointer, never a fork** (D6 → D8 here); **ship behind a flag plus an RBAC
capability** (D8 → D12 here).

---

## D1 — One noun: Agent

Unchanged from `agent-directory.md` D1. Our `bindings[]` over one governed `modelConfig` already *is*
the bundle a vendor "plugin" packages, and it is already attached to the persona. The store is a view
over Agents, not a new entity. Revisit only if the same tool+skill+KB combination gets re-bound
across three or more Agents.

## D2 — Publication is admin-gated: submit, review, approve

**This replaces `agent-directory.md` D7,** which argued that a review queue makes publication stop.
That argument is real and the mitigation is an operational commitment, not a technical one: a
**stated review SLA (two business days)** and a pending-count badge on the admin nav so the queue is
visible rather than discovered.

The lifecycle is a state machine on the listing:

```
draft ──▶ private ──▶ in_review ──▶ published ──▶ taken_down
                          │              │             │
                          └─▶ changes_requested ◀──────┘
                                     └──▶ in_review (resubmit)
```

- **Submit** (author) captures a category, an optional note to the reviewer, and runs the D7
  disclosure checks. `memory_space` bindings block submission outright.
- **Approve** (admin) writes the sparse index key and the Agent appears in the store immediately.
- **Request changes** (admin) returns it with a reason that renders on the author's own card, so the
  author never has to ask what happened.
- **Take down** (admin) clears the index key and notifies the author with a reason. It is a
  **delisting, not a revocation** — existing pins keep working, conversations underway keep running,
  and the Agent stays reachable by direct link because `visibility` is a separate axis (D3).

**Who owns the queue:** `system_admin`, via the existing `require_admin` dependency. A more granular
admin permission set (a "marketplace curator" who can review without holding full system admin) is a
deliberate follow-up, not a v1 scope item — do not build a second permission axis here. The queue's
pending count badges the admin nav so the work stays visible to the people who already hold the role.

**Review gates the listing, not subsequent edits.** An approved author can revise instructions
freely afterward. Re-review on every edit would make iteration miserable; the accepted risk is that a
listing can drift from what was approved. Mitigation is an audit trail (`updatedAt` surfaced in the
admin listings table), not a gate. Revisit if it is ever abused.

## D3 — `listing` is separate from `visibility`

Unchanged from `agent-directory.md` D3, and it remains the one decision with a data-safety
consequence. `PUBLIC` today means *anyone with the link may use this*
([`service.py:265`](../../backend/src/apis/shared/assistants/service.py)), and the share dialog
surfaces that link. If store membership were derived from `visibility`, shipping this would
retroactively publish every existing PUBLIC agent to the whole institution with no author consent.

`visibility` stays the access gate. `listing.state` is the publication state. **Backfill every
existing record to no `listing` block at all** — publication is an explicit forward act.

## D4 — The shelf carries an icon, a name, and one line

Browse rows and store-front tiles show **icon, name, one-line tagline** and nothing else. No model
chip, no tool/skill counts, no chat counts, no runnability badge.

Those numbers are still collected and still surfaced — on the detail page's Details panel, and in the
admin Listings table. What they are not is shelf furniture. A store row that reports its own
permissions and dependency list is a spec sheet, and it scans like one.

`tagline` is a new short field (≤ 80 chars), distinct from `description`. Falling back to a truncated
`description` produces rows that end mid-clause; the store is the first surface where the difference
between a summary and a subtitle matters.

## D5 — Agents get a square icon, with a generated fallback

Authors upload a square icon (**512×512, PNG or JPG, ≤ 400 KB**, stored in the existing assistants
asset bucket with the object key on the record — **not** inline on the DynamoDB item, per the 400 KB
item-limit lesson from MCP App icons).

When no icon is uploaded, the SPA renders a **deterministic gradient derived from the agent id plus
the existing `emoji`**. This is not a placeholder to be replaced later — it is the designed default,
so a store of mostly-unstyled agents still looks composed. Icons render at 84px (detail), 52px
(store front / My Agents), 40px (browse row), and 28px (sidebar, `@` menu); the upload UI previews
all the small sizes because an icon that reads at 84px often turns to mud at 28px.

## D6 — Runnability moves to the detail page

`agent-designer.md` D5 sets block-on-missing: every gated capability re-resolves against the invoking
user at run time, and a missing one blocks. A store strains that rule, so the detail page resolves
the Agent's `modelConfig` + `bindings` against the viewer's own RBAC-filtered
`GET /agents/bindable` results and renders one line under "What it can access":

- **Ready to run for you**
- **Runs with limits for you** — names the unavailable optional binding
- **Not available to you** — names what is missing; the Start chat button is disabled

**The cost, stated plainly:** with no badge on the shelf, a user only learns an Agent will not run
after tapping into it. That is the accepted price of D4. It is mitigated by the fact that most store
entries are published by central teams against broadly-granted capabilities, and by D9's
assignment-time check, which keeps unrunnable Agents out of role-seeded pins.

`GET /agents/{id}/runnability` composes `bindable_catalog.list_bindable` per kind and diffs — no new
access service (Designer D4 stands: compose the five per-primitive checks, do not invent a sixth).

## D7 — Submission discloses skill exposure and blocks on memory spaces

Unchanged in substance from `agent-directory.md` D5, now enforced at **submission** rather than at
publish, so the author sees it before a reviewer's time is spent.

Skills v2 D7 makes skill sharing invoke-through: a `skill` binding resolves when
`skill.owner_id == agent.owner_id` and the invoker has access to the Agent
([`access.py`](../../backend/src/apis/shared/skills/access.py)). Publishing therefore effectively
publishes the contents of every skill the author wrote and bound. Requirements:

1. The submit dialog **enumerates** the skills by name: "N skills you wrote become readable by
   anyone who runs this agent."
2. `memory_space` bindings **block submission**, with a message naming the space. A memory space is
   personal data; D5's re-resolve already denies anyone who lacks it, so a published agent bound to
   one is a guaranteed failure for every viewer.
3. Unpublishing revokes nothing retroactively. The UI says so rather than implying a recall.

## D8 — Adding an Agent is a pin, never a fork

Unchanged from `agent-directory.md` D6. Agents are addressable by id and already run for anyone with
access, so there is nothing to install. "Add to my agents" stores a pointer.

Explicitly **not** copy-on-add: forking would duplicate the record (version drift the moment the
author updates), re-owner the bindings (which breaks invoke-through in the confusing direction — the
fork's owner does not own the skills, so they silently stop resolving), and multiply takedown
surface. Users who want a variant build one in the Designer.

## D9 — Roles seed a starting set of pins, resolved live

Admins assign default pinned Agents per `AppRole`, so a role's members start with a useful sidebar
instead of an empty one. Six decisions:

**1. Role pins are resolved live, not materialized.** A user's effective pin list is computed per
request as *(role pins for every role they hold) − (their dismissals) + (their own pins)*. There is
no fan-out job writing 12,480 user records when an admin adds a pin.

> This reverses the "apply to everyone / new members only" choice shown in the mockup. Live
> resolution makes that distinction unrepresentable: removing a role pin removes it for everyone in
> the role. That is a real capability loss — an admin cannot let people who liked an Agent keep it
> while stopping new seeds — and it buys away an entire asynchronous fan-out subsystem, its
> partial-failure modes, and its backfill. The escape hatch is that a user who independently pins a
> seeded Agent has converted it to their own pin (`source: 'user'`), which survives the role pin's
> removal. The removal dialog must therefore say plainly: *"unpins for everyone in this role who
> hasn't pinned it themselves."*

**2. Pins from multiple roles merge.** Someone who is both staff and faculty gets the union. No
precedence rules; ordering is by `(locked desc, role priority desc, order asc)`.

**3. A user's dismissal is remembered.** The single most important detail. Without a tombstone, the
seed re-applies on the next resolution and the user can never remove it. Dismissals are stored
per-user and the resolver must respect them.

**4. `locked` is a separate axis.** A locked pin cannot be dismissed; the remove control is hidden
and the dismissal endpoint no-ops. Reserved for the one Agent a role genuinely must keep. The admin
UI defaults to unlocked and labels the alternative honestly.

**5. Assignment-time runnability check.** Adding an Agent to a role diffs the Agent's bindings
against that **role's** `effective_permissions` — which is already denormalized on the AppRole record
by `_compute_effective_permissions`
([`admin_service.py:276`](../../backend/src/apis/shared/rbac/admin_service.py)) — and warns before
saving. Seeding 410 researchers an Agent that fails on their first message is the exact failure this
prevents.

**6. ⚠️ `default` is a substitute, not a baseline.** `resolve_user_permissions`
([`service.py:33`](../../backend/src/apis/shared/rbac/service.py)) consults the `default` role
**only when the user matched zero AppRoles**, and never merges it alongside a matched role. Default
pins assigned to `default` therefore reach only users who match nothing else — in prod, close to
nobody. The admin UI must label that chip *"fallback only — does not apply to users with any other
role"* or admins will assume it means "everyone" and quietly seed no one.

## D10 — Admin owns seven levers, in one console

Every store behavior needs a surface, or it becomes a code deploy:

| Surface | Controls |
|---|---|
| **Review queue** | Approve / request changes, with a reason. Pending count badges the nav. |
| **Reports** | User-submitted problem reports (D15): resolve or dismiss, with the reporter visible. Open count badges the nav alongside submissions. |
| **Listings** | All published Agents: promote to store front, reassign category, take down. |
| **Store front** | The Featured row as an explicitly ordered list, with slots. |
| **Categories** | Fixed set in browse order: add, rename, reorder. Empty categories auto-hide. |
| **Publishers** | Publisher profiles, the `verified` mark, and per-user eligibility (D12). |
| **Default pins** | Per role: add, order, lock, remove (D9). |

The store front deserves its own surface because **it is the only ranking lever that exists.**
Everything below Featured is newest-first — there is no popularity sort (see Data model), so
promotion is how a good Agent gets found.

Categories follow the **`UserMenuLink` precedent**
([`user_menu_links/repository.py:73`](../../backend/src/apis/shared/user_menu_links/repository.py)):
a fixed partition, per-item records with an explicit `order: int`, sorted on read by
`(order, label)`. They are admin-managed data, **not** a build-time constant like
`curated-models.ts` — a category set that requires a deploy to change will not be maintained.

## D11 — `@`-mention is scoped to your own and pinned Agents

Typing `@` in the composer offers the user's own Agents plus everything pinned (including role-seeded
pins), grouped, with the publisher as secondary text. Mentioning one hands that turn to the Agent's
model, tools and skills without leaving the thread.

Scope is deliberately narrow. Making the `@` menu search the whole store turns an autocomplete into a
directory query, which is what the store page is for, and it exposes every user to every Agent's name
in a surface where they cannot evaluate it. The menu's last row is "Browse all agents →".

## D12 — Publisher is a display identity, set by the author and owned by the admin

Attribution motivates publication — people build better Agents when their name is on them. But an
institutional store also needs Agents that speak *as the institution*, not as whoever on staff
happened to build them. Both are true, so publisher becomes its own field.

**`listing.publisherId` references an admin-managed `PublisherProfile`**, separate from the Agent's
`ownerId`:

```
PublisherProfile {
  id, label, kind: 'institution' | 'department' | 'individual',
  verified: bool, iconKey?, order: int, enabled: bool
}
```

- **Authors propose.** At submission the author picks from the publishers they are eligible for:
  always their own individual profile (auto-created from their display name, `verified: false`), plus
  any department or institution profile an admin has made them eligible for.
- **Admins decide.** The reviewer can change the publisher at approval and at any time afterward from
  the Listings table. Approval is what makes an attribution authoritative.
- **Admins can publish as the institution.** An admin may set any listing's publisher to an
  institution or department profile regardless of who authored it — this is how the store gets its
  day-one set of official Agents (D10's "never an empty shelf") without those Agents carrying a staff
  member's personal name.
- **`verified` is admin-only** and drives the check mark next to the publisher on the detail page.
  Individual profiles are never verified; that mark means "a university team stands behind this."

⚠️ **Publisher is display-only and must never gate access.** `ownerId` continues to govern edit
rights and — critically — Skills v2 invoke-through resolution, which matches on
`skill.owner_id == agent.owner_id`. Re-attributing a listing to an institution publisher changes the
name on the shelf and **nothing** about who can run it or whose skills resolve. This is the same trap
as `allowedAppRoles` on a resource (CLAUDE.md RBAC rule): a display projection that looks like a
grant will eventually be read as one. Enforce it by keeping `publisherId` out of every access check.

## D13 — Everything Discover renders is admin-editable; behavior stays with the author

The store is a surface the institution puts its name on, so **every field the browse and detail pages
render must be editable by an admin without the author's involvement.** That splits the Agent record
along a line worth making explicit:

| Presentation — admin may edit | Behavior — author owns |
|---|---|
| `name`, `tagline`, `iconKey` | `instructions` |
| `listing.category`, `listing.publisherId` | `bindings[]`, `modelConfig` |
| store-front rank, `listing.state` | `starters[]` (author's, but admin may hide the listing) |

An admin fixing a typo in a tagline, swapping a category, or replacing an off-brand icon should not
require a round trip through the author. An admin **cannot** edit instructions, bindings or the model
— that would make the reviewer responsible for behavior they did not write and cannot test, and it
would silently break an Agent its author still maintains.

Admin edits to presentation are **recorded and shown to the author** on their My Agents card ("An
admin updated the category on Jul 24"). Editing someone's listing quietly is how you lose authors.

## D14 — Ship gated, GA by grant

House pattern: kill switch `AGENT_MARKETPLACE_ENABLED` (**default on**, `=false` disables — per the
feature-flag convention) plus an RBAC capability `agent-marketplace` that 404s the routes for
ungranted roles, mirroring the `skills` gate from Skills v2 PR-5. Note D9's `default`-role trap
applies here too: granting the capability to `default` does **not** reach everyone.

---

## D15 — Users report problems; the report is private and lands in the admin queue

A store with no way to say "this one is wrong" pushes that signal into email, or nowhere.
Any user who can run a published Agent can **report a problem with it** from the detail
page, and the report joins the admin console as a second work stream beside submissions.

**This is not a review, and the distinction is the whole design.** The non-goal below
still stands: no stars, no public comments, no visible counts. A report is a *private
message to the curator*, never shelf content, and nothing a reporter writes is ever
rendered to another browsing user. Ratings would make the store a popularity contest we
have deliberately declined to run (see the ranking caveat); reports make it maintainable.
Five decisions:

**1. Reports are triaged, never auto-forwarded to the author.** The reviewer decides what
reaches the person who built the thing. Piping raw user text straight to an author is how
you get one bad message ending a volunteer's willingness to publish — and the author
cannot act on "this is stupid" anyway. When a report is actionable the reviewer uses the
existing **request changes** or **takedown** path, whose reason field is already the
author-facing channel. Reports are the *evidence* for that reason, not a substitute for it.

**2. The reporter is visible to the admin and never to the author.** Admins need identity
to spot a brigade or a grudge; authors need the substance, not the name. This is the
narrowest split that serves both.

**3. Reportable means published.** You may report what the store offered you. Reporting a
`private` or `in_review` Agent is not a thing — nobody outside the author was invited to
it, and a takedown is not available as a remedy for something that was never listed.

**4. A reporter gets one open report per Agent.** A second submission while the first is
still open updates it rather than stacking. Without this the queue is trivially floodable
and the count at the top of the nav stops meaning anything.

**5. Reports have their own tiny lifecycle: `open → resolved | dismissed`.** Deliberately
not a mirror of the listing state machine — the report is a note about an Agent, not a
state of it. Resolving a report never changes `listing.state`; if a report warrants
delisting, the admin takes the Agent down and that is a separate, recorded act.

`reason` is a small fixed set (`inaccurate`, `broken`, `inappropriate`, `other`) plus free
text. The set exists so the queue can be sorted by severity without reading every note;
`inappropriate` is the one that should page a human rather than wait for a sweep.

⚠️ **A report is not a permission signal and not a ranking input.** It must not feed
`usageCount`, the store front, or any ordering. The moment report volume influences
placement, reporting becomes a way to bury a competitor's Agent.

## Data model

### Agent record (additive — no new table, per Designer D2)

```
Agent {
  ...                                    # unchanged
  tagline?:  str                         # ≤80 chars, shelf subtitle (D4)
  iconKey?:  str                         # S3 object key, 512×512 (D5); absent → generated fallback
  listing?:  AgentListing                # absent = never submitted (the backfill default, D3)
}

AgentListing {
  state:            'private' | 'in_review' | 'published' | 'changes_requested' | 'taken_down'
  category:         str                  # references an AgentCategory id
  publisherId:      str                  # references a PublisherProfile (D12) — display only
  submittedAt/By:   str
  reviewedAt/By:    str
  reviewNote:       str                  # rendered on the author's card
  adminEdits:       [ { field, at, by } ] # D13 — surfaced to the author, never silent
}
```

**Sparse directory index (GSI5 on `rag-assistants`)**, carried from `agent-directory.md` D2 and
written **only when `state == 'published'`**:

```
GSI5_PK = LISTED#{category}
GSI5_SK = CREATED#{created_at}
```

GSI5 is the next free slot (GSI_/GSI2/GSI3/GSI4 are `OwnerStatusIndex`, `VisibilityStatusIndex`,
`SharedWithIndex`, `DueSyncIndex`). `DueSyncIndex` is the direct precedent — a sparse index on the
same table whose keys exist only in the active state
([`rag-data-construct.ts:154`](../../infrastructure/lib/constructs/rag/rag-data-construct.ts)).
Unpublication is enforced by physics: no key, so the query cannot return it.

**Ranking caveat, stated plainly:** `GSI5_SK` is `created_at`, so browse is **newest-first**. A
popularity sort needs a mutable sort key (hot-item rewrite per use) or a periodic recompute; v1 does
neither. The store front (D10) is the manual override. Deferred to F6b, not silently approximated.

### Reports (D15)

Child rows under the Agent, so a report is deleted with the Agent it concerns and never
outlives it:

```
PK = AST#{agent_id}, SK = "REPORT#{created_at}#{report_id}"
{ reporterId, reporterName, reason, note, state, createdAt,
  resolvedAt?, resolvedBy?, resolutionNote? }
```

**Sparse open-report index (GSI6 on `rag-assistants`)**, written only while
`state == 'open'`, exactly as GSI5 is written only while published:

```
GSI6_PK = "REPORTS#OPEN"
GSI6_SK = CREATED#{created_at}
```

A single partition is correct here and would not be for the directory: the open queue is
bounded by how fast admins work, it is read only by the admin console, and it wants one
chronological sweep rather than per-category slices. If the queue ever outgrows a hot
partition, that is a *product* signal (nobody is triaging) before it is a capacity one.

D15.4's one-open-report-per-reporter rule needs a lookup by `(agent, reporter)`; do it
with a conditional write on a deterministic `report_id` derived from the reporter id,
not a second index — the write already knows both halves of the key.

### Store front, categories, publishers

```
PK = "AGENT_STOREFRONT", SK = "CONFIG"    # { featured: [agentId, ...] } — ordered array
PK = "AGENT_CATEGORIES", SK = "CAT#{id}"  # { id, label, order, enabled }
PK = "AGENT_PUBLISHERS", SK = "PUB#{id}"  # PublisherProfile (D12)
PK = "AGENT_PUBLISHERS", SK = "ELIG#{publisherId}#{userId}"   # who may propose this publisher
```

The store front is a **single item holding an ordered array** rather than per-item `order` fields:
the list is ≤ 10 entries and reordering must be atomic. Categories and publishers use per-item
records with `order` because they are referenced by listings and need independent rename/disable,
matching `UserMenuLink` exactly. Eligibility items are the *proposal* allowlist only — an admin can
set any publisher on any listing regardless of eligibility (D12), so eligibility never appears in an
access check.

An individual `PublisherProfile` is auto-created on first submission from the author's display name,
with `verified: false` and an eligibility item for that author alone.

### Default pins (role side)

On the existing **app-roles table**, alongside the grant items:

```
PK = ROLE#{role_id}, SK = AGENT_PIN#{agent_id}
   attributes: { order: int, locked: bool, createdAt, createdBy }
```

This mirrors `TOOL_GRANT#` / `MODEL_GRANT#` / `SKILL_GRANT#`
([`repository.py:344`](../../backend/src/apis/shared/rbac/repository.py)) and inherits the existing
`transact_write_items` path and `_invalidate_caches_for_role` bump for free.

**A pin is not a permission.** It must stay out of `EffectivePermissions` and out of
`_compute_effective_permissions`. Pins do not inherit through `inheritsFrom` and are not merged into
the permission payload; they are resolved by their own query. Folding them in would pollute a
structure the model call path depends on.

### User pin state (user side)

New item on the **user-settings table** (`PK=USER#{user_id}`), following the established
`USER#{id}` + uppercase-semantic-SK convention (`SETTINGS`, `TOOL_PREFERENCES`):

```
PK = USER#{user_id}, SK = "PINNED_AGENTS"
{
  pinned:    [ { agentId, order, pinnedAt } ],   # the user's own pins
  dismissed: [ agentId, ... ]                    # tombstones — D9.3, the resolver MUST respect these
}
```

Resolution (per request, cached with the existing role-cache TTL):

```
effective = (⋃ role pins for the user's matched roles)  −  dismissed(unlocked only)  ∪  own pins
sorted by (locked desc, role priority desc, order asc, name asc)
```

### Directory read shape

`AgentResponse` carries `instructions`; the store must not. Add `AgentListingResponse` —
`agentId, name, tagline, iconUrl, publisher {label, kind, verified, iconUrl}, category, state` —
with **no `instructions`, no `ref` values, and no binding ids**. The detail read adds `description`, `starters[]`, `model`, `updatedAt`,
and a `capabilities[]` of `{label, kind}` (names, not ids).

⚠️ **Behavior change to make explicit:** `GET /agents/{id}` today returns `instructions` to any
PUBLIC viewer. Gate `instructions` to `permission in ("owner","editor")` on the detail read. Under
link-sharing that exposure was bounded; under a store it is not.

## APIs

All SPA-facing under `apis/app_api/`, all `Depends(get_current_user_from_session)` per the CLAUDE.md
auth rule; admin routes `Depends(require_admin)` (= `require_app_roles("system_admin")`,
[`rbac.py:75`](../../backend/src/apis/shared/auth/rbac.py)). All behind the D14 gate.

| Route | Purpose |
|---|---|
| `GET /agents/store?category=&cursor=` | Browse. Sparse-GSI query → `AgentListingResponse[]`. Newest-first. |
| `GET /agents/store/front` | Featured order + categories for the browse header. |
| `GET /agents/{id}` | Detail. Adds `listing`; gates `instructions`. |
| `GET /agents/{id}/runnability` | Per-invoker capability preview (D6). |
| `POST /agents/{id}/icon` | Upload a square icon (D5) → S3, returns `iconKey`. |
| `POST /agents/{id}/listing/submit` | Author submits. Runs D7 checks; 400 on a `memory_space` binding. |
| `DELETE /agents/{id}/listing` | Author unpublishes. |
| `GET /agents/pins` · `POST`/`DELETE /agents/{id}/pin` | The user's effective pin list; pin / dismiss (D9). |
| `POST /agents/{id}/report` | Report a problem with a published Agent (D15). One open report per reporter. |
| `GET /admin/agents/reports` · `POST /admin/agents/reports/{reportId}/resolve` | Report queue; resolve or dismiss, reporter visible (D15). |
| `GET /admin/agents/submissions` · `POST /admin/agents/{id}/review` | Review queue; approve / request changes (D2). |
| `GET /admin/agents/listings` · `POST /admin/agents/{id}/takedown` | Listings table; delist with a reason. |
| `PATCH /admin/agents/{id}/listing` | Edit presentation only — `name`, `tagline`, `iconKey`, `category`, `publisherId` (D13). Rejects any behavior field. |
| `GET`/`POST`/`PATCH`/`DELETE /admin/agents/publishers` · `PUT .../{id}/eligibility` | Publisher profiles, `verified`, and who may propose each (D12). |
| `GET`/`PUT /admin/agents/storefront` | Featured order (D10). |
| `GET`/`POST`/`PATCH`/`DELETE /admin/agents/categories` | Category set (D10). |
| `GET`/`PUT /admin/roles/{roleId}/agent-pins` | Default pins for a role (D9). |

The role-pin route lives under `/admin/roles/` because **the AppRole record is the source of truth**
(CLAUDE.md RBAC rule). If we later add an agent-side "which roles get this pinned?" picker, it must
write *through* to each role's pin items and derive the field back on read — exactly the
`set_roles_for_skill` pattern
([`skills/service.py:523`](../../backend/src/apis/app_api/skills/service.py)), never a role list
persisted on the Agent.

## Frontend

`/agents` becomes a hub with tabs: **Discover · My Agents · Pinned**, plus **Admin** for
`system_admin`. Routes `/agents/discover`, `/agents/mine`, `/agents/pinned`, `/agents/admin/*`, and
`/agents/:id` for detail, so deep links and browser back work.

- **Discover** — large search, a Pinned strip, the Featured row (gradient tiles), then two-column
  category lists of icon + name + tagline rows with a `＋` affordance.
- **Detail** — 84px icon, publisher, tagline, Add / Start chat; a hero band carrying the
  `@Agent` starter prompt; About; "Try asking" from `starters[]`; a Details panel and "What it can
  access" with the D6 availability line.
- **My Agents** — listing-state badge per card (`Draft` / `Private` / `In review` /
  `Changes requested` / `Published`), the reviewer's note inline, Icon and Submit actions.
- **Admin** — left sub-nav over the seven D10 surfaces.
- **`@` menu** — grouped "Your agents" / "Pinned", publisher as secondary text, "Browse all →" last.

Reuse the list-page token idiom (`rounded-2xl`, `text-sm/6`, flat), `@angular/cdk/dialog` for every
dialog, signals throughout, tooltips on icon-only buttons, and the role-picker dialog structure from
`skill-role-dialog.component.ts` for the default-pins editor.

Working mockup of all of the above: the `agent-marketplace` artifact (v2, admin console included).

## Phasing

```
Phase 0  This spec                                                          ✅ shipped
Phase 1  listing block + state machine + sparse GSI5 + submit/review/takedown API
         + publisher profiles (D12) + admin Review queue & Listings
         + backfill (no listing block)                               ✅ shipped (PR #731)
Phase 2  GET /agents/store + the Discover page + categories admin           ← in progress
Phase 3  Detail page + runnability + the instructions gate
Phase 4  Icons: upload, S3, generated fallback, all four render sizes
Phase 5  Pins: user pin state + Pinned tab + store front admin
Phase 6  Default pins by role (D9) + the assignment-time runnability check
Phase 7  @-mention in the composer
Phase 8  Problem reports (D15): report action + admin Reports queue   ← depends on 3
Later    Ranking + full-corpus search via the Registry catalog (F6b)
```

Phase 8 sits after the detail page because that is where the report action lives — there
is no other surface a user is looking at when they decide something is wrong. It is
otherwise independent of 4–7 and can run alongside them.

Phase 1 is the smallest thing that is independently useful: authors can submit, admins can approve
and take down, and nothing is user-visible until Phase 2. Phases 4–7 are independent of each other
and parallelizable once 1–3 land.

## Non-goals (v1)

- **No "Plugin" primitive** (D1). One noun.
- **No install / fork / copy** (D8). Pin only.
- **No re-review on edit** (D2). The listing is reviewed, not every subsequent change.
- **No pin fan-out job** (D9.1). Role pins resolve live.
- **No ratings, reviews, or comments.** Social proof is the store front and the publisher.
  D15's problem reports are the deliberate exception that proves this: they are private to
  admins, never rendered to another browsing user, and never an input to ranking.
- **No popularity ranking.** Newest-first plus a curated front, honestly labeled.
- **No cross-tenant or external publishing.** The store is institution-scoped.
- **No agent versioning or changelogs.** A published Agent is a live pointer; updates are immediate.

## Resolved

- **Queue ownership** — `system_admin` for v1. A granular "marketplace curator" permission is an
  explicit follow-up; do not open a second permission axis in this epic (D2).
- **Publisher identity** — its own admin-managed `PublisherProfile`, proposed by the author and
  owned by the admin, with institution publishing supported so official Agents don't carry a staff
  member's personal name (D12). Display-only, never an access gate.

## Open questions

- **Review SLA.** D2's defense against `agent-directory.md` D7 is operational: a stated turnaround.
  The owner is settled; the commitment is not.
- **`tagline` backfill.** New required-ish field on existing agents. Derive from the first clause of
  `description` at submit time and let the author edit, or require it outright?
- **Locked-pin ceiling.** Should the system cap locked pins per role (say, two)? Without a limit,
  "locked" becomes the default choice and the sidebar becomes someone else's toolbar.
- **Quota attribution.** Confirm the invoker pays for a store-launched run, not the author. The
  existing per-user model implies yes, but a published Agent is the first case where a stranger's
  usage is attributable to someone else's configuration.
