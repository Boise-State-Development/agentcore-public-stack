# Requirements Document

## Introduction

This document specifies the requirements for replacing the platform's custom RAG
pipeline (Docling parse → Titan embed → Amazon S3 Vectors) with **Amazon Bedrock
Managed Knowledge Base** as the retrieval backend for assistant knowledge bases.

The decision to proceed is grounded in `docs/specs/bedrock-managed-kb-evaluation.md`,
whose §13.4 decision gate was **cleared on 2026-08-14**: on a 9-question benchmark
with every variable held constant, the current pipeline answered 4/9 and managed
answered 9/9. Two document classes moved from unusable to working — native
layout-heavy PDFs (1/3 → 3/3) and scanned/OCR PDFs (0/3 → 3/3).

The governing principle is **parity first, improvements later**. The user must
perceive nothing from the plumbing swap except the parser quality gain. Every
deliberate quality change that the evaluation identified as available — agentic
retrieval, raising the context cap, 0..N agent-to-KB bindings — is explicitly out
of scope here so that its effect remains attributable to itself.

Migration is **additive and reversible at every step**. Legacy resources remain in
place for dual reads, rollback, and retention; no legacy resource is removed by
this spec.

### Scope boundary

This spec covers phases 1–4 of the evaluation's §14.7 choreography:

1. additive schema, service role, IAM, worker resources, cleanup support;
2. dual backends dark, with mixed-version compatibility;
3. opted-in dual-read pilot, serving legacy;
4. opt-in migration with a rollback observation window.

Phases 5–8 (managed-by-default for new KBs, stopping legacy writes, reclaiming
legacy vectors, and final target-state cleanup) are **deliberately deferred to a
follow-up spec**. The flags in Requirement 19 exist so that those phases are
config changes rather than code changes.

### Non-goals

The following are explicitly **not** in scope, each for a stated reason:

- **Agentic retrieval.** Gated on the `AgenticRetrieveStream` account quota of
  60 requests/minute being raised (evaluation §6.4, §13.5 requirement 2). The
  user-triggered escalation design in §6.5 is a separate future feature.
- **Raising the 2,000-character context cap.** The §13.6 experiment measured no
  correctness change from 2,000 to 20,000 characters on either backend. Holding it
  constant is required to keep the swap attributable (§9, §13.5 requirement 3).
- **0..N agent-to-KB bindings (F4).** §10.6 requires that the engine swap and the
  binding-cardinality change not be coupled, because a joint failure is
  unattributable. This spec lands the `KnowledgeBase` entity record while
  preserving 1:1 binding semantics.
- **Routing conversation attachments through Managed KB.** §6.3 rejects this on
  four grounds, including that a chat attachment ingested into a shared agent KB
  becomes retrievable by every other user of that agent. Attachments remain
  session-scoped inline blocks.
- **Native Google Drive connector evaluation.** §11 question 4, never
  investigated; remains open.
- **Cleanup of the 101 stuck `deleting` and 95 `failed` legacy documents as a
  standalone production migration.** Requirement 21 folds this into the migration
  path instead.

## Glossary

- **Managed_KB**: An Amazon Bedrock Knowledge Base created with
  `type: "MANAGED"`, which provisions no customer-visible vector store. Distinct
  SKU from the classic `VECTOR` knowledge base, GA 2026-06-17.
- **Legacy_Backend**: The existing retrieval implementation over Amazon S3
  Vectors, as it exists today in `apis/shared/assistants/rag_service.py` and
  `apis/shared/embeddings/bedrock_embeddings.py`.
- **Managed_Backend**: The new retrieval implementation over a Managed_KB.
- **KB_Backend_Protocol**: The Python `Protocol` defining `search`, `ingest`, and
  `delete_document`, which both backends satisfy and behind which all callers sit.
- **Retrieval_Engine**: The per-knowledge-base discriminator selecting a backend.
  Values are `"s3vectors"` and `"managed"`; **absence means `"s3vectors"`**.
- **KB_Record**: The new DynamoDB entity representing a knowledge base as a
  first-class object, keyed by App_KB_Id.
- **App_KB_Id**: The stable, application-owned knowledge base identifier that
  agent bindings reference. Never the AWS `knowledgeBaseId`.
- **AWS_KB_Id**: The AWS-assigned `knowledgeBaseId`, which is replaceable across a
  dormancy/rehydration cycle and therefore never referenced by a binding.
- **Custom_Connector**: A Managed_KB data source of connector type `CUSTOM`,
  nested inside the `MANAGED_KNOWLEDGE_BASE_CONNECTOR` envelope, which accepts
  direct document ingestion.
- **Direct_Ingestion**: `IngestKnowledgeBaseDocuments`, which writes documents
  into a Custom_Connector without a sync job, bypassing the
  `StartIngestionJob` quota.
- **Ingestion_Consumer**: The durable S3 `ObjectCreated` event consumer that
  replaces the current Docling ingestion Lambda's orchestration role.
- **Migration_Worker**: The background worker that moves one knowledge base from
  Legacy_Backend to Managed_Backend through the Migration_State machine.
- **Migration_State**: The per-knowledge-base lifecycle
  `shadow → verify → promote → retain`, plus a terminal `failed` state that returns
  the knowledge base to Legacy_Backend, and a `reclaim` state reserved in the enum
  but never entered in this phase.
- **Reconciler**: The daily job that joins `ListKnowledgeBases` against KB_Records
  to detect orphaned AWS resources and stale pointers.
- **Doc_Status_Filter**: The query-time filter in
  `rag_service._filter_vectors_by_document_status` that drops chunks whose parent
  document is not `status == "complete"`.
- **Byte_Cap**: The enforced per-owner and per-knowledge-base limit on stored
  source bytes.
- **Tombstone**: A durable DynamoDB marker written before an AWS delete call and
  cleared only after AWS confirms deletion, so that a crashed delete is a
  retryable work item rather than a silent leak.
- **Parity_Contract**: The set of retrieval properties held identical across both
  backends so that the swap is perceptually invisible (Requirement 3).
- **Assistants_Table**: The existing DynamoDB table storing assistant and
  document records (`PK=AST#{assistant_id}`, `SK=DOC#{document_id}`).

## Requirements

### Requirement 1: Backend Abstraction Seam

**User Story:** As a developer, I want exactly one seam through which all
knowledge base retrieval and ingestion flows, so that the backend can be swapped
per knowledge base without any caller knowing which implementation it received.

#### Acceptance Criteria

1. THE system SHALL define a KB_Backend_Protocol in
   `backend/src/apis/shared/kb_backend/` exposing `search`, `ingest`, and
   `delete_document` operations.

> Placement note: a top-level package under `shared/`, **not** under
> `shared/assistants/`. `apis/shared/assistants/__init__.py` imports
> `rag_service`, which imports `apis.shared.embeddings.bedrock_embeddings` at
> module level — so importing the assistants package drags in the embeddings stack.
> `kb_sync/records.py` uses raw table access specifically to avoid that, and the
> new Lambdas have the same constraint. Requirement 24.15 enforces the boundary by
> test.
2. THE system SHALL provide two implementations of KB_Backend_Protocol:
   Legacy_Backend and Managed_Backend.
3. THE Legacy_Backend SHALL preserve the existing S3 Vectors behaviour, moved
   without functional change.
4. WHEN a caller resolves a backend, THE system SHALL select it solely from the
   knowledge base's Retrieval_Engine value.
5. THE two existing retrieval call sites (`inference_api/chat/routes.py` and
   `app_api/assistants/routes.py`, both via
   `search_assistant_knowledgebase_with_formatting`) SHALL be the only callers,
   and SHALL NOT branch on backend identity.
6. WHEN a KB_Record has no Retrieval_Engine attribute, THE system SHALL resolve
   the backend to Legacy_Backend.
7. THE system SHALL NOT write the value `"s3vectors"` to any record that does not
   already carry it, so that backwards compatibility is achieved by absence and
   requires zero backfill writes.

### Requirement 2: Score Direction Canonicalization

**User Story:** As a user, I want retrieved chunks ranked correctly regardless of
backend, so that answer quality does not silently invert when my knowledge base is
migrated.

#### Acceptance Criteria

1. THE KB_Backend_Protocol SHALL define chunk scores as **relevance**, where a
   higher value is more relevant.
2. WHEN the Legacy_Backend returns S3 Vectors cosine **distance** values, THE
   Legacy_Backend SHALL convert them to relevance before returning them across
   the seam.
3. THE Managed_Backend SHALL pass Managed_KB relevance scores through unchanged.
4. THE system SHALL include a test asserting that, for the same ordered input, both
   backends rank a known-best chunk first.

### Requirement 3: Parity Contract

**User Story:** As a user, I want a migrated knowledge base to behave exactly as it
did before except for parser quality, so that I cannot attribute any regression to
the upgrade.

#### Acceptance Criteria

1. THE system SHALL request `top_k = 5` on both backends.
2. THE system SHALL apply a context cap of **2,000 characters** on both backends,
   unchanged from today's `max_context_length` default.
3. THE system SHALL retain the Doc_Status_Filter on **both** backends during
   parity, even though Managed_Backend makes it redundant.
4. THE system SHALL build citations from the same `context_chunks` structure on
   both backends, with the excerpt clip held at 500 characters.
5. THE system SHALL NOT enable agentic retrieval on any path.
6. THE system SHALL NOT alter the answer model, system prompt, or `top_k` as part
   of this change.

### Requirement 4: Query Length Clamp

**User Story:** As a user, I want a long pasted message to still search my
knowledge base, so that I do not receive a hard failure for asking a long question.

#### Acceptance Criteria

1. WHEN a retrieval query is issued, THE system SHALL clamp the query string to at
   most **10,000 characters** before it reaches the backend.
2. THE clamp SHALL be applied at the KB_Backend_Protocol seam so that it protects
   both backends identically.
3. WHEN a query is clamped, THE system SHALL emit a metric or log record
   identifying that truncation occurred.
4. THE clamp SHALL NOT raise an error or fail the turn.
5. THE system SHALL remove the inline assertion in
   `apis/shared/embeddings/bedrock_embeddings.py` that no token validation is
   needed for the query string.

> Rationale: Managed_KB caps `Retrieve` query input at 10,000 characters and the
> quota is **not adjustable** (evaluation §6.4). Titan v2's ~32,000-character
> tolerance is the only reason nothing fails today. This is the single finding in
> the evaluation that produces a hard API failure rather than a cost or quality
> effect (§13.5 requirement 4).

### Requirement 5: Fail-Closed Document Status Filter

**User Story:** As a user who deleted a document, I want that document's content to
never be retrievable, so that a database problem cannot expose content I removed.

#### Acceptance Criteria

1. WHEN the Doc_Status_Filter cannot confirm a document's status because of a
   table-level lookup failure, THE system SHALL drop that document's chunks.
2. WHEN the Doc_Status_Filter cannot confirm a document's status because the
   documents table name is not configured, THE system SHALL drop all chunks.
3. WHEN the Doc_Status_Filter drops chunks because status could not be confirmed,
   THE system SHALL emit a distinct error-level signal separating this case from an
   ordinary empty-result case.
4. THE per-document lookup path SHALL continue to fail closed, as it does today.
5. **This requirement supersedes Requirement 3.4 of the
   `reliable-document-deletion` spec**, which specified that a DynamoDB error
   SHALL fall back to returning unfiltered results.
6. THE change SHALL ship as part of this feature's deployment, not as a standalone
   production change.

> Rationale: evaluation §7.4 documents this as a live fail-open path. §14.4
> requires the filter fail closed before migration. The prior behaviour was a
> deliberate availability-over-privacy choice; retiring it is therefore a
> supersession and must be recorded as one.

### Requirement 6: Knowledge Base as a First-Class Entity

**User Story:** As a developer, I want a knowledge base to be its own record with a
stable identifier, so that the AWS resource behind it can be replaced without
breaking any agent binding.

#### Acceptance Criteria

1. THE system SHALL introduce a KB_Record persisted in DynamoDB.
2. THE KB_Record SHALL carry at minimum: App_KB_Id; owner identity; visibility or
   ACL state; Retrieval_Engine; provisioning/lifecycle state; AWS_KB_Id;
   data-source id; embedding and parser configuration including immutable choices;
   stored-byte accounting; `lastRetrievedAt`; Migration_State with generation,
   progress, lease, error and rollback timestamps; and pin/retention/exemption
   flags.
3. Agent bindings SHALL reference App_KB_Id only.
4. THE system SHALL NOT persist AWS_KB_Id in any binding.
5. FOR this phase, THE system SHALL set `App_KB_Id == assistant_id`, preserving
   the existing 1:1 relationship.
6. WHEN no KB_Record exists for an assistant, THE system SHALL treat it as a
   virtual legacy S3 Vectors knowledge base and SHALL NOT create a record as a
   side effect of a read.
7. THE system SHALL NOT change the cardinality of the agent-to-knowledge-base
   relationship, and the existing rejections in `bindable_catalog.py` and
   `binding_validation.py` SHALL remain in force.
8. THE test suite SHALL assert that an explicit `knowledge_base` binding is still
   rejected and that `bindable_catalog` still returns an empty list for it, so the
   1:1 freeze is enforced by test rather than by intention.

### Requirement 7: Lazy Provisioning Saga

**User Story:** As a system operator, I want a knowledge base created in AWS only
when it is first needed and never duplicated, so that we do not pay for empty
resources or strand orphans.

#### Acceptance Criteria

1. THE system SHALL NOT call `CreateKnowledgeBase` when an assistant or knowledge
   base is created.
2. WHEN the first document for a knowledge base is successfully ready to ingest,
   THE system SHALL provision the Managed_KB.
3. THE system SHALL write the KB_Record in a `provisioning` state **before**
   calling AWS, and SHALL attach returned identifiers with a conditional write.
4. WHEN two ingestions race to provision the same knowledge base, THE system SHALL
   create at most one Managed_KB.
5. THE system SHALL pass a `clientToken` that satisfies the API's **33-character
   minimum**, 256-character maximum, and
   `[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}` pattern.
6. THE system SHALL construct the `clientToken` programmatically rather than by
   interpolating a template that may fall below the minimum length.
7. WHEN `CreateKnowledgeBase` fails with a message indicating the embedding model
   could not be verified, THE system SHALL treat the failure as retryable.
8. WHEN provisioning is interrupted after the AWS call but before the conditional
   write, THE KB_Record SHALL remain a durable retry anchor discoverable by the
   Reconciler.

> Rationale: §5.1 measured `CreateKnowledgeBase` → ACTIVE at 47–124 s (n=7), so
> this must never sit on an interactive path. The "embedding model could not be
> verified" failure was observed to be pure IAM eventual consistency against a
> model confirmed ACTIVE and invokable.

### Requirement 8: Managed Knowledge Base Configuration

**User Story:** As a system operator, I want each Managed_KB created with the exact
configuration the evaluation validated, so that we do not silently lose a
capability we are paying for.

#### Acceptance Criteria

1. THE system SHALL call `CreateKnowledgeBase` with `type: "MANAGED"`, a
   `roleArn`, and `managedKnowledgeBaseConfiguration`.

> Shape note, verified against the packaged botocore service model:
> `managedKnowledgeBaseConfiguration` has **no required members**, but its only
> members are `embeddingModelType`, `embeddingModelArn`,
> `embeddingModelConfiguration` and `serverSideEncryptionConfiguration`. So the
> embedding pin required by criterion 5 below has nowhere else to live, and sending
> a literal `{}` would make that criterion unsatisfiable. "No required members" is
> not the same as "must be empty" — earlier drafts of this spec said `{}`, which is
> why this note exists.
2. THE system SHALL omit `storageConfiguration` entirely.
3. THE system SHALL create its data source with
   `dataSourceConfiguration.type = "MANAGED_KNOWLEDGE_BASE_CONNECTOR"` and the
   real connector type in
   `managedKnowledgeBaseConnectorConfiguration.connectorParameters`.
4. THE system SHALL use connector type `CUSTOM`.
5. THE system SHALL set `embeddingModelType: CUSTOM` pinned to
   `amazon.titan-embed-text-v2:0` at `FLOAT32` (the service-model enum value; lowercase is rejected) and 1024 dimensions.
6. THE system SHALL enable
   `mediaExtractionConfiguration.imageExtractionConfiguration.imageExtractionStatus
   = ENABLED` on the data source.
7. THE system SHALL set the data source's `dataDeletionPolicy` to `RETAIN` at
   creation time.
8. THE system SHALL treat embedding configuration as immutable after creation and
   SHALL NOT attempt to change it.
9. A single Bedrock service role SHALL be reusable across many Managed_KBs.

> Rationale: §11.1 — image extraction is opt-in and silently indexes nothing if
> left default; custom Titan v2 embeddings measured identical cold-ingest time and
> identical 9/9 quality, and preserve continuity with today's embedding across an
> immutable choice; `dataDeletionPolicy: RETAIN` is the documented remedy for the
> `DELETE_UNSUCCESSFUL` state already observed in the dev account.

### Requirement 9: Direct Document Ingestion

**User Story:** As a user uploading documents, I want ingestion to keep up with
bulk uploads, so that a large batch is not serialized behind an API quota.

#### Acceptance Criteria

1. THE system SHALL ingest documents using Direct_Ingestion into the
   Custom_Connector.
2. THE system SHALL NOT use `StartIngestionJob` for per-document ingestion.
3. THE system SHALL send at most **10 documents** per
   `IngestKnowledgeBaseDocuments` call.
4. THE system SHALL set `customDocumentIdentifier` to the platform's
   `document_id`.
5. THE system SHALL treat concurrent `Ingest` and `Delete` document operations as
   limited to **10 per account** and SHALL bound its own concurrency accordingly.
6. THE system SHALL NOT carry forward the `{doc_id}#{chunk_index}` vector-key
   bookkeeping, including `delete_vector_tail` and the chunk-shrinkage stash, on
   the Managed_Backend path.

> Rationale: `StartIngestionJob` is 0.1 RPS account-wide and not adjustable
> (§9). The API reference caps the document array at 10; AWS's user guide claim of
> 25 was disproven server-side for managed knowledge bases (§11.1).

### Requirement 10: Durable Ingestion Control Plane

**User Story:** As a user, I want an upload to reliably become searchable even if a
worker crashes, so that documents do not silently fail to index.

#### Acceptance Criteria

1. THE Ingestion_Consumer SHALL be a durable, retryable compute resource triggered
   by the documents bucket's `ObjectCreated` notification.
2. THE Ingestion_Consumer SHALL resolve each document's knowledge base and
   Retrieval_Engine before doing any work.
3. WHEN a document belongs to a legacy knowledge base, THE Ingestion_Consumer
   SHALL route it to the existing pipeline.
4. WHEN a document belongs to a managed knowledge base, THE Ingestion_Consumer
   SHALL route it to Direct_Ingestion.
5. THE system SHALL NOT index the same document on both backends outside of a
   deliberate migration or dual-read pilot.
6. THE Ingestion_Consumer SHALL poll until the document is not merely reported
   indexed but **actually retrievable**, and SHALL record those as two distinct
   timestamps.
7. THE Ingestion_Consumer SHALL update the `DOC#` record to a terminal
   complete or failed state with bounded retries and a durable retry anchor.
8. THE system SHALL NOT perform ingestion orchestration in an in-process
   `asyncio.ensure_future` task.
9. THE Ingestion_Consumer SHALL tolerate ingestion latency of at least 300
   seconds for a single document.

> Rationale: §14.1 — the browser creates an `uploading` row and receives a
> presigned PUT; there is no upload-complete API call, so the S3 event remains the
> only trigger. §5.1 measured a fixed per-knowledge-base warm-up of ~68 s and a
> long tail to 264 s on a 50 KiB PDF, so timeouts must be generous.

### Requirement 11: Managed Retrieval Configuration

**User Story:** As a user, I want retrieval against a managed knowledge base to use
the correct API shape and managed reranking, so that results are well ordered.

#### Acceptance Criteria

1. THE Managed_Backend SHALL use `managedSearchConfiguration` and SHALL NOT send
   `vectorSearchConfiguration`.
2. THE Managed_Backend SHALL request managed reranking rather than
   `rerankingModelType: NONE`.
3. THE Managed_Backend SHALL NOT attempt to configure or toggle hybrid search.
4. WHEN a metadata filter is applied, THE system SHALL rely on filters failing
   **closed**, as measured.
5. THE Managed_Backend SHALL constrain any isolation-critical filter to `equals`
   or `in`.

> Rationale: §5.1 — `vectorSearchConfiguration` is rejected outright for managed
> knowledge bases. §11 question 3 measured `equals`, `startsWith` and
> `stringContains` on an impossible key all returning 0 results, disproving the
> silent-ignore/fail-open claim. §11.1 — managed reranking measurably separates
> scores (0.89/0.38/0.25/0.21/0.19 versus a nearly flat 1.00/0.84/0.78/0.77/0.77
> without it), and **the reranker is what makes a 2,000-character cap defensible**.

### Requirement 12: Enforceable Storage Cost Controls

**User Story:** As a platform owner, I want stored bytes capped per owner before any
managed knowledge base holds production data, so that storage cost cannot grow into
a six-figure monthly exposure.

#### Acceptance Criteria

1. THE system SHALL enforce a per-owner Byte_Cap and a per-knowledge-base
   Byte_Cap.
2. THE per-owner default SHALL be **100 MB**, an elevated admin-granted tier SHALL
   be **1 GB**, and the per-knowledge-base ceiling SHALL be **500 MB**. All three
   SHALL be configurable and resolvable by role tier. These values require product
   sign-off before implementation.
3. THE system SHALL determine a document's contribution to the Byte_Cap from an S3
   `HEAD` on the stored object, NOT from a client-reported size.
4. THE system SHALL apply byte accounting as an atomic reserve → commit → release
   flow.
5. WHEN two uploads race against the same remaining allowance, THE system SHALL
   NOT allow the combined committed total to exceed the Byte_Cap.
6. WHEN an ingestion fails, THE system SHALL release the reservation.
7. THE system SHALL NOT depend on the `RawDataSize` CloudWatch metric for
   enforcement.
8. THE system SHALL NOT depend on cost-allocation tags for enforcement.
9. THE Byte_Cap SHALL be enforced before any production traffic is promoted to
   Managed_Backend.
10. THE system SHALL define and document whether the knowledge base owner or the
    invoking user consumes retrieval quota.
11. THE Byte_Cap SHALL be enforced on **every** path that adds bytes to a managed
    knowledge base, including the migration re-ingest path, not only interactive
    upload.
12. WHEN a knowledge base's corpus would exceed its owner's remaining allowance,
    THE Migration_Worker SHALL reserve for the whole snapshot and fail the
    migration **before** entering `shadow`, rather than part-migrating a corpus
    that cannot fit.
13. THE system SHALL raise account-level alarms on total managed storage, on
    managed knowledge base count against the 10,000 quota, on daily
    Knowledge-Base `usagetype` cost, and on a sustained non-zero orphan count.
14. THE system SHALL emit a metric when a Byte_Cap reservation is rejected, so the
    chosen default can be validated against real behaviour before it hardens into
    policy.

> Rationale: §13.5 requirement 1 — managed storage is $5.00/GB-month against
> ~$0.15/GB-month today, a 35× increase. The existing 1 GB-per-user allowance
> would permit 30,000 GB at full adoption, i.e. **$150,000/month**. This is the
> only finding in the evaluation that can cause real financial damage.
> `RawDataSize` returned 0 datapoints for a directly-ingested document (§11
> question 2), so it is unproven for this purpose.

### Requirement 13: Deletion Sagas and Tombstones

**User Story:** As a system operator, I want every delete to either complete or
leave a retryable work item, so that a failed delete is never a silent paying leak.

#### Acceptance Criteria

1. WHEN deleting a knowledge base, data source, or document, THE system SHALL write
   a Tombstone **before** calling AWS.
2. THE system SHALL clear the Tombstone only after AWS confirms the resource is
   gone.
3. THE system SHALL NOT treat an accepted delete call as a completed deletion.
4. THE system SHALL verify knowledge base deletion by polling until the resource is
   absent, tolerating at least 6 minutes.
5. THE system SHALL NOT delete a knowledge base's service role until all of its
   knowledge bases are confirmed absent.
6. THE system SHALL NOT remove the last KB_Record, nor allow TTL to remove it,
   until AWS confirms deletion.
7. WHEN a knowledge base reports `DELETE_UNSUCCESSFUL`, THE system SHALL surface it
   as an actionable operator state rather than a completed delete.
8. A surviving Tombstone SHALL be discoverable as a retryable work item.

> Rationale: §12 measured deletion taking 2–6 minutes and verified only by polling
> `ListKnowledgeBases`. §12.2 documents a knowledge base stuck in
> `DELETE_UNSUCCESSFUL` since 2025-11-24 that no reconciler would ever notice.

### Requirement 14: Daily Reconciler

**User Story:** As a system operator, I want a daily job that finds AWS resources
our database does not know about, so that crash orphans are detected rather than
paid for indefinitely.

#### Acceptance Criteria

1. THE Reconciler SHALL run on a schedule and join a paginated, tag-filtered
   `ListKnowledgeBases` against KB_Records.
2. WHEN a Managed_KB exists in AWS with no KB_Record, THE Reconciler SHALL treat it
   as an orphan.
3. THE Reconciler SHALL age-gate orphan deletion on the **AWS-reported
   `createdAt`**, NOT on the time of discovery.
4. THE Reconciler SHALL delete an orphan only when it is older than 24 hours.
5. WHEN a KB_Record references an AWS_KB_Id that does not exist, THE Reconciler
   SHALL mark the record's vector state as missing and SHALL NOT delete the
   record.
6. WHEN both sides agree, THE Reconciler SHALL refresh stored-byte accounting.
7. THE Reconciler SHALL run in a report-only mode that logs intended deletions
   without performing them, and report-only SHALL be the initial deployed mode.
8. THE Reconciler SHALL apply a bounded per-run action limit.

> Rationale: §7.4 — age-gating on discovery time means a reconciler that was down
> for a week deletes in-flight creates. §7.3 requires shipping in report-only mode
> and arming later, and warns specifically about the empty-string workflow-variable
> case.

### Requirement 15: Migration State Machine

**User Story:** As a knowledge base owner, I want my knowledge base upgraded without
downtime and without re-uploading anything, so that the upgrade is invisible until
it succeeds.

#### Acceptance Criteria

1. THE system SHALL migrate a knowledge base through Migration_State
   `shadow → verify → promote → retain`, with `failed` as a terminal state that
   returns the knowledge base to Legacy_Backend. `reclaim` is reserved in the enum
   and SHALL NOT be entered in this phase.
2. THE system SHALL NOT mutate a live knowledge base in place.
3. DURING `shadow` and `verify`, THE knowledge base SHALL remain fully usable and
   SHALL continue serving from Legacy_Backend.
4. THE system SHALL re-ingest source bytes from their existing S3 location and
   SHALL NOT ask the user to re-supply any document.
5. THE system SHALL migrate only documents whose status is `complete`.
6. THE `verify` step SHALL compare an exact source manifest of `document_id` plus
   content hash or generation, NOT document-count parity alone.
7. THE `verify` step SHALL perform at least one canary retrieval that confirms
   expected content is returned from the Managed_Backend.
8. `promote` SHALL be a single conditional write flipping Retrieval_Engine to
   `"managed"`.
9. THE system SHALL NOT promote unless a catch-up pass has converged.
10. WHEN two workers attempt promotion concurrently, THE conditional write SHALL
    allow at most one to succeed.
11. DURING `retain`, THE system SHALL preserve legacy vector data for a rollback
    window of at least 30 days.
12. THE system SHALL NOT enter `reclaim` for a knowledge base until the retention
    window has expired AND that knowledge base has served managed traffic without
    a rollback.
13. THE Migration_Worker SHALL take a lease so that one knowledge base is not
    migrated concurrently by two workers.
14. THE Migration_Worker SHALL apply a bounded per-tick dispatch limit.

> Rationale: §10.3. Timing recomputed from §5.1's revised figures (~73 s median
> create + ~68 s first ingest + ~2.5 s per warm small document): a 20-document
> knowledge base is **~3 minutes** and 100 documents **~6.5 minutes**. §10.3's own
> "4 min / 9.5 min" figures were computed from the **superseded** §5 numbers and are
> not used here. For a PDF-heavy corpus, per-document parse time of 37–264 s
> dominates and a 20-PDF knowledge base can exceed an hour — so this is background
> work only, and progress must be reported per-document rather than as an ETA.

### Requirement 16: Writes and Deletes During Migration

**User Story:** As a user, I want to keep uploading and deleting documents while my
knowledge base is upgrading, so that the upgrade does not freeze my work or corrupt
the result.

#### Acceptance Criteria

1. DURING migration, THE existing upload path SHALL remain authoritative and SHALL
   continue writing to Legacy_Backend.
2. THE Migration_Worker SHALL snapshot the document-id set, migrate it, then run a
   catch-up pass for documents created since the snapshot.
3. THE Migration_Worker SHALL repeat catch-up passes until a pass finds nothing
   new.
4. THE system SHALL re-read each document's `DOC#` record immediately before
   ingesting it, and SHALL skip the document if it no longer exists or is no longer
   `complete`.
5. THE system SHALL NOT resurrect a document that was deleted mid-migration.
6. THE system SHALL NOT implement dual-write as the coexistence mechanism.

### Requirement 17: Rollback

**User Story:** As a knowledge base owner, I want an upgrade to be undoable, so that
a bad outcome is recoverable immediately rather than requiring data restoration.

#### Acceptance Criteria

1. THE system SHALL support rollback by writing Retrieval_Engine back to its prior
   value.
2. Rollback SHALL NOT move or restore any data.
3. Rollback SHALL be available for the entire `retain` window.
4. WHEN a migration fails at any stage before `promote`, THE knowledge base SHALL
   remain on Legacy_Backend and SHALL remain fully usable.
5. THE system SHALL record a rollback timestamp on the KB_Record.

### Requirement 18: Dual-Read Pilot

**User Story:** As a platform owner, I want real comparative evidence before
migrating anyone, so that the rollout rests on measurement rather than on the
benchmark alone.

#### Acceptance Criteria

1. THE system SHALL support running both backends for the same query on an opted-in
   knowledge base.
2. DURING a dual read, THE system SHALL serve results from Legacy_Backend.
3. THE system SHALL record, per dual read, the overlap in returned `document_id`
   values, a rank correlation, and per-backend latency.
4. THE dual-read path SHALL be opt-in per knowledge base and SHALL default to off.
5. THE dual-read path SHALL NOT increase user-visible latency beyond the legacy
   path's own latency.

### Requirement 19: Independent Feature Flags

**User Story:** As a platform operator, I want to ship the managed backend without
starting a fleet migration, so that the two risks are separable.

#### Acceptance Criteria

1. THE system SHALL provide a flag controlling whether new knowledge bases are
   created managed.
2. THE system SHALL provide a separate flag controlling whether the
   Migration_Worker runs at all.
3. THE system SHALL provide a third, separate flag controlling whether the
   Reconciler deletes rather than only reporting.
4. THE three flags SHALL be independently settable.
5. ALL three flags SHALL default to off.
6. WHEN the migration flag is off, THE Migration_Worker SHALL perform no work.
7. WHILE the Reconciler arming flag is off, THE Reconciler SHALL log intended
   deletions and delete nothing.
8. THE system SHALL treat an empty-string flag value as off.

### Requirement 20: IAM, Encryption, and Teardown

**User Story:** As a security engineer, I want least-privilege, confused-deputy-safe
roles and a teardown that removes runtime-created resources, so that the feature
neither over-grants nor leaks resources.

#### Acceptance Criteria

1. THE system SHALL define a dedicated Bedrock knowledge base service role.
2. THE service role's trust policy SHALL constrain `aws:SourceAccount` and SHALL
   apply an `ArnLike` condition on `AWS:SourceArn` scoped to `knowledge-base/*`.
3. THE caller's `iam:PassRole` grant SHALL be conditioned on
   `iam:PassedToService`.
4. S3 access SHALL be conditioned on `aws:ResourceAccount`.
5. WHERE customer-managed encryption is required, THE system SHALL supply
   `serverSideEncryptionConfiguration.kmsKeyArn`.
6. THE system SHALL scope provisioner/migrator CRUD, direct-ingestion, and
   inference `bedrock:Retrieve` permissions separately.
7. WHEN synchronous AWS SDK calls are made from an async request path, THE system
   SHALL execute them off the event loop.
8. THE teardown script SHALL list and delete only resources tagged for the project
   and environment, and SHALL do so **before** deleting their service role and the
   platform stack.
9. THE system SHALL include CDK assertions covering the IAM conditions in this
   requirement.
10. THE system SHALL grant `cloudwatch:PutMetricData` scoped to the
    `{projectPrefix}/ManagedKb` custom namespace on the **calling identities only**.
    THE namespace SHALL NOT begin with `AWS`. THE Bedrock service role SHALL NOT
    receive this grant.
11. WHEN a Managed_KB is created, THE system SHALL tag it with the project prefix,
    the environment, the App_KB_Id, and the owner identity.
12. THE owner tag value SHALL be an opaque identifier and SHALL NOT be an email
    address or any other personally identifying value.
13. THE identities that read Bedrock's own per-knowledge-base metrics SHALL be
    granted `cloudwatch:GetMetricData` and `cloudwatch:GetMetricStatistics`. Those
    metrics live in the `AWS/Bedrock/KnowledgeBases` namespace, which is a **read
    source only** and is never a `PutMetricData` target under 20.10.

> Note: tagging is a hard prerequisite, not housekeeping. Requirement 14.1's
> tag-filtered `ListKnowledgeBases` and Requirement 20.8's teardown both read these
> tags; without them the Reconciler cannot distinguish our resources from anything
> else in the account, and teardown cannot scope itself.

> **Why 20.10's namespace is not an `AWS/...` one, and must not be "fixed" back to
> one.** CloudWatch reserves every namespace beginning with `AWS` for its own
> services: "You cannot specify a namespace that begins with AWS. Namespaces that
> begin with AWS are reserved for use by Amazon Web Services products." A
> `PutMetricData` grant scoped to `AWS/Bedrock/KnowledgeBases` therefore authorizes
> no publish that can ever succeed — it reads as correct in a policy review and
> silently does nothing. 20.10 and 20.13 cover two different directions of traffic
> that were previously conflated:
>
> - **Writing** this platform's OWN metrics (`KbByteCapRejected`, `KbOrphansFound`,
>   `KbIdleGB`, `KbCount`, `KbStorageGB`, `KbQueryClamped`,
>   `KbStatusFilterFailClosed`, `KbMigration{Started,Promoted,Failed,RolledBack}`)
>   needs `PutMetricData` into the non-reserved `{projectPrefix}/ManagedKb`
>   namespace (20.10). The project prefix keeps two environments in one account from
>   blending their metrics.
> - **Reading** Bedrock's own per-KB metrics (`Invocations`, `ClientErrors`,
>   `ServerErrors`, `Throttles`, `TotalIterationCount`, `RawDataSize`) needs
>   `GetMetricData` / `GetMetricStatistics` against `AWS/Bedrock/KnowledgeBases`
>   (20.13). Reading a reserved namespace is permitted; only writing is not.

> Rationale: §14.5 and §14.0. Metric publishing is best-effort and
> permission-gated: omit the grant and metrics silently vanish while requests keep
> succeeding. Managed embedding and managed reranking need no Bedrock model access;
> only `CUSTOM` embedding or reranking does — and Requirement 8.5 chooses `CUSTOM`
> embedding, so that grant is required.

### Requirement 21: Failed and Stuck Legacy Documents

**User Story:** As a user whose upload failed months ago without telling me, I want
to find out and retry, so that migration does not quietly drop my document.

#### Acceptance Criteria

1. WHEN a knowledge base is migrated, THE system SHALL surface to its owner any
   document not in `complete` status that will therefore not be carried across.
2. THE system SHALL offer a retry path for such documents.
3. THE system SHALL NOT silently omit non-`complete` documents without surfacing
   them.
4. THE system SHALL distinguish, in user-facing messaging, an unsupported file
   format from a processing failure.

> Rationale: §7.4 measured 1,692 `DOC#` records of which 200 (11.8%) are not
> `complete` — 101 stuck `deleting`, 95 `failed`, 4 `uploading`. §10.3 ingests only
> `complete` documents, so migration would silently drop all 95 failures. §11.2
> documents that the deployed pipeline cannot ingest `.txt` at all despite the repo
> and frontend both advertising support, producing a 56-second wait and a generic
> failure message.

### Requirement 22: Observability

**User Story:** As a system operator, I want to see knowledge base count, stored
bytes, orphans and migration progress, so that cost and correctness problems are
visible before they become incidents.

#### Acceptance Criteria

1. THE system SHALL emit metrics for at least: knowledge base count, stored
   gigabytes, idle gigabytes, orphans found, and Byte_Cap rejections. THE system
   SHALL NOT emit a reclaimed-gigabytes metric, because nothing reclaims in this
   phase and a structurally-always-zero metric trains operators to ignore it.
2. THE system SHALL emit migration progress and failure counts.
3. THE system SHALL emit a metric when a query is clamped per Requirement 4.
4. THE system SHALL emit a metric when the Doc_Status_Filter drops chunks because
   status could not be confirmed per Requirement 5.
5. THE system SHALL derive idleness from the maximum of the knowledge base's own
   last-retrieved time and the last-used time of any bound agent, NOT from
   retrieval alone.
6. THE system SHALL NOT write a last-retrieved timestamp on every retrieval.
7. THE system SHALL attribute cost by filtering on `usagetype`, NOT on service code
   alone.
8. THE system SHALL treat a sustained non-zero orphan count as the signal that the
   delete saga is leaking.

> Rationale: §7.2 — idleness computed from retrieval alone evicts an actively used
> agent's knowledge base because its queries did not match. §7.3 requires a
> throttled conditional write rather than per-retrieval writes; §14.0 notes
> per-knowledge-base `Invocations` is a cheaper idleness signal. §8 — Managed KB
> bills under `AmazonBedrockAgentCore`, so anything keyed on `AmazonBedrock` misses
> it entirely and anything keyed on service code alone blends it into the Runtime
> memory line.

### Requirement 23: User Experience

**User Story:** As a knowledge base owner, I want the upgrade explained honestly and
never forced on me, so that I keep working normally and understand what changed.

#### Acceptance Criteria

1. WHEN a knowledge base is on Legacy_Backend and no action is required, THE system
   SHALL show no badge, banner, or prompt.
2. WHEN an upgrade is available, THE system SHALL present it as an inline, opt-in
   control describing only benefits proven by the §13 benchmark and stating that
   the knowledge base keeps working during the upgrade.
3. DURING `shadow` and `verify`, THE system SHALL show non-blocking progress and
   SHALL allow the user to navigate away.
4. WHEN promotion succeeds, THE system SHALL show a one-time dismissible notice and
   SHALL NOT show a permanent badge.
5. WHEN migration fails, THE system SHALL show a plain-language reason and a retry
   control, and the knowledge base SHALL remain usable on Legacy_Backend.
6. THE system SHALL NOT use the word "vector" in user-facing copy.
7. THE upgrade control SHALL be gated on existing edit permission, and viewers
   SHALL NOT see it.
8. THE system SHALL NOT auto-migrate knowledge bases silently in this phase.
9. THE admin surface SHALL list knowledge bases filterable by engine with stored
   bytes and document counts, and SHALL support bulk migrate and per-knowledge-base
   retry.

### Requirement 24: Minimum Test Coverage

**User Story:** As a reviewer, I want the risky paths covered by tests before
promotion, so that correctness does not rest on manual verification.

#### Acceptance Criteria

1. THE test suite SHALL cover adapter parity across both backends, including score
   direction.
2. THE test suite SHALL cover create, ingest, and delete idempotency.
3. THE test suite SHALL cover a crash after the AWS create call but before the
   database update.
4. THE test suite SHALL cover record-only and AWS-only reconciliation outcomes.
5. THE test suite SHALL cover uploads and deletes occurring during migration.
6. THE test suite SHALL cover fail-closed document status and fail-closed access
   checks.
7. THE test suite SHALL cover byte-cap reservation races.
8. THE test suite SHALL cover a mixed old/new deployment serving simultaneously.
9. THE test suite SHALL cover teardown of tagged dynamic resources.
10. THE test suite SHALL include CDK assertions for the IAM conditions in
    Requirement 20.
11. THE test suite SHALL stub managed AWS APIs rather than calling them.
12. THE test suite SHALL assert that resource policies are re-applied after a
    rehydration that produces a new AWS_KB_Id.
13. THE test suite SHALL assert the presence of the CloudWatch metric permissions
    in Requirement 20.10.
14. THE test suite SHALL cover published-agent corpus behaviour, asserting that an
    engine swap does not alter what a published agent retrieves and that a listed
    agent is exempt from lifecycle reclaim.
15. THE test suite SHALL assert that `apis.shared.kb_backend` does not transitively
    import `apis.shared.assistants`, so the Lambda image constraint is enforced by
    test rather than by convention.

### Requirement 25: Authorization, Isolation, and Publication Semantics

**User Story:** As a user, I want my knowledge base readable only by people who are
allowed to read it, so that sharing an agent does not silently expose my documents.

#### Acceptance Criteria

1. THE system SHALL resolve the invoking user's access to a knowledge base **before**
   retrieval is attempted.
2. THE system SHALL reuse the existing assistant permission model rather than
   introducing a parallel one, so that owner, editor, and viewer semantics are
   unchanged.
3. THE system SHALL treat the application as the authoritative authorization layer.
4. THE system SHALL NOT rely on a metadata filter as the tenant boundary.
5. THE system SHALL NOT adopt ACL-aware retrieval as an authorization mechanism in
   this phase.
6. WHERE a knowledge base is shared beyond its owner, THE system SHALL apply a
   resource policy for IAM-enforced `bedrock:Retrieve`.
7. WHEN a rehydration or replacement produces a new AWS_KB_Id, THE system SHALL
   re-apply any resource policy that was attached to the previous identifier.
8. WHEN a knowledge base's engine is migrated, THE system SHALL NOT change what a
   published agent retrieves.
9. WHILE an agent is listed in the marketplace, THE system SHALL exempt its
   knowledge base from lifecycle reclaim.
10. WHEN a listed agent transitions to `taken_down`, THE system SHALL require an
    explicit transition rather than allowing it to fall through to reclaim.
11. THE system SHALL NOT claim to resolve whether published agents pin a corpus
    revision; that question is owned by the marketplace spec and remains open.

> Rationale: closes evaluation gate §14.3. Managed KB ships two features whose names
> overstate what they provide. AWS's multi-tenant guidance calls metadata filtering
> *"filter-level (logical) isolation, not IAM-enforced (infrastructure) isolation"*,
> and states that ACL-aware retrieval *"is not authorization"* and does not
> authenticate users — its identity is **email only, with no alias resolution, and
> mismatches fail silently**. This platform authenticates via OIDC with claim
> mappings, so a silently-failing email match would be a worse primitive than an
> explicit app-side check. Because this phase holds `App_KB_Id == assistant_id`, the
> per-assistant boundary *is* a per-knowledge-base boundary, which is the strongest
> available isolation by construction. Resource policies are MANAGED-only and attach
> to the AWS knowledge base ARN, so a new identifier silently drops sharing (§11.1).
