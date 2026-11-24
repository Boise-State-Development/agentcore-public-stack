# Retiring a skill

**Status:** Proposed. Stage 0, Stage 2 and the user-skill path in §8 work with
today's code. Stages 1 and 3 need the §7 change, and none of it is built.
**Scope:** A `SKILL#<id>` row: an **admin-catalog** skill (`ownerId: "system"`,
granted through `grantedSkills`), or a **user-authored** skill (governed by
ownership and invoke-through, never by roles). Users switch either one on in
Customize → Skills. Agents and project harnesses bind them as `kind: "skill"`.
**Refs:** `docs/specs/mcp-server-retirement.md` and `docs/specs/model-retirement.md`
are the sibling runbooks. This one follows their shape (inventory, stop new
adoption, migrate, cut over, remove) and reuses the tools' picker rule and copy
helpers (`shared/utils/retirement.ts`). `docs/specs/skill-version-history.md`
explains why most "replace this skill" requests are an edit, not a retirement (§6).

---

## The question this answers

> Tools and models have a retirement path. Do skills?

**No, and the gap is not cosmetic.** Skills have a `status` field but no word
for "being retired". The only lever an admin has is the runtime off switch, and
the admin page's **Delete** button pulls it. Disabling a skill (or soft-deleting
it, which is the same write) removes it **at once** from every Agent and project
that binds it. No message is shown, and the Agent answers without the
procedure it was built around.

The tool runbook's Stage 1 needs `deprecated` plus a picker rule. Skills need
the same two things. Skills also need a third thing tools didn't: the turn has
to say when a bound skill is gone, because today a skill that is disabled, or
deleted with its grant left behind, disappears silently from a bound Agent (§2).

---

## 1. The thing that decides the design

Two checks decide whether a bound skill runs. They read different things:

| Check | Reads | Where |
|---|---|---|
| **Access**: may this invoker use the skill through this Agent? | Role grants (`*` expands over catalog rows of *any* status), ownership (`ACTIVE` rows only), invoke-through and the adder clause (**no status check**) | `resolve_invocable_skill_ids` ([`access.py:85`](../../backend/src/apis/shared/skills/access.py#L85)), called by `_resolve_skills` ([`agent_binding_resolver.py:298`](../../backend/src/apis/inference_api/chat/agent_binding_resolver.py#L298)) |
| **Load**: what does the agent build actually mount? | The row's `status`; anything not `ACTIVE` is dropped | `fetch_active_skill_records` ([`strands_mapping.py:230`](../../backend/src/agents/main_agent/skills/strands_mapping.py#L230), the filter at L253) |

A miss in the access check **blocks** the turn (D5, block-with-message), and
on a project harness it degrades with an `agent_notice`:

> This agent uses the skill **web-research**, which isn't available to your
> account. Ask an administrator for access, or use a different agent.

A miss in the load step is **silent**. The access check has already passed, so
nothing raises. The skill is not in `<available_skills>`, and the model
never learns it existed.

So whether a retired skill fails loudly or silently depends on which of the
two checks catches it. Today that depends on *how* it was retired, and for a
hard delete, on whether the invoker's role grants the skill by id or by `*`.

---

## 2. What each lever does today

| Lever | Plain chat (skill switched on) | Agent / published snapshot | Project harness |
|---|---|---|---|
| Admin **Delete** (default), status → `disabled` | gone from the picker and the turn | access passes, load drops: **silent** | silent (no `agent_notice`; the access check passed) |
| Admin sets status `draft` | same as disabled | **silent** | silent |
| Admin hard delete (`?hard=true`), grant left in place | gone | explicit `grantedSkills` holder: access passes, load finds no row: **silent**. `*` holder: id is no longer in the catalog snapshot, so the turn **blocks**. Same Agent, two outcomes | silent or degraded, by the same split |
| Revoke the grant (`/admin/skills/{id}/roles/remove`) | gone | **blocks** (D5). Cannot reach `*` holders | degrades + `agent_notice` |
| Author disables their own skill | gone | invoke-through passes with no status check, load drops: **silent**, for the author too | silent |
| Author deletes their own skill (`DELETE /skills/mine/{id}`) | gone | no row, so invoke-through fails: **blocks**, with the "ask an administrator" copy, which is wrong for an authored skill | degrades |

Row 1 is the one that matters. It matches tools §7a, where `disabled` is a
deliberate runtime off switch that drops even Agent bindings. The difference is
that tools also have `deprecated`, so `disabled` is never the *first* step. For
skills it is the only step, and the Delete button takes it.

Two more facts make Row 1 worse than the tool version:

- **The Designer still offers a disabled or draft catalog skill.**
  `bindable_catalog._list_skills`
  ([`bindable_catalog.py:171`](../../backend/src/apis/app_api/agent_designer/services/bindable_catalog.py#L171))
  lists every accessible id with no status filter. `_validate_skill`
  ([`binding_validation.py:281`](../../backend/src/apis/app_api/agent_designer/services/binding_validation.py#L281))
  checks the same list. So an author can bind a disabled skill today. The save
  succeeds, and the skill never runs. That is a bug in its own right, separate
  from retirement.
- **A pin doesn't protect a binding.** A pinned binding (`config.version`) takes
  its content from the version and its `status` from the live row
  ([`strands_mapping.py:234`](../../backend/src/agents/main_agent/skills/strands_mapping.py#L234)).
  Disabling the live row stops every pinned copy too. That is the right
  behaviour for an off switch, but it means pinning is no way to keep a
  retiring skill working.

The surfaces that *do* already check `ACTIVE` are all display or read paths:
`GET /skills/` (the Customize picker,
[`skills/routes.py:176`](../../backend/src/apis/app_api/skills/routes.py#L176)),
the slash-command list (`agent_detail.py:343`), a project's bound-skill reader
(`projects/skill_content.py:99`, `:119`), the workflow sheet (`workflows/sheet.py:312`)
and checklist steps (`workflow_checklist.py:120`). Each one hides a non-active
skill. None of them tells anyone it is gone.

---

## 3. Why `disabled` cannot be Stage 1

The tool runbook's §7a settled the meaning: `disabled` is "stop it now, for
everyone". That's useful to have, for example to pull a skill whose
instructions are actively harmful. Skills should keep it.

But retirement's first stage has to change **nothing** for anyone who already
has the skill, and `disabled` changes everything for them. Disabling a skill
and calling it "Stage 1" is a Stage 3 cutover with no notice and no error
messages.

`draft` is not an option either. It already means "not yet published" to the
skill form, and it drops at load exactly like `disabled`.

So Stage 1 needs a new status, `deprecated`. It means the same thing it means
for tools: a picker can turn it off but not on, and it loads for whoever
already has it.

---

## 4. Skills are prompt content, so the cutover is a prefix rewrite

A skill reaches the model as an entry in `<available_skills>`, which sits in
the system prompt, ahead of all restored history. That has two consequences:

- **Stage 1 must not change the system prompt.** `status`, `retirementNote` and
  `retiresOn` are display fields. They must never reach the disclosure block.
  An admin write moves the row's `updated_at`, and through
  `freshness.get_skill_updated_at` that moves the agent cache key, so cached
  agents rebuild. A rebuild with byte-identical prompt bytes reads the existing
  Bedrock cache. Prove it after the change: `systemPromptHash` on the session's
  `C#` rows must not move across the deprecation write.
- **Stage 3 is one prefix rewrite per live session that had the skill.** Taking
  a skill out of `<available_skills>` changes the system prompt. That is the
  cutover's unavoidable cost, at the cache-write premium on the session's own
  model. Make it a single status write, not a morning of toggling, so each
  session pays for it once.

---

## 5. Enumerating the blast radius

### a. Roles that grant it (catalog skills only)

```bash
curl -s "$APP_API/api/admin/skills/$SKILL_ID/roles" | jq '.roles[].roleId'
```

⚠️ **Blind to the wildcard**, exactly as for tools. A `*` grant writes
`GSI2PK = "SKILL#*"` ([`seed_bootstrap_data.py:869`](../../backend/scripts/seed_bootstrap_data.py#L869)),
so list the holders separately:

```bash
curl -s "$APP_API/api/admin/roles" | jq -r '.roles[] | select(.grantedSkills | index("*")) | .roleId'
```

### b. Agents and project harnesses that bind it

There is no reverse index from catalog skill to Agent. (`reach.py` exists, but
only for a *user's own* skill.) Scan the assistants table. Project harnesses
live in the same table (`kind: "project"`), and version snapshots share the
Agent's partition, so one scan covers live records, published snapshots and
harnesses:

```bash
aws dynamodb scan --table-name "$ASSISTANTS_TABLE" \
  --projection-expression "PK,SK,ownerId,#n,#k,visibility,listing,bindings" \
  --expression-attribute-names '{"#n":"name","#k":"kind"}' \
  --output json \
| jq --arg s "$SKILL_ID" '
    .Items[]
    | select([.bindings.L[]?.M | select(.kind.S == "skill") | .ref.S
              | select(. == $s)] | length > 0)
    | {pk: .PK.S, sk: .SK.S, name: .name.S, kind: .kind.S, owner: .ownerId.S,
       visibility: .visibility.S,
       publishedVersion: .listing.M.publishedVersion.N,
       pin: ([.bindings.L[].M | select(.kind.S=="skill" and .ref.S==$s)
              | .config.M.version.N] | first)}'
```

A binding's `ref` is always the bare skill id. The pin is in `config.version`,
and only the runtime composes `skill_id@n`, so an equality match is enough.

For a published Agent, the tool runbook's §5d applies unchanged: the author's
fix reaches everyone else only after a resubmit and an approval, or after an
admin takedown.

### c. Everything else holding the id

| Holder | Where | Action |
|---|---|---|
| Admin agent templates | `bindings[]` of kind `skill` (`agent_templates/models.py`, `TemplateBinding`) | **Remove in Stage 1.** A template creates a new binding every time someone starts from it |
| The seeded example skill | `seed_example_skills` ([`seed_bootstrap_data.py:1366`](../../backend/scripts/seed_bootstrap_data.py#L1366)) | Re-grants the default role **on every bootstrap run** (L1385), and re-creates the row, `active`, if it was hard-deleted. Retiring it means removing it from the seeder in the same PR as Stage 4 |
| Workflows and field sets the skill carries | `references/workflow.*.md`, `references/field-set.*.md` in the skill's own bundle | Retire with the skill: they live in its bundle and resolve only through it. Say so in the Stage 1 notice if the skill is a workflow carrier (`kind: "workflow"`) |
| Users' Customize toggles | `UserSkillPreference.skillPreferences`, saved by the skill catalog repository (`save_user_preferences`) | Leave them. A toggle naming a missing or disabled id is inert |
| Script review rows | `SCRIPTS_REVIEW#…` beside the skill | Leave them until Stage 4; they are keyed by script-set hash and do nothing without the skill |

---

## 6. Most "replacements" are edits, not retirements

A model successor is a different id: Sonnet 4.6 becomes Sonnet 5, so
`model-retirement.md` needs a redirect. A skill successor is usually **the same
skill, edited**. Version history records the change, and the pin-update offer
moves the Agents and projects still pinned to the old content
(`skill-version-history.md`). Before opening a retirement ticket, ask whether
the replacement could just be the next version of this id. If it can, there is
nothing to retire.

Retirement is for these cases:

- the capability is going away with no successor;
- it is being split or merged into differently named skills;
- the skill moves between tiers (a user's skill promoted into the catalog
  under a new id).

So this runbook deliberately has **no redirect lever**. `retirementNote` says in
words what replaces the skill. A structured `replacedBy` can be added later
underneath it if a real case needs one. It can't replace free text, because
"split across two skills" doesn't fit in an id.

---

## 7. The change Stages 1 and 3 need

### The rule, stated once

> A `deprecated` skill can be turned **off** but not **on**, and it keeps
> running wherever it already is. A `disabled` skill never runs, and **the turn
> says so** wherever an Agent or project bound it.

### Backend

1. **`SkillStatus.DEPRECATED = "deprecated"`** ([`models.py:51`](../../backend/src/apis/shared/skills/models.py#L51)).
   `str`-Enum, so it serialises as `"deprecated"`, which `isRetiring()`'s
   `!== 'active'` comparison already understands.
2. **One predicate for "does it run": `skill_runs(status)`**, true for `active`
   and `deprecated`, in `apis/shared/skills/models.py`. Every `== ACTIVE` check
   in §2's list that means "does an existing holder get it" switches to it:
   `_is_active_status` in `strands_mapping.py`, `agent_detail.py:343`,
   `skill_content.py:99`/`:119`, `workflows/sheet.py:312`,
   `workflow_checklist.py:120`, and the ownership clause's
   `status=ACTIVE` read (`access.py:48`). Without that last one, an author's
   own deprecated skill blocks their own Agent. An architecture test should fail
   on any new `SkillStatus.ACTIVE` comparison outside the predicate. This file
   is the third time "is it active?" has been answered in six places.
3. **Retirement fields** on `SkillDefinition`: `retirementNote` (≤300 chars) and
   `retiresOn` (`YYYY-MM-DD`, validated on the way in), with the same
   validators as `ToolDefinition`. They are **display only**: nothing reads
   them for access, and they never reach `<available_skills>` (§4). They go
   out on `AdminSkillResponse`, `UserSkillResponse` and the skill
   `BindableItem.meta`, together with `status`.
4. **Close the silent drop on bound skills.** In `_resolve_skills`, a bound
   skill whose row is missing or doesn't run counts as unavailable. On an
   Agent it blocks (D5). On a project harness it degrades with an
   `agent_notice`. Plain chat keeps dropping silently, because the picker
   already hid it and the user switched it on themselves.
   - **Copy.** A retired skill must not say "ask an administrator for access".
     Use *"This agent uses the skill **X**, which has been retired. {retirementNote}"*,
     or *"…which its author removed"* for a user skill with no row.
   - **TTFT.** No new round trip. `_resolve_skills` must read status from the
     per-skill freshness cache that the agent cache key already fills
     (`freshness.get_skill_updated_at` fetches the whole row and keeps only
     `updated_at`; keep `status` beside it). If ordering forces a separate
     read, measure it per the turn-path rule and get it accepted.
     `test_turn_path_io_inventory.py` must stay unchanged.
   - This also fixes §2's explicit-grant versus `*` split on a hard delete.
5. **The palette and validation refuse new adoption.**
   - `bindable_catalog._list_skills` leaves out `draft` and `disabled` skills
     and marks `deprecated` ones through `meta.status`.
   - `_validate_skill` refuses a binding that isn't already on the record when
     its skill is not `active`. A `kept` binding (the existing `kept=True`
     path) still saves, so an author editing an Agent that keeps a deprecated
     skill never gets a 403.
   - The project settings save goes through the same validation.
6. **Admin Delete stops being an unannounced cutover.** The admin list's soft
   delete keeps writing `disabled`. That is still a useful off switch. But the
   button should say what it does ("Disable now — stops it in N agents"), and it
   needs the catalog-skill reach count from the §5b scan, served by an admin
   endpoint (follow-up 1).

### SPA

Reuse `shared/utils/retirement.ts` (`isRetiring`, `formatRetiresOn`,
`retirementDetail`) so that skills, tools and models print the same sentence.

| Surface | Guard |
|---|---|
| Customize → Skills | Switch disabled only while off; a `retiring` badge and the `retirementDetail()` sentence as visible text, never hover-only |
| Agent Designer skill picker | Refuses to select an unselected retiring skill; a bound one stays visible with a badge, plus a section notice naming the retiring skills this Agent still binds |
| Project settings skill dialog (`project-skill-dialog.component`) | Same as the Designer, since it adds bindings to a harness |
| Admin skill form | `Deprecated` in the status options; the two retirement inputs revealed when status leaves `active`, hidden (not disabled) so a draft keeps its values |
| Slash-command menu | Nothing new: it lists what is switched on, and a deprecated skill stays switched on |

### Feature flag

None. This extends an existing status field with a value whose default
behaviour (an `active` skill) is unchanged, and an older SPA reads unknown
statuses as retiring. The prompt is not touched (§4). Per CLAUDE.md's flag
rule, prove it before merge instead. The proof is the `systemPromptHash`
check, a Designer save of an Agent that keeps a deprecated skill, and a blocked
turn on dev that shows the new copy.

---

## 8. The staged sequence (catalog skill)

**The blast line is Stage 3**, the first stage a user can experience as a
failure. **The point of no return is Stage 4**, the first whose state can't be
restored from what is left.

### Stage 0 — Inventory (no user-visible change)

Run all of §5. Export the skill now, because its bundle is the only copy:

```bash
curl -s "$APP_API/api/admin/skills/$SKILL_ID" > retirement-$SKILL_ID-metadata.json
curl -s -o retirement-$SKILL_ID.zip "$APP_API/api/admin/skills/$SKILL_ID/export"
```

Before going on, check §6: is this really a retirement, or the next version of
the skill?

**Rollback:** n/a.

### Stage 1 — Deprecate. Stop new adoption *(needs §7)*

1. Remove the skill's bindings from admin agent templates.
2. Set `status: deprecated`, with `retirementNote` and `retiresOn`. Make it
   **one** admin write (§4).

What does not change: grants, the row, every binding, every pin, every
user's toggle. Every Agent and project that binds the skill keeps running it.

Tell the owners now (§10). **Rollback:** set `status: active`. Instant, and
leaves no trace for users.

### Stage 2 — Migrate

- **Live-record Agents:** the owner removes the binding, or binds the
  successor. Removing a binding always saves.
- **Published Agents:** the owner also resubmits and an admin approves; or an
  admin takes the listing down, which makes the owner's fixed draft live
  (tool runbook, Stage 2).
- **Project harnesses:** an editor changes the project's skills. A successor
  added there is pinned to its content as of that save.
- **Workflow carriers:** move any saved workflows and field sets to the
  successor skill. References are same-skill only (`check_field_set_refs`), so
  this is a copy into the new bundle, not a re-point.

Re-run the §5b scan; don't trust the ticket.

### Stage 3 — Cut over. **The blast line**

Set `status: disabled`. From the next turn (≤ one freshness TTL), with §7:

- plain chat: gone;
- an Agent still binding it: blocks, with the retirement copy;
- a harness: runs without it, with an `agent_notice`.

Without §7, this step is silent for all of them. **Don't run Stage 3 before §7
ships.**

Revoking the grants is optional at this point. The status already denies
everyone, `*` holders included, which revoking cannot do.

**Rollback:** set `status: deprecated`. Errors already shown and the prefix
rewrites already paid (§4) stay as they were.

### Stage 4 — Remove. **The point of no return**

Only once the §5b scan is zero:

1. Remove the id from every role (`POST /api/admin/skills/{id}/roles/remove`).
   Do this *before* the row is deleted, for the same reason as tools: a
   surviving grant plus a missing row is §2's silent state if §7 has not
   shipped.
2. `DELETE /api/admin/skills/{id}?hard=true`. It removes the `METADATA` row and
   the skill's versions.
3. Clean up what the hard delete leaves:

| Leftover | Why | Action |
|---|---|---|
| The live `SKILL.md` and `resources[]` objects in S3 | `SkillCatalogService.delete_skill` calls `delete_versions` only; the user tier's delete purges the bundle, the admin tier's doesn't | Delete the skill's prefix by hand, after confirming the Stage 0 export opens |
| `SCRIPTS_REVIEW#…` / `SCRIPTS_SWITCH` rows | Not cascaded | Delete; inert either way |
| Users' `skillPreferences` keys | Never reconciled | Leave them |
| The seeder entry (example skill only) | §5c | Remove it in the same PR, or the next bootstrap run brings the skill back |

**Rollback:** re-import `retirement-<id>.zip` (`POST /api/admin/skills/import`)
and re-add the grants. Version history doesn't survive. Agents pinned to an
old version need a new pin.

---

## 9. Retiring a user-authored skill

The author is the admin for their own skill. Two parts of the path already
exist: the uses view (`/skills/mine/{id}/uses`, version-history D9) and the
reach-confirmed delete (`DELETE /skills/mine/{id}` answers 409
`skill_reach_unconfirmed` while a shared Agent or project uses it).

With §7, the path is:

1. Set the skill to `deprecated` from the skill form, or with
   `my_skill_update` (which accepts any `SkillStatus`, so it picks the new
   value up with no change).
2. Read the uses view, and fix or tell the people behind each place.
3. Delete with `confirmReach`. Places still binding it then block with *"…which
   its author removed"* (§7.4) instead of today's "ask an administrator".

`reach.py` can't see a project the author has left that still runs a skill
they added (its own docstring says so). The delete confirmation should say
"at least N places".

---

## 10. What to tell people

**Stage 1, to every owner in §5b and every role that grants it:**

> **`<Skill>` is being retired on `<date>`.** Nothing changes today:
> **`<Agent or project>`** uses it and will keep working exactly as it does now.
> From today it can't be added to new agents or projects.
> Before `<date>`, please remove it from `<Agent>` (and resubmit if it is
> published). After `<date>` an agent that still uses it will stop and tell its
> users the skill was retired; a project will keep running without it.
> Replacement: `<retirementNote>`.

**Stage 3, the last notice to anyone still bound:**

> `<Skill>` stops working `<date/time>`. `<N>` agents still use it and will stop
> running, showing: *"This agent uses the skill `<id>`, which has been retired."*

**Stage 4:** internal only.

---

## 11. Follow-ups this runbook wants

1. **Reach for catalog skills.** An admin endpoint that runs the §5b scan once
   and returns the count and the list, so the admin Delete button and Stage 2
   are a button, not a `jq` command. It matches tool runbook follow-up 1, and
   one endpoint could serve both.
2. **The Designer offers disabled and draft catalog skills today** (§2). Item 5
   of §7 fixes it. It is worth fixing on its own, ahead of the rest.
3. **Bound-skill status drift is invisible to admins.** No report answers
   "which Agents bind a non-`active` skill?". The same gap exists for tools
   (follow-up 5 there).
4. **`get_roles_for_skill` is blind to `*`**, as for tools. The admin skill
   page's role count reads "0" for the most widely granted skills.
