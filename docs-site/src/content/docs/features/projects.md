---
title: Shared Projects
description: Shared workspaces with their own instructions, files, members and tasks, where everyone works with the same assistant.
sidebar:
  label: Projects
  order: 6
---

A **project** is a shared workspace: a name, a set of people, and one assistant
that everyone in the project works with. The assistant's instructions, model,
tools, skills and files belong to the project, so a change one editor makes
applies to every member's next turn.

Each conversation started in a project is a **task**. A task stays private to
the person who started it until they choose to share it with the project.

Design and as-built notes live in
[`docs/specs/shared-projects.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/docs/specs/shared-projects.md).
This page describes what ships today (Phase 1). Project memory is Phase 2;
schedules and outputs are Phase 3.

## Roles

Everyone in a project has one role. People are identified by email, so someone
can be added before they have ever signed in: the project and an invitation are
waiting when they do.

| | Viewer | Editor | Owner |
| --- | --- | --- | --- |
| Open the project, its files and shared tasks | ✓ | ✓ | ✓ |
| Start tasks with the project's assistant | ✓ | ✓ | ✓ |
| Share their own tasks with the project | ✓ | ✓ | ✓ |
| Add, delete and download files | download only | ✓ | ✓ |
| Rename, edit the description | | ✓ | ✓ |
| Change instructions, model, tools and skills | | ✓ | ✓ |
| Add, remove and change members | | ✓ (unless the owner turns this off) | ✓ |
| See the Activity trail | | ✓ | ✓ |
| Archive, restore, delete, transfer ownership | | | ✓ |

- **There is one owner.** Nobody can change or remove them. The owner can hand
  the project to an editor who has signed in at least once; the old owner
  becomes an editor.
- **Editors manage members by default.** The owner can limit that to
  themselves in Settings.
- **Anyone but the owner can leave.**

## The project page

`/projects` lists your projects: everything you belong to, filtered by All,
Mine, Shared with me, or Archived. There is no menu item for it yet: open
`/projects` directly, or reach a project from its heading in your conversation
list or from a notification. Each project has these tabs:

- **Overview**: a composer that starts a task in the project, plus a summary
  of the instructions and the people.
- **Tasks**: your own tasks in the project, and the tasks others shared with it.
- **Files**: the documents the assistant works from.
- **Members**: people, roles, invitations.
- **Settings**: details, instructions, model, tools, skills and their history.
- **Activity** (editors and the owner): who changed what, newest first.

The sidebar lists project tasks in their usual Today / Yesterday / … groups,
under a small heading with the project's name.

## Tasks and sharing

A task is an ordinary conversation bound to the project's assistant. Only the
person who started it can see it in **Your tasks**.

To show a task to the project, open its **Share** dialog and choose
**Project members**. This option appears only for a task in a project. The share
is a snapshot: later messages are not included. Sharing the same task again
replaces its entry, so a task is listed once in **Shared with the project**.

From there, any member can:

- **Open** the snapshot (read-only).
- **Continue in my own task**, which copies the conversation into a new task of
  their own. While the project is active, the copy is a task in the same
  project, on the project's current assistant; from an archived project it is
  an ordinary conversation.
- **Stop sharing**, for the person who shared it.

Someone who leaves a project loses access to its shared tasks. The tasks they
shared stay listed for everyone else.

## Files

Files are the assistant's knowledge base, shared by the whole project.
Everyone can see and download them; editors add and delete them. When you
upload, the page says how many people will be able to open the file. Each file
shows who added it ("Unknown" for files added by someone who has since left).

## Settings and history

Instructions, model, tools and skills are saved one at a time. Every save that
changes something becomes a numbered version with the editor's email, and
**History** shows each version's diff against the one before. Version 1 is the
state the project was created with.

An editor can only add a tool or skill they themselves can use. The assistant
still runs with whatever each member is allowed: if a member cannot use one of
the project's tools, skills, its model, or its memory, their turn runs without
it. A notice above the composer names what was left out. That notice is not
saved, so it shows on the live turn only.

## Notifications

The bell next to your name in the sidebar shows notifications about projects:
being added, a role change, being removed, and becoming owner. Opening one marks
it read and takes you to the project. Notifications expire after 90 days. You
are never notified of your own actions.

## Personal instructions

**Settings › Chat › Personal instructions** holds up to 4,000 characters about
how you like to work: your role, preferred format, units. They are added to
every conversation you have, in projects and agents too. Where a project's or
agent's own instructions disagree, those win. Leave the field empty and your
prompt is exactly what it would be without the feature.

## Archive, delete, transfer

- **Archive** (owner) makes the project read-only for everyone: no new tasks,
  no edits, no member changes. Members can still read it. The owner can restore
  it.
- **Delete** (owner) works on an archived project only, so it is always two
  steps. It removes the project, its assistant, its files and its list of
  shared tasks. The tasks themselves are not deleted: each stays in its
  owner's conversation list.
- An administrator can archive or restore any project; see
  [Admin › Projects](/agentcore-public-stack/admin/projects/).
