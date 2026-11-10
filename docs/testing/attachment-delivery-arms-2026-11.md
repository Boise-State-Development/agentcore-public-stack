# Attachment delivery: forced arms and ceiling arms (phase 0)

**Date:** 2026-11-10 · **Spec:** `docs/specs/unified-attachment-delivery.md` §8.4, §8.5, §11 Q3
**Code:** #1968 (arms, ceilings, the §5.2 measurement switch), run from that branch
**Target:** three local inference-api stacks with dev's Runtime environment and dev's data plane, one per
ceiling (`cases.CEILING_ENV`). Dev's Runtime was not changed.
**Models:** Haiku 5.5 (`us.anthropic.claude-haiku-5-5`, k=3) and Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`,
k=1, 30k and 80k only, to fit the eval user's November quota)
**Spend:** about $9.35 as the `C#` rows price it (Haiku $1.28, Sonnet $8.02)

```bash
cd backend
# one stack per ceiling, e.g. the documents-excluded one:
AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS=100000 \
AGENTCORE_MEMORY_COMPACTION_CEILING_EXCLUDES_DOCUMENTS=true \
  <dev Runtime env> PYTHONPATH=src .venv/bin/python -m uvicorn apis.inference_api.main:app --port 8012
AWS_PROFILE=dev-ai PYTHONPATH=src .venv/bin/python scripts/eval_harness.py run --out /tmp/ad-h55-excluded \
    --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
    --invocations-url http://127.0.0.1:8012 --ceiling 100k_documents_excluded \
    --model-id us.anthropic.claude-haiku-5-5 --family attachment_delivery --match '\.ceiling_' --yes
AWS_PROFILE=dev-ai PYTHONPATH=src .venv/bin/python scripts/eval_harness.py readout --out /tmp/ad-h55-excluded
```

## Recommendation on §11 Q3

**Final (second round, 2026-11-10): holding a document never beat `on_demand`, neither on the
late question nor on the whole-document questions (list and count, compare two distant
sections, summarise), at any size or ceiling measured. Drop §5.2's subtraction and keep the
document budget under the chat ceiling: `document_budget = 0.5 × chat_ceiling`.**

What the second round added:

- **Whole-document questions, Haiku 5.5, k=3: 54/54.** Both arms passed every list, compare
  and summarise question at ~30k, ~80k and ~150k, and `on_demand` used **10–50× fewer input
  tokens** (9–25k against 100–470k). Its first token on the file's turn was faster (3.4–7.7 s
  against 7.0–30.7 s), and its turn duration was within the same 18–55 s range.
- **Whole-document questions, Sonnet 4.6, k=3: 51/54, then 54/54 with #1979.** `native`
  27/27 and `on_demand` 24/27 at first. The one losing cell, `on_demand` summarise at
  ~150k (0/3), was a `document_read` bug, not the mode, and it passed 3/3 once the fix
  deployed. The PDF had wrapped "Major / incident" across two lines, pattern mode
  matched line by line, and so the model never saw one of the four incidents. Fixed in #1979.
  Every other `on_demand` cell passed, using **14–66× fewer input tokens** (6–79k against
  84–398k).
- **Sonnet 4.6's 80k cells at k=3** make the held-then-lost failure stable: **0/6 at today's
  ceiling**, 2/3 at 100k with documents excluded (cold) and 2/3 at 200k (cold), against
  **6/6 for `on_demand`**. Every failure is the same answer: the document had left context
  (cut or offloaded), the model listed both work orders with the cancelled one first, and it
  asked which was meant. A document that is held and then dropped is the worst arm on
  quality, and the most expensive one.

- **Quality is equal.** `on_demand` passed the late question in every sample: Haiku 18/18 and
  Sonnet 4/4. Where a held document was still in context on turn 6, it also passed, but it
  added nothing. The two failures in the matrix are both a document that was held and then
  cut (Sonnet, 80k, default ceiling). After the cut, `document_read` returned the fact and its
  lookalike together, and the model named the lookalike first while asking which one was meant.
  It found the right value, but the answer fails the "value leads" check.
- **Cost is not equal.** A held document is re-read on every warm turn. Over the six turns,
  `on_demand` used **3–12× fewer input tokens on Haiku** (64–103k against 234–984k, the ratio
  growing with size) and **5–28× fewer on Sonnet** (42–67k against 350k–1.29M). The most expensive cell, Sonnet 80k held at
  100k with documents excluded, used 1.29M tokens against 45k for `on_demand`.
- **Excluding documents from the chat ceiling buys nothing on Haiku 5.5.** Its catalog window
  is 100k (its price tier), so its ceiling is 50k and its hard ceiling 70k, and the hard
  ceiling still counts documents. Every held document from ~48k measured upward was cut by
  turn 6 in both arms, and the late question was answered by `document_read` either way. The
  excluded arm used the same input tokens as the default arm (within 5%) for the same answers;
  its higher `$/sample` is the shared-cache artifact in the caveats, not a real cost.
- **On Sonnet 4.6 the switch did hold the 80k document**, and so did a 200k ceiling, while
  today's ceiling cut it. That is the one place §5.2 changes behaviour, and its quality result
  equals `on_demand`'s at 28× the tokens.
- **The cold-cache offload does its job.** In every cold-pace sample the held document was
  offloaded to its digest at the head of turn 5 (`document_offload`, `cache_expired`). The
  late question was then answered with `document_read`, at no quality loss.

**Implied `document_budget`** (the `native`/`text` share a conversation may hold before the
planner sends a file `on_demand`), with the ceiling left where it is:

| Model | Chat ceiling | Largest document held to turn 6 | Recommended `document_budget` |
|---|---|---|---|
| Haiku 5.5 | 50k (window pinned to its 100k price tier) | none of the sizes tried (smallest: ~48k measured) | **~25k tokens** (half the ceiling) |
| Sonnet 4.6 | 100k | ~48k measured (30k estimated) | **~50k tokens** (half the ceiling) |

Half the ceiling leaves the other half for the conversation, so a document under the budget
survives the turns that matter instead of being cut at turn 3–5. Above the budget, `on_demand`
is the better mode on every axis measured. It answered the same questions with an equal or
faster first token on the turn that carries the file (Haiku 1.7–8.2 s against 7.2–25.4 s; Sonnet
1.9–6.0 s against 8.8–25 s). A turn that calls `document_read` takes longer end to end, about
+5–15 s for its tool round. So the budget should be a per-model catalog value read by the
`token` profile, defaulting to `0.5 × chat_ceiling`.

The whole-document questions use planted marker phrases, which a pattern search finds
directly. That favours `on_demand`. A free-form summary of unmarked prose is the one case
still unmeasured, and it needs a judge, which stays advisory.

## Whole-document questions (Haiku 5.5, k=3, default ceiling)

A ~30k, ~80k or ~150k-token PDF (`delivery_yearbook_*`). **List** must name six boil-water
notice sites and say six: a seventh site, where a notice was considered and not issued, would
make it seven. **Compare** must give the change in a gauge level read at ~10% and ~88% depth,
and its direction. **Summarise** must name four incident sites, one per quarter. "Reads" is
the mean number of `document_read` calls.

| Question | Size | `native`: pass · tokens · reads | `on_demand`: pass · tokens · reads |
|---|---|---|---|
| list | 30k | 3/3 · 100k · 0.3 | 3/3 · 10k · 1.7 |
| list | 80k | 3/3 · 254k · 1.0 | 3/3 · 25k · 6.3 |
| list | 150k | 3/3 · 470k · 3.3 | 3/3 · 10k · 1.7 |
| compare | 30k | 3/3 · 100k · 0 | 3/3 · 11k · 1.3 |
| compare | 80k | 3/3 · 253k · 0 | 3/3 · 17k · 3.3 |
| compare | 150k | 3/3 · 468k · 0.3 | 3/3 · 9k · 1.3 |
| summarise | 30k | 3/3 · 100k · 0 | 3/3 · 16k · 3.3 |
| summarise | 80k | 3/3 · 254k · 0.3 | 3/3 · 18k · 4.0 |
| summarise | 150k | 3/3 · 468k · 0 | 3/3 · 16k · 3.7 |

At 150k the held model also called `document_read` to list items (3.3 reads). It held the
document and still searched it.

### Sonnet 4.6 (k=3, dev's deployed Runtime)

| Question | Size | `native`: pass · tokens · reads | `on_demand`: pass · tokens · reads |
|---|---|---|---|
| list | 30k | 3/3 · 86k · 3.3 | 3/3 · 7k · 1.0 |
| list | 80k | 3/3 · 216k · 3.0 | 3/3 · 8k · 1.3 |
| list | 150k | 3/3 · 398k · 3.0 | 3/3 · 11k · 2.3 |
| compare | 30k | 3/3 · 84k · 0 | 3/3 · 6k · 1.0 |
| compare | 80k | 3/3 · 214k · 0 | 3/3 · 9k · 2.0 |
| compare | 150k | 3/3 · 397k · 1.3 | 3/3 · 6k · 1.0 |
| summarise | 30k | 3/3 · 84k · 0 | 3/3 · 40k · 4.7 |
| summarise | 80k | 3/3 · 214k · 0 | 3/3 · 79k · 9.0 |
| summarise | 150k | 3/3 · 397k · 0.3 | **0/3** · 166k · 7.3 before #1979; **3/3** · 25k / 26k / 365k · 7–11 after it |

Summarise is where `on_demand` works hardest: 5–9 reads, paging through the document in
20-page slices. It still costs well under half the held arm, and it is the one question
type where a slice-level search bug shows up as a wrong answer. These ran concurrently with
identical prefixes, so first-token times are not comparable (see the caveats).

## Forced arms: the gating cells (Haiku 5.5, k=3, default ceiling)

| File | `native` | `text` | `on_demand` |
|---|---|---|---|
| pdf ~30k | 3/3 · $0.0018 · TTFT 12.4 s | 3/3 · $0.0009 · 6.0 s | 3/3 · $0.0015 · 5.0 s |
| docx ~30k | 3/3 · $0.0088 · 7.2 s | 3/3 · $0.0109 · 9.9 s | 3/3 · $0.0007 · 8.2 s |
| html ~30k | 3/3 · $0.0108 · 8.4 s | 3/3 · $0.0054 · 11.5 s | **0/3 → 3/3 after #1970** · $0.0006 · 5.0 s |
| pdf ~80k | 3/3 · $0.0361 · 25.4 s | — | 3/3 · $0.0010 · 6.2 s |
| md ~150k | 3/3 · $0.0521 · 10.3 s | — | 3/3 · $0.0009 · 8.0 s |

- **Text loses no layout on prose.** `native` and `text` agree on pdf, docx and html, which
  clears the risk row "Text mode loses layout a native docx block kept" for prose. These
  fixtures carry no tables, so the 2026-11-06 table comparison quoted in `document_cap.py`
  remains the evidence for tables. A native docx block and its `docx_markdown` text measured
  within 3 tokens of each other (42,484 against 42,487).
- **One bug found and fixed: #1970.** `document_read`'s pattern mode split html into raw markup
  lines. A minified page is one line, so a pattern returned only the first match in the whole
  document, here the lookalike, and the model reported the real value as absent. It now
  searches the rendered text. The cell went from 0/3 to 3/3 on the same stack.
- `search` is not measured. The legacy profile can only put a prepared reference-class
  (>4 MB) document in the attachments KB, so no fixture can be forced into it. It waits for
  phase 5.

## Ceiling arms

Six turns on one PDF: three lookups, two asides, then the 90%-depth fact on turn 6. `cold`
waits 330 s before turn 5. Pass is turn 6 only. "Tokens" is the sample's total input
(uncached + cache write + cache read) across all six turns. "Held" means the late turn made
no `document_read` call, so the document was still in context.

### Haiku 5.5 (k=3)

| PDF (est. / measured) | Arm | Ceiling | Warm: pass · tokens · held | Cold: pass · tokens · held |
|---|---|---|---|---|
| 30k / ~48k | native | default | 3/3 · 235k · no (cut) | 3/3 · 234k · no (cut) |
| 30k | native | 100k excl. docs | 3/3 · 241k · no (cut) | 3/3 · 247k · no (cut) |
| 30k | on_demand | default | 3/3 · 82k | 3/3 · 90k |
| 80k / ~128k | native | default | 3/3 · 545k · no (cut) | 3/3 · 553k · no (cut) |
| 80k | native | 100k excl. docs | 3/3 · 552k · no (cut) | 3/3 · 555k · no (cut) |
| 80k | on_demand | default | 3/3 · 64k | 3/3 · 72k |
| 150k / ~239k | native | default | 3/3 · 983k · no (cut) | 3/3 · 984k · no (cut) |
| 150k | native | 100k excl. docs | 3/3 · 984k · no (cut) | 3/3 · 983k · no (cut) |
| 150k | on_demand | default | 3/3 · 81k | 3/3 · 103k |

No 200k arm: Haiku 5.5's window is pinned to its 100k price tier, so a 200k cap changes nothing
(§8.4, "a model with a price tier at 100k runs the 100k arms only"). Cost rows understate
Haiku 5.5 prompts over 100k by about 5× (the long-context row; see the 2026-11 baseline), so
its held arms cost more than their dollar columns show.

### Sonnet 4.6 (k=1)

| PDF | Arm | Ceiling | Warm: pass · tokens · held | Cold: pass · tokens · held |
|---|---|---|---|---|
| 30k / ~48k | native | default | 1/1 · 506k · yes | 1/1 · 350k · offloaded at turn 5 |
| 30k | native | 200k | 1/1 · 506k · yes | 1/1 · 350k · offloaded |
| 30k | native | 100k excl. docs | 1/1 · 506k · yes | 1/1 · 350k · offloaded |
| 30k | on_demand | default | 1/1 · 42k | 1/1 · 67k |
| 80k / ~214k on Sonnet | native | default | **0/3** · 454k · no (cut) | **0/3** · 454k · no (cut) |
| 80k | native | 200k | 3/3 · 663k · no (offloaded, `over_ceiling`) | 2/3 · 663k · offloaded |
| 80k | native | 100k excl. docs | 3/3 · 1,288k · yes | 2/3 · 871k · offloaded |
| 80k | on_demand | default | 3/3 · 44k | 3/3 · 44k |

The 80k rows are k=3: the first run plus a two-sample top-up. The top-up's 200k samples ran
with #1978's offload fix. The 30k rows stay k=1.

Every failure found the right value. They all read "a cancelled one (WO-…79) and an
active one (WO-…97). Could you clarify which you're looking for?", which fails because the
lookalike leads. This held-then-cut path is the only one that failed. `on_demand`, which never
held the document, read the same slices and answered directly.

### Speed (medians)

| | First token, turn 1 | Turn 1 duration | Turn 6 first token / duration |
|---|---|---|---|
| Haiku native (all ceilings) | 9.0–30.8 s | 14.6–41.9 s | 1.7–9.0 s / 8.7–26.1 s |
| Haiku on_demand | 1.7–3.1 s | 18.3–24.1 s | 1.6–2.4 s / 11.1–16.7 s |
| Sonnet native | 8.8–25.0 s | 12.1–31.1 s | 1.0–10.5 s / 2.9–22.0 s |
| Sonnet on_demand | 1.9–6.0 s | 16.3–47.2 s | 1.2–8.1 s / 11.9–21.1 s |

## Caveats

- **Concurrent identical samples share cache writes.** Same document bytes, same prompt,
  same system prompt: a sample running while another has just written the prefix reads it
  instead. For example, Sonnet 80k native at the default ceiling wrote 652 tokens because
  the 200k run had written the document. So `$/sample` in the raw readouts below
  understates the first write for whichever sample came second, which is why the comparison
  above uses tokens. Running each arm as its own invocation would separate them. A
  per-sample nonce in the prompt would also work, but it would change the cases.
- **Turn-5 `cacheStatus` reads `hit` in cold samples too.** The static prefix (tools and
  system prompt) has a 1-hour TTL, so the first call of turn 5 reads it while re-writing the
  history behind it. The offload reason (`cache_expired`) is the cold signal. Don't use
  turn-5 `cacheStatus` for this.
- **Sonnet's 30k cells are k=1**, and its 80k cells are k=3. The held-then-lost failure is
  stable at 80k (0/6 at today's ceiling).
- **A PDF's tokens depend on the model.** The "80k" fixture (82 pages) is about 128k tokens
  on Haiku 5.5 but about 214k input on Sonnet 4.6 (about 2.3k per page against 1.56k). A
  `document_budget` in tokens has to come from per-model facts, not one per-page constant.
- **Held document tokens come from `estimate_document_tokens`** (pages × 1,500), not row
  facts (#1966 had not merged). Each fixture page measures about 1,560 tokens, so the
  subtraction was about 4% low on a PDF.
- **The docx token column (3,258) is a bytes/4 estimate of the zipped file**, known to be
  about 9× low. The `native` and `text` arms send about 42k.

## Raw readouts

Generated by `eval_harness.py readout`, one per run directory.

<details><summary>Haiku 5.5, default ceiling (forced arms and ceiling cases)</summary>

| Case | Pass | $/sample | First token | Turn | Last turn | Delivery | Cuts/sample | Offloads/sample |
|---|---|---|---|---|---|---|---|---|
| arm_pdf_30k_text | 3/3 | $0.0009 | 6.0s | 18.8s | — | text 3 | 0.0 | 0.0 |
| arm_pdf_30k_native | 3/3 | $0.0018 | 12.4s | 28.1s | — | native 3 | 0.0 | 0.0 |
| arm_pdf_30k_on_demand | 3/3 | $0.0015 | 5.0s | 28.8s | — | on_demand 3 | 0.0 | 0.0 |
| arm_docx_30k_native | 3/3 | $0.0088 | 7.2s | 14.2s | — | native 3 | 0.0 | 0.0 |
| arm_docx_30k_text | 3/3 | $0.0109 | 9.9s | 35.7s | — | text 3 | 0.0 | 0.0 |
| arm_docx_30k_on_demand | 3/3 | $0.0007 | 8.2s | 33.5s | — | on_demand 3 | 0.0 | 0.0 |
| arm_html_30k_native | 3/3 | $0.0108 | 8.4s | 35.9s | — | native 3 | 0.0 | 0.0 |
| arm_html_30k_text | 3/3 | $0.0054 | 11.5s | 32.2s | — | text 3 | 0.0 | 0.0 |
| arm_html_30k_on_demand | 0/3 | $0.0007 | 5.0s | 40.5s | — | on_demand 3 | 0.0 | 0.0 |
| arm_html_30k_on_demand (with #1970) | 3/3 | $0.0006 | 1.8s | 17.1s | — | on_demand 3 | 0.0 | 0.0 |
| arm_pdf_80k_native | 3/3 | $0.0361 | 25.4s | 46.1s | — | native 3 | 0.0 | 0.0 |
| arm_pdf_80k_on_demand | 3/3 | $0.0010 | 6.2s | 40.7s | — | on_demand 3 | 0.0 | 0.0 |
| arm_md_150k_native | 3/3 | $0.0521 | 10.3s | 28.7s | — | native 3 | 0.0 | 0.0 |
| arm_md_150k_on_demand | 3/3 | $0.0009 | 8.0s | 43.1s | — | on_demand 3 | 0.0 | 0.0 |
| ceiling_pdf_30k_native_default_warm | 3/3 | $0.0067 | 11.4s | 25.6s | 9.0s / 26.1s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_30k_native_default_cold | 3/3 | $0.0060 | 11.6s | 22.2s | 1.7s / 10.4s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_30k_on_demand_default_warm | 3/3 | $0.0038 | 3.0s | 23.0s | 1.8s / 11.6s | on_demand 3 | 0.0 | 0.0 |
| ceiling_pdf_30k_on_demand_default_cold | 3/3 | $0.0051 | 2.2s | 24.1s | 1.6s / 16.7s | on_demand 3 | 0.0 | 1.0 (document_offload) |
| ceiling_pdf_80k_native_default_warm | 3/3 | $0.0222 | 9.3s | 24.3s | 2.9s / 16.0s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_80k_native_default_cold | 3/3 | $0.0130 | 9.0s | 15.5s | 1.7s / 10.3s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_80k_on_demand_default_warm | 3/3 | $0.0031 | 2.6s | 20.1s | 1.8s / 16.1s | on_demand 3 | 0.0 | 0.0 |
| ceiling_pdf_80k_on_demand_default_cold | 3/3 | $0.0039 | 2.3s | 18.8s | 1.8s / 13.1s | on_demand 3 | 0.0 | 0.7 (document_offload) |
| ceiling_pdf_150k_native_default_warm | 3/3 | $0.0377 | 9.4s | 19.4s | 2.3s / 11.3s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_150k_native_default_cold | 3/3 | $0.0173 | 10.4s | 14.6s | 2.5s / 9.9s | native 3 | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_150k_on_demand_default_warm | 3/3 | $0.0041 | 3.1s | 19.8s | 1.9s / 14.5s | on_demand 3 | 0.0 | 0.0 |
| ceiling_pdf_150k_on_demand_default_cold | 3/3 | $0.0064 | 1.7s | 18.3s | 2.4s / 11.1s | on_demand 3 | 0.0 | 1.0 (document_offload) |

</details>

<details><summary>Haiku 5.5, 100k with documents excluded</summary>

| Case | Pass | $/sample | First token | Turn | Last turn | Cuts/sample | Offloads/sample |
|---|---|---|---|---|---|---|---|
| ceiling_pdf_30k_native_…_warm | 3/3 | $0.0182 | 11.2s | 33.4s | 1.9s / 20.6s | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_30k_native_…_cold | 3/3 | $0.0183 | 11.9s | 28.7s | 3.8s / 14.8s | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_80k_native_…_warm | 3/3 | $0.0415 | 22.3s | 27.3s | 5.1s / 14.2s | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_80k_native_…_cold | 3/3 | $0.0119 | 8.2s | 17.6s | 3.1s / 15.5s | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_150k_native_…_warm | 3/3 | $0.0610 | 30.8s | 41.9s | 2.1s / 13.8s | 1.0 (checkpoint) | 0.0 |
| ceiling_pdf_150k_native_…_cold | 3/3 | $0.0170 | 10.2s | 21.6s | 3.2s / 8.7s | 1.0 (checkpoint) | 0.0 |

</details>

<details><summary>Sonnet 4.6, all three ceilings (k=1)</summary>

| Case | Ceiling | Pass | $/sample | First token | Last turn | Cuts | Offloads |
|---|---|---|---|---|---|---|---|
| ceiling_pdf_30k_native_*_warm | default | 1/1 | $0.4903 | 14.3s | 7.5s / 12.1s | 0 | 0 |
| ceiling_pdf_30k_native_*_cold | default | 1/1 | $0.4579 | 14.0s | 2.4s / 20.7s | 0 | 1 |
| ceiling_pdf_30k_on_demand_*_warm | default | 1/1 | $0.0564 | 4.9s | 1.7s / 20.5s | 0 | 0 |
| ceiling_pdf_30k_on_demand_*_cold | default | 1/1 | $0.1404 | 6.0s | 1.2s / 19.6s | 0 | 1 |
| ceiling_pdf_80k_native_*_warm | default | 0/1 | $0.1748 | 10.3s | 2.3s / 22.0s | 1 | 0 |
| ceiling_pdf_80k_native_*_cold | default | 0/1 | $0.1784 | 10.8s | 1.4s / 4.6s | 1 | 0 |
| ceiling_pdf_80k_on_demand_*_warm | default | 1/1 | $0.0618 | 1.9s | 8.1s / 21.1s | 0 | 0 |
| ceiling_pdf_80k_on_demand_*_cold | default | 1/1 | $0.0712 | 2.9s | 3.1s / 11.9s | 0 | 0 |
| ceiling_pdf_30k_native_*_warm | 200k | 1/1 | $0.4903 | 8.8s | 2.3s / 2.9s | 0 | 0 |
| ceiling_pdf_30k_native_*_cold | 200k | 1/1 | $0.4581 | 13.1s | 1.1s / 9.1s | 0 | 1 |
| ceiling_pdf_80k_native_*_warm | 200k | 1/1 | $1.0596 | 20.5s | 1.0s / 9.8s | 0 | 1 |
| ceiling_pdf_80k_native_*_cold | 200k | 1/1 | $1.0575 | 25.0s | 1.0s / 6.8s | 0 | 1 |
| ceiling_pdf_30k_native_*_warm | 100k excl. | 1/1 | $0.4906 | 21.3s | 5.7s / 7.8s | 0 | 0 |
| ceiling_pdf_30k_native_*_cold | 100k excl. | 1/1 | $0.4577 | 21.8s | 1.2s / 9.1s | 0 | 1 |
| ceiling_pdf_80k_native_*_warm | 100k excl. | 1/1 | $1.2434 | 21.4s | 10.5s / 13.7s | 0 | 0 |
| ceiling_pdf_80k_native_*_cold | 100k excl. | 1/1 | $1.1254 | 21.4s | 3.7s / 13.7s | 0 | 1 |

</details>

## Validation on dev (after #1975 and #1978 deployed)

- **The family runs end to end against dev's build:** every default-ceiling case
  (`auto`, forced arms, ceiling arms, whole-document) at k=1 on Haiku 5.5 through
  presign: **65/65**, $1.21. That includes `arm_html_30k_on_demand`, which passes on
  the deployed #1970.
- **#1978 is live:** a kept single-turn sample whose input passed Haiku 5.5's 50k ceiling
  without a cut stores `policy = {ceiling: 50000, hardCeiling: 70000, contextWindow:
  100000, …}`. Before the fix, `policy` existed only after a cut, and the next turn's
  offload judged 100k. In the six-turn ceiling cases this changes nothing you can see:
  the attached document stays pinned for its next turns, and compaction cuts it first.
- **The Sonnet 80k document at a 200k ceiling is not held, and should not be.** It is
  ~214k input, over the 200k ceiling, so `over_ceiling` offloads it. That behaviour was
  correct before #1978 and is the same after it.

## Left open

- ~~The Sonnet 80k warm offload at a 200k ceiling.~~ Explained: it was a real
  `over_ceiling` (214k input against a 200k ceiling), and it is the same with #1978. #1978
  fixes a separate bug the investigation found: before a conversation's first cut, the
  offload judged a window-less 100k threshold instead of the model's own ceiling.
- ~~Re-run `whole_summarise_pdf_150k_on_demand` after #1979.~~ 3/3 on dev's deployed
  build. One of the three samples paged through the whole PDF (11 reads, 365k tokens),
  which is still under the 397k a held copy costs on that single turn.
- The Sonnet 4.6 `auto` baseline ran (58/66; see `attachment-delivery-baseline-2026-11.md`).
- A free-form summary of unmarked prose (judge-scored, advisory).
- Phase 2b should swap `held_document_tokens` to row facts if the switch is kept. With this
  readout, the recommendation is to delete it instead.
