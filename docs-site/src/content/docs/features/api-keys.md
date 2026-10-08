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


## Limits

Two limits apply to `POST /chat/api-converse`, and they measure different things:

| Limit | Scope | When it trips | Response |
|---|---|---|---|
| Rate | 60 requests a minute, per key | A script sends faster than that, whatever the calls' duration | `429`, `Retry-After: 60` |
| In flight | `API_CONVERSE_MAX_IN_FLIGHT` concurrent Bedrock calls, per app-api task (default 16) | Enough calls are still running at once, however slowly they were sent | `429`, `Retry-After: 5` |

The in-flight cap exists because a model call on this surface can take tens of seconds, so a script that stays under the rate limit can still hold a dozen calls open at once. Past the cap a call is refused immediately rather than queued, because a queued call would sit against the load balancer's 60-second idle timeout and fail anyway. Treat both `429`s the same way: wait for `Retry-After`, then resend.

A deployment sets the cap with the `CDK_APP_API_CONVERSE_MAX_IN_FLIGHT` GitHub environment variable (see `appApi.apiConverseMaxInFlight` in `infrastructure/lib/config.ts`). Raise it together with the task's CPU: each in-flight call holds a worker thread for the life of the model call.

Long completions should set `stream: true`. A non-streaming response has to finish inside the load balancer's 60-second idle timeout; a streamed one keeps the connection alive with each token.
