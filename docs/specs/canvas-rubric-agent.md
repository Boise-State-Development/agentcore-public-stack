# Spec: Canvas Rubric Agent

**Status:** Design approved, not yet implemented. Auth path validated in dev 2026-09-10 (§10); blocked on content-fidelity fixes (§4).
**Audience:** A fresh implementation session with no prior context — this doc is self-contained.
**Owner:** Phil Merrell
**Last updated:** 2026-09-10

---

## 1. One-line summary

A marketplace Agent that lets a Boise State instructor say *"make me a rubric for the Final
Project in BIOL 101"* and get a standards-aligned rubric **published directly into Canvas and
attached to the assignment** — asking as few questions as possible, because most of what a
rubric generator normally asks for can be retrieved instead.

The origin is a Colab notebook (`manage_rubrics.py`) that faculty currently use: fill in a course
ID, hand-author a CSV, run a cell, import to Canvas. This replaces the whole loop.

---

## 2. Decisions already made (do not re-litigate)

| Decision | Choice | Why |
|---|---|---|
| **Surface** | A marketplace **Agent**, not a bare skill | It must bind a tool, a knowledge base, a curated model, and conversation starters. Skills on this platform are pure knowledge bundles and bind no tools. |
| **Canvas access** | The existing **`canvas_faculty` external MCP server** | Already deployed, already RBAC'd, already has `create_rubric` / `associate_rubric`. No new tool protocol. |
| **CSV** | Demoted to an **optional import/export**, never the deliverable | The notebook needed CSV because a script cannot read prose. The agent can. A CSV terminus would just be the notebook with a nicer front end. |
| **Wizard style** | **Retrieve first, propose second, ask last** | Faculty-first. A blank five-variable template is a form, not a guide. See §5. |
| **Pedagogy location** | A **skill**, not the system prompt | Skills are progressively disclosed — the body costs nothing until activated. The system prompt is in the cached prefix on every turn. |
| **Publish gate** | **Prompt-enforced confirmation** in v1 | Platform-level `needsApproval` is unavailable until the tool catalog snapshot is refreshed (§4.4). |

### Non-goals

- **No grading.** The bound tool exposes `grade_submission`, `grade_with_rubric`, and
  `bulk_grade_submissions`; the system prompt fences them off. Grading is a different product
  with a different risk profile.
- **No assignment or course authoring.** Same reasoning — `create_assignment`, `create_page`,
  `create_module` etc. come along with the binding and must be fenced.
- **No new MCP server.** Everything lands in `mcp-servers/packages/canvas-faculty`.
- **No rubric analytics.** Out of scope for v1.

---

## 3. Verified current state (audited 2026-09-10)

> Re-verify before building; this is a point-in-time snapshot.

### 3.1 The MCP server already does the job

`mcp-servers/packages/canvas-faculty/app.py` implements every operation the notebook performs:

| Notebook cell | MCP tool |
|---|---|
| `create_rubric()` + `read_criteria_from_csv()` | `create_rubric(course_id, title, criteria, assignment_id?)` |
| `update_assignment()` / `create_rubric_association` | `associate_rubric(course_id, rubric_id, assignment_id)` |
| "Print all Assignment IDs" | `list_assignments` |
| "Print all Rubric IDs" | `list_rubrics` |
| "look at the URL for your course ID" | `list_courses` |

`create_rubric` with `assignment_id` set creates **and** attaches in a single call.

### 3.2 Environment state

| | dev-ai (`dev-boisestateai-v2`) | prod-ai (`boisestateai-v2`) |
|---|---|---|
| Server build | **42 tools**, all rubric tools present | **7 tools**, no rubric tools |
| OAuth provider | `canvas-faculty`, 8 scopes | `canvas-faculty`, 7 scopes |
| Rubric scopes | **absent** | **absent** |
| Canvas instance | `boisestatecanvas.test.instructure.com` | `boisestatecanvas.instructure.com` |
| Tool record | `TOOL#canvas_faculty`, `enabledByDefault=false`, `isPublic=false` | same, `isPublic=true` |
| Cached tool snapshot | 7 tools (stale) | 7 tools (stale) |
| RBAC | `faculty` grants `canvas_faculty` (bare) | `faculty`, `staff`, `student` grant it (bare) |

Verify the live tool surface with:

```bash
curl -sS -X POST "$LAMBDA_URL/mcp" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H 'Authorization: Bearer x' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}'
```

then repeat with `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`. `tools/list` does not call
Canvas, so any bearer value works.

### 3.3 Platform primitives (confirmed against code)

- **All four binding kinds resolve at runtime** — `knowledge_base`, `tool`, `skill`,
  `memory_space` (`inference_api/chat/agent_binding_resolver.py`). The
  *"`tool` and `skill` are accepted and stored but inert until Phase 2/3"* comment in
  `apis/shared/assistants/models.py` is **stale**; ignore it.
- **Tool bindings take bare catalog ids only.** `apis/shared/rbac/service.py` `can_access_tool`:
  *"callers pass a bare catalog id … never a scoped `base::tool` id."* Binding `canvas_faculty`
  therefore brings **all 42 tools** (~11.7k tokens of definitions). Trimming to the 5 rubric
  tools would require accepting scoped refs in `binding.ref` — a platform change, out of scope.
- **Tool bindings replace** the request's `enabled_tools`; a bound tool the invoker cannot access
  raises `AgentBindingBlockedError` and blocks the turn with a message (no silent drop).
- **Skills bind no tools.** `ChatAgent`: *"Skills are pure knowledge bundles … the tool universe
  comes solely from the Agent's bindings and RBAC-gated `enabled_tools`."* `allowed_tools` on a
  skill record is frontmatter passthrough, not a grant.
- **Skills are progressively disclosed** — an `<available_skills>` catalog plus a `skills`
  activation tool; the body loads on activation, `resources` load on demand via `read_skill_file`.
- Agents also carry `starters`, `emoji`, `tagline`, `description`, `visibility`, and a
  marketplace `listing`.

---

## 4. Blockers — these must ship before the agent is buildable

### 4.1 Rating `long_description` is silently dropped — HARD BLOCKER

`create_rubric`'s ratings loop sends only two fields:

```python
data.append((f"{rprefix}[description]", rating.get("description") or ""))
data.append((f"{rprefix}[points]", str(rating.get("points", 0))))
```

Canvas's API supports `rubric[criteria][x][ratings][y][long_description]`, and **that is where a
descriptor lives**. The descriptors are this agent's entire product.

**Validated in dev 2026-09-10 — the failure mode is worse than "descriptors go missing."** Asked
for three named levels (Exemplary / Proficient / Developing) plus a descriptor in all six cells,
the model had nowhere to put a descriptor, so it **overloaded the rating `description` field with
the descriptor sentence and dropped the level names entirely**. `get_rubric` confirms what Canvas
stored: ratings carry no `long_description` at all, criterion `long_description` is `""`, and the
rating name for a 4-point level reads *"Code is clean, efficient, and follows best practices with
no redundancy or inefficiency."* The rubric renders with paragraphs where the level labels belong
and no level labels anywhere.

This is a direct consequence of the unconstrained `criteria` schema (§4.7): the docstring offers
ratings only `{description, points}`, so a descriptor has exactly one place to go and it is the
wrong one. Fixing the schema and the passthrough together is what makes this correct — either
alone still produces a wrong rubric.

Fix: pass `long_description` through on ratings. One line. **Nothing else in this spec matters
until this lands** — §7.2's field-mapping skill is inert without it.

### 4.2 No `update_rubric` / `delete_rubric`

The server exposes `list_rubrics`, `get_rubric`, `create_rubric`, `associate_rubric`,
`grade_with_rubric` — no update, no delete. The conversation can iterate freely right up to the
write and then cannot iterate at all; a correction means the instructor opens Canvas. For
something billed as a guided experience this is the most damaging gap after §4.1.

Fix: add `update_rubric` and `delete_rubric`. Note Canvas restricts editing rubrics already used
for grading — surface that as a clean tool error rather than a raw 401/403.

### 4.3 Prod is unusable

Prod runs a 7-tool build **and** lacks all four rubric scopes:

```
url:GET|/api/v1/courses/:course_id/rubrics
url:GET|/api/v1/courses/:course_id/rubrics/:id
url:POST|/api/v1/courses/:course_id/rubrics
url:POST|/api/v1/courses/:course_id/rubric_associations
```

Fix: redeploy the current `canvas-faculty` image to prod; have a Canvas admin add the four scopes
to the boisestate.ai developer key (required if *Enforce Scopes* is on — the fact that we send a
specific list implies it is); update the provider record in both environments.

Two related notes:
- Prod is also missing `url:POST|/api/v1/conversations`, so `send_conversation` is presumably
  already broken there. Unrelated to rubrics; worth fixing in the same pass.
- **Scope widening DOES force re-consent automatically — verified in dev 2026-09-10.** An
  earlier draft of this spec claimed the opposite; it was wrong. AgentCore's token vault keys on
  the requested scope set, so the first tool call after a scope edit returned
  *"AUTHORIZATION NEEDED — Connect Canvas for Faculty"* rather than a Canvas 401. No
  `scopesHash` drift detection is needed; that field being unread is fine, not a gap.
  **Rollout implication instead:** the prompt fires on the first call to *any* tool on that
  provider, not just one needing the new scope — it hit `list_courses`, whose scope was
  unchanged. So adding the scopes in prod makes every connected faculty member reconnect on
  their next Canvas use. One click, not a broken state, but announce it rather than shipping it
  silently.

### 4.4 Stale catalog snapshot

`mcpConfig.tools` on `TOOL#canvas_faculty` is a 7-tool snapshot in both environments. It does not
gate the runtime — a bare grant loads whatever the live server returns — but it does gate the
admin per-tool picker and the `needsApproval` flag, so `create_rubric` cannot be marked as
requiring user approval. Fix: re-run tool discovery on the catalog record after the deploy.

### 4.5 Attaching a rubric silently rewrites the assignment's points — NEW, found 2026-09-10

`associate_rubric` with `use_for_grading: true` (the default) caused Canvas to change the
assignment's `points_possible` from **5.0 to 8.0** — the rubric's total. Nobody asked for that,
nothing warned, and in a live course it is a grade-affecting change to an assignment the
instructor did not think they were editing.

This is Canvas behaviour, not a bug in our tool, but it must be surfaced. Minimum: the
publishing skill warns before attaching (§7.3) and the agent states the change afterwards.
Better: `associate_rubric` and `create_rubric` return the assignment's before/after
`points_possible` so the agent can report it without a second call. Best: the agent reads
`points_possible` first and, when the rubric total differs, asks the instructor which number
should win before writing.

### 4.6 `get_rubric` cannot confirm an attachment — NEW, found 2026-09-10

After a successful `associate_rubric` (association id returned, attachment real),
`get_rubric` returned `"associations": []`. The attachment *was* live —
`get_assignment_details` on the assignment showed the rubric and the changed point value — so
`get_rubric`'s `associations` array is unreliable as a verification signal despite the tool
requesting `include[]=associations`.

Consequence: the "call `get_rubric` and compare" verification step in §7.3 does not work as
written. Verify attachment via `get_assignment_details` instead; `get_rubric` remains correct
for criteria and ratings. Worth a follow-up to find out why the include is not populating.

### 4.7 Recommended at the same time (not blocking)

- **Give `criteria` a real schema.** The live tool definition is
  `{"type":"array","items":{"type":"object","additionalProperties":true}}` — no properties, no
  required fields. Every bit of structure lives in a prose docstring. A Pydantic model costs
  nothing at runtime (tool-definition text is already in the cached prefix) and is the highest-
  leverage reliability fix on the server.
- **Default criterion `points`** to `max(rating points)` instead of raising `ToolError`. The
  notebook never sends criterion points and Canvas derives them; our tool hard-requires them.
  Canvas treats criterion points as authoritative for the rubric total, so a bad inference
  silently produces a rubric whose total disagrees with its own ratings.
- **Decide the no-assignment association case.** The notebook always sends
  `association_type: 'Course'`; `create_rubric` sends a `rubric_association` block *only* when
  `assignment_id` is given. Verify in a sandbox whether a rubric created with no association
  appears under Course → Rubrics. If it does not, "make a rubric, I'll attach it later" produces
  an invisible rubric.

---

## 5. The wizard contract

The agent is *wizard-capable, not wizard-obligated*. The rule is **retrieve, then propose, then
ask** — questions are the last resort, not the interface.

| Slot | Fill it from | Ask? |
|---|---|---|
| Course | `list_courses` — use it outright if they teach exactly one | Only to disambiguate |
| Assignment | `list_assignments`, matched on the name the instructor used | Only to disambiguate |
| Task description | **`get_assignment_details`** — the assignment's own description | Almost never |
| Points total | the assignment's `points_possible` | Never |
| Standards / outcomes | **knowledge base** — program and department outcomes | Only if no match |
| Scoring scale | knowledge base house default | **Propose, don't ask** |
| Criteria | derive from task description + outcomes | **Propose, don't ask** |

Target: *"Make a rubric for the Final Project in BIOL 101"* reaches a complete draft rubric with
**zero** questions. Where a question is unavoidable, ask for everything missing in one message —
never one question per turn.

This is the substantive departure from the template-prompt approach that motivated this work.
That prompt treats `{{task description}}`, `{{standards outcomes}}`, `{{scoring scale}}` and
`{{desired criteria}}` as things the instructor types. Three of the four are retrievable. Asking
for them anyway is the difference between a guide and a form.

---

## 6. Agent configuration

| Primitive | Value | Notes |
|---|---|---|
| **name** | Rubric Builder | |
| **tagline** | Build a rubric and publish it to Canvas | ≤80 chars |
| **instructions** | §7.1 | Keep short — cached prefix, every turn |
| **modelConfig** | Sonnet (not the Haiku default) | The descriptors *are* the deliverable |
| **binding: tool** | `canvas_faculty` | Bare id → all 42 tools, ~11.7k tokens |
| **binding: skill** | `rubric_authoring`, `canvas_rubric_publishing` | §7.2, §7.3 |
| **binding: knowledge_base** | Rubric Design Library | §7.4 |
| **binding: memory_space** | *optional*, one max | Instructor house style; see §9 |
| **starters** | see below | |
| **visibility** | `PRIVATE` → `SHARED` for pilot → marketplace `published` | |

**Starters:**
- "Create a rubric for one of my Canvas assignments"
- "Turn an existing rubric into a Canvas rubric"
- "Build a rubric aligned to my program's outcomes"

**Token budget note.** The `canvas_faculty` binding puts ~11.7k tokens of tool definitions in the
cacheable prefix on every turn of every session with this agent. It is deterministic and cached,
so it is a one-time write amortized across the session — acceptable, but it is the single largest
line item in this agent's cost and the reason §7.1 must stay short. The rubric workflow itself
needs only 7 of the 42 tools (~1.8k tokens); capturing that saving requires scoped `binding.ref`
support, deliberately deferred.

---

## 7. Draft artifacts

### 7.1 System instructions

```
You are a rubric design partner for Boise State University instructors. You help
faculty create evaluation rubrics and publish them directly into their Canvas courses.

## How you work

Reach a complete draft rubric with as few questions as possible. Retrieve what you
can. Propose what you can infer. Ask only where you are genuinely blocked.

Before asking the instructor anything, try to fill these slots:

| Slot | Fill it from |
|---|---|
| Course | `list_courses`. If they teach exactly one, use it. |
| Assignment | `list_assignments`, matched on the name they used. |
| Task description | `get_assignment_details` — use the assignment's own description. |
| Points total | The assignment's `points_possible`. |
| Standards / outcomes | Search your knowledge base for the program's or department's outcomes. |
| Scoring scale | Use the default scale in your knowledge base. |
| Criteria | Derive them from the task description and the outcomes. |

Anything the instructor already told you is filled — do not re-ask it or re-derive it.
Ask only for slots you could not fill, and ask for all of them in a single message.
Two questions is a lot. Zero is the goal.

## Producing the rubric

Activate the `rubric_authoring` skill before you draft. It carries the quality
standards this work is judged on.

Always show the complete rubric as a markdown table before writing anything to
Canvas: scoring levels as column headings with their points, criteria as rows, and
a descriptor in every cell. Say which outcomes it aligns to and what the points
total is.

## Publishing to Canvas

Never call `create_rubric` or `associate_rubric` until the instructor has seen the
table and approved it. "Looks good", "publish it", "yes" are approval. Silence,
a follow-up question, or an edit request are not.

Activate the `canvas_rubric_publishing` skill before your first write. It carries
the Canvas field mapping and the failure modes — following it is what makes the
published rubric match the table you showed.

After publishing, name the rubric, say which assignment it is attached to, and give
them the Canvas link.

## Scope

You create, revise, and publish rubrics. You do not grade student work, message
students, or create or edit assignments, pages, modules, or announcements — even
though you can see tools for those. If asked, say plainly that you build rubrics and
point them at the right place in Canvas.
```

### 7.2 Skill: `rubric_authoring`

The pedagogy. Adapted from the instructor-authored rubric-generator prompt already in use, with
the quality rules that prompt implies but does not state.

```
# Rubric authoring

Write as an experienced instructor in the discipline at hand. Use student-friendly
language — the rubric is read by students before they start work, not only by the
grader after.

Every rubric has three parts: a scoring scale, criteria, and descriptors.

## Scoring scale

Three to five levels, each with a name and a point value. Prefer the four-level
scale in the knowledge base unless the instructor or the department specifies
otherwise. Level names describe attainment, not praise.

## Criteria

Three to six. More than six and students stop reading; fewer than three and the
rubric cannot discriminate. Each criterion names something observable in the
student's work and maps to at least one stated outcome. Never grade effort,
compliance, or formatting as a criterion unless the outcomes actually call for it.

Criterion points sum to the assignment's total.

## Descriptors — this is the part that matters

One descriptor per (criterion, level) cell. No blanks.

- Describe what the work *does*, not how good it is. "Cites five or more
  peer-reviewed sources published within the last ten years" — not "Uses good
  sources."
- Keep a row parallel. The same dimension varies across the levels; only the
  degree changes. If two cells differ only by an adverb, the row is not finished.
- Do not write the bottom level as pure negation. "Cites fewer than three sources,
  or relies primarily on non-scholarly sources" — not "Does not use good sources."
- Be specific to the outcomes you were given. A descriptor that would fit any
  assignment in any discipline is not doing any work.
- Make the top level attainable and the bottom level survivable. Neither should
  describe a student who does not exist.

## Alignment

State which outcome each criterion serves. If an outcome the instructor gave you
has no criterion, say so — that is a gap worth naming, not something to paper over.

## Common failures to avoid

- A quality ladder (Excellent / Good / Fair / Poor) with no substance underneath
- Descriptors that differ only by an adverb or a number with no referent
- Criteria that grade compliance rather than learning
- Points that do not sum to the assignment total
- More than six criteria
```

### 7.3 Skill: `canvas_rubric_publishing`

The mechanical contract. Separate from §7.2 because it changes for different reasons, and because
an instructor who only wants a rubric *document* never needs it.

```
# Publishing a rubric to Canvas

## Field mapping — get this right or the rubric arrives gutted

| Rubric concept | create_rubric field |
|---|---|
| Scoring level name ("Proficient") | `criteria[].ratings[].description` |
| Level points | `criteria[].ratings[].points` |
| Criterion name | `criteria[].description` |
| Criterion explanation | `criteria[].long_description` |
| **Descriptor (the cell text)** | **`criteria[].ratings[].long_description`** |
| Criterion maximum | `criteria[].points` |

The descriptor is the rubric's whole value, and it goes in the *rating's*
`long_description` — not the rating's `description`, which holds only the level
name. Getting this wrong produces a rubric that looks structurally correct in
Canvas and carries none of the content.

## Criterion points

`create_rubric` requires `points` on every criterion. Set it to the highest rating's
points for that criterion. Canvas treats it as authoritative for the rubric total,
so if it disagrees with the ratings the displayed total will be wrong.

## Attaching to an assignment

`create_rubric` with `assignment_id` creates and attaches in one call — prefer it
over `create_rubric` followed by `associate_rubric`. Use `associate_rubric` only to
attach a rubric that already exists.

`use_for_grading: true` (the default) makes rubric scores post to the gradebook.
Leave it on unless the instructor says the rubric is for feedback only.

## The graded-discussion trap

A graded discussion has both a discussion id and an assignment id, and only the
assignment id can carry a rubric association. `list_discussion_topics` returns the
discussion id. **Only ever use ids from `list_assignments`.** Attaching to a
discussion id fails or attaches to the wrong object.

## Before writing

Call `list_rubrics` first. If a rubric with a similar name already exists, show it
to the instructor and ask whether to replace, attach the existing one, or create a
second — do not silently create a duplicate. `create_rubric` is not idempotent and
has no dry-run, so never retry it blind after an error; check with `list_rubrics`.

## Attaching changes the assignment's point value

Canvas sets the assignment's `points_possible` to the rubric's total when you attach
with `use_for_grading`. Call `get_assignment_details` BEFORE attaching. If the
assignment's points and the rubric's total differ, say so and ask which should win —
do not silently re-point an assignment students may already have seen.

## After writing

Verify with `get_assignment_details`, not `get_rubric`. `get_rubric` returns an empty
`associations` array even for a live attachment, so it cannot confirm the rubric is
attached; it is still correct for criteria and ratings.

Compare what came back against the table you showed the instructor. If anything is
missing — especially descriptors — say so rather than reporting success.
```

### 7.4 Knowledge base: Rubric Design Library

This is what lets the agent fill the standards/outcomes slot without asking, and it is the single
highest-value non-code item in this spec. Suggested contents:

1. **Program and department learning outcomes** — the biggest win. Lets the agent propose
   alignment instead of asking `{{standards outcomes}}`.
2. **Accreditation frameworks** relevant to BSU programs (ABET, AACSB, CAEP, etc.).
3. **University rubric guidance** from the Center for Teaching and Learning — including the house
   default scoring scale the system prompt refers to.
4. **Exemplar rubrics** from strong courses, spanning disciplines and assignment genres (lab
   report, studio critique, research paper, presentation, code project, clinical performance).
5. **The Canvas rubric CSV format**, for the import/export path.

Without (1) and (3) the agent must ask for outcomes and scale on every rubric, which collapses
§5's zero-question target. Build the KB before piloting.

**Backend note:** as of #1027 (`MANAGED_KB_NEW_DEFAULT`), a newly finalized agent is *born
managed* — its knowledge base is provisioned as a Bedrock Managed KB on first upload rather than
the legacy S3 vector index. Confirm the flag's state in the target environment before building
the library, and see `bedrock-managed-kb-evaluation.md` for the embedding-immutability and
score-inversion gotchas that come with it.

---

## 8. Phasing

1. **Phase 0 — Unblock the server.** §4.1 (rating `long_description`), §4.2 (`update_rubric` /
   `delete_rubric`), §4.7 (`criteria` schema, criterion-points default). One PR in `mcp-servers`.
   Deploy to dev.
2. **Phase 1 — Unblock dev config.** Add the four rubric scopes to the dev provider record and
   the Canvas test developer key. Re-run tool discovery on `TOOL#canvas_faculty` (§4.4). Verify
   `create_rubric` end-to-end against a sandbox course, confirming descriptors survive.
3. **Phase 2 — Build the KB.** §7.4. Start with the CTL guidance and one program's outcomes;
   breadth can follow.
4. **Phase 3 — Build the Agent in dev.** §6 configuration, §7.1–7.3 artifacts. Dev already runs
   the 42-tool server, so this is testable as soon as Phase 1 lands.
5. **Phase 4 — Faculty pilot.** `SHARED` visibility with a handful of instructors. The thing to
   watch is §5: count the questions asked per rubric. If it is consistently more than one, the KB
   is thin or the slot-filling instructions are not being followed.
6. **Phase 5 — Prod.** §4.3 — redeploy the server, add prod scopes, resolve the re-consent
   question. Then publish to the marketplace.

Phases 0 and 1 are prerequisites for everything. Phase 2 can run in parallel with 0/1.

---

## 9. Open questions

- ~~Scope re-consent~~ — **settled 2026-09-10**, see §4.3. Automatic; no code needed. The
  remaining question is comms, not engineering: when prod scopes change, every connected faculty
  member gets a reconnect prompt on their next Canvas use.
- **Memory space.** Binding one would let an instructor's house style (preferred scale, tone,
  standing outcomes) persist so the second rubric asks less than the first. v1 supports one Memory
  Space per Agent. Worth a phase-4 decision once we see whether faculty repeat themselves.
- **Prod `student` role grants `canvas_faculty`.** Harmless today (7 read-ish tools). Once the
  42-tool build lands, students see `create_assignment`, `grade_submission`, `create_rubric` in
  their picker. Canvas 403s them on their own role so it is not privilege escalation, but it is a
  confusing catalog entry that will generate support tickets. Clean up before Phase 5.
- **Rubric preview as an MCP App.** A rendered rubric grid would be a far better confirmation step
  than a markdown table, and the natural place to put the approve/publish control. The
  `canvas-faculty` server serves no UI resources today. Post-v1.
- **Does a rubric created with no association appear in Course → Rubrics?** (§4.7.) Needs a
  sandbox test; determines whether "I'll attach it later" is a supported path.

---

## 10. Dev validation log — 2026-09-10

Run against dev (`boisestatecanvas.test.instructure.com`), course 50994 "Faculty Demo: Intro to
MCP", as `system_admin`, Haiku 4.5, `canvas_faculty` enabled in the tool picker.

| Step | Result |
|---|---|
| Canvas developer key: 4 rubric scopes added | done by admin |
| Provider record `canvas-faculty`: 8 → 12 scopes | persisted; no AgentCore re-registration, no client-secret re-entry |
| First Canvas tool call after the scope edit | **consent re-prompt** (§4.3) — fired on `list_courses`, not a rubric tool |
| Reconnect; consent screen | listed the rubric scopes |
| `list_courses` | course 50994 returned |
| `list_rubrics` | `GET .../rubrics` scope works — course had no rubrics |
| `create_rubric` | `POST .../rubrics` scope works — rubric **256107**, 2 criteria, 8 points |
| `list_assignments` | assignment **1756044** "Syllabus Acknowledgment", 5.0 points |
| `associate_rubric` | `POST .../rubric_associations` scope works — association **519900** |
| `get_rubric` | ratings have **no** `long_description`; descriptors sit in `description`; level names lost (§4.1) |
| `get_rubric` associations | **`[]`** despite a live attachment (§4.6) |
| `get_assignment_details` | rubric attached; `points_possible` now **8.0**, was 5.0 (§4.5) |

All four rubric scopes are validated end to end. The auth and transport path is proven; what
remains blocking is content fidelity (§4.1) and the two behaviours found here (§4.5, §4.6).

Turn cost ran $0.033–$0.077 with ~23.8k–27.4k context tokens, consistent with §6's estimate
that `canvas_faculty`'s ~11.7k of tool definitions dominates the prefix.

---

## 11. Reference

- Origin notebook: `manage_rubrics.py` (Colab, shared read-only) — the workflow this replaces.
- MCP server: `mcp-servers/packages/canvas-faculty/app.py`, `README.md` (carries the full
  Canvas OAuth scope list and the 401-scope-vs-401-token diagnosis table).
- Binding resolution: `backend/src/apis/inference_api/chat/agent_binding_resolver.py`
- Binding validation: `backend/src/apis/app_api/agent_designer/services/binding_validation.py`
- Agent model: `backend/src/apis/shared/assistants/models.py`
- Skills runtime: `backend/src/agents/main_agent/skills/strands_mapping.py`
- Related specs: `agent-designer.md`, `agent-marketplace.md`, `google-tasks-todo.md` (Canvas
  OAuth provider precedent), `assistant-kb-sync.md`
