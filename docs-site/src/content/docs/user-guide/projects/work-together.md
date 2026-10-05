---
title: Work together in a project
description: Start tasks, share them, build up project memory, work with attachments and Word redlines, and keep track of changes.
sidebar:
  label: Work together
  order: 3
---

## Start a task

Type in the composer on the project's **Overview** tab, or choose **Start a
task** on the **Tasks** tab. The task runs on the project's assistant, with the
project's instructions, files, tools and memory, and appears in your sidebar
under the project's name. The breadcrumb at the top of the task shows which
project it belongs to.

![A task inside the project: the breadcrumb shows the project, and the assistant answers with an escalation table.](../../../../assets/user-guide/projects/project-task.jpg)

**Your tasks are private.** Nobody else in the project, including the owner, can
see them until you share one.

:::caution[Don't switch models mid-task]
Picking a different model from the model menu inside a task starts a **new
conversation outside the project**, without the project's assistant. To use a
different model for the whole team, change it in **Settings › Model**.
:::

### Tell the assistant who you are

The project assistant doesn't automatically know which member is talking to it.
Without help, it may tell the export-control specialist to "send this to
Research Compliance". Fix this once in **Settings › Chat › Personal
instructions**:

> I'm Priya Raman, export control and research security specialist in Research
> Compliance. I review ITAR/EAR, CUI and foreign-national clauses. Format
> findings as a table: Issue | Risk | Recommended language.

Personal instructions (up to 4,000 characters) are added to every conversation
you have, in every project. Where they conflict with a project's
instructions, the project's win.

## Attach a file to a task

Use **+** in the composer to attach a file to *your* task. Attachments are
private to that task. They aren't added to project Files and other members'
assistants can't see them.

Attach rather than upload when:

- The document is a **redline with tracked changes**. The assistant reads an
  attachment's tracked changes as real insertions and deletions, so it can
  quote exactly what the other side struck and added. Project Files flatten
  tracked changes.
- It's a **one-off** (an email thread, a draft you're not ready to share).
- The project's files are still **Setting up** and you need an answer now.

## Share a task with the project

Open the task's menu (the chevron next to its title), choose **Share**, and pick
**Project members**.

![The Share dialog with Project members selected, plus Public link and Limited share options.](../../../../assets/user-guide/projects/share-dialog.jpg)

- A share is a **snapshot**: messages you add later aren't included. Share
  again to update it. A new project share replaces the old entry, so the task
  is listed once.
- Shared tasks appear on everyone's **Tasks** tab under **Shared with the
  project**, with who shared them and when.
- To stop sharing, use the trash icon next to a task you shared.

![The Tasks tab: your private tasks on top, and tasks shared with the project below, each with Continue in my own task.](../../../../assets/user-guide/projects/tasks.jpg)

**Public link** and **Limited share** are also offered. They share the task
outside the project, so think twice before using them for project work.

:::note[Sharing doesn't notify anyone]
Teammates aren't notified when you share a task. If you need someone to look at
it, tell them, and include the task's title.
:::

## Pick up someone else's work

Open a shared task to read its snapshot.

![A shared read-only snapshot: the assistant's clause-by-clause table for an export-control clause.](../../../../assets/user-guide/projects/shared-snapshot.jpg)

To build on it, choose **Continue in my own task**. You get your own copy of the
conversation, still in the project and on the project's current assistant, and
the original is untouched. See
[Tips and current limits](/agentcore-public-stack/user-guide/projects/tips-and-limits/#current-limits)
for what a copy doesn't carry over today.

## Project memory

The assistant can remember things across tasks, at two levels:

| | **Project memory** | **Just for me** |
| --- | --- | --- |
| Who it's for | Everyone in the project | Only you, only in this project |
| Who can save to it | Editors and the owner | Any member |
| Good for | Deadlines, owners, agreed positions, open issues | Your formatting preferences, your own working notes |
| How to ask | "Record in **project memory** that…" | "Remember **for me** (not the team) that…" |

How it reaches other people's tasks: each memory has a short **index**, and the
index is what the assistant sees at the start of every task. Detailed entries
are only read when the assistant goes looking. So when you save something
important, ask for both:

> Record our agreed Article 3 payment position in project memory, **and add a
> one-line pointer to it in the project memory index.**

Other useful requests:

- "What's in project memory?" or "List the project memory files." (There's no
  memory tab to browse yet, so ask the assistant.)
- "Update the open-issues note: the indemnification clause is resolved."
- "In the Article 6 note, replace the 90-day review item. It's out of date."
  (The assistant edits a memory file by rewriting it. It can't delete a file
  outright.)

:::caution[Check what gets saved]
Project memory is shared with everyone, and there's no review step yet. If the
assistant gets something wrong, such as a misread clause or a math error, and
saves it, every teammate's assistant will repeat it. Ask the assistant to read
back what it saved, and correct it straight away.
:::

Viewers can't save to project memory. If a viewer asks, the assistant saves to
their "just for me" memory instead. A viewer with information the team needs,
such as a principal investigator's hard deadline, should share the task or ask an
editor to record it.

## Ask for a Word redline

If the project has the **Word Documents** tool, the assistant can produce a
`.docx` with **real tracked changes**. Attach the other side's draft and say
what you want:

> Attached is Cascadia's v2 redline. Create "Cascadia ISRA – BSU Response v3.docx":
> start from their text and show our changes as tracked changes (author "Boise
> State OSP"), with a short italic note under each article citing the playbook
> section.

![A generated Word document showing tracked insertions (underlined) and deletions (struck through), with a playbook note under each article.](../../../../assets/user-guide/projects/word-redline.jpg)

Open the file in Word and use **Review › Track Changes** to accept or reject
each change. The generated file is saved to **your task**, not the project:

- Download it from the task.
- Review it. The assistant can miss a clause or misstate a rule.
- Teammates can't open a file from your task, even after you share the task.
  To give the *assistant* the team's position, add a **clean copy** (all
  changes accepted) to project Files. Share the redline itself the way your
  office usually does, because Files flattens tracked changes.

## Settings history and Activity

Every saved change to instructions, model, tools or skills becomes a numbered
version. Open **Settings › History** and expand a version to see the change
and who made it.

![Settings › History: version 4 expanded, showing the added instruction line in green and the editor's email.](../../../../assets/user-guide/projects/settings-history.jpg)

To go back to an earlier version, copy the text from History into the editor and
save. That becomes a new version.

The **Activity** tab (editors and owner) lists who added people, changed roles,
added files, shared tasks, edited settings, archived or restored the project,
and left it.

![The Activity tab: a list of events such as restored, archived, shared a task, left the project, made someone the owner, added a file.](../../../../assets/user-guide/projects/activity.jpg)

## Notifications

The bell next to your name in the sidebar shows when someone:

- adds you to a project
- changes your role
- removes you
- makes you the owner

![The notifications panel: "made you the owner" and "added you … as an editor".](../../../../assets/user-guide/projects/notifications.jpg)

Opening a notification marks it read and takes you to the project.
Notifications expire after 90 days, and you're never notified about your own
actions. Shared tasks, new files, setting changes, archiving and departures
don't notify anyone. Check **Tasks** and **Activity**.

## Archive, restore, delete, transfer, leave

| Action | Who | Where | What happens |
| --- | --- | --- | --- |
| **Archive** | Owner | Settings › Owner controls | Read-only for everyone: no new tasks or messages, no file or setting changes. Files and shared tasks stay readable. |
| **Restore** | Owner | Settings | Back to normal. |
| **Delete** | Owner | Settings, archived projects only | Removes the project, its assistant, its files and its list of shared tasks. Each member keeps their own tasks as ordinary conversations. |
| **Transfer ownership** | Owner | Members › **Make owner** | Goes to an editor who has signed in at least once. The old owner becomes an editor, and the new owner is notified. |
| **Leave** | Anyone but the owner | Members › **Leave project** | You lose access. Tasks you shared stay listed for the team. |

![An archived project: a banner saying it's read-only and can be restored from Settings.](../../../../assets/user-guide/projects/archived.jpg)
