# Agent Marketplace — a browsable store for published Agents

**Status:** ✅ **Implemented** (phases 0–8 shipped 2026-07-24 → 2026-07-26). Originally drafted
2026-07-24 as a proposal. Every decision below describes code that exists; where a decision was
later revised, the revision is recorded in place rather than by editing history away — see D6
(runnability, two states not three), D9.7 (locked-pin friction) and D14 (GA). The only deferred item
is the ranking / full-corpus search note under D10, which belongs to the Registry epic.

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
**stated review SLA** and a pending-count badge on the admin nav so the queue is visible rather than
discovered.

**The SLA, committed (was an open question):**

| Queue | Turnaround |
|---|---|
| Submissions | **Two business days** |
| Reports — `inappropriate` | **Same day** |
| Reports — everything else | **Weekly sweep** |

Phase 8 added reports as a second queue, and they are deliberately not on the same clock. A
submission is a person waiting to publish; a report is mostly maintenance signal, and `inappropriate`
is the one that should page a human rather than wait (D15). One number covering both would either
over-promise on reports or under-promise on submissions.

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
listing can drift from what was approved. Mitigation is visibility, not a gate.

⚠️ **That risk got sharper when D9 added locked pins, and the mitigation had to get sharper with
it.** "No re-review on edit" is entirely defensible for an Agent someone *chose* to pin. It reads
differently once an admin has pinned it *for* them and removed the opt-out: the author rewrites the
instructions, the new behavior is live immediately for every member of the role, and the affected
user cannot even dismiss it. Each of these decisions was reasonable alone; none was evaluated against
the others.

So approval records a **SHA-256 of the instructions it approved** (`listing.approvedInstructionsHash`),
and the admin Listings table marks any published listing whose current instructions no longer match.
This is still not a gate — it is the curator's reason to look.

The marker reports **two distinct claims and must never merge them**:

| Value | Means | Basis |
|---|---|---|
| `instructions` | Behavior definitely changed after approval | Measured — hash mismatch |
| `edited` | The record changed after review; cause unknown | Inferred — `updatedAt` > `reviewedAt` |

The inferred value exists only for listings approved before the hash shipped — which is precisely the
already-published, possibly-locked back catalogue this decision is about, so a hash-only marker would
have been blind exactly where it mattered. It is deliberately the weaker signal: `updatedAt` bumps on
every write, including an admin's own D13 presentation edit and a harmless author rename. Reporting
those as "behavior changed" would have admins chasing their own typo fixes, and a governance marker
that cries wolf is one that gets learned-ignored. Everything approved since resolves to `instructions`
or to nothing.

Still deliberately absent: versioning, changelogs, and re-queueing on edit. Revisit if the marker
shows this is actually being abused rather than merely possible.

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
- **Not available to you** — names what is missing; the Start chat button is disabled

⚠️ **REVISED — there were originally three states.** A middle *"Runs with limits for you"* named
an unavailable **optional** binding. It was never reachable and has been removed (#747).

Two reasons, and the second is the one that matters. First, it degraded only when a binding declared
`config.optional == true`, and nothing ever wrote that flag — no API accepted it and the Designer
had no control for it, so every gap resolved to blocked and the model was really two states already.
Second, and decisive: building toward it would have contradicted `agent-designer.md` D5, whose
Non-goals say *"No **downgrade** on missing capability (block-only v1)"* and call downgrade "a later
opt-in". Block-only is what `agent_binding_resolver` actually implements — it raises for model, tool,
skill and memory_space alike. A preview that offers an outcome the runtime cannot produce is a
preview that lies, which is the same standard that put runnability on the detail page instead of the
shelf in the first place.

If downgrade is ever taken up, it starts at the resolver and this line grows back with it. It is not
a preview-layer decision.

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

**7. Locking is bounded by friction, not a cap.** (Resolves the former "locked-pin ceiling" open
question, #748.) It asked whether to cap locked pins per role, say at two. **No cap.** The admin
console shows the cost instead: a running locked count on the seed header, a warning past a
threshold, and — the part an admin cannot work out for themselves — how many locked seeds *every
other role* already holds.

The concern was real and phase 6 sharpened it. A locked seed genuinely cannot be dismissed
(verified on dev: a lock beats a user's own tombstone, which is the specced behavior and is right —
a lock a user could dismiss would be pointless). So an admin choosing between "seed" and "seed
locked" has no reason not to lock: locking guarantees the rollout lands and the cost falls on
someone else's sidebar.

**A cap does not work here, and the reason is structural.** Pins merge as a *union* across every
role a user matches, and a lock from any one of them wins (D9.2). So a per-role cap of two does not
mean a member sees at most two locked Agents — someone in five roles sees ten. Capping the union
instead is not implementable: role membership resolves per user from Entra claims at request time,
so which roles co-occur on one person is unknowable when an admin saves a seed list. Enforcing it at
read time would be worse than no cap, because it would silently drop an admin's lock for some users
— a rollout that appears to have landed and did not.

Friction has none of those failure modes, nothing to migrate for roles already over any line, and a
threshold that can move without breaking a saved list. If evidence of over-locking shows up, revisit
with the counts this change makes visible.

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

## D14 — Ship gated, GA by kill switch

**REVISED.** The kill switch `AGENT_MARKETPLACE_ENABLED` (**default on**, `=false` disables — per
the feature-flag convention) is the **only** lever. There is no `agent-marketplace` RBAC capability,
and the sidenav entry is gated on the API surface alone (`showAgents()`), not on `isAdmin()`.

This decision originally called for the kill switch *plus* an RBAC capability that 404s the routes
for ungranted roles, "mirroring the `skills` gate from Skills v2 PR-5". Two things were wrong with
that:

**The capability axis does not work.** The admin roles UI builds its `grantedTools` control from the
tool catalog with no free-text entry, so a feature-capability id cannot be granted from the UI at
all — only by hand-writing DynamoDB items. That is precisely why the `skills` gate was removed and
why `scheduled-runs` 403'd in prod and was dropped (see `AppRoleService.resolve_user_permissions`).
Building it a third time ships a gate nobody can open. Per-role rollout of a *feature surface* needs
a grantable capability axis that does not exist yet; it is not in this epic's scope.

**"Gated" was not true anyway.** Until this revision the de-facto gate was one template condition,
`@if (showAgents() && isAdmin())`, on the nav entry. `/agents/discover`, `/agents/:id` and
`/agents/pinned` carried `authGuard` only, the composer `@`-menu had no admin check at all, and a
role-seeded pin (D9) pushed Agents into a member's **Pinned** tab unprompted — working as designed.
The marketplace was reachable by any authenticated user through three doors while hidden behind the
one door we controlled. Half-revealed is worse than either end state: nobody could answer "who can
see this?" without reading four files, and the honest answer was "more people than the nav suggests".

So the store is GA. D9's `default`-role trap still applies to everything that *is* role-scoped here
(seeded pins): granting to `default` does **not** reach everyone, because that role is consulted
only when a user matches zero AppRoles.

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
PK = AST#{agent_id}, SK = "REPORT#{report_id}"
{ reporterId, reporterName, reason, note, state, createdAt,
  resolvedAt?, resolvedBy?, resolutionNote? }
```

⚠️ **As built, the sort key carries no timestamp.** It was drafted as
`REPORT#{created_at}#{report_id}`, which cannot satisfy the D15.4 rule two paragraphs
below — see the Phase 8 notes. `report_id` is `sha256(agent_id:reporter_id)[:16]`, and
the chronology lives in `GSI6_SK`, which is where it is actually read.

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
| `POST /agents/{id}/icon` | Upload a square icon (D5) → S3, returns `iconKey` + `iconUrl`. Owner or editor. |
| `DELETE /agents/{id}/icon` | Clear the icon, back to the generated gradient. Admin `PATCH` can only *replace* `iconKey`, so without this an author cannot undo an upload. |
| `GET /agents/{id}/icon` | Serve the bytes, `immutable` + ETag. What makes `iconUrl` a stable path (`/agents/{id}/icon?v={digest}`) rather than a presigned URL that changes on every read and re-downloads every shelf icon. Readable by anyone when the listing is published — the shelf already shows that agent's name, tagline and emoji to every browsing user. |
| `POST /agents/{id}/listing/submit` | Author submits. Runs D7 checks; 400 on a `memory_space` binding. |
| `DELETE /agents/{id}/listing` | Author unpublishes. |
| `GET /agents/pins` · `POST`/`DELETE /agents/{id}/pin` | The user's effective pin list; pin / dismiss (D9). Pinning is gated on the caller being able to *reach* the Agent, not on it being published. |
| `POST /agents/{id}/report` | Report a problem with a published Agent (D15). One open report per reporter. |
| `GET /admin/agents/reports` · `POST /admin/agents/{id}/reports/{reportId}/resolve` | Report queue; resolve or dismiss, reporter visible (D15). Also `GET /admin/agents/{id}/reports` for one Agent's history and `GET /admin/agents/queues` for the two nav counts. |
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
Phase 2  GET /agents/store + the Discover page + categories admin     ✅ shipped (PR #732)
Phase 3  Detail page + runnability + the instructions gate            ✅ shipped (PR #733)
Phase 3.5 Author Submit UI: submit dialog (category, note, D7 disclosures),
         listing-state badges + reviewer note + D13 edit trail on My Agents,
         withdraw/unpublish, GET /agents/{id}/listing/preflight   ✅ shipped (PR #734)
Phase 4  Icons: upload, S3, generated fallback, all four render sizes  ✅ shipped (PR #735)
Phase 5  Pins: user pin state + Pinned tab + store front admin        ✅ shipped (PR #736)
Phase 6  Default pins by role (D9) + the assignment-time runnability check
                                                                      ✅ shipped (PR #737)
Phase 7  @-mention in the composer                                    ✅ shipped (PR #738)
Phase 8  Problem reports (D15): report action + admin Reports queue         ← in progress
Later    Ranking + full-corpus search via the Registry catalog (F6b)
```

Phase 8 sits after the detail page because that is where the report action lives — there
is no other surface a user is looking at when they decide something is wrong. It is
otherwise independent of 4–7 and can run alongside them.

Phase 1 is the smallest thing that is independently useful: authors can submit, admins can approve
and take down, and nothing is user-visible until Phase 2. Phases 4–7 are independent of each other
and parallelizable once 1–3 land.

**Phase 5 notes (as built).** Two decisions worth recording because the spec left them open:

- **Pinning is gated on reachability, not on publication.** `POST /agents/{id}/pin` accepts anything
  the caller could open (owner, editor, viewer), not only published listings. Publication decides
  what the store *offers*; a pin is a bookmark, and D11 scopes the `@` menu to "your own and pinned
  Agents", which presumes an author can pin their own unpublished work. The read applies the same
  gate on every request, so a pin never becomes a grant.
- **The featured row is not self-healing.** A taken-down Agent drops out of what the store and the
  admin console *render*, but its id stays in `AGENT_STOREFRONT` until an admin saves the row. A GET
  that pruned would rewrite an admin's curation as a side effect, and a reversed takedown would have
  silently cost the Agent its slot. The admin surface names the stale ids instead
  (`AdminStoreFrontResponse.unavailable`).

Promotion lives on the **Store front** surface rather than as a star on the Listings table: the row
is an ordered list, and a per-row toggle can express membership but not position.

**Phase 6 notes (as built).** Four, the first of which was a live trap:

- **⚠️ `AppRoleRepository._delete_mapping_items` deleted every non-`DEFINITION` item under
  `PK=ROLE#{id}`, and `update_role` calls it before rebuilding.** Pins are deliberately not part of
  the `AppRole` record — a pin is not a permission — so nothing would have rewritten them: any edit
  to a role's name, priority or grants would have silently emptied its seed list. It now deletes by
  *prefix* (`JWT_MAPPING#`/`TOOL_GRANT#`/`MODEL_GRANT#`/`SKILL_GRANT#`), with `AGENT_PIN#` added only
  for `delete_role`, where the role itself is going away. Any future per-role item that is not
  reconstructible from `_build_role_items` needs the same protection.
- **Role pins live in `assistants/role_pins.py`, not in `AppRoleRepository`.** The repository already
  owns that table, and that is exactly the coupling worth refusing: keeping the pin read out of the
  module that computes permissions makes "a pin is not a permission" structural rather than a comment.
  The resolver reads `resolve_user_permissions` for its `app_roles` list *only* — the matched role
  ids, including the `default` substitution — and queries the pins itself.
- **Two separate checks on the admin row, because they have different owners.** *Reachability* is
  visibility (`PRIVATE`/`SHARED` → the seed resolves to nothing for members; the author fixes it) and
  *runnability* is the D9.5 diff against the role's `effective_permissions` (the admin fixes it).
  Both **warn**; neither blocks the save, since an admin may legitimately seed something whose author
  is mid-publish. `memory_space` is reported as a *note*, never as present or missing: a role does not
  grant memory spaces, so "ready" must not claim to have checked one. `MAX_ROLE_PINS = 25` — stricter
  than the user's own `MAX_PINS = 100`, because this shelf is somebody else's.
- **Where a user's own pins sort.** The spec's `(locked desc, role priority desc, order asc, name asc)`
  has no answer for a pin with no role, so own-only pins follow the seeded ones, ordered by their own
  `order`. An own pin that a role *also* seeds sorts as the seed and reads as `source: "user"` — the
  D9.1 escape hatch — while `locked` still follows the role's flag. The removal warning lives on the
  console's **Save**, not on the row's `✕`: the editor is staged, and Save is the moment anything
  reaches other people.

**Phase 7 notes (as built).** D11 said "hands **that turn** to the Agent … without leaving the
thread" in one sentence, and that sentence turned out to be the whole phase:

- **⚠️ The invocation path forbade it outright.** `chat/routes.py` 400s on both *"Cannot change
  assistants mid-session"* and *"Assistants can only be attached to new sessions"*, and the SPA
  treats the Agent as session-wide (URL query param → session preferences → self-heal on reload).
  A mention is precisely the case both rules were written to reject. **Phil chose the per-turn
  reading** over the two cheaper alternatives (mention opens a new thread; mention only on an
  empty thread), because the store's other surfaces already cover those and neither is worth a
  menu in the composer.
- **One field, one flag.** The mention rides the existing `rag_assistant_id` with a new
  `agent_mention: true` beside it, rather than a second id field — otherwise every downstream
  step (RAG, binding resolution, memory injection, the resume snapshot) would need teaching about
  a second way to name the Agent running the turn. The flag is a *binding* signal, never an
  authorization one: `get_assistant_with_access_check` still gates the Agent, so a forged flag
  buys nothing but a skipped write.
- **Validation and persistence move together**, via `binds_conversation` in
  `inference_api/chat/agent_binding_policy.py` (the `system_prompt_resolver` precedent — the rule
  is three lines, the route is a thousand, so it lives where a test can reach it). Splitting them
  gives two silent bugs: validate-only refuses the *second* mention in a thread; persist-only lets
  one `@` annex the conversation.
- **💰 A mention costs ONE prompt-cache prefix re-write, not two** — measured, not predicted. The
  Agent's bindings change `toolConfig`, which sits first in the prefix, so the mention turn
  invalidates system and history behind it and genuinely re-writes the whole prefix. **The next
  plain turn does not re-write it back**: the base `toolConfig` and system prompt revert
  byte-identically and read from the still-live pre-mention entry, so the swap-back is a cache
  *hit* that writes only the mention exchange as a delta.

  Measured on dev (session `bf9481e7-62ec-4a14-817f-546a2953588d`, Sonnet 5, mention and
  swap-back 28s apart):

  | turn | status | cacheRead | cacheWrite |
  |---|---|---|---|
  | mention | `miss_avoidable` | 0 | 4301 |
  | next plain turn | **`hit`** | 2720 | 138 |

  At a 50k-token prefix and Sonnet 5's $2.30/MTok write premium (`$2.50` write less `$0.20`
  read) that is roughly **$0.12 per mention**, about half what this section originally claimed.
  Still bounded per mention rather than per turn, and it still buys the thing the feature is for.
  If mentions ever become common in long threads, the lever is to *bind* the conversation after a
  mention rather than to make the swap cheaper.

  ⚠️ Two caveats worth keeping attached to the number. **The swap-back only hits inside the
  ~5-minute cache TTL** — a slower reply re-writes the prefix regardless, and that cost is not
  attributable to the mention. And **an Agent that pins its own model** (`modelConfig` →
  `model_override`) is categorically more expensive: cache entries are per-model, so such a
  mention can hit nothing and its write is never re-read.

  The original estimate here was not merely conservative — it was measured once against a bug.
  Before #741 was fixed the swap-back looked *cheaper* than this (write 71, not 138) because the
  stale agent was silently omitting the mention exchange from the prefix. A cost number taken
  from a broken run flattered us in one direction while the spec's prediction erred in the other.
  Re-measure after any change to the invocation path rather than trusting either.
- **Known edge, pre-existing:** `continue_truncated` skips the whole assistant block, so a
  "Continue" after a max_tokens truncation runs without the Agent — true for bound Agent
  conversations before this phase, and unchanged by it.

**Phase 8 notes (as built).** Six, the first of which is a contradiction inside this spec
and the fourth of which was a live trap:

- **⚠️ The report's sort key is deterministic, not chronological — the data model above
  and D15.4 could not both be satisfied.** The sketch says
  `SK = REPORT#{created_at}#{report_id}`; D15.4 says enforce one-open-report-per-reporter
  "with a conditional write on a deterministic `report_id` … the write already knows both
  halves of the key". It does not: a key containing `created_at` cannot be conditionally
  updated without first *reading* the row to learn the timestamp — which is the extra
  lookup D15.4 exists to avoid. So the key is `REPORT#{sha256(agent_id:reporter_id)[:16]}`
  and the chronology moved to `GSI6_SK`, which is the only place anything reads it (the
  queue never sorts by the table's sort key). The reporter is hashed rather than embedded
  so the sort key is not itself an enumerable directory of who reported what — D15.2 puts
  the reporter in the *item*, where an admin reads it, not in a key any partition query
  returns.
- **The rule is three conditional writes and no read**: create-if-absent → else
  update-if-open → else overwrite-if-closed. Amending an open report preserves `createdAt`
  and the index key, so re-submitting is not a way to jump the queue; filing again *after*
  a resolution starts a genuinely new report and clears the old verdict. Two taps racing
  cannot stack two reports or resurrect a resolved one.
- **Per (agent, reporter), history is one deep.** A new report replaces the resolved one
  at that key. An append-only archive was considered and declined: nothing would read it,
  and D15.1 already puts the durable record elsewhere — a report is the *evidence* for a
  request-changes or takedown, and that act is separately recorded on the listing.
- **⚠️ `_delete_assistant_cloud` deleted only the `METADATA` item.** Reports are child rows
  precisely so they never outlive the Agent, but nothing swept them — and an orphaned
  *open* report keeps its sparse GSI6 key, so it would have sat in the admin queue forever
  pointing at an Agent nobody can open. The sweep now runs inside the shared delete (so
  every delete path is covered, not just the Agent router's) and is best-effort: failing
  to tidy up must not make an Agent undeletable. The queue therefore also *flags* a row
  whose Agent is gone rather than dropping it — a row that is invisible but still counted
  is one an admin can neither see nor clear.
- **The nav badge needed a counts route.** `AdminListingsResponse.pendingCount` existed
  since Phase 1 but badged nothing: the layout never read it, and it is only populated
  once a queue page has loaded — a badge that appears *after* you visit the queue is not a
  badge. D10 wants both counts visible from anywhere in the console, so `GET
  /admin/agents/queues` returns the two integers and the layout fetches it on init. The
  alternative — having the nav load both queues — would have put a table scan and a full
  row projection behind every click in the admin console. It fails soft per half: a badge
  is orientation, and an unreachable count shows zero rather than breaking the shell.
- **Queue order is `(severity, oldest-first)`, and the owner is not excluded.** The spec
  asks for a chronological sweep and separately says `inappropriate` "should page a human
  rather than wait for a sweep"; sorting severity-first is how both hold. And D15's gate
  is exactly publication — an author can report their own published Agent, because adding
  an owner exception would be a rule the spec does not have, for a case that is at worst
  a redundant queue item.

**Phase 3.5 is a gap this table originally left open.** Phases 1–3 shipped the submit and withdraw
endpoints and the entire reviewer console, but no phase owned the *author's* half of the surface —
so the SPA called `POST /agents/{id}/listing/submit` from nowhere and nothing could reach the store
without a hand-rolled request. It also adds `GET /agents/{id}/listing/preflight`, because D7.1 asks
the dialog to enumerate the exposed skills *before* the author commits and Phase 1's disclosures
were only available in the submit response. The preflight reuses the submit path's own helpers
rather than restating the checks.

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
  This composes with "no re-review on edit" (D2) and locked pins (D9) into a real governance gap —
  see D2 for the drift marker that closes most of it without building versioning.

## Resolved

- **Queue ownership** — `system_admin` for v1. A granular "marketplace curator" permission is an
  explicit follow-up; do not open a second permission axis in this epic (D2).
- **Publisher identity** — its own admin-managed `PublisherProfile`, proposed by the author and
  owned by the admin, with institution publishing supported so official Agents don't carry a staff
  member's personal name (D12). Display-only, never an access gate.
- **Quota attribution** — **the invoker pays**, confirmed in code, not inferred.
  `quota_checker.check_quota(user=current_user, …)`
  ([`chat/routes.py:1319`](../../backend/src/apis/inference_api/chat/routes.py)) resolves against the
  caller, and there is no author-side lookup anywhere on the invocation path. Cost rows land on the
  same person: `PK = USER#{user_id}`
  ([`sessions/services/metadata.py:157`](../../backend/src/apis/app_api/sessions/services/metadata.py)).
  So spend and quota agree, and a published Agent does not bill its author for a stranger's run.
- **Review SLA** — committed, and split by queue. See the table in D2: two business days for
  submissions, same day for an `inappropriate` report, weekly for the rest.
- **`tagline` backfill** — **derive and let the author edit**, at submission. Requiring it outright
  adds a blocking field to every legacy Agent's first submission with no starting point offered,
  which is friction exactly where publication should be easy.

  ⚠️ Implementing this exposed a second problem the question did not mention: `tagline` was
  *author-owned on the wire and unsettable in the UI*. The API accepted it and the model said the
  author owns it, but no Designer control ever wrote one — the same dormant-field shape as the
  `limits` state (#747). So submission is now where an author sets it, prefilled from the
  description's first clause (`deriveTagline`). Prefilling is the point rather than the derivation
  itself: it puts the shelf row in front of the author at the one moment they are looking at what
  the store will say, so a bad line is one edit away instead of a surprise after publication.

## Open questions

*(None outstanding. Everything previously here is resolved above or tracked as its own issue.)*
