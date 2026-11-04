---
title: Conversation Modes
description: Admin-authored system prompts that users apply to a conversation, how they compose with the platform prompt, and what editing one costs.
sidebar:
  order: 12
---

Customization group. A **conversation mode** is a named system prompt an
admin writes once and users opt into per conversation: for example a "Guided
Learning" mode that tutors with questions instead of answers. The mode's
instructions are appended to the platform system prompt on every turn of a
conversation that has it selected. Managed under `/admin/system-prompts`, scope
`admin.system_prompts`.

Conversation modes have no feature flag. The catalog lives in its own DynamoDB
table (`system-prompts`); if `DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME` is not set,
the repository is disabled and every list is empty.

## Fields

| Field | Required | Limits | What it does |
|---|---|---|---|
| Name | Yes | 1 to 128 characters | Shown to users, and written into the system prompt as the mode's heading |
| Description | Yes | 1 to 512 characters | Shown to users to explain what the mode is for |
| Prompt text | Yes | 1 to 8,000 characters | The instructions appended to the system prompt. Users never see it |
| Status | No | `enabled` or `disabled`; defaults to `enabled` | Disabled modes are hidden from users and are not applied, even to conversations that already selected them |

The service also records who created the mode (`created_by`, the admin's
email) and created and updated timestamps.

The user-facing list returns only enabled modes, sorted by name, with their
id, name and description. The prompt text is only ever returned by the admin
routes.

## What users see

A user picks a mode for one conversation, not for their account. The choice is
stored on the session as `selectedPromptId` and sent with each turn as
`selected_prompt_id`.

- **Web app.** No picker currently ships. The composer settings drawer that
  held the Mode control was removed with the Customize work, and the
  replacement composer picker was parked before release. The SPA service, the
  user-facing list and the per-session preference are all still in place, and
  conversations that already carry a mode keep applying it. See the
  conversation-mode breaking-change entry in `RELEASE_NOTES.md`.
- **Terminal client.** `/mode` opens a picker, `/mode <name>` sets a mode by
  name, and `/mode off` clears it. It takes effect on the next turn.

Setting a mode through `PUT /sessions/{id}/metadata` checks that it exists and
is enabled, and returns 400 otherwise. Sending `selectedPromptId: null` clears
it; leaving the field out leaves it unchanged.

## How a mode is applied

On each turn the inference API resolves the active mode in
`apply_turn_prompt_extras` (`inference_api/chat/system_prompt_resolver.py`):

1. It takes the turn's `selected_prompt_id`, or, if the request carries none,
   the session's stored `selectedPromptId`. An id sent with the request is also
   written back onto the session so a reload or another device picks it up.
2. It reads that mode from the table. A mode that is missing or disabled is
   skipped: the turn runs without it rather than failing.
3. It appends the mode to the system prompt in this format:

```text
<platform system prompt>

## Active Mode: <name>

<prompt text>
```

The user's personal instructions, if they have any, follow the mode.

A mode is **not** applied on:

| Turn | Why |
|---|---|
| A turn that runs on an Agent | Agents bring their own instructions, which a mode could contradict |
| A resumed turn (after an interrupt such as a tool approval) | The resume replays the system prompt captured when the turn paused |
| A max-tokens continuation | Re-rendering the prompt mid-turn would invalidate the prompt cache for the truncated reply |
| An Agent preview session | Preview uses the live form's instructions |

So on an Agent turn the system prompt is built by `compose_turn_system_prompt`
from the platform prompt and the Agent's instructions, and no mode is added.

## Editing and deleting a mode

The inference API reads the mode by id on every turn, so an edit to its name,
prompt text or status applies from the next turn of every conversation using
it. The user-facing list is cached per process for about 60 seconds
(`CONFIG_CACHE_TTL_SECONDS`), so a new or renamed mode can take up to that long
to appear in it.

Deleting a mode removes it from the table. Conversations that had it selected
fall back to running without a mode. Disabling has the same effect on turns
and keeps the mode for later.

Changes to conversation modes are not recorded in the
[audit log](/agentcore-public-stack/admin/overview/#audit-log).

### Prompt-cache cost

The system prompt is part of the cached prompt prefix, ahead of the
conversation history. A mode's name and prompt text are written into that
prompt, so changing either changes the prefix for every conversation using the
mode. The next model call in each of those conversations re-writes the system
prompt and the history behind it at the cache-write rate instead of reading
them from the cache. Turning a mode on, off or to another mode mid-conversation
does the same for that conversation.

Edit a mode that many conversations use sparingly. To check the effect, compare `systemPromptHash`
on the session's `C#` rows, or use `GET /admin/costs/sessions/{id}/calls`.

## Routes

| Route | Purpose |
|---|---|
| `GET /admin/system-prompts/?enabled_only=` | Every mode, enabled and disabled, with prompt text |
| `GET /admin/system-prompts/{prompt_id}` | One mode |
| `POST /admin/system-prompts/` | Create a mode |
| `PATCH /admin/system-prompts/{prompt_id}` | Partial update; any field may be omitted |
| `DELETE /admin/system-prompts/{prompt_id}` | Delete a mode |
| `GET /system-prompts/` | Any signed-in user: enabled modes, name and description only |

All admin routes require `admin.system_prompts`, which a system admin can
delegate to a role. See [Admin Overview](/agentcore-public-stack/admin/overview/#admin-scopes).
