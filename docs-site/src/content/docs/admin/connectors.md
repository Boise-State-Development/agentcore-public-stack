---
title: Connectors
description: External OAuth connectors for tools.
sidebar:
  order: 7
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

AI Configuration group. Configure OAuth providers that tools connect through; backed by `admin/oauth` and the `/admin/connectors` pages.

## Microsoft (Entra ID)

A Microsoft connector has one setting no other vendor has: **Directory (tenant) ID**. Set it to the app registration's directory (tenant) ID or one of the tenant's verified domains (`contoso.onmicrosoft.com`), both shown on the registration's Overview page. A single-tenant registration, which is what Entra creates by default, can only sign users in through its own tenant, so set the field for one. Leave it empty only for a multi-tenant registration.

The tenant is part of the credential config AgentCore Identity holds, so changing or clearing it on an existing connector requires re-entering the client ID and secret, the same as a discovery URL change.

Microsoft issues an access token for **one resource at a time**. A connector whose scopes are for Microsoft Graph (`User.Read`, `Calendars.Read`) cannot also call another Microsoft API. For one that can, such as Azure DevOps, create a separate connector whose scopes name that resource, for example `499b84ac-1321-427f-aa17-267ca6975798/user_impersonation` plus `offline_access`, and grant that delegated permission on the app registration.
