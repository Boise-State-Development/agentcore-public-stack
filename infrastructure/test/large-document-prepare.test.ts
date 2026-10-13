/**
 * Large document attachments, PR-1 (docs/specs/large-document-attachments.md
 * §4C). Pinned:
 *   - CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED is opt-in ('' and junk are off);
 *   - off, nothing is synthesized: no queue, no Lambda, no alarm, no queue URL;
 *   - on, it costs exactly 11 resources in a stack near CloudFormation's 500;
 *   - the Lambda reads any original under `user-files/` but writes and deletes
 *     only `_derived/` sidecars, and is fed one message at a time;
 *   - app-api, and only app-api, gets the flag, the queue URL and a send grant;
 *     the Runtime's environment budget is untouched either way;
 *   - the deploy script can find the function (SSM), crashes and dead letters
 *     alarm, and the bootstrap image carries the handler's module.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig, loadConfig } from '../lib/config';
import {
  LARGE_DOCUMENT_PREPARE_ESM,
  LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT,
  LARGE_DOCUMENT_PREPARE_MEMORY_MB,
  LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES,
} from '../lib/constructs/large-document/large-document-prepare-construct';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';

const FLAG_ENV = 'CDK_LARGE_DOCUMENT_ATTACHMENTS_ENABLED';
const BOOTSTRAP_DIR = path.resolve(__dirname, '..', 'bootstrap-assets', 'large-document-prepare');

const BASE: Record<string, unknown> = {
  projectPrefix: 'test-project',
  awsRegion: 'us-west-2',
  awsAccount: '123456789012',
  vpcCidr: '10.0.0.0/16',
  corsOrigins: 'http://localhost:4200',
  production: false,
  retainDataOnDelete: false,
  frontend: { cloudFrontPriceClass: 'PriceClass_100' },
  appApi: { cpu: 1024, memory: 2048, desiredCount: 1, maxCapacity: 2 },
  inferenceApi: {},
  fineTuning: {},
  artifacts: { retentionDays: 90, extraFrameAncestors: [] },
  mcpSandbox: { extraFrameAncestors: [] },
  ragIngestion: {
    additionalCorsOrigins: '',
    lambdaMemorySize: 10240,
    lambdaTimeout: 900,
    embeddingModel: 'amazon.titan-embed-text-v2',
    vectorDimension: 1024,
    vectorDistanceMetric: 'cosine',
  },
};

function load(ctx: Record<string, unknown> = {}): AppConfig {
  const app = new cdk.App();
  for (const [k, v] of Object.entries({ ...BASE, ...ctx })) app.node.setContext(k, v);
  return loadConfig(app);
}

function synth(overrides: Partial<AppConfig> = {}): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

interface PolicyStatement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, unknown>;
}

const actions = (s: PolicyStatement): string[] => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']);

function runtimeEnv(template: Template): Record<string, unknown> {
  const runtimes = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime'));
  expect(runtimes).toHaveLength(1);
  return (runtimes[0] as any).Properties.EnvironmentVariables;
}

function appApiEnv(template: Template): Record<string, unknown> {
  const env: Record<string, unknown> = {};
  for (const td of Object.values(template.findResources('AWS::ECS::TaskDefinition')) as any[]) {
    for (const container of td.Properties.ContainerDefinitions ?? []) {
      for (const entry of container.Environment ?? []) env[entry.Name] = entry.Value;
    }
  }
  return env;
}

function policyStatements(template: Template, idPattern: RegExp): PolicyStatement[] {
  return Object.entries(template.findResources('AWS::IAM::Policy'))
    .filter(([id]) => idPattern.test(id))
    .flatMap(([, r]) => (r as any).Properties.PolicyDocument.Statement as PolicyStatement[]);
}

describe('largeDocumentAttachments config', () => {
  const saved = { ...process.env };
  let log: jest.SpyInstance;
  let warn: jest.SpyInstance;

  beforeEach(() => {
    delete process.env[FLAG_ENV];
    log = jest.spyOn(console, 'log').mockImplementation(() => undefined);
    warn = jest.spyOn(console, 'warn').mockImplementation(() => undefined);
  });
  afterEach(() => {
    process.env = { ...saved };
    log.mockRestore();
    warn.mockRestore();
  });

  it('is off by default', () => {
    expect(load().largeDocumentAttachments.enabled).toBe(false);
  });

  it.each(['', 'false', 'yes', '1'])('treats %j as off', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().largeDocumentAttachments.enabled).toBe(false);
  });

  it.each(['true', ' TRUE '])('turns on for %j', (value) => {
    process.env[FLAG_ENV] = value;
    expect(load().largeDocumentAttachments.enabled).toBe(true);
  });

  it('falls back to context, and the variable beats it', () => {
    expect(load({ largeDocumentAttachments: { enabled: true } }).largeDocumentAttachments.enabled).toBe(true);
    process.env[FLAG_ENV] = 'false';
    expect(load({ largeDocumentAttachments: { enabled: true } }).largeDocumentAttachments.enabled).toBe(false);
  });
});

describe('large-document preparation in PlatformStack', () => {
  const off = synth();
  const on = synth({ largeDocumentAttachments: { enabled: true } });

  function prepareFunction(): Record<string, any> {
    const matches = Object.entries(on.findResources('AWS::Lambda::Function'))
      .filter(([id]) => id.includes('LargeDocumentPrepareLambda'))
      .map(([, r]) => (r as any).Properties);
    expect(matches).toHaveLength(1);
    return matches[0];
  }

  function queues(template: Template): Record<string, any> {
    return Object.fromEntries(
      Object.values(template.findResources('AWS::SQS::Queue'))
        .map((q: any) => [q.Properties.QueueName, q.Properties])
        .filter(([name]) => typeof name === 'string' && name.includes('large-document-prepare')),
    );
  }

  describe('while off', () => {
    it('synthesizes nothing', () => {
      const ids = Object.keys(off.toJSON().Resources).filter((id) => /LargeDocumentPrepare/.test(id));
      expect(ids).toEqual([]);
      expect(queues(off)).toEqual({});
    });

    it('gives app-api the flag off and no queue', () => {
      const env = appApiEnv(off);
      expect(env.LARGE_DOCUMENT_ATTACHMENTS_ENABLED).toBe('false');
      expect(env).not.toHaveProperty('LARGE_DOCUMENT_PREPARE_QUEUE_URL');
    });
  });

  describe('while on', () => {
    it('costs exactly 11 resources', () => {
      // PlatformStack is near CloudFormation's 500-resource limit, so the
      // number a deployment pays to turn this on is pinned: two queues, the
      // queue's TLS policy, log group, function, role, its policy, the event
      // source mapping, the function-name parameter and two alarms.
      const before = new Set(Object.keys(off.toJSON().Resources));
      const added = Object.entries(on.toJSON().Resources)
        .filter(([id]) => !before.has(id))
        .map(([, r]) => (r as any).Type)
        .sort();
      expect(added).toEqual([
        'AWS::CloudWatch::Alarm',
        'AWS::CloudWatch::Alarm',
        'AWS::IAM::Policy',
        'AWS::IAM::Role',
        'AWS::Lambda::EventSourceMapping',
        'AWS::Lambda::Function',
        'AWS::Logs::LogGroup',
        'AWS::SQS::Queue',
        'AWS::SQS::Queue',
        'AWS::SQS::QueuePolicy',
        'AWS::SSM::Parameter',
      ]);
    });

    it('queues with a dead-letter queue and a visibility timeout of six function timeouts', () => {
      const q = queues(on);
      const main = q[`${MOCK_PREFIX}-large-document-prepare`];
      expect(main).toBeDefined();
      expect(q[`${MOCK_PREFIX}-large-document-prepare-dlq`]).toBeDefined();
      expect(main.VisibilityTimeout).toBe(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES * 60 * 6);
      expect(main.RedrivePolicy.maxReceiveCount).toBe(LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT);
      expect(main.SqsManagedSseEnabled).toBe(true);
    });

    it('runs an arm64 image function sized from the measurement', () => {
      const fn = prepareFunction();
      expect(fn.PackageType).toBe('Image');
      expect(fn.Architectures).toEqual(['arm64']);
      expect(fn.MemorySize).toBe(LARGE_DOCUMENT_PREPARE_MEMORY_MB);
      expect(fn.Timeout).toBe(LARGE_DOCUMENT_PREPARE_TIMEOUT_MINUTES * 60);
      const env = fn.Environment.Variables;
      expect(env.LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT).toBe(String(LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT));
      expect(env.DYNAMODB_USER_FILES_TABLE_NAME).toBeDefined();
      expect(env.S3_USER_FILES_BUCKET_NAME).toBeDefined();
      expect(env.WORD_TRACKED_CHANGES_ENABLED).toBe('false');
    });

    it('matches the DOCX text layer to the Runtime by carrying the tracked-changes flag', () => {
      const both = synth({ largeDocumentAttachments: { enabled: true }, wordTrackedChanges: { enabled: true } });
      const fn = Object.entries(both.findResources('AWS::Lambda::Function'))
        .find(([id]) => id.includes('LargeDocumentPrepareLambda'))![1] as any;
      expect(fn.Properties.Environment.Variables.WORD_TRACKED_CHANGES_ENABLED).toBe('true');
    });

    it('takes one message at a time and reports item failures', () => {
      on.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
        BatchSize: LARGE_DOCUMENT_PREPARE_ESM.BATCH_SIZE,
        ScalingConfig: { MaximumConcurrency: LARGE_DOCUMENT_PREPARE_ESM.MAX_CONCURRENCY },
        FunctionResponseTypes: ['ReportBatchItemFailures'],
      });
      expect(LARGE_DOCUMENT_PREPARE_ESM.BATCH_SIZE).toBe(1);
    });

    it('reads originals but writes and deletes only _derived/ sidecars', () => {
      const statements = policyStatements(on, /LargeDocumentPrepareLambdaServiceRoleDefaultPolicy/);
      const s3Writes = statements.filter((s) => actions(s).some((a) => /^s3:(Put|Delete)/.test(a)));
      expect(s3Writes).toHaveLength(1);
      expect(actions(s3Writes[0]).sort()).toEqual(['s3:DeleteObject', 's3:PutObject']);
      expect(JSON.stringify(s3Writes[0].Resource)).toContain('/user-files/*/_derived/*');

      const reads = statements.filter((s) => actions(s).includes('s3:GetObject'));
      expect(reads).toHaveLength(1);
      expect(JSON.stringify(reads[0].Resource)).toContain('/user-files/*');

      const list = statements.find((s) => actions(s).includes('s3:ListBucket'))!;
      expect(list.Condition).toEqual({ StringLike: { 's3:prefix': ['user-files/*'] } });

      const ddb = statements.filter((s) => actions(s).some((a) => a.startsWith('dynamodb:')));
      expect(ddb).toHaveLength(1);
      expect(actions(ddb[0]).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:UpdateItem']);

      const bedrock = statements.filter((s) => actions(s).some((a) => a.startsWith('bedrock:')));
      expect(bedrock).toHaveLength(1);
      expect(actions(bedrock[0])).toEqual(['bedrock:InvokeModel']);
      expect(JSON.stringify(bedrock[0].Resource)).toContain('nova-micro');

      const wildcardWrites = statements.filter(
        (s) => actions(s).some((a) => a === '*' || a === 's3:*' || a === 'dynamodb:*'),
      );
      expect(wildcardWrites).toEqual([]);
    });

    it('gives app-api the flag, the queue URL and a send grant on the queue', () => {
      const env = appApiEnv(on);
      expect(env.LARGE_DOCUMENT_ATTACHMENTS_ENABLED).toBe('true');
      expect(JSON.stringify(env.LARGE_DOCUMENT_PREPARE_QUEUE_URL)).toMatch(/LargeDocumentPrepareQueue/);

      const queuePolicy = Object.values(on.findResources('AWS::SQS::QueuePolicy')).find((p: any) =>
        JSON.stringify(p.Properties.Queues).includes('LargeDocumentPrepareQueue'),
      ) as any;
      const sends = (queuePolicy.Properties.PolicyDocument.Statement as any[]).filter(
        (s) => s.Effect === 'Allow' && actions(s).includes('sqs:SendMessage'),
      );
      expect(sends).toHaveLength(1);
      expect(actions(sends[0])).toEqual(['sqs:SendMessage']);
      expect(JSON.stringify(sends[0].Principal)).toMatch(/AppApi.*TaskRole/);
    });

    it('leaves the app-api role policies, which overflow near the IAM size limit, untouched', () => {
      // The role already spills into CDK overflow managed policies; a send
      // grant there pushed the fullest past IAM's 6,144 characters.
      const docs = (t: Template) =>
        Object.entries(t.toJSON().Resources)
          .filter(([id, r]: [string, any]) =>
            /AppApi.*TaskRole/.test(id) && ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'].includes(r.Type))
          .map(([id, r]: [string, any]) => [id, JSON.stringify(r.Properties.PolicyDocument)]);
      expect(docs(on)).toEqual(docs(off));
    });

    it('spends none of the Runtime environment budget, on or off', () => {
      for (const template of [off, on]) {
        const keys = Object.keys(runtimeEnv(template));
        expect(keys.filter((k) => k.startsWith('LARGE_DOCUMENT'))).toEqual([]);
      }
    });

    it('alarms on crashes and on dead letters, routed to the alarm topic when there is one', () => {
      on.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: `${MOCK_PREFIX}-lambda-large-document-prepare-errors`,
        MetricName: 'Errors',
        Namespace: 'AWS/Lambda',
        Threshold: 0,
      });
      on.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: `${MOCK_PREFIX}-dlq-large-document-prepare-not-empty`,
        Namespace: 'AWS/SQS',
        MetricName: 'ApproximateNumberOfMessagesVisible',
        Threshold: 0,
      });
      const names = Object.values(on.findResources('AWS::CloudWatch::Alarm')).map(
        (a: any) => a.Properties.AlarmName,
      );
      expect(names.filter((n) => /large-document-prepare-throttles/.test(n))).toEqual([]);
    });

    it('publishes the function name for the code-deploy step', () => {
      on.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/${MOCK_PREFIX}/large-document-prepare/function-name`,
        Value: Match.objectLike({ Ref: Match.stringLikeRegexp('LargeDocumentPrepareLambda') }),
      });
    });
  });
});

describe('bootstrap image', () => {
  it('carries a stub at the real handler path that returns every message to the queue', () => {
    const dockerfile = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'Dockerfile'), 'utf8');
    expect(dockerfile).toContain('CMD ["handler.lambda_handler"]');
    const handler = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'handler.py'), 'utf8');
    expect(handler).toContain('def lambda_handler');
    expect(handler).toContain('batchItemFailures');

    const real = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'backend', 'Dockerfile.large-document-prepare'),
      'utf8',
    );
    expect(real).toContain('CMD ["handler.lambda_handler"]');
    expect(real).toContain('${LAMBDA_TASK_ROOT}/handler.py');
  });
});
