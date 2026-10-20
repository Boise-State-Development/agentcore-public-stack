# AgentCore Runtime V2 migration

**Status:** plan steps 1–3 shipped in 1.27.0. The first dev V2 attempt (2026-10-09) failed on an environment-variable size limit (B4) and was rolled back to V1. Step 4, PR A (#1533) and PR B (#1535), is merged to `develop`, and **dev has run V2 since 2026-10-10 19:10Z** (step 5). V2 must not be selected on any release before PR B ships (§8). **The first GB-hours readout (2026-10-16, §9) is "need more data" for prod:** on dev, V2 used about as many GB-hours per day as V1 while carrying more traffic, and Runtime spend roughly doubled. **Prod has run V2 since 2026-10-16 19:25Z** (1.28.0), and the prod readout (2026-10-20, §10) gives its V1 baseline — **1.179 GB-hours per session** — and reads prod's first V2 days as cost-neutral to modestly cheaper, not the near-doubling dev saw. Tracks `docs/kaizen/review-queue.md ▸ [2026-09-25] A/B the V2 AgentCore Runtime in dev`.
**Sources:**
- The AWS ML blog post [The new AgentCore Runtime: elastic, optimized, and consistently fast starts](https://aws.amazon.com/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/) (2026-09-18).
- The [What's New post](https://aws.amazon.com/about-aws/whats-new/2026/09/new-agentcore-runtime-generally-available).
- The CloudFormation registry schema for `AWS::BedrockAgentCore::Runtime` (us-west-2), read on 2026-09-27.
- botocore's `bedrock-agentcore-control` service model on `develop`.
- A read-only `GetAgentRuntime` against the dev runtime.
- The failed dev V2 update on 2026-10-09 (§3 B4), and read-only `GetAgentRuntime` calls against the dev and prod runtimes that day.
- The AgentCore pricing page and the Price List API, read on 2026-10-16. Read-only Cost Explorer, CloudWatch and Logs Insights queries against the dev account the same day (§9).
- Read-only Cost Explorer, CloudWatch and Logs Insights queries against the **prod** account on 2026-10-20 (§10), covering 2026-09-29 → 2026-10-20.

Labels used in this document:
- **Verified**: checked against AWS or our tree on 2026-09-27.
- **Asserted**: AWS says it, and we have not reproduced it.
- **Hypothesis**: a risk we inferred, not observed.

---

## 1. What V2 changes (asserted by AWS)

| | V1 (what we run today) | V2 |
|---|---|---|
| Opt-in | none | `platformVersion: "V2"` on Create/UpdateAgentRuntime |
| How an instance starts | pulls the image and boots the container per session | **loads the agent once, snapshots the running environment, and restores that snapshot for every new instance**. Excess transient state is stripped so snapshot size stays steady |
| Cold start (P75, echo agent) | rises with image size: 5.4 s at 200 MB, ~30 s at 2 GB | **~2 s flat** from 200 MB to 2 GB. Image size stops mattering |
| Memory | allocated memory is held, so usage tracks the **high watermark** | paged in on demand. **Freed or cold memory is reclaimed** |
| Billing | GB-hours at peak | **"a higher rate but on far fewer GB-hours"**, following the memory actually used. There is no standing charge for provisioned capacity |
| Architecture | ARM64 | ARM64 only. x86 is "coming soon" |
| Regions (What's New) | — | us-east-1, us-east-2, **us-west-2**, eu-west-1, ap-northeast-1 |

The blog lists these as **coming soon**:
- Suspend and resume sessions with memory snapshotting.
- Serialize state before an active session terminates, "so sessions can resume indefinitely".
- Per-session scoped identity ("session context keys").
- Larger RAM, vCPU and session storage.
- A committed-baseline discount: reserve a memory floor and burst above it on demand.

**The V2 price (verified 2026-10-16, us-west-2).** The [AgentCore pricing page](https://aws.amazon.com/bedrock/agentcore/pricing/), the Price List API (`--service-code AmazonBedrockAgentCore`, effective date 2026-10-06) and the rates actually billed on dev all give the same numbers. V2 bills under its own usage types, `USW2-Runtime:Consumption-based:Memory-v2` and `…:vCPU-v2`, so Cost Explorer can tell the two versions apart.

| | V1 | V2 consumption | V2 committed baseline ("launching by October 2026") |
|---|---|---|---|
| Memory | $0.00945 / GB-hour | **$0.0169 / GB-hour** (1.79× V1) | $0.0132 / GB-hour (1.40×) |
| vCPU | $0.0895 / vCPU-hour | **$0.1276 / vCPU-hour** (1.43× V1) | $0.0997 / vCPU-hour (1.11×) |
| V2 is cheaper only if its usage falls below | — | 56% of V1's GB-hours, 70% of V1's vCPU-hours | 72% of V1's GB-hours, 90% of V1's vCPU-hours |

The pricing page's V2 example says memory "is reclaimed within the session when unused for 120 seconds", and CPU is billed only while it is actively processing, as on V1. The first dev readout against these rates is in §9.

**What AWS does not say:**
- Nothing about the `/ping`/`/invocations` contract or `lifecycleConfiguration`.
- **When** in the container's startup the snapshot is taken.

That last gap drives most of the risk in §3.

## 2. Gate 1: will CloudFormation take it? Answered: yes (verified)

- **The CFN schema has `PlatformVersion`.** It is a string of 1–128 non-whitespace characters with no enum.
- **It is not `createOnlyProperties`.** Only `AgentRuntimeName` is. Flipping the version is an **in-place update**: the runtime ID and ARN stay the same, so SSM `/<prefix>/inference-api/runtime-id` does not rotate.
- `aws-cdk-lib` 2.270.0 still has no typed prop, so the change is `runtime.addPropertyOverride('PlatformVersion', ...)`.
- **The dev runtime reports `platformVersion: 'V1'` today**, with `lifecycleConfiguration` `{idleRuntimeSessionTimeout: 900, maxLifetime: 28800}` (the defaults; our construct sets none). So the V1 literal is `"V1"`, which the rollback plan below depends on.
- **Our SDKs are too old to see the field:**
  - botocore `develop` has `platformVersion` on `CreateAgentRuntimeRequest`, `UpdateAgentRuntimeRequest` and `GetAgentRuntimeResponse`.
  - Our pin (`boto3==1.43.68`) does not, and neither does a local `aws-cli` 2.22.12. Latest botocore on PyPI is 1.43.103.
  - The dev read above was done by pointing `AWS_DATA_PATH` at the upstream model, not by bumping anything.

## 3. Blockers and risks found in our tree

### B1. `backend.yml` would strip V2 on every image deploy (fixed in the deploy script, and verified on a live image roll)

`scripts/build/deploy-runtime-image-if-changed.sh` rolls a new image with `update-agent-runtime`, which is a **full-replacement** API. It rebuilds the payload from `get-agent-runtime` through an `ALLOWED` allow-list, and **`platformVersion` is not on it**. `capacityProviderConfiguration` is not on it either.

What happens next depends on the runner's CLI:
- **CLI knows the field.** `get` returns `V2`, the allow-list drops it, and `update` omits it. Omitted probably means "service default" (V1) on a full-replace API, but that is **unverified**. If so, every backend deploy reverts the runtime to V1 and the next `platform.yml` sets it back. The runtime flaps, and both arms of the A/B are contaminated without anything failing.
- **CLI too old.** `get` never parses the field, and the result is the same.

This is the same class of problem as the image-tag-in-SSM convention in CLAUDE.md: a field CFN owns that the out-of-band deploy must not revert.

**Fix (landed, plan step 1):**
1. The payload now carries every field the CLI's own `update-agent-runtime --generate-cli-skeleton input` lists (minus `clientToken`), instead of a fixed `ALLOWED` set. That picks up `platformVersion` and `capacityProviderConfiguration`, and any field AWS adds to the update API later.
2. After the update, the script re-reads `platformVersion` and fails the job (exit 8) if it differs from the pre-update value.
3. Before the update, it fails (exit 7) if the CLI's update skeleton has no `platformVersion`. The skeleton is generated locally from the CLI's model, so this needs no pager, network or credentials. Silently dropping the field is the failure mode.

AWS CLI 2.36.46 is the first release whose `bedrock-agentcore-control` model has `platformVersion` (2.36.45 does not; checked 2026-10-08). The `ubuntu-24.04` runner image (20260927) ships 2.37.4. A URI-unchanged run skips the update and both checks, since it changes nothing.

**Verified on dev, 2026-10-08:** the `backend.yml` run for #1515 rolled a new image and logged `Current platform version: V1` … `Platform version unchanged: V1`. **Omission keeps the current version:** the CFN rollback on 2026-10-09 sent the old template, which has no `PlatformVersion`, and the runtime stayed on V2 (B4). So the "flapping" failure above would not have happened in that form; the fix is still worth keeping, because it makes the payload carry every field rather than relying on that service behaviour.

### B2. Snapshot-restore clones process-start state (hypothesis; verify in dev)

Under V2, anything computed at import or lifespan time is computed **once per snapshot** and then restored into every session's microVM, possibly hours later. Specific cases in our tree:

1. **Idle-reaper origin: `apis/inference_api/runtime_health.py`.** The module-level `tracker = RuntimeActivityTracker()` stamps `_status_since = time.time()` at import. That is deliberate on V1: a microVM that boots and never gets a turn is reaped 900 s after boot.
   - On a restored V2 instance the stamp is the **snapshot time**. Until the first request enters the middleware, `/ping` reports `Healthy` with a `time_of_last_update` that can be far more than 900 s old. That makes the new microVM immediately eligible for reaping.
   - Whether that bites depends on whether the platform polls `/ping` (and acts on it) before routing the first invocation. That is unknown.
   - Symptom to look for: 424s or doubled cold starts on first turns.
   - **Fixed (plan step 3):** the first `/ping` after more than `RESTORE_GAP_SECONDS` (60 s) without one restarts the idle clock, at most once per process, and logs `No /ping for Ns; treating this as a snapshot restore`. The gap is measured from the last poll, or from import if there was none, so it covers a snapshot taken before or after polling began. "Origin at the first `/ping`" alone was rejected: a snapshot taken after polling starts carries that stamp too. The once-only limit is what keeps it safe on V1: if the platform's polling were ever irregular, an unbounded rule would re-arm the immortal-microVM bug, while this costs at most one extra idle period.
   - In the dev A/B, that log line on a fresh session is the evidence that V2 restores happen after polling, and how stale the snapshot was.
2. **Warm-up (`apis/inference_api/warmup.py`) must finish before `/ping` reports healthy. Resolved by AWS's documentation and measured on dev.**
   - AWS: "AgentCore Runtime takes the snapshot on the first healthy `/ping` response. Report health from `/ping` only after initialization completes" (devguide, *Platform versions* ▸ *What to expect*; and *Optimize your agent for AgentCore Runtime V2*). The blog post says the same: the Runtime "waits for it to report healthy, then captures a snapshot of the running environment."
   - Until this fix, warm-up ran on a daemon thread so `/ping` answered at once, a V1 choice. On dev's first V2 snapshot (2026-10-10) the last pre-snapshot `/ping` was about 0.9 s before warm-up logged `complete`. New sessions then redid the rest on their first turn: first-turn preludes of 1.7–3.4 s on about one new session in five, against about 1 s otherwise.
   - **Fix:** `warm_before_ready()` is awaited in the lifespan before it yields. uvicorn binds its socket only after startup returns (verified on the pinned 0.46.0: the port accepts 0.03 s after startup ends), so no `/ping` and no snapshot can precede warm-up. Bounded by `WARMUP_READY_TIMEOUT_SECONDS` (60 s), inside the Runtime's 120-second health deadline. On V1 the change is invisible: AgentCore pre-boots containers ahead of sessions (see B3), and a request reaching a container mid-boot waited on the same imports anyway.
3. **Cloned sockets and credentials.** AWS's guidance reverses the caution below: "Construct and exercise your clients at startup, and expect the first call after a restore to re-establish the connection transparently"; the snapshot keeps the service-model parse, endpoint and credential resolution and the pool, and only the socket is re-established. So a warm-up *call* is fine on V2, and the strategy-id read already makes one. Credentials must still be refreshable, which boto3's container provider is. Original note: warm-up builds boto3 clients but makes no calls, so no pooled TCP connections should be in the snapshot today.
   - Keep it that way: anything that opens a connection at init restores a dead socket into every session. That is the stale-connection class behind #1338 (the LTM retrieval fix).
   - Add a comment in `warmup.py` so nobody "optimises" it into a real call.
4. **Cloned randomness and IDs.** Python's `random` module state and any ID generated at init are identical across every restored instance.
   - `uuid4`, `secrets` and `os.urandom` read the kernel RNG, which is fine only if the platform reseeds on restore (unverified).
   - Worth a grep of the inference-api import path for init-time `random` and ID generation. Today it only turns up in app-api modules, which don't run here.
   - Check the **OTEL resource**: if ADOT (`opentelemetry-instrument` in `Dockerfile.inference-api`) derives `service.instance.id` at SDK init, every session reports the same instance.
5. **Monotonic clocks across a restore.** Any TTL cache keyed on `time.monotonic()` that is populated at init could be read as fresh after restore. That includes the 60 s catalog cache, `oauth_token_cache` and the agent cache. All of them are empty at init today, so this is only a rule to keep: don't pre-populate time-bounded caches at startup.

### B3. The plan's latency metric can't see what V2 improves (verified), and V1 hides its cold starts behind a pool

**Measured on dev, 2026-10-10.** Comparing first-turn first-token times between V1 and V2 smoke runs is misleading, because on V1 most new sessions never cold-start:
- Over the previous week on V1, 153 of the 178 microVMs that served a new session had been booted a median of 498 s before their first request; the deploy itself pre-boots a batch (all six VMs of a V1 smoke run started within the same second, 10 s after the version went READY, and waited 111–146 s). Only the sessions that miss that pool pay V1's real cold start.
- On V2 every new session restored a snapshot on demand: all 12 VMs in two passes logged the restore 0.6–1.0 s before their first request (outliers 1.7–4.7 s, the B2.2 race above).
- So V2's gain is the **pool-miss case** (V1's 5.4–30 s P75 by image size per AWS) and the cost of keeping a pool; on the pooled common case, V1 and V2 should be close once B2.2 is fixed. Measure V2 against V1 pool misses, not against V1's pooled sessions.
- AWS's ~2 s P75 is platform start only (an echo agent whose code ran in 34 ms); our first turn adds a prelude of about 1 s and the model's own time to first token.

**The metric.** The review proposed comparing the turn-latency EMF (#1184). `turn_timing.py` states that its clock starts at handler entry, so it "excludes the app-api hop and any Runtime cold start". The ~1.5 s cold routing gap and V2's snapshot restore both happen before that clock starts.

- `PreludeTotalMs` will only move if B2.2 changes where warm-up lands.
- The cold-start comparison has to come from the client side: `tests/load`, or the SPA-observed send→first-delta gap.
- Our image is ~183 MB compressed in ECR. The blog doesn't say whether its curve is by compressed or uncompressed size, so read our V1 baseline off our own measurement (cold 6.7 s vs warm 3.75 s prelude), not off the blog's chart.

### B4. V2 limits the runtime's environment variables to 2,560 bytes (verified, the hard way)

The first dev V2 attempt failed. CloudFormation's update of the Runtime returned:

> The environment variable payload is 3007 bytes, exceeding the 2560-byte maximum supported for V2 agents.

- **Neither environment fits.** Dev has 49 variables, 2,938 bytes by our count. Prod has 45 variables, 2,579 bytes; it is over before AWS's overhead is added.
- **Our count is not AWS's.** For dev, the sum of key and value lengths is 2,938 against AWS's 3,007. No simple formula reproduces 3,007: `k=v` per variable gives 2,987, and compact JSON gives 3,233. Budget against the limit with a margin (§7.5), not against our sum.
- **The limit was not in the AgentCore docs** we searched on 2026-10-09, or in the V2 blog or What's New post. V1 has no comparable limit; the 49-variable dev payload ran on V1 the whole time.
- **The CFN rollback failed too.** The rollback sends the previous template, which has no `PlatformVersion`. The runtime stayed on V2 (see B1), so the rollback hit the same limit and the stack ended in `UPDATE_ROLLBACK_FAILED`. Every platform deploy, and every inference-api image roll (the deploy script refuses a runtime that isn't `READY`), was blocked until a manual recovery. Chat kept working: the `DEFAULT` endpoint kept serving the last `READY` version.
- **Most of the payload is redundant.** About 30 of the 49 values are the stack prefix plus a fixed suffix, and the code could derive them. §7 is the refactor; it would take dev to about 19 variables and 755 bytes, and also clears the separate 50-variable ceiling dev is one variable short of.

#### Recovery runbook (used on dev, 2026-10-09)

If a Runtime update leaves the stack in `UPDATE_ROLLBACK_FAILED`:

1. Set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION` back to `V1` (or the last working value) in the GitHub environment, so the next platform deploy doesn't repeat the failure.
2. Restore the runtime directly. Read the last `READY` version with `get-agent-runtime --agent-runtime-version <n>`, keep the fields `UpdateAgentRuntime` accepts, set `platformVersion` to `V1`, and call `update-agent-runtime`. Wait for the runtime and the `DEFAULT` endpoint to report `READY`. With a pre-2.36.46 local CLI, point `AWS_DATA_PATH` at a current `bedrock-agentcore-control` model.
3. `aws cloudformation continue-update-rollback --resources-to-skip <runtime logical id>`. Skip the runtime, because step 2 already put it in a good state; without the skip, the rollback rolls the image back to the one the previous deploy resolved.
4. Re-run the failed Platform Stack deploy. It writes the explicit `PlatformVersion` and the current image, and brings CloudFormation's record back in line with the runtime.

On dev this took about ten minutes, and the stack ended `UPDATE_COMPLETE` with the runtime on V1, at the current image.

## 4. Cost model

The billing basis inverts, so conclusions from V1 don't carry over:

- **Idle sessions get cheap.** On V1 an idle microVM bills its peak for the full 900 s `idleRuntimeSessionTimeout`. On V2 that memory is reclaimed.
  - The trade-off behind `runtime_health.py` shifts: it reaps idle VMs aggressively because idle time was expensive.
  - With ~2 s restores, a *shorter* idle timeout is cheaper to live with.
  - With reclaimed idle memory, a *longer* one costs little and saves restores.
  - Leave the timeout at 900 s for the A/B and decide from data.
- **In-process caches now carry a latency cost.** The agent cache, catalog cache and MCP tool listings were free on V1 as long as they sat under the peak.
  - On V2, a cache entry that goes cold is reclaimed and **paged back in on its next hit**. That is a TTFT cost on exactly the path the cache exists to speed up.
  - The reclaim window isn't published. Watch agent-cache-hit turns for a latency regression that looks like a miss.
- **The rate is now known (§1): memory 1.79× and vCPU 1.43× V1.** So V2 wins on memory only below ~56% of V1's GB-hours for the same work. V2 does bill under distinct usage types (`Memory-v2`, `vCPU-v2`). Even so, dev can only be compared before and after, because the runtime is on one version at a time. The first readout is in §9.
- **W5 instance-based SKUs.** Hold that arithmetic until the committed-baseline pricing lands. That is the V2-native answer to the same question, and V1 crossover math would compare against the wrong basis.

## 5. Plan

1. **Deploy script PR (B1). Done.** Payload fields derived from the CLI's update skeleton, the post-update equality assertion, and the CLI capability check. Harmless on V1.
2. **Infra PR (per-environment version). Done.**
   - `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION` → `config.inferenceApi.runtimePlatformVersion`, `V1` or `V2`, default `V1` (`""` falls through to it). A version value rather than a `*_V2_ENABLED` boolean, so a later version is a one-line addition to `AGENTCORE_RUNTIME_PLATFORM_VERSIONS`, not a second flag. Anything else fails synth, because the CFN schema would take any non-blank string.
   - **Always** set the property explicitly: `addPropertyOverride('PlatformVersion', version)`, V1 included. If we omit it for V1, whether removing the property reverts the runtime is up to CFN. An explicit `V1` makes rollback a deterministic in-place update.
   - Synth test for both values (`infrastructure/test/runtime-platform-version.test.ts`), and `platform.yml` forwards the variable.
   - This is infra-only, so there is no backend `feature_flags.py` or SPA flag.
   - `aws-cdk-lib` 2.272.0 types `platformVersion`; swap the override for the typed property when CDK is next bumped.
3. **Runtime-health hardening (B2.1). Done.** Restore-safe idle clock (see B2.1), with tests for a restore before and after the first poll, the once-only limit, and a restored microVM still being reaped. Harmless on V1.
4. **Environment-variable refactor (B4). In progress.** See §7. Two PRs: **PR A** derives the names at process start and adds the synth-time payload guard and the manifest test (no behaviour change while the variables are still set); **PR B** drops the variables from the Runtime. Prod needs this too, because it is over the limit on its own. Release sequencing and what each release's notes must say are in §8.
5. **Dev A/B.** Set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` in the `development` environment.
   - First tried 2026-10-09, before step 4, and rolled back (B4). Retry only once the payload guard (§7.5) passes for dev's real values.
   - Verify with `get-agent-runtime` after `platform.yml`, **and again after the next `backend.yml`**. The second check is B1's real test.
   - Watch for 424s on first turns (B2.1).
   - Measure the client-side cold first-token gap with `tests/load` (B3), `PreludeTotalMs` split by cold vs warm, agent-cache-hit turn latency (§4), and Runtime GB-hours from the Cost Explorer sync.
6. **Decide warm-up placement (B2.2).** Only after step 5 shows whether warm-up work lands in the snapshot.
7. **Prod.** Only after a clean dev week, with the V2 rate known, and with step 4 deployed to prod. The rate is now known (§1). The first week's readout (§9) does not yet support V2 on cost, so the prod decision rests on latency, with the cost premium stated.

## 5a. Phase 2: prewarm the session when the user engages (after the dev A/B)

**Status (2026-10-10): built, behind `SESSION_PREWARM_ENABLED` / `features.sessionPrewarm` (in development, default off).** The trigger depends on how strong the page's intent to send is (decided with the developer driving this, 2026-10-10):

- **A new conversation warms on page load, while the tab is visible.** Opening it is almost always intent to send, the composer takes focus as it loads, and it is where paste-then-send and prompt-starter sends happen, which a keystroke trigger would miss or warm too late (a warm needs ~1.5–2 s, the session start it hides). A background tab warms when the user first looks at it, and again on return after 10 minutes.
- **An existing conversation warms on the composer's first input (typing or paste).** Opening one is often just reading, and a reply takes long enough to type that the warm still finishes first. The page arms the conversation on load; the composer reports input; nothing else warms it. A conversation this tab just created, or any conversation with a turn in the last 10 minutes, is skipped: a real turn already started its microVM.
- Each conversation warms at most once per 10 minutes (SPA and app-api), and app-api caps a user at 10 warms a minute.

What was built:
- **Runtime:** `InvocationRequest.warm`. The `/invocations` handler returns `{"warmed": true}` as its first statement, before the ownership read: no rows, no lease, no quota, no model. No flag and no environment variable on the Runtime, so the V2 byte budget is untouched.
- **app-api:** `POST /chat/prewarm` (`chat/proxy_routes.py`), cookie auth. Answers 202 immediately with `status` (`accepted`, `recently_warmed`, `rate_limited`, or `skipped` when runtime-session affinity is off) and forwards in a background task with the same affinity header the first turn will carry. It logs `prewarm forwarded -> <status> in <ms>`, which is the start time a user no longer waits for.
- **SPA:** `SessionPrewarmService`. A new conversation's id is minted when the page warms it and claimed by the first send (`chat-request.service.ts`), so the send registers the conversation exactly as before and lands on the warmed microVM. An attach-first conversation adopts the same id for its staged uploads. Existing conversations are armed by the session page and warmed by `ChatInputComponent.onTextareaInput`; composers on other pages (agent preview, marketplace review) arm nothing and never warm.

Measure it client-side, as below: first token on a new conversation's first turn, with the page open a few seconds before sending, flag on against off.


The blog's tip is to start the session as soon as the user engages, for example when they open a chat or begin typing, instead of waiting for submit. That hides the start time behind the time they spend typing. **V2 does not do this for us.** A microVM starts only when an invocation arrives with a runtime session ID. We pin that ID per conversation (`runtime_session_id_for` in `apis/shared/harness/runner.py`, a hash of the user and the conversation's session ID). So today **every new conversation's first turn is a cold start**, and nothing happens before the user sends.

**Why it waits for V2.** A prewarm for a chat the user never sends leaves a microVM idle for `idleRuntimeSessionTimeout` (900 s).
- On V1 that idle time bills at peak memory. Prewarming every composer focus would be a real cost.
- On V2, idle memory is reclaimed. Build this only once the A/B (step 5) shows what an idle V2 session actually costs.

**Most of the pieces already exist (verified in our tree):**
- **The ID exists before the first send.** The SPA already mints the conversation ID client-side for file attachments before the first message (`stagedSessionId` in `session/session.page.ts`, `onFileAttached`). Prewarming would stage the same ID when the user shows intent.
- **The warm call lands on the right microVM.** The app-api proxy already reads `session_id` from the body and applies the affinity header (`apis/app_api/chat/proxy_routes.py`). A warm call that carries the staged ID restores exactly the microVM the first real turn will use.

**What to build:**
1. **A no-op warm action on `/invocations`.** It cannot be a new inference-api route, because the runtime only proxies `/invocations` and `/ping` (see the Inference API boundary in CLAUDE.md). The action must:
   - **not** take the single-flight lease, or the real first send collides with it (409, or the proxy's 424-while-lease-held path);
   - create no session or metadata rows;
   - charge no quota;
   - make no model call;
   - pass through `InvocationActivityMiddleware`, so the idle clock starts from the warm call.
2. **An app-api endpoint** (cookie auth, `get_current_user_from_session`) that forwards the warm action with the affinity header.
   - Rate-limit it per user, and warm each staged ID at most once.
   - Fire it on **intent**, meaning composer focus or the first keystroke. Never fire it on page load, since that would pay for every tab left open.
3. **SPA:**
   - Stage the ID and fire the warm call on intent.
   - Make it fire-and-forget: a failure or a slow warm call must never delay or block the send.
   - A send that arrives mid-warm simply lands on the same microVM while it restores, which is no worse than today.
4. **Optional:** warm on opening an existing conversation whose last activity is older than the idle timeout. That microVM has been reaped, so its next turn is also cold.

**What it hides.** On today's cold path it hides:
- the Runtime start: ~2 s on V2 per AWS, before our handler is entered;
- the in-container import warm-up (`warmup.py`, the 3.6 s residual in `load-test-assessment-2026-09.md`);
- part of the 6.7 s vs 3.75 s cold/warm prelude gap.

A later step could also pre-build the agent for the currently selected model and tools, about 1.5 s on an agent-cache miss. Keep that separate: the selections can change before send, and a wasted build costs CPU on a microVM the user may never use.

**TTFT.** This adds nothing to the send path. It moves work that is already there to before the send. Measure it client-side, from first keystroke to first delta on turn 1 (B3), not with `PreludeTotalMs`.

**Flag.** In development, so it defaults OFF, with a backend flag (`SESSION_PREWARM_ENABLED`) and a matching SPA flag (`features.sessionPrewarm`), per the Feature Flags section of CLAUDE.md. The backend is the gate: while the flag is off, the endpoint 404s and the SPA never calls it.

**Interaction with B2.1.** A prewarm puts a real request right after restore, which narrows the stale-idle-clock window. It does not replace the fix, because unwarmed paths (warming off, or a failed warm call) still restore cold.

## 6. Open questions for AWS or the docs

- When is the snapshot taken: after the first healthy `/ping`, or after some quiescence? Does restore reseed the RNG and step the monotonic clock?
- ~~Does a full-replace `UpdateAgentRuntime` that omits `platformVersion` reset it to V1, or leave it unchanged?~~ It leaves it unchanged (B1, B4).
- How does V2 count the 2,560 bytes? Our key-plus-value sum is 69 bytes under AWS's figure for dev (B4). Is the limit a fixed contract or an adjustable quota?
- ~~Does V2 bill under a distinct Cost Explorer usage type, and at what rate?~~ Yes: `Runtime:Consumption-based:Memory-v2` and `…:vCPU-v2`, at $0.0169/GB-hour and $0.1276/vCPU-hour (§1).
- What is the reclaim window for cold memory, and what does a page-in cost?
- What does an idle V2 session cost for 900 s after a single no-op invocation? This decides whether §5a pays for itself.

## 7. Environment-variable refactor (B4)

**Goal:** the Runtime's environment fits V2's 2,560-byte limit with room to grow, whatever a deployment's prefix and domain are, without disrupting the deployments that build on this repository. The same change takes the variable count well under the 50-variable ceiling.

### 7.1 Inventory (dev, 2026-10-09)

| Kind | Count | Examples | Treatment |
|---|---|---|---|
| Prefix plus a fixed suffix (and the account, for three buckets) | 26 | `DYNAMODB_SESSIONS_METADATA_TABLE_NAME` = `{prefix}-sessions-metadata`, `S3_USER_FILES_BUCKET_NAME` = `{prefix}-user-file-uploads-{account}`, `AGENTCORE_RUNTIME_WORKLOAD_NAME` = `{prefix}-platform-workload` | **Derive** (the manifest, §7.2). This is the whole of PR B |
| A transform of a derivable name | 4 | `AUTH_PROVIDER_SECRETS_ARN` (an ARN; the name `{prefix}-auth-provider-secrets` would do), `BROWSER_POLICY_S3` (`s3://{prefix}-browser-policy-{account}/policies/…`), `AGENTCORE_LOCAL_OAUTH_CALLBACK_URL` (`FRONTEND_URL` + `/oauth-complete`), `TOKEN_EXCHANGE_SECRET_ID` (conditional) | **Keep for now.** Each needs its own check (IAM by name, a conditional resource, a URL rule), and together they are ~300 bytes the budget does not need. Candidates for a later PR C |
| Not derivable | 19 | Physical ids with a random suffix (`AGENTCORE_MEMORY_ID`, `AGENTCORE_CODE_INTERPRETER_ID`, `BROWSER_ID`), `FRONTEND_URL`, `CORS_ORIGINS`, `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`, `TOKEN_EXCHANGE_URL`/`_CLIENT_ID`, feature flags, `MEMORY_LINT`, `LOG_LEVEL`, `PROJECT_PREFIX`, `AWS_DEFAULT_REGION`, `AGENTCORE_GATEWAY_INBOUND_AUTH` | Keep |

| | Today | After PR B |
|---|---|---|
| Dev | 49 variables, 2,938 bytes by our sum (3,007 by AWS) | 24 variables, roughly 1,400 bytes |
| Prod | 45 variables, 2,579 bytes | 20 variables, roughly 1,150 bytes |
| Worst-case synth in CI (`test-project` prefix, every conditional block on) | 49 variables, ~2,851 bytes estimated | 24 variables, ~1,280 bytes estimated |

"After" includes one new variable, `AWS_ACCOUNT_ID` (~28 bytes), which the three account-scoped bucket names need. The manifest test (§7.3) found the same `{prefix}-{suffix}` shape for every entry in the synthesized template, so there is no legacy-named resource to special-case.

### 7.2 Mechanism: hydrate the environment at process start, keep the variable as an override

The first draft of this section proposed a resolver function and migrating every read site to it. The inventory made that the wrong trade: there are **~330 reads of these names across ~96 files** (`apis/shared` 55, `app_api` 26, `agents` 14, `inference_api` 1), the read idioms vary (`os.environ.get`, `os.environ[...]`, `os.getenv(..., default)`, module-level constants), and a 96-file diff is exactly the kind of change a fork cannot merge cleanly. What shipped instead (PR A):

- **One manifest**, `backend/src/apis/shared/config/derived_environment.json`: each variable's suffix and whether it carries the account. Nothing else holds the suffixes; CDK's `getResourceName` calls and this file are the two places a name exists, and §7.3 pins them together.
- **`hydrate_derived_environment()`** in `apis/shared/config/runtime_environment.py` runs **once, first thing** in `inference_api/main.py`, before any other `apis.*` import (a test parses the entrypoint's AST to keep it that way, because `bedrock_embeddings` and several repositories read these names at import). For each manifest entry it sets the variable **only if it is absent** (an empty string counts as absent: `.env` files written from `.env.example` leave blank lines, and every read site treats `""` as unset). The derived value is `{PROJECT_PREFIX}-{suffix}`, plus `-{AWS_ACCOUNT_ID}` for the account-scoped buckets. With `PROJECT_PREFIX` unset it does nothing, so tests and bare local runs are untouched.
- **Every read site is unchanged.** They read the same `os.environ` they always did; it just has the names in it. The explicit variable stays an override for local `.env` files, tests, and any deployment that keeps sending it.
- **Drift is reported, not guessed at.** When a variable is set *and* differs from the derived name, startup logs a `WARNING` naming it and keeps the explicit value. While the Runtime still sends every variable (between PR A and PR B), this compares the derivation against every real deployment for free. **app-api runs the comparison only** (`audit_derived_environment()` in its lifespan): ECS has no payload limit, app-api keeps its explicit variables, and writing names into app-api's environment would be a behaviour change for no gain.
- **The account id, before PR B adds `AWS_ACCOUNT_ID`.** `resolve_account_id()` takes `AWS_ACCOUNT_ID` when set, else reads the 12-digit tail off any explicit account-scoped value that already fits the pattern. That is what lets PR A check all 26 names on today's Runtime with no new variable.
- **No turn-path cost.** String formatting at process start, once. Nothing reaches the prompt or `toolConfig`, and under V2 the hydrated environment is simply part of the snapshot.

**Rejected alternatives:**
- **A resolver function plus a read-site migration** (the first draft). Correct but ~96 files of churn for an open-source stack with downstream forks, and it would have had to touch `app_api` as well, which gains nothing from it.
- **Fetch config from SSM or S3 at startup.** A network call on every cold start (or baked into the V2 snapshot, stale), an IAM grant, and a new way to fail at boot, to carry values that are deterministic anyway.
- **Pack the variables into one JSON value.** Saves only the key bytes, about a third, and every reader would then have to parse it.

### 7.3 Keeping CDK and the manifest in step

The names exist twice: CDK creates them with `getResourceName(config, suffix)`, and the hydrator rebuilds them. Two checks keep that from drifting silently:

1. **`infrastructure/test/runtime-derived-environment-manifest.test.ts` reads the manifest** and, for every entry, resolves the Runtime's variable in the synthesized worst-case template to a literal (`Ref` → the resource's literal `TableName`/`BucketName`/`Name`, `Fn::Join` with `AWS::AccountId` → the mock account) and asserts it equals `{prefix}-{suffix}[-{account}]`. Renaming a resource in CDK without the manifest fails CI, as does a manifest entry for a resource CDK names some other way (`getTruncatedResourceName`, a physical id). All 26 entries pass today. It also prints how many bytes the manifest would remove, which is the number PR B is sized by.
2. **The startup drift warning** (§7.2) does the same comparison against every real deployment's live values, on both services, from the first deploy of PR A.

### 7.4 Things to get right

- **Conditional resources stay conditional.** `TOKEN_EXCHANGE_SECRET_ID` is not in the manifest, so a deployment without token exchange cannot acquire a secret name it has no secret for.
- **Absence as a feature gate.** Some read sites treat an unset name as "feature off": `UserRepository` and `UserSettingsRepository` (`_enabled = bool(table_name)`), `NotificationService` (`DYNAMODB_PROJECTS_TABLE_NAME`), the tool-result offloader (`S3_USER_FILES_BUCKET_NAME`). On the Runtime every one of these is set today, and hydration sets them to the same values, so nothing changes there. Where it *could* change is a local run with `PROJECT_PREFIX` set and a name deliberately left blank: that name is now filled in, pointing at the deployment the prefix names. That is the intended reading of "prefix set", and the applied names are logged at INFO so it is visible.
- **The secret by name** (if `AUTH_PROVIDER_SECRETS_ARN` ever moves to the manifest): check that `auth_providers/repository.py` passes the value straight to `GetSecretValue`, and that the Runtime role's statement covers the name as well as the ARN.
- **Don't derive domains.** `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`, `FRONTEND_URL` and `CORS_ORIGINS` follow domain and certificate rules that live in CDK; a manifest test asserts they are not listed.
- **Keep `apis.shared.config` import-light.** The entrypoint imports it before anything else; a test asserts it pulls in no boto3, FastAPI or Strands.

### 7.5 The payload guard: a synth-time error, not a jest number

The first draft proposed a jest assertion. A jest test protects the GitHub workflow (`platform.yml` runs jest before deploy) but not a deployer running `cdk deploy` from a laptop, and the failure it guards against wedges a stack. So the guard is a **CDK aspect**, `RuntimeEnvironmentPayloadGuard` (`infrastructure/lib/constructs/inference-api/runtime-environment-payload-guard.ts`), attached in the inference construct:

- It visits every `AWS::BedrockAgentCore::Runtime`, resolves its `EnvironmentVariables` and estimates the payload: `Σ(len(key) + len(value) + 2)`. A `Ref` to a resource with a literal name resolves to that name; `Fn::Join` is summed; pseudo parameters get their real widths; a `GetAtt` of a generated id is charged 64 bytes and anything else unresolvable 128, and both are named in the message. The estimate **errs high on purpose**: for dev's real values it gives more than AWS's 3,007, and a guard must never say "fits" when AWS would say "too big".
- **`V2` and over 2,560 bytes → `addError`**: `cdk synth` fails, so `cdk deploy` and `platform.yml` stop before anything reaches CloudFormation. The message names the limit, the dev incident, the largest variables, and the two ways out (set `V1`, or shrink the payload).
- **`V1` and over 2,560 → `addWarning`** ("this deployment cannot switch to V2 until it shrinks"), so a V1 deployment that drifts back over the limit is told so on every synth.
- **Over the 2,000-byte budget but under the limit → `addWarning`** on either version. That leaves ~20% under 2,560 for AWS's unexplained overhead and for growth.
- `infrastructure/test/runtime-environment-payload-guard.test.ts` covers the estimator (literals, `Ref` to a named table and bucket, `Fn::Join` with the account, the unresolved fallback, the dev data point) and each verdict on a synthetic runtime, and prints the worst-case PlatformStack's estimate on every run.

The 50-variable ceiling test is unchanged and still applies; it printed 49/50 on the worst-case synth when PR A landed.

### 7.6 Rollout

1. **PR A (this branch): manifest, hydrator, entrypoint wiring, app-api audit, synth-time guard, manifest test.** The Runtime still receives every variable, so behaviour is unchanged; hydration sets nothing and only compares. **Validate on dev:** the inference-api and app-api startup lines read `… 0 drifted`, and a synth of dev's config on V1 prints the "cannot switch to V2" warning with the expected estimate. Harmless on prod.
2. **PR B: drop the 26 manifest variables from the Runtime in CDK and add `AWS_ACCOUNT_ID`.** *Pre-checked 2026-10-09, read-only:* running `audit_derived_environment()` over the live dev and prod Runtime environments found all 26 names matching the derivation and zero drift on both; after PR B dev goes from 49 variables and 2,851 bytes (our sum) to 24 and about 1,150, and prod from 46 and 2,526 to 21 and about 925. PR B also adds the deploy-order preflight (§8.4). Tighten the budget check to a jest assertion for the worst-case synth (about 1,280 estimated bytes after the change), so a V1 deployment can never drift back over the budget unnoticed. This is the change that can break the Runtime, so it lands only after A has run on dev with no drift. **Validate on dev, still on V1:** a new conversation's first turn; the tools that touch the derived resources (files, artifacts, memory spaces, skills, browser, quota); `get-agent-runtime` showing 24 variables; the startup line reading `26 set, 0 drifted`. Rollback is reverting B; the variables come back on the next platform deploy.
3. **Then plan step 5, the dev V2 retry**, with the guard now passing for dev's values.
4. **PR C, optional, later:** the four transforms in §7.1, one at a time, each with its own IAM or conditional check. Not needed for V2.

No rollout switch: PR B's off path would be the very variables it removes, and a revert restores them in one platform deploy.

## 8. Release sequencing and what the notes must say

This stack is deployed by other organisations from its releases, so the change has to be safe to take in any order a deployer takes releases, and each release's notes have to say what is true at that point. The failure mode to prevent is specific: **a deployer reads 1.27.0's notes, sets `V2`, and wedges their stack** (B4). 1.27.0 offered the variable with no mention of the limit, because B4 was found the day after it shipped.

| Release | Code state | What the notes must say |
|---|---|---|
| **1.27.0 (shipped)** | `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION` exists; every deployment is over the V2 limit; nothing stops a V2 flip | Left as published (decided 2026-10-09: no amendments to past release descriptions). The durable warning lives on the docs-site (the Runtime V2 page and the environments table), and the next release's notes repeat it (§8.2) |
| **Next release (carries PR A)** | Names derived at startup (no-op); synth refuses V2 over the limit; manifest test; drift warning | A **Changed** entry and a **Deployment notes** callout: V2 is still not selectable, but selecting it now fails at synth instead of wedging the stack; a startup drift `WARNING` is something to act on before the following release; no infrastructure change beyond a Runtime definition that is byte-identical |
| **The release that carries PR B** | 26 variables gone from the Runtime; `AWS_ACCOUNT_ID` added; payload ~half the limit | A **Changed** entry with a ⚠️ and a **Deployment notes** callout: the Runtime definition changes (an in-place update); a deployment that renamed any of the 26 resources must keep sending that variable (how to tell: the drift warning from the previous release); V2 is now selectable, dev first; link to the runbook |
| **The release after the dev A/B** | V2 proven on dev; `AGENT_BUILD_SHARED_SESSION_ENABLED` and the other V2-gated rollout switches eligible to retire (#1422) | A **Highlights** paragraph on V2 with the measured cold-start and GB-hour numbers, and the recommendation per environment |

### 8.1 1.27.0: no amendment, the docs carry it

Past release descriptions are not amended. A deployer on 1.27.0 who looks up the variable finds the warning on the docs-site Runtime V2 page and in the environments table, and the next release's Deployment notes (§8.2) say it again with the synth guard as the backstop. The docs-site text is the one to keep current.

### 8.2 Copy for the release that carries PR A

CHANGELOG, under **Changed**:

> - **The inference-api derives its resource-name variables from `PROJECT_PREFIX` at startup**, for the AgentCore Runtime V2 migration. Twenty-six Runtime variables (`DYNAMODB_*`, the `S3_*` buckets and index, `AGENTCORE_RUNTIME_WORKLOAD_NAME`) are the stack prefix plus a fixed suffix; `apis/shared/config/derived_environment.json` lists them, and `hydrate_derived_environment()` fills in any that are absent before anything reads the environment. **Nothing changes yet:** CDK still sends every variable, an explicit variable always wins, and with `PROJECT_PREFIX` unset nothing happens. What is new is a startup line on both services comparing the sent names with the derived ones, and a `WARNING` naming any that differ. A CDK test checks every manifest entry against the synthesized template (`docs/specs/agentcore-runtime-v2.md` §7) (#PR-A)
> - **`cdk synth` now refuses a V2 AgentCore Runtime whose environment exceeds V2's 2,560-byte limit**, instead of letting CloudFormation discover it at the Runtime update and leave the stack `UPDATE_ROLLBACK_FAILED` (which is what happened on dev on 2026-10-09). On V1 the same check is a warning once the payload would block a switch to V2. Every deployment through 1.27.0 is over the limit; the next release reduces the payload (`docs/specs/agentcore-runtime-v2.md` §7.5) (#PR-A)

RELEASE_NOTES, in **Deployment notes**:

> 🧭 **AgentCore Runtime V2: still not selectable, now safe to get wrong.** If you are on 1.27.0, do not set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` before upgrading: 1.27.0 has no guard and the failed update leaves the stack `UPDATE_ROLLBACK_FAILED` (recovery runbook on the docs-site Runtime V2 page). V2 limits the Runtime's environment to 2,560 bytes and this release is still over it, but selecting `V2` now fails at `cdk synth` with the reason, rather than wedging the stack. Leave `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION` unset or `V1`. **After deploying, check both services' startup logs** for a line like `inference-api derived environment (prefix=…): 0 set, 26 matched, 0 drifted` and `app-api derived environment …`. A `WARNING` naming a variable means one of your resources is not named `{prefix}-{suffix}`; the following release stops sending those names to the Runtime, so a deployment with drift must either rename the resource or keep sending that variable explicitly. Nothing in the Runtime definition changes in this release.

### 8.3 Copy for the release that carries PR B

CHANGELOG, under **Changed** (with the ⚠️):

> - ⚠️ **The AgentCore Runtime no longer receives 26 resource-name variables; the inference-api derives them from `PROJECT_PREFIX` and a new `AWS_ACCOUNT_ID`.** The Runtime's environment goes from about 2,600–3,000 bytes to about 1,200–1,400, under the V2 Runtime's 2,560-byte limit, and from 45–49 variables to 20–24. The names are the ones CDK has always built with `getResourceName`, and a CDK test holds the two in step. **A deployment that has renamed any of these resources** (the previous release's startup drift `WARNING` names them) must keep sending that variable through a CDK override, or the Runtime will address a table or bucket that does not exist. A CDK deploy updates the Runtime definition in place (`docs/specs/agentcore-runtime-v2.md` §7.6) (#PR-B)

RELEASE_NOTES, in the header callout and **Deployment notes**:

> 🏗️ **A CDK deploy is required.** It rewrites the AgentCore Runtime's environment (26 variables removed, `AWS_ACCOUNT_ID` added), an in-place update that keeps the runtime id. Do it with `platform.yml` before `backend.yml`, as usual.
>
> ✅ **AgentCore Runtime V2 can now be selected.** With the smaller payload, `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` passes the synth check and the Runtime update succeeds. Switch one environment at a time, dev first, and read *Deployment ▸ AgentCore Runtime V2* on the docs site for the prerequisites, the verification steps and the recovery runbook. V2 bills memory at a higher per-GB-hour rate in exchange for not billing idle memory; the spec's §4 says when that is a saving.
>
> ⚠️ **If the previous release's startup log showed a derived-environment `WARNING`**, resolve it before this deploy: the variable it names is one the Runtime will no longer be sent.
>
> 🔀 **Deploy order matters once, and the pipeline enforces it.** The Runtime can only run without the removed variables on an inference-api image from the previous release or later. If the Platform Stack deploy runs while an older image is live (for example when upgrading straight from 1.27.0), it now stops before changing anything and says to run the Backend Deploy first. Do that, then re-run the Platform Stack deploy.

### 8.4 Deploy order across the change (the preflight)

PR B is the first change in this stack where **infrastructure removes something old application code requires**. CloudFormation re-registers the Runtime with whatever image is live (it reads `/<prefix>/inference-api/image-tag` at deploy time), so a platform deploy of PR B over an image older than PR A starts a Runtime with no table names, and every turn fails until `backend.yml` rolls a newer image. Nothing makes the documented platform→backend order hold: both workflows fire on the same push and race for one concurrency slot, and a deployer who skips a release takes PR A and PR B together.

The documented order would be the dangerous one here, so prose is not enough:

- **The image declares what it can do.** PR A adds `LABEL org.agentcore-public-stack.runtime-env-contract="derived-names-v1"` to `backend/Dockerfile.inference-api`.
- **The platform deploy checks the live image.** `scripts/platform/check-runtime-env-contract.sh` runs in `scripts/platform/deploy.sh` after synth and before `cdk deploy`. When the template no longer sends the names, it reads the live image's config from ECR (resolving a multi-arch index to arm64) and fails with "run backend.yml first, then re-run this deploy; nothing has been changed" if the label is missing. Verified against dev's live image (pre-label): it reads the config and refuses.
- **It skips what it can't judge:** a template that still sends the names, a first deploy (no image-tag parameter, or the CDK bootstrap image in the assets repository), and a custom image in another repository (with a note that it must derive the names itself). `SKIP_RUNTIME_ENV_CONTRACT_CHECK=true`, forwarded by `platform.yml`, is the escape hatch for a deploy role without ECR read.
- **Result:** in the backend-first race order everything just works; in the platform-first order the platform run fails safely, the backend deploy lands, and a re-run of the platform deploy succeeds. No outage either way. The preflight is permanent, not a rollout switch: it guards every future upgrade from a pre-PR-A image, and costs two ECR reads per platform deploy.

### 8.5 Rules for the deployer-facing story

- **Never ship a release in which `V2` is selectable and wrong.** From PR A on, that is enforced by the guard rather than by prose.
- **Every release's notes state whether V2 is selectable on that release**, in the Deployment notes, until the dev A/B is done and the recommendation is written.
- **The docs-site page is the durable version** of the warning, the prerequisites and the runbook; the notes link to it rather than repeating the runbook.
- **Name the exact log line** a deployer should look for, because "check for drift" is not actionable and the line was written to be grepped.

## 9. Dev readout: Runtime GB-hours, V2 against V1 (2026-10-16)

**Read this as a sample of about six days on dev, not a verdict.** Dev traffic is a handful of people plus test runs. Its totals are a few dollars a week, and both windows contain test spikes that are a large share of the total.

### 9.1 The numbers

| | V1 week | V2 window | V2 / V1 |
|---|---|---|---|
| Window (UTC) | 10-03 00:00 → 10-10 00:00 (168 h) | 10-10 19:00 → 10-16 19:00 (144 h) | |
| Memory GB-hours | 150.1 | 127.6 | — |
| vCPU-hours | 1.62 | 3.82 | — |
| Runtime sessions (AWS `Sessions`) | 174 | 367 | — |
| Invocations, of which prewarm | 290, 0 | 595, 205 | — |
| **GB-hours per day** | **21.4** | **21.3** | **0.99** |
| vCPU-hours per day | 0.23 | 0.64 | 2.75 |
| **Runtime $ per day** (memory + vCPU) | **$0.22** | **$0.44** | **1.97 (+$0.22/day)** |
| Runtime $ over the window | $1.56 | $2.64 (≈ $3.08 for 7 days) | |

V2's memory is 82% of its Runtime dollars and V1's is 91%. V2's vCPU line went from $0.02 to $0.08 a day.

Per unit of work the picture is different, and it depends on the unit you pick:

| Normalised GB-hours | V1 | V2 | V2 / V1 | Bias |
|---|---|---|---|---|
| per session | 0.86 | 0.35 | **0.40** | Flatters V2. Prewarm adds sessions that hold little memory (9.3) |
| per session, if every warm were a separate session with no turn | 0.86 | 0.79 | 0.91 | The opposite bound |
| per non-warm invocation | 0.52 | 0.33 | **0.63** | Slightly against V2. The numerator includes the warm sessions' memory |
| per vCPU-hour of work | 92.7 | 33.4 | **0.36** | Flatters V2 if V2 spends more CPU per unit of work, which it appears to (below) |

**Against the break-even.** Raw daily GB-hours (0.99) are nowhere near the 0.56 that V2 needs. Normalised, V2 used 0.36–0.63 of V1's GB-hours per unit of work, which straddles 0.56. vCPU went the wrong way: 0.0064 vCPU-hours per invocation against V1's 0.0056 (1.15×, or 1.75× per non-warm invocation), at a rate 1.43× higher. Priced per non-warm invocation, V2 cost **$0.0068 against V1's $0.0054 (1.26×)**. Per session it cost $0.0072 against $0.0090 (0.80×), but that is the measure prewarm flatters most.

So the memory reclaim is real: a V2 session holds about 40% of the GB-hours a V1 session held, which is what the 120-second reclaim predicts against V1's 900 s idle at peak. It is not yet enough to pay for a 1.79× rate on a 1.43× pricier vCPU line.

### 9.2 Method

- **CloudWatch** (`AWS/Bedrock-AgentCore`, hourly `Sum`): `MemoryUsed-GBHours` and `CPUUsed-vCPUHours` on the runtime's `DEFAULT` endpoint, and `Sessions` and `Invocations` for the same runtime. The hourly series sets the window boundaries.
- **Cost Explorer** (daily, by `USAGE_TYPE`, service `Amazon Bedrock AgentCore`) sets the rates and the cross-check. **CloudWatch's daily totals match Cost Explorer's to 0.01 GB-hour on every complete UTC day.** After the flip, the V1 usage types fell to zero. The account's other runtimes were idle, so its Runtime line was this runtime alone, and no proxy was needed. Hourly and resource-level Cost Explorer data are payer opt-ins that are not enabled, which is why the hourly split comes from CloudWatch. Cost Explorer lags by about a day, so its 10-16 row is partial.
- **The flip hour.** Draining V1 microVMs billed about 3.3 GB-hours and 0.01 vCPU-hours under the V1 usage types after 19:00 on 10-10. That usage is subtracted from the V2 window.
- **Prewarm counts** come from Logs Insights on the runtime's log group (`prewarm invocation` lines). There were 205 in the V2 window and none in the V1 week.
- **Dollars** apply the published rates (§1) to the usage. They reproduce Cost Explorer's billed amounts.

### 9.3 Confounders

1. **Prewarm (#1543) is on in dev for the whole V2 window and did not exist in the V1 week.** It sent 205 warm invocations. A warm for a conversation with no live microVM starts a session. Whether that session ever gets a turn is not measured here. That share is the gap between the 0.40 and 0.91 per-session bounds. Prewarm is a §5a feature that V2 makes affordable, so its cost belongs to V2's bill in practice, but it is not like-for-like with V1.
2. **Smoke, load and validation runs land in both windows.**
   - **V2:** the flip evening (10-10 19:00–24:00) carried four smoke passes, 20 fresh one-turn sessions, a 12-pair cold/warm A/B and prewarm validation. That is 31.8 GB-hours and 83 sessions in five hours, a quarter of the V2 window's memory. Without that evening, V2's daily GB-hours fall to 17.1 (0.80 of V1), and the per-unit ratios barely move (0.40 / 0.64 / 0.35). The window also includes the Projects launch QA on 10-15 and the release smoke on 10-16.
   - **V1:** 10-09 (the failed V2 flip, its recovery and two smoke passes) is 57.1 GB-hours, 38% of the week.
3. **More traffic on V2.** Per day it had 2.5× the sessions, 1.6× the non-warm invocations and 2.75× the vCPU-hours. Its GB-hours held flat while traffic rose, which is the reclaim at work. It also means raw totals cannot be compared directly.
4. **V1's pre-booted pool.** On V1, most new sessions land on a microVM booted minutes earlier (B3). Whether that pool time is billed to us, and therefore whether V1's 0.86 GB-hours per session includes it, is not visible from either source.
5. **V2 vCPU per unit of work rose, and we don't know why yet.** Candidates are restore and page-in CPU billed to the session, the warm sessions' own CPU, and a heavier workload (the Projects features and their tools in the V2 window). This decides whether the per-vCPU-hour normalisation (0.36) can be trusted.

### 9.4 First read for prod: **need more data, leaning no-go on cost alone**

- **Cost.** On dev, V2 did not reduce GB-hours. It held them flat while traffic rose about 1.6–2.5×, and the rates turned that into roughly double the Runtime spend. Per unit of work, memory is at or a little under the 56% break-even. The pricier vCPU line takes back the difference, so per real invocation V2 still costs about 1.26× V1. Nothing here says V2 saves money on prod.
- **Latency.** V2's case is the cold start: ~0.7 s restore-to-request on every new session (B3), against V1's pool misses. Prewarm (§5a) cut the median first token by 1.5 s in a 12-pair dev A/B on 2026-10-10, and it depends on V2's cheap idle time. If prod takes V2, it should be decided as a latency purchase with the premium stated, not as a cost saving.
- **The committed baseline** ($0.0132 / $0.0997) moves the memory break-even to 72%. That is within reach of the normalised 0.36–0.63. Re-run this readout against it once it can be bought.
- **Data that would settle it:**
  1. Join warm invocations to sessions in Logs Insights to get the turnless-warm share. That collapses the 0.40–0.91 per-session range.
  2. A second V2 week (10-17 → 10-23) with no flip-day burst.
  3. The V2 vCPU-per-invocation question (9.3 item 5).
  4. ~~Prod's V1 GB-hours per session on real traffic, read-only, as the baseline any prod projection multiplies.~~ **Answered in §10 (2026-10-20): 1.179 GB-hours per session**, 385.0 per day, 0.349 per invocation, over 14 complete V1 days. Prod turned out to be on V2 already (since 2026-10-16 19:25Z), so §10 is the before side of a prod A/B rather than a projection, and prod's own first V2 days put the cost at 0.59–0.99× V1 per unit of work against dev's 1.26×.

## 10. Prod readout: the V1 GB-hours baseline (2026-10-20)

**Read this as a solid V1 baseline and a very early V2 sighting, not a verdict.** The baseline below is 14 complete UTC days of ordinary prod traffic on V1 (2026-10-02 → 2026-10-16), and it is firm: two exact calendar weeks, no session prewarm, no load-test burst, and Cost Explorer and CloudWatch agree to 0.005%. Everything in §10.3 onward rests on **three** V2 days, two of which are a weekend and one of which is anomalous, so it moves the question without closing it.

**Prod is already on V2.** §9.4 wrote item 4 as the baseline "any prod projection multiplies", on the understanding that prod still ran V1. It does not: prod shipped 1.28.0 on `platformVersion: V2` on **2026-10-16 at 19:25Z**, and Cost Explorer confirms it — the V1 usage types bill for the last time on 10-16 and are zero from 10-17. So this is not a projection onto a V1 production estate. It is the **before** side of a real prod A/B whose after side has started, and the decision it informs is whether prod *stays* on V2.

### 10.1 The V1 baseline

Window **2026-10-02 00:00Z → 2026-10-16 00:00Z**, 14 days, `platformVersion: V1` throughout. Memory 5,389.8 GB-hours, vCPU 50.59 vCPU-hours, 4,571 sessions, 15,442 invocations, none of them prewarm.

| | per day | per session | per invocation |
|---|---|---|---|
| **Memory GB-hours** | **385.0** | **1.179** | **0.3490** |
| vCPU-hours | 3.614 | 0.01107 | 0.003276 |
| Runtime $ | $3.96 | $0.01213 | $0.003592 |

Runtime dollars over the window were **$55.46**, of which **91.8% is memory** and 8.2% vCPU — the same split dev's V1 week showed (91%). Annualised at this rate the prod Runtime line is about **$1,450/year**.

The per-session figure is stable, which is what makes it usable as a baseline. Widening the window to 17 days (2026-09-29 → 10-16) moves it from 1.179 to **1.178** GB-hours per session, and no single day in the window departs from the mean by more than 25%:

| | days | GB-h/day | GB-h/session | vCPU-h/session | inv/session |
|---|---|---|---|---|---|
| Window A (baseline, two full weeks) | 14 | 385.0 | 1.179 | 0.01107 | 3.38 |
| Window B (sensitivity) | 17 | 409.7 | 1.178 | 0.01123 | 3.60 |
| Window A, weekdays only | 10 | 470.6 | 1.157 | 0.01101 | 3.26 |
| Window A, weekend only | 4 | 170.9 | 1.353 | 0.01153 | 4.37 |

Prod has a strong weekly cycle that dev does not — a weekday carries 2.8× a weekend day's GB-hours — so the **per-day** figure only means anything over a whole number of weeks. That is why the baseline window is exactly two. The **per-session** figure is nearly immune to it (1.157 weekday against 1.353 weekend), which is the second reason to make it the headline.

### 10.2 Prod against dev: prod sessions are longer, not fatter

| V1, per session | dev (§9) | prod | prod / dev |
|---|---|---|---|
| Memory GB-hours | 0.86 | **1.179** | **1.37×** |
| Invocations | 1.67 | 3.38 | 2.02× |
| Memory GB-hours per invocation | 0.52 | 0.349 | 0.67× |

A prod V1 session holds **37% more GB-hours than a dev one, while costing 33% less per invocation**. The two facts are the same fact: prod sessions take about twice as many turns, and on V1 memory bills at the high watermark for the session's whole life, so a session that lives long enough for three or four turns bills more GB-hours in total and less per turn. Prod is not using a heavier container; it is holding the same container open across more of a conversation.

**This cuts in V2's favour, and it is the main reason dev's readout cannot be transplanted.** V2's saving is the 120-second reclaim of idle memory *within* a session. A longer session with more turns contains more inter-turn idle — think time between a user reading an answer and typing the next prompt — so there is strictly more for the reclaim to take back. Dev's 1.67 invocations per session is close to one-turn-and-done, which is the shape that gives the reclaim the least to work with. On this reasoning prod should do *better* under V2 than dev did, and §10.3 is consistent with that.

### 10.3 Prod's first V2 days (provisional)

Three complete UTC days on V2: 10-17 (Sat), 10-18 (Sun), 10-19 (Mon). Prewarm is on, so invocations are split into warm and non-warm; the warm counts are Logs Insights line counts (§10.5).

| | GB-h | vCPU-h | sessions | invocations | of which prewarm |
|---|---|---|---|---|---|
| 10-17 Sat | 95.26 | 3.146 | 181 | 809 | 171 (21%) |
| 10-18 Sun | 124.41 | 3.594 | 265 | 885 | 235 (27%) |
| 10-19 Mon | 467.21 | 16.048 | 1,033 | 6,461 | 941 (15%) |

Against the V1 baseline, per unit of work:

| V2 / V1 (prod) | all 3 days | weekend only |
|---|---|---|
| **Memory GB-hours per session** | **0.39** | **0.42** |
| Memory GB-hours per non-warm invocation | 0.29 | 0.49 |
| vCPU-hours per non-warm invocation | 1.02 | 1.60 |
| **Runtime $ per non-warm invocation** | **0.59** | **0.99** |
| Runtime $ per session | 0.81 | 0.84 |

Prod's memory reclaim reproduces dev's almost exactly: **0.39–0.42 of V1's GB-hours per session, against dev's 0.40.** The reclaim is the most transferable number in either readout. What differs is where it lands against the rate: because prod's V1 baseline bills 1.179 GB-hours per session rather than 0.86, the same 0.40 multiplier removes more absolute memory, and prod's all-in cost per unit of work comes out at **0.59–0.99× V1 where dev's was 1.26×**.

The vCPU line is the same unresolved worry §9.3 item 5 raises, and prod does not settle it either: per non-warm invocation vCPU is 1.02× on the three days but 1.60× on the weekend pair, and the spread is wider than the signal.

### 10.4 The projection against the break-evens

V2 needs memory below **56%** of V1's GB-hours at consumption rates, or **72%** at the committed baseline. Prod's measured per-session memory ratio, 0.39–0.42, clears both.

Applied to prod's own V1 volume (326.5 sessions and 1,103 invocations a day), holding the work constant and substituting V2's measured per-unit usage:

| Basis | GB-h/day | vCPU-h/day | $/day | vs V1 $3.96 | $/year |
|---|---|---|---|---|---|
| per session, 3 days @ consumption | 151.6 | 5.03 | $3.20 | 0.81× | −$276 |
| per session, 3 days @ committed | 151.6 | 5.03 | $2.50 | 0.63× | −$532 |
| per session, weekend @ consumption | 160.8 | 4.93 | $3.35 | 0.84× | −$224 |
| per non-warm invocation, 3 days @ consumption | 111.3 | 3.69 | $2.35 | 0.59× | −$588 |
| per non-warm invocation, weekend @ consumption | 188.1 | 5.77 | $3.92 | 0.99× | −$17 |

For comparison, the transplant §9.4 item 4 originally asked for — **dev's** ratios on prod's volume — spans much wider and straddles break-even, which is the point of having measured prod instead:

| dev ratio transplanted | $/day @ consumption | vs V1 | $/day @ committed | vs V1 |
|---|---|---|---|---|
| per session 0.40 (vCPU 1.15×) | $3.13 | 0.79× | $2.45 | 0.62× |
| per non-warm invocation 0.63 (vCPU 1.75×) | $4.91 | 1.24× | $3.83 | 0.97× |
| raw daily 0.99 (vCPU 2.75×) | $7.71 | 1.95× | $6.02 | 1.52× |

**Every one of these is a saving of a few hundred dollars a year at most, and the pessimistic transplant is a loss of about the same.** The Runtime line is ~$1,450/year against a prod Bedrock model bill orders of magnitude larger. V2 on prod should be decided on cold-start latency and on prewarm, which it makes affordable; on cost it is close enough to neutral that the measurement error here is comparable to the saving.

### 10.5 Method

Mirrors §9.2, so the two readouts are comparable.

- **Cost Explorer**, daily, grouped by `USAGE_TYPE`, service `Amazon Bedrock AgentCore`, us-east-1 endpoint. Hourly and resource-level Cost Explorer data are payer opt-ins that are not enabled, so the hourly split comes from CloudWatch. Cost Explorer lags about a day: it had no row for 10-20 at all, so the last complete day is 10-19.
- **The rates billed on prod confirm §1 to the cent**: `USW2-Runtime:Consumption-based:Memory` $0.009450/GB-hour, `…:vCPU` $0.089500/vCPU-hour, `…:Memory-v2` $0.016900, `…:vCPU-v2` $0.127590. Dollars in this section apply those rates to the usage and reproduce Cost Explorer's billed amounts to within 0.01%.
- **CloudWatch** (`AWS/Bedrock-AgentCore`, hourly `Sum`): `MemoryUsed-GBHours` and `CPUUsed-vCPUHours` on the runtime, and `Sessions` / `Invocations` on the `Name`+`Operation`+`Resource` dimension set. `ActiveSessionCount` (`Service` dimension, `Maximum`) for the burst check.
- **The cross-check holds.** CloudWatch's daily memory totals match Cost Explorer's on every complete UTC day, worst case 0.39 GB-hour on a 470 GB-hour day (**0.008%**); over the baseline window the two differ by 0.25 GB-hour in 5,390 (0.005%). This is looser than dev's 0.01 GB-hour in absolute terms and tighter in relative terms, which is what 25× the volume predicts. vCPU agrees to 0.01 vCPU-hour.
- **Attribution needs no proxy.** The prod account also runs AgentCore Browser, Code Interpreter, Gateway and Memory, but each bills under its own usage type (`USW2-BrowserTool:…`, `USW2-CodeInterpreter:…`, `USW2-Gateway:…`, `USW2-Memory:…`), so the two `USW2-Runtime:…` types are the Runtime alone. Within the Runtime, the `Service: AgentCore.Runtime` rollup of `MemoryUsed-GBHours` equals the single runtime `boisestateai_v2_agentcore_runtime-h4MSyY7YSh`'s own series exactly — **0.000 GB-hours and 0.000 vCPU-hours unattributed** over the whole 21 days. There are 10 further runtime *log groups* in the account, left behind by deleted runtimes; none of them bills.
- **Prewarm counts** come from Logs Insights on the runtime's log group, matching `prewarm invocation` and excluding the OpenTelemetry JSON duplicate of the same record (every line is emitted twice, once as text and once inside an OTEL envelope, so an unqualified `/prewarm/` filter double-counts). **Zero in the entire V1 window** — prewarm reached prod with 1.28.0 on 10-16, which is the first day it appears (73). This is the one confounder dev had to carry that the prod baseline does not.

### 10.6 Confounders

1. **The V2 side is three days, and one carries a workload the baseline never saw.** 10-19 (Mon) carried 1,033 sessions and 6,461 invocations — 2.2× and 4.3× a V1 Monday — concentrated in 20:00–24:00Z at 140–192 sessions an hour with **8.1–9.6 invocations per session** against a prod norm of 3.4. **This was identified rather than assumed, and it is real traffic.** Logs Insights attributes about 3,900 of that evening's knowledge-base retrievals — the great majority of the turns — to a single public course assistant: a multi-round business-simulation bot on Sonnet 5, played 20:00–24:00Z, which is 14:00–18:00 local. Its design is the explanation for the invocation rate: one conversation is a whole simulation, so 8–10 turns per session is the workload behaving normally, not a loop. It is not a load test (`ActiveSessionCount` peaked at 141, where the three confirmed September load tests sustained 241/608/1,404), and it is unrelated to the kb-ingestion DLQ alarm that fired the same day at 02:34Z. Per-session GB-hours (0.452) sit right on the other two V2 days' (0.526, 0.469), so **it does not distort the headline ratio** — the reason to report it separately is that it is a *new shape* of prod traffic, absent from the V1 baseline window at this volume (the same assistant served about 26 retrievals on 10-12 and about 200 on 10-20), and one that should recur weekly while the course runs. Treat it as prod's emerging normal, not as contamination to strip.
   **A cost finding fell out of this and is worth fixing independently of V2:** that assistant has **no knowledge-base documents at all** — one `METADATA` row and no `DOC#` rows — yet every turn still issues a retrieval against its classic vector index, and about 97% of the evening's retrievals returned zero chunks. That is one guaranteed-empty vector search per turn, ~3,900 of them in four hours, paid in latency on the turn path (§4's TTFT budget) for a result that cannot exist. The retrieval should be gated on the assistant actually having documents, not on its having an index.
2. **The V1 window is clean, and that was checked rather than assumed.** `ActiveSessionCount` peaked at 12–60 with a mean of 6–13 on every day of it, nowhere near the 241 floor of a confirmed load test. The two prod releases inside the window — 1.26.0 (10-03 16:08Z) and 1.27.0 (10-09 03:36Z) — both land on days *below* the window mean, so release smoke on prod is not inflating the baseline measurably. The releases that worried the brief, 1.29.0 and 1.30.0, are both after the V1 window closes and so cannot touch it; 1.28.0/1.28.1 land on 10-16, the flip day, which is excluded from both sides.
3. **10-16 is excluded entirely.** The flip was 19:25Z, so that day bills under both version's usage types (370.2 GB-hours V1, 41.1 V2). Rather than apportion it as §9.2 did for dev's flip hour, it is simply left out of both windows; at 411 GB-hours it is one day in 14 and including it changes no conclusion.
4. **Prewarm is on for the V2 days and did not exist in the V1 window** — dev's confounder 1, with the same consequence. 15–27% of V2 invocations are warms. A warm for a conversation with no live microVM starts a *session*, so the per-session denominator is inflated by sessions that may never take a turn, which flatters V2; the per-non-warm-invocation row is the counterweight, and the truth is between them. The turnless-warm share is still unmeasured — it is §9.4 item 1, and it is now a prod question too.
5. **V1's pre-booted pool.** Unchanged from §9.3 item 4: whether the pool's idle time is billed to us, and therefore whether prod's 1.179 GB-hours per session includes it, is not visible from either source. If it is billed, part of the V1 baseline is overhead V2 does not have, and V2 looks better than the ratios here suggest.
6. **The V1 window predates eight features that 1.28.0 turned on in prod** (Projects, large documents, folder attachments and the rest). Those change what a turn does and therefore what it holds in memory. The baseline measures the stack as it ran up to 10-16, not the stack running now, so a V1-versus-V2 comparison across 10-16 is not a clean single-variable change — a caveat that applies to the V2 ratios in §10.3, not to the baseline itself.

### 10.7 Verdict: §9.4 item 4 is answered; the prod decision is now a different question

- **The baseline asked for exists: prod V1 is 1.179 GB-hours per session** (385.0 per day over two full weeks, 0.349 per invocation), with vCPU at 0.01107 vCPU-hours per session and a Runtime line of $3.96/day split 91.8% memory. It is measured on 14 complete days of real traffic, cross-checked between two sources to 0.005%, and stable to 0.001 GB-hours per session against a 17-day window. §9.4 item 4 can be closed.
- **The framing in §9.4 is stale in one respect worth correcting:** prod is not waiting for this number in order to decide whether to adopt V2. It adopted V2 on 2026-10-16. The live question is whether to stay, and the rollback is cheap and known (set `V1`, run `platform.yml`).
- **On cost, prod's early V2 data is better for V2 than dev's was, and the reason is structural, not noise.** The memory reclaim reproduces at 0.39–0.42 per session against dev's 0.40, but it applies to a 1.37× larger V1 baseline per session, because prod sessions run 3.4 turns to dev's 1.7 and therefore contain more reclaimable idle. Per unit of work prod V2 costs 0.59–0.99× V1 where dev measured 1.26×. **Nothing here is a reason to roll back on cost.**
- **It is also not yet a reason to claim a saving.** Three days, two of them a weekend, one automated, with prewarm newly on and the turnless-warm share unmeasured, and a vCPU-per-invocation ratio that swings 1.02–1.60× depending on which days you take. The honest statement is **cost-neutral to modestly cheaper, with the error bar wider than the effect**.
- **What is still missing**, now that the baseline is not:
  1. The week-2 prod readout (**scheduled 2026-10-24**), over 10-17 → 10-23, which gives a full week including five weekdays and dilutes 10-19.
  2. ~~What ran on 10-19 evening.~~ **Answered (§10.6 item 1): a course simulation assistant, real student traffic, whose multi-round design makes 8–10 turns per session normal.** It should be *kept* in the week-2 readout, not stripped — but read as a new workload shape rather than as part of the baseline's mix, and expected to recur weekly. The separate waste it exposed (a per-turn retrieval against an empty knowledge base) is a turn-path fix, not a V2 question.
  3. The turnless-warm share (§9.4 item 1), which bounds the per-session numbers on both environments.
  4. The vCPU question (§9.3 item 5), which prod widened rather than narrowed.
- **The decision this should be taken on is latency, not cost.** The Runtime line is ~$1,450/year on V1 and the plausible V2 range is roughly ±$600 of it. Prewarm cut dev's median first token by 1.5 s and depends on V2's cheap idle. That is worth more than the money either way, which is what §9.4 already concluded — prod's numbers make the cost side of it closer to free than dev's did.
