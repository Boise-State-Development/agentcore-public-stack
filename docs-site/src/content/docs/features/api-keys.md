---
title: API Keys
description: Call the platform's models from a script with a personal API key and the X-API-Key header.
sidebar:
  order: 9
---

An API key lets a script call the models you can already use in the web app,
without a browser session. Each user manages their own key under
**Settings → API Keys** (`/settings/api-keys`). Requests send it in the
`X-API-Key` header to one endpoint, `POST /chat/api-converse`.

This is a plain model call, not the agent. There are no tools, no skills, no
memory and no saved conversations: you send the messages, the model answers.
For the full agent from a terminal, sign in with the
[terminal client](/agentcore-public-stack/features/terminal-client/) instead.

There is no feature flag and no role gate on keys themselves. What a key can
reach is decided per request by the same roles and quotas as the web app.

## Managing your key

| Action | How |
|---|---|
| Create | **Settings → API Keys**, enter a name (for example `my-app`) and create |
| See it | The page shows the key's name, created date, expiry and last use, never the key itself |
| Replace | Creating a new key while one exists asks you to confirm, then deletes the old one |
| Revoke | The delete button on the key; it stops working at once |

Things worth knowing:

- **One key per user.** Creating a key replaces any key you already have, and
  anything still using the old one stops working immediately.
- **Shown once.** The raw key is returned only in the create response, and the
  page asks you to copy it then. The platform stores only a SHA-256 hash of it,
  so it cannot show it again. If you lose it, create a new one.
- **Keys expire after 90 days.** The expiry is fixed; there is no setting for
  it. An expired key gets `401 Invalid or expired API key`. Create a new key
  before the old one runs out.
- Treat the key like a password: anyone holding it can spend your quota.

The same operations are available as `POST`, `GET` and `DELETE /auth/api-keys`.
They authenticate with your browser session, not with an API key, so a key
cannot mint or revoke keys.

## Making a request

```bash
curl -X POST "https://ai.example.com/api/chat/api-converse" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "messages": [
      {"role": "user", "content": "Hello, what can you help me with?"}
    ]
  }'
```

The base URL is your deployment's web address with `/api` appended. You do
not need to work it out: the **Usage Examples** section of the API Keys page
generates ready-to-copy cURL, Python and JavaScript for your deployment. Pick a
model from the ones you can use, streaming or non-streaming, a single message
or a conversation with history, and optionally set temperature, max tokens,
top P and a system prompt. The **Available Models** list on the same page shows
each model id you may pass, with a copy button.

### Request body

| Field | Type | Default | Notes |
|---|---|---|---|
| `model_id` | string | required | A model id from the catalog, as shown under **Available Models** |
| `messages` | array | required | `{"role": "user" \| "assistant", "content": "<text>"}`; must not be empty. Send earlier turns yourself for a multi-turn conversation |
| `system_prompt` | string | none | Instructions for the model |
| `temperature` | number | model default | |
| `max_tokens` | integer | `4096` | |
| `top_p` | number | model default | |
| `stream` | boolean | `false` | `true` returns Server-Sent Events |

Content is text only.

### Non-streaming response

```json
{
  "role": "assistant",
  "content": "Hi! I can help with ...",
  "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "usage": {"inputTokens": 14, "outputTokens": 52, "totalTokens": 66},
  "stop_reason": "end_turn",
  "reasoning": null
}
```

`reasoning` carries the model's thinking text when a reasoning model returns
one. If the model you asked for has been retired in favour of a successor, the
call runs on the successor and `model_id` names the model that actually
answered.

### Streaming response

With `"stream": true` the response is `text/event-stream`. Each frame is an
`event:` line and a JSON `data:` line:

| Event | Data |
|---|---|
| `message_start` | `{"role": "assistant"}` |
| `content_block_start` | `{"contentBlockIndex", "type": "text"}` |
| `content_block_delta` | `{"contentBlockIndex", "type": "text", "text"}`: the next piece of the answer |
| `reasoning_start` / `reasoning_delta` / `reasoning_stop` | Thinking text from a reasoning model, around its deltas |
| `content_block_stop` | `{"contentBlockIndex"}` |
| `message_stop` | `{"stopReason"}` |
| `metadata` | `{"usage", "metrics"}` |
| `error` | `{"error"}`: the model call failed after the stream opened |
| `done` | `{}`: always the last frame |

Join the `text` of the `content_block_delta` frames to get the answer. With
cURL, add `--no-buffer` to see tokens as they arrive.

## Same models, roles and quotas

A key carries no permissions of its own. On every request the platform reads
the key owner's roles back from the users table, so the call resolves the same
grants as the web app:

- **Models:** only models your roles grant are accepted. A model you cannot use
  returns `403 Access denied to model`. A disabled catalog model is refused
  the same way it is in chat.
- **Quota:** each call's cost is recorded against your monthly quota, and a
  call that would exceed it returns `429` with the quota message. Usage from
  the API and the web app is one total.
- **Retired models:** a retired model with no successor returns `410`.

## Errors

| Status | Meaning |
|---|---|
| `400` | Empty `messages`, or Bedrock rejected the request (model id, message format or content policy) |
| `401` | Missing, unknown or expired key |
| `403` | Your roles do not grant the model, or the model is not available |
| `410` | The model is retired and has no successor |
| `429` | Rate limit, in-flight cap, quota exceeded, or the model is throttling (see below) |
| `502` | The model call failed |

## Limits

Two limits apply to `POST /chat/api-converse`, and they measure different things:

| Limit | Scope | When it trips | Response |
|---|---|---|---|
| Rate | 60 requests a minute, per key | A script sends faster than that, whatever the calls' duration | `429`, `Retry-After: 60` |
| In flight | `API_CONVERSE_MAX_IN_FLIGHT` concurrent Bedrock calls, per app-api task (default 16) | Enough calls are still running at once, however slowly they were sent | `429`, `Retry-After: 5` |

The in-flight cap exists because a model call on this surface can take tens of seconds, so a script that stays under the rate limit can still hold a dozen calls open at once. Past the cap a call is refused immediately rather than queued, because a queued call would sit against the load balancer's 60-second idle timeout and fail anyway. Treat both `429`s the same way: wait for `Retry-After`, then resend.

A deployment sets the cap with the `CDK_APP_API_CONVERSE_MAX_IN_FLIGHT` GitHub environment variable (see `appApi.apiConverseMaxInFlight` in `infrastructure/lib/config.ts`). Raise it together with the task's CPU: each in-flight call holds a worker thread for the life of the model call.

Long completions should set `stream: true`. A non-streaming response has to finish inside the load balancer's 60-second idle timeout; a streamed one keeps the connection alive with each token.
