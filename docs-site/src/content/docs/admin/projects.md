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
on the project's **Activity** tab.

## Turning Projects on

Projects are still in development, so they are **off unless a deployment turns
them on**. Two switches, one per side, and they should agree:

- **Backend (the real gate):** set the GitHub environment variable
  `CDK_PROJECTS_ENABLED` to `true` and run the platform deploy. CDK passes
  `PROJECTS_ENABLED=true` to app-api and the AgentCore Runtime. Unset or any
  other value means off.
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
| `PROJECTS_ENABLED` | app-api, inference-api | off | Only `true` enables (see above). Set by CDK from `CDK_PROJECTS_ENABLED` |
| `DYNAMODB_PROJECTS_TABLE_NAME` | app-api, inference-api | — | The `{prefix}-projects` table. Set by CDK |
| `DYNAMODB_AUDIT_LOG_TABLE_NAME` | app-api | — | Where the Activity trail is written. Without it, nothing is recorded |
| `PROJECTS_MAX_MEMBERS` | app-api | `200` | Members per project, besides the owner. Invitations past the cap are reported, not added |
| `PROJECTS_EDITORS_MANAGE_MEMBERS_DEFAULT` | app-api | on | Whether a new project lets editors manage members. The owner can change it per project |
| `DIRECTORY_PROVIDER` | app-api | `users_table` | Where the member picker searches. `users_table` is the only provider today |

The last three are not set by CDK. Set them on the app-api task only to
change the default.

## Where the data lives

| Store | What |
| --- | --- |
| `{prefix}-projects` | Project, member and shared-task rows, monthly cost rollups (`COST#{YYYY-MM}` and one per member), and every user's notification inbox (`INBOX#{email}`, 90-day TTL) |
| `{prefix}-rag-assistants` | Each project's assistant: a hidden agent (`kind = "project"`) that never appears in agent lists or the marketplace. Its versions are the project's settings history |
| The assistant's knowledge base | The project's files |
| `{prefix}-sessions-metadata` | Tasks are ordinary sessions with `preferences.projectId`, indexed by `ProjectSessionIndex` |
| `{prefix}-shared-conversations` | Shares with `access_level = "project"` |
| `{prefix}-audit-log` | The project trail, `AUDIT#project#{id}` |
