# Attachment delivery: the document budget on 1M-context models

**Date:** 2026-11-17 · **Spec:** `docs/specs/unified-attachment-delivery.md` §5.2, §8.4
**Code:** this PR's cells and `run.file_arms`, run from commit 8456e3b1c (plus the terse cells added afterwards)
**Target:** dev's deployed Runtime, build 1.36.1 (inference-api `7aaa1dbb7d760853`), the `token` profile.
Dev's Runtime and its defaults were not changed.
**Models:** Opus 5.5 (`us.anthropic.claude-opus-5-5`) and Sonnet 5.5 (`us.anthropic.claude-sonnet-5-5`), k=3
**Spend:** $33.21 as the `C#` rows price it (Opus $22.15, Sonnet $11.06). The harness estimated $26.88
(Opus $18.33, Sonnet $8.55); my pre-run estimate was about $32 ($22 and $10).

## Recommendation

**Keep the 50k document budget on the 1M models.** Neither model lost an answer at today's
budget. No arm that held more beat it on quality, on any set or question measured. Holding more
cost 2–7× as much per session and made the first token 2–4× slower. Of the three options in the brief:

- **Keep 50k: recommended.**
- **Raise the cap per model** (a per-model `COMPACTION_CEILING_CAP_TOKENS`): not supported. It buys
  holding, and holding bought nothing here. Its cost is the one the 2026-09-15 replay found dominant:
  the whole held prefix is re-written after every pause past the cache TTL.
- **Raise the document budget only for multi-document turns:** not supported either, and it conflicts
  with the ceiling. A 200k budget under the 100k chat ceiling holds ~186k tokens on turn 1. The
  `over_ceiling` offload then replaces those documents with their digests once their two-turn pin ends.
  So the extra budget pays full price for two turns and then sends the documents on demand anyway.

The field failure that prompted this was five policy PDFs (31 pages) on Opus 5.5, where one on-demand
file went unread and the answer came from four. It did **not** reproduce in 60 sample-turns with
three to nine on-demand files, on either model: every on-demand file was read, every time. That
includes a terse question, written in the field case's shape, that names no district and does not ask
for every figure. So the field failure looks like a guidance problem, not a budget problem: the note
on an on-demand file, which PR 2105 makes directive. Re-run these cells after it merges.

## The cells

Two sets of synthetic policy PDFs (`delivery_policy{5,10}_*.pdf`). Each set has one district per
document, and each document carries two planted figures with a withdrawn-draft lookalike apiece:

| Set | Files | Pages | Planner cost on a 1M model (est.) | Measured prefix, all held |
|---|---|---|---|---|
| 5 | 5 × ~5.5k tokens | 34 | 94k | ~101k on both models |
| 10 | 10 × ~13.5k tokens | 150 | 429k | ~435k on both models |

- **Turn 1** asks for every district's critical-incident response target, then the shortest. The
  shortest is in the 4th file (set 5) or the 8th (set 10), which today's budget sends on demand.
  **Turn 2** asks the same for records retention, inside the cache TTL. **Terse** is one turn:
  "Which district commits to the fastest critical-incident response?" Each figure sits at 58–90%
  depth, so no digest and no first page carries it.
- **Arms.** `auto` is today's planner on dev, at a 50k budget. Its `attachmentPlan` held 2 of the 5
  files and 1 of the 10. `b200k` is the planner's own split at a 200k budget, 4 of the 10 held,
  forced per file through `run.file_arms`. `held` forces every file `native`. In set 5 a 100k or 200k
  budget already holds every file, so `held` stands for both. The splits are the real
  `plan_attachments` → `refine_for_model` result, pinned by
  `test_budget_split_is_the_planner_s_own_at_each_budget` for both models.
- **Controls** (Opus only): the existing single-document cells `arm_pdf_30k_{native,on_demand}` and
  `whole_list_pdf_30k_{native,on_demand}`. A ~30k-token PDF costs ~83k on a 1M model, so today it
  goes on demand, and at 200k it would be held.
- **Scoring.** Every district must appear beside its own figure, and the extreme must be named on the
  closing line. The turn-1 score is the readout's, from that turn's own answer; the harness scores the
  last turn. "Unread" counts on-demand files with no `document_read` call naming their `upload_id` that
  turn.

```bash
cd backend
AWS_PROFILE=dev-ai PYTHONPATH=src .venv/bin/python scripts/eval_harness.py run --out /tmp/budget-opus \
    --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
    --model-id us.anthropic.claude-opus-5-5 --family attachment_delivery \
    --match 'compare_.*_policies_.*_(warm|terse)|arm_pdf_30k_(native|on_demand)|whole_list_pdf_30k' --yes
```

## Results

### Quality

Every sample of every arm passed, on both turns, on both models. The unread column counts
on-demand files per sample-turn:

| Model | Cells | Sample-turns | Passed | On-demand files unread |
|---|---|---|---|---|
| Opus 5.5 | compare warm, cold (set 5), terse; controls | 66 | 66 | 0 of 186 |
| Sonnet 5.5 | compare warm, terse | 45 | 45 | 0 of 162 |

The on-demand arms read every file in one parallel round: 3, 6 or 9 `document_read` calls in the turn's
second model call. They read them again on the follow-up, because they had kept only the matching lines.
`held` answered both turns without a tool call.

### Cost, priced at the catalog rates

Opus 5.5: $4.40 input, $22 output, $5.50 cache write, $0.22 cache read per MTok.
Sonnet 5.5: $2.20, $11, $2.75, $0.11.
- **Turn 1** is measured, from samples whose first call read only the 6.7k static prefix (see the caveats).
- **Warm follow-up** is measured.
- **Cold follow-up** is turn 2 after a pause past the 5-minute TTL. It re-writes everything after the
  static prefix, which has a 1-hour TTL. It is priced from turn 2's measured first-call prefix. That
  model matched the one real cold measurement (`auto`, set 5, Opus) to 1%: $0.254 predicted, $0.257
  measured.

| Model | Set | Arm | Turn 1 | + warm turn 2 | + cold turn 2 | Session, cold | × `auto` |
|---|---|---|---|---|---|---|---|
| Opus 5.5 | 10 | auto | $0.32 | $0.08 | $0.34 | **$0.65** | 1.0 |
| Opus 5.5 | 10 | b200k | $1.05 | $0.12 | $1.07 | **$2.12** | 3.3 |
| Opus 5.5 | 10 | held | $2.37 | $0.11 | $2.37 | **$4.73** | 7.3 |
| Opus 5.5 | 5 | auto | $0.25 | $0.04 | $0.25 | **$0.50** | 1.0 |
| Opus 5.5 | 5 | held | $0.60 | $0.07 | $0.57 | **$1.17** | 2.3 |
| Sonnet 5.5 | 10 | auto | $0.15 | $0.04 | $0.16 | **$0.32** | 1.0 |
| Sonnet 5.5 | 10 | b200k | $0.52 | $0.06 | $0.53 | **$1.05** | 3.3 |
| Sonnet 5.5 | 10 | held | $1.18 | $0.05 | $1.18 | **$2.36** | 7.4 |
| Sonnet 5.5 | 5 | auto | $0.12 | $0.02 | $0.13 | **$0.25** | 1.0 |
| Sonnet 5.5 | 5 | held | $0.28 | $0.01 | $0.26 | **$0.54** | 2.2 |

- **What each turn sent.** Input tokens per turn, summed over its model calls (uncached + cache write
  + cache read):

  | Set | `auto` | `b200k` | `held` |
  |---|---|---|---|
  | 10 | ~108k | ~369k | ~435k |
  | 5 | ~93k | — | ~101k in one call; ~201k on Opus, whose turn makes a second call |

  `auto` sends more than its prefix because its second call re-reads the prefix with the tool results.
  It still writes the least, and writes are what a pause re-pays.
- **Warm, holding is cheap per turn.** A held follow-up inside the TTL is a cache read: $0.05–0.11 for
  435k tokens. Holding's cost is the first write and every re-write after a pause, which is the
  cost the 2026-09-15 replay found dominant in heavy sessions.
- **The single-document control agrees with phase 0.** A ~30k PDF held cost $0.54 for a lookup and
  $0.57 for a list question. On demand cost $0.023 and $0.050, for the same 3/3 answers.

### First token

The runtime's own first-token time on turn 1, taken from samples that wrote their own prefix, in
seconds:

| Model | Set 10: auto · b200k · held | Set 5: auto · held | 30k PDF: native · on_demand |
|---|---|---|---|
| Opus 5.5 | 7.4–8.7 · 15.1–16.7 · **30.3–34.3** | 6.2–8.0 · 13.1 | 10.6–12.4 · 1.6–1.7 |
| Sonnet 5.5 | 3.2–5.6 · 9.4–11.9 · **23.1–26.5** | 2.9–6.6 · 6.1–7.1 | — |

Turn duration, Opus set 10: `auto` 20.6 s, `b200k` 26.1 s, `held` 32.2 s. The on-demand tool round
costs less than the time to write a large held prefix. A cold return re-pays that write as well: a
held 435k prefix means about 30 s of waiting on Opus after every pause.

## Caveats

- **Shared cache writes.** Samples of one case carry byte-identical prefixes: the system prompt, tools
  and the same PDFs. A concurrent sample then reads what another wrote. `first_read` above 6.7k marks
  it, and those samples are left out of the cost and first-token columns. This is also why the
  measured cold run of the `held` arm read its prefix after 330 s (the warm samples kept it alive), so
  that arm's cold column is priced, not measured. Real users do not share prefixes like this.
- **Synthetic documents.** Uniform filler, a distinctive sentence per figure, and a question that
  names its attribute exactly. `pattern` finds every figure in one round. A real policy set can phrase
  the figure differently per document, have no text layer, or have a digest that looks like it already
  answers. Those are the cells where on demand could lose and holding could win, and none is measured
  here. The field report remains the one data point in that direction.
- **The field case's guidance.** Dev ran without PR 2105's directive note, so these results are
  today's guidance. Its effect on the field shape is not measured.
- **Cold, measured once.** Only `auto` on set 5 measured a real cold return (k=2), and its turn-2 write
  matched the pricing model.
- **Sonnet 5.5 controls not run**, to stay within the eval's estimate.

## Follow-ups

- **Re-run the terse cells after PR 2105**, and add a cell where on demand can lose: per-document
  phrasing of the figure with no shared term, or a scanned PDF.
- **The catalog override in §5.2** ("a catalog field could override the budget per model") stays
  unbuilt. Nothing here asks for it.
