---
title: Users
description: View and manage user accounts.
sidebar:
  order: 8
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

Identity & Access group. Browse users and inspect their detail/sessions; backed by `admin/users` and the `/admin/users` pages.

## Direct access

With direct user grants turned on (`CDK_USER_GRANTS_ENABLED=true` and
`features.userGrants: true`, off by default), a user's detail page gains a
**Direct access** section for system admins. It grants tools, models and
skills to **this one user** beside whatever their roles give: a pilot user, one
researcher's model, a temporary exception.

- Tick rows in the three pickers, optionally set an **Expires** time and a
  **Note** saying why, and **Save**. Saving with nothing ticked removes the
  grant; **Remove grant** does the same in one step.
- A grant is additive. It never takes away what a role gives; to take a model or
  tool away from everyone, disable it in its catalog.
- An expired grant stays on the page, marked **Expired**, until it is removed,
  so the history is visible.
- The user sees the change at once on app-api (the tool picker marks such tools
  *direct*) and within about five minutes on the chat path.

Every save and removal is written to the audit log with the lists' before and
after. Only a system admin can use this section: granting to
a person is role editing by another route, so it cannot be delegated. See
[RBAC and Permissions](/configuration/rbac/#direct-user-grants).
