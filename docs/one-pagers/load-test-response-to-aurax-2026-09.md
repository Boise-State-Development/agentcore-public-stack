# Response to "Five performance fixes, proven by a controlled A/B"

**To:** AuraX platform team
**From:** Boise State AI platform team
**Re:** `AgentCoreSolution-PerformanceTesting-ProductionReadyFixes.pdf`, run 2026-09-15
**Date:** 2026-09-19
**Working assessment:** [`docs/specs/load-test-assessment-2026-09.md`](../specs/load-test-assessment-2026-09.md)

---

## Short version

Thank you for this. Four of your five findings describe real defects in our
mainline, the fifth is a deployment-configuration gap we had left as a warning,
and all three of your stated residuals are correctly identified. We validated
each claim against `develop` before touching anything, then shipped. **All five
findings now have code merged to `develop`**, across five pull requests — items
1 through 4 are verified running in dev, the rest deploy on the next rollout. Two of your items led us somewhere you did not claim: a
long-standing bug in our token-attribution telemetry, and a second layer of the
per-turn rebuild that your fix would only have half-addressed.

Two things in the report do not transfer to our numbers, and one residual is
mis-attributed to the wrong AWS quota. Both are detailed below — neither
changes your conclusion.

Your methodology is the part we would keep. Building both arms from one source
tree and reverting only the five changes, rather than comparing against
mainline, is exactly right given that mainline carries a load-shedding
subsystem your branch does not. We would not have trusted the deltas otherwise.

---

## Status at a glance

| # | Your finding | Our verdict | Shipped as | State |
|---|---|---|---|---|
| 1 | Prompt counted 3× per turn; agent rebuilt each turn | **Confirmed**, plus a second cause you did not reach | #1154, #1155 | Merged · verified in dev |
| 2 | Image ships no compiled bytecode | **Confirmed** | #1154 | Merged · verified in dev |
| 3 | One CountTokens in front of every model stream | **Confirmed**; fixed differently, see below | #1154 | Merged · verified in dev |
| 4 | Uploads bucket has no CORS rule | **Environment gap, not a mainline bug** | #1154 | Merged · verified in dev |
| 5 | Assistant refuses an attached spreadsheet | **Confirmed**; narrower scope, see below | #1158 | Merged · deploy pending |
| R1 | 6 conversation-memory lookups per message | **Confirmed mechanism, wrong count** | #1157, #1155 | Merged · deploy pending |
| R2 | Memory quota 30 lookups/s | **Right number, wrong API** | — | Quota request open |
| R3 | Micro-VM cold start ~12 s | **Plausible, unverified by us** | — | AWS thread open |
| — | *(not claimed)* System-prompt token count was never authoritative | Found while verifying your probe-message detail | #1156 | Merged · deploy pending |

Everything above is merged to `develop`. Items 1–4 are verified running in dev;
5, R1 and the probe fix deploy on the next rollout. Production ships on the
next release; every change carries a kill switch (table at the
end).

---

## 1 · The prompt was counted three times per turn

**Your finding.** The agent is rebuilt each turn and re-splits its context
attribution, costing two extra Bedrock CountTokens calls on top of the SDK's
own. Fix: memoise the split across the per-turn rebuild.

**Our verdict: confirmed, and the rebuild itself had a cause worth fixing.**

We reproduced the mechanism exactly. Our context-attribution hook memoised its
split on the `Agent` *instance*, which is sufficient only while one `Agent`
serves a session for its whole life. It does not, because our agent cache
refused to cache any agent carrying per-request ("injected") tools — a cohort
that was **2,720 of 3,565 prod sessions (76%)** on our last measurement, driven
by the spreadsheet-analysis tools. Those sessions rebuilt the `Agent` every
turn, and with it a fresh session manager, a full `initialize()` and an
AgentCore Memory restore. Your two extra counts rode on top of that.

**What we changed.**

- **Memoised the split by session and configuration**, not by instance — keyed
  on `(session_id, system-prompt digest, tool-spec digest)` in a bounded
  process-level map, so a rebuilt `Agent` adopts its predecessor's measurement
  instead of re-counting ([#1154]).
- **Made the rebuild stop happening.** The agent cache refused these sessions
  because their tool builders close over request scope the cache key did not
  describe. We widened the key rather than the exception: Word, Excel,
  PowerPoint and workspace tools were already describable (they capture only
  session and user) and were promoted ([#1154]); the spreadsheet family also
  captures the assistant id, so we added `assistant_id` to the cache key, the
  construction snapshot and the paused-turn snapshot, and made the resume path
  replay it ([#1155]). Every picker-gated injected tool family is now
  cache-eligible.

That second half matters for your §5 residual too: a cached agent skips
`initialize()`, which is where five of the memory lookups live.

**Dev evidence.** Injected-tool sessions now log `agent_cache outcome=hit` from
their second turn onward, where they previously logged `miss` on every turn.

**What's left.** Confirm the per-turn CountTokens count is ≤ 1 after cold start
under load, in the A/B rerun below.

---

## 2 · Every new chat compiled the app's bytecode

**Your finding.** The image ships no compiled bytecode (a side effect of the
package installer), so each fresh micro-VM compiles the app — and the symbolic
math library behind the calculator tool — when the first message arrives. Fix:
precompile into the image and warm the first turn's residual work at startup.

**Our verdict: confirmed, exactly as described.** Our inference-api Dockerfile
runs `uv sync` without bytecode compilation and copies source raw. The
container runs as a non-root user and every micro-VM starts from the image
layer, so nothing written at runtime survives to the next session — the image
is the only place the bytecode can live.

**What we changed** ([#1154]):

- `UV_COMPILE_BYTECODE=1` for the virtual environment and `compileall` over our
  own source in the final image stage.
- A daemon-thread warm-up at application startup that imports the calculator's
  symbolic-math dependency and the agent-construction modules, and builds one
  client per AWS service so the service models are parsed and cached. It runs
  off the request path; the health endpoint answers immediately, and a request
  arriving mid-warm-up waits on Python's import lock rather than redoing the
  work.

**Dev evidence.** Warm-up completes in **400–550 ms per container**, of which
**350–480 ms is the symbolic-math import** — that is the work that used to land
on the first user's first message, and it is now paid while the runtime is
still coming up.

**What's left.** Nothing. The remaining cold-start time is item R3.

---

## 3 · One token-count call still sat in front of the model

**Your finding.** A single CountTokens call remained on the critical path ahead
of every model stream; under load AWS throttled it before the model began
writing. This is the call whose removal drives the 541 → 0 throttle result.
Fix: run the count as a background side-request that never mutates model
configuration, counting the system prompt against a probe user message.

**Our verdict: confirmed mechanism. We bounded the call rather than
backgrounding it, deliberately.**

The mechanism is exactly as you describe: enabling native token counting makes
the SDK await one CountTokens before every model call, and CountTokens has its
own request-rate quota separate from the model's token quota, so it throttles
first. We would add one detail: the latency is not the call, it is the SDK's
shared client retrying the throttle with backoff before falling back to its
heuristic. The reply waits out that backoff.

**What we changed** ([#1154]):

- **A dedicated CountTokens client with one attempt and a 2-second timeout.** A
  throttle now costs one failed request before the heuristic estimate, instead
  of a retry sequence on the reply's first-token latency. Both values are
  environment-tunable.
- **Removed the model-id mutation.** We suspect this is what your "never
  mutates model configuration" is pointing at, and we think it is the sharpest
  observation in the report. Our subclass swapped `config["model_id"]` to the
  base foundation-model id for the duration of the count and restored it
  afterward — safe *only* while a count and a stream can never overlap on one
  model instance. That invariant dies the moment the count goes concurrent, and
  a stream reading the base id fails on-demand invocation. We now pass the base
  id to the API explicitly and never touch the config.

**Why we did not move it fully off the reply path.** Our proactive compaction
reads the same projected token count. Compaction timing is a prompt-cache cost
contract for us: compacting a turn early or late rewrites a 30k–150k-token
cached prefix at the cache-write premium, which is 1.25× the model's own base
input rate. Dropping the authoritative count on checkpoint-deciding turns trades
a latency win for a cost regression we would not see in a latency-only harness.
The bounded client captures most of the throttle-latency win with none of that
exposure. **We would revisit this if your ledgers show the residual mattered** —
specifically, whether the background count changed compaction frequency in your
arm. That is question 2 at the end.

**What's left.** Measure whether the bound is sufficient under the 35 msg/s
ceiling, or whether the remaining awaited call still shows up in first-token
p95. That is part of the A/B rerun.

---

## 4 · File uploads failed in a real browser

**Your finding.** The uploads bucket had no cross-origin rule, so the browser
blocked every upload. A load-only program would have shipped this bug.

**Our verdict: a deployment-configuration gap, not a mainline defect — and the
gap was ours to make it possible.** Our infrastructure code does attach a CORS
rule to the uploads bucket, but only when a domain or an explicit origin list
is configured. With neither set, it printed a warning to synth output and
created the bucket with no rule. Your environment hit exactly that path.

We want to be clear that this does not diminish the finding. A warning nobody
reads in CloudFormation synth output is not a safeguard, and your point about
load-only testing stands on its own — the load harness uploads outside a
browser, so no amount of load testing would have caught it.

**What we changed** ([#1154]): missing CORS origins now **fail synth** rather
than warn, with an explicit opt-out environment variable for a deployment that
genuinely has no browser front-end. A deployment that would have shipped this
bug no longer deploys.

**What's left.** Nothing on our side. Worth confirming your environment now sets
a domain or origin list, since the guard will stop your next synth otherwise.

---

## 5 · The assistant refused to read an attached spreadsheet

**Your finding.** Attaching a CSV with the default tool set makes the assistant
decline and tell the user to enable a file tool first — a dead end unless they
know the trick. Fix: auto-enable the read-only file tools when a turn carries
an attachment, gated on the caller's existing permissions.

**Our verdict: confirmed, and the dead end was worse than "unhelpful" — it was
instructed.** Spreadsheets never go inline at any size (they route through the
analysis tools to avoid Bedrock's document-size limit), the analysis tools are
opt-in in the picker, and our system prompt contains an explicit clause telling
the model to name the capability and send the user to the sidebar. So the model
was following instructions into a wall.

**What we changed** ([#1158]). A session that holds a spreadsheet — this turn's
attachment, or a ready upload from an earlier turn — gets the Spreadsheet
Analysis tools added to that turn's effective tool set, **gated on the caller's
role grant** through the same predicate the picker and agent bindings use. It
enables; it never grants. We also rewrote the fallback message for the
remaining case (a role that genuinely lacks the tool) so it no longer points at
a sidebar toggle the user may not have.

**Two deliberate narrowings from your version:**

- **Spreadsheets only, not read-only file tools generally.** PDFs, Word, text
  and Markdown attachments already reach the model through a document-read tool
  that is gated on the attachment itself rather than on the picker, so they
  were never in the dead end. Spreadsheets were the gap.
- **Sticky per session.** The answer feeds our agent-cache key, so a gate that
  flipped between the attach turn and the follow-up would bounce the session
  between two cache slots and re-write the cached prefix each time. We memoise
  the positive answer for the session's life.

**What's left.** One open review question we flagged on the PR: the auto-enable
currently also applies on top of an explicit Agent tool binding, on the
reasoning that the user's own attachment is the governing intent. Reasonable
people could want bindings to stay closed. Not urgent; no prod Agent binding
excludes these tools today.

---

## R1 · Conversation-memory lookups per message

**Your finding.** Six conversation-memory lookups per message; platform code;
open, reduce to three.

**Our verdict: the mechanism is real, but "6 per message" conflates two
different APIs charged against two different quotas.** What we measured:

| Operation | API | When | Count |
|---|---|---|---|
| Session and agent restore | `ListEvents` | Cold `initialize()` only | 5 |
| Long-term memory retrieval | `RetrieveMemoryRecords` | **Every user message** | up to 3 |

The five restore calls are per *cold start*, not per message — but for the 76%
cohort in item 1, every turn *was* a cold start, which is very likely how the
two collapsed into one number on your side. Fixing the rebuild ([#1155]) removes
those five from every turn but the first.

The three per-message calls are one per configured memory strategy. The third
was the current session's own conversation summary: AgentCore's summary of the
conversation the model already has in front of it, retrieved and prepended to
every user message. That is input tokens on every turn for information already
in context, plus a lookup against the quota.

**What we changed** ([#1157]):

- **Dropped the summary-namespace retrieval from the per-message path.** The
  compaction path still reads summaries where they actually matter, at
  checkpoint advance. Restorable by environment variable. Lookups per message:
  **3 → 2**.
- **Bounded the retrieval client**, same treatment as CountTokens in item 3 and
  for the same reason: the retrieval hook is awaited before the model call, so
  a throttled lookup put retry backoff on first-token latency. One attempt, a
  2-second timeout, and a turn that simply runs without long-term context —
  which is what a miss already meant. Writes keep their retries; a dropped
  message is a corrupted conversation, a missed retrieval is not.

**What's left.** Your target of three is met and beaten for the steady-state
path. We have not yet reduced the five cold-restore `ListEvents` calls
themselves — they are SDK-internal and would need an upstream change or a
narrower restore. Worth doing only if the A/B shows cold restore still
dominating.

---

## R2 · The memory service quota

**Your finding.** Conversation-memory service quota of 30 lookups/s, an AWS
limit, remedied by a quota request to 300/s; crossed at roughly 5 messages per
second.

**Our verdict: the number is right and the remedy is right, but it applies to a
different API than the residual above implies — and there is a second cap that
no quota increase will move.** From the AgentCore quota tables:

| Quota | Default | Adjustable |
|---|---|---|
| `RetrieveMemoryRecords` per account | 30/s | Yes |
| `ListEvents` per account | 200/s | Yes |
| **`ListEvents` per actor, per session** | **20/s** | **No** |
| `CreateEvent` per account | 200/s | Yes |
| `CreateEvent` per actor/session (conversational) | 5/s | No |

So the 30/s you crossed is the long-term-memory retrieval quota, and raising it
to 300/s is the correct ask. But the restore calls run against `ListEvents`,
whose per-session cap of 20/s is **fixed**. That cap is generous for one user,
and it means per-session lookup *count* is the only lever there — a quota
increase cannot buy it back. This is an argument for the rebuild fix in item 1
being the load-bearing change, not the quota request.

**What's left (ours).** File the `RetrieveMemoryRecords` increase from 30 to
300/s. We deliberately did the code reduction first so the request is sized
against real post-fix traffic rather than pre-fix waste.

---

## R3 · Micro-VM cold start

**Your finding.** Roughly 12 seconds to boot a fresh machine; AWS platform; no
code lever, support request only.

**Our verdict: plausible, and we have not independently measured it.** Our own
controlled probe in August measured a cold first turn end-to-end at 7–8 seconds
in dev, which is not the same quantity — that number includes our own
initialization, and it was measured at a load nothing like yours. We have no
reason to dispute 12 seconds at the ceiling.

We agree there is no code lever, and we agree with your framing of the honest
remedies: an AWS-side warm pool, or a pre-warm-on-new-chat feature that costs
idle capacity. We would add that the warm-up in item 2 removes 400–550 ms that
was previously *inside* this number and attributable to us, so the AWS-side
figure should be measured after that change lands, or it will be overstated.

**What's left (ours).** Open the support thread, with our post-warm-up cold-turn
breakdown attached so the conversation is about AWS boot time and not our
imports.

---

## Not in your report: your probe-message detail found us a bug

Item 3 mentions, almost in passing, that you count the system prompt against a
probe user message because "the model rejects an empty conversation." We went to
verify that, because our own context-attribution code counts the system prompt
with an empty message list. It is true:

```
CountTokens, empty messages + system → ValidationException:
  "A conversation must start with a user message."
```

Our SDK swallows that exception into a characters-divided-by-four heuristic. So
the system-prompt partition of our per-turn context breakdown — which we surface
to users in a context badge and persist on every call's cost row — **has been an
estimate since the day it shipped, presented as an authoritative count.** No
alarm fired, because the fallback is silent by design.

Fixed in [#1156] the way you described: count against a fixed probe user message
and subtract the probe's own weight, which is a per-model constant (24 tokens on
our default model) measured once per process.

We would not have found this from the report's headline numbers. Thank you.

---

## Where our numbers will not match yours

Three caveats, none of which undercut your conclusion — we are recording them so
nobody later cites your table as a prediction for our production fleet.

**Your baseline is worse than our mainline, so the deltas are upper bounds.**
Your branch predates our runtime session-affinity fix from 2026-08-05. Before
that fix, nothing forwarded a runtime session id, AWS assigned a fresh micro-VM
per invocation, and our agent cache therefore never hit *at all* — for any
session, not just the injected-tool cohort. Your baseline arm rebuilt the agent
on every turn for 100% of sessions; our mainline did so for 76%. Expect the
real-world item-1 gain to be large but smaller than 18.2 s → 6.6 s.

**The cost figure should not be reused.** $0.0025 per message at a 99%
prompt-cache hit rate reflects short, highly repetitive synthetic prompts. Our
production write-to-read ratio runs near 1:2, and document conversations alone
are about 31% of spend. We will publish our own post-fix number from the admin
cost drill-down rather than cite yours.

**Your load model checks out.** 15 messages per user per day with 10% in the
busiest hour gives 1 message/s ≈ 2,400 daily users, so 17 msg/s ≈ 40,800 is a
fair readiness target. We will reuse this mapping so our rerun is comparable to
yours.

We also agree with your handling of the two starred success cells. Footnoting
them as ramp-opening cold-start bursts, rather than hiding them or calling them
a load ceiling, is the right call, and the throttle curve carries the
conclusion without them.

---

## Open questions for you

1. **The upstream contribution.** The report says one of the five is already
   contributed back upstream. We searched the Strands SDK for token-counting and
   estimation pull requests and found nothing matching the background
   side-request or probe-message shape. A link would let us drop our local
   version when it lands, rather than carry a divergent fix.
2. **Compaction behaviour in your arm.** When you moved the count off the reply
   path, did proactive compaction fire at a different rate or at different
   checkpoints? This is the one measurement that would move us from bounding the
   call to backgrounding it (see item 3).
3. **The probe string.** Which probe user message did you use? It is a per-model
   constant that shifts every system-prompt figure derived from it, so if we are
   comparing attribution numbers later we should use the same one.
4. **The diff, the ledgers, and the merge base.** The branch or patch set, the
   run ledgers behind the tables, and the commit your branch forked from. The
   fastest path for us is reviewing it as a pull request against our `develop`.

---

## What is left to do

| Item | Owner | State |
|---|---|---|
| Merge all five findings | Us | **Done** — #1154, #1155, #1156, #1157, #1158 on `develop` |
| Rerun the A/B on our harness: 10 and 17 msg/s, 8-minute holds, before/after | Us | Not started; needs the load-test provisioning gate (creates users in the shared pool and suspends cost limits, so it is a deliberate manual step) |
| File `RetrieveMemoryRecords` quota increase, 30 → 300/s | Us | Not filed; sized after the code reduction |
| Open the AWS support thread on micro-VM cold start | Us | Not opened; attach post-warm-up breakdown |
| Production rollout | Us | Next release; all changes carry kill switches |
| Answer the four questions above | AuraX | Open |
| Narrow the picker's tool list through role grants on the chat route | Us | Noticed in passing, not scoped — the schedules and runs routes do this, the chat route does not |
| Make two document-read tests deterministic under parallel workers | Us | In progress — their regex guard is a wall-clock budget that CPU contention trips |

### Kill switches, for your environment

| Change | Variable | Effect |
|---|---|---|
| Startup warm-up | `INFERENCE_WARMUP_ENABLED=false` | Disables warm-up; bytecode precompile stays |
| CountTokens bound | `COUNT_TOKENS_MAX_ATTEMPTS`, `COUNT_TOKENS_TIMEOUT_SECONDS` | Retries and timeout for the count |
| Memory retrieval bound | `MEMORY_RETRIEVAL_MAX_ATTEMPTS`, `MEMORY_RETRIEVAL_TIMEOUT_SECONDS` | Same, for long-term memory |
| Summary namespace | `MEMORY_SUMMARY_NAMESPACE_RETRIEVAL_ENABLED=true` | Restores per-message summary retrieval |
| Spreadsheet auto-enable | `ATTACHMENT_TOOL_AUTOENABLE_ENABLED=false` | Back to picker-only |
| CORS synth guard | `CDK_ALLOW_NO_CORS_ORIGINS=true` | Allows a deployment with no browser front-end |

[#1154]: https://github.com/Boise-State-Development/agentcore-public-stack/pull/1154
[#1155]: https://github.com/Boise-State-Development/agentcore-public-stack/pull/1155
[#1156]: https://github.com/Boise-State-Development/agentcore-public-stack/pull/1156
[#1157]: https://github.com/Boise-State-Development/agentcore-public-stack/pull/1157
[#1158]: https://github.com/Boise-State-Development/agentcore-public-stack/pull/1158
