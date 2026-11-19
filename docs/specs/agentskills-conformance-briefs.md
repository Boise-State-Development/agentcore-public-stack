# Agent Skills conformance: decisions and per-PR briefs

Companion to `agentskills-conformance.md` (the audit and design). This file is what an
implementing session is handed: the decisions it must not re-open, the rules every PR
follows, and one brief per PR with acceptance criteria.

## 1. Decisions

**All thirteen were accepted as recommended on 2026-11-18.** The "Decided" column is the
decision; an implementing session does not re-open it. A brief that depends on one
names it.

| # | Question | Decided | Needed by |
|---|---|---|---|
| **D-1** | `WORD_SKILL_SCRIPTS_ENABLED` is **on in production** (GitHub variable set 2026-11-18) and in development, and the Code Interpreter those scripts run in has open internet egress. Do we isolate now, or accept it until PR 8? | Do PR 2 (isolation) straight after PR 1. A skill script from a shared Agent runs on the invoker's document | PR 2 |
| **D-2** | Isolation adds one `CfnCodeInterpreterCustom` to `PlatformStack` (`networkMode: 'SANDBOX'`). OK to add the resource? | Yes. One resource; it is the only control that stops a script sending a document out | PR 2 |
| **D-3** | Import allocates the skill id from the frontmatter `name` (`mcp-builder` → `mcp_builder`) instead of from the display name. This reverses a detail of `skill-bundle-import.md` §4 | Yes: it is what makes a round trip keep the name. Display name is still derived from `name` and still editable | PR 5 |
| **D-4** | Non-standard top-level frontmatter keys are folded under `metadata` on export, as strings. Input stays lenient | Yes. The alternative is exporting files the reference validator rejects | PR 3 |
| **D-5** | User-tier skill writes go into the admin audit log (`apis/shared/audit`), which today records administrative mutations only | Yes, with new `skill.*` actions. Ids and hashes only, never instruction text | PR 6 |
| **D-6** | Audit `my_skill_*` tool writes and script runs too? They happen on the AgentCore Runtime, which has no audit table name or grant today | Yes, by adding the table to `derived_environment.json` (no Runtime environment bytes) plus an IAM grant | PR 6 |
| **D-7** | An Agent or project that binds a skill with an `import_source` pins the current version by default | Yes. The binding keeps running what was reviewed until someone clicks Update | PR 6 |
| **D-8** | Who may export: the owner, and a catalog admin for catalog skills. A user who can only *use* a skill through a shared Agent may not | Yes, matching invoke-through (use is shared, content is not) | PR 4 |
| **D-9** | Compaction: when a cut removes a `skills` activation, the summary gains one line naming those skills and telling the model to activate them again. No protected blocks, no deduplication | Yes. It is a pure function of the messages being cut, adds nothing to the turn path, and avoids keeping multi-kilobyte bodies alive forever | PR 8 |
| **D-10** | Hard caps: body 200 KiB on every write path (already the import cap); frontmatter 16 KiB; a warning, not a refusal, above 5,000 tokens | Yes | PR 1 |
| **D-11** | `allowed-tools` stays advisory. No narrowing mode in this plan | Yes | None |
| **D-12** | Local skill discovery in the terminal client is out of this plan | Yes | None |
| **D-13** | A general "run any skill script" tool is out of this plan. Script execution stays the Word tools' `skill_name` / `path` path | Yes. It would need its own spec and feature switch | None |

Facts the audit left open, now settled:

- **Bundle import is on in dev.** `CDK_SKILL_BUNDLE_IMPORT_ENABLED=true` in the
  `development` GitHub environment (set 2026-11-18) and `skillBundleImport: true` in
  `environment.development.ts`. It is not set in production. So finding S1 is reachable
  on dev today.
- **Word skill scripts are on in dev and prod** (D-1). CLAUDE.md's "off everywhere" for
  this flag is stale; PR 2 corrects it.
- **Skill scripts have no execution timeout.** `_ci_exec` in
  `agents/builtin_tools/office/_storage.py` iterates the response stream with no bound.
- **The Word tools install nothing in the sandbox** (no `pip install` in
  `word_document_tool.py` or `office/`), and files move through the `writeFiles` /
  `readFiles` API, not the network. So a no-egress interpreter should run today's
  scripts unchanged; PR 2 proves it on dev before switching.
- **The audit sink exists**: `AuditService.record(action=…, actor=…, target_id=…,
  target_type=…, before=…, after=…)` in `apis/shared/audit/service.py`. It never raises.
  Its table name reaches app-api and the scheduled-runs Lambdas only.
- **Compaction replaces older turns with a model-written summary**
  (`session/compaction_policy.py`, `compaction_summary.py`). Nothing treats a `skills`
  result specially, so an activation older than the cut is gone.

## 2. Rules for every PR

An implementing session reads `CLAUDE.md` and `agentskills-conformance.md` first. The
points most likely to be missed:

- **Branch from `develop`**, `feature/<short-description>`, PR into `develop`. Each PR
  needs an issue on the team board and `Closes #n`; open it with the
  `web-team-dev:open-pr` skill. Do not merge. Do not enable auto-merge.
- **No new packages.** Everything here uses the standard library, PyYAML and what is
  already pinned.
- **Import boundary.** `app_api`, `inference_api` and `agents/` import only from
  `apis.shared`, never from each other. Shared skill code goes in `apis/shared/skills/`.
- **Prompt cache.** No PR may change the bytes of the `<available_skills>` block, the
  `skills` tool spec, or the `read_skill_file` tool spec (name, docstring, arguments).
  PR 3 adds a test that pins all three; later PRs must keep it green.
- **Time to first token.** No new work between a request arriving and the first token.
  Every check in this plan runs at ingest or inside a tool call.
- **Event loop.** Parsing, zip building and S3 calls on app-api run in a worker thread
  (`run_blocking`), as bundle import already does.
- **Routes** under `apis/app_api/` authenticate with `get_current_user_from_session`
  (admin: `require_admin`). A flag-gated route authenticates first, then 404s.
- **No new feature flag** unless a brief says so.
- **Tests on this machine:** `uv run python -m pytest -n 3`, `vitest --maxWorkers=2`.
  Do not put a wall-clock bound over a moto-backed test.
- **Done means:** the listed tests exist and pass, the backend suite and the SPA suite
  pass, the brief's QA steps were carried out and their evidence is in the PR body, and
  anything not done is stated there.

**QA.** Backend-only PRs are proven by tests. A PR with a UI change is checked in the
in-app browser against a local stack from the worktree (`SKIP_AUTH`), clicking by
element reference. Dev validation happens after merge, in a new session started after
the deploy finishes, and is recorded on the PR.

## 3. Order

```
PR 1 ──► PR 3 ──► PR 4 ──► PR 5
  │
  └────► PR 2            PR 6, PR 7, PR 8: after PR 3, independent of each other
```

PR 1 first, alone, as the trial of the loop. PR 2 next. PRs 3 → 4 → 5 are
a chain: each starts after the previous one has merged. At most three sessions at once.

---

## PR 1. Parser hardening

**Goal.** One hardened `SKILL.md` parser, and the small defects closed. Fixes S1, S6,
S7, S8 (name and description), S9, F4, F11.

**Change.**

1. New `backend/src/apis/shared/skills/skill_md.py`:
   - `parse_skill_md(text: str) -> ParsedSkillMd` with `frontmatter: dict`, `body: str`,
     `diagnostics: list[Diagnostic]`. Move the delimiter logic from
     `bundle_import.parse_skill_md`; keep that name importable as a thin wrapper so
     callers and tests do not churn.
   - A `yaml.SafeLoader` subclass that raises on **any anchor or alias** (override
     `compose_node` to refuse an `AliasEvent`, and refuse a node with an anchor), and
     counts nodes and depth while composing: at most 2,000 nodes, depth 8.
   - Refuse a frontmatter block over 16 KiB before parsing. Strip a leading BOM.
   - Errors raise `SkillMdError` with a message a user can act on; `bundle_import`
     maps it to `BundleImportError`.
   - `find_unsafe_characters(text) -> list[UnsafeCharacter]`: C0 and C1 controls except
     `\t` and `\n`, zero-width and bidirectional format characters (U+200B–U+200F,
     U+202A–U+202E, U+2060–U+2069, U+FEFF), and the tag block (U+E0000–U+E007F).
2. `bundle_import.py`: use the new parser. `_dynamo_safe` gets a node budget as a second
   guard, so it is safe even if handed a structure from somewhere else.
3. `seed_bootstrap_data.py` `load_seed_skill_bundle`: replace the flat parser with
   `parse_skill_md`.
4. `strands_mapping.record_to_strands_skill`: spread `skill_metadata` **first**, then
   `skill_id` and `display_name`.
5. Admin create (`app_api/skills/service.py`): refuse a `skill_id` whose
   `slugify_skill_name` equals that of an existing skill. Existing rows are untouched.
6. Description: refuse blank on admin create and update (`min_length=1` after trim).
   Refuse unsafe characters in `description` and `display_name` on every write path
   (admin DTOs, user DTOs, `_field_problems` in the tools, import).
7. Body cap: 200 KiB of UTF-8 on admin create/update, user create/update, and the
   `my_skill_create` / `my_skill_update` tools. One constant in `apis/shared/skills/models.py`.

**Acceptance.**

- A frontmatter with one alias is refused. The audit's probe, as a test: an 8-wide,
  10-level alias document is refused **without being expanded** (assert on the error,
  and that the composer saw fewer than 2,100 nodes; no timing assertion).
- Depth 9 refused, depth 8 accepted. 2,001 nodes refused. 16 KiB + 1 refused.
- Every existing `test_skill_bundle_import.py` case still passes unchanged.
- The seed skills (`web_research`, `skill_creator`) load to the same rows as before
  (compare the dicts the seed tests already build).
- `record_to_strands_skill` with `skill_metadata={"skill_id": "evil"}` yields the true id.
- Admin create of `web__research` when `web_research` exists is a 409.
- A description containing U+202E is refused on all four write paths; a body of
  200 KiB + 1 is refused on all three.
- A row that already has an empty description can still be read, listed and disabled.

**QA.** Tests only. After merge, on dev: import a zip whose `SKILL.md` has an alias and
confirm a clean 400, and confirm a normal bundle (`anthropics/skills`, `mcp-builder`)
still previews and imports.

**Out of scope.** Field validation beyond the above (PR 3). The SPA parsers (PR 5).

---

## PR 2. Isolate skill script execution

**Depends on D-1, D-2.** Fixes S2. Infrastructure plus Runtime.

**Change.**

1. `infrastructure/lib/constructs/agentcore/code-interpreter-construct.ts`: a second
   `CfnCodeInterpreterCustom` named with `getResourceName(config, 'skill_script_interpreter')`
   (hyphens to underscores, as the first), `networkMode: 'SANDBOX'`, reusing the
   existing logs-only execution role. Keep explicit stable names: `executionRoleArn` is
   create-only.
2. Its id reaches the Runtime **without spending the V2 environment budget**. It is a
   generated id, not a `{prefix}-{suffix}` name, so it cannot go in
   `derived_environment.json`. Publish it to SSM beside the existing interpreter's
   parameter and read it lazily on first script run, cached for the process (the
   pattern `code_interpreter_diagram_tool.py` already uses). Grant the Runtime role
   `ssm:GetParameter` on that one parameter and the session actions on the new
   interpreter, alongside the existing grants.
3. `word_document_tool._run_docx_skill_script`: start its session on the skill-script
   interpreter. If the id cannot be resolved, **refuse** with a message for the model;
   never fall back to the public interpreter. Model-written `python_code` keeps using
   the existing one.
4. A timeout on the skill-script call: run it in a worker thread under
   `asyncio.wait_for` (default 120 s, constant in `skill_scripts.py`), stop the session
   on timeout, and return an error result.
5. Correct `CLAUDE.md`: the flag is on in the `development` and `production` GitHub
   environments, and scripts run in the no-egress interpreter. Add the row to the
   docs-site feature-flags page.

**Acceptance.**

- CDK test: the new interpreter has `networkMode: 'SANDBOX'`; the Runtime's
  environment payload is byte-identical to before (the existing
  `RuntimeEnvironmentPayloadGuard` test and a snapshot of the variable names).
- Unit: the script path asks for the skill-script interpreter; an unresolved id returns
  a refusal and starts no session; a script that sleeps past the timeout returns an
  error and the session is stopped (fake interpreter, no real sleep).
- `test_word_skill_scripts.py` still passes; the two Word tool spec digests are unchanged.

**QA (dev, after the platform and backend deploys).** In a new conversation with the
Contract Redline Direct demo skill: run its script through `modify_word_document` and
confirm the redlined document is produced. Then, with a throwaway skill whose script
calls `urllib.request.urlopen("https://example.com")`, confirm it fails with a network
error and the tool reports it. Record both in the PR.

**Risk to check first.** That today's scripts need nothing from the network. The audit
found no `pip install` in the Word tools; confirm by running the demo script in a
`SANDBOX` session on dev before changing the default path. If it fails, stop and report.

**Out of scope.** Hash-bound consent and pinned-only execution (spec §6.6 items 3–4).
They change the approval flow and need their own brief once D-13 is revisited.

---

## PR 3. Validator and conformant projection

**Depends on D-4.** Fixes F5–F9, F13.

**Change.**

1. `skill_md.py` gains `validate_frontmatter(fm, *, strict: bool) -> list[Diagnostic]`
   with the rules in spec §6.1: `name` (1–64, `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, no
   `--`), `description` (1–1024), `compatibility` (string, 1–500), `license` (string,
   ≤ 500), `metadata` (string → string, ≤ 50 keys, ≤ 16 KiB), `allowed-tools` (string or
   list in, ≤ 64 names), unknown top-level keys (warning when lenient, error when strict).
   A `Diagnostic` has `level` (`error` | `warning`), `field`, `message`.
2. `bundle.generate_skill_md` emits exactly the six standard keys, in this order: `name`,
   `description`, `license`, `compatibility`, `metadata`, `allowed-tools`.
   - `allowed-tools` as one space-separated string.
   - `license` and `compatibility` from `skill_metadata` when they are valid strings.
   - `metadata`: the row's `skill_metadata["metadata"]` map if present, plus every other
     non-standard key, stringified (`str` as is, everything else `json.dumps(…,
     sort_keys=True)`). On a key clash the explicit `metadata` entry wins.
   - **It never raises.** A row that cannot produce a valid file (a legacy empty
     description) still gets a projection, and a warning is logged with the skill id.
3. Admin DTOs (`SkillCreateRequest`, `SkillUpdateRequest`) gain `allowedTools` and
   `skillMetadata`, validated with the lenient validator. User DTOs and the
   `my_skill_*` tools validate `skillMetadata` the same way (errors refuse, warnings pass).
4. `record_to_strands_skill` sets `Skill.license` and `Skill.compatibility` from
   `skill_metadata` when they are strings.
5. `backend/scripts/backfill_skill_bundles.py`: a mode that regenerates every
   projection. Idempotent, dry-run by default, prints counts.

**Acceptance.**

- Table-driven validator tests: every valid and invalid example on the specification
  page (`PDF-Processing`, `-pdf`, `pdf--processing`, a 65-character name, a
  1,025-character description, a 501-character compatibility, a non-string metadata value).
- For every fixture row, `validate_frontmatter(parse(generate_skill_md(row)), strict=True)`
  returns no errors. Includes rows with nested metadata, a `compose` list, and
  non-standard keys.
- **Prompt-cache pin** (new test, kept green by every later PR): for a fixed set of
  records, the `<available_skills>` XML, the `skills` tool spec and the
  `read_skill_file` tool spec are byte-identical to golden values captured before
  this PR's changes.
- A skill with `compatibility` set returns a `Compatibility:` line on activation; one
  without it returns exactly today's text.
- A legacy row with an empty description still saves and still projects.

**QA.** Tests. After merge: run the backfill dry-run against dev and report the counts;
run it for real on dev only when asked.

**Out of scope.** Export (PR 4). Name and id allocation (PR 5).

---

## PR 4. Export

**Depends on D-8, PR 3.** Fixes F12, and F3 for the exported artifact.

**Change.**

1. `apis/shared/skills/bundle_export.py`: `build_skill_zip(record, read_bytes) -> bytes`.
   Layout `{slug}/SKILL.md`, `{slug}/references/…`, `{slug}/scripts/…`, `{slug}/assets/…`,
   where `slug = slugify_skill_name(skill_id)`. `SKILL.md` comes from
   `generate_skill_md` (never from S3). Entries in sorted order, fixed timestamps, so
   the same skill exports to the same bytes.
2. Routes, both building the zip in a worker thread:
   - `GET /skills/mine/{skill_id}/export` — owner only (`require_owned`, 404 otherwise).
   - `GET /admin/skills/{skill_id}/export` — `admin.skills` scope, catalog skills only.
   - Optional `?version=n` exports a frozen version's content and files.
   - Response: `application/zip`, `Content-Disposition: attachment; filename="{slug}.zip"`,
     `X-Content-Type-Options: nosniff`.
   - A file missing from S3 fails the export with a 409 naming it; never a partial zip.
3. SPA: **Download skill** on the skill detail page (`customize/skills`) and on the
   admin skill page. Shown only where the route would succeed.

**Acceptance.**

- Round trip: for each fixture bundle and both seed skills, `parse_bundle(export(skill))`
  yields the same description, instructions, `allowed_tools`, metadata and file set
  (names, kinds, hashes).
- The exported `SKILL.md` passes `validate_frontmatter(strict=True)`, and `name` equals
  the top-level directory.
- Strands opens it: unzip to a temp directory and `Skill.from_file(path, strict=True)`
  loads without raising.
- A user who can use a skill through a shared Agent but does not own it gets 404. An
  admin gets 404 for a user-tier skill.
- Two exports of an unchanged skill are byte-identical.
- SPA specs for the button's visibility and the download call.

**QA (in-app browser, local stack).** Create a skill with one reference and one script,
download it, and import the downloaded zip as a new skill; the preview shows no
skipped files and no diagnostics.

**Out of scope.** Nested paths (PR 7): files are exported at the flat names we store.

---

## PR 5. One ingest path

**Depends on D-3, PR 3.** Fixes F1, F2, C5, C6, S4.

**Change.**

1. `POST /skills/mine/import/parse` and `POST /admin/skills/import/parse`: multipart
   `file` (one `SKILL.md`, ≤ 200 KiB). Returns
   `{name, displayName, description, instructions, allowedTools, skillMetadata, diagnostics}`
   and writes nothing. The user route is **not** behind `SKILL_BUNDLE_IMPORT_ENABLED`:
   single-file import exists today without that flag.
2. SPA: both "Import SKILL.md" buttons call the endpoint and patch the form from the
   response. Delete `admin/skills/models/skill-import.util.ts` and its spec. The admin
   form now keeps `allowedTools` and `skillMetadata` (it dropped them) and sends them
   through the PR 3 DTO fields. Diagnostics render under the import control.
3. `parse_skill_md`: on a YAML error, retry once after quoting unquoted values that
   contain `: ` (port Strands' `_fix_yaml_colons` behaviour); a successful retry adds a
   warning diagnostic. The alias, depth and size refusals are never retried.
4. Id from the frontmatter name: `UserSkillService.import_bundle` allocates from
   `name.replace("-", "_")` when the name is valid and fits the id pattern, falling back
   to the display name. Store the frontmatter's `name` as `import_source.originalName`.
   The preview returns `storedName` and a diagnostic when it differs from `name`.
5. Preview diff: for a re-import or refresh, return `descriptionDiff` and
   `instructionsDiff` as unified diffs (`difflib`, 3 lines of context, capped at 400
   lines with a `truncated` flag), plus `previousCommitSha` beside the new one. The SPA
   shows them in the import dialog.
6. Preview diagnostics: frontmatter validation results and any unsafe characters found
   in the body or in text files (position and code point, first 20).

**Acceptance.**

- `description: Use when: the user asks` parses on retry with a warning.
- A `SKILL.md` with a nested `metadata:` map imports through both SPA buttons with the
  map intact (it was dropped before).
- Importing `mcp-builder` when no `mcp_builder` exists yields that id and exports back
  as `mcp-builder`; when it exists, the id is suffixed and the preview says so.
- A refresh whose instructions changed returns a diff containing the changed lines; an
  unchanged refresh returns empty diffs.
- No file under `frontend/ai.client/src` parses frontmatter (grep-style architecture
  test, or deletion plus compile).
- SPA specs for both forms and the import dialog.

**QA (in-app browser, local stack).** Import a single `SKILL.md` on the admin form and
on the My Skills form; import a public GitHub skill, edit nothing, refresh, and see
"no changes"; then point at a ref where the body differs and see the diff.

**Out of scope.** Promoting `SKILL_BUNDLE_IMPORT_ENABLED`.

---

## PR 6. Provenance and reach

**Depends on D-5, D-6, D-7.** Fixes S3, S5, S11.

**Change.**

1. `AuditAction` gains `skill.created`, `skill.updated`, `skill.imported`,
   `skill.refreshed`, `skill.deleted`, `skill.exported`, `skill.script_run`, with a
   `TARGET_SKILL` target type. Recorded at the service layer (`SkillService`,
   `UserSkillService`), once per mutation. `before` / `after` carry the tier, owner id,
   content hash (`versions.py`'s hash), file names with their hashes, and for imports
   the source type, owner, repo, ref and commit SHA. Never instruction text.
2. Add the audit table to `derived_environment.json` and its CDK pin
   test, grant the Runtime role `dynamodb:PutItem` on it, and record the four
   `my_skill_*` writes and each script run from the Runtime. `AuditService.record` takes
   a `User`; build it from the turn's identity, or add a narrow overload.
3. Reach on REST writes: `PUT /skills/mine/{id}` and the re-import route accept
   `confirmReach: true`. Without it, when `lookup_skill_reach` reports unpinned bindings
   owned by anyone other than the caller, they return 409 with the reach summary the
   approval card already renders. The SPA shows it and resubmits on confirm.
4. Pin by default (D-7): binding a skill that has an `import_source` to an Agent or a
   project stores `{"version": n}` from `pin_current`, in the Agent Designer save path
   and `projects/harness_settings.py`. The picker shows "Pinned to v*n*".
5. Provenance in the UI: "Imported from *owner/repo* @ *sha7*" on the skill detail
   page, in the Designer's and the project's skill picker rows, and in the share dialog
   beside each bound skill.
6. Architecture test: under `backend/src`, `AgentSkills(` is constructed only in
   `strands_mapping.py`, and `Skill.from_url`, `Skill.from_file` and
   `Skill.from_directory` appear nowhere.

**Acceptance.**

- One audit record per mutation, none on a no-op save; a failing audit write does not
  fail the mutation.
- No audit record contains the instructions or description text (assert on the
  serialized record).
- The 409 fires only when someone else's unpinned binding exists; an edit to a skill
  bound nowhere, or bound only by its owner, saves as today.
- Binding an imported skill creates a pin; binding a hand-written one does not.
- SPA specs for the confirm dialog, the pin label and the provenance line.

**QA (persona stack, two users).** User A imports a skill and binds it to an Agent
shared with user B. A edits the skill: sees the reach confirmation. B's next turn still
runs the pinned version. The audit log shows the import, the edit, and nothing
duplicated.

---

## PR 7. Nested paths

**Depends on PR 4.** Fixes F10. The riskiest PR: the manifest is read by version
snapshots, field sets, structured extraction, script resolution, the authoring tools
and the project skill views. Read each consumer before changing the model.

**Change.**

1. `SkillResourceRef` gains `path: str` — the path below the kind directory, `/`
   separated, at most 4 segments, each matching the existing filename rule. For every
   existing row `path == filename`, defaulted in `from_dynamo_item`; **no data
   migration**. `filename` stays the basename.
2. S3 key `skills/{id}/{kind_dir}/{path}`, identical to today's key when the path has
   one segment.
3. Manifest uniqueness becomes `(kind, path)`. The 50-file and 1 MiB caps are unchanged.
4. `_manifest_path` returns `{kind_dir}/{path}`. `_resolve_resource` matches the exact
   manifest path first, then the bare filename when exactly one file has it. The
   basename fallback in `_resolve_readable_resource` stays for old rows; the script
   lookup stays strict.
5. Bundle import stops flattening: `legal/nda.md` under `references/` is stored at that
   path. Files at the bundle root still map to `references/` or `assets/` by type.
6. Field sets stay `references/field-set.<name>.md` at depth one; a nested file with a
   field-set name is not a field set.
7. Export writes files at their paths. Uploads through the API and the tools accept an
   optional `path`; the SPA file list shows it. Deeper UI (folders) is not required.
8. Version snapshots already freeze the `resources` list; confirm the content hash
   includes `path` for new rows and is **unchanged for existing rows** (omit the field
   from the hash input when it equals `filename`).

**Acceptance.**

- Every existing skills test passes unchanged.
- A row written before this PR reads back with `path == filename`, the same S3 key, the
  same version content hash, and the same "Available reference files" listing.
- Import a bundle with `references/legal/nda.md` and `references/hr/nda.md`: both
  stored, both readable by their full path, a bare `nda.md` reports the ambiguity and
  lists both, and the export contains both at their paths.
- A path with `..`, a leading `/`, a backslash, an empty segment or 5 segments is refused
  on every write path.
- `document_extract` still resolves a field set; a Word script still resolves by
  `scripts/x.py`.
- The prompt-cache pin from PR 3 is green.

**QA (in-app browser, local stack).** Import a nested bundle, open the file list, ask
the model to read a nested reference, export, and re-import.

---

## PR 8. Context management

**Depends on D-9, PR 3.** Fixes C1, C3. Drops C2 and C4 (see D-9).

**Change.**

1. A pure function in `agents/main_agent/session/`:
   `activated_skills_in(messages) -> list[str]`, the `skill_name` of every `toolUse`
   named `skills` in the given messages, in first-seen order, deduplicated.
2. Where the compaction summary is assembled for the messages being cut, when that list
   is non-empty append one line: `Skills that were active before this point: a, b.
   Activate one again with the skills tool before relying on its instructions.` Only
   names that are still in the turn's effective skill set are listed.
3. The generated listing in `_reference_files_section` becomes "Bundled files", grouped
   as References, Scripts and Assets. This changes new activation results only.

**Constraints.** The summary is written once, at compaction, and is byte-stable
afterwards; this line is part of what is written, never recomputed on restore. Nothing
runs in a `Before*` hook. State is derived from the messages, never held on an agent
instance (one session can be served by more than one agent).

**Acceptance.**

- Unit tests for the function (none, one, repeated, a `skills` call with a missing name).
- A compaction test: history with an activation before the cut gets the line; history
  with the activation after the cut, or with none, is byte-identical to today's summary.
- Restore after compaction yields the same bytes on two consecutive loads.
- An eval case beside the existing `*_after_compaction` cases: activate a skill, pass
  the cut, then ask for something the skill governs; the model re-activates.

**QA.** State the eval run's cost per model and in total in the PR. On dev, a real
conversation pushed past compaction with a skill active.

---

## Not briefed

- **Hash-bound consent and pinned-only script execution** (spec §6.6, items 3–4), and
  any general script runner (D-13).
- **Promoting `SKILL_BUNDLE_IMPORT_ENABLED` to default-on.** A separate decision, not
  before PRs 1 and 5 are in production.
- **The docs-site conformance statement** (spec §9): a docs change after PR 5.
