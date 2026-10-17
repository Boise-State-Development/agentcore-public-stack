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

## Providers you add here survive PlatformStack deploys

When you add a provider on this page, app-api registers it in the Cognito user pool and adds it to the BFF app client's supported identity providers. CloudFormation doesn't know about that second change. The template's list holds only `COGNITO` plus whatever `CDK_COGNITO_SUPPORTED_IDPS` names, which is usually nothing.

So a PlatformStack deploy that changes **any** property of the app client can drop your provider. Callback or logout URLs, OAuth scopes and token settings all count. CloudFormation sends the template's full settings, and Cognito resets every field to what the request says. Afterwards the provider's button is still on the sign-in page, because the SPA reads the list from DynamoDB. But Cognito rejects the sign-in, because the app client no longer lists the provider. A deploy that doesn't touch the app client leaves it alone.

app-api repairs this on its own. No configuration or post-deploy step is needed.

| | |
|---|---|
| **When** | Once at app-api startup, then about every minute on each app-api task. |
| **What** | Each **enabled** provider missing from the app client is added back. It also runs when you re-enable a provider. |
| **What it never does** | Remove anything. A provider CDK manages, one you disabled here, and `COGNITO` are left as they are. |
| **Cost** | One `DescribeUserPoolClient` and one small DynamoDB query per task per minute. It writes only when something is missing, and logs a `WARNING` naming what it restored. |
| **Gap** | Up to about a minute after the deploy, while federated sign-in fails. `/auth/login?local=1` still works then, since Cognito sign-in is never dropped. |

To close the gap completely, also list the provider's name (its **Provider ID**) in the `CDK_COGNITO_SUPPORTED_IDPS` GitHub environment variable (comma-separated). The template then carries it, and deploys keep it. Only add a name once the provider exists in the user pool, because CloudFormation refuses an app client that names a provider the pool doesn't have.

To take a provider off the sign-in page, disable or delete it here. Removing it from the app client in the AWS console doesn't stick while it's enabled here: app-api adds it back.
