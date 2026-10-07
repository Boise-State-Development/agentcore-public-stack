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

:::note[Switching models starts a new task]
Picking a different model from the model menu inside a task starts a **new
task in the same project**, on the project's assistant. The conversation so far
stays in the old task and doesn't come along. The menu says this before you
choose. If the project pins a model in **Settings › Model**, the menu is locked
to it; change it there to use a different model for the whole team.
:::

### Your role and preferences

The project assistant knows which member is talking to it: your name, email
and project role come with every message you send, so it can tell you apart
from the teammates named in the project's instructions.

It doesn't know your job, though. To tell it what you do and how you like
answers laid out, set **Settings › Chat › Personal instructions** once:

> I'm the export control and research security specialist in Research
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

- It's a **one-off** (an email thread, a draft you're not ready to share).
- Only **you** need it. Anything in Files is searched for everyone's tasks.
- The project's files are still **Setting up** and you need an answer now.

Both work for a **redline with tracked changes**: the assistant can quote what
the other side struck and what they added, whether the redline is attached or
in [Files](/agentcore-public-stack/user-guide/projects/set-up/#word-redlines-in-files).
Put it in Files when the whole team is working from it.

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

### Let people know

Sharing tells nobody unless you ask it to. Under **Let people know**, choose
**Everyone** to notify every member, or **Choose people** to pick members from
a list. You can only notify people already in the project.

Add a **note** (up to 280 characters) to say what you need, such as "Can you
check the spring dates?" The note appears under the task in **Shared with the
project** and in the notification. Sharing the task again replaces the note
along with the snapshot.

![The Tasks tab: your private tasks on top, and tasks shared with the project below, each with Continue in my own task.](../../../../assets/user-guide/projects/tasks.jpg)

**Public link** and **Limited share** are also offered. They share the task
outside the project, so think twice before using them for project work.

## Pick up someone else's work

Open a shared task to read its snapshot.

![A shared read-only snapshot: the assistant's clause-by-clause table for an export-control clause.](../../../../assets/user-guide/projects/shared-snapshot.jpg)

To build on it, choose **Continue in my own task**. You get your own copy of the
conversation, still in the project and on the project's current assistant, and
the original is untouched. The assistant sees the copied conversation from your
first message, and the copy shows what the original author typed.

**Attachments aren't copied.** They belong to the person who shared the task.
Where the original had one, the copy says which files weren't copied. Attach
your own copy of any you need, or ask the person who shared the task to add it
to Files.

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
are only read when the assistant goes looking. When the assistant saves a new
memory, it adds a line for it to the index automatically, so you only need to
say what to record:

> Record our agreed Article 3 payment position in project memory.

"Just for me" memory works the same way. Ask the assistant to remember a
preference **for you** and it's saved and applied in your next task in this
project:

> Remember this just for me, not the team: when I ask for a status check, reply
> with one line per open article.

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
  To give the team and the assistant your draft, download it and add it to
  project Files. Its tracked changes stay readable there (see
  [Word redlines in Files](/agentcore-public-stack/user-guide/projects/set-up/#word-redlines-in-files)).
  Name it so it's clear it's your side's proposal.

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
- archives or restores a project you're in
- leaves a project you own
- shares a task and chooses to notify you, with their note if they left one

![The notifications panel: "made you the owner" and "added you … as an editor".](../../../../assets/user-guide/projects/notifications.jpg)

Opening a notification marks it read and takes you to the project (to
**Members** when someone left, and to the task itself when someone shared one).
If the task has since stopped being shared, the page says so. Notifications
expire after 90 days, and you're never notified about your own actions. New
files and setting changes don't notify anyone. Check **Activity**.

## Archive, restore, delete, transfer, leave

| Action | Who | Where | What happens |
| --- | --- | --- | --- |
| **Archive** | Owner | Settings › Owner controls | Read-only for everyone: no new tasks or messages, no file or setting changes. Files and shared tasks stay readable. Every member is notified. |
| **Restore** | Owner | Settings | Back to normal. Every member is notified. |
| **Delete** | Owner | Settings, archived projects only | Removes the project, its assistant, its files and its list of shared tasks. Each member keeps their own tasks as ordinary conversations. |
| **Transfer ownership** | Owner | Members › **Make owner** | Goes to an editor who has signed in at least once. The old owner becomes an editor, and the new owner is notified. |
| **Leave** | Anyone but the owner | Members › **Leave project** | You lose access, and the owner is notified. Tasks you shared stay listed for the team. |

![An archived project: a banner saying it's read-only and can be restored from Settings.](../../../../assets/user-guide/projects/archived.jpg)
