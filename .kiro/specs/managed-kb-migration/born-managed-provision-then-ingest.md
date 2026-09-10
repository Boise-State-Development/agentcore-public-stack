# Born-managed: provision-then-ingest on first upload

**Status:** Draft (supersedes the enroll-reuse design in `new-default-wiring.md`) ·
**Part of:** `managed-kb-migration` (rollout ladder step 2) ·
**Supersedes the approach in:** PR #1027 (keeps its flag reader + infra wiring)

## Decision

`MANAGED_KB_NEW_DEFAULT` makes new agents born managed. Provisioning is stacked
onto the **first document upload** (not agent creation, not save):

- Prompt-only agents and abandoned drafts never provision a KB → kind to the
  ~10,000-KB/account quota.
- The agent-creation page's playground (left = form, right = test) uploads docs to
  the **draft**; provisioning at first upload means what you test **is** the
  managed engine you ship — WYSIWYG.
- The cost — a one-time "provisioning" delay on the first doc — is surfaced as a
  status, not hidden.

This is a **provision-then-ingest** flow, NOT the upgrade/migrate reuse. Reusing
`enroll` writes `retrievalEngine=managed` only at promotion, so the first doc would
be grabbed by the legacy pipeline (verified: the legacy handler skips a doc only
when the agent is already promoted), tested on legacy, and double-ingested. We
instead mark the KB managed up front so the first doc goes straight to managed.

## Status vocabulary (extends task 16.4)

Add one leading DOC# status: **`provisioning`** → label **"Provisioning knowledge
base…"**. Full managed lifecycle for the first doc becomes:

```
provisioning → uploading → (Processing) → complete    [ or → failed ]
```

Only the doc that triggers KB creation shows `provisioning`. Subsequent docs (KB
already active) start at `uploading` as today. Frontend `statusLabel`
(`KnowledgeBaseSectionComponent`) gains the one new case.

## The flow

### 1. First upload trigger — `documents/routes.py:generate_upload_url_endpoint`
When `new_default_enabled()` AND the agent has **no KB_Record** (first doc):
- Create the KB_Record with `retrievalEngine=managed` and
  `provisioningState=provisioning`, atomically/idempotently
  (`create_provisioning`; a concurrent first upload loses the race benignly).
- Persist the DOC# row with status `provisioning` (instead of `uploading`).
- Enqueue a **born-managed provisioning job** for the worker (a work key /
  dispatcher pickup — reuse the existing sparse-index + lease machinery; do NOT
  run minutes-long provisioning in the API request).
- Issue the presigned URL as normal.

Setting `retrievalEngine=managed` up front makes the **legacy pipeline skip** the
doc (it only ingests non-promoted agents).

### 2. The doc lands before the KB exists — managed consumer must DEFER, not fail
The S3 event fires within seconds; provisioning takes minutes. Today the managed
consumer raises `IngestionRoutingError("not provisioned")` when
`retrievalEngine=managed` but `awsKbId` is absent → 2 redeliveries → dead-letter.

**Change:** when the record is `provisioningState=provisioning` (managed intent,
KB not yet built), the consumer returns a benign **"deferred — provisioner owns
this"** result and leaves the DOC# in `provisioning`. It neither ingests nor
dead-letters. The born-managed job (below) owns ingestion once the KB is ready.

### 3. Born-managed provisioning job (worker) — owns the handoff
The robust place for minutes-long provisioning (leases, dispatcher retries, not
EventBridge-capped). On pickup:
- `provision_managed_kb(assistant_id, owner_user_id=...)` → KB ACTIVE, data source
  created, `awsKbId`/`awsDataSourceId` set, `provisioningState=active`.
- List DOC# rows still in `provisioning` and ingest each **directly** into managed
  (reuse the ingestion consumer's ingest→wait-indexed→wait-retrievable→terminal
  logic; drive `provisioning → uploading → complete`). Byte cap is enforced here
  via the same S3-HEAD reconcile as PR #1019 (the first doc is never request-time
  capped because the KB wasn't active at upload).
- The job owns the ingestion trigger, so correctness never depends on the 2-try S3
  redelivery window.

### 4. Subsequent uploads — plain managed path
Once `provisioningState=active` and `retrievalEngine=managed`, further uploads are
ordinary managed: legacy skips, the managed consumer ingests on the S3 event,
request-time byte cap (PR #1019) applies, statuses start at `uploading`.

## Failure & rollback
- **Provisioning fails.** Do NOT leave the agent stranded (managed intent, no KB →
  legacy skips it, managed can't serve). **Remove `retrievalEngine`** (back to
  legacy-by-absence) and mark the `provisioning` doc `failed` with an actionable
  message ("couldn't prepare the knowledge base; try again"). The agent then works
  on legacy and a re-upload takes the legacy path — safe fallback, no dead end.
- **Job crashes mid-provision.** Idempotent: `provision_managed_kb` resumes via its
  persisted `clientToken`; the dead-letter/doc reconciler (16.5) is the backstop
  for a doc stuck in `provisioning` past a grace window.
- **Agent deleted mid-provision.** Provisioning/ingestion writes are conditional on
  the record existing; teardown removes the KB (task 14.6 tag-scoped teardown).

## What PR #1027 keeps vs replaces
- **Keep:** `new_default_enabled()` (flag reader); the app-api `MANAGED_KB_NEW_DEFAULT`
  env wiring + infra test; the flag-read backend tests.
- **Replace:** `maybe_enroll_new_default` (enroll-reuse) and the two finalize hooks
  in `assistants/routes.py` → the first-upload trigger + the provisioning job.

## Tasks
- [x] 1. First-upload trigger in `generate_upload_url_endpoint`: managed-intent
      record + `provisioning` DOC# status + enqueue the job (gated on
      `new_default_enabled()` and no existing record).
- [x] 2. New `provisioning` DOC# status; frontend `statusLabel` case
      ("Provisioning knowledge base…").
- [x] 3. Managed ingestion consumer: DEFER (benign no-op, no dead-letter) when
      `provisioningState=provisioning`.
- [x] 4. Born-managed provisioning worker job: provision → set active →
      ingest pending `provisioning` docs → drive to terminal; idempotent, leased.
- [x] 5. Failure path: provisioning failure removes `retrievalEngine` (legacy
      fallback) + marks the doc failed; grace-window reconcile backstop.
- [x] 6. Remove the enroll-reuse helper + finalize hooks from #1027.
- [x] 7. Tests: first-upload provisions managed-intent + `provisioning` status;
      legacy skips a provisioning-intent doc; consumer defers (does not
      dead-letter) while provisioning; job provisions + ingests to complete;
      byte cap enforced on the first doc; provisioning-failure falls back to
      legacy + marks doc failed. Mutation guards: dropping the consumer DEFER
      dead-letters the first doc; dropping the failure-path rollback strands the
      agent.
- [ ] 8. (Turn-on, later) Bedrock KB-count quota headroom before enabling the flag.

## As built

Decisions taken during implementation that the design above did not fix.

**The job is a new migration state, `born_managed`.** The spec asked for "a work
key / dispatcher pickup — reuse the existing sparse-index + lease machinery", and
the cheapest way to get exactly that is a fourth work-eligible value in
`migrationState`. It borrows the column and inherits the queue, the lease, the
generation fence and the retry-until-terminal contract without a second dispatcher.
It is deliberately NOT spelled `provisioning`: that string is already the
`provisioningState` value, and two attributes carrying one word with two meanings
is how the wrong one gets read. Terminal state on success is `retain` — the
existing "ended up on managed" terminal — with `retainUntil` left unset, because
born-managed has no legacy vectors to retain.

**The dispatcher gates each work state on its own flag.** `born_managed` answers
to `MANAGED_KB_NEW_DEFAULT`, the migration states to
`MANAGED_KB_MIGRATION_ENABLED`, and the EventBridge rule is enabled by either. Two
alternatives were rejected: gating born-managed on the migration flag would have
made ladder step 2 useless alone (the trigger would queue jobs nothing picked up,
parking every first upload on "Provisioning…" forever), and enabling all states
from either flag would have turned step 2 into a back door for step 3's blast
radius. `_work_states()` (ordering) and `_enabled_work_states()` (flags) are
separate functions because a single one doing both cannot be tested for either.

**The first document IS byte-capped at request time.** The design implies the
first document escapes the request-time reserve because the KB is not active yet.
It must not: the ingestion reconcile *commits* the reservation (`reservedBytes -=
n`), so a document that committed without reserving drives the counter negative and
corrupts the cap permanently. The trigger runs before the reserve and the record it
writes already resolves to managed, so the existing `_reserve_managed_upload` path
covers it unchanged. The failure path returns those reservations via
`settle_once`.

**Ingestion reuses `ingestion_consumer.handle_object` outright** rather than
reimplementing ingest → wait-indexed → wait-retrievable → terminal. That function
is where §5.37, §5.38 and §5.39 are encoded, plus the Requirement 12.3 S3-HEAD
reconcile; a second copy would be a second place to forget them. The job moves the
row `provisioning → uploading` first, then calls it, so by the time it reads the
record `provisioningState` is `active` and it takes the ordinary managed path.

**One document per invocation.** The worker's timeout is 15 min and one document's
indexing budget is already 10.5, so a second could not finish. Anything left over
re-arms the work key. More than one pending document only happens when the author
uploaded again during the provisioning window.

**Failure ordering is documents → engine → terminal state.** Documents are failed
while the record still says managed (the state in which they are unambiguously this
job's to resolve), then `retrievalEngine` is REMOVEd, then the work keys go. A
crash between any two leaves the work keys in place, so the dispatcher re-runs the
job and its engine check closes out the "already rolled back" case. Terminal-first
is the one ordering that would strand the agent, so it is the one ordering avoided.

**`provisioning` is a document-reconciler candidate.** Safe because the sweep
already skips any record with no `awsKbId`, so a document is never probed against a
knowledge base that does not exist; once one does, a document still parked past the
grace window probes `NOT_FOUND` and is re-ingested from S3.

### Known cost, not yet addressed
Pickup latency is up to one dispatcher interval (15 min) before provisioning even
starts, so a first upload can read "Provisioning knowledge base…" for that long
before the real 47–124 s create begins. The fix is a direct async worker invoke
from the API alongside the work key (which stays as the durable anchor), and it was
left out on purpose: app-api is an ECS Fargate service, so it needs a task-role
`lambda:InvokeFunction` grant and a container env var threaded through the app-api
construct — real plumbing that does not belong in the same change as the flow
itself. Worth doing before the flag is turned on for anyone who cares about the
first-upload experience.

## Successor design: Lambda durable functions

Recorded because the orchestration choice here was deliberate and the better option
is now available. **Nothing below is a criticism of what shipped** — it is where to
aim if the migration engine is ever rebuilt, so the next person does not
re-derive it.

### The mechanism this feature actually needed
Strip born-managed to its essentials and it is one linear sequence with two slow
waits in the middle:

```
declare the record managed → create the KB → wait for ACTIVE
→ ingest the document → wait for INDEXED → wait for retrievable → complete
                                        ↘ on any failure → roll back to legacy
```

Everything else in this change is scaffolding to make that sequence survive a
process that can die at any point: the `born_managed` work state, the sparse work
keys, the lease, the 15-minute dispatcher tick, the consumer's DEFER, the
one-document-per-invocation cap, and the re-arm. All of it exists because a Lambda
is killed at 15 minutes and an S3 event gets 3 delivery attempts.

### Why durable functions fit better than Step Functions
Step Functions was considered and rejected on two grounds (see the conversation on
PR #1027): it would be the **only** state machine in the stack — a new AWS service
for every fork maintainer to learn — and it moves the saga out of Python into ASL,
which would forfeit `tests/property/test_pbt_kb_migration_convergence.py`, the
crash-at-every-step property test that already caught a double-promotion bug.

Lambda durable functions (re:Invent 2025; Python supported) avoid both. A durable
function **is** a normal Lambda with a `DurableConfig`, so no new service enters the
stack, and the sequence stays as ordinary Python — the SDK adds `context.step()`,
`context.wait()`, `context.waitForCondition()`, `map()`, `parallel()`. Completed
steps are checkpointed; on failure or resume Lambda replays the handler from the top
and skips them. Waits suspend for up to a year and incur **no duration charge** on
on-demand functions.

`waitForCondition()` — pause until a supplied check function passes — is a direct
replacement for all three of this feature's hand-rolled poll loops
(`_wait_for_knowledge_base_active`, `wait_until_indexed`,
`wait_until_retrievable`), and it deletes the reason the consumer currently burns
billed Lambda time asleep.

### What it would delete
The dispatcher Lambda; `GSI7_PK`/`GSI7_SK` and the work-key invariant;
`acquire_lease`/`migrationLeaseUntil`/`LeaseLost`; `defer_verify` and
`verifyAttempts`; `dispatch_limit` and the priority ordering in `_work_states`;
`MAX_DOCUMENTS_PER_INVOCATION` and the re-arm; the consumer's DEFER branch and the
`born_managed` state itself; the 15-minute pickup latency; and most of the document
reconciler, which exists because events dead-letter and durable executions do not.
`retain` becomes a single 30-day `wait()` instead of a stored `retainUntil` plus a
nightly job to notice it.

### What survives any rewrite
Everything that makes the *writes* safe, as opposed to the orchestration:
`resolve_engine`'s absence-means-legacy default, the conditional-write guards
(`adopt_managed_engine`, `promote_engine`'s four guards, `attach_aws_ids`), the
persisted `clientToken` that stops a retry creating a second knowledge base, and
`byte_cap.settle_once`. Durability guarantees the sequence resumes; it does not make
an individual AWS or DynamoDB call idempotent. **And the first-upload trigger order
survives unchanged**: the engine must still be declared before the object lands, or
the legacy pipeline takes the first document.

### Costs, which is why this is not a follow-up ticket yet
- **An existing function cannot be converted.** AWS is explicit that
  `DurableConfig` cannot be added to a function created without it, so this is a new
  Lambda plus a cutover, not a flag flip on the worker.
- **It forces a deploy-pipeline change.** Durable functions must be invoked by a
  qualified (version/alias) ARN so replays run the same code. `backend.yml`
  currently pushes images onto `$LATEST` via `update-function-code --image-uri`.
- **Replay determinism is a real footgun.** The handler re-runs from the top on
  every resume, so anything outside a `step()` executes again. Code that reads a
  record at the top and branches on its state needs deliberate care, and the bugs
  only appear after a crash.
- **Young, and a public-repo dependency.** GA'd through 2026, AWS describes the SDKs
  as fast-moving, and fork maintainers inherit the SDK plus an IAM policy
  (`AWSLambdaBasicDurableExecutionRole`). Confirm region availability for this
  deployment's region before planning.

### Recommended first move
Not a rewrite. A throwaway spike of **born-managed alone** as a durable function —
it is the smallest complete instance of the pattern — to find out whether replay
determinism is pleasant or nasty against this codebase's read-record-then-branch
style. That answer decides whether the engine-wide rewrite is real. If it is, the
order is: born-managed first (no legacy corpus, lowest blast radius), then
shadow→verify→promote.

## Risks
- **KB-count quota** (~10k/account, one KB per agent) still gates turning the flag
  ON — unchanged by this design; first-doc provisioning at least bounds it to
  RAG-using agents, not all agents.
- **New leading status** touches the frontend status map and any status-set
  invariants (e.g. the fail-closed status filter) — keep `provisioning`
  non-terminal and non-retrievable so it can never serve content.
