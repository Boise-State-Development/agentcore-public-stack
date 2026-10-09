# AgentCore Runtime V2 migration

**Status:** plan steps 1–3 shipped. The first dev V2 attempt (2026-10-09) failed on an environment-variable size limit (B4) and was rolled back to V1; step 4 (the variable refactor in §7) must land before V2 is tried again. Tracks `docs/kaizen/review-queue.md ▸ [2026-09-25] A/B the V2 AgentCore Runtime in dev` (Proposal 1 in `reviews/2026-09-25.md`).
**Sources:**
- The AWS ML blog post [The new AgentCore Runtime: elastic, optimized, and consistently fast starts](https://aws.amazon.com/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/) (2026-09-18).
- The [What's New post](https://aws.amazon.com/about-aws/whats-new/2026/09/new-agentcore-runtime-generally-available).
- The CloudFormation registry schema for `AWS::BedrockAgentCore::Runtime` (us-west-2), read on 2026-09-27.
- botocore's `bedrock-agentcore-control` service model on `develop`.
- A read-only `GetAgentRuntime` against the dev runtime.
- The failed dev V2 update on 2026-10-09 (§3 B4), and read-only `GetAgentRuntime` calls against the dev and prod runtimes that day.

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

**What AWS does not say:**
- No V2 price. The pricing page had no V2 row as of the 09-25 research.
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
2. **Warm-up (`apis/inference_api/warmup.py`) could become free, or wasted.**
   - Today it runs on a daemon thread so `/ping` answers immediately. It covers the 3.6 s residual first-turn cost from `load-test-assessment-2026-09.md` §1.
   - If V2 snapshots after `/ping` goes healthy and **before** warm-up finishes, the work isn't in the snapshot, and each restore redoes the rest.
   - If the snapshot waits for readiness, holding `/ping` until warm-up completes would bake the imports and botocore model parses into the snapshot. That is paid once per deploy instead of once per session, the reverse of the V1 trade-off.
   - Measure before choosing. See §5.
3. **Cloned sockets and credentials.** Warm-up builds boto3 clients but makes no calls, so no pooled TCP connections should be in the snapshot today.
   - Keep it that way: anything that opens a connection at init restores a dead socket into every session. That is the stale-connection class behind #1338 (the LTM retrieval fix).
   - Add a comment in `warmup.py` so nobody "optimises" it into a real call.
4. **Cloned randomness and IDs.** Python's `random` module state and any ID generated at init are identical across every restored instance.
   - `uuid4`, `secrets` and `os.urandom` read the kernel RNG, which is fine only if the platform reseeds on restore (unverified).
   - Worth a grep of the inference-api import path for init-time `random` and ID generation. Today it only turns up in app-api modules, which don't run here.
   - Check the **OTEL resource**: if ADOT (`opentelemetry-instrument` in `Dockerfile.inference-api`) derives `service.instance.id` at SDK init, every session reports the same instance.
5. **Monotonic clocks across a restore.** Any TTL cache keyed on `time.monotonic()` that is populated at init could be read as fresh after restore. That includes the 60 s catalog cache, `oauth_token_cache` and the agent cache. All of them are empty at init today, so this is only a rule to keep: don't pre-populate time-bounded caches at startup.

### B3. The plan's latency metric can't see what V2 improves (verified)

The review proposed comparing the turn-latency EMF (#1184). `turn_timing.py` states that its clock starts at handler entry, so it "excludes the app-api hop and any Runtime cold start". The ~1.5 s cold routing gap and V2's snapshot restore both happen before that clock starts.

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
- **Unknown rate.** "Higher rate × fewer GB-hours" nets out unknown until AWS publishes the V2 price. The Cost Explorer sync (#1235) is the measurement.
  - Check first whether V2 bills under a distinct usage type. If it doesn't, a dev week can only be compared as a before/after, not side by side.
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
4. **Environment-variable refactor (B4).** See §7. Two PRs: the resolver first (no behaviour change while the variables are still set), then dropping the variables from the Runtime with a payload guard in CI. Prod needs this too, because it is over the limit on its own.
5. **Dev A/B.** Set `CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION=V2` in the `development` environment.
   - First tried 2026-10-09, before step 4, and rolled back (B4). Retry only once the payload guard (§7.5) passes for dev's real values.
   - Verify with `get-agent-runtime` after `platform.yml`, **and again after the next `backend.yml`**. The second check is B1's real test.
   - Watch for 424s on first turns (B2.1).
   - Measure the client-side cold first-token gap with `tests/load` (B3), `PreludeTotalMs` split by cold vs warm, agent-cache-hit turn latency (§4), and Runtime GB-hours from the Cost Explorer sync.
6. **Decide warm-up placement (B2.2).** Only after step 5 shows whether warm-up work lands in the snapshot.
7. **Prod.** Only after a clean dev week, with the V2 rate known, and with step 4 deployed to prod.

## 5a. Phase 2: prewarm the session when the user engages (after the dev A/B)

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
- Does V2 bill under a distinct Cost Explorer usage type, and at what rate?
- What is the reclaim window for cold memory, and what does a page-in cost?
- What does an idle V2 session cost for 900 s after a single no-op invocation? This decides whether §5a pays for itself.

## 7. Environment-variable refactor (B4)

**Goal:** the Runtime's environment fits V2's 2,560-byte limit with room to grow, whatever a deployment's prefix and domain are. The same change takes the variable count well under the 50-variable ceiling.

### 7.1 Inventory (dev, 2026-10-09)

| Kind | Count | Examples | Treatment |
|---|---|---|---|
| Prefix plus a fixed suffix (and the account, for some buckets) | 28 | `DYNAMODB_SESSIONS_METADATA_TABLE_NAME` = `{prefix}-sessions-metadata`, `S3_USER_FILES_BUCKET_NAME` = `{prefix}-user-file-uploads-{account}`, `BROWSER_POLICY_S3`, `AGENTCORE_RUNTIME_WORKLOAD_NAME` | Derive in code |
| Derivable from another variable | 3 | `MEMORY_ARN` (from `AGENTCORE_MEMORY_ID`, region, account), `AGENTCORE_LOCAL_OAUTH_CALLBACK_URL` (`FRONTEND_URL` + `/oauth-complete`), `AUTH_PROVIDER_SECRETS_ARN` (Secrets Manager accepts the secret's name, `{prefix}-auth-provider-secrets`) | Derive in code |
| Not derivable | 18 | Physical ids with a random suffix (`AGENTCORE_MEMORY_ID`, `AGENTCORE_CODE_INTERPRETER_ID`, `BROWSER_ID`), `FRONTEND_URL`, `CORS_ORIGINS`, `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`, `TOKEN_EXCHANGE_URL`/`_CLIENT_ID`, feature flags, `LOG_LEVEL`, `PROJECT_PREFIX`, `AWS_DEFAULT_REGION` | Keep |

| | Today | After |
|---|---|---|
| Dev | 49 variables, 2,938 bytes | ~19 variables, ~755 bytes |
| Prod | 45 variables, 2,579 bytes | ~16 variables, ~581 bytes |

"After" includes one new variable, `AWS_ACCOUNT_ID` (~26 bytes), which the account-scoped bucket names and `MEMORY_ARN` need. The template comparison found the same `{prefix}-{suffix}` shape for every derivable value in both dev and prod, so there is no legacy-named resource to special-case today.

### 7.2 Mechanism: derive names in `apis.shared`, keep the variable as an override

- **One resolver in `apis/shared/config/`**, for example `resource_name("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")`. It returns the environment variable when set (local `.env`, tests, app-api), otherwise `{PROJECT_PREFIX}-{suffix}` (plus `-{AWS_ACCOUNT_ID}` where the bucket carries it), otherwise whatever the read site returns today when the variable is unset. That last rule keeps tests and local runs with no prefix behaving exactly as now.
- **One manifest** maps each variable to its suffix and whether it carries the account. The resolver reads it; nothing else holds the suffixes.
- **Every read site migrates to the resolver.** That is about 57 files reading `DYNAMODB_*`/`S3_*` alone. `app_api`, `inference_api` and `agents` all reach it through `apis.shared`, so the import-boundary rule holds.
- **No turn-path cost.** It is string formatting, memoized per name, and runs at import or first use. Nothing reaches the prompt or `toolConfig`.

**Rejected alternatives:**
- **Fetch config from SSM or S3 at startup.** It adds a network call to every cold start (or to the V2 snapshot), an IAM grant and a new way to fail at boot, to carry values that are deterministic anyway.
- **Pack the variables into one JSON value.** It saves only the key bytes, about a third, and every reader would then have to parse it.

### 7.3 Keeping CDK and the resolver in step

The names exist twice: CDK creates them with `getResourceName(config, suffix)`, and the resolver rebuilds them. Two checks keep that from drifting silently:

1. **A CDK test reads the manifest** and asserts that the synthesized template has a resource with each derived physical name (a `TableName`, `BucketName` or `SecretName` equal to `{prefix}-{suffix}`). Renaming a resource in CDK without the manifest fails CI. None of these resources use `getTruncatedResourceName`, which shortens the prefix (verified 2026-10-09); the test should fail for any that starts to.
2. **app-api keeps its explicit variables** in the first PR. ECS has no comparable limit. At app-api startup, the resolver compares each explicit value with the derived one and logs a warning on any difference, which checks the derivation against every real deployment for free. Dropping them from app-api too is optional, later.

### 7.4 Things to get right

- **Conditional resources stay conditional.** CDK only sets the token-exchange variables when `config.tokenExchange` is configured. Derive `TOKEN_EXCHANGE_SECRET_ID` only when `TOKEN_EXCHANGE_URL` is set, so a deployment without token exchange doesn't suddenly look like it has a secret.
- **Absence as a feature gate.** Some read sites treat an unset name as "feature off". For example, `apis/shared/notifications/service.py` defaults `DYNAMODB_PROJECTS_TABLE_NAME` to `""`. Once the name is always derived, that code path always runs. Audit each read site: deriving is right only where the resource exists in every deployment, and the behaviour change has to be intended.
- **The secret by name.** Check that `auth_providers/repository.py` passes the value straight to `GetSecretValue`, and that the Runtime role's IAM statement matches the name as well as the ARN (a `{name}-*` resource pattern covers both).
- **Don't derive domains.** `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` and `CORS_ORIGINS` follow domain and certificate rules that live in CDK; keep them explicit.

### 7.5 The payload guard

A jest test beside the 50-variable guard (`infrastructure/test/runtime-env-var-limit.test.ts`):

- Synthesize the worst-case config (every conditional block on, a long prefix and domain).
- Resolve each Runtime environment value: literals as-is, a `Ref` or `Fn::GetAtt` to a resource with a literal name to that name, and anything else at a conservative maximum length.
- Fail when `sum(len(key) + len(value))` exceeds **2,000 bytes**. That leaves about 20% under 2,560 for AWS's unexplained overhead (69 bytes on dev) and for growth.
- It applies on V1 too, so a V1 deployment can't drift back into a payload that blocks V2.

### 7.6 Rollout

1. **PR A: resolver, manifest, read-site migration, CDK manifest test, app-api drift warning.** The Runtime still receives every variable, so behaviour is unchanged. Validate on dev: no drift warnings in app-api's log.
2. **PR B: drop the derivable variables from the Runtime in CDK, add `AWS_ACCOUNT_ID`, add the payload guard.** This is the change that can break the Runtime, so it lands after A has run on dev. Rollback is reverting B; the variables come back on the next platform deploy. Validate on dev, on V1: a new conversation's first turn, the tools that touch the derived resources (files, artifacts, memory spaces, skills, browser), and `get-agent-runtime` showing the smaller payload.
3. Then plan step 5, the dev V2 retry.

No rollout switch: PR B's off path would be the very variables it removes, and a revert restores them in one platform deploy.

