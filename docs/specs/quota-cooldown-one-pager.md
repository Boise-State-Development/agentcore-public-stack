# AI Cost Controls: From Monthly Cutoffs to Paced Budgets

*One-page rationale for committee review — companion to the technical spec
(`quota-cooldown-windows.md`).*

## The problem with what we have today

Our only cost control is a per-user monthly dollar limit with two behaviors: a
warning at a set percentage, and a **hard cutoff at 100% that lasts until the
1st of the next month**. This creates four problems, and they compound:

1. **It punishes exactly the wrong people.** The users who hit the limit are
   our most engaged adopters — the people getting the most value from the
   platform. Their reward for productivity is a lockout measured in weeks,
   often mid-project. That is the single worst adoption signal we can send.
2. **It strands most of the budget.** AI usage is extremely uneven: most users
   spend pennies, a modest group spends a few dollars, and a small group of
   power users drives real value. Equal per-person allowances guarantee that
   light users strand their budget unspent *while* heavy users are cut off —
   waste and frustration from the same mechanism.
3. **It guarantees nothing about total spend.** Today's aggregate cost is
   simply "whatever the sum of individual limits allows." Setting limits is
   guesswork, and the institution has no enforced total — only a hoped-for one.
4. **It forces conservative limits.** Because the failure mode (a weeks-long
   lockout) is so harsh, limits get set low to avoid it — shrinking the value
   of the platform for everyone to protect against the behavior of a few.

## The proposed model: three controls, each with one job

**1. Cooldown windows — pace, don't punish.** Every user gets a healthy budget
for each 5-hour stretch of work — enough for a genuinely heavy session. Exceed
it and you wait **hours, with an exact reset time shown ("resets at 2:30 PM")**
— not weeks. This is the same mechanism Anthropic uses for Claude's own
subscribers: it caps the *rate* of spending without capping ambition.
*Alleviates problem 1:* a heavy user's worst day becomes "take a break until
mid-afternoon."

**2. A pooled budget with one hard, adjustable ceiling — the fiscal
guarantee.** Leadership sets a single number: target average cost per user ×
user count. The system **enforces** that total — spend cannot exceed it.
Within the pool, budget flows to whoever is producing with it rather than
being reserved per person.
*Alleviates problems 2 and 3:* no stranded allowances, and institutional
exposure becomes one deliberately set number instead of an emergent sum of
guesses.

**3. A generous individual monthly backstop — where the premium ends, not
where the month ends.** Each user keeps a personal monthly cap that normal
work never reaches; only sustained heavy use lands there, and only after
early warnings. Reaching it is not designed to end anyone's month: the target
behavior is an **automatic switch to a much cheaper model for the remainder**
(with warnings plus a documented exception path in the interim). It stops
runaways and bounds sustained heavy use; it does not ration normal work.
*Alleviates problem 4:* with the window bounding burn *rate* and the ceiling
bounding the *total*, individual caps can finally be generous.

**Why the three compose:** the windows keep any individual or small group from
draining the shared pool quickly, which is what makes a hard shared ceiling
safe and fair. Each control does one job; none has to compensate for another.

## Proposed opening configuration

| Knob | Value | Why |
|---|---|---|
| Target average cost per user | **$5/month** | The committee's investment decision; fixes the ceiling (× user count) |
| Session budget (per 5-hour window) | **$2.00** | A genuinely heavy work session — sized for real bursts (long documents, multi-step tasks), and for the reality that users have only 2–3 usable sessions in a waking day |
| Individual monthly backstop | **$30** (6× target) | Where premium-model use ends for the month — normal months never reach it; sustained heavy use gets early warnings, then (per roadmap) an economy model rather than a stop; genuine exceptions get an override |
| Backstop horizon | **Monthly** (weekly optional per group) | Monthly suits deadline-clustered semester work (one crunch week fits inside it); a weekly cap — the model Claude itself uses — suits steadily heavy groups and safely permits even larger session budgets. Configurable per group; the pilot data shows which fits whom |

Both pacing values are re-tuned from the first month's real usage
distribution; the dashboard is built to show exactly that.

## Flexibility built in

- **Exceptions without side doors.** Designated roles (e.g. grant-funded
  research) or individuals can be exempted — permanently via policy tied to
  the university's own identity roles, or temporarily via self-expiring
  overrides ("capstone week"). Exempt usage is **always still measured and
  reported**; exemption is an explicit, auditable grant, so the fiscal
  guarantee stays honest: *spend cannot exceed the ceiling except for
  designated users, whose spend is fully visible.*
- **Budget changes take effect within a minute.** Nothing is ever "marked
  blocked" — every request is evaluated against the current numbers. If
  leadership raises the ceiling as it approaches, affected users are working
  again on their next message. No deployment, no reset, no ticket.
- **Policy follows the org chart automatically.** Tiers are assigned through
  the university's identity system — when someone becomes faculty, their
  treatment upgrades with zero administrative action.
- **Users see their own status.** A personal meter shows session and
  monthly/weekly usage with exact reset times — no surprise cutoffs, and
  fewer "how much do I have left?" support questions.
- **Every number is configuration, not code.** As an open-source platform,
  the session budget, window length, caps, horizon (monthly vs. weekly), and
  ceiling are all settings — each adopting institution, and each user group
  within it, sets its own policy.

## Safeguards at the ceiling

- **Alerts at 80% and 90%**, with a live dashboard: month-to-date spend vs.
  ceiling, burn rate, projected month-end total, and where the money goes —
  problems surface weeks out, not overnight.
- **A built-in ~10% reserve:** enforcement triggers below the stated budget,
  so "hitting the limit" still leaves leadership deciding with headroom.
- **Scheduled upgrade — degrade, don't stop:** replace pauses with automatic
  fallback to a much cheaper model — first for individuals at their monthly
  cap, later near the ceiling — so people keep working at a fraction of the
  cost rather than stopping.

Reaching the ceiling is not a failure — it means the budget was fully
utilized, and the decision is simply whether demonstrated demand justifies
next month's number.

## At a glance

| | Today | Proposed |
|---|---|---|
| Heavy user hits a limit | Locked out until the 1st | Waits until a stated time, e.g. 2:30 PM |
| Institutional total spend | Unenforced; sum of guesses | One hard, adjustable ceiling |
| Unused light-user budget | Stranded | Pooled — flows to productive use |
| Setting limits | Guesswork, tuned by fear of lockouts | Users × target cost/user, plus two pacing values |
| Exceptions | Ad hoc | Auditable exemptions + self-expiring overrides, spend still visible |
| Raising the budget | — | Effective within a minute, mid-month |
| Visibility | Per-user usage only | Live spend vs. ceiling, burn rate, projection, distribution |

## The decision requested

1. Approve the paced-budget model (windows + pooled ceiling + backstop).
2. Set the initial target average cost per user (proposed: **$5/month**),
   which fixes the ceiling.
3. Approve a phased rollout: pacing windows first, then ceiling enforcement
   and the budget dashboard — each phase reversible and adjustable without
   rework.

*Engineering cost is modest: the model builds on cost tracking the platform
already performs; no new infrastructure of consequence.*
