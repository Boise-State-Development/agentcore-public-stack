# Idle-time compaction: the cost replay, and what it says to test next

**Status:** cost half MEASURED 2026-11-04 against prod (30 days, read-only, content-free); quality
veto PASSED the same day (§5.3, paid `ask`). Candidate: a 75k trigger with a 25k floor, ~$227/month.
Not built.
**Prompted by:** review-queue [2026-11-03] *Spike idle-time compaction after the cache TTL*
(research/2026-11-03: Claude Code 2.1.314 compacts an idle >200K conversation so the first message
after the TTL is cheap). Its own gate: "justified only if `C#` rows show material post-gap re-write
spend."
**Instrument:** `backend/scripts/compaction_cost_replay.py` (tests:
`backend/tests/test_compaction_cost_replay.py`). It is the cost twin of
`compaction_quality_harness.py`: that one says what a cut loses, this one says what it saves.

---

## 1. The gate: post-gap re-write spend is material

Prod, `C#` rows from 2026-10-05 to 2026-11-04: 8,486 sessions, 75,384 priced calls, **$3,309.99**
(API-converse rows excluded: they have no cachePoint).

| | |
|---|---|
| Cache writes on a call after a 5+ minute pause | **$1,073.93 = 32.4% of spend** (28% on 2026-10-09's run) |
| by context at the return | <25k $59 · 25–50k $200 · 50–100k $416 · 100k+ $398 |
| by pause length | 5–15m **$580** · 15–60m $257 · 1–4h $93 · 4–24h $77 · 1d+ $67 |
| users who come back after a pause, by context | <25k 40% · 25–50k 62% · 50–100k 69% · 100k+ 75% |

The bigger the conversation, the more likely the user returns to it, which is what makes a job that
runs on every idle large session pay. **$398 of re-writes land at 100k+, above production's own
100k ceiling.** Those sessions are not failing to compact; they are failing to *shrink*: the fleet
logged 1,114 `floor_unreachable` and 470 `forced` events, because the protected tail (the last 3
user turns, with their tool results and documents) alone exceeds the floor. No idle trigger fixes
that, and the replay models it (§2).

## 2. What the replay does

Each session's calls are re-priced in order under each arm; the observed bill is the control, so
every number is a delta against what production actually charged. Rates are recovered per call from
the row itself (write = 1.25× base, read = 0.1×), so every model prices at what it was billed.

A cut removes `history − max(target, tail + summary)`, where the tail is the last 3 user turns
(60 s gaps, as the dashboard infers turns) and the summary is 3.4k tokens (the 2026-09-26 dev cut).
The removal carries forward to later calls until production's own compaction catches up. Each job
costs $0.015 (the same dev cut's two Nova 2 Lite calls), including the 469 jobs on sessions nobody
returned to. History is context minus the session's smallest context, which over-counts the static
prefix, so savings lean conservative. The docstring states every assumption.

## 3. Results

| arm | 30-day delta | jobs | sessions cut |
|---|---|---|---|
| **idle 50k → 20k, at TTL expiry** | **−$268 (−8.1%)** | $32 | 797 |
| idle 25k → 15k | −$285 (−8.6%) | $49 | 1,135 |
| idle 75k → 25k | −$227 (−6.9%) | $21 | 551 |
| idle 50k → 30k | −$224 (−6.8%) | $31 | 748 |
| idle 100k → 25k | −$176 (−5.3%) | $13 | 399 |
| cold 50k → 20k (cut in the turn on return) | −$275 (−8.3%) | $25 | 534 |
| idle 50k → 20k, job after 15 min | −$157 (−4.7%) | $21 | 789 |
| idle 50k → 20k, job after 30 min | −$94 (−2.8%) | $18 | 772 |
| 1h TTL on every cache point | **+$103 (+3.1%)** | — | — |
| 1h TTL + idle 50k → 20k after an hour | +$6 (+0.2%) | $17 | 756 |

Sensitivity of the headline arm: jobs at 4× ($0.06) −$173; an 8k summary −$250; 2 protected turns
−$312; 1 protected turn −$373. Of the $300 gross saving, $261 (87%) is the smaller re-write on the
return itself and $39 the cheaper reads after it. It is concentrated: the top 50 sessions carry $168
and the top 200 all of the net (the rest is a slightly negative long tail of wasted jobs).

**Readings.**
1. **Idle compaction pays, at roughly $270/month on this fleet**, and survives every sensitivity
   above. Most of it is in the 50k–100k+ bands; below 25k there is nothing to win.
2. **It must run at TTL expiry.** Half the cliff is 5–15 minute returns; a job that waits 15 minutes
   gives up 40% of the saving. Wasted jobs (no return) cost ~$7/month at the headline arm.
3. **Cold (in-turn) is ~$7/month cheaper and costs ~14 s of first-token latency** on every cut
   turn. The TTFT budget rules it out; this is the price of keeping it.
4. **A 1h TTL loses money (+3.1%)**, consistent with the 2026-09-27 static-prefix sweep: every
   growth write pays 2×, and that outweighs the returns it saves. Drop it as an alternative.
5. **The trigger matters less than the target.** 50k and 25k triggers save about the same; what
   moves the number is how far the cut goes, and that is exactly what the quality harness must judge.

## 4. What this cannot see

- **Quality.** A cut at 50k is twice as early as production's ceiling. 797 sessions a month would be
  summarized that are not today. The 2026-09-25 run showed plain compression dropping 28% of standing
  instructions; extract-then-compress fixed that *at the 100k cut*. Whether it holds at 50k → 20k is
  unmeasured.
- **Re-work.** A user or model re-fetching what a cut removed shows up as cost the replay does not
  charge. The quality harness's `UNKNOWN` rate is the proxy for it.
- **Content.** Tool-result clearing (stub old tool results instead of summarizing) needs per-message
  sizes the `C#` rows do not carry. It is the obvious next arm and needs one content-free field
  (largest-tool-result tokens in the history) or a run of the quality harness's corpus.
- **Turn boundaries** are inferred from 60 s gaps, as the dashboard does.

## 5. Next: the quality half

The harness already runs production's cut under an explicit `CompactionConfig`, at a `cold` pace.
Add arms that mirror §3's candidates, screen them free on availability, then pay for `ask` on the
survivors (~$9 a run):

| harness arm | config |
|---|---|
| `idle_50_20` | ceiling 50k, floor 20k, cold pace |
| `idle_75_25` | ceiling 75k, floor 25k, cold pace |
| `idle_50_30` | ceiling 50k, floor 30k, cold pace |
| control | `full`, and `model_relative` (production) |

Ship gate: no family below production's own arm by a McNemar-significant margin. If 50k → 20k loses
facts and 75k → 25k does not, the replay says the cheaper-quality-safe point is worth ~$227/month.
Then build the job outside the turn path (it must never sit before a first token), behind an
in-development flag.

### 5.1 First screen, $0 (2026-11-04): what the earlier cut removes

The three arms are in `compaction_quality/arms.py` (`IDLE_CUTS`). `cut` on the default corpus
(12 transcripts × 48 turns, 200k window, 15k overhead) with production's **fallback** summary, no
model:

| arm (cold pace) | cuts/session | probe tokens | turns retained | constraint | decision | reference | superseded |
|---|---|---|---|---|---|---|---|
| full | 0 | 163,804 | 100% | 100% | 100% | 100% | 100% |
| model_relative (prod) | 1.6 | 62,578 | 33.3% | 38.9% | 25.0% | 12.5% | 70.8% |
| idle_75_25 | 3.0 | 48,871 | 26.9% | 33.3% | 8.3% | 12.5% | 66.7% |
| idle_50_20 | 6.6 | 40,128 | 23.1% | 33.3% | 4.2% | 12.5% | 54.2% |
| idle_50_30 | **11.8** | 45,936 | 25.0% | 33.3% | 4.2% | 12.5% | 54.2% |

The restore pace orders the arms the same way (prod retains 51.9% of turns, `idle_50_20` 25.0%).

- **This is not the quality verdict.** The fallback summary is ~350 tokens, so even production
  keeps only 39% of standing instructions here. Production summarizes from LTM records with Nova 2
  Lite and extract-then-compress, and that path took availability to 100% in §9.2 of the veto
  harness doc. What this screen does measure is how much raw history the earlier cut takes away,
  which is how much the summary has to carry: the retained-turn share falls from a third to under a
  quarter.
- **`idle_50_30` is out.** A 30k floor under a 50k ceiling leaves no hysteresis room: it cut
  11.8 times a session, with 4 forced cuts. Every cut is a cache re-write the cost replay does not
  charge, because the replay only cuts on idle. Any idle policy needs the floor well under the
  trigger, and production's 0.25 ratio is the safe default.
- **Next, paid but not `ask`** (~$6): a `records` pass (`--chunk-turns 1 --record-words 600`, about
  $3.50 on Haiku 4.5) and `cut --summary records --summary-model` on `full,model_relative,
  idle_75_25,idle_50_20` at the cold pace (about 134 Nova 2 Lite cuts). That availability table is
  the one that tracked the paid score family by family in September.

### 5.2 Second screen, production's summarizer (2026-11-04, ~$6)

A fresh `records` pass (`--chunk-turns 1 --record-words 600`, 576 Haiku 4.5 calls, dev), then
`cut --summary records --summary-model` at the cold pace. That is production's path: LTM-shaped
records, bounded by extract-then-compress on Nova 2 Lite. One sample per arm (the compressor
samples at temperature 0.1).

| arm | cuts/session | probe tokens | summary tokens | turns retained | constraint (36) | decision (24) | reference (24) | superseded (24) |
|---|---|---|---|---|---|---|---|---|
| full | 0 | 163,804 | 0 | 100% | 100% | 100% | 100% | 100% |
| model_relative (prod) | 1.8 | 54,590 | 2,097 | 28.7% | 94.4% | 100% | 95.8% | 100% |
| idle_75_25 | 3.0 | 55,178 | 2,022 | 29.6% | **97.2%** | 100% | 95.8% | 100% |
| idle_50_20 | 7.9 | 37,444 | 1,876 | 21.3% | 91.7% | 100% | 95.8% | 100% |

- **The summary carries what the earlier cut removes.** With production's summarizer, availability
  barely moves even though `idle_50_20` keeps a fifth of turns verbatim against production's
  quarter-plus. §5.1's gap was the fallback summary, not the cut.
- **The misses are nested, which makes the comparison paired:** production loses 3 of 108 planted
  values; `idle_75_25` loses a subset of those 3 (2); `idle_50_20` loses all 3 plus one more
  standing instruction (4). At this n, one plant is noise (exact McNemar p = 1.0 either way).
- **`idle_75_25` is the candidate to confirm.** It is at least as available as production, saves
  ~$227/month in §3, and cuts 3.0 times a session against 7.9 for `idle_50_20`.
- Availability is an upper bound, and `superseded` scores "the new value is present", not "the old
  one is gone". The paid `ask` scores both.

### 5.3 Paid `ask` (2026-11-04, $7.98): the veto does not fire

The four §5.2 histories, `ask --k 3 --workers 4` on Haiku 4.5 in dev: 1,296 calls, all scored
(estimate $12.87; real tokens priced at $7.98). Majority of 3 per task, exact McNemar, paired.

| arm vs **production** | constraint (36) | decision (24) | reference (24) | superseded (24) |
|---|---|---|---|---|
| idle_75_25 | 0.97 vs 0.94 (0 losses / 1 win) | 1.00 vs 1.00 | 0.92 vs 0.96 (1 / 0) | 0.96 vs 0.96 |
| idle_50_20 | 0.92 vs 0.94 (1 / 0) | 1.00 vs 1.00 | 0.96 vs 0.96 | 0.96 vs 0.96 |

Against the full history, the worst row is `idle_50_20` constraint at 0.92 (3 losses / 0 wins,
p = 0.25); production's own is 0.94 (2 / 0, p = 0.5). Every other p is 1.0. Accuracy on facts whose
stating turn was cut is 0.95–0.96 on all three compacted arms, against 0.97 kept. Wrong answers are
still mostly `UNKNOWN` (9 / 9 / 11 for production / 75–25 / 50–20): when it misses, the model says it
does not know rather than inventing a value.

**Verdict.**
- **No idle arm is worse than production in any family.** The quality veto does not fire for
  either. The cost replay's saving is available at production's quality.
- **Ship `idle_75_25` as the candidate.** It is the arm that is never behind production in
  availability, cuts 3 times a session against 7.9, and saves ~$227/month (§3). `idle_50_20`
  saves ~$40/month more and is directionally one standing instruction worse; at this n that is noise,
  but it is the direction the §8 failure ran, so take the safer point.
- **What this does not show.** 108 tasks per arm cannot detect a loss under ~10 points; one
  synthetic, uniform document-editing corpus; one answering model; one compressor sample. Prod's
  deep tail (single agentic turns larger than the floor) is not in the corpus, and that is where
  `floor_unreachable` lives, so the build must re-measure cuts on dev against real sessions.

**Next: build it**, behind an in-development flag and outside the turn path:
1. A job that fires when a session goes `--ttl` quiet with context ≥ 75k, and runs production's
   own cut (`update_after_turn` with a 75k ceiling / 25k floor) so the summary is computed while
   nobody waits. The next turn applies it as today's free apply (`miss_ttl_expired`), so the
   rewrite it saves never gets paid.
2. The trigger must not move production's own byte-stability rules (truncation anchor, version
   monotonicity across agent instances), and it must never run for a session with a turn in flight.
3. Readout: `idleCompaction` events on the `C#` rows, then this replay re-run on the post-launch
   window to check the predicted ~6.9%.

## Reproduce

```bash
aws dynamodb scan --profile <PROFILE> --table-name <PREFIX>-sessions-metadata --filter-expression "begins_with(SK, :c) AND #t >= :d" --projection-expression "sessionId,#t,#c,tokenUsage,modelInfo.modelId,cacheStatus,compactionEvents" --expression-attribute-names '{"#t":"timestamp","#c":"cost"}' --expression-attribute-values '{":c":{"S":"C#"},":d":{"S":"<START>"}}' --output json > <scratch>/cost_rows.json
```

```bash
cd backend && python3 -I scripts/compaction_cost_replay.py --cost-rows <scratch>/cost_rows.json --window-end <END>
```

Keep the scan in a scratch directory. The script prints aggregates only.
