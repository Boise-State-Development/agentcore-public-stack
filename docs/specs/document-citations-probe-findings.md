# G3 — the citations baseline probe, run

**Date:** August 12, 2026 · **Gate:** G3 (`cost-effectiveness-roadmap.md`)
**Required by:** `document-offload-evaluation.md` §1
**Script:** `backend/scripts/probe_document_citations.py` (self-contained;
builds its own corpus, no fixtures, no user content)

**Result: G3 clears, and it clears by falsifying the premise it was built on.
Production already reads charts, image tables and scanned pages at full
fidelity, with no citations config. Citations are a *text-layer* feature, not
the switch that turns on visual understanding.**

---

## 1. What the gate was for

The #836 validation pass established that we send **no citations config in
production** — `DocumentHandler.create_content_block` emits `format` / `name` /
`source.bytes` and nothing else. It then reasoned that on Bedrock the visual
(page-image) PDF path is *tied to* citations-enabled document handling, and
concluded we did not know whether production could see charts at all.

That mattered because every offload quality comparison inherits its baseline
from arm A. If arm A were blind to figures, "the digest lost the chart" would
be unmeasurable — you cannot lose what was never there.

## 2. Method

Five documents, fourteen questions, each asked twice — once with the document
block exactly as production builds it, once with `citations: {enabled: True}`
added and **nothing else changed**.

| document | construction | what a correct answer proves |
|---|---|---|
| `chart_only.pdf` | bar chart, image-only | values read off an axis from pixels |
| `table_in_image.pdf` | table rendered as an image | cell lookup with no text layer |
| `scanned_page.pdf` | memo text rasterized, rotated 0.7° | OCR-equivalent reading |
| `text_layer.pdf` | real PDF text object, no image | **canary** — if this fails the probe is broken |
| `mixed_text_and_chart.pdf` | p1 real text layer, p2 embedded JPEG chart | the realistic production shape |

The four image-only PDFs were verified to carry **no extractable text**
(`strings` over the raw bytes finds none of the ground-truth values), so a
correct answer cannot come from a text layer. Chart-value questions are scored
with a tolerance band because the bars carry no printed labels — reading them
means estimating against the axis, and an exact-match bar would be unfair.

Models: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (the dev default) and
`us.anthropic.claude-sonnet-5`.

## 3. Results

**Both models, both arms: 14/14 correct.**

| family | bare | cited |
|---|---|---|
| chart-value (2) · chart-compare · chart-structure | 4/4 | 4/4 |
| table-cell (2) · table-compare | 3/3 | 3/3 |
| scan-fact (2) | 2/2 | 2/2 |
| text-layer canary | 1/1 | 1/1 |
| mixed: text · chart (2) · cross-page | 4/4 | 4/4 |

Nothing diverged between arms on correctness. The model read 631 off an
unlabeled bar, found `$401` in a table that exists only as pixels, and pulled
`PR-2291` off a rotated scan.

### Where citations actually fired

This is the finding with teeth. Of the 14 responses in the **cited** arm,
exactly three carried `citationsContent` — and *the same three on both models*:

| id | document | question draws on | cited |
|---|---|---|---|
| `k1` | `text_layer.pdf` | the text layer | ✅ |
| `m1` | `mixed_text_and_chart.pdf` | page-1 prose | ✅ |
| `m4` | `mixed_text_and_chart.pdf` | page-1 prose **and** page-2 figure | ✅ |
| `m2`, `m3` | `mixed_text_and_chart.pdf` | page-2 figure only | ❌ |
| `c1`–`c4`, `t1`–`t3`, `s1`–`s2` | the three image-only PDFs | pixels only | ❌ |

Every image-only document returned plain `text` blocks and **no citations at
all**, in both models, with citations explicitly enabled. The mixed document is
the clean demonstration: same request, same document, citations on — the two
questions answerable only from the figure came back uncited, while the two that
touch the prose came back cited.

**Citations require a text layer to cite.** They are not a visual-fidelity
switch, and enabling them does not make the model see more. Both capabilities
coexist happily in one document: page-1 prose answers arrive cited with a page
location, page-2 figure answers arrive correct and uncited.

### The citation block's shape

```
citationsContent:
  content:   [{text: "3,400 pounds"}]          <- the answer
  citations: [{title: "text layer canary",
               sourceContent: [{text: "...verbatim source excerpt..."}],
               location: {documentPage: {documentIndex: 0, start: 1, end: 2}}}]
```

Two things follow.

⚠️ **With citations enabled the answer text moves *inside* `citationsContent`
and the top-level `text` blocks go empty.** Any consumer that reads only
`text` sees a blank answer. This probe hit it directly — a correct answer
scored as a miss until the extractor was fixed. Anything that would consume a
citations-enabled response (the SSE content path, the eval harness scorer,
`_BEDROCK_CONTENT_BLOCK_KEYS`) has to handle both shapes before citations
could be turned on anywhere.

✅ `location.documentPage` gives `{documentIndex, start, end}` — which is
exactly the page-identity primitive `document-offload-evaluation.md` §2.3 needs
for the `document_read(page_range=…)` remapping test. That family is buildable
against a real page number rather than an inferred one.

## 4. What this changes

**For the offload work (#836):**

1. **The G3 baseline is "full visual fidelity, uncited."** Arms B and C are
   measured against a model that today reads charts, image tables and scans.
   The bar is higher than the spec assumed.
2. **The spec's "native blocks, never flattened text" rule is now empirically
   justified**, not precautionary. A text-only digest demonstrably discards
   capability the current path has — we measured the capability rather than
   assuming it.
3. **The citation-loss concern narrows sharply.** Offloading an *image-only*
   document costs no citations, because there were never any. It is only
   text-layer documents where offload trades away attribution — and since the
   product does not send the citations config today, that trade currently costs
   nothing at all.
4. **The dual-encoding premise is confirmed but re-attributed.** PDFs do get
   visual understanding. It simply is not gated on the citations config, which
   is what the spec had wrong.

**For the product, separately:** "should we enable citations?" is now a clean,
independent question about answer attribution — worth asking on its own merits,
with the response-shape migration in §3 as its real cost. It is **not** a
prerequisite for anything in the offload arc, and the offload arc should stop
treating it as one.

## 5. Corrections to existing documents

- `document-context-offload.md` §1 / `document-context-offload-validation.md`:
  the claim that the visual PDF path is tied to citations-enabled handling is
  **disproven**. Visual understanding is unconditional.
- `document-offload-evaluation.md` §1: the required baseline experiment is
  **run**; §2.3's note that "arm A scores here reflect the §1 baseline probe"
  resolves to *arm A is at full fidelity*, so the citation family gates B and C
  against each other, as that section anticipated.

## 6. Loose end worth knowing

`us.anthropic.claude-sonnet-5` rejects `temperature` outright
(`ValidationException: temperature is deprecated for this model`). Our main
chat path only sends it when a value is explicitly set, and the Nova Micro
title path is unaffected — so this is **latent, not a live defect**. But an
admin pinning `temperature` on a Claude-5-family model via the inference-params
surface would 400 the turn. Worth a guard when someone is next in that code.
