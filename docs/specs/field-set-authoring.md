# Field set authoring

**Status:** proposed — not built.
**Closes:** `docs/specs/structured-extraction.md` §4, "what is given up" #1.
**Depends on:** structured extraction (`DOCUMENT_EXTRACT_ENABLED`), in development.

---

## 1. The gap, stated exactly

Structured extraction shipped to `develop` on 2026-10-20 and runs on dev. A named,
typed **field set** is resolved from a skill's reference bundle, run over N documents on
its own constrained model call, and every value comes back with a provenance state.

A field set is one file: `references/field-set.<name>.md`.

**There is no way to write that file without an API client.** Today it takes a
`POST /skills/mine` and a `POST /skills/mine/{id}/resources` with a multipart body whose
contents the author had to format by hand. The structured-extraction spec recorded this
as a deliberate sequencing choice — "prove the extraction is trustworthy before handing
out the schema editor" — and the extraction is now demonstrably trustworthy, so the
choice has expired rather than been proven wrong.

### 1.1 What is *not* missing — verified on dev, 2026-10-20

An earlier framing of this gap said the feature was API-only end to end. It is not. On
dev, signed in as an ordinary user, through the SPA and nothing else:

| Step | Result |
|---|---|
| Composer → **Add files or photos** → a document | Chip renders, bytes reach the turn |
| Composer → **Skills** → *NSF Solicitation Review (test)* | Inserts `/nsf-solicitation-review-test`, sends `enabled_skills` |
| Ask for the field set over the attached document | `document_extract` ran in 6.6s, `schemaEnforced: true` |

The run's seven rows against the fixture's captured ground truth: **7/7 values correct**,
all four provenance states present (`supported` ×3, `quote_unsupported` ×1,
`unassessed` ×2, `unverified` ×1), one artifact and one workbook written, an `EXTR#` row
persisted and replayed on `GET /messages` as `extractions`.

The skill carrying the set is a **user-owned** skill — it lists under `/skills/mine`
with `field-set.nsf-solicitation.md` in its manifest. So the entire user tier already
works: resolution, access, the mount gate, the constrained call, provenance, persistence.

**The gap is one file the user cannot write.** Everything downstream of it is built.

### 1.2 Two things the same run surfaced, neither in this scope

* **The rows are not rendered.** The thread shows the model's prose summary and nothing
  else; the typed table reaches the SPA on the `document_extraction` event and in the
  `extractions` sidecar, and nothing reads either. The model, having no way to know,
  told the user the table "is already displayed in your chat files". Structured
  extraction's PR-3 owns this and places it outside slice one. It is the larger of the
  two remaining gaps by user-visible effect, and it should be sequenced next.
* **The first `document_extract` call failed in 71 ms** because the model guessed an
  upload id; it recovered by calling `document_read` and retrying. A wasted round trip
  on the happy path, not a correctness problem.

---

## 2. Scope

**In:** creating, reading, editing, renaming and deleting a field set on a skill the
signed-in user owns, through the Customize surface, with the same parser that the
extraction tool uses as the only authority on whether the file is valid.

**Out**, each for its own reason:

| Not doing | Why |
|---|---|
| Editing or validating `## Rules` | G9 owns that section and nothing reads it yet. The editor **preserves it verbatim** and shows it read-only; it must never be the thing that silently deletes an author's rules |
| Sharing one field set across skills | A set has no identity outside its skill. That is D2 of structured extraction, and it is what buys the access model for free |
| Versioning beyond the frontmatter `version` | No history, no diff, no restore. The skill's own bundle versioning is the backstop |
| Anything the optimizer owns | No `find`-phrase suggestions, no accuracy-per-field, no "try this set on a document" from inside the editor |
| Rendering extraction results | §1.2. Separate work, separate PR |
| A field-set picker in the composer | The tool resolves sets from the turn's active skills. A picker would be a second answer to a question already answered |
| Admin authoring (`/admin/skills/...`) | The admin resource surface exists and works. This is about the user tier, which has none |

---

## 3. Where it lives

**Decision (D1): a dedicated route under the skill that owns the set.** Not a top-level
Customize tab, not a modal, not a section crammed into the skill form.

```
/customize/skills/:skillId/field-sets/new
/customize/skills/:skillId/field-sets/:name
```

Reached from a **Field sets** section on the skill edit form, above the existing
Supporting files fieldset, listing each set with its title, field count and version.

Three reasons, in order of weight:

1. **A top-level tab would imply an entity that does not exist.** Structured extraction's
   D2 is that a field set is reference material inside a skill — that is what makes its
   visibility, RBAC grant, Agent binding and project-harness binding already correct,
   four access questions nobody has to re-answer. A `/customize/field-sets` page listing
   sets across skills would be a list of things with no owner on screen, and the first
   feature request against it would be "let me share one", which D2 exists to refuse.
2. **A skill can have several sets, and a set needs room.** Seven field cards with a
   multi-line `find` on each is a page, not a fieldset. The skill form is already long.
3. **It keeps the bundle view honest.** The Supporting files list still shows
   `field-set.nsf-solicitation.md` as the file it is, with View and Delete; a field-set
   file additionally gets **Edit** , which routes here. Two views of one file, with the
   file never hidden.

---

## 4. The format is owned by the backend, on both sides

**Decision (D2): the SPA never parses and never serializes the field-set format.**

The wire is structured JSON. `apis.shared.extraction.field_sets` owns bytes in both
directions: it already owns `parse_field_set`, and this adds `serialize_field_set`
beside it, in the same pure, stdlib-only module, under the same unit tests.

This is the same argument as `grants_model_access` — a format with two implementations
has two answers, and they diverge. A TypeScript parser would have to re-implement
`_COLUMN_ALIASES`, the delimiter-row test, the enum comma-or-pipe split, the
`MAX_FIELDS = 14` ceiling and six distinct authoring errors, and the day they drift is
the day the editor accepts a file the tool refuses — at which point the author sees a
green Save and a broken extraction, which is the worst available failure.

It also means **the API path gets validation for free**: a `curl` that uploads a
malformed field set is refused with the same message the editor would have shown.

### 4.1 Routes

All in `backend/src/apis/app_api/skills/routes.py`, all
`Depends(get_current_user_from_session)`, all scoped to a skill the caller owns through
the existing `UserSkillService`. No new table, no new GSI, no new RBAC noun.

| Route | Does |
|---|---|
| `GET /skills/mine/{skill_id}/field-sets` | Lists the sets on this skill. Derived from the resource **manifest** — filenames only, no S3 GET |
| `GET /skills/mine/{skill_id}/field-sets/{name}` | One set, parsed: `{name, title, version, fields[], rules, markdown, unmanagedColumns[]}`. Carries the raw `markdown` too, so the escape hatch needs no second call |
| `PUT /skills/mine/{skill_id}/field-sets/{name}` | Writes one set. Body is **either** `{title, version, fields[], rules}` (structured) **or** `{markdown}` (raw). `?validate=true` parses and returns the same body shape without writing |
| `DELETE` | Already exists: `DELETE /skills/mine/{id}/resources/{filename}`. Not duplicated |

A `GET` of a set whose stored file does not parse returns **400 with the parser's
message and the raw markdown**, never a 500 and never a silent empty form. A file that
is already broken is exactly the file someone needs to open.

### 4.2 Validation on write, unconditionally

**Decision (D6): `SkillBundleFiles.add_resource` parses any file whose name is a field
set, and refuses it on `FieldSetError`.** Not only on the new routes — on the existing
resource upload too, and whether or not any feature flag is on.

The alternative is to validate only behind `FIELD_SET_AUTHORING_ENABLED`, which would
mean a deployment that later flips the flag discovers files that were invalid all along.
A stored-invalid field set fails at *extraction* time, mid-turn, in front of a user who
did not author it.

**This is a behaviour change for deployments that are not using this feature**: a
`POST .../resources` of a file named `field-set.*.md` that does not parse now returns
400 where it returned 200. It is a correct 400 — the file could never have worked — but
it is a change, and it gets a CHANGELOG line of its own. *(Open question 1 below.)*

---

## 5. The editor

**Decision (D3): a vertical list of field cards, not a grid.** An office authors seven
rows of prose-ish search phrases, not a spreadsheet.

Each card:

| Input | Control | Notes |
|---|---|---|
| **Title** | text | What the column is called. First field, because it is what the author is thinking in |
| **Name** | text, auto-derived | `snake_case`, derived from Title on first type and editable after. A rename is a new field, not a relabel — the card says so when an existing name changes |
| **Type** | select | `string · number · currency · date · boolean · enum · list` |
| **Find** | **textarea, 3 rows** | The visual centre of the card, with the widest control on the page |
| **Allowed values** | chips | Only when Type is `enum`. Hidden otherwise, because the parser *refuses* values on any other type |
| **Optional** | checkbox | "The document may legitimately not state this" |

### 5.1 `find` is the field, and the UI has to say so

The parser's own docstring: a field named `cost_share` tells a model almost nothing;
*"whether the funder requires institutional cost sharing, and at what percentage of
total project cost"* tells it what to look for. It is the only required column with no
default, it is what gets tuned when a field under-performs, and it is the one an
outsider leaves blank.

So it is not a narrow cell in a row. It is a labelled multi-line input with a helper
line — *"Say what to look for, the way you would tell a new colleague"* — and an example
drawn from the card's own Title. A card with an empty `find` cannot be saved, and the
message is the parser's, which already explains itself: *"'find' is required — it is the
phrase that locates the value, and a field without one is a column header with no
question behind it."*

**Measured, and worth telling the author:** field description length is free. Fourteen
fields with one-sentence descriptions compile a Bedrock grammar as fast as with
three-word ones. There is no cost argument for a terse `find`, and the editor should not
imply one by giving it a small box.

### 5.2 Order is declared

**Decision (D4): explicit Move up / Move down buttons on every card. Never sorted, never
drag-only.**

Authored row order *is* the order: it becomes the generated schema's property order, and
the editor must never reorder on read, on save, or as a side effect of anything. Drag
handles may be added later as an *additional* affordance; they are never the only one,
because reordering by keyboard is the case that gets dropped and this is a field an
office will reorder.

### 5.3 The ceiling is 14, and the editor says why

`MAX_FIELDS = 14` is measured, not chosen — Bedrock refuses to compile a wider
constrained schema in our shape, with four separate undocumented limits behind it. The
editor shows **"7 of 14 fields"** from the first card, disables Add at 14, and when
refusing says what the parser says: split the set in two and run them separately. A
cap that appears only on save, as a server error, after someone has written the
fifteenth `find` phrase, is a cap that wasted their afternoon.

### 5.4 The markdown escape hatch

**Decision (D7): an "Edit as markdown" mode beside the form, over the same file.**

The form cannot express everything the format allows, and three of those things are
real:

* `## Rules` — carried verbatim by the parser, read by nobody yet, and **lost forever**
  if a structured editor round-trips without it.
* **Unknown columns.** The parser ignores them deliberately, "so a set can carry a note
  column for its own authors". A form that writes back only the six columns it knows
  silently deletes that column.
* Prose before and after the table, which a field set is allowed to have because it is
  "a document an office reads, not only a payload".

Rather than invent a form field for each, the editor **preserves all three through the
structured path** (they ride on the `PUT` body as opaque strings) and offers raw
markdown for anything else. On opening a set whose table has an unmanaged column, the
form shows a banner naming the column and pointing at markdown mode — it does not
silently drop it and it does not refuse to open.

---

## 6. What stability actually requires — a correction

The brief for this work said an editor that changes "key order, spacing or separators"
produces a different schema and costs minutes of grammar compilation. **That is not what
the code does, and the difference changes the test.**

`model_for_field_set` keys on `(name, version, field_set_fingerprint(field_set))`, and
`field_set_fingerprint` hashes exactly `name · title · type · find · enum · optional`
per field, in order. `build_model` reads the parsed `FieldSet`. **Neither ever sees the
file's bytes.** So:

* Reformatting the markdown — different cell padding, `|:---|` instead of `|---|`,
  the `field` alias instead of `name`, reordered *header columns* — produces a
  byte-identical schema and reuses the compiled grammar. It costs nothing.
* Changing a `find` phrase changes the fingerprint and so misses our in-process model
  cache. It does **not** necessarily cost a grammar compile: AWS documents that changing
  only a name or description does not invalidate a cached grammar, and `find` reaches
  the schema only as part of a field's `description`.
* Reordering *fields*, renaming one, retyping one, or changing the enum values is what
  genuinely produces a new schema.

**Decision (D5): pin two different properties, for two different reasons.**

1. **Semantic round-trip, because it protects the author's cost.**
   `parse(serialize(parse(bytes))) == parse(bytes)` — field-for-field, in order,
   including `rules`. This is the property that guarantees opening a set and saving it
   unchanged changes nothing the schema can see. It must hold for hand-authored files
   with foreign formatting, which is the case a byte test cannot cover.
2. **Exact serializer bytes, because it protects the diff.**
   A pinned fixture of `serialize_field_set` output, asserted byte-for-byte, so the
   editor's own output format cannot drift between PRs and turn every saved file into a
   whole-file diff. This is a review-hygiene property, not a cost one, and the spec
   should not claim otherwise.

Two further rules follow and are enforced, not merely stated:

* **A no-op save does not bump `version`.** `version` is part of the cache key; bumping
  it on every save is a forced grammar recompile per keystroke-session. The editor
  bumps it only when the author does, and offers to when the field list changed shape.
* **The editor never reorders, retitles or normalizes whitespace inside `find`.** Trim
  the ends, touch nothing else.

---

## 7. Flag

**Decision (D8): its own switch, `FIELD_SET_AUTHORING_ENABLED` /
`CDK_FIELD_SET_AUTHORING_ENABLED`, SPA `features.fieldSetAuthoring`. app-api and the SPA
only — the Runtime has none, because resolution does not change.** In development, so
`"true"` and only `"true"` enables, and it is off in all three environment files.

Why not ride `DOCUMENT_EXTRACT_ENABLED`: "may this deployment run extractions" and "may
users write their own extraction schemas" are different decisions, and structured
extraction's §4 drew that line itself. A deployment can reasonably want
admin-authored sets with no user editor — which is exactly dev's state today.

It is a **feature switch**, not a rollout switch: it does not retire. Turning it off
takes an editor away from people, which is a product choice, not a kill switch for a
change meant to be invisible.

**Ceilings:** app-api gains one environment variable and no AWS resources, so the
expected all-on delta is **zero**. `infrastructure/test/platform-ceilings.test.ts` fails
by name until `fieldSetAuthoring` is added to `allFlagsOn()`; that failure is the guard
working, and the PR records the measured before/after counts rather than asserting zero.

**No new RBAC noun (D9).** A field set is a resource on a skill the user already owns
and can already upload arbitrary files to. Adding a catalog tool id or a role grant
would be inventing a permission for a capability the user demonstrably has.

---

## 8. Build order

| PR | Contents |
|---|---|
| **1** | `serialize_field_set` in `apis.shared.extraction.field_sets`, both pinned tests (§6), validate-on-write in `SkillBundleFiles` (§4.2) |
| **2** | The four app-api routes (§4.1), the flag, CDK config + ceilings entry, docs-site row |
| **3** | The SPA: route, list section on the skill form, the card editor, markdown mode |

PR 1 is useful on its own: it stops a malformed field set being storable at all, through
any path, whether or not the editor is ever turned on.

---

## 9. Open questions for review

1. **Unconditional validation (§4.2).** It is a 400 where there was a 200 for any
   deployment with a file named `field-set.*.md`. Correct, but a change. Flag-gate it
   instead?
2. **Does the escape hatch earn its place in slice one (§5.4)?** It is small, and it is
   the honest answer to `## Rules` and unknown columns. It is also a second editor over
   one file.
3. **Sequencing against the render gap (§1.2).** Authoring hands an office a schema whose
   results it still cannot read in the thread. Build the render first?
