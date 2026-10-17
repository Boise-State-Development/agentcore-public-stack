---
title: Auth Providers
description: Configure sign-in identity providers.
sidebar:
  order: 10
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

Identity & Access group. Manage the IdPs users authenticate with; backed by `admin/auth_providers` and the `/admin/auth-providers` pages.

## Sign-in page: hiding Cognito sign-in

The sign-in page offers **Sign in with Cognito** (the user pool's own username and password) above any providers you add here. An organization that signs everyone in through a federated provider, such as Microsoft Entra ID, can hide that button so users aren't asked to choose.

Turn on **Hide Cognito sign-in** in the **Sign-in page** panel at the top of **Admin → Auth Providers**. Only a `system_admin` can change it: like the rest of this page, it sits under the non-delegable `admin.auth_providers` scope.

| | |
|---|---|
| **Default** | Shown. A deployment that never touches the setting, including every fork that upgrades into it, keeps today's sign-in page. |
| **Takes effect** | On the next sign-in page load. No deploy, environment variable or feature flag is involved. |
| **Stored** | One `SYSTEM_SETTINGS#login-page` item in the auth-providers table, beside the first-boot record. |
| **API** | `GET` / `PUT /admin/auth-providers/login-page-settings` (`{"hide_cognito_login": true}`). The public `GET /auth/providers` adds `cognito_login_visible`, which a client that doesn't know the field can ignore. |

### It can't lock you out

- **Hiding is cosmetic.** Cognito stays an identity provider on the app client, so Cognito users (including the first-boot admin) can still sign in.
- **Break-glass URL.** `/auth/login?local=1` always shows the Cognito button. Use it to reach the admin console if the federated provider is misconfigured.
- **Never the only way in.** The setting can't be turned on while no provider is enabled (`422`). If every provider is later disabled or deleted, the sign-in page shows the Cognito button again even though the setting is still on, and the panel says so.

To stop password sign-in altogether, rather than hide the button, `COGNITO` would have to come off the app client's supported identity providers in CDK. That is not offered, because it would lock out the first-boot admin.
