# Source provenance verification: whether a cited passage exists, and whether it says what we claim

**Status:** In development, behind `SOURCE_PROVENANCE_ENABLED` (off by default; only
the literal `"true"` enables). Spec approved 2026-10-20. PR-1 (the pure module, its
tests, the flag) and PR-2 (`verify_sources`, the SSE event, the `SRCV#` store and its
replay) implemented; not yet dev-validated. PR-3 (the SPA render) not started, so the
states are backend-observable only. Decisions recorded in §9.
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-10-20
**Targets:** `develop`
**Motivating gap:** G2 of `_local-notes/vandalizer-parity-and-gap-analysis.md`. Every
cited answer this platform produces today asserts a source we have not checked
supports the claim. The design is ported from Vandalizer (`extraction_sources.py`,
`page_locator.py` — GPLv3, read for its reasoning, not copied); where we differ from
it, §9 says why.
**Related:** `docs/specs/document-context-offload.md` (the digest and `document_read`
this attaches to), `docs/specs/large-document-attachments.md` (§4C–D the `_derived/`
sidecars this reads, §4F the attachments-KB `query` mode this deliberately does not
cover), `docs/specs/turn-path-ttft.md` (why none of this runs before the first token).

---

## 1. Problem

A model that invents an award amount and pairs it with a real sentence from the
budget section produces output indistinguishable from a correct answer. We have no
way to tell the two apart, and no surface that admits it.

Concretely, across every path a document reaches a model on:

| Path | What the citation rests on | What is verified |
|---|---|---|
| Inline attachment (Converse `document` block) | The model's own reading of page images + text | Nothing. No page markers exist on this path at all |
| `document_read` `pages` / `pages_text` | A page range the *tool* chose, so the page number is real | That the model's quote came from it: nothing |
| `document_read` `pattern` | A measured page per match ([`_scan_pages`](../../backend/src/apis/shared/files/document_read.py:806) sets `entry["page"]`) | The match is real. Whether a value the model later asserts appears in it: nothing |
| `document_read` `query` (attachments KB) | Chunk-level `_excerpt_page_number` ([`attachment_search.py:53`](../../backend/src/apis/shared/files/attachment_search.py:53)) | Nothing, and the page is the chunk's, not the passage's |
| Assistant / project KB chat | Chunk metadata from the managed KB | Nothing |

Two distinct failures hide in that table, and conflating them is the trap:

1. **The quote may not exist.** The model may fabricate a passage outright.
2. **The quote may exist and not support the value.** This is the one that survives
   the obvious fix. A located quote is evidence that the model read the document. It
   is not evidence that the document says what the answer says.

Vandalizer shipped (1) first and their own source comments name the result
*provenance theatre*: a fabricated award amount carrying any real sentence from the
budget section earned the same "traced to p. 12" chip as a correct one. That is the
failure mode a research administrator has already been burned by elsewhere, and it is
worse than no badge, because a badge is a claim we are making on the model's behalf.

## 2. What already exists (verified in code, 2026-10-20)

More than the gap analysis assumed. Three corrections, each of which changes the
build:

**2.1 Per-unit text is already extracted everywhere, through one function.**
[`_units_for(fmt, raw)`](../../backend/src/apis/shared/files/document_digest.py:168)
returns `(unit_name, [text_per_unit])` — `page` for PDF (via `_open_pdf` +
`_pdf_page_text`, i.e. `pypdfium2`), `paragraph` for DOCX, `line` for the text
family. Every document-reading surface already goes through it or its two halves.

**2.2 Page markers are already persisted — we simply never called them that.**
[`text.jsonl`](../../backend/src/apis/shared/files/large_document.py:136) is one JSON
record per unit, 1-indexed, `{"<unit>": n, "text": ...}`, written by the
large-document prepare Lambda ([`handler.py:239`](../../backend/src/lambdas/large_document_prepare/handler.py:239))
and read back by `decode_text_layer`. `UNIT_FOR_FORMAT` pins `pdf → page`. A prepared
PDF's measured page boundaries are therefore already on disk, and the character
offsets a quote search needs are *derivable from them exactly* — concatenate the unit
texts in order and you have both the text and its boundaries.

So the gap analysis' "we must persist page markers" is wrong, and acting on it would
add a redundant sidecar. **Slice one persists no new document artifact.** What is
missing is a locator *model* (what kind of unit, and how trustworthy its position is)
and the verification itself.

**2.3 The chunk-level page lives in `attachment_search.py`, not `managed_backend.py`.**
`_excerpt_page_number` appears exactly once in the tree, on the attachments-KB path.
`managed_backend.py` never mentions a page. §7 explains why neither KB path is in
slice one.

## 3. The two questions, and why they never collapse

The whole design rests on asking two things and storing the answers apart.

**Q1 — Does the quoted passage exist in the document?** Resolved by
unicode-normalized string search against the document's own extracted text. Answerable
cheaply, deterministically, with no model and no network beyond fetching the text.

**Q2 — Does the asserted value actually appear in that passage?** This is the
load-bearing one. Q1 alone certifies the weaker proposition, and a consumer that only
knows about Q1 will quietly re-certify it.

Q2 is only asked once Q1 says yes. A fabricated passage that happens to contain the
value proves nothing, so an unlocated quote is never promoted.

## 4. The four states

One reader-facing state, derived once, and the only thing any consumer may read.

| State | Meaning | Renders as |
|---|---|---|
| `supported` | The value appears in a passage located in the document | A plain locator — `p. 12`. **The only state that earns one** |
| `quote_unsupported` | The passage is real; the value is not in it | The hallucination signal. Must look like one: a warning affordance naming the document and the passage, never a page badge |
| `unassessed` | The passage is real, but the value is not the kind of thing findable in it — an enum answer that maps the prose rather than quoting it, a non-scalar, a quote too long to scan | Traceable, not confirmable: the locator with an explicit "not confirmed" qualifier |
| `unverified` | No passage could be located, or none was given | "No source found". Nothing here is traced |

### 4.1 The state is the API, and there is no way to build one by hand

Vandalizer exposes `support_state(verified, value_supported)` — a public function
taking the two booleans. That is itself the misuse vector: any caller can pass a pair
it made up, or pass `verified` twice. We close it at the type level instead:

* `SupportState` values are produced **only** by `verify_claim(...)`. There is no
  public function that accepts the pair, so there is nothing to call wrongly.
* The two booleans are **not fields on the result**. Nothing downstream can branch on
  `quote_located` and reach its own verdict, because nothing downstream is given it.
* What is recorded for measurement instead is `method` — the single reason the state
  came out as it did (`no_quote`, `quote_not_located`, `literal`, `numeric`, `date`,
  `no_match`, `enum_field`, `non_scalar`, `quote_too_long`, `empty_value`,
  `document_unreadable`). It carries strictly more information than the boolean pair
  and, unlike the pair, it is not a verdict anyone can re-derive a different answer
  from.

A `tests/architecture/` test asserts no module outside `source_provenance.py` names
the state constants in a conditional that could re-derive them — specifically, that no
other module imports anything from it except `verify_claim`, the result type, the
state constants, and the renderers.

## 5. The locator, and how an estimated position is represented

```python
@dataclass(frozen=True)
class Locator:
    kind: str        # "page" | "paragraph" | "line" — the unit the document HAS
    value: int       # 1-indexed
    precision: str   # "measured" | "estimated" — REQUIRED, always serialized
```

Two honesty axes, not one:

**`kind`** stops us inventing a page for a document that has none. A `.docx` reaches
`document_read` as paragraphs; `pypdfium2` is never involved; there is no page
boundary to measure. Rendering `p. 3` there is as invented as rendering an
interpolated page. So the renderer is `p. 12` / `¶ 34` / `line 88` and nothing else.

**`precision`** is where Vandalizer's bug was, and we answer it at the data model
rather than with a detector. Their `approximate` flag was *optional*; markers written
before it existed carried no key, read as exact, and a scanned 400-page package
rendered confident `p. 234` citations off evenly-spread guesses. Their fix,
`with_marker_provenance()`, recognizes the interpolator's uniform-spacing signature
and restores the flag — a retrofit that works because its asymmetry is right (an
unnecessary tilde costs a tilde; invented precision costs trust), but a retrofit all
the same.

We do not need it, because `pypdfium2` measures every PDF page boundary and
`text.jsonl` records them. But designing only for that case is how the optional flag
happened. So `precision` is a **required** field, always written, never defaulted on
read. A writer cannot omit it; a reader cannot guess. Renderers hedge an `estimated`
page as `p. ~12`.

Paths that would legitimately produce `estimated`, named now so the field is not
theoretical:

| Path | Why estimated |
|---|---|
| Attachments KB / any managed-parser chunk | `_excerpt_page_number` is the *chunk's* page. A quote inside a chunk spanning a boundary resolves to the chunk's first page, which may not be the quote's |
| Textract or any future OCR without page structure | Positions spread across a known page count (exactly Vandalizer's interpolator) |
| A quote straddling a unit boundary | See §6.3 — reported at the unit it *starts* on, which is a convention, not a measurement |

## 6. The verification module

`backend/src/apis/shared/files/source_provenance.py`. **Pure string and offset work:
stdlib only — no boto, no DynamoDB, no Beanie, no model call, no import from any
other `apis.shared` package.** That is what makes it safe to import from app-api, the
Runtime and a Lambda alike, and trivially testable. Like Vandalizer we duplicate a
little number/date parsing in miniature rather than importing a heavier validator;
the trade buys the purity.

### 6.1 Public surface

```python
def verify_claim(value, quote, document: DocumentUnits) -> SourceVerification
def verify_claims(claims: Sequence[Claim], document: DocumentUnits) -> list[SourceVerification]
def render_locator(locator: Locator | None) -> str | None
def document_units(unit: str, texts: Sequence[str], *, precision: str) -> DocumentUnits
```

`DocumentUnits` normalizes once and caches the index map, so N claims against one
document pay normalization once — the reason `verify_claims` exists as its own entry
point.

### 6.2 Normalization

Applied to both the document text and the quote before matching, because LLM output
routinely differs from a PDF text layer by exactly these:

* Curly quotes → straight; dash and minus variants → `-`; NBSP / figure / thin /
  narrow-no-break space → space; `fi` / `fl` ligatures → their letters.
* Dropped entirely: soft hyphen, BOM, zero-width space / non-joiner / joiner.
* Lowercased, whitespace-collapsed, leading/trailing whitespace trimmed.
* An index map carries each normalized character back to its original offset, so a
  match in normalized space projects to a real position.

Exact match is tried first and short-circuits; normalization is the fallback.

### 6.3 Locating

The document's unit texts are concatenated with a single separator and a parallel
`unit_starts` array, so an offset resolves to a unit by binary search. This is
Vandalizer's marker list, derived rather than persisted (§2.2).

Searching the concatenation rather than each unit in turn is deliberate: a quote
spanning a page break exists in neither page and would read as `unverified` — a false
hallucination signal, which is the one kind of error this feature must not
manufacture. A straddling match is reported at the unit it **starts** on, with
`precision: "estimated"`, because the single number we return is a convention about a
passage that genuinely occupies two.

### 6.4 The value check

Deliberately strict. Only three things count, and a literal match must sit on value
boundaries:

* **Literal** — the normalized value occurs in the normalized quote as a whole value.
  Every occurrence is scanned, because the first may be a fragment while a later one
  is genuine. Boundaries matter: without them `5000000` "supports" `500000`,
  `$500,000.99` supports `$500,000`, and "John Smithson" supports "John Smith" —
  over-claiming in exactly the direction this check exists to catch. Numeric
  boundaries reject an adjacent digit, and reject `.` or `,` only when a digit
  follows, so ordinary sentence punctuation still ends a value.
* **Numeric** — equal as numbers, tolerating currency symbols, thousands commas and
  surrounding whitespace. **The percent sign travels with the value**: `$50` must not
  be certified by a quote reading "50% of MTDC", so `(50.0, is_percent)` is the
  compared quantity, never the bare number. Thousands groups are matched only in
  thousands position — a permissive `[\d,]*` swallows "1, 2 and 3" as one token which
  then parses as `123`.
* **Date** — equal as dates across the common written forms.

The question is **not asked** (state `unassessed`) when: there is no quote; the quote
is over 4,000 characters (a quote is a passage, not a document — and the date scan
backtracks badly on long space-free text, which is model-controlled input); the value
is a not-found sentinel (`n/a`, `none`, `unknown`, …) with nothing to support; the
field is an enum whose allowed values map the prose rather than quote it; or the value
is a list, dict or bool with no verbatim form.

**Three weaknesses to read any measured distribution against**, recorded here so a
`True` is not over-read:

1. Very short values ("1", "A") match almost any passage by coincidence.
2. A value that is a judgment about the passage rather than a span of it ("Yes" for
   "cost sharing is required") reads as unsupported unless the field is an enum.
3. The check asks whether the value appears *somewhere* in the passage, **not whether
   the passage assigns it to this field**. A "Direct Costs" value is supported by a
   quote labelling that same figure indirect, and a year (2024) by a dollar amount
   ($2,024). Reading `supported` as "the quote says this field is this value"
   over-reads it. The honest claim is: *this number is in this passage, on this page*.

A multi-part value assembled from a sentence ("Jane Smith, Chemistry") reads as
unsupported. That is the conservative direction, and the right one while the state is
new.

## 7. What slice one covers, and what it does not

**Covered: documents reachable through `document_read`** — the session's own
attachments, prepared and not.

* Prepared (reference-class, `_derived/` sidecars): text comes from `text.jsonl` via
  `decode_text_layer`. One S3 GET. `precision: "measured"` for PDF.
* Not prepared (inline-class): bytes are fetched and `_units_for` runs, the same work
  `document_read` already does per call, bounded by the existing
  `DOCUMENT_READ_MAX_SOURCE_BYTES` (8 MB).

**Not covered, with reasons:**

| Path | Why not |
|---|---|
| Assistant / project KB chat (`rag_service`) | Retrieval returns a *chunk*, not the document. Verifying a quote against the chunk answers a different and much weaker question — "is this in the context we gave you" — and badging it as document provenance would be the same over-claim in a new place. Doing it properly means fetching the source document per citation, which is a retrieval-path cost decision of its own |
| `document_read` `query` (attachments KB) | Same, plus the page is the chunk's. This is where `precision: "estimated"` will first be used, when it lands |
| Conversation search results | Cites a past turn, not a document. Different claim entirely |
| The model's prose, verified automatically | Deciding which number in a sentence is "the asserted value for this quote" is not solvable deterministically. Guesswork dressed as verification is the failure mode this spec exists to kill, so the claim is explicit or it is not made (§8) |
| Scanned / image-only PDFs | No text layer, so nothing to search; `method: "document_unreadable"`, state `unverified`. G3 of the gap analysis is the fix, and it is a prerequisite for provenance on scanned documents, not a part of this |

## 8. How a claim reaches us: `verify_sources`

The two questions need a *value* and a *quote* as separately identified inputs, and
only the model knows which value it is asserting. So the claim is explicit.

A new builtin tool, `backend/src/agents/builtin_tools/verify_sources_tool.py`:

```
verify_sources(upload_id, claims: [{id, value, quote}])
```

**Where it is built.** Inside the existing
[`_build_document_tools`](../../backend/src/apis/inference_api/chat/routes.py:1218)
gate, beside `document_read`. That gate already answers "does this session have a
readable document", already feeds the agent cache key as `has_document_tools`, and is
deliberately **not** gated on `enabled_tools` — the governing capability is the user's
own attachment. Consequences, all good:

* The spec reaches `toolConfig` only in sessions that have a document, and is constant
  per deployment, so it is one prefix shape, not a per-session one.
* **No new agent-cache key element.** The flag is a deployment constant and the gate's
  answer is already in the key.
* No RBAC catalog entry and no `INJECTED_TOOL_IDS` change — same reasoning
  `document_read` records.

**What goes back to the model: the state, and nothing else.** `{claims: [{id, state}]}`
— one short word per claim. Not the quote (it wrote it), not the locator, not the
method.

* A model that cannot see `quote_unsupported` cannot correct it, and correcting it is
  the most valuable thing that can happen in the turn.
* It is ~3 tokens per claim. This is the single deliberate exception to "verification
  output stays off the content blocks", and it is stated as an exception rather than
  smuggled: a tool's result *is* the Converse payload and *is* in the cacheable
  prefix.
* It is also adversarially weak on its own — a model that fabricated a value can
  fabricate a better-matching quote — which is exactly why the user-facing render
  comes from the SSE event regardless of what the model does next. Whether returning
  the state earns its tokens is the one decision in this spec worth revisiting with
  measurement (§10).

**What does not go back to the model** — quote, locator, `precision`, `method`,
document title — travels on a new SSE event and a side-channel row only, the
`tool_group_summary` precedent
([`tool_summaries/store.py`](../../backend/src/apis/shared/tool_summaries/store.py)):
display metadata written into the cacheable prefix is paid at model rates on every
later turn, forever.

### 8.1 SSE: `source_provenance`

```
{type, sessionId, toolUseId, claims: [
  {id, state, method, quote, locator: {kind, value, precision} | null,
   uploadId, filename}
]}
```

Emitted from `stream_coordinator` on the same drain as `tool_group_summary` (which
already runs alongside the agent stream and is flushed once before `done`), so the
rail updates while the turn continues. Persisted as `SRCV#<toolUseId>` rows in
`sessions-metadata` on the existing `SessionLookupIndex` GSI — **zero new infra**,
same as `TSUM#` — and replayed by
[`messages.py`](../../backend/src/apis/shared/sessions/messages.py:516) as
`sourceProvenance`, because the event never re-streams.

### 8.2 Where it runs, against the TTFT budget

Nowhere near the first token. `verify_sources` runs only when the model calls it,
which is after it has read a document and formed an answer. There is **no
`BeforeInvocationEvent` and no `BeforeModelCallEvent` hook** — the latter fires before
every model call, so its cost would repeat per tool round. The tool spec's contribution
to the prompt is a constant, already-gated string. The verification itself is pure CPU
over text we fetch on the tool's own call.

## 9. Decisions, with reasons

**D1 — Two booleans stored, one state exposed; and no public constructor for the
state.** Vandalizer's `support_state(verified, value_supported)` is public, so a
caller can pass a pair it invented. We expose only `verify_claim`, and drop the
booleans from the result in favour of `method`, which carries more and re-derives
nothing. This is a genuine improvement on the ported design, not a stylistic change.

**D2 — `precision` is required and always serialized, never inferred.** Vandalizer's
`approximate` was optional, legacy rows read as exact, and a scanned package rendered
confident pages off guesses; `with_marker_provenance()` is the retrofit. A required
field makes the retrofit unnecessary. Rejected: omitting `"measured"` to save bytes —
that reintroduces exactly the ambiguity the field exists to remove, and these rows are
tiny and off the prefix.

**D3 — `kind` as well as `precision`.** A `.docx` has paragraphs, not pages. Inventing
a page for one is the same error as inventing a position, and the ported design does
not cover it because their reader always produced pages.

**D4 — No new persisted document artifact.** `text.jsonl` already records measured
page boundaries; offsets are derived from it exactly. A second marker sidecar would be
redundant and could disagree with the first.

**D5 — Search the concatenation, not each unit.** A quote spanning a page break would
otherwise read `unverified` — a manufactured hallucination signal, the one error this
feature must not produce.

**D6 — The claim is explicit, via a tool.** Inferring which value a quote supports
from prose is not deterministic. An inferred claim verified confidently is provenance
theatre with extra steps.

**D7 — The state goes back to the model; the provenance does not.** Cheapest possible
path to self-correction (§8), while the payload that would cost model rates every turn
stays on the side channel.

**D8 — Neither KB path in slice one.** A chunk is not the document; verifying against
it answers a weaker question under the same badge (§7).

**D9 — `unassessed` is a first-class state, not a quiet `supported`.** An unmeasured
value must not inherit the strongest badge by default. This is the rule Vandalizer
had to add for rows written before `value_supported` existed, and the reason is the
same for new rows whose check does not apply.

## 10. The flag

| | |
|---|---|
| Backend | `SOURCE_PROVENANCE_ENABLED`, read by `source_provenance_enabled()` in `apis/shared/feature_flags.py`. **In-development: only the literal `"true"` enables**; unset arrives as `""` |
| Set on | The AgentCore Runtime (builds the tool, verifies, emits the event) and app-api (replays `SRCV#` rows on `GET /messages`) |
| CDK | `sourceProvenance.enabled` in `infrastructure/lib/config.ts` from `CDK_SOURCE_PROVENANCE_ENABLED`; forwarded in `.github/workflows/platform.yml`. Written to the Runtime's environment **only while on**, so the default costs nothing against the 2,560-byte V2 budget. A `config.test.ts` case pins both sides |
| SPA | `features.sourceProvenance`, `false` in all three `environment*.ts`. Added in slice one so the two sides can land independently; the render is slice two (§11) |
| Kind | **Feature switch** — permanent. It adds a tool to `toolConfig`, changes what the product asserts to a user about a document, and is a judgement a deployment may reasonably decline. It is configuration, not a rollout hedge, so it does not retire. When the feature is finished, flipping it to default-on with an `=false` kill switch is its own deliberate change (`!= "false"`, CDK default `true`, Runtime copy written only while off) |

## 11. PR sequence

| PR | Contents |
|---|---|
| PR-0 | This spec |
| PR-1 ✅ | `source_provenance.py` (pure) + unit tests: all four states, every `method`, unicode-normalization edge cases, boundary over-claims (`5000000`/`500000`, `$500,000.99`, "John Smithson"), percent-vs-bare-number, straddling quotes, `kind` per format, `precision` round-trip. The flag, CDK wiring and CDK test. Nothing wired to a turn |
| PR-2 ✅ | `verify_sources` tool inside the `_build_document_tools` gate; `document_provenance_units` for prepared and unprepared documents; the `source_provenance` SSE event drained in `stream_coordinator`; the `SRCV#` store and its `GET /messages` replay as `sourceProvenance`. Plus `tests/architecture/test_source_provenance_state_boundary.py`, which keeps the state derivable in one place and the module pure. Backend complete and dev-verifiable by reading the stream |
| PR-3 | SPA render behind `features.sourceProvenance`: a plain locator for `supported`, a warning affordance for `quote_unsupported`, a "not confirmed" qualifier for `unassessed`, "no source found" for `unverified` |

PR-1 and PR-2 are slice one. PR-3 is stated as out of slice one rather than assumed.

## 12. How we'll know

* **Correctness is unit-testable and is tested**, because the module is pure. The four
  states and every `method` have cases.
* **On dev**, attach a solicitation PDF, ask for an award amount with a quote, and read
  the `source_provenance` frame: `supported` with a measured page. Then ask for a value
  the document does not contain and confirm `quote_unsupported` — the case that
  matters, and the one that cannot be faked by reading a happy path.
* **A reload replays it.** `GET /messages` returns `sourceProvenance`.
* **Cost.** A turn with `verify_sources` must show no change in
  `systemPromptHash`/`toolConfigHash` between consecutive calls, and no `partial_miss`
  attributable to it (`GET /admin/costs/sessions/{id}/calls`). The tool's spec is
  constant; only the per-claim state words are new input.
* **Distribution before badges.** `method` is recorded from PR-2, so the split across
  `literal` / `numeric` / `date` / `no_match` / the `unassessed` reasons is measurable
  before PR-3 renders anything — and before anyone argues for a partial-coverage rule
  (§6.4), which must have evidence behind it.
* **The readout is content-free EMF**, one record per call in
  `AgentCoreStack/Compaction` beside the `document_read` records: `SourceVerify` and
  `SourceVerifyClaims`, with properties `format` and a count per state. No quote,
  value or filename is ever emitted, so the honesty measurement carries none of the
  document. It rides `PROMPT_CACHE_OBSERVABILITY_ENABLED` like its neighbours.

## 13. Non-goals

* Typed structured extraction (G1). This is sequenced first on purpose: it improves
  every citation the platform already gives, and it is what would make extraction
  trustworthy later. Nothing here assumes extraction exists or builds toward it.
* OCR (G3). A scanned document has no text layer and reads `unverified` with
  `method: "document_unreadable"`, honestly.
* Automatic verification of free prose (§7, D6).
* Verifying that a passage *assigns* a value to a field (§6.4 weakness 3). The claim
  we make is "this value is in this passage, on this page", and the renderer must not
  say more than that.

## Sources

* `_local-notes/vandalizer-parity-and-gap-analysis.md` §G2, §G3 (the path/OCR table).
* Vandalizer `backend/app/services/extraction_sources.py`, `page_locator.py` (GPLv3) —
  read for the reasoning in their module docstrings. Design ported, source not.
* Verified in this tree 2026-10-20: `document_read.py` (`_open_pdf`,
  `_pdf_page_text`, `_scan_pages`, `_pages_text`), `document_digest.py` (`_units_for`),
  `large_document.py` (`encode_text_layer`, `UNIT_FOR_FORMAT`),
  `large_document_prepare/handler.py:239`, `attachment_search.py:53`,
  `inference_api/chat/routes.py:1218`, `tool_summaries/store.py`,
  `streaming/stream_coordinator.py:3133`, `sessions/messages.py:516`.
