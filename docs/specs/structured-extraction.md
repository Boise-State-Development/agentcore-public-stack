# Structured extraction: a named field set over a document, as rows that carry their own verdict

**Status:** Spec only. Nothing implemented. Behind `DOCUMENT_EXTRACT_ENABLED` (off by
default; only the literal `"true"` enables) once PR-1 lands. **Implementation is
blocked on source-provenance verification (G2) reaching `develop`** — §13 names
exactly what collides and why. Decisions recorded in §11.
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-10-20
**Targets:** `develop`
**Motivating gap:** G1 of `_local-notes/vandalizer-parity-and-gap-analysis.md`. The
platform can be asked to pull fields out of a document and will answer in prose or a
freehand markdown table it wrote itself. There is no field-schema concept, no
extraction result store, no typed-results surface, and — verified by grep — no use of
constrained structured output anywhere in the tree. The design is ported from
Vandalizer (`models/search_set.py`, `services/extraction_engine.py` — GPLv3, read for
the reasoning in their field model, not copied); where we differ, §11 says why.
**Related:** `docs/specs/source-provenance-verification.md` (G2 — every extracted
value is a claim in its sense; this spec composes with its module and must not
duplicate it), `docs/specs/document-context-offload.md` (`document_read` and the
digest), `docs/specs/large-document-attachments.md` (§4C–D the `_derived/` sidecars
the text comes from), `docs/specs/turn-path-ttft.md` (why none of this runs before the
first token), `docs/specs/shared-projects.md` §3.3 (the Outputs library the results
land in).

---

## 1. Problem

A research-administration office processes hundreds of funding solicitations and award
packages per cycle, and for each one it needs the same twenty answers: submission
deadline, budget cap, cost-share requirement, allowable indirect rate, PI eligibility,
page limits. Today the platform can be asked for those, and it will answer. What it
cannot do is answer the *same way twice*, in a shape anyone can check.

Three things are missing, and they are missing together:

1. **A named, typed, reusable field set.** The office has to re-describe what it wants
   on every document, in prose, and gets whatever the model felt like naming the
   columns that time.
2. **Rows.** The output is a paragraph, or a table the model composed freehand — not a
   structure anything downstream can read, diff, sort, or merge with last week's.
3. **A per-value verdict.** This is the one that makes the other two worth shipping.
   G2's problem statement applies with full force here, and worse: a table of twenty
   values is twenty assertions presented as one tidy object, and tidiness reads as
   confidence. An extraction surface without honest per-value provenance is
   *provenance theatre at scale* — which is precisely why G1 is sequenced after G2 and
   not before it.

## 2. What already exists (verified in code, 2026-10-20)

Four corrections to the framing this spec was handed. Each changes the build.

**2.1 `_build_document_tools` is the wrong attach point, and the collision with G2
there is imaginary.** The gate at
[`routes.py:1218`](../../backend/src/apis/inference_api/chat/routes.py:1218) has only
`(session_id, user_id, turn_has_document, text_only)`. It cannot see the turn's
resolved skills, and the turn's skills are where a field set lives (§4). Skills are
mounted one layer down, in
[`ChatAgent._create_agent`](../../backend/src/agents/main_agent/chat_agent.py:86),
where `build_skills_runtime` returns the plugin *and* `read_skill_file` — described in
place as "infrastructure, not an RBAC-gated tool, implicitly scoped to the turn's
skills". That is exactly what `document_extract` is, and `self.session_id` /
`self.user_id` are already in hand there (the tool-result offloader reads them six
lines later). So this feature does not touch `_build_document_tools` at all, and does
not contend with `verify_sources` for those lines.

**2.2 `openpyxl==3.1.5` is a *base* dependency, in every image.** The existing
`create_excel_spreadsheet` drives openpyxl *inside* Bedrock Code Interpreter from code
the model writes — which, for extraction, would mean the row data travelling through
the model's tool input, paid at model rates, to produce a file the backend already had
the data for. It is the right tool for "build me a workbook" and the wrong one here.
Writing the `.xlsx` in-process needs no new dependency, no sandbox and no model-written
code (§8).

**2.3 `create_artifact_record` is a plain server-side function.** The artifact path is
reachable without the model authoring the content:
[`create_artifact_record(user_id, session_id, title, content, content_type)`](../../backend/src/agents/builtin_tools/artifacts/service.py:453)
is called by the `create_artifact` tool but is not part of it. A backend-authored
markdown table is therefore one call, with the rows never entering the conversation.

**2.4 The Outputs library is artifact-only, and nothing on the agent side publishes to
it.** `project_outputs` lives entirely in `app_api`, which the Runtime may not import.
An artifact reaches Outputs when a user shares it (`POST /artifacts/{id}/shares` with
`accessLevel: "project"`) or when a **project schedule** publishes its run's artifacts
via
[`ScheduleDelivery._publish_outputs`](../../backend/src/apis/shared/projects/schedule_delivery.py:163),
which runs in the scheduled-runs worker through `apis.shared.shares.writer`. A
`.xlsx` is a `FileMetadata` row, not an artifact, so it **cannot** reach Outputs under
the current model. §8 settles what we do about that, which is: nothing, deliberately.

**2.5 One thing the framing got right and is worth restating.** `grep` over
`backend/src` for `response_format` / `json_schema` / `outputConfig` returns no
production use. The single `toolChoice` in the tree is
[`planner.py:328`](../../backend/src/apis/shared/memory/maintenance/planner.py:328) —
a forced one-tool Converse call whose result is then validated in Python, and whose own
code comments record that `tool_use["input"]` may arrive as a **string** needing
`json.loads`. That is the house precedent for a schema-shaped side-channel call, and
also local evidence that an unenforced `inputSchema` guarantees nothing about shape.

## 3. The field set

A field set is a named, ordered list of fields. Our model, written from the reasoning
in theirs:

| Key | Meaning |
|---|---|
| `name` | `snake_case`. The column header, the JSON Schema property, and the claim id handed to G2. Stable across runs — it is what makes two runs comparable |
| `title` | Display label. Separate from `name` because a column header changes without invalidating last month's rows |
| `type` | `string` \| `number` \| `currency` \| `date` \| `boolean` \| `enum` \| `list` |
| `find` | The phrase that finds it. **This is the load-bearing field**, and the one an outsider would leave out |
| `enum` | Allowed values; required for `type: enum`, meaningless otherwise |
| `optional` | The document may legitimately not contain it |

Two notes on where that shape comes from.

**`find` is their `searchphrase`, and it is the whole reason a field set beats a
prompt.** A field named `cost_share` tells a model almost nothing. "Whether the funder
requires institutional cost sharing, and at what percentage of total project cost"
tells it what to look for. Their model makes the phrase a first-class per-field
attribute rather than folding it into a description, and that is correct: the phrase is
the field's prompt, it is what gets tuned when a field under-performs, and it is what
an office actually edits. We keep it under its own key for the same reason.

**Order is declared, never derived.** Their `SearchSet` carries an explicit
`item_order` list rather than relying on query order. They added it for drag-and-drop;
we need it for the prompt-cache contract and for schema-grammar reuse (§6), where a
field list that comes back in a different order is a different schema. Our ordering is
the authored order of the rows in the file, read top to bottom. **Never a dict
iteration, never a sort applied on read.**

Not carried over, and why: their `domain` (`nsf | nih | dod | doe`) selects
domain-specific prompts from code, which is the thing the gap analysis identifies as
their structural ceiling — a new funder is a deploy. Our equivalent is a Skill and a
Project Memory Space, which the office edits. `extraction_config`,
`extraction_config_override` and `tuning_result` belong to their optimizer (G4) and
have no home here yet. `cross_field_rules` is G9 (§15).

## 4. Where the field set lives: a skill reference file

**Decision: reference material inside a Skill, resolved server-side by name. Not a new
entity, not a new table, not a new RBAC noun.** (D2)

A field set is one markdown file in a skill's reference bundle, at
`references/field-sets/<set-name>.md`: YAML frontmatter for the set's own metadata
(`title`, `version`), then one markdown table whose rows are the fields in order, then
an optional `## Rules` section that G9 will read and this slice ignores.

Why this and not a first-class entity:

* **Access control is structural, and already correct.** `read_skill_file` enforces
  access by *construction*: `by_slug` is built from exactly the turn's effective skill
  records, "so there is no id the model can name to reach one", and the comment there
  argues this is strictly stronger than re-checking a caller-supplied id. A field set
  resolved the same way inherits the skill's visibility, the skill's RBAC grant, the
  skill's Agent binding and the skill's project harness binding — four access
  questions already answered, none re-asked.
* **It costs nothing in the prefix until the skill is mounted.** The field *names*
  reach the model through the skill's own `SKILL.md`, which is already accounted for
  in the disclosure budget. The field *definitions* never reach the model at all (§5).
* **No new GSI.** `rag-assistants` carries seven already, and this repo's one-GSI-per-
  `UpdateTable` deploy trap makes an eighth a sequencing problem for a feature that
  does not need one.
* **Versioning and sharing come free.** Skills already have `status`, `visibility`,
  pinned versions (`skill_id@3`), per-role grants and an S3 `SKILL.md` projection. A
  field set nobody can version or share would be a worse product; a field set that is a
  skill resource is versioned and shareable on day one without a line of new code.

**What is given up, stated plainly:**

1. **Authoring is an admin surface today.** Resources are managed through
   `/admin/skills/{id}/resources`. A grants office cannot author its own field set
   unless someone grants them the skill-authoring tools (`SKILL_AUTHORING_ENABLED`,
   itself in development and inert until a role holds `my_skill_create`). So slice one
   ships with field sets authored *for* an office, not *by* it. That is a real
   limitation and the honest sequencing: prove the extraction is trustworthy before
   handing out the schema editor.
2. **There is no Extractions tab.** The experience map's Project tab implies a surface
   that lists extraction runs and lets a reviewer correct them. This design gives the
   reviewer an artifact per run in Outputs, which is a weaker thing: readable and
   shareable, but not editable, not queryable across runs, and not correctable in place.
   Building that surface means persisting runs as a first-class entity, and it should be
   built **when there is a measured reason to**, against real rows from this slice,
   rather than designed now against a guess at what reviewers will want to change. The
   migration path is open: §9's `EXTR#` rows already carry the full typed result, so a
   later entity reads them rather than re-extracting.
3. **A markdown table is parsed, and parsing can fail.** A malformed table is a
   conversational error from the tool, not a 500 — and the parser is pure and
   unit-testable, which is the trade that makes it acceptable.

## 5. The tool, and why the field list is not in its schema

```
document_extract(upload_id, field_set, pages?) -> bounded result
```

**The agent-facing spec is a constant per deployment.** `field_set` is a string the
model names; `upload_id` selects the document; `pages` optionally narrows a long
package. No field appears in `inputSchema`. The prefix shape is therefore one per
deployment, not one per session and not one per field set — the same property
`verify_sources` has, and for the same reason.

The gap analysis proposed the opposite: generate `inputSchema` from the field list, and
accept "a session that switches schemas pays one prefix rewrite". That costing is too
kind. The spec would be in `toolConfig`, which sits ahead of the system prompt and the
whole restored history in the cacheable prefix, so a change to it does not cost "one
rewrite" of the tool block — it re-writes **everything behind it**, at the cache-write
premium of 1.25× the model's own base input rate, on a 30k–150k-token prefix. And it
would not be once: editing one `find` phrase mid-session, adding a column, or running a
second field set each does it again. On our default Haiku 4.5 at $1.375/MTok written,
a 100k prefix is ~$0.14 per bust, and the first conversation where an office tunes a
field set over six iterations pays it six times for nothing.

**But the agent-facing spec is not where the schema belongs anyway**, and that is the
part the "constant spec vs. generated spec" framing misses. The typed field schema does
not have to be in *a tool* at all. Bedrock Converse takes a JSON Schema directly, as
`outputConfig.textFormat`, on a request of its own (§6). So:

* The agent calls `document_extract` — constant spec, zero prefix churn.
* The tool resolves the field set server-side from the skill resource, generates the
  JSON Schema, and makes **its own Converse call** carrying the document text, the
  field schema and nothing else.
* That call is a side channel in the exact sense `session_title` and
  `tool_group_summary` already are: its own request, its own messages, concurrent with
  nothing in the conversation, appending nothing to it. It has no cacheable prefix to
  bust because it has no conversation.

So we keep per-field types and enums in front of the model *and* pay no prefix churn.
Three further things fall out, which is usually the sign the seam is in the right place:

* **The extraction model is chosen independently of the turn's model.** A user chatting
  on a Mantle-hosted model still gets Bedrock-native structured output (§6).
* **The extraction context is a fresh one.** The input is the document and the schema,
  not the conversation — so extraction on turn 40 of a long thread costs the same as on
  turn 1.
* **It can be forced.** The side-channel call constrains the output absolutely;
  an `outputConfig` on the agent's own turn would hijack the turn's prose.

### 5.1 What goes back to the model

```json
{"fieldSet": "nsf-solicitation", "artifactId": "…", "fields": [
  {"name": "deadline",   "value": "2026-03-03", "state": "supported"},
  {"name": "budget_cap", "value": "$500,000",   "state": "quote_unsupported"},
  {"name": "cost_share", "value": null,         "state": "unverified"}
]}
```

Plus a one-line summary telling the model the table and the download card are already
on screen, so it does not helpfully re-type either — the `_download_card` precedent,
whose summary string exists for exactly that failure.

**The values go into the content blocks, and this is consistent with the cost tenet
rather than an exception to it.** G2's rule is that *display* metadata must not enter
the Converse payload, because it is paid at model rates on every later turn forever.
Extraction values are not display metadata; they are the answer the user asked for, and
the follow-up question ("so what was the budget cap?") is the normal case. A model that
cannot see the values must re-run the extraction to answer it — a document-sized call
to recover data we already had. Short scalars are the cheap side of that trade:
twenty fields at ~40 characters is ~150 tokens once.

Bounded, so "short scalars" stays true: `MAX_VALUE_CHARS = 200` per value,
`MAX_RESULT_CHARS = 2000` for the whole result, `MAX_FIELDS = 40` per set. A value over
the per-value cap is truncated with an ellipsis and the full text stays in the artifact
and the row; a result over the total cap drops to names and states with a line saying
to read the table. A `list` value is joined to a bounded string here — the structured
form lives in the artifact.

**What does not go back:** the quote, the locator, `precision`, `method`, the rendered
table, the XLSX bytes. Those travel on the SSE event and the persisted row (§9).

### 5.2 Where it is built, and what it is gated on

In `ChatAgent._create_agent`, beside `read_skill_file`, from the same skill records
(§2.1). It is mounted when the flag is on **and** at least one of the turn's active
skills declares a field set.

That second condition is the gate, and the governing capability is **a field set, not a
document** — which is the one substantive difference from `verify_sources`. A session
with a document and no field set has nothing to extract; a session with a field set and
no readable document gets a conversational error on the call, which is cheaper than
injecting a tool that cannot work. The check costs nothing: a skill's resource
*manifest* is on the DynamoDB record already loaded by `fetch_active_skill_records`, so
"does any active skill carry `references/field-sets/*.md`" is a comprehension over
in-memory data with no S3 GET (§10).

**No new agent-cache key element.** The turn's skills are already in the key, the flag
is a deployment constant, and nothing else varies.

## 6. Structured output on Bedrock: what is actually enforced

Verified against the pinned SDK and the AWS docs on 2026-10-20, because "the fleet
behaves uniformly" is exactly the assumption that breaks in production.

**6.1 Two distinct mechanisms, and we want the second one.**

| Mechanism | Where the schema goes | What it constrains |
|---|---|---|
| Strict tool use | `toolSpec.strict: true` beside `inputSchema` | The model's tool-call *arguments* |
| JSON Schema output format | `outputConfig.textFormat.jsonSchema` | The model's *text response* |

Both are present in the **already-pinned** `boto3==1.43.103` / `botocore==1.43.103`:
`ToolSpecification` has members `['name', 'description', 'inputSchema', 'strict']`, and
`ConverseRequest` carries `outputConfig` → `OutputConfig{textFormat, effort}` →
`OutputFormatStructure` (a union) → `JsonSchemaDefinition{schema, name, description}`,
where `schema` is a **required string** holding the JSON-encoded schema. **No
dependency bump is needed**, which also means no approval to ask for.

We use `outputConfig.textFormat`, not `strict`, because the extraction call wants a
typed *object* back, not a tool invocation, and because it keeps the schema out of every
`toolConfig` in the system by construction rather than by discipline.

**6.2 Without one of those flags, nothing is enforced.** `strict` is opt-in on the tool
definition, and the AWS docs describe it as what "enable[s] schema validation on tool
names and inputs". Bedrock validates the *schema's own shape* at request time (a
missing top-level `type` is a 400), not the model's conformance to it. Our own
`planner.py` corroborates it from the other direction: it defends against a tool input
arriving as a JSON string, and `_parse_changes` re-validates everything. So every
unenforced tool argument in this codebase — `document_extract`'s own included — must be
parsed defensively. It always had to be; this just records why.

**6.3 The JSON Schema subset is narrower than JSON Schema, and the gap is where G9
lives.** Supported: all basic types including `null`, `enum` (strings/numbers/booleans/
nulls), `const`, `anyOf`/`allOf` with limits, internal `$ref`/`$defs`, string `format`
(`date`, `date-time`, `email`, `uri`, `uuid`, …), and array `minItems` — but only the
values 0 and 1. **Not** supported: recursive schemas, external `$ref`, numeric
constraints (`minimum`/`maximum`/`multipleOf`), string constraints
(`minLength`/`maxLength`), and `additionalProperties` set to anything but `false`.

Consequences for our generator, each a real constraint rather than a note:

* `additionalProperties: false` always, on every object.
* `type: enum` → a `string` with `enum`. Native, and the reason types are worth having.
* `type: date` → `string` with `"format": "date"`. Enforced as a format, and still
  re-parsed in Python, because a well-formed date can still be the wrong one.
* `type: currency` → `string`, not `number`. A currency value is "$500,000" in the
  document and the user wants to see what the document said; coercing to a float
  discards the symbol and invents precision. The numeric comparison that matters
  happens in G2's value check, which already handles currency symbols and thousands
  separators.
* `optional: true` → the property stays in `required` and its type becomes a union with
  `null`. Omitting it from `required` would let the model silently drop a field, and a
  missing key and an absent value are different facts: `null` is the model saying "not
  in this document", which is answerable, and a missing key is the model saying
  nothing, which is not.
* **A `minimum`/`maximum` on a numeric field is not expressible.** Range checks, and
  every cross-field rule, must run in Python after the rows come back. That is where
  G9 attaches (§15) and it is not a workaround — a constraint the grammar enforces is
  one the model cannot report honestly, so "the award amount is implausible" is better
  as a flag on a row than as a value the model was forbidden to emit.

**6.4 Grammar compilation is a latency cliff, and the schema string must be byte-
stable.** Bedrock compiles a new schema's grammar on first use — the docs say "up to a
few minutes" — then caches it for **24 hours from first access**, per account, for
identical schemas. Two consequences:

* **The generated schema must serialize identically every run.** Field order from the
  authored order, fixed key order in the emitted JSON, a fixed separator convention. A
  schema that differs by a byte is a cache miss, which here means minutes, not cents.
  This is the same determinism discipline as the prompt-cache contract, enforced for a
  different reason, and a unit test pins the exact serialized bytes for a fixture set.
* **First use of a new or edited field set can be slow, and the tool must survive it.**
  The extraction call gets a bounded timeout. On timeout, or on a 400 naming an
  unsupported schema feature, it retries **once without `outputConfig`**, keeping the
  same prompt and validating in Python, and marks the run `schema_unenforced`. That
  marker reaches the SSE event and the row — never silently — so a reviewer knows the
  types were asked for rather than guaranteed. A field set used weekly recompiles each
  time; if that proves to be the common case rather than the edge, the answer is a
  nightly warm call per field set, which is one scheduled call and is **out of this
  slice**.

**6.5 Which models actually support it, and the one that bit.** Verified on the Claude
Haiku 4.5 model card: structured outputs is **supported** on the `bedrock-runtime`
endpoint (and on `bedrock-mantle`), and the single exclusion is the India geographic
profile — "Structured outputs are not supported when you invoke Claude Haiku 4.5
through the India Geographic cross-Region inference profile
(`in.anthropic.claude-haiku-4-5-20251001-v1:0`)". Our dev ids are `us.*` and prod's are
`global.*`; both carry it. The general shape of the trap is real and worth carrying
forward: **support is per model *and* per inference profile**, and a deployment that
localises its model ids can lose the feature without changing a line of code.

**6.6 The fleet problem dissolves, rather than being solved.** `mantle.py` builds
Strands `OpenAIModel` / `OpenAIResponsesModel` against the Bedrock Mantle
OpenAI-compatible surface, with canonical→native parameter maps per API mode and the
documented behaviour that "a canonical param without an entry here is silently
dropped". A hosted OpenAI model is therefore not speaking Converse at all, and
`outputConfig` is not a thing it would receive — it would be dropped without a word.

The side-channel design means we never have to care: the extraction call names its own
model, from `DOCUMENT_EXTRACT_MODEL_ID` (CDK-set per environment, `us.*` on dev,
`global.*` on prod, matching how the stack already distinguishes them). The turn's
model is irrelevant to it. **So the answer to "what happens on a model that does not
enforce" is that such a model is never asked**, and the only unenforced path left is
6.4's deliberate, marked fallback.

The cost of that: the extraction may run on a different (usually cheaper) model than
the one the user picked for the conversation, which is a thing to disclose on the result
rather than hide. The SSE event carries the extraction model id.

## 7. How a row carries its provenance

**Every extracted field is a claim in G2's sense, and G2 answers it. We add nothing.**
(D4)

For each field the extraction call returns a `value` and a `quote` — the verbatim
passage it took the value from, asked for in the same schema as a required sibling
property of the value. Then, once per run:

```python
units  = document_units(fmt, texts)                 # built once; normalizes once
claims = [Claim(claim_id=f.name, value=row[f.name], quote=row_quote[f.name],
                enum_field=(f.type == "enum")) for f in field_set.fields]
results = verify_claims(claims, units)
```

That is the whole integration. `verify_claims` exists as its own entry point precisely
because `DocumentUnits` normalizes on construction, so twenty fields against one
document pay normalization once. We import only `Claim`, `DocumentUnits` /
`document_units`, `verify_claims`, `render_locator`, the state constants and the
`SourceVerification` type — which is the entire allowance
`tests/architecture/test_source_provenance_state_boundary.py` permits, and this feature
must not widen it.

Respecting its decisions, each of which constrains us:

* **D1 — no inventing a verdict.** There is no public function taking the two booleans,
  and we do not want one. We never write a state literal, never construct a
  `SourceVerification`, and never branch toward a stronger state than we were handed.
  The boundary test covers this automatically, since `"provenance"` will appear in the
  path of the module that reads these results.
* **D9 — nothing inherits the strongest badge.** A field the model returned without a
  quote is `unverified`/`no_quote`. A field whose value is `null` or a not-found
  sentinel is `unassessed`/`empty_value`. A `boolean` or `enum` field answering a
  judgment about the prose is `unassessed`/`enum_field`. None of those is a bug to be
  smoothed over, and a reviewer reading twenty `supported` rows should be suspicious.
* **`kind` and `precision` travel with the locator**, and the renderer is G2's
  `render_locator` — `p. 12` / `¶ 34` / `line 88`, hedged to `p. ~12` when estimated.
  A `.docx` field set renders paragraphs, because a `.docx` has no pages.

**One genuine improvement over `verify_sources`, and it is free.** That tool takes
`from_fixed_set` from the *model*, which is the model self-certifying that the value
question does not apply to its answer. We do not have to trust that: the field set
declares `type: enum`, so `enum_field` is derived from the schema the office authored,
not from the model's say-so. A model cannot escape the value check here by claiming its
answer was a judgment call. Worth carrying back into `verify_sources` if a caller there
ever has a schema to hand.

**The weakness G2 records in its §6.4 applies in full and must not be papered over.**
The value check asks whether the value appears *somewhere* in the passage, not whether
the passage *assigns* it to this field — so a `direct_costs` value is `supported` by a
quote labelling that same figure indirect. In a prose answer that is a caveat. In a
twenty-row table of `supported` badges it is a stronger invitation to over-read, because
the table's structure implies the assignment was checked. So the artifact's header
states the claim exactly once, in G2's own words: *this value is in this passage, on
this page*. The renderer says nothing more, and a column header must never read
"verified".

## 8. Where the results go

**Three destinations, in order of who reads them.** (D5)

**8.1 The live surface: an SSE event.** The rows, states, locators and quotes reach the
SPA on `document_extraction` (§9) while the turn continues, rendered as a table with a
per-cell state affordance following G2's PR-3 vocabulary: a plain locator for
`supported`, a warning for `quote_unsupported`, a "not confirmed" qualifier for
`unassessed`, "no source found" for `unverified`.

**8.2 The reviewable record: a backend-authored markdown artifact.**
`create_artifact_record(..., content_type="text/markdown")` with a table the *tool*
renders — field, value, locator, state — plus a header naming the document, the field
set and its version, the extraction model, and `schema_unenforced` if it applied. The
row data never passes through the model, which is the whole point of using the service
function rather than the `create_artifact` tool (§2.3). It is versioned, shareable and
openable like any artifact, and `GSI1PK=SESSION#…` puts it in the session's artifact
list for free.

**8.3 The file of record: an `.xlsx` written in-process.** openpyxl is already in the
image (§2.2): one sheet of rows with a provenance column per field, stored through the
existing user-files path and surfaced with `_download_card`, which costs ~60 tokens of
content block and no row data. **Not** Code Interpreter, and **not** model-written code.

**8.4 Outputs: reachable, but by the user or a schedule — not by this tool.** Confirmed
in §2.4: `project_outputs` is app-api-only and the publishing writer
(`apis.shared.shares.writer`, driven by `ScheduleDelivery._publish_outputs`) is bound
to a schedule. So:

* **An interactive extraction's artifact reaches Outputs when the user shares it**,
  from the artifact's own Share dialog, exactly as every other artifact does. We are not
  building a second publisher, because an agent tool that publishes to a shared project
  without the user asking is a sharing decision made by a model.
* **A recurring extraction's artifact reaches Outputs automatically**, because a project
  schedule that publishes outputs already does this and already retires the previous
  edition. The "weekly sweep of new solicitations" use case is therefore covered by
  composition, with no new code.
* **The `.xlsx` cannot reach Outputs at all**, since `ProjectOutput` is keyed on
  `artifact_id`. We accept that: the workbook lands in the chat's Files panel with a
  download card, and the artifact is the thing a project keeps. Extending Outputs to
  files is a change to the Outputs model and is **out of scope here** (§15) — but the
  gap analysis' claim that both land in Outputs "already built" is wrong, and that is
  worth recording rather than quietly designing around.

## 9. The SSE event and the stored row

Following `verify_sources` and `tool_group_summary`, with no new infrastructure.

```
document_extraction
{type, sessionId, toolUseId, fieldSet, fieldSetVersion, uploadId, filename,
 modelId, schemaEnforced, artifactId, downloadUploadId,
 fields: [{name, title, type, value, state, method,
           quote, locator: {kind, value, precision} | null}]}
```

* **Hand-off** through an in-process broker keyed on `(user_id, session_id)` — never on
  the session alone, for the reason `provenance_broker.py` states: one container serves
  many turns and a session id alone does not name a conversation. Drained in
  `stream_coordinator` on the same pass as tool-batch summaries, so the table appears
  while the turn continues. Whether that is a second broker module or a generalization
  of G2's is an implementation call for PR-2, to be made *after* G2 merges so the
  decision is taken against real code.
* **Persisted** as `EXTR#<tool_use_id>` rows in `sessions-metadata` on the existing
  `SessionLookupIndex` GSI, with `GSI_SK: EXTR#<created_at>` and the same TTL as
  `SRCV#`. Zero new infra. Replayed by `messages.py` as `extractions`, because the
  event never re-streams.
* **Bounded against the 400 KB item limit**, which `provenance_store.py` already guards
  for (`_MAX_QUOTE_CHARS = 1200`, `_MAX_CLAIMS = 24`): at most `MAX_FIELDS = 40` fields,
  each quote clamped to 1,000 characters, each value to 1,000. A row that would still
  exceed the limit stores names, values and states only, with a pointer to the artifact
  — which holds the full table regardless, so nothing is lost. Persisted **before** the
  broker hand-off, for the reason `verify_sources` records: the emit loop may never
  reach the payload on a cancelled turn, and the row is what survives a reload.
* **Metered, unlike its side-channel neighbours.** `session_title` and
  `tool_group_summary` are left unmetered because they are bounded, tiny, and measured
  at ~0.3% of spend. The extraction call is not in that class: **its input scales with
  the document**, so one run over a 300-page award package is a real cost. It records a
  `C#` row through `store_message_metadata` with the extraction model id, so it prices
  through the catalog, appears in `GET /admin/costs/sessions/{id}/calls`, and counts
  against quota like any other call. **The trap, named because it is easy to hit:** if
  `DOCUMENT_EXTRACT_MODEL_ID` names a model with no catalog row, the call prices to
  `None`, costs $0, counts against nothing, and fires `UnmeteredModelCall`. A CDK test
  pins the configured id, and the dev readout checks a real run produced a priced row.

## 10. Cost and TTFT

**Nothing lands before the first token.** Extraction runs when the model calls the
tool, after it has read a document and decided to extract. There is **no
`BeforeInvocationEvent` and no `BeforeModelCallEvent` hook** — the latter fires before
every model call, so its cost would repeat on each tool round.

The one thing this feature adds to the turn path is the mount check in
`ChatAgent._create_agent` (§5.2): a comprehension over resource manifests already
loaded from DynamoDB by `fetch_active_skill_records`. No S3 GET, no extra query, no
network. The preamble was cut from 455 ms to 22–37 ms and each addition is several
percent of that budget, so this is stated rather than assumed, and the dev readout
compares `turn_prelude` marks with the flag on and off (§14).

**Against the prompt-cache contract:**

* `toolConfig` gains one constant spec per deployment when the gate fires. One prefix
  shape, not one per session or per field set (§5).
* Nothing in this feature mutates restored history, and nothing it writes enters the
  message content blocks beyond §5.1's bounded result and the download card.
* The extraction call has no conversation and therefore no prefix to bust. Where the
  document text exceeds Haiku 4.5's 4,096-token minimum cache checkpoint, a
  `cachePoint` on the document segment makes a second field set over the same document
  a cache read — **recorded as an opportunity, not built in slice one**, because its
  value depends on whether two-field-sets-one-document is actually common, which this
  slice is what measures.

**The honest cost line:** one bounded model call per extraction, whose input is the
document (or the pages named) plus the schema, and whose output is one row per field.
`pages` is the lever, the prepared `text.jsonl` text layer is the cheap source, and both
are measured (§14).

## 11. Decisions, with reasons

**D1 — The agent-facing tool spec is constant; the typed schema goes on a side-channel
Converse call via `outputConfig.textFormat`.** Generating `inputSchema` from the field
list puts a per-session, per-edit value ahead of the entire cacheable prefix, and
re-writes 30k–150k tokens at 1.25× base input rate on every change — not the "one
rewrite" the gap analysis costed. A constant spec with the detail as a runtime argument
is the `verify_sources` answer and would have been enough; `outputConfig` is strictly
better, because it keeps the enums and types the runtime-argument shape gives up, on a
request with no prefix to protect. Rejected: `strict: true` on the agent's tool — it
constrains arguments, not an answer, and it would put the schema back in a `toolConfig`.

**D2 — The field set is a skill reference file, resolved server-side by name; not a
first-class entity.** It inherits four already-correct access decisions by
construction, adds no table, no GSI, no RBAC noun and no admin page, and is versioned
and shareable on day one. Given up: in-slice authoring by the office (admin surface
only until skill authoring is on), and the Extractions tab the experience map implies
(§4). The `EXTR#` rows keep that path open — a later entity reads them rather than
re-extracting.

**D3 — Field definitions are resolved by the backend and never pass through the
model.** A model that reads the field table through `read_skill_file` and re-types the
names into a tool call pays for the table in the content blocks and can mangle a name,
which silently breaks comparability with last month's rows. `SKILL.md` names the
available sets; the model names one; the backend reads it. The skill's prose must say
*not* to read the field file.

**D4 — Field order is the authored order of the markdown rows.** Their `item_order`
exists for drag-and-drop; we need it for grammar reuse (§6.4) and for every
determinism rule in `CLAUDE.md`. Never a dict iteration, never a sort on read.

**D5 — `optional` becomes a nullable union, not an absent `required` entry.** A missing
key and an explicit `null` are different facts, and only one of them is the model
answering the question.

**D6 — `currency` is a string.** The document says "$500,000"; a float discards the
symbol and invents precision. Numeric comparison is G2's job and it already handles
currency symbols, thousands separators and the percent sign.

**D7 — `enum_field` is derived from the field's declared type, not from the model.**
`verify_sources` trusts the model's `from_fixed_set`, which lets a model exempt its own
answer from the value check. A schema-declared type cannot be talked out of.

**D8 — Short values go back to the model; provenance does not.** The values are the
answer, and the follow-up question is the normal case — a model that cannot see them
must re-run a document-sized call to answer "what was the budget cap?". Display
metadata (quote, locator, `precision`, `method`, the table) would be paid at model rates
every later turn, and stays on the side channel. Bounded at 200 chars per value and
2,000 per result so "short" remains true. This is an application of G2's D7, not an
exception to it: G2's payload was display metadata and ours is the deliverable.

**D9 — Mounted on a field set, not on a document.** The one deliberate divergence from
`verify_sources`' gate. A session with a document and no field set has nothing to
extract, and a tool that cannot work costs prefix for nothing. The document is checked
at call time, where a conversational error is the right failure.

**D10 — Built in `ChatAgent._create_agent`, not in `_build_document_tools`.** It needs
the turn's skill records, which that gate cannot see (§2.1). `read_skill_file` is the
precedent in the same lines: infrastructure, implicitly scoped to the turn's skills, not
RBAC-gated. Skills are already a cache-key element, so no new key element.

**D11 — The unenforced fallback is marked, never silent.** On a grammar-compilation
timeout or a 400 naming an unsupported schema feature, retry once without
`outputConfig` and validate in Python, with `schemaEnforced: false` on the event, the
row and the artifact header. A reviewer must be able to tell the difference between
types that were guaranteed and types that were requested.

**D12 — The extraction call is metered.** Unlike `session_title` and
`tool_group_summary` its input scales with the document, so the "negligible side
channel" finding does not transfer. A `C#` row through `store_message_metadata` prices
it, bills it against quota and makes it visible in the cost drill-down — and the
`UnmeteredModelCall` trap is pinned by a CDK test (§9).

**D13 — Backend-authored artifact and in-process XLSX; nothing through Code
Interpreter.** `create_artifact_record` and `openpyxl` are both already available
(§2.2, §2.3). Driving the existing `create_excel_spreadsheet` would route the rows
through the model's tool input to build a file from data the backend is holding.

**D14 — No new publisher to Outputs.** A user shares the artifact, or a project
schedule publishes it (§8.4). An agent tool that pushes into a shared project is a
sharing decision made by a model.

## 12. The flag

| | |
|---|---|
| Backend | `DOCUMENT_EXTRACT_ENABLED`, read by `document_extract_enabled()` in `apis/shared/feature_flags.py`. **In-development: only the literal `"true"` enables**; an unset GitHub variable arrives as `""` |
| Also | `DOCUMENT_EXTRACT_MODEL_ID` — the Bedrock-native Converse model id the extraction call uses. CDK-set per environment (`us.*` dev, `global.*` prod), and it must have a catalog row (§9) |
| Set on | The AgentCore Runtime (mounts the tool, extracts, verifies, writes the artifact and the workbook, emits the event) and app-api (replays `EXTR#` rows on `GET /messages`) |
| CDK | `documentExtract.enabled` and `documentExtract.modelId` in `infrastructure/lib/config.ts` from `CDK_DOCUMENT_EXTRACT_ENABLED` / `CDK_DOCUMENT_EXTRACT_MODEL_ID`, forwarded in `.github/workflows/platform.yml`. Written to the Runtime's environment **only while on**, so the default costs nothing against the 2,560-byte V2 budget. A `config.test.ts` case pins both sides, and a second pins the model id |
| SPA | `features.documentExtract`, `false` in all three `environment*.ts`, injected via the `FEATURES` token — never an environment file imported into a component. Added in slice one so the two sides land independently; the table render is slice three |
| Kind | **Feature switch** — permanent. It spends money per run on a model call whose input scales with a document, it adds a tool to `toolConfig`, and it makes the product assert typed values about a document on a user's behalf. Every one of those is a judgement a deployment may reasonably decline, so it is configuration, not a rollout hedge, and it does not retire. Promoting it to default-on with an `=false` kill switch is its own deliberate change |

## 13. PR sequence

**Everything after PR-0 is blocked until G2 merges to `develop`.** This is not caution
about the design; it is two writers in the same lines. G2's three commits are local and
unpushed on `claude/field-source-verification-bb4500` with no PR open, and the files
both features must edit are:

| File | Why it collides |
|---|---|
| `backend/src/apis/shared/feature_flags.py` | Each adds a reader to the same region of the file |
| `infrastructure/lib/config.ts` | Each adds a config block |
| `infrastructure/test/config.test.ts`, `helpers/mock-config.ts` | Each adds cases and a mock entry |
| `.github/workflows/platform.yml` | Each forwards a `CDK_*` variable |
| `app-api-environment.ts`, `inference-agentcore-construct.ts` | Each threads an env var |
| the three `environment*.ts` + `feature-flags.ts` + `features.spec.ts` | Each adds a `FeatureFlags` field |
| `CLAUDE.MD` | **Worst.** The same paragraph: each adds an in-development flag entry to "Current in-development flags" |
| `backend/src/apis/shared/sessions/messages.py`, `models.py` | Each adds a replay field |
| `backend/src/agents/main_agent/streaming/stream_coordinator.py` | Each adds a drain |

Two further reasons the order is strict rather than tidy: this feature **imports**
`source_provenance.py` and `document_provenance_units`, neither of which exists on
`develop`; and it must obey
`tests/architecture/test_source_provenance_state_boundary.py`, which arrives with G2.
Building against an unpushed branch would mean rebasing a feature onto a moving
dependency and discovering the boundary test at the end.

One correction to the brief: `_build_document_tools` in `inference_api/chat/routes.py`
is **not** on the collision list, because this feature does not touch it (§2.1, D10).

| PR | Contents | Blocked on G2 |
|---|---|---|
| PR-0 | This spec. One new file | **No** — open now |
| PR-1 | The pure layer: the field-set markdown parser, the field model, the JSON Schema generator with byte-stable serialization, and unit tests (the subset rules of §6.3, nullable optionals, enum fields, `currency` as string, the authored-order guarantee, a pinned serialized-bytes fixture, malformed-table errors). Plus the flag, the CDK wiring, the CDK tests and the SPA flag field. **Nothing wired to a turn** | Yes — flag and CDK files |
| PR-2 | `document_extract` mounted in `ChatAgent._create_agent`; field-set resolution through the skill resource store; the side-channel Converse call with `outputConfig.textFormat` and the marked unenforced fallback; the `verify_claims` integration; the backend-authored artifact and the in-process `.xlsx`; the `document_extraction` SSE event and its broker; the `EXTR#` store and its `GET /messages` replay; the `C#` metering row; the content-free EMF record. Backend complete and dev-verifiable by reading the stream | Yes — imports G2 |
| PR-3 | SPA render behind `features.documentExtract`: the rows table with per-cell provenance in G2's vocabulary, the `schemaEnforced` disclosure, and reload replay | Yes — follows G2's PR-3 vocabulary |

PR-1 and PR-2 are slice one. PR-3 is stated as outside it rather than assumed.

## 14. How we'll know

* **The pure layer is unit-tested**, because parsing a table and generating a schema are
  pure. The §6.3 subset rules, the authored-order guarantee and the byte-stable
  serialization each have cases, and the serialization has a pinned-bytes fixture
  because a one-byte drift costs minutes of grammar compilation, not cents.
* **On dev**, attach a real solicitation PDF, run a 10-field set, and read the
  `document_extraction` frame: typed values, and a locator with `precision: "measured"`
  on the `supported` ones. Then run a field set containing a field the document does not
  answer and confirm it comes back `null`/`unverified` rather than invented — the case
  that matters, and the one a happy path cannot show.
* **The dishonest case, deliberately provoked.** Craft a field whose `find` phrase
  points at a section the document discusses but does not quantify, and confirm the row
  reads `quote_unsupported` rather than `supported`. If every row of every run is
  `supported`, the integration is not working — it is the table that was always
  produced, with badges on it.
* **A reload replays it.** `GET /messages` returns `extractions`, and the artifact and
  the workbook are both openable.
* **Enforcement is confirmed, not assumed.** A run whose schema declares an enum must
  never return a value outside it while `schemaEnforced` is true. One run against a
  deliberately unsupported schema feature (a `minimum`) must produce the marked
  fallback rather than an error reaching the user.
* **Grammar compilation is measured once**, because nobody will remember to later:
  time the first run of a fresh field set, then an immediate second run, then one the
  next day. If "up to a few minutes" is the common case rather than the edge, the
  warm-call question in §6.4 becomes urgent instead of optional.
* **Cost.** A turn with `document_extract` shows no change in
  `systemPromptHash`/`toolConfigHash` between consecutive calls and no `partial_miss`
  attributable to it (`GET /admin/costs/sessions/{id}/calls`). The extraction call's own
  `C#` row exists and is **priced** — a $0 row means `DOCUMENT_EXTRACT_MODEL_ID` has no
  catalog row (§9), and `UnmeteredModelCall` will have fired.
* **TTFT.** `turn_prelude` marks with the flag on and off, on a session with a
  field-set-bearing skill mounted, must be indistinguishable. The mount check does no
  I/O (§10); this is the measurement that proves it.
* **Content-free EMF**, one record per run in `AgentCoreStack/Compaction` beside the
  `document_read` and `SourceVerify` records: `DocumentExtract`,
  `DocumentExtractFields`, with properties `format`, `fieldCount`, `schemaEnforced`, the
  per-state counts, and the extraction call's token counts. **No field name, value,
  quote, filename or field-set name is ever emitted** — a field-set name is an
  institution's business process, and the per-state split is the readout we need.
  Rides `PROMPT_CACHE_OBSERVABILITY_ENABLED` like its neighbours.
* **Distribution before badges.** The split across G2's `method` values over real runs
  is what says whether typed extraction is honest enough to put in front of a grants
  office. It is readable from PR-2, before PR-3 renders a single badge.

## 15. Non-goals

* **Cross-field validation rules (G9).** Their `cross_field_rules` sits on the
  `SearchSet`, beside the fields, and ours attaches the same way: a `## Rules` section
  in the field-set file, evaluated in Python **after** `verify_claims` and before the
  artifact is written, never in the JSON Schema — §6.3 shows the schema subset cannot
  express a numeric range, and a constraint the grammar enforces is one the model cannot
  report honestly. Nothing in this slice reads that section; the parser ignores it
  rather than rejecting it, so a field set authored with rules is forward-compatible.
* **OCR (G3).** A scanned PDF has no text layer. `document_provenance_units` yields no
  units, every field reads `unverified`/`document_unreadable`, and the run says so. The
  fix is G3 and it is a prerequisite for extraction from scanned documents, not a part
  of this.
* **Batch fan-out over many documents (G5).** One document per call. A batch wraps
  `document_extract` **at the harness level**, not inside it: the scheduled-runs worker
  already runs a headless harness and already publishes a run's artifacts to a project's
  Outputs, so a batch is one run per document with one artifact per run, plus the
  operational hardening the gap analysis says to take whole (heartbeat, reaper, atomic
  `finalized_at`, bounded delivery attempts, a distinct `delivery_failures` state).
  Nothing here should make that harder, which is why the tool takes exactly one
  `upload_id` and holds no run state.
* **Any re-implementation of provenance logic.** We call `verify_claims` and read
  `state`. We do not derive a state, construct a `SourceVerification`, reintroduce the
  boolean pair, or widen the import allowance the boundary test enforces.
* **The Extractions surface** — a persisted, queryable, correctable run entity (§4,
  given up #2). Built when there is a measured reason, against rows from this slice.
* **Extending the Outputs library to files**, so the `.xlsx` could land there (§8.4).
  A change to the Outputs model, not to extraction.
* **An eval and tuning loop over field sets** — their `extraction_config`,
  `tuning_result` and optimizer. That is G4, and it shares its runner with G5.
* **A warm call per field set** to keep grammars compiled (§6.4), until the measurement
  says it is needed.

## Sources

* `_local-notes/vandalizer-parity-and-gap-analysis.md` §G1 (this spec's seed), §G2 (the
  provenance model composed with), §G5, §G9 (both deliberately out of scope).
* Vandalizer `backend/app/models/search_set.py`, `app/services/extraction_engine.py`
  (GPLv3) — read for the reasoning in their field model: the per-field search phrase,
  the explicit `item_order`, the optional flag, the enum values, the display title, and
  the funder-domain selector we deliberately do not port. **Design ported, source not.**
* `docs/specs/source-provenance-verification.md` §4 (the states), §5 (the locator), §6
  (the module surface), §8 (how a claim reaches us), §9 (D1, D7, D9).
* AWS, verified 2026-10-20: [Get validated JSON results from
  models](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
  (the two mechanisms, the supported JSON Schema subset, grammar compilation and the
  24-hour cache); the [Claude Haiku 4.5 model
  card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html)
  (structured outputs supported on `bedrock-runtime`, excluded only on the `in.*`
  profile; explicit prompt caching min 4,096 tokens).
* Verified in this tree 2026-10-20: `boto3`/`botocore==1.43.103` service model
  (`ToolSpecification.strict`, `ConverseRequest.outputConfig` →
  `OutputConfig{textFormat, effort}` → `OutputFormatStructure` →
  `JsonSchemaDefinition{schema: String (required), name, description}`);
  `chat_agent.py:86` (`build_skills_runtime`, `read_skill_file`);
  `skills/strands_mapping.py` (`fetch_active_skill_records`,
  `make_read_skill_file_tool` and its structural-access argument);
  `skills/models.py` (`SkillResourceRef`, the S3-backed manifest);
  `skills/resource_store.py` (`resource_key`, `get`);
  `inference_api/chat/routes.py:1218` (`_build_document_tools` and what it does *not*
  have); `builtin_tools/artifacts/service.py:453` (`create_artifact_record`);
  `builtin_tools/excel_spreadsheet_tool.py` and `office/_storage.py`
  (`_download_card`, Code Interpreter); `backend/pyproject.toml`
  (`openpyxl==3.1.5` as a base dependency); `apis/app_api/projects/output_routes.py`
  and `artifacts/project_outputs.py` (Outputs is artifact-only, app-api-only);
  `apis/shared/projects/schedule_delivery.py:163` (`_publish_outputs`);
  `apis/shared/memory/maintenance/planner.py:328` (the only `toolChoice` in the tree);
  `apis/shared/tool_summaries/summarizer.py` (the side-channel call pattern);
  `apis/shared/sessions/metadata.py:310` (the `C#` row);
  `apis/shared/files/provenance_store.py` and `provenance_broker.py` (the `SRCV#` row
  and broker this mirrors, read on the G2 branch).
