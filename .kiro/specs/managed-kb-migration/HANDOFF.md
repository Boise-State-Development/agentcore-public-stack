# Managed KB Migration — Handoff

**Last updated:** 2026-09-03 · **Shipped to production in 1.16.0, inert behind flags** ·
**A migration has completed end to end in dev; adding a document to it needed one more IAM action**

Working state for this feature so a fresh session can pick it up without
re-deriving anything. Read this, then `tasks.md`.

---

## 0. Read this first

Four things invalidate earlier versions of this document:

1. **It is deployed.** The feature shipped to production in release 1.16.0 and the
   platform deploy succeeded on 2026-08-28, so `GSI7`, the Bedrock service role and
   the four Lambdas exist in **both** dev and prod. Earlier revisions of this file
   said "Nothing deployed"; that is no longer true.
2. **Sixteen defects were found only by running it**, each reviewed clean and
   deployed clean. They are §5 items 25–41 and they are the most useful part of
   this document. Three clusters: 32–36 trace to the two engines never being made
   exclusive (PR #900); 37–39 to the ingestion consumer never actually knowing when
   a document was ready (PRs #901, #908); and **40–41 are about answer quality and
   are both still OPEN** — the managed backend currently gives a *worse* answer than
   legacy on a question it retrieves *better*. Start there. §5.33 is also open.
3. **The `document_id` "known unknown" was a false alarm** and is now resolved with
   measurements — see §6. An earlier revision listed it as the top open risk. The
   probe was reading facade keys that have never existed. Two genuine findings came
   out of checking it (§5.32, §5.33), both still open.
4. **Iterate locally, but do not trust it for IAM.**
   `scripts/local-dev/run-kb-migration.py` drives the whole state machine
   in-process against dev with your SSO credentials. Three of the
   five defects would have been minutes of work instead of a merge → image build →
   deploy → 15-minute-tick cycle each. Use it.

   But your SSO identity is **broader than every Lambda role**, so the driver is
   structurally blind to IAM gaps — that is how §5.31 shipped after a migration had
   already "completed end to end". Get the logic right locally; prove the
   permissions by deploying and letting the real roles do the work.

---

## 1. Status

| | |
|---|---|
| Spec | Complete. Requirement **8.5 was amended by measurement on 2026-08-31** — see §5.29 |
| Implementation | Groups 1–14 except 14.5. A migration has completed `shadow → verify → promote → retain` in dev and serves from the managed backend |
| Tests | 640 infra (jest) · ~6,840 backend (pytest) · 1,936 frontend (vitest) · 5 pre-existing unrelated Strands failures |
| Deployed | **dev and prod.** Flags off in prod; `migrationEnabled` on in dev |
| Open PRs | **#908** — the filtered retrievability probe (§5.38) and `TEXT_INDEXED` (§5.39), plus this document. · merged: #898 `ef2f4c9e`, #899, #900 `df93471c`, #901 `a4b660ba` |
| Uncommitted | none |

### Flag state (GitHub Environment variables)

| Flag | development | production |
|---|---|---|
| `CDK_MANAGED_KB_MIGRATION_ENABLED` | `true` | `false` |
| `CDK_MANAGED_KB_NEW_DEFAULT` | `false` | `false` |
| `CDK_MANAGED_KB_RECONCILER_ARMED` | `false` | `false` |
| `CDK_TAG_ENVIRONMENT` | `dev` | `prod` |

`newDefault` has **no reader anywhere in `backend/src`** — "new knowledge bases are
created managed" is design §14.7 steps 5–8, a follow-up spec. Setting it does
nothing, which is worth knowing before someone flips it expecting an effect.

⚠️ **Production carries every defect fixed after 1.16.0 shipped.** It cannot fire,
because nothing enrols while `migrationEnabled` is false. Do not turn that flag on
in prod until #898 and the uncommitted work have landed and shipped.

### Commits

**On `fix/kb-legacy-pipeline-engine-gate` — PR #900, open:**

```
e3398f30  propagate document deletion to the managed knowledge base       (§5.36)
8a35dbc6  the legacy pipeline stands down for a promoted knowledge base   (§5.32, §5.34)
```

**Merged as `ef2f4c9e` (was PR #898):**

```
fdf15d21  grant bedrock:StartIngestionJob, which authorizes direct ingestion  (§5.31)
6420f148  drop the embedding pin, defer verify, complete a migration in dev   (§5.29, §5.30)
7542d907  record the knowledge base id before anything else can fail          (§5.28)
```

**Already merged (16, on develop):**

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

**Working tree: clean.** An earlier revision listed the 14.3 upgrade surface and
then the 8.5 amendment as uncommitted; both have landed. `apis/app_api/kb_upgrade/`
is merged — see §7 for the file map and §2 for how to run it.

### Is the feature reachable yet?

**Yes, end to end, once the flag is on — except that nothing performs the work.**
Group 14.3 closed the last gap in the *control* path: a user can now enrol a
knowledge base, which writes a `KB#` record in `shadow` with the GSI7 work keys.
Before it, nothing wrote either, so every group could have been finished with the
feature unreachable (§5 defect 21).

The worker's image **is deployed** — PR #886 shipped `Dockerfile.kb-migration` and
all four Lambdas run real handlers. An earlier revision of this section said the
image was undeployed and a `shadow` record would sit forever; that is no longer
true. The dispatcher's rule is `ENABLED` and ticking every 15 minutes in dev.

⚠️ **That tick is a hazard while #898 is unmerged.** The deployed worker predates
it, so an enrolled record can be picked up by pre-fix code and failed at `verify`.
Always drive a local migration with `--break-lease`, which defers `dueAt` 20 minutes
out so the deployed dispatcher skips it.

### What works today, verified live in dev

| | |
|---|---|
| Chat against a promoted KB | **Yes.** `ast-1a90784a7f18` is `retain` / `managed` and serves real chunks |
| Add a document to a promoted KB | **Yes, once #898 deploys.** Blocked before that by §5.31. The consumer's EventBridge rule is `ENABLED`, the bucket has EventBridge notification on, and `grantRetrieval` is attached for the retrievability poll |
| Create a knowledge base that is managed from birth | **No — not implemented.** `newDefault` has **zero readers** in `backend/src` (grep for `NEW_DEFAULT`, `newDefault`, `new_default`: no matches). Design §14.7 steps 5–8, a follow-up spec. The only route onto the managed engine is enrol → migrate |
| Image-only PDFs | **Managed yes, legacy no.** A pure-diagram flowchart fails Docling outright (`zero chunks` → `failed`, permanently) and is served fine by managed via `imageExtractionStatus: ENABLED` (§5.35) |
| Deleting a document | **Propagates to managed only after #900 deploys.** Before that the managed copy is orphaned; the fail-closed status filter is what keeps it unserved (§5.36) |
| Whole local chain | **Yes.** SPA :4200 → app_api :8000 → inference_api :8001. `chat-http.service.ts` posts to `{appApiUrl}/chat/stream`; the app_api proxy forwards to `INFERENCE_API_URL`, which defaults to and is set to `http://localhost:8001`. Retrieval runs at `inference_api/chat/routes.py:1814`, so the RAG code answering a local chat is the code on disk |

**Three behaviour changes are live on the existing legacy path** regardless of any
flag:
1. The document-status filter now **fails closed** (group 6) — except for the one
   fail-open line in §5.33.
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
cd infrastructure && npx jest               # 640 passing, 30 suites

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

### Driving a migration locally (do this before deploying anything)

```bash
cd backend
uv run python ../scripts/local-dev/run-kb-migration.py <assistant-id> --show
uv run python ../scripts/local-dev/run-kb-migration.py <assistant-id> --break-lease
```

Runs the worker's steps in-process against dev with your SSO credentials, so the
whole state machine iterates in seconds. `--break-lease` clears the 15-minute lease
between steps and defers `dueAt` 20 minutes out so the deployed dispatcher does not
race you.

It needs five variables in `backend/src/.env` that the app-api task definition does
not carry — copy them from the deployed worker Lambda:
`MANAGED_KB_SERVICE_ROLE_ARN`, `MANAGED_KB_TAG_VALUE_PREFIX`,
`MANAGED_KB_TAG_VALUE_ENVIRONMENT`, `MANAGED_KB_METRIC_NAMESPACE`,
`KB_MIGRATION_RETAIN_DAYS`.

**What it does not prove:** the worker Lambda's IAM role (your SSO identity is
broader), the CDK environment wiring, or the image contents. Those are deploy-time
concerns — check them by deploying. Getting the logic right here first is the point,
and two of the five defects below were IAM/wiring and could only surface that way.

### Two read-only diagnostics worth knowing before you debug anything

```bash
cd backend
# Per-document ingestion timing, and WHICH engine did the work.
uv run python ../scripts/local-dev/kb-doc-timings.py <assistant-id>

# The same query through BOTH engines, side by side.
uv run python ../scripts/local-dev/kb-compare-engines.py <assistant-id> "CS434"
uv run python ../scripts/local-dev/kb-compare-engines.py <assistant-id> -f queries.txt
```

`kb-doc-timings.py` derives the engine from the record rather than guessing:
`chunkCount`/`vectorStoreId` are only ever written by the legacy pipeline,
`indexedAt`/`retrievableAt` only by the managed consumer, so a document carrying
both was double-indexed (§5.32). Measured on a 132 KB PDF: legacy `complete` at
30 s, managed retrievable at **95 s**, `INDEXED → retrievable` **0.9 s** — which
independently confirms the evaluation's 0.75–1.03 s.

⚠️ It reports legacy as `(lost)` for a double-indexed document, and that is a real
limit, not a bug: the managed consumer overwrites `updatedAt`, so the legacy
finish time is unrecoverable afterwards. Capture it live if you need it.

`kb-compare-engines.py` exploits the `retain` window — promotion moves no data, so
for 30 days **both** indexes hold the corpus and the same query can be put to
both. Marks each chunk `[in ]`/`[CUT]` against `MAX_CONTEXT_CHARS`, which is the
detail that makes the comparison meaningful: only ~2,000 characters reach the
model, so in practice **one or two chunks**, and precision@1 is nearly the whole
game. Absolute scores are not comparable across engines (legacy is a negated
cosine distance); order and **spread** are.

The clearest measured difference, and the one to reach for first — exact-token
search, where legacy is pure vector and managed is hybrid:

| query `CS434` | top chunk | spread |
|---|---|---|
| legacy | `SECTION 4: TECHNICAL ELECTIVES…` (does not contain CS434) | **0.0561** |
| managed | `- Algorithms of Machine Learning (3) CS434 - Applied Deep…` | **0.4988** |

Legacy's five chunks sit within 0.056 of each other — flat, so its ranking is
close to arbitrary. Nine times the separation on managed, with the literal match
first.

⚠️ **PR #900 ends this trick.** Once the legacy pipeline stands down for a
promoted knowledge base, new uploads land in the managed index only, so a
like-for-like A/B needs two assistants or documents that predate promotion.

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

### AWS authorizes some Bedrock APIs under a *different* action name ⚠️

Three times now, a grant listing exactly the API the code calls has deployed clean,
reviewed clean, and failed on first real use:

| API called | IAM action actually checked | Symptom |
|---|---|---|
| `CreateKnowledgeBase` with tags | `bedrock:TagResource` | fails the moment a real KB is created (§5.26) |
| (reconciler tag read) | `bedrock:ListTagsForResource` | fails closed — every KB looks untagged, orphan sweep reports a clean account forever (§5.26) |
| `IngestKnowledgeBaseDocuments` | **`bedrock:StartIngestionJob`** | every document upload `AccessDenied` (§5.31) |

The action name and the API name are not the same namespace. **Check the service
authorization reference or the feature's own prerequisites page before assuming a
grant is complete**, and never infer completeness from a successful local run — SSO
identities are broader than every Lambda role here.

Corollary: an action in a grant that no code calls is not necessarily dead. Read the
docblock before deleting it.

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

### The six that only running it revealed

These came out in sequence over 2026-08-27 to 08-31, each one step further into the
saga than the last. Every one reviewed clean, deployed clean, and did nothing or
failed on first real use. If you read only one section of this file, read this one.

25. **The dispatcher could not find the worker.** The construct set
    `MANAGED_KB_WORKER_FUNCTION_NAME`; `dispatcher.py` reads
    `KB_MIGRATION_WORKER_FUNCTION_NAME` — the convention its siblings use. Every
    tick raised `RuntimeError: ... is not set`, on a fifteen-minute schedule, in
    silence unless someone read the logs. `kb-sync` does not have this bug for
    exactly one reason: `kb-sync.test.ts` asserts the variable exists.
    A second mismatch found by the same sweep: the construct published
    `MANAGED_KB_RETENTION_WINDOW_DAYS`, which nothing reads, while
    `worker._retain_days()` reads `KB_MIGRATION_RETAIN_DAYS` — so Req 15.11's
    configured window was silently replaced by the code's 30-day floor. **Guard:**
    `tests/supply_chain/test_kb_migration_env_contract.py` now asserts every
    `os.environ` name the handlers read is set by the construct, and that the
    construct publishes nothing unread.

26. **`bedrock:TagResource` was not granted.** `CreateKnowledgeBase` is called
    *with* tags and AWS authorises the tagging as a separate action, so the grant
    reviewed as complete and failed the moment a real knowledge base was created.
    `bedrock:ListTagsForResource` was missing for the same reason, with a quieter
    failure: the reconciler fails closed on a tag read, so every knowledge base
    would look untagged and the orphan sweep would report a clean account forever.

27. **`CreateDataSource` ran against a `CREATING` knowledge base.**
    `CreateKnowledgeBase` returns before the knowledge base is usable — this
    module's own header records 47–124 s to `ACTIVE` — and the code called
    `CreateDataSource` immediately. `ConflictException` is deliberately not
    retryable and `_call`'s backoff tops out near 60 s, so the wait had to be
    explicit. **Why no test caught it:** `FakeBedrockAgent.create_knowledge_base`
    returned `status: "ACTIVE"`, which the real API never does.

28. **The knowledge base id was never recorded until both creates succeeded.**
    `attach_aws_ids` needs both identifiers, so a failure between them left a
    record with no `awsKbId` — and every later attempt re-entered the create path
    and was refused, permanently, because the *name* was taken. **The
    `clientToken` does not save this**: AWS idempotency tokens expire within
    minutes. Earlier revisions of this document and of the module header claimed
    otherwise; they were wrong. Fixed by `records.attach_knowledge_base_id`
    (persist immediately) plus adopt-by-name for records already stuck.
    **Why no test caught it:** two tests *certified* the bug —
    `test_the_record_survives_as_a_discoverable_retry_anchor` asserted
    `"awsKbId" not in anchor`, and its sibling relied on the fake modelling
    `clientToken` dedup as **permanent** while not modelling name uniqueness at
    all. The fake now enforces name uniqueness and treats tokens as expired by
    default.

29. **The embedding pin and managed reranking are mutually exclusive.** Req 8.5
    pinned `titan-embed-text-v2:0` via `embeddingModelType: CUSTOM`; Req 11.2
    requires `rerankingModelType: MANAGED`. AWS rejects the combination, and the
    §13 evaluation had measured the two **separately, never together**. Req 8.5 is
    now amended: the pin protected a failure mode that cannot occur in managed mode
    (we never embed the query — Bedrock embeds both sides), and the evaluation
    measured the pin as worth nothing while reranking measurably separates scores.
    Confirmed in dev: pinned + `NONE` gives flat 1.00/0.982/0.952; unpinned +
    `MANAGED` gives 0.413/0.199.

30. **`verify` failed a good migration for being asked too early.** The canary
    retrieval returned nothing because the freshly-ingested document was not yet
    queryable, and that was treated as terminal. Measured: **~45 s** from ingest to
    retrievable on a fresh knowledge base, against the docstring's "0.75–1.03 s"
    (a warm-knowledge-base figure). `verify` now defers via
    `records.defer_verify`, bounded at `MAX_VERIFY_ATTEMPTS`. Adoption also learned
    to skip knowledge bases in `DELETING`, found the same way: a local recreate
    adopted one mid-delete.

---

### The seventh, from adding a document rather than migrating one

31. **`bedrock:StartIngestionJob` was not granted, so direct ingestion could not
    run at all.** Found by uploading a second document to the already-promoted
    knowledge base in dev. The `DOC#` record went to `failed` carrying:

    ```
    AccessDeniedException ... IngestKnowledgeBaseDocuments ... not authorized
    to perform: bedrock:StartIngestionJob on resource: knowledge-base/M8WQZVQJ8X
    ```

    AWS authorises `IngestKnowledgeBaseDocuments` under the **adjacent action
    name** `bedrock:StartIngestionJob`. Both are listed in one statement in AWS's
    direct-ingestion prerequisites
    (`bedrock/latest/userguide/kb-direct-ingestion-prereq.html`).
    `grantManagedKbDirectIngestion` carried only the name matching the API call,
    so it reviewed as complete, deployed clean, and failed on first real use —
    identical in shape to §5.26's missing `bedrock:TagResource`. Third occurrence
    of that pattern; assume a fourth exists.

    **The worker had the same gap.** It receives the same grant and calls the same
    API, so the first migration driven by the *deployed* dispatcher would have
    failed the same way. It stayed hidden because every migration to date was
    driven by `run-kb-migration.py` under an SSO identity broader than either
    Lambda role — the exact limitation §2 names. The local driver cannot find this
    class of defect, ever. Only a deployed run can.

    ⚠️ **The action looks like a mistake and is not.** Requirement 9.2 forbids
    *calling* `StartIngestionJob` (0.1 RPS account-wide — one document per ten
    seconds) and nothing calls it. Holding it is authorisation, not invocation. A
    docblock on the grant and a separately-named test carry that reason so the
    obvious cleanup fails a test that explains itself.
    `bedrock:ListKnowledgeBaseDocuments` is in AWS's example policy and omitted
    deliberately: no code path calls it.

    Guards: three tests across `managed-kb.test.ts` and `kb-migration.test.ts`,
    one asserting **both** the worker and ingestion-consumer roles carry it.
    Mutation-tested — removing the action fails exactly four tests, each named for
    the reason.

---

### The eighth through eleventh, all from one evening of running it — fixed in PR #900

These four are one root cause wearing four costumes: **the two engines were never
made exclusive.** The consumer stands down for a legacy document; nothing made the
legacy pipeline stand down for a managed one, and nothing propagated a deletion to
the managed engine at all. Every symptom below follows from that.

32. ✅ **Routing exclusivity was enforced on only one side, so a document added to
    a managed knowledge base was indexed twice.** design.md §537 states "a
    document is indexed on exactly one backend outside a deliberate migration or
    dual-read pilot, so no double-indexing", and task 9.2 claims tests for it. The
    consumer does return immediately for a legacy document. But the **legacy
    handler had no engine gate at all**, and its `s3:ObjectCreated:*` notification
    on `assistants/` is still live alongside the (now enabled) EventBridge rule.
    Both fired.

    Visible in real data on `DOC#DOC-dc8b65658e29`: `chunkCount: 8` and
    `vectorStoreId: assistants-index` written by the legacy pipeline, while the
    managed side failed with §5.31.

    **Why no test caught it:** every exclusivity test in
    `test_kb_ingestion_consumer.py` is on the consumer's side
    (`test_a_legacy_document_is_not_ingested_here` and siblings). Nothing asserted
    the legacy handler skips a managed document, which is precisely the half that
    did not exist. A test suite can be thorough about the side that works.

    Fixed: `handler.py` resolves the engine before writing anything and returns
    early for `managed`. An earlier revision of this entry said the gate was
    missing from "both copies" including
    `infrastructure/bootstrap-assets/rag-ingestion/handler.py` — that was
    misleading. The bootstrap copy is a 33-line no-op placeholder that indexes
    nothing and needs no gate.

33. ⚠️ **STILL OPEN. The document-status filter has one fail-*open* line in an
    otherwise fail-closed function.** `_filter_vectors_by_document_status` opens
    with `if not doc_ids: return vectors` — so a batch of chunks carrying no
    `document_id` at all bypasses the DynamoDB check entirely and is served
    unverified. Every other unprovable path in that function returns `[]` and
    emits `METRIC_STATUS_FILTER_FAIL_CLOSED`.

    Not firing today: managed chunks do carry the id (§6 below), and legacy chunks
    always have. It predates this feature. But `_document_id` returns `""` when
    `location.customDocumentLocation.id` and both metadata mirrors are absent,
    which is exactly the input that would trip it, and the failure is silent in
    the serving direction.

34. ✅ **Two writers owned one `status` field, so "ready" was decided by a
    coin toss.** The sharpest consequence of §5.32, and worth its own entry
    because the duplicate vectors are the cheap half of that defect.

    Both outcomes were observed within an hour of each other in dev:

    | document | what happened |
    |---|---|
    | `DOC-b5d5d8019f44` | legacy wrote `complete` at **+30 s**; the managed KB could not answer until **+95 s**. Sixty-five seconds of "your document is ready" followed by an answer that does not mention it. |
    | `DOC-d637491d6cb1` | Docling produced **zero chunks** → `failed`. Bedrock indexed it fine and served it. It only ended up reading `complete` because the consumer finished **second**. |

    That second row is the alarming one, and the ordering was luck: reverse it —
    purely a function of parse time — and a good, retrievable document reads
    `failed` permanently, with no retry endpoint to recover it (task 14.4 open).

    It also defeated the exact protection `ingestion_consumer.py` documents in its
    header: *"the UI says the upload worked, the user asks a question straight
    away, and the answer does not mention their document."* The consumer polls
    until the document is genuinely retrievable to prevent that. A second,
    ungated writer undid it in one line.

    ⚠️ **The lesson generalises past this feature: any field two components can
    write needs a stated owner.** Nobody chose this race; it appeared because a
    new writer was added beside an old one and the question was never asked.

35. ✅ **Bedrock's image extraction works, and it makes the legacy pipeline's
    hard failure visible.** Not a defect in the new code — a capability
    difference nobody had measured, found while testing an image-only PDF.

    A 4-year curriculum flowchart (465 KB, pure diagram, no text layer) on a
    **legacy** assistant: `ValueError: Docling produced zero chunks`, status
    `failed`, permanently unusable — `docling_processor.py` sets `do_ocr=False`
    and `generate_page_images=False`. On the **promoted** assistant the same file
    was retrievable in 94.5 s, and the chunks show Bedrock's vision model output
    (`<analysis> <image_type> Hierarchical and Timeline Diagram`), including
    prerequisite chains that exist nowhere in the document as text.

    `imageExtractionStatus: ENABLED` is set by `provisioning.py` and was confirmed
    live on the dev data source. Note the old bundled `aws` CLI does **not** echo
    `mediaExtractionConfiguration` back from `get-data-source` — it looked
    unset until re-read with the pinned boto3. Do not conclude a field was not
    applied from CLI output alone.

36. ✅ **Deletion was never propagated to the managed engine, so a promoted
    corpus could only grow.** `cleanup_service` removed the legacy S3 Vectors copy
    and the `DOC#` row and never touched the managed knowledge base. The managed
    delete path existed — `kb_backend/tombstones.py` — but its only callers are in
    the reconciler, which is report-only with `reconcilerArmed` off.

    Three consequences, none of which raised anything:

    * storage paid for indefinitely at **$5.00/GB-month** against S3 Vectors'
      ~$0.15 — orphans on the expensive engine;
    * **silent retrieval degradation**, because the status filter runs *after*
      retrieval: each orphan consumed a slot in `top_k` and was then dropped, so a
      query could return five chunks and the model see two;
    * the only thing preventing deleted content from being **served** was the
      fail-closed status filter — which made §5.33's one fail-open line
      load-bearing in a way it was never designed to be.

    Fixed with a third, engine-gated phase in `cleanup_document_resources`,
    conjoined into `all_succeeded` so a failure blocks the hard delete.

    ⚠️ **The gate here is deliberately the opposite of the ingestion gate, and
    that asymmetry is the interesting part.** On ingest, an unreadable KB record
    resolves to *legacy*: being wrong costs a duplicate index while the consumer
    still drives the document to a correct terminal state. On delete it must
    **fail**: the `DOC#` row is what the status filter joins against, so reporting
    success on a failed managed delete would remove the row *and* leave the
    content — the one combination that turns a storage leak into a disclosure.
    Same question, opposite answers, for a reason worth keeping.

    IAM again had no code to give it away: neither the app-api task role nor the
    kb-sync worker could delete from a managed knowledge base, so this would have
    deployed clean and failed on first use. New `grantManagedKbDocumentDeletion`,
    narrower than `grantDirectIngestion` on purpose — these callers only remove,
    so a bug in the delete path cannot add content and a bug in the ingest path
    cannot remove it. Fourth instance of the §3 adjacent-action pattern, so it
    carries `StartIngestionJob` pre-emptively rather than waiting to be taught.

    kb-sync needed the image change too: its worker calls
    `cleanup_document_resources` after soft-deleting a document whose upstream
    source has vanished.

---

### The twelfth through fourteenth: the ingestion consumer never actually knew when a document was ready

All three are one theme. The consumer had to answer "is this document usable yet?"
and every mechanism it used to answer was measuring something else.

37. ✅ **Three stacked bugs left a fully retrievable document parked at
    `uploading` forever** (fixed in PR #901). A 1.5 MB PDF, uploaded to a promoted
    knowledge base:

    | | |
    |---|---|
    | **Wrong measurement** | The consumer polled a *retrieval* for 30 s, justified in its own header by the `INDEXED → retrievable` gap of "0.75–1.03 s". But the poll starts when the ingest call returns, so it had to cover `ingest → INDEXED → retrievable` — measured at 37–264 s for PDFs in the evaluation's §5.1, and 5 m 30 s for this file. The budget was smaller than the documented *lower bound*. |
    | **Self-defeating retries** | `IngestKnowledgeBaseDocuments` is fire-and-forget and nothing asked Bedrock what it already knew, so "not indexed yet" and "never submitted" were indistinguishable. All three deliveries re-ingested, discarding progress. The document reached INDEXED **54 s after the final attempt was dead-lettered**. |
    | **Fabricated timestamp** | `indexed_at = _now_iso()` ran right after the ingest call returned — recording when we *asked*, labelled as when indexing *finished*. |

    Fixed by probing `GetKnowledgeBaseDocuments` first and branching on the real
    status, never re-ingesting work already in flight, and using Bedrock's own
    `updatedAt`.

    ⚠️ **Lambda's async retry is capped at 2 attempts.** A hard service limit, and
    it is why the wait has to happen *inside* one invocation. I first "fixed" this
    with a `RetryPolicy` on the EventBridge target, which does nothing: for a
    Lambda target EventBridge hands the event off and the function's own async
    retry config governs. The construct now carries a comment saying so instead of
    the useless setting. Do not add it back.

    **Why no test caught the fabricated timestamp:** the test asserted only that
    `indexedAt` existed and was truthy, which any fabricated value satisfies. Same
    shape as §5.28 — the fake modelled instant success, so a 30 s window looked
    adequate for work that takes minutes.

    This is §5.30 for a second time. `verify` had the identical bug against the
    identical 0.75–1.03 s figure; that fix never reached this component, which
    inherited the constant. **When a wrong constant is found, grep for its other
    homes.**

38. ✅ **The retrievability probe searched for the document id as query text and
    could not find its own document** (fixed in PR #908). `wait_until_retrievable`
    ran `search(kb_ref, document_id, 5)` — the id *as the query* — then checked
    whether that document appeared. A document id is meaningless to an embedding
    model, so the search returned whatever the reranker preferred. Measured in dev
    with two documents present:

    ```
    query=DOC-40e985680a63 -> 5 chunks, ALL from DOC-db44eaf8f072   FOUND ITSELF: False
    ```

    A perfectly retrievable document reported as not retrievable. **It scales the
    wrong way:** the more documents a knowledge base holds, the less likely the
    target lands in an unfiltered top-5, so every upload to a mature knowledge base
    would burn its poll budget and dead-letter. It only ever worked while the
    knowledge base held exactly one document — where anything returned was
    necessarily the right thing.

    Fixed with an `equals` filter on `document_id`, so a non-empty result *is*
    proof and an empty one is a true negative. Verified in dev: each document
    returns 5 of its own chunks, a fabricated id returns none.

    ⚠️ **This is the third iteration on this one function, and I was wrong about
    what it measured twice.** Note also that yesterday's claim that a measured
    0.9 s gap "confirmed the 0.75–1.03 s figure" was false — with the fabricated
    timestamp it was measuring ingest-return → retrievable, not INDEXED →
    retrievable. A number agreeing with your expectation is not confirmation.

39. ✅ **The live service returns `TEXT_INDEXED`, which is not in the packaged
    SDK's `DocumentStatus` enum** (handled in PR #908). Observed on a document with
    image extraction enabled: `TEXT_INDEXED` (text searchable, media still
    processing) then `INDEXED`. The enum in the packaged model lists twelve values
    and this is not among them, so **do not derive status handling from the SDK
    enum** — it is incomplete against the running service.

    Treated as in-flight, not done: marking a document complete at `TEXT_INDEXED`
    would tell a user an image-only page is ready while the vision model is still
    running, which is the exact report the consumer exists to prevent. Unrecognised
    statuses now also default to "keep waiting" rather than "unknown, give up", so
    the next undeclared value AWS adds does not dead-letter documents.

    **A mutation-testing note worth keeping:** removing `TEXT_INDEXED` from the
    in-flight set is *behaviour-equivalent*, because the unknown-status fallback
    also waits — so the mutation survived every behavioural assertion. The honest
    resolution was to assert the only thing that genuinely differs: that the status
    is classified, and does not fall through the unknown branch. Not every
    surviving mutant means a missing test; some mean the mutation changes nothing.

---

### The fifteenth and sixteenth: retrieval got better and answers got worse

These two are different in kind from everything above. Nothing is broken, no
error is raised, every test passes — and the managed backend gives a **worse
answer than legacy** on a question it retrieves *better*. Both are open.

40. ⚠️ **OPEN, and the most consequential item in this document. The
    2,000-character context cap silently reduces `top_k=5` to `top_k=1` on the
    managed backend.** Bedrock's chunks are roughly 3× larger than Docling's, and
    `MAX_CONTEXT_CHARS` was sized for Docling's. Measured on one query:

    | | chunk sizes (chars) | how many fit in 2,000 |
    |---|---|---|
    | legacy | 388, 130, 1035, 106, 843 | **4** (1,659 chars) |
    | managed | 1111, 1091, 1151, 1197, 877 | **1** (1,111 chars) |

    So reranking does real work — §5 and the evaluation both show it separating
    scores properly — and then four of its five results are discarded before the
    model sees them.

    **It produced a materially wrong answer.** Asked about `CS434`, legacy said it
    is Major Core and mandatory (**correct** — the source PDF lists it under
    `SECTION 2: MAJOR CORE (Complete ALL)`). Managed called it "part of a
    specialized track/elective list" — the opposite of the advice a student needs —
    and invented a course title. Not because retrieval failed: managed's top chunk
    was the *only* one containing the literal string `CS434`, which legacy never
    found at all. It failed because that single surviving chunk had lost its
    `SECTION 2` header, and the nearest header it did contain was
    `TECHNICAL ELECTIVES`. The model reasoned correctly over a truncated window.

    ⚠️ **This contradicts a spec exclusion.** requirements.md's out-of-scope list
    says raising the cap is excluded because "the §13.6 experiment measured no
    correctness change from 2,000 to 20,000 characters on either backend". That
    experiment presumably ran on the three-document benchmark, where the answer sat
    in the top chunk anyway. It does not hold once chunks are large enough that only
    one fits **and** the context needed to interpret it lives in a neighbour. Same
    shape as Requirement 8.5: measured under conditions that excluded the real case.
    The exclusion note now carries this amendment.

    **Do not simply raise the number.** The cap is a parity control — §9 and §13.5
    require it held constant so the engine swap stays attributable. Changing it
    changes both backends and forfeits that. The honest options are to raise it for
    the managed path only and accept the asymmetry, or to re-run §13.6 on a corpus
    where section context lives outside the top chunk. Either needs measurement,
    not a constant bump.

    ⚠️ **My earlier `CS434` demo advice was wrong and the reason matters.** I
    called it a "safe crowd-pleaser" on the strength of retrieval *score spread*
    (0.0561 legacy versus 0.4988 managed). I measured the retriever and never
    checked the answer. Score separation is not answer quality.

41. ⚠️ **OPEN. Column-structured diagrams yield confidently wrong answers.** The
    capability in §5.35 is real — an image-only flowchart that legacy cannot ingest
    at all becomes retrievable, and Bedrock's vision model genuinely decodes it.
    But asked "what should they take semester 4?" from a 3.5-year curriculum
    flowchart, the answer reported **11 credits when the chart says 19**, invented
    `ENGR 220` (which belongs to an earlier column), missed four courses, and
    misdescribed `ME 215`.

    The cause is structural, not a tuning problem: correctness depends on **which
    column a box sits in**, and a retrieved chunk carries no coordinates. The
    vision model's description flattens or partially covers the columns, and the
    model then assembles a plausible table from courses that are genuinely adjacent
    in the document but belong to different semesters.

    Worth knowing how hard this is to spot: verifying it took three attempts with
    the PDF open, because the first two column reconstructions mis-assigned
    boundaries — the digit after "Semester" fell into the next bucket. If it takes
    that to check, a chunk of prose was never going to carry it.

    **Guidance until this is understood:** image extraction is worth demonstrating
    as *retrievable where it was previously impossible*, not as a source of precise
    tabular answers. Do not put a per-column question from a diagram in front of an
    audience.

---

## 6. Remaining work

### Do these first

| | |
|---|---|
| **Merge #900** | Engine exclusivity, both halves. Triggers `backend.yml` (rebuilds rag-ingestion **and** kb-sync — the content hash moves because `kb_backend` was added to both images' `SOURCE_DIRS`) and `platform.yml` (the two new IAM grants). Wait for the platform deploy before testing a deletion on a promoted assistant, or the delete fails on IAM and the `DOC#` row is deliberately kept |
| **Then re-add a document in dev** | `DOC#DOC-dc8b65658e29` is parked at `failed` from §5.31 and is not retried retroactively — there is no reprocess endpoint (task 14.4). Re-upload; that path works |
| **Then drive a migration from the *deployed* dispatcher, not the local driver** | §5.31 is the proof that the local driver cannot see IAM defects: its SSO identity is broader than either Lambda role. Every remaining unknown in this feature is of that class |

### Open, in rough order

| Group | Notes |
|---|---|
| **§5.40** the 2,000-char cap — START HERE | Managed's chunks are ~3× Docling's, so only ONE reaches the model and reranking's other four results are discarded. It produced a materially wrong answer (a required course described as an elective). Needs measurement, not a constant bump: the cap is a parity control (§9, §13.5). Options are managed-only asymmetry, or re-running §13.6 on a corpus where section context sits outside the top chunk |
| **§5.41** diagram answers | Column-structured diagrams give confident wrong answers because chunks carry no coordinates. Understand the shape before promising anything about tabular image content |
| **§5.33** the one fail-open line | The only finding from 2026-08-31 still open. `if not doc_ids: return vectors` in `_filter_vectors_by_document_status`. Make it fail closed with `METRIC_STATUS_FILTER_FAIL_CLOSED` like every other unprovable path in that function, and pin it with a test that mutation-fails. Lower stakes now that §5.36 removes deleted content from the managed engine, but still the one silent-serving path left |
| **engine visibility** | Nothing logs *which* engine served a query — the resolver only logs on failure — so "is the new one actually working?" can only be answered from the KB record. One INFO line in the facade, plus a `Managed`/`Classic` badge in the knowledge base section, both unbuilt. Wanted before a wide rollout, because this feature's whole risk profile is silent regressions |
| **14.4** one-click document retry | Req 21.2. Ingestion is S3-event-triggered and there is no reprocess endpoint, so this needs new backend against a live pipeline. The card currently directs the user to re-upload, which works today. Close it by building the endpoint **or** by amending Req 21.2 to accept re-upload |
| **14.5** admin surface | not started. Filter by engine, stored bytes, document counts, bulk migrate, per-KB retry |
| **15.1** packaged-SDK probe | the *static* half is done and passing (`boto3==1.43.68` carries `MANAGED`, the embedding members, `FLOAT32`, all four document ops, no `AWS_DATA_PATH`). The live half has now effectively been done by hand — a real create → ingest → retrieve → promote succeeded in dev |
| **15.2** ingestion-concurrency probe | unanswered. Do not size a wide fleet migration before it |
| **15.3** full matrix | run it once the above land |

### RESOLVED — the `document_id` / `relevance` "known unknown" was a false alarm

An earlier revision of this file reported that
`search_assistant_knowledgebase_with_formatting` returned `document_id: None` and
`relevance: None` after promotion, and inferred that the status filter was either
not running on the managed path or losing the id. **Both inferences were wrong.**
Measured against the live dev knowledge base (`M8WQZVQJ8X`) on 2026-08-31 with the
pinned boto3 1.43.68:

| Layer | Result |
|---|---|
| Raw `Retrieve` | `location.customDocumentLocation.id = "DOC-ae5cc5434f2d"` on both chunks |
| `ManagedKbBackend._to_chunk` | `document_id='DOC-ae5cc5434f2d'`, `relevance=0.4226 / 0.1616` |
| Facade output | `metadata.document_id = 'DOC-ae5cc5434f2d'`, `distance = −0.4226 / −0.1616` |

The probe read `result["document_id"]` and `result["relevance"]` at the **top level**
of the facade output. Those keys have never existed. The facade has emitted exactly
four keys — `text`, `distance`, `metadata`, `key` — since the function was first
written (`git log -L` on the `formatted_results.append` block confirms it, back
through `e34d928c`). The id lives at `metadata.document_id`; relevance is exposed as
`distance`, its exact negation. Reading a key outside the contract returns `None` on
**both** backends, so the observation said nothing about the managed path.

The status filter is genuinely running, not bypassed: it collected
`{DOC-ae5cc5434f2d}`, looked it up, found `status = "complete"`, and kept both
chunks. Verified independently — that `DOC#` record does read `complete`.

**The lesson is about the probe, not the code.** Asserting on a response shape
nobody had checked against the producing function turned four correct layers into a
reported defect, and it was written up as the highest-priority open item. Confirm
the contract before believing a `None`. (While confirming it, §5.33 turned up as a
genuine finding in the same function.)

### Known deferrals (correct, not oversights)

- **Reconciler EventBridge wiring** (Reqs 14.1, 14.7) — the rule exists and is
  enabled; `reconcilerArmed` stays off so it reports rather than deletes.
- **Group 7's snapshot reservation** has its caller (`run_shadow`).

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

scripts/local-dev/
  run-kb-migration.py             drive the real worker in-process; --break-lease
  kb-doc-timings.py               per-document ingest timing + which engine did it
  kb-compare-engines.py           one query, both engines, side by side

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
