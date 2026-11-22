# Agent Skills (agentskills.io) conformance and hardening

**Status:** implemented and released in 1.38.0 (2026-11-20). Audit taken 2026-11-18 against `develop` at `5336f41cc`. Section 10 records what shipped.
**Extends:** `skills-as-agent-primitive.md` (D1, D2, D4, D5), `skill-bundle-import.md`,
`skill-creator.md`. Where this document disagrees with them, it says so and says why.

## 1. Summary

We adopted agentskills.io as the storage and interchange format in Skills v2 and run
the Strands `AgentSkills` plugin as the disclosure engine. The audit finds the runtime
half (catalog, activation, resource reads, access) in good shape, and the **format
half only partly true**: a skill goes in through five different parsers, comes out
through none, and what we project to S3 is not a bundle a strict validator accepts.

Three things to know before reading the detail:

1. **We cannot export a skill.** D2 promised "import and export are round-trip-faithful".
   There is no export route. This is the largest conformance gap.
2. **The standard has no security model.** agentskills.io has no security page. Its
   whole guidance is "consider gating project-level skill loading on a trust check" and
   "allowlist skill directories". It is written for a local agent with a filesystem and a
   shell, run by the person who installed the skill. We are a hosted, multi-tenant
   service where the author of a skill and the person whose session runs it are often
   different people. Section 5 is therefore our own threat model, not a checklist.
3. **Two findings were live, not hypothetical, when the audit was taken.** YAML aliases
   in an imported `SKILL.md` expanded exponentially in the metadata sanitiser (S1), and
   bundle import was on in dev. Skill scripts ran in a sandbox with open internet egress
   (S2), and `WORD_SKILL_SCRIPTS_ENABLED` was on in dev and in production. Both are
   closed in 1.38.0 (section 10).

The plan (§7) is eight PRs. The decisions they depend on and a brief for each are in
`agentskills-conformance-briefs.md`.

## 2. What "the standard" is

| Source | What it is | Normative weight |
|---|---|---|
| `agentskills.io/specification` | Frontmatter fields, directory layout, progressive disclosure | The format. No version number, no changelog |
| `agentskills.io/client-implementation/adding-skills-support` | What a client should do: discovery, parsing leniency, catalog, activation, context management | Guidance ("should", "consider") |
| `agentskills.io/skill-creation/using-scripts` | How authors write scripts (`uv run`, PEP 723, `npx`, no interactive prompts) | Author guidance; assumes a shell and network |
| `skills-ref` (GitHub) | Reference validator. Its README says it is for demonstration, not production | De facto strictness test |
| Strands `AgentSkills` plugin, 1.65.0 (our pin) | Our runtime. Follows the spec; lenient by default, `strict=True` raises | What actually runs |

Two places where the sources disagree, and what we do:

- **Unknown top-level frontmatter keys.** The spec is silent; `skills-ref` rejects them
  ("Unexpected fields in frontmatter"). We accept on the way in and never emit them on
  the way out (§6.2).
- **`name` characters.** The spec says `a-z`, `0-9`, hyphen. `skills-ref` allows any
  Unicode alphanumeric. Strands uses the ASCII pattern. We use the ASCII pattern.

The only field marked experimental is `allowed-tools`. `disable-model-invocation` and
the `activate_skill` tool name appear in the client guide as examples only.

## 3. What conformance means for this deployment

The standard has three layers. We can meet them to different degrees, and a spec that
says "fully conformant" without separating them would be claiming something untrue.

| Layer | Target | Why |
|---|---|---|
| **Format** — a skill is a directory with a valid `SKILL.md` | **Full.** Any conformant bundle imports; every export passes a strict validator; `import(export(x)) == x` | No technical obstacle. This is where the gaps are |
| **Client behaviour** — catalog, activation, resource access, context management | **Full where it applies.** Filesystem discovery (`.agents/skills/`, project vs user scope) does not apply to a hosted service; our tiers (catalog, own, Agent-bound) are the equivalent | Mostly done already through Strands |
| **Execution** — running `scripts/` | **A declared subset.** Python, in an isolated sandbox, no network, no dependency install. Never on the Runtime host | The spec leaves this to the implementation ("depend on the agent implementation"), so a subset is conformant. Everything in the scripts guide beyond that assumes a trusted local machine |

A deliberate non-goal: `uvx`, `npx`, `pipx run`, `deno run`, `go run` and PEP 723 inline
dependencies. Each fetches and runs code from a public registry at call time. We will
say so in the docs and surface a skill's `compatibility` line to the model so it does
not attempt them.

## 4. Audit: conformance

Status: ✅ meets, ⚠️ partial, ❌ gap, ➖ not applicable.

### 4.1 Format

| Requirement | Status | What we do today | Gap |
|---|---|---|---|
| `SKILL.md` = YAML frontmatter + Markdown body | ⚠️ | Real YAML only in bundle import (`bundle_import.py` `parse_skill_md`, `yaml.safe_load`). The admin SPA import and the My Skills single-file import use a hand-rolled `key: value` regex (`skill-import.util.ts`); the seed loader has a third flat parser | **F1** Five ingest paths, three parsers. Nested YAML (`metadata:` map, block scalars) is silently dropped on two of them |
| `name`: 1–64, `a-z0-9-`, no leading/trailing/double hyphen | ⚠️ | Never stored. Regenerated as `slugify_skill_name(skill_id)`, which always yields a valid name. The frontmatter `name` of an import only seeds the display name; the id comes from the display name | **F2** An imported `mcp-builder` can come back as a different name. Never validated at ingest, so the user is not told theirs was invalid |
| `name` matches the parent directory | ❌ | S3 projection is `skills/{skill_id}/SKILL.md` with `name: pdf-workflows` under directory `pdf_workflows` | **F3** `bundle.py`'s claim that the prefix "is a valid agentskills.io bundle" is false for any id with an underscore |
| `description`: 1–1024, non-empty | ⚠️ | 1024 enforced everywhere. Blank refused on user, import and tool paths; **admin create accepts empty** | **F4** |
| `license` (optional string) | ⚠️ | Kept in `skill_metadata` when the ingest path keeps metadata; dropped on the admin path. Never mapped to Strands `Skill.license` | **F5** |
| `compatibility` (optional, ≤500) | ❌ | Stored unvalidated; **never mapped to `Skill.compatibility`, so the model never sees it** | **F6** The one field that tells the model "this needs network and `uv`" is invisible to it |
| `metadata` (map of string → string) | ⚠️ | Arbitrary nested values accepted and re-emitted | **F7** Not validated as string → string |
| `allowed-tools` (space-separated string, experimental) | ⚠️ | Parsed from string or list. **Projected as a YAML list** (`generate_skill_md`) | **F8** Emit the spec's string form |
| No other top-level keys (per `skills-ref`) | ❌ | Every other frontmatter key passes through verbatim to the projection | **F9** Our own output fails the reference validator |
| "May contain any files and directories" | ⚠️ | Manifest is flat, keyed by filename; nested paths are flattened (`legal/nda.md` → `legal-nda.md`). 50 files, 1 MiB each, extension allowlist (no `.html`, `.xhtml`, `.svg`; `.xml` and `.xsd` are stored and served as inert `text/plain`) | **F10** Relative links in the body stop matching; `read_skill_file` papers over it with a basename fallback. The caps and the allowlist stay, as a documented limit |
| Body guidance: under 500 lines / ~5,000 tokens | ❌ | No cap on the admin API, user API or tools; only import caps `SKILL.md` at 200 KiB. The DynamoDB item limit is the real ceiling | **F11** See also S9: activation results are exempt from offload |
| Export | ❌ | No route, service or UI. Only single resource files download | **F12** |
| Validation tooling | ❌ | No check that what we write is spec-valid | **F13** |

### 4.2 Client behaviour

| Guidance | Status | What we do today | Gap |
|---|---|---|---|
| Catalog of name + description in the system prompt | ✅ | Strands `<available_skills>`, XML-escaped, sorted by `skill_id` for cache stability, appended after the last system cache point | None. Deliberately no `<location>`: there is no file to read |
| Omit catalog and tool when there are no skills | ✅ | `build_skills_runtime` returns nothing; neither plugin nor tool is wired | |
| Hide skills the user cannot use | ✅ | Effective set = (catalog ∪ own) ∩ opt-in selection, or the Agent's bindings. A skill outside it has no name the model can say | |
| Dedicated activation tool when the model cannot read files | ✅ | Strands `skills(skill_name)` | |
| Constrain the tool's `skill_name` to an enum | ➖ | Not done, **on purpose** | An enum puts the skill list into `toolConfig`, which sits ahead of the system prompt. Every skill-set change would rewrite the whole cached prefix. The tool spec stays one constant |
| List bundled resources on activation, do not read them eagerly; cap the listing | ⚠️ | We append "Available reference files" to the instructions; nothing is read until `read_skill_file`. Bounded by the 50-file cap | **C1** Wording says "reference files" for scripts and assets too |
| Wrap activation output (`<skill_content name=…>`) | ❌ | Raw instructions | **C2** |
| Protect skill content from compaction | ❌ | No protection found in the session manager or compaction path | **C3** A compacted conversation can lose the instructions it is following |
| Do not re-inject an already active skill | ❌ | A second `skills` call returns the full body again | **C4** Only safe once C3 holds |
| User-explicit activation (`/skill-name`) | ✅ | Slash commands (`skill-slash-commands.md`) | |
| Lenient parsing: warn and load on a bad name, skip on a missing description, retry unquoted colons | ⚠️ | Import refuses invalid YAML outright; no colon-quoting retry | **C5** A common authoring slip (`description: Use when: …`) fails with "not valid YAML" |
| Record diagnostics somewhere the user can see | ⚠️ | Import preview lists skipped files with reasons; nothing for frontmatter problems | **C6** |
| Name collisions: pick a precedence, warn | ⚠️ | No collision possible in the user tier (ids are allocated). Admin-chosen ids can collide after slugging (S6) | |
| Filesystem discovery, project/user scopes, trust check on project skills | ➖ | Hosted service | The equivalent trust question is §5 |
| Subagent delegation | ➖ | Optional in the guide; not planned here | |

### 4.3 Execution

| Guidance | Status | What we do today |
|---|---|---|
| Agents can run `scripts/` | ⚠️ | Stored and returned as text with an "inert" notice. One path runs them: the Word tools under `WORD_SKILL_SCRIPTS_ENABLED` (on in dev and prod), `.py` only, strict manifest lookup, in AgentCore Code Interpreter |
| Runtimes other than Python; one-off registry commands; inline dependencies | ➖ | Not supported, by decision (§3) |

## 5. Audit: security

### 5.1 Trust boundaries

The risk in a skill is who wrote it compared with whose session runs it.

| Skill reaches the model through | Author | Runs with | Risk |
|---|---|---|---|
| Own skill, written by hand | The user | The user's tools, tokens and files | Self only |
| Own skill, **imported** from a zip or GitHub | A third party | The user's | Third-party text acts with the user's authority |
| Own skill, **refreshed** from GitHub | A third party, later | The user's | The source changed after the user first read it |
| Catalog skill | An admin | Every granted user's | Wide reach; admins are trusted |
| Skill bound to a **shared Agent or project** (invoke-through, D7) | The Agent's owner or the binding's adder | **The invoker's** | Author and victim differ. This is the boundary that matters most |

Skill instructions are instructions by design. No filter makes hostile instructions
safe; the controls that work are provenance, review before first use, pinning what was
reviewed, and limiting what any instruction can reach. D1 (a skill never grants or
mounts a tool) is the most important existing control and must not be weakened.

### 5.2 Findings

Severity is for our deployment as configured today. "Reproduced" means run in this
audit; "read" means established from the code only.

| # | Finding | Evidence | Severity | Fix |
|---|---|---|---|---|
| **S1** | **YAML alias expansion in bundle import.** `yaml.safe_load` permits anchors and aliases; `_dynamo_safe` then deep-copies the shared structure. A 314-byte frontmatter produced 2,097,152 leaves in 277 ms, growing 8× per added line; a few more lines exhaust an app-api task's memory. Any signed-in user can send it | Reproduced against `parse_skill_md` + `_dynamo_safe` | **High on dev**, where `SKILL_BUNDLE_IMPORT_ENABLED` is on (GitHub variable set 2026-11-18); none in production, where it is unset | Refuse anchors and aliases in the one shared parser; cap depth and node count (§6.1) |
| **S2** | **Skill scripts run with open internet egress.** The Code Interpreter is `networkMode: 'PUBLIC'`. A skill script receives the user's document and `params`. With a shared Agent the author is not the person whose document it is, so a script can send that document anywhere. The execution role is logs-only, so there is no AWS data to reach | Read (`code-interpreter-construct.ts`, `word_document_tool.py`). `_ci_exec` has no timeout. The flag is **on in the `development` and `production` GitHub environments** (production since 2026-11-18); CLAUDE.md's "off everywhere" is stale | **High, live in production.** Reaching it needs a shared Agent or project whose bound skill carries a script, and an invoker with the Word tools | A second interpreter in `SANDBOX` mode for skill scripts and a timeout now (briefs, PR 2); the consent gates in §6.6 later. Model-written code already runs in the public one; the new element is code that persists and was written by someone else. Superseded by the `VPC`-mode interpreter with no network path out (#2244) |
| **S3** | **An unpinned shared skill can change under its users.** Editing a skill bound to a shared Agent changes what runs in other people's sessions on their next turn. The `my_skill_*` tool writes show the reach on an approval card; the REST writes and bundle re-import show nothing | Read | Medium | Reach confirmation on every write path; bindings to imported skills pin by default (§6.5) |
| **S4** | **A GitHub refresh shows that the instructions changed, not how.** The preview returns per-field change flags and an instruction length. D3's "nothing is written that the user did not see" holds for files, not for the text that steers the model | Read (`skill-bundle-import.md` §3) | Medium | Return a text diff of description and instructions; show owner, repo and both commit SHAs |
| **S5** | **No audit trail.** Skill create, update, import, refresh and delete write no audit event; `import_source` records only the latest import | Read (no audit call under `apis/shared/skills` or the skills routes) | Medium | Audit events with content hashes (§6.5) |
| **S6** | **Slug collision in admin-chosen ids.** `web_research`, `web__research` and `web_research_` all slug to `web-research`. `by_slug` and the Strands skill map keep the last one silently, so one skill answers to the other's name. User-tier allocation cannot produce these shapes | Read | Low (admin action only) | Refuse an id whose slug is already taken |
| **S7** | **Frontmatter can overwrite runtime metadata.** `record_to_strands_skill` builds `{"skill_id": …, "display_name": …, **skill_metadata}`, so a frontmatter key named `skill_id` wins. No reader of `metadata["skill_id"]` exists today | Read | Low, latent | Spread `skill_metadata` first |
| **S8** | **Invisible text.** No ingest path strips or flags zero-width characters, bidirectional overrides, Unicode tag characters, or control characters. A reviewer and the model can be shown different instructions. The description is the sharper case: 1,024 characters of another person's text enter the invoker's system prompt | Read (no such filter in any validator) | Medium | Refuse in `name` and `description`; flag and show in the preview for the body and text files |
| **S9** | **Unbounded activation cost.** Instructions are uncapped on three paths, and `skills` results are exempt from tool-result offload, so a 300 KB body enters history whole and is re-sent every turn | Read | Low (cost, and quota burn on a shared Agent) | Cap the body at 200 KiB everywhere, warn above 5,000 tokens |
| **S10** | **`allowed-tools` means "pre-approved to run" in the spec.** Honouring that literally would let a skill's author approve their own tool calls in someone else's session | Not a defect: we treat it as advisory (D4) | Design constraint | Never a grant, never an approval bypass. A narrowing-only mode is possible later (§8) |
| **S11** | **Remote skill sources exist in the SDK.** `Skill.from_url` and filesystem path sources would bypass every ingest control | Not used today | Guard | Architecture test: skills reach `AgentSkills` only as `Skill` instances built by `record_to_strands_skill` |
| **S12** | **Integrity of live files.** Bucket versioning is off, files are overwritten in place, and the row update has no optimistic lock | Read | Low | Out of scope here; pinned versions already read frozen, content-addressed copies |

### 5.3 What already holds

Worth stating so later changes do not undo it:

- Resource reads are a manifest lookup, never a path: the only S3 key read is one the
  row names. `..` never falls back. Script resolution uses the strict lookup.
- Access is structural: tools are built over the turn's effective records, so there is
  no id the model can supply to reach another skill.
- Import refuses traversal, absolute paths, encrypted entries and oversize archives;
  reads every entry through a bounded reader; skips symlinks.
- The GitHub fetch goes to one host we name, with redirects off, no credentials, and
  the resolve-then-connect transport. The user's URL is parsed, never fetched.
- Uploads are typed by extension allowlist; downloads are attachments with `nosniff`
  and a sandboxed CSP.
- Scripts are never imported, compiled or executed on the Runtime host.

## 6. Design

### 6.1 One parser, one validator, in `apis.shared`

New module `apis/shared/skills/skill_md.py`, used by every ingest path and by export.

- `parse_skill_md(text) -> ParsedSkillMd(frontmatter, body, diagnostics)`.
  - Hardened YAML: a `SafeLoader` subclass that **raises on any anchor or alias**, with
    a depth limit of 8 and a node limit of 2,000. Frontmatter over 16 KiB is refused
    before parsing. A conformant frontmatter is a dozen scalars.
  - Lenient where the client guide asks for it: strip a BOM, accept CRLF, and on a YAML
    error retry once with the unquoted-colon fix; record the retry as a diagnostic.
- `validate_frontmatter(fm, *, strict) -> list[Diagnostic]`, a port of the spec's rules
  (and `skills-ref`'s, which we do not add as a dependency: its README rules that out).
  - `name`: 1–64, `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, no `--`.
  - `description`: 1–1024 after trimming.
  - `compatibility`: string, 1–500. `license`: string, ≤ 500.
  - `metadata`: string → string, ≤ 50 keys, 16 KiB total.
  - `allowed-tools`: string or list on input; ≤ 64 names.
  - Unknown top-level keys: a warning on input, never present on output.
  - Text hygiene (S8): refuse C0/C1 controls (except tab and newline in the body),
    zero-width and bidirectional format characters, and the Unicode tag block in `name`
    and `description`; report their positions in the body and in text files.
- Ingest is lenient (`strict=False`): problems become preview diagnostics, and only a
  missing description or unparseable YAML refuses. Export and the S3 projection are
  strict: a test fails if either emits something the validator rejects.

The SPA stops parsing the format. Its two "Import SKILL.md" buttons post the file to
`POST /skills/mine/import/parse` (and the admin equivalent) and patch the form from the
response, the pattern the field-set editor already follows. The seed loader calls the
same function.

### 6.2 Conformant output

`generate_skill_md` changes to emit exactly the six standard keys:

```yaml
name: pdf-workflows            # the slug; always valid
description: …
license: Apache-2.0            # only if set
compatibility: …               # only if set
metadata:                      # string → string
  author: example-org
  x-original-key: "…"          # any non-standard key we were given, stringified
allowed-tools: Read Bash(git:*)   # space-separated string
```

Non-standard top-level keys are folded into `metadata` (non-strings JSON-encoded), so
nothing is lost and the file validates. `compose` is left out of the frontmatter; it is
ours, unused at runtime, and stays on the row.

Because `SKILL.md` is a projection, changing its bytes rewrites no prompt: the model
never reads it. A one-off backfill regenerates existing projections.

### 6.3 Name and directory

- The standard `name` stays derived from `skill_id`. It is stable, always valid, and in
  the user tier one-to-one with the id.
- Import allocates the id **from the frontmatter `name`** when it is valid
  (`mcp-builder` → `mcp_builder`, suffixed on collision), falling back to the display
  name. The original is kept in `import_source.originalName`, and the preview says when
  the stored name will differ.
- Export's directory is the slug, so `name` matches its parent.
- The S3 prefix stays `skills/{skill_id}/`. Moving keys is churn for no reader. The
  docstrings that call the prefix a valid bundle are corrected to say that the
  **export** is the conformant artifact.
- Admin create refuses an id whose slug is already taken (S6).

### 6.4 Export

`GET /skills/mine/{skillId}/export` and `GET /admin/skills/{skillId}/export`
(scope `admin.skills`) return `{slug}.zip` containing `{slug}/SKILL.md` and the files
under `references/`, `scripts/`, `assets/`.

- Owner or catalog admin only. A user who can merely *use* a skill through an Agent
  cannot export it, matching today's rule that invoke-through shares use, not content.
- Built in a worker thread from the row and the manifest's keys; streamed; capped by the
  existing 50 × 1 MiB bound.
- `?version=n` exports a frozen version.
- No flag: it widens nothing an owner cannot already read file by file. It is tested by
  `import(export(x)) == x` over fixtures and the seed skills, and by the strict validator.

### 6.5 Provenance, review and reach

- **Audit events** (`skill.created`, `updated`, `imported`, `refreshed`, `deleted`,
  `exported`, `script_run`) carrying actor, skill id, tier, the version content hash,
  and for imports the source, ref and commit SHA. Ids and hashes only.
- **Import and refresh preview** returns a text diff of description and instructions
  (S4) and the frontmatter diagnostics (C6).
- **Provenance in the UI**: an "Imported from *owner/repo* @ *sha*" line on the skill,
  in the Agent Designer's skill picker, and in the share dialog beside each bound skill.
- **Reach on every write path** (S3): the REST update and re-import return the same
  reach summary the approval card shows, and the SPA confirms before saving when other
  people's Agents or projects run the skill unpinned.
- **Pin by default** when an Agent or project binds a skill that has an `import_source`.
  The binding runs what was reviewed until someone clicks Update.

### 6.6 Script execution

Scripts stay inert outside the Word tools. That one path is already on in dev and
production, so items 1 and 5 below are done first (briefs, PR 2). Before the flag is
promoted to default-on, or any general runner is built, all of the following hold:

1. **A dedicated Code Interpreter for skill scripts with `networkMode: 'SANDBOX'`.**
   One more resource in `PlatformStack`, with the same logs-only role. No egress means a
   script cannot send a document anywhere and cannot fetch dependencies.
   **Superseded:** every interpreter call, skill scripts and model-written code alike, now
   goes to one interpreter in `VPC` mode in an isolated VPC with no route, no
   security-group egress and DNS off, and the `PUBLIC` and `SANDBOX` interpreters are
   removed (#2244). AWS documents `SANDBOX` as allowing S3 access.
2. **Python only, from the manifest only**, by strict lookup, from a skill bound to the
   turn. No shell tool, no interpreter choice, no `uv`/`npx`.
3. **The script's hash is part of the consent.** The first run of a given
   `(skill, path, sha256)` in a conversation pauses on an approval card naming the
   skill, its author and its provenance. A changed hash asks again.
4. **Across a trust boundary, only pinned versions run.** A script from a skill the
   invoker does not own runs only from a frozen version.
5. **A repo-side timeout and output cap** on the sandbox call, and a fresh session per
   run (already true).
6. Each run writes a `skill.script_run` audit event.

A general `run_skill_script` tool, if we want one, gets its own spec and its own feature
switch. The compatibility statement in §3 is what we publish in the meantime.

### 6.7 Runtime disclosure

- Map `license` and `compatibility` onto the Strands `Skill` (F5, F6). Strands then adds
  a `Compatibility:` line to the activation result. Cost: a few tokens, only for skills
  that declare it, only in new activation results.
- Spread `skill_metadata` before our own keys (S7).
- Rename the generated listing to "Bundled files" and group it by kind (C1).
- **C3, losing an active skill at compaction.** Compaction replaces older turns with a
  summary, so an activation older than the cut is gone. The fix is one line in that
  summary naming the skills whose activation it removed, so the model activates them
  again. It is a pure function of the messages being cut and is written once.
- **C2 and C4 are dropped.** Wrapping needs our own tool around the plugin, and
  deduplication is wrong whenever compaction has removed the first activation.

**Prompt-cache check for the whole design.** The catalog block's bytes do not change.
The `skills` and `read_skill_file` tool specs do not change (no enum, no docstring
edit), so `toolConfig` is untouched. Activation results change only for activations made
after deploy; history already written is not rewritten. Nothing is added before the
first token: every new check runs at ingest or inside a tool call.

## 7. Plan

Eight PRs, numbered as in `agentskills-conformance-briefs.md`, which holds the brief and
acceptance criteria for each. No new feature flag: each PR is a finished fix or an
additive capability gated by what already gates skills.

| PR | Contents | Closes |
|---|---|---|
| **1. Parser hardening** | `skill_md.py` with the hardened loader; bundle import and seeds call it; metadata spread order; slug-collision refusal; unsafe characters refused in `name`/`description`; 200 KiB body cap on every path; admin description non-empty | S1, S6, S7, S8 (part), S9, F4, F11 |
| **2. Isolate skill script execution** | A second, no-egress Code Interpreter for skill scripts; a timeout; CLAUDE.md corrected | S2 (network and timeout) |
| **3. Validator and conformant projection** | `validate_frontmatter`; new `generate_skill_md`; admin DTOs gain `allowedTools` and `skillMetadata`; `license`/`compatibility` mapped to Strands; projection backfill; the prompt-cache pin test | F5–F9, F13 |
| **4. Export** | Owner and admin export routes; "Download skill" in the SPA | F3 (for the artifact), F12 |
| **5. One ingest path** | Server-side parse endpoint; SPA regex parser deleted; id from frontmatter `name`; preview diagnostics and instruction diff; colon-quoting retry | F1, F2, C5, C6, S4 |
| **6. Provenance and reach** | Audit events; provenance lines; reach confirmation on REST writes; pin by default for imported skills; architecture test on skill sources | S3, S5, S11 |
| **7. Nested paths** | `SkillResourceRef.path`; manifest unique by path; import stops flattening | F10 |
| **8. Context management** | The compaction summary names skills whose activation it removed; listing wording | C1, C3 |

PR 1 goes first, alone. PR 2 follows it. PRs 3 → 4 → 5 are a chain. PRs 6, 7 and 8 start
after PR 3 and are independent of each other.

Not in the plan: hash-bound consent and pinned-only script execution (§6.6 items 3–4),
any general script runner, promoting `SKILL_BUNDLE_IMPORT_ENABLED` to default-on (not
before PRs 1 and 5 are in production), wrapping activation output (C2) and
deduplicating activations (C4).

## 8. Decisions

The thirteen decisions this plan depends on are in
`agentskills-conformance-briefs.md` §1; all were accepted as recommended on
2026-11-18. The ones that change behaviour people will
notice: isolating script execution now (D-1, D-2), allocating an imported skill's id
from its frontmatter `name` (D-3), auditing user-tier skill writes (D-5, D-6), and
pinning imported skills by default when they are bound (D-7).

## 9. Conformance statement (to publish on the docs site after PR 5)

> This platform imports and exports skills in the agentskills.io format. It reads
> `name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`;
> other top-level keys are preserved under `metadata`. `allowed-tools` is shown to the
> model and never grants or approves a tool. A skill may hold up to 50 files of 1 MiB
> each, of the allowed types. Scripts are stored and can be read; they run only where an
> administrator has enabled sandboxed execution, in Python, without network access or
> dependency installation. Skills are disclosed to the model as a catalog in the system
> prompt and loaded on demand through a tool.

## 10. Outcome (2026-11-19; released 2026-11-20)

All eight PRs merged into `develop` and passed their dev checks, each recorded as a comment on its pull request. They shipped together in release 1.38.0.

| PR | Pull request | Closed |
|---|---|---|
| 1. Parser hardening | 2147 | S1, S6 (new admin ids), S7, S8 (name and description), S9, F4, F11 |
| 2. Isolate skill script execution | 2149 | S2: skill scripts run in a `SANDBOX` Code Interpreter with a 120 s limit. Dev check: public internet and both S3 endpoints unreachable, the Contract Redline script unaffected. Superseded by the `VPC`-mode interpreter (#2244) |
| 3. Validator and conformant projection | 2151, follow-up 2153 | F5–F9, F13. The follow-up made non-string metadata a warning on input, so stored skills stay editable |
| 4. Export | 2155 | F3 for the artifact, F12 |
| 5. One ingest path | 2157 | F1, F2, C5, C6, S4 |
| 6. Provenance and reach | 2162 | S3 (REST writes), S5, S11 |
| 7. Nested paths | 2163 | F10. Uniqueness is by path across kinds, not `(kind, path)`, which keeps re-upload under a new kind a move |
| 8. Context management | 2159 | C1, C3 |

Backfill: the projection backfill (`backfill_skill_bundles.py --projections-only`) ran on dev after PR 3 (18 skills, 4 projections rewritten) and on production after the 1.38.0 deploy (41 skills, 3 rewritten). Neither found a shared activation name or a projection failing strict validation.

Production check after the 1.38.0 deploy: a Word skill script (the Contract Redline engine) ran in the `SANDBOX` interpreter, applied all its revisions with validation passing, and wrote one `skill.script_run` audit record with outcome `ok`.

Still open:

- **Reach gap, mostly closed.** `lookup_skill_reach` missed a skill a project editor bound into someone else's project (the `addedBy` clause). Pull request 2165 (also in 1.38.0) reads the author's project memberships, so the approval card and the 409 now name those projects. Still missed: a project the author has since left, because nothing indexes a binding by who added it. Covering that needs a new index.
- **Legacy admin ids** that slug to the same name as a newer user-tier id are not prevented at user allocation; the backfill found none on dev.
- **Emoji joiners in names.** PR 1 refuses U+200D in names, which also blocks compound emoji and some scripts. Undecided.
- **Eval.** The compaction case passes on Haiku 5.5 with and without the summary line, because its prompt names the skill's subject; a case that isolates the line is a follow-up.
- **Out of the plan by decision:** hash-bound consent and pinned-only execution for scripts (section 6.6 items 3–4), a general script runner, narrowing `allowed-tools`, local skill discovery in the terminal client.
