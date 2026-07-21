# Session workspace tools (`workspace_list` / `workspace_read` / `workspace_write`)

**Status:** PR-1 implemented (branch `feature/session-workspace-tools`, off `develop`)
**Inspired by:** the shared-workspace tool pattern in
`aws-samples/sample-strands-agent-with-agentcore` (adapted, not adopted — see
"Why not the reference implementation as-is")

## Problem

The agent can already work with files — but only through vertical slices, each
of which reimplements the same substrate:

- `word_document_tool.py` writes generated `.docx` to the user-files bucket,
  registers `FileMetadata`, and returns an inline download card.
- `spreadsheet_analysis/` lists tabular attachments from DynamoDB and analyzes
  them in the AgentCore Code Interpreter sandbox.
- `code_interpreter_diagram_tool.py` produces images.
- The SPA uploads attachments via presigned URLs
  (`apis/app_api/files/service.py`) into
  `user-files/{userId}/{sessionId}/{uploadId}/{filename}`.

What's missing is the **horizontal primitive**: a generic list/read/write
surface over the user's files that any tool, skill, or future harness lane can
compose with. Concretely, today the agent cannot:

- enumerate what the user has uploaded (except spreadsheets) or what other
  tools have produced this session;
- read a text/markdown/CSV attachment *on demand* mid-turn (attachments are
  push-only: inlined as content blocks on the user message by
  `multimodal/prompt_builder.py`);
- write a plain text/markdown/CSV/JSON deliverable the user can download
  (only `.docx` has a write path);
- chain tools through files (code-interpreter output → document tool →
  download) without each pair growing a bespoke bridge.

This is also a prerequisite primitive for the agentic-platform roadmap: skills
that produce files, the F1 headless/harness entrypoint, and any future
general-purpose code-interpreter tool all need a durable file surface between
ephemeral sandbox sessions.

## Why not the reference implementation as-is

The reference repo's `workspace_*` tools are raw S3 wrappers. Three conflicts
with this stack's conventions:

1. **They bypass the metadata layer.** In this stack the DynamoDB user-files
   table is the source of truth (SPA listing, status, quota, thumbnails, TTL).
   A raw `put_object` produces a file invisible to the Files UI and outside
   quota accounting; a raw `list_objects_v2` sees a different world than the
   SPA does. Every workspace operation must go through
   `apis.shared.files` (`FileMetadata` + `FileUploadRepository`), exactly like
   `word_document_tool._store_document` already does.
2. **Base64 file content through the model violates the token-cost tenet.**
   `workspace_read` returning base64 of a binary file is an unbounded per-turn
   payload — a 2 MB PPTX ≈ 2.7 MB of base64 in a tool result, at model prices,
   likely blowing context outright. Binary files move **by reference**
   (upload_id / presigned URL); byte processing belongs in the code-interpreter
   sandbox. Text reads must be bounded.
3. **`invocation_state.get('user_id', 'default_user')` is a cross-tenant bug
   waiting to happen.** A missing identity would silently collapse every
   affected session into one shared namespace. This stack injects identity by
   closure — `make_*_tool(session_id, user_id)` factories instantiated
   per-request in `apis/inference_api/chat/routes.py` — so identity is
   guaranteed present or the tool is never built. Same "fail loudly" posture
   as PR #706 (unconfigured bucket).

Also dropped: the parallel `code-agent-workspace/` / `documents/<type>/`
namespace scheme. That's an artifact of the reference repo's multiple
sub-agents. Here, the existing key layout plus a `source` attribute on the
metadata row carries the same information without a second key convention.

## Decision summary

| Question | Decision |
|----------|----------|
| Source of truth | **DynamoDB user-files table** — never raw S3 listing |
| Write path | Through `FileMetadata` + repository (the `_store_document` pattern), status `READY` |
| Read scope | **User-scoped** — all the user's `READY` files, any session |
| Write scope | **Session-scoped** — new files land under the current session's prefix |
| Binary handling | **By reference only** — metadata + presigned URL; no base64 through the model |
| Text read bound | `WORKSPACE_READ_MAX_BYTES` (default **48 KB**) per call, with `offset` for continuation |
| Identity | Closure-injected via `make_workspace_*_tool(session_id, user_id)` factories |
| Key layout | Existing `user-files/{userId}/{sessionId}/{uploadId}/{filename}` — no new namespaces |
| Provenance | New optional `source` field on `FileMetadata` (`"upload"` default, `"agent"`, `"word_document"`, …) |
| Quota | Agent-written files **count against** the user's existing quota; quota-exceeded is a friendly tool error |
| Overwrite semantics | **No in-place overwrite** — each write is a new `uploadId`; same-name writes supersede in listings |
| Lifecycle | Inherit existing 365-day DynamoDB TTL + matching S3 expiration — nothing new |
| Module home | Service in `apis/shared/files/workspace.py`; tools in `agents/builtin_tools/workspace_tools.py` |
| RBAC | One catalog entry (`workspace_files`) as the gate key provisioning all three tools (the `create_word_document` pattern) |
| Feature flag | `WORKSPACE_TOOLS_ENABLED`, **default true**, `=false` kill switch |
| UI | `workspace_write` returns the same inline download-card contract as the word tool |

## Tool surface

All three tools are `make_*` factories (closure identity), registered in
`ToolRegistry` alongside the word-document tools and instantiated per-request
in `apis/inference_api/chat/routes.py`. All returns are compact JSON strings;
errors return `{"error": ..., "status": "error"}` conversationally (never
raise through the agent loop).

### `workspace_list`

```
workspace_list(scope: str = "session") -> str
    scope: "session" (this conversation) | "user" (all conversations)
```

- Queries DynamoDB only: GSI1 (`CONV#{sessionId}`) for session scope, the
  `USER#{userId}` partition for user scope. Never touches S3.
- Filters to `status == READY`.
- Returns per file: `upload_id`, `filename`, `mime_type`, `size_bytes`,
  `source`, `session_id` (user scope only), `created_at`, and a `readable`
  bool (text-type per the MIME allowlist).
- Bounded output: cap at ~100 most-recent entries with a `truncated` flag —
  a tool result is a per-turn payload too.

### `workspace_read`

```
workspace_read(upload_id: str, offset: int = 0) -> str
```

- Looks up `FileMetadata` by `(user_id, upload_id)` — ownership is enforced by
  the table key shape (`PK = USER#{userId}`), so cross-user reads are
  impossible by construction. Cross-*session* reads are allowed (precedent:
  `word_document_tool._find_word_document` already reads across sessions).
- **Text MIME types** (`text/*`, `application/json`, csv/md/html): return
  UTF-8 content from `offset`, capped at `WORKSPACE_READ_MAX_BYTES` (48 KB
  default), with `truncated` + `next_offset` for continuation. Ranged S3 GET
  (`Range` header) so a 200 MB file never enters process memory.
- **Binary / everything else**: return metadata + a short-lived presigned GET
  URL and a hint naming the right tool for the bytes
  (`analyze_spreadsheet` for tabular, code interpreter for the rest). Never
  base64. PDFs and images already reach the model as content blocks via the
  attachment flow; this tool does not duplicate that path in v1.

### `workspace_write`

```
workspace_write(filename: str, content: str, mime_type: str = "text/plain") -> str
```

- **Text-only in v1.** `mime_type` must be in a text allowlist (plain,
  markdown, csv, json, html). Binary production stays with the dedicated
  tools (`create_word_document`, diagram tool); when a general
  code-interpreter tool lands, its sandbox→S3 sync becomes the binary write
  path and this tool stays as-is.
- Size-capped per call (`WORKSPACE_WRITE_MAX_BYTES`, default 1 MB — content
  the model just generated is inherently small).
- Follows `_store_document` exactly: quota check → `put_object` under
  `user-files/{userId}/{sessionId}/{uploadId}/{filename}` → `FileMetadata`
  row (`source="agent"`, `status=READY`) → `increment_quota` → presigned
  download URL.
- Returns the inline download-card contract
  (`ui_type` / `ui_display: "inline"` / payload) so the SPA renders a
  first-class download card. Reuses the shared `file_download` `ui_type`
  already routed to `FileDownloadRendererComponent` in
  `inline-visual.component.ts`, so no frontend change is needed.
- Filename validation mirrors the word tool's (`_validate_document_name`
  generalized): no path separators, no traversal, extension must match
  `mime_type`.

## Design notes

### Read scope: why user-scoped

`FileMetadata`'s native key shape is user-partitioned (`PK = USER#{userId}`,
GSI1 by session), so "all my files" is the cheap query, not the expensive one.
The compelling UX is *"use the CSV I uploaded yesterday"* — without
cross-session read, users must re-upload per conversation. The word tool
already crossed this line quietly; this spec just makes it the stated policy.
Writes stay session-scoped so provenance ("this file came from that
conversation") stays intact and the S3 key layout is unchanged.

### Quota: why agent files count

Simplest and safest: same repository path as uploads, no second accounting
regime, and it bounds a runaway agent loop writing files. The write cap plus
quota makes the worst case boring. If agent-generated deliverables ever crowd
out upload quota in practice, carve-out is a follow-up, not a v1 concern.

### Token-cost audit (per CLAUDE.md tenet)

- Nothing added to the cacheable prefix beyond three small static tool
  definitions in `toolConfig` (deterministic ordering via the existing
  registry path).
- Per-turn payloads are all bounded: list ≤ ~100 rows, read ≤ 48 KB/call,
  write returns a small card. No unbounded pass-through anywhere.
- The pull model is a net token *saving* vs. today's push model for large
  text attachments: the model reads the 5 KB it needs instead of receiving
  the whole document as a content block on every restored turn.

### Attachment-flow interaction (explicitly out of scope for v1)

Today `prompt_builder.py` inlines every attachment as content blocks. Once
`workspace_read` exists, a future change could stop inlining large text files
and inject a one-line pointer ("attached: `report.md`, upload_id …") instead —
a meaningful prompt-size win, but it changes restored-history bytes and
therefore interacts with the prompt-cache byte-stability contract. Do it as
its own PR with `cacheStatus` verification, not as a rider on this one.

## Security

- **Tenant isolation by construction:** every repository call is keyed by the
  closure-injected `user_id`; there is no path where model-supplied input
  selects the partition.
- **No model-supplied S3 keys:** tools accept `upload_id` / `filename` only;
  S3 keys are always derived server-side.
- **Filename sanitization** on write (no separators/traversal, extension ↔
  MIME agreement).
- **Presigned URLs** are short-TTL GET-only, same posture as the existing
  preview-url endpoint.
- Governance posture per platform norm: identity-claims gating, no content
  inspection.

## Phasing

**PR-1 — service + tools (backend) + download card (frontend).**
`apis/shared/files/workspace.py` (bounded ranged read, list queries, write
helper extracted/shared with `_store_document`), `source` field on
`FileMetadata` (additive, default `"upload"` on read), the three tool
factories + registry/catalog/RBAC wiring, `file_download` inline card,
tests (unit + import-boundary clean).

**PR-2 (optional, later) — word tool convergence.** Reimplement
`_store_document` and `_find_word_document` on the workspace service so
there is one write path. Pure refactor, no behavior change.

**Deferred:** attachment-flow pull-model change (cache-sensitive, own PR);
binary write via code-interpreter sandbox sync; SPA "Files" page surfacing
`source` provenance.

## Open questions

1. Does the SPA Files page need any change for v1 beyond the download card?
   (Agent-written files will simply appear in the existing list; showing a
   "generated" badge off `source` is nice-to-have.)
2. Should `workspace_read` serve image files as proper image content blocks
   (size-gated) instead of URL-by-reference? Useful for "look at the diagram
   you made", but content-block emission from a tool result needs Strands
   plumbing verification first.
3. RBAC granularity: one `workspace_files` catalog entry vs. three. One entry is
   recommended (they're useless separately), but confirm the admin-tools page
   renders a multi-tool entry cleanly.
