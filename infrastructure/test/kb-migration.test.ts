/**
 * Managed_KB migration control plane — CDK assertions
 * (.kiro/specs/managed-kb-migration, tasks 2.1–2.5).
 *
 * The properties asserted here are all invisible when correct and
 * expensive when wrong, which is the only kind worth a synth-time test:
 *
 *   - Four Lambdas must share ONE image. Split it and the platform
 *     deploy pushes four images and the backend workflow updates one.
 *   - The bootstrap asset must be byte-stable. If its digest moves
 *     between synths, the next platform deploy silently reverts all four
 *     functions to a no-op stub — no error, just an ingestion pipeline
 *     that stops working.
 *   - Every flag must default OFF and an empty string must resolve to
 *     off. An unset GitHub Actions variable forwards as an empty string,
 *     so this is the difference between a dark feature and a fleet
 *     migration nobody asked for.
 *   - The ingestion consumer's timeout must clear 300 s and it must have
 *     a DLQ. Under 300 s turns a slow-but-succeeding ingestion into a
 *     failure; without a DLQ that failure is silent.
 *   - The owner tag must never carry PII.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig, loadConfig, ManagedKbConfig } from '../lib/config';
import {
  KbMigrationConstruct,
  MANAGED_KB_ACCOUNT_QUOTA,
  MANAGED_KB_COUNT_ALARM_FRACTION,
  MANAGED_KB_TAG_KEYS,
  managedKbEnvironmentTagValue,
} from '../lib/constructs/managed-kb/kb-migration-construct';
import { ManagedKbRoleConstruct } from '../lib/constructs/managed-kb/managed-kb-role-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const BOOTSTRAP_DIR = path.resolve(__dirname, '..', 'bootstrap-assets', 'kb-migration');

/** Dotted handler paths, one per function. */
const HANDLERS = {
  dispatcher: 'apis.app_api.kb_migration.dispatcher.lambda_handler',
  worker: 'apis.app_api.kb_migration.worker.lambda_handler',
  reconciler: 'apis.app_api.kb_migration.reconciler.lambda_handler',
  ingestionConsumer: 'apis.app_api.kb_migration.ingestion_consumer.lambda_handler',
} as const;

const METRIC_NAMESPACE = 'test-project/ManagedKb';

interface LambdaProps {
  ImageConfig?: { Command?: string[] };
  PackageType?: string;
  Architectures?: string[];
  FunctionName?: string;
  Timeout?: number;
  MemorySize?: number;
  DeadLetterConfig?: { TargetArn?: unknown };
  Environment?: { Variables?: Record<string, string> };
  Code?: { ImageUri?: unknown };
}

interface PolicyStatement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
}

function buildConfig(overrides: Partial<ManagedKbConfig> = {}): AppConfig {
  const base = createMockConfig();
  return createMockConfig({ managedKb: { ...base.managedKb, ...overrides } });
}

/** Construct-scoped isolated stack, matching kb-sync.test.ts. */
function synth(overrides: Partial<ManagedKbConfig> = {}): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  const config = buildConfig(overrides);
  const ragData = new RagDataConstruct(stack, 'RagData', { config });
  const managedKbRole = new ManagedKbRoleConstruct(stack, 'ManagedKbRole', {
    config,
    documentsBucket: ragData.documentsBucket,
  });
  new KbMigrationConstruct(stack, 'KbMigration', {
    config,
    assistantsTable: ragData.assistantsTable,
    documentsBucket: ragData.documentsBucket,
    managedKbRole,
  });
  return Template.fromStack(stack);
}

/** Every Lambda in the template, as its raw properties. */
function lambdas(t: Template): LambdaProps[] {
  return Object.values(t.findResources('AWS::Lambda::Function')).map(
    (r) => r.Properties as LambdaProps,
  );
}

/** The single Lambda whose ImageConfig.Command is `handler`. */
function lambdaFor(t: Template, handler: string): LambdaProps {
  const matches = lambdas(t).filter((p) => p.ImageConfig?.Command?.[0] === handler);
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Every migration Lambda (i.e. excluding any CDK-generated helper). */
function migrationLambdas(t: Template): LambdaProps[] {
  const wanted = new Set<string>(Object.values(HANDLERS));
  return lambdas(t).filter((p) => wanted.has(p.ImageConfig?.Command?.[0] ?? ''));
}

function allStatements(t: Template): PolicyStatement[] {
  const out: PolicyStatement[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type))) {
      const props = r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } };
      out.push(...(props?.PolicyDocument?.Statement ?? []));
    }
  }
  return out;
}

function env(p: LambdaProps): Record<string, string> {
  return p.Environment?.Variables ?? {};
}

/**
 * Every policy statement attached to the role whose logical id matches
 * `rolePattern`, across inline and managed policies.
 *
 * The SID-keyed helper in the IAM block below answers "who holds this
 * named grant"; this one answers the opposite and more important
 * question — "everything this role can do" — which is the only view that
 * catches an over-grant arriving under a SID nobody thought to look for.
 */
function statementsForRole(t: Template, rolePattern: RegExp): PolicyStatement[] {
  const roleIds = Object.keys(t.findResources('AWS::IAM::Role')).filter((id) =>
    rolePattern.test(id),
  );
  expect(roleIds).toHaveLength(1);

  const out: PolicyStatement[] = [];
  for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
    for (const r of Object.values(t.findResources(type))) {
      const props = r.Properties as {
        PolicyDocument?: { Statement?: PolicyStatement[] };
        Roles?: Array<{ Ref?: string }>;
      };
      if (!(props.Roles ?? []).some((x) => x.Ref === roleIds[0])) continue;
      out.push(...(props.PolicyDocument?.Statement ?? []));
    }
  }
  // Inline policies declared on the role itself.
  const roleProps = t.findResources('AWS::IAM::Role')[roleIds[0]].Properties as {
    Policies?: Array<{ PolicyDocument?: { Statement?: PolicyStatement[] } }>;
  };
  for (const p of roleProps.Policies ?? []) {
    out.push(...(p.PolicyDocument?.Statement ?? []));
  }
  return out;
}

// ============================================================
// 2.1 — four Lambdas, one image
// ============================================================

describe('KbMigrationConstruct — Lambdas and image sharing', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  it('creates exactly four DockerImage Lambdas, one per handler', () => {
    expect(migrationLambdas(t)).toHaveLength(4);
    for (const handler of Object.values(HANDLERS)) {
      const p = lambdaFor(t, handler);
      expect(p.PackageType).toBe('Image');
    }
  });

  it('all four functions share ONE image asset', () => {
    // The whole point of the ImageConfig.Command override pattern: one
    // build, one ECR push, one out-of-band `update-function-code` per
    // deploy. Pointing any function at a second build context would work
    // at synth time, deploy fine, and quietly double the image inventory
    // while leaving that function on a stale image forever after —
    // because the backend workflow only ships one.
    const imageUris = migrationLambdas(t).map((p) => JSON.stringify(p.Code?.ImageUri));
    expect(imageUris).toHaveLength(4);
    for (const uri of imageUris) {
      expect(uri).not.toBe('undefined');
    }
    expect(new Set(imageUris).size).toBe(1);
  });

  it('runs on ARM64 and lets CDK generate every function name', () => {
    for (const p of migrationLambdas(t)) {
      expect(p.Architectures).toEqual(['arm64']);
      // An explicit FunctionName would remove the reason the SSM
      // publications below exist, and would also block the
      // replace-on-update path if one is ever needed.
      expect(p.FunctionName).toBeUndefined();
    }
  });

  it('publishes all four generated function names to SSM under /kb-migration/', () => {
    for (const slug of ['dispatcher', 'worker', 'reconciler', 'ingestion-consumer']) {
      t.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/test-project/kb-migration/${slug}-function-name`,
      });
    }
  });
});

// ============================================================
// 2.1 — bootstrap asset byte-stability
// ============================================================

describe('kb-migration bootstrap asset', () => {
  it('contains exactly the Dockerfile and the four stub modules', () => {
    // Anything else in this directory becomes part of the asset
    // fingerprint. A stray generated file — a build log, a compiled
    // .pyc, an editor backup — changes the digest on the next synth and
    // makes the platform deploy revert all four functions to the stub.
    const entries = fs.readdirSync(BOOTSTRAP_DIR).sort();
    expect(entries).toEqual([
      'Dockerfile',
      'dispatcher.py',
      'ingestion_consumer.py',
      'reconciler.py',
      'worker.py',
    ]);
  });

  it('pins the base image by sha256 digest, not a floating tag', () => {
    // A floating `python:3.12` tag is the classic way a "byte-stable"
    // asset stops being stable: the local file bytes never change, but
    // the built image does, so the digest CDK records moves on whichever
    // machine next synthesizes.
    const dockerfile = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'Dockerfile'), 'utf8');
    const fromLines = dockerfile
      .split('\n')
      .filter((l) => l.trimStart().startsWith('FROM '));
    expect(fromLines).toHaveLength(1);
    expect(fromLines[0]).toMatch(/@sha256:[0-9a-f]{64}$/);
  });

  it('COPYs a module for every ImageConfig.Command the construct sets', () => {
    // The commands are function configuration and survive an
    // out-of-band image swap, so a command whose module is not in the
    // stub image is an ImportError during the first-deploy window —
    // exactly when nobody is watching these functions.
    const dockerfile = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'Dockerfile'), 'utf8');
    for (const handler of Object.values(HANDLERS)) {
      // apis.app_api.kb_migration.worker.lambda_handler
      //   -> apis/app_api/kb_migration/worker.py
      const modulePath = `${handler.replace('.lambda_handler', '').split('.').join('/')}.py`;
      expect(dockerfile).toContain(modulePath);
    }
  });

  it('produces an identical image digest across independent synths', () => {
    // Byte-stability, asserted as the property that actually matters:
    // determinism. Two separate Apps fingerprint the same directory and
    // must agree. This fails if anything nondeterministic (a timestamp,
    // a generated file, a mutable base tag) enters the build context.
    const first = JSON.stringify(migrationLambdas(synth())[0].Code?.ImageUri);
    const second = JSON.stringify(migrationLambdas(synth())[0].Code?.ImageUri);
    expect(first).toBe(second);
    expect(first).not.toBe('undefined');
  });
});

// ============================================================
// 2.1 — schedules
// ============================================================

describe('KbMigrationConstruct — schedules', () => {
  it('creates exactly two schedule rules plus the documents rule', () => {
    const rules = Object.values(synth().findResources('AWS::Events::Rule'));
    expect(rules).toHaveLength(3);
  });

  it('dispatcher runs on a rate() schedule and targets the dispatcher', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(15 minutes)',
      Targets: Match.arrayWith([
        Match.objectLike({ Arn: { 'Fn::GetAtt': [Match.stringLikeRegexp('KbMigrationDispatcherLambda'), 'Arn'] } }),
      ]),
    });
  });

  it('reconciler runs on a daily rate() schedule and targets the reconciler', () => {
    const t = synth();
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 day)',
      Targets: Match.arrayWith([
        Match.objectLike({ Arn: { 'Fn::GetAtt': [Match.stringLikeRegexp('KbMigrationReconcilerLambda'), 'Arn'] } }),
      ]),
    });
  });

  it('dispatcher rule is DISABLED when migrationEnabled is false', () => {
    const t = synth({ migrationEnabled: false });
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(15 minutes)',
      State: 'DISABLED',
    });
  });

  it('dispatcher rule is ENABLED when migrationEnabled is true', () => {
    const t = synth({ migrationEnabled: true });
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(15 minutes)',
      State: 'ENABLED',
    });
  });

  it('reconciler rule is ENABLED even with every flag off, because report-only is the point', () => {
    // Requirement 14.7 makes report-only the INITIAL DEPLOYED MODE so
    // the Reconciler's judgement can be audited for weeks before
    // `reconcilerArmed` lets it delete. Gating the schedule on a flag
    // would mean the first thing it ever did was delete.
    const t = synth({ newDefault: false, migrationEnabled: false, reconcilerArmed: false });
    t.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 day)',
      State: 'ENABLED',
    });
  });
});

// ============================================================
// 2.2 — ingestion consumer
// ============================================================

describe('KbMigrationConstruct — ingestion consumer (task 2.2)', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  it('tolerates at least 300 seconds of ingestion latency', () => {
    // Requirement 10.9. The floor is measured, not guessed: a fixed
    // ~68 s per-knowledge-base warm-up plus a tail to 264 s on a 50 KiB
    // PDF, and the consumer polls until the document is actually
    // retrievable. A shorter timeout converts a slow success into a
    // dead-lettered document.
    const p = lambdaFor(t, HANDLERS.ingestionConsumer);
    expect(typeof p.Timeout).toBe('number');
    expect(p.Timeout!).toBeGreaterThanOrEqual(300);
  });

  it('has a dead-letter queue wired as its async-invocation DLQ', () => {
    // Asserted in both directions: the queue exists, and the function
    // actually points at it. A queue nobody references is worse than no
    // queue — it looks like coverage.
    t.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'test-project-kb-ingestion-consumer-dlq',
      MessageRetentionPeriod: 1209600, // 14 days, the SQS maximum
      SqsManagedSseEnabled: true,
    });

    const p = lambdaFor(t, HANDLERS.ingestionConsumer);
    expect(p.DeadLetterConfig).toBeDefined();
    const target = JSON.stringify(p.DeadLetterConfig?.TargetArn);
    expect(target).toContain('KbIngestionConsumerDlq');
  });

  it('enforces TLS on the dead-letter queue', () => {
    t.hasResourceProperties('AWS::SQS::QueuePolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Deny',
            Condition: { Bool: { 'aws:SecureTransport': 'false' } },
          }),
        ]),
      }),
    });
  });

  it('is triggered by documents-bucket Object Created events under assistants/', () => {
    t.hasResourceProperties('AWS::Events::Rule', {
      EventPattern: Match.objectLike({
        source: ['aws.s3'],
        'detail-type': ['Object Created'],
        detail: Match.objectLike({
          object: { key: [{ prefix: 'assistants/' }] },
        }),
      }),
      Targets: Match.arrayWith([
        Match.objectLike({ Arn: { 'Fn::GetAtt': [Match.stringLikeRegexp('KbIngestionConsumerLambda'), 'Arn'] } }),
      ]),
    });
  });

  it('scopes the trigger to the RAG documents bucket, not every bucket', () => {
    const rules = Object.values(t.findResources('AWS::Events::Rule'))
      .map((r) => (r.Properties as { EventPattern?: Record<string, unknown> }).EventPattern)
      .filter((ep): ep is Record<string, unknown> => ep?.source !== undefined);
    expect(rules).toHaveLength(1);
    const detail = rules[0].detail as { bucket?: { name?: unknown[] } };
    const bucketNames = JSON.stringify(detail.bucket?.name);
    expect(bucketNames).toContain('RagDocumentsBucket');
    expect(bucketNames).not.toContain('"*"');
  });

  it('trigger rule is DISABLED with every flag off, so uploads are not double-ingested', () => {
    // The legacy rag-ingestion notification still owns the S3 event. If
    // this rule were live too, every upload would reach both consumers
    // and a legacy document would be routed straight back into the
    // legacy pipeline — ingested twice (Requirement 10.5).
    const t2 = synth({ newDefault: false, migrationEnabled: false });
    const disabled = Object.values(t2.findResources('AWS::Events::Rule')).filter((r) => {
      const props = r.Properties as { EventPattern?: unknown; State?: string };
      return props.EventPattern !== undefined && props.State === 'DISABLED';
    });
    expect(disabled).toHaveLength(1);
  });

  it('trigger rule is ENABLED once managed creation or migration is on', () => {
    for (const overrides of [{ newDefault: true }, { migrationEnabled: true }]) {
      const t2 = synth(overrides);
      const enabled = Object.values(t2.findResources('AWS::Events::Rule')).filter((r) => {
        const props = r.Properties as { EventPattern?: Record<string, unknown>; State?: string };
        return props.EventPattern?.source !== undefined && props.State === 'ENABLED';
      });
      expect(enabled).toHaveLength(1);
    }
  });
});

// ============================================================
// 2.3 — flags and config
// ============================================================

describe('KbMigrationConstruct — flags reach every function', () => {
  it('all three flags are "false" on all four functions by default', () => {
    const t = synth();
    const fns = migrationLambdas(t);
    expect(fns).toHaveLength(4);
    for (const p of fns) {
      expect(env(p).MANAGED_KB_NEW_DEFAULT).toBe('false');
      expect(env(p).MANAGED_KB_MIGRATION_ENABLED).toBe('false');
      expect(env(p).MANAGED_KB_RECONCILER_ARMED).toBe('false');
    }
  });

  it('each flag is forwarded independently', () => {
    // Requirement 19.4. Wiring two of them to the same config field
    // would leave every default-off test above green.
    const t = synth({ newDefault: true, migrationEnabled: false, reconcilerArmed: false });
    const p = lambdaFor(t, HANDLERS.worker);
    expect(env(p).MANAGED_KB_NEW_DEFAULT).toBe('true');
    expect(env(p).MANAGED_KB_MIGRATION_ENABLED).toBe('false');
    expect(env(p).MANAGED_KB_RECONCILER_ARMED).toBe('false');

    const t2 = synth({ newDefault: false, migrationEnabled: false, reconcilerArmed: true });
    const p2 = lambdaFor(t2, HANDLERS.reconciler);
    expect(env(p2).MANAGED_KB_NEW_DEFAULT).toBe('false');
    expect(env(p2).MANAGED_KB_RECONCILER_ARMED).toBe('true');
  });

  it('gives the dispatcher the worker function name the Python actually reads', () => {
    // Shipped as MANAGED_KB_WORKER_FUNCTION_NAME while dispatcher.py reads
    // KB_MIGRATION_WORKER_FUNCTION_NAME. Every tick raised
    // `RuntimeError: KB_MIGRATION_WORKER_FUNCTION_NAME is not set` and no
    // knowledge base could migrate. Its sibling kb-sync works precisely
    // because kb-sync.test.ts asserts the same thing.
    const t = synth();
    const p = lambdaFor(t, HANDLERS.dispatcher);
    expect(env(p).KB_MIGRATION_WORKER_FUNCTION_NAME).toBeDefined();
    expect(env(p).MANAGED_KB_WORKER_FUNCTION_NAME).toBeUndefined();
  });

  it('gives the worker the retention window under the name it reads', () => {
    // worker._retain_days() reads KB_MIGRATION_RETAIN_DAYS. Published as
    // MANAGED_KB_RETENTION_WINDOW_DAYS, the configured window was silently
    // replaced by the code's 30-day floor.
    const t = synth();
    const p = lambdaFor(t, HANDLERS.worker);
    expect(env(p).KB_MIGRATION_RETAIN_DAYS).toBe('30');
    expect(env(p).MANAGED_KB_RETENTION_WINDOW_DAYS).toBeUndefined();
  });

  it('forwards the byte caps and retention window', () => {
    const t = synth();
    const p = lambdaFor(t, HANDLERS.worker);
    expect(env(p).MANAGED_KB_PER_OWNER_DEFAULT_BYTES).toBe(String(100 * 1024 * 1024));
    expect(env(p).MANAGED_KB_PER_OWNER_ELEVATED_BYTES).toBe(String(1024 * 1024 * 1024));
    expect(env(p).MANAGED_KB_PER_KB_CEILING_BYTES).toBe(String(500 * 1024 * 1024));
    // KB_MIGRATION_RETAIN_DAYS, not MANAGED_KB_RETENTION_WINDOW_DAYS. This
    // assertion previously pinned the latter — which the construct set and
    // `worker._retain_days()` never read, so it confirmed the construct against
    // itself and passed while the configured window was being dropped.
    expect(env(p).KB_MIGRATION_RETAIN_DAYS).toBe('30');
  });

  it('keeps the standard per-owner cap below the 1 GB user-files precedent', () => {
    // Requirement 12.2's whole reason for existing: the 1 GB precedent
    // applied to managed storage would permit 30 TB at 30,000 users,
    // i.e. ~$150,000/month.
    const config = buildConfig();
    expect(config.managedKb.perOwnerDefaultBytes).toBeLessThan(1024 * 1024 * 1024);
  });

  it('retention window is at least the 30 days rollback requires', () => {
    // Requirement 15.11 — legacy vectors must survive long enough for a
    // rollback to be a real option rather than a stated one.
    expect(buildConfig().managedKb.retentionWindowDays).toBeGreaterThanOrEqual(30);
  });
});

describe('loadConfig — managedKb flag resolution (Requirements 19.5, 19.8)', () => {
  const FLAG_ENV_KEYS = [
    'CDK_MANAGED_KB_NEW_DEFAULT',
    'CDK_MANAGED_KB_MIGRATION_ENABLED',
    'CDK_MANAGED_KB_RECONCILER_ARMED',
  ] as const;

  let app: cdk.App;
  let originalEnv: NodeJS.ProcessEnv;

  function clearFlagEnv(): void {
    for (const key of FLAG_ENV_KEYS) {
      delete process.env[key];
    }
  }

  beforeEach(() => {
    originalEnv = { ...process.env };
    process.env = { ...originalEnv };
    clearFlagEnv();

    app = new cdk.App();
    app.node.setContext('projectPrefix', 'test-project');
    app.node.setContext('awsRegion', 'us-east-1');
    app.node.setContext('awsAccount', '123456789012');
    app.node.setContext('vpcCidr', '10.0.0.0/16');
    app.node.setContext('domainName', 'test.example.com');
    app.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
    app.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4 });
    app.node.setContext('inferenceApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 4, logLevel: 'INFO' });
    app.node.setContext('gateway', { apiType: 'REST' });
    app.node.setContext('fileUpload', { maxFileSizeBytes: 4194304 });
    app.node.setContext('ragIngestion', {
      additionalCorsOrigins: '',
      lambdaMemorySize: 10240,
      lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2',
      vectorDimension: 1024,
      vectorDistanceMetric: 'cosine',
    });
  });

  afterEach(() => {
    clearFlagEnv();
    process.env = originalEnv;
  });

  it('defaults all three flags to false when nothing is set', () => {
    const config = loadConfig(app);
    expect(config.managedKb.newDefault).toBe(false);
    expect(config.managedKb.migrationEnabled).toBe(false);
    expect(config.managedKb.reconcilerArmed).toBe(false);
  });

  it('resolves an EMPTY STRING to false, not to true', () => {
    // The load-bearing case. An unset GitHub Actions variable forwards
    // as '' — not as absent — so the `X ? X !== 'false' : default` shape
    // the default-ON flags use would resolve every one of these to TRUE
    // on a fork that set no variables at all, arming a fleet migration
    // and a deleting reconciler by omission.
    for (const key of FLAG_ENV_KEYS) {
      process.env[key] = '';
    }
    const config = loadConfig(app);
    expect(config.managedKb.newDefault).toBe(false);
    expect(config.managedKb.migrationEnabled).toBe(false);
    expect(config.managedKb.reconcilerArmed).toBe(false);
  });

  it('honours an explicit "true" per flag, independently', () => {
    process.env.CDK_MANAGED_KB_MIGRATION_ENABLED = 'true';
    const config = loadConfig(app);
    expect(config.managedKb.migrationEnabled).toBe(true);
    // The other two must NOT come along for the ride.
    expect(config.managedKb.newDefault).toBe(false);
    expect(config.managedKb.reconcilerArmed).toBe(false);
  });

  it('honours an explicit "false"', () => {
    process.env.CDK_MANAGED_KB_RECONCILER_ARMED = 'false';
    expect(loadConfig(app).managedKb.reconcilerArmed).toBe(false);
  });

  it('applies the documented numeric defaults', () => {
    const config = loadConfig(app);
    expect(config.managedKb.perOwnerDefaultBytes).toBe(100 * 1024 * 1024);
    expect(config.managedKb.perOwnerElevatedBytes).toBe(1024 * 1024 * 1024);
    expect(config.managedKb.perKnowledgeBaseCeilingBytes).toBe(500 * 1024 * 1024);
    expect(config.managedKb.retentionWindowDays).toBe(30);
  });
});

// ============================================================
// 2.4 — tagging
// ============================================================

describe('Managed_KB tagging contract (task 2.4)', () => {  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  it('ships the four tag KEYS to every function', () => {
    // Three independent readers must agree on these strings: the
    // provisioner that writes them, the Reconciler's tag-filtered
    // ListKnowledgeBases, and teardown. A typo in any one is silent —
    // the Reconciler reports a clean account while orphans bill.
    //
    // Asserted as LITERALS, deliberately not as a read of
    // MANAGED_KB_TAG_KEYS: importing the production constant would make
    // this test follow any rename rather than catch it, which is how a
    // tag contract quietly breaks its two other readers.
    for (const p of migrationLambdas(t)) {
      const e = env(p);
      expect(e.MANAGED_KB_TAG_KEY_PREFIX).toBe('ManagedKbPrefix');
      expect(e.MANAGED_KB_TAG_KEY_ENVIRONMENT).toBe('ManagedKbEnvironment');
      expect(e.MANAGED_KB_TAG_KEY_APP_KB_ID).toBe('ManagedKbAppKbId');
      expect(e.MANAGED_KB_TAG_KEY_OWNER_USER_ID).toBe('ManagedKbOwnerUserId');
    }
  });

  it('exports the same tag keys it ships, so the runtime and teardown agree', () => {
    // The exported map is what scripts/teardown and the backend
    // provisioner will import. Pinned to the same literals as above.
    expect(MANAGED_KB_TAG_KEYS).toEqual({
      PREFIX: 'ManagedKbPrefix',
      ENVIRONMENT: 'ManagedKbEnvironment',
      APP_KB_ID: 'ManagedKbAppKbId',
      OWNER_USER_ID: 'ManagedKbOwnerUserId',
    });
  });

  it('ships the two deploy-time tag VALUES', () => {
    const e = env(lambdaFor(t, HANDLERS.worker));
    expect(e.MANAGED_KB_TAG_VALUE_PREFIX).toBe('test-project');
    expect(e.MANAGED_KB_TAG_VALUE_ENVIRONMENT).toBe('test');
  });

  it('ships the tag VALUES to every function, not only the one that writes them', () => {
    // The worker writes the tags; the RECONCILER filters on them, and teardown
    // reads the same variables. A value shipped only to the writer is the shape
    // of the defect that motivated `kb_backend/tags.py`: the provisioning code
    // read variables it was never given, fell back to a hardcoded default, and
    // the reader agreed with it only because it used the same default. The
    // symptom was a teardown that matched nothing and reported success.
    for (const p of migrationLambdas(t)) {
      const e = env(p);
      expect(e.MANAGED_KB_TAG_VALUE_PREFIX).toBe('test-project');
      expect(e.MANAGED_KB_TAG_VALUE_ENVIRONMENT).toBe('test');
      // Non-empty is the property that matters: an empty prefix or environment
      // widens the Reconciler's and teardown's scope to the whole account.
      expect(e.MANAGED_KB_TAG_VALUE_PREFIX).not.toBe('');
      expect(e.MANAGED_KB_TAG_VALUE_ENVIRONMENT).not.toBe('');
    }
  });

  it('resolves a non-empty environment tag value even with no Environment tag configured', () => {
    // This value is a FILTER for the Reconciler and for teardown. An
    // empty string would widen both to every environment in the
    // account, so the fallback is mandatory rather than cosmetic.
    const noTags = createMockConfig({ tags: {} });
    expect(managedKbEnvironmentTagValue(noTags)).toBe('nonprod');
    const prod = createMockConfig({ tags: {}, production: true });
    expect(managedKbEnvironmentTagValue(prod)).toBe('prod');
    // A configured Environment tag still wins.
    expect(managedKbEnvironmentTagValue(createMockConfig())).toBe('test');
  });

  it('never supplies an owner tag VALUE from CDK, and leaks no PII', () => {
    // Requirement 20.12: the owner tag must be an opaque identifier.
    // CDK cannot know it — it is per-knowledge-base and only exists at
    // CreateKnowledgeBase time — so CDK must supply the KEY and nothing
    // else. An email address here would be written into Cost Explorer,
    // CloudTrail and every ListTagsForResource caller in the account,
    // where unlike a database column it cannot be scrubbed afterwards.
    for (const p of migrationLambdas(t)) {
      const e = env(p);
      expect(e.MANAGED_KB_TAG_VALUE_OWNER_USER_ID).toBeUndefined();
      for (const [key, value] of Object.entries(e)) {
        if (!key.startsWith('MANAGED_KB_TAG_')) continue;
        expect(value).not.toContain('@');
      }
    }
  });
});

// ============================================================
// 2.5 — account-level alarms
// ============================================================

describe('Managed_KB account-level alarms (task 2.5)', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  it('creates exactly the four required alarms', () => {
    t.resourceCountIs('AWS::CloudWatch::Alarm', 4);
  });

  it('every alarm treats missing data as NOT breaching', () => {
    // The metrics do not exist until backend task 14.1 publishes them.
    // An alarm that screamed INSUFFICIENT_DATA from creation would be
    // muted within a week and worth nothing when it finally had
    // something to say.
    const alarms = Object.values(t.findResources('AWS::CloudWatch::Alarm'));
    expect(alarms).toHaveLength(4);
    for (const a of alarms) {
      expect((a.Properties as { TreatMissingData?: string }).TreatMissingData).toBe('notBreaching');
    }
  });

  it('alarms on total managed storage against the configured GB threshold', () => {
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-total-storage',
      MetricName: 'KbStorageGB',
      Namespace: METRIC_NAMESPACE,
      Threshold: 500,
    });
  });

  it('alarms on knowledge base count at 80% of the 10,000 quota', () => {
    // 80%, not 100%: the quota is adjustable but an increase takes lead
    // time, so the alarm has to fire while there is headroom to file the
    // request.
    expect(MANAGED_KB_ACCOUNT_QUOTA * MANAGED_KB_COUNT_ALARM_FRACTION).toBe(8000);
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-count',
      MetricName: 'KbCount',
      Namespace: METRIC_NAMESPACE,
      Threshold: 8000,
      ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    });
  });

  it('alarms on daily Knowledge-Base usagetype cost', () => {
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-daily-cost',
      MetricName: 'KbDailyCostUsd',
      Namespace: METRIC_NAMESPACE,
      Threshold: 100,
      Period: 86400,
    });
  });

  it('reads the cost metric from our own namespace, never AWS/Billing', () => {
    // AWS/Billing exists only in us-east-1 and a CloudWatch alarm
    // cannot read a metric from another region, so a billing-namespace
    // alarm would sit at INSUFFICIENT_DATA forever in a us-west-2
    // deployment. Also guards the reserved-namespace trap: CloudWatch
    // rejects PutMetricData into any `AWS...` namespace.
    const namespaces = Object.values(t.findResources('AWS::CloudWatch::Alarm')).map(
      (a) => (a.Properties as { Namespace?: string }).Namespace,
    );
    expect(namespaces).toHaveLength(4);
    for (const ns of namespaces) {
      expect(ns).toBe(METRIC_NAMESPACE);
      expect(ns!.startsWith('AWS')).toBe(false);
    }
  });

  it('alarms on a SUSTAINED non-zero orphan count', () => {
    // Threshold 0 with GreaterThanThreshold, so any orphan counts — but
    // three consecutive daily runs, because one orphan on one day is a
    // crashed create that self-heals, while the same finding three days
    // running means the delete saga is leaking and each leak bills.
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-orphans',
      MetricName: 'KbOrphansFound',
      Threshold: 0,
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 3,
    });
  });

  it('scales the storage and cost thresholds with config', () => {
    const t2 = synth({ storageAlarmGb: 42, dailyCostAlarmUsd: 7 });
    t2.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-total-storage',
      Threshold: 42,
    });
    t2.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'test-project-managed-kb-daily-cost',
      Threshold: 7,
    });
  });
});

// ============================================================
// IAM — least privilege across the four functions
// ============================================================

describe('KbMigrationConstruct — IAM', () => {
  let t: Template;
  beforeAll(() => {
    t = synth();
  });

  /** Logical ids of the roles holding a statement with `sid`. */
  function roleIdsWithSid(sid: string): string[] {
    const out: string[] = [];
    for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
      for (const r of Object.values(t.findResources(type))) {
        const props = r.Properties as {
          PolicyDocument?: { Statement?: PolicyStatement[] };
          Roles?: Array<{ Ref?: string }>;
        };
        if (!(props.PolicyDocument?.Statement ?? []).some((s) => s.Sid === sid)) continue;
        out.push(...(props.Roles ?? []).map((x) => x.Ref).filter((x): x is string => !!x));
      }
    }
    return out;
  }

  it('reuses the task 1.2 provisioning grant for the worker and reconciler only', () => {
    // Attached through ManagedKbRoleConstruct rather than re-declared,
    // so the confused-deputy conditions have exactly one definition
    // (asserted in managed-kb.test.ts).
    const holders = roleIdsWithSid('ManagedKbProvisionCrud');
    expect(holders).toHaveLength(2);
    expect(holders.some((id) => /WorkerLambdaServiceRole/.test(id))).toBe(true);
    expect(holders.some((id) => /ReconcilerLambdaServiceRole/.test(id))).toBe(true);
    // The dispatcher reads an index and invokes the worker; it must
    // never be able to create or delete a knowledge base.
    expect(holders.some((id) => /DispatcherLambdaServiceRole/.test(id))).toBe(false);
  });

  it('reuses the task 1.2 PassRole grant on the same two roles', () => {
    const holders = roleIdsWithSid('ManagedKbPassServiceRole');
    expect(holders).toHaveLength(2);
    expect(holders.some((id) => /DispatcherLambdaServiceRole/.test(id))).toBe(false);
    expect(holders.some((id) => /IngestionConsumerLambdaServiceRole/.test(id))).toBe(false);
  });

  it('reuses the task 1.2 direct-ingestion grant for the worker and ingestion consumer', () => {
    const holders = roleIdsWithSid('ManagedKbDirectIngestion');
    expect(holders).toHaveLength(2);
    expect(holders.some((id) => /WorkerLambdaServiceRole/.test(id))).toBe(true);
    expect(holders.some((id) => /IngestionConsumerLambdaServiceRole/.test(id))).toBe(true);
  });

  it('gives both ingesting roles bedrock:StartIngestionJob, not just the Ingest action', () => {
    // Regression for the defect that failed a real upload in dev. AWS
    // authorizes `IngestKnowledgeBaseDocuments` under the adjacent action
    // name `bedrock:StartIngestionJob`, so a grant carrying only the
    // matching name deploys and reviews clean, then returns
    // AccessDeniedException on the first document.
    //
    // Both roles are checked because both call
    // `ingest_knowledge_base_documents`: the ingestion consumer surfaced it,
    // and the worker had the identical gap — invisible until now only
    // because every migration so far was driven locally under a broader SSO
    // identity than the Lambda role.
    const statements = allStatements(t).filter((s) => s.Sid === 'ManagedKbDirectIngestion');
    expect(statements).toHaveLength(2);
    for (const s of statements) {
      expect(s.Action).toContain('bedrock:StartIngestionJob');
      expect(s.Action).toContain('bedrock:IngestKnowledgeBaseDocuments');
    }
  });

  it('gives the dispatcher namespace-conditioned metrics and nothing Bedrock-shaped', () => {
    const statements = allStatements(t).filter((s) => s.Sid === 'ManagedKbDispatchMetrics');
    expect(statements).toHaveLength(1);
    expect(statements[0].Action).toBe('cloudwatch:PutMetricData');
    expect(statements[0].Condition).toEqual({
      StringEquals: { 'cloudwatch:namespace': METRIC_NAMESPACE },
    });
  });

  it('grants the dispatcher NOTHING outside its whitelist, by any SID', () => {
    // The assertion above pins the shape of the metrics statement, but a
    // rogue grant does not have to arrive inside it: an extra
    // `addToRolePolicy` with a fresh SID, or a `grantProvisioning` call
    // wired to the wrong role, adds a SEPARATE statement that a
    // SID-targeted assertion cannot see. Verified by mutation — adding an
    // inline `bedrock:Retrieve` + `bedrock:CreateKnowledgeBase` statement
    // to the dispatcher role left every other test in this file green.
    //
    // So enumerate the whole role instead of a named statement, and
    // whitelist rather than blacklist: a blacklist only catches the
    // over-grant somebody already thought of. The dispatcher reads an
    // index, invokes the worker, and reports a metric. It must never be
    // able to create, delete, read from or write to a knowledge base,
    // never pass the service role, and never touch user documents in S3.
    const allowedPrefixes = [
      'dynamodb:', // KB_Records + the sparse KbWorkIndex sweep
      'lambda:InvokeFunction', // async-invoke the worker
      'cloudwatch:PutMetricData', // dispatch metrics
      'ecr:', // pull the real image after the out-of-band swap
      'logs:', // basic execution
      'xray:', // if tracing is ever enabled
    ];

    const actions = statementsForRole(t, /KbMigrationDispatcherLambdaServiceRole/).flatMap((s) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]).filter(
        (a): a is string => typeof a === 'string',
      ),
    );

    // Sanity: the enumeration found the role at all, so a logical-id
    // rename cannot turn this into a vacuous pass.
    expect(actions).toContain('lambda:InvokeFunction');
    expect(actions).toContain('cloudwatch:PutMetricData');

    const unexpected = actions.filter(
      (a) => !allowedPrefixes.some((p) => a === p || a.startsWith(p)),
    );
    expect(unexpected).toEqual([]);

    // Stated explicitly as well, so the failure message names the risk
    // rather than just listing a diff.
    for (const action of actions) {
      expect(action.startsWith('bedrock')).toBe(false);
      expect(action).not.toBe('iam:PassRole');
      expect(action.startsWith('s3:')).toBe(false);
    }
  });

  it('lets the dispatcher invoke the worker', () => {
    t.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({ Action: 'lambda:InvokeFunction', Effect: 'Allow' }),
        ]),
      }),
    });
  });

  it('gives the worker and consumer READ-only access to the documents bucket', () => {
    // Source bytes for re-ingestion and the S3 HEAD byte accounting
    // sizes documents with (Requirement 12.3). Nothing here writes user
    // documents, and a write grant would let a migration corrupt the
    // only copy of a corpus it is meant to be copying.
    const docStatements = allStatements(t).filter((s) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.some((a) => typeof a === 'string' && a.startsWith('s3:'));
    });
    expect(docStatements.length).toBeGreaterThan(0);
    const allS3Actions = docStatements.flatMap((s) =>
      (Array.isArray(s.Action) ? s.Action : [s.Action]) as string[],
    );
    expect(allS3Actions).not.toContain('s3:PutObject');
    expect(allS3Actions).not.toContain('s3:DeleteObject');
  });
});

// ============================================================
// PlatformStack wiring — additive, existing notification intact
// ============================================================

describe('KbMigration wiring on PlatformStack', () => {
  let t: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: {
        shareInboxEnabled: false, retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
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

  it('creates all four migration Lambdas in the platform stack', () => {
    expect(migrationLambdas(t)).toHaveLength(4);
  });

  it('keeps the existing rag-ingestion ObjectCreated notification intact', () => {
    // The hard constraint on task 2.2. S3 rejects two notification
    // configurations with overlapping prefixes for the same event type,
    // so the new consumer is routed via EventBridge instead — which is a
    // SEPARATE field of the notification configuration. The legacy
    // LambdaFunctionConfiguration must therefore survive untouched:
    // dropping it would silently stop every upload from being indexed.
    const notifications = Object.values(t.findResources('Custom::S3BucketNotifications'));
    const ragNotification = notifications.find((n) => {
      const cfg = (n.Properties as { NotificationConfiguration?: unknown }).NotificationConfiguration;
      return JSON.stringify(cfg).includes('LambdaFunctionConfigurations');
    });
    expect(ragNotification).toBeDefined();
    const cfg = (ragNotification!.Properties as {
      NotificationConfiguration: {
        LambdaFunctionConfigurations?: Array<{ Events?: string[]; Filter?: unknown }>;
        EventBridgeConfiguration?: unknown;
      };
    }).NotificationConfiguration;

    // Exactly one Lambda subscription, still ObjectCreated + assistants/.
    expect(cfg.LambdaFunctionConfigurations).toHaveLength(1);
    expect(cfg.LambdaFunctionConfigurations![0].Events).toEqual(['s3:ObjectCreated:*']);
    expect(JSON.stringify(cfg.LambdaFunctionConfigurations![0].Filter)).toContain('assistants/');

    // And EventBridge delivery ADDED alongside it, not instead of it.
    expect(cfg.EventBridgeConfiguration).toBeDefined();
  });

  it('publishes the four kb-migration function names for the code-deploy step', () => {
    for (const slug of ['dispatcher', 'worker', 'reconciler', 'ingestion-consumer']) {
      t.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/test-project/kb-migration/${slug}-function-name`,
      });
    }
  });

  it('never reads the service-role ARN back from the SSM path this stack publishes', () => {
    // Passing the ManagedKbRoleConstruct by ref is not a style choice:
    // CloudFormation resolves AWS::SSM::Parameter::Value template
    // parameters BEFORE any stack resource exists, so a same-stack read
    // is unsatisfiable on first deploy.
    const params = t.toJSON().Parameters ?? {};
    for (const param of Object.values(params) as Array<Record<string, unknown>>) {
      const type = param.Type as string | undefined;
      if (!type?.startsWith('AWS::SSM::Parameter::Value')) continue;
      expect(param.Default).not.toBe('/test-project/managed-kb/service-role-arn');
    }
  });
});
