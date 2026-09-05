# Response feedback

**Status:** PROPOSED — no code. Written 2026-09-04 from the "how would we
benefit?" conversation.
**Refs:** `docs/specs/agentcore-evaluations-spike-findings.md` (the eval
harness this feeds), `docs/specs/mid-turn-steering.md` (the injection path
Phase 1 reuses), `docs/specs/agent-marketplace.md` D15 (the *other* feedback
channel — see §3), `docs/kaizen/research/2026-08-28.md` (built-in skill
evaluators)

## 1. Problem

Two commented-out lines have been sitting in the codebase for a long time:

```
backend/src/apis/shared/sessions/models.py:645   # Note: Feedback will be added in future implementation
backend/src/apis/shared/sessions/models.py:646   # feedback: Optional[Feedback] = None
backend/src/apis/app_api/messages/models.py:132  # Note: Feedback will be added in future implementation
backend/src/apis/app_api/messages/models.py:133  # feedback: Optional[Feedback] = None
```

Nothing was ever built behind them. The question this spec answers is not "can
we add a thumb" — that is an afternoon — but **what a thumb is worth here, and
what shape makes it worth that.**

The honest starting position is that thumbs data is usually near-worthless.
Coverage runs 1–5% of turns, skews negative, and a single down-thumb conflates
"factually wrong", "ignored my instructions", "far too long", and "the MCP tool
500'd and the model apologised". Anyone treating the raw rate as a KPI is
reading noise.

What changes the calculus here is the **join surface this platform already
has, and the one axis it is missing.**

Every model call already writes a `C#` row carrying model id, attribution,
cost breakdown, token usage, `cacheStatus`, `cacheGapSeconds`, `wastedUsd`,
and `agentSwitched`. There is an admin anatomy endpoint over it
(`GET /admin/costs/sessions/{id}/calls`), EMF metrics, a fleet dashboard, and
a five-workstream cost-effectiveness roadmap. **Cost is instrumented to three
decimal places. Quality is not instrumented at all.**

CLAUDE.md's cost tenet states: *"When cost and answer quality genuinely
conflict, quality wins."* Today there is no instrument that could ever detect
that conflict. Every compaction tuning decision, every model downgrade, every
context-offload threshold has been made against a cost number with quality
asserted rather than measured. A thumb is the cheapest possible counterweight.

## 2. The thesis

> **Feedback is a sampler and a label. It is not a metric.**

Three consequences follow, and they drive every decision below:

1. **As a sampler:** a down-thumb marks the small subset of turns worth
   spending expensive LLM-judge tokens on. The Evaluations spike proved the
   judging pipeline works end to end today, but judging is per-trace and
   costs real money — it cannot run fleet-wide. Human-marked failures are the
   priority queue that makes it affordable.
2. **As a label:** joined against the config dimensions already stamped on
   every call, a down-thumb answers questions currently unanswerable — does
   quality actually drop after compaction? is Sonnet worth 3× Haiku on *this*
   workload? Relative comparison between config arms, never absolute rates.
3. **Never as a metric:** the absolute rate over a self-selected 3% means
   nothing. It must not appear on any leadership dashboard as a quality score.
   §9 makes this a hard rule, not a caution.

## 3. What this is NOT — de-conflicting with agent reports

Marketplace Phase 8 (D15) already shipped a feedback channel, and it is a
different object. `AgentReport` (`apis/shared/assistants/models.py:1621`) is
agent-scoped, free-text, identity-bearing, and lands in a **moderation queue**
sorted by `REPORT_REASON_SEVERITY`. `ReportReason` deliberately includes
`suggestion` so that a user who does not know whether they hit a defect or a
missing capability is not asked to pick the right intake form. It surfaces as
`app-agent-feedback-link` at the foot of a conversation
(`message-list.component.html`, `showFeedbackLink()`), published agents only.

Response feedback is **per-message, fixed-set, high-volume, and analytical.**
It has no queue and no moderator. Nobody reads an individual down-thumb.

| | Agent report (D15, shipped) | Response feedback (this spec) |
|---|---|---|
| Scope | One published Agent | One assistant message |
| Volume | Rare, hand-written | Frequent, one click |
| Consumer | A human admin, one at a time | Aggregation + the eval harness |
| Identity | `reporterId` shown to admin, never to author | Same rule (§8) |
| Surface | Foot of conversation, published agents only | Every assistant message |

**They must not be merged, and they must not both shout.** The conversation
tail can hold one report link and per-message thumbs without collision, but the
down-thumb reason sheet must include an escape hatch — *"this is a problem with
the Agent itself"* — that hands off to the existing report dialog rather than
inventing a second moderation path. That handoff is the only coupling.

## 4. What already exists

Almost every mechanism this needs is built for another reason.

**A per-message row family keyed the right way.** `sessions-metadata` already
holds two SK prefixes under `PK = USER#<user_id>`: `C#` cost rows
(`SK C#{timestamp}#{uuid}`, `GSI_SK C#{timestamp}`, carrying `messageId`) and
`D#` display rows (`SK D#{session_id}#{message_id}`, `GSI_SK D#{message_id}`).
The `D#` row is the closer model: deterministic key, one row per message, no
collision uuid. A feedback row is a third prefix in a table that already
supports it, with a `GSI_PK = SESSION#<id>` read path that already exists.

**A reader that already unions prefixes.** `get_session_cost_anatomy`
(`admin/costs/routes.py:183`) queries `C#` and `D#` in parallel over
`SessionLookupIndex` (`metadata.py:1987`). Adding `F#` is a third leg of a
query that is already fan-out shaped.

**A verified evaluation pipeline.** Per the Evaluations spike: 16 built-in
`ACTIVE` evaluators, `EvaluationClient.run()` returning scored results with
explanations quoting real dev conversation text, and — critically — session
correlation that needs no new plumbing, since
`runtime_session_id_for()` (`apis/shared/harness/runner.py:63`) is
`sid-<sha256(session_id)>`. A feedback row carrying `sessionId` +
`messageId` is already joinable to the spans.

**Built-in skill evaluators.** `Builtin.SkillSelectionAccuracy` and
`Builtin.SkillInstructionFollowing` (2026-08-28 research) target the exact
failure mode the Skills v2 epic left unmeasured. They need a sampling signal
to be affordable.

**An injection path for the retry loop.** `POST /sessions/{id}/steer`
(`app_api/sessions/routes.py:741`) already accepts a correction into a turn,
gated by `mid_turn_steering_enabled()`. "Retry with this correction" is the
same shape aimed at a finished turn instead of a running one.

**A place to put the buttons.** `message-actions.component.ts` already renders
Copy and Continue under every assistant message. Thumbs belong in that rail,
not in a new component.

**A precedent for attaching conversation content to feedback.**
`report_service.file_report` verifies an attached `sessionId` belongs to the
reporter before storing it. Reuse that check verbatim.

## 5. Decision summary

| Question | Decision |
|---|---|
| Storage | Third SK prefix `F#` on `sessions-metadata`, **never** on the message itself |
| Key | `SK F#{session_id}#{message_id}`, `GSI_SK F#{message_id}` — deterministic, idempotent upsert |
| Mutability | Overwrite in place. Changing or clearing your mind is a normal act, not an audit event |
| Signal set | `up` / `down` + optional fixed-set reason + optional free text |
| Reason set | Fixed and small (§6). Free text is secondary and never required |
| Prompt cache | Feedback is a pure side-channel write. It never enters conversation history, `toolConfig`, or the system prompt |
| Consequence | Phase 1 ships a **visible** consequence (retry-with-correction), not a suggestion box |
| Implicit signals | Captured in the same row family, same phase. They are denser than thumbs and cost no UI |
| LLM-judged signals | Offline batch over persisted turns only. Never an inline per-turn model call |
| Reporting | Relative comparison between config arms. Absolute rate is never published as a quality score |
| Identity | Stored. Visible to admins with the scope; never to an agent author |
| Flag | `RESPONSE_FEEDBACK_ENABLED`, default on with a kill switch (house convention) |

## 6. The signal set

Down-thumb opens a chip row. One tap, no typing, dismissible:

- **Wrong or made up** → routes to `Correctness` / `Faithfulness` evaluators
- **Didn't follow my instructions** → `InstructionFollowing`
- **Too long / too short** → a style signal, and a Memory Spaces input (§7)
- **A tool or search failed** → **ops, not model.** Auto-corroborated against
  the turn's `tool_result` blocks
- **Out of date** → KB freshness; joins to `kb_sync` state
- **Something else** → free text

The mapping to evaluators is the point of a fixed set. An unbucketed
down-thumb is noise; a bucketed one is a routing decision.

Up-thumb takes no reason. Asking for one on a positive signal collapses the
response rate and buys almost nothing.

## 7. What it unlocks

### Platform

**Negative-sample mining.** Down-thumbed turns become the eval harness's input
queue. This flips the harness from "run against a synthetic suite someone wrote"
to "run against observed real-world failures" — a categorically better asset,
and the reason this spec is worth more than the sum of its UI.

**Config-dimension attribution.** The prize. Join down-thumb rate against
dimensions already on the `C#` row and against session state:

- **Compaction.** Does quality degrade after a compaction event? Today there is
  a `compaction` SSE event, a checkpoint, and a truncation anchor — and zero
  evidence about whether users notice. The compaction-death-spiral incident
  cost a faculty member $27 of a $30 quota in five days; nobody knows what it
  cost in answer quality.
- **Model.** Haiku vs Sonnet per workload, measured rather than assumed.
- **`agentSwitched`.** The `@`-mention history fork is a known cache cost. Is
  it also a quality cost?
- **KB freshness.** Do stale-sync assistants draw more "out of date"?
- **Skills.** Which granted skills correlate with down-thumbs, as the cheap
  pre-filter for `SkillSelectionAccuracy`.

**Rework cost.** Sum the call cost of a down-thumbed turn plus the retries that
follow it. This denominates quality in dollars, on the dashboards that already
exist. Strategically this is the most valuable single number in the spec: it
makes quality legible in the budget conversation instead of being the thing we
assert but cannot defend.

**Marketplace ranking and version regression.** Store tiles rank on nothing
user-derived. A helpfulness rate per **published `AgentVersion`** gives ranking
*and* an alarm when a new version's rate drops — a trigger for the §8 rollback
path that currently has no automated reason to fire.

**Tool and MCP health.** "A tool failed" + an errored `tool_result` is an ops
alert, not a model signal. Worth noting that dev ran with zero working MCP
tools for weeks and nothing surfaced it.

**Preference data.** A `fine_tuning` domain already exists, and paired
up/down on the same prompt is DPO-shaped. Explicitly **out of scope until
Phase 5** and gated on a policy decision, not an engineering one (§8).

### User

**The retry loop is the actual user-facing feature.** A down-thumb that offers
*"retry with that in mind"* turns feedback from a suggestion box into a
steering act: the user gets a better answer in seconds, and we get a labeled
pair for free. Ship the thumb without this and the response rate decays to
zero within weeks — at which point the data is too sparse to use and the whole
spec is dead. **The consequence is not a Phase 2 nicety; it is the thing that
keeps Phase 1 alive.**

**Personalization.** Repeated "too long" → a durable style preference in a
Memory Space. The primitive exists; this is the missing input to it.

**Agent authors are a distinct user class.** A marketplace publisher currently
receives no signal about whether their instructions and tool bindings work in
the wild. Aggregate-only, never per-user (§8).

## 8. Governance

An admin reading a down-thumbed turn is an admin reading someone's
conversation. Three rules:

1. **Aggregates by default; content behind a scope.** Rate, reason
   distribution, and config joins need no content. Reading the actual message
   requires a delegated-admin scope (`granular-admin-permissions.md`) and
   writes an audit row.
2. **Authors get substance, never identity.** Same rule as D15.2, for the same
   reason: authors need to know what went wrong, admins need identity to spot a
   brigade.
3. **Training use is a separate consent decision.** Aggregation and evaluation
   are platform operation. Building a preference dataset from user
   conversations is not, and it does not ride in on this spec's flag. Per the
   governance-via-identity-claims principle, that gate belongs at the claim
   level, decided before Phase 5 is scoped — not inferred from the fact that
   the rows exist.

## 9. Risks and the rules that answer them

| Risk | Rule |
|---|---|
| Someone publishes the raw rate as a quality KPI | Aggregate endpoints return **comparisons between arms**, and every response carries its `n` and coverage %. No single-number "quality score" field exists to be quoted |
| Sparse, self-selected, negative-skewed data | Treat as a sampler. Implicit signals (§10) carry the density; thumbs carry the intent |
| Feedback write mutates the cacheable prefix | Structural: `F#` is its own DynamoDB item. Nothing in this spec touches conversation history, `toolConfig`, or the system prompt. A design that puts feedback on the message object is rejected for this reason |
| An inline LLM judge is added "just to classify" | Batch/offline over persisted turns only. An inline per-turn model call on every session for the life of the platform is exactly what the cost tenet exists to stop |
| Two feedback affordances confuse users | The report link stays agent-scoped at the tail; thumbs are per-message. The down-thumb sheet hands off to the report dialog rather than duplicating it (§3) |
| Feedback becomes a void | Phase 1 ships the consequence with the button, or Phase 1 does not ship |

## 10. Implicit signals

Denser than thumbs, ~100% coverage, and free of UI cost:

- **Copy to clipboard** — already a click in `message-actions.component.ts`
- **Continue** on a truncated or interrupted response
- **Edit-and-resend** of the preceding user message
- **Abandonment** — session goes idle immediately after a response
- **Dissatisfaction in the *next* user message** — the strongest dense proxy,
  and the one that most needs the offline-batch rule from §9

These write the same `F#` row family with a `signal: "implicit"` discriminator.
Explicit and implicit must never be summed into one rate; they answer
different questions and have wildly different base rates.

## 11. Phasing

Sized so each phase is independently shippable and each earns the next.

**PR-1 — Capture + consequence.** `F#` row, `POST/DELETE
/sessions/{id}/messages/{message_id}/feedback` on **app-api** (per the
inference-api boundary rule), thumbs in `message-actions.component.ts`, the
reason sheet, and retry-with-correction. Uncomment and fill the `Feedback`
model at the two placeholder sites. Flag `RESPONSE_FEEDBACK_ENABLED`.
*User value on day one; no analytics build.*

**PR-2 — Implicit signals.** Copy / continue / edit-resend / abandonment into
the same row family. *Density.*

**PR-3 — Read model + admin panel.** Third leg on the anatomy query; a quality
panel beside the cost panels, joined to model / compaction / `agentSwitched` /
skills. *The attribution payoff.*

**PR-4 — Eval sampling.** Down-thumbed turns feed `EvaluationClient.run()`,
routed to evaluators by reason bucket. *Makes judging affordable.*

**PR-5 — Author and marketplace surfaces.** Per-version helpfulness rate,
regression alarm against the rollback path.

**Phase 6 (not scoped) — preference dataset.** Policy-gated per §8, and only
if PR-3 proves the signal is real.

## 12. Open questions

1. **Does the retry-with-correction path reuse `/steer` or need its own?**
   `/steer` targets a *running* turn via the lease row. Correcting a finished
   turn is a normal new turn carrying a structured preamble. Probably a
   different endpoint with the same UX — worth confirming before PR-1.
2. **Does a corrected retry write a linked pair?** It should, for Phase 6 —
   but that is the training-consent question, so PR-1 may need to record the
   link without recording the content.
3. **Coverage floor for publishing a comparison.** Below some `n`, an arm
   comparison is theatre. Pick the number before the panel exists, not after
   someone dislikes a result.
4. **Do preview sessions participate?** The `D#` write skips them
   (`metadata.py:156`); the `C#` cost write has **no such guard**, so the two
   prefixes already disagree — settle the rule for `F#` deliberately rather
   than copying whichever neighbour is read first. Agent Designer previews are
   exactly where an author would want to thumb their own work, but that data
   must never reach fleet aggregates.
