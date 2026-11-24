# Skill version history and restore

**Status:** built and validated on dev (2026-11-24, issue #2352). Behind
`SKILL_VERSION_HISTORY_ENABLED` / `features.skillVersionHistory`: on in the
`development` environment and the dev build, off by default and in prod. §11 records
what each PR shipped and where the build differs from this design.
**Extends:** `skills-as-agent-primitive.md`, shared-projects 3.1 (skill pins,
`apis/shared/skills/versions.py`), `skill-bundle-import.md`.
**Pattern from:** `agent-version-snapshots.md` §8 (Agent rollback).

## 1. Why

A skill's owner, or an admin for a catalog skill, cannot see what a skill used to
say, and cannot put it back. Three things make that worse than it sounds:

- **A catalog skill reaches people through role grants, and a grant runs the live
  skill.** An admin edit, or a re-import from GitHub (`skill-bundle-import.md` §6a),
  changes the skill for everyone at once and leaves nothing to go back to.
- **Versions exist, but only where something pinned.** A version is cut when a
  Shared Project adds a skill, when someone clicks Update on a project's Skills
  page, or when an imported skill is bound to an Agent (`pin_imported_skill_bindings`).
  A skill nobody pinned has no history at all, and one that was pinned has history
  only at the moments it was pinned.
- **Nothing lists the versions that do exist.** The only way to read one is
  `GET …/export?version=n`, and nothing says which `n` exist. The audit log can't
  fill the gap: it is content-free by design (`test_skill_pin_by_default` asserts
  skill text never reaches an audit record).

And the other direction is no better. **A change does not reach anything that pinned
the skill, and nothing tells the author so.** A pin is the safety a project or an
imported-skill binding asked for, so that is correct. But the author is not told when
they save, a project shows "Update available" only to someone who opens its Skills
page, and an Agent has no Update at all. An Agent's pin moves only if someone turns the
skill off and on again in the Agent form. An author can reasonably assume their fix is
live everywhere when it is live nowhere that pinned (D9).

Agents solved the history problem in July (`GET /admin/agents/{id}/versions`,
`POST /admin/agents/{id}/rollback`). Skills need the same capability, for both
tiers.

## 2. What exists today

| Piece | Where | Notes |
|---|---|---|
| Version row | `SKILL#{id}` / `VERSION#{n:08d}` in the skill catalog table (`SkillVersion`) | Write-once. Freezes `SNAPSHOT_FIELDS`: display name, description, instructions, compose, advisory tools, frontmatter, files. Never status, owner or visibility. |
| Frozen files | `skill-versions/{skill_id}/{sha256}` (`version_blob_key`) | Content-addressed, so two versions that share a file share one object. |
| Cutting | `pinning.pin_current` | Reuses the latest version when the live `content_hash` matches, otherwise freezes files and allocates `n+1` with a conditional put. |
| Applying | `versions.apply_version` | An overlay on the live row; status and ownership stay live. |
| Reading | `repository.get_latest_skill_version`, `batch_get_skill_versions` | No list method. |
| Export | `GET /skills/mine/{id}/export?version=n`, `GET /admin/skills/{id}/export?version=n` | The only read surface. |
| Cleanup | `delete_skill_versions` on hard delete (both tiers) | Versions never outlive the skill. |
| Updating a project's pin | `POST /projects/{id}/skills/{skill_id}/pin` (`HarnessSettingsService.pin_skill`) | Editor only; records a settings version. The project Skills page shows "Update available". |
| Updating an Agent's pin | none | Only by turning the skill off and on in the Agent form (`skillBindingConfigs`). Nothing shows that a newer version exists. |
| Where a user skill is bound | `reach.lookup_skill_reach` | The owner's Agents and projects, and projects they added it to as an editor, each with its pinned version. Used by the reach 409 and the approval card. |

`pin_status` reports `update_available` by comparing the live `content_hash` with the
pinned version's. Nothing in this spec changes that.

## 3. Decisions

**D1. Capture on every content write, deduplicated by content.** The rule becomes:
*every content the live row ever holds is a version.* Each write that changes a
`SNAPSHOT_FIELDS` field captures the result as a version, and a write that leaves the
content where the latest version already has it captures nothing (the `content_hash`
check `pin_current` already does). This retires "versions are cut when something pins"
(`versions.py` docstring). A pin then almost always reuses the version its save cut.

Rejected alternatives:

- *Cut only on "save-shaped" actions* (form save, import, restore) and not on file
  uploads or the `my_skill_*` authoring tools. Fewer rows, but a run of file edits
  overwrites its own middle states, which is exactly what someone wants back after an
  authoring agent rewrites a reference file twice.
- *Snapshot only before whole-skill replacements* (re-import, restore). Covers the
  scariest write and none of the common ones.

The noise that D1 creates (an authoring session that writes ten files cuts ten
versions) is a display problem. It is solved in the list (§6.2), not in storage.

**D2. Capture before *and* after.** Before a content write, ensure the live content
is already a version; after it, capture the new content. When the previous write's
capture succeeded, the "before" step is one Query and a hash compare. It exists for
the first write after the flag turns on and for a write whose capture failed: neither
may destroy content that history does not hold. For restore and re-import the "before"
step **fails closed** (no checkpoint, no write). For every other write both steps are
best-effort: a failed capture is logged and counted (§9), and the next write's
"before" step repairs it while that content is still live.

**D3. Restore is a write, not a pointer move.** Agents roll back by moving
`publishedVersion`. Skills have no published pointer: the live row *is* what role
grants and unpinned bindings run. So restoring version `n`:

1. checkpoints the live content (D2, fail closed);
2. writes `n`'s `SNAPSHOT_FIELDS` onto the live row, and its files back to the live
   bundle keys;
3. captures the result, which is a **new** version `m` with `n`'s `content_hash`.

History stays linear and append-only, and a restore can be undone by restoring
`m - 1`. The UI must say "Restore v3 (saved as v8)" or similar, never "rolled back to
v3", and its control must not hide when the current version is the highest. Agent
rollback shipped that bug first (`agent-version-snapshots.md` §8).

**D4. A restore moves no pins.** A project pinned to v3 keeps v3, exactly as it does
across an edit. Its Skills page then shows "Update available" whenever the live
content differs from v3, which a restore usually causes. Pinning a project binding to
an *older* version is a separate feature (§12).

**D5. Restore brings back the display name too.** It is a frozen field, and the
version list shows it, so restoring the instructions under today's name would be a
state that never existed. The import rule "the display name stays the user's" is about
pulling content from outside, which a restore is not.

**D6. Versions keep their full import source from now on.** Today a version stores
only `skill_provenance()` (type, owner, repo, ref, commit). A restore of a version
imported from GitHub has to put the source back, or "Check GitHub for changes" and
"Imported from … at abc1234" describe the content that was replaced. New versions
store the whole `SkillImportSource`. Read APIs keep serving provenance only, as the
project settings page does today. A restore of an older version that carries only
provenance leaves the live `import_source` as it is and shows a notice in the result.

**D7. Who sees and restores what.**

| Tier | List and read | Restore |
|---|---|---|
| My Skills | The owner (`require_owned`) | The owner, with the reach confirmation an edit gets (`require_reach_confirmed`, 409 → confirm → `confirmReach`) |
| Admin catalog | `admin.skills`, catalog skills only (`require_catalog_skill`; a user skill is a 404) | Same scope. No reach confirmation, as for an admin edit |

A project editor who pinned a user's skill sees the version their project runs on the
project's Skills page, as today, and not the owner's whole history.

**D8. Retention is unbounded**, as for Agents (`agent-version-snapshots.md` §8):
deleting a version now deletes a way back. Rough size: a version row is the
instructions plus a few hundred bytes, and a file is stored once per distinct content.
A skill edited 500 times with 20 KB of instructions is about 10 MB of rows. Revisit
when a single skill's partition read is slow, not on a row count. Hard delete still
removes all of a skill's versions.

**D9. After a change, offer to update what pinned the skill. Never update silently.**
A pin keeps running its version on purpose, so the author has to be told *at the
moment of the change* where it has not landed, and given one step to bring those
places along. The offer splits every use of the skill into three groups, so nobody has
to guess which applies:

1. **Using this version now.** Unpinned bindings. They run the live skill, so they
   already have the change. Listed so the author knows who just got it.
2. **On an older version, yours to update.** Pinned bindings behind the live content
   that the author can edit: their own Agents, projects they own, and projects where
   they are still an editor. Each is a checkbox ("Contract review project: v3 → v8"),
   all ticked, with **Update selected** and **Not now**.
3. **On an older version, not yours to update.** A count, with one line saying their
   editors see Update available. For a user skill this is usually empty, because a
   private skill runs only where its owner bound it.

Rules:

- **When it appears.** After a change made from the skill page: form save, import,
  re-import and restore. Not after each file upload, where a prompt per file would
  teach people to dismiss it. The skill page's **Where it's used** section (§6.3) shows
  the same three groups at any time, and that is where file-by-file authors find it.
- **It never blocks or slows the save.** The write happens first. The SPA asks for the
  uses afterwards, and a lookup that fails or stops early says so ("We couldn't check
  every project") instead of claiming the skill is used nowhere.
- **An update is the existing pin operation, run per target.** For a project that is
  `HarnessSettingsService.pin_skill` (editor check, a settings version, its audit). For
  an Agent it is a new `pin_skill` on the Agent service with the same shape: owner
  check, `validate_agent_write`, and the Agent's own version rules. Because D1 already
  cut a version at save, every pin reuses it (`capture_current` deduplicates), so
  updating five places creates no new versions. A target that fails is reported on
  its own row; the others still update.
- **A marketplace Agent updates its draft, and the offer says so.** Everyone but the
  owner runs a published Agent's approved snapshot (`agent-version-snapshots.md`,
  PR-3), and that snapshot carries its skill pins. Updating the pin changes the
  owner's draft. Users get it only once the Agent is resubmitted and approved, so
  that row reads "Updates your draft. Resubmit to reach users", never just "Updated".
- **Agents get Update too.** The Agent form shows a pinned skill's version and
  "Update available" from `pin_status`, the same check projects use, with an Update
  button. This closes the gap whether or not the author ever sees the offer.
- **The authoring tools say it in words.** After a `my_skill_*` write, the tool result
  names how many pinned places are behind (one bounded line, no names) so the agent
  can tell the user. The update itself stays a click on the skill page: an authoring
  agent does not move other projects' pins.
- **The admin catalog enumerates nothing.** A catalog skill is bound in other people's
  Agents and projects, nothing indexes bindings by skill, and an admin does not edit
  other people's Agents. So the admin's offer is one plain statement: "Agents and
  projects that pinned an earlier version keep it until their editors update it.
  They see Update available." Reaching those editors directly needs a binding index
  (§12).

## 4. The write path

### 4.1 One capture function

`pinning.pin_current` becomes the general function, in a new
`apis/shared/skills/history.py`:

```python
async def capture_current(
    skill_id: str, *, actor: Optional[str], trigger: VersionTrigger, live: Optional[SkillDefinition] = None
) -> Optional[SkillVersion]
```

`pin_current(skill_id, pinned_by=…)` stays as a wrapper with `trigger="pin"`, so
`harness_settings` and `binding_validation` do not change. `live` lets a caller that
has just written the row skip the re-read.

`SkillVersion` gains two fields, outside `content_hash`:

- `trigger`: `create`, `save`, `file`, `import`, `reimport`, `restore`, `pin`,
  `authoring` (`my_skill_*` tools), `checkpoint` (D2's "before" step).
- `restored_from`: the version number, on a `restore` row.

`created_by` changes meaning from "who pinned" to "whose action cut it". Nothing
displays it today: `PinStatus` doesn't carry it, and pin attribution lives on the
binding (`binding_adder`).

The service write methods take an optional `trigger` (default `save` or `file`). The
`my_skill_*` tools pass `authoring`, and the services don't otherwise know who is
calling.

### 4.2 Make the capture cheap

As written, `_freeze_resources` HEADs every file's frozen key on every cut: up to 50
S3 calls for a skill that changed one line of instructions. Capture runs on every
save, so it needs two changes:

- Reuse a frozen key from the latest version's manifest whenever the path and
  `content_hash` match. That makes the S3 calls proportional to the files that
  changed, usually zero or one.
- When the caller has the bytes in hand (an upload, an import, a restore), freeze from
  them instead of reading the live key back.

**As built (#2361):** the first change shipped, matching on `content_hash` alone
rather than path and hash, since a frozen key is named by its content. The second
was **deferred**: with reuse in place, a changed file costs one existence check, one
read and one write, so freezing from bytes in hand would save one read per changed
file. It is still worth doing if capture cost ever shows up.

Cost per content write: two Queries, one conditional PutItem, and one S3 put per
changed file. All of it runs in app-api, or in the Runtime during a `my_skill_*` tool
call. None of it is on the path to the first token.

⚠️ **Every new DynamoDB call goes through `run_blocking`.** The existing skill
repository calls boto3 synchronously inside `async def`. That is the shape of the
2026-11-16 prod event-loop outage. New code doesn't copy it, and the existing version
methods move to it in the same PR.

### 4.3 Every write path, enforced

| Write | Where | Trigger |
|---|---|---|
| Admin create, PUT | `SkillCatalogService.create_skill`, `update_skill` | `create`, `save` |
| Admin file upload, delete | `SkillCatalogService.add_resource`, `delete_resource` → `SkillBundleFiles` | `file` |
| Admin import, re-import | `SkillCatalogService.import_bundle` | `import`, `reimport` |
| User create, PUT | `UserSkillService.create_my_skill`, `update_my_skill` | `create`, `save` |
| User file upload, delete, text file | `UserSkillService.add_resource`, `delete_resource`, `write_text_file` | `file` |
| User import, re-import | `UserSkillService.import_bundle` | `import`, `reimport` |
| Authoring tools | `agents/builtin_tools/skill_authoring/tools.py` → `UserSkillService.create_my_skill` / `update_my_skill` / `write_text_file` / `delete_resource` | `authoring` |
| Field-set and workflow saves | `skills/routes.py` and `workflows/routes.py` → `UserSkillService.create_my_skill` / `add_resource` | `create`, `file` |
| Restore | new, both tiers | `restore` |

The capture hooks sit in the services, after each row write. That is the point where
both tiers already converge (`SkillBundleFiles._persist_resources`, the two
`import_bundle`s). A new architecture test lists every call to
`SkillCatalogRepository.create_skill` / `update_skill` / `_persist_resources` and
fails when one is not followed by a capture, so a future write path can't skip
history quietly. Admin soft delete (status only) is not a content write and cuts
nothing.

## 5. Restore

`restore_version(skill_id, n, actor, *, expected_updated_at)` lives in
`apis/shared/skills/history.py` and serves both tiers. Each tier's service calls it
after its own access check.

1. Read the live row and version `n`; 404 when either is missing.
2. `expected_updated_at` must match the live row, or 409 ("This skill changed since
   you opened its history"), as re-import's D3.
3. Checkpoint (D2, fail closed).
4. Write the files: `put_imported_files` with `n`'s files, read from their frozen keys
   and checked against `content_hash`, so the same file rules apply. Restoring
   cannot bring back a file type the allowlist has since dropped. Such a file is
   refused, and the restore reports it rather than half-applying.
5. One row update: `n`'s `SNAPSHOT_FIELDS`, plus `import_source` per D6.
6. Write the `SKILL.md` projection, invalidate freshness, then `gc_orphaned(old, new)`.
   Frozen keys are never collected: they belong to versions.
7. Capture (`trigger="restore"`, `restored_from=n`).
8. Audit `skill.restored` (new `AuditAction`), content-free like every skill event,
   with `restoredFrom` and the new version number.

It is the re-import write order (`skill-bundle-import.md` §6) with a different
source. The failure exposure is the same: a failure after step 4 leaves new bytes
under some live keys while the row still names the old manifest. Pinned versions
read frozen copies, so no project sees it.

## 6. Surface

### 6.1 Routes

The tiers share one module, `apis/app_api/skills/version_history_core.py`: wire
models, list and detail builders, and the diff. Each route set adds only its auth and
its target lookup. This is the pattern `bundle_import_core.py` set up.

| Route | Tier | Returns |
|---|---|---|
| `GET /skills/mine/{id}/versions?limit&cursor` | owner | `{versions: [VersionSummary], nextCursor}` |
| `GET /skills/mine/{id}/versions/{n}` | owner | `VersionDetail` |
| `POST /skills/mine/{id}/versions/{n}/restore` | owner | `{skill, version}`; 409 with the reach summary unless `confirmReach` |
| `GET /admin/skills/{id}/versions…`, `…/{n}`, `…/{n}/restore` | `admin.skills`, catalog only | same, with `skill` as `AdminSkillResponse` |

All of them return 404 after auth while the flag is off. The admin routes register
before `/{skill_id}` as the import ones do.

- **`VersionSummary`:** `version`, `createdAt`, `trigger`, `restoredFrom`,
  `displayName`, `fileCount`, `isCurrent` (its `content_hash` equals the live row's),
  `importSource` (provenance only), and the actor.
  - The admin tier resolves the actor to an email, as the console does elsewhere.
  - The user tier returns only `createdByYou: bool`. A version cut by a project
    editor's pin must not disclose who that was, matching `harness_settings`.
  - The list Query projects out `instructions` and `resources`, so a page stays
    small no matter how long the skill is.
- **`VersionDetail`:** the summary, plus the version's description and instructions,
  its file list, and the diff against the live skill: `descriptionDiff` and
  `instructionsDiff` from `bundle_import.text_diff`, and file rows
  (added/changed/removed by path and `content_hash`). That is the same shape the
  import preview uses, so the SPA reuses `ImportDiffComponent`.

The existing `…/export?version=n` stays the download.

### 6.2 SPA

One shared component, `shared/skills/skill-version-history.component.ts`. It takes a
`SkillVersionHistoryApi<TSkill>` input, implemented by `MySkillService` and
`AdminSkillService`, and emits the restored skill. That is the
`SkillBundleImportComponent` pattern. It renders on:

- the admin skill form, edit mode, beside the bundle import section;
- the Customize skill detail page, for a skill you own.

**As built (#2368):** History is not on the user's edit form; a restore refreshes the
detail page instead. Restore sends the `updatedAt` the page last saw, so each host
keeps it fresh. The admin form re-reads the skill's timestamp after a file upload or
delete on the same page, and the Customize page reads it from My Skills rather than
its cached detail record, which edits made on the skill form do not refresh. Without
that, a restore could refuse a change the user had just made.

The list shows newest first. Each row has the version number, a relative date, who
(admin) or "You" / "Someone else" (user), what cut it in words ("Saved", "File
changed", "Imported from GitHub", "Restored from v3", "Pinned by a project"), and a
**Current** badge on the row that matches the live content.

Consecutive rows with the same actor, `file` or `authoring` trigger, and under 10
minutes apart collapse into one expandable row ("4 file changes"). This is how D1's
noise is handled; storage keeps every version.

Selecting a row opens its detail: the diff against the current skill, **Download
this version**, and **Restore this version**. Restore opens a CDK dialog that names
the new version number it will create and says pinned projects keep their version.
On the user tier, a 409 reach summary goes through `confirmSkillReach`, exactly as
re-import does. Restore is offered on every row except the Current one (D3).

### 6.3 Where it's used, and the update offer (D9)

Backend, My Skills only:

| Route | Returns |
|---|---|
| `GET /skills/mine/{id}/uses` | `{current: [Use], behind: [Use], othersBehind: int, complete: bool}`, each `Use` being `{kind: "agent" \| "project", id, name, pinnedVersion, canUpdate, shared, published}` |
| `POST /skills/mine/{id}/uses/update` | Body `{targets: [{kind, id}]}`, at most 25. Returns one `{kind, id, ok, version?, error?}` per target |
| `POST /agents/{agentId}/skills/{skillId}/pin` | The Agent twin of the project route. Owner or editor, as for any Agent edit; refused for a project harness and for an unpinned binding. Returns `{skillId, version}` |
| `GET /agents/{agentId}/skills/pins` | Each pinned skill binding's `version` and `updateAvailable` (`pin_status`), for the Agent form. Owner or editor |

`uses` is `lookup_skill_reach` with two additions. `SkillBinding` gains the Agent or
project id. Editor-project harnesses carry the caller's current role, so `canUpdate`
is true only for an editor. The update route re-checks every target with its own
service, so a stale `canUpdate` can only fail a row, never widen access. The lookup is
synchronous boto3 and runs in `run_blocking`.

SPA:

- A shared `SkillUsesComponent`, taking a `SkillUsesApi`, renders the three groups.
  It appears as the skill page's **Where it's used** section, and as a dialog after a
  form save, import, re-import or restore whenever `behind` is not empty. The admin
  form shows only D9's one-line statement there.
- The Agent form's skill rows show `v3` and "Update available" with an **Update**
  button, from `pin_status` on the Agent's skill bindings, read when the form loads.

**As built (#2375):** the skills picker (`ToolSelector`) has a per-row note but no
per-row button, so the Update lives in a **Skill updates** strip under the picker
(`AgentSkillPinsComponent`). It lists only pinned skills with a newer version, and
reads `GET /agents/{id}/skills/pins`. The Agent form sends each binding's stored
config back on save, so the strip hands the new version to the form
(`onSkillPinMoved`); otherwise a later save would quietly put the old pin back.
The authoring tools' note (#2370) is a count with no names, added only when a pinned
place is behind and the flag is on.

## 7. Feature flag

`SKILL_VERSION_HISTORY_ENABLED` / `CDK_SKILL_VERSION_HISTORY_ENABLED`,
`features.skillVersionHistory`. In development: only `"true"` enables it. It is off
by default and in prod. It has been on in the `development` environment and
`environment.development.ts` since 2026-11-24 (#2376). The SPA half arrived with
PR-3 (#2368), the first PR that read it, not PR-1 as planned below.

- **app-api:** gates the routes and the capture.
- **AgentCore Runtime:** gates the capture inside the `my_skill_*` tools. Without it,
  the authoring tools would be the one write path that skips history. Written to the
  Runtime only while on (about 35 bytes of the V2 environment budget).
- **Kind:** a **feature switch** once finished, because restore widens what users can
  do. The capture half could be argued to be a rollout switch, but splitting it would
  mean a flag that turns history on without a way to use it.

While off, everything behaves as today: versions are cut on pin only.

## 8. Cost, cache and latency answers

- **Prompt cache:** nothing new reaches the system prompt or `toolConfig`. A restore
  changes a skill's description or instructions exactly as an edit does, with the same
  one-time prefix rewrite for sessions that list it. Capture writes nothing the model
  reads.
- **Per-turn payload:** D9 adds one bounded line to a `my_skill_*` tool result
  (a count, no names), only when something pinned is behind.
- **TTFT:** no change. Capture runs after a skill write, never in a hook or before the
  first token. Inside a `my_skill_*` tool call it adds the §4.2 cost to that tool's
  duration.
- **Storage:** §3 D8.

## 9. Observability

- A `skill_version_captured` log line with `trigger`, `skill_id` (scrubbed) and the
  number of files frozen.
- A `SkillVersionCaptureFailed` EMF count.
- `skill.restored` in the audit log.

That is enough to tell whether D2's best-effort path is ever taken in practice.

## 10. Tests

- **Capture:** each row of §4.3 cuts one version per content change and none for a
  no-op save, with the trigger recorded. Plus the architecture test in §4.3.
- **D2:**
  - the first write after the flag turns on checkpoints the old content;
  - a failed checkpoint blocks a restore and a re-import, but not a plain save;
  - a failed capture is repaired by the next write.
- **§4.2:** a save that changes only instructions makes no S3 call.
- **Restore:**
  - writes a new version with the old `content_hash`;
  - restores the live files byte for byte and the display name;
  - moves no pin (`pin_status` shows `update_available`);
  - a stale `expectedUpdatedAt` is a 409;
  - restoring a disallowed file type is refused whole;
  - restoring the restore puts the previous content back.
- **Access:** the admin routes 404 a user skill; the user routes 404 another user's
  skill; the user tier never returns another user's id or email; the flag-off routes
  404 after auth.
- **SPA:**
  - specs for the shared component, run through both tiers' adapters;
  - the collapsed-row grouping;
  - Restore is hidden on the Current row only, and shown on every older row even
    when Current is not the highest number (D3).
- **Update offer (D9):**
  - `uses` splits a skill's bindings into current, behind and others;
  - `canUpdate` is false on a project where the author is now a viewer, and the update
    route refuses that target even when asked;
  - updating three targets cuts no new version;
  - one failing target leaves the other two updated;
  - a failed lookup reports itself incomplete;
  - the Agent pin route is owner-only and records the change;
  - the Agent form shows Update available exactly when `pin_status` says so;
  - the save dialog appears only when something is behind;
  - a published marketplace Agent's row says the update lands on its draft.
- **Dev validation:**
  1. edit an admin skill three times and restore the first edit;
  2. check that a project pinned to an older version still runs it;
  3. restore again to undo;
  4. on My Skills, a skill used unpinned by a shared Agent asks for the reach
     confirmation first;
  5. edit an imported skill bound to an Agent and to a project: the save offers both,
     **Update selected** moves both pins, and the Agent form then shows no Update.

## 11. Phasing

| PR | Content | Shipped |
|---|---|---|
| **PR-1** | `history.py`: `capture_current` with `pin_current` as a wrapper, the `trigger` / `restored_from` / full `import_source` fields, the §4.2 freeze reuse, `run_blocking` on the version repository methods, the capture hooks on every §4.3 path, and the architecture test. Flag wiring: backend reader, CDK config and test, `platform.yml`, Runtime copy, docs-site row, SPA flag fields. No routes and no UI. History starts accumulating on dev. | #2361. The SPA flag moved to PR-3, and freezing from bytes in hand (§4.2) was deferred |
| **PR-2** | `version_history_core.py`, list, detail and restore routes for both tiers, `skill.restored`, and the repository's projected list Query. | #2366, plus `update_skill(clear_fields=…)` so a restore can clear `import_source` (D6) |
| **PR-3** | SPA: `SkillVersionHistoryComponent`, both services' adapters, the admin form and Customize detail page, and specs. | #2368, with the SPA flag |
| **PR-4** | D9: `uses` and `uses/update`, the Agent pin route and `pin_status` on the Agent form, `SkillUsesComponent` (section and post-save dialog), and the authoring tools' result line. Depends on PR-1 only, so it can land before PR-2 and PR-3. | Split in two so each could be reviewed: **PR-4a** #2370 (backend) and **PR-4b** #2375 (UI). See §6.3's "as built" |
| **PR-5** | Dev validation (§10), then the release notes and #1798 entry if dev is turned on in prod. | Flag on in dev (#2376); validated 2026-11-24, below. Prod not yet |

PR-1 can land well before the others, so dev has real history to show by the time
the UI arrives.

**Dev validation (2026-11-24).** Done with a throwaway skill and Agent; the results
are on issue #2352.
- **Capture:** one version per change; a category-only edit cut nothing.
- **First change after turn-on:** the first admin edit of a skill that predates the
  flag saved its old content as a `checkpoint` first (D2).
- **Restore:** through the UI, it became a new version with its files byte for byte,
  and a stale view got a 409.
- **The offer after a form save:** it moved an Agent's pin 2 → 7, reusing the save's
  version. After restoring the content an Agent pinned, that Agent correctly read as
  current and no offer opened.
- **The Agent form's Update:** it moved a pin 7 → 8, and a later save of the form
  kept 8.
- **Runtime:** its environment has the flag.
- **Not exercised live:** a `my_skill_*` authoring write, which needs a chat turn and
  an approval. Its capture path is covered by tests.

**Prod rollout** (not done):
1. set `CDK_SKILL_VERSION_HISTORY_ENABLED=true` in the `production` environment;
2. flip `skillVersionHistory` in `environment.production.ts`;
3. add the flag to #1798, per CLAUDE.md "Feature Flags".

## 12. Not in this change

- **Pinning a project or Agent binding to an older version.** It's a version picker
  on the project's Skills page, which can now read this list. Its own decision: who
  may pin backwards, and what the page says about it.
- **Telling other people's editors that a catalog skill changed.** It would take an
  index of bindings by skill (a GSI or a sparse `SKILLREF#` row per binding on the
  assistants table) and an inbox entry per pinned binding's editors. D9 stops at
  telling the admin plainly.
- **Restoring a deleted skill.** Hard delete removes the versions.
- **Named or starred versions,** and a manual "Save version" button. Possible later on
  the same rows, through a `label` field.
- **Version history for workflow-carrying skills as workflows.** A workflow file is a
  skill file, so it gets versions for free. A workflow-shaped history view is the
  workflow designer's to build.

## 13. Decisions recorded (2026-11-23)

The developer driving this accepted every recommendation:

1. **D1:** capture on every content write, with consecutive file changes grouped in
   the list rather than in storage.
2. **D8:** retention is unbounded, as for Agents.
3. **D7:** a project editor sees only the version their project runs.
4. **D5:** restore brings back the display name.

They also asked that a change never leave people assuming it reached everything that
uses the skill. That became D9: after a change, offer to update what pinned it, and
give Agents the Update button they lack.
