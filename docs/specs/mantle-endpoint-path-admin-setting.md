# Design note: revive `mantleEndpointPath` as a live admin setting

**Status:** Proposal (not started)
**Author:** (drafted with Claude)
**Date:** 2026-07-13
**Related:** `apis/shared/models/mantle.py`, `apis/shared/bedrock/bearer_token.py`,
`frontend/.../admin/manage-models/models/curated-models.ts`

## Problem

Bedrock Mantle serves different models at different OpenAI-compatible base paths
on the *same* host, and there is **no discovery API** — the path is a per-model
fact published only in each model's AWS model card:

| Model family | Mantle base path |
|---|---|
| `openai.gpt-oss-*`, `qwen.*` | `/v1` |
| `google.gemma-3-*` | `/v1` |
| `google.gemma-4-*` | `/openai/v1` |
| `openai.gpt-5.*` | `/openai/v1` |

Today the base path is derived *inside the Strands SDK* from a hardcoded
prefix table (`strands.models._openai_bedrock._OPENAI_PATH_MODEL_PREFIXES`,
currently just `("openai.gpt-5.",)`). Any model whose id isn't in that table
falls through to `/v1`. When that's wrong (Gemma 4), inference 401s with
`access_denied` / "... is not enabled for this account".

We've now hit this **twice** (gpt-5, then Gemma 4). Each occurrence requires a
dependency bump or a build-time monkeypatch of the SDK's private tuple
(`_ensure_gemma4_openai_v1_routing`, added 2026-07-13 as the stopgap). The path
is the one per-model Mantle fact we do **not** model as admin data — `apiMode`
(`chat` vs `responses`) and `region` already are.

## Why it's SDK-derived today

The builder delegates the whole Mantle wire-up to the SDK's
`bedrock_mantle_config`, which bundles three things:

1. Region resolution
2. **Per-request bearer-token minting + rotation** (`provide_token`)
3. `base_url` derivation from the model id (the prefix table)

`resolve_bedrock_client_args` sets `base_url` itself and the model classes
**reject** a caller-supplied `base_url` when `bedrock_mantle_config` is set
(fail-fast `ValueError`). So #3 is not separable from #1/#2 — to control the
path you must stop using `bedrock_mantle_config` entirely.

There is a now-`[DEPRECATED]` field `mantleEndpointPath` (`ManagedModel*` in
`apis/shared/models/models.py`) and a helper `get_mantle_base_url(region,
endpoint_path)` that already builds arbitrary paths. The plumbing is half
present; it was retired, not removed.

## Proposal

Make the base path an **admin-set, per-model** value again — data, not code —
by building the OpenAI client ourselves instead of via `bedrock_mantle_config`.

### Backend (`build_mantle_model`)

- Un-deprecate `mantleEndpointPath`. Default `/v1`; admin sets `/openai/v1` for
  Gemma 4 / gpt-5 (documented on the model card, so it's a copy-paste fact).
- When building the model, construct `client_args` ourselves:
  - `base_url = get_mantle_base_url(region, endpoint_path)`
  - `api_key = generate_bedrock_bearer_token(region)` (already exists)
  - Do **not** pass `bedrock_mantle_config`.
- Delete `_ensure_gemma4_openai_v1_routing` and the SDK-tuple monkeypatch.

### Frontend

- Add an "Endpoint path" field to the Mantle model form (default `/v1`), with a
  hint linking to "the path shown on the model's AWS model card."
- Curated cards carry the correct `mantleEndpointPath` per model.

### The cost we take back: token lifecycle

`bedrock_mantle_config` mints a **fresh token per request**; a self-set
`api_key` is fixed at model construction. Mitigations:

- The agent model is rebuilt per turn (`AgentFactory`), so a token minted at
  build time is fresh for the turn's duration — the common case is covered.
- The minted token's lifetime (~12h) far exceeds a single turn.
- **Risk:** a very long-lived / resumed agent could outlive the token. Needs a
  check of the longest-lived `build_mantle_model` consumer
  (`/chat/api-converse`, agent resume) before committing. If any consumer holds
  one model across the token lifetime, add a small refresh shim (mint via a
  callable the client invokes per request) — reproducing what the SDK does.

## Decision

| | Revive admin path (this note) | Keep SDK-derived + patch (current) |
|---|---|---|
| New `/openai/v1` family | data change, zero code | dep bump or monkeypatch |
| Token minting/rotation | **we own it** | free from SDK |
| Consistency with `apiMode`/`region` | matches | inconsistent |
| Blast radius | medium (touches token path) | tiny (one tuple) |

**Recommendation:** ship the monkeypatch now (done) to unblock Gemma 4. Adopt
this note **only if a third family** forces the issue, or if we want to stop
tracking upstream SDK releases for a routing table we can trivially own. The
one open question — token lifetime vs. longest-lived consumer — must be
answered before implementation.

## Open questions

1. What is the longest-lived single `OpenAIModel`/`OpenAIResponsesModel`
   instance across all `build_mantle_model` callers? (Determines whether a
   static build-time token is safe or a refresh shim is required.)
2. Does `provide_token` from `aws-bedrock-token-generator` expose the token
   TTL, so we could set an explicit shorter expiry and know when to refresh?
3. Any Mantle model served on a path *other* than `/v1` or `/openai/v1`? (If
   the space stays two-valued, a boolean toggle may beat a free-text path.)
