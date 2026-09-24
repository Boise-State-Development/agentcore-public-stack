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

## Turning Projects off

Projects are **on by default**. To turn them off in one environment, set the
GitHub environment variable `CDK_PROJECTS_ENABLED` to `false` and redeploy.
CDK passes it on as `PROJECTS_ENABLED=false` to app-api and to the AgentCore
Runtime. Unset or any other value means on.

While off:

- `/projects/**` returns 404 to signed-in users, and `/admin/projects` is not
  mounted.
- The sidebar still shows **Projects**; the page says Projects aren't available
  in this environment.
- A conversation can no longer be shared with "Project members". Existing
  project shares open only for the person who shared them.
- Nothing is deleted. Turning Projects back on restores everything as it was.

:::caution[Not a full stop]
The switch gates app-api. The inference API does not read it, so a member can
still run a turn in an **existing** project task on the project's assistant.
New tasks can't be started from a project page, because that page is gone while
the switch is off.
:::

## Configuration

| Variable | Service | Default | Purpose |
| --- | --- | --- | --- |
| `PROJECTS_ENABLED` | app-api, inference-api | on | Kill switch (see above). Set by CDK from `CDK_PROJECTS_ENABLED` |
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
