/**
 * Managed_KB IAM assertions (Requirement 20.9).
 *
 * The conditions asserted here are the confused-deputy guards on the
 * Bedrock knowledge base service role and the least-privilege split
 * between provisioning, ingestion and retrieval. They are invisible at
 * runtime when correct and silently over-permissive when dropped, which
 * is exactly the class of regression a synth-time assertion catches.
 */
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Match, Template } from 'aws-cdk-lib/assertions';

import {
  grantManagedKbRetrieval,
  ManagedKbRoleConstruct,
} from '../lib/constructs/managed-kb/managed-kb-role-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface PolicyStatement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
}

const KB_ARN_WILDCARD = `arn:aws:bedrock:${MOCK_REGION}:${MOCK_ACCOUNT}:knowledge-base/*`;

/**
 * Expected `cloudwatch:namespace` condition value for the Managed_KB
 * PutMetricData grants — deliberately a literal, not a call into
 * `managedKbMetricNamespace()`, so that changing the production
 * namespace fails here instead of silently following it.
 *
 * It must not begin with `AWS`: CloudWatch reserves those namespaces
 * for its own services and rejects `PutMetricData` into them, so an
 * `AWS/...`-scoped grant authorizes nothing that can ever succeed.
 * Bedrock's own `AWS/Bedrock/KnowledgeBases` metrics are a *read*
 * source (Requirement 20.13), never a publish target.
 */
const EXPECTED_METRIC_NAMESPACE = 'test-project/ManagedKb';

/**
 * Construct-level harness: the service role plus three stand-in caller
 * roles, one per grant, so each grant's statements can be attributed
 * unambiguously. The migration Lambdas that will really carry the
 * provisioning and ingestion grants arrive in task 2.1.
 */
function synthConstruct(): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = createMockConfig();
  const ragData = new RagDataConstruct(stack, 'RagData', { config });
  const managedKb = new ManagedKbRoleConstruct(stack, 'ManagedKbRole', {
    config,
    documentsBucket: ragData.documentsBucket,
  });

  const lambdaPrincipal = new iam.ServicePrincipal('lambda.amazonaws.com');
  const provisioner = new iam.Role(stack, 'FakeProvisioner', { assumedBy: lambdaPrincipal });
  const ingestor = new iam.Role(stack, 'FakeIngestor', { assumedBy: lambdaPrincipal });
  const retriever = new iam.Role(stack, 'FakeRetriever', { assumedBy: lambdaPrincipal });
  const sharer = new iam.Role(stack, 'FakeSharer', { assumedBy: lambdaPrincipal });

  // Exercise the public methods (the surface task 2.1 calls) for
  // provisioning/ingestion and the free function for retrieval (the
  // surface the compute IAM modules call).
  managedKb.grantProvisioning(provisioner);
  managedKb.grantDirectIngestion(ingestor);
  managedKb.grantResourcePolicyAdmin(sharer);
  managedKb.grantMetricsRead(provisioner);
  grantManagedKbRetrieval(config, retriever);

  return Template.fromStack(stack);
}

/**
 * Both resource types a role's identity policy can render into. CDK
 * spills statements from an oversized role policy into a generated
 * `...OverflowPolicy` managed policy, and both the Runtime role and the
 * App API task role are already past that threshold — so a scan that
 * only looked at `AWS::IAM::Policy` would silently find nothing.
 */
const POLICY_TYPES = ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'] as const;

interface PolicyHolder {
  type: string;
  policyId: string;
  roleIds: string[];
  statements: PolicyStatement[];
  json: string;
}

/** Every identity policy in the template, inline and overflow alike. */
function policyHolders(t: Template): PolicyHolder[] {
  const out: PolicyHolder[] = [];
  for (const type of POLICY_TYPES) {
    for (const [policyId, resource] of Object.entries(t.findResources(type))) {
      const props = resource.Properties as {
        PolicyDocument?: { Statement?: PolicyStatement[] };
        Roles?: Array<{ Ref?: string }>;
      };
      out.push({
        type,
        policyId,
        roleIds: (props?.Roles ?? [])
          .map((r) => r.Ref)
          .filter((r): r is string => typeof r === 'string'),
        statements: props?.PolicyDocument?.Statement ?? [],
        json: JSON.stringify(resource),
      });
    }
  }
  return out;
}

/** Every statement in every identity policy in the template. */
function allStatements(t: Template): PolicyStatement[] {
  return policyHolders(t).flatMap((h) => h.statements);
}

function statementBySid(t: Template, sid: string): PolicyStatement {
  const matches = allStatements(t).filter((s) => s.Sid === sid);
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** The identity policies whose statements include `sid`. */
function policiesWithSid(t: Template, sid: string): PolicyHolder[] {
  return policyHolders(t).filter((h) => h.statements.some((s) => s.Sid === sid));
}

const LAMBDA_PRINCIPAL = 'lambda.amazonaws.com';
const ECS_TASKS_PRINCIPAL = 'ecs-tasks.amazonaws.com';

interface RoleProps {
  RoleName?: string;
  AssumeRolePolicyDocument?: { Statement?: Array<{ Principal?: { Service?: unknown } }> };
}

/**
 * Predicate: does this role's trust policy name `service`?
 *
 * Auto-named roles (the Fargate task role, every Lambda execution role)
 * have no stable RoleName to match on, so the trust principal is the only
 * durable way to identify them in a synthesized template.
 */
function trustsService(service: string): (r: RoleProps) => boolean {
  return (r) =>
    (r.AssumeRolePolicyDocument?.Statement ?? []).some(
      (s) => JSON.stringify(s.Principal?.Service ?? '').includes(service),
    );
}

describe('ManagedKbRoleConstruct — service role', () => {
  let t: Template;
  beforeAll(() => {
    t = synthConstruct();
  });

  it('trusts bedrock.amazonaws.com with the aws:SourceAccount confused-deputy guard', () => {
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'test-project-managed-kb-service-role',
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: { Service: 'bedrock.amazonaws.com' },
            Condition: Match.objectLike({
              StringEquals: { 'aws:SourceAccount': MOCK_ACCOUNT },
            }),
          }),
        ]),
      },
    });
  });

  it('scopes the trust policy ArnLike AWS:SourceArn to knowledge-base/* only', () => {
    // Without the ArnLike scope any Bedrock resource in the account
    // (agents, flows, evaluation jobs) could induce Bedrock to assume
    // this role and read the documents bucket.
    t.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'test-project-managed-kb-service-role',
      AssumeRolePolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Condition: Match.objectLike({
              ArnLike: { 'AWS:SourceArn': KB_ARN_WILDCARD },
            }),
          }),
        ]),
      },
    });
  });

  it('conditions documents-bucket read on aws:ResourceAccount', () => {
    const s = statementBySid(t, 'ManagedKbDocumentsRead');
    expect(s.Action).toEqual(['s3:GetObject', 's3:ListBucket']);
    expect(s.Condition).toEqual({ StringEquals: { 'aws:ResourceAccount': MOCK_ACCOUNT } });
  });

  it('scopes documents-bucket read to the RAG documents bucket and nothing else', () => {
    // `aws:ResourceAccount` scopes the ACCOUNT, not the bucket. Without
    // a Resource scope this grant would let the Bedrock KB service role
    // read every bucket in the account — file uploads, fine-tuning,
    // artifacts, the SPA bucket — so assert the scope explicitly.
    //
    // Plain structural assertions: CDK `Match` matchers only work
    // inside `Template.hasResourceProperties`, not inside `toEqual`.
    const s = statementBySid(t, 'ManagedKbDocumentsRead');
    const resources = s.Resource as unknown[];
    expect(Array.isArray(resources)).toBe(true);
    // Exactly two entries: the bucket ARN and the bucket's objects.
    expect(resources).toHaveLength(2);

    // 1. The bucket itself, as an Fn::GetAtt on the RAG documents
    //    bucket's logical id — never a literal wildcard.
    const bucketArn = resources[0] as { 'Fn::GetAtt'?: [string, string] };
    expect(bucketArn['Fn::GetAtt']).toBeDefined();
    const [bucketLogicalId, attribute] = bucketArn['Fn::GetAtt']!;
    expect(bucketLogicalId).toMatch(/RagDocumentsBucket/);
    expect(attribute).toBe('Arn');

    // 2. The objects under that SAME bucket: Fn::Join of the identical
    //    Fn::GetAtt plus the '/*' suffix.
    const objectsArn = resources[1] as { 'Fn::Join'?: [string, unknown[]] };
    expect(objectsArn['Fn::Join']).toBeDefined();
    const [delimiter, parts] = objectsArn['Fn::Join']!;
    expect(delimiter).toBe('');
    expect(parts).toEqual([{ 'Fn::GetAtt': [bucketLogicalId, 'Arn'] }, '/*']);

    // No wildcard resource, and no other bucket smuggled in.
    expect(JSON.stringify(s.Resource)).not.toContain('"*"');
    const referencedBuckets = new Set(
      JSON.stringify(s.Resource).match(/[A-Za-z0-9]*Bucket[A-Za-z0-9]*/g) ?? [],
    );
    expect([...referencedBuckets]).toEqual([bucketLogicalId]);
  });

  it('restricts bedrock:InvokeModel to the pinned titan-embed model only', () => {
    // Requirement 8.5 pins embeddingModelType CUSTOM at
    // amazon.titan-embed-text-v2:0 — that pin is the only reason this
    // grant exists, so it must not widen to foundation-model/*.
    const s = statementBySid(t, 'ManagedKbEmbeddingModelInvoke');
    expect(s.Action).toBe('bedrock:InvokeModel');
    expect(s.Resource).toBe(
      `arn:aws:bedrock:${MOCK_REGION}::foundation-model/amazon.titan-embed-text-v2:0`,
    );
    expect(JSON.stringify(s.Resource)).not.toContain('foundation-model/*');
  });

  it('grants the service role no PutMetricData, since it could never use it', () => {
    // Bedrock assumes this role to read source bytes and embed them. Our own
    // custom metrics are published by our Lambdas under their own identities,
    // so a grant here authorizes nothing. Asserted as an absence because the
    // failure mode is silent: an inert permission looks exactly like a working
    // one until someone checks whether anything ever calls it.
    const sids = allStatements(t).map((s) => s.Sid);
    expect(sids).not.toContain('ManagedKbServiceRoleMetrics');

    // Belt and braces: scan the service role's own policies by role ref, so
    // re-adding the grant under a different SID is caught too.
    const serviceRolePolicies = policyHolders(t).filter((h) =>
      h.roleIds.some((id) => /ManagedKbServiceRole/.test(id)),
    );
    expect(serviceRolePolicies.length).toBeGreaterThan(0); // else the scan is vacuous
    const actions = serviceRolePolicies
      .flatMap((h) => h.statements)
      .flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
    expect(actions).not.toContain('cloudwatch:PutMetricData');
  });

  it('publishes the service role ARN to SSM', () => {
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/managed-kb/service-role-arn',
    });
  });
});

describe('ManagedKbRoleConstruct — caller grants', () => {
  let t: Template;
  beforeAll(() => {
    t = synthConstruct();
  });

  it('conditions iam:PassRole on iam:PassedToService = bedrock.amazonaws.com', () => {
    const s = statementBySid(t, 'ManagedKbPassServiceRole');
    expect(s.Action).toBe('iam:PassRole');
    expect(s.Condition).toEqual({
      StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' },
    });
  });

  it('scopes iam:PassRole to the Managed_KB service role and nothing else', () => {
    // The Resource scope is the other half of Requirement 20.3. With
    // `iam:PassRole` on `*`, the PassedToService condition alone would
    // still let the provisioner hand ANY role in the account to Bedrock
    // and read whatever that role can read via a KB data source — a
    // real privilege-escalation path, so assert the scope explicitly.
    const s = statementBySid(t, 'ManagedKbPassServiceRole');
    // Renders as an Fn::GetAtt on the service role's logical id, never
    // a literal wildcard. (Plain structural assertions — CDK `Match`
    // matchers only work inside `Template.hasResourceProperties`.)
    const resource = s.Resource as { 'Fn::GetAtt'?: [string, string] };
    expect(resource['Fn::GetAtt']).toBeDefined();
    const [logicalId, attribute] = resource['Fn::GetAtt']!;
    expect(logicalId).toMatch(/ManagedKbServiceRole/);
    expect(attribute).toBe('Arn');
    expect(JSON.stringify(s.Resource)).not.toContain('"*"');
  });

  it('scopes provisioning CRUD to knowledge-base ARNs in this account', () => {
    const s = statementBySid(t, 'ManagedKbProvisionCrud');
    expect(s.Action).toEqual([
      'bedrock:GetKnowledgeBase',
      'bedrock:DeleteKnowledgeBase',
      'bedrock:CreateDataSource',
      'bedrock:DeleteDataSource',
      'bedrock:TagResource',
      'bedrock:ListTagsForResource',
    ]);
    expect(s.Resource).toBe(KB_ARN_WILDCARD);
  });

  it('can tag a knowledge base at create time', () => {
    // `CreateKnowledgeBase` is called WITH tags, and AWS authorises the tagging
    // as a separate action. Missing it, provisioning failed in dev with
    //
    //   AccessDeniedException: not authorized to perform bedrock:TagResource
    //
    // *after* passing review, because the create action itself was granted. The
    // tags are what the reconciler and teardown match on, so an untagged
    // knowledge base would be worse than a failed create.
    const s = statementBySid(t, 'ManagedKbProvisionCrud');
    expect(s.Action).toContain('bedrock:TagResource');
    // Must be the wildcard: at create time the knowledge base has no ARN, so a
    // resource-specific grant could never match.
    expect(s.Resource).toBe(KB_ARN_WILDCARD);
  });

  it('can read tags, which is how the reconciler recognises its own resources', () => {
    // `iter_project_knowledge_bases` fails closed on a tag read error, so
    // without this the orphan sweep sees every knowledge base as untagged,
    // matches nothing, and reports a clean account forever.
    const s = statementBySid(t, 'ManagedKbProvisionCrud');
    expect(s.Action).toContain('bedrock:ListTagsForResource');
  });

  it('keeps the non-resource-scopable create/list actions in their own statement', () => {
    const s = statementBySid(t, 'ManagedKbProvisionCreateList');
    expect(s.Action).toEqual(['bedrock:CreateKnowledgeBase', 'bedrock:ListKnowledgeBases']);
    expect(s.Resource).toBe('*');
  });

  it('scopes direct ingestion separately from CRUD', () => {
    const s = statementBySid(t, 'ManagedKbDirectIngestion');
    expect(s.Action).toEqual([
      'bedrock:IngestKnowledgeBaseDocuments',
      'bedrock:StartIngestionJob',
      'bedrock:DeleteKnowledgeBaseDocuments',
      'bedrock:GetKnowledgeBaseDocuments',
    ]);
    expect(s.Resource).toBe(KB_ARN_WILDCARD);
  });

  it('grants bedrock:StartIngestionJob, which is what authorizes IngestKnowledgeBaseDocuments', () => {
    // Regression. AWS authorizes `IngestKnowledgeBaseDocuments` under the
    // adjacent action name `bedrock:StartIngestionJob` — both appear in one
    // statement in AWS's direct-ingestion prerequisites. Granting only the
    // name that matches the API call deploys clean, reviews clean, and then
    // fails every real upload:
    //
    //   AccessDeniedException ... not authorized to perform:
    //   bedrock:StartIngestionJob on resource: knowledge-base/XXXXXXXXXX
    //
    // That is what happened in dev: a document added to a promoted knowledge
    // base went straight to `failed`. It had been masked because the local
    // driver runs under a broader SSO identity than the Lambda role.
    //
    // Asserted on its own, not just inside the array above, so the reason
    // survives a future reordering or trimming of that list — and so the
    // "obvious cleanup" of an action nothing calls fails a test that says why.
    const s = statementBySid(t, 'ManagedKbDirectIngestion');
    expect(s.Action).toContain('bedrock:StartIngestionJob');

    // Both halves are required together; neither alone is sufficient.
    expect(s.Action).toContain('bedrock:IngestKnowledgeBaseDocuments');
  });

  it('gives every holder of the ingestion grant the StartIngestionJob authorization', () => {
    // This suite attaches the grant to one fake role; the real pairing of
    // worker + ingestion consumer is asserted in kb-migration.test.ts. What
    // matters here is that whoever holds the statement holds both halves,
    // since neither action alone permits an upload.
    const holders = policiesWithSid(t, 'ManagedKbDirectIngestion');
    expect(holders).toHaveLength(1);
    for (const holder of holders) {
      expect(holder.json).toContain('bedrock:StartIngestionJob');
      expect(holder.json).toContain('bedrock:IngestKnowledgeBaseDocuments');
    }
  });

  it('grants PutMetricData on the same non-reserved namespace to every calling identity', () => {
    for (const sid of [
      'ManagedKbProvisionMetrics',
      'ManagedKbIngestMetrics',
      'ManagedKbRetrieveMetrics',
      'ManagedKbResourcePolicyMetrics',
    ]) {
      const s = statementBySid(t, sid);
      expect(s.Action).toBe('cloudwatch:PutMetricData');
      expect(s.Condition).toEqual({
        StringEquals: { 'cloudwatch:namespace': EXPECTED_METRIC_NAMESPACE },
      });
      const ns = (s.Condition!.StringEquals['cloudwatch:namespace']) as string;
      expect(ns.startsWith('AWS')).toBe(false);
    }
  });

  it('gives the retrieval caller bedrock:Retrieve and nothing else from the CRUD set', () => {
    const s = statementBySid(t, 'ManagedKbRetrieve');
    expect(s.Action).toBe('bedrock:Retrieve');
    expect(s.Resource).toBe(KB_ARN_WILDCARD);

    // The retrieval grant must not quietly grow write-side actions.
    const retrieverPolicies = policiesWithSid(t, 'ManagedKbRetrieve');
    expect(retrieverPolicies).toHaveLength(1);
    const retrieverJson = retrieverPolicies[0].json;
    expect(retrieverJson).not.toContain('CreateKnowledgeBase');
    expect(retrieverJson).not.toContain('DeleteKnowledgeBase');
    expect(retrieverJson).not.toContain('IngestKnowledgeBaseDocuments');
    expect(retrieverJson).not.toContain('iam:PassRole');
  });

  it('scopes resource-policy administration to knowledge-base ARNs in this account', () => {
    // Requirement 25.6. Sharing is the one grant that can change who else
    // may read a corpus, so it is its own statement on its own role rather
    // than folded into provisioning CRUD.
    const s = statementBySid(t, 'ManagedKbResourcePolicyAdmin');
    expect(s.Action).toEqual([
      'bedrock:PutResourcePolicy',
      'bedrock:GetResourcePolicy',
      'bedrock:DeleteResourcePolicy',
    ]);
    expect(s.Resource).toBe(KB_ARN_WILDCARD);
    // No `*` resource: PutResourcePolicy is resource-scopable, unlike
    // CreateKnowledgeBase, so there is no excuse for account-wide reach.
    expect(s.Resource).not.toBe('*');
  });

  it('keeps the sharing caller away from documents, CRUD and the service role', () => {
    // The writer of a resource policy is a control-plane identity. It
    // never reads document bytes — which is why `bedrock:GetDocumentContent`
    // appears in the policy documents it writes but not in its own grant —
    // and it must not be able to create or delete a knowledge base.
    const holders = policiesWithSid(t, 'ManagedKbResourcePolicyAdmin');
    expect(holders).toHaveLength(1);
    const json = holders[0].json;
    for (const forbidden of [
      'CreateKnowledgeBase',
      'DeleteKnowledgeBase',
      'IngestKnowledgeBaseDocuments',
      'GetDocumentContent',
      'bedrock:Retrieve',
      'iam:PassRole',
      's3:GetObject',
    ]) {
      expect(json).not.toContain(forbidden);
    }
  });

  it('does not attach resource-policy administration to any retrieval identity', () => {
    // A turn that retrieves must never be able to rewrite the policy that
    // decides who may retrieve. Asserted across the whole template rather
    // than on one role, so a future wiring mistake is caught here.
    const sharing = policiesWithSid(t, 'ManagedKbResourcePolicyAdmin');
    const retrieving = policiesWithSid(t, 'ManagedKbRetrieve');
    const sharingRoles = new Set(sharing.flatMap((h) => h.roleIds));
    for (const roleId of retrieving.flatMap((h) => h.roleIds)) {
      expect(sharingRoles.has(roleId)).toBe(false);
    }
  });

  it('grants Bedrock-metrics reads as reads, in the opposite direction from the writes', () => {
    // Requirement 20.13. `Invocations` per knowledge base lives in Bedrock's
    // reserved `AWS/Bedrock/KnowledgeBases` namespace, so it is READ here.
    // Conflating this with the publish direction once produced a
    // PutMetricData grant scoped to that namespace, which would have
    // deployed cleanly and published nothing forever.
    const s = statementBySid(t, 'ManagedKbBedrockMetricsRead');
    expect(s.Action).toEqual(['cloudwatch:GetMetricData', 'cloudwatch:GetMetricStatistics']);
    expect(s.Resource).toBe('*');
    // Read-only: no publish action may ride along in this statement.
    const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
    expect(actions).not.toContain('cloudwatch:PutMetricData');
  });

  it('never conditions a PutMetricData grant on a reserved AWS namespace', () => {
    // The defect this asserts against is invisible at deploy time: the grant
    // applies cleanly and every publish is then silently denied.
    for (const statement of allStatements(t)) {
      const actions = Array.isArray(statement.Action) ? statement.Action : [statement.Action];
      if (!actions.includes('cloudwatch:PutMetricData')) continue;
      const namespace = statement.Condition?.StringEquals?.['cloudwatch:namespace'];
      expect(namespace).toBeDefined();
      expect(String(namespace).startsWith('AWS')).toBe(false);
    }
  });
});

describe('Managed_KB retrieval wiring on PlatformStack', () => {
  let t: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {
        enabled: true,
        defaultQuotaHours: 0,
      },
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    t = Template.fromStack(stack);
  });

  it('creates exactly one shared service role and publishes its ARN', () => {
    // Requirement 8.9: one role reused across every Managed_KB.
    const roles = Object.values(t.findResources('AWS::IAM::Role')).filter(
      (r) => (r.Properties as { RoleName?: string })?.RoleName
        === 'test-project-managed-kb-service-role',
    );
    expect(roles).toHaveLength(1);
    t.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/managed-kb/service-role-arn',
    });
  });

  it('attaches retrieval to the AgentCore Runtime role and the App API task role', () => {
    const roles = t.findResources('AWS::IAM::Role');
    const holders = policiesWithSid(t, 'ManagedKbRetrieve');
    // FOUR holders as of task 2.1: the two compute identities that serve
    // user turns, plus the migration worker (the `verify` canary
    // retrieval, Requirement 15.7) and the ingestion consumer (polling
    // until a document is actually retrievable, Requirement 10.6). The
    // exact count is asserted so a fifth holder appearing has to be a
    // deliberate edit here rather than a silent widening.
    expect(holders).toHaveLength(4);

    const holderRoleIds = holders.flatMap((h) => h.roleIds);
    const holderRoles = holderRoleIds.map((id) => {
      expect(roles[id]).toBeDefined();
      return roles[id].Properties as {
        RoleName?: string;
        AssumeRolePolicyDocument?: { Statement?: Array<{ Principal?: { Service?: unknown } }> };
      };
    });

    // 1. The AgentCore Runtime execution role, identified by its
    //    stable explicit name.
    expect(holderRoles.some((r) => r.RoleName === 'test-project-agentcore-runtime-role')).toBe(true);

    // 2. The App API Fargate task role — auto-named, so identified by
    //    its ecs-tasks trust principal.
    expect(holderRoles.some(trustsService(ECS_TASKS_PRINCIPAL))).toBe(true);

    // 3. The remaining two are the migration Lambda roles, and nothing
    //    else: every holder must be one of those three trust shapes.
    const lambdaHolders = holderRoles.filter(trustsService(LAMBDA_PRINCIPAL));
    expect(lambdaHolders).toHaveLength(2);
    const unaccounted = holderRoles.filter(
      (r) => r.RoleName !== 'test-project-agentcore-runtime-role'
        && !trustsService(ECS_TASKS_PRINCIPAL)(r)
        && !trustsService(LAMBDA_PRINCIPAL)(r),
    );
    expect(unaccounted).toHaveLength(0);
  });

  it('grants knowledge-base CRUD, PassRole and direct ingestion only to Lambda roles', () => {
    // Until task 2.1 these four SIDs were absent from PlatformStack
    // entirely, because the grants had no callers. They now have exactly
    // one class of caller: the migration Lambdas. This assertion is the
    // successor to that absence check and is strictly stronger — it
    // pins WHO holds each grant rather than merely that nobody does.
    //
    // The failure mode being guarded is a plausible future edit:
    // "app-api needs to create knowledge bases for the upgrade button",
    // which would hand knowledge-base delete and `iam:PassRole` to a
    // long-lived, internet-facing Fargate task role. Provisioning
    // belongs to background compute that no user request reaches.
    const roles = t.findResources('AWS::IAM::Role');
    const privilegedSids = [
      'ManagedKbProvisionCrud',
      'ManagedKbProvisionCreateList',
      'ManagedKbPassServiceRole',
      'ManagedKbDirectIngestion',
    ];

    for (const sid of privilegedSids) {
      const holders = policiesWithSid(t, sid);
      // Present — the grants are wired now, not dead code.
      expect(holders.length).toBeGreaterThan(0);

      for (const holder of holders) {
        expect(holder.roleIds.length).toBeGreaterThan(0);
        for (const roleId of holder.roleIds) {
          const props = roles[roleId]?.Properties as RoleProps | undefined;
          expect(props).toBeDefined();
          // Lambda execution roles only.
          expect(trustsService(LAMBDA_PRINCIPAL)(props!)).toBe(true);
          // And explicitly NOT either compute identity.
          expect(trustsService(ECS_TASKS_PRINCIPAL)(props!)).toBe(false);
          expect(props!.RoleName).not.toBe('test-project-agentcore-runtime-role');
        }
      }
    }
  });
});
