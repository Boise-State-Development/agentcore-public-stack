---
title: Environment Variables
description: Key environment variables across services.
sidebar:
  order: 1
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

Document feature flags such as AGENTCORE_MCP_APPS_HOST_ENABLED, CDK_MCP_SANDBOX_ENABLED, and SKIP_AUTH.

## Shared Projects

| Variable | Service | Default | Notes |
| --- | --- | --- | --- |
| `CDK_PROJECTS_ENABLED` | GitHub environment variable → CDK | off | Set to `true` to turn Projects on in that environment |
| `PROJECTS_ENABLED` | app-api, inference-api | off | Set by CDK from the above; only `true` enables |
| `DYNAMODB_PROJECTS_TABLE_NAME` | app-api, inference-api | — | `{prefix}-projects`, set by CDK |
| `DYNAMODB_AUDIT_LOG_TABLE_NAME` | app-api | — | Required for the project Activity trail |
| `PROJECTS_MAX_MEMBERS` | app-api | `200` | Not set by CDK |
| `PROJECTS_EDITORS_MANAGE_MEMBERS_DEFAULT` | app-api | on | Not set by CDK |
| `DIRECTORY_PROVIDER` | app-api | `users_table` | Not set by CDK |

The SPA has a matching switch, `features.projects`, in its environment files. Both sides, and what the switch stops: [Admin › Projects](/agentcore-public-stack/admin/projects/#turning-projects-on).
