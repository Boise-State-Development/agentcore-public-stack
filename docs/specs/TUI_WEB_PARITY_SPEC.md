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
| Rename / delete / bulk delete | `PUT /sessions/{id}/metadata`, `DELETE`, `POST /sessions/bulk-delete` | ✅ `r` and Delete in `/conversations`, `/rename <title>` in chat; Space marks rows and Delete removes every marked one in one request (chunked at the server's 20; failures stay marked) |
| Read / unread | `POST /sessions/{id}/read` \| `/unread` | ✅ done |
| Shared Projects (chat in one) | `GET /projects`, `GET /projects/{id}`, `rag_assistant_id = harnessAgentId` on every turn, `preferences.projectId` on sessions | ✅ `/project [name\|off\|info\|shared]`; picker (Mine / Shared with me); status-bar badge; `/conversations` badge + rebind on open; gate refusals carry slash-command hints; `/project shared` reads `GET /projects/{id}/shared-tasks` → `GET /shared/{shareId}` (snapshot) → `POST /shares/{id}/export` (continue as mine, keeps the project). Management surfaces (members, instructions, memory, schedules, outputs) deliberately web-only |
| Agents (own, shared, pinned) | `GET /agents`, `GET /agents/pins`, `GET /agents/{id}`, `rag_assistant_id = agentId` | ✅ `/agent [name\|off\|info]`; picker grouped Mine / Shared with me / Pinned (the web `@` menu's scope; drafts excluded); `@ <name>` in the status bar; mutually exclusive with a project; reopening an agent conversation rebinds by its stored `assistantId`. Store browsing, pinning and the designer stay web-only |
| Skills | `GET /skills/`, `PUT /skills/preferences`, `enabled_skills`, `invoked_skills` | ✅ `/skills` picker (opt-in; shows each `/slug`); `/<slug>` in a message invokes it by the web composer's token rule; enabled skills listed in the `/` menu after commands (a command name always wins) |
| Conversation modes | `GET /system-prompts/`, `selected_prompt_id`, `PUT /sessions/{id}/metadata {selectedPromptId: null}` to clear | ✅ `/mode [name\|off]`; sent every non-agent turn; restored when a conversation opens; clearing writes null because the server falls back to the stored mode |
| File attachments | `POST /files/presign` → PUT → `POST /files/{id}/complete` | ✅ `/attach <path>`; `file_upload_ids` on the next turn |
| Title generation | `session_title` on the first turn's stream; `POST /chat/generate-title` is only for voice sessions | ✅ shown as the screen's subtitle and in `/conversations`; `/rename <title>` overrides it. The web app has no regenerate action, so neither does the TUI |
| Sampling overrides | `inference_params` | ✅ `/effort [level\|off]`, the one knob the web app still exposes (its Advanced form with temperature/top_p/max_tokens was removed). Control exists when the model's `supportedParams` declares `allowed` levels for `effort` or `reasoning_effort` and is not locked; kept per model for the run, sent as `{key: level}` on every request of a turn |
| Continue after truncation | `continue_truncated: true` with empty message | ✅ `/continue` |
| Resume a paused turn | `interrupt_responses`; `pendingInterrupts` on `GET /sessions/{id}/messages`; `POST /connectors/{id}/initiate-consent` | ✅ all five interrupt kinds answered in the transcript, live or on reopening a conversation that paused while nobody was there (OAuth rows get a fresh URL; there is no GET on `/pending-interrupts`, only DELETE) |
| Conversation search | `GET /sessions/search` | ✅ Enter in the list's filter; local title filter when the server has it off |
| **Client-executed tools** | `client_tools` on the request → `client_tool_call` SSE → `interrupt_responses` | ✅ see below — the one capability the terminal has that the web app does not |

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
| Artifact rendering | A sandboxed iframe with a render token. `/artifacts [dir]` lists the conversation's artifacts (`GET /artifacts?session_id=`) and saves the chosen version's source (`GET /artifacts/{id}/content?version=`) as `<title-slug>-vN.<ext>`, never overwriting. Above the server's 2 MB preview cap (413) the user is sent to the web app's download. |
| MCP Apps | Cross-origin iframe + postMessage bridge. Iframe-shaped by construction; show `GET /mcp-apps/cards` metadata only. |
| Appearance settings | No meaning in a terminal. |
| `manage-sessions` as a screen | Redundant with the conversation list; its bulk delete is folded in (Space marks, Delete). |

## Client-executed tools

The terminal can run commands and read or write files on the user's machine.
The web app cannot, and the server must not try. The protocol keeps the agent
loop on the server and only the *execution* on the client, reusing the
interrupt machinery that already exists for `ask_user_question`.

1. **Declare.** Every `/chat/stream` request carries `client_tools`: a list of
   `{name, description, inputSchema}` (`apis.shared.client_tools.ClientToolSpec`).
   The TUI sends the same five every turn (`workspace_info`, `run_command`,
   `read_file`, `list_directory`, `write_file`;
   `tui/src/aps_tui/local_tools/specs.py`).
   The SPA never sends the field, so nothing below runs for a web turn.
2. **Build.** `build_client_tools` makes one Strands tool per spec, named
   `local_<name>` so it cannot collide with a registry, gateway or MCP tool. Its
   body raises an interrupt through `ToolContext` with reason
   `{"type": "client_tool_call", toolUseId, toolName, arguments}`.
3. **Pause.** `stream_coordinator._extract_client_tool_call_events` emits one
   `client_tool_call` SSE event per pending interrupt at `done`, and writes a
   `PendingInterrupt(kind="client_tool")` breadcrumb. The paused-turn snapshot
   carries the specs (`PausedTurnSnapshot.client_tools`).
4. **Approve and run.** The TUI mounts a `ClientToolPrompt` showing the exact
   command or path. `y` runs it, `n` declines, `a` runs it and remembers the
   tool for the session. `ToolExecutor` bounds everything: 60 s timeout,
   6 000-char output (head and tail kept), 64 KB reads, 256 KB writes.
5. **Resume.** The client POSTs `interrupt_responses: [{interruptId, response}]`
   with `response` = `{"output", "isError"}` or `{"declined": true}`. Resume
   rebuilds the client tools from the snapshot — Strands re-enters the paused
   tool body, so the tool has to exist on the rebuilt agent — and the body
   returns the client's output as the tool result. A decline tells the model
   not to retry.

**Cache key.** `client_tools_digest` folds into the tools element of the agent
cache key, so a changed spec set rebuilds the agent and a turn without the
field keys exactly as before. The specs are therefore sent identically every
turn; a per-turn variation would rebuild the agent every turn.

**Bounds, because specs are prompt text and results re-enter the prompt:** 16
tools, 64-char names, 1 KB descriptions, 8 KB schemas, 32 K result characters
(server side; the client caps far lower).

**Trust.** The server trusts nothing about the tools beyond size. Whether a
command is safe is decided by the person at the terminal, with the command in
front of them. There is no allow-list: one that permits `git` permits
`git push --force`.

### OAuth consent needs redesigning, not porting

The SPA opens a popup, passes `OAuth2CallbackUrl`, and receives a `postMessage`
from `/oauth-complete`. A terminal cannot. The replacement mirrors the device
flow that already works: print the authorization URL, let the user complete it in
any browser, then send `interrupt_responses: [{interruptId, response}]` to resume.
Same shape for `POST /connectors/{id}/initiate-consent`.

## How features are reached: slash commands only

Decided 2026-10-29, modelled on Kiro CLI. Every feature in the terminal is a
slash command typed into the composer; `/` opens a completion popup; `/help`
is generated from the same registry (`tui/src/aps_tui/commands.py`).
There are no function keys and no command palette, so a parity row below
maps to a *command*, never a key. The web app's gear icon, sidebar and panels
become `/model`, `/tools`, `/conversations`, `/attach`, and later `/project`
and `/agent`. Keys are reserved for Enter, Esc, Ctrl+Q, newline and
navigation. Rationale and the rule set live in
`.kiro/steering/terminal-client.md`.

**Projects (next).** A project conversation is an ordinary session whose first
turn sends `rag_assistant_id = project.harnessAgentId`; the server derives
`projectId`, applies the harness's instructions, model, tools, skills,
knowledge and memory, and persists `preferences.projectId`. The terminal
therefore needs: `/project [name]` over `GET /projects` (Mine / Shared with
me), the harness id on the first turn, the project name in the status bar and
grouped in `/conversations`, the gate refusals (archived, deleted, no access)
as typed errors, and `GET /projects/{id}/tasks|shared-tasks` to open a
teammate's shared conversation read-only. Read-only `/project info`
(instructions, model, tools, members, memory files) follows. Member
management, instruction versions, the memory editor, proposals, schedules
and outputs stay in the web app.

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

## The agent knows which client it is talking to

Parity is not only about what the client can do — it is also about what the agent
is *told*. The shared system prompt used to instruct every client to toggle tools
from "the gear icon next to the message input" and offered KaTeX and Mermaid, so
a terminal user was directed to a control that does not exist and handed two
renderers a terminal draws as literal noise.

`InvocationRequest.client_surface` fixes that. `"web"` (the default, and what an
older client that omits the field gets) keeps every browser instruction; the TUI
sends `"terminal"` and receives keybindings, a prohibition on KaTeX/Mermaid, a
width warning, and the fact that OAuth consent cannot be completed there.

Composition lives in `agents/main_agent/core/system_prompt_builder.py`:
`SHARED_SYSTEM_PROMPT` + one entry from `SURFACE_GUIDANCE`. Adding a surface is a
block plus a key. Three things about it are load-bearing:

* **The surface is part of the agent cache key.** It changes the *built* prompt
  while leaving the raw `system_prompt` argument untouched — and that argument is
  `None` on the default path, so `prompt_hash` cannot separate surfaces. Since the
  key begins with `session_id` and the TUI now lists and resumes web
  conversations, omitting it would serve the second client an agent carrying the
  first client's guidance.
* **A paused turn records its surface** in `PausedTurnSnapshot`, because a resume
  must rebuild under the same cache key or the paused agent is stranded and
  Strands reports "must resume from interrupt with list of interruptResponse's".
* **The interface section is appended after `<user_instructions>`.** What a client
  can physically render is a fact, not a preference, so an assistant author must
  not be able to talk the agent into emitting KaTeX to a terminal.

Deployment order is safe in either direction: the model sets no `extra='forbid'`,
so a deployed backend that predates the field ignores it, and a deployed backend
that has it defaults to web for clients that omit it.

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
  fit for a scriptable `aps-tui run "prompt"`.
* **Most reads are cheap and pollable.** `/tools/` is backed by a 10-second TTL
  cache. Every `/memory/*` read is a vector search and is *not* cheap; neither is
  `/costs/*`, `/tools/{id}/discover`, or anything that starts an agent turn.
