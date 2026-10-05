---
title: Set up a project
description: Create a project, give its assistant instructions, choose tools, add files and invite your team.
sidebar:
  label: Set up a project
  order: 2
---

Setting up a project takes about ten minutes. Most of that is writing good
instructions, and that time pays back on every task anyone runs in the project.

## 1. Create the project

Open **Projects** and choose **New project**. Give it a name people will
recognize in the sidebar, and a one-line description of the goal. You become
its **owner**.

![The New project dialog with Name and Description fields.](../../../../assets/user-guide/projects/new-project-dialog.jpg)

## 2. Write the instructions

Open **Settings › Instructions**. Instructions are what the assistant is told at
the start of *every* task, for *every* member. Treat them as a standing brief:
who the assistant works for, which documents are authoritative, how answers
should be laid out, and what it must never do.

A good brief covers four things:

1. **Role and context.** "You are the contract-review assistant for the Office
   of Sponsored Programs, working on the Cascadia research agreement."
2. **Sources of truth.** "Always compare sponsor language against the
   negotiation playbook in the project files, and cite the section."
3. **Output format.** "Go clause by clause. Classify each change as ACCEPT,
   COUNTER or ESCALATE, and give paste-ready contract language."
4. **Hard rules and routing.** "Never accept terms the playbook marks
   non-negotiable. Say who on the team should handle questions outside contract
   terms."

![Settings tab: the Instructions editor with the version number and character count.](../../../../assets/user-guide/projects/settings-instructions.jpg)

Every save that changes something becomes a new **version**. See
[History](/agentcore-public-stack/user-guide/projects/work-together/#settings-history-and-activity).

:::tip[Keep instructions about the project, not about people]
Write instructions for the whole team. Things that only apply to you, like
your role or your preferred format, belong in your
[personal instructions](/agentcore-public-stack/user-guide/projects/work-together/#tell-the-assistant-who-you-are),
which ride along on every conversation you have.
:::

## 3. Choose a model, tools and skills

Still in **Settings**:

- **Model.** Leave it on *Each member's default model*, or pin one model for
  the whole team. Anyone who can't use the pinned model gets their own
  default instead.
- **Tools.** Turn on only what the work needs. Each tool's description is
  shown, and you can only add tools you can use yourself. Useful
  combinations:
  - *Contract or policy work:* **Word Documents** (so the assistant can produce
    a redlined `.docx`), **Policy Search**, **Clarifying Questions**.
  - *Budgets and numbers:* **Calculator**, **Analyze Spreadsheet**, **Excel
    Spreadsheets**.
  - *Reports and briefings:* **Word Documents**, **PowerPoint Presentations**,
    **Charts & Graphs**.
- **Skills.** Add any skills the team relies on.

If a member doesn't have access to one of the project's tools, skills, its model
or its memory, their tasks still run without it, and a notice above the
composer says what was left out.

## 4. Add the files the assistant should work from

Open **Files** and choose **Add files**. PDF, Word, PowerPoint, Excel, CSV,
Markdown, HTML and plain text are accepted. Everyone in the project can open
these files, and the assistant searches them to answer *anyone* in the project.
The page reminds you how many people that is.

![The Files tab: five files, each showing who added it, with Ready or Setting up status and a storage meter.](../../../../assets/user-guide/projects/files-setting-up.jpg)

Each file shows who added it and a status:

- **Setting up / Uploading**: the file is being prepared. The assistant can't
  use it yet.
- **Ready**: the assistant can search it.

:::caution[The first files take a while]
A project's first upload also creates its search index. When you add several
files at once to a brand-new project, they become **Ready** one at a time, and
the last one can take an hour or more. Files added after that are usually ready
within a few minutes. Start your team with the single most important document
(a playbook, policy or template), then add the rest.
:::

What to put in Files:

- **Reference material everyone needs**: playbooks, policies, templates,
  executed agreements, the statement of work.
- **Clean, final text.** The assistant reads files as plain text. **Tracked
  changes are flattened**: deleted and inserted wording run together with no
  markup. Keep redlines out of Files and attach them to a task instead (see
  [Attach a file to a task](/agentcore-public-stack/user-guide/projects/work-together/#attach-a-file-to-a-task)).
- **One current version** of each document. Delete superseded drafts so the
  assistant doesn't quote an old one.

## 5. Invite your team

Open **Members**. In **Add people**, type a name or email, or paste a list of
emails, and pick a role. Invalid addresses and people who are already members
are reported back to you. Anyone you add gets a notification (the bell next to
your name in the sidebar), and the project appears in their Projects list.

![The Members tab from the owner's view: role pickers, remove buttons and Make owner.](../../../../assets/user-guide/projects/members.jpg)

- Change a role with the role picker next to a person's name, or remove them
  with the trash icon.
- By default, **editors can manage members too**. To keep that to yourself,
  clear **Settings › Owner controls › Editors can manage members**.

![Owner controls: the Editors can manage members checkbox and the Archive project button.](../../../../assets/user-guide/projects/owner-controls.jpg)

## 6. Seed the project memory

Before the team starts, give the assistant the facts everyone should share: key
dates, who owns which part, decisions already made. Start a task and ask:

> Please record these kickoff facts in the shared project memory so everyone on
> the team sees them: our response is due 16 October; Marcus drafts the
> response; Priya owns export control; Tom owns IP. **Add a line for it to the
> project memory index.**

The last sentence matters. See
[Project memory](/agentcore-public-stack/user-guide/projects/work-together/#project-memory).
