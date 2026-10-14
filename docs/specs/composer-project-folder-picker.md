# Project or folder: a picker beneath a new conversation's composer

**Status:** proposed (2026-10-14). PR-1 ships with this spec. **Depends on:** Shared Projects (`shared-projects.md`, `features.projects`) and Drive folder attachments PR-A to PR-C (`drive-folder-attachments.md`, `features.folderAttachments`). **Flags:** none of its own; each half rides the flag of the feature it starts.

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
3. **Recent folders** (PR-2): folders the user has attached before, one row per Drive folder, newest first, with the Drive path underneath.
4. **New project**: opens the existing create dialog, then binds the new conversation to the new project. The composer keeps its text.
5. **Add folder from {source}**: one row per file source the `+` menu lists, opening the same browser in its attach mode. Shown only while the message has room for a folder.

### 3.4 What picking does

- **A project** sets `assistantId=<harnessAgentId>` on the current URL, the same binding every Agent launcher uses. The launch card appears, the draft text and staged attachments stay, and the first turn stamps `preferences.projectId` as it does from the project page. No backend change.
- **No project** clears `assistantId`.
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
| **PR-1** | This spec. The picker on the empty-state composer: projects, No project, New project, Add folder from {source} | Yes, where `features.projects` or `features.folderAttachments` is on |
| **PR-2** | Recent folders: `GET /folders/recent` in app-api (one query on the caller's `FOLDER#` rows, deduplicated by Drive folder id, behind `FOLDER_ATTACHMENTS_ENABLED`), and the picker rows | Yes, with folders on |
| **PR-3** | Save as project (§5) | Yes |

## 5. Follow-up: a folder people return to

A recent folder re-attached to each new conversation re-downloads and re-prepares the same files every time. That is right for one-off work and wasteful for a folder someone opens weekly. The folder spec's "make it permanent" follow-up is the answer, and this picker is its natural home: a **Save as project** action on a folder row creates a project whose Files are that folder, with a sync policy (`assistant-kb-sync.md`). The picker then shows the two options side by side, which is the choice the person is actually making: just this chat, or keep working from it.

## 6. Decisions

1. **A project can be started from `/`**, not only from the project page. Claude allows it, the sidebar already groups the result under the project, and the launch card shows what was chosen.
2. **One project and up to two folders**, not one choice of either (§2).
3. **New conversations only.** A project cannot change after the first turn, and a thread already has the `+` menu for folders.
4. **No flag of its own.** Each half is gated by the feature it starts.
