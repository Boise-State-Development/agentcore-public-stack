---
title: RBAC and Permissions
description: Role-based access and tool visibility.
sidebar:
  order: 4
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

enabled_tools, ToolRegistry, admin (/admin/<domain>/) vs user-facing (/<domain>/) endpoints.

## Direct user grants

A role is how access is handed out. A **direct user grant** is the exception
path: tools, models and skills granted to one user beside whatever their roles
give them — one person piloting a tool before their cohort gets it, a
researcher who needs a model nobody else in their role does, a temporary
exception with an end date.

- **Additive only.** A grant can widen what a user's roles give, never narrow
  it. To take a model or tool away from everyone, disable it in its catalog.
- **Same ids as a role.** Base tool ids (a whole MCP server, not a
  `server::tool` ref), provider model ids, skill ids. No `*`: wildcard access is
  a role, and a per-person superuser is a role assignment.
- **Optional expiry.** `expiresAt` (ISO 8601, UTC) makes the grant lapse by
  itself; the row stays for the admin to see and remove.
- **Resolved with the roles.** The grant is unioned into the user's effective
  permissions, so every check — the chat turn, the tool picker, Agent Designer
  bindings, scheduled runs — honours it with no special case. A change lands
  within the per-user permission cache TTL (5 minutes) on the AgentCore
  Runtime; app-api sees it at once.
- **Quota tier is not a grant.** Per-user quota already has its own direct
  assignment under Quota; admin scopes are delegated by role only.

Written through `PUT /admin/user-grants/{userId}` (full replace; three empty
lists remove the grant), read back with `GET`, removed with `DELETE`, and
listed by resource with `GET /admin/user-grants/for/{tool|model|skill}/{id}`.
Every write is audited (`user_grant.updated` / `user_grant.deleted`) with the
lists' before and after. The surface is **system admin only** and never
scope-delegable: whoever can write a grant can write one for themselves, which
is role editing by another route.

Opt-in while in development: `CDK_USER_GRANTS_ENABLED=true` (app-api) and
`features.userGrants: true` (SPA). See [Feature flags](/configuration/feature-flags/).
