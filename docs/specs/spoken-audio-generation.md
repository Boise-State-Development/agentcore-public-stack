# Spoken audio generation: scripts rendered as speech

Status: proposed, nothing built. Will ship in development behind
`SPOKEN_AUDIO_ENABLED` (off by default).

## 1. Summary

A user asks for something to listen to: a podcast ("turn this report into a
ten-minute two-host episode"), a narration of a document, or a dialogue for a
language lesson. The chat model writes the script, as it writes any document
today, and calls a `create_spoken_audio` tool. The tool is a general
script-to-speech renderer with one to five speakers; nothing in it is specific to
podcasts, which are the motivating example.

The tool hands the script to an **external renderer**: an open-weight
text-to-speech model (MOSS-TTSD v1.0) on a SageMaker asynchronous endpoint that
scales to zero, deployed as its own stack outside `PlatformStack`. When the render finishes, the finished MP3 lands in the
conversation's Files, a cost row charges the GPU time to the user's quota, and
an inline card in the chat turns from "Rendering…" into an audio player.

The design keeps the platform side as small as it can be: **no new
`PlatformStack` resources, no new statements on the Runtime or app-api roles,
and no Runtime environment bytes beyond the flag itself.** Everything heavy lives
in the renderer stack, which reaches into the platform only through names it can
derive from the project prefix.

## 2. Why this shape

Three shapes were weighed (2026-10-31):

| Shape | Quota sees the GPU spend | MP3 in the conversation's Files | Platform footprint |
|---|---|---|---|
| Built into `PlatformStack` | yes | yes | ~15–20 resources, new Runtime/app-api grants |
| External MCP server | no (server-side caps only) | no (server's own bucket) | none |
| **Hybrid (chosen)** | **yes** | **yes** | **none** |

`PlatformStack` sits against CloudFormation's 500-resource limit (432 all-on in
dev as of 2026-11-01, guarded by `infrastructure/test/platform-ceilings.test.ts`),
and the app-api and Runtime roles' fullest overflow policies have a few hundred
characters free. A SageMaker endpoint with its autoscaling, roles and
completion pipeline would spend a large share of that headroom on one optional
feature. An MCP server avoids the cost but loses the two things that make the
feature fit the platform: metering against the user's quota and the file landing
in the conversation. The hybrid keeps both and moves every resource out of the
stack.

Model choice comes from a bake-off on 2026-10-31 (Appendix A): MOSS-TTSD
rendered a two-host script at about half real time, matched Polly's
intelligibility, and cloned two synthetic "house voices" cleanly. It is
Apache-2.0 licensed, supports up to five speakers and, per its model card, up to
60 minutes in one pass.

## 3. Architecture

```
 chat turn (Runtime)                       renderer stack ({prefix}-SpokenAudioRendererStack)
 ───────────────────                       ──────────────────────────────────────────────
 create_spoken_audio(title, script)
   ├─ PutItem   SPKA#<jobId>  status=queued ──┐
   └─ PutObject requests/<jobId>.json ────────┼──▶ S3 event ─▶ spoken-audio-submit λ
      (allowed by the renderer bucket's       │                 ├─ InvokeEndpointAsync
       resource policy, not the Runtime role) │                 └─ SPKA# status=submitted,
   returns ~1 s later: a "rendering" card     │                    coldStart, estimatedReadyAt
                                              │
                                              │   SageMaker async endpoint (MOSS-TTSD, 0..N)
                                              │     writes audio/<jobId>.mp3 + outputs/<id>.out
                                              │                       │ (or failures/<id>.out)
                                              │                       ▼
 SPA card polls app-api                       │   S3 event ─▶ spoken-audio-finalize λ
   GET /sessions/{id}/spoken-audio/{jobId}        │                 ├─ copy MP3 → user-files bucket
   (one row read)  ◀──────────────────────────┴──────────────── ├─ FileMetadata row (READY)
                                                                ├─ C# cost row → user-cost-summary
                                                                └─ SPKA# status=ready
```

## 4. The renderer

### 4.1 Where it lives and how it deploys

- **Container:** `backend/src/spoken_audio_renderer/`. A SageMaker bring-your-own-container
  server (`/ping`, `/invocations`) on the AWS PyTorch inference deep learning
  container, which is pulled from AWS ECR and so avoids the Docker Hub rate limit
  that has blocked backend deploys before. The package is outside the `apis/`
  import boundary and never imports `apis.shared`: it carries its own exactly
  pinned torch/transformers stack (MOSS-TTSD pins `transformers==5.0.0`,
  `torch==2.9.1+cu128`).
- **Stack:** `infrastructure/lib/spoken-audio-renderer/spoken-audio-renderer-stack.ts`, with
  its own entry point `infrastructure/bin/spoken-audio-renderer.ts`. It reuses
  `config.ts` (`loadConfig`, `getResourceName`), so every name it derives for
  the platform's tables, bucket and Runtime role is computed by the same code
  that named them.
- **Lambdas:** `backend/src/lambdas/spoken_audio_submit/` and
  `backend/src/lambdas/spoken_audio_finalize/`, following the one-folder-per-Lambda
  convention. The finalizer imports `apis.shared`, as
  `large_document_prepare` does, so the cost row and file row are written by the
  same code the platform uses.
- **Workflow:** `.github/workflows/spoken-audio-renderer.yml`, run by an environment
  that wants the feature. It builds and pushes the image, then
  `cdk deploy {prefix}-SpokenAudioRendererStack -a "npx ts-node bin/spoken-audio-renderer.ts"`.
  The two Lambdas are container-image functions, like `large_document_prepare`,
  and the renderer stack creates them on a bootstrap image. Their **code** is
  updated by `backend.yml` through `scripts/build/deploy-image-lambda-one.sh`
  (new `spoken-audio-submit` / `spoken-audio-finalize` cases, targeting the function by its
  derived name), alongside every other backend deploy. That keeps the finalizer
  on the same commit as the `apis.shared` schema it writes, so the two stacks can
  never skew. In an environment that never deployed the renderer, the step finds
  no function and skips; it does not fail the deploy.
- **Order:** the platform first, then the renderer. The renderer's bucket policy
  names the Runtime role, and S3 rejects a policy whose principal does not exist
  yet. In an existing deployment this always holds.

**This is the repository's first stack besides `PlatformStack`.** It is a
deliberate exception to the single-stack convention, and the implementing PR
updates CLAUDE.md to say so. The exception is narrow: there are **no
CloudFormation exports or imports** between the stacks, so neither blocks the
other's update or deletion, and deploy order does not matter beyond the one
condition above. A deployment that does not want spoken audio never deploys
the renderer.

### 4.2 Model, weights and voices

- MOSS-TTSD v1.0 (8B, BF16) and MOSS-Audio-Tokenizer, at **pinned Hugging Face
  revisions**. Both load with `trust_remote_code`, so the modeling code is
  reviewed and vendored into the image at that revision, and the container runs
  with `HF_HUB_OFFLINE=1`. Nothing is downloaded at runtime.
- Weights (~16 GB) ship as a SageMaker `model.tar.gz` in the stack's model-data
  bucket, not in the image, so the image stays small enough for a GitHub runner
  to build. A runner with the default disk is still tight for a CUDA base image;
  PR-1 confirms it or moves the build to a larger runner.
- **House voices only.** v1 ships five synthetic voices, one per speaker tag
  `[S1]`–`[S5]`: short clips MOSS-TTSD generated from fixed seeds with no
  reference audio, plus their transcripts, versioned in the image under
  `voices/`. The bake-off minted and tested two; PR-0 mints the remaining three
  from fixed seeds. Requests name a voice id; the request schema has **no audio
  field**, and the renderer rejects any voice id it does
  not ship. No user-supplied or real person's voice is ever used. (MOSS-TTSD's own
  sample reference clips are recordings of public figures and are not used.)
- Output: mono MP3, 128 kbps, loudness-normalised to -16 LUFS (ffmpeg in the
  image), the format used in the bake-off.

### 4.3 Endpoint and scaling

- **Instance:** `ml.g6e.xlarge` (NVIDIA L40S, 48 GB). The bake-off peaked at
  24.8 GB of GPU memory, so 24 GB cards (A10G, L4) are ruled out.
- **Scale to zero:** an Application Auto Scaling target with minimum 0 and maximum
  `CDK_SPOKEN_AUDIO_RENDERER_MAX_INSTANCES` (default 1, matching dev's quota of one
  `ml.g6e.xlarge` endpoint instance). It scales out from zero on the
  `HasBacklogWithoutCapacity` metric and back in on
  `ApproximateBacklogSizePerInstance`, after
  `CDK_SPOKEN_AUDIO_RENDERER_IDLE_MINUTES` (default 15) with an empty queue.
- **One render per instance** (`MaxConcurrentInvocationsPerInstance: 1`). Jobs
  queue in SageMaker's own async queue.
- **Request TTL:** a request that waits longer than 1 hour lands in
  `failures/` and is finalized as failed, so nothing waits forever.
- **Cold start is unmeasured.** It covers provisioning an instance, pulling the
  image and loading 16 GB of weights. PR-0 measures it; the target is under 8
  minutes. An environment can add a keep-warm schedule (minimum 1 during working
  hours) at $2.6054 per hour if cold starts prove too slow.

### 4.4 The bucket contract

One private bucket, `{prefix}-spoken-audio-renderer-io-{account}`: SSE-S3,
public access blocked, SSL enforced. The suffix and every key prefix live in
`backend/src/apis/shared/spoken_audio/renderer_contract.json`, which both the Python
side and the CDK stack read, with a test on each side pinning them.

| Prefix | Written by | Holds | Lifecycle |
|---|---|---|---|
| `requests/` | Runtime role (bucket policy, `PutObject` on this prefix only) | `{jobId, userId, sessionId, title, script, voices, wordCount}` | 7 days |
| `outputs/` | SageMaker | JSON manifest `{jobId, audioKey, audioS, renderS, model, modelRevision}` | 7 days |
| `failures/` | SageMaker | error body | 7 days |
| `audio/` | the container | the MP3 | 7 days (the user's copy lives in Files) |

The Runtime finds the bucket by deriving its name from `PROJECT_PREFIX` and
`AWS_ACCOUNT_ID`, which it already has, using the contract file's suffix. It
deliberately does not go in `derived_environment.json`, whose test checks every
entry against the `PlatformStack` template, and this bucket is not in it.

## 5. Platform side

### 5.1 The tool: `create_spoken_audio`

```
create_spoken_audio(title: str, script: str) -> card
```

- **Description** (it ships in `toolConfig` on every turn while the tool is
  enabled, so it stays short):

  > Render a script as spoken audio (MP3) that is saved to this chat's Files.
  > Use it for podcasts, narrations and dialogues. Speech only. Write the script
  > as turns, one per line, each starting with a speaker tag `[S1]` to `[S5]`;
  > use only `[S1]` for a single narrator. Rendering runs in the background.

  It names podcasts, narrations and dialogues as examples and says "speech
  only", so it never suggests music or sound effects, which the model cannot
  make.
- **Script format:** turns tagged `[S1]` to `[S5]`, one per line, the format
  MOSS-TTSD and the bake-off used. Each tag maps to one house voice (§4.2), so
  one to five speakers, a single narrator included. Choosing voices is an open
  question (§14).
- **Quality is measured for two speakers only.** The bake-off rendered a
  two-host script. A single narrator and three to five speakers are unmeasured
  until PR-0 renders them (§12).
- **Validation:** at most 3,000 words in v1 (about 15 minutes at the 3.4 words
  per second MOSS-TTSD spoke in the bake-off), until PR-0 measures long-form
  renders. At least one turn, every turn tagged `[S1]` to `[S5]`, and at most 2
  unfinished jobs per user.
- **Quota pre-check:** the estimated cost (§7) is checked against the user's
  remaining quota with the same check the turn path uses. Over quota, the tool
  returns a refusal the model can relay, and nothing is submitted.
- **Submit:** `PutItem` of a `SPKA#<jobId>` row (§5.2), then `PutObject` of the
  request JSON. The job id is a random UUID. Both calls are on resources the
  Runtime can already reach: sessions-metadata through its existing grant, and
  the renderer bucket through the bucket's own policy. **The Runtime role gains
  nothing.**
- **Returns in about a second.** The result is an inline card (`ui_type:
  "spoken_audio_render"`, `ui_display: "inline"`, payload `{job_id, title,
  est_audio_s, est_ready_at}`) and a one-sentence summary for the model:
  "Rendering *Title* (about N minutes of audio). A card showing its progress
  and, when done, a player is already displayed; you cannot check its status."
  There is no URL and no echo of the script.
- **No renderer deployed:** `PutObject` fails with `NoSuchBucket`, and the tool
  says spoken audio is not set up in this environment.
- **Registration:** a context-bound tool built per request
  (`_build_spoken_audio_tools` in `inference_api/chat/routes.py`) with a
  `SPOKEN_AUDIO_TOOL_IDS` family in `apis/shared/tools/injected.py`. Its closure is
  only `(session_id, user_id)`, both cache-key elements, so the family joins
  `KEY_DESCRIBED_INJECTED_TOOL_IDS` and enabling it does not bypass the agent
  cache. The catalog row `create_spoken_audio` is seeded `enabledByDefault: false` and
  reaches users only through a role grant.

### 5.2 The job row

A `SPKA#<jobId>` row in sessions-metadata, keyed like the `TSUM#` and `EXTR#`
rows (the owner in the partition key, reachable per session through
`SessionLookupIndex`), so it needs no new table or index. It has a TTL like the
`C#` rows.

| Field | Set by |
|---|---|
| `status`: `queued` → `submitted` → `ready` / `failed` | tool / submit λ / finalize λ |
| `title`, `wordCount`, `estAudioS`, `createdAt` | tool |
| `inferenceId`, `coldStart`, `submittedAt`, `estimatedReadyAt` | submit λ |
| `uploadId`, `audioS`, `renderS`, `costUsd`, `completedAt` | finalize λ |
| `error` (a short code, never a stack trace) | finalize λ |

`coldStart` is the submit Lambda reading the endpoint's current instance count
(`DescribeEndpoint`): zero means the card should say "warming up".

### 5.3 How the platform learns a job finished: push, not poll

The finalizer is triggered by S3 events on `outputs/` and `failures/`. It is the
**only** writer of terminal state. Steps, all idempotent:

1. Read the manifest (or the failure body) and the original request.
2. **Claim** the job: a conditional update of `SPKA#` from `submitted` or
   `queued` to `finalizing`. If the condition fails, another delivery already
   handled it, and the finalizer exits.
3. On success:
   - Copy the MP3 into the user-files bucket under the same key scheme
     `_store_document` uses.
   - Write a `FileMetadata` row with status `READY`, MIME type `audio/mpeg` and
     filename `<slug>.mp3`.
   - Write the `C#` cost row (§7).
   - Set `SPKA#` to `ready`.
4. On failure: set `SPKA#` to `failed` with an error code, write no cost row,
   and emit an EMF metric.

Its grants live on its own role in the renderer stack: read the renderer bucket;
put to `user-files/*` in the user-files bucket; write the user-file-uploads,
sessions-metadata, user-cost-summary and system-rollup tables. The user-files
bucket is SSE-S3 and the tables use AWS-managed keys, so no key policy needs
editing. The submit Lambda gets `InvokeEndpointAsync`, `DescribeEndpoint` and
`UpdateItem` on sessions-metadata.

`_store_document`'s key scheme and `FileMetadata` construction currently live in
`agents/builtin_tools/office/_storage.py`, which a Lambda cannot import. PR-2
moves them into `apis/shared/files/` and leaves the office tools calling the
shared copy.

**Alternatives rejected:**

- **The tool waits for the render.** A render takes about half the audio's length
  plus a cold start of minutes, which breaks the 600 s turn limit for anything
  but a short clip. It would also hold the session's single-flight lease, so the
  user could not send another message, and tie the render to a Runtime microVM.
- **app-api finalizes lazily when the card polls.** A render whose user closes
  the tab would never be metered and never land in Files. It would also need
  app-api to read the renderer bucket.
- **The renderer calls back into app-api over HTTP.** That adds a
  service-authenticated route on the public API and a shared secret, to do what
  one Lambda with the right grants does directly.

**The 600 s turn limit is never in play.** The tool returns after two writes, and
everything after that runs without the turn. The user can keep chatting, close
the tab or reload: the render, the metering and the file land regardless. The
Runtime is not involved after the submit.

**Stale jobs:** a job still `queued` or `submitted` more than 2 hours after
`createdAt` (for example, the renderer stack was torn down mid-render) is reported
as `expired` by the status route. That is derived at read time, so no sweeper is
needed, and nothing is charged.

### 5.4 Status route

`GET /sessions/{session_id}/spoken-audio/{job_id}` in `apis/app_api/spoken_audio/`.

- **Auth:** `get_current_user_from_session`, which authenticates before the flag
  check, per the convention for flag-gated routes. Ownership is the `(session,
  user)` key of the row, so a job id alone reveals nothing. 404 when the flag is
  off.
- **Read:** one `GetItem` on `SPKA#`, no other calls, the same shape as
  `GET /files/{id}/search-status`.
- **Response:** `{status, coldStart, estimatedReadyAt, uploadId?, audioS?,
  error?}`. app-api already reads sessions-metadata, so it gains no grants.

## 6. What the SPA shows

A new inline renderer next to `file-download-renderer.component.ts`, mounted from
the tool result's `spoken_audio_render` card. It uses signals and is gated on
`features.spokenAudio`; with the flag off the result falls back to the
ordinary tool rail.

| State | Card |
|---|---|
| `queued` / `submitted`, `coldStart: true` | "Warming up the renderer. The first render in a while takes a few extra minutes." Indeterminate bar |
| `submitted` | "Rendering *Title*, about N min of audio". A progress bar driven by `estimatedReadyAt` that slows near the end and never shows 100% before `ready` |
| `ready` | Native `<audio controls preload="none">` player, duration, a Download button and "Saved to this chat's Files" |
| `failed` / `expired` | What happened, in a sentence, and that asking again will retry. No cost was charged |

- **Polling:** every 5 s for the first minute, then every 15 s. It pauses while
  `document.hidden` and stops at a terminal state. A reloaded conversation
  re-mounts the card from the persisted tool result and polls once, so a
  finished job settles immediately.
- **Playback:** the player resolves a presigned URL through
  `/files/{uploadId}/download`, as the existing file previews do, and resolves it
  again if it expires mid-session. S3 serves range requests, so seeking works
  without downloading the whole file.
- **Accessibility:** status text sits in an `aria-live="polite"` region, and the
  player is the browser's own.
- **Files:** the MP3 appears in the Files panel like any generated file. Claude
  models cannot take audio input, so the attachment flow must not offer it as a
  chat attachment; PR-3 checks how the panel treats unsupported MIME types.
- **Terminal client:** shows the summary line. The file is reachable through
  Files; there is no player.

## 7. Pricing the GPU time

```
costUsd = renderS × gpuUsdPerHour / 3600
```

- **`renderS`** is measured inside the container: wall time around generation
  plus MP3 encoding for this job only, rounded up to the second.
- **`gpuUsdPerHour`** comes from renderer config
  (`CDK_SPOKEN_AUDIO_RENDERER_GPU_USD_PER_HOUR`), set on the finalizer. The
  SageMaker async-inference rate for `ml.g6e.xlarge` in us-west-2 is
  **$2.6054/hour** (Price List API, `AmazonSageMaker`, usage type
  `USW2-AsyncInf:ml.g6e.xlarge`, verified 2026-11-02), about 40% above the EC2
  on-demand rate for the same instance. The value is re-verified whenever the
  instance type or region changes.
- **The cost row** is a normal `C#` row written through `store_message_metadata`
  with message id `spoken-audio:<jobId>` and model `spoken-audio/moss-ttsd-v1.0`, so it
  updates the user's cost summary (quota), the system rollup and the admin cost
  drill-down exactly as a model call does. Its cost is never `None`, so
  `UnmeteredModelCall` does not fire. Like voice rows, it carries no prompt-cache
  fields.
- **Not charged to users:** the cold start, the idle tail before scale-in, and
  failed renders. These are platform overhead, reported through EMF metrics
  (`SpokenAudioRenderSeconds`, `SpokenAudioBilledUsd`) and reconciled monthly against
  the endpoint's actual instance-hours. Charging a cold start to whoever happened
  to trigger it would make the same render cost a user anywhere from $0.05 to
  $0.40.
- **Pre-check estimate** (§5.1): `wordCount / 3.4 × 0.5 × rate / 3600 × 1.5`,
  which uses the bake-off's speaking rate and render speed with a 50% margin.

| Script | Render (at 0.5× real time) | Charged |
|---|---|---|
| Bake-off script (487 words, 2.3 min) | ~70 s | ~$0.05 |
| 10 minutes | ~300 s | ~$0.22 |
| 15 minutes (v1 cap) | ~450 s | ~$0.33 |

The overhead per cold cycle is roughly the warm-up (~5 min, ~$0.22, unmeasured)
plus 15 idle minutes (~$0.65).

## 8. Flags and configuration

| | Name | Default |
|---|---|---|
| Backend (Runtime builds the tool; app-api mounts the status route) | `SPOKEN_AUDIO_ENABLED` / `CDK_SPOKEN_AUDIO_ENABLED` / `config.spokenAudio.enabled` | off; only `"true"` enables |
| SPA | `features.spokenAudio` | `false` in all three environment files until dev turns the backend on |
| Renderer instance type | `CDK_SPOKEN_AUDIO_RENDERER_INSTANCE_TYPE` | `ml.g6e.xlarge` |
| Renderer max instances | `CDK_SPOKEN_AUDIO_RENDERER_MAX_INSTANCES` | `1` |
| Idle before scale-in | `CDK_SPOKEN_AUDIO_RENDERER_IDLE_MINUTES` | `15` |
| GPU rate | `CDK_SPOKEN_AUDIO_RENDERER_GPU_USD_PER_HOUR` | `2.6054` |

- The Runtime copy of the flag is written only while on, so the default spends
  none of the V2 environment budget. While on it costs one short variable.
- **Kind:** a feature switch. It costs money, so it is permanent configuration
  and never retires.
- Turning it on in prod adds a line to #1798. The docs-site feature-flags page
  gets a row, and the release gate sees the new SPA flag.
- Synth cannot check that the renderer stack exists, so the flag does not
  require it. The tool reports the missing bucket at call time instead (§5.1).

## 9. Token and prompt-cache cost

- **Tool spec:** a constant, about 250 tokens in `toolConfig`, present only for
  users granted the tool, in deterministic order.
- **Script:** model output once, then part of the conversation history as the
  tool call's input. The 3,000-word cap bounds it at about 4k tokens, read from
  the cache at 0.1× on later turns.
- **Result:** under 60 tokens: no URL, no transcript, no status.
- **Nothing re-enters the conversation when the render finishes.** History stays
  append-only and byte-stable, so the prompt cache is unaffected. The model is
  told it cannot see status, so it points the user at the card instead of
  guessing.
- **Time to first token:** untouched. The tool runs only when called, and
  nothing is added to `BeforeInvocation` or `BeforeModelCall` hooks.

## 10. Safety and data handling

- **Voices:** house voices only (§4.2). The request schema has no reference-audio
  field and the renderer rejects unknown voice ids.
- **Script text:** user content. It stays in the renderer bucket for at most 7
  days, never appears in logs (job ids and counts only), and is never sent
  anywhere but the endpoint.
- **Ownership:** only the Runtime role can write `requests/`. The finalizer
  trusts the `userId`/`sessionId` in that request, because nothing else can write
  it, and the status route checks `(session, user)`.
- **Abuse and cost:** the role grant, the per-user active-job cap, the word cap
  and the quota pre-check.
- **Supply chain:** pinned model revisions, vendored and reviewed remote code,
  and an offline hub at runtime (§4.2).

## 11. Platform footprint

| Ceiling | Change |
|---|---|
| `PlatformStack` resources | **0** (verified by `platform-ceilings.test.ts` in PR-2) |
| Runtime / app-api role policies | **0 characters**: the renderer bucket's policy grants the Runtime; the renderer stack's own roles do the rest |
| Runtime environment (V2 budget) | the flag variable while on; the bucket name is derived |
| New tables or indexes | none (`SPKA#` rows in sessions-metadata) |

## 12. Phasing

1. **PR-0, renderer spike (no platform change).**
   - Render 5, 10 and 15 minutes in a single pass, and record memory and quality.
   - Measure a SageMaker async cold start from zero.
   - Build FlashAttention and re-measure render speed.
   - Mint five house voices.
   - Render a single-narrator script and a three-to-five-speaker script, and
     score both the way the bake-off scored two hosts. The bake-off tested only
     a two-host script, so these are unmeasured until then. The
     three-to-five-speaker render is also the check on the new house voices.
   - These results set the word cap and the cold-start copy.
2. **PR-1, renderer stack.**
   - Container, stack, workflow, and the submit and finalize Lambdas.
   - Deployed to dev only and smoke-tested by writing a request object by hand.
3. **PR-2, platform backend.**
   - Flag pair, the `apis/shared/spoken_audio/` contract module, and the tool with its
     catalog seed and injected family.
   - The `SPKA#` model and status route.
   - The `_store_document` key scheme moved to `apis/shared/files/`.
   - Tests (§13).
4. **PR-3, SPA.** The card, player, polling and feature flag.
5. **PR-4, enablement.**
   - Dev validation end to end and the docs-site page.
   - Skills that write good scripts for the tool, starting with a "podcast
     producer" (episode structure, pacing, length) and a narrator, plus a sample
     agent in Agent Designer.

## 13. Tests

- **Contract:** the Python and CDK sides read the same `renderer_contract.json`.
  A stack test pins the bucket name, prefixes and the Runtime role principal to
  `getResourceName`.
- **Tool:**
  - Validation: a single-narrator `[S1]` script is accepted, an `[S6]` tag or an
    untagged turn is refused, the word cap, the active-job limit.
  - The quota refusal path.
  - The missing-bucket message.
  - The result carries no URL and no script.
  - Injected-family cache eligibility (`tests/shared/test_injected_tool_cache_eligibility.py`).
- **Finalizer** (moto):
  - Success writes the file row, `C#` row and `SPKA#`.
  - A second delivery is a no-op.
  - A failure writes no cost row.
  - Cost arithmetic.
- **Status route:**
  - Auth before flag.
  - Another user's job and another session's job both 404.
  - Derivation of `expired`.
- **CDK:**
  - `platform-ceilings.test.ts` unchanged with the flag on.
  - The Runtime environment test with the flag on.
  - A renderer stack snapshot.
- **SPA:**
  - Card states.
  - Polling backs off, pauses while hidden and stops at terminal states.
  - Fallback when the flag is off.

## 14. Open questions

1. **Notify a user who left before it finished** (an announcement, email or
   push), or is the Files panel enough?
2. **Voices:** a voice picker, and adding house voices beyond five.
3. **Editing:** re-render one segment instead of the whole script.
4. **Captions:** ship the script as a `.txt` or WebVTT sidecar for accessibility.
   MOSS-TTSD gives no word timestamps, so WebVTT would need alignment.
5. **Labelling:** whether to mark generated audio as AI-made, in ID3 metadata
   and/or spoken.
6. **Cold starts in prod:** whether to add a keep-warm schedule, decided from
   PR-0's measurement and real usage.
7. **Other clients:** later expose the same renderer as an MCP server for Claude
   Desktop and Code. The hybrid keeps that door open: the renderer does not know
   the platform exists, apart from the finalizer.

## Appendix A: bake-off, 2026-10-31

The same 487-word, two-host script went through each arm. Open models ran on one
EC2 g6e.xlarge (L40S) in dev, us-west-2; Polly ran from a laptop. WER is
Whisper `medium.en` against the script. Polly reads text verbatim, so its 0.048
is the scorer's floor.

| Arm | Audio | Render | × real time | Peak GPU | WER |
|---|---|---|---|---|---|
| Polly generative (Ruth / Matthew) | 160.1 s | 46.1 s, 21 sequential requests | 0.29 | — | 0.048 |
| MOSS-TTSD, no reference | 142.0 s | 70.3 s | 0.49 | 24.6 GB | 0.054 |
| **MOSS-TTSD, house voices** | 140.7 s | 69.7 s | 0.50 | 24.8 GB | 0.050 |
| Dia2-2B, no reference, 3 chunks | 181.3 s | 121.6 s | 0.67 | 10.7 GB | 0.109 |
| Dia2-2B, house voices, 3 chunks | 140.9 s | 206.4 s | 1.46 | 15.2 GB | 0.058 |

- **Only the unconditioned Dia2 arm garbled content:** a whole passage came back
  as nonsense.
- **MOSS-TTSD ran without FlashAttention** (it fell back to SDPA), so its render
  times are an upper bound.
- **The house voices** were two ~5.5 s clips MOSS-TTSD generated with no
  reference. Both open models cloned them.
- **Excluded:** VibeVoice (Microsoft withdrew the TTS code), Fish Audio S2 Pro
  (research licence) and Higgs Audio v3 (non-commercial).
