# Scoping the quality-veto harness — what it is, what already exists, and the cheapest first slice

**Status:** Slice 1 BUILT 2026-09-25: `backend/scripts/compaction_quality_harness.py` (+ `compaction_quality/`, tests in `backend/tests/test_compaction_quality_harness.py`). The open questions in §6 are answered in §7. The missed-free-apply fix (§7.4) has landed with it, so the first full run can go ahead.
**Prompted by:** the two waivers recorded 2026-09-21 (`compaction-model-relative-thresholds.md` §5,
`document-offload-evaluation.md` §2), both of which name "build the harness" as trigger 1 — the only
path to an answer that does not wait on user volume.
**Design is not in scope here.** It is already written, in three places:
`compaction-over-threshold-cache-spiral.md` §4.3, `document-offload-evaluation.md` §2.1–2.4, and
`agentcore-evaluations-spike-findings.md` §2–3. This document does not redesign them. It answers the
question those specs never did: **what is the smallest thing we can build that produces a real
number, and in what order.**

---

## 1. Why it never got built, stated plainly

It is the one deliverable in either epic that ships no user-visible behaviour, and it lost to every
PR that did. Both specs assumed a single harness serving both, which made it one large project with
no owner rather than several small ones with obvious first steps.

The cheap substitute is now gone too. `GET /admin/feedback/fleet` already computes the comparison
both specs want (`arms.turnClass`, bucketed full > retrieved > digestOnly) — and returns nothing,
because prod carries **13 `F#` rows, one of which is a thumb**, against a floor of 20 per arm across
three arms. That is measured, 2026-09-21, and it is why this is worth scoping rather than deferring
again.

---

## 2. The finding that changes the sequencing

The specs and the spike both describe one harness driving live multi-turn sessions through the
deployed runtime, for both epics. **That is right for offload and substantially wrong for
compaction**, and nobody noticed because the two were always scoped together.

### 2.1 The compaction cut is a pure function — no runtime, no deploy, no env change

`CompactionPolicy.resolve(config, context_window)` and `choose_checkpoint(messages, cutoffs,
protected_turns, floor_tokens, history_tokens)` (`session/compaction_policy.py:77,199`) take a
`CompactionConfig` **object** and a message list. They read no environment at call time. So both arms
can be constructed in-process:

```python
relative = CompactionConfig(model_relative_enabled=True,  ceiling_ratio=0.5, floor_ratio=0.25, ...)
fixed    = CompactionConfig(model_relative_enabled=False, token_threshold=...)
```

Apply each to the *same* synthetic transcript, and the two cut points fall out deterministically.
No session, no runtime, no quota, no 60 live turns. The model is needed only for (a) generating the
bounded summary once per arm (`compaction_summary.bound_summary`, a Nova Micro call) and (b) one
answer per (task, arm) on the resulting history.

**What this removes:** the dominant cost of the compaction half. The spec's shape — "simulate long
editing sessions (30–60 turns)" — reads as 60 live turns × 2 arms × k=3. It does not have to be. A
synthetic transcript with planted constraints is *authored*, not generated, which is also the only
way the planted facts are known ground truth.

### 2.2 The offload arms genuinely need the runtime

The thing under test is whether the model **calls `document_read` when it needs to** and answers
correctly from what comes back. That is the agent loop, not a context shape you can assemble offline.
Arms B and C need real sessions.

### 2.3 ⚠️ The arm runner generalizes on transport, NOT on the arm mechanism

The spike says *"extend `experiment_agent_cache_arms.py`, don't rebuild it."* Half right — and the
half that is wrong is the half that matters. Read before planning around it:

| Part | Generalizes? |
|---|---|
| `run_arm()` — N sequential turns in one session via `run_agent_headless` | **Yes.** Prompts are a module constant; a task corpus is a parameter change. |
| `attach_cost_rows()` — reads `C#` rows back via `SessionLookupIndex` | **Yes**, directly. This is the token/cost instrument for any arm. |
| `attach_agent_cache_outcomes()` — CloudWatch log scrape | Agent-cache specific. Ignore. |
| **The arm definition** — `ARMS` is a dict of `enabled_tools` lists | **No.** This is the blocker. |

That script's arms differ by `enabled_tools` precisely because its docstring says *"the arms need no
redeploy"* — a deliberate trick for that experiment, not a general mechanism. `run_agent_headless`
(`apis/shared/harness/runner.py:136`) exposes `model_id`, `rag_assistant_id`, `enabled_tools`,
`agent_type`, `inference_params` — mirroring `InvocationRequest` — and **nothing else**. There is no
compaction or document knob on it.

Consequences, per epic:

- **Compaction**: `CompactionConfig.from_env()` reads
  `AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO` / `_FLOOR_RATIO` /
  `COMPACTION_MODEL_RELATIVE_ENABLED` from the inference-api runtime environment. Two arms on one
  runtime is **not possible** through the live path. §2.1 is the way out: don't use the live path.
- **Offload, B vs C**: `offload_enabled_for()` buckets on `crc32(session_id) % 100`, so the PR-4 arm
  **can** be selected per session by choosing session ids that hash into or out of the bucket — no
  deploy. Genuinely useful, and free.
- **Offload, A vs B**: `DOCUMENT_REHYDRATE_ENABLED` / `DOCUMENT_READ_ENABLED` /
  `DOCUMENT_DIGEST_ENABLED` are plain global env flags. A-vs-B needs a runtime env change between
  arms — sequential arms, not concurrent, with the temporal-drift caveat that implies.

---

## 3. The slice that needs no judge

Both specs bury the same fact under their holistic-judge designs: **two of the three families in each
are programmatically generatable *and* programmatically scorable.**

| Spec | Judge-free families | Needs a judge |
|---|---|---|
| Compaction (spiral §4.3) | constraint retention, reference lookup | revision continuity (rubric) |
| Offload (eval §2.1) | lookup, citation | holistic (rubric, blinded) |

Planted constraints, known decisions, known page numbers — scoring is exact match against ground
truth we authored. Both specs say so directly ("programmatically generatable and scorable… so scale
n there"), and both then put the expensive third family first in the write-up, which is why the whole
thing reads as judge infrastructure.

It is also where the statistical power lives. The spiral spec's own sizing note: ~40 paired binary
tasks detects only a ~25-point regression; ≥100 is what makes a subtle regression visible, and ≥100
is affordable **only** in the programmatic families.

**So slice 1 needs: no blinded judge, no scrubber, no AgentCore Evaluations, no LLM-judging spend.**
A corpus, a cut, and exact-match scoring.

---

## 4. Proposed sequence

### Slice 1 — compaction, constraint retention + reference lookup (offline)

The whole slice runs on a laptop against pure functions plus two model calls per arm.

1. **Corpus** (`backend/scripts/` + a fixtures dir): author a parameterised 40–60 turn editing
   transcript. Constraints planted at known turns ("never change the thesis wording", "citation style
   X"), decisions stated once at a known turn. Generate ≥100 task instances by varying plant position,
   constraint type, and the turn the question is asked at. Deterministic, seeded.
2. **Arms**: two `CompactionConfig` objects. Apply `resolve` + `choose_checkpoint` to the same
   transcript; produce two post-cut histories. Generate each arm's bounded summary once via
   `bound_summary`.
3. **Ask**: one Converse call per (task, arm) on the post-cut history. k=3 at production temperature.
4. **Score**: exact match / containment against the planted ground truth. Paired, per family,
   McNemar. Report `n` per family; no pooled score.

**Output**: the first quality number either epic has ever had, and the thing that either deletes the
compaction waiver or fires its veto.

### Slice 2 — offload, lookup + citation (live, B vs C only)

Reuses slice 1's scoring and statistics; adds the corpus of documents with planted facts on known
pages, and the live arm runner. B-vs-C via crc32 session-id selection — **no deploy**. Per the
readout's prediction that PR-4 fires ≈0 times, run the mechanism check first: if `document_offload`
events are zero, this slice is measuring nothing and should stop.

### Slice 3 — A vs B, and the rubric families

Only after 1 and 2 have produced numbers. This is where the runtime env change per arm, the blinded
pairwise judge, and the scrubber live — i.e. all the infrastructure, deferred behind two slices that
do not need it. ⚠️ Blinding cannot go through AgentCore Evaluations: arm C transcripts carry
`document_read` calls and `<document-digest>` blocks that give the arm away, and feeding the evaluator
whole spans *is* the mechanism — there is no scrubbing seam (spike §2). This judge is ours or it is
not blinded.

⚠️ **Score A→B and B→C separately** whenever both are in play. Per the offload spec §5, PRs 1–3 are a
*correctness* fix where quality should go **up**, and only PR-4 is the cost trade. Pooling lets a
strip-fix win mask an offload regression.

---

## 5. What this is not

- **Not a replacement for the thumbs.** Different question. This measures a controlled arm difference
  on a synthetic corpus; `F#` rows measure real users on real work. The waivers' trigger 2 stands.
- **Not prod data.** Spike §2's scoping note applies in reverse here: the corpus is ours and contains
  no user content, which is exactly why the `body` payload reaching an AWS-managed evaluator is a
  non-issue. **If anyone later points this at recorded production conversations, that is a new
  decision** — do not let it happen by reusing the script.
- **Not a fleet quality score.** Per response-feedback spec §9, every number is a comparison between
  arms with its `n`, or it is not reported.

---

## 6. Open questions for whoever picks this up

1. **Does the authored transcript reproduce the cut the real path takes?** Slice 1 stands or falls on
   this. `choose_checkpoint` rescales per-message estimates to `history_tokens`, so the synthetic
   transcript must supply a realistic measured history size, not just realistic text. Verify against
   one real session's `C#` rows before trusting any slice-1 number.
2. **Is `bound_summary` reachable offline** without the session manager's surrounding state? It is
   `async` and takes a model; it looked self-contained but was not exercised.
3. **What is the control for compaction?** The spec says "the fixed-threshold arm." Confirm that
   `model_relative_enabled=False` reproduces pre-#1125 behaviour exactly, rather than approximately —
   if it does not, the control is a third policy and the comparison means something narrower.

---

## 7. Slice 1 as built (2026-09-25)

### 7.1 Shape

`cut` (free) → `records` / `ask` (spend, estimate first, `--yes` to run) → `score` (free).

- The corpus is seeded and authored. It is 48-turn grant-proposal editing
  sessions with 9 facts planted per transcript, across four families:
  `constraint`, `decision`, `reference` and `superseded`. `superseded` is new,
  and fails an answer that repeats the old value.
- The arms are `full` (the control), `model_relative`, `legacy` and `floor_50`.
  Each is an explicit `CompactionConfig`. Nothing is read from the environment.
- The paces are `restore` (rebuild every turn with a cold cache), `cold` (a
  warm agent with every gap past the TTL) and `warm` (every gap inside it).
- The summary modes are `fallback` (production's first-line summary), `none`,
  and `records`. `records` stands in for AgentCore's summary records: one
  model-written record per 8 turns, lagging one turn. It is an approximation,
  so label any number it produces.
- **Nothing reimplements the cut.** Each arm drives a bare
  `TurnBasedSessionManager` through `update_after_turn`,
  `apply_pending_compaction` and, on the restore pace, `_apply_compaction`.
  Only the I/O is stubbed: DynamoDB, LTM retrieval, EMF and the clock.
- `cut` also writes a free **availability** table: whether each planted value
  is still anywhere in the probe-time context. That is an upper bound on what
  any model can answer.
- `ask` puts a cachePoint after the shared history, so repeat questions on a
  history are cache reads. The dev smoke run wrote 311k tokens and read
  2.49M.

### 7.2 Answers to §6

1. **Realistic cut?** It is sized so that one 48-turn session crosses the
   100k ceiling once or twice, and so that the full history still fits a 200k
   window. Compare it against the prod readout (§7.4): prod cuts retained a
   median of 37k tokens. The harness lands under 25k because authored turns
   are uniform. Prod's deeper tail comes from single agentic turns larger
   than the floor, which this corpus does not model.
2. **Is `bound_summary` reachable offline?** Yes. It runs inside
   `update_after_turn` unchanged. The model path (`--summary-model`) needs AWS.
3. **Control?** Not the kill switch. See spec §5: the summary is still bounded
   with the switch off, and `legacy` keeps fewer turns than `model_relative`.
   The control is `full`.

### 7.3 Lesson from building it

The first clock stub backdated every save by the pace's gap, and that
**hid a production bug**. A stub for time has to stamp *now* the way
`_save_compaction_state` does, and apply the gap only between turns. The fix
reads the turn's gap from a stamp captured before any head-of-turn save, and
`test_restore_pace_applies_the_parked_cut_for_free` now holds it in place.

### 7.4 Prod readout, 2026-09-25 (aggregate, read-only, ~4 days after 1.23.0)

- **Scale:** 74 cuts in 44 sessions, about 4% of active sessions.
- **Guards held:** hysteresis held, no summary exceeded 8k, and no cut applied
  inside the TTL.
- **Missed free apply:** on a restore, `_maybe_advance_truncation_anchor`
  saves (stamping `updatedAt`) before the head-of-turn
  `apply_pending_compaction` reads the gap. The gap reads about 0 s, and the
  parked cut waits for the paid hard-ceiling apply. About half of the
  compacted sessions still held a parked cut.
- **Retained after a cut:** median 37k, p90 129k. The floor was unreachable on
  about 64% of cuts.
- **Forced cuts:** about 22% of cuts. They are mostly single agentic turns
  that grow past the ceiling; compaction acts only between turns.
- **Summary source:** `ltm` on every sampled last cut. Model-compressed
  summaries go from a median of ~20k to ~760 tokens. **That compression is the
  first thing the full run should score.** Use `records` mode with records
  large enough to trigger it, plus `--summary-model`.
- **Measurement gaps:**
  - the per-call `compactionEvents` ledger records only ~57% of cuts;
  - `prefixTokens` is unusable;
  - `contextBreakdown` is absent in prod;
  - the per-cut EMF carries no session id.

