---
title: Tips and current limits
description: How to get reliable answers from a project's assistant, and what Projects don't do yet.
sidebar:
  label: Tips and current limits
  order: 5
---

## Ask questions the files can answer

Before the assistant answers, it searches the project's files once, using
**the words of your message**, and it can open any **Ready** file whole when you
name it. So either the wording of your question decides which passages it gets
to see, or the file name decides which document it reads.

- **Name the file** when you want work on one document: "read
  sponsor-v2-redline.docx and list every change", "grade submission 7 against
  the rubric". The assistant can list the project's files and read a named one
  in full, so it never has to guess from a search which document you meant.
- **Name the section and the subject.** "Playbook section 7, export control:
  what is the standard position on foreign nationals?" finds the right
  passage. A long message that opens with a greeting and ends with three
  questions often doesn't.
- **One topic per message** when you need the files. Ask the follow-ups next.
- **Paste the exact clause** you're asking about. Quoted text is the most
  reliable way to get the right passage back.
- **Ask what it couldn't see.** End important requests with "…and tell me what
  you could NOT see, so I know how complete this is."
- If an answer says a document "isn't available", check **Files**. It may still
  be **Setting up**.

## Keep project memory useful

- **Decide who records decisions**, usually the lead editor, and have them say
  "record in project memory …". The assistant adds a line for each new memory
  to the index on its own.
- Open **Memory** at the start of a review session to see what the assistant
  will rely on, and who put it there.
- **Pin** the facts everything else depends on, so no later change can drop
  them by accident.
- **Correct mistakes immediately.** Anything saved to project memory is
  repeated in every teammate's conversations.
- Use **"just for me"** memory for preferences that apply to this project, and
  **personal instructions** for your role and how you like answers formatted
  everywhere.

## Keep files clean

- One current version of each document. Delete superseded drafts.
- Name each redline so it's clear whose draft it is and which round
  ("Sponsor v2 redline", "University response v3"). When two redlines of the
  same contract are in Files, the name is how you and the assistant tell them
  apart.

## Review before you rely on it

The assistant drafts quickly and cites its sources, but in testing it still:

- miscalculated a cash-flow figure, even with the Calculator tool on;
- labeled a clause "non-negotiable" when the playbook said "escalate";
- missed one of eight requested edits in a redlined document.

Treat its output as a strong first draft that a qualified person reviews before
it is saved to memory or sent outside the university.

## Current limits

:::caution[As of October 2026]
These reflect the current release and are being worked on. Your administrator
can tell you which have changed on your deployment.
:::

**Working together**

- **Few notifications for team activity.** Archiving and restoring a project
  notify every member, the owner hears when someone leaves, and sharing a
  conversation notifies the people you choose. New files and changes to instructions don't
  notify anyone. Check **Activity**.
- **No comments or @-mentions** on shared conversations. To hand work to a
  specific person, share the conversation, notify them, and add a note.
- **Activity is for editors and the owner only.** Viewers don't see it.
- **People appear by email address**, not name or title.
- **Viewers can't add files or save to project memory directly.** They can
  propose a memory change for an editor to approve, share a conversation, or
  ask an editor to add a file.
- **The assistant reads a shared conversation's messages, not its attachments
  or tool results.** If a teammate's answer depended on a file, add the file to
  **Files** so everyone's conversations can use it.

**Continuing someone's conversation** ("Continue in my own conversation")

- **Attachments don't come along.** The copy says which files weren't copied.
  Attach your own copy of any you need, or ask for it to be added to Files.

**Files and documents**

- A file showing **Ready** can take a minute or two more before the assistant
  finds it in a search. It can be opened by name as soon as it shows **Ready**.
- **Reading by name is for text.** PDF, Word, text, Markdown and HTML files open
  whole, in pages. Spreadsheets, presentations and images are listed but can't
  be opened this way; attach those to the conversation instead.
- **Older redlines may still read flattened.** A redline added to Files before
  tracked changes were supported (October 2026) keeps its old text, with deleted
  and inserted wording run together. Delete it and upload it again (see
  [Word redlines in Files](/agentcore-public-stack/user-guide/projects/set-up/#word-redlines-in-files)).
- Documents the assistant creates are **saved to your conversation only**.
  There's no "save to project" button. Download the file and add it to Files.
- There's no version history for files. Each upload is a separate file.

**Memory and settings**

- **Memory history is per file.** Each file keeps every version, but the index
  (`MEMORY.md`) has no history, so copy it somewhere before a big rewrite.
- **No one-click revert** for instructions. Copy an old version from History
  and save it.
- **Switching models inside a project conversation** starts a new
  conversation in the project. The conversation so far doesn't carry over.

**Governance**

- The **Share** menu in a project conversation also offers **Public link**
  and **Limited share**, which share outside the project. There's no project
  setting to turn these off.
