# TUI Web Parity

**Goal:** the terminal client is a full replacement for the web UI's user-facing
surface, not a chat demo. Every capability app-api exposes to a signed-in user
should be reachable from the terminal.

**Status:** **Phase 1 done and verified live.** Session-native chat against the
agent already worked (see `CLI_DEVICE_AUTH_SPEC.md`); this adds the server's real
model catalogue, a tool picker, and a conversation list with history restore.

**Audience:** whoever picks up the TUI next.

---

## The shape of the problem

Two surveys (SPA features, app-api surface) produced one clarifying finding:

> **The product is one route.** `''` and `s/:sessionId` both load
> `session/session.page.ts`. Everything else in the SPA is support CRUD around it.

So parity is mostly *depth on the conversation*, not breadth of screens. The
composer alone drives eleven request fields, and getting those right is worth
more than any number of list views.

A second finding sets the ordering: the SPA's own navigation is an accident.
`memories`, `files`, `schedules`, `manage-sessions` and `my-skills` are **not in
the sidenav** — they are reached through a user dropdown and admin-configured
menu links. The terminal should expose all of them as first-class commands
rather than reproducing that hierarchy.

## Inventory

424 operations across 51 tags; ~180 are non-admin. Grouped by what they are worth:

### Tier 1 — this *is* the product

| Capability | Endpoints | State |
|---|---|---|
| Agent turn | `POST /chat/stream` | ✅ done |
| Stop a turn | `POST /sessions/{id}/interrupt` | ✅ done |
| Model choice | `GET /models` | ✅ done — real catalogue, provider carried |
| Tool choice | `GET /tools/`, `PUT /tools/preferences` | ✅ done (`discover` pending) |
| Conversation list & resume | `GET /sessions`, `GET /sessions/{id}/messages` | ✅ done |
| Rename / delete / bulk delete | `PUT /sessions/{id}/metadata`, `DELETE`, `POST /sessions/bulk-delete` | ✅ rename + delete; bulk pending |
| Read / unread | `POST /sessions/{id}/read` \| `/unread` | ✅ done |
| Skills | `GET /skills/`, `PUT /skills/preferences` | ❌ |
| Conversation modes | `GET /system-prompts/` → `selected_prompt_id` | ❌ |
| File attachments | `POST /files/presign` → PUT → `POST /files/{id}/complete` | ❌ |
| Title generation | `POST /chat/generate-title` | ✅ client done, not yet auto-fired |
| Sampling overrides | `inference_params` | ❌ |
| Continue after truncation | `continue_truncated: true` with empty message | ❌ |
| Resume a paused turn | `interrupt_responses`, `GET .../pending-interrupts` | ❌ |

### Tier 2 — structured CRUD, cheap, high value

Files browser + quota; memory (facts, preferences, search, delete) and memory
spaces; schedules + `POST /runs/now`; my-skills CRUD; user settings
(`defaultModelId`); API keys; costs.

### Tier 3 — needs design, not just plumbing

Agents (list/get/`bindable`/`runnability`/pins, `@`-mention via
`rag_assistant_id` + `agent_mention`); sharing and export; connectors.

### Will not build, with reasons

| Feature | Why not |
|---|---|
| Voice mode | `wss://…/voice/stream` needs an allowlisted `Origin`, a single-use ticket, **and the session cookie read off the handshake** — WebSockets skip `SessionRefreshMiddleware`, so there is no `Authorization: BFF` branch to use. The audio layer (`getUserMedia` + AudioWorklet at 16 kHz) has no terminal equivalent either. |
| Artifact rendering | A sandboxed iframe with a render token. Partly salvageable: `GET /artifacts/{id}/content` can download to disk, which is what we will do. |
| MCP Apps | Cross-origin iframe + postMessage bridge. Iframe-shaped by construction; show `GET /mcp-apps/cards` metadata only. |
| Appearance settings | No meaning in a terminal. |
| `manage-sessions` as a screen | Redundant with the conversation list; fold its bulk actions in. |

### OAuth consent needs redesigning, not porting

The SPA opens a popup, passes `OAuth2CallbackUrl`, and receives a `postMessage`
from `/oauth-complete`. A terminal cannot. The replacement mirrors the device
flow that already works: print the authorization URL, let the user complete it in
any browser, then send `interrupt_responses: [{interruptId, response}]` to resume.
Same shape for `POST /connectors/{id}/initiate-consent`.

## Phases

**Phase 1 — done.** Real model catalogue carrying the provider; tool picker
persisting to `PUT /tools/preferences`; conversation list with history restore,
rename, delete and read/unread. Backed by `client/catalog.py`, the JSON-API
sibling of the two SSE transports, and verified live: a Mantle model answered, a
turn restricted to `[calculator]` used exactly that tool while `[]` used none, and
a resumed conversation answered from server-side memory.

**Phase 2 — the rest of the composer.** Skills, conversation modes, file
attachments, sampling overrides, Continue, and resuming a paused turn.

**Phase 3 — support surfaces.** Files, memory, memory spaces, schedules,
my-skills, agents and `@`-mentions, settings, API keys, costs.

**Phase 4 — sharing, export, connectors.**

## Facts worth not rediscovering

* **`model_id` and `provider` travel together**, and both are absent for "system
  default" — but omitting `provider` alone is **not** broken. inference-api does
  `provider = input_data.provider or registry_provider`, resolving it from the
  managed-model registry, and a Mantle-served model answers correctly either way
  (verified live). Send it anyway, as the SPA does: it makes the client's choice
  explicit instead of dependent on registry state, and it is the only way to
  disambiguate an id ever served by two providers.
* **There is no `POST /sessions`.** The client mints a UUIDv4 and the first
  `/chat/stream` creates the record. The SPA fires `POST /chat/generate-title` in
  parallel with the first turn.
* **`enabled_tools` / `enabled_skills` narrow, never grant** — the server
  intersects them with what the user's roles allow. Absent means "all"; `[]`
  means "none".
* **`selected_prompt_id` and an assistant are mutually exclusive.** The SPA omits
  the prompt on assistant turns so a mode cannot contradict assistant
  instructions.
* **Two casing conventions coexist.** `/chat/*`, `/artifacts/*`,
  `/auth/api-keys` and `/system/*` are snake_case; the rest is camelCase with
  `populate_by_name` aliases. A likely source of silently dropped fields.
* **File upload: the `PUT` `Content-Type` must match `mimeType` byte-for-byte**
  (it is a signed parameter), `Content-Length` must NOT be signed, no
  `Authorization` goes to S3, and `POST /files/{id}/complete` is mandatory —
  only `ready` files can be attached. Assistant KB documents are different: no
  complete call (S3-event ingestion), poll the document until `status == complete`.
* **`GET /shared/{share_id}` is not public** despite a comment saying so; it
  calls `get_current_user_from_session`.
* **`POST /runs/now`** is a complete non-streaming agent turn in one request,
  returning `finalMessage`, `toolTrace`, `usage` and `oauthRequired` — a natural
  fit for a scriptable `agentcore-tui run "prompt"`.
* **Most reads are cheap and pollable.** `/tools/` is backed by a 10-second TTL
  cache. Every `/memory/*` read is a vector search and is *not* cheap; neither is
  `/costs/*`, `/tools/{id}/discover`, or anything that starts an agent turn.
