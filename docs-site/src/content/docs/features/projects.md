---
title: Shared Projects
description: Shared workspaces with their own instructions, files, members and conversations, where everyone works with the same assistant.
sidebar:
  label: Projects
  order: 6
---

A **project** is a shared workspace: a name, a set of people, and one assistant
that everyone in the project works with. The assistant's instructions, model,
tools, skills and files belong to the project, so a change one editor makes
applies to every member's next turn.

Each conversation started in a project stays private to the person who started
it until they choose to share it with the project.

Design and as-built notes live in
[`docs/specs/shared-projects.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/docs/specs/shared-projects.md).
This page is the feature reference. For a task-oriented guide written for the
people who use projects, with an illustrated walkthrough, see
[User Guide › Projects](/agentcore-public-stack/user-guide/projects/overview/).
Project memory (shared and "just for me" spaces the assistant reads and writes)
ships with Phase 2; schedules and outputs are Phase 3. Projects are still in development, so a
deployment has to turn them on; see
[Admin › Projects](/agentcore-public-stack/admin/projects/#turning-projects-on).

## Roles

Everyone in a project has one role. People are identified by email, so someone
can be added before they have ever signed in: the project and an invitation are
waiting when they do.

| | Viewer | Editor | Owner |
| --- | --- | --- | --- |
| Open the project, its files and shared conversations | ✓ | ✓ | ✓ |
| Start conversations with the project's assistant | ✓ | ✓ | ✓ |
| Share their own conversations with the project | ✓ | ✓ | ✓ |
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
  themselves on the Members page.
- **Anyone but the owner can leave.**

## The project page

`/projects` lists your projects: everything you belong to, filtered by All,
Mine, Shared with me, or Archived. It's in the sidebar menu when your deployment
has Projects turned on.

A project's page is a composer that starts a conversation in the project, with
your **Recents** (your own conversations, newest first) and the conversations
others **shared with the project** beneath it. Beside them, a rail lists the
project's settings as one-line summaries. Each row opens that setting's own
page, except **Model**, which is a menu on its row:

- **Instructions**: what the assistant is told in every conversation.
- **Files**: the documents the assistant works from.
- **Memory**: what the assistant remembers for the project, and for you.
- **Model**: the model every conversation runs on.
- **Tools**: what the assistant can use.
- **Skills**: the playbooks the assistant can follow, and the version of each.
- **Schedules**: prompts that run in the project on a cadence (see
  [Schedules](#schedules)).
- **Members**: people, roles, invitations, and (for the owner) whether editors
  may manage members.
- **Activity** (editors and the owner): who changed what, newest first.
- **History**: every saved settings version and its diff.

The menu on the project's name in the breadcrumb renames the project, archives
or restores it, deletes it (once archived), or lets you leave it. Links that
name a section, such as `/projects/{id}/members`, open that page.

The sidebar lists project conversations in their usual Today / Yesterday / …
groups, under a small heading with the project's name.

## Conversations and sharing

A project conversation is an ordinary conversation bound to the project's
assistant. Only the person who started it can see it in **Recents**.

To show a conversation to the project, open its **Share** dialog and choose
**Project members**. This option appears only for a conversation in a project.
The share is a snapshot: later messages are not included. Sharing the same
conversation again replaces its entry, so it is listed once in **Shared with
the project**.

From there, any member can:

- **Open** the snapshot (read-only).
- **Continue in my own conversation**, which copies the conversation into a
  new one of their own. While the project is active, the copy is a conversation
  in the same project, on the project's current assistant; from an archived
  project it is an ordinary conversation outside the project.
- **Stop sharing**, for the person who shared it.

Someone who leaves a project loses access to its shared conversations. The
ones they shared stay listed for everyone else.

## Schedules

A schedule runs a prompt in the project for you, every day, every weekday, once
a week or every few hours. It runs on the project's assistant with the project's
tools, as the person who made it. The **Schedules** row in the project's rail
shows a count of schedules and how many are paused, or "Not turned on" when your
deployment has scheduled runs off.

- **Editors and the owner** can add a schedule with **New schedule**. The form
  asks for a name, the prompt and when it runs. There is no agent or tool to
  pick: the project decides.
- **Share each run with the project** puts every finished run under **Shared
  with the project**, tagged **Scheduled**. The assistant can read past runs
  too, so a weekly summary can build on last week's. The newest runs stay
  shared and older ones stop being shared. Without it, each run stays a
  conversation of your own in the project.
- **Notify members when a run is shared** sends everyone else a notification
  for each run. It is only available while runs are shared.
- **Publish its artifacts to Outputs** puts whatever a run makes, such as a
  report, a chart or a table, under the project's **Outputs**, tagged
  **Scheduled**. Each run's artifacts replace the previous run's, so Outputs
  always show the latest edition; earlier editions stay in each run's shared
  conversation. A run that makes nothing leaves the last edition in place. It
  is only available while runs are shared.
- **Run now** on the form runs the prompt once, straight away, as a
  conversation of your own. It doesn't save the schedule or share the run.

A schedule runs as the person who made it, so only they can **Edit** or
**Resume** it. An editor or the owner can **Pause** or **Delete** anyone's
schedule, and its creator is told. Everyone in the project, viewers included,
can see every schedule and open its **Run history**: when each run finished,
whether it worked, and a link to the run while it is still shared.

A schedule stops on its own when it can no longer run: its creator leaves the
project or stops being an editor, the project is archived, or it fails several
times in a row. The Schedules page says why, and the creator and the owner get
a notification. Restoring an archived project starts its schedules again.

Your project schedules also appear on your own **Schedules** page, marked with
the project's name. You can pause or delete them there; to change or resume one,
use **Manage in project**.

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

### Skill versions

A skill added to a project stays on the version it was added at. If its author
changes it later, the project keeps running the version it was added at until
an editor updates it. **Skills** shows each skill's version and the
date it was saved, and says when a newer version is available. **Update** moves
the project to the skill's current version. The update is saved straight away
and appears in History like any other settings change. A skill added before
versions existed shows "Uses the latest version" until an editor pins it with
**Pin this version**.

A version saves the skill's instructions and its reference files. It does not
save whether the skill is switched on. If an administrator disables or deletes
the skill, the project stops using it, whatever version it is on.

## Notifications

The bell next to your name in the sidebar shows notifications about projects:
being added, a role change, being removed, becoming owner, a project being
archived or restored (sent to every member), and, for the owner, someone
leaving. A schedule that stopped tells its creator and the owner. Opening a
notification marks it read and takes you to the project, or, for a stopped
schedule, to the project's schedules. Notifications
expire after 90 days. You are never notified of your own actions.

## Personal instructions

**Settings › Chat › Personal instructions** holds up to 4,000 characters about
how you like to work: your role, preferred format, units. They are added to
every conversation you have, in projects and agents too. Where a project's or
agent's own instructions disagree, those win. Leave the field empty and your
prompt is exactly what it would be without the feature.

## Archive, delete, transfer

- **Archive** (owner) makes the project read-only for everyone: no new
  conversations, no edits, no member changes. Members can still read it. The owner can restore
  it.
- **Delete** (owner) works on an archived project only, so it is always two
  steps. It removes the project, its assistant, its files and its list of
  shared conversations. The conversations themselves are not deleted: each
  stays in its owner's conversation list.
- An administrator can archive or restore any project; see
  [Admin › Projects](/agentcore-public-stack/admin/projects/).
