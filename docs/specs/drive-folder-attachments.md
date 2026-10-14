# Attach a Google Drive folder to a conversation

**Status:** PR-A, PR-B and PR-C shipped to dev (2026-10-14); PR-D (Refresh) in review. **Depends on:** the composer `+` menu and `POST /files/import` (`composer-add-menu.md`), large-document attachments PR-1 and PR-2 (`large-document-attachments.md`), and, for search, the attachments KB (PR-3, PR-4). **Flag:** `FOLDER_ATTACHMENTS_ENABLED` / `CDK_FOLDER_ATTACHMENTS_ENABLED`, `features.folderAttachments`, in development, default off.

## 1. Problem

Claude's desktop app lets a person point a conversation at a folder and work from it: list it, open what matters, nothing enters the prompt until it is read. Users here ask for the same with Google Drive: "here is the course folder, find the rubric and the three late submissions", "summarise what changed across these contracts".

Today a conversation takes at most five files per message, each one either inline (a Bedrock `document` block, up to 4 MB) or, over 4 MB and behind a flag, by reference. A folder is the wrong shape for both rules. Forty 300 KB PDFs would be forty inline document blocks, re-sent on every turn as part of the cached prefix and far past the inline byte budget; and five at a time means the user is the retrieval system.

The platform already has the half that makes folders cheap: `document_read` (listing, pattern, page ranges over `_derived/` sidecars), the preparation Lambda, and the attachments KB with `document_read`'s `query` mode. What is missing is a way to bring a folder's files in **by reference regardless of size**, as one attachment, with a manifest the model can plan from.

## 2. What already exists

| Piece | Where | Reused as |
|---|---|---|
| Drive browser with folder navigation and a `pick-folder` mode | `FileSourceBrowserDialogComponent` (export destination picker) | The folder picker, in a new `attach-folder` mode |
| `GET /connectors/{id}/browse` with cursor paging, `downloadMimeType` on entries | `file_sources/routes.py` | The server-side listing of the folder |
| `POST /files/import` → `FileUploadService.import_file` | `files/routes.py`, `files/service.py` | The per-file import, called by the expansion task rather than the SPA |
| `is_reference_document`, the prepare queue and Lambda, `_derived/` sidecars, digest | `apis/shared/files/large_document.py`, `lambdas/large-document-prepare` | Every folder file is prepared this way, whatever its size (§4.3) |
| `document_read` (list, pattern, page_range, `query` with the KB) | `agents/builtin_tools/document_read_tool.py` | How the model reads folder files; the listing gains the folder path |
| `render_reference` / `<document-reference>` stub, persisted once per turn | `apis/shared/files/reference_attachments.py` | The folder manifest follows the same contract (§4.4) |
| Attachments KB ingest, per-user cap, 30-day eviction, re-index on open | `apis/shared/files/attachment_index.py` | Folder files are eligible like any prepared document |
| Metadata-first change detection for Drive (`modifiedTime`, `md5Checksum`) | `kb_sync/worker.py` §6.1 | Refresh (§4.6) |

## 3. Options considered

| Option | Verdict |
|---|---|
| **Expand the folder into ordinary attachments** (N files on the message) | No. Inline delivery for small files is the cost problem; the 5-file cap would have to go; the message would carry N ids and the resolver N reads. |
| **Upload the folder into an agent or project knowledge base** | Already possible for an agent's KB. Wrong lifetime and ownership for a conversation ("this folder, this chat"), and the user has to build an agent first. Keep as the "make it permanent" follow-up (§8). |
| **Folder by reference: one folder attachment, files prepared and read on demand** | **Chosen.** One attachment on the message, one bounded manifest in the prompt, every byte read through `document_read`, optional KB search. |
| **Local folders through the browser** (`webkitdirectory`, File System Access API) | No. Still an upload of every byte, no Drive identity, no refresh. Left to the desktop app. |

## 4. Design

### Principle

**A folder is one attachment. Its files are session files delivered by reference, never inline, whatever their size. The prompt gets a manifest; the bytes are read on demand.**

### 4.1 Picking a folder

The `+` menu's **Add from {Drive connector}** opens the browser in `attach` mode as today. In that mode the footer gains **Attach this folder** beside **Attach** once the user is inside a folder or a root (the `pick-folder` mode's `canPickCurrentFolder` rule). The dialog closes with `{connector, folder: {folderId, folderName}}`; the composer calls `POST /sessions/{session_id}/folders`.

Roots are allowed ("My Drive", a shared drive) but hit the caps (§4.5) early; the manifest says so and the card offers to pick a subfolder.

### 4.2 The folder attachment and its expansion

`POST /sessions/{session_id}/folders` body `{connectorId, folderId, folderName}`. app-api resolves the connector (`resolve_file_source`, `require_file_source_token`, the browse route's gating), creates a **folder row** and schedules the expansion as a fire-and-forget task (the `documents/services/import_service.py` pattern). It returns the row at once; the SPA shows a card and polls `GET /sessions/{session_id}/folders/{folder_attach_id}`.

Folder row, in the user-files table beside the file rows (`PK=USER#{user}`, `SK=FOLDER#{folder_attach_id}`, `GSI1PK=CONV#{session}`, `GSI1SK=FOLDER#{folder_attach_id}`, so the session listing finds it with the files):

```
folder_attach_id   ULID-style, like upload ids
session_id, user_id
connector_id, provider_folder_id, folder_name, folder_path   (display; path from the browser's breadcrumbs)
status             expanding | ready | partial | failed
counts             {listed, attached, skipped_type, skipped_size, failed, truncated}
files              [{upload_id, name, relative_path, size_bytes, mime_type}]   ≤ FOLDER_ATTACH_MAX_FILES entries
snapshot_at        when the listing was taken (for refresh, §4.6)
created_at, updated_at, ttl (365 days, as file rows)
```

The expansion task, with the user's token held in memory for its lifetime:

1. Walk the folder breadth-first through `adapter.browse`, following subfolders, until `FOLDER_ATTACH_MAX_FILES` attachable files are found or the tree is exhausted. Attachable is the composer's rule (`isAttachableSourceFile` server-side: the downloaded type is in the chat's allowlist and within its class cap) narrowed to what has an on-demand reader (§4.3): document-class files, spreadsheets and decks. Images are counted as `skipped_type`, never fetched.
2. For each file, in listing order and at most 3 concurrently: `adapter.download(token, file_id, max_bytes=cap)` then `import_file(..., source=f"connector:{id}:folder:{folder_attach_id}", delivery="reference")`. A failure marks that one entry `failed` with a short reason and continues. The row's `counts` and `files` are updated every 5 files, so the card's progress moves.
3. End state `ready` (everything attached), `partial` (some skipped or failed, or truncated by a cap), `failed` (the listing itself failed, e.g. token revoked: 409 semantics on the row, with a Connect affordance on the card).

Files are ordinary session file rows: they show in `document_read`'s listing, in `list_spreadsheets`, in the Files page, and they are deleted with the session. The folder row adds a `relative_path` the file rows do not carry.

### 4.3 Delivery: reference regardless of size

`import_file` gains `delivery: Literal["inline", "reference"] = "inline"`, stored on the row as `delivery`. `is_reference_document(meta)` becomes:

```
meta.delivery == "reference" and is document class
or (the existing size rule)
```

So a 300 KB PDF from a folder is prepared by the Lambda (text layer, shards, digest) exactly like a 30 MB one, `document_read` reads it from sidecars, and it never rides inline. `_partition_attachments` needs no change: the predicate already routes reference rows to the `reference` list, and a folder file is never on `file_upload_ids` anyway (§4.4). Spreadsheets and decks in a folder keep their tool routing (`analyze_spreadsheet`, `read_powerpoint_presentation`), which already reads session files on demand; they are not prepared.

The preparation Lambda is sized for 50 MB files, so forty small ones are the easy case; they share the 2-in-flight bound. The attachments KB ingests every prepared folder file when `ATTACHMENT_KB_ENABLED` is on, under the existing per-user cap (500 MB) and eviction (30 days), so `document_read`'s `query` mode searches the whole folder.

`delivery` is honoured only while `LARGE_DOCUMENT_ATTACHMENTS_ENABLED` is on, which the folder flag requires (synth refuses the folder flag without it, as the KB flag does). Without the Lambda there is nothing to read small files through.

### 4.4 What the turn carries

The SPA sends `folder_attachment_ids: [...]` beside `file_upload_ids`. A folder counts as **one** against `MAX_FILES_PER_MESSAGE`, not as its file count, and its files are never on `file_upload_ids`.

The route resolves each folder row (one `GetItem`, no S3) and the user message gains one block per folder, rendered once into the persisted message and a pure function of the row as read on that turn, the §4B.5 contract:

```
<folder name="BIO 101 Spring" source="Google Drive" files="38" status="ready">
  syllabus.pdf — upload_id=… — 1.2 MB — pdf
  assignments/rubric.docx — upload_id=… — 84 KB — docx
  …
</folder>
```

Bounded: the first `FOLDER_MANIFEST_MAX_ENTRIES` (50) entries, then `… and 12 more; document_read with no arguments lists them all`. While `expanding`, the block says `status="expanding" attached="12" listed="38"` and lists what is attached so far; a later turn renders the newer row, the earlier message is never rewritten. The guidance sentence that accompanies reference attachments already tells the model to use `document_read` with `pattern` then `page_range`; the folder block adds one line: *to find which file holds something, use `document_read` with `query` (when searchable) or `pattern` per file.* (PR-B names `pattern` only: `document_read`'s `query` mode arrives with large-document PR-4, and the line gains it then.)

`document_read`'s no-argument listing shows `folder/relative_path` for folder files, read from the folder row's `files` map, so a model that lost the manifest to compaction can still find its way.

Cost: a 50-entry manifest is about 1,500 tokens, in the user message of the turn the folder was attached with, paid once and then cached as history. Later turns cost nothing unless the user attaches the folder again. The tool specs are unchanged.

### 4.5 Caps

| Cap | Default | Where enforced |
|---|---|---|
| `FOLDER_ATTACH_MAX_FILES` | 100 | Expansion stops listing; row `truncated: true` |
| `FOLDER_ATTACH_MAX_BYTES` | 250 MB | Running total of attached bytes; the rest `skipped_size` |
| Per-file size | The file's class cap (`max_size_for`) | `import_file`, as today |
| Per-user storage quota | 1 GB (`FILE_UPLOAD_USER_QUOTA_BYTES`) | `import_file`; the task stops at the first quota refusal and marks the row `partial` |
| Attachments KB per-user cap | 500 MB | Existing; over-cap files are `skipped_cap` in the KB and still readable |
| Folders per message | 2 | SPA and route |

The caps are deliberately below the quota so one folder cannot consume a user's whole storage.

### 4.6 Refresh

The folder card offers **Refresh**. `POST /sessions/{id}/folders/{fid}/refresh` re-lists the folder and, for each file, applies the KB-sync change rule: a binary with the same `md5Checksum`, or a native document with the same `modifiedTime`, is unchanged; a changed file is re-downloaded to the **same upload id's key** (new bytes, `preparation` reset to pending, re-prepared and, when indexed, re-ingested); a new file is imported; a removed file keeps its row (the conversation may cite it) and is marked `removed_from_source` in the manifest. A refresh in a later message renders a fresh manifest in that message. (As built: the KB-sync worker's actual gates are Drive's `version`, then a sha256 of the downloaded bytes, rather than `md5Checksum`/`modifiedTime`; refresh records both per file at import and uses the same two gates. Refresh starts from the folder chip on a sent message and puts the folder back on the composer, since the composer's card exists only until the message is sent.) No schedule: a conversation is not a sync target, and `assistant-kb-sync.md` already covers the recurring case for knowledge bases.

### 4.7 The SPA

- Browser dialog: `attach-folder` affordance in `attach` mode (§4.1).
- Composer: a **folder card** (`app-folder-card`) beside the file cards: folder icon, name, "Adding 12 of 38…" then "38 files" or "31 of 38 (7 skipped)", with a tooltip listing skip reasons, Refresh and Remove. Remove deletes the folder row and its files (`DELETE /sessions/{id}/folders/{fid}`), the same path as removing a file card. Held in the draft like a restored attachment (`StoredFolder {folderAttachId, name, counts}`), reconciled on return.
- Message: the user message shows the folder chip; clicking it expands the manifest.
- Polling: `AttachmentSearchStatusService`'s cadence (10, 15, 20, 30 s) for the `expanding` row; stops at a settled status.
- Send is allowed while expanding: the manifest says what is ready. Enter is not held, unlike an upload, because expansion can take minutes and the user may want to ask about the first files.

### 4.8 Feature flags

| Flag | Where | Kind | Default |
|---|---|---|---|
| `FOLDER_ATTACHMENTS_ENABLED` / `CDK_FOLDER_ATTACHMENTS_ENABLED` | app-api (routes, expansion) and the Runtime (`folder_attachment_ids` resolution; the Runtime env budget takes one more variable, spread only while on, as `ATTACHMENT_KB_ENABLED` is) | Feature switch | Off; synth refuses it without `CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED` |
| `features.folderAttachments` | SPA | Feature switch | Off locally and in prod, on in `environment.development.ts` once dev turns the backend on |

Search over the folder needs `ATTACHMENT_KB_ENABLED` too; without it the manifest's guidance names `pattern` only.

### 4.9 Failure modes, all fail-open

- Listing fails mid-walk: row `partial`, what was attached stays usable.
- Token revoked during expansion: row `failed` with `needs_consent`; the card shows Connect; a retry re-runs the expansion from the row (already-attached files are skipped by provider file id).
- app-api restarts mid-expansion: the task is lost; the row stays `expanding` past `FOLDER_EXPANSION_TIMEOUT` (15 min) and the SPA's next poll calls `…/resume`, which re-runs the walk, skipping attached ids. (The task is in-process like the KB import's; moving it to the prepare queue is an option if restarts prove common.)
- Preparation fails for a file: its stub says `failed`; `document_read` falls back to the original as today.
- A folder file deleted from the Files page: the folder row's entry is marked `removed`; the manifest on the next turn says so.

### 4.10 No new infrastructure

Folder rows live in the user-files table under the existing GSI. The expansion runs in app-api. Preparation and indexing ride the existing queue and Lambda. Two flag variables are the only CDK change. The dev stack is at 494 of 500 resources, which is why this spec adds none.

## 5. PR sequence

| PR | Scope | User-visible |
|---|---|---|
| **PR-A** | `delivery` on file rows and in `is_reference_document`; folder row model and repository; `POST/GET/DELETE /sessions/{id}/folders`; expansion task with caps and progress; tests with a stubbed adapter | No |
| **PR-B** | Runtime: `folder_attachment_ids` on the chat request, manifest rendering, one-against-the-cap, `document_read` listing with folder paths; turn-path tests (byte-stability, manifest bound) | No |
| **PR-C** | SPA: `attach-folder` in the browser, folder card with progress and Remove, draft persistence, message chip, flag | Yes, when the flag is on |
| **PR-D** | Refresh (§4.6) and resume (§4.9) | Yes |
| **PR-E** | Readout (§6); decide default-on | — |

PR-A before PR-B so a folder can be expanded and read through `document_read`'s listing on dev before the manifest exists.

## 6. How we'll know

- `FolderAttachment` EMF metric: files listed, attached, skipped by reason, bytes, wall time, truncated; content-free.
- Per-turn: manifest tokens (from the user message) and `document_read` calls per folder turn, from the cost rows; the question is whether the model reads a few files or walks the folder.
- Dev pilot on a course folder and a contracts folder, with the questions in §1 as the acceptance script; success is a correct answer that names the file and page.
- Promote when: two weeks on dev, expansion p95 under 3 minutes for 40 files, no `expanding` rows older than the timeout, and the pilot's answers hold.

## 7. Non-goals

- Local folders from the user's disk (the desktop app's feature).
- Writing back into the folder (the export connector covers "Save to Drive" for one document).
- Scheduled sync of a conversation's folder (`assistant-kb-sync.md` is the recurring case).
- Images in folders (nothing reads them on demand; counted as skipped). Audio and video likewise (large-document spec §7a).
- Folders in `api-converse` and scheduled runs: SPA-only in v1, as the KB re-index on open is.

## 8. Follow-ups

- **Make it permanent:** "Add this folder to a knowledge base" on the folder card, which hands the folder to an agent or project KB with a sync policy. That is the existing import plus `assistant-kb-sync.md`; it just needs the entry point.
- **Other file sources:** the contract is the adapter's; a SharePoint or Canvas adapter that implements `browse` and `download` gets folders for free.

## 9. Decisions to confirm

1. **A folder counts as one attachment**, not its file count, against the 5-per-message cap. The alternative (count files) makes a 40-file folder impossible, which is the point of the feature.
2. **Reference delivery for every folder file**, including ones under 4 MB. The alternative (inline when small) re-creates the cost problem for exactly the folders people attach.
3. **Expansion in app-api, not the Lambda.** Same pattern as KB import today; the resume path (§4.9) covers restarts. Revisit if folders are large or restarts frequent.
4. **No automatic sync.** Refresh is a button.
