# Quota Cooldown Windows + Platform Ceiling — pacing instead of a monthly cliff

> Status: DRAFT (spec only, nothing built). Revised 2026-07-10 to add the
> **universal platform ceiling** as the fiscal-control layer (superseding the
> earlier statistical-overbooking policy). Replaces the current all-or-nothing
> enforcement UX — warn at a percent, hard-block until the 1st of next month —
> with a three-layer model: **anchored cooldown windows** (Claude-style
> "resets at 2:30 PM") pace individuals; a **hard, adjustable, platform-wide
> monthly ceiling** guarantees the institutional total; the per-user monthly
> limit is demoted to a generous **anti-runaway backstop**.

## 1. Summary

Three enforcement layers, each with one job, plus an observability surface:

1. **Anchored cooldown window (pacing).** The platform defines one window
   length — admin-adjustable via the platform-budget setting (`windowHours`,
   default 5, env `QUOTA_WINDOW_HOURS` as fallback default); each tier
   optionally gets a `window_cost_limit` (recommended default $2.00). A
   user's window anchors at
   their first turn; spend accumulates; exhausting it blocks until
   `windowStart + QUOTA_WINDOW_HOURS` — an **exact, user-visible reset
   timestamp**, hours away instead of weeks. Null `window_cost_limit` = no
   window for that tier; per-tier opt-in is the rollout gate, no new global
   flag. (Why the window *length* is platform-wide, not per-tier: §4.5.)
2. **Platform ceiling (fiscal guarantee).** One admin-set number — target
   average cost per user × user count (e.g. $5 × 5,000 = $25,000/month) —
   **enforced**, not aspirational. Fleet MTD spend is compared against an
   enforcement line set below the stated ceiling by a reserve margin (default
   10%), with admin alert events at 80%/90%. Because enforcement is computed
   per turn against live config (nothing is ever "marked blocked"), raising
   the ceiling mid-month takes effect within the config cache TTL (~1 min) —
   no deploy, no reset. Tiers/overrides can be explicitly **ceiling-exempt**
   (spend still counted and reported; only enforcement is bypassed).
3. **Per-user monthly backstop ($30).** The existing `monthly_cost_limit`,
   re-purposed: it marks **where the month's premium-model subsidy ends**, not
   a guessed budget. Target behavior on reaching it is **degrade to an
   economy model** (PR-7), with block-plus-early-warning as the bridge until
   degrade lands; the ceiling protects the total (§12).

Plus: **fleet budget-health admin view** — MTD spend vs. ceiling, burn rate,
projected exhaustion date, per-user spend distribution — from the existing
`SystemCostRollup` table and `PeriodCostIndex` GSI. Read-only; no
auto-actuation in v1.

`action_on_limit: "degrade"` (continue on a cheap model instead of blocking)
is **PR-7 — scheduled, not optional**: with a burst-sized $2 window, honest
sustained use *can* reach the $30 backstop, so degrade is what keeps the
no-cliff promise there (decisions 14–15, §15). Until it lands, the backstop
blocks with early warning + the override lane as the documented bridge.
Extending degrade to ceiling behavior ("everyone rides the economy model near
the ceiling") stays roadmap.

## 2. Why an *anchored* window

Claude's own mechanic is not a rolling sum — it's a window that **starts at your
first message** and resets at a stated time. That shape is deliberately what we
copy, because it wins on all three axes that matter here:

- **Explainable:** "You've used your 5-hour allowance — it resets at 2:30 PM"
  beats both "your budget refills at $0.02/hour" (token bucket) and "your usage
  over the trailing 5 hours exceeds…" (rolling sum, where the reset time is
  unknowable in advance).
- **Cheap to store:** one small item per user, overwritten in place. A rolling
  sum needs time-bucketed items and a multi-item read per check; a token bucket
  is equally cheap but loses on explainability.
- **Cheap to enforce:** one extra comparison in `QuotaChecker`, one extra atomic
  `ADD` in the existing per-turn write path.

Don't derive the window budget by dividing the monthly budget into windows
($5 ÷ ~144 five-hour windows ≈ 3¢ — useless). The knobs solve different
problems: the **window** is sized to a genuinely heavy working session —
accounting for the fact that real users only have 2–3 usable windows in a
waking day (§12); the **backstop** marks where the month's premium subsidy
ends; the **ceiling** is sized to the institutional budget. The windows are also what make a shared
ceiling safe and fair: they bound every individual's burn rate, so no small
cohort can drain the pool quickly and the fleet trajectory stays smooth and
predictable.

## 3. Current state (verified 2026-07-10)

| Piece | Where | State |
|---|---|---|
| Tier model | `agents/main_agent/quota/models.py:26` (`QuotaTier`) | `monthly_cost_limit`, optional `daily_cost_limit`, `period_type: Literal["daily","monthly"]`, `soft_limit_percentage` (default 80), `action_on_limit: Literal["block","warn"]` |
| Check logic | `agents/main_agent/quota/checker.py:28` (`check_quota`) | Resolves tier → reads monthly/daily aggregate → warn at soft % (90% **hardcoded** at `checker.py:103`) → block at 100% if `action_on_limit=="block"`. No-tier fails **closed** (`:47`); cost-read error fails **open** (`:78`) |
| Usage store | `apis/shared/storage/dynamodb_storage.py:312` (`update_user_cost_summary`) | `user-cost-summary` table, `PK=USER#{id}` / `SK=PERIOD#{YYYY-MM}`, atomic `ADD`, `GSI2 PeriodCostIndex` (`PERIOD#` / `COST#{padded-cents}`). **No TTL configured on this table** (`cost-tracking-tables-construct.ts:86` — only `sessions-metadata` has `timeToLiveAttribute`, `:49`) |
| Usage write (per turn) | `apis/app_api/sessions/services/metadata.py:190` → `_update_cost_summary_async` (`:201`) | Derives `period` from message timestamp (`:293`), calls `update_user_cost_summary`. Also writes per-message `C#` records with 365-day TTL (`:151`) |
| Usage read | `apis/shared/costs/aggregator.py` (`CostAggregator.get_user_cost_summary`) | 30s in-process cache keyed `user+period` (`:20-27`) |
| Reset cadence | implicit | No reset job — the `PERIOD#` key rolls over at month boundary. Monthly cutoff lives in exactly two places: the storage key and `_get_current_period()` (`checker.py:188`) |
| Chat enforcement | `apis/inference_api/chat/routes.py:1116-1154` | Gated on `is_quota_enforcement_enabled()` + not resume/continuation; exceeded → **conversational assistant message** (`stop_reason="quota_exceeded"`, `build_quota_exceeded_event`), not an HTTP error. Warning injected first-thing into the SSE stream by `stream_with_quota_warning()` (`:1729`) |
| API-key enforcement | `apis/inference_api/chat/converse_routes.py:369-393` | Same `check_quota` but raises `HTTPException(429)`. No-tier fails **open** here (`:376`) — pre-existing inconsistency with the chat path. `Retry-After` convention already used at `:354` |
| SSE events | `apis/shared/quota.py` (`QuotaWarningEvent:102`, `QuotaExceededEvent:120`, builders `:144-270`) | `reset_info` is a human string ("Quota resets in N day(s)") computed as days-to-month-end |
| Events audit | `agents/main_agent/quota/event_recorder.py`; `QuotaEvent.event_type: Literal["warning","block","reset","override_applied"]` (`models.py:136`) | Warning dedup 60 min; `record_reset` exists but has no caller |
| Unlimited tier | `checker.py:59` | `monthly_cost_limit == inf or >= 999999` → skip all checks |
| Overrides | `QuotaOverride` (`models.py:206`) | Time-bounded custom/unlimited per-user override, resolved first — the standing mechanism for super users and temporary increases; kept and extended (§5) |
| Tier resolution | `agents/main_agent/quota/resolver.py:47` | Priority: override → direct user → app role → JWT role → email domain → default tier; 5-min TTL cache |
| Admin API/UI | `apis/app_api/admin/quota/` ; SPA `admin/quota-tiers/` (tier-detail, assignment-list, override-list, event-viewer, quota-inspector) | Full tier/assignment/override CRUD exists — new fields slot into existing forms |
| Fleet rollups | `system-cost-rollup` table (`cost-tracking-tables-construct.ts:116`), written by `_update_system_rollups_async` (`dynamodb_storage.py`) | Daily/monthly/per-model system-wide aggregates. **Best-effort async write today** — becomes an enforcement input under the ceiling, needs hardening (§8). Inference-runtime read wiring (env + IAM) for this table **needs verification** — may currently be app-api-only |
| SPA quota surfaces | `components/quota-warning-banner/` + `services/quota/quota-warning.service.ts`; SSE dispatch `stream-parser-core.ts:659` | Banner shows severity, `formattedUsage`, `resetInfo` string |
| Frontend models | `admin/quota-tiers/models/quota.models.ts` | `WarningLevel = 'none'\|'80%'\|'90%'` mirrors backend |

## 4. Key design decisions

1. **Anchored window, not rolling sum, not token bucket.** See §2. *Rejected:
   rolling sum over the per-message `C#` records — those are TTL'd and reading
   them is a per-check range scan; also no predictable reset time. Rejected:
   token bucket (continuous drip) — mathematically cleaner (no cliff, cooldown
   exactly proportional to overdraft) but unexplainable to a student audience
   and harder for admins to parameterize. Revisit if anchored windows prove too
   coarse.*
2. **Window state is one reusable item per user in the `user-cost-summary`
   table**: `PK=USER#{user_id}` / `SK=WINDOW#CURRENT`, attributes
   `{windowStart: ISO, windowCost: Decimal, updatedAt}`. Overwritten in place
   when a stale window is re-anchored — so no TTL needed (the table has none
   configured) and no unbounded growth. *Rejected: hourly TTL buckets modeled on
   `rate_limit.py` — right pattern for a rolling sum we aren't building, and
   would require enabling TTL on the table (CDK change). Rejected: a new table —
   both app-api (write) and the inference runtime (read via `CostAggregator`)
   already have IAM + env wiring for `user-cost-summary`; zero CDK changes this
   way.*
3. **The anchor is set by the write path, checked by the read path.** A turn's
   cost lands after the turn finishes (`_update_cost_summary_async`), so the
   window anchors at the first *completed* turn. The checker treats a missing or
   expired item as "fresh window, usage 0". Consequence, accepted: enforcement
   is check-before-turn against the balance at turn start — a single expensive
   turn can overdraw the window. The overdraft just makes the cooldown feel
   fuller; identical tolerance to today's monthly check.
4. **Concurrency via condition-expression two-step.** Live window: `UpdateItem`
   `ADD windowCost :cost` with condition `windowStart >= :cutoff`
   (`cutoff = now - QUOTA_WINDOW_HOURS`). On `ConditionalCheckFailed` (item
   missing or stale): `PutItem` a fresh `{windowStart: now, windowCost: cost}`
   with condition `attribute_not_exists(SK) OR windowStart < :cutoff`; if
   *that* races and fails, retry the `ADD` once. Same atomic-increment
   discipline as the monthly summary.
5. **Window length is platform-wide; only the dollar budget is per-tier.** The
   write path must stay tier-agnostic (resolving a quota tier inside the cost
   write would drag `QuotaResolver` into `metadata.py` and add a resolve per
   turn), so the writer re-anchors on a fixed horizon. If tiers could set their
   own `window_hours`, any tier window shorter than that horizon would get
   enforcement holes: the accumulator keeps growing past the tier's window
   while the checker already reads it as stale/zero, so the user is only ever
   enforced during the first `window_hours` of each horizon period. Rather than
   ship subtly-broken flexibility, v1 pins one platform-wide window length,
   **stored in the platform-budget sentinel (`windowHours`, default 5; env
   `QUOTA_WINDOW_HOURS` as fallback default)** and read identically by writer
   (app-api) and checker (inference-api) through the same short-TTL cached
   settings helper in `apis.shared` — tiers configure only
   `window_cost_limit`. Being a stored setting rather than env makes the
   length admin-adjustable in minutes, a deliberate pilot requirement
   (§12 playbook). Per-tier lengths are a follow-on with a known path
   (§16.1). Changing the length mid-flight causes a transient
   under/over-count on existing anchors for at most one window; acceptable.
6. **Per-tier opt-in is the rollout gate; no new global flag.**
   `window_cost_limit` is nullable — set → enforced, null → skipped. The admin
   turns it on tier-by-tier; `ENABLE_QUOTA_ENFORCEMENT` remains the existing
   global kill switch above it. The ceiling has its own independent gate: no
   ceiling setting stored → no ceiling check. Unlimited tiers (`checker.py:59`)
   continue to skip per-user checks (window + backstop) — but **not** the
   ceiling, unless also explicitly ceiling-exempt (decision 8).
7. **All three checks are independent; block reports the binding one.** Order:
   resolve tier → ceiling check (unless exempt) → window check → monthly
   backstop check. `QuotaCheckResult` gains
   `blocked_by: Literal["ceiling","window","monthly"] | None` plus window
   fields (§5). A window block carries `resets_at` (exact ISO timestamp); a
   monthly block keeps days-to-month-end copy; a ceiling block gets apologetic
   it's-not-you copy (§9).
8. **Ceiling exemption is an explicit flag, separate from "unlimited".** An
   unlimited personal quota and permission-to-spend-past-the-institutional-cap
   are different grants; bundling them would let every super user silently
   weaken the fiscal guarantee. `exempt_from_ceiling: bool = False` on
   `QuotaTier` and `QuotaOverride`. Exempt users' spend is **always still
   counted** in summaries, rollups, and the dashboard — exemption bypasses
   enforcement only. The guarantee stays honest and auditable: "spend cannot
   exceed the ceiling except for explicitly designated users, whose spend is
   fully visible."
9. **Enforcement is computed per turn, never stored.** There is no
   blocked-state to set or clear anywhere — every check compares live usage
   against current config. Consequence, and a headline property: raising the
   ceiling (or any limit/override) mid-month unblocks affected users on their
   next message, within the config cache TTL (~1 min). No deploy, no restart,
   no reset job.
10. **Ceiling enforcement line = ceiling × (1 − reserve).** Default reserve
    10%, admin-adjustable. Alerts fire at 80% and 90% of the *stated* ceiling
    (admin alert events via `QuotaEventRecorder`, new system-scoped event
    type); enforcement triggers at the reserve line. The gap is deliberate
    headroom for a human decision ("raise it or ride it out") made during a
    slow, visible approach rather than after an overrun.
11. **The ceiling reads the monthly `system-cost-rollup` item through a
    30–60s cache.** Fleet spend moves slowly relative to one turn; this is the
    same staleness tolerance the per-user checks already run with, and adds no
    per-turn Dynamo read beyond one cached fetch. The stored setting
    (`monthlyCeiling`, `reservePercent`) is a `SYSTEM_SETTINGS#` sentinel item
    in the auth-providers table (the established first-boot/skills-mode
    pattern; zero CDK) read through the same short cache.
12. **Fleet policy is enforcement + observability, not a control algorithm.**
    The ceiling enforces; the dashboard informs; the admin decides. *Rejected
    for v1 (unchanged from the overbooking draft): any scheduled job that
    auto-tightens limits — a silent mid-month budget cut is a support-ticket
    generator.*
13. **Unify the warning thresholds while we're in the file.** The 90% warning
    is hardcoded (`checker.py:103`) while the soft threshold is tier-config.
    Add `critical_warning_percentage` (default 90) next to
    `soft_limit_percentage` so monthly and window warnings derive from tier
    config, and widen the frontend `WarningLevel` union from the hardcoded
    `'80%'|'90%'` literals to a string. Low-risk, additive, kills a footgun.
14. **Window sized for real bursts, using waking-hours math.** The scary
    arithmetic against a bigger window ("$2 × 4.8 windows/day = $9.60") assumes
    24/7 usage; real students and staff have 2–3 usable windows in a waking
    day, so the realistic worst honest day at a $2 window is $4–6.
    Premium-model work (long documents, RAG, agentic tool loops with several
    model calls per user turn) runs $0.30–1.00+ per task — $1 is a *chat* budget,
    not a *work* budget; $2 is the burst size. Consequence, accepted
    deliberately: an enthusiastic daily user can now reach the $30 backstop
    (~15 maxed windows) — which is why the backstop's action must become
    degrade (decision 15). *Rejected: a $1.50 "invariant-preserving" middle —
    only half-fixes bursts and leaves no margin.*
15. **Degrade-at-backstop is the target behavior; block-with-warning is the
    bridge.** Reaching $30 should drop the user to an economy model for the
    rest of the month, not lock them out — otherwise the $2 window recreates a
    miniature monthly cliff for the heaviest honest users. PR-7 is therefore
    scheduled, not open-endedly deferred; until it lands, the 80% warning
    ($24, days of notice) + override lane are the documented path.
    *Rejected as an ADDITIONAL cap stacked under the $30 monthly: a weekly
    limit — tight enough to matter (~$8) it blocks a legitimate finals-crunch
    week (~$16–20 at two maxed windows/day), and stacking horizons multiplies
    knobs without adding protection the ceiling doesn't already provide. A
    weekly backstop **instead of** monthly is a different matter — a
    legitimate alternative horizon (Claude's own session+weekly pairing),
    supported per tier via `period_type: "weekly"`; see decision 16.
    Rejected: dynamic windows scaled to remaining-backstop ÷
    days-left — it treats the backstop as an entitlement (resurrecting the
    per-person-allowance mental model this design kills), collapses
    explainability (the limit changes daily), and has inverted fleet timing:
    everyone's burn rate scales UP at month end, exactly when fleet spend is
    closest to the ceiling, destroying the smooth-trajectory property that
    makes a hard shared ceiling safe. The fleet-safe dynamic direction —
    windows tightening as the pool depletes — is the deferred
    auto-actuation/degrade territory, not per-user scaling.*
16. **The backstop horizon is a per-tier choice — monthly or weekly; the
    institution decides.** With the ceiling owning fiscal protection and
    degrade removing lockout severity, weekly-vs-monthly is purely a question
    of which burst *shape* a population needs. **Monthly** permits week-scale
    bursts — one crunch week (~$18) plus three quiet ones — matching
    deadline-clustered academic use. **Weekly** is the safe partner for a
    bigger session window ($3–4): it bounds the integral so the rate can
    rise, a blown week costs days not weeks, and every Monday is a fresh
    start — Claude's own session+weekly pairing exists for exactly this
    reason (the 5h window caps burst rate; the weekly cap bounds 24/7-style
    sustained use that per-session caps can't see). `period_type` gains
    `"weekly"` and the write path maintains a weekly aggregate
    unconditionally alongside the monthly one (one extra atomic ADD — the
    same tier-agnostic-writer argument as the window accumulator), so
    flipping a tier's horizon needs no backfill and takes effect in minutes
    like every other knob. As an open-source platform this stays
    configuration, not policy: each adopting institution picks the horizon —
    and every other number — per tier. The observe-only pilot month reveals
    which shape each population actually has: deadline-clustered → monthly;
    steady-heavy → weekly + bigger window.

## 5. Data model changes

### `QuotaTier` (`agents/main_agent/quota/models.py`) — additive

```python
window_cost_limit: Optional[Decimal] = Field(None, alias="windowCostLimit", gt=0)
weekly_cost_limit: Optional[Decimal] = Field(None, alias="weeklyCostLimit", gt=0)
exempt_from_ceiling: bool = Field(default=False, alias="exemptFromCeiling")
critical_warning_percentage: Decimal = Field(default=Decimal("90.0"),
    alias="criticalWarningPercentage", ge=0, le=100)
```

`period_type` widens from `Literal["daily","monthly"]` to
`Literal["daily","weekly","monthly"]` (decision 16). Limit resolution follows
the existing daily pattern (`checker.py:91`): `weekly` → `weekly_cost_limit`,
`daily` → `daily_cost_limit`, fallback `monthly_cost_limit`.

Existing tier items deserialize unchanged (new fields optional or defaulted).

`QuotaOverride` gains optional `window_cost_limit` and `exempt_from_ceiling`
too — a time-bounded override (already resolved ahead of everything, already
self-expiring via `valid_from`/`valid_until`) is the standing instrument for
super users and temporary increases ("capstone crunch week"), no cleanup
required.

### Ceiling setting (sentinel item, auth-providers table)

```
PK = SK = SYSTEM_SETTINGS#platform-budget
monthlyCeiling      = Decimal dollars (absent ⇒ ceiling disabled)
reservePercent      = Decimal, default 10.0
targetAvgCostPerUser = Decimal (informational; drives dashboard context)
windowHours         = Decimal, default 5 (absent ⇒ env QUOTA_WINDOW_HOURS ⇒ 5)
updatedAt / updatedBy
```

### Window state item (`user-cost-summary` table)

```
PK = USER#{user_id}
SK = WINDOW#CURRENT
windowStart  = ISO 8601 UTC of the anchoring turn
windowCost   = Decimal dollars (atomic ADD)
updatedAt    = ISO 8601 UTC
```

One item per user, ever. `WINDOW#CURRENT` sorts outside the `PERIOD#` prefix so
existing period queries are unaffected; the item carries no GSI2 attributes so
it never appears in `PeriodCostIndex`.

### Weekly aggregate item (`user-cost-summary` table)

```
PK = USER#{user_id}
SK = PERIOD#{ISO week}     e.g. PERIOD#2026-W28  (strftime "%G-W%V", UTC)
totalCost (atomic ADD), totalRequests, lastUpdated
```

A slim sibling of the monthly summary: totals only — no GSI2 attributes (the
dashboard's distribution queries stay monthly-keyed) and no per-model
breakdown. Writer and checker must derive the week key identically
(`%G-W%V`, UTC — ISO week, resets Monday 00:00 UTC).

### `QuotaCheckResult` (`models.py:162`) — additive

```python
blocked_by: Optional[Literal["ceiling", "window", "monthly"]] = Field(None, alias="blockedBy")
window_usage: Optional[Decimal] = Field(None, alias="windowUsage")
window_limit: Optional[Decimal] = Field(None, alias="windowLimit")
window_resets_at: Optional[str] = Field(None, alias="windowResetsAt")  # ISO 8601 UTC
```

`warning_level` loosens from `Literal["none","80%","90%"]` to `str` (values
now derived from tier config — decision 13).

### `QuotaEvent` (`models.py:136`)

`event_type` Literal gains `"window_block"`, `"window_warning"`,
`"ceiling_block"`, and `"ceiling_alert"` (the last recorded system-scoped —
`PK=USER#__SYSTEM__` — at the 80%/90% thresholds, deduped like warnings).
`metadata` carries `{windowStart, windowResetsAt}` / `{fleetSpend, ceiling}`
as applicable. `QuotaEventRecorder` gets the corresponding record methods.

## 6. Write path

In `_update_cost_summary_async` (`metadata.py:201`), after the existing
`update_user_cost_summary` call, add:

```python
await storage.update_user_window_cost(user_id=user_id, cost_delta=cost,
                                      timestamp=timestamp)
await storage.update_user_weekly_cost(user_id=user_id, cost_delta=cost,
                                      timestamp=timestamp)  # PERIOD#%G-W%V
```

`DynamoDBStorage.update_user_window_cost` implements the two-step conditional
write from decision 4, with `cutoff = timestamp - QUOTA_WINDOW_HOURS`. It is
**tier-agnostic and unconditional** — it always accumulates, even for users on
window-less or unlimited tiers (a handful of wasted `ADD`s beats a resolver
dependency in the write path, and it means flipping `window_cost_limit` on a
tier takes effect against already-accumulated state — no "first window is
free" gap). Failures log and swallow, exactly like the monthly-summary write —
cost accounting must never fail a turn.

Writer and checker MUST share the same window-length source: a
`get_quota_window_hours()` helper in `apis.shared` — resolution order
**sentinel `windowHours` (through a ~60s cache) → env `QUOTA_WINDOW_HOURS` →
`5`** — consumed by `dynamodb_storage.py` (app-api side) and `QuotaChecker`
(inference side). Cache skew between the two processes is bounded by the TTL
and costs at most one transiently mis-windowed anchor, same tolerance as a
mid-flight length change (decision 5). The sentinel lives in the
auth-providers table, whose name + IAM read are already wired to both
containers, so no CDK change.

## 7. Check flow (`QuotaChecker.check_quota`)

```
resolve tier (unchanged; fail-closed on none)
exempt = tier.exempt_from_ceiling or (active override with exempt_from_ceiling)

ceiling check (skip if exempt, or no ceiling setting stored):
    fleet = rollup_reader.get_monthly_fleet_spend()          # 30–60s cached
    line  = ceiling.monthlyCeiling * (1 - reservePercent/100)
    record ceiling_alert events at 80%/90% of stated ceiling (deduped)
    if fleet >= line:
        → allowed=False, blocked_by="ceiling"

if unlimited tier: skip window + monthly (existing behavior, checker.py:59)

window check (skip if tier.window_cost_limit is None):
    item = aggregator.get_user_window(user_id)               # 30s cached
    fresh = item and item.windowStart + QUOTA_WINDOW_HOURS > now
    window_usage = item.windowCost if fresh else 0
    resets_at    = item.windowStart + QUOTA_WINDOW_HOURS if fresh else None
    if window_usage >= tier.window_cost_limit and tier.action_on_limit == "block":
        → allowed=False, blocked_by="window", window_resets_at=resets_at
    elif window pct >= soft/critical thresholds:
        warning_level set from window (window warnings win ties over monthly —
        they're the more actionable signal)

monthly/weekly/daily backstop: same logic (checker.py:89-165); the period key
and limit resolve from tier.period_type (weekly → PERIOD#%G-W%V +
weekly_cost_limit — decision 16); critical threshold now from
tier.critical_warning_percentage instead of hardcoded 90; reset copy for
weekly tiers = "resets Monday" with the exact timestamp
```

Failure semantics unchanged: any usage-read error (window, monthly, or fleet)
fails open with a logged warning, mirroring the existing monthly path
(`checker.py:78`).

## 8. Hardening the rollup path (ceiling prerequisite)

The ceiling turns `system-cost-rollup` from an observability aggregate into an
**enforcement input**. Two items before ceiling enforcement ships:

1. `_update_system_rollups_async` is best-effort today — silent failures would
   under-count fleet spend against the ceiling. Minimum bar: failures must log
   at ERROR with a metric/alarm, and the budget-health view shows a
   reconciliation check (sum of `PeriodCostIndex` user totals vs. the rollup
   item) so drift is visible. Full fix (retry queue) only if drift is observed.
2. Verify the inference runtime has env + IAM read access to the
   `system-cost-rollup` table — the per-user summary table is wired to both
   sides, but rollups may be app-api-only today. If not, a small CDK/env
   addition (thread through `PlatformComputeRefs`, per house convention).

## 9. SSE / messaging

- `QuotaWarningEvent` / `QuotaExceededEvent` (`apis/shared/quota.py`): add
  optional `resetsAt` (ISO UTC) + `blockedBy`; `formattedUsage` shows the
  binding limit's numbers (`"$1.72 / $2.00"` for a window event).
- `build_quota_exceeded_event` gains three branches:
  - **window:** *"You've used your 5-hour allowance. It resets at {time} —
    your conversation and history are untouched, just check back then."*
  - **monthly:** existing copy (days to month end), plus a pointer to the
    exception/override request path — bridge behavior until PR-7 replaces
    the block with degrade.
  - **ceiling:** apologetic, explicitly not-the-user's-fault: *"The
    platform's shared monthly budget has been reached, so responses are
    paused for everyone — this isn't about your usage. Service resumes when
    the budget is increased or the month resets."* No usage table (the user's
    own numbers are irrelevant and showing them implies blame).
- Delivery unchanged — blocks still stream as conversational assistant
  messages (`routes.py:1141-1154`); warnings still injected by
  `stream_with_quota_warning` (`:1729`) with the new fields riding along.
- SPA: `stream-parser-types.ts` validators + `quota-warning.service.ts` accept
  the new optional fields; banner renders `resetsAt` in local time with a
  countdown for window blocks ("Resets in 2h 14m · 2:30 PM"); ceiling blocks
  render a distinct platform-wide-notice style, not the personal-usage style.
  Additive — events without the new fields render exactly as today.

### Always-visible quota status (user-facing endpoint)

Everything a personal usage meter needs already exists per user by
construction: the `WINDOW#CURRENT` item (window usage + anchor → percentage +
reset time), the `PERIOD#` aggregates (monthly or weekly usage), and the
resolved tier's limits. But SSE events only surface at warning/exceeded
thresholds — an always-visible meter needs one thin read endpoint:

`GET /quota/status` on app-api (`Depends(get_current_user_from_session)` per
the auth rule), returning the caller's own state:

```json
{
  "window":  { "usage": 0.80, "limit": 2.00, "percentage": 40,
               "resetsAt": "2026-07-10T21:30:00Z" },   // null if no window
  "period":  { "type": "monthly", "usage": 11.20, "limit": 30.00,
               "percentage": 37, "resetsAt": "2026-08-01T00:00:00Z" },
  "unlimited": false,
  "exemptFromCeiling": false
}
```

This is a self-scoped variant of what the admin quota inspector already
computes per user (`service.py:294`) — same resolution + reads the checker
uses, no new data. Ships in PR-4; the usage settings page renders both meters
("$0.80 of $2.00 this window · resets 2:30 PM"; "$11.20 of $30.00 this
month"), and unlimited tiers render as "no limits apply". A compact meter
near the composer is a possible later add once the endpoint exists.

## 10. API-key / Converse path

`converse_routes.py:369-393`: on `blocked_by == "window"`, the 429's
`Retry-After` becomes the real seconds until `window_resets_at` (today a quota
block sends no retry hint). Ceiling and monthly blocks keep plain 429
semantics. Also fix the no-tier fail-open (`:376`) to fail closed, matching
the chat path — a one-line consistency fix riding along.

## 11. Admin API + UI

- **Tier CRUD** (`apis/app_api/admin/quota/`): pass through
  `window_cost_limit`, `exempt_from_ceiling`, `critical_warning_percentage`;
  tier-detail form gains a "Cooldown window" section (single dollar field;
  helper text states the platform window length, read from a config echo on
  the tier-list response so the SPA doesn't hardcode "5 hours") and an
  "Exempt from platform ceiling" toggle with a warning-styled description.
- **Overrides**: form gains optional `window_cost_limit` +
  `exempt_from_ceiling` — the super-user / temporary-increase lane (§5).
- **Quota inspector** (`GET /admin/quota/users/{user_id}`, `service.py:294`):
  adds live window state (`windowUsage`, `windowLimit`, `windowStart`,
  `windowResetsAt`, in-cooldown flag) and ceiling exemption status.
- **Event viewer**: renders `window_block` / `window_warning` /
  `ceiling_block` / `ceiling_alert`.

### Tier catalog guidance (documentation, not code)

Fewer tiers, not more — with the ceiling holding the fiscal line, tiers only
encode *pacing*, so two or three cover the institution:

| Tier | Window / 5h | Monthly backstop | Ceiling |
|---|---|---|---|
| Default (students, staff) | $2.00 | $30 | counted |
| Faculty / power users | $3.00–4.00 | $40–60 monthly **or** $15–18 weekly | counted |
| Research / grant-funded | none | very high | exempt (own funding) |
| Service accounts / headless agents | tight | tight | counted |

Assignments carry over untouched (resolver ladder, §3): the default tier makes
the whole population paced on day one; JWT-role/email-domain rungs mean tier
membership tracks Entra automatically.

## 12. Recommended initial configuration + sizing rationale

| Knob | Value | Rationale |
|---|---|---|
| Window length (`windowHours` sentinel; env fallback) | 5 | Claude parity; matches a real work session |
| Default-tier `window_cost_limit` | **$2.00** | A genuinely heavy 5-hour *work* session: premium-model tasks (long documents, RAG, agentic tool loops) run $0.30–1.00+ each, so $1 covers chat but not bursts. Waking-hours math bounds the realistic worst honest day at 2–3 windows = $4–6 (decision 14) |
| Default-tier `monthly_cost_limit` (backstop) | **$30** (6× target) | Where the month's premium-model subsidy ends. With a $2 window an enthusiastic daily user *can* reach it (~15 maxed windows) — by design its action becomes degrade-to-economy-model (PR-7, decision 15), not lockout; on bridge behavior, the 80% warning at $24 gives days of notice and the override lane is the exception path. $30 naturally fits the academic rhythm: one crunch week (~$16–20 at two maxed windows/day) + three normal weeks. Even in absurd scenarios it barely moves the average (2% of users at $30 adds $0.60 to the mean) — the ceiling, not the backstop, protects the budget |
| Backstop horizon (`period_type`) | **monthly** (default) | Per-tier choice (decision 16): monthly fits deadline-clustered student use; **weekly ($15–18) is the safe partner for a $3–4 power-user window** — pilot data decides which shape each population has |
| `targetAvgCostPerUser` | **$5** | Institutional target; informational context on the dashboard |
| `monthlyCeiling` | users × $5 | The enforced total |
| `reservePercent` | 10 | Enforcement at 90% of stated budget = decision headroom |

**Retuning after month one:** set the backstop at ~p99.5 of the observed
user-spend distribution (budget-health view provides it). **The health metric
for "are we punishing power users" is window-block events per week** — the
window is the limit honest heavy users can actually feel. Backstop events
should be rare; under bridge behavior each one deserves a look in the event
log, and their frequency is the direct measure of how urgently PR-7 is
needed.

### Pilot tuning playbook

Every number in the system is admin-adjustable at runtime, and because
enforcement is computed per turn (decision 9), a change applies to everyone's
next message once the relevant cache rolls — no deploy, no reset, nobody to
unblock. The symptom→knob mapping, for responding to pilot feedback same-day:

| Observed during pilot | Knob to turn | Where | Effective within |
|---|---|---|---|
| Too many users hitting cooldowns | Tier `window_cost_limit` ↑ | Tier form | ≤5 min (resolver cache) |
| Cooldowns feel too long to wait out | `windowHours` ↓ (budget ↓ proportionally) | Platform-budget editor | ~1 min |
| One cohort systematically constrained | New tier / reassignment | Tier + assignment CRUD | ≤5 min |
| One person in a legitimate crunch | Self-expiring override | Override form | ≤5 min |
| Fleet trending hot/cold vs. target | `monthlyCeiling` / `reservePercent` | Platform-budget editor | ~1 min |
| Honest users reaching the backstop | Tier `monthly_cost_limit` ↑; accelerate PR-7 | Tier form | ≤5 min |
| Power users want bigger bursts than a raised window safely allows | Switch tier to `period_type: "weekly"` + raise the window (decision 16) | Tier form | ≤5 min |
| Warnings too naggy / too late | Tier `soft_limit_percentage` / `critical_warning_percentage` | Tier form | ≤5 min |
| Any enforcement feels too harsh mid-pilot | Tier `action_on_limit` → `"warn"` | Tier form | ≤5 min |

**Recommended pilot phase 1: observe-only.** Set the pilot tier's
`action_on_limit` to `"warn"` — the entire mechanism runs and records
(window/backstop events, warnings, fleet telemetry, dashboard) but blocks
nobody. One month of that yields the would-have-been cooldown rate and the
real spend distribution at zero user risk; then flip to `"block"` with
numbers the data has already validated. Phase 1 provably cannot harm a user,
which is also the cleanest possible answer to pilot-risk questions.

Diagnosis reads straight off the telemetry: window-block (or would-have-
blocked warning) events → window budget; backstop events → backstop size or
PR-7 urgency; ceiling gauge slope → ceiling/reserve; a skewed per-tier
distribution → tier design. Each failing pilot criterion points at exactly
one dial.

The observe-only month also answers the **horizon question** (decision 16):
because the write path maintains monthly and weekly aggregates for everyone,
the pilot data shows each population's spend shape directly —
deadline-clustered (a few hot weeks, quiet otherwise → monthly backstop fits)
vs. steady-heavy (consistent weeks → weekly backstop + a bigger window fits).
Choosing the horizon per tier is then a data read, not a debate.

## 13. Fleet budget health (dashboard)

New read-only admin surface: `GET /admin/costs/budget-health?period=YYYY-MM`
(lives in `apis/app_api/admin/costs/` — it reads cost data, not quota config).

| Field | Source |
|---|---|
| `totalSpendMTD`, `perModelBreakdown` | `system-cost-rollup` monthly item |
| `ceiling`, `reservePercent`, `enforcementLine`, `targetAvgCostPerUser` | sentinel setting |
| `activeUsers`, `avgCostPerUser` | `PeriodCostIndex` partition count; quotient |
| `burnRate`, `projectedEOM`, `projectedExhaustionDate` | daily rollups; linear extrapolation |
| `distribution` (p50/p90/p95/p99, top-N spenders) | `PeriodCostIndex` sorted by `COST#` |
| `reconciliation` (rollup vs. summed user totals) | drift check per §8 |
| `exemptSpendMTD` | sum over users on exempt tiers/overrides — keeps the guarantee auditable |

SPA page (`admin/costs/`): spend-vs-ceiling gauge with the 80/90/enforcement
markers, burn-rate trend, projected exhaustion date, distribution curve,
exempt-spend callout, and the platform-budget editor
(ceiling/reserve/target/window-length, admin-gated).

## 14. PR breakdown

All PRs target `develop`, additive schemas, safe to ship dark.

- **PR-1 — window data + write path (dark).** `QuotaTier`/`QuotaOverride`/
  `QuotaEvent` model fields; `get_quota_window_hours()` shared helper
  (sentinel → env → default resolution from day one, so the length is
  admin-adjustable as soon as an editor exists);
  `update_user_window_cost` two-step conditional write +
  `update_user_weekly_cost` slim atomic ADD (`PERIOD#%G-W%V`); hook both into
  `_update_cost_summary_async`; `CostAggregator.get_user_window` with its own
  cache key. Unit tests incl. the re-anchor race and week-key derivation.
  Nothing reads the new tier fields yet.
- **PR-2 — window enforcement.** `QuotaChecker` window check + threshold
  unification (decision 13) + weekly `period_type` resolution (decision 16);
  `QuotaCheckResult` fields; event recorder types; SSE event fields + window
  copy; chat path passthrough; converse path `Retry-After` + fail-closed fix.
  Tier config remains the gate — behavior unchanged until an admin sets a
  `window_cost_limit` or flips a `period_type`.
- **PR-3 — admin API + UI for windows.** Tier/override CRUD passthrough, tier
  form section, exemption toggle (field only — ceiling not yet enforced),
  quota inspector window state, event viewer types, and a minimal
  window-length setting editor (the sentinel's `windowHours` only — the full
  platform-budget editor arrives with PR-6, but pilot tuning needs the length
  dial before then). Windows fully operable — and pilot-tunable — after this.
- **PR-4 — SPA user surfaces.** `GET /quota/status` endpoint (§9) +
  stream-parser validators, banner countdown + local-time reset rendering,
  usage settings page renders the window and period meters
  ("$0.80 of $2.00 this window · resets 2:30 PM").
- **PR-5 — ceiling enforcement.** Sentinel setting + admin CRUD; rollup-read
  helper with cache; checker ceiling step + exemption resolution; `ceiling_*`
  events; ceiling SSE copy + SPA notice style; rollup hardening + IAM/env
  verification (§8). Absent setting = disabled, so ships dark.
- **PR-6 — budget-health dashboard.** Endpoint + SPA page per §13, including
  the ceiling editor.
- **PR-7 — degrade at the backstop (`action_on_limit: "degrade"`).**
  Scheduled, not optional (decision 15): completes the no-cliff promise for
  the $30 backstop. See §15. Extending degrade to ceiling and window blocks
  is a natural follow-on once the mechanism exists.

Rollout order is deliberate: windows land and pace the population *before*
ceiling enforcement exists, so by the time the ceiling is live the fleet
trajectory is already smoothed — the ceiling should be boring on day one.

## 15. PR-7: degrade instead of block

Limit reached → continue on a cheap model (Haiku/Nova-class) instead of
stopping, messaged as "you're on the economy model until {reset}."

**Committed scope: the monthly backstop.** With a $2 window, honest sustained
use can reach $30, and degrade is what keeps that from being a miniature
monthly cliff (decision 15) — the heavy user's month becomes "work hard all
month, maybe finish it on the economy model, full reset on the 1st." It is
sequenced last because it's the one piece that isn't additive: model
selection isn't quota-aware today, and forcing an override touches agent
construction and the per-session agent cache key
(`inference_api/chat/service.py`). Until it lands, the backstop blocks with
the 80% warning + override lane as the bridge, and backstop-event frequency
(§12) measures the urgency.

Prereqs from earlier PRs: `blocked_by` on the check result (PR-2/5) and a
tier-configured degrade model id; pricing for the degraded model already
lives in `ManagedModels`. Natural follow-ons once the mechanism exists:
degrade at the ceiling ("everyone rides the economy model for the rest of the
month" beats a platform pause) and optionally at the window.

## 16. Open questions

1. **Per-tier window lengths.** v1 deliberately pins one platform-wide length
   (decision 5). If heterogeneous lengths are ever wanted, the clean path is
   making the *write* tier-aware without a resolver call: the enforcement path
   already resolved the tier that turn, so stamp the resolved window length
   into the turn's message metadata and let `_update_cost_summary_async` pass
   it through to the anchor arithmetic. Small, but touches the metadata
   contract — build it when someone actually asks for a non-5h window.
2. **Per-unit ceilings.** If a college funds its own power users, that's the
   seed of per-unit (per-tier or per-department) ceilings with their own
   budgets. Needs tier/unit attribution on cost summaries — explicitly out of
   v1; the `exempt_from_ceiling` flag is the v1 answer for separately-funded
   cohorts.
3. **Should a window block suppress the monthly warning banner?** Proposed:
   yes — one banner, binding constraint only. SPA-side concern; decide in
   PR-4.
4. **Daily `period_type` tiers + windows.** Nothing conflicts (the window is
   orthogonal to the aggregate period), but the admin form should discourage
   configuring both a daily limit and a window — they overlap in purpose. Copy
   question, not a code question.
5. **Ceiling-approach notification routing.** `ceiling_alert` events land in
   the event log + dashboard in v1; wiring them to email/Teams for admins is
   an obvious later add once a platform notification channel exists.
