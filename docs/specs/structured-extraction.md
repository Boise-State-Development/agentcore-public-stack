# Structured extraction: a named field set over a document, as rows that carry their own verdict

**Status:** PR-1 and PR-2 implemented (slice one), behind `DOCUMENT_EXTRACT_ENABLED`
(off by default; only the literal `"true"` enables). PR-3 (the SPA render) is not
built. Source-provenance verification (G2) merged to `develop` on 2026-10-20, which
unblocked this. **§16 records where this spec was wrong** — it was written before the
code, and five of its decisions needed correcting on contact. Decisions recorded in §11. Revised 2026-10-20 after
reading `strands-agents==1.57.2`: the constrained call is
`BedrockModel.structured_output` with `strict_tools=True` on a dedicated model
instance, not a hand-rolled `outputConfig.textFormat` request — same architecture, with
the schema generator and the result validator already written (§2.6, §6.1, D1, D15).
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

**Amendment (2026-10-20): N documents per call, as concurrency inside the tool.**
`document_extract` takes a **list** of `upload_ids` rather than one, and runs the
extraction for each concurrently under a semaphore (§17). The outcome worth building is a
**judgement across a set** — "of these fourteen, three are worth pursuing; two collide on
the same March deadline and both need the same PI; one demands cost share we can't meet" —
which a per-document pipeline structurally cannot produce. One conversation per document
would reproduce that limitation with worse ergonomics and 5-10x the cost (§17.1, where the
briefed "~200x" is corrected). The reduce step is **a skill, not code** (§17.3): the model can already reason over N row sets;
what it lacks is knowing what a grants office wants out of a set, and that belongs in
markdown where it costs nothing until the skill activates. Each extraction call stays a
pure function of `(document, field set)`, which the one-document design already
guaranteed and which is now load-bearing rather than speculative. §15's bare exclusion of
batch fan-out is replaced by a measured trigger condition for the unattended path, and
the "G5" label is retired (§15).

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

**2.6 Strands already ships this, twice, and only one of the two is usable.** The
pinned `strands-agents==1.57.2` has a `structured_output` subsystem, and the difference
between its two entry points is exactly the difference this spec's hardest decision
turns on.

| Path | What it does | Verdict |
|---|---|---|
| **Agent-level** — `Agent(structured_output_model=…)`, or `structured_output_model=` on a call | Builds a `StructuredOutputTool` from a Pydantic model and `register_dynamic_tool`s it into **that agent's** registry, so its spec enters **the turn's `toolConfig`**. Also appends a forcing prompt to the conversation ("You must format the previous response as structured output") | **Unusable.** This is the per-session prefix churn D1 exists to refuse, with a conversation mutation on top |
| **Model-level** — [`BedrockModel.structured_output(output_model, prompt, system_prompt)`](https://github.com/strands-agents/sdk-python) | `self.stream(messages=prompt, tool_specs=[one_spec], tool_choice={"any": {}})` — a **fresh request with its own messages and exactly one tool**, no conversation and no other tool spec, then `output_model(**tool_use["input"])` for Pydantic validation | **This is the design.** It is the side channel §5 describes, SDK-provided, with validation included |

Plus a config flag worth knowing about before someone finds it the hard way:
`BedrockModel(strict_tools=True)` adds `strict: true` to **every** tool spec in that
model's requests and runs `ensure_strict_json_schema` over each `inputSchema`. On a
one-tool request that is exactly the grammar enforcement we want. On the chat agent it
would be a loaded gun (D15).

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
not have to be in *the turn's* `toolConfig` at all — it belongs on a request of its
own, which `BedrockModel.structured_output` already makes (§2.6, §6). So:

* The agent calls `document_extract` — constant spec, zero prefix churn.
* The tool resolves the field set server-side from the skill resource, builds a Pydantic
  model from it, and makes **its own model call** carrying the document text, the field
  schema and nothing else.
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
* **It can be forced.** The side-channel call uses `toolChoice: {"any": {}}`, so the
  typed answer is the only answer it can give. Forcing the agent's own turn that way
  would take the turn's prose away from the user.

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

**6.1 Bedrock has two mechanisms; Strands exposes one of them; we take the Strands
one.**

| Mechanism | Where the schema goes | What it constrains | Reachable via Strands |
|---|---|---|---|
| Strict tool use | `toolSpec.strict: true` beside `inputSchema` | The model's tool-call *arguments* | Yes — `BedrockModel(strict_tools=True)` |
| JSON Schema output format | `outputConfig.textFormat.jsonSchema` | The model's *text response* | No |

Both are present in the **already-pinned** `boto3==1.43.103` / `botocore==1.43.103`:
`ToolSpecification` has members `['name', 'description', 'inputSchema', 'strict']`, and
`ConverseRequest` carries `outputConfig` → `OutputConfig{textFormat, effort}` →
`OutputFormatStructure` (a union) → `JsonSchemaDefinition{schema, name, description}`,
where `schema` is a **required string** holding the JSON-encoded schema. **No
dependency bump is needed for either**, which also means no approval to ask for.

**We use Strands' model-level path with `strict_tools=True`**, which is strict tool use
on a request carrying exactly one tool. Concretely, per run:

```python
model = BedrockModel(model_id=DOCUMENT_EXTRACT_MODEL_ID, strict_tools=True)  # dedicated
RowModel = pydantic_model_for(field_set)      # cached per (field set, version)
async for event in model.structured_output(RowModel, [{"role": "user", "content": …}],
                                           system_prompt=…):
    ...                                        # last event carries {"output": RowModel(...)}
```

Why this rather than hand-rolling `outputConfig.textFormat`, which an earlier draft of
this spec chose:

* **It deletes a component.** No hand-written JSON Schema generator and no hand-written
  canonical serializer — `create_model` plus `model_json_schema()` produces the schema,
  and `ensure_strict_json_schema` adds the `additionalProperties: false` that strict
  mode requires. Those are two fewer things to get subtly wrong than a bespoke emitter.
* **Validation comes with it.** `output_model(**tool_use["input"])` is Pydantic, so a
  type the grammar somehow let through still fails here, with field-path errors. Against
  `outputConfig` we would parse and validate the returned text ourselves.
* **The same Pydantic model is the field set's shape everywhere** — the schema sent, the
  validator on return, and what the artifact and workbook writers iterate. One source of
  truth beats a generator plus a parallel dataclass.
* **It is the SDK surface, so a Strands upgrade carries it.** Hand-rolled `outputConfig`
  would sit beside `BedrockModel` doing a job `BedrockModel` already does.

`outputConfig.textFormat` stays documented here as the escape hatch, because it is one
`client.converse` call away and it is the fallback if strict mode's own restrictions
(6.5) turn out to bite a real field set. Rejected outright: **Strands' agent-level
structured output**, for the reason §2.6 gives — it registers the schema as a tool in
the turn's own `toolConfig` and appends a forcing prompt to the conversation, which is
both halves of what D1 refuses.

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

Consequences for our Pydantic model builder, each a real constraint rather than a note:

* `additionalProperties: false` always, on every object — which `strict_tools` applies
  for us via `ensure_strict_json_schema`, so this one is handled rather than remembered.
* `type: enum` → a `string` with `enum`. Native, and the reason types are worth having.
* `type: date` → `string` with `"format": "date"`. Enforced as a format, and still
  re-parsed in Python, because a well-formed date can still be the wrong one.
* `type: currency` → `string`, not `number`. A currency value is "$500,000" in the
  document and the user wants to see what the document said; coercing to a float
  discards the symbol and invents precision. The numeric comparison that matters
  happens in G2's value check, which already handles currency symbols and thousands
  separators.
* `optional: true` → `Optional[str]` with no default, so the property stays in
  `required` and its type becomes a union with `null`. Omitting it from `required` would
  let the model silently drop a field, and a missing key and an absent value are
  different facts: `null` is the model saying "not in this document", which is
  answerable, and a missing key is the model saying nothing, which is not. **Strands
  does not force this for us** — `ensure_strict_json_schema`'s `require_all_properties`
  defaults to `False` for Bedrock (it exists for OpenAI), so a Pydantic field given a
  default would drop out of `required` and we would not be told. It is our rule to keep,
  and it has a second payoff under 6.5.
* **A `minimum`/`maximum` on a numeric field is not expressible.** Range checks, and
  every cross-field rule, must run in Python after the rows come back. That is where
  G9 attaches (§15) and it is not a workaround — a constraint the grammar enforces is
  one the model cannot report honestly, so "the award amount is implausible" is better
  as a flag on a row than as a value the model was forbidden to emit.

**6.4 Grammar compilation is a latency cliff, and the schema must be stable across
runs.** Bedrock compiles a new schema's grammar on first use — the docs say "up to a
few minutes" — then caches it for **24 hours from first access**, per account, for
identical schemas. Two consequences:

* **One field-set version must produce one schema.** Not "a canonical serializer", which
  is what this spec asked for before Strands' surface was read — Pydantic emits the
  schema and we do not hand-serialize it. What we own instead is the *model*: build it
  with `create_model` from the authored field order, and **cache the generated class per
  `(field set, version)`** so two runs of one field set are the same class and therefore
  the same schema. The cache is not an optimization: a fresh class per call also misses
  Strands' own `_TOOL_SPEC_CACHE`, which keys on the model type. A unit test asserts the
  emitted `inputSchema` is equal across two builds of one fixture set, which is the
  property that matters and is weaker than byte-equality of a string we no longer write.
* **First use of a new or edited field set can be slow, and the tool must survive it.**
  The extraction call gets a bounded timeout. On timeout, or on a `ValidationException`
  naming an unsupported schema feature, it retries **once on a model instance without
  `strict_tools`**, keeping the same prompt and the same Pydantic validation on return,
  and marks the run `schema_unenforced`. (`BedrockModel.structured_output` also raises
  `ValueError` when the model answers without calling the forced tool; that takes the
  same path.) The marker reaches the SSE event and the row — never silently — so a
  reviewer knows the types were asked for rather than guaranteed. A field set used
  weekly recompiles each time; if that proves to be the common case rather than the
  edge, the answer is a nightly warm call per field set, which is one scheduled call and
  is **out of this slice**.

**6.5 Which models actually support it, and the one that bit.** Verified on the Claude
Haiku 4.5 model card: structured outputs is **supported** on the `bedrock-runtime`
endpoint (and on `bedrock-mantle`), and the single exclusion is the India geographic
profile — "Structured outputs are not supported when you invoke Claude Haiku 4.5
through the India Geographic cross-Region inference profile
(`in.anthropic.claude-haiku-4-5-20251001-v1:0`)". Our dev ids are `us.*` and prod's are
`global.*`; both carry it. The general shape of the trap is real and worth carrying
forward: **support is per model *and* per inference profile**, and a deployment that
localises its model ids can lose the feature without changing a line of code.

One further restriction belongs to strict tool use specifically, and Strands' own
docstring is where it is written down rather than the AWS page: with `strict: true`,
"optional parameters are capped across all tools in the request", and `oneOf` is
unsupported. We do not know the cap's number and should not guess one — but **D5
sidesteps it entirely**, because every field is `required` with optionality expressed as
a nullable type, so a 40-field set declares zero optional parameters. A rule adopted for
honesty about missing values turns out to be the one that keeps a large field set inside
strict mode. Worth noticing rather than relying on: if a real field set ever trips this,
6.1's `outputConfig` escape hatch is the answer, since that mechanism's documented
subset carries no such cap.

**6.6 The fleet problem dissolves, rather than being solved.** `mantle.py` builds
Strands `OpenAIModel` / `OpenAIResponsesModel` against the Bedrock Mantle
OpenAI-compatible surface, with canonical→native parameter maps per API mode and the
documented behaviour that "a canonical param without an entry here is silently
dropped". A hosted OpenAI model is therefore not speaking Converse at all, and neither
`outputConfig` nor `strict_tools` is a thing it would receive. (`OpenAIModel` has its
own `structured_output`, against `response_format`; we simply never reach it.)

The side-channel design means we never have to care: the extraction call builds **its
own dedicated `BedrockModel`**, named by `DOCUMENT_EXTRACT_MODEL_ID` (CDK-set per
environment, `us.*` on dev, `global.*` on prod, matching how the stack already
distinguishes them). The turn's model is irrelevant to it, and so is the turn's model
*class*. **So the answer to "what happens on a model that does not
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
call, made with `BedrockModel.structured_output` on a dedicated `strict_tools=True`
model.** Generating the agent tool's `inputSchema` from the field list puts a
per-session, per-edit value ahead of the entire cacheable prefix, and re-writes
30k–150k tokens at 1.25× base input rate on every change — not the "one rewrite" the
gap analysis costed. A constant spec with the detail as a runtime argument is the
`verify_sources` answer and would have been enough; a schema on a request of its own is
strictly better, because it keeps the enums and types the runtime-argument shape gives
up, on a request with no prefix to protect.

*Which* request-of-its-own took two passes, and the second answer is the SDK's.
Hand-rolled `outputConfig.textFormat` works and is still the escape hatch, but Strands'
model-level path is the same architecture with the schema generator, the canonical
serializer and the result validator already written (§6.1). **Rejected: Strands'
agent-level structured output** — it registers the schema as a dynamic tool in the
turn's own registry and appends a forcing prompt to the conversation, which is both
halves of what this decision refuses.

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

**D4 — Field order is the authored order of the markdown rows, and the generated
Pydantic model is cached per `(field set, version)`.** Their `item_order` exists for
drag-and-drop; we need it for grammar reuse (§6.4) and for every determinism rule in
`CLAUDE.md`. Never a dict iteration, never a sort on read. The cache is part of the
decision rather than a tuning detail: one class per field-set version is what makes two
runs the same schema, and it is also what Strands' own `_TOOL_SPEC_CACHE` keys on.

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
timeout, a `ValidationException` naming an unsupported schema feature, or the
`ValueError` Strands raises when the model answers without calling the forced tool,
retry once on a model instance **without** `strict_tools` — same prompt, same Pydantic
validation on return — and set `schemaEnforced: false` on the event, the row and the
artifact header. A reviewer must be able to tell the difference between types that were
guaranteed and types that were requested.

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

**D15 — `strict_tools` is set on the extraction model instance only, and never on the
chat agent's.** It is a `BedrockModel` *config* flag, so it adds `strict: true` to
**every** tool spec in that model's requests and restricts the JSON Schema features
every one of them may use — and "a schema that uses an unsupported feature fails at
request time with a `ValidationException`". Our agents carry MCP tool schemas we do not
author and cannot fix, where `oneOf` is ordinary. Setting it on the agent's model would
make a third party's tool definition able to fail an entire turn. The agent factory
builds `CountTokensBedrockModel(native_projection=False, **bedrock_config)`, so the flag
would arrive through `bedrock_config` if anyone ever added it there; the extraction path
constructs its own instance instead and shares nothing with it. A test asserts the flag
appears in exactly one place.

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
| PR-1 | The pure layer: the field-set markdown parser, the field model, the `create_model` builder with its per-`(set, version)` cache, and unit tests (the subset rules of §6.3, nullable optionals staying in `required`, enum fields, `currency` as string, the authored-order guarantee, schema equality across two builds of one fixture, malformed-table errors). Plus the flag, the CDK wiring, the CDK tests and the SPA flag field. **Nothing wired to a turn** | Yes — flag and CDK files |
| PR-2 | `document_extract` mounted in `ChatAgent._create_agent`; field-set resolution through the skill resource store; the dedicated `BedrockModel(strict_tools=True)` and its `structured_output` call, with the marked unenforced fallback; the `verify_claims` integration; the backend-authored artifact and the in-process `.xlsx`; the `document_extraction` SSE event and its broker; the `EXTR#` store and its `GET /messages` replay; the `C#` metering row; the content-free EMF record; the D15 single-call-site test. Backend complete and dev-verifiable by reading the stream | Yes — imports G2 |
| PR-3 | SPA render behind `features.documentExtract`: the rows table with per-cell provenance in G2's vocabulary, the `schemaEnforced` disclosure, and reload replay | Yes — follows G2's PR-3 vocabulary |

PR-1 and PR-2 are slice one. PR-3 is stated as outside it rather than assumed.

## 14. How we'll know

* **The pure layer is unit-tested**, because parsing a table and building a Pydantic
  model are pure. The §6.3 subset rules and the authored-order guarantee each have
  cases, and one asserts the emitted `inputSchema` is equal across two builds of one
  fixture set — schema drift costs minutes of grammar compilation, not cents.
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
* **A durable, unattended path for large backlogs.** Not built. N documents per call
  (§17) covers the **attended** case — an office sitting with a week's intake, getting a
  judgement across it in one turn — and that is deliberately where this stops. The
  unattended path means work that survives the tab closing: a queue, a heartbeat, a
  reaper, an atomic `finalized_at`, bounded delivery attempts and a distinct
  `delivery_failures` state. That is a different feature with its own failure modes, and
  building it before anyone needs it would be building it against a guess.

  **It is built when one of two things happens**, and not before: someone hits the
  measured ceiling in §17.7 in real use — the reduce going unreliable above ten
  documents, or batches running up against `MAX_DOCUMENTS` — or someone asks for runs
  that must survive the tab. Either is a concrete signal; "we might want this at scale"
  is not.

  **The "G5" label from the gap analysis is retired**, because it conflated two
  different jobs under one name: weekly triage of an intake, and one-off bulk
  processing of a backlog. Only the first is in scope, it is now §17 rather than a
  non-goal, and keeping the single label would make "is G5 done?" unanswerable.

  Nothing in §17 should make the unattended path harder, which is why each extraction
  call is a pure function of `(document, field set)` holding no run state: a queue-driven
  worker would call the same function with the same arguments.
* **Any re-implementation of provenance logic.** We call `verify_claims` and read
  `state`. We do not derive a state, construct a `SourceVerification`, reintroduce the
  boolean pair, or widen the import allowance the boundary test enforces.
* **The Extractions surface** — a persisted, queryable, correctable run entity (§4,
  given up #2). Built when there is a measured reason, against rows from this slice.
* **Extending the Outputs library to files**, so the `.xlsx` could land there (§8.4).
  A change to the Outputs model, not to extraction.
* **An eval and tuning loop over field sets** — their `extraction_config`,
  `tuning_result` and optimizer. That is G4. It would share a runner with the
  unattended path above, which is another reason not to build either on a guess.
* **A warm call per field set** to keep grammars compiled (§6.4), until the measurement
  says it is needed.

---

## 16. Where this spec was wrong

Written before the code, so parts of it were a hypothesis. Recorded here rather than
quietly fixed, because the next reader needs to know which decisions survived contact.
Each has a test naming the finding.

**16.1 `references/field-sets/<name>.md` is not an expressible path (§4).**
`SkillBundleFiles.validate_filename` is `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` — no path
separators — and a manifest entry's standard path is `{kind_dir}/{filename}` with no
nesting. The convention shipped is **`field-set.<name>.md`**, read at
`references/field-set.<name>.md`. Every property D2 rests on survives (a flat manifest
scan with no S3 GET, skill-scoped access by construction, versioning and sharing for
free); only the directory is given up.

**16.2 D5's letter conflicts with §14, and §14 wins.** D5 reads as "`optional: true`
becomes a nullable union", implying a declared-required field is non-nullable. That
cannot hold with §14's requirement that a field the document does not answer come back
`null` rather than invented: a non-nullable value leaves the model no way to say "not
here" except to write something. **Every value is nullable**, and `optional` now records
the author's expectation instead of the schema's — a null on a field the author marked
required is a finding for the reviewer, not a schema violation. D5's load-bearing half is
kept exactly: nothing ever leaves `required`, so absence is always an explicit null and
never a missing key, which also preserves §6.5's payoff (a 40-field set declares zero
optional parameters).

**16.3 Strands rewrites the schema on the way out, and §6.3's rules have to be checked
on the rewritten form.** `model_json_schema()` is not what Bedrock receives.
`convert_pydantic_to_tool_spec` inlines every `$defs` and collapses each `anyOf` into a
type array, pushing nullability *down into nested branches*. Two defects were invisible
at the Pydantic level:

* **`Optional[Literal[...]]` becomes `{"enum": [...], "type": ["string","null"]}`** —
  `type` permits null, `enum` refuses it, and `enum` wins. **An enum field could not
  report an absent value**, so the model's only legal answer was to pick one, which is
  the invention §14 exists to catch. `None` is now a member of the enum, which §6.3's
  subset explicitly allows.
* **`Optional[List[str]]` leaks null into `items`**, making `["climate", null]` a
  schema-valid answer. The list is nullable; its entries are not.

Also: a `format` passed through Pydantic's `json_schema_extra` lands *beside* the
`anyOf`, where no validator reads it. Annotating the inner type puts it inside the
string branch.

**16.4 The model-cache key should include the content, not just `(set, version)` (D4).**
Keying on the version alone serves the previous schema for the life of the process to an
author who edits a `find` phrase without bumping `version` — and the symptom reads as a
model regression. The key is `(name, version, content-digest)`, so a cache hit means
exactly "the same schema", which is the property §6.4 actually needs. Two identical sets
still share one class, so grammar reuse is unaffected.

**16.5 `pages` must not narrow what provenance is verified against (§5, §7).** The spec
does not say this and the obvious implementation gets it wrong. A quote the model took
from page 1 of a run narrowed to pages 3–5 is still a real passage of this document;
searching only the selection reports it `unverified` — **manufacturing** a hallucination
signal, which is the one error this family of features must never produce. Extraction
sees the selection; `document_units` always gets the whole document.

**16.6 Two implementation decisions the spec deliberately left open, settled against
real code.**

* **The broker (§9) is a sibling of `provenance_broker`, not a generalization of it.**
  That module is already generic — a thread-locked bounded dict keyed on
  `(user_id, session_id)` — and only its name and its drain's event type make it
  "provenance". Generalizing means a kind discriminator on every payload plus a branch
  in a drain that currently has none, to avoid duplicating about forty lines. Two
  brokers that each do one thing is cheaper, and it keeps either feature's kill switch
  out of the other's code.
* **Metering (D12) writes its `C#` row with `message_id="extract:<tool_use_id>"`.** The
  row's SK is `C#<timestamp>#<uuid>` and `message_id` is only an attribute, so a
  non-turn call can tag itself — the precedent is the voice path's `voice:<index>`.

**16.7 Two traps found in the SDK and the model layer, neither in this spec.**

* **Token usage is not where it looks.** `process_stream` wraps each provider chunk as
  `{"event": chunk}`, so the metadata is a level deeper than it appears, and the
  authoritative reading is the final `ModelStopReason`'s `{"stop": (stop_reason,
  message, usage, metrics)}`. `event["metadata"]["usage"]` — the obvious shape — never
  occurs. This was silent in the worst way: a metering path that skips an empty reading
  writes **no** `C#` row, and `UnmeteredModelCall` does not fire either, since that
  covers a call that reported usage and failed to price. So §9's `$0` trap had nothing
  left to detect it. Strands also seeds usage at zero and overwrites it on the metadata
  chunk, so an all-zero reading means "not reported yet", not "free".
* **`getattr(model, "json")` returns Pydantic's deprecated method, not the field.** A
  field named `json`, `copy` or `schema` is bound to a renamed attribute with the
  authored name as its alias, and an attribute-first read yields a bound method — which
  is not `None`, so it passes every guard and surfaces as a mysteriously empty column.
  Look up by alias through `model_fields` first.

**16.8 One invariant the spec assumed and nothing enforced.**
`document_read.DOCUMENT_CLASS_FORMATS` and `source_provenance.UNIT_KINDS` must agree, or
`document_units(fmt, texts)` raises through the agent's event loop and fails the turn —
for `verify_sources` as much as for `document_extract`. They matched; now a test says so.

**16.9 The capacity limits are real, undocumented, and far tighter than §5
assumed. `MAX_FIELDS = 40` was never reachable.** Measured on dev against
`us.anthropic.claude-haiku-4-5-20251001-v1:0`, 2026-10-20, with randomized
property names so every schema was provably cold.

Strict tool use has **four** distinct capacity failures, none of them in the
AWS documentation, each with its own message and its own timescale:

| Bedrock's message | When it arrives |
|---|---|
| `too many parameters with union types (… limit: 16 parameters with unions)` | < 1s, a pure count check |
| `Schemas contains too many parameters and cause performance issues` | ~0.3s, a complexity pre-check |
| `Schema is too complex for compilation` | ~25s |
| `Compiled grammar size (393.0MB) exceeds maximum allowed size (300MB)` | 55–95s, after compiling |

What the sweep found, in our schema shape (one nested
`{nullable value, string quote}` object per field):

| Fields | Result |
|---|---|
| 8 | compiles, ~13s |
| 10 | compiles, ~15s |
| 12 | compiles, ~19s |
| **14** | **compiles, ~20–23s — the ceiling** |
| 16 | refused in 0.29s (performance pre-check) |

So `MAX_FIELDS` is **14**, measured rather than chosen, and the refusal says
whose limit it is — an author told "the limit is 14" assumes it is ours and
asks for it to be raised.

Three things the sweep corrects, each of which inverts an assumption:

* **Nullability is not the driver.** Eight *non-nullable* flat strings produce
  the same 314.4MB grammar as eight nullable ones. §6.5's reasoning — that D5's
  nullable-union trick "sidesteps" the optional-parameter cap — was wrong in
  its premise but right in its conclusion, and for a different reason: the
  separate 16-union counter does exist, and an enum-with-`null` does **not**
  count against it, so 16.3's enum fix buys union budget as well as honesty.
* **Flat top-level scalars are the expensive shape; nesting is cheap.** Eight
  flat strings blow the 300MB ceiling, while sixteen nested two-property
  objects (thirty-two leaf strings) compile in 22s. The per-field object this
  spec adopted for §7's "quote as a sibling of the value" turns out to be what
  makes any useful field count reachable at all. It was chosen for provenance
  and it paid for itself in grammar size.
* **Description length is free.** Fourteen fields with one-sentence `find`
  phrases compile as fast as fourteen with three-word ones (19.6s vs 22.9s),
  which matches AWS's own statement that changing only `name` or `description`
  does not invalidate a cached grammar. **Tuning a `find` phrase therefore
  costs no recompilation** — the thing an office does most often is the thing
  that is cheapest, which is worth knowing before anyone optimizes for it.

**This leaves §1's motivating example not fitting in one call.** A grants
office wanting "the same twenty answers" gets fourteen per field set and must
split the rest into a second set. That is a real product limitation, and it is
recorded here rather than discovered by the first office to author a wide set.
Chunking one logical set across several constrained calls is the obvious fix
and is **not** built — see §15.

**16.10 A capacity failure must not fall back to unenforced.** D11 would have
retried *any* schema-shaped `ValidationException` without `strict_tools`. But a
field set one field too wide is not a transient fault: it would have produced
unenforced types on **every run, forever**, with a `schemaEnforced: false`
marker as the only trace, and the office that authored it would never be told
the cause. `SchemaTooComplexError` is raised instead, naming the field set and
saying it needs splitting. D11's fallback keeps its original job — a schema
Bedrock refuses on a *feature* — which is what it was actually written for.

**16.11 Parallel calls against a cold schema each pay compilation, but the
wall-clock cost is one compilation, not N.** Measured with five concurrent
calls on a cold 10-field schema: every call took 15.3–16.0s where the warm
median is 2.95s, so none was served a grammar warmed by another. Wall clock for
all five was 16.05s.

The feared outcome — "the first run of any new or edited field set is
catastrophically slow, N times over" — **does not occur**, because the
compilations proceed concurrently. What is wasted is Bedrock's compute, N times
over, not the user's time.

Warming with one call before fanning out is still adopted, but on the honest
grounds rather than the assumed ones: it costs about three seconds *more* wall
clock on a cold schema (16s → 19s) and one document's latency on a warm one, and
it buys (a) one clear error instead of N identical ones when the field set
exceeds a capacity limit — which the measurements above show is an easy mistake
to make — and (b) no N-fold redundant compilation, which matters against a
deployment that already has a Bedrock throttle alarm. It is not a latency
optimization and the spec should not claim it is.

---

## 17. N documents per call (Amendment 2026-10-20)

### 17.1 Why the set, and not the documents

The outcome worth building is a **judgement across a set**: *of these fourteen,
three are worth pursuing; two collide on the same March deadline and both need
the same PI; one demands cost share we can't meet.* None of those three clauses
is a fact about any single document. A per-document pipeline structurally
cannot produce them, which is exactly why this is worth doing rather than
buying.

So `document_extract` takes `upload_ids: List[str]`, and one tool call carries
the whole stack. One conversation per document would reproduce the limitation
with worse ergonomics, and cost **5–10x more** — each conversation paying a
cache *write* for its own system prompt, `toolConfig` and history, where one
conversation pays that once and then reads it at 0.1x:

| N | prefix | one conversation each | one conversation | ratio |
|---|---|---|---|---|
| 10 | 30k tok | $0.412 | $0.075 | 5.5x |
| 50 | 30k tok | $2.062 | $0.221 | 9.3x |
| 10 | 150k tok | $2.062 | $0.358 | 5.8x |
| 50 | 150k tok | $10.312 | $1.033 | 10.0x |

(Haiku 4.5 Regional: $1.10/MTok base, $1.375 cache write, $0.11 cache read.)

**The "~200x" this amendment was briefed with does not hold** — the arithmetic
gives 5–10x across the realistic range, growing with N because the single
conversation amortizes one prefix while the alternative re-writes it per
document. The decision is unaffected: 10x is still decisive, and the
*structural* argument above — that a per-document pipeline cannot produce the
answer at all — was always the load-bearing one. Recorded because a number
nobody has checked is worth less than a smaller one that holds.

### 17.2 One artifact and one workbook, not N

The deliverable of a batch is the **comparison**, so the batch writes one
markdown artifact and one workbook, both holding the whole set. The artifact
leads with a comparison table oriented **field-per-row, document-per-column**,
because that is the orientation in which a collision is visible at a glance —
the same deadline twice on one row. The per-document sections follow, carrying
the provenance a reviewer goes to once the comparison has told them where to
look. The workbook carries both orientations as two sheets, since sorting and
filtering want one row per `(document, field)`.

A reviewer handed fourteen separate tables has been handed the work of joining
them, which is the work the feature exists to do.

**The stored row is keyed `EXTR#<tool_use_id>#<upload_id>`**, not on the tool
use alone: N documents in one call share a `toolUseId`, and the single-document
key would have had them silently overwrite each other, leaving a reload showing
one row set where the user saw fourteen. Keying on both also keeps each row
independently inside the 400KB item limit.

### 17.3 The reduce step is a skill, not code

The model can already reason over N row sets. What it cannot know is what a
research-administration office wants out of a stack — which collisions matter,
what counts as an outlier, that a dismissal is only useful with its reason
attached. That is institutional knowledge, it differs between offices, and it
changes without a deploy.

So it is markdown in the funder-family skill that already holds the field set
(`docs/examples/skills/funder-family/references/reduce-across-documents.md`),
where it costs nothing until the skill activates. **No synthesis function is
written in Python.** A Python reduce would freeze one office's judgement into
every deployment's build and make "we also care about internal competition" a
pull request.

### 17.4 Bounded concurrency, and the number

`asyncio.gather` under a semaphore, default **4**, from
`DOCUMENT_EXTRACT_CONCURRENCY` and clamped to `1..16`.

Four is deliberately conservative. Each call is a constrained Converse request
whose input is a whole document, so four in flight is already meaningful
Bedrock traffic from a single turn — and this deployment has a Bedrock throttle
alarm that an over-eager default would trip on the first real batch, which is
the worst possible moment to discover the number. Four also captures most of
the available win: fourteen documents at four-wide is four waves, not fourteen.

A semaphore rather than chunked waves, because chunking makes every wave wait
for its slowest member — one 300-page document in a batch of four would stall
three idle slots.

**Raise it on evidence**: the `DocumentExtract` EMF records plus the absence of
throttling across a few real batches. The lever is an environment variable, so
trying a different number in one environment needs no deploy.

### 17.5 Warming the grammar, and what measurement said about it

The concern was that N parallel calls against a **cold** field set would each
pay grammar compilation, making the first run of any new or edited set
catastrophically slow, N times over.

**Measured** (§16.11): five concurrent calls on a cold 10-field schema each
took 15.3–16.0s against a warm median of 2.95s — so yes, every call paid
compilation and none was served a grammar another had warmed. But they paid it
*concurrently*: wall clock for all five was 16.05s. **The N-times-slower first
run does not happen.** What is wasted is Bedrock's compute, N times over, not
the user's time.

Warming with one call before fanning out is still adopted, on honest grounds
rather than the assumed ones. It costs about three seconds more on a cold
schema and one document's latency on a warm one, and buys:

* **Error economy.** A field set that exceeds one of the four undocumented
  capacity limits (§16.9) fails in under a second. Warming turns that into one
  clear error instead of N identical ones, and stops before spending the batch.
* **No N-fold redundant compilation**, against an account with a throttle alarm.

The warming call is **document one doing real work**, not a throwaway, so
nothing is spent to get it. If it fails *because the field set cannot be
compiled*, the batch stops and that one reason is returned for every document —
firing the rest would produce N copies of an error about the author's field set
that no user can act on differently. Any other first-document failure is just
one bad document, and the rest proceed.

### 17.6 Partial failure is the normal case

One unreadable document in fourteen must not sink the batch. Every document
comes back as a result or a reason, the reasons are named in the model-facing
note, and the artifact lists them above the comparison. A set where three
documents failed silently is a judgement made on eleven documents while reading
as though it were made on fourteen — which is a worse failure than returning
nothing.

### 17.7 The measured ceiling

**Token cost is linear and cheap.** Built from the real model-facing payload
for a full-width 14-field set:

| N | payload chars | ≈tokens | per document | Haiku in | Sonnet in |
|---|---|---|---|---|---|
| 1 | 1,621 | 450 | 450 | $0.0005 | $0.0015 |
| 10 | 12,044 | 3,346 | 335 | $0.0037 | $0.0110 |
| 25 | 29,414 | 8,171 | 327 | $0.0090 | $0.0270 |
| 50 | 58,364 | 16,212 | 324 | $0.0178 | $0.0535 |

~325 tokens per document, flat. At N=50 the payload is ~16k tokens sitting in
the cacheable prefix, re-read at 0.1x on every later turn — about $0.036 on
Haiku across twenty further turns. **Cost is not the binding constraint.**

**Judgement is.** Three trials per N, with a planted cross-document collision
(two documents sharing a deadline *and* a required PI) and a planted outlier
budget cap, scored on whether the reduce found each:

| N | collision found | outlier found |
|---|---|---|
| 10 | **3/3** | 2/3 |
| 25 | 1/3 | 2/3 |
| 50 | 2/3 | 2/3 |

**The reduce is reliable at N≈10 and roughly a coin flip above it.** There is
no clean cliff — N=25 scoring below N=50 is noise at three trials — and the
scorer is a substring match that can undercount a correct answer phrased
differently. The honest reading is: reliable at ten, unreliable beyond, and the
limit is the model's attention across a wide set rather than anything about
cost or concurrency.

That inverts the intuition worth recording: **the expensive-looking thing is
cheap, and the free-looking thing is the ceiling.**

One mitigation falls straight out of the failure mode. The misses are always a
*cross-document* fact going unnoticed, so above fifteen documents the tool's
note tells the model to work the comparison **field by field** rather than
document by document. Costs nothing and addresses the thing that actually fails.

**A note on how this was measured, because it nearly produced a false result.**
The first run of this experiment reported collision detection at 0/3 for N=50.
The fixture was wrong: it paired every value with one boilerplate quote, so
`verify_claims` correctly marked all 350 values `quote_unsupported`, and the
model correctly refused to make any recommendation on data where every single
value had failed its provenance check. That was the integration working
exactly as §7 intends, scored as a quality failure. The fixture now asserts
every planted row is `supported` before the model is asked anything.

## Sources

* `_local-notes/vandalizer-parity-and-gap-analysis.md` §G1 (this spec's seed), §G2 (the
  provenance model composed with), §G5 (whose label is retired — see §15),
  §G9 (deliberately out of scope).
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
* `strands-agents==1.57.2`, read in the venv 2026-10-20:
  `strands/models/bedrock.py` (`BedrockModel.structured_output` — one tool spec,
  `tool_choice: {"any": {}}`, Pydantic validation on return; the `strict_tools` config
  flag, and its docstring as the written source for the optional-parameter cap and the
  `oneOf` restriction), `strands/models/_strict_schema.py`
  (`ensure_strict_json_schema`; `require_all_properties` defaults to `False` for
  Bedrock), `strands/tools/structured_output/` (`StructuredOutputTool` and
  `_structured_output_context.register_tool` → `register_dynamic_tool` — the
  agent-level path this spec rejects), `strands/models/openai.py` (`structured_output`
  on the Mantle-facing class, never reached). Our own
  `agents/main_agent/core/agent_factory.py:50` builds
  `CountTokensBedrockModel(native_projection=False, **bedrock_config)`, which is why
  D15 is a decision rather than a note.
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
