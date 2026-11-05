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

## Finding a user

The **User Lookup** search box matches part of a name, an email or a user ID,
ignoring case and accents, and starts a search once you stop typing (Enter
searches at once). Results are ranked exact match first, then matches at the
start of a name or email, then matches anywhere, then near misses, so a small
typo such as `osacr filson` still finds Oscar Filson. Within each group the most
recent sign-in comes first.

- The **status** filter (all, active, inactive, suspended) and the **domain**
  filter (shown when users come from more than one email domain) apply to the
  search as well as to the plain list. Every row is labelled with its status.
- **Load More** pages through every match, not just the first page of users.
- Searching a whole email lists every profile for it. When more than one
  profile shares an email, the one marked **Signs in** is the live account and
  the others are leftovers from an earlier login setup.

The search works on a copy of the user list held in each app-api task. It is
re-read from the users table at most once a minute, and only while admins use
the page. One read costs a fraction of a cent even with 100,000 users, and
needs no extra AWS resources. A user who signed in for the first time a moment
ago may take up to a minute to show up, except that searching their whole email
always finds them. `ADMIN_USER_INDEX_MAX_ROWS` (default 100,000) caps how many
profiles the copy holds.

## Direct access

With direct user grants on (the default; `CDK_USER_GRANTS_ENABLED=false` and
`features.userGrants: false` turn them off), a user's detail page gains a
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
