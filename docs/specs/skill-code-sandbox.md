# Skill code sandbox: running a skill's scripts, and the code its instructions ask for, with no network

**Status:** **Revised 2026-11-23, awaiting approval:** §8 replaces D4's per-conversation approval card with trust decided once, when someone adopts a skill. D4 and D5 are revised, D10–D17 are new, and §12 has a new plan from PR-2 on. Three choices were settled with the author before writing: the Word tools share the gate, with a backfill; a review pins the version; and the strict policy is deploy config. **Since the first approval:** PR-1 (`run_code`, #2254), Node scripts (#2312), LibreOffice (#2285), docx-js (#2304) and the pptx packages (#2323) are merged and validated on dev. #2245 shipped to production in 1.39.0. `CODE_SANDBOX_ENABLED` is on in dev only.

Approved 2026-11-20, with every §10 recommendation accepted. **PR-0 (D8) is merged (#2245) and validated on dev (2026-11-21):** one `VPC`-mode interpreter in an isolated VPC replaced both the `PUBLIC` and `SANDBOX` ones. Every Code Interpreter tool ran on it with every network probe failing. Production verification waits for the next release. **PR-1 (`run_code`) is in progress (#2252).** **D9 (LibreOffice from a read-only mount, with DNS back on behind a DNS Firewall) was accepted 2026-11-22 after a dev spike; its PR is open (#2280).**
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-11-20
**Targets:** `develop`
**Follows:** `docs/specs/agentskills-conformance.md` §6.6, which says a general script runner "gets its own spec and its own feature switch". This is that spec.

## 1. Why

We imported Anthropic's `pdf` skill and it cannot do its job here. Its instructions assume a code interpreter:

- **It runs its own scripts from the command line.** For example, `python scripts/fill_fillable_fields.py in.pdf fields.json out.pdf`. Seven scripts (about 21 KB) read and write files on disk, read `sys.argv`, and import each other (`from extract_form_field_info import get_field_info`).
- **It asks the model to write short programs.** Most of `SKILL.md` is `pypdf`, `pdfplumber` and `reportlab` snippets for merging, splitting, table extraction and creating PDFs.
- **Its forms workflow takes several steps, and one of them is visual.** It extracts the fields, the model writes values as JSON, a script fills the form, another renders the pages to PNG, and the model looks at the images to check the result.

Today `read_skill_file` returns a script as text with the inert notice ("read it to understand the approach, then reproduce the steps yourself"). The model then retypes the program as code, which is the most expensive way to use a skill. One recorded Word run spent 20,398 output tokens retyping a skill's script before `WORD_SKILL_SCRIPTS_ENABLED` let it run by reference (`docs/specs/skill-creator.md`). Filling one fillable form with the PDF skill would mean retyping about 11 KB (~3k tokens) every time, plus any bugs introduced in the copy.

The PDF skill is the first of many. Anthropic's `docx`, `pptx` and `xlsx` skills, and most community skills, are built the same way.

## 2. What exists today

| Piece | Where | What it gives us |
|---|---|---|
| A Code Interpreter with **no network path out** (`VPC` mode, D8) | `code-interpreter-construct.ts` (`{prefix}_code_sandbox`), id in the Runtime's `AGENTCORE_CODE_INTERPRETER_ID` and in SSM at `/<prefix>/agentcore/skill-script-interpreter-id` | The isolation boundary: an isolated VPC with no route, no security-group egress and DNS off. Its execution role can only write its own logs. It's the stack's only interpreter |
| Interpreter id lookup that refuses rather than guesses | `skill_scripts.resolve_skill_script_interpreter_id` | A script is refused, not run elsewhere, when the id can't be read |
| Script resolution from the turn's bound skills | `skill_scripts.resolve_skill_script` / `load_skill_script`, `_resolve_resource` | The only S3 key the tool can read is one a bound skill's manifest names, of kind `script`, at most 1 MiB |
| A per-turn sandbox keyed on `(session, user)` | `spreadsheet_analysis/sandbox_pool.py` + `AnalyzeSandboxHook` | A sandbox that is kept for one turn's calls and stopped when the turn ends, including on interrupt and dropped stream |
| Copying files in and out | `office/_storage.py` (`_ci_write_bytes`, `_ci_read_bytes`, `_ci_exec`) | Base64 in, decode in the sandbox; read back as bytes |
| Saving a generated file with a download card | `office/_storage._store_document`, `_download_card` | S3 put, `FileMetadata` row (`source="agent"`), card preview, no presigned URL in the result |
| Run audit | `AuditAction.SKILL_SCRIPT_RUN` (`skill.script_run`) | One record per run, ids only |
| Approval card | `agents/main_agent/session/hooks/tool_approval.py` (`tool_approval_required`) | Pause before a call; Approve or Deny; resume |
| Preinstalled libraries | AWS's list for Code Interpreter | `pypdf`, `PyPDF2`, `pdfplumber`, `pdfminer.six`, `pdf2image`, `pypdfium2`, `reportlab`, `fpdf`, `pillow`, `pandas`, `python-docx`, `python-pptx`, `openpyxl`, `lxml`, and others. **Not listed:** `qpdf`, `pdftk`, and the poppler command-line tools that `pdf2image` calls (§11) |

The Word tools already run skill scripts in the no-network interpreter, but only through a Word-specific contract (`run_file(input_path, output_path, params)` or `run(doc, params)`). A script written as a command-line program, which is what imported skills ship, does not fit it.

## 3. The safety model

**The network is the boundary, not the code.** Code that cannot reach any host cannot send the user's documents anywhere, whoever wrote it and whatever it imports.

`SANDBOX` network mode is not that boundary, since AWS documents it as allowing Amazon S3 access. The boundary this section relies on is D8's: `VPC` mode with no route out.

So this tool does **not** use the AST allowlist in `apis/shared/security/python_ast_policy.py`:

- That policy predates the isolation: the diagram and analysis tools used to run in a `PUBLIC` interpreter.
- It rejects `sys`, `os`, `open()` and relative imports, so it would reject every script the PDF skill ships.
- Once the network is gone, blocking `subprocess` protects nothing that the network boundary does not already protect.

| Threat | Control | Residual risk |
|---|---|---|
| Sending a staged document out (to the internet or S3) | An interpreter in `VPC` mode in subnets with no route out and a security group with no egress rules, in a VPC with DNS support off (D8). With the office mount on (D9), DNS is on behind a DNS Firewall that answers only the file system's own name, and the only egress is NFS to its mount targets. No fallback; the tool refuses if the interpreter id cannot be resolved | Rests on the VPC configuration, which dev validation proved with raw-socket and DNS probes (§11.1, §12, §11.2). With D9 it also rests on the firewall's association staying on the VPC, which CDK pins but nothing re-checks at run time |
| A user's script changing what the next user's run executes | The office file system is mounted read-only: the run-time role has no `ClientWrite`, the file-system policy denies writes and root access to every principal but the seeding role, and the read-only access point's POSIX user doesn't own the tree (D9) | Rests on IAM and EFS's enforcement of it, which the spike proved (`Read-only file system`, §11.2) |
| Reaching AWS data | Logs-only execution role | None known |
| Another user's or another conversation's files | Files resolved only from the caller's own conversation, matched on `user_id`; references go through `readable_view`. One sandbox per `(session, user)`, never per session alone, and stopped at turn end | None known |
| A script by someone else (a shared Agent's or project's skill) that corrupts outputs or misreports what it did | Outputs are always new files; originals are never overwritten. A script runs for someone other than its author only if a named person has allowed that exact script set (its hash). That person is the one who adopted it: the importer, the owner who bound it, or an admin. Members run the version that person reviewed (§8, D4, D5). The author or an admin can turn its scripts off everywhere | A reviewer who allows a bad script gives its users bad output. They can't lose data they hadn't staged. The model can still retype a refused script as `code` (§8.8) |
| Prompt injection in a staged document steering the model's code | Same boundary: only staged files and `outputs/` are reachable. Printed output and images back to the model are capped | Same as `document_read` today. What the model reads can steer its other tools; this tool adds no new path out |
| Runaway compute or output | Per-call timeout, session timeout, caps on output, files and images, at most `N` calls per turn, and an RBAC grant | Code Interpreter billing for the time it runs |
| Escaping the sandbox to the host | AWS's microVM boundary. Nothing is imported, compiled or run on the Runtime | Rests on AWS |

## 4. The tool

One tool, `run_code`, gated by its own catalog row (§7). One tool rather than two because the skill's workflow interleaves the two kinds: a script fills the form, then a snippet the model wrote checks it, in the same working directory.

```
run_code(
  code: str | None = None,          # a program the model wrote; exactly one of code / path
  skill_name: str | None = None,    # a skill bound to this turn (as read_skill_file takes it)
  path: str | None = None,          # a scripts/*.py, .js, .cjs or .mjs file in that skill's manifest
  argv: list[str] = [],             # command-line arguments for the script
  files: list[str] = [],            # this conversation's files to copy into inputs/, by name or upload id
  show_images: list[str] = [],      # up to 4 image paths to return to the model
  language: str | None = None,      # code's language: "python" (default) or "javascript" (D2, #2311)
) -> result
```

### 4.1 The sandbox layout

The working directory holds three folders:

- **`inputs/`** has the files named in `files`, copied in once per turn and reused by later calls. Names are sanitized the way `_safe_document_name` does it.
- **`skills/<slug>/`** has the **whole bundle** of each bound skill a call names, written once per turn: `scripts/`, `references/` and `assets/`, as the manifest lists them. Scripts load their own references and assets, so all three are needed.
- **`outputs/`** is collected after every call (§4.3).

Files persist between calls in the same turn, because the sandbox does. The next turn starts a fresh sandbox, and the model re-stages what it needs, including outputs from the earlier turn, which are by then conversation files (D3).

### 4.2 Running a script

The tool resolves `skill_name` + `path` through `resolve_skill_script` exactly as the Word tools do: bound skills only, strict manifest lookup, kind `script`, at most 1 MiB. The one difference is the extension: the Word tools take `.py` only, and `run_code` also takes `.js`, `.cjs` and `.mjs` (D2). A fixed entry then runs a Python script like this:

```python
import runpy, sys
sys.argv = [<script path>, *argv]
sys.path.insert(0, <the script's folder>)      # so sibling imports work
try:
    runpy.run_path(<script path>, run_name="__main__")
except SystemExit as e:
    ...                                         # exit code comes back as the call's status
```

A JavaScript target, whether a skill's script or `code` sent with `language: "javascript"` (written to `.run_code/snippet.js`), replaces the entry's process with `node <path> <argv…>` (`os.execvp`). The exit code, the output and `timeout`'s signal are then Node's own. `require('./x')` resolves beside the file, and the office mount's `NODE_PATH` applies. Timeouts, the output cap, `outputs/` collection and image previews are the same for both languages.

`argv` is passed as data in a JSON file, never pasted into the entry's source, the same way the Word tools pass `params`. It is capped at 4 KB.

The script runs from the bundle that was copied in, not from a re-fetch. Before it runs, each script's bytes are checked against the manifest hash that the trust verdict was computed from (§8.2).

**Whose scripts run** is §8. Until PR-2b lands, a script from a skill the user doesn't own is refused, unless it's an admin-catalog skill. The refusal says so plainly and names the skill. The user's own skills' scripts and model-written `code` still run. That keeps PR-1 safe on its own: the integrity guard §8 adds is missing only for scripts PR-1 never runs.

**No network, stated up front.** With DNS off, code that does a lookup waits out the resolver's timeout instead of failing at once (162 s for the §11.1 probe). The per-call timeout caps that. The tool's description says the sandbox has no network and no package installs, and a run that ends on the timeout says the same, so the model doesn't retry a download.

### 4.3 What comes back

**To the model (the tool result, which stays in history):**

- `ok` or `failed`, plus the exit code.
- stdout and stderr together, capped at **4,000 characters**, tail kept. That's the same cap as Word skill scripts.
- One line per file saved from `outputs/`: its name, its upload id, and its size.
- Up to **4 images** from `show_images`, downscaled to at most about 1.15 megapixels (a long edge of at most 1,568 px), the size Claude reads without resizing. That's about 1,600 input tokens per page, paid again on every later turn, so the model asks only for the pages it needs.

**To the user:** a download card per saved file, through `_store_document`. At most 5 files per call and 25 MB each. Anything over is reported to the model by name and not saved. `_store_document` does not check the user's file-storage quota today, and neither do the Word tools that use it. A tool that can write five 25 MB files per call should check it, so PR-1 adds the check there, for every caller.

Nothing reaches the system prompt or `toolConfig`, and the tool's spec is constant per deployment.

### 4.4 Limits

| Limit | Value | Why |
|---|---|---|
| Per-call timeout | 120 s (`SKILL_SCRIPT_TIMEOUT_SECONDS`) | Same as Word skill scripts; the session is stopped on expiry |
| Session timeout | Turn length + margin, at most 900 s | Backstop if teardown never runs (`sandbox_pool`'s default) |
| `code` | 64 KiB | It is prompt text and stays in history |
| `files` staged per turn | 10 files, 100 MB in total | Several documents, bounded |
| Calls per turn | 20 | A loop that never converges stops |
| Output, files, images | §4.3 | Per-turn payloads stay bounded (CLAUDE.md cost tenet 2) |

## 5. How the model finds out

`read_skill_file` puts a notice ahead of a script's text (`strands_mapping._script_notice`). A third text is added, chosen while `CODE_SANDBOX_ENABLED` is on:

> [This script is stored with the skill. Run it with `run_code` (`skill_name`, `path`, `argv`) instead of copying it. Read it only if you need to know what it does.]

It is chosen per deployment, so a session never sees it change. The skill's own instructions already say how to call each script. Nothing else is added to the system prompt.

## 6. Turn path and cost

- **Before the first token:** nothing. The tool is built with the others; the sandbox starts inside the first call.
- **`toolConfig`:** one constant spec, re-measured with `scripts/measure_tool_spec_tokens.py` and stated in the PR. Turning the flag on rewrites the prefix once per deployment.
- **Per call:** the tool call (tens of tokens for a script, the code's length for `code`), at most 4,000 characters of output, and the images asked for.
- **Compared with today:** a form fill drops from about 3k retyped output tokens per run, plus a read of the scripts, to a few short calls.
- **Code Interpreter time:** billed for as long as a session runs; turn-scoped sessions mean nothing bills while the user is reading or deciding.

## 7. Flags and gating

- **`CODE_SANDBOX_ENABLED` / `CDK_CODE_SANDBOX_ENABLED`.** AgentCore Runtime only, written to the Runtime only while on, in development (only `"true"` enables). A **feature switch** once finished: it runs code users can steer, which some deployments will not want.
- **`CODE_SANDBOX_OFFICE_ENABLED` / `CDK_CODE_SANDBOX_OFFICE_ENABLED` (D9).** LibreOffice on a read-only mount for `run_code`'s sessions. AgentCore Runtime only, written only while on, in development, and synth refuses it without the code sandbox. A **feature switch** once finished: it adds resources and turns the sandbox's DNS on behind its firewall.
- **No SPA flag.** Download cards and images in tool results exist already. §8's review sheet, badges and rail entry don't get a flag either. They extend existing flows (an Agent save, a project's skills, import, the tool rail), and they appear only when a bound skill has scripts. The Word tools already run scripts in production, so those surfaces are needed whether or not `run_code` is on. The 409 that asks for a review is enforced in the same PR as the sheet that answers it (PR-2e), so no save breaks while the work is half landed.
- **Trust policy is deploy config, not a flag (D11, §8.9).** `SKILL_SCRIPT_TRUST_POLICY` (default `owner`) and `SKILL_IMPORT_GITHUB_ORGS` (default empty).
- **Catalog row `run_code`.** `enabledByDefault: false`, so a role has to be granted it. RBAC granularity is one `tool_id`, as with `request_user_login`.
- **`WORD_SKILL_SCRIPTS_ENABLED` stays as it is.** Its scripts use a Word-specific contract and need no change. Folding it into this tool later is a separate decision.

## 8. Trust: whose scripts run

This section replaces D4's approval card. D4 would have asked on the first run of each `(skill, path, sha256)` in every conversation, so a project member would have approved the same script in their tenth conversation as in their first.

Network isolation changes what the question is. A script can't send a document anywhere, and its outputs are always new files (§3). What's left is **integrity**: a script can produce a wrong or misleading result. Whether to trust an author's code to produce good results is a judgement about that author. It's best made once, by the person who adopts the skill, on behalf of everyone who uses it after them. **The people who use an Agent or project never see a prompt.**

The same gate covers the Word tools' skill scripts (D14). Today they run any bound skill's script for anyone, and they are on in production.

### 8.1 Trust paths

A script runs when **one path holds for the exact script set it belongs to** (§8.2), and **no switch has turned it off** (§8.4). Otherwise the tool refuses (§8.7).

| How the skill reaches the turn | Who decided | When | `trustedBy` |
|---|---|---|---|
| The invoker wrote it (`owner_id` is the invoker) | The invoker | By writing it | `own` |
| The invoker imported it (`import_source` is set) | The invoker | At import (§8.5). Skills imported before the import sheet existed count too: the importer still chose the source; the sheet only makes that choice informed | `imported` |
| Admin catalog (`owner_id == "system"`) | Admins | By curating it. The invoker still needs the RBAC grant, as today | `admin` |
| Bound to an Agent or a project harness, written by someone else | The person who bound it, on behalf of everyone who uses it | When binding, and at each Update that changes the scripts (§8.3). Binding a hand-written skill of your own *is* the decision, with no sheet; an imported skill, or one someone else wrote, shows the review sheet (§8.5, D17) | `reviewed` |
| Shared directly with the invoker | The recipient | When they turn it on | `accepted` |

Notes on the table:

- **Today the binder is always the author.** A user can reach someone else's skill only through invoke-through: the Agent's owner wrote it, or, in a project, the person who added the binding wrote it (`resolve_invocable_skill_ids`, `apis/shared/skills/access.py`). So `reviewed` means "the author vouches for these scripts to the people they share the Agent or project with". When skills can be shared directly, a binder may bind a skill someone else wrote, and the same review then means "I checked someone else's scripts for my members".
- **Direct skill sharing isn't built.** `skills-as-agent-primitive.md` §6 lists it for phase 2. The `accepted` row is the rule it will follow: turning on a shared skill that has scripts shows the import sheet's copy (§8.5) with "Turn on" in place of "Import". Nothing here builds it.
- **The invoker's own and imported skills run in plain chat and through their own Agents.** Through an Agent they run the binding's pinned version (§8.3).

### 8.2 The script set and its hash

`scriptsHash` is the sha256 of canonical JSON. It covers `[[path, contentHash], …]` for every manifest entry of kind `script`, sorted by manifest path (`_manifest_path`). It's shown as its first 8 hex characters: "version 3f2a9c1e". A skill with no scripts has no hash and never shows a sheet, a badge or a rail line.

- **Scripts only, not the whole bundle.** The skill's instructions and references already steer the model's own `code`, which runs for everyone without review (§8.8). Hashing them too would ask for a review on every edit to the instructions, and owners would learn to click through. A script that reads one of its own `references/` or `assets/` files is the one case this misses. The reviewer reads the script, sees what it loads, and the sheet lists those files beside it.
- **Computed from the manifest the turn already holds.** It needs no S3 read. Each manifest entry already carries the sha256 of its file (`SkillResourceRef.contentHash`).
- **Checked against the bytes that run.** When staging, the tool hashes each script's bytes and refuses on a mismatch with its manifest entry (`changed`, §8.7). A pinned version is content-addressed (`skill-versions/{id}/{sha256}`), so it can't mismatch. The check catches an in-place overwrite on a live record, racing the run.
- **An entry with no `contentHash` can't be reviewed.** The backfill (PR-2d) fills in any that exist from before files carried a hash, by hashing their bytes.

### 8.3 Updates: people keep the version that was reviewed (D5)

**A review always pins the binding**, through the existing `pinning.pin_current`. The review is therefore of a frozen, content-addressed version, and that version is what everyone runs.

- **Projects** already pin every added skill (`_pin_and_attribute_added_skills`). **Agents** pin only imported skills today (`pin_imported_skill_bindings`). That rule widens to **imported skills and any skill with scripts**. Hand-written skills without scripts keep running live, as now.
- **When the author edits the skill**, members go on running the pinned version, as pins already guarantee. `pin_status` reports `updateAvailable`:
  - If the scripts hash changed, **Update** opens the review sheet with the script diff (§8.5). For a hand-written skill of the binder's own, it moves the pin with no sheet (D17).
  - If only instructions or references changed, Update works as it does today, with no sheet.
- **Agent Designer gains the Update control** projects already have ("Update", "Pinned to v3 · since Oct 8, 2026 · A newer version is available."). It's the only new piece. Everything else this decision needs exists already, which is why it costs less than the alternative.
- **The alternative, pausing until re-review, was weighed and rejected.** With bindings left live, each save the author made would stop every member's scripts until the owner re-reviewed, and members would meet refusals for changes they never saw. It needs no pin, but it trades the author's convenience for everyone else's.
- **Legacy live bindings.** Some Agent bindings of hand-written skills and some pre-pinning project bindings run live. The backfill pins each one that has scripts at its current version and records a review (D14). After the backfill, every binding of a user skill with scripts is pinned. A live one without a review refuses (`not_reviewed`), and a test pins that rule.

### 8.4 Records, switches and revocation

The rows live in the AppRoles table beside the skill's own rows. There's no new table and no new index. A project harness is an `Assistant`, so Agents and projects share one key shape.

| Row | Key | Holds | Written by |
|---|---|---|---|
| Review | `PK=SKILL#{skillId}`, `SK=SCRIPTS_REVIEW#AST#{assistantId}#{scriptsHash}` | `reviewedBy`, `reviewedAt`, `version` (the pin's number), `scripts` (`{path: sha256}`), `method` (`review`, `update`, `authored` or `backfill`), `state` (`approved`, or `pending` under the admin policy), `approvedBy`, `approvedAt` | app-api, on an Agent save, a project skills save or an Update; the backfill |
| Acceptance (with direct sharing) | `PK=SKILL#{skillId}`, `SK=SCRIPTS_REVIEW#USER#{userId}#{scriptsHash}` | The same fields | app-api, when the recipient turns the skill on |
| Switch | `PK=SKILL#{skillId}`, `SK=SCRIPTS_SWITCH` | `off`, `by` (`author` or `admin`), `byUserId`, `at`, `reason`, and optional `hashes` (only those versions are off) | The author, from their skill's settings; an admin (§8.10) |
| Review queue (admin policy only) | `PK=SCRIPTS_REVIEW_QUEUE`, `SK={requestedAt}#{skillId}#{scriptsHash}` | A pointer to the pending review | app-api, with the pending review; deleted on approve or decline |

**The import acknowledgement is the import itself.** `import_source` gains `scriptsHash` and `scripts` (`{path: sha256}`) when a bundle has scripts, and `skill.imported` / `skill.refreshed` record them. Own skills run as `own` or `imported` whatever the row says, so the acknowledgement needs no row of its own.

**Reading the rows at run time:**

- **Inside the tool call, never before the first token.** One `BatchGetItem` reads the switch row, plus the review row for the turn's scope at the record's hash when the path needs one. That's single-digit milliseconds beside a run that takes seconds.
- **Never cached:** not on the agent, not on the tool, not in the process. The agent cache keys on configuration (CLAUDE.md), and a revocation changes no configuration, so anything held would outlive it.
- **The holder carries only the turn's scope.** The scope is the assistant id the turn runs as. It is configuration: an `@`-mention builds its own agent. The verdict is computed from the records and the scope on every call.
- **The keys are deliberately wider than `(session, user)`.** A review is per `(assistant, skill, hash)`, because one owner's review covers every member. The invoker's id decides only `own` and `imported`. Nothing is keyed on the session.

**Who can revoke:**

| Who | What | How | Effect |
|---|---|---|---|
| The binder, the Agent's owner, or the project's owner or an editor | That binding's review | **Stop allowing scripts** on the skill's binding row, or removing the skill (both delete the binding's review rows) | Scripts refuse on that Agent or project (`not_reviewed`). The skill's instructions still apply |
| The author | Their skill's scripts, everywhere | **Scripts can run** off, in their skill's settings | Switch off `by: author`. Scripts refuse for everyone, the author included, until the author turns them back on |
| An admin | One skill's scripts everywhere, for all versions or one | The admin page (§8.10) | Switch off `by: admin`. Only an admin can turn it back on. The author sees "Turned off by an administrator" |
| A member | Nothing per skill in v1 | Stop using the Agent or project, or **Report** it (§8.6) | D15 |

### 8.5 What the owner and importer see

**The review sheet, at binding.**

*When it appears:* saving an Agent (create or update), saving a project's skills (add or Update), or confirming an Update, whenever a skill with scripts has no approved review for this assistant at its hash, **and the binder didn't write those scripts by hand**. That means an imported skill (`import_source` set), or a skill someone else wrote (with direct sharing). D17 explains why.

*A hand-written skill of the binder's own gets no sheet.* The save records the review itself, with `method: authored`, `reviewedBy` the binder and the current hash, and pins the binding as any review does. The binder learns of it from a one-line note on the binding row, not from a dialog: "Scripts run for everyone who uses {Agent name} · version 3f2a9c1e". In a project, that's "for everyone in {project name}". Update on such a skill moves the pin and records a new `authored` review in the same click, with no diff. The author made the change, so there's nothing for them to review. "Stop allowing scripts" on the row withdraws it, as for any review.

*The gate is the provenance of the scripts, not the skill.* A hand-written skill that has had a script imported into it, through a refresh or a bundle merge, carries `import_source`, so it shows the sheet. Writing a script by hand into an imported skill doesn't clear `import_source`, so that skill keeps showing the sheet too.

*How it works:* app-api answers `409` with `code: skill_scripts_unreviewed` and a body. The SPA shows the sheet, then resends the save with `scriptsReviewed: {skillId: scriptsHash}`. This is the reach-confirmation pattern (`confirmReach`, `skill_reach_unconfirmed`). If a hash no longer matches because an edit raced the save, app-api answers `409` again with the new diff.

The body:

```json
{
  "skills": [
    {
      "skillId": "...",
      "displayName": "...",
      "scriptsHash": "...",
      "fromVersion": 3,
      "provenance": {"...": "..."},
      "scripts": [{"path": "...", "size": 0, "change": "new | changed | removed | same"}]
    }
  ]
}
```

> **Review the scripts in {skill}**
>
> Anyone who uses {Agent name} can run these scripts without being asked first. They run in a sandbox with no internet access, so they can't send anyone's files anywhere. They can still produce wrong or misleading results, so allow only scripts you've checked or whose author you trust.
>
> {provenance line}
>
> `scripts/fill_fillable_fields.py` · 4 KB · View
> `scripts/extract_form_field_info.py` · 3 KB · View
> Reads `references/forms.md` · View
>
> [Allow scripts]  [Add without scripts]  [Cancel]

- In a project, "Anyone who uses {Agent name}" reads "Anyone in {project name}".
- **Provenance line:** "Written by {author}", or "Imported from {owner}/{repo} @ {sha7} by {author}", or "Imported from a zip file by {author}".
- **View** opens the file's source read-only, in the same viewer the skill detail page uses.
- **Add without scripts** binds the skill with no review. Its instructions apply, and its scripts refuse for everyone but the author.

**On Update, when the scripts changed,** the heading and the first line change:

> **{n} scripts changed since you reviewed them on {date}**
>
> Until you allow this version, everyone keeps running the version you reviewed.

- Each script is marked **New**, **Changed** or **Removed**; unchanged ones collapse to "{n} unchanged".
- The secondary button reads **Keep the reviewed version**.

**Under the admin policy (§8.9),** the body gains a line and the button changes:

> An administrator reviews scripts before they run for anyone else. Until then, {Agent name} runs without them.

- The primary button reads **Send for review**.

**The binding row, afterwards** (Agent Designer and the project's skills page):

- "Scripts allowed · version 3f2a9c1e · Nov 23, 2026" (an imported skill, after the sheet)
- "Scripts run for everyone who uses {Agent name} · version 3f2a9c1e" (a hand-written skill of the binder's own, with no sheet)
- "Scripts changed since your review · Review" (imported skills only; a hand-written skill shows the existing "A newer version is available" and Update)
- "Scripts not allowed · Review"
- "Scripts waiting for an administrator's review"
- "An administrator declined these scripts"
- "Scripts turned off by an administrator"

**Import.** The bundle preview (`POST /skills/mine/import/preview`) already lists files. When the bundle has scripts, the import step shows:

> **This skill includes {n} scripts**
>
> Scripts run in a sandbox with no internet access. They can read the files you give them and write new ones, and they could still produce wrong or misleading results. Only import skills from authors you trust.
>
> From {owner}/{repo} @ {sha7}
>
> `scripts/fill_fillable_fields.py` · 4 KB · View
>
> [Import]

- From a zip, the source line reads "From a zip file. No source is recorded."
- **Import** is the acknowledgement. A checkbox in front of the button would ask for the same click twice.
- **On refresh** (`POST /skills/mine/{skillId}/import`), if the scripts changed: "{n} scripts changed in this update", with the same markers.

**The author's switch** (skill settings, own skills with scripts):

> **Scripts can run** [on]
> Turn this off to stop this skill's scripts from running anywhere, including in the Agents and projects that use it.

- When an admin has turned the scripts off, the toggle is replaced by: "An administrator turned off this skill's scripts on {date}."

### 8.6 What members see

Members see no prompts.

**The badge**, on the Agent details, the project details and the marketplace listing:

> **Runs scripts in a sandbox with no internet access** · Reviewed by {reviewer}, {date}

- For a hand-written skill, the last part reads "Shared by {author}, {date}" instead. A `method: authored` review is a person vouching for their own code, not a review of someone else's, so the badge says so.

- When skills were reviewed by different people, or there are several skills: "Runs scripts from {n} skills in a sandbox with no internet access". Its details list "{skill} · version 3f2a9c1e · Reviewed by {reviewer}, {date}", or "· From the admin catalog".
- It appears only when a bound skill has scripts that can run for the viewer, meaning an approved review or the admin catalog.

**The tool-rail entry**, for `run_code` and for the Word tools' script runs:

- **Collapsed:** "Running {filename} from {skill}", then "Ran {filename} from {skill}". A refused run shows "Didn't run {filename} from {skill}".
- **Expanded:**

> {skill} by {author} · version 3f2a9c1e
> {trust line}
> Ran in a sandbox with no internet access

- **The trust line:**
  - "You wrote this skill"
  - "You imported this skill from {owner}/{repo}" (or "from a zip file")
  - "Reviewed by {reviewer}, {date}" (or "Shared by {author}, {date}" for `method: authored`)
  - "From the admin catalog"
- **Where the data comes from:**
  - `skill_name` and `path` come from the tool input.
  - The version comes from the result's `Script version:` line (§8.11). The parse is pinned by a fixture shared with the Python, as `renderDeliveryBlock` is.
  - The author and the trust line come from `GET /skills/{skillId}/script-trust?hash=…&assistantId=…`, fetched when the entry is expanded. It answers only for a skill the caller can invoke, and returns display names, never ids. A collapsed rail costs nothing.

**No once-per-member note.** The badge says what such a note would say, and a note read once becomes noise (D15).

**When scripts don't run**, the member sees the model relay the refusal (§8.7) and the rail's "Didn't run". A member whose role lacks `run_code` never sees the tool at all. Projects already say so through `agent_notice` ("Some of this project's setup isn't available to your account…"), with `run_code` in `unavailableTools`. Agents block with the existing message (shared-projects §9.6).

**Report this skill.** The rail entry's menu has **Report this skill**. It opens the existing report dialog (`report-agent-dialog.component.ts`) with a new reason, **Its scripts gave wrong results** (`ReportReason.script`), and carries `skillId` and `scriptsHash`.

- It posts to `POST /agents/{assistantId}/report`, which also accepts a project harness, since a project harness is an `Assistant` and reports already live under `AST#`.
- It lands in the existing admin reports queue, naming the skill and linking to §8.10's page.
- The dialog's existing lede is kept: "This goes privately to the people who curate the store…"
- There's no report for the user's own skills.

### 8.7 Refusals

Refusals are tool results only. The model relays them. Each one names what didn't run, why, and who can fix it, and tells the model not to retype the script. Every one ends with the same two sentences: "Nothing ran. Don't copy the script into `code`." Each refusal also writes `skill.script_run` with `outcome: refused` (§9).

| Code | When | Text (before the common ending) |
|---|---|---|
| `not_reviewed` | No approved review for this scope at this hash, including "Add without scripts" | "'{skill}/{path}' didn't run: its scripts haven't been allowed in {scope name}. Tell the user that {scope name}'s owner can allow them in its settings." |
| `pending_admin_review` | Admin policy, review `pending` | "'{skill}/{path}' didn't run: an administrator hasn't reviewed this skill's scripts yet. Tell the user it will run once they have." |
| `off_by_author` | Switch off, `by: author` | "'{skill}/{path}' didn't run: the skill's author turned its scripts off." |
| `off_by_admin` | Switch off, `by: admin` | "'{skill}/{path}' didn't run: an administrator turned this skill's scripts off." |
| `not_trusted` | Someone else's skill with no scope (plain chat), which can only happen once direct sharing exists and before acceptance | "'{skill}/{path}' didn't run: it's another user's skill, and its scripts haven't been turned on for this account. Tell the user they can turn them on in Settings → Skills." |
| `changed` | Staged bytes don't match the manifest (§8.2) | "'{skill}/{path}' didn't run: its files changed while the run was being prepared. Try once more, and tell the user if it happens again." |
| `unreviewable` | A script with no `contentHash`, before the backfill | "'{skill}/{path}' didn't run: this skill's scripts need to be saved again before they can run for other people. Tell the user to ask its author." |

The Word tools return the same texts.

### 8.8 What this doesn't do

- **Model-written `code` isn't gated.** A model that has read a refused script could retype it. The refusal tells it not to, but that's an instruction, not a boundary. The gate is about running another author's stored code by name. That code is opaque to the people using it. A retyped copy appears in the tool call's arguments, in the rail, and costs its length in output tokens, so it isn't silent. Being honest about this is better than inventing a policy check on `code`, which D1 and §3 rule out.
- **Instructions and references aren't reviewed.** They're trusted as today, by the invoke-through rule.
- **A review is a claim by a person, not an analysis of the code.** The badge names who made it.

### 8.9 Policy for stricter deployments (D11)

These are deploy config, set like the feature flags (CDK config, then the GitHub environment variable, forwarded in `platform.yml`):

| Setting | Default | Stricter | Read by |
|---|---|---|---|
| `SKILL_SCRIPT_TRUST_POLICY` / `CDK_SKILL_SCRIPT_TRUST_POLICY` | `owner` (an unset variable arrives as `""`, which means `owner`) | `admin`: a `reviewed` or `accepted` script set runs for anyone else only after an admin approves that hash. `own`, `imported` and `admin` are unchanged | app-api (the sheet and the queue) and the Runtime (the verdict). The Runtime copy is written only while it isn't `owner`, so the default spends none of the V2 environment budget |
| `SKILL_IMPORT_GITHUB_ORGS` / `CDK_SKILL_IMPORT_GITHUB_ORGS` | Empty: any public repository | A comma-separated list of GitHub owners. A bundle with scripts from anywhere else, or a zip with scripts, is refused at import. Bundles without scripts are unaffected | app-api |

The import refusal reads: "This skill includes {n} scripts. This deployment imports skills with scripts only from {orgs}."

**Neither setting is a boundary.** A user can still write scripts by hand (`own`). The allowlist governs what arrives from outside, and the admin policy governs what runs for other people. An admin approval is audited like a review (§9). In the marketplace review page, a listing whose bindings have pending script reviews shows them with Approve and Decline, so a reviewer handles both in one place.

### 8.10 The admin surface

There's a new delegable scope, `admin.skill_scripts`, rather than reusing `admin.skills`. That scope manages the admin catalog, and it reads user-authored skills as 404 by design. This page shows user-authored scripts, a wider view, so it's granted on purpose.

The page is `/admin/skills/scripts`, with routes under `/admin/skill-scripts/`:

- `GET /{skillId}`: the author and provenance; the scripts, with read-only source; each reviewed version and where it's reviewed (Agent or project name, reviewer, date); and the switch.
- `PUT /{skillId}/switch` `{off, hashes?, reason}`: turns scripts off everywhere, for all versions or the ones listed, or back on.
- `GET /queue`, and `POST /{skillId}/reviews/{assistantId}/{scriptsHash}/approve` or `/decline`, under the admin policy only.

An admin reaches a skill from a report, from the queue, or by its id in an audit record. Every action is audited (§9).

### 8.11 What the model is told (D12)

- **Nothing new in the system prompt or `toolConfig`.** `run_code`'s spec says nothing about ownership, and the Word tools' specs don't change, so no prefix is rewritten. `read_skill_file`'s script notice (§5) is unchanged.
- **A refusal is a tool result (§8.7).**
- **A script run's result gains one line**, `Script version: 3f2a9c1e`, about 8 tokens a call. The rail reads it, and the model can answer "which version ran?". The trust path, the reviewer and the author never reach the model.

### 8.12 Turn path and cost

- **Before the first token:** nothing. The holder already carries the records, and the scope is a value from the build.
- **Per script call:** one `BatchGetItem` and the version line.
- **App-api:** the review check on save reads the skills' manifests the save already loads, plus one `BatchGetItem` for the review rows. The badge and the rail endpoint are reads on open or expand.

## 9. Audit

- **A script run** writes the existing `skill.script_run` record. `run_code` and the Word tools both write it. Its `after` gains:
  - `scriptsHash`
  - `trustedBy` (`own`, `imported`, `reviewed`, `admin` or `accepted`)
  - `reviewedBy` and `scopeId` (the assistant id), when `reviewed`
  - `version`, the pin's number
  - `outcome`, now `ok`, `failed` or `refused`, with `refusedReason` (§8.7) on a refusal
  - `verdict` and `wouldRefuse`, during the Word tools' shadow release (D14)
- **A `code` run** writes a `sandbox.code_run` record: ids, the code's length and hash, duration and exit status. No code text, no file content.
- **`skill.scripts_reviewed`** (new) is written for a review, an Update, a hand-written skill's binding (`authored`), an admin approval or decline, and each backfilled binding. The actor is the reviewer. Target: the skill. `after`: `scriptsHash`, `scripts` (`{path: sha256}`), `scopeType` (`agent`, `project` or `user`), `scopeId`, `version`, `method` (`review`, `update`, `authored`, `backfill`, `admin_approve` or `admin_decline`) and `state`.
- **`skill.scripts_review_withdrawn`** (new) is written when "Stop allowing scripts" is used or the skill is removed from a binding. It carries the same keys.
- **`skill.scripts_switched`** (new) is written when the author or an admin turns scripts off or on. `after`: `off`, `by`, `hashes` and `reason`.
- **`skill.imported` and `skill.refreshed`** already carry `importSource`, which now holds `scriptsHash` and `scripts`. That's the import acknowledgement, with its provenance.
- **Ids, hashes and display-free fields only.** Never script text.

## 10. Decisions

D1–D9 were accepted on 2026-11-20 and 2026-11-22. D4 and D5 were revised, and D10–D17 added, on 2026-11-23, pending approval of this revision. D4 reverses the conformance plan's "out of plan by decision" for §6.6 item 3.

| # | Decision | Accepted |
|---|---|---|
| **D1** | Should `run_code` accept model-written `code`, or only skill scripts? | **Both.** The PDF skill needs both, in the same working directory. Without `code`, its snippets still get retyped into a tool that can't run them |
| **D2** (revised 2026-11-22, #2311) | Which languages? | **Python and Node.** v1 was Python only, as §6.6 item 2 says. Without network, shell and Node are no riskier than Python, since Python can already spawn processes, so adding `.js` was a question of demand, not safety. The demand came with docx-js on the office mount (#2302). On its dev validation turn, the model sent the docx skill's JavaScript as `code` twice and got a `SyntaxError` both times, before it wrapped the script in Python and called `node`. The pptx skill also ships `scripts/apply_theme.js`. So a skill's `scripts/*.js`, `.cjs` or `.mjs` runs with `node` by `skill_name` + `path`, under the same resolution, sha256, audit and D4 owner rule as `.py`, and `code` takes an optional `language` (`"python"`, the default and v1's behaviour, or `"javascript"`). The spec grew from 811 to 886 tokens (+75, CountTokens on Haiku 4.5), one `toolConfig` prefix rewrite at deploy. There is no flag of its own: it is an optional argument whose default is v1, gated by `CODE_SANDBOX_ENABLED`. Shell (`.sh`) is still refused, since no bundled skill needs it yet. The Word tools stay `.py` only |
| **D3** | Should the sandbox last one turn or the whole conversation? | **One turn.** Outputs become conversation files, so a later turn re-stages them in a second or two. A conversation-long sandbox bills while the user thinks and needs its own reaper |
| **D4** (revised 2026-11-23, §8) | Consent for scripts by other authors (§6.6 item 3) | **Trust is decided once, at adoption, by the person who adopts the skill, keyed on the hash of its script set. Members are never asked.** The importer decides at import, the owner who binds the skill to an Agent or project decides at binding and at each Update that changes the scripts, a recipient decides when turning a shared skill on, and admins have already decided for the catalog (§8.1). Isolation leaves integrity as the only risk (§3), and that's a judgement about an author, which a member asked in every conversation is the person least placed to make. *Replaces (2026-11-20):* an approval card on the first run of each `(skill, path, sha256)` in a conversation. That card would have repeated itself in every conversation of every member, and it would have put the decision on the member, who has never seen the script. `tool_approval.py` isn't used: it decides one `toolUseId` and remembers nothing, which is right for an MCP write and wrong for a script set that doesn't change between calls |
| **D5** (revised 2026-11-23, §8.3) | Only pinned versions across a trust boundary (§6.6 item 4)? | **Yes: a review pins the binding, and members run the version that was reviewed.** An Update that changes the scripts asks the owner again, with the diff. Projects already pin every added skill, and Agents pin imported ones. The rule widens to any skill with scripts, and Agent Designer gains the Update control projects have. Pausing new scripts until a re-review was rejected: every save the author made would stop every member's runs. *Was (2026-11-20):* defer, because D4's hash made a changed script ask again |
| **D6** | Should the tool be offered whenever granted, or only when a bound skill has scripts? | **Whenever granted.** Tying it to the skills on the turn would change `toolConfig` when a user switches a skill on mid-conversation, rewriting the cached prefix. A role grant is constant per deployment and user |
| **D7** | Could a skill ship pure-Python wheels for packages that aren't preinstalled? | **Not in v1.** A skill needing a missing package fails with a clear message naming it |
| **D8** (accepted 2026-11-20, added after §11) | How is the interpreter isolated, given that AWS documents `SANDBOX` as allowing S3 access? | **One interpreter in `VPC` mode in its own VPC, replacing both the `PUBLIC` and `SANDBOX` ones.** No caller needs a network: the Word, Excel and PowerPoint `python_code` tools, `generate_diagram_and_validate`, `analyze_spreadsheet` and skill scripts all work on files copied in through the API (the Office egress assessment found the same). The VPC has two `PRIVATE_ISOLATED` subnets (no gateway, NAT, endpoint or route), a security group with `allowAllOutbound: false`, and **DNS support off**. Security groups don't filter the VPC's DNS resolver, and with it on, public names resolved from the spike (§11.1), so a lookup could carry data out. Turning DNS off needs a VPC of its own, because app-api's tasks need DNS. That's simpler than a DNS Firewall and leaves nothing to allowlist. Net +8 resources: the VPC, 2 subnets, 2 route tables, 2 associations, the security group and CDK's default-SG restriction, less the two old interpreters |
| **D9** (accepted 2026-11-22, after §11.2) | Can `run_code` have LibreOffice, so the xlsx skill's `recalc.py` and docx/pptx → PDF run as written? | **Yes: a LibreOffice build on an EFS file system, mounted read-only at `/mnt/office` when `run_code` starts a session, with `/mnt/office/current/program` first on the run's `PATH`, and the release's pinned Node packages first on its `NODE_PATH` (`/mnt/office/current/node/node_modules`), so the docx skill's `require('docx')` (docx-js) resolves.** Code Interpreter has no custom images, but `StartCodeInterpreterSession` can mount an EFS access point in `VPC` mode. AgentCore mounts EFS by its DNS name, so this **reverses D8's DNS-off**: DNS support and hostnames come back on, and a Route 53 Resolver DNS Firewall answers the file system's exact name and NXDOMAIN for everything else (no wildcard, so no `<data>.allowed-name` channel; it fails closed). The firewall exists wherever the code sandbox does, DNS on or off, so it is associated before a later deploy turns DNS on; CloudFormation updates the VPC before creating anything that references it, so turning both on in one deploy would leave about a minute of public DNS. Only a separate seeding interpreter on its own role may write the file system (`backend/scripts/seed_code_sandbox_office.py`). The Node packages are installed on the operator's machine by `npm ci --ignore-scripts --omit=optional` from a committed lockfile (`backend/scripts/code_sandbox_office_node/`: exact pins, every package a hashed npm-registry tarball), and only JavaScript and WebAssembly are accepted: a native addon, `binding.gyp` or install script is refused, since the sandbox is aarch64 with no network to build or fetch one. The pptx skill's packages are there too (#2316): `pptxgenjs` is pure JavaScript, and its icon recipe's `sharp` (a native libvips addon) loads its WebAssembly build, `@img/sharp-wasm32` (libvips with resvg), because its native platform packages are optional and never installed; its unbuilt C++ sources are removed. A failed mount falls back to a session without it. Behind `CODE_SANDBOX_OFFICE_ENABLED` / `CDK_CODE_SANDBOX_OFFICE_ENABLED` (in development, needs the code sandbox). Nothing reaches the prompt or `toolConfig`. About +14 resources while on, +4 (the firewall) with the code sandbox alone |
| **D10** (§8.4) | Where do trust records live, and who can revoke them? | **Rows beside the skill in the AppRoles table, keyed `(skill, scope, scriptsHash)`, where the scope is the assistant (an Agent or project) or, for direct shares, the user. There's also one switch row per skill.** The scope is deliberately wider than `(session, user)`, because one review covers every member, and nothing is keyed on a session. The rows are read once per script call, inside the tool, and never cached on an agent, a tool or a process, because a revocation changes no configuration the agent cache keys on. Revoking: the binder, the Agent's owner, or the project's owner or an editor, for one binding; the author, for their skill everywhere; an admin, for any skill everywhere, by version or wholesale |
| **D11** (§8.9) | The admin policy surface | **Deploy config, default `owner`.** `SKILL_SCRIPT_TRUST_POLICY=admin` makes every `reviewed` or `accepted` script set wait for an admin's approval of its hash. `SKILL_IMPORT_GITHUB_ORGS` limits imports with scripts to listed GitHub owners and refuses zips with scripts. These are fixed per environment like the feature flags, with no admin page to build and no stored setting for the Runtime to read. A deployment that wants to change them redeploys |
| **D12** (§8.11) | What is the model told? | **Nothing in the system prompt or `toolConfig`.** Refusals are tool results naming what didn't run, why and who can fix it, and they tell the model not to retype the script. A successful script run's result gains one `Script version:` line (~8 tokens) for the rail and for the model to cite. The trust path and the people involved stay off the content blocks |
| **D13** (§9) | Audit | **`skill.script_run` gains `scriptsHash`, `trustedBy`, `reviewedBy`, `scopeId`, `version` and a `refused` outcome with its reason. New: `skill.scripts_reviewed`, `skill.scripts_review_withdrawn` and `skill.scripts_switched`.** The import acknowledgement is `skill.imported`'s `importSource`, which now carries the scripts and their hash |
| **D14** (§8, §12) | Do the Word tools share the gate? | **Yes, with a backfill.** Today `create_word_document` and `modify_word_document` run any bound skill's script for anyone, and they're on in production, so two rules for the same file would be the gap. A backfill pins every existing binding of a user skill with scripts and records a review by the binding's owner at its current hash (`method: backfill`), so live Agents keep working on the deploy that adds the gate. The Word tools then spend one release in **shadow**: they compute the verdict and write it to `skill.script_run` (`verdict`, `wouldRefuse`), and run anyway. The next release enforces it, once every `wouldRefuse` in the shadow is explained. There's no flag: the shadow release is the undo window, and a revert of one small PR is the undo after it. `run_code` is off in production, so it enforces from the start |
| **D15** (§8.6) | What do members get beyond the badge and the rail? | **Clear refusals and a report path, and no once-per-member note or per-skill opt-out in v1.** A note would repeat the badge. An opt-out would add a read to every run for a choice better made by leaving the Agent, or by reporting it, which reaches the people who can act. Reports reuse the marketplace's report storage and queue, with a `script` reason and the skill and hash attached |
| **D16** (§8.2) | What does the trust hash cover? | **The `scripts/` set only: every `script` manifest entry's path and sha256.** The instructions and references already steer the model's own `code`, which runs for everyone without review, so hashing them would ask for a review on every edit to the instructions and train owners to click through. The sheet lists the files each script reads, so the reviewer sees them |
| **D17** (§8.5, added 2026-11-23) | Does an owner binding their own hand-written skill see the review sheet? | **No. The sheet shows only for scripts the binder didn't write by hand: an imported skill, or, with direct sharing, someone else's.** Today the binder is always the author, so for a hand-written skill the sheet would ask people to confirm their own code to themselves. That's a dialog they'd learn to dismiss, and it would teach them to dismiss the one that matters. Where the sheet earns its place is an import: the `pdf` skill from GitHub is code the binder never wrote, about to run for everyone in the project. A hand-written binding still records a review (`method: authored`) and still pins, so members, the badge ("Shared by"), audit and revocation work the same. The test is `import_source`, which a later import sets and a hand edit never clears, so a skill with imported scripts never loses the sheet |

## 11. Dev checks (run 2026-11-20)

These ran in dev's `SANDBOX` skill-script interpreter, with five sessions in all, each stopped afterwards. The Office egress assessment from the same day needs checks 2 and 5 too.

1. **Command-line tools.**
   - Present: poppler (`pdftoppm`, `pdftotext`, `pdfimages`, `pdfinfo`) and `node`.
   - Missing: `qpdf`, `pdftk`, `gs`, `tesseract`, `soffice` and `pandoc`.
   - Python is 3.12.13.
   - The PDF skill's `qpdf` and `pdftk` examples won't run, and neither will its OCR recipe (no `tesseract`, no `pytesseract`). `pypdf` covers merge, split, rotate and decryption.
2. **Network.** Public names didn't resolve and public addresses were unreachable, but `SANDBOX` mode allows S3 access, as AWS documents. That is why the code sandbox uses `VPC` mode with no route out (D8, §11.1).
3. **Working directory.**
   - It's `/opt/amazon/genesis1p-tools/var`, which is also `$HOME`, with about 9 GB free.
   - `writeFiles` paths are relative to it, and subfolders are created on write.
   - Payloads of 1, 4, 8 and 16 MB all wrote, the largest taking 8.5 s.
4. **Start-up time.** A session start took 0.84–1.18 s, and its first `executeCode` 0.21–0.35 s, across three sessions.
5. **Imports.**
   - All load with no network: `pypdf` 6.2.0, `PyPDF2` 3.0.1, `pdfplumber` 0.11.0, `pdfminer`, `pdf2image`, `pypdfium2`, `reportlab` 4.2.0, `fpdf` 1.7.2, `PIL` 10.3.0, `pandas` 2.3.1, `docx` 1.1.2, `openpyxl` 3.1.3, `pptx` 1.0.2, `lxml` 5.2.2, `matplotlib` 3.9.0.
   - Missing: `pytesseract` and `fitz` (PyMuPDF).

**The PDF skill's scripts, run the way §4.2 would run them.** `check_fillable_fields.py`, `extract_form_field_info.py` and `convert_pdf_to_images.py` ran unchanged against a two-page form generated with `reportlab`, through `runpy` with `sys.argv` set. All exited 0 and wrote `fields.json` and two page PNGs, and `pypdfium2` also rendered a page. A sibling import (`fill_fillable_fields.py` importing `extract_form_field_info`) wasn't exercised.

### 11.1 Spike: a `VPC`-mode interpreter with no egress (2026-11-20)

The spike was a throwaway VPC on dev (`10.250.0.0/24`): two subnets with no route out, a security group with its egress rules revoked, and an interpreter in `VPC` mode on the existing logs-only role. All of it was deleted afterwards. The probe was §11's, plus `pdf2image` rendering a page and the Office imports.

| Probe | DNS support on | DNS support off |
|---|---|---|
| Session start, first call | 1.16 s | 1.02 s, then 0.2 s for a trivial call |
| `example.com`, `pypi.org` | **Resolve** (through the VPC resolver) | Fail |
| AWS names (`s3`, `sts`) | Resolve | Fail |
| TCP to `1.1.1.1`, `8.8.8.8`, S3 IPs | Time out | Time out |
| HTTPS to S3 | Times out | Fails (no name) |
| `169.254.169.254` | Answers 401 | Answers 401 |
| `169.254.170.2` | No route | No route |
| `pdf2image` render, Office and PDF imports | Work | Work |

**Reading.** With DNS on, nothing connects, but lookups of public names leave the sandbox, which is a way out for data. With DNS off, nothing gets out, and the workload is unaffected. The probe call itself took 162 s with DNS off, because each lookup waits out its timeout. Code that does lookups is slow to fail rather than fast; none of the tools' code does any.

The instance metadata address answers 401 in every mode. It's AWS's host, not our network; its IMDSv2 token was not requested. It's worth asking AWS what it would hand out.

### 11.2 Spike: LibreOffice from a read-only EFS mount (2026-11-21)

Throwaway resources on dev (their own role, security groups and interpreters; the real ones untouched), all deleted afterwards.

| Check | Result |
|---|---|
| Mount in the sandbox VPC as built by D8 (DNS off) | **Fails at session start:** `EFS mount failed: resource not found`. Same with DNS support on and hostnames off |
| Why | AgentCore mounts through the file system's DNS name (a local TLS tunnel at `127.0.0.1`, NFS 4.1). The name exists only with `enableDnsSupport` **and** `enableDnsHostnames` on, and only for mount targets created after hostnames are on |
| DNS on, behind a DNS Firewall allowing only `fs-….efs.<region>.amazonaws.com` | Mount works. `example.com`, S3, STS, `compute.internal` and even the AZ-prefixed form of the file system's name all NXDOMAIN in ~10 ms (DNS-off waits 162 s per lookup) |
| Read-only | `touch` / `mkdir` anywhere under `/mnt/office` → `Read-only file system` |
| Raw network from a mounted session | Only TCP 2049 to the mount target connects; public addresses, S3 and port 53 over TCP and UDP time out |
| Session start | 1.4–1.7 s without the mount, 2.5–2.6 s with it (the first two mounted starts: 6.9 s and 4.2 s) |
| LibreOffice 26.8.3 aarch64 (official RPMs; `libXinerama` and `libXrandr` from AL2023 added to `program/`) | `soffice --version` 1.2 s cold. docx → PDF 4.0 s first, 1.2 s after; pptx → PDF 1.4 s |
| The xlsx skill's `recalc.py`, unchanged, with only `PATH` set | 2.6 s, values cached, a planted `#DIV/0!` reported, and a real `#NAME?` the test file had by accident (`TEXTJOIN` written without `_xlfn.`) |
| Seeding through a write-access-point session | `writeFiles` in 24 MB chunks plus `tar -x` onto EFS: about 5 minutes |
| docx-js 9.16.0 in the same release (#2302, seeded on dev 2026-11-22) | `node/node_modules` adds 24.1 MB in 233 files (`docx` is 20 MB of it), 558 → 582 MB in all. The whole seed took 352 s, 191 s of it the unpack onto EFS, with a cold download cache. On the seeding session, `require('docx')` from `/mnt/office/current/node/node_modules` wrote a 9,454-byte document |
| pptx packages in the same release (#2316, seeded on dev 2026-11-22) | `node/node_modules` grows from 24.1 to 138.5 MB in 949 files (`react-icons` is 85 MB in 143 of them, `@img/sharp-wasm32` 9 MB), 582 → 696 MB in all, a 272 MB archive. The whole seed took 506 s, 198 s of it the unpack onto EFS. On the seeding session, from `current`, `sharp` loaded its WebAssembly build and rasterized a react-icons SVG to a 256 px PNG, and `pptxgenjs` wrote a 40-part deck holding it. Locally, two icons rasterized in 154 ms after a 269 ms module load |

## 12. PR plan

0. **PR-0, isolation (D8). Merged (#2245), validated on dev 2026-11-21.** On dev, the deployed interpreter was in `VPC` mode with DNS off and only the VPC's local route. DNS lookups and TCP connections from it all failed. Word, Excel, PowerPoint, the diagram tool, spreadsheet analysis and the Contract Redline skill script all ran on it (17 real turns). Production verification follows the next release.
   - `code-interpreter-construct.ts` now has one `VPC`-mode interpreter (`{prefix}_code_sandbox`) in its own VPC (`10.255.0.0/24`, isolated subnets, a security group with no egress, DNS off).
   - `AGENTCORE_CODE_INTERPRETER_ID` and the existing skill-script SSM parameter both name it, so no backend change is needed. The `PUBLIC` and `SANDBOX` interpreters are deleted by the same deploy, in CloudFormation's cleanup phase, after the Runtime has been updated to the new id.
   - The Runtime's separate grant on the skill-script interpreter is gone, and `CodeInterpreterAccess` names the new one. `AiPathAlarms` follows the new id.
   - Pinned by `infrastructure/test/code-sandbox-interpreter.test.ts`.
   - A turn in flight while the deploy switches interpreters may lose one tool call.
   - The new VPC counts against the account's VPC quota (default 5 per region).
   - **The network is retained on delete.** After the spike's interpreter was deleted, its two AgentCore-managed interfaces (`InterfaceType: agentic_ai`) stayed `in-use` for about 100 minutes, so the security group, subnets and VPC could not be deleted. A rollback of this deploy would hit the same failure and leave the stack in `UPDATE_ROLLBACK_FAILED`. With `Retain`, a rollback leaves the free network resources behind instead, to delete by hand once AWS releases the interfaces.
1. **PR-1, backend, dark (#2252). Merged (#2254), validated on dev 2026-11-21; Node scripts and `language: "javascript"` followed in #2312.**
   - `run_code` behind `CODE_SANDBOX_ENABLED`: script and code modes, staging, output collection, images.
   - Scripts from a skill the user doesn't own are refused until PR-2b (§4.2).
   - The storage-quota check in `_store_document` (§4.3).
   - Not switched on in production until #2245 is deployed and verified there.
   - The catalog row and the third `read_skill_file` notice.
   - CDK config, the `platform.yml` variable and the CDK test.
   - Tests:
     - the spec digest, pinned
     - a refusal when the interpreter id can't be read
     - owner-scoped file resolution
     - sibling imports and `argv`
     - each cap
     - the `(session, user)` key and turn-end stop
2. **PR-2, trust at adoption (§8, D4, D5, D10–D17).** This replaces the approval card. PR-2b to PR-2e and PR-2f's switch come before production; the admin policy and the allowlist can follow it.
   - **PR-2a, this spec revision** (docs only).
   - **PR-2b, the verdict (backend, Runtime).**
     - `apis/shared/skills/script_trust.py` holds the script-set hash (§8.2), the row shapes (§8.4) and one verdict function, `script_verdict(record, invoker_id, scope, rows) -> Verdict`. It's pure, so the Runtime and app-api share it, and a test reads the rows for it.
     - `run_code` replaces `_may_run_scripts_of` with it: one `BatchGetItem` per script call, the staged-bytes check, the `Script version:` line, and the §8.7 refusals.
     - The holder carries the turn's scope from the build.
     - The Word tools call the same function in **shadow** (D14).
     - `skill.script_run` gains §9's fields.
     - Tests:
       - every row of §8.1 and §8.7
       - a verdict never read from a cached agent (revoke between two calls on one cached agent, and the second call refuses)
       - the hash's canonical form, pinned
       - both tool spec digests unchanged
   - **PR-2c, recording reviews (backend, app-api).**
     - Agent create and update, project skills saves and Update accept `scriptsReviewed`, and write review rows and `skill.scripts_reviewed`.
     - A hand-written skill of the binder's own gets its `authored` review on save, with no `scriptsReviewed` needed (D17).
     - Agent auto-pin widens to skills with scripts.
     - `GET /skills/{skillId}/script-trust`; the author switch (`PUT /skills/mine/{skillId}/scripts`).
     - Import and refresh record `scriptsHash` and `scripts` in `import_source`, and the preview returns the script list.
     - Unbinding deletes the binding's review rows.
     - Nothing is required yet, so the SPA keeps working.
   - **PR-2d, the backfill** (`backend/scripts/script_trust_backfill.py`).
     - Every Agent and project binding of a user skill with scripts is pinned if it's live and gets a review by the binding's owner (`method: backfill`, `skill.scripts_reviewed`).
     - Missing `contentHash` values are filled in.
     - It's idempotent and has a `--dry-run` that lists what it would write. It's run on dev, then on production before the release that carries PR-2b.
   - **PR-2e, the SPA, and the 409 that needs it.**
     - The review sheet, its Update form and the binding-row notes (§8.5).
     - Agent Designer's Update control.
     - The badges on Agent details, project details and the marketplace listing.
     - The import step's scripts block.
     - The author switch.
     - The rail entry, with the fixture for the version line.
     - App-api starts answering `skill_scripts_unreviewed` in the same PR.
   - **PR-2f, admin and reports.**
     - The `admin.skill_scripts` scope and page (§8.10), with the switch.
     - `ReportReason.script` with `skillId` and `scriptsHash`, the rail's **Report this skill**, and the report route accepting a project harness.
     - Then `SKILL_SCRIPT_TRUST_POLICY=admin` with the queue, and `SKILL_IMPORT_GITHUB_ORGS` (§8.9), with the CDK config, the `platform.yml` variables, the CDK test and the docs-site rows. These two can land after production.
   - **PR-2g, the Word tools enforce (D14).** This lands one release after PR-2b, once that release's `skill.script_run` records hold no `wouldRefuse` that isn't explained.
3. **Dev validation.**
   - Run the PDF skill end to end: a fillable form, a non-fillable form with annotations, merge and split, and table extraction.
   - Repeat the §11 network checks.
   - Measure the spec's tokens and a form fill's total tokens against a run with scripts inert.
   - **Trust, with the persona stack** (an author and a member):
     - The author binds an imported skill with scripts to an Agent and to a project, and sees the sheet.
     - The author binds a hand-written skill with scripts and sees no sheet, only the row note. The member's badge reads "Shared by".
     - The member runs its scripts in a new conversation with no prompt, and sees the badge and the rail line.
     - The author edits a script. The member still runs the reviewed version (the same `Script version:`), and the author's Update shows the diff.
     - "Add without scripts" refuses `not_reviewed` for the member.
     - The author's switch, then an admin's switch, each refuse on the member's next call, with no new conversation.
     - A report lands in the admin queue with the skill named.
     - The Word tools in shadow write a `verdict` and run.
     - Check the audit records for each step.
4. **LibreOffice (D9, #2280). Merged (#2285), with docx-js (#2304) and the pptx packages (#2323), all validated on dev.**
   - `code-sandbox-office-construct.ts`: the DNS Firewall (with the code sandbox), and with `codeSandboxOffice` the EFS file system, its mount targets and two access points, the mount-target security group and NFS-only egress, the seeding interpreter and role, and the mount's SSM parameter. DNS support and hostnames on. Pinned by `infrastructure/test/code-sandbox-office.test.ts`.
   - `run_code` starts its session with the read-only mount (`code_sandbox/office_mount.py`), replacing a session `analyze_spreadsheet` started without it, and runs with LibreOffice first on `PATH`. A failed mount falls back to no mount, skipped for 5 minutes.
   - `backend/scripts/seed_code_sandbox_office.py` installs a pinned, checksummed build into a release folder of its own and switches `current` with one rename.
   - **docx-js (#2302).** The same release folder carries `node/node_modules`: `docx` from `backend/scripts/code_sandbox_office_node/` (exact pin, committed lockfile, `npm ci --ignore-scripts` on the operator's machine, JavaScript and WebAssembly only), checked on the seeding session by writing a document before `current` moves. `run_code` puts it first on `NODE_PATH` beside `PATH`, keeping whatever `NODE_PATH` the sandbox had after it. No flag of its own: it extends what `CODE_SANDBOX_OFFICE_ENABLED` already chose, nothing changes in the tool's spec, and an unseeded release only means `require('docx')` fails as it did before. Dev validation: the docx skill creates a document with docx-js in a real `run_code` turn, and `recalc.py` and `soffice` still work.
   - **pptx (#2316).** The same `node/node_modules` carries what Anthropic's pptx skill loads: `pptxgenjs` 4.0.1 (with the `jszip` its `scripts/apply_theme.js` resolves from beside it), and for its icon recipe `react-icons` 5.7.0, `react` and `react-dom` 19.3.1, and `sharp` 0.35.5 with `@img/sharp-wasm32` 0.35.5. Only the icon recipe needs `sharp` (render a react-icons SVG, rasterize it at 256 px, `addImage` the PNG); `pptxgenjs` itself never loads it. Without it the recipe's `require('sharp')` fails, the skill's `npm install` fallback has no network, and its likeliest workaround, `addImage` with SVG data, is worse than it looks: in Node pptxgenjs embeds the SVG with its built-in broken-image PNG as the fallback, which older viewers and possibly LibreOffice thumbnails show. `sharp` loads `@img/sharp-wasm32` whenever no native binary does, so the seed installs with `--omit=optional` (none of its ~25 native platform packages) and removes `sharp/src` (unbuilt C++ with a `binding.gyp`); the seed's check refuses a `sharp` that did not load the WebAssembly build (only it reports `emscripten` in `sharp.versions`), rasterizes a react-icons SVG and writes a deck holding it with `pptxgenjs`, on the operator's machine and on the seeding session before `current` moves. `image-size`, which `pptxgenjs` declares, has two open denial-of-service advisories, but `pptxgenjs` never requires it. `@img/sharp-wasm32` is Apache-2.0 and LGPL-3.0-or-later (libvips), redistributed unmodified. No flag of its own, for the same reasons as docx-js. Dev validation: the pptx skill builds a deck with a react-icons icon in a real `run_code` turn, and `scripts/apply_theme.js`, run by `skill_name` + `path`, writes its theme.
   - **Rollout:** deploy with the code sandbox on first (the firewall associates while DNS is still off), then turn `CDK_CODE_SANDBOX_OFFICE_ENABLED` on in a later deploy, then run the seeding script.
   - Dev validation: `recalc.py` and a docx → PDF in a real `run_code` turn; public names still NXDOMAIN from the sandbox; a write under `/mnt/office` still fails.
5. **`run_code` on in production.**
   - **Before:** PR-2b to PR-2e and PR-2f's switch are released. The backfill has run on production (`--dry-run` first, its output kept with the release notes). #2245's network checks (§11.1) are repeated against the production interpreter.
   - **Then:**
     - Set `CDK_CODE_SANDBOX_ENABLED=true` in the `production` GitHub environment.
     - Seed the `TOOL#run_code` catalog row and grant it to the roles that should have it. Its `enabledByDefault: false` means nobody gets it by accident.
     - Add a line to #1798, since the flag is then on in production while off by default, and say in that release's notes why it stays opt-in or that it's promoted.
   - **LibreOffice in production** (`CDK_CODE_SANDBOX_OFFICE_ENABLED`) follows in a later deploy and is seeded with `seed_code_sandbox_office.py`, per step 4's rollout.
   - **After:** read the release readout for `run_code`'s tool errors and payload, and the first week's `skill.script_run` refusals by reason.
