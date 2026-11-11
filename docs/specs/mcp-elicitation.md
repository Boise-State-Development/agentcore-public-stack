# MCP elicitation: a server asking the user for structured input during a tool call

**Status:** In development, behind `MCP_ELICITATION_ENABLED` (off by default; only the literal `"true"` enables). Approved 2026-11-09 with every §9 recommendation accepted. PR-1 (backend, dark) implemented; §11 lists where it differs from this draft.
**Author:** Phil Merrell (with Claude Code)
**Date:** 2026-11-09
**Targets:** `develop`
**Follows:** the mcp 2.3.0 upgrade (`docs/kaizen/scoping/2026-10-17-mcp-sdk-v2-upgrade.md`, PR 1666), whose finding 3 deferred this.

## 1. What exists today

**We never advertise elicitation, so no server asks.**
- Neither `external_mcp_client.py` nor `UICapableMCPClient` passes `elicitation_callback`. Strands' `MCPClient` hands `None` to `ClientSession`, which substitutes `_default_elicitation_callback`. That callback answers `INVALID_REQUEST "Elicitation not supported"`.
- `ClientSession._build_capabilities` advertises elicitation only when the callback is not that default. It then advertises **both** `form` and `url`; there is no per-mode switch. `_UIExtensionClientSession` (our `ClientSession` subclass in `mcp_apps.py`) is the place to narrow that.

**Strands already drives both wire shapes through that one callback.**
- **Legacy (≤ 2025-11-25).** The server sends `elicitation/create` while its `tools/call` is still open. The SDK's `_on_request` runs the callback in the session's receive loop, not in the call's task.
- **2026-07-28 (SEP-2322).** `tools/call` returns an `InputRequiredResult` carrying `input_requests` and an opaque `request_state`. Strands' `_compat._drive_input_required` dispatches each request through the same callback, then re-sends the call with the responses and the echoed state. It allows up to 10 rounds. The callback runs **inside the call's own task**.
- An exception raised in the callback on the 2026-07-28 path escapes `call_tool`. `MCPClient.call_tool_async` turns it into an error `MCPToolResult`, and nothing more is sent to the server.

**Servers on dev.**
- Only GitHub (`api.githubcopilot.com/mcp/`) speaks 2026-07-28. It has exactly one elicitation-gated tool, `delete_repository`.
- `delete_repository` is SEP-2322 form mode. One round asks for `repository_name`, a required string, with the message `Type "owner/repo" to confirm permanent deletion`. The server deletes only when `action == "accept"` and the string matches exactly. `request_state` is encrypted and expires after 10 minutes.
- The 21 other servers are legacy, and none of them elicits.
- **Dev's GitHub catalog row lists 10 tools explicitly** (`TOOL#github_issues` in `dev-boisestateai-v2-app-roles`), and `delete_repository` is not one of them. A non-empty `MCPServerConfig.tools` is an allowlist, so advertising form elicitation would not put `delete_repository` in front of the model on dev.

## 2. The approach: pause, then replay the call

An elicitation becomes a Strands interrupt. The turn pauses and the SPA shows a form. On resume the **tool call runs again from the start**, and this time the callback answers from the stored response.

**Why replay works, and why it is the right shape for 2026-07-28:**
- SEP-2322 is stateless by design. The first round returns before the server acts, and everything it needs comes back in the retry.
- A replay starts a fresh round, so GitHub's 10-minute `request_state` TTL never runs out while the user thinks. If we persisted the state and resumed it, a user who answered after lunch would get "confirmation expired".
- It reuses the whole interrupt path: the `PausedTurnSnapshot`, the resume guard, the `PendingInterrupt` breadcrumb, reload, Stop and the `interrupt_responses` POST. Nothing stays open while the user is away: no SSE stream, no Runtime invocation and no MCP request.

**The seam is an `ExecuteToolStage` Wrap middleware (Strands 1.63), not a hook and not a tool subclass.**
- `ExecuteToolContext.interrupt(name, reason=…)` raises on the first pass and returns the response on resume. This is the same contract `ToolContext` gives `ask_user_question`.
- The interrupt id is `v1:middleware_execute_tool:{toolUseId}:{uuid5(name)}`.

```
ElicitationMiddleware (registered only while the flag is on)
  tool is not an MCPAgentTool from an elicitation-enabled server  → pass through
  slot = CallElicitationSlot(answers = prior responses for this toolUseId)
  set contextvar _ELICITATION_SLOT = slot          # copied into the background loop
  run inner chain                                  #   by run_coroutine_threadsafe
  slot.pending?  → ctx.interrupt("mcp_elicitation:{round}:{key}", reason=form payload)
                   (the inner chain's error result is discarded, never yielded)
  else           → yield the inner chain's result unchanged

elicitation_callback (one function, passed to every opted-in MCPClient)
  slot = _ELICITATION_SLOT.get()  → none: answer cancel (we cannot attribute it)
  slot has an answer for (round, key) whose schema digest matches → return ElicitResult(answer)
  otherwise: record (round, key, message, schema) as pending, raise _ElicitationPending
```

- **Matching on replay** uses the round index, the input-request key and a digest of `requestedSchema`. The message is deliberately left out, because a server may stamp an id or a timestamp into it. If the schema changed between rounds, the user is asked again; we never feed an answer to a form it wasn't given for.
- **One form per pause.** `_drive_input_required` dispatches a round's requests one at a time and stops at the first exception. A round carrying two requests therefore pauses twice. GitHub sends one, and the rare multi-request round works, just with an extra stop.
- **A second round on the same call** is a second interrupt on the same `toolUseId` under a new name. The responses to earlier rounds have to survive that second pause (see §8, risk 1).

**Session state.** The slot exists only for the duration of one call, in a contextvar. The answers are read from the agent's `_interrupt_state`, which Strands restores per resume from the session store, exactly as `ask_user_question` already relies on. Nothing is cached on the agent instance or on the shared `MCPClient`.

### 2a. Rejected: hold the call open

The alternative is to keep the MCP request and the SSE stream open and deliver the answer mid-turn through a side channel, the way mid-turn steering uses the lease row.

Rejected because:
- the Runtime invocation, the stream's 600 s ceiling and the MCP read timeout would all be held while a person reads a form;
- GitHub's 10-minute state would bound how long the user may take;
- it needs a second answer transport beside `interrupt_responses`.

Its one advantage is that it is the only way to serve a legacy server that does work *before* it elicits without doing that work twice. That is why legacy is out of v1 (§3).

## 3. Scope of v1

| In | Out (later, each its own decision) |
|---|---|
| **Form mode**, 2026-07-28 connections only. `_UIExtensionClientSession._build_capabilities` keeps `form`, drops `url`, and adds elicitation only when the negotiated version is ≥ 2026-07-28 | **URL mode.** Out-of-band sign-in and payment. It overlaps `oauth_required` and needs its own trust UI |
| Servers whose catalog row opts in (`elicitationEnabled`, §6) | **Legacy `elicitation/create`.** Replaying a legacy call repeats whatever the server did before it asked. No dev server elicits on legacy, so there is nothing to test it against |
| The SPA form, reload, Decline | Handoff relay of an elicitation (`handoff/relay.py` kinds) and the TUI renderer. A new event is harmless to the TUI, which logs it once |

**Effect of the version gate on a legacy server:** the default callback is kept on that connection, so the server sees exactly what it sees today.

## 4. Event and resume shape

**A new event, `mcp_elicitation_required`, not a reuse of `user_question_required`.** Three things differ, and folding them into `ask_user_question`'s payload would weaken it:
- **Who is asking.** The text and the form come from a third-party server, not from our model. The form must say so, with the server's name and icon. The MCP spec requires the client to show which server is requesting, and to let the user review, decline and cancel.
- **The answer is typed and closed.** An enum offers no "Other". Fields can be text, numbers or booleans. The reply is `accept | decline | cancel` plus content, not a set of selections.
- **Validation happens on our side** against the server's schema before anything is sent back.

Payload, emitted after `message_stop` in the same `done` block as the other interrupts, one event per pending form:

```jsonc
{
  "type": "mcp_elicitation_required",
  "interruptId": "v1:middleware_execute_tool:…",
  "toolUseId": "…",
  "toolName": "github_issues___delete_repository",
  "serverName": "GitHub",          // the catalog row's display name (§11)
  "message": "Type \"acme/scratch\" to confirm permanent deletion of this repository.",
  "fields": [
    { "name": "repository_name", "title": "Repository name", "description": "Enter acme/scratch exactly…",
      "kind": "text", "required": true, "minLength": null, "maxLength": null, "format": null, "default": null }
  ]
}
```

- `kind` is one of `text`, `number`, `integer`, `boolean`, `choice`. A `choice` field carries `options: [{value, label}]` and `multiSelect`, with labels taken from `oneOf[].title` / `enumNames`, or else from the value.
- **Bounds.** The message is capped at 2,000 chars and the form at 10 fields. A choice field may have up to 50 options, and a title or description up to 200 chars.
- **What happens to a form we can't render.** A schema outside the spec's restricted subset, or over those bounds, is never shown. The server gets `cancel`, and the model gets a one-line tool error saying the server asked for input this app cannot show.

**Resume** goes through the existing route and `interrupt_responses` entry. `response` is always an object, never null:
- `{"action": "accept", "content": {"repository_name": "acme/scratch"}}`
- `{"action": "decline"}`, from the form's Decline button
- `{"action": "cancel"}`, sent when the user dismisses the form; see §9.5

The callback validates `content` against the server's schema before the replay. An answer that does not fit is sent to the server as `cancel`, never forwarded (§11).

**Breadcrumb.** `PendingInterrupt(kind="mcp_elicitation")`, holding the normalized form JSON-encoded, as `user_question` does. That is one addition to the `kind` Literal and to `AwaitingInputKind`.

**What re-enters the conversation: nothing new.** The tool's result is the server's own final result, for example `Repository deletion was not confirmed. The repository was not deleted.`. The schema and message travel on SSE and in the breadcrumb, and the answer goes to the server. `toolConfig` and history are untouched. The answer is also kept out of the model's context on purpose, because a server may legitimately ask for something the model has no need to see.

## 5. Picker changes

A new `McpElicitationPrompt` component, a sibling of `UserQuestionPrompt`. It reuses that component's card chrome and option-chip control, but not its state machine. The new parts:

| Field | Control |
|---|---|
| `choice`, single, ≤ 4 options | The existing chip row, with no "Other" |
| `choice`, single, > 4 options | `<select>` |
| `choice`, multi | Chips with `aria-pressed`, honouring `minItems` / `maxItems` |
| `boolean` | Yes / No chips. A required boolean with no default starts unset |
| `text` | `<input>`, typed by `format` (`email`, `uri`, `date`, `date-time`), with min/max length |
| `number` / `integer` | `<input type="number">` with min, max and step |

- **Header.** The server's icon and name, then a line saying **"GitHub is asking for this."** It never reads as the assistant speaking.
- **Fixed notice on any form with a text field:** "Don't enter passwords or other secrets here." The spec forbids servers from asking for sensitive data in form mode, but we can't enforce that on the server.
- **Buttons:** Submit (disabled until required fields are valid) and Decline. Submit sends `accept`.
- **Reload.** The form is rebuilt from the breadcrumb on `GET /messages`, exactly like `UserQuestionPrompt`.

## 6. Flag, opt-in and RBAC

**Feature switch, in development, default OFF.** It widens what users can do. Two switches:
- **Backend:** `MCP_ELICITATION_ENABLED` / `CDK_MCP_ELICITATION_ENABLED`, read on the AgentCore Runtime and on app-api. Only `"true"` enables. The Runtime copy is written only while it is on.
- **SPA:** `features.mcpElicitation`. It gates the admin toggle below, and the prompt's route into the message list.

With the flag off:
- no callback is passed;
- the capabilities are byte-identical to today's;
- the middleware is never registered.

**Per-server opt-in: `MCPServerConfig.elicitation_enabled` (`elicitationEnabled`), default `false`.** This is a catalog setting, not a second flag. Two reasons:
- Advertising elicitation changes what a server returns from `tools/list`. The admin who owns the server should choose that, server by server.
- It keeps the eventual legacy question (§3) as a per-server statement: "this server asks before it acts".

App-api's admin discovery advertises form elicitation to an opted-in server too, through a callback that always cancels. That is the only way the admin's tool picker can see `delete_repository` in order to allow it. Discovery never calls a tool, so the callback never runs.

**RBAC answer for newly exposed tools: no new mechanism.**
- A server with a non-empty `tools` list (dev's GitHub) offers a gated tool only once an admin adds it. That is the decision point, and it is the existing one.
- A server with an empty list (discover at runtime) would offer it as soon as elicitation is enabled. The admin toggle says so in its help text.
- For `delete_repository` I'd **not** add `needsApproval` by default. GitHub's own form, which requires the exact `owner/repo` to be typed, is a stronger confirmation than our Approve/Deny card, and two prompts for one action teaches people to click through. An admin can still set `needsApproval`; the card then runs first, from `MCPExternalApprovalHook`.
- Role grants are unchanged. Whoever can use the server can use whatever tools the row allows.

## 7. Constraints, checked

- **TTFT.** No work is added before the first token.
  - The capability is part of `_build_capabilities`, which already runs at connect, so it adds no round trip.
  - The middleware is registered at agent build. For a tool it doesn't handle, it costs one `isinstance` check and one set lookup per tool call, after the model has answered.
  - A resumed delete pays one extra MCP round trip, the replayed first round (~100–300 ms). That is on the resume, after the user has answered.
- **Prompt cache.** No schema enters `toolConfig`, and nothing is appended to history beyond the server's own result (§4).
  - The one possible prefix change is GitHub's `tools/list` growing by `delete_repository`. On an allowlisted row like dev's, that never reaches `toolConfig`. On an empty-list row it is one deliberate rewrite when the admin enables the server.
  - Step 0 of §8 checks whether advertising form elicitation changes any *other* tool spec GitHub returns.
- **Session state.** A per-call contextvar plus Strands' own `_interrupt_state`. No cache on the agent instance or on the shared client (§2). The contextvar also cannot route user A's form to user B on a shared client: a callback that finds no slot answers `cancel`.

## 8. Test plan

**Risks to close first:**
1. **Middleware interrupts are a third flavor.** Hook interrupts (`v1:…`) and tool interrupts (`v1:tool_call:…`) are proven through our resume path. Middleware interrupts (`v1:middleware_execute_tool:…`) are not. Add a real-Strands integration test modelled on `test_user_question_interrupt_integration.py`. It must cover:
   - pause, with `pending_tool_execution` captured;
   - resume;
   - **two rounds on one `toolUseId` across two pauses**, checking that round 0's response is still in `_interrupt_state` after the second pause. Fallback if it isn't: the round-n reason carries the earlier answers, and the middleware reads them from there.
2. **The contextvar crosses into the background loop.** Strands' own comment says `run_coroutine_threadsafe` copies the caller's context. A real-wire test proves the SEP-2322 callback sees the slot.

**Hermetic:**
- The schema normaliser: every primitive, titled and untitled enums, multi-select arrays, bounds, and a refusal for anything unsupported.
- The answer validator.
- The capability ad. Flag off: byte-identical to today. Flag on: `form` only, never `url`, and only on ≥ 2026-07-28 and an opted-in server.
- The middleware:
  - pass-through for non-MCP tools and for servers that haven't opted in;
  - pause;
  - resume and accept;
  - decline;
  - a schema-digest mismatch, which asks again;
  - an invalid answer, which asks again;
  - two parallel calls that each pause.
- The stream coordinator: event extraction and breadcrumb persistence. Flag off: nothing emitted.

**Real-wire.** A 2.x `MCPServer` fixture whose tool returns `InputRequiredResult` (`FlakyMCPServer` style), driven through `UICapableMCPClient` and a real `Agent`. It covers pause, resume and the answer reaching the server, decline, and a replay after the fixture's state has "expired".

**SPA:**
- the parser spec for the new event;
- component specs: each control, required-field validation, Decline, the "server is asking" header, the secrets notice;
- the resume POST shape;
- rehydration from the breadcrumb;
- axe on the form.

**Dev:**
0. **Probe**, read-only `tools/list` with form advertised. Does `delete_repository` appear? Are the specs of the other GitHub tools unchanged (digest)?
1. Turn the flag on in the `development` GitHub environment and in `environment.development.ts`. Set `elicitationEnabled` on `TOOL#github_issues` and add `delete_repository` to its allowlist on dev only.
2. Using a **scratch repository** and a session started after the deploy:
   - Decline: the result says "not deleted", and the repo still exists.
   - A wrong name: the server refuses, and the repo still exists.
   - Reload while paused: the form comes back.
   - Accept with the right name: exactly that repo is deleted.
3. `C#` rows: no `toolConfigHash` rotation beyond the one explained by step 0, and `cacheStatus` stays clean across the pause and resume.
4. `turn_prelude` marks on a GitHub turn, flag on versus off: no difference.
5. `/smoke-regression` on dev. The decline path becomes a turn-matrix row; it is safe to run repeatedly.

**The real server for end to end on dev is GitHub's `delete_repository`, against a scratch repo.** It is the only server we have that speaks 2026-07-28 and elicits.

## 9. Decisions for Phil

1. **Pause-and-replay (§2) over hold-open (§2a).** Recommended.
2. **v1 is form mode on 2026-07-28 only.** Legacy `elicitation/create` and URL mode come later. Recommended.
3. **Per-server opt-in (`elicitationEnabled`) on top of the global flag.** Recommended. The alternative is the flag alone, which advertises to every 2026-07-28 server at once.
4. **`delete_repository` goes through the allowlist with no default approval card.** Recommended. The alternative is `needsApproval` by default, which means two prompts.
5. **What a form left unanswered means.** Recommended: nothing is sent while the turn sits paused; if the user sends a new message instead, the stale-interrupt path abandons the turn as it does today, and the server never hears back. That is safe for SEP-2322, because the server holds no state. The Decline button is the only path that sends `decline`.

## 10. PRs once agreed

- **PR-1, backend, dark.** The flag and its CDK wiring, `elicitationEnabled`, the capability narrowing in `_UIExtensionClientSession`, the callback and the slot, `ElicitationMiddleware`, the normaliser and validator, the event and the breadcrumb, and the §8 hermetic and real-wire tests.
- **PR-2, SPA.** `McpElicitationPrompt`, the parser and the resume, the admin toggle behind `features.mcpElicitation`, plus app-api discovery advertising. Docs: the CLAUDE.md SSE row and in-development flag line, and a docs-site feature-flags row of kind **Feature switch**.
- **PR-3, dev enablement.** The GitHub environment variable, `environment.development.ts`, the dev catalog change, and the §8 dev run recorded here.

## 11. As built (PR-1, 2026-11-09)

- **Risk 1 is closed.** A real Strands `Agent` over a real 2.x `MCPServer` (`test_mcp_elicitation_real_wire.py`) pauses on a middleware interrupt, resumes, and a call that asks twice pauses twice with its first answer still in `_interrupt_state`. `_InterruptState.to_dict` / `from_dict` keep answered interrupts, so a rebuilt agent restores them too. Two parallel calls that each ask pause together and resume in one request.
- **Risk 2 is closed.** The contextvar the middleware sets reaches the callback on the MCP client's background loop; the same test reads the capability off the wire: `clientCapabilities.elicitation == {"form": {}}`.
- **A replayed delete costs three `tools/call` POSTs in all:** the paused first round, the replayed first round, and the retry carrying the answer.
- **An answer that does not fit is sent as `cancel`**, not re-asked. The interrupt name is fixed once answered, so the same name cannot pause again, and the SPA validates before it posts; a misfit is a broken or hostile client.
- **`serverName` is the catalog row's display name** (`MCPClient.context_label`), not the server's own `serverInfo`. A server cannot then claim to be someone else in the header. The icon is left to PR-2.
- **`AwaitingInputKind` gains `mcp_elicitation` in PR-2**, with the SPA that renders it. Until then the SPA skips the new breadcrumb kind on reload (its hydrator falls through to the OAuth branch, which needs a `providerId`).
- **The middleware rides a Strands `Plugin`** (`McpElicitationPlugin`, whose `init_agent` calls `add_middleware`), appended to `ChatAgent`'s plugin list only while the flag is on. It is a deployment constant, so it adds no agent-cache key element.
- **Strands logs `tool execution failed` with a traceback for any exception from a call**, the pause included. A logging filter on `strands.tools.mcp.mcp_client` drops that one record (`ElicitationPending`, also inside an `ExceptionGroup`) and passes every other.
- **Unattended runs** (scheduled prompts, `run_agent_headless`) invoke the same Runtime turn path, so they get the middleware too. A run that reaches a server's form pauses exactly as it does today on an approval card or an `ask_user_question` prompt: the harness knows none of those events and reports the run `completed` with the turn left paused. That is existing behaviour for every interrupt kind, not new here; a headless status for paused turns is its own change.
