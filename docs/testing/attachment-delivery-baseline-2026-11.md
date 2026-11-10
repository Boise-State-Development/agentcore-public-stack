# Attachment delivery: phase 0 baseline on dev

**Date:** 2026-11-09 · **Spec:** `docs/specs/unified-attachment-delivery.md` §8 · **Arm:** `auto` (today's turn path)
**Target:** `runtime:dev-boisestateai-v2`, app 1.35.0, inference-api `dc8d00fd7731b316`
**Family:** `attachment_delivery`, 22 cases, k=3, uploads through presign → PUT → complete

This is the "before" picture that phases 3 to 5 are measured against. It is today's
behaviour, so it records what the turn path does now. It does not judge whether that
is what the turn path should do.

```bash
cd backend
AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/ad-haiku55 \
    --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
    --model-id us.anthropic.claude-haiku-5-5 --family attachment_delivery --upload-via presign --yes
AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py readout --out /tmp/ad-haiku55
```

## Haiku 5.5 (dev's default)

| Case | Pass | $/sample | First token | Turn | Doc tokens | Delivery | Turn-2 cache | Cuts/sample |
|---|---|---|---|---|---|---|---|---|
| lookup_md_5k | 3/3 | $0.0010 | 3.0s | 9.1s | 5,279 | inline 3 | — | 0.0 |
| lookup_md_30k | 3/3 | $0.0034 | 3.4s | 8.5s | 30,487 | inline 3 | — | 0.0 |
| lookup_md_80k | 3/3 | $0.0122 | 4.2s | 9.5s | 80,876 | inline 3 | — | 0.0 |
| lookup_md_150k | 3/3 | $0.0138 | 3.9s | 9.5s | 151,476 | inline 3 | — | 0.0 |
| lookup_md_300k | 3/3 | $0.0623 | 10.9s | 17.8s | 302,737 | inline 3 | — | 0.0 |
| lookup_txt_30k | 3/3 | $0.0027 | 3.4s | 7.0s | 30,412 | inline 3 | — | 0.0 |
| lookup_pdf_30k | 3/3 | $0.0064 | 6.2s | 11.3s | 48,000 | inline 3 | — | 0.0 |
| lookup_pdf_80k | 3/3 | $0.0153 | 11.7s | 19.0s | 127,500 | inline 3 | — | 0.0 |
| lookup_pdf_150k | 3/3 | $0.0495 | 23.8s | 33.4s | 238,500 | inline 3 | — | 0.0 |
| lookup_docx_30k | 3/3 | $0.0031 | 3.3s | 8.4s | 3,258 | inline 3 | — | 0.0 |
| lookup_html_30k | 3/3 | $0.0031 | 4.2s | 9.4s | 30,823 | inline 3 | — | 0.0 |
| lookup_pptx_30k | 3/3 | $0.0015 | 1.4s | 13.5s | 0 | none 3 | — | 0.0 |
| lookup_csv_30k | 3/3 | $0.0009 | 1.5s | 14.1s | 0 | none 3 | — | 0.0 |
| lookup_xlsx_30k | 3/3 | $0.0005 | 1.5s | 14.1s | 0 | none 3 | — | 0.0 |
| lookup_png | 3/3 | $0.0002 | 1.5s | 2.8s | 1,500 | inline 3 | — | 0.0 |
| cross_document_30k | 3/3 | $0.0159 | 4.3s | 36.8s | 78,487 | inline 1, on_demand 2 | — | 0.0 |
| cross_document_80k | 3/3 | $0.0350 | 6.5s | 23.8s | 208,376 | inline 2, on_demand 1 | — | 0.0 |
| absent_fact_30k | 3/3 | $0.0033 | 3.5s | 10.5s | 30,487 | inline 2, on_demand 1 | — | 0.0 |
| absent_fact_150k | 3/3 | $0.0218 | 7.1s | 11.8s | 151,476 | inline 3 | — | 0.0 |
| follow_up_30k | 3/3 | $0.0040 | 3.3s | 8.1s | 30,487 | inline 3 | hit 3 | 0.0 |
| follow_up_150k | 3/3 | $0.0268 | 7.0s | 13.3s | 151,476 | inline 3 | hit 3 | 1.0 (checkpoint) |
| count_five_documents | 3/3 | $0.0022 | 3.5s | 6.6s | 26,477 | inline 3 | — | 0.0 |

**66/66 samples passed; $0.85 in all as the `C#` rows price it.** No offloads in any
sample. "First token" and "Turn" are the Runtime's own numbers from the final
`metadata` frame, for the turn that carries the files. "none" means nothing went inline: a
spreadsheet or deck tool read the file. "on_demand" means the model called
`document_read` although the document was inline. Upload to usable was 0.4–1.3 s for
one file through presign (median 0.6 s), and 2.7 s for five.

## What the baseline says

1. **Quality is not today's limit on this model.** Every size passed, up to 300k
   tokens inline. Haiku 5.5's catalog window is pinned at 100k for pricing, but the
   model takes 1M, and compaction acts only between turns. So a single 300k turn goes
   through and then gets cut: `follow_up_150k` takes a checkpoint after turn 1 in
   every sample. The arms in the next wave have a high bar: on this model `auto`
   already answers.
2. **The cost rows understate every case over 100k by about 5×.** AWS bills a Haiku 5.5
   prompt over 100,000 tokens on its Long Context row, at 5× every bucket. The catalog
   holds one flat rate (see the comment on the row in `curated-models.ts`). So
   `lookup_md_150k`, `lookup_md_300k`, `lookup_pdf_80k` (127k), `lookup_pdf_150k`
   (239k), `cross_document_80k`, `absent_fact_150k` and `follow_up_150k` cost about
   5× what this table shows. The planner's budget has to price that step. It is the
   strongest cost argument for `on_demand` above 100k on this model.
3. **`documentTokens` is wrong for docx.** It is bytes/4 of the compressed file: 3,258
   for a document of about 30,500 tokens. On dev the document reaches the model as
   text (`CONVERSE_DOCUMENTS_AS_TEXT_ENABLED`), so the context holds about 9× what the
   row says. This is the "one estimator" gap phase 2 closes. The planner must not
   budget a docx from its bytes.
4. **PDFs are priced per page, and pages are the cost.** The estimate is 1,500 tokens
   a page; the probe measured about 1,560 (§2.1). The 80k-token PDF (85 pages) and the
   150k one (159 pages) carry 127k and 239k tokens of page images, so their first
   token is 11.7 s and 23.8 s against 4 s for the same text as Markdown. The 159-page
   PDF passed because Haiku 5.5's page ceiling is 600. On Haiku 4.5 (prod's default)
   it would be refused at 100 pages.
5. **Two documents invite tool calls.** In 3 of 6 cross-document samples, and one
   absent-fact sample, the model called `document_read` on a document it already had
   inline. That cost 20–30 s of turn time (36.8 s median at 30k). An `on_demand` arm
   would make that read the only one, not a second copy.
6. **Session start dominates the first token users see.** The harness measures
   client-side too. Its first token runs 6–15 s above the Runtime's for every case,
   because each sample opens a new session (no prewarm on the headless path). The
   Runtime's number is the one that moves with delivery.

## Not run yet

- **The larger model.** A Sonnet 4.6 k=3 run hit the eval user's $10 monthly quota on
  dev after the Haiku run, so 58 of its 66 samples answered with the quota message.
  That run is discarded. The harness now stops on `quota_exceeded` instead of scoring
  it.
- **The forced arms, the ceiling arms and the paced follow-up** (§8.4). These wait for
  the planner's fixture-only `delivery` value. The cases already carry `run.arm` and
  `run.ceiling`.
- **20 files**, because today's cap is 5. **Scanned and chart PDFs** are in `document_qa`
  and `attachment_tools`, not repeated here.
