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
