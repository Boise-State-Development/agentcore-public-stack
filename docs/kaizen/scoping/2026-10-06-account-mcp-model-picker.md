# Scoping — Account MCP Server + Default-Model Picker (MCP App)

> Status: Scoping (no code yet)
> Owner: TBD
> Builds on: `.kiro/specs/platform-self-service/` (Phases 1–3 + 6 shipped in 1.25.0) and `docs/kaizen/scoping/mcp-apps-host-renderer.md` (host shipped).

## Goal

Let a user change their default model from an inline dropdown in chat, instead of typing "change my model to X" and then confirming. The dropdown lists only the models that user may use. Picking one and pressing **Set as default** saves it.

Do it by serving the self-service account tools from a small **in-process MCP server** that ships an MCP App (SEP-1865) UI. This is the first MCP App we author ourselves, and it sets the pattern for the Phase 7 self-service tools (tool toggles, memory, files, connections).

**Out of scope:** exposing this server to outside MCP clients (Claude Desktop, etc.), Phase 4/5 (platform-knowledge skill, hidden skill type), and changing the existing text-path tools beyond what's listed below.

## What already exists

| Piece | Where | State |
|---|---|---|
| Account tools (`whoami`, `get_my_quota`, `get_my_settings`, `set_default_model`) | `backend/src/agents/local_tools/account_tools.py` | Shipped. Plain Strands `@tool`s, user bound by closure. |
| Allowed-model list | `_accessible_models(user)` in the same file | Shipped. Single source of "which models may this user default to". |
| Tool injection, behind `CDK_PLATFORM_SELF_SERVICE_ENABLED` | `_build_account_tools()` in `backend/src/apis/inference_api/chat/routes.py` | Shipped. On in dev, off in prod. |
| MCP Apps host (sandbox iframe, `ui/*` bridge, app-initiated `tools/call`, reload survival) | `backend/src/agents/main_agent/integrations/mcp_apps.py`, `backend/src/apis/inference_api/chat/app_tool_dispatch.py`, `frontend/.../mcp-apps/` | Shipped. `AGENTCORE_MCP_APPS_HOST_ENABLED` defaults to true. |
| UI-capable MCP client | `UICapableMCPClient` in `mcp_apps.py` | Shipped. Takes **any** transport callable, not just HTTP. |
| RFC 8693 token exchange | `backend/src/agents/main_agent/integrations/token_exchange.py` | Shipped. Not used here (see decision 2). |

**The gap:** the host only renders UIs for tools that come from an MCP server. The account tools are plain Python tools, so nothing can draw a dropdown for them today.

## Architectural decisions

### 1. Server location — in-process, inside the agent runtime

The MCP server runs in the inference runtime's own process. The agent talks to it over an in-memory transport (`mcp.shared.memory` streams), with no network hop.

- **Identity:** the server is built per agent with the signed-in `User` bound at construction, exactly as the `make_*_tool(user)` factories do today. No tool argument names a user, so the server can only ever read or write the caller's own records.
- **Data access:** unchanged. The runtime already reads and writes user settings, quota and RBAC through `apis.shared`. No new IAM.
- **Infra:** none. No route, no catalog row per environment, no CDK.

**Rejected: host it in app_api as a streamable-HTTP endpoint.** That needs a JWT check on a new route, a seeded `mcp_external` catalog row with a per-environment URL, the user's token on the wire, and a network round trip per call (the runtime is `networkMode: 'PUBLIC'`, so it would go out through the ALB). Its only extra value is reuse by outside MCP clients, and those would bring their own OAuth login, which is a separate project. Revisit if that becomes a goal.

### 2. No token exchange, no token forwarding

The OBO exchange exists to turn a Cognito token into a token-service JWT for a downstream API that does not trust Cognito. An in-process server needs no token at all: identity is the bound `User`. Even under the rejected app_api option, plain `forward_auth_token` would do, because app_api already validates Cognito tokens. Using the exchange would add the token service as a runtime dependency for no gain.

### 3. Two tools, split by `_meta.ui.visibility`

| Tool | Visibility | What it does |
|---|---|---|
| `show_model_picker` | `["model", "app"]` | Read-only. Returns the allowed models plus the current default, as both text (so the model can answer in words) and structured content (for the UI). Carries `_meta.ui.resourceUri = "ui://account/model-picker"`. |
| `apply_default_model` | `["app"]` | Write. Takes `model_id`, re-validates it against `_accessible_models(user)` **at call time** (never trusts the list the iframe was given), then writes `defaultModelId`. |

The model cannot see or call `apply_default_model`: `record_and_filter_ui_tools` drops app-only tools from the model's list, and `dispatch_app_tool_call` rejects calls whose visibility excludes `app`.

### 4. The click is the consent

Today's `set_default_model` is two-step (`confirm=true`) because the model drives the write. In the picker, the user's own click drives it, so there is no confirm step. The rails that remain:

- `apply_default_model` is app-only (decision 3), so prompt-injected text cannot trigger it through the model.
- The picker HTML is ours (served by our server), and it only calls `tools/call` from the button's click handler. Nothing calls it on load.
- Every app-initiated call is already published as a `tool_use` / `tool_result` card in the thread by `app_tool_dispatch`, so the change leaves an audit trail.
- After a successful write, the picker sends `ui/update-model-context` ("default model is now X"), so the model's next turn knows.

### 5. Keep the typed path

`set_default_model` (two-step, model-callable) stays for users who type "set my default to Sonnet". The server's `apply_default_model` and the typed tool share one internal helper (validate → write), so the two paths cannot drift. `whoami`, `get_my_quota` and `get_my_settings` stay as plain tools for now and move onto the server when they get UIs.

### 6. `mcp` dependency

`backend/pyproject.toml` deliberately takes `mcp` only through `strands-agents` (pinned `mcp<2`). This feature imports `mcp.server.fastmcp` and `mcp.shared.memory` from it. **Decision needed:** keep it transitive (no change, but a Strands bump could move it) or promote it to a pinned direct dependency. Recommendation: keep it transitive and add an import-smoke test, matching the existing `tests/supply_chain/test_strands_agentcore_pairing.py` approach.

## Prerequisite fix — per-user UI tool catalog

`UIToolCatalog` (`mcp_apps.py`) is **process-global and keyed by tool name only**, and it stores the MCP client that surfaced each tool. `dispatch_app_tool_call` resolves the client from the catalog by name and ignores the `agent` it is given (`app_tool_dispatch.py`, `_resolve_client`).

With a per-user server, two users in one process would overwrite each other's entry, so user A's click could run on user B's client and change **B's** default model. In prod this is probably contained, because AgentCore Runtime normally gives each session its own microVM. That is not verified for our deployment, and local dev runs every user in one process. It also affects any existing per-user (OAuth or forwarded-token) MCP App server.

**Fix (own PR, first):** key the catalog by `(user_id, tool_name)`, or better, resolve the client from the conversation's own agent (its tool providers) and use the catalog only for `_meta.ui`. Add a test that proves two users' clients for the same tool name never cross.

## Design

### Server — `backend/src/agents/local_tools/account_mcp_server.py` (new)

- `build_account_mcp_server(user: User) -> FastMCP` registers the two tools and the `ui://account/model-picker` resource (`text/html;profile=mcp-app`, or whatever MIME `_extract_html_content` expects).
- It reuses `_accessible_models`, `_model_label` and the shared validate-and-write helper from `account_tools.py`. Error handling mirrors `set_default_model` (exposes the AWS error type only, with the same AccessDenied message).
- It stays within the `agents/` import boundary: it imports `apis.shared` only (enforced by `tests/architecture/test_import_boundaries.py`).

### Transport — in-memory adapter (new, ~40 lines)

An async context manager that creates `mcp.shared.memory` stream pairs, runs the FastMCP low-level server on one end in a task group, and yields the client end. It is passed to `UICapableMCPClient(lambda: memory_transport(server))`.

Strands runs each `MCPClient` on its own background thread and event loop. The server task must live inside that context manager, so it runs on the client's loop and is torn down with it.

### Wiring — `_build_account_tools()` in `routes.py`

When the self-service flag is on **and** `set_default_model` is in the effective tool set (same RBAC / system-tool gating as today), also return one `UICapableMCPClient` for the account server. `extra_tools` already flows into the same tool list as external MCP clients (`base_agent.py`), so Strands treats it as a tool provider.

**Agent cache:** `injected_tools_are_key_described()` must still hold. The client closes over the user, who is already in the cache key. Confirm this and extend that predicate's tests.

### Picker UI — `backend/src/agents/local_tools/account_ui/model_picker.html` (new, static)

- Vanilla HTML + JS. No CDN, so it stays within the default sandbox CSP and needs no `_meta.ui.csp` additions.
- Protocol (already supported by the host bridge):
  - `ui/initialize`
  - read the models from `ui/notifications/tool-result`
  - render a `<select>` with the current default preselected
  - on click, `tools/call apply_default_model`
  - show the result
  - then `ui/update-model-context` and `ui/notifications/size-changed`
- It uses theme variables from `host-context-changed` so it reads well in dark mode. It is accessible: a labelled select, a real button, and an `aria-live` status line.

## Touch points

| File | Change | Size |
|---|---|---|
| `backend/src/agents/main_agent/integrations/mcp_apps.py` | Per-user catalog keying (prerequisite) | S |
| `backend/src/apis/inference_api/chat/app_tool_dispatch.py` | Resolve the client per user / from the agent (prerequisite) | S |
| `backend/src/agents/local_tools/account_mcp_server.py` | New server: 2 tools + 1 resource | M |
| `backend/src/agents/local_tools/account_ui/model_picker.html` | New picker page | M |
| `backend/src/agents/local_tools/account_tools.py` | Extract the shared validate-and-write helper | S |
| `backend/src/agents/main_agent/integrations/` (new `memory_transport.py`) | In-memory transport adapter | S |
| `backend/src/apis/inference_api/chat/routes.py` | Return the account MCP client from `_build_account_tools` | S |
| `backend/tests/...` | See test plan | M |
| Frontend | None expected; the bridge is generic | — |
| Infrastructure | None | — |

## PR plan and estimate

1. **Per-user UI tool catalog** (prerequisite, independent): ~0.5 day.
2. **Spike: in-memory transport + `UICapableMCPClient`.** Prove `initialize` (with the UI extension), `tools/list`, `resources/read` and `call_tool_sync` work in-process, including from `app_tool_dispatch`'s worker thread. ~0.5 day. **This is the main risk.** If it fails, fall back to the app_api HTTP option (adds ~2 days).
3. **Account server + picker + wiring + tests:** ~1–1.5 days.
4. **Dev verification** (manual, in the dashboard Browser panel): ~0.5 day.

**Total: about 2.5–3 days** on the in-process path.

## Test plan

- The server tools write only the bound user's settings, and no tool schema has a user argument.
- `apply_default_model` refuses a model outside `_accessible_models`, even if the iframe sends a valid-looking id.
- `apply_default_model` is absent from the model's tool list and is callable through `dispatch_app_tool_call`.
- The catalog fix: two users' clients for the same tool name stay separate.
- A transport round trip: list, read resource, call tool.
- The typed `set_default_model` still works and shares the helper.
- The agent-cache predicate still reports key-described when the account client is injected.
- Manual (dev): the picker renders, preselects the current default, saves, the settings page reflects it, a reload keeps the card, and a refused model shows an error.

## Open questions

1. Promote `mcp` to a direct dependency, or keep it transitive with a smoke test? (Decision 6.)
2. Should `show_model_picker` replace the model's habit of calling `set_default_model` for "what can I switch to?" The system-prompt or tool-description nudge needs tuning.
3. Hidden-tool panel gap (self-service task 1.3): system tools may still appear in the settings tool panel. It's unrelated to the picker, but should be fixed before a prod flip.
4. Prod rollout rides the existing `CDK_PLATFORM_SELF_SERVICE_ENABLED` flag. Do we want a separate flag for the picker?
