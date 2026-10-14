# Project or folder: a picker beneath a new conversation's composer

**Status:** PR-1 and PR-2 merged (2026-10-14); PR-3 in review; folder sync (§5.3) proposed. **Depends on:** Shared Projects (`shared-projects.md`, `features.projects`) and Drive folder attachments PR-A to PR-C (`drive-folder-attachments.md`, `features.folderAttachments`). **Flags:** none of its own; each half rides the flag of the feature it starts.

## 1. Problem

Claude's desktop app puts a **Project or folder** control under the composer of a new session. One click scopes the work before the first message: pick a project, pick a recent folder, create a project, or add a folder.

This app now has both halves but no such front door. A Shared Project conversation starts only from the project's own page, or from a link that carries the project's harness Agent on the URL. A Drive folder attaches only through the `+` menu, two dialogs deep (Add from Google Drive, then Attach this folder). People who work from a project or a folder every day have to know where each entry point lives.

## 2. What the two rows mean here

They are different kinds of scope, and the picker must not blur them.

| | Project | Folder |
|---|---|---|
| What it is | A Shared Project: instructions, files, memory, members, run by its hidden harness Agent | A Google Drive folder, attached by reference to this conversation only |
| Lifetime | The whole conversation. It binds at the first turn and cannot change afterwards (`agent_binding_policy.py`) | This message. A message takes up to two; later messages may add more |
| After the first send | Shown by the existing project crumb in the top bar | Shown as a chip on the user message |
| How many | One | Up to two per message |

So the picker allows one project **and** up to two folders, not a single choice. Picking a project replaces any Agent already chosen for the new conversation, exactly as launching another Agent does today.

## 3. Design

### 3.1 Where it sits

On the line beneath the composer, left side, in a **new** conversation only: the full-page empty state, never in a thread, never in the embedded previews (agent editor, marketplace test drive). That line exists today but renders in the empty state only while a status hint applies; the picker makes it render whenever the picker is visible. The model picker stays where it is.

Once the conversation has messages, the picker is gone: the project is shown by the top bar's project crumb, and folders are added through the `+` menu as today.

### 3.2 The trigger

A quiet text button: **Project or folder** with a chevron. With a project chosen it reads the project's name instead, with a project mark. The name comes from the project list, never from the harness Agent, whose name goes stale on rename.

### 3.3 The popover

An overlay anchored to the trigger, holding an APG combobox: a filter field with focus, and one listbox beneath it. ArrowUp and ArrowDown move the active option, Enter picks it, Escape closes and returns focus to the trigger. The match count is announced politely as the filter narrows. This is the pattern the memory link picker already uses.

Options, in order:

1. **No project**, only while a project is chosen. Clears it.
2. **Projects**: the caller's active projects, own and shared, most recently updated first, filtered by name. Archived projects are left out, because a new conversation in one is refused. The chosen project is marked selected.
3. **Recent folders** (PR-2): folders the user has attached before, one row per Drive folder, newest first, with the Drive path underneath. Filtered with the projects, by name or path. Only folders whose file source the user can still see are offered (the `GET /file-sources` list, which also supplies the connector the attach needs), and never one already on this message. The list is read again on every open, so a folder attached a moment ago comes first.
4. **New project**: opens the existing create dialog, then binds the new conversation to the new project. The composer keeps its text.
5. **Add folder from {source}**: one row per file source the `+` menu lists, opening the same browser in its attach mode. Shown only while the message has room for a folder.

### 3.4 What picking does

- **A project** sets `project=<projectId>` on the current URL (and clears any `assistantId`). The session page resolves the project to its harness Agent (`ConversationBindingService`: the project list, else one `GET /projects/{id}`) and sends the harness as the turn's `assistant_id`, so the backend is unchanged: it still derives the project from the harness, and the first turn stamps `preferences.projectId` as it does from the project page. The empty state shows the greeting, not the harness's launch card, and the composer's placeholder names the project ("Ask anything in {project}…"). The draft text and staged attachments stay.
- **No project** clears `project` (and `assistantId`).
- **Why `?project=` and not the harness id.** A project conversation runs on a hidden Agent, and an earlier revision put that Agent on the URL like any other (`?assistantId=<harnessAgentId>`). That leaked the harness into every link, and made "is this a project?" a reverse lookup over the project list. Every project link now uses `?project=`: the picker, the project page's tasks, the sidebar and search (from `preferences.projectId`), a model switch, New task, and the redirect that fills in a bare `/s/:id`. An older link with a harness in `assistantId` still opens the project and is rewritten to `?project=` once the project is known. A message sent while a `?project=` is still resolving waits for it rather than going out unbound.
- **A folder** goes through the composer's existing folder attach: one card, polled until it settles, sent as `folder_attachment_ids`.
- **A recent folder** (PR-2) attaches the same Drive folder afresh to this conversation. The files are downloaded and prepared again: folder rows belong to one conversation, by design (`drive-folder-attachments.md` §9). §5 is the answer for folders people return to.

### 3.5 Flags and visibility

- The projects rows show only while `features.projects` is on.
- The folder rows show only while the composer can take a folder (`features.folderAttachments`, and room left on the message).
- The trigger hides when neither has anything to offer.

### 3.6 Cost and latency

Nothing here touches the turn path or the prompt. A project binds through the harness path that already exists. A folder reaches the model only through the folder manifest that PR-B renders. The project list is loaded once per page (the sidebar usually has it already); the file sources load when the popover opens, as the `+` menu's do.

## 4. PR sequence

| PR | Scope | User-visible |
|---|---|---|
| **PR-1** (merged) | This spec. The picker on the empty-state composer: projects, No project, New project, Add folder from {source} | Yes, where `features.projects` or `features.folderAttachments` is on |
| **PR-2** (merged) | Recent folders: `GET /folders/recent?limit=` in app-api (default 8, at most 20), and the picker rows. One key query on the caller's own partition (`PK=USER#{user}`, the `FOLDER#` sort prefix), newest first because folder ids lead with a millisecond timestamp, projecting only the six attributes shown and never the file list. One row per `(connector, folder id)`, named from its newest row. The read stops at the limit or at 100 rows, so a folder attached to many conversations cannot make it unbounded. Behind `FOLDER_ATTACHMENTS_ENABLED`, authenticated before the flag's 404. No new index: deleting a conversation already deletes its folder rows, so its folders drop out | Yes, with folders on |
| **PR-3** (in review) | Save as project, a copy (§5.1, §5.2): a button on a sent folder chip; SPA only, over `POST /projects` and the project's Drive import | Yes, with projects and folders on |
| **PR-4** (proposed) | Folder sync for a saved project (§5.3): a `drive_folder` source in KB sync | Yes |

## 5. A folder people return to: Save as project

A recent folder re-attached to each new conversation re-downloads and re-prepares the same files every time. That is right for one-off work and wasteful for a folder someone opens weekly. The folder spec's "make it permanent" follow-up is the answer: **Save as project** makes a project whose Files are the folder's files.

### 5.1 Where it lives

On the **folder chip of a sent message**, beside Refresh, as an icon button named "Save {folder} as a project". That is where a person has the folder in hand and has just used it. It is not a row action in the picker: an APG combobox option cannot hold a second control, and a separate "Save … as project" option per recent folder would double the list.

### 5.2 What it does (PR-3)

A dialog names the project after the folder (editable), says how many files will be added and from which source, and says plainly that the project keeps a copy. Saving:

1. Finds the chip's folder row in the conversation on screen, the lookup Refresh already does.
2. Creates the project (`POST /projects`), the caller its owner, described as "Files from the folder “{path}” in {source}".
3. Imports the folder's files into the project's Files through `POST /projects/{id}/knowledge/import`, in batches of 50 (the route's limit), with the caller's own Drive connection. The files are the row's attached entries still in the folder, once each, by provider file id. They are documents, spreadsheets and decks; import checks no type up front, so a file the project's knowledge base cannot ingest shows as failed on its Files page, as any import does.
4. Toasts "Saved as project" with **Open project**. The person stays in the conversation.

A failed create leaves nothing behind. A failed import after the create does not hide the project: the dialog says how many files went in and why the rest did not, and offers **Open project**.

No backend change: every step is a route that exists, with its own authorization, audit and born-managed knowledge-base provisioning. Folder paths are not kept; a project's Files are one flat list.

### 5.3 Keeping a saved project in step with its folder (proposed, PR-4)

The copy goes stale as the Drive folder changes. KB sync (`assistant-kb-sync.md`) cannot keep it fresh as built:

- **Its source is one file.** A `drive_file` policy re-fetches one imported document. A file added to the folder later has no document and no policy, so nothing would ever bring it in.
- **Policies are capped at 10 per Agent** (`KB_SYNC_MAX_POLICIES_PER_ASSISTANT`), a runaway safeguard (§7 of that spec). A project's Files belong to its harness Agent, so a 40-file folder cannot have one policy per file.

The fit is a third source type, **`drive_folder`**: one policy per folder, whatever its file count, on the existing sweeper and worker. Each run re-lists the folder (the expansion's walk and caps), re-fetches changed files through the existing per-file path (version, then content hash), imports new ones, and marks files gone from the folder rather than deleting them. It inherits every safeguard (interval enum, circuit breaker, inactivity pause, reauth pause) and adds a per-run file cap. Save as project would then offer "Keep in sync with the folder" with an interval.

It is a change to the KB sync worker image and the import path the worker would need, so it is its own PR with its own review of the safeguards, not part of PR-3.

## 6. Decisions

1. **A project can be started from `/`**, not only from the project page. Claude allows it, the sidebar already groups the result under the project, and the launch card shows what was chosen.
2. **One project and up to two folders**, not one choice of either (§2).
3. **New conversations only.** A project cannot change after the first turn, and a thread already has the `+` menu for folders.
4. **No flag of its own.** Each half is gated by the feature it starts. Save as project needs both projects and folder attachments on.
5. **Save as project is a copy until folder sync exists** (§5.3). Per-file sync policies cannot cover a folder: they miss new files and hit the 10-per-Agent cap.
