# Managed KB Migration — Handoff

**Last updated:** 2026-08-25 (groups 11–13) · **Branch:** `feature/kb-migration` · **Nothing deployed**

Working state for this feature so a fresh session can pick it up without re-deriving
anything. Read this, then `tasks.md`.

---

## 1. Status

| | |
|---|---|
| Spec | Complete, audited 3× to clean. 25 requirements, 201 criteria, 0 dangling refs |
| Implementation | Groups **1–13 of 15** done. 10 subtasks left across groups 14–15 |
| Tests | **614** infra (jest) · **6,457** backend (pytest) · 5 pre-existing unrelated failures |
| Deployed | **Nothing.** No `cdk deploy`, no AWS mutation, at any point |
| Feature flags | All three ship **off** |

### Commits (12 on the branch)

```
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

### Is the feature reachable yet?

**No, and deliberately so.** Verified: nothing calls the provisioning saga, no route
enrolls a knowledge base, and the resolver has only `s3vectors` registered. A record
saying `retrievalEngine: "managed"` raises `BackendUnavailable` — which is the correct
fail-safe, since substituting the legacy index for a promoted KB would serve a stale
corpus. Registering the managed backend and adding an enrollment surface are group 14.
The dispatcher and worker exist but the migration flag ships off, so a tick
invokes nothing and no record is ever enrolled.

The dual-read pilot inherits that: `start_managed_read` returns `None` whenever no
managed backend is registered, so setting `dualReadPilot: true` on a record today is
harmless.

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
cd backend && uv run python -m pytest tests/ -q     # ~6 min, 6301 passing
cd backend && uv run ruff check <your files>
```

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

## 6. Remaining work

| Group | Subtasks | Notes |
|---|---|---|
| **14** Surfaces, observability, teardown | 7 | UI, admin surface, metrics, teardown script. Also where the managed backend gets **registered** in the resolver, and where the dual-read pilot flag gets a surface that can set it. |
| **15** Pre-promotion verification | 3 | The gate before any real traffic moves. |

### Known deferrals (correct, not oversights)

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
