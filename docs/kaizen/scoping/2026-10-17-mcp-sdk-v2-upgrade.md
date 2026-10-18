# Scoping — `mcp` Python SDK 1.30.0 → 2.1.1 (protocol 2026-07-28)

> Status: **Implemented** on `feature/mcp-sdk-v2` (approved by Phil 2026-10-17). The scope below is kept as written. "As built" lists where the implementation differs from it.
> Owner: Phil Merrell
> Source: PR #1662 (MCP App "form shown, nothing written yet" result shows as Failed). Its text-match fallback retires after this upgrade.
> Base: `develop` at `e87a40d3` (after #1657, #1659, #1660, #1661), with #1662 read from `fix/mcp-app-awaiting-user-submission`.

## As built (2026-10-17)

- **Pins as recommended:**
  - `mcp==2.1.1`, `httpx2`/`httpcore2==2.13.1` and `truststore==0.10.4` as `[tool.uv]` constraints;
  - `idna==3.20`;
  - `mcp-types` 2.1.1 locked.

  `pydantic-settings` and `httpx-sse` left the lock (1.x-only dependencies; nothing imports them).
- **The scheduled-runs Lambda image was a second SDK line.** It installs Strands, and so `mcp`, from its own `requirements.txt`, pinned `mcp==1.30.0`, and no guard checked it. Both files now carry the same set as the lock, and `test_lambda_images_that_install_strands_carry_the_same_mcp_set` enforces it. The set resolves on the image's Python 3.12.
- **Transport:** a new `integrations/mcp_transport.py` (`streamable_http_transport`) builds the 2.x transport over httpx2. `OAuthBearerAuth`, `CompositeAuth` and `SigV4HTTPXAuth` are now `httpx2.Auth`. `SigV4HTTPXAuth` declares `requires_request_body`. It did not use Strands' private `_compat` adapter.
- **R4 and R5 share one fix, and it differs from the scope.** A response hook records each failed POST's HTTP status by JSON-RPC id. The read stream then rewrites the SDK's stand-in message to `Server returned HTTP <status> <reason>` (`mcp_session.http_error_message`). So a 401 reads as a 401 again, and the existing detectors (`_is_auth_failure`, `looks_like_auth_failure`) work unchanged; no per-client auth record was needed. `server_error_outcome_unknown` turns a 5xx on a live session into the unknown-outcome text, in both `MCPSessionHealHook` and the App dispatch. A server's own JSON-RPC error body keeps its message.
- **R1, measured and removed.** `mcp.excalidraw.com` (legacy):
  - 1.30.0: 201 ms p50 connect;
  - 2.1.1 as shipped: 325 ms;
  - 2.1.1 with the legacy verdict: 205 ms after the first connect per process.

  The verdict works like this. `_UIExtensionClientSession` records a URL as legacy for an hour when its probe was rejected and `initialize` then succeeded; a 5xx or timeout is not a verdict. While the verdict is fresh, `send_discover` refuses locally. A modern server (GitHub) is never marked. Noted in `docs/specs/turn-path-ttft.md`.
- **R6 settled.** ADOT 0.19.0 declares `mcp >= 1.10.0` (no upper bound), so its MCP instrumentor still loads, and it emits nothing (it wraps the removed `BaseSession`). The SDK's own `mcp-python-sdk` client spans carry method and tool name only. `test_otel_content_capture.py` now pins both facts, and its `test_adot_mcp_instrumentor_cannot_see_the_sdk` fails the day ADOT instruments 2.x again. The SDK also injects W3C `traceparent` into every request's `_meta` (SEP-414): trace ids, no content, reach third-party servers.
- **`ReleasesCallsOnSessionEnd` stays.** Two different failures now behave differently on 2.x:
  - a 503 no longer kills the session;
  - a response broken off mid-body still does (the transport error escapes its task group).

  The upstream hang still reproduces with a dropped connection. `FlakyMCPServer` gained `drop_calls`, `down_status`, `discover_reply`, request recording and `MCPServer` identity kwargs.
- **#1662's text fallback is kept** until dev shows the marker from `api.githubcopilot.com`. A real-wire test (`test_a_2026_07_28_server_sends_the_marker_itself`) shows a 2.x server's marker reaching the Strands result whole.
- **Also seen:**
  - Strands attaches each result's `_meta` as `metadata`, which on a modern connection always includes the server-info stamp. It is persisted, and no model formatter sends it.
  - `MCPServer` adds `structuredContent: {"result": …}` to plain-string tools.

## Recommendation

Pin **`mcp==2.1.1`** as a `[tool.uv] constraint-dependencies` entry. It replaces the `mcp<2` guard rail, and `mcp` stays a transitive dependency of Strands, as it is today. Keep `strands-agents==1.57.2`.

| Package | Today | Proposed | Why |
|---|---|---|---|
| `mcp` (constraint) | `<2` → 1.30.0 | `==2.1.1` | Newest release that Strands 1.57.2 allows (`mcp>=1.23.0,<2.2`). Released 2026-08-25, so it is 53 days old. 2.0.1 is a backport released a day later. 2.2.0 and 2.3.0 are outside Strands' range, and still are on Strands 1.61.1. |
| `mcp-types` | — | `2.1.1` (exact-pinned by `mcp`) | The wire types moved into their own distribution. |
| `httpx2` / `httpcore2` | — | constrain `==2.13.1` | `mcp` asks for `>=2.5.0`. 2.13.1 is from 2026-09-23. Pinning it means the lock does not drift. |
| `truststore` | — | constrain `==0.10.4` | Pulled in by httpx2. The resolver would pick 0.10.5, released 2026-10-12, which is under two weeks old. |
| `idna` | `==3.15` | `==3.20` | httpx2 needs `idna>=3.18`. 3.20 is from 2026-09-17. It is still the security pin, just raised. |

Strands already supports both lines. `strands/tools/mcp/_compat.py` detects 2.x with `MCP_V2 = hasattr(ClientSession, "discover")`. It switches field reads to snake_case, negotiates through `negotiate_auto`, drives `InputRequiredResult` rounds, opens `subscriptions/listen`, and adapts `httpx.Auth` to httpx2. Strands therefore needs no change. **Our** code needs the changes below.

## The payoff

- On 2026-07-28, GitHub sends `structuredContent: {"status": "awaiting_user_submission", …}` on the `issue_write` and pull-request form stubs itself. Its own test `TestTypedIssueWriteAwaitingForm` asserts the marker for `ProtocolVersionMultiRoundTrip` and `Nil` for `2025-11-25`. Strands copies it onto the tool result (`_compat.structured_content`). PR #1662's marker path then works without the text match.
- 2.x no longer ends the whole session when one POST gets a non-2xx answer (see risk R4). The hang that `ReleasesCallsOnSessionEnd` works around, and the dead shared session that #1657 and #1659 heal, are both caused by that teardown in 1.x.

## What 2026-07-28 changes on the wire with GitHub

These findings come from `github/github-mcp-server` (`pkg/inventory/tool_availability.go`, `typed_output.go`, `typed_schema.go`, `docs/typed-tool-schemas.md`).

1. **Negotiation.** There is no `initialize` handshake. The 2.x client probes `server/discover` and then stamps protocol version, client info and client capabilities into every request's `_meta`. A server that rejects the probe gets the legacy `initialize` instead (`mcp.client._probe.negotiate_auto`). GitHub is modern, so it takes the discover path.
2. **Multi-round-trip input requests (SEP-2322).** A `tools/call` can return `InputRequiredResult` in place of sending a server-initiated `elicitation/create`. Strands' `_compat.call_tool` handles the retry loop (up to 10 rounds) and resolves each input through the session's `elicitation_callback`. **We pass none**, so 2.x falls back to its default callback ("Elicitation not supported") and **does not advertise the elicitation capability**. No new request type reaches our code.
3. **Elicitation-gated tools.** Exactly one tool is gated: `delete_repository` (`MinimumProtocolVersion = 2026-07-28`, `RequiredElicitationMode = form`). GitHub drops it from `tools/list` unless the client advertises form elicitation. We don't, so **no new tool appears and there is nothing new to gate.** Advertising elicitation later would expose it, and that should be its own RBAC and approval-card decision.
4. **Typed outputs.** Every tool registered with a typed output gains an `outputSchema` in `tools/list`, plus `structuredContent` on results. GitHub also **rewrites the text content of a successful result to the JSON of `structuredContent`** (`applyModernStructuredText`). Error results, the awaiting stub among them, keep their text. Both points matter for cost (R2, R3).
5. **No protocol sessions.** A modern connection carries no `Mcp-Session-Id`, and nothing is sent on close (no `DELETE`).

## Code changes the upgrade implies

### Hard breaks: import errors or `AttributeError` on 2.x

| Where | 1.x usage | 2.x change |
|---|---|---|
| `agents/main_agent/integrations/external_mcp_client.py:25,352` | `from mcp.client.streamable_http import streamablehttp_client` | Removed. 2.x has `streamable_http_client(url, http_client=…)`, built with `create_mcp_http_client(headers, auth=…)`, and the auth must be an **httpx2** `Auth`. Either port `OAuthBearerAuth` / `CompositeAuth` (`oauth_auth.py`) to `httpx2.Auth` (same protocol, different model classes), or use Strands' `_compat.streamable_http_transport`. That is a private module, so a direct port is preferable. |
| `agents/main_agent/integrations/gateway_mcp_client.py:9,236,298` | same transport | Same change. `SigV4HTTPXAuth` (`gateway_auth.py`) signs the body, so it must keep `requires_request_body = True` once ported. |
| `tests/fixtures/flaky_mcp_server.py` | `mcp.server.fastmcp.FastMCP`, `streamablehttp_client` | `mcp.server.fastmcp` now raises `ModuleNotFoundError` on import. Use `mcp.server.mcpserver.MCPServer` and the new client transport. |
| `tests/security/test_otel_content_capture.py:48-49` | `FastMCP`, `mcp.shared.memory.create_connected_server_and_client_session` | Both gone. Port to `MCPServer` and `create_client_server_memory_streams` (or `mcp.client.Client` in memory). |
| `tests/agents/main_agent/integrations/test_mcp_apps.py:29,110-170` | patches `mcp.shared.session.BaseSession.send_request` and builds `InitializeResult(protocolVersion=…, serverInfo=…)` | `mcp.shared.session` no longer exists. Rewrite alongside the `_UIExtensionClientSession` change below. |
| `tests/supply_chain/test_strands_agentcore_pairing.py` (guard rail 2) | asserts `mcp<2` is declared and locked | Rewrite it to assert the declared `mcp==2.1.1` constraint, a lock that agrees, and that the pin stays inside Strands' declared range. |

### Silent breaks: no error, wrong behavior

The camelCase reads below return `None` on 2.x because every pydantic field is now snake_case. Constructing a model with camelCase keyword arguments still works (`alias_generator=to_camel, populate_by_name=True`), so test fixtures like `Tool(inputSchema=…)` and `TextResourceContents(mimeType=…)` can stay as they are.

| Where | Read | Effect on 2.x | Change |
|---|---|---|---|
| `mcp_apps.py:639-697` `_UIExtensionClientSession.send_request` | `isinstance(request.root, InitializeRequest)`, `root.params.capabilities` | Requests are no longer `RootModel`s and a modern connection never sends `initialize`. **The MCP Apps UI extension stops being advertised.** GitHub then stops serving its App forms, and MCP App frames go blank. | Replace the `send_request` override with `__init__(…, extensions=None, **kw)`. It merges `{MCP_APPS_UI_EXTENSION_KEY: MCP_APPS_UI_CAPABILITY}` into `extensions`, keeping any key Strands passes, e.g. the tasks extension, so `result_claims` stays valid. 2.x builds capabilities from `extensions` for both the handshake and the modern `_meta` stamp (`ClientSession._build_capabilities`). The flag check moves into the constructor. |
| `mcp_apps.py:695` | `getattr(result, "serverInfo")` | Not captured, so App headers fall back to the `ui://` authority and the manifest icon. | Delete the capture. Read `session.server_info` in `_resolve_server_identity`. On a modern connection it comes from the discover result's optional `_meta` stamp, so it can be legitimately `None`. The existing fallback covers that case. |
| `mcp_apps.py:284` `_extract_html_content` | `item.mimeType` | Every `resources/read` item looks untyped. The spec MIME is lost and an explicit `text/plain` body could pass as the App. | `mime_type` |
| `apis/app_api/tools/discovery.py:143,179,185,194` | `nextCursor`, `resourceTemplates`, `uriTemplate`, `mimeType` | **Admin capability discovery silently truncates** to page 1, templates come back empty, and MIME types are dropped. | `next_cursor`, `resource_templates`, `uri_template`, `mime_type` |
| `apis/inference_api/chat/app_tool_dispatch.py:147` `_structured_content` | `getattr(result, "structuredContent")` | Today's path reads the Strands result **dict**, whose keys are camelCase on both lines, so nothing changes. The object branch is dead on 2.x. | Read `structured_content` in the object branch (`_is_error` already falls back to `is_error`). |
| `apis/shared/tools/mcp_session.py`, `mcp_session_end.py` | Strands privates (`_is_session_active`, `_background_thread`, `_tool_provider_started`, `_consumers`, `_loaded_tools`, `_background_task`) | Unchanged, because these depend on the Strands version, not on `mcp`. | None for the SDK. Their 503 *trigger* changes (R4). |

Everything else that reads MCP results reads the **Strands `MCPToolResult` dict** (`structuredContent`, `isError`, `status`, `content`), whose keys don't change: the #1662 module, its hook, `stream_processor`, persistence, and the SPA.

## The #1662 fallback: what to delete, which tests flip

Delete these once dev shows the marker arriving from GitHub:

- `apis/shared/mcp_apps/awaiting_submission.py`: `_GITHUB_FORM_STUB_PREFIX`, `_is_form_stub_text`, `mark_awaiting_user_submission`. `awaiting_user_submission()` becomes `has_awaiting_marker()`, and its `is_app_tool` argument goes with it.
- `agents/main_agent/session/hooks/mcp_app_awaiting.py`: `McpAppAwaitingSubmissionHook`. Its only job is writing the marker onto the stub. Remove its registration in `base_agent.py` and `hooks/__init__.py`. `call_awaits_user_submission` stays for `AgentStatusHook` and `ToolCensusHook`, and becomes a marker check.

Tests in #1662 that flip or go:

| Test | Fate |
|---|---|
| `tests/shared/test_mcp_app_awaiting_submission.py::TestAwaitingUserSubmission::test_githubs_stub_without_the_marker_counts_for_an_app_tool` | **Flips.** A bare stub with no marker is no longer "awaiting". Keep it as a negative test. |
| `…::test_the_sentence_must_open_the_result` | Delete (no text match left). |
| `…::TestMarkAwaitingUserSubmission` (4 tests) | Delete with the function. |
| `…::test_the_marker_counts_whatever_the_tool`, `test_the_stub_text_never_counts_for_a_tool_without_an_app`, `test_an_ordinary_app_failure_is_still_a_failure`, `test_malformed_results_are_not_awaiting` | Keep. |
| `tests/agents/main_agent/session/test_mcp_app_awaiting_hook.py` (8 tests) | Delete with the hook. Move `test_the_marker_is_not_in_what_bedrock_receives` to the predicate's tests. |
| `test_agent_status_hook.py::test_an_app_form_waiting_for_the_user_is_not_a_failure`, `test_tool_census_hook.py::test_an_app_form_waiting_for_the_user_is_not_an_error` | **Flip** unless their fixtures gain `structuredContent: {"status": "awaiting_user_submission"}`. They currently build the bare stub text. Add the marker. |
| `test_stream_processor.py::test_tool_result_carries_structured_content_to_the_client`, the `test_summarizer.py` awaiting tests | Unaffected: they already carry the marker or the `awaiting` flag. |
| **New:** a real-wire test (2.x `MCPServer` stub returning `isError` and the marker → Strands `MCPClient` → hook chain) asserting the marker reaches the `tool_result` SSE | Add. |

**Option:** keep the text fallback for one release after the upgrade, then delete it. A deployment pointing at an older self-hosted `github-mcp-server` would still send the bare stub. I'd delete it in the upgrade PR only if dev shows the marker from `api.githubcopilot.com`.

## Risks

**R1 — TTFT: one extra round trip per legacy-server connect.** `negotiate_auto` probes `server/discover` before falling back to `initialize`. A server that doesn't speak 2026-07-28 therefore pays discover → error → initialize on every `MCPClient.start()`. That probably covers the AgentCore Gateway and our externally hosted FastMCP servers, but it needs measuring. The cost lands on the agent build's MCP pre-flight, so a cache miss pays it and a cache hit does not. Pre-flight runs per server in parallel, so a turn pays the slowest server's extra hop, not the sum. A legacy server that **never answers** an unknown method costs `DISCOVER_TIMEOUT_SECONDS = 10.0` before the fallback. Measure per server on dev (recipe below) and state the number in the PR. If a server hangs on the probe, the fallback is to force `initialize()` for it in our `MCPClient` subclass. That is another Strands seam, so use it only if the measurement demands it.

**R2 — `toolConfig` prefix stability.** GitHub's tools gain `outputSchema` and Strands puts it on `tool_spec`. Strands' Bedrock formatter sends only `name` / `description` / `inputSchema`, so **`toolConfig` should be byte-identical**. Verify that, don't assume it:
- compare `toolConfigHash` on the `C#` rows of a GitHub-connected session across the deploy;
- check the OpenAI-compatible and Mantle formatters too, since a formatter that forwards `outputSchema` would re-write the prefix;
- check that `delete_repository` stays absent.

Any real change is a one-time re-write per live GitHub conversation, priced at 1.25× the model's input rate on that prefix. It would not be a per-turn cost.

**R3 — Tool results reaching the model change shape.** On a modern connection GitHub replaces a typed tool's success text with `json.dumps(structuredContent)`. That append-only change doesn't touch the cached prefix, but every GitHub tool result in history from then on is a different size and shape. Before deciding, compare tokens per result on a few common tools (`list_issues`, `issue_read`, `search_code`, `get_file_contents`) between 1.30 and 2.1.1, against the token-cost tenet's "bounded payloads".

**R4 — The 503 duplicate-write guard regresses.** In 2.1.1 a non-2xx POST answers *only that request*, with `MCPError(INTERNAL_ERROR, "Server returned an error response")`, and the session survives (`StreamableHTTPTransport._handle_post_request`). GitHub's "wrote the issue, then answered 503" will then show as a plain **failed** call, not #1657's "may have completed, check before retrying". `session_crashed` never fires, so the duplicate-write risk that #1657 fixed comes back. Fix: treat a `tools/call` error carrying that SDK message, i.e. any HTTP ≥500 on the call itself, as an unknown outcome as well, in `app_tool_dispatch` and in `MCPSessionHealHook`'s AfterToolCall. Keep the session-death path for real transport loss. Test churn:
- `test_a_call_whose_session_dies_resolves_rather_than_hangs` flips by design: the plain client stops hanging, because the 503 no longer kills the session;
- the `FlakyMCPServer` fixture needs a real connection-abort mode to keep exercising heal and revive (`test_without_healing_…`, `test_a_cached_agents_next_model_driven_call_reconnects`, `test_a_new_agent_built_over_the_crashed_client_reconnects`, `test_a_failed_reconnect_is_tried_again_on_the_next_call`, and the dispatch tests `test_a_connection_lost_mid_call_reports_an_unknown_outcome` and `test_a_crashed_session_is_reset_before_it_is_revived`);
- add a "503 on a live session → unknown outcome" test.

Keep `ReleasesCallsOnSessionEnd` until a test shows the hang can't happen on 2.x through some other session death.

**R5 — OAuth 401 detection goes blind.** The same transport branch drops the HTTP status: a 401 or 403 with no JSON-RPC body becomes the same generic `INTERNAL_ERROR` text, with no `.response`. Two detectors stop seeing auth failures:
- `_is_auth_failure` (pre-flight `oauth_required` and vault recovery, `external_mcp_client.py:205`);
- `looks_like_auth_failure` (token invalidation after a call, `apis/shared/oauth/auth_failure.py`).

Users with an expired or cold token would get a silent tool drop or a "failed" call instead of the Connect prompt or a refresh. Fix: the ported httpx2 `OAuthBearerAuth` can see each response (`response = yield request`) and record the last auth status. Both detectors then consult that record instead of parsing text. **This blocks the merge.** The cold-cache GitHub pre-flight 401 seen on dev is exactly this path.

**R6 — MCP client spans disappear.** ADOT 0.19.0 (pinned) and 0.21.0 (latest) both declare `McpInstrumentor` for `mcp >= 1.10.0, < 2` and wrap `mcp.shared.session.BaseSession`, which no longer exists. OTel's dependency check skips the instrumentor. That loses observability (no `mcp.client` spans), not function. 2.x ships its own OTel (`mcp/shared/_otel.py`, `opentelemetry-api` dependency). Check what it emits and whether the no-content-capture guarantee that `test_otel_content_capture.py` pins still holds for its attributes. That test has to be rewritten anyway.

**R7 — TLS trust store.** httpx2 depends on `truststore`, which uses the OS trust store rather than certifi. Confirm the app-api and Runtime images have `ca-certificates` and that connections to GitHub, Google and the Gateway verify. Also check that `SigV4HTTPXAuth` signs correctly against httpx2 requests (body and header copy).

**R8 — Coupling.** `_UIExtensionClientSession` still swaps `strands.tools.mcp.mcp_client.ClientSession`. The swap now goes through `__init__` instead of `send_request`, which is a smaller surface. The flag-off path becomes a pure pass-through. A test should assert that the ctor merge keeps Strands' own `extensions` / `result_claims` keyword arguments.

**Rollback.** A dependency major can't sit behind a feature flag. Rolling back means reverting the PR and redeploying `backend.yml`, and the turn path has nothing to migrate. App-api and the Runtime ship together from one lock, so they never mix SDK lines.

## Test and dev-validation plan

1. **Hermetic.** Run `uv run python -m pytest tests/ -v` with the changes above, plus:
   - new unit tests for the `extensions` ctor merge (flag on/off; Strands' tasks extension kept) and for `server_info` identity resolution (handshake vs discover, with and without the stamp);
   - the snake_case reads in `discovery.py` and `mcp_apps.py`;
   - the R4 unknown-outcome rule;
   - the R5 auth-status record.
2. **Local real-wire** (existing `FlakyMCPServer` style, 2.x `MCPServer`):
   - a modern server: discover path, UI extension seen in the request `_meta` capabilities;
   - a legacy-only server: fallback path, timed;
   - 503-after-write: unknown outcome, session alive;
   - 401: `oauth_required` pre-flight emitted;
   - connection abort: heal hook reconnects.
3. **`/smoke-regression`** (`scripts/smoke_turns.py` turn matrix) on a local stack, then on dev after merge, because this touches the turn path and the `tool_result` / `ui_resource` / `oauth_required` events.
4. **Dev, after merge.** Use a session started after the deploy (memory: dev validation needs a post-deploy session).
   - GitHub connector, `issue_write`: the form mounts, the rail shows *Waiting for you* with **no hook write** (the marker came from GitHub), submit creates exactly one issue.
   - MCP App header shows "GitHub" and its icon. Excalidraw App frame mounts.
   - `toolConfigHash` and `cacheStatus` on the `C#` rows: no rotation beyond the explained one. `delete_repository` is absent.
   - Runtime logs: per-server pre-flight times before and after the deploy (the `turn_prelude` marks and the MCP pre-flight timing lines). Report the added milliseconds for the Gateway and each external server.
   - Expire or revoke a GitHub token: the Connect prompt appears (R5).
   - Admin → Tools → discover on a server with more than one page of resources (the `discovery.py` truncation).
   - CloudWatch: confirm whether MCP spans still appear (R6).

## Size

One PR, backend only. About 10 source files: `pyproject.toml`, `uv.lock`, the two transport call sites, the two auth classes, `mcp_apps.py`, `discovery.py`, `app_tool_dispatch.py`, the heal hook, and `external_mcp_client.py` / `auth_failure.py` for R5. Plus about 8 test files and the #1662 cleanup, either in the same PR or right after. No CDK, no SPA, no SSE contract change.

## Decision needed

**Proceed with the `mcp` 1.30.0 → 2.1.1 upgrade as scoped?** That means pinning `mcp==2.1.1`, raising `idna` to 3.20, and adding `httpx2`/`httpcore2==2.13.1` and `truststore==0.10.4` as constraints. Strands stays at 1.57.2. R4 and R5 get fixed in the same PR.
