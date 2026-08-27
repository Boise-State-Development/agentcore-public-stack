# Managed KB Migration — Handoff

**Last updated:** 2026-08-26 (groups 11–13, group 14 backend half, tag contract, **14.3 upgrade UX + enrolment surface**) · **Branch:** `feature/kb-migration` · **Nothing deployed**

Working state for this feature so a fresh session can pick it up without re-deriving
anything. Read this, then `tasks.md`.

---

## 1. Status

| | |
|---|---|
| Spec | Complete, audited 3× to clean. 25 requirements, 201 criteria, 0 dangling refs |
| Implementation | Groups **1–13** done, plus group 14 except 14.5. 14.4's one-click document retry is deferred. 4 subtasks left: 14.4's retry, 14.5, and group 15 |
| Tests | 617 infra (jest) · **6,603** backend (pytest, 6 m 20 s) · **1,886** frontend (vitest, 7 s) · 5 pre-existing unrelated failures |
| Deployed | **Nothing.** No `cdk deploy`, no AWS mutation, at any point |
| Feature flags | `migrationEnabled` **on in development**, off in production. `newDefault` and `reconcilerArmed` off in both (explicit `false`, set as GitHub Environment variables) |

### Commits (16 on the branch, all pushed)

```
45239838  one source of truth for the managed KB tag contract
4acaa8f2  handoff reflects group 14 backend half and three more defects
e59f771c  register the managed backend, fleet metrics, tagged teardown  (group 14 backend)
d5e56f31  handoff reflects group 13 and four more defects
ee091971  migration dispatcher and the shadow/verify/promote/retain worker (group 13)
53476544  handoff reflects groups 11-12 and two new defects
a361fdd4  opt-in dual-read pilot that legacy always wins                (group 12)
a43d80bf  app-side authorization, IAM-enforced sharing, publication      (group 11)
58f0c6b6  handoff document and accurate task-list state
8079f7e2  tombstone deletion sagas and the report-only reconciler        (group 10)
e6936b0b  ingestion consumer with exclusive engine routing              (group 9)
620fa49c  managed KB provisioning, retrieval and direct ingestion        (group 8)
d433d6f1  per-owner byte cap with atomic reserve/commit/release          (group 7)
f2e86afe  clamp retrieval queries and fail closed on status              (groups 5, 6)
24689de1  backend abstraction seam behind the retrieval entry point      (group 4)
ffa7a408  KB_Record data layer with conditional state transitions        (group 3)
5f2c98b1  spec, schema and worker platform                              (groups 1, 2)
```

**Uncommitted working tree:** the 14.3 upgrade surface — `apis/app_api/kb_upgrade/`,
two transitions appended to `kb_backend/records.py`, the Angular card and service,
three test files. See §7 for the file map and §2 for how to run it.

### Is the feature reachable yet?

**Yes, end to end, once the flag is on — except that nothing performs the work.**
Group 14.3 closed the last gap in the *control* path: a user can now enrol a
knowledge base, which writes a `KB#` record in `shadow` with the GSI7 work keys.
Before it, nothing wrote either, so every group could have been finished with the
feature unreachable (§5 defect 21).

What is still missing is the **worker's deployment**, not its code. The dispatcher
and worker are Lambdas behind an undeployed image, so an enrolled record sits in
`shadow` indefinitely and the card shows perpetual progress. That is the correct
local behaviour, not a bug.

**Three behaviour changes ARE live on the existing path** and are the only things
worth testing by hand right now:
1. The document-status filter now **fails closed** (group 6).
2. Retrieval queries are **clamped to 10,000 chars** (group 5).
3. Retrieval requires a resolved access grant (group 11). Both production callers
   pass one; the parameter is required and keyword-only, so a third caller added
   without one fails at the call site rather than silently serving nothing.

---

## 2. Environment

macOS host, tooling installed locally. **There is no devcontainer.**
`.kiro/steering/dev-environment.md` describes a different machine (WSL2/nspawn,
`/home/colin/...` paths) — ignore it here.

```bash
# infrastructure
cd infrastructure && npm run build          # tsc
cd infrastructure && npx jest               # 611 passing

# backend
cd backend && uv run python -m pytest tests/ -q     # 6 m 20 s, 6,603 passing
cd backend && uv run ruff check <your files>

# frontend
cd frontend/ai.client && npx ng test --watch=false   # 1,886 passing, ~7 s
cd frontend/ai.client && npx ng test --watch=false --include="**/kb-upgrade*"
cd frontend/ai.client && npx tsc -p tsconfig.app.json --noEmit
```

There is **no eslint config** in the repo despite the steering docs mentioning
ESLint; `npx eslint` fails with "couldn't find an eslint.config.*". Type-check
with `tsc --noEmit` and build with `ng build` instead.

### Running the upgrade UI locally

```bash
# 1. turn the offer on (LOCAL ONLY — backend/src/.env is gitignored)
echo 'MANAGED_KB_MIGRATION_ENABLED=true' >> backend/src/.env

# 2. app_api on :8000, reading the dev account's DynamoDB via backend/src/.env
cd backend/src/apis/app_api && uv run python main.py

# 3. SPA on :4200 — environment.ts already points at localhost:8000
cd frontend/ai.client && npm run start
```

Then edit an assistant that has documents. Without the flag the card renders
nothing at all, which is correct rather than broken.

⚠️ **The local API writes to the real dev tables.** Enrolling writes a genuine
`KB#{id}` item under `AST#{id}`. Use a throwaway assistant; undo by deleting that
item. The upgrade will sit at "Upgrading…" forever because the worker Lambda is
not deployed — expected, not a bug.

**Baselines that are NOT your fault:**
- 5 backend failures in `tests/agents/main_agent/{session/test_async_persistence.py,streaming/test_cancellation_state.py}` — Strands SDK contract tests looking for `cancel_signal`/`async_mode` that the installed SDK lacks. Pre-existing, unrelated.
- `ruff check src/ tests/` repo-wide reports **369** pre-existing errors in untouched files. Scope ruff to your own files.

---

## 3. Constraints that will bite you

### DynamoDB one-GSI-per-deploy limit ⚠️ RELEASE-BLOCKING

`UpdateTable` permits exactly **one** GSI create/delete per call, and CloudFormation
issues one per changed table. Two indexes on an existing table = failed deploy + full
stack rollback. **This took production down on 2026-08-01 in release 1.12.0.**

This feature's `GSI7` (`KbWorkIndex`) consumes the **entire** `rag-assistants` GSI
budget for whatever release ships it. If another branch adds a GSI to that table, the
two cannot ship together.

Guards: `infrastructure/test/gsi-update-limit.test.ts` (generation) and
`scripts/release/check-gsi-update-limit.mjs` (CI, vs `origin/main`).
Regenerate: `cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit`

### DynamoDB cannot do arithmetic in a ConditionExpression

`storedBytes + reservedBytes + :n <= :cap` is **rejected** (`Cannot parse condition
starting at:+ reserved <= :cap`). The byte cap therefore keeps a single `totalBytes`
accumulator and compares it against a **client-computed literal** (`cap - n`). One
atomic conditional `ADD`, so concurrent reservations cannot collectively overshoot.
See `byte_cap.py`.

### DynamoDB reserved keywords bit this feature twice

`total` and `ttl` are both reserved. Alias via `ExpressionAttributeNames`. The failure
is a `ValidationException`, which is loud — but it also masquerades as a caught
mutation (see §4).

### Import boundary — the reason `kb_backend` exists as its own package

`apis.shared.assistants.__init__` imports `rag_service`, which imports the embeddings
stack at module scope. Pulling that into a Lambda image blows the size budget.

- `kb_backend/__init__.py` is **empty**, deliberately.
- Module-level imports are **stdlib only**; `boto3` and anything heavy is
  function-local.
- Enforced by `backend/tests/architecture/test_kb_backend_boundary.py`.
- `apis.shared.embeddings` is a *separate* package and is fine to use.

### Module constants must be read at call time

Never `def f(timeout=MODULE_CONSTANT)`. Python binds default arguments once at import,
so the constant becomes unpatchable. This cost a 33-second test that silently ignored
its own override. Use `timeout: Optional[float] = None` and resolve inside.

### The knowledge-base component spec needs every collaborator stubbed

`KnowledgeBaseSectionComponent` loads documents, crawls, sync policies and
connectors on hydration. Leave any of those services real and their HTTP requests
stay pending, so `fixture.whenStable()` never settles and **every test in the file
times out at 5 s** with "Test timed out in 5000ms" and no hint as to why. 30 of 31
failed this way before the stubs went in.

`quietCollaborators()` in `knowledge-base-section.component.spec.ts` provides all
six (`DocumentService`, `FileSourceService`, `WebSourceService`,
`SyncPolicyService`, `UserConnectorsService`, `OAuthConsentService`). Note
`OAuthConsentService`'s `completion` and `inFlightProviders` must be **signals**,
not plain values — the component calls them.

---

## 4. Mutation testing — the discipline that has repeatedly paid

Every security- or correctness-relevant assertion in this feature has been verified by
breaking the guard and watching a **specific, correct** test fail. This has caught
**seven** tests that passed with their guard removed. Do not skip it.

**Four ways a mutation lies to you.** All four have happened here:

| Trap | Symptom | Fix |
|---|---|---|
| Anchor never matched | reported "caught", file unchanged | `diff` the file; assert the anchor matches exactly once |
| Orphaned expression values | removing a `ConditionExpression` leaves its values unused → `ValidationException` → the **happy-path** test goes red | strip the orphaned values too, so the write genuinely succeeds |
| Syntax error | collection error mistaken for a detection | `ast.parse`/`py_compile` the mutant |
| Wrong test failed | something failed, but not the guard's test | always check **which** test failed by name |

**Also:** never assert a constant against itself. `assert CAP == module.CAP` is a
tautology that follows the constant wherever it moves. Pin the literal, with a comment
saying why that number is a property of AWS rather than a knob.

---

## 5. Defects found and fixed (do not reintroduce)

### In my own spec

1. **`PutMetricData` on a reserved namespace.** Req 20.10 originally scoped it to
   `AWS/Bedrock/KnowledgeBases`. AWS reserves every namespace beginning with `AWS` and
   rejects writes. The grant would have deployed cleanly and published **nothing**,
   forever. Root cause: conflating *reading* Bedrock's own metrics (genuinely in that
   namespace) with *writing* ours. Now `{projectPrefix}/ManagedKb`; Req 20.13 appended
   for the read grant.
2. **Dead grant on the service role.** Same metric grant was also on the Bedrock
   service role, which Bedrock assumes and which never publishes our metrics. Removed;
   Req 20.10 now says "calling identities only".
3. **`managedKnowledgeBaseConfiguration={}`.** The shape has no *required* members,
   but its only members are the embedding pin and encryption — so a literal `{}` makes
   Req 8.5's pin unsatisfiable. "No required members" ≠ "must be empty".
4. **`float32`** → the enum value is **`FLOAT32`**; lowercase is rejected.
5. **Missing gate §14.3.** Authorization/publication was absent entirely while the
   test matrix demanded tests for it. Added as Requirement 25 → group 11.
6. **Ordering issue:** tasks 4.5/4.7 reference the managed adapter, which task 8.1
   builds. Resolved with a fake backend conforming to the protocol — legitimate, since
   the score conversion is adapter-local and the parity rules belong to the facade.

### In the code

7. **Reconciler arming bypass (MAJOR).** `lambda_handler` forwarded an `armed` field
   from the invocation event, so an EventBridge target with constant
   `{"armed": true}` — or anyone with `lambda:InvokeFunction` — would delete user
   knowledge bases while all reviewable config said report-only. The pre-existing test
   was named `test_an_event_cannot_arm_by_accident` but only covered the *string*
   `"true"`; the boolean that actually armed was untested.
8. **Dispatcher over-grant undetectable.** A test asserted only one statement's shape,
   so Bedrock permissions added in a *separate* statement went unnoticed. Now a
   whole-role whitelist scan.
9. **Inline metadata unbounded** against a 50-attribute limit, and truncation was
   alphabetical — which would have dropped `document_id`, the status filter's join
   key. Reserved keys now go first.
10. **Latent config bug, twice.** `--context managedKb.x=…` sets a **flat dotted**
    key; a nested-only `tryGetContext('managedKb')?.x` read silently ignores it. Hit
    the byte caps and then the alarm thresholds.
11. **`{}` treated as an unreadable record (group 11).** `is_reclaim_exempt` used
    `if not kb_record`, which conflated "absent, so fail closed" with "read, no
    holds set". Every unheld knowledge base would have been exempt and the whole
    predicate vacuous. `None` and `{}` are now distinct. Found by writing the test
    first and believing it over the implementation.
12. **Requirement 25.6 had no IAM behind it (group 11).** There was no
    resource-policy grant anywhere in the construct, so the sharing code would have
    deployed as inert. Same category as defect 1: correct-looking, clean-deploying,
    authorizes nothing. Now `grantManagedKbResourcePolicyAdmin`, on its own role,
    with a test asserting no retrieval identity ever receives it.
13. **A resumed migration re-ingested everything (group 13).** The
    completed-document set lived inside `migrationProgress`, which a later write
    replaces wholesale, so a crash near the end of a 25-document corpus re-parsed
    all 25 — 37–264 s each. Now a separate `migratedDocIds` string set updated with
    `ADD` per batch. Found by the convergence property test counting a document
    ingested twice.
14. **`promote_engine` permitted a second promotion (group 13).** Every guard it
    had stayed true *after* a successful promotion, so two genuinely concurrent
    workers would both succeed — exactly what Req 15.10 forbids. Now guarded on
    `attribute_not_exists(retrievalEngine)`; rollback `REMOVE`s it, so a deliberate
    re-promotion still works.
15. **Fixing 14 then broke resumption (group 13).** A resume after a successful
    promotion had its write refused and marked the migration `failed` — a promoted
    knowledge base with no retention window. `run_promote` now treats "already
    promoted" as success, re-reading before deciding so a genuine guard failure
    still raises.
16. **Four mutation-test lies, in one sitting (group 13).** A limit assertion the
    final `[:limit]` trim masked; a derivation whose test was vacuous because the
    priority list happened to be complete; a `match=` pattern loose enough that the
    *other* check satisfied it; and an `except LeaseLost: raise` that was dead code
    because the lease was taken outside the `try`. Each was fixed rather than
    annotated.

---

17. **Nothing registered the managed backend (group 14).** `register_backend`
    was defined in task 4.2 and called by nothing. All 15 groups could have been
    finished with the feature unreachable — a promoted record raises
    `BackendUnavailable`, a correct fail-safe and a useless signal. Registration is
    now at import, so there is no startup sequence to forget.
18. **A three-defect shell script (group 14).** `scripts/teardown/managed-kb.sh`,
    all three found by *running* it: an infinite spin at a zero poll interval that
    burned sixteen hours of a test run; `list | cut | grep -q` reporting false
    absence when SIGPIPE became the pipeline's status under `pipefail`; and a
    swallowed `list-knowledge-bases` failure reporting a clean teardown having
    deleted nothing. `set -e` is suspended inside a function called in a condition,
    which is why the last one was silent.
19. **Requirement 20.13 existed only as a comment (group 14).** The metrics *read*
    grant was described in a comment explaining the write grant and never
    implemented, so the reconciler could not have read Bedrock's own `Invocations`.

---

20. **The tag contract had drifted three ways (post-group-14).** The Python wrote
    keys `prefix`/`env` from variables the provisioning Lambda never receives; the
    reconciler's filter was a documented *mirror* of that writer; the construct
    declared different key names and exported the correct values as env vars
    **nothing read**; and the teardown script read a third pair. Writer and
    reconciler agreed only because both fell back to the same hardcoded defaults,
    so the sole symptom was a teardown that matched nothing and reported success.
    Now `kb_backend/tags.py` owns the keys and one fallback chain, and
    `tests/supply_chain/test_kb_tag_contract.py` parses the TypeScript and the
    shell script to assert agreement across all three languages.

    ⚠️ **Tag keys are namespaced** (`ManagedKbPrefix`, not `prefix`) because many
    accounts carry an org-wide cost-allocation tag literally called `env`. Note the
    KB_Record *attribute* `appKbId` is a different thing from the AWS *tag*
    `ManagedKbAppKbId`; only the latter belongs to this contract.

---

21. **Nothing enrolled a knowledge base (group 14.3).** The mirror image of
    defect 17, and missed by it. `register_backend` made a promoted record
    *servable*; this is about a record ever reaching `shadow` in the first place.
    The worker only picks up records already in a migration state and the
    dispatcher only sweeps GSI7, so with no enrolment surface both were correct
    and inert. Task 14.3 was written as frontend-only, which is how it hid: the
    missing piece was an **HTTP surface** nobody had scoped. Now
    `apis/app_api/kb_upgrade/`.

22. **A one-put enrolment would have stranded every knowledge base (group 14.3).**
    `KbRecord.to_item` does not write `GSI7_PK`/`GSI7_SK` — only
    `set_migration_state` maintains them. So the obvious enrolment (one
    `put_item` with `migrationState="shadow"`) yields a record that reports an
    upgrade in progress to every surface while being invisible to the dispatcher's
    sweep **forever**: a spinner with nothing behind it, and no error anywhere.
    Enrolment is therefore `create_provisioning` *then* `set_migration_state`,
    both conditional. Two tests pin it, including one asserting the created record
    does **not** carry `migrationState`.

23. **An unrecognised failure reason leaked the operator's string (group 14.3).**
    Found by mutation, not review. `_failure_reason` maps known tokens to
    plain-language copy, and the test proved that for `ByteCapExceeded` — so
    mutating the fallback to `return stored or _FAILURE_FALLBACK` **survived**, and
    a user would have read `ClientError: An error occurred
    (AccessDeniedException)…` in the card. Testing the mapped path proved nothing
    about the unmapped one; that is the whole lesson.
    `test_an_unrecognised_failure_does_not_leak_the_operator_string` pins it.

24. **The upgrade flag never reached the service that reads it (pre-merge).**
    Third instance of this feature's signature failure, and the most nearly
    shipped. `kb-migration-construct.ts` sets all three `MANAGED_KB_*` booleans on
    the four migration Lambdas; `app-api-environment.ts` set the byte caps and the
    metric namespace but **not** `MANAGED_KB_MIGRATION_ENABLED` — which
    `apis/app_api/kb_upgrade/service.py` reads to decide whether to offer the
    upgrade at all.

    Setting the environment variable in GitHub would therefore have changed
    nothing: the card would render `phase: "none"` for every user in every
    environment, forever, with a clean deploy and no log line. Found only by
    tracing where the flag is actually consumed before setting it.

    Now wired, shipped as an explicit `'false'` rather than omitted so the state
    is readable in the task definition, and guarded by three tests in
    `app-api-environment.test.ts`. The mutation — deleting the line, which is
    precisely what the defect was — is caught.

    ⚠️ `managedKb.newDefault` has **no reader anywhere in `backend/src`**. It is
    set on the Lambdas' environment and consumed by nothing, because
    "new knowledge bases are created managed" is a follow-up spec (design §14.7
    steps 5–8), not this phase. Leave it off; turning it on is a no-op that reads
    like a behaviour change.

---

## 6. Remaining work

| Group | Subtasks | Notes |
|---|---|---|
| **14** Surfaces | 1½ | **14.5** admin surface (filter by engine, stored bytes + document counts, bulk migrate, per-KB retry) — not started. **14.4** is surfaced but its one-click document retry is deferred; see the deferral below. 14.0–14.3, 14.6, 14.7 are **done**. |
| **15** Pre-promotion verification | 3 | The gate before any real traffic moves. |

### Known deferrals (correct, not oversights)

- **One-click document reprocess (Req 21.2).** Ingestion is S3-event-triggered
  (`documents/ingestion/handler.py`) and there is **no reprocess endpoint** — the
  only document writes are upload-url, import, upload-failed and delete. A retry
  control therefore needs new backend that re-fires the pipeline against bytes
  already in S3, which is a change to a live ingestion path. Deliberately not
  improvised. The card directs the user to re-upload via "Add files", a retry path
  that works today. **Close by building the endpoint or by amending Req 21.2 to
  accept re-upload** — do not leave it ambiguous.
- **`backend/Dockerfile.kb-migration`** does not exist yet, on purpose. The real image
  needs five artefacts that do not exist: the handler modules, their
  `requirements.txt`, a case in `scripts/build/build-one.sh`, `backend.yml` jobs, and
  entries in the **hand-maintained** lists in
  `backend/tests/supply_chain/test_dockerfile_pinning.py` and
  `test_lambda_image_imports.py`. Per platform-as-bootstrap, CDK ships the bootstrap
  stub and the **workflow** ships the real image.
- **Reconciler EventBridge wiring** (Reqs 14.1, 14.7) — `infrastructure/`, platform
  group. Backend code never deploys before the IAM and resources it requires.
- **Group 7's snapshot reservation now has its caller** (`run_shadow`), reserving
  the whole corpus before anything is provisioned.

---

## 7. File map

```
.kiro/specs/managed-kb-migration/       requirements.md · design.md · tasks.md · HANDOFF.md
docs/specs/bedrock-managed-kb-evaluation.md    the measured source of truth

backend/src/apis/shared/kb_backend/
  __init__.py          EMPTY, deliberately
  records.py           KB_Record + conditional transitions
  protocol.py          KnowledgeBaseBackend + frozen Chunk (score = relevance)
  resolver.py          engine → backend registry; absence ⇒ legacy; load_record
  s3vectors_backend.py legacy adapter; converts distance → relevance HERE
  managed_backend.py   ManagedKbBackend: retrieval + direct ingestion
  provisioning.py      create saga + CUSTOM connector data source
  byte_cap.py          reserve / commit / release
  tombstones.py        delete sagas
  resource_policy.py   IAM-enforced sharing; staleness is state, not an event
  dual_read.py         pilot: start early, detach, compare, serve legacy
  idleness.py          activity = max(retrieval, bound agents' use)
  tags.py              THE tag contract — keys + value resolution, one place
  query_guard.py       10,000-char clamp
  metrics.py           namespace + best-effort emit_count / emit_value

backend/src/apis/shared/assistants/
  rag_service.py       the FACADE — access gate, dual read, status filter, caps
  kb_access.py         KbAccess grant; reuses resolve_assistant_permission
  kb_publication.py    engine swap ≠ corpus change; reclaim exemption

backend/src/apis/app_api/kb_migration/
  ingestion_consumer.py   routes by engine; legacy ⇒ do nothing
  reconciler.py           daily join, report-only
  dispatcher.py           sparse-index sweep, bounded, no-ops when the flag is off
  worker.py               ONE step per invocation, leased, resumable

backend/src/apis/app_api/kb_upgrade/     the OWNER-FACING surface (HTTP only)
  models.py               camelCase wire models; UpgradePhase + DocumentIssueKind
  service.py              phase derivation, enrolment, retry, notice, doc triage
  routes.py               4 endpoints; read is any permission, writes are edit-only
                          NOT in kb_migration/: that package's modules share one
                          size-constrained Lambda image and this one imports the
                          embeddings-pulling assistants package

frontend/ai.client/src/app/knowledge-base/
  kb-upgrade.service.ts             fails soft; getStatus resolves to phase 'none'
  knowledge-base-section.component.*  the card: offer / progress / notice / failure
                                      + the stranded-document disclosure

scripts/teardown/
  managed-kb.sh                   delete tag-matched KBs BEFORE any stack

docs/specs/
  managed-kb-cost-attribution.md  filter on usagetype, never service code alone

infrastructure/lib/constructs/managed-kb/
  managed-kb-role-construct.ts    Bedrock service role + grant methods
  kb-migration-construct.ts       4 Lambdas sharing ONE image + alarms
```

### Where authorization lives, and why not in `kb_backend`

`kb_access` and `kb_publication` sit in `apis.shared.assistants` because they reuse
`resolve_assistant_permission` and `listing.is_on_shelf`, and `kb_backend` may not
import that package. Authorization is above the seam by nature anyway: the answer is
the same whichever engine serves the query, so implementing it once above both
adapters is the only way it cannot differ between them.

The facade's `access` parameter is **required and keyword-only**. Forgetting it is a
`TypeError` at the call site; a genuine denial passes `None` and fails closed. A
`KbAccess` cannot be built with a permission outside the read set, so holding one is
evidence the permission model was consulted — holding a string is not.

Reclaim exemption keys on `listing.is_on_shelf`, **never** `is_listed`: an admin
requesting changes on a live listing leaves it serving but moves its state out of
`LISTED_STATES`, so by state name alone a reclaim pass would delete the corpus behind
an agent users can still see in the store.

### Score direction — the highest-silent-risk detail

S3 Vectors returns cosine **distance** (lower better). Managed returns **relevance**
(higher better). The protocol canonicalizes on `relevance`; `s3vectors_backend`
converts by **exact negation** (order-preserving and losslessly reversible, unlike
`1-d`); the managed adapter applies **no** conversion. The facade still emits a
derived `distance` key so no caller changed.

Invert it and nothing raises — retrieval keeps returning five chunks and the answers
quietly get worse. `tests/property/test_pbt_kb_score_direction.py` is the only guard.

---

## 8. Data model

```
PK = AST#{assistant_id}
SK = METADATA                              # the assistant row (pre-existing)
SK = KB#{app_kb_id}                        # app_kb_id == assistant_id THIS PHASE
SK = KBTOMB#{app_kb_id}                    # whole-KB tombstone, NO TTL
SK = KBTOMB#{app_kb_id}#DOC#{document_id}  # document tombstone, NO TTL

GSI7 "KbWorkIndex" (projection ALL) — sparse
  GSI7_PK = KBWORK#{state}     GSI7_SK = {dueAt ISO-8601}
```

Keys are written **only** while a record is work-eligible and `REMOVE`d on reaching a
terminal state, so ineligible knowledge bases are invisible to the dispatcher **by
physics** rather than by filter. Third use of this convention on this table
(`DueSyncIndex`, `AgentDirectoryIndex`, `AgentReportsIndex`).

**Absence means legacy.** `retrievalEngine` is only ever written as `"managed"`.
Nothing writes `"s3vectors"` onto a record that lacked it — that is what makes the
migration zero-backfill across 1,692 existing records and makes rollback a single
attribute `REMOVE` rather than a data rewrite.

Same convention for two more attributes:

- `dualReadPilot` — read as `is True`, never truthiness. Absence is off.
- `policyAwsKbId` — the `awsKbId` the resource policy was last applied to.
  `policy_is_stale` compares it against the live one, so re-application after a
  replacement identifier is a comparison nothing can bypass by omission.

Source bytes already live at
`assistants/{assistant_id}/documents/{document_id}/{filename}`. Migration is a
**re-ingest**, never a re-upload.
