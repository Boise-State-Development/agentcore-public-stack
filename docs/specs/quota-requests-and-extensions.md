# Quota requests and extensions — asking for more, granting it, and knowing who pays

**Status:** In progress — PR-1 (backend core) built; PR-2–PR-4 to come
**Date:** 2026-10-30
**Targets branch:** `develop`
**Related:** `docs/specs/quota-cooldown-windows.md` (draft pacing model, compatible),
`docs/specs/granular-admin-permissions.md` (admin scopes), `docs/specs/platform-cost-visibility.md`

---

## 1. Summary

Today a user who runs out of quota is told to "contact your administrator", and
the administrator's only tool is a **quota override** that *replaces* the user's
tier for a date range. Neither side has a workflow, and nothing records whose
money the extra capacity was.

This spec adds three things, each general enough for any deployment of the
stack:

1. **Quota requests.** A user asks for more quota from the places they learn
   they are running out (the warning tab above the composer, the blocked
   message, the Usage page). A request carries an amount, a date, a
   justification, and, when the deployment asks for one, a **funding source**.
2. **Quota extensions.** The thing an admin grants. An extension is
   **additive and period-scoped**: it adds `$X` on top of the user's tier
   limit for each quota period between two dates, and it records whether that
   `$X` is **centrally funded** or **charged back** to a named funding source.
   Extensions stack on the existing tier/override resolution; overrides stay
   exactly as they are. A **standing** extension is the power-user case: an
   admin sets it once per user, and past the central allowance the user keeps
   going with the overage charged to their fund, capped by default and
   announced the turn it starts (§4.4).
3. **Chargeback accounting.** Because the tier limit stays the central
   allowance and extensions sit on top, spend above the tier limit is
   attributable to extensions in a fixed order, and a monthly **chargeback
   report** names, per funding source, what was committed and what was
   actually consumed. The report is the hand-off to whatever process a
   deployment uses to move money; the stack does not post journal entries.

The first release deliberately lets an admin grant without a funding source.
Chargeback is designed in from the start so that turning it on later is a
policy change, not a migration.

---

## 2. What exists today (verified 2026-10-30)

| Piece | Where | Behaviour |
|---|---|---|
| Tier | `agents/main_agent/quota/models.py` `QuotaTier` | `monthly_cost_limit`, optional daily, `period_type`, warning ladder, `action_on_limit` |
| Resolution | `agents/main_agent/quota/resolver.py` `_resolve_from_db` | Override → direct user → AppRole → JWT role → email domain → default tier. Cached 5 min per user, in-process |
| Override | `QuotaOverride` + `UserOverrideIndex` (GSI4) | **Replaces** the tier (`custom_limit` or `unlimited`), one active at a time (`Limit=1`), all-or-nothing. Admin-only, hand-entered user id |
| Enforcement | `agents/main_agent/quota/checker.py` | Per-turn, computed live from the period aggregate; no stored "blocked" state |
| Blocked UX | `apis/shared/quota.py` `build_quota_exceeded_event` | Streams an assistant message: usage table, reset date, "please contact your administrator" |
| Warning UX | `quota-warning-banner.component.ts` | A tab welded to the composer at the tier's ladder rungs (50/75/soft/90) and the per-session notice |
| Self status | `GET /costs/quota-status`, Usage settings page | Limit, usage, remaining, reset, `hasActiveOverride` |
| Admin | `/admin/quota` (Tiers, Assignments, Overrides, Inspector, Events), user detail page | Override form is a bare user-id text box; no queue, no requester context |
| Admin scope | `admin.quota` (delegable) | Guards the whole `admin/quota/` package |
| Inbox | `apis/shared/notifications` (`INBOX#{email}`) | Generic per-user in-app notifications; the bell is currently shown only with `features.projects` |
| Audit | `apis/shared/audit` | Closed action registry, before/after per mutation, denied attempts recorded |
| Storage | `user-quotas` table, `GSI1PK=ASSIGNMENT_TYPE#…`, `GSI2PK=USER#…` (direct assignments), `GSI4PK=USER#…` (overrides) | Pay-per-request, PITR |

Three facts shape the design:

- **Overrides are the wrong primitive for chargeback.** A replacement limit
  erases the boundary between the centrally funded allowance and the extra, so
  there is nothing to bill against. (They remain the right primitive for
  "unlimited for this person" and stay.)
- **Enforcement is computed per turn against live config.** Nothing needs to be
  "unblocked"; a grant takes effect as soon as resolution sees it. The only
  staleness is the resolver's 5-minute cache, and §8.3 removes it for the one
  case that matters.
- **Per-user spend is already aggregated per period** (`get_user_cost_summary`,
  `PERIOD#{YYYY-MM}`). Chargeback can be derived at read time from numbers the
  write path already maintains, so the turn path gains no new write.

---

## 3. Goals and non-goals

**Goals**

- A user can ask for more quota in under a minute, from where they are, and
  can see what happened to the request without asking anyone.
- An admin sees a queue with enough context to decide in one screen (who,
  how much they use, what they asked for, why, what they got before) and can
  approve, adjust, ask for a funding source, or deny, each in one action.
- An admin can also extend a user's quota directly, with or without a request.
- Every extension records its funding mode, and a monthly report attributes
  actual over-tier spend to funding sources. Central spend on extensions is
  reported too.
- Everything that names an institution's finance vocabulary (cost center,
  fund/org/program, grant number, PO) is **deployment-configurable**, never
  hard-coded.
- Adds nothing to the turn path's time to first token and nothing to the
  model prompt.

**Non-goals (v1)**

- Posting charges to a finance system. The report is the integration point.
- Lump-sum extensions that roll over across periods (§13 D3 explains why).
- Multi-step approval chains, or routing requests to different approvers by
  department. The scope model supports delegating approval; routing is later.
- Changing the pacing model. Cooldown windows (`quota-cooldown-windows.md`)
  are orthogonal; §8.5 notes how the two compose.
- Email delivery. The repo has no mail path; notifications are in-app.

---

## 4. Concepts

### 4.1 Central allowance and extensions

```
effective limit (period P) = base limit (tier, or override if one is active)
                           + Σ amount of every extension active in P
```

- The **base limit** is what the deployment funds centrally for everyone in
  that tier. Nothing about it changes.
- An **extension** is `{amount, validFrom, validUntil, fundingMode,
  fundingSource?}`. It adds `amount` to the effective limit **in every quota
  period it overlaps**. The common grant is "the rest of this month"; a
  multi-month grant is explicitly "`$X` more per month, November through
  January".
- Extensions **stack**: two active extensions add both amounts.
- An extension on top of an `unlimited` override is inert, and the UI says so
  rather than offering it.
- Warning rungs, the per-session notice, and the blocked check all run on the
  **effective** limit, so a user at 95% who is granted +$20 on a $30 tier drops
  to 57% and the warnings go quiet on their own.

### 4.2 Funding mode

| `fundingMode` | Meaning | When an admin picks it |
|---|---|---|
| `central` | The extra is absorbed by the central budget. No funding source | Default in v1; small bumps; pilots; "we don't charge back yet" |
| `chargeback` | Over-tier spend attributable to this extension is billed to `fundingSource` | A deployment that charges back, or a large/recurring grant |

The admin chooses per extension. The policy (§4.4) only sets the *default* and
whether the request form asks the user for a funding source at all.

### 4.3 Funding source

A funding source is a small, **schema-defined** record. The deployment defines
the fields; the stack validates and stores them and never interprets them.

```json
{
  "label": "COAS Dean's Office",          // optional, free text the human sees
  "fields": { "fund": "100000", "org": "4210", "program": "10" },
  "authorizerEmail": "fund-owner@example.edu"      // optional: who can authorize this fund
}
```

The field schema lives in the policy (`fundingFields`, §7.5): each field has a
`key`, a `label`, `required`, an optional `pattern` (regex) and `help` text.
A university on Banner defines fund/org/program; a company defines a cost
center; a lab defines a grant number. `authorizerEmail` is a plain field in
v1 (it rides the chargeback report so finance knows whom to confirm with);
asking that person to confirm in-app is Phase 2 (§14).

### 4.4 Standing chargeback (power users)

Some users should never have to ask. A researcher spending a grant, or a team
whose department has agreed to pay, should run past the central allowance and
simply have the overage charged to their fund. That is a **standing
extension**: an extension with `standing: true`, a required funding source,
and an `amount` that is either a per-period **cap** or `null` for uncapped.

- It is a conscious, per-user admin act (§6.4), audited and visible to the
  user on the Usage page from the moment it exists: *"After your $30 included
  quota, usage is charged to Fund 100000 · Org 4210, up to $200 per month,
  through Jun 30, 2027."* The user is also notified when it is created.
- It behaves exactly like any other extension in resolution and in the
  report. The only new behaviour is the **funding notice** (§8.2): the turn
  on which the user crosses from the central allowance into charged spend
  tells them so, and the tab stays up while they are in that band, with the
  running charged amount. Spending someone's fund silently is not a good
  experience for the fund owner or the user.
- **A cap is the default and the recommendation.** The one-conversation
  cache spiral that burned a month's quota in five days is the reason: an
  uncapped arrangement turns that incident into an unbounded bill. The form
  requires a cap unless the admin ticks *No cap* and reads a one-line warning.
  With a cap, the warning ladder and the block run on `base + cap`, so the
  user still hears about it ahead of time and can request more on top.
- A standing extension ends on `validUntil` (default: the policy's
  `fiscalYearEnd`, so a university sets `06-30` once). The Usage page and the
  tab show *ends in 14 days* lazily at read time; no scheduler is needed.
- A user may also *ask* for one: the request form offers *Make this
  ongoing* when the deployment collects funding sources, and the admin
  approves it as standing (§7.1 `kind`).

Standing extensions are last in the consumption order (§9.1), so any
centrally funded extension the user also holds is spent before the fund is
touched.

### 4.5 Policy

One admin-editable record per deployment decides how requests behave at
runtime (the feature flag decides whether the surface exists at all, §12):

| Setting | Default | Purpose |
|---|---|---|
| `amountPresets` | `[10, 25, 50]` | Quick-pick amounts on the request form |
| `allowCustomAmount` | `true` | Whether the user may type an amount |
| `maxRequestAmount` | `100` (0 = no cap) | Upper bound on a *request*; admins can still grant more |
| `defaultThrough` | `end_of_period` | Default end date on the form (`end_of_period` or `days:N`) |
| `maxDurationDays` | `92` | Longest span a request may ask for |
| `justificationMinChars` | `20` | Forces a sentence, not a word |
| `fundingSourceMode` | `optional` | `hidden` (never ask the user), `optional` (collapsible section), `required` (must be filled to submit) |
| `fundingFields` | `[]` | The schema in §4.3; empty with mode `hidden` is the out-of-the-box state |
| `defaultFundingMode` | `central` | Pre-selected mode on the approve form |
| `standingEnabled` | `true` | Whether admins may create standing extensions (needs a non-empty `fundingFields`) |
| `standingDefaultCap` | `100` | Pre-filled per-period cap on the standing form (0 = form starts at *No cap*) |
| `fiscalYearEnd` | `12-31` | `MM-DD`; default `validUntil` for a standing extension is the next occurrence |
| `reportGroupField` | unset | One `fundingFields` key the charge ledger indexes for roll-ups (§7.3), for example `org` |
| `notifyEmails` | `[]` | Admins who get an in-app notification on each new request |

---

## 5. User experience

### 5.1 Where a user can ask

All three entry points open the same dialog. Each shows only when
`features.quotaRequests` is on **and** the user's quota status is not
unlimited.

1. **The warning tab above the composer.** From the tier's soft limit upward
   the tab gains a trailing link: `80% of quota used · $6.00 remaining ·
   Request more`. Below the soft limit the tab stays as it is; the 50% and 75%
   rungs are information, not a prompt to spend more.
2. **The blocked state.** The streamed assistant message keeps its usage table
   and reset date; its last line becomes *"Your administrator can extend your
   quota."* (neutral copy, because the Runtime does not know whether the
   deployment turned requests on, §12). The SPA, which does know, renders a
   **Request more quota** button in the composer's quota tab whenever
   `QuotaWarningService.isQuotaExceeded()` is true. The composer stays
   disabled, as today.
3. **Settings → Usage.** A `Request more quota` button beside the quota bar,
   and a **Your requests** list under it (status, amount, dates, the admin's
   note).

While a request is open, every entry point shows its state instead of the
button: `Request pending · sent Oct 30` (with *Withdraw*), or `Your admin asked
for a funding source · Add it` (§5.4).

### 5.2 The request dialog

```
Request more quota
──────────────────────────────────────────────
You've used $28.40 of your $30.00 monthly quota · resets in 12 days

How much more do you need?      [ $10 ] [ $25 ] [ $50 ]  [ Other: $ ___ ]
                                per month, through  [ Nov 30, 2026 ▾ ]   (end of this period)

Why do you need it?             ┌──────────────────────────────────────────┐
                                │                                          │
                                └──────────────────────────────────────────┘
                                A sentence or two helps your administrator decide quickly.

▸ I have a funding source to charge this to (optional)        ← only when fundingSourceMode ≠ hidden
     Fund  [______]   Org  [______]   Program  [____]
     Who authorizes this fund? (email)  [________________]

                                              [ Cancel ]  [ Send request ]
```

- The header line is the live quota status, so the user never guesses.
- Presets come from policy; "Other" appears only with `allowCustomAmount`;
  amounts above `maxRequestAmount` are refused inline with the cap named.
- "Through" defaults per policy and is capped at `maxDurationDays`. Picking a
  date before the end of the current period shows: *"Your quota drops back to
  $30 on that date, mid-month."*
- The funding section follows `fundingSourceMode`. With `required` it is
  expanded and the submit button waits on it. When it is shown, a checkbox
  **Make this ongoing** turns the request into a standing one (§4.4): the
  amount becomes a per-month cap, the through date defaults to the fiscal
  year end, and the funding source becomes required.
- A user who already holds a standing extension sees its terms in the header
  line instead of a blocked or warning state, and the entry points offer
  *Request a higher cap* only once the cap itself is within the warning
  ladder.
- Submitting shows: *"Sent. You'll see the decision here and in your
  notifications."* Nothing else changes until an admin acts.

### 5.3 After a decision

- **Approved.** The quota bar on the Usage page becomes two segments,
  `$30 central + $20 additional through Nov 30`, and the composer tab's
  numbers update on the next turn. The notification reads *"Your quota was
  increased by $20 per month through Nov 30"* with the admin's note if any.
  If the admin approved a different amount than requested, the notification
  says so plainly: *"You asked for $50; $25 was approved."*
- **Denied.** Notification with the admin's note (required on deny). The
  Usage page keeps the request in the list as *Denied*. The user may submit a
  new request; there is no cooldown in v1 (§15).
- **Expired without an answer.** Not a state in v1; a pending request stays
  pending and visible to admins. Revisit if queues go stale (§15).

### 5.4 When the admin asks for a funding source

The request moves to `info_requested` with the admin's note (for example
*"Happy to approve this. Which fund should it be charged to?"*). The user's
entry points show *Your admin asked for a funding source · Add it*, the
dialog reopens on the funding section with the note at the top, and
submitting returns the request to `pending` with the funding source attached.
The user may also withdraw.

### 5.5 Copy rules

- Dollar amounts always carry the period word from the user's tier
  (`per month`, `per day`).
- Never show the admin a user's conversation content, and never show a user
  another user's anything. The request's context snapshot is numbers only
  (§7.1).
- The blocked message never blames the user and never suggests a workaround
  that bypasses quota.

---

## 6. Admin experience

### 6.1 Where it lives

`/admin/quota` gains a **Requests** tab (first in the tab order while any are
pending, with a count badge) and an **Extensions** tab beside **Overrides**.
The chargeback report is a view on the Requests tab (period picker + export),
not another top-level page. Policy lives under Requests → *Settings*.

The admin nav's *Quotas* item is unchanged and still renders synchronously;
the pending count is fetched after render and only decorates it.

### 6.2 The queue

One row per request, pending first, then `info_requested`, then decided
(collapsed behind a filter):

| Who | Tier · usage now | Asked for | Through | Why (first line) | Funding | Age |
|---|---|---|---|---|---|---|
| R. Okafor (Faculty) | $30.44 / $30 · **blocked** | +$25 /mo | Nov 30 | "Grant proposal due Nov 15, the agent is drafting…" | ◦ none | 2 h |

Usage is **live**, not the snapshot, so an admin sees whether the person is
still stuck. The snapshot (§7.1) is shown on the detail view as "when they
asked".

### 6.3 Deciding

Clicking a row opens a detail panel with everything in one place:

- Requester: name, email, roles, tier and how it resolved (`matchedBy`).
- Usage: this period's spend vs limit (live), last three periods' spend, and
  a link to the existing per-user cost drill-down (`/admin/users/:id`, which
  already lists conversations content-free for `admin.costs` holders).
- The full justification and, if present, the funding source.
- History: prior requests and extensions for this user, with outcomes.

Three buttons, each a single confirmed action:

**Approve** opens a short form prefilled from the request and editable:

```
Amount          [$ 25.00 ] per month
Through         [ Nov 30, 2026 ]
Funding         (•) Centrally funded     ( ) Charge to a funding source
                    Fund [100000]  Org [4210]  Program [10]  Authorizer [fund-owner@example.edu]
Note to user    [ optional ]
                                                 [ Approve ]
```

- Choosing *Charge to a funding source* with the fields empty disables
  Approve and offers **Ask the user for a funding source** instead.
- The admin may fill or correct the funding fields themselves (a department
  admin often knows the cost center the user does not).
- Approve creates the extension, stamps the decision on the request,
  notifies the user, and writes an audit record.

**Ask for info** sends the request back to the user (`info_requested`) with a
required note. Used mainly for "which fund?".

**Deny** requires a note; the note goes to the user verbatim, so the form
says so.

An admin cannot decide their own request (§8.6).

### 6.4 Extending directly, and standing chargeback

Two places offer **Extend quota** without a request: the Extensions tab's
*New extension* button and the quota card on the admin user detail page
(beside the existing *Create override*). Both open the approve form above
with a user picker (search by email or name through the users directory, not
a raw id box) and a required reason. The override form's bare user-id text box
is the model of what *not* to do here.

The same form has a **Kind** switch: *One-time extension* (the default) or
*Standing chargeback*. Choosing standing changes three things:

```
Kind            ( ) One-time extension    (•) Standing chargeback
Funding source  Fund [100000]  Org [4210]  Program [10]  Authorizer [fund-owner@example.edu]      ← required
Monthly cap     [$ 200.00 ]   [ ] No cap
                ⚠ Without a cap one runaway conversation can bill this fund without limit.
Through         [ Jun 30, 2027 ]   (fiscal year end)
Note to user    [ optional ]
                                                 [ Create standing chargeback ]
```

The confirmation names the user, the fund and the cap in one sentence,
because this is the action that commits someone else's money. The user is
notified and the Usage page shows the terms immediately. A deployment that
does not collect funding sources (`fundingFields` empty) sees the switch
disabled with a link to Settings.

The admin user detail's quota card then reads *Standing chargeback: Fund
100000, $42.10 charged this month of $200 cap, through Jun 30* beside the
tier, so support can answer "why wasn't this person blocked?" at a glance.

### 6.5 Extensions tab

Active and recent extensions: user, kind (one-time or standing), amount or
cap, period, through, funding mode, source label, consumed so far this
period, created by, from request or direct. A *Standing* filter lists every
arrangement a deployment carries, which is the list a finance office asks
for at year end. **Revoke** ends an extension now (`validUntil = now`) with a reason;
the user is notified. Extensions are never deleted; a revoked one stays in
the report for the periods it covered.

### 6.6 Chargeback report

Requests tab → *Chargeback* → pick a period (default last complete month):

| Funding source | Extension | User | Granted | Consumed | Mode | Approved by | Approved |
|---|---|---|---|---|---|---|---|
| Fund 100000 · Org 4210 · Program 10 | ext_… | rokafor@example.edu | $25.00 | $18.62 | chargeback | admin@example.edu | Oct 30 |
| *Central budget* | ext_… | lvance@example.edu | $10.00 | $10.00 | central | admin@example.edu | Oct 12 |

Grouped by funding source with subtotals; central rows grouped under
*Central budget* so the admin also sees what central money the extension
lane cost, which is the number that tells them whether a tier limit is set
too low. **Export CSV** flattens the funding fields into columns using the
schema's keys, which is what a finance analyst pastes into a journal upload.

Above the table: a period range (one month, a quarter, the fiscal year), a
fund picker, a grouping-field picker when the policy names one (*By org*),
and the mode filter. Switching the grouping collapses the table to one row
per group with a drill-down. The current month is labelled **Provisional**;
closed months show their close date and a *Recalculate* action for finance.
The maths is in §9, the indexes behind the filters in §7.3.

### 6.7 Settings

A single form for the policy in §4.4. The funding-field schema editor is a
small table (key, label, required, pattern, help) with add/remove rows and a
live preview of the user's funding section. Saving writes one item; there is
no versioning of the schema in v1 (an extension stores the field values it was
created with, so an old row stays readable after the schema changes).

---

## 7. Data model

All items live in the existing `user-quotas` table. No new table, no new GSI.

### 7.1 `QuotaRequest`

```
PK = QUOTA_REQUEST#{requestId}      SK = METADATA
GSI1PK = QUOTA_REQUEST#STATUS#{status}   GSI1SK = {createdAt}#{requestId}   (queue by status, oldest first)
GSI2PK = QUOTA_REQUEST#USER#{userId}     GSI2SK = {createdAt}#{requestId}   (a user's own requests)
```

| Field | Type | Notes |
|---|---|---|
| `requestId` | str | uuid |
| `userId`, `userEmail` | str | email denormalised for display and search; never the key |
| `status` | `pending` · `info_requested` · `approved` · `denied` · `withdrawn` | §7.6 |
| `kind` | `one_time` · `standing` | standing requires `fundingSource` (§4.4) |
| `requestedAmount` | Decimal > 0 | per period; for `standing`, the requested cap |
| `requestedUntil` | ISO date | inclusive |
| `justification` | str | `justificationMinChars`..2000 |
| `fundingSource` | object? | §4.3 shape, validated against the schema in force at submit |
| `context` | object | **numbers only**: `{tierId, tierName, periodType, period, baseLimit, currentUsage, percentageUsed, blocked}` — what the user saw when they asked |
| `adminNote` | str? | the note sent with `info_requested` |
| `decision` | object? | `{decidedBy, decidedAt, outcome, note?, amount?, validUntil?, fundingMode?, extensionId?}` |
| `createdAt`, `updatedAt` | ISO | |

The existing `GSI1` is keyed `ASSIGNMENT_TYPE#…` and `GSI2` `USER#…` for
assignments; the new prefixes cannot collide with either exact-match query.

### 7.2 `QuotaExtension`

```
PK = QUOTA_EXT#{extensionId}            SK = METADATA
GSI2PK = QUOTA_EXT#USER#{userId}        GSI2SK = VALID_UNTIL#{validUntil}   (active-for-user query, like GSI4 for overrides)
GSI1PK = QUOTA_EXT#FUND#{fundingKey}    GSI1SK = VALID_UNTIL#{validUntil}   (every extension ever charged to one fund; chargeback only)
```

| Field | Type | Notes |
|---|---|---|
| `extensionId` | str | uuid |
| `userId`, `userEmail` | str | |
| `standing` | bool | §4.4; a standing extension must be `chargeback` |
| `amount` | Decimal > 0, or null | per period; null means **uncapped** and is only legal on a standing extension |
| `validFrom`, `validUntil` | ISO datetime | inclusive; `validFrom` defaults to now |
| `fundingMode` | `central` · `chargeback` | |
| `fundingSource` | object? | required when `chargeback` |
| `fundingKey` | str? | canonical form of `fundingSource.fields` (§7.3); indexed |
| `requestId` | str? | null for a direct extension |
| `reason` | str | the admin's reason or the approval note |
| `baseLimitSnapshot` | Decimal | the base limit at creation, for the report (§9.2) |
| `createdBy`, `createdAt` | | |
| `enabled` | bool | |
| `revokedAt`, `revokedBy`, `revokeReason` | ? | revoke sets `validUntil = now` **and** these, and re-derives `GSI2SK` (the stale-GSI trap that bit overrides) |

**Period pointers.** The report for period P must find every extension that
overlapped P without scanning. A multi-period extension therefore writes one
small **pointer item** per overlapped period
(`PK=QUOTA_EXT_PERIOD#{YYYY-MM}`, `SK={userId}#{extensionId}`, no payload
beyond the ids) in the same transaction as the extension. `maxDurationDays`
bounds the fan-out for one-time extensions (92 days ⇒ at most 4 rows); a
standing extension through a fiscal year end writes at most 12. Revoking
leaves the pointers; the close step reads the extension and clips to the
revoke date.

### 7.3 Funding key and the charge ledger

**`fundingKey`.** Reporting needs one string that means "this fund" no matter
how an admin typed it. It is derived, never entered: the schema's fields in
schema order, values trimmed, joined as `key=value` with `|`
(`fund=100000|org=4210|program=10`). Fields the schema marks `required:
false` and left blank are omitted. It is stamped on every chargeback
extension and request at write time and recomputed if the admin edits the
fields during approval. A schema change that renames a key does not rewrite
history; old keys remain queryable as they were written.

**`QuotaChargeLine`.** The numbers in §9.1 are derived from spend
aggregates, which is right for the open month and wrong for finance: a
closed month's charges must not move because a tier changed in December or
a straggler cost row arrived. So each closed period is **materialised** as
one line per `(period, extension)`:

```
PK = QUOTA_CHARGE#{YYYY-MM}                         SK = {fundingKey or "central"}#{userId}#{extensionId}
GSI1PK = QUOTA_CHARGE#FUND#{fundingKey}             GSI1SK = {YYYY-MM}#{userId}        (one fund across periods)
GSI2PK = QUOTA_CHARGE#USER#{userId}                 GSI2SK = {YYYY-MM}#{extensionId}   (one user's charged history)
GSI3PK = QUOTA_CHARGE#AUTH#{authorizerEmail}        GSI3SK = {YYYY-MM}#{userId}        (what one fund owner will see billed)
GSI6PK = QUOTA_CHARGE#GROUP#{groupValue}            GSI6SK = {YYYY-MM}#{fundingKey}    (the deployment's grouping field, §7.6 below)
```

| Field | Notes |
|---|---|
| `period`, `userId`, `userEmail`, `extensionId`, `requestId?` | identity |
| `fundingMode`, `fundingKey?`, `fundingLabel?`, `authorizerEmail?` | from the extension |
| `fields` | the funding fields **flattened as top-level attributes** under a `f_` prefix (`f_fund`, `f_org`, `f_program`) so a `FilterExpression` inside one period's partition can slice by any field without an index |
| `standing`, `granted` (null = uncapped), `consumed`, `baseLimitSnapshot`, `periodSpend` | the §9.1 result and its inputs |
| `approvedBy`, `approvedAt`, `revokedAt?` | provenance |
| `closedAt`, `closeVersion` | when and which close wrote it |

The partition's sort key begins with the funding key, so one period's report
is a single query that comes back already grouped by fund, and a
`begins_with` on the sort key narrows it to one fund with no index at all.
The four GSIs answer the cross-period questions: a fund over a fiscal year,
a user's history, an authorizer's statement, and a department roll-up.

**Grouping field.** Institutions slice by different things: a university by
org (department), a company by cost center, a lab by grant. Rather than an
index per field, the policy names one field as `reportGroupField` (for
example `org`), and the close step copies its value into `GSI6PK`. Changing
the grouping field only affects periods closed after the change; re-closing
an old period re-keys it (§9.4).

All of these reuse prefixes on indexes the table already has. `GSI1`
(`ASSIGNMENT_TYPE#`), `GSI2` (`USER#`), `GSI3` (`ROLE#`) and `GSI6`
(`APP_ROLE#`) are each queried today only by exact-match partition key with
one fixed prefix, so the `QUOTA_CHARGE#…` and `QUOTA_EXT#…` families cannot
collide with them. Zero new indexes, so no one-GSI-per-deploy ordering
constraint. Charge lines have no TTL: they are finance records.

**Why flattened fields instead of an index per field.** Any one period holds
at most a few hundred lines, so a filter inside that partition is one cheap
query. Cross-period slicing by an arbitrary field is the only case that
would want more indexes, and the grouping field covers the one such slice a
deployment actually reports on. A second one can become a Phase 2 index once
a deployment names it.

### 7.4 Resolution output

`ResolvedQuota` gains:

```python
extensions: List[QuotaExtension] = []    # active now, ordered by validUntil asc, createdAt asc
base_limit: Optional[Decimal]            # tier or override limit
effective_limit: Optional[Decimal]       # base_limit + Σ extensions, None when unlimited
```

`QuotaCheckResult.quota_limit` becomes the effective limit (None for an
uncapped standing extension, which the SPA renders as the standing terms,
not as "unlimited"), which is what the SSE events and the SPA already
consume. `UserQuotaStatusResponse` gains `baseLimit`, `effectiveLimit`,
`extensions: [{amount, validUntil, fundingMode, standing, fundingLabel?}]`,
`charged: {label, thisPeriod, cap?} | null` (the funding-notice numbers, so
the Usage page can draw the charged band without a turn), and
`openRequest: {requestId, status, requestedAmount, createdAt} | null`.

### 7.5 `QuotaRequestPolicy`

```
PK = QUOTA_POLICY#REQUESTS   SK = METADATA
```

The fields in §4.4 plus `updatedBy`, `updatedAt`. Read through the existing
config cache (`CONFIG_CACHE_ENABLED` TTL), since every request-form open and
every submit validates against it. A missing item means defaults.

### 7.6 Request states

```
                 ┌──────────────── withdraw ────────────────┐
                 ▼                                          │
  submit ──► pending ──── approve ──► approved        info_requested
               │  ▲                                     ▲    │
               │  └──── user adds funding source ───────┘    │
               ├──── ask for info ───────────────────────────┘
               └──── deny ──► denied

  approved ──► (extension created; later revoke acts on the extension, the request stays "approved")
```

Terminal: `approved`, `denied`, `withdrawn`. A user may hold **one** request
in `pending` or `info_requested` at a time (`POST` returns 409 with the open
request's id); decided requests do not block a new one.

---

## 8. Resolution and enforcement

### 8.1 Where extensions enter

`QuotaResolver._resolve_from_db` already starts with
`get_active_override(user_id)`. Extensions are fetched **concurrently** with
that call (`asyncio.gather`), through the same `GSI2SK >= VALID_UNTIL#{now}`
shape overrides use on GSI4, and attached to whichever `ResolvedQuota` the
ladder returns. One extra DynamoDB query per **cache fill** (every 5 minutes
per user), zero per turn. Nothing new on the turn path's critical section.

### 8.2 Checker arithmetic

```python
base_limit = daily or monthly limit, as today
if any(e.amount is None for e in resolved.extensions):   # uncapped standing
    limit = None                                          # never block, keep measuring
else:
    limit = base_limit + sum(e.amount for e in resolved.extensions)
```

Everything downstream (`percentage_used`, `remaining`, the ladder, the
session-notice share, the blocked decision) is unchanged and runs on `limit`.
An `unlimited` base short-circuits before extensions, as it does today. An
**uncapped standing extension is not the same short-circuit**: the checker
still reads the period spend, still raises the per-session notice (which is
scaled to `base_limit` when there is no cap), and emits the funding notice
below. It just never blocks.

**Funding notice.** When `current_usage > base_limit` and the spend above it
falls on a `chargeback` extension, the check result carries
`funding_notice = {label, chargedThisPeriod, cap?, remainingUnderCap?}` and
the route streams it as a `quota_funding_notice` event at the head of the
stream, beside `quota_warning`, re-emitted every turn while in the band
(dismissal is client-side per period, the `quota_warning` contract). The
SPA renders it in the composer tab: *"Included quota used · charging Fund
100000 · $42.10 this month of $200"*. It never reaches the prompt. The
`CLAUDE.md` SSE table gains a row for it.

### 8.3 Fresh-on-block

The resolver caches per user for 5 minutes, and the Runtime's cache cannot be
invalidated from app-api. For warnings that staleness is harmless. For a
**blocked** user it is the whole experience: they get approved, try again, and
are blocked for up to five more minutes.

Rule: **when a check would block, re-resolve once, bypassing the cache, and
decide on the fresh result.** A blocked turn is about to be refused anyway, so
the extra ~2 DynamoDB reads cost nothing anyone is waiting on, and every other
turn keeps the cached path. Approval therefore takes effect on the user's very
next send. (This also fixes the same latency for overrides, which have had it
since they shipped.)

### 8.4 Cost and prompt-cache position

- Nothing in this spec reaches the system prompt, `toolConfig`, or history.
  The prompt-cache prefix is untouched.
- The turn path gains no synchronous work: one concurrent read per cache
  fill, and the fresh-on-block read only on the path that ends in a refusal.
- No new EMF per turn. Request and decision counts are emitted from app-api
  (§11.3).

### 8.5 Composition with cooldown windows

If the pacing model in `quota-cooldown-windows.md` lands, an extension raises
the **period backstop** only. A deployment that also wants a bigger burst
window for a user would add an optional `windowAmount` to the extension; the
request form would not expose it. The two designs share nothing else.

### 8.6 Guards

- **No self-approval.** The approve/deny/request-info routes refuse when
  `request.userId == admin.user_id`, record an audit row with
  `outcome=denied`, and return 403. The same check applies to a direct
  extension on oneself.
- **Scope.** A new delegable scope `admin.quota_requests` ("Quota Requests:
  decide quota requests and manage extensions") guards the new router
  package `apis/app_api/admin/quota_requests/`, so a department approver can
  be given the queue without the tier editor. `admin.quota` holders get the
  nav item too (the role form can grant both). `system_admin` holds every
  scope implicitly, as always. The package boundary is the permission
  boundary (`tests/architecture/test_admin_scope_coverage.py`).
- **Amount bounds.** The request route enforces `maxRequestAmount` and
  `maxDurationDays`; the admin routes enforce only `> 0` and a sane ceiling
  (`$10,000`) so a fat-fingered `25000` is caught.
- **Funding validation.** A funding source is validated against the field
  schema (required, pattern) at submit and at approve; unknown keys are
  rejected, so the report's CSV columns stay stable.
- **Content.** The request stores no conversation text, and the admin views
  read no conversation text; the user's cost drill-down link already applies
  the content policy.

---

## 9. Chargeback accounting

### 9.1 The waterfall

For one user and one period P:

```
spend_P        = the user's aggregated spend in P (existing per-period summary)
central_P      = min(spend_P, base_P)
over_P         = max(0, spend_P − base_P)
for each extension e active in P, in consumption order:
    consumed_e_P = over_P if e.amount is None else min(e.amount, over_P)
    over_P      -= consumed_e_P
unattributed_P = over_P        # > 0 only if the user exceeded the effective limit (action_on_limit: "warn" tiers)
```

**Consumption order:** centrally funded extensions first, then capped
chargeback extensions, then standing extensions; within each group by
`validUntil asc, createdAt asc`. Money the institution already committed is
spent before any fund is touched, short grants before long ones, and an
uncapped arrangement absorbs whatever is left. "Consumed" is **actual**
spend, never the granted amount, so a fund is only charged for what was
used; "granted" (or *uncapped*) is reported beside it as the commitment. The
ordering is deterministic and printed in the report's footer so two people
reading it agree.

### 9.2 Which base limit

`base_P` is the extension's `baseLimitSnapshot` when every extension in P
carries the same snapshot, which is the normal case. If an admin changed the
tier mid-period (snapshots differ), the report uses the **largest** snapshot,
which charges funds the least, and marks the row *base limit changed during
period*. Finance resolves the rare ambiguity; the report never silently
over-charges a fund.

### 9.3 Reading the report

Two paths, one shape:

- **Open period (the current month):** reads the period's pointer items
  (§7.2), loads each extension, loads each user's period summary once, runs
  §9.1 and returns the lines marked **provisional**. A few hundred
  extensions is a few hundred reads, well within an interactive request.
- **Closed period:** one query on `QUOTA_CHARGE#{period}` returns the
  materialised lines (§7.3), already in fund order. Filters are query
  parameters (§10.2): `fundingKey` narrows by sort-key prefix, `group`,
  `userId` and `authorizer` use their indexes, `from`/`to` iterate periods,
  and any `f_<field>=value` applies a filter expression inside the
  partition.

### 9.4 Closing a period

A period closes **lazily on its first read after a grace window** (period
end + 48 hours, so cost rows written in the last minutes of the month have
landed), by the same code path that computes the open view: compute §9.1,
write the lines in a batch, and record `closedAt`. The close is idempotent
and conditional on no existing `closeVersion`, so two admins opening the
report together produce one close. No scheduler is needed; a period nobody
reads is closed the day someone does, with the same numbers.

**Re-close** is an explicit admin action (*Recalculate this period*) for the
rare case finance needs: a cost correction, a revoke that should have been
back-dated, a changed grouping field. It bumps `closeVersion`, rewrites the
lines, and writes an audit record (`quota_period.reclosed`) carrying the
per-fund totals before and after, so a changed statement is explainable. The
open month is never "closed"; it is always computed.

### 9.5 What the report is not

It is not an invoice and it does not move money. It is the evidence a finance
process needs: who, how much was committed, how much was used, against which
coded source, approved by whom and when. The CSV columns are
`period, userEmail, extensionId, requestId, fundingMode, <one column per
schema key>, authorizerEmail, granted, consumed, approvedBy, approvedAt,
revokedAt`.

---

## 10. API

All routes on app-api. User routes use `get_current_user_from_session`; admin
routes use `require_admin_scope("admin.quota_requests")`. Everything 404s
while `QUOTA_REQUESTS_ENABLED` is off, **after** authentication (flag-gated
routes authenticate first).

### 10.1 User

| Route | Purpose |
|---|---|
| `GET /quota/policy` | The user-facing subset of the policy: presets, custom allowed, max, default through, funding mode and fields. Drives the dialog |
| `GET /quota/requests` | The caller's requests, newest first |
| `POST /quota/requests` | Submit. 409 `{openRequestId}` when one is open. Validates amount, duration, justification, funding fields |
| `PATCH /quota/requests/{id}` | `{fundingSource}` while `info_requested` (returns to `pending`); `{withdraw: true}` while open |
| `GET /costs/quota-status` | Extended with `baseLimit`, `effectiveLimit`, `extensions`, `openRequest` (§7.3) |

### 10.2 Admin

| Route | Purpose |
|---|---|
| `GET /admin/quota-requests?status=&cursor=&limit=` | The queue |
| `GET /admin/quota-requests/summary` | `{pending, infoRequested}` for the badge |
| `GET /admin/quota-requests/{id}` | Request + live user context (tier, usage, last 3 periods, prior requests, extensions) |
| `POST /admin/quota-requests/{id}/approve` | `{amount, validUntil, fundingMode, fundingSource?, note?}` → creates the extension |
| `POST /admin/quota-requests/{id}/request-info` | `{note}` |
| `POST /admin/quota-requests/{id}/deny` | `{note}` |
| `GET /admin/quota-extensions?userId=&active=` | List |
| `POST /admin/quota-extensions` | Direct extension `{userId, standing, amount (null = uncapped, standing only), validFrom?, validUntil, fundingMode, fundingSource?, reason, note?}` |
| `POST /admin/quota-extensions/{id}/revoke` | `{reason}` |
| `GET /admin/quota-requests/chargeback?period=YYYY-MM` or `from=&to=`, plus `fundingKey=`, `group=`, `userId=`, `authorizer=`, `mode=`, `f_<field>=`, `format=json\|csv` | §9.3; closed periods come from the ledger, the open one is computed and marked provisional |
| `POST /admin/quota-requests/chargeback/{period}/close` | Re-close a period (§9.4); audited |
| `GET /admin/quota-requests/chargeback/funds` | Distinct funding keys with label, authorizer, first/last period and lifetime consumed, for the report's fund picker |
| `GET` / `PUT /admin/quota-requests/policy` | §4.4 |

The existing `UserQuotaInfo` (admin inspector) and the admin user detail's
quota card gain `extensions` and `effectiveLimit` (§7.4), so the inspector explains
a number that no longer matches the tier.

### 10.3 Frontend models

`QuotaStatus` gains the fields above; new `QuotaRequest`, `QuotaExtension`,
`QuotaRequestPolicy`, `FundingSource` interfaces mirror §7. The Usage page,
the composer tab and the dialog share `QuotaStatusService`, which is reloaded
after submit, withdraw and on an inbox notification of a decision.

---

## 11. Notifications, audit, metrics

### 11.1 In-app notifications

New `NotificationKind` values in `apis/shared/notifications`:

| Kind | Recipient | Payload |
|---|---|---|
| `quota_request_submitted` | each `policy.notifyEmails` | `{requestId, userEmail, amount, through}` |
| `quota_request_decided` | requester | `{requestId, outcome, amount?, through?, note?}` |
| `quota_request_info_requested` | requester | `{requestId, note}` |
| `quota_extension_granted` | user (direct extension) | `{extensionId, standing, amount?, fundingLabel?, through, note?}` — for a standing one the copy spells out the fund and the cap |
| `quota_extension_revoked` | user | `{extensionId, reason}` |

The bell is shown today only with `features.projects`. This spec changes that
gate to `features.projects || features.quotaRequests`; the inbox routes were
never behind the projects flag. The quota surfaces themselves show every
state, so the inbox is a second channel, not the only one.

### 11.2 Audit

New actions in the closed registry, target `quota_request` / `quota_extension`
keyed by id:

`quota_request.approved`, `quota_request.denied`,
`quota_request.info_requested`, `quota_extension.created`,
`quota_extension.revoked`, `quota_period.reclosed` (target `quota_period`,
with per-fund totals before and after), and `quota_request.mutation_denied`
for a refused self-approval. User submissions are not admin mutations; the request row is
their record.

### 11.3 Metrics

From app-api, in a new EMF namespace `AgentCoreStack/Quota` (beside the
existing `Compaction`, `PromptCache` and `TurnLatency`): `QuotaRequestSubmitted`,
`QuotaRequestDecided` (dimension `outcome`), `QuotaExtensionCreated`
(dimension `fundingMode`), `QuotaExtensionAmountUsd`. A dashboard widget with
pending age (p50/max) is a follow-on once there is data.

---

## 12. Feature flag, scope, configuration

| Side | Switch | Default |
|---|---|---|
| Backend | `QUOTA_REQUESTS_ENABLED` (`CDK_QUOTA_REQUESTS_ENABLED`), **app-api only** | Off; only `"true"` enables |
| Frontend | `features.quotaRequests` | `false` in all three environment files until a deployment turns the backend on |

- The Runtime gets **no** environment variable. Resolution reads extensions
  wherever it runs, without a switch, exactly as direct user grants do: while
  the flag is off nothing *creates* extensions, and turning it off never
  revokes one. This spends none of the Runtime's V2 environment budget and
  keeps the checker's behaviour independent of a flag it cannot see.
- The blocked message's neutral last line (§5.1) is unconditional for the
  same reason.
- Kind once finished: a **feature switch**, permanent. A deployment may
  reasonably not want users to be able to ask (it is a product and budget
  choice), so it does not retire.
- New delegable admin scope `admin.quota_requests` (§8.6); docs-site rows for
  the flag and the scope; CDK config and test; `platform.yml` variable.

---

## 13. Decisions

1. **Extensions are additive, not replacing.** The tier limit stays the
   central allowance so over-tier spend is attributable. *Rejected:*
   generalising `QuotaOverride` with a `fundingSource` field. A replacement
   limit has no boundary to bill against, and an `unlimited` override cannot
   be charged to anything.
2. **Overrides stay untouched.** They answer a different question
   ("unlimited for this person"); forcing them through the request flow adds
   nothing. The admin user page offers both, labelled.
3. **Per-period amounts, not a rolling lump sum.** `amount` applies in each
   period the extension overlaps. The write path already maintains per-period
   aggregates, so consumption derives at read time with no new write on the
   turn path, and finance reconciles monthly anyway. *Rejected:* a `$X` pool
   consumed across months, which needs a persisted running total updated per
   turn and an answer to "which month's statement does the carry-over land
   on". A `rollover` flag can be added later without changing the model.
4. **Funding source is a deployment-defined schema, not fields we invent.**
   Cost center, FOAPAL, grant number and PO are the same thing with different
   shapes. Validation (required, regex) is the only behaviour the stack owns.
   *Rejected:* a hard-coded `costCenter` string; it would be wrong for the
   first university and the first company.
5. **The admin picks the funding mode per extension; policy sets defaults.**
   This is the user's stated requirement and the right generality: the same
   deployment grants a $10 pilot centrally and a $500 research run against a
   grant.
6. **Consumed is actual spend, granted is the commitment; both are reported.**
   Charging the granted amount would bill a fund for capacity never used.
7. **Fresh-on-block.** Bypass the resolver cache only on the path that would
   refuse the turn. *Rejected:* a cross-process invalidation channel (a
   version item read per turn), which adds a read to every turn to fix a
   five-minute delay felt only by blocked users.
8. **One open request per user.** Keeps the queue honest and the user's state
   unambiguous. Decided requests never block a new one.
9. **A separate admin scope.** Approving requests is the one admin task most
   likely to be delegated to someone who should not edit tiers.
10. **No new table or GSI.** Requests, extensions and charge lines reuse
    `GSI1`/`GSI2`/`GSI3`/`GSI6` with distinct key prefixes, and the
    per-period pointer item replaces a period index. Keeps the platform-stack
    resource budget flat and avoids the one-GSI-per-deploy ordering trap.
11. **In-app only.** The repo has no email path; building one for this feature
    alone is not justified. The surfaces show state without the inbox.
12. **Zero prompt or turn-path cost.** Nothing here touches the model call or
    the cacheable prefix; the only enforcement change is two additions in the
    checker and one concurrent read per cache fill.
13. **Standing chargeback is an extension, not a user attribute.** A flag on
    the user record would need its own dates, funding source, cap, revoke and
    report path, duplicating all of §7.2. As a `standing` extension it gets
    resolution, the waterfall, the report, revoke and audit for free, and the
    Extensions tab lists it beside everything else. *Rejected:* a per-user
    `chargebackEnabled` boolean.
14. **Standing arrangements are capped by default and loud when crossed.**
    The cap is pre-filled and *No cap* is an explicit tick with a warning; the
    funding notice fires the turn a user starts spending a fund. *Rejected:*
    silent pass-through once the central allowance is used, which makes the
    first sign of a runaway conversation the fund owner's statement.
15. **Consumption order is central → capped chargeback → standing.** The
    institution's committed money goes first and an uncapped fund last;
    pure expiry order would bill a grant before a central bump that happened
    to expire later.
16. **Closed periods are materialised; the open one is computed.** Finance
    needs a number that does not move after the statement goes out, and the
    read-time derivation moves whenever a tier or a late cost row does. The
    ledger line is written on first read after a grace window and only
    rewritten by an explicit, audited re-close. *Rejected:* a nightly job
    (new infrastructure for something a lazy close does on demand) and
    read-time-only reporting (no stable statement, and every filter is a
    full recomputation).
17. **One canonical funding key, flattened fields, one grouping index.**
    The key makes "this fund" queryable however it was typed; the flattened
    `f_` attributes make any field filterable within a period; the single
    policy-chosen grouping field gets the one cross-period index a
    deployment reports on. *Rejected:* an index per funding field (schemas
    differ per deployment, and GSIs are a per-deploy ordering hazard) and
    storing the fields only as a nested map (unfilterable without a scan).

---

## 14. Delivery plan

Each PR targets `develop`, defaults off, and is independently mergeable.

| PR | Scope | Done when |
|---|---|---|
| **PR-1 backend core** | Models, repository (requests, extensions, pointers, policy), resolver + checker stacking, fresh-on-block, user routes, admin router package + scope (`quota_requests/routes.py` added to `EXPECTED_MODULE_SCOPES` in `tests/architecture/test_admin_scope_coverage.py`), audit actions, notification kinds, flag, CDK var + test, docs-site rows (the flag table, and the `admin/quotas.md` page, which is still a scaffold) | A request can be submitted and approved with `curl`; a blocked user's next turn succeeds; `quota-status` shows the split; route tests + architecture tests green |
| **PR-2 SPA user side** | `features.quotaRequests`; dialog; the three entry points; Usage page split bar + *Your requests*; `info_requested` flow; bell gate change; notification rendering | Browser-verified on a local stack with `SKIP_AUTH` and a seeded tier |
| **PR-3 SPA admin side** | Requests tab with badge, detail panel, approve/info/deny; Extensions tab with revoke and the *Standing* filter; direct *Extend quota* on the user detail page with a user picker and the one-time / standing switch; policy settings form with schema editor and the standing fields | Browser-verified against PR-1 on `:8010`; no raw-id inputs; the no-cap warning and the standing confirmation sentence are present |
| **PR-4 chargeback** | `fundingKey` stamping (PR-1 writes the attribute; PR-4 adds the index use), charge-ledger model, lazy close + re-close, report route with filters and CSV, funds listing, Chargeback view with range, fund and grouping pickers, provisional/closed labelling, `baseLimitSnapshot` ambiguity marker | A seeded month with central, capped chargeback and standing extensions reproduces §9.1 by hand; closing twice writes once; a re-close after a revoke changes exactly the affected fund's total and leaves an audit row |
| **Phase 2 (separate specs)** | Fiscal-authorizer confirmation; funding-source catalogue/allowlist; auto-approve rules (for example ≤ `$10` once per period); approver routing by funding source or department; `rollover` extensions; `windowAmount` once cooldown windows exist; dashboard widget | |

Turn on in the `development` GitHub environment after PR-3; production after
one release cycle with the report (PR-4) in hand.

---

## 15. Open questions

1. **Cooldown after a denial?** v1 allows an immediate resubmission (the admin
   sees history). If repeat requests become noise, add `resubmitAfterDays`
   to the policy.
2. **Stale pending requests.** Should a request auto-expire (say 30 days) with
   a notification, or stay until decided? Leaning: stay, and surface age in
   the queue, until a deployment reports a problem.
3. **Policy visibility to users.** Should the dialog show *"requests over $50
   usually need a funding source"*? Cheap once `fundingSourceMode` has a
   threshold variant; not in v1.
4. **Admin notification fan-out.** `notifyEmails` is explicit. Deriving the
   list from "everyone holding `admin.quota_requests`" needs a reverse lookup
   roles → users that does not exist yet; revisit with the scope's PR-3 holder
   counts.
5. **Daily tiers.** An extension on a daily tier adds `amount` per day. Rare;
   the copy handles it, but confirm nobody wants "per month on a daily tier".
6. **Should the user acknowledge a standing arrangement?** v1 notifies and
   displays the terms; it does not require a click. If an institution's
   policy says the person whose fund is charged must consent in-app, add an
   `acknowledgedAt` on the extension and hold the funding notice's wording
   until it is set. The fiscal-authorizer confirmation in Phase 2 is the
   stronger version of the same idea.
7. **Standing arrangements for a group.** "Everyone in this department
   charges to this cost center after their allowance" is a tier-level or
   AppRole-level standing extension. The model supports it (an extension
   keyed by role instead of user, resolved beside the user's), but it needs
   its own report grouping; defer until a deployment asks.

---

## 16. Test plan (highlights)

- **Checker:** effective limit sums active extensions; an expired or revoked
  extension is excluded; unlimited base ignores extensions; ladder rungs and
  session notice compute on the effective limit; fresh-on-block re-resolves
  exactly once and only when the cached result blocks. Standing: an uncapped
  extension never blocks but still reads spend and raises the session notice
  against the base limit; the funding notice appears exactly when spend
  crosses the base limit onto a `chargeback` extension and not when the
  overage lands on a `central` one; a capped standing extension blocks at
  `base + cap`.
- **Resolver:** extension query runs concurrently with the override query;
  ordering is `validUntil asc, createdAt asc`; a GSI2 item with the old
  `USER#` prefix is never read as an extension.
- **Repository:** revoke re-derives `GSI2SK`; multi-period extension writes
  one pointer per overlapped period and nothing beyond `maxDurationDays`.
- **Routes:** one-open-request 409; policy bounds; funding schema validation
  (required, pattern, unknown key); self-approval 403 with a denied audit
  row; flag off → 404 after auth; scope coverage architecture test lists the
  new package; import boundaries unchanged.
- **Chargeback:** §9.1 worked examples (under tier, over tier within one
  extension, spanning two extensions, over the effective limit on a `warn`
  tier, mid-period revoke, differing snapshots, a central extension beside a
  standing one with the central consumed first, an uncapped standing
  extension absorbing the remainder).
- **Routes:** `amount: null` refused unless `standing`; `standing` refused
  without a funding source or with `fundingMode: central`; `standingEnabled:
  false` refuses creation but existing standing extensions keep resolving.
- **Ledger:** `fundingKey` is identical for the same fields typed with
  different whitespace or case in labels, and differs when any value
  differs; a close before the grace window returns provisional lines and
  writes nothing; two concurrent closes write one set; a re-close bumps
  `closeVersion` and audits per-fund before/after; each GSI family answers
  its question without touching another prefix's items (a `USER#…`
  assignment row is never returned by a `QUOTA_CHARGE#USER#…` query, and the
  reverse); the CSV carries one `f_` column per schema key, blank where a
  line predates the key.
- **SPA:** dialog honours policy (presets, custom, max, through cap, funding
  mode); entry points swap to request state; split bar; `info_requested`
  round trip; admin approve form disables Approve when chargeback has empty
  fields; API timestamps are formatted through `parseIso`
  (`shared/utils/iso-date.ts`), never the bare `date` pipe, which throws
  `NG02100` on the legacy `+00:00Z` spelling and blanks the rest of the view
  (the override cards did exactly this in prod).
- **Smoke:** the L1 turn matrix gains a row: blocked user → approve → next
  turn streams.
