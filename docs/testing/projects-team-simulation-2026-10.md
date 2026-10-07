# Shared Projects: team simulation on dev (2026-10-05)

**What this is:** an end-to-end exercise of Shared Projects under realistic team
use, with a findings list ranked by severity. A simulated eight-person research
administration team (modeled on a Division of Research and Economic Development:
Sponsored Programs, Research Compliance, Technology Transfer, post-award, a PI,
counsel and an associate VP) negotiated a fictional industry-sponsored research
agreement: a sponsor redline with Word tracked changes, a negotiation playbook,
a statement of work and an NDA. All people, the sponsor and the documents are
fictional.

**User-facing docs written from this run:** `docs-site/src/content/docs/user-guide/projects/`.

## Status

Updated 2026-10-06. All fixes below are merged into `develop`.

| Findings | Fixed by | What changed for users |
| --- | --- | --- |
| B1, B2, B3, B4 | [PR 1430](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1430) | "Continue in my own task" sees the copied conversation from its first turn and shows the original author's message, not the RAG-augmented prompt. Attachments are replaced by a line naming the files that weren't copied. Session files are still not copied, by design (B4). Forks broken before the fix are repaired when their history is next restored. |
| B11, B5 | [PR 1429](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1429) | The worker releases its lease after each step, and a born-managed backlog is handed to the ingestion consumer in parallel. A new project's first files become Ready in minutes instead of up to about 90 minutes. |
| B9 | [PR 1431](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1431), [PR 1436](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1436) | Word tracked changes are written out as `[deleted: …]` / `[inserted: …]` (comments as `[comment by <author>: …]`) before indexing, with a note naming the authors. Managed KBs receive the annotated text; legacy KBs receive an annotated `.docx`. Redlines ingested before the fix keep their flattened text until re-uploaded or re-ingested. |
| G2, G18 | [PR 1432](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1432) | A new memory file is added to its scope's `MEMORY.md` index automatically. The project harness is told which member is speaking (name, email, project role) on every turn. |
| G17 | [PR 1433](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1433) | "Remember this just for me" saves to the member's personal project memory, and the next task applies it. |
| B8 | [PR 1440](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1440) | Switching models inside a project task starts a new task in the same project (and a new session with the same agent for an Agent session) instead of a non-project chat. The menu says what will happen before you choose. A pinned project model still locks the picker. |
| B7 | [PR 1438](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1438), [PR 1443](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1443) | A shared snapshot's tool rail shows finished steps as "Ran …" / "Couldn't …" instead of "Running …". Snapshots made after #1443 also carry the model's one-line tool summaries; older snapshots keep the plain lines until re-shared. |
| B10 | [PR 1438](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1438) | Re-sharing a task to the project now says "This task is already shared with the project. Sharing again replaces the snapshot the project sees." |
| G20 | [PR 1438](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1438), [PR 1442](https://github.com/Boise-State-Development/agentcore-public-stack/pull/1442) | A turn in an archived project says the project is read-only and to ask the owner to restore it. Archive and restore notify every member, and the owner is notified when someone leaves. |

**Still open:** B6, and every gap except G2, G17, G18 and G20. G6 is partly
addressed: archive, restore and leaving notify, and so does sharing a task when
the sharer picks who to tell (2.5b, with an optional note). New files and
instruction changes still don't. The end-user guide no longer lists the fixed items
as limits.

## How it was run

- **Environment:** dev, deployed at `ea4d75be` (code identical to `develop` at
  the time; the newer commit was docs-only).
- **Identities:** one real dev user drove the deployed SPA in the in-app browser.
  Seven synthetic personas ran through a scratch-only local stack: develop-branch
  app-api with `dependency_overrides` mapping an `X-Persona` header to a `User`,
  forwarding an unsigned JWT with the same claims to a local inference-api, whose
  trusted extractor decodes claims without verifying, as it does behind the
  Runtime authorizer. All persona writes went to **dev** DynamoDB, S3, the managed
  KB and Bedrock. Persona chat turns ran on local inference code, not the deployed
  Runtime. Every high-severity bug below was reproduced on the deployed SPA and
  Runtime, or traced to code shared by both.
- **Coverage:** create/settings/instructions/tools; invites (bulk, invalid,
  duplicate); notifications; role changes; `editorsManageMembers`; 10 negative
  permission probes plus an outsider probe; uploads by role; KB ingestion;
  shared and personal project memory; chat attachments with tracked changes;
  Word generation with tracked changes; share to project, snapshot, fork;
  settings history and diff; Activity; transfer; leave; archive and restore.

## Findings

Severity: **High** = data loss, wrong answers presented as correct, or a broken
core flow. **Med** = a core flow degraded with a workaround. **Low** = polish.

### Bugs

| # | Sev | Finding | Evidence | Pointer |
| --- | --- | --- | --- | --- |
| B1 | High | **A fork of a task that had a document attachment breaks permanently** once the agent is rebuilt: every turn fails with `Agent force-stopped: … messages[0].content[1].document: "source"`. | Reproduced on **deployed dev** in the SPA (fork, idle, next message) and locally. | `app_api/shares/service.py` `_snapshot_msg_to_converse` copies the snapshot's display-only `document` block (`{format,name}`, no bytes) into Converse history. Drop it or replace it with a text placeholder. Images probably have the same issue. |
| B2 | High | **A fork's first turns don't see the copied conversation.** The assistant says "I don't have access to Tom's analysis" while it's on screen. | **Deployed dev**, 3 turns (Haiku, then Sonnet). Runtime log: `Restore @init: 0 messages`. | Hypothesis: export writes messages via `create_message` but never creates the session's agent record, so the SDK's `initialize()` treats the session as new and skips the message list. Later rebuilds find the messages, and then hit B1. |
| B9 | High | **KB ingestion flattens Word tracked changes**: deleted and inserted text are concatenated with no markers ("…net thirty (30) days**payable in full within ninety (90) days**…"). On deployed dev the assistant then read Article 10 as "already matches standard, ACCEPT", although the sponsor had demanded university indemnification. | Chunks API on both redlines; deployed-dev turn. | The managed ingestion parser. Chat attachments (`document_read`) keep ins/del correctly. Options: accept-all text, or annotate `[deleted: …]`/`[inserted: …]`. |
| B11 | High | **KB worker lease never released after a step.** `LEASE_MINUTES = 15` equals the dispatcher's `rate(15 min)`, so a re-armed job's lease outlives the next tick by about 1 s ("another worker holds the lease … leaving it alone" at 15:20:36, though the worker finished at 15:07). Each re-queue costs 30 min, not 15. | Worker and dispatcher logs. | `app_api/kb_migration/worker.py` (no `release_lease`; `_rearm` keeps the lease). Probably also slows migration re-arms. |
| B5 | Med | **Born-managed ingests one document per invocation** (`MAX_DOCUMENTS_PER_INVOCATION = 1`; the comment assumes "the common case is exactly one"). A project seeded with 4 files: the 1st was ready in 2.5 min, the 2nd in 31 min, the last projected at about 90 min. Meanwhile a file uploaded later was ready in 3 min. | Files tab, worker logs. | `app_api/kb_migration/provisioner.py`. After provisioning, hand the waiting documents to the normal managed consumer. |
| B3 | Med | **A fork shows the RAG-augmented prompt** ("The following context is retrieved from the assistant's knowledge base… [Context 1] …") as the original author's first message, instead of their display text. | Deployed dev. | Snapshot/export copies model-facing content, not `displayText`. |
| B4 | Med | **Fork drops chat attachments** (the session files aren't copied), so the assistant can't re-read the document the conversation is about. | `/files?sessionId=` on original vs fork. | Same export path. |
| B8 | Med | **Model picker inside a project task silently leaves the project.** Choosing another model navigates to a new non-project chat, with no warning. | Deployed dev. | `components/model-dropdown/model-dropdown.component.ts` `selectModel`. In a project, start a new task in the same project, or block the switch when the project pins a model. |
| B6 | Low | A file showed **Ready** about 90 s before the managed KB returned it (0 chunks, then 2). | Runtime log vs `retrievableAt`. | Likely eventual consistency. Consider a short grace period or "Ready (indexing)". |
| B7 | Low | Shared snapshot labels completed tool steps "Running memory read/list/save". | Deployed dev. | Snapshot tool-rail state. |
| B10 | Low | Share dialog in a project task says "Creating a new share will add another snapshot", but a project re-share replaces the listed entry. | Deployed dev. | Copy. |

### Gaps and missing features

**Collaboration and awareness**

- **G5: The assistant can't read shared tasks.** Team knowledge reaches other
  members' tasks only through Files and project memory. Combined with G2 and G7,
  counsel's escalation summary listed 2 of 7 escalations: the rest lived in shared
  snapshots and a viewer's personal memory.
- **G6: Nothing notifies on team activity.** No notification for a shared task,
  new file, instruction change, archive, or someone leaving. There are no
  comments or @mentions on shared tasks, and no way to direct a share to a person.
  Activity is editors-only, so a PI (viewer) has no feed at all.
- **G18: The assistant doesn't know which member is speaking.** It told the
  export-control specialist to "send this to [her own name]". The harness
  context carries no member identity. Personal instructions are the workaround.
- **G8:** people appear by email everywhere (members, notifications, owner, Activity).
  The directory already knows names.
- **G9:** a member's `hasSignedIn` means "has opened this project", so an active
  platform user shows as not signed in, and "Make owner" stays hidden until they
  open it.
- **G13:** Overview has no "what's happening" (recent shares, file status,
  memory highlights).

**Memory**

- **G1: No project-memory UI.** Members can't see, review or correct shared
  memory. In this run two wrong claims (a $0 cash-flow figure, and a license
  called an assignment) became team "memory" with no review step. 2.5
  proposals/review would address this.
- **G2: `memory_save` doesn't index new entries.** Only `MEMORY.md` is injected,
  and indexing is a separate save the model usually forgets: 2 of 3 team entries
  and the personal preference were orphaned. Auto-append an index line from
  `description` on create, or return a nudge in the save result.
- **G17:** consequence of G2: a personal "just for me" preference saved in one
  task wasn't applied in the next.
- **G7:** viewers can't contribute files or team memory (the PI couldn't upload her
  own SOW, and her hard thesis requirement couldn't reach the team).
- **G12:** the memory-spaces API returns internal user ids in `updatedBy`, where the
  Projects API is deliberately email-only.

**Files and retrieval**

- **G3: No agent tool to search or open project Files on demand.** There's one
  automatic search per message, using the raw message text, top 5. The managed engine
  returned 2 of 3 playbook chunks and missed the export-control section on a long
  conversational message. A short keyword-rich rephrase found it.
- **G4:** while files were Setting up, the assistant had no idea, told the user to
  "upload the files to this chat", and confused project memory with project files.
- **G14:** files the assistant generates (the Word redline) are owner-only: a
  teammate gets 404 even after the task is shared, and there's no "save to project
  Files". There's also no file versioning (v2 and v3 are unrelated files).

**Settings and governance**

- **G10:** no revert to a previous instructions version.
- **G19:** the Share menu in a project task also offers Public link and Limited
  share. Any member, viewers included, can publish project-derived content
  (including KB excerpts) outside the project, and there's no owner policy to
  restrict this.
- **G20:** members aren't notified of an archive. Continuing an existing task in an
  archived project says it "can't start new conversations". The owner isn't told
  when someone leaves.

## What worked well

- **Permissions held everywhere.** All 10 negative probes and the outsider
  (project, files, memory, snapshot, fork and the harness chat itself) were
  refused, with clear messages. Transfer to a viewer was refused, and a transfer
  to an editor demoted the old owner correctly.
- **Invites:** bulk add with case-insensitive duplicate detection and invalid-email
  reporting; notifications for invite, role change and ownership.
- **Files tab:** sharing notice with head count, "Added by", usage meter.
- **Settings history:** per-version diff with the editor's email. Activity is
  readable, with display names for tools.
- **Memory isolation:** shared memory readable across members. Personal memory
  returned 404 to everyone else, including the owner. A viewer's team-memory write
  was refused and fell back to personal.
- **Chat attachments read tracked changes correctly.** The assistant quoted
  deletions and insertions separately.
- **Word generation produced genuine tracked changes:** 9 `w:ins` / 11 `w:del`
  authored "Boise State OSP", well-formed XML, rendering correctly. It missed one
  of the requested edits.
- **Answer quality, when retrieval hit:** clause-by-clause ACCEPT/COUNTER/ESCALATE
  with playbook citations and paste-ready language. The assistant routed an
  escalation to the right person from team memory, and listed its own blind spots
  when asked.
- **Archive:** a clear banner, read-only settings, uploads 409, chat refused with
  a streamed message, files and shared tasks still readable. Restore worked.

## Suggested order of work

1. B1 + B2 + B3 + B4 (fork/export): one PR in `shares/service.py`. B1 bricks
   tasks on dev today.
2. B11 then B5 (KB throughput): release the lease on re-arm (or shorten it), then
   ingest a born-managed backlog in parallel.
3. B9 (tracked changes in KB): a correctness risk for any redline workflow.
4. G2 (auto-index memory) + G18 (member identity in harness context): small
   changes with a large effect on team recall.
5. G6 + G5 + G1: the collaboration layer (notifications for shares, letting the
   assistant read shared tasks, and the memory review UI already planned for 2.5).
