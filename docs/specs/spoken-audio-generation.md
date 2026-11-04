# Spoken audio generation: scripts rendered as speech

Status: proposed, nothing built. The PR-0 renderer spike ran on 2026-11-03
(Appendix B). Will ship in development behind `SPOKEN_AUDIO_ENABLED` (off by
default).

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
- Weights (23.8 GB: 16 GB of MOSS-TTSD and 6.7 GB of codec) ship **uncompressed**
  under an S3 prefix in the stack's model-data bucket (`ModelDataSource`,
  `S3DataType: S3Prefix`, `CompressionType: None`), not in the image and not
  as a `model.tar.gz`. SageMaker copied the 38 files to `/opt/ml/model` in
  2m47s in PR-0, and nothing had to be unpacked (Appendix B).
- **The image** needs no CUDA base image: torch's cu128 wheels carry their own
  CUDA runtime, so PR-0 used `python:3.12-slim` (from ECR Public) plus ffmpeg.
  It was 12.7 GB unpacked and 4.5 GB compressed in ECR. PR-1 confirms a
  default GitHub runner has the disk for it or moves the build to a larger one.
- **The image manifest must be Docker v2 schema 2.** SageMaker `CreateModel`
  rejected both an OCI image index and a plain OCI image manifest. Build
  without attestations and with Docker media types (`docker buildx build
  --provenance=false --output type=registry,oci-mediatypes=false`).
- The endpoint uses `InferenceAmiVersion: al2023-ami-sagemaker-inference-gpu-4-1`,
  whose driver ran the cu128 wheels in PR-0.
- **Attention stays on SDPA.** FlashAttention 2 was slower on the L40S (Appendix
  B). transformers 5.0 applies `attn_implementation` only to the outer
  MOSS-TTSD config and leaves the Qwen3 backbone on SDPA. Anyone re-testing it
  must also set `model.language_model.config._attn_implementation` and check
  every attention layer.
- **House voices only.** v1 ships five synthetic voices, one per speaker tag
  `[S1]`–`[S5]`: short clips MOSS-TTSD generated from fixed seeds with no
  reference audio, plus their transcripts, versioned in the image under
  `voices/`. The bake-off minted two (seeds 7 and 11), and PR-0 minted the
  other three (seeds 13, 31 and 19) and checked all five in one render
  (Appendix B). Requests name a voice id; the request schema has **no audio
  field**, and the renderer rejects any voice id it does
  not ship. No user-supplied or real person's voice is ever used. (MOSS-TTSD's own
  sample reference clips are recordings of public figures and are not used.)
- Output: mono MP3, 128 kbps, loudness-normalised to -16 LUFS (ffmpeg in the
  image), the format used in the bake-off.

### 4.3 Endpoint and scaling

- **Instance:** `ml.g6e.xlarge` (NVIDIA L40S, 48 GB). The weights alone take
  23.9 GB on the GPU, and an 18-minute render peaked at 28.1 GB allocated (30.4 GB
  reserved), so 24 GB cards (A10G, L4) are ruled out.
- **Scale to zero:** an Application Auto Scaling target with minimum 0 and maximum
  `CDK_SPOKEN_AUDIO_RENDERER_MAX_INSTANCES` (default 1, matching dev's quota of one
  `ml.g6e.xlarge` endpoint instance). It scales out from zero on the
  `HasBacklogWithoutCapacity` metric and back in on
  `ApproximateBacklogSizePerInstance`, after
  `CDK_SPOKEN_AUDIO_RENDERER_IDLE_MINUTES` (default 15) with an empty queue.
- **One render per instance** (`MaxConcurrentInvocationsPerInstance: 1`). Jobs
  queue in SageMaker's own async queue.
- **Request TTL:** a request that waits longer than 1 hour lands in
  `failures/` and is finalized as failed, so nothing waits forever. This must
  be set explicitly on every `InvokeEndpointAsync` (`RequestTTLSeconds`). PR-0
  left it unset, and a request that never got an instance was still queued,
  with nothing in `failures/`, 2h53m later.
- **Cold start (measured in PR-0, Appendix B) has two parts, and only one is
  bounded.**
  - **Once SageMaker has an instance, it is about 6 minutes.** From host boot to
    `/ping` passing took 4m59s: the weights copied to `/opt/ml/model` in 2m47s,
    the container started 32 s later, and the model loaded and warmed up in 34 s.
    DescribeEndpoint reported `InService` about 70 s after that. The old
    8-minute target holds for this part.
  - **Getting an instance is unbounded.** Creating the endpoint waited 25m43s
    before its host booted. The one scale-from-zero cycle never got an instance:
    the backlog alarm fired 4m23s after the request was queued, and SageMaker
    then reported `InsufficientInstanceCapacity` for `ml.g6e.xlarge`. Application
    Auto Scaling marked the scale-out `Unfulfilled` after 2 hours. The endpoint
    was still at zero instances when PR-0 tore it down 2h48m after the request.
    That is one night in one region, but a scale-to-zero design has to treat
    "no capacity for hours" as a normal outcome, not an edge case.
  - **Consequences:** the 1-hour request TTL and the `expired` state (§5.3) are
    paths users will see, not just a safety net. The card never promises a time
    while cold (§6). An environment that needs predictable latency keeps one
    instance warm (§14). A warm instance is the only way PR-0 found to bound the
    wait, at $2.6054 per hour: about $573 a month for 10 hours on each of 22
    working days.

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
- **Quality is measured for one, two and five speakers** (Appendix B). A
  single-narrator `[S1]` script scored WER 0.000. A five-speaker panel scored
  0.002, with every scored turn attributed to the right house voice.
- **Validation:** at most **3,600 words** in v1, about 18 minutes at the 3.3–3.8
  words per second MOSS-TTSD spoke in PR-0. That is the longest render PR-0
  measured, at 3,678 words. It rendered in one pass in 558 s with WER 0.008 and
  no voice drift, at 28.1 GB peak GPU memory. Longer scripts are unmeasured,
  not known to fail. The renderer's `max_new_tokens` must scale with the word
  count: PR-0 used `words / 2.2 × 12.5` frames, and no render reached it. At
  least one turn, every turn tagged `[S1]` to `[S5]`, and at most 2 unfinished
  jobs per user.
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
  plus a cold start of minutes to hours (§4.3), which breaks the 600 s turn limit for anything
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
| `queued` / `submitted`, `coldStart: true` | "Starting the renderer. After it has been idle this can take anywhere from a few minutes to over an hour. You can leave; the audio will be saved to this chat's Files." Indeterminate bar, no time estimate |
| `submitted` | "Rendering *Title*, about N min of audio". A progress bar driven by `estimatedReadyAt` that slows near the end and never shows 100% before `ready` |
| `ready` | Native `<audio controls preload="none">` player, duration, a Download button and "Saved to this chat's Files" |
| `failed` / `expired` | What happened, in a sentence, and that asking again will retry. No cost was charged. When the request timed out without an instance, say the renderer could not start and suggest trying later, because asking again right away will most likely wait in the same queue |

- **Cold copy names no number of minutes.** PR-0 measured about 6 minutes once
  an instance exists, but 26 minutes to get one on create and no instance at all
  within 2h48m on scale-from-zero (§4.3). The submit Lambda does not set
  `estimatedReadyAt` while `coldStart` is true, so a cold job's card shows no
  estimate until it reaches a terminal state. Nothing in this design notices
  when the instance arrives; switching to an estimate partway through would
  need a writer that does, which is left out of v1.

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
- **Render speed is 0.52× real time and flat with length** (PR-0, SDPA, one L40S):
  0.517–0.522 across every render from 2 to 18 minutes of audio. FlashAttention 2
  was slower, at 0.608–0.614, so the figure is not an upper bound waiting on
  a faster kernel.
- **Pre-check estimate** (§5.1): `wordCount / 3.3 × 0.52 × rate / 3600 × 1.5`.
  It uses the slowest two-speaker speaking rate and the render speed PR-0
  measured, with a 50% margin. A 3,600-word script estimates at $0.62.

| Script (PR-0 renders) | Audio | Render | Charged |
|---|---|---|---|
| Bake-off script (466 words) | 2.3 min | 73.4 s | $0.053 |
| 2,018 words | 9.9 min | 305.8 s | $0.221 |
| 2,992 words | 13.6 min | 420.9 s | $0.305 |
| 3,678 words (≈ v1 cap) | 18.0 min | 558.2 s | $0.404 |

The overhead per cold cycle is the time from host boot to serving (4m59s,
$0.22, measured), plus 15 idle minutes ($0.65). PR-0 does not show whether
SageMaker bills the time it spends waiting for capacity before the host boots.
The monthly reconciliation against the endpoint's instance-hours will show it.

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
  tool call's input. The 3,600-word cap bounds it at about 5k tokens, read from
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

1. **PR-0, renderer spike (no platform change). Done 2026-11-03; results in
   Appendix B.**
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
6. **Cold starts in prod:** PR-0 found that getting an `ml.g6e.xlarge` from zero
   took 26 minutes once and failed for over 2 hours the other time (§4.3). The
   options:
   - A keep-warm schedule. About $573 a month for working hours, and a scale-in
     outside those hours meets the same capacity problem the next morning.
   - A second instance type or region as a fallback.
   - Accepting long waits for a feature whose output lands in Files anyway.

   The choice is per deployment and needs real usage. PR-1 can make the
   schedule a config knob.
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
- **MOSS-TTSD ran without FlashAttention** (it fell back to SDPA). PR-0 later
  found FlashAttention 2 slower on this card (Appendix B), so SDPA stays.
- **The house voices** were two ~5.5 s clips MOSS-TTSD generated with no
  reference. Both open models cloned them.
- **Excluded:** VibeVoice (Microsoft withdrew the TTS code), Fish Audio S2 Pro
  (research licence) and Higgs Audio v3 (non-commercial).

## Appendix B: PR-0 renderer spike, 2026-11-03

Everything ran in dev, us-west-2:

- Renders and scoring on one EC2 g6e.xlarge (NVIDIA L40S, 46,068 MiB, driver
  595), from the same weights the endpoint read.
- The FlashAttention wheel and the endpoint image were built on a c7i.8xlarge.
- The cold-start measurement used a SageMaker async endpoint on `ml.g6e.xlarge`.

All of it was torn down afterwards. The scripts that produced these numbers are in
[`spoken-audio-generation/pr0-spike/`](spoken-audio-generation/pr0-spike/).

**Setup.**

- Model revisions:
  - MOSS-TTSD v1.0 `c7cd852d87aff71cab5bd2b9b05509cedc0ef1ba`
  - MOSS-Audio-Tokenizer `3cd226ba2947efa357ef453bcad111b6eafba782`
  - The MOSS-TTSD repository's `generation_utils.py` at `46973e425d`
- Rendering: BF16, `voice_clone_and_continuation` against the house voices,
  temperature 1.1, top-p 0.9, top-k 50, repetition penalty 1.1, seed 42.
- Scoring:
  - WER is Whisper `medium.en` against the script.
  - Voice hold aligns Whisper's word timestamps to the script's turns. It embeds
    every turn of at least 2 s with WavLM base-plus-sv and attributes the turn
    to the most similar house voice in that script.
  - "Similarity" is the mean cosine to the right voice. "Margin" is how far
    that beats the closest other voice.
- **Comparing WER with Appendix A:** this scorer gives the bake-off script
  0.017, against Appendix A's 0.050 for the same arm. The scorer's text
  normalisation is the likely cause; Polly's 0.048 floor in Appendix A points
  the same way. Compare WERs within this appendix, not across the two.

### B.1 Long-form and speaker-count renders (SDPA)

| Script | Speakers | Words | Audio | Render | × real time | Peak GPU (alloc / reserved) | WER | WER by third |
|---|---|---|---|---|---|---|---|---|
| Bake-off | 2 | 466 | 140.7 s | 73.4 s | 0.522 | 24.8 / 25.4 GB | 0.017 | 0.019 / 0.006 / 0.025 |
| Narrator | 1 | 409 | 107.4 s | 55.9 s | 0.521 | 24.6 GB | 0.000 | — |
| Panel | 5 | 472 | 133.8 s | 69.7 s | 0.521 | 25.3 GB | 0.002 | — |
| ~5 min | 2 | 1,004 | 272.6 s | 141.4 s | 0.519 | 25.4 / 26.4 GB | 0.006 | — |
| ~10 min | 2 | 2,018 | 591.2 s | 305.8 s | 0.517 | 26.4 / 27.4 GB | 0.005 | 0.010 / 0.000 / 0.006 |
| ~15 min | 2 | 2,992 | 814.2 s | 420.9 s | 0.517 | 27.4 / 28.8 GB | 0.004 | 0.005 / 0.004 / 0.004 |
| ~18 min | 2 | 3,678 | 1,078.9 s | 558.2 s | 0.517 | 28.1 / 30.4 GB | 0.008 | 0.008 / 0.008 / 0.007 |

**Voice hold.** No render attributed any scored turn to the wrong voice.

| Script | Turns scored | Similarity by third | Margin by third |
|---|---|---|---|
| Bake-off | 17 | 0.961 / 0.949 / 0.967 | 0.228 / 0.217 / 0.229 |
| Narrator | 9 | 0.971 / 0.961 / 0.958 | — |
| Panel (5 voices) | 16 | 0.957 / 0.955 / 0.945 | 0.191 / 0.226 / 0.161 |
| ~5 min | 28 | 0.966 / 0.965 / 0.958 | 0.259 / 0.243 / 0.233 |
| ~10 min | 59 | 0.966 / 0.960 / 0.961 | 0.225 / 0.219 / 0.219 |
| ~15 min | 86 | 0.964 / 0.960 / 0.961 | 0.252 / 0.244 / 0.250 |
| ~18 min | 109 | 0.966 / 0.965 / 0.960 | 0.207 / 0.217 / 0.209 |

- **No late-render drift.** In every render, the last third's similarity and
  margin are within about 0.01 of the first third's, and its WER is no worse.
- **One pass, no chunking.** No render reached its token cap.
- **Render speed and memory with length.** Render speed did not change with
  length. Peak memory grew about 1 GB per 1,000 words above the weights' 23.9 GB.
- **Speaking rate** was 3.3–3.8 words per second.

### B.2 FlashAttention 2

- **Build:** FlashAttention `v2.8.3.post2` built from source for
  `torch==2.9.1+cu128` / cp312, with `FLASH_ATTN_CUDA_ARCHS=80`. That code
  runs on the L40S's sm_89. The build took 1,178 s on a c7i.8xlarge with
  `MAX_JOBS=10` and produced a 59.6 MB wheel.
- **The first run was a silent no-op.** Its WAVs were byte-identical to the SDPA
  run. transformers 5.0 had applied `attn_implementation="flash_attention_2"` to
  the outer `MossTTSDelayModel` config only, so all 36 Qwen3 attention layers
  stayed on SDPA. The second run set the backbone's
  `config._attn_implementation` too and asserted that every attention layer
  reported it.

| Script | SDPA × real time | FlashAttention 2 × real time | FA2 WER | FA2 turns misattributed |
|---|---|---|---|---|
| Bake-off | 0.522 | 0.614 | 0.015 | 0 / 20 |
| ~5 min | 0.519 | 0.608 | 0.005 | 0 / 29 |
| ~10 min | 0.517 | 0.608 | 0.004 | 0 / 57 |

- **FlashAttention 2 was about 17% slower and saved no memory.** Peak GPU
  memory matched SDPA. Quality was the same within noise. SDPA stays.

### B.3 House voices

- **Seeds:** S1 and S2 are the bake-off's voices, from seeds 7 and 11. Their
  re-mints matched the bake-off clips (cosine 1.000 to 3 dp, same length).
- **Minting S3–S5:** each was minted under seeds 13, 17, 19, 23, 29 and 31 with
  no reference audio.
- **Selection:**
  - Candidates Whisper could not read back word for word were dropped:
    S4 seeds 17 and 23, and S5 seed 23.
  - S3, S4 and S5 were then chosen in turn. For each, the candidate least
    similar to the voices already chosen was taken.

| Voice | Seed | Clip |
|---|---|---|
| S1 | 7 | 5.6 s |
| S2 | 11 | 5.4 s |
| S3 | 13 | 5.9 s |
| S4 | 31 | 5.1 s |
| S5 | 19 | 5.4 s |

Pairwise similarity of the clips:

| | S2 | S3 | S4 | S5 |
|---|---|---|---|---|
| **S1** | 0.756 | 0.639 | 0.563 | 0.274 |
| **S2** | | 0.372 | 0.696 | 0.672 |
| **S3** | | | 0.641 | 0.165 |
| **S4** | | | | 0.632 |

- **The voices stay apart in a render, even the closest pair.** S1 and S2 are
  the most similar clips (0.756), yet in the panel render every turn still
  matched its own voice best, by a margin of 0.16–0.23 per third.
- **No real recordings.** None of MOSS-TTSD's bundled reference clips was used.

### B.4 SageMaker async cold start

**Endpoint configuration.**

- The PR-0 image (`python:3.12-slim` + ffmpeg + the pinned stack): 12.7 GB
  unpacked, 4.5 GB compressed. Docker build 331 s, push to ECR 66 s.
- Weights: an uncompressed `S3Prefix` of 38 files, 23.8 GB.
- Instance and AMI: `ml.g6e.xlarge`, `InferenceAmiVersion
  al2023-ami-sagemaker-inference-gpu-4-1`, `MaxConcurrentInvocationsPerInstance 1`.
- **Manifest format:** SageMaker rejected the image as pushed, both as an OCI
  image index and as an OCI image manifest. The manifest had to be rewritten
  as Docker v2 schema 2.

**Where each timestamp came from.**

- The container's own log lines: the host boot time from `/proc/stat`, the
  modification-time window of the files SageMaker wrote to `/opt/ml/model`, and
  each startup stage.
- DescribeEndpoint, polled every 10 s.
- The backlog alarm's history and the Application Auto Scaling activities.

**Endpoint create (InitialInstanceCount 1):**

| Stage | Time | Elapsed from create |
|---|---|---|
| `CreateEndpoint` called | 00:13:06.6Z | 0 |
| Host booted | 00:38:50Z | 25m43s |
| First weight file written | 00:39:55Z | 26m48s |
| Last weight file written (2m47s of copying) | 00:42:42Z | 29m36s |
| Container process started | 00:43:14.7Z | 30m08s |
| torch imported, CUDA up | 00:43:18.6Z | 30m12s |
| Model loaded (20.6 s, 23.9 GB on GPU) | 00:43:46.5Z | 30m40s |
| Warm-up render done (2.2 s), `/ping` healthy | 00:43:48.7Z | 30m42s |
| DescribeEndpoint shows `InService` | 00:44:59.5Z | 31m53s |

- **Host boot to serving took 4m59s.** The image pull is not separately
  visible: it fits inside the same window, which also holds the weight copy.

**Warm render on the endpoint.** The bake-off script was queued at 00:45:23.6Z,
and the container received it 0.1 s later. Its manifest reported:

- 145.7 s of audio
- 73.5 s of generation
- 76.6 s of render, including MP3 encoding
- 24.8 GB peak GPU memory

The output and the 2.3 MB MP3 landed in S3 at 00:46:41Z, about 77 s end to end.

**Scale to zero.** `DesiredInstanceCount 0` was set at 00:47:08Z. The instance
count read 0 by 00:47:39Z, and the endpoint was back to `InService` at 0/0 by
00:48:28Z.

**Scale from zero (step scaling on `HasBacklogWithoutCapacity`, values from the
SageMaker async autoscaling guide: 60 s period, 2 of 2 datapoints):**

| Event | Time | Elapsed from queue |
|---|---|---|
| Request queued | 00:48:54.4Z | 0 |
| Alarm `INSUFFICIENT_DATA` → `OK` | 00:50:17.7Z | 1m23s |
| Alarm `OK` → `ALARM`; scaling activity starts, desired 0 → 1 | 00:53:17.7Z | 4m23s |
| DescribeEndpoint: `InService`, 0 of 1, "Unable to provision all the requested ML compute capacity due to InsufficientInstanceCapacity error" | by 01:28:24Z | ≤ 39m30s |
| Scaling activity ends `Unfulfilled` | 02:53:47Z | 2h04m53s |
| Still 0 of 1, nothing in `failures/`; torn down | 03:41:55Z | 2h53m01s |

- **The one scale-from-zero attempt never got an instance.** The alarm took
  4m23s to act on a queued request. After that, the wait was entirely
  SageMaker looking for `ml.g6e.xlarge` capacity, which it did not find in
  nearly three hours.
- **A second cycle was not attempted.** With no capacity available, it would
  have measured the same queue.
- **The request TTL was left at its default.** The request was still queued
  when the endpoint was deleted.
