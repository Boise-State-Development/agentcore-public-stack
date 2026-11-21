# Skill code sandbox: running a skill's scripts, and the code its instructions ask for, with no network

**Status:** Approved 2026-11-20, with every §9 recommendation accepted. **PR-0 (D8) is merged (#2245) and validated on dev (2026-11-21):** one `VPC`-mode interpreter in an isolated VPC replaced both the `PUBLIC` and `SANDBOX` ones. Every Code Interpreter tool ran on it with every network probe failing. Production verification waits for the next release. **PR-1 (`run_code`) is in progress (#2252).**
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
| Preinstalled libraries | AWS's list for Code Interpreter | `pypdf`, `PyPDF2`, `pdfplumber`, `pdfminer.six`, `pdf2image`, `pypdfium2`, `reportlab`, `fpdf`, `pillow`, `pandas`, `python-docx`, `python-pptx`, `openpyxl`, `lxml`, and others. **Not listed:** `qpdf`, `pdftk`, and the poppler command-line tools that `pdf2image` calls (§10) |

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
| Sending a staged document out (to the internet or S3) | An interpreter in `VPC` mode in subnets with no route out and a security group with no egress rules, in a VPC with DNS support off (D8). No fallback; the tool refuses if the interpreter id cannot be resolved | Rests on the VPC configuration, which dev validation proved with raw-socket and DNS probes (§10.1, §11) |
| Reaching AWS data | Logs-only execution role | None known |
| Another user's or another conversation's files | Files resolved only from the caller's own conversation, matched on `user_id`; references go through `readable_view`. One sandbox per `(session, user)`, never per session alone, and stopped at turn end | None known |
| A script by someone else (a shared Agent's or project's skill) that corrupts outputs or misreports what it did | Outputs are always new files; originals are never overwritten. Approval card on the first run of each `(skill, path, sha256)` in a conversation (D4) | A user who approves a bad script gets bad output. They can't lose data they hadn't staged |
| Prompt injection in a staged document steering the model's code | Same boundary: only staged files and `outputs/` are reachable. Printed output and images back to the model are capped | Same as `document_read` today. What the model reads can steer its other tools; this tool adds no new path out |
| Runaway compute or output | Per-call timeout, session timeout, caps on output, files and images, at most `N` calls per turn, and an RBAC grant | Code Interpreter billing for the time it runs |
| Escaping the sandbox to the host | AWS's microVM boundary. Nothing is imported, compiled or run on the Runtime | Rests on AWS |

## 4. The tool

One tool, `run_code`, gated by its own catalog row (§7). One tool rather than two because the skill's workflow interleaves the two kinds: a script fills the form, then a snippet the model wrote checks it, in the same working directory.

```
run_code(
  code: str | None = None,          # Python the model wrote; exactly one of code / path
  skill_name: str | None = None,    # a skill bound to this turn (as read_skill_file takes it)
  path: str | None = None,          # a scripts/*.py file in that skill's manifest
  argv: list[str] = [],             # command-line arguments for the script
  files: list[str] = [],            # this conversation's files to copy into inputs/, by name or upload id
  show_images: list[str] = [],      # up to 4 image paths to return to the model
) -> result
```

### 4.1 The sandbox layout

The working directory holds three folders:

- **`inputs/`** has the files named in `files`, copied in once per turn and reused by later calls. Names are sanitized the way `_safe_document_name` does it.
- **`skills/<slug>/`** has the **whole bundle** of each bound skill a call names, written once per turn: `scripts/`, `references/` and `assets/`, as the manifest lists them. Scripts load their own references and assets, so all three are needed.
- **`outputs/`** is collected after every call (§4.3).

Files persist between calls in the same turn, because the sandbox does. The next turn starts a fresh sandbox, and the model re-stages what it needs, including outputs from the earlier turn, which are by then conversation files (D3).

### 4.2 Running a script

The tool resolves `skill_name` + `path` through `resolve_skill_script` exactly as the Word tools do: bound skills only, strict manifest lookup, kind `script`, `.py`, at most 1 MiB. A fixed entry then runs it:

```python
import runpy, sys
sys.argv = [<script path>, *argv]
sys.path.insert(0, <the script's folder>)      # so sibling imports work
try:
    runpy.run_path(<script path>, run_name="__main__")
except SystemExit as e:
    ...                                         # exit code comes back as the call's status
```

`argv` is passed as data in a JSON file, never pasted into the entry's source, the same way the Word tools pass `params`. It is capped at 4 KB.

The script runs from the bundle that was copied in, not from a re-fetch. The approval card's hash (D4) is the hash of those exact bytes.

**Until PR-2 lands, a script from a skill the user doesn't own is refused.** The refusal says so plainly and names the skill. The user's own skills' scripts and model-written `code` still run. That keeps PR-1 safe on its own: the integrity guard D4 adds is missing only for scripts PR-1 never runs.

**No network, stated up front.** With DNS off, code that does a lookup waits out the resolver's timeout instead of failing at once (162 s for the §10.1 probe). The per-call timeout caps that. The tool's description says the sandbox has no network and no package installs, and a run that ends on the timeout says the same, so the model doesn't retry a download.

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
- **No SPA flag.** Download cards, images in tool results and the approval card all exist already.
- **Catalog row `run_code`.** `enabledByDefault: false`, so a role has to be granted it. RBAC granularity is one `tool_id`, as with `request_user_login`.
- **`WORD_SKILL_SCRIPTS_ENABLED` stays as it is.** Its scripts use a Word-specific contract and need no change. Folding it into this tool later is a separate decision.

## 8. Audit

- A script run writes the existing `skill.script_run` record, adding the script's sha256 and whether it was approved.
- A `code` run writes a new `sandbox.code_run` record: ids, the code's length and hash, duration and exit status. No code text, no file content.

## 9. Decisions

All seven recommendations were accepted on 2026-11-20. D4 reverses the conformance plan's "out of plan by decision" for §6.6 item 3.

| # | Decision | Accepted |
|---|---|---|
| **D1** | Should `run_code` accept model-written `code`, or only skill scripts? | **Both.** The PDF skill needs both, in the same working directory. Without `code`, its snippets still get retyped into a tool that can't run them |
| **D2** | Which languages? | **Python only in v1**, as §6.6 item 2 says. Without network, shell and Node are no riskier than Python (Python can spawn processes), so adding `.js` and `.sh` later is a question of demand, not safety. Anthropic's `pptx` skill uses Node |
| **D3** | Should the sandbox last one turn or the whole conversation? | **One turn.** Outputs become conversation files, so a later turn re-stages them in a second or two. A conversation-long sandbox bills while the user thinks and needs its own reaper |
| **D4** | Consent for scripts by other authors (§6.6 item 3) | **An approval card on the first run of each `(skill, path, sha256)` in a conversation, for skills the invoker does not own.** Own skills run without one. Network isolation removes the exfiltration case, so the card guards integrity only. The conformance plan listed this as "out of plan by decision", so it needs a fresh yes |
| **D5** | Only pinned versions across a trust boundary (§6.6 item 4)? | **Defer.** With D4's hash in the consent, a changed script asks again. Pinning adds little on top and depends on how a project binds skills |
| **D6** | Should the tool be offered whenever granted, or only when a bound skill has scripts? | **Whenever granted.** Tying it to the skills on the turn would change `toolConfig` when a user switches a skill on mid-conversation, rewriting the cached prefix. A role grant is constant per deployment and user |
| **D7** | Could a skill ship pure-Python wheels for packages that aren't preinstalled? | **Not in v1.** A skill needing a missing package fails with a clear message naming it |
| **D8** (accepted 2026-11-20, added after §10) | How is the interpreter isolated, given that AWS documents `SANDBOX` as allowing S3 access? | **One interpreter in `VPC` mode in its own VPC, replacing both the `PUBLIC` and `SANDBOX` ones.** No caller needs a network: the Word, Excel and PowerPoint `python_code` tools, `generate_diagram_and_validate`, `analyze_spreadsheet` and skill scripts all work on files copied in through the API (the Office egress assessment found the same). The VPC has two `PRIVATE_ISOLATED` subnets (no gateway, NAT, endpoint or route), a security group with `allowAllOutbound: false`, and **DNS support off**. Security groups don't filter the VPC's DNS resolver, and with it on, public names resolved from the spike (§10.1), so a lookup could carry data out. Turning DNS off needs a VPC of its own, because app-api's tasks need DNS. That's simpler than a DNS Firewall and leaves nothing to allowlist. Net +8 resources: the VPC, 2 subnets, 2 route tables, 2 associations, the security group and CDK's default-SG restriction, less the two old interpreters |

## 10. Dev checks (run 2026-11-20)

These ran in dev's `SANDBOX` skill-script interpreter, with five sessions in all, each stopped afterwards. The Office egress assessment from the same day needs checks 2 and 5 too.

1. **Command-line tools.**
   - Present: poppler (`pdftoppm`, `pdftotext`, `pdfimages`, `pdfinfo`) and `node`.
   - Missing: `qpdf`, `pdftk`, `gs`, `tesseract`, `soffice` and `pandoc`.
   - Python is 3.12.13.
   - The PDF skill's `qpdf` and `pdftk` examples won't run, and neither will its OCR recipe (no `tesseract`, no `pytesseract`). `pypdf` covers merge, split, rotate and decryption.
2. **Network.** Public names didn't resolve and public addresses were unreachable, but `SANDBOX` mode allows S3 access, as AWS documents. That is why the code sandbox uses `VPC` mode with no route out (D8, §10.1).
3. **Working directory.**
   - It's `/opt/amazon/genesis1p-tools/var`, which is also `$HOME`, with about 9 GB free.
   - `writeFiles` paths are relative to it, and subfolders are created on write.
   - Payloads of 1, 4, 8 and 16 MB all wrote, the largest taking 8.5 s.
4. **Start-up time.** A session start took 0.84–1.18 s, and its first `executeCode` 0.21–0.35 s, across three sessions.
5. **Imports.**
   - All load with no network: `pypdf` 6.2.0, `PyPDF2` 3.0.1, `pdfplumber` 0.11.0, `pdfminer`, `pdf2image`, `pypdfium2`, `reportlab` 4.2.0, `fpdf` 1.7.2, `PIL` 10.3.0, `pandas` 2.3.1, `docx` 1.1.2, `openpyxl` 3.1.3, `pptx` 1.0.2, `lxml` 5.2.2, `matplotlib` 3.9.0.
   - Missing: `pytesseract` and `fitz` (PyMuPDF).

**The PDF skill's scripts, run the way §4.2 would run them.** `check_fillable_fields.py`, `extract_form_field_info.py` and `convert_pdf_to_images.py` ran unchanged against a two-page form generated with `reportlab`, through `runpy` with `sys.argv` set. All exited 0 and wrote `fields.json` and two page PNGs, and `pypdfium2` also rendered a page. A sibling import (`fill_fillable_fields.py` importing `extract_form_field_info`) wasn't exercised.

### 10.1 Spike: a `VPC`-mode interpreter with no egress (2026-11-20)

The spike was a throwaway VPC on dev (`10.250.0.0/24`): two subnets with no route out, a security group with its egress rules revoked, and an interpreter in `VPC` mode on the existing logs-only role. All of it was deleted afterwards. The probe was §10's, plus `pdf2image` rendering a page and the Office imports.

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

## 11. PR plan

0. **PR-0, isolation (D8). Merged (#2245), validated on dev 2026-11-21.** On dev, the deployed interpreter was in `VPC` mode with DNS off and only the VPC's local route. DNS lookups and TCP connections from it all failed. Word, Excel, PowerPoint, the diagram tool, spreadsheet analysis and the Contract Redline skill script all ran on it (17 real turns). Production verification follows the next release.
   - `code-interpreter-construct.ts` now has one `VPC`-mode interpreter (`{prefix}_code_sandbox`) in its own VPC (`10.255.0.0/24`, isolated subnets, a security group with no egress, DNS off).
   - `AGENTCORE_CODE_INTERPRETER_ID` and the existing skill-script SSM parameter both name it, so no backend change is needed. The `PUBLIC` and `SANDBOX` interpreters are deleted by the same deploy, in CloudFormation's cleanup phase, after the Runtime has been updated to the new id.
   - The Runtime's separate grant on the skill-script interpreter is gone, and `CodeInterpreterAccess` names the new one. `AiPathAlarms` follows the new id.
   - Pinned by `infrastructure/test/code-sandbox-interpreter.test.ts`.
   - A turn in flight while the deploy switches interpreters may lose one tool call.
   - The new VPC counts against the account's VPC quota (default 5 per region).
   - **The network is retained on delete.** After the spike's interpreter was deleted, its two AgentCore-managed interfaces (`InterfaceType: agentic_ai`) stayed `in-use` for about 100 minutes, so the security group, subnets and VPC could not be deleted. A rollback of this deploy would hit the same failure and leave the stack in `UPDATE_ROLLBACK_FAILED`. With `Retain`, a rollback leaves the free network resources behind instead, to delete by hand once AWS releases the interfaces.
1. **PR-1, backend, dark (#2252).**
   - `run_code` behind `CODE_SANDBOX_ENABLED`: script and code modes, staging, output collection, images.
   - Scripts from a skill the user doesn't own are refused until PR-2 (§4.2).
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
2. **PR-2, consent (D4).** The approval card on the first run of each `(skill, path, sha256)` for skills the invoker does not own, using `tool_approval.py`.
3. **Dev validation.**
   - Run the PDF skill end to end: a fillable form, a non-fillable form with annotations, merge and split, and table extraction.
   - Repeat the §10 network checks.
   - Measure the spec's tokens and a form fill's total tokens against a run with scripts inert.
