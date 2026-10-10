---
title: Projects
description: Oversee Shared Projects, turn the feature off per environment, and configure its limits.
sidebar:
  order: 12
---

Operator notes for [Shared Projects](/agentcore-public-stack/features/projects/).
The design and its as-built record are in
[`docs/specs/shared-projects.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/docs/specs/shared-projects.md).

## The `admin.projects` scope

`admin.projects` is a delegable admin scope in the Agent Marketplace group.
An administrator with it can act on any project without being a member of it.
There is no admin page yet; the scope guards these app-api routes:

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/admin/projects?limit&cursor` | Every project, paginated by project id |
| GET | `/admin/projects/{id}` | One project: owner, status, member count, harness agent id |
| PATCH | `/admin/projects/{id}` `{status, reason}` | Force-archive (`archived`) or restore (`active`). The reason is kept on the project's trail |
| GET | `/admin/projects/{id}/audit?limit&cursor` | The project's full audit trail, user ids included |

An archived project is read-only for every member, the owner included, until it
is restored. Only the owner can delete a project, and only once it is
archived. Admins archive; they do not purge.

Every admin change is recorded on the project's trail with the admin as actor.
Members with the editor role see the same trail, by email and without user ids,
in the project's **Activity** dialog.

## Turning Projects on

Projects are still in development, so they are **off unless a deployment turns
them on**. Two switches, one per side, and they should agree:

- **Backend (the real gate):** set the GitHub environment variable
  `CDK_PROJECTS_ENABLED` to `true` and run the platform deploy. CDK passes
  `PROJECTS_ENABLED=true` to app-api, the AgentCore Runtime and the
  scheduled-runs dispatcher and worker. Unset or any other value means off.
  Turning it off later pauses every project schedule at its next due time.
- **Front end:** `features.projects` in the SPA's environment file for that
  build: `environment.development.ts` for the deployed dev site,
  `environment.production.ts` for prod, `environment.ts` for local `ng serve`.
  It decides whether the UI offers Projects at all (the nav item, the
  notification bell, the `/projects` routes, project headings in the
  conversation list, "Project members" sharing). It takes effect on the next
  frontend deploy.

If the two disagree, the damage is cosmetic: the UI offers Projects and the page
says they aren't available, or the UI hides Projects that would work.

While the backend switch is off:

- `/projects/**` returns 404 to signed-in users, and `/admin/projects` is not
  mounted. The `/projects` page says Projects aren't available in this
  environment.
- A project's assistant refuses everyone: its members and its creator. A turn in
  an existing project task gets a message in the conversation saying Projects
  are turned off, and the agent routes (documents, sync policies) refuse its
  assistant too.
- A conversation can no longer be shared with "Project members". Existing
  project shares open only for the person who shared them.
- Nothing is deleted. Turning Projects back on restores everything as it was.

## Configuration

| Variable | Service | Default | Purpose |
| --- | --- | --- | --- |
| `PROJECTS_ENABLED` | app-api, inference-api, scheduled-runs dispatcher and worker | off | Only `true` enables (see above). Set by CDK from `CDK_PROJECTS_ENABLED` |
| `DYNAMODB_PROJECTS_TABLE_NAME` | app-api, inference-api | — | The `{prefix}-projects` table. Set by CDK |
| `DYNAMODB_AUDIT_LOG_TABLE_NAME` | app-api | — | Where the Activity trail is written. Without it, nothing is recorded |
| `PROJECTS_MAX_MEMBERS` | app-api | `200` | Members per project, besides the owner. Invitations past the cap are reported, not added |
| `PROJECTS_EDITORS_MANAGE_MEMBERS_DEFAULT` | app-api | on | Whether a new project lets editors manage members. The owner can change it per project |
| `DIRECTORY_PROVIDER` | app-api | `users_table` | Where the member picker searches. `users_table` is the only provider today |

The last three are not set by CDK. Set them on the app-api task only to
change the default.

## Memory content check

Every save into a project's memory (an edit on the Memory page, the
assistant's `memory_save` and `memory_propose`, an approved proposal, a
restore from the archive or from history, and a tidy-up's merges and new
files) goes through a deterministic content check. It looks for:

- text that reads like an instruction to the assistant ("ignore previous
  instructions", "you must now", "new instructions:");
- prompt or tool-call markup (`</memory_space>`, `<function_calls>`,
  `"toolUse":`);
- credential-shaped strings (AWS access keys, private keys, GitHub, Slack and
  API keys, signed tokens, `password: …`, a password in a URL);
- your deployment's own patterns, if you set any.

There is no model and no redaction. It is a flag for people, on top of the
structural rule that memory reaches a task as labelled reference material,
never as instructions.

| Variable (GitHub) | Values | Default | What it does |
| --- | --- | --- | --- |
| `CDK_MEMORY_LINT_MODE` | `off`, `warn`, `block` | `warn` | `warn` saves and reports what it found. `block` refuses the save with a sentence saying what to take out. `off` skips the check |
| `CDK_MEMORY_SENSITIVE_PATTERNS` | Python regular expressions | none | A JSON list of strings or `{"pattern": "…", "label": "a student ID"}` objects, or one pattern per line. At most 3,000 characters |

```json
[{"pattern": "\\bS\\d{8}\\b", "label": "a student ID"}, "(?i)\\bconfidential\\b"]
```

- **What it reads.** Only the items a save adds or changes; an item already in
  the file, word for word, isn't read again. A changed description is read,
  since it becomes the file's index line, and so is each new line of the
  index. **Only new findings block:** an item that already had a finding
  saves with a warning even in `block` mode, so turning `block` on never stops
  an unrelated edit. A restore is read like any save, so `block` keeps out
  text that an older setting allowed.
- **Where people see it.** The save's response (and the assistant's tool
  result), a warning toast on the Memory page, a line under each flagged item
  in the file view, and a banner in the review queue for what a proposal adds.
  Findings are worked out again on each read and never stored, so nothing is
  written into memory and nothing changes what a task sees.
- **Scope.** A project's memory only, both the shared memory and each member's
  own. Personal memory spaces and agent memory outside Projects are not
  checked, so this rides `PROJECTS_ENABLED`; it is configuration, not a
  feature flag.
- **Patterns.** A pattern that doesn't compile is skipped and logged, never a
  failed save. One that nests a quantified group inside a quantifier, such as
  `(a+)+`, is refused for the same reason, since it can take minutes on a long
  item. A pattern's label is what people see ("matches a pattern this
  deployment treats as sensitive (a student ID)"); a match is never quoted
  back, and neither is a credential.
- **Cost.** On an 8,000-token file with the 14 built-in rules and 36
  deployment patterns, a first save of the whole file takes about 20 ms; an
  ordinary one-item edit about 0.1 ms. Nothing runs before a task's first
  token.
- **Wiring.** CDK passes `MEMORY_LINT_MODE` and `MEMORY_SENSITIVE_PATTERNS` to
  app-api and the maintenance worker. The AgentCore Runtime's environment is
  near its limits, so it gets one small value, `MEMORY_LINT` (the mode), and
  reads the patterns from the SSM parameter
  `/{prefix}/memory/sensitive-patterns`, once per process. CDK creates that
  parameter only when you set patterns, and a change to them rolls the
  Runtime. An unknown mode or a malformed list fails the synth.
- **Per project.** There is no per-project setting yet. A project designated
  as holding regulated data (Phase 4) will force `block`.

## Memory export

Any member can download a scope's memory from **Export** on the Memory page
(`GET /projects/{id}/memory/export?scope=project|mine`). The zip holds
`MEMORY.md`, each file with its frontmatter under `entries/`, `metadata.json`,
and `provenance.json`: for each file, its version, its pinned items, and each
item by anchor with who added it and when, who last changed it, the task it
came from, the proposal, proposer and approver, who restored it, and the file
a tidy-up moved it from. People are emails. The same `provenance.json` is in
the ordinary memory-space export for any item-format space.

## Where the data lives

| Store | What |
| --- | --- |
| `{prefix}-projects` | Project, member and shared-task rows, schedule pointers (`SCHEDULE#{id}`) and run rows (`SCHEDULE_RUN#…`, 90-day TTL), monthly cost rollups (`COST#{YYYY-MM}` and one per member), and every user's notification inbox (`INBOX#{email}`, 90-day TTL) |
| `{prefix}-rag-assistants` | Each project's assistant: a hidden agent (`kind = "project"`) that never appears in agent lists or the marketplace. Its versions are the project's settings history |
| The assistant's knowledge base | The project's files |
| `{prefix}-sessions-metadata` | Tasks are ordinary sessions with `preferences.projectId`, indexed by `ProjectSessionIndex`. A project schedule is a scheduled prompt in its creator's partition (`USER#{id}` / `SCHEDPROMPT#{id}`) with `projectId` |
| `{prefix}-shared-conversations` | Shares with `access_level = "project"` |
| `{prefix}-audit-log` | The project trail, `AUDIT#project#{id}` |
