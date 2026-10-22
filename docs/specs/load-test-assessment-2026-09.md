# Load-test report assessment — "Five performance fixes, proven by a controlled A/B"

**Source:** `AgentCoreSolution-PerformanceTesting-ProductionReadyFixes.pdf`, AuraX platform team,
run 2026-09-15 on a production-mirror environment, Claude Haiku 4.5, 5M-token/min quota.
**Assessed against:** `develop` at `ca3e8d2c` (2026-09-18), strands-agents 1.55.0,
bedrock-agentcore 1.21.0.
**Status:** **COMPLETE — all five findings and one residual shipped.** Merged to `develop` 2026-09-19
(#1154/#1155 verified in dev; the rest deploy on the next rollout): #1154 (bytecode + warm-up, bounded
CountTokens, session split memo, four cache families, CORS synth guard), #1155
(`assistant_id` in the cache key → spreadsheet sessions cache their agent),
#1156 (system prompt counted against a probe message — Bedrock rejects an empty
conversation, verified live, so `systemTokens` had been chars/4 since it
shipped), #1157 (bounded memory retrieval, summary namespace off the per-message
path: 3 → 2 lookups), #1158 (Spreadsheet Analysis auto-enabled on attachment,
RBAC-gated). The response to AuraX is at
[`docs/one-pagers/load-test-response-to-aurax-2026-09.md`](../one-pagers/load-test-response-to-aurax-2026-09.md).
Still open: the Phase 3 A/B rerun (needs the provisioning gate), the
`RetrieveMemoryRecords` 30 → 300/s quota request, the AWS cold-start thread, and
the Phase 0 asks to AuraX.

---

## 1. Verdict in one table

| # | Report claim | Verdict on `develop` | Where it lives |
|---|---|---|---|
| 1 | The prompt is counted **three times per turn** because the agent is rebuilt every turn and re-splits its context attribution | **Confirmed for the majority cohort.** The split is memoised per `Agent` *instance*; only artifact-tool and no-injected-tool sessions reuse an instance. Every other injected-tool family rebuilds the agent each turn → 2 attribution counts + Strands' own 1 = 3 | `session/hooks/context_attribution.py:139-160`, `inference_api/chat/service.py:340`, `apis/shared/tools/injected.py:102` |
| 2 | The image ships **no compiled bytecode**, so each fresh micro-VM compiles the app + sympy on the first message | **Confirmed.** `uv sync` runs without `UV_COMPILE_BYTECODE`; `/app/src` is copied raw; the container runs as a non-root user, so nothing can be written back at runtime | `backend/Dockerfile.inference-api` |
| 3 | **One CountTokens call sits in front of every model stream** and gets throttled under load | **Confirmed.** `use_native_token_count=True` makes Strands `await _estimate_input_tokens()` before every model call; a throttle costs boto's retry/backoff on the reply path, then falls back to chars/4 | `core/model_config.py:434`, strands `event_loop/event_loop.py:494` |
| 4 | Browser uploads fail: the uploads bucket has **no CORS rule** | **Environment gap, not a mainline bug.** CDK attaches CORS to `UserFilesBucket` *only if* `CDK_DOMAIN_NAME` / `CDK_CORS_ORIGINS` is set; an unset value only prints a warning | `infrastructure/lib/constructs/data/file-upload-construct.ts:49-80`, `lib/config.ts:1477` |
| 5 | With the default tool set the assistant **refuses to read an attached CSV** and tells the user to enable a tool | **Confirmed.** CSV/XLSX are never sent inline at any size; the Spreadsheet Analysis tools are opt-in (`enabledByDefault: False`); the system prompt instructs the model to send the user to Customize → Tools | `inference_api/chat/routes.py:874-905, 1075-1090`, `core/system_prompt_builder.py:101-125` |
| R1 | Residual: **6 conversation-memory lookups per message** | **Confirmed mechanism, count depends on cohort.** A cold `initialize()` = 5 `ListEvents` (read_session ×2, read_agent ×2, list_messages ×1). *Every* user message = up to 3 `RetrieveMemoryRecords` (preferences, facts, summary namespaces) inside the SDK's `append_message`. Rebuilt-per-turn sessions pay all of it every turn | SDK `session_manager.py:320-333, 463-481, 783, 872`; `session/session_factory.py:205-235` |
| R2 | Residual: memory service quota **30 lookups/s**, crossed at ~5 msg/s | **Quota identified.** `RetrieveMemoryRecords` = 30 req/s/account (adjustable). `ListEvents` = 200/s/account (adjustable) + 20/s per actor-session (fixed). The report's "30" is the retrieval quota, not ListEvents | AWS quotas page (fetched 2026-09-18) |
| R3 | Residual: micro-VM cold start **~12 s** | **Plausible, unverified here.** No warm pool exists in the Runtime product; our own Aug-05 probe measured turn-1 at 7–8 s in dev | `docs/specs/agent-cache-extra-tools-bypass.md` §8.2 |

Two supporting claims could not be verified:

- **"One of the five is already contributed upstream."** No matching PR exists in this
  repo, and a search of `strands-agents/sdk-python` for count_tokens / token-estimation
  PRs found nothing resembling "background side-request + probe user message". Ask
  AuraX for the link.
- **"The model rejects an empty conversation"** (the reason for their probe message).
  Our attribution hook calls `count_tokens(messages=[])` for the system-prompt
  partition. If Bedrock rejects that, Strands swallows the error and the
  `systemTokens` partition has been chars/4 all along. Dev SSO was expired during
  this assessment; the probe is one command (§5).

---

## 2. Why the numbers are credible — and where they don't transfer

**Mechanism matches the curve.** Bedrock throttles CountTokens on a *request-rate*
quota separate from the model's token quota. Three counts per turn at 35 msg/s is
~105 extra Bedrock requests/s on the critical path; removing them removes the
request-rate exhaustion while the *token* burn rises (their 53% → 72%), which is
exactly what "the fixes convert wasted request budget into productive output"
predicts. The 541 → 0 throttle result is consistent with our code.

**The baseline is worse than `develop`, so the deltas are upper bounds.** Their branch
predates the "load-shedding subsystem" on mainline (our per-user quota runway and
cooldowns), and very likely predates #841 (2026-08-05, runtime session-id pinning).
Before #841 the agent cache *never* hit, so their baseline rebuilt the agent on
every turn for every session. On `develop` that is true only for non-artifact
injected-tool sessions — which was 76% of prod sessions when last measured
(2026-08-03), driven by `analyze_spreadsheet` / `list_spreadsheets`. Expect the
real-world gain on fix 1 to be large but smaller than 18.2 s → 6.6 s.

**Load model arithmetic checks out.** 15 msg/user/day × 10% in the peak hour =
1.5 msg/user/hour, so 1 msg/s ≈ 2,400 daily users. 17 msg/s ≈ 40,800 is a fair
readiness target.

**Cold-start cells are honestly footnoted.** The 59% / 88% success cells are ramp
first-steps, not load ceilings; the throttle curve and new-chat p95 carry the case.

**Do not reuse their cost figure.** $0.0025/message at a 99% prompt-cache hit rate
reflects tiny, repetitive synthetic prompts. Prod runs at a write:read ratio near
1:2 with attachments at ~31% of spend; our own admin cost drill-down is the number.

**Their harness is ours.** "2,000 synthetic accounts, SSE support, realistic
peak-hour task mix" describes `tests/load/` + `scripts/load-test/provision.sh`. That
is good news: the A/B is reproducible on dev without new tooling.

---

## 3. Design notes on the fixes as described (read before re-implementing)

1. **Backgrounding the count changes what compaction sees.** If
   `_estimate_input_tokens` no longer awaits CountTokens, `projected_input_tokens`
   is either `None` or chars/4 on that call. Our compaction policy and the
   attribution hook both consume it. The prompt-cache contract makes compaction
   timing a *cost* decision, so any change here must keep the authoritative count
   on the turns that decide a checkpoint (cold start, post-tool batches) and may
   only drop it where the warm-path delta is small.
2. **"Never mutates model configuration."** `CountTokensBedrockModel.count_tokens`
   swaps `config["model_id"]` to the base id for the duration of the call. That is
   safe only because count and stream never overlap on one instance. The moment
   the count becomes a concurrent task, a `stream()` can read the base id and fail
   on-demand invocation. Pass `modelId` explicitly instead of mutating config.
3. **Throttle latency is retry backoff, not the call.** Strands wraps the boto
   client's default retry mode. A dedicated CountTokens client with
   `retries={"total_max_attempts": 1}` and a ~2 s read timeout bounds the cost of a
   throttle to one failed attempt before the heuristic fallback — a smaller change
   than restructuring the event loop, and it composes with note 1.
4. **Auto-enabling spreadsheet tools must ride the cache key.** The existing
   `document_tools` bit in `_create_cache_key` is the pattern: the gate answer and
   the cache key must be computed by the same function or a paused agent gets
   orphaned. RBAC still applies — injection is "enable for this turn", never
   "grant". It changes `toolConfig` once on the attach turn (one prefix re-write,
   same as `document_read` today).
5. **Bytecode: precompile, don't rely on runtime writes.** The image runs as
   `bedrock_agentcore` (uid 1000) with `/app` chowned, so Python *could* write
   `__pycache__`, but every micro-VM starts from the image layer, so it recompiles
   every time regardless. `UV_COMPILE_BYTECODE=1` for the venv plus
   `python -m compileall -q /app/src` in the final stage is the whole fix.

---

## 4. Plan

Ordered by (impact on new-chat p95 and throttles) ÷ (effort and risk). Each item
names its proof so we do not ship on the report's numbers alone.

### Phase 0 — Get the artifacts (this week, no code)

- Ask AuraX for: the branch/diff of the five fixes, the run ledgers, the
  upstream PR link, and the exact `git merge-base` with our `develop`. A
  contribution case is fastest to evaluate as a PR against `develop`; each fix
  will need re-basing because the branch predates #839/#841 and the quota runway.
- Run the empty-messages CountTokens probe (§5) to settle whether our
  `systemTokens` partition has ever been authoritative.

### Phase 1 — No-regret changes (target: one PR each, all this sprint)

**P1-A  Precompile bytecode + warm the first turn** (fix 2)
- `Dockerfile.inference-api`: `ENV UV_COMPILE_BYTECODE=1` in the builder stage;
  `RUN python -m compileall -q /app/src` after the source copy in the final stage.
- Inference-api lifespan: import `strands_tools.calculator` (sympy), build the
  Bedrock / AgentCore Memory clients, and construct one throwaway
  `CountTokensBedrockModel` at startup so the first message pays none of it.
- **Proof:** `docker run … python -X importtime -c "import apis.inference_api.main"`
  before/after; then `agent_status` `thinking` cycle-0 timing and the Runtime
  `InvocationLatency` on new sessions in dev.

**P1-B  Bound CountTokens on the reply path** (fix 3, notes 1–3)
- Dedicated `bedrock-runtime` client for counting with
  `retries={"total_max_attempts": 1}`, 2 s read timeout; pass `modelId` explicitly
  (remove the config swap).
- Keep the awaited authoritative count on cold start and on the first call after a
  tool batch (checkpoint-deciding turns); on plain warm turns, where Strands counts
  only the new messages, run it as a task and let compaction use the last
  authoritative baseline + chars/4 for the delta.
- **Proof:** unit test injecting `ThrottlingException` on count asserts the model
  call starts within the timeout; dev: `C#` rows show unchanged `cacheStatus` and
  no compaction-timing drift over a 20-turn session.

**P1-C  Memoise the attribution split per session, and stop rebuilding the agent**
(fix 1, and half of R1)
- Short term: key the split by `(session_id, systemPromptHash, toolConfigHash)` in a
  process-level dict instead of the `Agent` instance, so a rebuilt agent never
  recounts.
- Real fix: extend `KEY_DESCRIBED_INJECTED_TOOL_IDS` to the spreadsheet-analysis,
  Word, Excel, workspace and PowerPoint families (spec
  `agent-cache-extra-tools-bypass.md` §6). Each family's builder must close over
  only cache-key inputs; audit the closures first.
- **Proof:** `agent_cache outcome=hit` ratio per session in dev logs for a
  spreadsheet-tool session goes from 0 to (turns−1)/turns; CountTokens call count
  per turn (CloudTrail or a counter on the count client) drops to ≤1.

**P1-D  Make missing uploads-bucket CORS a synth-time error** (fix 4)
- In `validateConfig`, throw when file uploads are provisioned and
  `buildCorsOrigins(config)` is empty, instead of `console.warn`. Add a jest case.
- Confirm dev/prod buckets carry the rule (`aws s3api get-bucket-cors`) — they
  should, since uploads work there today.
- **Proof:** `npx jest` + a deliberate synth without `CDK_DOMAIN_NAME` fails.

### Phase 2 — Behaviour and quota changes (next sprint)

**P2-E  Attached spreadsheets are readable on the default tool set** (fix 5)
- On a turn whose attachments include a tabular file, inject
  `list_spreadsheets` + `analyze_spreadsheet` for that session when the caller's
  effective RBAC grants them, mirroring `_document_tools_gate` (same function feeds
  the builder and the cache key). Update the "HANDLING MISSING TOOLS" prompt clause
  so the model stops sending users to the sidebar for a file they just attached.
- Consider a small-CSV inline path (e.g. ≤ 64 KB as `document_read` format `csv`)
  so a tiny file never needs the sandbox at all.
- **Proof:** SPA e2e: attach a CSV with default tools, ask for a total, get a
  number. `C#` row shows a single `toolConfigHash` change on the attach turn only.

**P2-F  Halve conversation-memory lookups per message; then raise the quota** (R1, R2)
- Long-term retrieval runs 3 `RetrieveMemoryRecords` on *every* user message. Move
  preferences/facts retrieval to once per session (cache on the session manager,
  refresh on a timer) and drop summary-namespace retrieval from `append_message`
  entirely — checkpoint advance already calls `_retrieve_session_summaries` where
  it is needed. Target ≤ 1 retrieval per message, ≤ 3 lookups per cold restore.
- P1-C removes the per-turn 5 `ListEvents` for the rebuilt-agent cohort.
- Only then file Service Quotas: `RetrieveMemoryRecords` 30 → 300/s (adjustable);
  check `ListEvents` (200/s account, **20/s per actor-session is fixed** — a
  single busy session cannot exceed it, so per-session lookups matter as much as
  the account total).
- **Proof:** CloudWatch `Bedrock-AgentCore` throttle metrics by API during a
  `locustfile_classroom.py` run; before/after lookups-per-message from CloudTrail.

**P2-G  Cold start** (R3)
- No code lever. Open the AWS support thread on Runtime warm capacity with our
  measured turn-1 breakdown (P1-A gives the after-number). Track separately in
  the `agent_status` cycle-0 timing so we can tell AWS boot from our import time.

### Phase 3 — Reproduce the A/B on our harness and keep it

- Run `tests/load/locustfile_classroom.py` against dev at 10 / 17 msg/s, 8-minute
  holds, before and after Phase 1, with `scripts/load-test/watch-tpm.sh`. Record
  new-chat first-token p95, Bedrock throttles, and success rate the way §2 of the
  report does.
- Make the 17 msg/s run a release-gate check (manual, never CI — the provisioning
  scripts mutate the shared user pool by design).

---

## 5. Commands referenced

Empty-messages CountTokens probe (needs a fresh `aws sso login --profile dev-ai`):

```bash
AWS_PROFILE=dev-ai AWS_REGION=us-west-2 backend/.venv/bin/python - <<'EOF'
import boto3, botocore
c = boto3.client("bedrock-runtime", region_name="us-west-2",
                 config=botocore.config.Config(retries={"max_attempts": 1}))
mid = "anthropic.claude-haiku-4-5-20251001-v1:0"
sys_ = [{"text": "You are a helpful assistant."}]
for label, inp in [
    ("empty messages", {"converse": {"system": sys_, "messages": []}}),
    ("probe user msg", {"converse": {"system": sys_, "messages": [{"role": "user", "content": [{"text": "hi"}]}]}}),
]:
    try:
        print(label, "OK", c.count_tokens(modelId=mid, input=inp)["inputTokens"])
    except Exception as e:
        print(label, "ERR", type(e).__name__, str(e)[:140])
EOF
```

Confirm a deployed uploads bucket has CORS:

```bash
AWS_PROFILE=dev-ai aws s3api get-bucket-cors --bucket dev-boisestateai-v2-user-file-uploads-490617140655
```
