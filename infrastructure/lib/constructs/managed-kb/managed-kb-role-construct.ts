import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/**
 * Embedding model the Managed_KB provisioner pins via
 * `embeddingModelType: CUSTOM` (Requirement 8.5 — float32, 1024 dims).
 *
 * The CUSTOM choice is precisely why the service role needs
 * `bedrock:InvokeModel` at all: managed embedding runs inside the
 * Bedrock service account and needs no caller-visible model grant,
 * whereas CUSTOM embedding is invoked *as the service role*. Keep this
 * a single-model allow-list — widening it to `foundation-model/*` would
 * hand every Bedrock model in the account to the KB service.
 */
export const MANAGED_KB_EMBEDDING_MODEL_ID = 'amazon.titan-embed-text-v2:0';

/**
 * CloudWatch namespace the Managed_KB code paths publish their OWN
 * custom metrics into — `KbByteCapRejected`, `KbOrphansFound`,
 * `KbIdleGB` and the rest of the design's metrics table
 * (Requirement 20.10).
 *
 * NOT an `AWS/...` namespace, and this must not be "corrected" back to
 * one. CloudWatch reserves every namespace beginning with `AWS` for its
 * own services and rejects `PutMetricData` into them, so a grant scoped
 * to `AWS/Bedrock/KnowledgeBases` would authorize no publish that can
 * ever succeed — a permission that looks right and silently does
 * nothing. Bedrock's own per-knowledge-base metrics do live in
 * `AWS/Bedrock/KnowledgeBases`, but those are *read* (Requirement
 * 20.13's `cloudwatch:GetMetricData` / `GetMetricStatistics`), never
 * written by us.
 *
 * Prefixed with the project prefix so two environments in one account
 * do not blend their metrics together.
 *
 * The grant is deliberately namespace-conditioned: metric publishing is
 * best-effort, so an over-broad grant buys nothing while a missing one
 * makes metrics vanish silently.
 */
export function managedKbMetricNamespace(config: AppConfig): string {
  return `${config.projectPrefix}/ManagedKb`;
}

/** Every knowledge base in this account+region. */
function knowledgeBaseArnWildcard(config: AppConfig): string {
  return `arn:aws:bedrock:${config.awsRegion}:${config.awsAccount}:knowledge-base/*`;
}

/**
 * Namespace-conditioned `cloudwatch:PutMetricData`.
 *
 * Requirement 20.10 wants this on the service role *and* on every
 * calling identity, so each grant helper below attaches its own copy
 * under its own SID. Same statement body, distinct SIDs: a role can
 * receive two different grants (say provisioning and ingestion) without
 * tripping CloudFormation's rejection of duplicate SIDs inside one
 * policy document.
 */
function putMetricDataStatement(config: AppConfig, sid: string): iam.PolicyStatement {
  return new iam.PolicyStatement({
    sid,
    effect: iam.Effect.ALLOW,
    actions: ['cloudwatch:PutMetricData'],
    // PutMetricData has no resource-level permissions; the namespace
    // condition is the only available scope.
    resources: ['*'],
    conditions: {
      StringEquals: { 'cloudwatch:namespace': managedKbMetricNamespace(config) },
    },
  });
}

/**
 * Inference-side grant: query an existing Managed_KB and nothing else
 * (Requirement 20.6). Attached to the AgentCore Runtime execution role
 * and the App API task role — neither may create or delete knowledge
 * bases, so the CRUD grant below is deliberately kept away from them.
 */
export function grantManagedKbRetrieval(config: AppConfig, role: iam.IRole): void {
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbRetrieve',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock:Retrieve'],
    resources: [knowledgeBaseArnWildcard(config)],
  }));
  role.addToPrincipalPolicy(putMetricDataStatement(config, 'ManagedKbRetrieveMetrics'));
}

/**
 * Provisioner / migrator grant: knowledge-base and data-source CRUD,
 * plus the `iam:PassRole` needed to hand the service role to Bedrock at
 * create time (Requirements 20.3, 20.6).
 *
 * Intentionally not attached to anything yet — the migration Lambdas
 * and their roles arrive in task 2.1, which calls
 * `ManagedKbRoleConstruct.grantProvisioning()`. Defining it here keeps
 * the whole IAM surface of the feature in one reviewable file and lets
 * task 1.4 assert the conditions before the callers exist.
 */
export function grantManagedKbProvisioning(
  config: AppConfig,
  role: iam.IRole,
  serviceRoleArn: string,
): void {
  // Create and List take no resource-level ARN: at Create time the
  // knowledge base has no ARN yet, and List is an account-wide
  // collection read. Scoping is therefore by action, not resource —
  // the two resource-scopable halves live in the next statement.
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbProvisionCreateList',
    effect: iam.Effect.ALLOW,
    actions: ['bedrock:CreateKnowledgeBase', 'bedrock:ListKnowledgeBases'],
    resources: ['*'],
  }));
  // `knowledge-base/*` also covers data-source ARNs, which nest under
  // it (`knowledge-base/{kbId}/data-source/{dsId}`) — IAM's `*` spans
  // path separators.
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbProvisionCrud',
    effect: iam.Effect.ALLOW,
    actions: [
      'bedrock:GetKnowledgeBase',
      'bedrock:DeleteKnowledgeBase',
      'bedrock:CreateDataSource',
      'bedrock:DeleteDataSource',
    ],
    resources: [knowledgeBaseArnWildcard(config)],
  }));
  // Confused-deputy guard on the pass itself: this principal may hand
  // the service role to Bedrock and to nothing else (Requirement 20.3).
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbPassServiceRole',
    effect: iam.Effect.ALLOW,
    actions: ['iam:PassRole'],
    resources: [serviceRoleArn],
    conditions: { StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' } },
  }));
  role.addToPrincipalPolicy(putMetricDataStatement(config, 'ManagedKbProvisionMetrics'));
}

/**
 * Direct-ingestion grant: push document bytes straight at a Managed_KB
 * without an S3 data-source crawl (Requirement 20.6). Kept separate
 * from CRUD so an ingestion-only caller can never delete a knowledge
 * base.
 *
 * Also intentionally unattached for now — wired in task 2.1 alongside
 * the migration Lambdas, via
 * `ManagedKbRoleConstruct.grantDirectIngestion()`.
 */
export function grantManagedKbDirectIngestion(config: AppConfig, role: iam.IRole): void {
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbDirectIngestion',
    effect: iam.Effect.ALLOW,
    actions: [
      'bedrock:IngestKnowledgeBaseDocuments',
      'bedrock:DeleteKnowledgeBaseDocuments',
      'bedrock:GetKnowledgeBaseDocuments',
    ],
    resources: [knowledgeBaseArnWildcard(config)],
  }));
  role.addToPrincipalPolicy(putMetricDataStatement(config, 'ManagedKbIngestMetrics'));
}

/**
 * Sharing grant: administer a knowledge base's *resource* policy
 * (Requirement 25.6). Separate from every other grant because it is the
 * only one that can change who else may read a corpus — a caller that
 * ingests documents has no business rewriting that.
 *
 * `bedrock:GetDocumentContent` appears in the policy documents this
 * caller writes but is deliberately absent from this grant: writing a
 * policy that permits an action is not the same as holding it, and the
 * writer is a control-plane path that never reads document bytes.
 *
 * No `PutResourcePolicy` condition is available to pin the policy's
 * contents, so the scope here is the resource: this caller may only
 * touch policies on knowledge bases in this account and region. What
 * stops it writing an over-broad policy is
 * `resource_policy.retrieve_policy_document`, which has no branch that
 * emits a wildcard principal, and the test that asserts so.
 *
 * Intentionally unattached until task 2.1's migration Lambda roles
 * exist, like the provisioning and ingestion grants above.
 */
export function grantManagedKbResourcePolicyAdmin(config: AppConfig, role: iam.IRole): void {
  role.addToPrincipalPolicy(new iam.PolicyStatement({
    sid: 'ManagedKbResourcePolicyAdmin',
    effect: iam.Effect.ALLOW,
    actions: [
      'bedrock:PutResourcePolicy',
      'bedrock:GetResourcePolicy',
      'bedrock:DeleteResourcePolicy',
    ],
    resources: [knowledgeBaseArnWildcard(config)],
  }));
  role.addToPrincipalPolicy(putMetricDataStatement(config, 'ManagedKbResourcePolicyMetrics'));
}

export interface ManagedKbRoleConstructProps {
  config: AppConfig;
  /**
   * RAG documents bucket. Managed_KB S3 data sources read source bytes
   * from here, so the service role needs conditioned read access to it
   * (Requirement 20.4).
   */
  documentsBucket: s3.IBucket;
}

/**
 * ManagedKbRoleConstruct — the single Bedrock knowledge base service
 * role, plus the caller-side grant helpers that reference it
 * (Requirement 20; .kiro/specs/managed-kb-migration).
 *
 * One role, not one per knowledge base (Requirement 8.9): every
 * Managed_KB this platform provisions is handed the same
 * `serviceRoleArn`, which is why nothing in the trust policy or the
 * grants below names an individual knowledge base.
 *
 * Confused-deputy posture (Requirement 20.2): Bedrock assumes this role
 * on our behalf, so the trust policy pins both `aws:SourceAccount` to
 * this account and `ArnLike` `AWS:SourceArn` to `knowledge-base/*`.
 * Without the ArnLike scope, any Bedrock resource in the account —
 * agents, flows, evaluation jobs — could induce Bedrock to assume this
 * role and read the documents bucket.
 *
 * The S3 statement is hand-written rather than `bucket.grantRead(role)`
 * because grant methods cannot carry a `Condition`, and Requirement
 * 20.4 mandates `aws:ResourceAccount`.
 *
 * No KMS grant: the RAG documents bucket uses SSE-S3 (S3_MANAGED), so
 * Requirement 20.5's `serverSideEncryptionConfiguration.kmsKeyArn` is
 * vacuous here. If that bucket ever moves to a CMK, this role needs
 * `kms:Decrypt` on the key and the KB config needs the key ARN.
 *
 * Additive by design: instantiating this construct creates one IAM role
 * and one SSM parameter and changes no existing resource. The
 * provisioning and direct-ingestion grants are defined but attached to
 * nothing until task 2.1 creates the migration Lambda roles.
 *
 * SSM publications (consumed by the migration/provisioner backend code):
 *   /{prefix}/managed-kb/service-role-arn
 */
export class ManagedKbRoleConstruct extends Construct {
  public readonly serviceRole: iam.Role;
  public readonly serviceRoleArn: string;

  private readonly config: AppConfig;

  constructor(scope: Construct, id: string, props: ManagedKbRoleConstructProps) {
    super(scope, id);

    const { config, documentsBucket } = props;
    this.config = config;

    // ── The service role Bedrock assumes per knowledge base ──
    // Explicit, stable roleName: the ARN is handed to Bedrock in every
    // CreateKnowledgeBase call and read back from SSM by backend code,
    // so an auto-generated name that churns on refactor would be a
    // liability. Same trade-off as agentcore-runtime-role — an orphaned
    // role of this name from a failed deploy must be deleted before a
    // fresh deploy.
    this.serviceRole = new iam.Role(this, 'ManagedKbServiceRole', {
      roleName: getResourceName(config, 'managed-kb-service-role'),
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': config.awsAccount },
          ArnLike: { 'AWS:SourceArn': knowledgeBaseArnWildcard(config) },
        },
      }),
      description: 'Service role assumed by Amazon Bedrock for managed knowledge bases',
    });
    this.serviceRoleArn = this.serviceRole.roleArn;

    // ── S3 read on the documents bucket, account-conditioned ──
    this.serviceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ManagedKbDocumentsRead',
      effect: iam.Effect.ALLOW,
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [documentsBucket.bucketArn, `${documentsBucket.bucketArn}/*`],
      conditions: { StringEquals: { 'aws:ResourceAccount': config.awsAccount } },
    }));

    // ── CUSTOM embedding model invocation (Requirement 8.5) ──
    // Foundation-model ARNs are account-less by design.
    this.serviceRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ManagedKbEmbeddingModelInvoke',
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/${MANAGED_KB_EMBEDDING_MODEL_ID}`,
      ],
    }));

    // ── No metrics grant on the service role, deliberately ──
    //
    // Requirement 20.10 covers only the *calling* identities. This role is
    // assumed by Bedrock to read source bytes and invoke the embedding model;
    // the platform's own custom metrics (`KbByteCapRejected`, `KbOrphansFound`,
    // …) are published by our Lambdas and services under their own identities,
    // never by Bedrock on our behalf. Granting `PutMetricData` here would be a
    // permission that nothing can ever exercise — the same category of mistake
    // as the reserved-namespace bug documented on `managedKbMetricNamespace`:
    // it reads as correct, deploys cleanly, and is simply inert. Left ungranted
    // so that least privilege is legible rather than assumed.

    new ssm.StringParameter(this, 'ManagedKbServiceRoleArnParameter', {
      parameterName: `/${config.projectPrefix}/managed-kb/service-role-arn`,
      stringValue: this.serviceRoleArn,
      description:
        'Bedrock managed knowledge base service role ARN (consumed by the Managed_KB provisioner/migrator backend code)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }

  /**
   * Attach the provisioner/migrator CRUD + `iam:PassRole` grant to a
   * caller role. Called by the migration construct in task 2.1; no
   * caller today, by design.
   */
  public grantProvisioning(role: iam.IRole): void {
    grantManagedKbProvisioning(this.config, role, this.serviceRoleArn);
  }

  /**
   * Attach the direct-ingestion grant to a caller role. Called by the
   * migration construct in task 2.1; no caller today, by design.
   */
  public grantDirectIngestion(role: iam.IRole): void {
    grantManagedKbDirectIngestion(this.config, role);
  }

  /** Attach the inference-side `bedrock:Retrieve` grant to a caller role. */
  public grantRetrieval(role: iam.IRole): void {
    grantManagedKbRetrieval(this.config, role);
  }

  /**
   * Attach the resource-policy administration grant to a caller role —
   * the identity that shares a knowledge base beyond its owner. No
   * caller today, by design.
   */
  public grantResourcePolicyAdmin(role: iam.IRole): void {
    grantManagedKbResourcePolicyAdmin(this.config, role);
  }
}
