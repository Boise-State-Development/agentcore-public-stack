# Cost-Effectiveness Plan of Record

**For:** engineering · **Date:** August 5, 2026 · **Owner:** Phil Merrell
**Status:** living document — updated when a gate below is decided, a PR in the
arc merges, or a number moves. This is a *map*, not a fifth spec: each
workstream's authority lives in its own spec, and if this page disagrees with
a spec, the spec wins and this page gets fixed.

*Companion specs:* `compaction-over-threshold-cache-spiral.md` (#833) ·
`agent-cache-extra-tools-bypass.md` (#834) · `compaction-v2-versioned-prefix.md`
(#835) · `document-context-offload.md` + validation + evaluation (#836) ·
`quota-cooldown-windows.md` · `tool-search-token-bloat-strategy.md` ·
`session-workspace-tools.md` · `share-large-conversations-s3-offload.md`

---

## The tenet, restated as a plan

CLAUDE.md's token-cost tenet says: engineer against **waste**, never against
context, and verify — don't guess. The 2026 investigations turned that tenet
into measured findings: attachment conversations are 31% of model spend,
cold prefix re-writes ~25%, the fleet writes 2× what it reads from cache, and
one compaction spiral spent 90% of a user's monthly quota on cache re-writes.
Meanwhile the AICC platform report found **AgentCore Runtime memory is ~73% of
the total AWS bill** — the model-token arc below optimizes the *minority* of
spend, and the plan has to hold both levers in frame.

## North-star metrics (proposed — ratify at first review)

1. **Unexplained-waste share of model spend** = (partial-miss + avoidable-miss
   dollars, net of `agentSwitched` re-writes) ÷ total model spend. Today:
   still unknown, but no longer unmeasurable — #833 PR-1 built the instrument
   (`partial_miss` on every `C#` row, `PartialMiss`/`PartialMissUsd` EMF,
   `partialMissCount`/`partialMissUsd` session rollups). Baseline is the first
   full week of prod traffic after it deploys; note the numerator only counts
   calls written *after* deploy — nothing is backfilled, so the incident's own
   56 rows still read `hit`. Provisional target: **< 5%**. This is the number
   that says the *prefix-stability* and *payload* work is done.
2. **Cost per weekly-active-user, trend** — flat-or-down while usage grows.
   This is the number that says the platform scales. (Absolute totals are
   understated ~20% by legacy missing-cost rows — see the #836 validation —
   so trend, not level, is the signal.)
3. **Runtime-memory minutes per active session** — the infra lever's
   equivalent of metric 1. Baseline exists from the reaper work (post-#827
   microVM lifetimes 18–50 min vs 480–520 before).

## Five workstreams

| # | workstream | what it protects | authority | state |
|---|---|---|---|---|
| W1 | **Measurement** | every other row of this table | #833 PR-1 (`partial_miss`), cohort scan §4.1, dashboards #699/#700 | **PR-1 built** (`feature/partial-miss-cache-status`) — G0 clears on deploy; cohort scan §4.1 next |
| W2 | **Prefix stability** | don't rewrite what didn't change | #834 bypass fix → #833 PR-2/3/4 → #835 v2 (gated) | specs open, nothing built |
| W3 | **Payload boundedness** | nothing unbounded enters the prefix | #836 offload · tool-search strategy · workspace tools (PR-1 built) · S3 share-offload | offload PRs unbuilt; citations baseline probe required first |
| W4 | **Demand governance** | bound the blast radius of any failure | quota-cooldown spec · #833 PR-5 (earlier warnings, per-session notice) | drafted; PR-5 unbuilt |
| W5 | **Infrastructure economics** | the 73% of the bill that isn't tokens | reaper #827 (shipped) + open follow-ups: session-id forwarding, `StopRuntimeSession` | follow-ups un-specced — **gap** |

W4 is deliberately independent: it must protect users even when W1–W3 fail,
because the spiral incident showed a platform bug can spend a user's whole
quota (equity note: with a working cache that user's month was ~$10, not $30).

## Order of operations and decision gates

```mermaid
graph LR
  PR1["G0 · #833 PR-1<br/>partial_miss instrument"] --> BYP["#834 bypass fix"]
  PR1 --> TRI["#833 PR-2 summary cap<br/>+ PR-4 memory pinning"]
  BYP --> G1{"G1 · bypass causality:<br/>create_artifact<br/>single-builder experiment"}
  TRI --> SCAN["cohort scan<br/>(#833 §4.1)"]
  BYP --> SCAN
  SCAN --> G2{"G2 · compaction v2<br/>go / no-go (#835 §7)"}
  G2 -- go --> V2["build v2<br/>(absorbs #833 PR-3)"]
  G2 -- defer --> INV["v2 invariants become<br/>review criteria only"]
  PROBE["G3 · citations baseline probe<br/>(#836 eval §1)"] --> OFF["offload arms A→B→C"]
  PR1 --> OFF
  PR5["#833 PR-5 quota runway"]:::indep
  W5["W5 · reaper follow-ups<br/>(needs a spec)"]:::indep
  classDef indep stroke-dasharray: 5 5;
```

Gate summary — each is a *measurement with a decision attached*, not a date:

- **G0** — nothing else in W1–W3 is credibly evaluable before `partial_miss`
  exists. **Built**; the gate clears when it is deployed and has produced a
  baseline week, since an instrument nobody has read yet proves nothing. It
  also ships the first *per-session* alarm ($5 of partial-miss waste in 24h) —
  the fleet sums it sits beside never saw the incident that motivated any of
  this.
- **G1** — the bypass→cache-write causality is confounded in observational
  data (#834 §3 says so itself). The single-builder experiment settles it.
- **G2** — #835 §7 lists the falsifiable go criteria (cohort >3% of sessions
  or >15% of spend, or residual waste >$50/mo post-triage). Defer is a
  respectable outcome: the invariants persist as review criteria.
- **G3** — no citations config is sent in prod today, so we don't know what
  the current document path can even see. Every offload quality comparison
  inherits its baseline from this probe.

Independent of all gates: #833 PR-5 (quota runway) and the W5 follow-ups —
cheap, and they don't wait on measurement.

## Shared assets — build once, name an owner

- **The quality-veto eval harness.** Three specs describe it (#833 §4.3, #835
  §8, #836 eval §2); it must be built **once** — synthetic corpus + paired
  per-family tasks + arm runner — with offload's version (the most fully
  designed) as the base and the long-session continuity families added for
  compaction. Unowned today; assign before any W2/W3 build PR merges.
- **The replay harness** (#833 §4.2): deterministic re-run of a session's
  event stream through an arm, asserting predicted vs. actual cache
  reads/writes per turn. Same owner as above.
- **The cohort re-scan scripts** from the #836 validation (content-free
  projections, both cost-row shapes handled). Denominator rule from that
  validation applies to every number anyone quotes: state it once, use it
  consistently.

## Known tensions the specs don't resolve (umbrella-level watch list)

1. **1h Bedrock TTL vs. compaction v2's scheduler.** Blanket 1h TTL measured
   as a net cost *loss* (+12.3%); v2's "advance when cold" invariant gets more
   valuable with longer TTLs. If v2 goes ahead, re-run the TTL analysis with
   v2's advance cadence in the model — the answer may flip for the
   over-threshold cohort specifically.
2. **Upstream drift.** W2/W3 lean on Strands' newest module
   (`conversation_manager/compression/`) and an open upstream cachePoint
   lookback issue (harness-sdk#3348). Exact pins protect us; every Strands
   bump that touches conversation management re-runs the eval harness.
3. **W3 can hide W2 regressions** (and vice versa): offloading payloads
   shrinks the prefix that instability re-writes, making W2 waste look
   smaller without being fixed. Arm-separated attribution (#833 §4.2) exists
   precisely for this — never quote a combined number as a single fix's win.

## Review cadence

This page is reviewed at the **Friday kaizen cycle** (research → review-prep
already scan internal signals weekly): gates decided, metrics updated, the W5
spec gap tracked until closed. Any PR that merges in the arc updates its row
here in the same PR.
