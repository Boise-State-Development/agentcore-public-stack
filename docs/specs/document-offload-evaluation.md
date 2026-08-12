# Evaluating document-context offload — quality, tokens, cost

**Status:** Draft, companion to `docs/specs/document-context-offload.md` (§6)
**Validation basis:** `docs/specs/document-context-offload-validation.md` —
every prod number cited here was independently re-derived 2026-08-03.

**The guardrail, stated first.** Per the repo tenet (`CLAUDE.md`, the
token-cost bullet): *when cost and answer quality genuinely conflict, quality
wins — the goal is the cheaper path to the **same** quality, not a cheaper
answer.* Operationally: the quality gate in §2 is a **veto**. No cost result,
however good, ships a confirmed quality regression; the permitted responses to
a quality regression are widen pinning, fatten the digest, or abandon (§4).
This design's job is to be able to *detect* that conflict, which is why the
quality axis gets the most machinery.

---

## 1. What is actually being compared

Three system states, not two:

| arm | restore path (turn 2+ after rebuild) | steady-state prefix |
|---|---|---|
| **A. today (prod)** | contentless placeholder | full document until the strip fires |
| **B. PRs 1–3** | digest + `document_read` | full document (no offload trigger yet) |
| **C. PRs 1–4** | digest + `document_read` | digest + retrieval after offload |

A→B is the *correctness* comparison (quality should go **up**; cost roughly
flat). B→C is the *cost* comparison (cost should go down; quality must not).
Evaluating only A→C conflates them: a quality win from fixing the strip could
mask a quality loss from the offload, and would misattribute both. Every
experiment below names which comparison it serves.

**Required baseline experiment (before any scoring).** The validation pass
found that **no citations config is sent today**, and on Bedrock the visual
(page-image) PDF path is tied to citations-enabled document handling. So we do
not currently know whether arm A even sees charts. Run a 10-question probe
(chart-only PDF, table-in-image PDF, scanned page) against arm A twice — bare
document block, and with `citations: {enabled: true}` — and record what the
model can actually answer. Every subsequent quality comparison inherits its
definition of "full fidelity" from this probe, and the offload spec's
"dual-encoded" premise is unverified until it runs.

---

## 2. Answer quality

> **Amended 2026-08-12 — what to build vs. what to adopt.** This section is the
> base design for the harness shared with #833 §4.3 and #835 §8. A spike
> (`agentcore-evaluations-spike-findings.md`) verified against dev-ai that the
> managed `bedrock-agentcore` evaluation service — available on our existing
> pin, authorized, no infrastructure — supplies a usable third of it. Read the
> spike's §3 before writing a runner. The split, for this section:
>
> - **Adopt:** the tool-trajectory families (`Builtin.TrajectoryInOrderMatch`,
>   `Builtin.ToolSelectionAccuracy`, `Builtin.ToolParameterAccuracy`) score the
>   `document_read` health band in §3.1 directly and for free. Consider
>   `Builtin.Faithfulness` — "is the response supported by the provided
>   context" is close to this spec's actual question, *did the digest lose
>   something the document had* — as a candidate primary for the holistic
>   family, or at minimum as the second judge §2.4 asks for on the 20% sample.
> - **Build:** the arms, the k=3 replication, the corpus and planted facts, the
>   programmatic lookup/citation scoring, the statistics — and the blinded
>   holistic judge, for the reason in §2.4.

### 2.1 Critique of the spec's ~30-task design, and what replaces it

The shape (holistic / lookup / citation) is right and maps onto the failure
modes offload can cause. The size is not defensible as a gate: with 30 paired
tasks scored pass/fail, McNemar at α=0.05 / 80 % power only detects a
regression of roughly ≥25 percentage points. That detects breakage, not the
"subtle but critical context loss" the spec itself warns about. Fixes, in
order of leverage:

1. **Scale tasks, not effort per task.** Lookup and citation tasks are
   programmatically generatable and scorable (planted facts, known pages), so
   n is cheap there. Target: **120 tasks** (40 holistic / 50 lookup / 30
   citation) over ~20 documents. At 120 paired binary outcomes, detectable
   regression drops to ~10–12 points; holistic tasks scored on a rubric add
   sensitivity via continuous scores.
2. **Replicate generation, not just tasks.** k=3 samples per (task, arm) at the
   production temperature; score all three. Generation variance is otherwise
   indistinguishable from arm effects at this n.
3. **Score paired, report per-family.** Offload's predicted failure signature
   is family-specific (holistic and citation degrade, lookup holds). A pooled
   score would dilute exactly the signal the spec says matters.

### 2.2 Documents — provenance without touching user content

Never prod user files. Two sources, both matched to the measured prod corpus
shape (mime mix 290 PDF / 172 DOCX / 185 image / 54 text-family per the
validation scan; upload p50 ≈ 0.2 MB, p90 ≈ 2.2 MB; include several 40+ page
documents since >100k-token peaks are 4× more common in attachment sessions):

- **Public BSU corpus** (realism): policy PDFs, the academic catalog, job
  descriptions, award nomination forms, financial-report PDFs — the same
  genres the offload spec names. Scrape from public boisestate.edu pages.
- **Synthetic planted-fact corpus** (ground truth): generated DOCX/PDFs with
  known facts at known pages, tables with known cells, figures with known
  captions, and **deliberately planted internal contradictions** for holistic
  tasks. Render PDFs with real layout (tables, charts as images) so the
  dual-encoding question is exercised — a text-extractable-only synthetic
  corpus would hide exactly the regression we fear. Include chart-only pages
  whose answer is unreachable from any text layer.

### 2.3 Task families, with the two stack-specific risks built in

- **Holistic** (A/B/C): summarize; compare two documents; find the planted
  contradiction. Scored by rubric (coverage of gold key-points, correctness,
  no fabrication).
- **Lookup**: single fact; **table cell**; **figure/chart caption and
  chart-value questions**. The last two are the *dual-encoding canaries*: the
  digest is text-only, so a correct answer in arm C requires the model to call
  `document_read` and get **native blocks** back. If `document_read` returns
  flattened text for DOCX (as specced) the DOCX-table tasks will show it.
  Scored by exact/normalized match — no judge needed.
- **Citation**: does the answer cite the right page. Two sub-checks the spec
  misses:
  - *Page-identity under reassembly*: `document_read(page_range={4,7})`
    returns a re-assembled sub-document whose internal pages are 1–4. A native
    citation against it will say "page 2" meaning original page 5. The tool
    must remap (or embed original page labels); this task family is the test
    that it does.
  - *Citations only exist if the config ships*: arm A scores here reflect the
    §1 baseline probe; if citations aren't enabled in prod yet, the citation
    family gates B/C against each other, not against A.
- **Restore/turn-2 family** (the PR-3 fix, A→B): two-turn conversations run
  with a spreadsheet tool enabled (forcing the cache bypass, hence a rebuild
  between turns): attach + ask on turn 1, follow-up question on turn 2. Arm A
  should fail these near-totally (the placeholder has zero content) — which
  doubles as a harness canary: if arm A *passes*, the harness isn't actually
  exercising the restore path.
- **Pinning-boundary family** (B→C): ask about the document on the attach
  turn, digress for two turns (so it unpins and offloads), then ask a holistic
  question. This is where "pinning too narrow" shows up first.

### 2.4 Scoring, blinding, agreement

- **Lookup/citation:** programmatic scoring against planted ground truth.
  Blinding is moot; bias-free by construction.
- **Holistic:** LLM judge, pairwise A-vs-B per task, both orders (positions
  swapped), judged against the gold key-point list — not free-form preference,
  which is where self-preference bias lives. **Blinding is a real problem the
  spec hand-waves:** arm C transcripts contain `<document-digest>` blocks and
  `document_read` tool calls — an instant giveaway. The harness must strip
  transcripts to *final answer text only* before judging, and a scrubber must
  verify no digest/tool artifacts survive (grep for the digest tag and tool
  names; reject the sample into manual review if found).

  ⚠️ **This requirement rules the managed evaluation service out for the
  holistic judge — structurally, not as a gap to work around** (added
  2026-08-12). AgentCore Evaluations works by handing the evaluator the whole
  collected span set; content-in-spans *is* the mechanism, and there is no
  scrubbing seam between collection and judging. The blinded pairwise judge
  must therefore be ours, over scrubbed final-answer text. Everything else in
  §2 can go through the service. Do not design around a `ReferenceInputs`
  field or a custom evaluator to recover blinding — the transcript reaches the
  judge regardless of what ground truth is attached.
- **Consistency checks:** (1) each pair judged in both orders — an
  order-flipped verdict is recorded as a tie; (2) a second judge model on a
  20 % sample, report inter-judge agreement; (3) human (Phil or delegate)
  scores a 20 % sample blind, target κ ≥ 0.7 vs the judge — below that, the
  rubric is rewritten before results are read; (4) 5 % of tasks duplicated
  verbatim as self-consistency probes for the judge.
- **Detectable effect, stated honestly:** with 120 tasks × 3 replicates,
  binary families resolve ~10-point differences; the holistic rubric resolves
  ~0.4 SD. The ship gate is **non-inferiority**: arm C within δ = 5 points of
  arm B per family at 90 % CI is the *goal*, but at this n a true 5-point
  regression can evade detection — which is why the per-family canaries
  (chart-lookup, pinning-boundary, citation-page-identity) exist: they are
  designed so the predicted failure modes produce *large*, detectable effects,
  not diffuse small ones.

---

## 3. Token efficiency

### 3.1 Metrics and exactly where each is read

| metric | source | comparison |
|---|---|---|
| **cacheWrite:cacheRead token ratio** per attachment session (primary) | `C#` rows, `tokenUsage.cacheWriteInputTokens` / `cacheReadInputTokens` | B vs C cohorts |
| cacheWrite tokens per session, and `cacheWriteCost` share of session cost | `C#` `tokenUsage` + `cost` map (map-shaped rows only — floats carry no split; ~6 % of spend, footnote it) | B vs C |
| steady-state prefix size | `contextWindow` (written per call by `stream_coordinator`) trajectory within session | B vs C |
| offload events fired, tokens evicted per event | **new**: counter + EMF emitted by the PR-4 trigger; also stamp an `offloadEvent` marker field on the next `C#` row | C only (mechanism check) |
| `document_read` calls per attachment session, pages per call, mode (range/pattern/full) | **new**: EMF from the tool, mirroring the workspace-tools pattern | C health band: ~0.3–3 calls/session; ≈0 ⇒ digest answering unaided (suspicious — cross-check quality); >3/turn ⇒ digest too thin |
| re-upload rate, **byte-identical duplicates only** (per validation: 87 % of dup groups are size-identical; that subset is the loss signal, size-differing ones are revisions) | uploads table, (sessionId, filename, sizeBytes) groups | A vs B — this is the one metric PRs 1–3 must move on their own |

Explicitly **not** trusted, per the prior findings this spec inherits:
`wastedUsd` and the `AvoidableMiss` alarm are blind to the dominant waste mode
(`cacheStatus="hit"` masks partial-prefix misses), and `cacheStatus` counts are
therefore secondary color, not endpoints. The write:read *token ratio* is the
metric that actually moves.

### 3.2 Comparison design — how workload mix is kept out

Plain before/after is disqualified by the validation findings themselves
(missing-cost rows cluster in specific months; traffic mix drifts; July has
13× March's rows). Instead:

1. **Randomized per-session flag.** Extend `DOCUMENT_OFFLOAD_ENABLED` from a
   boolean to a percentage rollout keyed on `hash(session_id) % 100` (the
   standard default-on-with-kill-switch pattern, plus a bucket knob). Arm B =
   flag off, arm C = flag on, running **concurrently**. Randomization at the
   session level removes user- and time-mix confounds outright — this is the
   lesson of the bypass spec's confounded §3 cohort comparison, and of its
   proposed `create_artifact` experiment, which validation showed would treat
   only 53 self-selected sessions. Do not repeat either shape here.
2. **Cohort hygiene:** attachment sessions only; exclude tabular-only sessions
   (different mechanism, per §7 of the offload spec); stratify reporting by
   {PDF, DOCX/text, image, mixed} and by model id. Sessions that never had a
   rebuild or a >5 min gap can't benefit and dilute — report the treated-
   opportunity subset (≥1 cold-eligible turn) alongside intent-to-treat.
3. **A→B (PRs 1–3 ship together, unflagged):** this one is before/after by
   necessity. Use difference-in-differences with non-attachment sessions as
   the control series to absorb temporal drift, and report the re-upload rate
   (byte-identical definition) weekly: 14 % → <3 % is the spec's own success
   line, and it should start moving within days of deploy if the recovery path
   reaches users (if it doesn't, check the tool gate first — the
   `workspace_files` grant lesson).

One measurement caveat to carry: turn-1 of every non-PDF attachment writes its
document into the cache one turn late (the leading-document cache-point rule
found in validation §claim-2). This inflates turn-2 cacheWrite in *both* arms
equally under randomization — harmless for B vs C, but it will make absolute
write:read ratios look worse than the offload can fix; don't chase it as a
regression.

---

## 4. Cost effectiveness

### 4.1 Is −15 % well-founded?

The measured envelope: attachment-session spend $233–249 (denominator
convention per validation claim 1), of which cold-write cost on >5 min-gap
turns is **$58–66 (~25–28 %)**. But the offload recovers only the *document
share* of each cold re-write — history, system prompt, and tool config still
re-write in full — minus whatever pinning deliberately keeps inline, and minus
new spend it introduces (digest tokens every turn ≈1.5k, `document_read`
retrievals, Haiku extraction off-path). The document share of attachment-
session prefixes is **currently unmeasured** (no `documentTokens`
instrumentation exists — that is PR-7).

So: −15 % is *plausible* (it requires documents ≈ 55–60 % of cold-write bytes
net of pinning and digest overhead) but it is a guess sitting on an unmeasured
quantity. Two consequences:

- **Land PR-7's `documentTokens` before or with the PR-4 flag**, and restate
  the target as derived: expected recovery ≈ `0.6 × documentShare × 25 %` of
  attachment spend. If measured documentShare comes back at 30 %, −15 % was
  never achievable and a −5 % result is a *success*, not a miss — without the
  measurement, that outcome would read as failure and kill a good fix.
- Power: session cost is heavy-tailed (median $0.16, mean $0.62). Compare on
  log-cost (or Mann-Whitney), and expect to need **≥150 attachment sessions
  per arm** (≈4 weeks at ~50/week prod rate, so plan for 6–8 weeks or
  supplement with replayed synthetic sessions in dev for a faster directional
  read).

### 4.2 Stopping rule

Evaluate at 150 sessions/arm or 8 weeks, whichever first. Primary endpoint:
attachment-session cost per session (log scale), arm C vs arm B. Secondary:
write:read ratio, quality gate (§2), `document_read` health band.

- **Ship (flag to 100 %):** cost −10 % or better at 90 % CI *and* quality
  non-inferior per §2 *and* `document_read` in band *and* offload events
  actually firing (mechanism check — a win with zero offload events is a
  confound, not a result).
- **Widen pinning (iterate, don't ship or kill):** quality regression
  concentrated in holistic / citation / pinning-boundary families, or
  `document_read` >3 calls/turn. Widen pinning or raise the digest budget,
  re-run the quality gate only (cost re-check follows automatically from the
  running flag). Two iterations maximum — the tenet says quality wins, and it
  is satisfied by *paying* for quality, not by shipping the regression.
- **Abandon (or re-scope):** cost improvement <5 % with quality flat *and*
  offload events confirmed firing — the prefix is dominated by non-document
  content, and the recoverable envelope was mis-attributed. The money then
  says: keep PRs 1–3 (they're a correctness fix and cost-neutral), drop PR-4,
  and put the effort into the `extra_tools` cache-bypass fix, whose cohort
  carries ~95 % of spend. Also abandon if two pinning-widening iterations
  can't clear the quality gate: that is the "cost and quality genuinely
  conflict" case, and quality wins.

### 4.3 What this design can and can't conclude

It can causally attribute cost movement to the offload (randomized flag), can
detect the *predicted* quality failure modes at practical n (targeted
canaries), and can distinguish "fix works" from "envelope was misjudged"
(mechanism counters + documentTokens). It cannot detect a diffuse ≤5-point
quality regression across all families at n=120×3 — that residual risk is
accepted explicitly and bounded by the post-ship signals (re-upload rate and
`document_read` anomalies both regress quickly if users are silently getting
worse answers).
