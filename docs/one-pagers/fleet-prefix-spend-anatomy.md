# Where the model spend actually goes — fleet-wide, all conversations

**Measured:** 2026-08-05, prod (`boisestateai-v2-sessions-metadata`, account
897729136999) · **Method:** `backend/scripts/scan_fleet_prefix_spend.py`
(read-only, content-free) · **Owner:** Phil Merrell

**Why this exists.** Every cost investigation on this platform so far entered
through a specific door — attachment conversations (#836), one compaction
spiral (#833), the agent-cache bypass (#834) — and each one sized *its own
cohort*. None of them ever asked the flat question: across **all**
conversations, short and long, where do the dollars go? This is that question,
answered once, so the roadmap can rank work against a spend anatomy instead of
against whichever cohort was measured most recently.

**Denominator, stated once and used throughout:** 18,907 model-call (`C#`)
rows carrying a cost, across 3,505 sessions. A further **2,299 rows (10.8%)
carry no cost attribute at all** — they are *unknown*, not $0, and are
excluded from every figure below. (Session rows show the same gap: 760 of
3,808, 20.0%.) This is the understatement the #836 validation caught; it has
not been fixed, so treat every absolute total here as a floor.

---

## 1. The anatomy

| component | spend | share |
|---|---|---|
| **cache write** | **$461.57** | **55.2%** |
| output | $165.49 | 19.8% |
| input (uncached) | $146.64 | 17.5% |
| cache read | $63.03 | 7.5% |

Total recorded model spend: **$888.35** (17,820 of 18,907 rows carry a cost
breakdown; the rest carry only a total).

**More than half of every dollar is spent writing cache entries.** In *tokens*
the ratio looks benign — 289.9M read against 186.4M written, a 1.6:1
read:write — but a written token costs roughly 12× a read one, so the dollar
picture is governed by how often the prefix is re-written rather than by how
much of it is read.

⚠️ **55% is not 55% waste.** At Bedrock's pricing a cached prefix breaks even
after roughly 0.3 reads, and the fleet reads 1.6× what it writes, so prompt
caching is net-positive overall. The waste is in the *re-writing*, quantified
in §3.

## 2. Prefix mutation happens in ordinary conversations

Across 2,483 sessions with more than one model call (15,402 call-to-call
transitions):

| prefix component | transitions where it changed | sessions affected |
|---|---|---|
| `systemPromptHash` | 703 (4.6%) | **305 (12.3%)** |
| `toolConfigHash` | 96 (0.6%) | 69 (2.8%) |

`toolConfigHash` holding at 0.6% says the prompt-cache ordering contract is
largely working. The mover is the **memory-augmented system prompt**, which
re-retrieves UserPreference / SemanticFact records per turn — including
records extracted from the conversation in progress. $53.39 of cache-write
spend landed on the call immediately following such a flip.

That is `#833` **D4 / PR-4**, which the spec ranks as "small next to D2/D3"
because it was judged inside a single incident. Fleet-wide it touches **one
multi-turn conversation in eight**, and it is the cheapest fix in that epic.

## 3. The north-star metric, measured for the first time

Running #838's `partial_miss` predicate offline over historical rows:

- **215 calls classified, $52.14 of avoidable waste — 5.9% of total model
  spend**, across 63 sessions.
- **206 of those 215 rows say `hit` today.** This is exactly the blind spot
  #838 was built to close, quantified before #838 has reached prod.
- Concentration: `c94a3172…` (the compaction-spiral incident) is $25.56 of the
  $52.14. The remaining $26.58 is spread across 62 other sessions.

The roadmap's provisional target for unexplained-waste share is **< 5%**. This
reads **5.9%**, and it is a **floor** for three reasons:

1. It compares each call only to its immediate predecessor, where the shipped
   classifier uses a 10-row same-prefix lookback.
2. Rows written before #697 carry no fingerprints and cannot be classified
   either way.
3. The 10.8% of rows with no cost contribute nothing.

**This does not clear G0.** It is an offline reimplementation over historical
rows, not a reading of the shipped instrument. G0 still clears on a prod
baseline week after #838 releases — but "we cannot know until then" is no
longer true, and the direction is now evidence rather than assumption.

## 4. Cost is not concentrated where the arc has been looking

| calls in session | sessions | spend | write:read (tokens) | sys-prompt flips /1k transitions | partial-miss waste |
|---|---|---|---|---|---|
| 1 | 1,022 | $35.17 | **5.23** | — | $0.00 |
| 2–5 | 1,588 | $138.02 | 0.82 | 31 | $0.98 |
| 6–15 | 656 | $209.93 | 0.49 | 39 | $3.73 |
| 16–40 | 202 | $279.25 | 0.53 | 51 | $7.35 |
| 41+ | 37 | $225.96 | 0.72 | 69 | $40.08 |

Two readings, and they point in different directions on purpose:

- **$383 of $888 — 43% — sits in conversations of 15 calls or fewer**, which
  no item in the current arc addresses.
- **Prefix instability rises monotonically with length** (31 → 69 flips per
  1,000 transitions) and partial-miss waste concentrates almost entirely in
  the 41+ bucket. The long-conversation work is where the *concentration* is;
  it is not where the *generality* is.

### 4a. Cache written and never read

**700 sessions wrote cache tokens and read none back — $33.92, or 7.3% of all
cache-write spend, returning nothing.** Single-call sessions are 29% of all
sessions and spend **67% of their money on cache writes they can never use**
($23.49 of $35.17).

It is almost entirely a short-conversation cost: $22.68 of it in single-call
sessions, $10.73 in 2–5-call sessions, and $0.51 in everything longer. The
mirror image of the partial-miss column, which is $40.08 of $52.14 in the 41+
bucket — the two failure shapes sit at opposite ends of the length
distribution, and an arc that only looks at long conversations sees one of
them.

Some of this is structural: a conversation that ends after one turn cannot
amortize a write, and nothing knows in advance that it will end. But nothing
in the arc has ever examined **cachePoint policy for first or short turns**,
and at 29% of sessions it is not a rounding error. This is a *new* work item,
not a re-ranking of an existing one.

## 5. What this changes

1. **PR-4 (memory-prompt pinning) is a general lever, not a footnote.** One
   multi-turn conversation in eight; cheapest fix in #833.
2. **cachePoint policy for short/first turns is unowned and unspecced.**
   ~$34 returning nothing at this traffic level.
3. **Cohort share is the wrong ranking function.** #835 §7's G2 criteria ask
   "is the cohort >3% of sessions" — a volume test for defects that are driven
   by conversation *age* and by per-turn prefix behavior. Applied to the
   numbers above, that gate would defer work whose mechanism is fleet-wide.
4. **The missing-cost gap (10.8% of calls, 20.0% of sessions) is now load-
   bearing.** Every total on this page is a floor because of it. Worth its own
   fix before the next round of numbers gets quoted.

## Caveats

- Offline reimplementation of #838's predicate; not the shipped classifier's
  output. Treat §3 as directional and as a floor.
- Fingerprints exist only on rows written since #697.
- Single point in time, and — per Phil, 2026-08-05 — **this is the lowest
  traffic the platform will ever see**. Rates (share of transitions, share of
  sessions) generalize; absolute dollars do not.
- Prompt caching remains net-positive fleet-wide. Nothing here argues for
  fewer cachePoints in long conversations.
