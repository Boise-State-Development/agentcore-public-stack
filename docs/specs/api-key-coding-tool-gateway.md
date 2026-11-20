# Coding-tool gateway: OpenAI- and Anthropic-compatible routes for API keys

**Status:** Proposed (feature request). Nothing built.
**Date:** 2026-11-19
**Targets branch:** `develop`
**Related:** `backend/src/apis/app_api/chat/converse_routes.py` (the one API-key
route today), `docs/specs/quota-requests-and-extensions.md` (who pays),
`docs/specs/model-retirement.md` (`resolve_effective_model`), the model-access
rule in `CLAUDE.MD` (`grants_model_access`).

---

## 1. Summary

Add four routes to app-api that speak the two wire formats coding tools
already use, authenticated by the API keys users mint today:

| Route | Format | Used by |
| --- | --- | --- |
| `POST /v1/messages` | Anthropic Messages | Claude Code, the `anthropic` SDKs, Cline, Zed, opencode, Aider |
| `POST /v1/messages/count_tokens` | Anthropic Messages | Claude Code (`/context`); optional |
| `GET /v1/models` | both | model pickers in every client above |
| `POST /v1/chat/completions` | OpenAI Chat Completions | the `openai` SDKs, Jupyter/RStudio notebooks, Continue, Cline, Cursor (custom key), most other tools |

A user points a tool at `https://<host>/api` (Anthropic clients) or
`https://<host>/api/v1` (OpenAI clients) with their platform API key. Every
call runs through the checks `api_converse` already makes (key, rate limit,
real user, quota, retired models, RBAC, routing) and is priced into the same
cost rows, so quotas, chargeback and the admin cost dashboards cover it with no
new reporting.

The key research finding (§4) makes this smaller than it looks: **Bedrock
itself already serves the Anthropic Messages API for Claude**, on
`bedrock-runtime`'s `/anthropic` route. So `/v1/messages` is an authenticated
pass-through, not a translator. Only `/v1/chat/completions` on a Claude model
needs translation.

## 2. Why

CS courses, research labs working in Jupyter or RStudio, and staff developers
each buy personal coding-assistant subscriptions ($20 to $200 a month) or
personal API accounts. That spend sits outside the university's AWS account,
outside our quotas, and outside the chargeback grants that quota extensions
already track. The platform already has the pieces those users need: Bedrock
access, per-user model grants, quotas, cost tracking and API keys. What it
lacks is an endpoint their tools can talk to.

Prior art: UC Irvine's [ZotGPT Gateway](https://www.zotgpt.uci.edu/services/gateway/clients)
publishes setup guides for pointing Claude Code, VS Code and opencode at a
campus gateway. Theirs is built on Portkey. Ours stays inside this repository
and this AWS account.

## 3. What exists today, and the gaps

`POST /chat/api-converse` (`converse_routes.py:708`) runs this pipeline:

| Step | What it does | Where |
| --- | --- | --- |
| 1 | Validate `X-API-Key` (SHA-256 lookup, expiry) | `_validate_api_key` |
| 1.5 | 60 requests/minute per key, fail-open | `converse_routes.py:726` |
| 2.5 | Read the owner's real roles from the Users table, fail closed on no profile | `_build_user_from_api_key` |
| 2.6 | Quota check, fail-open on error **and on "no tier"** | `converse_routes.py:755` |
| 2.65 | Retired model → successor, or 410 | `resolve_effective_model` |
| 2.7 | RBAC model access via the one shared predicate | `can_access_model` → `grants_model_access` |
| 2.8 | Provider routing: Bedrock Converse vs Mantle vs bedrock-runtime Responses | `_resolve_model_routing` |
| 2.9 | In-flight cap (16 boto3 worker slots per task) for Converse paths | `converse_routes.py:841`, `bedrock_offload.py` |
| after | Price the call and write a `C#` cost row under the `api-converse-<key>` pseudo-session | `_record_cost` |

All of that carries over. What a coding tool needs and the route lacks:

1. **Tool calling.** `ConverseMessage.content` is a `str` and no `toolConfig`
   is ever sent. A coding agent is nothing but tool calls ("read this file",
   "run this command"), so today's endpoint cannot serve one at all.
2. **The wire formats.** Our SSE event names are our own. Anthropic clients
   expect Anthropic's events; OpenAI clients expect `chat.completion.chunk`
   frames ending in `data: [DONE]`.
3. **Client-placed prompt caching.** Claude Code marks cache breakpoints with
   `cache_control`. A gateway that drops them bills every turn as uncached
   input, with no error.
4. **Images and multi-part content.**
5. **`Authorization: Bearer`.** OpenAI clients send the key that way.
   Anthropic clients send `x-api-key` (which we already read, since headers are
   case-insensitive) or `Authorization: Bearer` (`ANTHROPIC_AUTH_TOKEN`).
6. **`GET /v1/models`.**
7. **Limits sized for agents.** See §5.8.

## 4. Research: Bedrock already speaks both formats

From the Bedrock user guide,
[Inference using Anthropic Messages API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html)
and [Chat Completions API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html):

- **`bedrock-runtime` serves the Anthropic Messages API for Claude** at
  `https://bedrock-runtime.{region}.amazonaws.com/anthropic/v1/messages`, in
  every Region that has Claude. The documented auth is a Bedrock API key or a
  short-term Bedrock bearer token in `x-api-key`, with an `anthropic-version`
  header. The body is the Anthropic body, `model` included, with Bedrock model
  ids (`us.anthropic.claude-sonnet-5`). Tools, vision, prompt caching,
  extended and adaptive thinking, structured outputs and mid-conversation
  system messages are all listed as supported.
- **`bedrock-runtime` serves OpenAI Chat Completions** at `/openai/v1` for the
  OpenAI and open-weight families (the docs' example is `openai.gpt-oss-120b`).
  Claude is not on that surface. Bedrock Mantle serves the same API for its
  roster.
- **Token counting** for cross-Region-only Claude models goes through the
  `bedrock-mantle` `count_tokens` path
  ([Count tokens](https://docs.aws.amazon.com/bedrock/latest/userguide/count-tokens.html)).

What we already have that this reuses:

- `apis/shared/bedrock/bearer_token.py` mints the short-term token from the
  task role's credentials. app-api already uses it for Mantle and the
  bedrock-runtime Responses surface.
- The app-api task role already holds `bedrock:CallWithBearerToken`
  (`app-api-iam-grants.ts:691`, sid `BedrockRuntimeCallWithBearerToken`).

Claude Code's own
[gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol.md)
says what a gateway must do. The rules that shape this design:

- Forward `anthropic-version` and `anthropic-beta` **verbatim**; do not
  allow-list beta values, since the set changes every release.
- Forward the `system` array and `cache_control` markers unchanged; never
  flatten block content to strings.
- **Forward upstream error bodies unmodified.** Claude Code's automatic
  recovery (dropping a rejected beta, `context_management`, `output_config`,
  mid-conversation system messages) matches on the upstream's wording. An
  Anthropic-format client pointed at a Bedrock upstream sends features Bedrock
  rejects, and this retry path is how those turns still succeed.
- Stream event by event; never buffer. End with `message_delta` and
  `message_stop`.
- Keep bytes flowing. Claude Code aborts a stream after its idle watchdog, and
  during a long thinking pause the upstream's `ping` events may be the only
  bytes. The guide notes Bedrock's event stream has no pings.
- `retry-after` in integer seconds; above 60 makes Claude Code stop retrying.
- Model discovery is `GET /v1/models?limit=1000` with a 3-second timeout and
  no redirects. It keeps entries whose `id` contains `claude` or `anthropic`,
  and runs only when `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`.

## 5. Design

### 5.1 Base URL and paths

CloudFront's `/api/*` behavior strips `/api` and forwards to the app-api ALB
(`spa-distribution-construct.ts`, the `api-path-strip` function). New routers
mount at the app-api root under `/v1`, which no existing route uses:

| Client | Configured base URL | Request path at the edge | Path at app-api |
| --- | --- | --- | --- |
| Anthropic (`ANTHROPIC_BASE_URL`) | `https://<host>/api` | `/api/v1/messages` | `/v1/messages` |
| OpenAI (`base_url`) | `https://<host>/api/v1` | `/api/v1/chat/completions` | `/v1/chat/completions` |

The `/api/*` behavior already forwards `Authorization` and every other viewer
header (`ALL_VIEWER_EXCEPT_HOST_HEADER`, caching disabled). The TUI's
`Authorization: BFF` traffic, verified live against a deployed dev
environment, proves this path. Claude Code's
`HEAD /api/hello` warm-up probe becomes `/api/api/hello` and 404s, which the
guide says is harmless.

### 5.2 Credentials

- Accept the platform key in `x-api-key` **or** `Authorization: Bearer <key>`.
  If both are present and differ, refuse with 401. `X-API-Key` on
  `/chat/api-converse` is unchanged.
- On app-api, a `Bearer` value is only ever a platform API key checked by
  `ApiKeyService.validate_key`. It is never decoded as a JWT. This keeps the
  `CLAUDE.MD` rule that `get_current_user_trusted` stays off app-api.
- No middleware change. `SessionRefreshMiddleware` resolves only the `BFF`
  scheme and lets `Bearer` fall through (`session_refresh.py:87`), and
  `CSRFMiddleware` enforces only when a session cookie is present.
- **The client's credential never goes upstream, and ours never comes back.**
  The upstream request carries a freshly minted bearer token (re-minted per
  request, as `bedrock_responses.py` does, because a frozen token works only
  until it expires). That token is never logged, never echoed in an error, and
  never returned. Tests assert both directions (§7).

### 5.3 One shared guard: `authorize_api_key_call()`

Extract steps 1 through 2.9 of `api_converse` into one function in a new
`apis/app_api/chat/api_key_guard.py`:

```python
@dataclass
class ApiKeyCall:
    key: ValidatedApiKey
    user: User                     # hydrated, never synthesized
    model_id: str                  # after retirement redirect
    record: Optional[ManagedModel]
    provider: str                  # "bedrock" | "mantle" | "bedrock-responses"
    api_mode: MantleApiMode
    region: Optional[str]
    release_slot: Callable[[], None]

async def authorize_api_key_call(raw_key: str, model_id: str, *,
                                 needs_worker_slot: bool,
                                 require_quota_tier: bool) -> ApiKeyCall: ...
```

`require_quota_tier` is `True` on every `/v1` route (D1) and `False` on
api-converse, which keeps its current behavior.

It raises one typed `ApiKeyCallRefused(status, kind, message, retry_after)`.
Each route renders that refusal in its own format:

| Format | Error body |
| --- | --- |
| api-converse | `{"detail": "..."}` (unchanged) |
| Anthropic | `{"type": "error", "error": {"type": "rate_limit_error", "message": "..."}}` |
| OpenAI | `{"error": {"message": "...", "type": "rate_limit_exceeded", "code": "..."}}` |

Status codes stay exactly as today (401, 403, 410, 429). `api_converse` is
rewritten to call the guard first, as its own PR, with no behavior change. Its
existing tests are the safety net: `test_api_converse_mantle.py`,
`test_api_converse_offload.py`, `test_api_converse_prompt_cache.py`,
`test_api_converse_cache_classification.py`, `test_converse_cost_accounting.py`,
`test_model_access_convergence.py` and `test_pbt_model_access.py` under
`backend/tests/routes/`.

This is also why the guard comes first: there must be exactly one place that
decides whether an API-key call may run. The model-access check has already
diverged twice in this repo when it existed in more than one place.

### 5.4 `POST /v1/messages`: authenticated pass-through

1. Parse the JSON body. Read `model` and `stream`; leave everything else alone.
2. Resolve `model` to a catalog row. Accept the catalog id
   (`us.anthropic.claude-sonnet-4-6`) and the Anthropic id Claude Code sends
   by default (`claude-sonnet-4-6`). The Anthropic id resolves through a
   small alias index built from the catalog: strip the profile prefix and
   `anthropic.`, then match. Rewrite `model` to the catalog id. That is the only
   change to the body.
3. Run the guard. A model whose provider is not Bedrock Claude gets a 400 in
   Anthropic's error format. v1 serves Claude only on this route.
4. Send the request to `bedrock-runtime.{region}/anthropic/v1/messages` with
   async `httpx`, using the minted token, the client's `anthropic-version` and
   `anthropic-beta` headers verbatim, and nothing else from the client.
5. **Streaming:** relay each SSE frame the moment it arrives. While relaying,
   read `message_start.message.usage` and each `message_delta.usage` to build
   the cost. If no frame has arrived for 15 seconds, send our own
   `event: ping`. That covers Claude Code's watchdog, CloudFront's 60-second
   origin read timeout (`spa-distribution-construct.ts:227`) and the ALB's
   60-second idle timeout. Pings are harmless if Bedrock sends its own too.
6. **Errors:** forward Bedrock's status and body byte for byte (§4). Map a
   throttle to 429 with an integer `retry-after`.
7. **Cost:** record in a `finally`, so a client disconnect still records what
   Bedrock already billed. A disconnect after `message_start` still costs the
   input and cache tokens, and on a coding turn those are most of the bill.
   Map the usage like this:

   | Anthropic usage | Our usage dict |
   | --- | --- |
   | `input_tokens` | `inputTokens` |
   | `output_tokens` (last `message_delta`) | `outputTokens` |
   | `cache_read_input_tokens` | `cacheReadInputTokens` |
   | `cache_creation_input_tokens` | `cacheWriteInputTokens` |

   Anthropic's `input_tokens` excludes the cache buckets, the same as Converse,
   so no inclusive-to-exclusive correction is needed (the OpenAI surfaces need
   one; see `usage_normalization.py`). Rows use the existing
   `api-converse-<key>` pseudo-session, so the release ledger
   (`ledger_rollups.py:178`), per-user cost summaries and quota already count
   them with no change.
8. **No worker slot.** The call is async `httpx` and holds no boto3 thread, so
   the in-flight cap does not apply. That matches how the Mantle surfaces are
   treated today. §5.8 adds a per-key concurrency cap instead.
9. **We add nothing to the prompt:** no cache points (the client places them),
   no system text, no guardrail. We do not log content, only ids and counts.

### 5.5 `POST /v1/messages/count_tokens`

The guard runs (key, rate limit, RBAC), but the call is not counted against
quota. It forwards to Bedrock's count-tokens path. Our catalog uses
cross-Region profile ids, so that is most likely the Mantle `count_tokens`
path; the spike confirms it. This route is optional: without it, Claude Code
estimates from character counts. It ships in its own PR.

### 5.6 `GET /v1/models`

One route serves both formats. It lists the catalog models the caller's roles
allow, via `filter_accessible_models` (which delegates to
`grants_model_access`), and drops non-chat models. Each entry carries both
formats' fields, since clients of both read `data[].id` and ignore the rest:

```json
{
  "object": "list",
  "data": [
    {
      "id": "us.anthropic.claude-sonnet-4-6",
      "object": "model", "type": "model",
      "display_name": "Claude Sonnet 4.6",
      "created": 1767225600, "created_at": "2026-01-01T00:00:00Z",
      "owned_by": "bedrock"
    }
  ],
  "has_more": false
}
```

It must answer in under 3 seconds and must never redirect. Cache it per user
for 60 seconds. Catalog Claude ids contain `anthropic`, so they pass Claude
Code's discovery filter.

### 5.7 `POST /v1/chat/completions`

Routing depends on the catalog row's provider:

**(a) `mantle` or `bedrock-responses` models in Chat Completions mode:**
pass-through to Bedrock's own OpenAI endpoint, with the same pattern as §5.4.
Usage goes through the existing `usage_normalization.py`, because OpenAI's
`prompt_tokens` includes cached tokens. Caveat: GPT-5.6 caches only over the
Responses API (`bedrock_responses.py`), so Chat Completions calls to it run
uncached. A `/v1/responses` route would fix that (§9).

**(b) Claude models:** translate OpenAI to Anthropic Messages, then reuse the
§5.4 transport. We chose this over Converse because §5.4 already does async
streaming with no worker slot, and it has one usage parser and one error path.
Translating to Converse would put every IDE stream on the 16-slot boto3 pool.

| OpenAI request | Anthropic request |
| --- | --- |
| `system` / `developer` messages | `system` blocks, in order |
| `content` string or parts (`text`, `image_url` with a `data:` URI) | content blocks (`text`, `image` base64). Remote image URLs are refused with 400, so the gateway never fetches URLs. |
| assistant `tool_calls[]` | `tool_use` blocks (`id`, `name`, `input` = parsed `arguments`) |
| `role: "tool"` messages | `tool_result` blocks; consecutive ones merge into one user turn |
| `tools[].function` | `tools[]` (`name`, `description`, `input_schema` = `parameters`) |
| `tool_choice` `auto` / `required` / named / `none` | `auto` / `any` / `{"type":"tool","name":…}` / `none` |
| `max_tokens` or `max_completion_tokens` | `max_tokens` (required by Anthropic; default from the catalog row's output cap) |
| `temperature`, `top_p`, `stop` | `temperature`, `top_p`, `stop_sequences` |
| `n > 1`, `logprobs`, `response_format: json_schema` | 400 in v1 (structured output is a follow-up) |

**Prompt caching:** OpenAI clients send no cache markers, so we place them
ourselves, using the policy `_with_cache_points` already applies to
api-converse. Mark the last tool, the end of the system prompt, and the last
message when there are two or more messages. Coding agents re-send 50k to
150k tokens of the same prefix on every tool round trip, so this decides
whether the route is affordable. A cache read costs about a tenth of input.

**Response:** a `chat.completion` object, or a `chat.completion.chunk` stream.
The stream sends `delta.role` first, then `delta.content`, then
`delta.tool_calls[{index, id, function: {name, arguments}}]` as Anthropic's
`input_json_delta` arrives. Then a final chunk with `finish_reason`, a usage
chunk when `stream_options.include_usage` is set, and `data: [DONE]`.
`finish_reason` maps `end_turn` / `stop_sequence` to `stop`, `tool_use` to
`tool_calls`, `max_tokens` to `length`, and refusals to `content_filter`.
Usage reports `prompt_tokens` = input + cache read + cache write, with
`prompt_tokens_details.cached_tokens` = cache read.

**Thinking stays off on this path in v1.** With thinking on, Claude requires
earlier thinking blocks to be sent back inside a tool-use loop, and an OpenAI
client has nowhere to keep them. A `reasoning_effort` field is ignored and
logged. Anthropic-format clients get thinking through §5.4.

Non-Claude Converse models (Nova, Llama, and others) are out of v1.

### 5.8 Limits sized for coding traffic

| Limit | Today | Proposal |
| --- | --- | --- |
| Rate limit | 60/min per key, hardcoded (`rate_limit.py`) | A setting, `API_KEY_GATEWAY_RATE_LIMIT_PER_MINUTE`, for the `/v1` routes. Each tool round trip is one request, Claude Code makes background calls, and subagents run in parallel, so one busy session can pass 60. Start at 120 and measure (D2). |
| Concurrency | none on async paths | Per-key cap on open `/v1` streams (default 8), so one user's subagent fan-out cannot crowd out a task |
| Quota with no tier | fail-open (`converse_routes.py:755`) | **Decided (D1): fail closed on `/v1`.** No tier means 403 "no quota assigned", in the route's own error format. Quota is always enforced for these routes. Coding traffic is far more expensive than script traffic. |
| Quota error | fail-open | unchanged |
| Overshoot | quota checked before the call | unchanged: one request at most. The `max_tokens` cap bounds it. |
| Non-streaming duration | CloudFront and ALB cut at 60 s | Document "use `stream: true`". Coding tools always stream. Long non-streamed notebook calls 504; raising CloudFront's origin read timeout (a quota increase, up to 180 s) is a follow-up if notebooks hit it. |
| Request size | not measured | Coding agents send multi-MB bodies. The spike sends a 5 MB body through CloudFront → ALB → app-api → Bedrock. |
| Keys per user | one; minting revokes the old key (`service.py:38`); 90-day expiry (`service.py:13`) | **Decided (D6): stays at one key per user.** The same key works in every tool. The setup tab (§5.10) says so, and warns that making a new key signs out every tool using the old one. |

### 5.9 Flag, config, infrastructure

- `API_KEY_GATEWAY_ENABLED` / `CDK_API_KEY_GATEWAY_ENABLED`, app-api only. All
  `/v1` routes 404 while it is off. SPA flag `features.apiKeyGateway` controls
  the setup tab. Plumb the flag the usual way: `config.ts`,
  `scripts/common/load-env.sh`, the `platform.yml` job env, and the
  `createMockConfig` test mock.
- **No new AWS resources.** The IAM grants already exist (§4). The spike must
  still confirm which action the `/anthropic` route authorizes against. If it
  is not covered by the task role's existing `bedrock:InvokeModel*` grant,
  that is a one-statement CDK change.
- No change to the Runtime, the agent, or `inference_api`. This is app-api
  only, which also keeps it clear of the import boundaries.

### 5.10 SPA: "Use with coding tools"

Add a tab on the API Keys page
(`frontend/ai.client/src/app/settings/pages/api-keys/api-keys.page.ts`) next to
the existing curl, Python and JavaScript examples. It builds its URLs from the
same absolute base the page already computes. It contains copy only:

```bash
# Claude Code
export ANTHROPIC_BASE_URL="https://<host>/api"
export ANTHROPIC_AUTH_TOKEN="<your key>"
export ANTHROPIC_MODEL="us.anthropic.claude-sonnet-4-6"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="us.anthropic.claude-haiku-4-5-20251001-v1:0"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
```

```python
# OpenAI SDK (notebooks, scripts)
from openai import OpenAI
client = OpenAI(base_url="https://<host>/api/v1", api_key="<your key>")
```

Pinning `ANTHROPIC_DEFAULT_HAIKU_MODEL` matters for cost. Behind a gateway,
Claude Code otherwise runs its background tasks on the main model.

Each user has one key (D6), so the tab tells them to use that same key in
every tool, and that creating a new key revokes the old one everywhere.

## 6. Plan

| PR | Content | Size |
| --- | --- | --- |
| PR-0 | **Spike, dev only, no merge.** From the app-api task role in dev, call `/anthropic/v1/messages` with a minted token. Record: which header form the route accepts (`x-api-key` vs `Authorization`), which IAM action it evaluates, whether the stream carries `ping`, the exact error bodies for a rejected `anthropic-beta` value and for `context_management`, the usage fields on a cache-hit turn, which model-id forms it accepts (`us.` / `global.`), and whether a 5 MB body survives CloudFront. Capture one real tool-using stream as a test fixture. Findings go back into this spec. | 0.5 day |
| PR-1 | Extract `authorize_api_key_call()`; `api_converse` uses it. No behavior change. | 1 day |
| PR-2 | Flag, credential headers, `/v1/messages` pass-through, `/v1/models`, cost recording, ping keepalive, per-key stream cap, fail-closed quota (D1). | 2–3 days |
| PR-3 | `/v1/messages/count_tokens`. | 0.5 day |
| PR-4 | `/v1/chat/completions`: OpenAI → Anthropic translation for Claude, pass-through for Chat-Completions-mode Mantle models. | 3–4 days |
| PR-5 | SPA tab and user docs. | 1 day |
| PR-6 | Rate limit setting (after D2 is decided). | 0.5 day |

About two weeks of work for one developer. PR-2 alone makes Claude Code,
Cline and the `anthropic` SDK work, so it can ship to dev on its own.

## 7. Testing

- **Real captures over typed fixtures.** The TUI's agent-stream dialect passed
  87 type-derived tests and still broke on the first real stream. Replay
  PR-0's captured Bedrock stream through `/v1/messages`. Then assert the frames
  arrive unmodified, the cost row matches the usage in the capture, and a
  `ping` appears across an artificial 20-second gap.
- **Official SDKs as the contract.** Dev-dependency tests point the `anthropic`
  and `openai` Python SDKs at the app through an ASGI transport, with Bedrock
  mocked. They run non-streaming, streaming, a tool call and a tool result
  round trip. The SDKs' own parsers are the strictest check that our shapes are
  right.
- **Guard parity.** PR-1 changes no existing assertion. A property test runs
  the same refusal cases through all three routes and checks they agree on the
  status code. A user with no quota tier gets 403 on every `/v1` route and
  still passes on api-converse.
- **Security.** A `Bearer` JWT is rejected and never decoded. The client's
  `Authorization` and `x-api-key` never appear on the upstream request. The
  minted token never appears in a response body, a header or a log record.
  A key in both headers with different values gets 401.
- **Cost.** A two-turn conversation writes two `C#` rows, and the second shows
  `cacheReadInputTokens > 0`. A client disconnect after `message_start` still
  writes a row.
- **Live, in dev:** one tool-using Claude Code task, one streamed OpenAI SDK
  tool call, and Cline against both routes.

## 8. Decisions needed

| # | Question | Recommendation |
| --- | --- | --- |
| D1 | Should `/v1` refuse a user with no quota tier? | **Decided 2026-11-19: yes.** Quota is always enforced on `/v1`. api-converse is unchanged. |
| D2 | `/v1` rate limit default | 120/min, then measure in dev |
| D3 | Can every key holder use `/v1`, or only a role permission? | Every key holder. Model grants and quota already gate it, and a second permission is one more thing an admin can misconfigure. |
| D4 | One-hour cache writes (`cache_creation.ephemeral_1h_input_tokens`) cost 2× input, not 1.25×, and the pricing snapshot has one write rate | v1 prices all writes at the 5-minute rate and logs the 1-hour volume; add the second rate if it turns out to matter |
| D5 | Show Claude thinking to OpenAI clients? | No, not in v1 (§5.7) |
| D6 | Multiple named keys per user? | **Decided 2026-11-19: no.** One key per user, used in every tool. |

## 9. Out of scope and alternatives rejected

- **`/v1/responses`** (OpenAI Responses API, used by Codex CLI and needed for
  GPT-5.6 caching). A follow-up; it reuses the guard and the pass-through.
- **Embeddings** (`/v1/embeddings`).
- **Exposing Bedrock's own InvokeModel paths** (`/model/{id}/invoke-with-response-stream`,
  `CLAUDE_CODE_USE_BEDROCK=1` with `ANTHROPIC_BEDROCK_BASE_URL`). Rejected:
  Claude Code then sends only Bedrock's feature subset, the binary
  `application/vnd.amazon.eventstream` body must be relayed byte for byte, and
  few tools besides Claude Code speak that format.
- **A LiteLLM sidecar.** It speaks both formats already, but it brings its own
  key store and budget system. We would have to wire our keys, RBAC, quotas
  and cost rows into its auth hook, which is about as much work as §5.4 and
  §5.7. We would also run and patch a second service.
- **Changing `/chat/api-converse`.** Existing scripts depend on its shape. It
  keeps working and gains only the shared guard.
