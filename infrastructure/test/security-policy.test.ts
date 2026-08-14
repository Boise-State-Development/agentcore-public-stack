/**
 * Security policy + data-plane hardening tests.
 *
 * Restores assertions previously held by the deleted
 * app-api-stack.test.ts / inference-api-stack.test.ts /
 * security-best-practices.test.ts. Now exercised against the new
 * app-api-iam-grants.ts (411 lines) and inference-api-iam-roles.ts
 * (291 lines).
 *
 * Coverage:
 *   1. No managed policy in the stack has both Action: "*" AND
 *      Resource: "*". Excludes service-managed roles AWS itself
 *      stamps with admin-equivalent policies (e.g.
 *      AWSLambdaBasicExecutionRole, when it appears).
 *   2. The BFF cookie-signing KMS key only grants Decrypt to the
 *      app-api task role — never kms:GenerateDataKey or kms:Encrypt
 *      (the SPA receives its session cookie value from the
 *      server-side encryption flow; the client never re-encrypts).
 *   3. Every S3 bucket has SSE configured (BucketEncryption) and
 *      PublicAccessBlock fully blocked.
 *   4. Every DynamoDB table has SSE enabled.
 */
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface PolicyStatement {
  Effect?: string;
  Action?: string | string[];
  Resource?: string | string[] | Record<string, unknown>;
  Sid?: string;
}

function asArray<T>(v: T | T[] | undefined): T[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

function hasWildcard(values: ReadonlyArray<string | Record<string, unknown>>): boolean {
  return values.some((v) => v === '*');
}

describe('Security policy hardening', () => {
  let template: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {},
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  // ──────────────────────────────────────────────────────────
  // 1. No Action:* + Resource:* policy
  // ──────────────────────────────────────────────────────────

  describe('Action:* + Resource:* prohibition', () => {
    function collectPolicyStatements(): Array<{ logicalId: string; sid: string | undefined; statement: PolicyStatement }> {
      const out: Array<{ logicalId: string; sid: string | undefined; statement: PolicyStatement }> = [];

      for (const [logicalId, resource] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
        const stmts = ((resource.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) out.push({ logicalId, sid: s.Sid, statement: s });
      }
      for (const [logicalId, resource] of Object.entries(template.findResources('AWS::IAM::ManagedPolicy'))) {
        const stmts = ((resource.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) out.push({ logicalId, sid: s.Sid, statement: s });
      }
      // Inline role policies
      for (const [logicalId, resource] of Object.entries(template.findResources('AWS::IAM::Role'))) {
        const inlinePolicies = ((resource.Properties as { Policies?: Array<{ PolicyDocument: { Statement: PolicyStatement[] } }> })?.Policies) ?? [];
        for (const p of inlinePolicies) {
          for (const s of p.PolicyDocument.Statement ?? []) out.push({ logicalId, sid: s.Sid, statement: s });
        }
      }
      return out;
    }

    it('no policy statement grants Action:* with Resource:*', () => {
      const violations: string[] = [];
      for (const { logicalId, sid, statement } of collectPolicyStatements()) {
        if (statement.Effect !== 'Allow') continue;
        const actions = asArray(statement.Action);
        const resources = asArray(statement.Resource);
        const actionWildcard = actions.length > 0 && actions.every((a) => a === '*');
        const resourceWildcard = resources.length > 0 && hasWildcard(resources);
        if (actionWildcard && resourceWildcard) {
          violations.push(`  ${logicalId} (Sid=${sid ?? '<unset>'}): Action:* + Resource:*`);
        }
      }
      if (violations.length > 0) {
        throw new Error(
          `Found ${violations.length} policy statement(s) with the dangerous Action:* + Resource:* combination:\n` +
            violations.join('\n'),
        );
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 2. BFF cookie KMS key — Decrypt-only for app-api
  // ──────────────────────────────────────────────────────────

  describe('BFF cookie-signing KMS key', () => {
    it('app-api role KMS grant on the BFF cookie key is Decrypt-only (no GenerateDataKey, Encrypt, or *)', () => {
      // Find any policy statement whose Sid identifies the BFF
      // cookie key grant. We iterate both AWS::IAM::Policy AND
      // AWS::IAM::ManagedPolicy because CDK auto-splits inline
      // policies over the 6144-byte CFN limit into managed
      // overflow policies attached to the same role.
      const candidates: PolicyStatement[] = [];
      for (const [, r] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
        const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) candidates.push(s);
      }
      for (const [, r] of Object.entries(template.findResources('AWS::IAM::ManagedPolicy'))) {
        const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) candidates.push(s);
      }

      const matches = candidates.filter(
        (s) => s.Sid === 'BffCookieSigningKeyDecrypt' || s.Sid === 'KmsBffCookieSigningKeyDecrypt',
      );

      if (matches.length === 0) {
        throw new Error(
          "Could not locate the BFF cookie-signing KMS grant. " +
            "Looked for Sid 'BffCookieSigningKeyDecrypt' or 'KmsBffCookieSigningKeyDecrypt' in AWS::IAM::Policy + AWS::IAM::ManagedPolicy. " +
            "If the Sid was renamed, update this test.",
        );
      }

      for (const s of matches) {
        const actions = asArray(s.Action);
        // Must contain kms:Decrypt
        expect(actions).toContain('kms:Decrypt');
        // Must NOT contain anything else
        const forbidden = actions.filter((a) => a !== 'kms:Decrypt');
        expect(forbidden).toEqual([]);

        // Resource must be a key ARN, not '*'
        const resources = asArray(s.Resource);
        expect(resources).not.toContain('*');
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 2b. App-api DynamoDB table grants
  // ──────────────────────────────────────────────────────────

  describe('App-api shared-conversations table grant', () => {
    // Regression guard: the shared-conversations table was threaded
    // into the app-api container as an env var but never granted on
    // the task role, so every /conversations/{id}/share PutItem and
    // /conversations/{id}/shares Query returned AccessDeniedException
    // (surfaced to users as a 500 "Failed to create share"). The grant
    // lives in app-api-iam-grants.ts under Sid 'SharedConversationsAccess'.
    it("app-api role can PutItem/Query/GetItem on the shared-conversations table (incl. its GSIs)", () => {
      // Iterate both AWS::IAM::Policy AND AWS::IAM::ManagedPolicy because
      // CDK auto-splits oversized inline policies into managed overflow
      // policies attached to the same role.
      const candidates: PolicyStatement[] = [];
      for (const [, r] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
        const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) candidates.push(s);
      }
      for (const [, r] of Object.entries(template.findResources('AWS::IAM::ManagedPolicy'))) {
        const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) candidates.push(s);
      }

      const matches = candidates.filter((s) => s.Sid === 'SharedConversationsAccess');

      if (matches.length === 0) {
        throw new Error(
          "Could not locate the shared-conversations DynamoDB grant. " +
            "Looked for Sid 'SharedConversationsAccess' in AWS::IAM::Policy + AWS::IAM::ManagedPolicy. " +
            "Without it, creating/listing conversation shares fails with AccessDeniedException. " +
            "If the Sid was renamed, update this test.",
        );
      }

      for (const s of matches) {
        const actions = asArray(s.Action);
        // The share service does PutItem (create), Query on SessionShareIndex
        // (list/delete-for-session), and GetItem (retrieve a single share).
        expect(actions).toContain('dynamodb:PutItem');
        expect(actions).toContain('dynamodb:Query');
        expect(actions).toContain('dynamodb:GetItem');
        // Never a wildcard resource.
        const resources = asArray(s.Resource);
        expect(resources).not.toContain('*');
        // Must cover the table's GSIs (SessionShareIndex) — the list/revoke
        // paths Query that index, which requires an index/* resource entry.
        const resourceStrs = resources.map((r) => JSON.stringify(r));
        expect(resourceStrs.some((r) => r.includes('index/'))).toBe(true);
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 2c. Grants proven missing by prod CloudWatch AccessDenied
  // ──────────────────────────────────────────────────────────

  // Both grants below were found the same way as SharedConversationsAccess
  // above: the capability was wired end-to-end except for the IAM statement,
  // so the failure only ever surfaced as an AccessDeniedException in the
  // prod logs. Iterate AWS::IAM::Policy AND AWS::IAM::ManagedPolicy because
  // CDK auto-splits oversized inline policies into managed overflow policies
  // attached to the same role.
  function statementsWithSid(sid: string): PolicyStatement[] {
    const candidates: PolicyStatement[] = [];
    for (const [, r] of Object.entries(template.findResources('AWS::IAM::Policy'))) {
      const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
      for (const s of stmts) candidates.push(s);
    }
    for (const [, r] of Object.entries(template.findResources('AWS::IAM::ManagedPolicy'))) {
      const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
      for (const s of stmts) candidates.push(s);
    }
    return candidates.filter((s) => s.Sid === sid);
  }

  describe('App-api S3 Vectors grant', () => {
    // Regression guard: the grant listed read actions only, so every
    // document-delete cleanup exhausted its 3 retries on
    // AccessDeniedException and logged "Cleanup incomplete ... TTL will
    // auto-expire" — orphaning the document's vectors in the index, where
    // they stayed searchable until TTL.
    it('app-api role can DeleteVectors as well as query the RAG index', () => {
      const matches = statementsWithSid('S3VectorsQueryAccess');

      if (matches.length === 0) {
        throw new Error(
          "Could not locate the app-api S3 Vectors grant. " +
            "Looked for Sid 'S3VectorsQueryAccess'. If the Sid was renamed, update this test.",
        );
      }

      // Both app-api and the AgentCore runtime declare this Sid; only
      // app-api runs cleanup, so assert at least one statement carries the
      // delete action rather than requiring it of every match.
      const withDelete = matches.filter((s) => asArray(s.Action).includes('s3vectors:DeleteVectors'));
      expect(withDelete.length).toBeGreaterThan(0);

      for (const s of matches) {
        const actions = asArray(s.Action);
        // The query path is what the runtime needs; keep it intact.
        expect(actions).toContain('s3vectors:QueryVectors');
        // s3vectors' batch delete is DeleteVectors (plural). DeleteVector
        // (singular, what rag-ingestion holds) is a DIFFERENT action and
        // does not authorize the cleanup service's call.
        expect(actions).not.toContain('s3vectors:DeleteVector');
        const resources = asArray(s.Resource);
        expect(resources).not.toContain('*');
      }
    });
  });

  describe('AgentCore runtime user-settings grant', () => {
    // Regression guard: inference-agentcore-construct.ts injects
    // DYNAMODB_USER_SETTINGS_TABLE_NAME, which makes UserSettingsRepository
    // report itself enabled — but the table was absent from the runtime
    // role's grants. get_settings swallowed the AccessDeniedException into
    // DEFAULT_SETTINGS, silently ignoring the user's chosen default model.
    it('runtime role can GetItem on the user-settings table', () => {
      const matches = statementsWithSid('UserSettingsTableReadAccess');

      if (matches.length === 0) {
        throw new Error(
          "Could not locate the runtime user-settings grant. " +
            "Looked for Sid 'UserSettingsTableReadAccess'. Without it the user's saved " +
            "defaultModelId is silently ignored on the inference path. " +
            "If the Sid was renamed, update this test.",
        );
      }

      for (const s of matches) {
        const actions = asArray(s.Action);
        expect(actions).toContain('dynamodb:GetItem');
        // The runtime never writes settings — app-api owns that path.
        expect(actions).not.toContain('dynamodb:PutItem');
        expect(actions).not.toContain('dynamodb:UpdateItem');
        expect(actions).not.toContain('dynamodb:DeleteItem');
        const resources = asArray(s.Resource);
        expect(resources).not.toContain('*');
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 3. S3 hardening (encryption + public-access-block)
  // ──────────────────────────────────────────────────────────

  describe('S3 hardening', () => {
    // Buckets created out-of-band for asset publishing (cdk-assets,
    // CDK bootstrap) are not in this template; we only validate
    // the ones PlatformStack itself creates.
    function listBuckets(): Array<{ logicalId: string; props: Record<string, unknown> }> {
      return Object.entries(template.findResources('AWS::S3::Bucket')).map(
        ([logicalId, r]) => ({ logicalId, props: (r.Properties ?? {}) as Record<string, unknown> }),
      );
    }

    it('every bucket has BucketEncryption configured', () => {
      const violations: string[] = [];
      for (const { logicalId, props } of listBuckets()) {
        if (!props.BucketEncryption) {
          violations.push(`  ${logicalId}: missing BucketEncryption`);
        }
      }
      if (violations.length > 0) {
        throw new Error(`Found ${violations.length} bucket(s) without server-side encryption:\n` + violations.join('\n'));
      }
    });

    it('every bucket has PublicAccessBlockConfiguration fully blocked', () => {
      const violations: string[] = [];
      for (const { logicalId, props } of listBuckets()) {
        const pab = props.PublicAccessBlockConfiguration as Record<string, unknown> | undefined;
        if (!pab) {
          violations.push(`  ${logicalId}: missing PublicAccessBlockConfiguration`);
          continue;
        }
        const fullyBlocked =
          pab.BlockPublicAcls === true &&
          pab.BlockPublicPolicy === true &&
          pab.IgnorePublicAcls === true &&
          pab.RestrictPublicBuckets === true;
        if (!fullyBlocked) {
          violations.push(`  ${logicalId}: PublicAccessBlock not fully restricted (${JSON.stringify(pab)})`);
        }
      }
      if (violations.length > 0) {
        throw new Error(`Found ${violations.length} bucket(s) with incomplete public-access-block:\n` + violations.join('\n'));
      }
    });

    it('every bucket enforces SSL (denies non-TLS via bucket policy)', () => {
      const buckets = listBuckets();
      const policies = template.findResources('AWS::S3::BucketPolicy');

      // Index policies by the bucket they apply to.
      const policyByBucket = new Map<string, Record<string, unknown>>();
      for (const [, p] of Object.entries(policies)) {
        const bucketRef = (p.Properties as { Bucket?: { Ref?: string } | string })?.Bucket;
        const bucketLogicalId = typeof bucketRef === 'string' ? bucketRef : bucketRef?.Ref;
        if (bucketLogicalId) policyByBucket.set(bucketLogicalId, p.Properties as Record<string, unknown>);
      }

      const violations: string[] = [];
      for (const { logicalId } of buckets) {
        const policy = policyByBucket.get(logicalId);
        if (!policy) {
          violations.push(`  ${logicalId}: no bucket policy (enforceSSL: true was expected)`);
          continue;
        }
        const stmts = (policy.PolicyDocument as { Statement?: PolicyStatement[] })?.Statement ?? [];
        const hasSslDeny = stmts.some(
          (s) =>
            s.Effect === 'Deny' &&
            (s as PolicyStatement & { Condition?: { Bool?: { 'aws:SecureTransport'?: string | boolean } } })?.Condition?.Bool?.[
              'aws:SecureTransport'
            ] !== undefined,
        );
        if (!hasSslDeny) {
          violations.push(`  ${logicalId}: bucket policy missing aws:SecureTransport=false Deny`);
        }
      }
      if (violations.length > 0) {
        throw new Error(`Found ${violations.length} bucket(s) without enforceSSL:\n` + violations.join('\n'));
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 4. DynamoDB SSE
  // ──────────────────────────────────────────────────────────

  describe('DynamoDB hardening', () => {
    it('every table has SSE enabled', () => {
      const tables = template.findResources('AWS::DynamoDB::Table');
      const violations: string[] = [];

      for (const [logicalId, r] of Object.entries(tables)) {
        const sse = (r.Properties as { SSESpecification?: { SSEEnabled?: boolean } })?.SSESpecification;
        if (!sse || sse.SSEEnabled !== true) {
          violations.push(`  ${logicalId}: SSESpecification.SSEEnabled is not true (${JSON.stringify(sse)})`);
        }
      }

      if (violations.length > 0) {
        throw new Error(`Found ${violations.length} DDB table(s) without SSE:\n` + violations.join('\n'));
      }
    });
  });

  // ──────────────────────────────────────────────────────────
  // 5. Sanity: stack exists and synthesizes
  // ──────────────────────────────────────────────────────────

  it('stack synthesizes with the resources it claims to', () => {
    template.resourceCountIs('AWS::ECS::TaskDefinition', 1);
    expect(Object.keys(template.findResources('AWS::S3::Bucket')).length).toBeGreaterThan(0);
    expect(Object.keys(template.findResources('AWS::DynamoDB::Table')).length).toBeGreaterThan(0);
  });
});
