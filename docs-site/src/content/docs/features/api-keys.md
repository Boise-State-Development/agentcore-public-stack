---
title: API Keys
description: Programmatic access with X-API-Key.
sidebar:
  order: 9
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

Per-user keys minted at `/auth/api-keys` (create/list/revoke); requests authenticate with the `X-API-Key` header instead of the session cookie. The raw key value is shown only once at creation.
