# Spec: Canvas Rubric Agent

**Status:** Built and smoke-tested in dev 2026-09-10 (§10, §10a). All server fixes merged (mcp-servers#38, #39, #40). **All seven §8.1 acceptance criteria pass** (§10a). Remaining: institutional KB content (§10a), then prod cutover (§8.2).
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
| **Publish gate** | **Platform approval interrupt** on `create_rubric`, *plus* a readable table in chat | Verified in dev 2026-09-10. Two layers on purpose: the table is readable but is the model's *claim*; the approval card renders the exact `tool_input` that will be sent, so it is the ground truth. See §4.4 and §10. |

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
| RBAC | `faculty` grants `canvas_faculty` (bare); `isPublic: false` | `faculty`, `staff`, `student` grant it (bare) — **and `isPublic: true`, which grants it to every authenticated user regardless of role** (§9) |

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

### 4.1 Rating `long_description` is silently dropped — FIX BUILT, mcp-servers#38

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

### 4.2 No `update_rubric` / `delete_rubric` — FIX BUILT, mcp-servers#38

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

**mcp-servers#38 adds two more scopes** — `url:PUT|/api/v1/courses/:course_id/rubrics/:id` and
`url:DELETE|/api/v1/courses/:course_id/rubrics/:id` — for `update_rubric` / `delete_rubric`.
**Six scopes total now, not four**, and dev needs the two new ones as well before those tools
work there.

Fix: redeploy the current `canvas-faculty` image to prod; have a Canvas admin add the six scopes
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

### 4.4 Stale catalog snapshot — RESOLVED in dev 2026-09-10

`mcpConfig.tools` on `TOOL#canvas_faculty` was a 7-tool snapshot, which gated the admin per-tool
picker and the `needsApproval` flag. **Fixed in dev** with *Discover from server* on
Admin → Tools → Canvas for Faculty: the catalog now caches all 42 tools, and existing approval
flags survived the rediscovery (`send_conversation` kept its own).

`create_rubric` is now flagged `needsApproval: true` in dev, and the interrupt was verified in
both directions:

- **Approve** → `MCPExternalApprovalHook` raises the interrupt, the SPA renders
  *"APPROVAL NEEDED — Approve `create_rubric` to let the assistant continue"* with a
  **View arguments** expander showing the pretty-printed `tool_input`, and the call proceeds.
- **Decline** → the tool result becomes *"User declined to approve the 'create_rubric' tool
  call; the agent should not invoke it."* and nothing is written to Canvas.

**Still to do in prod:** run the same discovery and set the same flag after the prod deploy
(§4.3).

**Decided 2026-09-10: `associate_rubric` is gated too.** Dev now flags both
(`gated: send_conversation, create_rubric, associate_rubric`). The extra prompt on the
less-common path is worth it because `associate_rubric` is the call that re-points the
assignment (§4.5) — the one side effect in this feature that touches student-visible grades.

### 4.5 Attaching a rubric silently rewrites the assignment's points — MITIGATED, mcp-servers#38

`associate_rubric` with `use_for_grading: true` (the default) caused Canvas to change the
assignment's `points_possible` from **5.0 to 8.0** — the rubric's total. Nobody asked for that,
nothing warned, and in a live course it is a grade-affecting change to an assignment the
instructor did not think they were editing.

This is Canvas behaviour, not a bug in our tool, but it must be surfaced.

**The approval gate does not cover this on its own.** The approval card renders `tool_input` —
for `associate_rubric` that is `course_id`, `rubric_id`, `assignment_id`, `use_for_grading`.
Nothing in those four values tells the instructor that approving will change the assignment
from 5 points to 8. **The card shows the inputs, not the consequences.** So the gate is
necessary but not sufficient: the agent has to state the point change in the message *before*
the prompt, or the instructor approves a payload whose effect is invisible.

Minimum: the publishing skill warns before attaching (§7.3) and the agent states the change
afterwards.
Better: `associate_rubric` and `create_rubric` return the assignment's before/after
`points_possible` so the agent can report it without a second call. Best: the agent reads
`points_possible` first and, when the rubric total differs, asks the instructor which number
should win before writing.

### 4.6 `get_rubric` cannot confirm an attachment — MITIGATED, mcp-servers#38

After a successful `associate_rubric` (association id returned, attachment real),
`get_rubric` returned `"associations": []`. The attachment *was* live —
`get_assignment_details` on the assignment showed the rubric and the changed point value — so
`get_rubric`'s `associations` array is unreliable as a verification signal despite the tool
requesting `include[]=associations`.

Consequence: the "call `get_rubric` and compare" verification step in §7.3 does not work as
written. Verify attachment via `get_assignment_details` instead; `get_rubric` remains correct
for criteria and ratings. Worth a follow-up to find out why the include is not populating.

### 4.7 Recommended at the same time — DONE in mcp-servers#38

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

**Token budget note.** After mcp-servers#38 this is ~13.2k (44 tools + the structured `criteria` schema). The `canvas_faculty` binding puts ~13.2k tokens of tool definitions in the
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

Always show the complete rubric as a **markdown table written directly in your
reply** before writing anything to Canvas: scoring levels as column headings with
their points, criteria as rows, and a descriptor in every cell. Say which outcomes
it aligns to and what the points total is.

Do not use a charting or visualization tool to render the rubric. A rubric is text
in a grid, not a data visualization.

## Publishing to Canvas

Never call `create_rubric` or `associate_rubric` until the instructor has seen the
table and approved it. "Looks good", "publish it", "yes" are approval. Silence,
a follow-up question, or an edit request are not.

Activate the `canvas_rubric_publishing` skill before your first write. It carries
the Canvas field mapping and the failure modes — following it is what makes the
published rubric match the table you showed.

Creating a rubric requires the instructor to approve the tool call — they will see
an approval prompt showing the exact data you are about to send. This is expected,
not an error. If they decline, treat it as a considered editorial decision: ask what
they want changed and revise the draft. Never describe a decline as a permissions
problem, a Canvas restriction, or something to retry — they meant it.

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

## The approval prompt

Creating or attaching a rubric pauses for the instructor's approval. The card they
see lists the raw arguments, not the effects — so anything consequential must be in
your message BEFORE the prompt, in plain language. Above all: if attaching will
change the assignment's point value, say both numbers first.

A decline means they want something different. Ask what to change. Never retry the
same call and never tell them it is a permissions problem.

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

### 8.1 Acceptance criteria

The agent is done when a faculty member who has never used it can do this unaided:

1. **Zero-question path.** "Make a rubric for the Final Project in BIOL 101" produces a complete
   draft rubric — criteria, levels, a descriptor in every cell, aligned to named outcomes — with
   **no clarifying questions**. This is the headline criterion; if it needs questions, either the
   KB is thin or the slot-filling instructions are not landing (§5).
2. **Questions are batched.** Where a question *is* unavoidable, everything missing is asked in
   one message, not one question per turn.
3. **The table matches the payload.** The markdown table the agent shows and the `tool_input` on
   the approval card describe the same rubric. A divergence is a correctness bug, not a cosmetic
   one.
4. **Descriptors survive the round trip.** `get_rubric` after publishing returns a
   `long_description` on every rating, matching the table cell. This fails today (§4.1) and is
   the single check that proves the blocker fixed.
5. **Point changes are announced before the gate.** If attaching will re-point the assignment,
   the agent says so *before* the approval prompt, with both numbers (§4.5).
6. **Decline is respected.** Declining the gate produces a revision conversation, not a retry or
   a permissions diagnosis (§10).
7. **Fences hold.** Asked to grade, message students, or edit an assignment, the agent declines
   and redirects — even though it can see those tools.

### 8.2 Prod cutover checklist

Ordered; each step has a different owner, which is why it is worth writing down.

| # | Step | Owner |
|---|---|---|
| 1 | Merge and deploy the `mcp-servers` fix (§4.1, §4.2, §4.7) | eng |
| 2 | Redeploy `canvas-faculty` to prod; confirm `tools/list` returns **44** (42 + `update_rubric` + `delete_rubric`) | eng |
| 3 | Add the **6** rubric scopes (4 original + PUT/DELETE from mcp-servers#38) **and** `url:POST|/api/v1/conversations` to the **production** Canvas developer key | Canvas admin |
| 4 | Add the same scopes to the prod `canvas-faculty` provider record | connectors admin |
| 5 | **Announce the reconnect** before step 4 lands — every connected faculty member gets a consent prompt on their next Canvas use, including for tools whose scopes did not change (§4.3) | comms |
| 6 | Admin → Tools → Canvas for Faculty → *Discover from server* (refreshes 7 → 44) | tools admin |
| 7 | Flag `create_rubric`, `associate_rubric` **and `delete_rubric`** as **Needs approval**; save | tools admin |
| 8 | **Decide who should hold the 44-tool version** — see §9. Step 6 widens it from 7 tools to 44 for *every authenticated user*, because `canvas_faculty` is `isPublic: true` (a role grant alone does not gate it). At minimum flag the destructive tools `needsApproval` | tools admin + RBAC admin |
| 9 | Build the KB (§7.4) and the Agent (§6) | eng |
| 10 | Smoke-test §8.1 criteria 3–7 against a sandbox course before publishing the listing | eng |

Steps 3 and 4 must not be separated by long — between them, rubric tools 401 with a message that
tells the user a Canvas admin must act, which will already be done.

### 8.3 Residual risk: the prompt fence is not a control

The Agent binds `canvas_faculty` as a bare id, so it holds all 42 tools including
`grade_submission`, `bulk_grade_submissions`, `create_assignment` and `create_page`. Two of the
42 are gated by approval; the rest are held back **only by the system prompt** (§7.1 Scope).
That is a real fence for ordinary use and no fence at all against a determined prompt.

The structural fix is scoped tool bindings (`binding.ref` accepting `canvas_faculty::create_rubric`),
which `rbac/service.py` explicitly rejects today (§3.3). Until then, the mitigations available are:
flag the genuinely destructive tools `needsApproval` as well, so the blast radius of a prompt
that gets past the fence is still one click wide; and keep the Agent's visibility `SHARED`
during the pilot so the population is known. Worth an explicit decision before the marketplace
listing goes public.

## 9. Open questions

- ~~Scope re-consent~~ — **settled 2026-09-10**, see §4.3. Automatic; no code needed. The
  remaining question is comms, not engineering: when prod scopes change, every connected faculty
  member gets a reconnect prompt on their next Canvas use.
- **Memory space.** Binding one would let an instructor's house style (preferred scale, tone,
  standing outcomes) persist so the second rubric asks less than the first. v1 supports one Memory
  Space per Agent. Worth a phase-4 decision once we see whether faculty repeat themselves.
- **Who sees the 44-tool `canvas_faculty` in prod.** *An earlier draft of this spec said to "fix
  the prod `student` role grant." That advice was wrong twice over, and the correction matters
  because it changes the action.*

  **First: dropping the role grant would change nothing.** `canvas_faculty` is
  **`isPublic: true`** in prod, and `rbac/service.py` `_tool_grant_set` unions every public tool
  into each user's grant set (`granted | set(await get_public_tool_ids())`). There are two
  independent grants. Removing one leaves the other, and every authenticated user keeps the tool.

  **Second: student access is probably deliberate, not an oversight.** It is one of 18 public
  tools in prod and the *only* Canvas tool there — `student_myboisestate` is the portal, not
  Canvas. With today's 7 read-ish tools, a student connecting Canvas gets "what are my courses
  and assignments", scoped by Canvas to their own enrollments. Someone chose that, twice.

  **The real issue is not who holds the tool — it is what the tool becomes.** Step 6 of §8.2
  takes that same population from 7 tools to 44, putting `grade_submission`,
  `bulk_grade_submissions`, `create_assignment` and `delete_rubric` in their picker. This is
  **not** privilege escalation: Canvas enforces per-enrollment and a student's token 403s on
  teacher actions. Three costs remain, in order:

  1. **Canvas permissions are per-enrollment, not per-person.** Someone with the `student` app
     role who also TAs a lab holds teacher rights *in that course*, so `grade_submission` and
     `delete_rubric` genuinely work for them there. Narrow, but it is grading.
  2. A student seeing "grade submissions" in their own tool list generates support tickets.
  3. ~13.2k tokens of tool definitions in the prefix for users who can use a fraction of them.

  **Options:**

  - **A — leave it.** Accept that TAs can grade through chat. May even be desirable.
  - **B — set `isPublic: false` *and* drop the `student` grant.** Both, or nothing changes.
    Restricts to faculty/staff/admin but breaks the student "what are my assignments" use.
  - **C — split into two catalog records** against the same server: a read-only student one and
    the full faculty one. Preserves both uses. ⚠️ **Verify this actually filters at runtime
    before relying on it** — a bare-id grant loads whatever the live server returns, and a second
    record with a narrower cached `tools` list may restrict only the picker, not the turn.
  - **D — flag the destructive tools `needsApproval`** so even a TA gets a prompt before a grade
    changes. Cheap, certain, and stacks with any of the above.

  Recommendation: **D regardless** — one checkbox per tool, and it closes the grading path. Then
  choose between A and C on whether students should keep Canvas access.
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

### Approval gate (same session)

| Step | Result |
|---|---|
| Admin → Tools → Canvas for Faculty → *Discover from server* | catalog refreshed 7 → **42** tools; existing `needsApproval` flags preserved |
| `create_rubric` → **Needs approval** ✓, saved | persisted `needsApproval: true` |
| "show me the rubric as a table first, then create it" | rendered a **markdown table**, then called `create_rubric` |
| Approval interrupt | *"APPROVAL NEEDED — Approve `create_rubric` to let the assistant continue"*, with **View arguments** showing the full pretty-printed `tool_input` |
| Approve | rubric **256108** created |
| Decline | *"User declined to approve the 'create_rubric' tool call; the agent should not invoke it."* — nothing written |
| `associate_rubric` → **Needs approval** ✓, saved | dev now gates `create_rubric` **and** `associate_rubric` (plus the pre-existing `send_conversation`) |

Two behaviours worth designing around, both folded into §7.1:

- Asked to "show the rubric as a table", the model first reached for a **charting tool** and
  drew a bar chart of the point values before producing the markdown table. The instruction has
  to say *markdown table written in your reply*, and say not to visualize.
- On decline, the model guessed at the cause — *"This may be a safety check or approval gate in
  your Canvas environment... do you need to verify permissions first?"* A decline is an
  editorial decision by the instructor, not a permissions failure, and the agent must treat it
  that way or it will push users toward "fixing" a gate that is working.

All four rubric scopes are validated end to end, and the approval gate works in both directions.
The auth, transport and consent paths are proven; what remains blocking is content fidelity
(§4.1) and the two Canvas behaviours found here (§4.5, §4.6).

### Round-trip verification — 2026-09-10, after mcp-servers#38 and #39

**§8.1 criterion 4 is proven.** A rubric created with descriptors and **no assignment**, read
straight back with `get_rubric`:

```json
{"id": "_1326", "description": "Exemplary",
 "long_description": "Student supports all claims with specific, relevant evidence…",
 "points": 4.0}
```

Level names in `description`, descriptors in `long_description`, criterion points derived to 4.0
from the highest rating, and the rubric is fetchable despite having no assignment.

Notably, **the schema alone changed the model's behaviour** — twice, with different wording, and
with no prompt guidance. Before #38 the same request packed descriptors into `description` and
lost the level names. That is the argument for typed tool inputs over docstring instructions.

| Check | Result |
|---|---|
| Six rubric scopes on the Canvas test key + provider record | ✅ |
| Server 44 tools, structured `$defs` | ✅ |
| Catalog rediscovered to 44, flags preserved | ✅ |
| `create_rubric` / `associate_rubric` / `delete_rubric` gated | ✅ |
| Descriptors survive create → `get_rubric` | ✅ |
| Course-bound rubric readable with no assignment (#39) | ✅ |
| `delete_rubric` on an associated rubric | ✅ (256107, 256110) |
| `delete_rubric` on a pre-#39 orphan | ❌ Canvas **500** — UI only |
| `update_assignment` to restore assignment points | ❌ scope not on the dev provider |

Three things this surfaced, all folded into mcp-servers#40 or below:

- `create_rubric` reported a **course** id under an `assignment_id` key for the Course fallback.
- `delete_rubric` returned an all-null digest, so a success read as a failure.
- **Two gates can stack on one tool call.** A scope change and an approval gate both fired on the
  same `create_rubric`, so the user saw *"Connect Canvas for Faculty"* and *"Approve
  create_rubric"* simultaneously, with nothing indicating which comes first. Harmless for someone
  who knows the system; a faculty member would reasonably guess wrong. Worth sequencing in the
  SPA, and worth a line in §7.1 if not.

**Admin-UI bug blocking §8.2 step 4:** saving the connector form fails with *"Discovery config
can only be updated together with a credential rotation (client_id + client_secret)."* The SPA
sends `oauthDiscoveryUrl` on every save and `admin/oauth/routes.py` treats any non-None value as
a discovery change — so **a scopes-only edit is impossible through the admin UI** for any
provider that has a discovery URL. Worked around with a direct scopes-only `PATCH`
(`X-CSRF-Token` from the `__Host-bff_csrf` cookie). A connectors admin following §8.2 step 4 in
prod will hit this.

**Course 50994 cleanup:** 256107 and 256110 deleted via the API. **256108 and 256109 cannot be
deleted at all.** Every route 500s or 404s — including `DELETE` from a full Canvas *admin* browser
session, and a rescue attempt that POSTs a Course `rubric_association` (both `purpose` values).
They are absent from the Canvas UI's Rubrics page under both Saved and Archived, so they are inert;
only the API index endpoint reveals them. Removing them needs Instructure support (the 500 bodies
carry `error_report_id`s) or the monthly reset of the test instance. **The 500 is not an OAuth
scope problem** — it reproduces for an admin — so do not debug it as one. Assignment 1756044 is
still at 8 points (was 5): restoring it needs
`url:PUT|/api/v1/courses/:course_id/assignments/:id`, which the dev provider does not grant.

---

## 10a. Built in dev — 2026-09-10

**Agent `ast-9149ef191614` "Rubric Builder"**, owned by phil, `PRIVATE`.

| Primitive | Value |
|---|---|
| Model | `us.anthropic.claude-sonnet-5` |
| Tool binding | `canvas_faculty` |
| Skill bindings | `rubric_authoring`, `canvas_rubric_publishing` |
| Knowledge base | 5 documents, 33 chunks, all `complete` |
| Starters | the three from §6 |

Skills created as system skills in the dev catalog. Knowledge base documents:
`rubric-design-guide.md`, `scoring-scales.md`, `canvas-rubric-mechanics.md`,
`canvas-rubric-csv-format.md`, `exemplar-rubrics.md`.

### Smoke test — §8.1 criterion 1 passes

*"Make a rubric for the Syllabus Acknowledgment assignment in my Canvas course."*

`list_courses` → `list_assignments` → `get_assignment_details` → activated
`rubric_authoring` → complete draft table. **Zero questions asked.** Points totalled 8 to match
the assignment. It stopped and asked before publishing, so the confirm gate held.

Two behaviours worth noting because they are the difference between a KB that is read and a KB
that is obeyed:

- It **deviated from the guide with a stated reason** — used 2 criteria rather than the guide's
  3–6, explaining that "stretching it to 3+ criteria would force compliance-flavored rows that
  the design guide says to avoid." That is the guide being reasoned with, not pattern-matched.
- It **named the outcome gap unprompted**: "no course learning outcomes are attached to this
  assignment", then said what it aligned to instead.

Turn cost $0.12 on Sonnet 5, ~28.9k context. Note the binding replacement is visible in the UI —
"Tools 1 enabled" — confirming the Agent's bindings override the user's own tool selection.

### Full §8.1 acceptance pass — 2026-09-10, all seven criteria

Run against the agent itself (Sonnet 5), not plain chat. Nothing was written to Canvas: the one
write attempt was declined on purpose to exercise criterion 6.

| # | Criterion | Result |
|---|---|---|
| 1 | Zero-question path | ✅ complete draft, no questions |
| 2 | Questions batched | ✅ retrieved first, then asked exactly two things in one message |
| 3 | Table matches payload | ✅ `tool_input` matched the table word-for-word |
| 4 | Descriptors survive the round trip | ✅ (§10) |
| 5 | Point changes announced before the gate | ✅ |
| 6 | Decline respected | ✅ |
| 7 | Fences hold | ✅ refused to grade, redirected |

**Criterion 5** is the one that protects grades, and it behaved better than the spec asked. Told
to build a 20-point rubric for a 100-point assignment, it stopped before the gate with:

> ⚠️ Point mismatch to flag: the assignment is currently worth 100 points; you asked for a
> 20-point rubric. Attaching this rubric with grading enabled will re-point the assignment from
> 100 → 20. Let me know if that's intended, or if you'd like me to scale the rubric to 100
> instead.

Both numbers, the consequence, and an alternative — before the approval card, which shows only
the arguments (§4.5).

**Criterion 6** also exceeded the spec. It confirmed the no-op state rather than just accepting
the decline: "No changes were made — the assignment is still worth 100 points and no rubric was
attached", then offered four concrete revision directions. No retry, no permissions diagnosis.

**Criterion 2** produced direct evidence for the KB gap below — the agent named it itself: "I
don't have a program outcomes list in my knowledge base for this course/program, so please paste
them or point me to where they're defined."

Also observed: the agent follows `canvas_rubric_publishing` without being told to — it called
`list_rubrics` before drafting, as the skill's "Before writing" section instructs. Turn costs
ran $0.03–$0.13 on Sonnet 5.

### The gap that remains: institutional content

The knowledge base currently holds **craft** guidance only — rubric design, scoring-scale
conventions, Canvas mechanics, the CSV format, and exemplar rubrics written as phrasing models.
All of it is authored for this agent and none of it is institutional.

**Not present, and deliberately not invented:** Boise State program and department learning
outcomes, the Center for Teaching and Learning's actual rubric guidance and house scoring scale,
and accreditation framework criteria (ABET, AACSB, CAEP, …). Those are real institutional
documents; fabricating plausible substitutes would produce an agent that aligns rubrics to
outcomes nobody adopted.

This is the difference between §5's target and what the agent does today. The smoke test hit zero
questions because that assignment had no outcomes to align to — the agent said so and aligned to
the task's own purpose. **Given a real assignment in a real program, the outcomes slot will not
fill and the agent will have to ask.** Supplying (1) program outcomes and (2) the CTL guidance and
default scale is what closes it, and it is the single highest-value item remaining. It needs no
engineering.

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
