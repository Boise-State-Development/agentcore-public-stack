# Implementation Plan: Managed Knowledge Base Migration

## Overview

Introduce Amazon Bedrock Managed Knowledge Base as a second retrieval backend
behind a single abstraction seam, then migrate knowledge bases to it one at a time,
opt-in, with rollback available throughout.

Task order enforces the deployment rule that **backend code never deploys before
the IAM and resources it requires**. Groups 1–2 are platform-only and change no
behaviour. Groups 3–11 land backend code that stays dark behind flags. Groups
12–13 enable the pilot and opt-in migration. Groups 14–15 add the user-facing
surfaces and the pre-promotion verification gate.

**Scope:** §14.7 phases 1–4 only. Managed-by-default, stopping legacy writes,
reclaiming legacy vectors, and removing the old pipeline are a follow-up spec.
All three flags — managed-default, migration, and reconciler arming — ship **off**.

## Tasks

- [x] 1. Platform: additive schema and IAM (no behaviour change)
  - [x] 1.1 Add the sparse work-discovery GSI to the assistants table
    - In `infrastructure/lib/constructs/rag/rag-data-construct.ts`, add GSI
      `KbWorkIndex` with partition key `GSI7_PK` and sort key `GSI7_SK`, both
      STRING, `projectionType: ALL`
    - **GSI7, not GSI1** — the table already has six indexes using `GSI_PK`/`GSI_SK`
      for the first and `GSI2_PK` through `GSI6_PK` thereafter
    - Follow the sparse pattern and comment style of the adjacent `DueSyncIndex`
      (GSI4), `AgentDirectoryIndex` (GSI5) and `AgentReportsIndex` (GSI6): keys are
      written only while the record is eligible, so ineligible and pinned knowledge
      bases are invisible to the dispatcher's query by physics rather than by filter
    - Add `GSI7_PK` / `GSI7_SK` to the generic assistant-update path's immutable
      attribute list, mirroring `GSI5_*`, so a routine edit cannot resurrect a work
      key on a knowledge base that has left the queue
    - ⚠️ **This consumes the entire `rag-assistants` GSI budget for whichever
      release ships it.** DynamoDB's `UpdateTable` permits exactly ONE GSI creation
      or deletion per call, and CloudFormation issues one `UpdateTable` per changed
      table, so a release that adds a second index to this table fails the deploy
      and rolls the whole stack back. This is not theoretical: it took production
      down on 2026-08-01 in release 1.12.0, when `AgentDirectoryIndex` and
      `AgentReportsIndex` arrived in separate `develop` merges and collapsed into a
      single prod update. If any other in-flight spec adds a GSI to
      `rag-assistants`, the two must ship in different releases.
    - Regenerate the committed inventory after adding the index:
      `cd infrastructure && UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit`, and
      confirm the diff is exactly one line. `infrastructure/test/gsi-update-limit.test.ts`
      fails until this is done, and `scripts/release/check-gsi-update-limit.mjs`
      re-checks it against `origin/main` on PRs into `main`.
    - _Requirements: 15.14, 15.13_

  - [x] 1.2 Create the Bedrock knowledge base service role
    - New construct `infrastructure/lib/constructs/managed-kb/managed-kb-role-construct.ts`
    - Trust policy: `bedrock.amazonaws.com` with `aws:SourceAccount` equal to the
      account and `ArnLike` on `AWS:SourceArn` scoped to `knowledge-base/*`
    - Grant S3 read on the documents bucket conditioned on `aws:ResourceAccount`
    - Grant `bedrock:InvokeModel` on `amazon.titan-embed-text-v2:0` only (required
      because Requirement 8.5 pins `embeddingModelType: CUSTOM`)
    - Grant `cloudwatch:PutMetricData` scoped to the non-reserved
      `${prefix}/ManagedKb` namespace. NOT `AWS/Bedrock/KnowledgeBases`: CloudWatch
      reserves every namespace beginning with `AWS` and rejects writes to them, so
      an `AWS/...`-scoped grant authorizes nothing while looking correct. Bedrock's
      own `AWS/Bedrock/KnowledgeBases` metrics are a read source (Req 20.13), not a
      publish target
    - Publish the role ARN to SSM at `/${prefix}/managed-kb/service-role-arn`
    - _Requirements: 20.1, 20.2, 20.4, 20.5, 20.10, 8.5, 8.9_

  - [x] 1.3 Grant caller permissions for provisioning, ingestion, and retrieval
    - Separate policy statements with distinct SIDs for: provisioner/migrator CRUD
      (`bedrock:CreateKnowledgeBase`, `CreateDataSource`, `DeleteKnowledgeBase`,
      `DeleteDataSource`, `ListKnowledgeBases`, `GetKnowledgeBase`), direct
      ingestion (`IngestKnowledgeBaseDocuments`, `DeleteKnowledgeBaseDocuments`,
      `GetKnowledgeBaseDocuments`), and inference (`bedrock:Retrieve`)
    - Add `iam:PassRole` on the service role conditioned on `iam:PassedToService`
      equal to `bedrock.amazonaws.com`
    - Attach retrieval to the AgentCore Runtime role and the App API task role;
      attach CRUD only to the migration Lambdas' roles
    - _Requirements: 20.3, 20.6_

  - [x] 1.4 Write CDK assertions for the IAM conditions
    - New `infrastructure/test/managed-kb.test.ts`, following
      `infrastructure/test/kb-sync.test.ts`
    - Assert the `aws:SourceAccount` and `ArnLike` `AWS:SourceArn` conditions, the
      `iam:PassedToService` condition, the `aws:ResourceAccount` S3 condition, and
      the presence of the `PutMetricData` grant on the calling identities, and its
      **absence** on the service role
    - Assert the S3 statement's **Resource** as well as its Condition:
      `aws:ResourceAccount` scopes the account, not the bucket, so without a
      Resource assertion the grant can widen to every bucket in the account (file
      uploads, fine-tuning, artifacts, SPA) with all tests still green
    - Assert the `PutMetricData` namespace does not begin with `AWS`, so nobody
      reverts it to the reserved `AWS/Bedrock/KnowledgeBases` namespace that
      authorizes no publish
    - The `PutMetricData` assertion matters because metric publishing is
      best-effort: omit the grant and metrics silently vanish while requests keep
      succeeding
    - _Requirements: 20.9, 24.10, 24.13_

- [x] 2. Platform: worker resources and config
  - [x] 2.1 Add the migration construct with dispatcher, worker, and reconciler
    - New `infrastructure/lib/constructs/managed-kb/kb-migration-construct.ts`,
      following `infrastructure/lib/constructs/kb-sync/kb-sync-construct.ts`
    - Three DockerImage Lambdas sharing ONE image
      (`backend/Dockerfile.kb-migration`)
    - Byte-stable bootstrap stub at
      `infrastructure/bootstrap-assets/kb-migration/`, per the
      platform-as-bootstrap pattern
    - Publish generated function names to SSM under `/${prefix}/kb-migration/`
    - EventBridge `rate()` schedule into the dispatcher and into the reconciler
    - Wire the construct in `infrastructure/lib/platform-stack.ts`
    - _Requirements: 14.1, 15.13, 15.14_

  - [x] 2.2 Add the ingestion consumer Lambda
    - Same construct; triggered by the documents bucket `ObjectCreated`
      notification, wired in `platform-stack.ts` alongside the existing
      notification to avoid a circular dependency
    - Timeout ≥300 s (a 50 KiB PDF was measured at 264 s) and a dead-letter queue
    - _Requirements: 10.1, 10.9_

  - [x] 2.3 Add configuration properties and flags
    - In `infrastructure/lib/config.ts`, add a `managedKb` section carrying
      `newDefault`, `migrationEnabled`, `reconcilerArmed`, per-owner byte cap
      defaults by role tier, and the retention window in days
    - Follow the 7-step config pattern: `config.ts` interface → `loadConfig` →
      construct → `scripts/common/load-env.sh` → `synth.sh` and `deploy.sh`
      (identical context flags) → workflow job-level `env:` → GitHub variable
    - All three booleans default to **false**, and an empty string resolves to
      false
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.8, 12.2, 14.7, 15.11_

  - [x] 2.4 Add tagging for reconciliation and teardown
    - Tag every runtime-created knowledge base with `prefix`, `env`, `appKbId`, and
      an opaque `ownerUserId`
    - The owner tag must be an opaque identifier, never an email address or other
      PII
    - This is a hard prerequisite, not housekeeping: the Reconciler's tag-filtered
      `ListKnowledgeBases` and the teardown script both read these tags
    - _Requirements: 20.11, 20.12_

  - [x] 2.5 Add account-level alarms
    - New alarms in the managed-kb construct on total managed storage, managed
      knowledge base count against 80% of the 10,000 quota, daily
      Knowledge-Base `usagetype` cost, and sustained non-zero `KbOrphansFound`
    - Use `TreatMissingData.NOT_BREACHING`, matching the posture of the existing
      kb-sync, scheduled-runs and prompt-cache observability constructs
    - Per-owner caps bound one user; these bound the fleet, and the gap between
      ~$169/month expected and ~$15,000/month permitted is why they are required
    - _Requirements: 12.13_

- [x] 3. KB_Record data layer
  - [x] 3.1 Define the KB_Record model
    - New `backend/src/apis/shared/kb_backend/records.py`
    - Keys `PK=AST#{assistant_id}`, `SK=KB#{app_kb_id}`, with
      `app_kb_id == assistant_id` in this phase
    - Fields per the design's data-model table, including `retrievalEngine`,
      `provisioningState`, `awsKbId`, `awsDataSourceId`, immutable embedding
      config, `storedBytes`, `reservedBytes`, `lastRetrievedAt`, migration state
      with generation and lease, and lifecycle exemption flags
    - _Requirements: 6.1, 6.2, 6.5_

  - [x] 3.2 Implement conditional state transitions
    - `create_provisioning`, `attach_aws_ids`, `promote_engine`,
      `rollback_engine`, `set_migration_state`, `acquire_lease`
    - Every transition uses a DynamoDB condition expression; `promote_engine` is
      conditional on converged catch-up so two workers cannot both promote
    - Sparse GSI attributes are written on entering an eligible state and
      **removed** on reaching a terminal state
    - _Requirements: 15.8, 15.10, 15.13, 17.1, 17.5_

  - [x] 3.3 Write property test for engine resolution by absence
    - **Property 1: absence means legacy**
    - Using `hypothesis`, for any KB_Record shape with no `retrievalEngine`
      attribute, verify resolution returns the legacy backend, and verify no code
      path writes the literal `"s3vectors"` to a record that did not already carry
      it
    - **Validates: Requirements 1.6, 1.7, 6.6**
    - File: `backend/tests/property/test_pbt_kb_engine_resolution.py`

  - [x] 3.4 Write unit tests for conditional transitions
    - Concurrent `create_provisioning` yields exactly one winner
    - Concurrent `promote_engine` yields exactly one winner
    - Terminal transitions remove the GSI attributes
    - File: `backend/tests/shared/test_kb_records.py`
    - _Requirements: 7.4, 15.10, 15.13_

- [x] 4. Backend abstraction seam
  - [x] 4.1 Define the protocol and canonical chunk shape
    - New `backend/src/apis/shared/kb_backend/protocol.py`
    - `KnowledgeBaseBackend` Protocol with `search`, `ingest`, `delete_document`
    - Frozen `Chunk` dataclass whose score field is named `relevance` and is
      documented as higher-is-more-relevant
    - _Requirements: 1.1, 2.1_

  - [x] 4.2 Implement the backend resolver
    - New `backend/src/apis/shared/kb_backend/resolver.py`
    - Reads `retrievalEngine` from the KB_Record; absence resolves to
      `S3VectorsBackend`
    - _Requirements: 1.4, 1.6_

  - [x] 4.3 Extract the legacy backend verbatim
    - New `backend/src/apis/shared/kb_backend/s3vectors_backend.py`
    - Move the existing S3 Vectors search path from
      `apis/shared/assistants/rag_service.py` and
      `apis/shared/embeddings/bedrock_embeddings.py` without functional change
    - Convert S3 Vectors cosine **distance** to **relevance** inside this adapter
    - _Requirements: 1.2, 1.3, 2.2_

  - [x] 4.4 Convert the entry point into a facade
    - In `apis/shared/assistants/rag_service.py`, reduce
      `search_assistant_knowledgebase_with_formatting(assistant_id, query, top_k=5)`
      to resolve-then-delegate, preserving its public signature
    - Keep emitting a `distance` key in the formatted result, derived from
      `relevance`, so no existing consumer breaks on the field rename
    - Neither of the two call sites
      (`inference_api/chat/routes.py`, `app_api/assistants/routes.py`) changes
    - _Requirements: 1.5, 3.4_

  - [x] 4.5 Write property test for score direction equivalence
    - **Property 2: ranking is backend-independent**
    - Using `hypothesis`, for any list of chunks with distinct scores, verify both
      backends return the known-best chunk first after adapter conversion
    - This is the only test that can catch a silent ranking inversion; without it
      the failure mode produces no error, just worse answers
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 24.1**
    - File: `backend/tests/property/test_pbt_kb_score_direction.py`

  - [x] 4.6 Apply the document-status filter above the seam, on both backends
    - Move the `status == "complete"` post-filter into the facade so there is one
      implementation covering both backends
    - It works on the managed path only because `customDocumentIdentifier` is the
      platform `document_id` (task 8.4); the filter needs a `document_id` per chunk
      to join on
    - Keep it on the managed path even though managed ingestion makes it largely
      redundant — removing it in the same change that swaps the engine would
      confound the comparison
    - Apply the 2,000-character context cap in the same place, for the same reason
    - _Requirements: 3.2, 3.3_

  - [x] 4.7 Write test for parity properties on the managed path
    - Assert `top_k=5`, the 2,000-character cap, the status filter, and the
      500-character citation clip all hold on the managed backend, not just legacy
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 4.8 Write architecture test for the Lambda import constraint
    - Assert `apis.shared.kb_backend` does not transitively import
      `apis.shared.assistants`, whose `__init__` drags in the embeddings stack
    - Add alongside the existing boundary tests in `backend/tests/architecture/`
    - Keep `kb_backend/__init__.py` empty and heavy imports function-local, matching
      the convention in `kb_sync/records.py`
    - _Requirements: 24.15_

- [x] 5. Query clamp
  - [x] 5.1 Implement the query guard
    - New `backend/src/apis/shared/kb_backend/query_guard.py` with
      `MAX_QUERY_CHARS = 10_000`
    - Applied in the facade before backend dispatch so both backends are protected
      identically; never raises
    - Emit a `KbQueryClamped` metric on truncation
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 22.3_

  - [x] 5.2 Remove the stale no-validation assertion
    - In `apis/shared/embeddings/bedrock_embeddings.py`, delete the inline comment
      stating the query is a "short string, no token validation needed"
    - It is true only because Titan v2 tolerates ~32,000 characters; Managed KB
      caps `Retrieve` input at 10,000 and the limit is not adjustable
    - _Requirements: 4.5_

  - [x] 5.3 Write property test for the clamp
    - **Property 3: clamp is total and non-throwing**
    - Using `hypothesis`, for any input string of any length, verify the output is
      at most 10,000 characters, the function never raises, and a truncation signal
      is emitted exactly when the input exceeded the cap
    - **Validates: Requirements 4.1, 4.3, 4.4**
    - File: `backend/tests/property/test_pbt_kb_query_clamp.py`

- [x] 6. Fail-closed document status filter
  - [x] 6.1 Make the status filter fail closed
    - In `apis/shared/assistants/rag_service.py`, change
      `_filter_vectors_by_document_status` so both fallback paths drop chunks
      instead of returning them unfiltered:
      the missing-table-name branch (currently `valid_doc_ids = doc_ids`) and the
      outer exception handler (currently `valid_doc_ids = doc_ids  # Graceful
      degradation`)
    - Leave the per-document handler as-is; it already fails closed
    - Emit `KbStatusFilterFailClosed` at error level, distinct from an ordinary
      empty-result log line
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 22.4_

  - [x] 6.2 Record the supersession in the prior spec
    - In `.kiro/specs/reliable-document-deletion/requirements.md`, annotate
      Requirement 3.4 as superseded by Requirement 5 of this spec
    - That requirement specified the fail-open deliberately, so retiring it is a
      supersession and must be recorded rather than silently contradicted
    - _Requirements: 5.5_

  - [x] 6.3 Write property test for fail-closed behaviour
    - **Property 4: unconfirmable status never leaks**
    - Using `hypothesis`, for any set of vectors and any injected table-level
      failure or missing table-name condition, verify zero chunks are returned
    - **Validates: Requirements 5.1, 5.2, 24.6**
    - File: `backend/tests/property/test_pbt_kb_status_fail_closed.py`

  - [x] 6.4 Update existing tests that assert the fail-open contract
    - Search `backend/tests/` for tests asserting unfiltered fallback and invert
      their expectations, citing this spec's Requirement 5
    - _Requirements: 5.5_

- [x] 7. Byte cap accounting
  - [x] 7.1 Implement reserve / commit / release
    - New `backend/src/apis/shared/kb_backend/byte_cap.py`
    - `reserve` is a conditional update failing when
      `storedBytes + reservedBytes + n > cap`; `commit` moves reserved to stored;
      `release` returns the reservation on failure
    - Resolve the per-owner cap by role tier, defaulting **below** the existing
      1 GB user-files precedent
    - Determine size from an S3 `HEAD` on the stored object, never from a
      client-reported value
    - Do not read `RawDataSize` for enforcement; it returned 0 datapoints for a
      directly-ingested document and remains unconfirmed
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.6, 12.7, 12.8_

  - [x] 7.2 Document the retrieval-quota payer decision
    - Record in the design whether the knowledge base owner or the invoking user
      consumes retrieval quota, and implement accordingly
    - _Requirements: 12.10_

  - [x] 7.3 Write property test for reservation races
    - **Property 5: the cap is never exceeded under concurrency**
    - Using `hypothesis`, for any interleaving of N concurrent reserve/commit
      operations against a cap, verify the committed total never exceeds the cap
      and released reservations are fully returned
    - **Validates: Requirements 12.4, 12.5, 12.6, 24.7**
    - File: `backend/tests/property/test_pbt_kb_byte_cap.py`

  - [x] 7.4 Enforce the cap on the migration re-ingest path
    - The Migration_Worker reserves for the **whole snapshot** before entering
      `shadow`, and fails the migration up front rather than part-migrating a corpus
      that cannot fit
    - Surface the failure as a plain-language reason with the option to request an
      elevated tier
    - Migration is the largest byte-adding operation in the system and the only one
      that runs unattended, so it is both the easiest and the worst place to omit
      the check
    - Emit `KbByteCapRejected` on rejection
    - _Requirements: 12.11, 12.12, 12.14_

  - [x] 7.5 Write test for migration byte-cap rejection
    - A corpus exceeding the owner's remaining allowance fails before `shadow`, and
      leaves no partially-ingested managed knowledge base behind
    - _Requirements: 12.11, 12.12_

- [x] 8. Managed backend: provisioning and retrieval
  - [x] 8.1 Implement the provisioning saga
    - New `backend/src/apis/shared/kb_backend/provisioning.py`
    - Write the KB_Record in `provisioning` **before** calling AWS; attach returned
      ids with a conditional update
    - `CreateKnowledgeBase` with `type="MANAGED"`, `roleArn`, and
      `managedKnowledgeBaseConfiguration` carrying the embedding pin (it has no
      required members, but the pin has nowhere else to live — NOT literally `{}`);
      omit `storageConfiguration` entirely
    - Build the `clientToken` programmatically to satisfy the **33-character
      minimum** and persist it so a retry reuses it — a natural
      `{id}-{variant}-kb` token is 31 characters and fails client-side validation
    - Treat "unable to verify the specified embedding model" as **retryable**; it
      was observed as pure IAM eventual consistency against a model confirmed
      ACTIVE and invokable
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 8.1, 8.2_

  - [x] 8.2 Create the CUSTOM connector data source
    - `dataSourceConfiguration.type = "MANAGED_KNOWLEDGE_BASE_CONNECTOR"` with the
      real type in
      `managedKnowledgeBaseConnectorConfiguration.connectorParameters`
    - `embeddingModelType: CUSTOM` pinned to `amazon.titan-embed-text-v2:0`,
      `FLOAT32` (upper-case: that is the service-model enum value), 1024 dimensions
    - `mediaExtractionConfiguration.imageExtractionConfiguration.imageExtractionStatus
      = ENABLED` — opt-in, and silently indexes no chart or image content if left
      default
    - `dataDeletionPolicy = RETAIN` at creation, the documented remedy for the
      `DELETE_UNSUCCESSFUL` state already present in the dev account
    - _Requirements: 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [x] 8.3 Implement managed retrieval
    - New `backend/src/apis/shared/kb_backend/managed_backend.py`
    - Use `managedSearchConfiguration` with `numberOfResults=5` and
      `rerankingModelType="MANAGED"`; never send `vectorSearchConfiguration`, which
      is rejected outright for managed knowledge bases
    - Do not attempt to configure hybrid search; it is not toggleable
    - Constrain any isolation-critical filter to `equals` or `in`
    - Run synchronous boto3 calls off the event loop
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 3.1, 20.7_

  - [x] 8.4 Implement direct ingestion and document delete
    - `IngestKnowledgeBaseDocuments` batched at **10 documents maximum**,
      server-enforced; the user guide's claim of 25 does not apply to managed
      knowledge bases
    - `customDocumentIdentifier = document_id`, which retires the
      `{doc_id}#{chunk_index}` scheme including `delete_vector_tail` and the
      chunk-shrinkage stash on this path
    - Never call `StartIngestionJob` — 0.1 RPS account-wide and not adjustable
    - Bound concurrency against the 10-per-account concurrent document-operation
      limit
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [x] 8.5 Write unit tests with stubbed AWS APIs
    - Stub `bedrock-agent` and the agent runtime client; never call live
    - Cover create/ingest/delete idempotency, the 10-document batch boundary,
      retryable embedding-verification failure, and `clientToken` length ≥33
    - File: `backend/tests/shared/test_managed_kb_backend.py`
    - _Requirements: 24.2, 24.11_

  - [x] 8.6 Write test for crash between AWS create and record update
    - Simulate a crash after `CreateKnowledgeBase` returns but before the
      conditional update; verify the record remains a discoverable retry anchor and
      that a retry does not create a second knowledge base
    - _Requirements: 7.8, 24.3_

- [x] 9. Ingestion control plane
  - [x] 9.1 Implement the ingestion consumer
    - New `backend/src/apis/app_api/kb_migration/ingestion_consumer.py`
    - Follow `kb_sync/records.py`'s raw-table-access convention: importing
      `apis.shared.assistants` drags in the whole embeddings stack, and keeping the
      Lambda image small is a deliberate constraint
    - Resolve each document's knowledge base and engine, then route legacy
      documents to the existing pipeline and managed documents to direct ingestion
    - Never index the same document on both backends outside a deliberate migration
      or pilot
    - Poll until **actually retrievable**, recording `indexedAt` and
      `retrievableAt` as two distinct timestamps
    - Update `DOC#` to a terminal state with bounded retries and a durable retry
      anchor
    - No in-process `asyncio.ensure_future` orchestration
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8_

  - [x] 9.2 Write unit tests for routing exclusivity
    - Legacy document routes to the old pipeline only; managed document routes to
      direct ingestion only; neither is double-indexed
    - File: `backend/tests/lambdas/test_kb_ingestion_consumer.py`
    - _Requirements: 10.3, 10.4, 10.5_

- [x] 10. Deletion sagas and reconciler
  - [x] 10.1 Implement tombstones
    - New `backend/src/apis/shared/kb_backend/tombstones.py`
    - Write `KBTOMB#{app_kb_id}` (and the `#DOC#{document_id}` variant) **before**
      calling AWS; clear only after AWS confirms absence
    - No TTL on tombstones — TTL removal would recreate the silent-leak class this
      design exists to close
    - Verify knowledge base deletion by polling `ListKnowledgeBases` until the name
      disappears, tolerating ≥6 minutes; deletion took 2–6 minutes when measured
    - Never delete the service role until all of its knowledge bases are confirmed
      absent, and never delete the last KB_Record before AWS confirms
    - Surface `DELETE_UNSUCCESSFUL` as an actionable operator state
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8_

  - [x] 10.2 Implement the daily reconciler
    - New `backend/src/apis/app_api/kb_migration/reconciler.py`
    - Join paginated, tag-filtered `ListKnowledgeBases` against KB_Records
    - AWS-only ⇒ orphan, deleted only if the **AWS-reported `createdAt`** is >24 h
      old; age-gating on discovery time would make a reconciler that was down for a
      week delete every in-flight create
    - Record-only ⇒ mark `vectorState: missing` and **never** delete the record;
      the documents are still valid
    - Both ⇒ refresh `storedBytes`
    - Ship in **report-only** mode, which logs intended deletions and deletes
      nothing; arming is a separate flag that treats an empty string as off
    - Apply a bounded per-run action limit
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8, 19.7_

  - [x] 10.3 Write reconciliation tests
    - Record-only and AWS-only outcomes; age-gate honours AWS `createdAt` rather
      than discovery time; report-only performs no deletes
    - File: `backend/tests/lambdas/test_kb_reconciler.py`
    - _Requirements: 24.4_

- [x] 11. Authorization, isolation, and publication
  - [x] 11.1 Resolve access before retrieval
    - In the facade, resolve the invoking user's access to the knowledge base
      **before** attempting retrieval, reusing the existing assistant permission
      model rather than introducing a parallel one
    - Because this phase holds `App_KB_Id == assistant_id`, "can this user invoke
      this agent" already answers "may this turn retrieve"; do not build for the
      0..N case, which is F4's problem
    - _Requirements: 25.1, 25.2, 25.3_

  - [x] 11.2 Keep filters out of the tenant boundary
    - Do not use a metadata filter as the isolation mechanism; the per-knowledge-base
      boundary is the tenant boundary in this phase
    - Do not adopt ACL-aware retrieval: its identity is email-only with no alias
      resolution and mismatches fail silently, which is a worse primitive than an
      explicit app-side check on an OIDC claim-mapped platform
    - _Requirements: 25.4, 25.5, 11.5_

  - [x] 11.3 Apply resource policies for shared knowledge bases
    - Where a knowledge base is shared beyond its owner, attach a resource policy
      granting IAM-enforced `bedrock:Retrieve`
    - Re-apply the policy whenever a new `awsKbId` is produced; policies attach to
      the AWS ARN, so a replacement silently drops sharing
    - _Requirements: 25.6, 25.7_

  - [x] 11.4 Preserve published-agent semantics
    - An engine migration must not change what a published agent retrieves; parity
      is the contract, so a swap is not a corpus change and needs no re-review
    - Exempt listed agents' knowledge bases from lifecycle reclaim; `taken_down`
      requires an explicit transition rather than falling through
    - Do not attempt to resolve corpus-revision pinning; it belongs to the
      marketplace spec
    - _Requirements: 25.8, 25.9, 25.10, 25.11_

  - [x] 11.5 Write authorization tests
    - Viewer can read through the agent but never sees the upgrade control; a user
      with no access never reaches retrieval; access checks fail closed
    - Published-agent corpus behaviour and reclaim exemption
    - Resource policy is re-applied after a new `awsKbId`
    - _Requirements: 24.6, 24.12, 24.14_

  - [x] 11.6 Write test asserting the 1:1 binding freeze
    - Assert an explicit `knowledge_base` binding is still rejected by
      `binding_validation.py` and that `bindable_catalog.py` still returns an empty
      list for it, so the freeze is enforced by test rather than by intention
    - _Requirements: 6.7, 6.8_

- [x] 12. Dual-read pilot
  - [x] 12.1 Implement opt-in dual read
    - In the facade, when a knowledge base is flagged for the pilot, run both
      backends for the same query and **serve legacy**
    - Record per read: overlap in returned `document_id` values, a rank
      correlation, and per-backend latency
    - Default off; must not increase user-visible latency beyond the legacy path's
      own latency
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [x] 12.2 Write dual-read tests
    - Legacy results are always the ones served; comparison metrics are emitted;
      a managed-side failure does not fail the turn
    - File: `backend/tests/shared/test_kb_dual_read.py`
    - _Requirements: 18.2, 18.5_

- [x] 13. Migration dispatcher and worker
  - [x] 13.1 Implement the dispatcher
    - New `backend/src/apis/app_api/kb_migration/dispatcher.py`, following
      `kb_sync/dispatcher.py`
    - Query the sparse `KbWorkIndex`, apply a bounded per-tick dispatch limit
      (mirroring `KB_SYNC_DISPATCH_LIMIT`, default 20), and no-op entirely when the
      migration flag is off
    - _Requirements: 19.6, 15.14_

  - [x] 13.2 Implement the migration worker state machine
    - New `backend/src/apis/app_api/kb_migration/worker.py`
    - `shadow`: provision, then re-ingest every `complete` document from its
      existing S3 key at `assistants/{assistant_id}/documents/{document_id}/{filename}`
      — never ask the user to re-supply anything
    - `verify`: compare an exact source manifest of `document_id` + content hash or
      generation, **not** document-count parity, then run a canary retrieval
    - `promote`: single conditional write of `retrievalEngine="managed"`, only after
      a converged catch-up pass **and** only once the Byte_Cap is enforced on this
      knowledge base — no traffic is promoted to an unmetered corpus
    - `retain`: set `retainUntil` at least 30 days out
    - Take a lease so one knowledge base is never migrated by two workers
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.9, 15.11, 15.13, 12.9_

  - [x] 13.3 Implement catch-up convergence
    - Snapshot the doc-id set, migrate, then run catch-up passes until a pass finds
      nothing new — the same converge-on-quiet shape as the crawler's
      consecutive-miss rule
    - Re-read each document's `DOC#` record immediately before ingesting and skip it
      if gone or no longer `complete`, so a document deleted mid-migration cannot
      resurrect
    - Do not implement dual-write; one write path stays authoritative until
      promotion
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6_

  - [x] 13.4 Implement rollback
    - Write `retrievalEngine` back to its prior value and stamp `rolledBackAt`;
      move no data
    - Available for the entire `retain` window; a pre-promotion failure leaves the
      knowledge base on legacy and fully usable
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

  - [x] 13.5 Write property test for migration idempotency
    - **Property 6: interrupted migration converges without duplication**
    - Using `hypothesis`, for any interruption point in the state machine, verify a
      resumed run reaches the same terminal state, creates exactly one knowledge
      base, and ingests each document at most once
    - **Validates: Requirements 15.9, 15.10, 15.13, 7.4**
    - File: `backend/tests/property/test_pbt_kb_migration_convergence.py`

  - [x] 13.6 Write tests for interference during migration
    - Upload during migration is picked up by catch-up; delete during migration
      never resurrects; concurrent promotion attempts yield one winner
    - File: `backend/tests/lambdas/test_kb_migration_worker.py`
    - _Requirements: 16.2, 16.4, 16.5, 24.5_

  - [x] 13.7 Write test for resource-policy re-application after rehydration
    - A rehydration producing a new `awsKbId` re-applies the resource policy;
      policies attach to the AWS ARN, so a new id otherwise silently drops sharing
    - _Requirements: 24.12_

  - [x] 13.8 Write test for mixed old/new deployment
    - Old and new code serving simultaneously; a record with no `retrievalEngine`
      resolves to legacy under both
    - _Requirements: 1.6, 24.8_

- [ ] 14. Surfaces, observability, and teardown
  - [ ] 14.1 Emit EMF metrics
    - Alongside the existing PromptCache metrics: `KbCount`, `KbStorageGB`,
      `KbIdleGB`, `KbOrphansFound`, `KbQueryClamped`,
      `KbStatusFilterFailClosed`, and
      `KbMigration{Started,Promoted,Failed,RolledBack}`
    - Compute idleness as `max(own lastRetrievedAt, max(lastUsedAt) over bound
      agents)`, never retrieval alone, or an actively used agent's knowledge base is
      evicted because its queries did not match
    - Write `lastRetrievedAt` through a throttled conditional write (one winner per
      24 h), never per retrieval; prefer per-knowledge-base `Invocations` from
      `AWS/Bedrock/KnowledgeBases` where sufficient — that is a *read* of Bedrock's
      own namespace and needs `cloudwatch:GetMetricData` /
      `GetMetricStatistics` (Req 20.13). Our own metrics above publish to
      `${prefix}/ManagedKb` (Req 20.10), never into an `AWS/...` namespace
    - _Requirements: 22.1, 22.2, 22.5, 22.6, 22.8, 20.13_

  - [ ] 14.2 Document cost attribution
    - Record that Managed KB bills under `AmazonBedrockAgentCore` and that queries
      must filter on `usagetype` — keying on `AmazonBedrock` misses it entirely, and
      keying on service code alone blends it into the AgentCore Runtime memory line
    - _Requirements: 22.7_

  - [ ] 14.3 Build the upgrade UX
    - In `frontend/ai.client/src/app/knowledge-base/knowledge-base-section.component.ts`,
      add the opt-in upgrade card, non-blocking progress, one-time success notice,
      and a failure state with a retry control
    - Show nothing at all for a legacy knowledge base needing no action
    - Never use the word "vector" in user-facing copy
    - Gate on the existing `_require_edit_permission`; viewers never see the control
    - Angular signals, `OnPush`, Tailwind utilities, both light and dark modes,
      WCAG AA
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8_

  - [ ] 14.4 Surface failed and stuck documents
    - During the upgrade flow, list any non-`complete` document that will not be
      carried across and offer retry; 200 of 1,692 production `DOC#` records
      (11.8%) are affected, including 95 `failed` whose owners believe the uploads
      worked
    - Distinguish an unsupported file format from a processing failure in messaging
    - _Requirements: 21.1, 21.2, 21.3, 21.4_

  - [ ] 14.5 Build the admin surface
    - Knowledge bases filterable by engine, with stored bytes and document counts,
      bulk migrate, and per-knowledge-base retry
    - _Requirements: 23.9_

  - [ ] 14.6 Extend teardown for runtime-created resources
    - In `scripts/teardown/destroy.sh`, list and delete only knowledge bases tagged
      for the project and environment, **before** deleting their service role and
      the platform stack
    - Poll until each resource is confirmed absent; "delete call accepted" is not
      "resource gone"
    - _Requirements: 20.8, 13.4, 13.5_

  - [ ] 14.7 Write teardown test
    - Only tagged resources are deleted, and the service role is deleted only after
      all its knowledge bases are confirmed absent
    - _Requirements: 24.9_

- [ ] 15. Pre-promotion verification
  - [ ] 15.1 Run the packaged-SDK contract probe
    - Using the checked-in environment with **no** `AWS_DATA_PATH` override, run a
      create → ingest → retrieve smoke probe against dev-ai
    - This is the contract test that the pinned `boto3==1.43.68` and its packaged
      service model are genuinely sufficient, rather than the side-loaded model the
      evaluation used
    - _Requirements: 8.1, 9.1, 11.1_

  - [ ] 15.2 Probe for an account-level ingestion-concurrency limit
    - During the pilot, run a many-knowledge-base backfill to determine whether an
      account-level ingestion-concurrency limit exists; the quota page lists none
    - Do not size a wide fleet migration before this is answered
    - _Requirements: 9.5_

  - [ ] 15.3 Confirm the full test matrix passes
    - Run the backend suite, the infrastructure suite, and `mypy`/`ruff` inside the
      dev container
    - Verify every Requirement 24 item has a corresponding passing test
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7, 24.8, 24.9, 24.10, 24.11, 24.12, 24.13, 24.14, 24.15_
