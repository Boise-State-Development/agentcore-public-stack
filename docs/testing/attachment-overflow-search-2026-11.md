# Attachment overflow: `on_demand` against `search` steering (phase 5 readout, 2026-11-11)

`docs/specs/unified-attachment-delivery.md` §4.3.2 and §9, phase 5. The question this
answers is the size of a conversation's on-demand set above which the turn's guidance
should steer the model to `document_read`'s `query` instead of a `pattern` per document
(`attachment_plan.SEARCH_STEER_MIN_DOCUMENTS`).

**Answer: no set size measured pays.** At 2, 4 and 8 documents both arms answered every
sample, and steering to search cost more input tokens on Haiku 5.5 at every size and on
Sonnet 4.6 at 2 and 4. It saved tokens only on Sonnet 4.6 at 8 documents. The threshold
is therefore set above the largest set measured (9), and the per-file guidance for an
over-budget file keeps `pattern` first even when the file is indexed. The indexing and
the `search` mode stay: they cost a few cents a month, and they are what a future
steering rule or a question with no distinctive term would use.

## Setup

- Dev, build 64cafa60f (#2002), `ATTACHMENT_PLAN_ENABLED` and `ATTACHMENT_KB_ENABLED` on.
- Cells `overflow_{2,4,8}_documents_{on_demand,search}` (`scripts/eval_fixtures/delivery_cases.py`):
  2, 4 or 8 ~30k-token Markdown dossiers, attached two per turn. Each attaching turn asks
  an early-depth fact from the first dossier it names. The last turn asks a 90%-depth fact
  from a dossier attached on an earlier turn, without naming the dossier.
- Arms are fixture-only row values. `on_demand` is never indexed and never steered.
  `search` is indexed before the first turn (125 s for 8 documents) and always steered.
- k=3 per cell, on `us.anthropic.claude-haiku-5-5` and `us.anthropic.claude-sonnet-4-6`.
  $0.07 and $1.42.
- Tokens are every call's `inputTokens + cacheReadInputTokens + cacheWriteInputTokens`
  from the sample's `C#` rows. Concurrent samples share cache writes, so dollars are not
  compared.

```bash
cd backend
AWS_PROFILE=dev-ai PYTHONPATH=src .venv/bin/python scripts/eval_harness.py run --out /tmp/ov-h55 \
    --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
    --model-id us.anthropic.claude-haiku-5-5 --family attachment_delivery --match '\.overflow_' --yes
AWS_PROFILE=dev-ai PYTHONPATH=src .venv/bin/python scripts/eval_harness.py readout --out /tmp/ov-h55
```

## Results

Medians over three samples. "Last turn" is the unnamed question.

| Model | Documents | Pass (on_demand / search) | Input tokens, sample | Input tokens, last turn | Last turn duration |
|---|---|---|---|---|---|
| Haiku 5.5 | 2 | 3/3 / 3/3 | 56,869 → 74,618 (+31%) | 29,780 → 36,656 (+23%) | 7.1 s → 9.3 s |
| Haiku 5.5 | 4 | 3/3 / 3/3 | 117,203 → 162,324 (+38%) | 41,472 → 66,835 (+61%) | 7.6 s → 8.5 s |
| Haiku 5.5 | 8 | 3/3 / 3/3 | 221,224 → 314,730 (+42%) | 58,998 → 83,925 (+42%) | 8.5 s → 9.3 s |
| Sonnet 4.6 | 2 | 3/3 / 3/3 | 44,602 → 48,801 (+9%) | 23,316 → 26,941 (+16%) | 5.2 s → 5.2 s |
| Sonnet 4.6 | 4 | 3/3 / 3/3 | 79,904 → 83,976 (+5%) | 30,611 → 33,653 (+10%) | 5.9 s → 6.2 s |
| Sonnet 4.6 | 8 | 3/3 / 3/3 | 170,024 → 153,082 (−10%) | 45,239 → 43,263 (−4%) | 8.8 s → 4.0 s |

The last turn behaved as designed. `on_demand` called `pattern` once per document (2, 4
or 8 calls, in parallel in one round), and `search` called `query` once. On Sonnet 4.6 at 2
documents, one of the three `search` samples used `pattern` instead.

## Why search did not pay

- **A `query` result is large and it stays.** It is up to five chunks of up to 2,000
  characters, so about 2.5k tokens, more at Haiku 5.5's 1.40 tokenizer factor. It is
  re-sent on every later call. A `pattern` hit is a line or two per document.
- **The steered model searched on every turn, not only the last.** With `search` mode
  offering `query` first for a named file, Haiku 5.5's attaching turns also used `query`,
  which cost about 2.5–5k tokens more per turn than `pattern`. Samples whose early turns
  used `pattern` came out about even on the last turn. In real traffic a file is not
  searchable on the turn that attaches it (ingest takes 68–264 s), so the early turns would
  look like those samples.
- **These questions name their subject.** A unique site name is a distinctive regex
  term, so `pattern` finds the fact in one round. A question with no such term (a
  paraphrase, or a scanned page with no text layer) is where search should win. It is not
  measured here. The large-document readout's 38% saving from naming `query` first came
  from scanned and 40+ MB documents, not from these.

## What changed (this PR)

- `SEARCH_STEER_MIN_DOCUMENTS` 3 → 9: above the largest set measured. A conversation
  below it records `steer: on_demand, reason: below_threshold` on `attachmentPlan.conversation`.
- The per-file guidance for a file over a budget keeps `pattern` first even when the
  planner sent it `search`. A reference-class document keeps `query` first, as before
  phase 5.
- No change to indexing, `search` mode, `attachmentPlan` or the harness.

## Dev validation (#2002, 2026-11-11)

- **Indexing a real upload.** A 30k-token Markdown file uploaded through presign
  (`lookup_md_30k`) was planned `on_demand` / `over_document_budget`. The facts run then
  ingested it (`kbIndex: indexing`, then searchable). A later turn that re-attached it
  planned it `search` / `over_document_budget`, with `conversation.reason = below_threshold`.
- **Search finds the planted fact.** In every `search`-arm sample the last turn found the
  unnamed fact through one `query`. A cold-restored session (StopRuntimeSession, then a new
  question) found a new fact through `query` and answered correctly.
- **`attachmentPlan`.** Per file `search` / `forced` or `on_demand` / `forced`.
  `conversation` reads `{documents, searchable, tokens, steer, reason}` (`forced`,
  `none_searchable`, `below_threshold` seen).
- **Restore.** Answers were correct after a cold stop on both arms. The first call after
  restore re-wrote 13–19k tokens on both arms. The cause was the truncation anchor advancing
  after the cache had expired (an 853 s gap), not phase 5.
- **Fork.** A fork of an 8-document `search` session answered correctly. Search does not
  work there: the fork's file rows belong to the root, so its conversation set is empty, and
  a `query` naming a root upload was refused ("No file with id … found in this
  conversation") because KB search filters on the caller's session id. The model fell back
  to `pattern`. This predates phase 5: reference-class search in a fork has the same gap.
- **First token.** The session-files read (`attachment conversation set: … wait_ms=`)
  waited 0.0 ms in 136 of 138 turns and 4.1 ms at most.

## Follow-ups

- **Search in a fork.** Scope `attachment_search` and the conversation set to the fork's
  `rootSessionId` / `forkedFrom` rows, as #2000 did for document rehydration.
- **A cell search should win.** Questions with no distinctive term (a paraphrase of the
  subject) and scanned documents, before steering is turned on at any size.
- **A smaller `query` result.** Top 3 instead of 5, or a lower per-chunk cap, would change
  the trade-off measured here. Re-run these cells after.
